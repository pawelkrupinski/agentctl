# agentctl, part `landedchange`: the queue's landed changes: which open changes already reached main (closed), the stranded ones, and the migration of a local queue
# A real module (lib/agentctl/__init__.py MODULES): its own imports, the standard library's and MODULES' only.

import os
from typing import Any
from functools import partial
from agentctl.statemachines import StaleVersion, TerminalState, machine_state, open_items
from agentctl.clock import epoch_now
from agentctl.timefmt import ts
from agentctl.queueknobs import knob_landed_look
from agentctl.core import host_role, process_cache, remote
from agentctl.repos import movies_repo_path
from agentctl.procs import safe_cwd
from agentctl.waitingdir import ChangeRecords, drop_entry, entry_final, waiting_state
from agentctl.inboxentry import inbox_write
from agentctl.onmain import LandState, UNKNOWN, landed_state
from agentctl.conditions import open_condition, run_probe
from agentctl.waiting import already_landed
from agentctl.queuesubmit import SUBMIT_FIELDS, submit_ref
from agentctl.gitrepo import commit_known


_landed_looked = process_cache({})


def change_state(repo, head, commits = None):
    """The LandState of the change at head against origin's main: its commits (the entry's, when it lists them) count
    from the first one's parent, and `via` is the main commit carrying it. A change without a head is UNKNOWN."""
    if not head:
        return LandState(UNKNOWN)
    after = f"{commits[0]}^" if commits and commit_known(repo, f"{commits[0]}^") else None
    return landed_state(repo, f"refs/remotes/{remote()}/main", head, after=after, with_via=True)


def landed_look_due(cid, t) -> bool:
    if t - _landed_looked.get(cid, 0) < knob_landed_look():
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
                      event={"cause": "landed", "text": f"LANDED {cid}: {why}", "refs": {"entry": cid}})
        return True
    except (StaleVersion, TerminalState):
        return False


def _close_landed_entries(repo: Any, st: Any, d: Any, t: Any, did: list[str]) -> set[str]:
    """The queue entries of d on main by content close as LANDED (lines to did); the ids of the open ones the entries
    cover, whose store items are not looked at again."""
    carried: set[str] = set()
    for e in ChangeRecords(st, d).every() if d else []:
        if entry_final(e.get("status")) or e.get("status") == "pushing":
            continue
        carried.add(e["id"])
        state = change_state(repo, e.get("head"), e.get("commits")) if landed_look_due(e["id"], t) else None
        if state and state.landed:  # unknown (this clone lacks the head): stays open, looked at again next interval
            already_landed(st, d, e, state.via)
            did.append(f"{e['id']}: its commits are on main (by content): landed")
            carried.discard(e["id"])
    return carried


def close_landed_changes(repo: Any, clock: Any, store: Any, d: Any = None) -> Any:
    """Every open change whose commits are on main by content closes as LANDED: the queue entries of d (with their
    items), then the store's other open items (by their head). Returns what it did."""
    st, t = store, clock.time()
    did: list[str] = []
    carried = _close_landed_entries(repo, st, d, t, did)
    for it in open_items(st, "change"):
        if it["state"] == "pushing" or it["id"] in carried:
            continue
        if not landed_look_due(it["id"], t):
            continue
        state = change_state(repo, (it.get("data") or {}).get("head"))
        if state.landed and close_item_landed(st, it["id"], "its commits are on main (by content)", state.via):
            did.append(f"{it['id']}: its commits are on main (by content): landed")
    return did


def stranded_changes(repo, d, store, now_ = None):
    """THE SERVER'S: each open store change item its queue (d) has no entry for -- no source, nothing will ever check,
    push or close it -- opens a STRANDED CHANGE condition naming it; its probe (the item closed, or an entry for it)
    closes it. Returns the ids."""
    st = store
    entries = {e["id"] for e in ChangeRecords(st, d).every()}
    out = []
    for it in open_items(st, "change"):
        if it["id"] in entries:
            continue
        r = run_probe(partial(probe_change_source, store=st), {"id": it["id"], "repo": repo})
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
    if ChangeRecords(store, d).get(a["id"]):
        return True, f"change {a['id']} is in the queue"
    return False, f"change {a['id']} is {it['state']} and no queue entry carries it"


def _migrate_landed(st: Any, d: Any, e: Any, via: Any) -> str:
    """Entry e's commits are on main (via): it is LANDED, here and in the store. The line saying so."""
    e.update(status="landed", landed_sha=via, decided=ts(), why="its commits are on main (by content)")
    ChangeRecords(None, d).save(e)  # the laptop's outbox
    close_item_landed(st, e["id"], "its commits are on main (by content)", via)
    return f"{e['id']}: its commits are on main (by content): landed"


def _migration_failure(repo: Any, st: Any, d: Any, e: Any) -> Any:
    """Entry e pushed to refs/heads/land/<id> and submitted to ci-runner's queue: why it could not be (None: moved)."""
    if not submit_ref(repo, d, e):
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
        ChangeRecords(None, d).save(e)  # the laptop's outbox: no item
        inbox_write("land", line, [line], [], 1, cwd=e.get("worktree") or safe_cwd())


def migrate_local_queue(repo: Any, store: Any) -> Any:
    """THE LAPTOP'S (sync role): its local waiting dir holds no queue, only an OUTBOX (a `land --follow` while main is
    red: slots.join). Each open entry there is LANDED when its commits are on main by content, else pushed to
    refs/heads/land/<id> and submitted to ci-runner's queue under its own id, and its file DELETED: the store's item is
    the change from then on (docs/design/one-change-model.md, step 2). One that cannot move is said loudly (stderr,
    and its author's inbox once an hour). Returns what it did."""
    if host_role() != "sync" or not os.path.isdir(repo):
        return []
    d = waiting_state(repo)[0]
    did: list[str] = []
    for e in ChangeRecords(None, d).open():  # the outbox
        state = change_state(repo, e.get("head"), e.get("commits"))
        if state.landed:  # unknown: migrated to ci-runner's queue like an unlanded one, which decides there
            did.append(_migrate_landed(store, d, e, state.via))
            continue
        why = _migration_failure(repo, store, d, e)
        if why is None:
            drop_entry(d, e["id"])
            did.append(f"{e['id']}: moved to ci-runner's queue")
            continue
        line = (f"LOCAL QUEUE: {e['id']} ({e.get('worktree')}) is in the laptop's local queue, which nothing runs: {why}; "
                "tried again every sync cycle")
        did.append(line)
        _tell_stranded(d, e, line)
    return did
