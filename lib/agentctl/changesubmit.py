# agentctl, part `changesubmit`: a change submission: validated, queued, the older ones it supersedes, the store's grace after downtime
# A real module (lib/agentctl/__init__.py MODULES): its own imports, the standard library's and MODULES' only.

import os
import re
from typing import Any
from agentctl.statemachines import StaleVersion, TerminalState, open_items
from agentctl.filelock import fcntl
from agentctl.ids import stamp
from agentctl.timefmt import iso_epoch, now, ts
from agentctl.repos import server_repo
from agentctl.trailers import do_not_land
from agentctl.waitingdir import ChangeRecords, entry_queued_since, waiting_state
from agentctl.conflicts import CONFLICT_PLACE_KEPT
from agentctl.queuerecheck import same_change_source
from agentctl.waiting import supersede
from agentctl.queuesubmit import SUBMIT_FIELDS
from agentctl.blamerecords import blame_land_refusal


def store_grace(st, now_=None):
    """§6.2 'Store downtime grace': at the service's start every live lease is extended by the time the store was down
    (meta last_alive), so a store restart never expires everyone at once. Returns the gap applied."""
    now_ = now_ if now_ is not None else st.clock()
    last = float(st.meta_get("last_alive", 0) or 0)
    gap = now_ - last if last else 0
    if gap > 60:
        c = st.db()
        try:
            c.execute("UPDATE leases SET expires_at=expires_at+?, max_until=max_until+? WHERE holder!='' AND expires_at>?",
                      (gap, gap, last))
        finally:
            c.close()
    st.meta_set("last_alive", now_)
    return gap if gap > 60 else 0


def _conflict_rejected_at(last: Any) -> Any:
    """Epoch of the entry's rejection for a conflict; None when it was not rejected for one (or is no entry)."""
    if not last or last.get("status") != "rejected" or last.get("conflict_files") is None:
        return None
    return iso_epoch(last.get("decided"))


def conflict_bounced(every, e, now_=None):
    """The newest entry of e's worktree and branch, when it was rejected for a conflict within CONFLICT_PLACE_KEPT s
    (and nothing of that source was queued since), or is still held for its regeneration (queueclose.py regen_hold): the
    place e takes. None otherwise."""
    mine = [x for x in every if x["id"] != e["id"] and same_change_source(x, e["worktree"], e.get("branch"))]
    last = max(mine, key=lambda x: x["seq"], default=None)
    if last and last.get("regen_hold") and last.get("status") == "queued":
        return last
    at = _conflict_rejected_at(last)
    if at is None:
        return None
    return last if (now_ or now().timestamp()) - at <= CONFLICT_PLACE_KEPT else None


def supersede_older(st: Any, d: Any, e: Any) -> None:
    """RE-QUEUEING REPLACES: only the newest entry per worktree and branch runs."""
    for old in ChangeRecords(st, d).queued():
        if old["id"] != e["id"] and same_change_source(old, e["worktree"], e.get("branch")):
            supersede(st, d, old, e["id"], f"{e['worktree']} queued again ({e['head'][:10]}); only the newest entry runs")


def queue_change(st, d, fields, entry_id=None):
    """A submitted change as a queued change: its item (carrying its record) made at once, then its entry's file
    (ChangeRecords.save); its number from the store's counter (ChangeRecords.draw_seq, under seq.lock: one submission
    at a time per queue); re-queueing from the same source supersedes the older one. Returns (entry, n ahead). Shared by
    `land` (phase 1) and POST /v1/changes (phase 2)."""
    recs = ChangeRecords(st, d)
    with open(os.path.join(d, "seq.lock"), "a+") as seq_lock:  # never the landing lock (a push holds it)
        fcntl.flock(seq_lock, fcntl.LOCK_EX)
        known = recs.get(entry_id) if entry_id else None
        if known:  # a submission answered again: the change it made
            return known, max(len(recs.queued()) - 1, 0)
        seq = recs.draw_seq()
        eid = entry_id or f"{stamp('entry')}-{os.path.basename(fields['worktree'])}-{seq}"
        e = {**fields, "id": eid, "seq": seq, "drawn": seq, "status": "queued", "created": ts()}
        bounced = conflict_bounced(recs.every(), e)
        if bounced:  # A RESOLVED CONFLICT KEEPS ITS PLACE -- and its queue age (the store item's submitted_at)
            e.update(seq=bounced["seq"], requeued_from=bounced["id"],
                     queued_since=entry_queued_since(bounced) or entry_queued_since(e))
        recs.save(e)
        supersede_older(st, d, e)
    return e, max(len(recs.queued()) - 1, 0)


def check_submission(repo: str, body: dict[str, Any]) -> None:
    """Raise ValueError unless body is a well-formed change submission and repo is cloned."""
    if not os.path.isdir(repo):
        raise ValueError(f"the reconciler has not cloned {repo} yet")
    missing = [k for k in ("worktree", "head", "commits") if not body.get(k)]
    if missing or not re.fullmatch(r"[0-9a-f]{40}", str(body.get("head"))):
        raise ValueError(f"a change needs worktree, head (a full sha) and commits; missing or bad: {missing or ['head']}")
    if body.get("id") and not re.fullmatch(r"[\w.-]{1,120}", body["id"]):
        raise ValueError("a bad change id")


def submit_change(st, body, repo=None):
    """POST /v1/changes: a change the laptop pushed to refs/heads/land/<id> becomes a queued entry of the server's queue."""
    repo = repo or server_repo()
    check_submission(repo, body)
    commits = body["commits"] if isinstance(body["commits"], list) else str(body["commits"]).split()
    why = do_not_land(repo, commits, body.get("branch") or "") or blame_land_refusal(repo, commits, st, body.get("worktree"))
    if why:
        raise ValueError(f"NOT LANDED: {why}")
    d = waiting_state(repo)[0]
    e, ahead = queue_change(st, d, {k: body.get(k) for k in SUBMIT_FIELDS}, body.get("id"))
    supersede_stranded(d, e, st)
    return {"id": e["id"], "seq": e["seq"], "ahead": ahead}


def supersede_stranded(d, e, st):
    """A submission replaces every older open item of the same worktree that no entry carries (queue_change supersedes
    the entries; an item mirrored from another host's queue has none): SUPERSEDED, never left waiting."""
    carried = {x["id"] for x in ChangeRecords(st, d).every()}
    for it in open_items(st, "change"):
        dd = it.get("data") or {}
        if it["id"] in carried or \
                os.path.realpath(dd.get("worktree") or "") != os.path.realpath(e.get("worktree") or ""):
            continue
        try:
            st.transition("change", it["id"], "superseded", it["version"], data={"superseded_by": e["id"]},
                          event={"cause": "superseded", "text": f"SUPERSEDED {it['id']} by {e['id']}: {e['worktree']} submitted again",
                                 "refs": {"entry": it["id"]}})
        except (StaleVersion, TerminalState):
            pass
