# agentctl, part `queueclose`: one queue closed by content: bounces, regen-only rows, overdue regen holds.
# A real module (lib/agentctl/__init__.py MODULES): its own imports, the standard library's and MODULES' only.

from agentctl.clock import epoch_now
import os
import time
from typing import Any
from contextlib import suppress
from agentctl.timefmt import ts
from agentctl.statefile import read_record
from agentctl.statemachines import StaleVersion, TerminalState, machine_state
from agentctl.clock import Clock
from agentctl.core import cherry_landed, env_num, fmt_age, git, git_ok, host_role, process_cache, remote
from agentctl.repos import movies_repo_path, precheck_hook, waiting_supported
from agentctl.waitingdir import ENTRY_FINAL, save_entry, waiting_entries, waiting_state, wlog
from agentctl.procs import safe_cwd
from agentctl.inboxentry import inbox_write
from agentctl.clones import clone_shared
from agentctl.patches import equivalent_on
from agentctl.queueslots import check_tree_slot, checker_lease, entry_owned, set_change
from agentctl.conditions import open_condition, run_probe
from agentctl.queue import VERIFY_MODE
from agentctl.conflicts import CONFLICT_PLACE_KEPT
from agentctl.changekind import (
    HOLD_SHOWN, change_hold_visible, classify_change, file_unheld_change, kind_text, queue_facts)
from agentctl.waiting import already_landed, must_wait, waiting_commits
from agentctl.queuesubmit import SUBMIT_FIELDS, submit_ref
from agentctl.redseen import gate_reds
from agentctl.gov import gov_class_of
from agentctl.queuebatch import run_entry_checks
from agentctl.queuecheck import (
    adopted_run, check_mode, commits_here, fetched_main, judge_check_failure, requeued_for_infra, round_ended,
    round_head, tip_refused)
from agentctl.queuehold import change_hold_stuck, restart_hold, stuck_recovery


# ONE QUEUE, CLOSED BY CONTENT (2026-10-30 split brain: six pre-cutover entries in the laptop's local waiting dir and
# four store items mirrored from them sat "waiting" for 11 h, every one already on main under rebased shas, and W1
# escalated). A change whose commits are on main BY CONTENT (`git cherry`: landing rebases, so never by sha) is LANDED,
# looked at every LANDED_LOOK_EVERY s per change, every tick: the server's entries and items, and on the laptop the
# store's items its clone has the commits of. The laptop holds no queue of its own: what its local dir still holds
# goes to ci-runner's queue (migrate_local_queue). A store item no queue carries is a STRANDED CHANGE condition.
LANDED_LOOK_EVERY = 600
_landed_looked = process_cache({})


def change_landed(repo, head, commits = None):
    """(landed, via): landed True when the change at head is on main by content (head an ancestor, or every commit of it
    patch-equivalent there or with a content twin, cherry_landed), False when not, None when this clone cannot tell (head unknown here); via the main commit
    carrying its last commit, when known."""
    main = f"refs/remotes/{remote()}/main"
    if not head or not git_ok(repo, "cat-file", "-e", f"{head}^{{commit}}"):
        return None, None
    if git_ok(repo, "merge-base", "--is-ancestor", head, main):
        return True, head
    limit = [f"{commits[0]}^"] if commits and git_ok(repo, "cat-file", "-e", f"{commits[0]}^^{{commit}}") else []
    commits, left = cherry_landed(repo, main, head, *limit)
    if commits is None:
        return None, None
    if not commits or left:
        return False, None
    return True, equivalent_on(repo, head, main)


def landed_look_due(cid, t) -> bool:
    if t - _landed_looked.get(cid, 0) < env_num("AGENTCTL_LANDED_LOOK", LANDED_LOOK_EVERY):
        return False
    _landed_looked[cid] = t
    return True


def close_item_landed(st, cid, why, via = None) -> bool:
    """The store's change item cid, unless terminal (or missing), LANDED with an event saying why; via (the main
    commit carrying it) is its landed sha, which await_entry watches. True when moved."""
    it = st.item("change", cid)
    if not it or machine_state("change", it["state"]).terminal:
        return False
    try:
        st.transition("change", cid, "landed", it["version"], data={"why": why, **({"landed_sha": via} if via else {})},
                      event={"text": f"LANDED {cid}: {why}", "refs": {"entry": cid}})
        return True
    except (StaleVersion, TerminalState):
        return False


def _close_landed_entries(repo: Any, st: Any, d: Any, t: Any, did: list[str]) -> set[str]:
    """The queue entries of d on main by content close as LANDED (lines to did); the ids of the open ones the entries
    cover, whose store items are not looked at again."""
    carried: set[str] = set()
    for e in waiting_entries(d, open_only=False) if d else []:
        if e.get("status") in ENTRY_FINAL or e.get("status") == "pushing":
            continue
        carried.add(e["id"])
        landed, via = change_landed(repo, e.get("head"), e.get("commits")) if landed_look_due(e["id"], t) else (0, 0)
        if landed:
            already_landed(st, d, e, via)
            did.append(f"{e['id']}: its commits are on main (by content): landed")
            carried.discard(e["id"])
    return carried


def close_landed_changes(repo: Any, clock: Any, store: Any, d: Any = None) -> Any:
    """Every open change whose commits are on main by content closes as LANDED: the queue entries of d (with their
    items), then the store's other open items (by their head). Returns what it did."""
    st, t = store, clock.time()
    did: list[str] = []
    carried = _close_landed_entries(repo, st, d, t, did)
    for it in st.items("change"):
        if machine_state("change", it["state"]).terminal or it["state"] == "pushing" or it["id"] in carried:
            continue
        if not landed_look_due(it["id"], t):
            continue
        landed, via = change_landed(repo, (it.get("data") or {}).get("head"))
        if landed and close_item_landed(st, it["id"], "its commits are on main (by content)", via):
            did.append(f"{it['id']}: its commits are on main (by content): landed")
    return did


def stranded_changes(repo, d, store, now_ = None):
    """THE SERVER'S: each open store change item its queue (d) has no entry for -- no source, nothing will ever check,
    push or close it -- opens a STRANDED CHANGE condition naming it; its probe (the item closed, or an entry for it)
    closes it. Returns the ids."""
    st = store
    entries = {e["id"] for e in waiting_entries(d, open_only=False)}
    out = []
    for it in st.items("change"):
        if machine_state("change", it["state"]).terminal or it["id"] in entries:
            continue
        r = run_probe("change_source", {"id": it["id"], "repo": repo})
        if r.ok is False:
            dd = it.get("data") or {}
            open_condition("change-stranded", f"STRANDED CHANGE {it['id']} ({it['state']}, head "
                           f"{str(dd.get('head') or '?')[:10]}, {dd.get('worktree') or '?'}): no queue entry carries it, "
                           "so nothing checks, pushes or closes it. Landed: it closes itself once a clone with its "
                           f"commits sees them on main. Else `agentctl land cancel {it['id']}`, and land it again.",
                           r, [], key=f"change-stranded-{it['id']}", store=st)
            out.append(it["id"])
    return out


def probe_change_source(a, store):
    """The change item is closed, or the server's queue carries it again."""
    it = store.item("change", a["id"])
    if not it or machine_state("change", it["state"]).terminal:
        return True, f"change {a['id']} is {it['state'] if it else 'gone'}"
    d = waiting_state(a.get("repo") or movies_repo_path())[0]
    if os.path.exists(os.path.join(d, "entries", f"{a['id']}.json")):
        return True, f"change {a['id']} is in the queue"
    return False, f"change {a['id']} is {it['state']} and no queue entry carries it"


def _migrate_landed(st: Any, d: Any, e: Any, via: Any) -> str:
    """Entry e's commits are on main (via): it is LANDED, here and in the store. The line saying so."""
    e.update(status="landed", landed_sha=via, decided=ts(), why="its commits are on main (by content)")
    save_entry(d, e)
    close_item_landed(st, e["id"], "its commits are on main (by content)", via)
    return f"{e['id']}: its commits are on main (by content): landed"


def _migration_failure(repo: Any, st: Any, d: Any, e: Any) -> Any:
    """Entry e pushed to refs/heads/land/<id> and submitted to ci-runner's queue: why it could not be (None: moved)."""
    if not submit_ref(repo, d, e, to_server=True):
        return f"could not push refs/heads/land/{e['id']}"
    try:
        st.submit({**{k: e.get(k) for k in SUBMIT_FIELDS}, "id": e["id"]})
    except (Exception, SystemExit) as ex:
        return f"ci-runner's store refused or did not answer ({str(ex)[:160]})"
    return None


def _tell_stranded(d: Any, e: Any, line: str) -> None:
    """The unmoved entry's author hears of it once an hour."""
    if epoch_now() - float(e.get("migrate_told_at") or 0) > 3600:
        e["migrate_told_at"] = epoch_now()
        save_entry(d, e)
        inbox_write("land", line, [line], [], 1, cwd=e.get("worktree") or safe_cwd())


def migrate_local_queue(repo: Any, store: Any) -> Any:
    """THE LAPTOP'S (sync role): its local waiting dir holds no queue. Each open entry there is LANDED when its commits
    are on main by content, else pushed to refs/heads/land/<id> and submitted to ci-runner's queue under its own id
    (the entry: `migrated`). One that cannot move is said loudly (stderr, and its author's inbox once an hour).
    Returns what it did."""
    if host_role() != "sync" or not os.path.isdir(repo):
        return []
    d = waiting_state(repo)[0]
    did: list[str] = []
    for e in waiting_entries(d, open_only=False):
        if e.get("status") in ENTRY_FINAL:
            continue
        landed, via = change_landed(repo, e.get("head"), e.get("commits"))
        if landed:
            did.append(_migrate_landed(store, d, e, via))
            continue
        why = _migration_failure(repo, store, d, e)
        if why is None:
            e.update(status="migrated", decided=ts(), why="moved to ci-runner's queue")
            save_entry(d, e)
            did.append(f"{e['id']}: moved to ci-runner's queue")
            continue
        line = (f"LOCAL QUEUE: {e['id']} ({e.get('worktree')}) is in the laptop's local queue, which nothing runs: {why}; "
                "tried again every sync cycle")
        did.append(line)
        _tell_stranded(d, e, line)
    return did


def _entry_created(i: dict[str, Any]) -> float:
    """When the change's own queue entry was created (a change item from before `entry_created_at`: its queue age)."""
    data = i.get("data") or {}
    return float(data.get("entry_created_at") or data.get("submitted_at") or i.get("entered_at") or 0)


def _change_row(i: dict[str, Any]) -> dict[str, Any]:
    """One open change item as its row of `change_rows`."""
    data = i.get("data") or {}
    return {"id": i["id"], "state": i["state"], "worktree": data.get("worktree"), "head": data.get("head"),
            "deadline": i["deadline"], "level": i["level"],
            "hold": (data.get("hold") or {}).get("why") if i["state"] in HOLD_SHOWN else None,
            "kind": kind_text(data.get("change_kind")) or None,
            "resolved": data.get("autoresolved") if i["state"] == "checking" else None,
            "since": float(data.get("submitted_at") or i.get("entered_at") or 0),
            "created": _entry_created(i)}


def change_rows(store: Any) -> list[dict[str, Any]]:
    """THE QUEUE as everyone reads it (`land status`, the status line, the page): the store's open change items,
    by id, as {id, state, worktree, head, deadline, level, hold, kind (a red fix's, changekind.py), since (submitted: the queue age a kept place
    inherits), created (this entry's own creation)}."""
    rows = [_change_row(i) for i in store.items("change") if not machine_state("change", i["state"]).terminal]
    return sorted(rows, key=lambda r: r["id"])


# AN OVERDUE REGEN HOLD RECOVERS (the owner, 2026-11-20: "1 approve, 2 drop the reject"; lane F2b's 24-h NOT LANDED is
# struck). Past CONFLICT_PLACE_KEPT the change tick runs the regeneration AGAIN: a new round on the change's item
# (`regen_round`), which the laptop's sync answers by starting `agentctl land` in its worktree once more (server.py
# answer_regen_holds, once per change, head and round; it says so in `regen_answered`). When the hold still stands
# REGEN_RECOVERY_WAIT later -- the laptop unreachable, its JVM down, the regen failing -- the owner is told ONCE (an
# escalation on the change: what is held and why, whether the laptop answered). The change stays held in its place.
REGEN_RECOVERY_WAIT = 2 * 3600  # s from the re-run to the owner's one escalation ($AGENTCTL_REGEN_RECOVERY_WAIT)


def _regen_round(e: Any, h: Any, it: Any, t: Any, files: str, cmd: Any) -> Any:
    """The overdue hold's first recovery: a new regeneration round on the change's item. (its update, its event, the text)"""
    rnd = int((it.get("data") or {}).get("regen_round") or 0) + 1
    h["recovery"] = {"at": t, "round": rnd}
    text = (f"REGEN RECOVERY {e['id']}: held {fmt_age(t - float(h['since']))} for its regeneration of {files}; "
            f"running it again (the laptop's sync starts `{cmd}`), round {rnd}")
    return {"regen_round": rnd}, {"kind": "regen.recover", "severity": "info", "text": text}, text


def _regen_escalation(e: Any, h: Any, rec: Any, it: Any, t: Any, files: str, cmd: Any) -> Any:
    """The hold still standing a wait after its re-run: the owner's one escalation. (its update, its event, the text)"""
    answered = int((it.get("data") or {}).get("regen_answered") or 0) >= int(rec.get("round") or 1)
    rec["escalated"] = t
    text = (f"REGEN HELD {e['id']}: held {fmt_age(t - float(h['since']))}: its {files} need a regen only the "
            f"laptop runs (both sides changed a row); run again {fmt_age(t - float(rec['at']))} ago, "
            + ("the laptop started it, yet it still holds" if answered else "the laptop never answered")
            + f" -- the laptop or its JVM is unreachable, or the regen fails: run `{cmd}` there, or "
            f"`agentctl land cancel {e['id']}`")
    return {}, {"kind": "regen.escalate", "severity": "escalate", "text": text}, text


def _recovery_settled(rec: Any, t: Any, wait: Any) -> bool:
    """The recovery was escalated already, or its re-run is within the wait."""
    return bool(rec and (rec.get("escalated") or t - float(rec.get("at") or t) < wait))


def _record_regen_step(st: Any, d: Any, e: Any, it: Any, step: Any, t: Any, wait: Any) -> None:
    """The recovery's step (its update, event and text) kept on the entry and on the change's item, and said."""
    update, ev, text = step
    e["retry_after"] = t + wait
    save_entry(d, e)
    with suppress(StaleVersion):  # recorded on the entry; the item catches up next tick
        if update:
            st.update_item("change", e["id"], it["version"], update)
            it = st.item("change", e["id"])
        st.transition("change", e["id"], it["state"], it["version"], event=ev)
    wlog(d, text)


def _open_change_item(st, eid):
    """The change's item unless it is missing or ended (None)."""
    it = st.item("change", eid)
    return None if not it or machine_state("change", it["state"]).terminal else it


def regen_hold_overdue(st: Any, d: Any, e: Any, t: Any) -> bool:
    """The change tick's step for regen-held entry e (above): False while within its bound (the retry decides), else
    True -- held, never checked again nor rejected -- with the recovery's round or escalation done; lines to `did`."""
    h = e.get("regen_hold") or {}
    if not h or t - float(h.get("since") or t) < CONFLICT_PLACE_KEPT:
        return False
    rec = h.get("recovery") or {}
    wait = env_num("AGENTCTL_REGEN_RECOVERY_WAIT", REGEN_RECOVERY_WAIT)
    if _recovery_settled(rec, t, wait):
        return True
    it = _open_change_item(st, e["id"])
    if not it:
        return True
    files, cmd = ", ".join(h.get("files") or []), h.get("cmd")
    step = _regen_escalation(e, h, rec, it, t, files, cmd) if rec else _regen_round(e, h, it, t, files, cmd)
    e["regen_hold"] = h
    _record_regen_step(st, d, e, it, step, t, wait)
    return True


def check_entry(repo: str, eid: str, clock: Clock, store: Any) -> int:
    """THE EXECUTOR of one change's checks (`waiting check <id>`, detached): holding check/<entry> and a check tree slot
    (leases), it takes the change to checking and leaves it checked (with the tip and base it passed on), rejected (a
    test failure, a conflict) or queued again (infra). It never pushes: the tick does (push_change)."""
    d, _c, _w = waiting_state(repo)
    lease = checker_lease(store, eid)
    if not lease.acquire():  # another checker holds it (and progresses)
        return 0
    own = entry_owned(eid)
    if not own.acquire():
        lease.release()
        return 0
    try:
        e = read_record(os.path.join(d, "entries", f"{eid}.json"))
        if not e or e.get("status") not in ("queued", "checking") or e.get("backlog_batch"):  # a backlog batch's
            return 0
        fix = bool(e.get("fixes")) and classify_change(e, queue_facts(gate_reds(repo), store), clock.time())["red_fix"]
        slot = check_tree_slot(store, repo, d, fix=fix)
        if not slot:  # all trees busy: it stays queued, the tick starts it again when one frees
            return 0
        i, slot_h = slot
        try:
            with clone_shared(repo):  # a clone swap (repair_clone) waits for the check
                return _check_entry(store, repo, d, e, i, clock, own)
        finally:
            slot_h.release()
    finally:
        own.release()
        lease.release()


def _check_entry(st: Any, repo: str, d: str, e: dict[str, Any], i: int, clock: Clock, own: Any = None) -> int:
    """One change's check round: main fetched, the change rebased onto it (or its remote run adopted), refused for its
    blame or an unreplayed fix, the hook's checks run, and the change marked checked. 0: the round is over (checked,
    requeued, landed already), 1: the change is rejected."""
    mark_checking(st, d, e)
    base = round_base(st, repo, d, e, clock)
    if base is None:
        return 0
    rem = e.get("remote") or {}
    adopt = adopted_run(repo, e, rem, base)
    tree, head, via, base, conflicts = round_head(st, d, e, repo, i, base, rem, adopt)
    ended = round_ended(st, d, e, tree, base, head, via, conflicts)
    if ended is not None:
        return ended
    e["tip"] = head
    save_entry(d, e)
    commits, reds = waiting_commits(tree, base, head), gate_reds(repo) if waiting_supported(repo) else set()
    if tip_refused(st, repo, d, e, tree, commits, base):
        return 1
    hook = precheck_hook(repo)
    mode = check_mode(d, e, tree, base, head, rem if adopt else {}, bool(reds) and must_wait(repo, commits, reds, st), hook)
    done, where, known_red = run_hook_checks(st, repo, d, e, tree, base, head, mode, gov_class_of(repo, commits, reds),
                                             os.path.join(d, f"check-{e['id']}"), clock, own) if hook \
        else (None, "no hook", None)
    if done is not None:
        return done
    mark_checked(st, d, e, i, base, head, mode, where, known_red)
    return 0


def run_hook_checks(st: Any, repo: str, d: str, e: dict[str, Any], tree: Any, base: str, head: str, mode: str,
                    cls: Any, log_path: str, clock: Clock, own: Any) -> tuple[int | None, str, Any]:
    """The hook's checks over the change: (the round's exit code when it ended here, else None; where they ran; the
    red they failed only as its base fails, if any). An infra failure (stalled, network, laptop, time cap) requeues."""
    ok, out, where = run_entry_checks(repo, d, e, tree, base, head, mode, cls, log_path, clock, st, own)
    if requeued_for_infra(st, repo, d, e, ok, out, where, clock):
        return 0, where, None
    if ok:
        return None, where, None
    return judge_check_failure(st, repo, d, e, tree, base, out, where, clock.time())


def mark_checked(st: Any, d: str, e: dict[str, Any], i: int, base: str, head: str, mode: str, where: str,
                 known_red: Any) -> None:
    """The change marked checked: on its own base, or verified (the earlier full check carried to this main)."""
    verify = mode == VERIFY_MODE
    full_base = (e.get("full_base") or e.get("checked_base") or base) if verify else base
    full_tip = e.get("full_tip") if verify else head
    areas = e.pop("verify_areas", None) if verify else None
    verified = (f"verified {head[:10]} on {base[:10]} (fully checked on {full_base[:10]}"
                + (f"; with the area tests: sbt modules both sides touch, {', '.join(areas)}" if areas else "") + ")") \
        if verify else f"checked {head[:10]} on {base[:10]}"
    set_change(st, d, e, "checked", data={"tip": head, "base": base, "mode": mode, "where": where},
               event={"text": f"{e['id']}: {verified} ({where})"
                      + (" -- failing only as its base fails" if known_red else "")},
               tip=head, checked_base=base, full_base=full_base, full_tip=full_tip, checked_mode=mode, checked_where=where, tree=i,
               infra_reason=None, infra_same=0, known_red=known_red, verify=False, push_alone=None,
               verified_areas=areas)
    wlog(d, f"{e['id']}: {verified} ({where})")


def mark_checking(st: Any, d: str, e: dict[str, Any]) -> None:
    if e.get("status") != "checking":
        set_change(st, d, e, "checking", event={"text": f"{e['id']}: checking"}, since=ts())


def round_base(st: Any, repo: str, d: str, e: dict[str, Any], clock: Clock) -> str | None:
    """The main the round checks on, with the change's commits here; None when either failed (the change requeued)."""
    base = fetched_main(st, repo, d, e, clock)
    return base if base is not None and commits_here(st, repo, d, e, clock) else None


def _rederive_stuck_hold(st: Any, e: Any, state: Any, it: Any, hold: Any, stuck: Any, t: Any) -> None:
    """SELF-HEALING (2026-11-20, THE HOLD TABLE): a hold that describes a world that moved -- its blocker ended, or
    its kind's bound passed -- is dropped, and the tick decides afresh (it records the hold again if one still
    applies, its clock restarted); said quietly on the change's events, never a page
    NEVER UNHELD BETWEEN TWO READINGS (2026-11-23, W1's "nothing holds it"): kept, its clock restarted."""
    restart_hold(st, e, t)
    with suppress(StaleVersion):
        it = st.item("change", e["id"]) or it
        st.transition("change", e["id"], state, it["version"],
                      event={"kind": "change.rederived", "severity": "info",
                             "text": f"{e['id']}: {state} for {fmt_age(t - it['entered_at'])}; its hold "
                                     f"({hold.get('why')}) was stuck -- {stuck} -- so it is decided afresh"})


def _lap_text(e: Any, state: Any, it: Any, hold: Any, stuck: Any, note: Any, issue: Any, t: Any) -> str:
    """What a change past its deadline says: its age, its hold (or that nothing holds it), why it is stuck, a note."""
    return (f"{e['id']}: {state} for {fmt_age(t - it['entered_at'])}, "
            + (f"held: {hold['why']}" if hold else "nothing holds it")
            + (f" -- STUCK: {stuck}" if stuck else "") + (f" ({note})" if note else "")
            + (f" -- filed as agentctl issue {issue}" if issue else ""))


def _overdue_change(st: Any, e: Any, state: Any, t: Any) -> Any:
    """The change's item when it is still in `state` and past its deadline, else None."""
    it = st.item("change", e["id"])
    return it if it and it["state"] == state and t > it["deadline"] else None


def lap_change_deadline(st: Any, e: Any, state: Any, t: Any, note: Any = None) -> Any:
    """A queued or checked change past its deadline: said and re-deadlined -- quietly while its hold is live, an
    escalation when it is stuck (change_hold_stuck: nothing holds it, its blocker is gone, held too long). note: what
    else the line says (a checked change's last push)."""
    it = _overdue_change(st, e, state, t)
    if not it:
        return
    hold = change_hold_visible(st, it, e, t)
    stuck = change_hold_stuck(st, hold, it, t)
    if stuck and stuck_recovery(hold, stuck) == "rederive":
        _rederive_stuck_hold(st, e, state, it, hold, stuck, t)
        return
    issue = None if hold else file_unheld_change(st, it, t, "agentctl (the change tick)")  # agentctl's bug: an issue,
    # A HELD LAP IS NO INBOX ENTRY (2026-11-19 08:39Z, 08:55Z: movies-fix-replay's "queued for 15 min / 30 min, held:
    # ..." reached the inbox every 15 min -- 93 of the day's 442 entries -- because the lap's event named the worktree
    # as its audience, and the projector writes every event with one). A live hold's lap is an event with no audience:
    # `land status` and the change's events say it; only a stuck or unheld lap goes to its author.
    quiet = bool(hold) and not stuck
    # never an owner page (changekind.py, A CHANGE PAST ITS DEADLINE WITH NO RECORDED HOLD); a stale version: the next
    # tick says it
    with suppress(StaleVersion):
        it = st.item("change", e["id"]) or it
        st.transition("change", e["id"], state, it["version"],
                      event={"text": _lap_text(e, state, it, hold, stuck, note, issue, t),
                             **({} if quiet else {"audience": {"worktree": e.get("worktree")}}),
                             **({} if stuck and not issue else {"severity": "info"})})
