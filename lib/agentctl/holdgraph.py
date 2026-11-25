# agentctl, part `holdgraph`: the queue's hold graph -- stacked changes (a branch carrying another queued change's
# commits) and cycles among the holds. Pure: entries and recorded holds in, answers out.
# A real module (lib/agentctl/__init__.py MODULES): the standard library's imports only; `st` is the store, read only.

from typing import Any, Dict, Iterable, List, Optional, Set

# THE DEADLOCK OF 2026-11-23 (22 movies changes unchanged for over an hour): py-gitops-specs (checked) held its push for
# landing-check-py's combined-tree verification (THE OLDEST VERIFICATION PUSHES FIRST); landing-check-py's check waited
# for kick-convergence-py ("both change" 51 files); kick-convergence-py's waited for py-gitops-specs (docs/tools.md).
# kick-convergence-py was STACKED on landing-check-py -- its branch held landing-check-py's six commits -- so its
# "overlap" with it was its parent's own files, and its kept place (an older seq) made it the older of the two. Two
# rules close that shape for good:
#
# A STACKED CHANGE WAITS FOR ITS PARENT. A change whose commits include every commit of another open change of another
# worktree (its PARENT: the one with the most such commits) cannot land before it without landing it too. It is no
# overlapping peer: it waits for its parent as a dependency (hold `stacked`), lands after it and is checked once on top
# of it; its parent never waits for it; and its overlap with every other change is read from its OWN commits, the ones
# past its parent's tip (a landed parent's too: its commits were re-cut onto main, the stacked branch still carries the
# originals).
#
# A HOLD CYCLE IS BROKEN, NEVER WAITED OUT. Every hold names at most one blocker, so the holds are a functional graph;
# a cycle in it (a push waiting for a verification, a check for a check) can never clear by itself. The tick finds
# every cycle among the recorded holds and lets the OLDEST change in it (the queue's own order: lowest seq, then id)
# proceed: its wait on its blocker in the cycle is dropped for good (`cycle_ignore` on its entry), the cycle is said on
# its item and in `status`. Tests: tests/test_hold_graph.py.
STACK_PARENT_STATES = ("queued", "checking", "checked", "pushing", "waiting", "landed")
OPEN_PARENT_STATES = ("queued", "checking", "checked", "pushing", "waiting")


def stacked_on(e: Dict[str, Any], entries: Iterable[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """The entry change e is stacked on (A STACKED CHANGE WAITS FOR ITS PARENT): another worktree's change, open or
    landed, every one of whose commits e carries, and fewer than e's; the one with the most (the nearest), the newest
    on a tie. None when there is none."""
    mine = set(e.get("commits") or [])
    parents = [o for o in entries if may_parent(o, e, mine)]
    return max(parents, key=lambda o: (len(o["commits"]), o["seq"])) if parents else None


def may_parent(o: Dict[str, Any], e: Dict[str, Any], mine: Set[str]) -> bool:
    """Whether entry o may be the parent of e (whose commits are `mine`): another worktree's open or landed change, its
    commits a proper, non-empty subset of e's."""
    theirs = set(o.get("commits") or [])
    if o["id"] == e["id"] or o.get("status") not in STACK_PARENT_STATES:
        return False
    return bool(theirs) and len(theirs) < len(mine) and theirs <= mine and not same_worktree(o, e)


def same_worktree(o: Dict[str, Any], e: Dict[str, Any]) -> bool:
    return bool(o.get("worktree")) and o.get("worktree") == e.get("worktree")


def stack_map(todo: Iterable[Dict[str, Any]], entries: Iterable[Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
    """{entry id: the entry it is stacked on} for the entries of todo that are stacked."""
    pool = list(entries)
    out = {}
    for e in todo:
        p = stacked_on(e, pool)
        if p is not None:
            out[e["id"]] = p
    return out


def own_commits(e: Dict[str, Any], parent: Optional[Dict[str, Any]]) -> List[str]:
    """e's commits past its parent's (all of them with no parent)."""
    theirs = set((parent or {}).get("commits") or [])
    return [c for c in e.get("commits") or [] if c not in theirs]


def dependency_between(o: Dict[str, Any], e: Dict[str, Any], stacks: Dict[str, Dict[str, Any]]) -> bool:
    """Whether one of o and e is stacked on the other: a dependency, never an overlap."""
    return bool((stacks.get(o["id"]) or {}).get("id") == e["id"] or (stacks.get(e["id"]) or {}).get("id") == o["id"])


def as_own(e: Dict[str, Any], stacks: Dict[str, Dict[str, Any]]) -> Dict[str, Any]:
    """Entry e as its own change: its commits past its parent's when it is stacked, else e itself."""
    p = stacks.get(e["id"])
    return {**e, "commits": own_commits(e, p)} if p is not None else e


def hold_cycles(edges: Dict[str, str]) -> List[List[str]]:
    """Every cycle of a functional graph ({node: the one node it waits for}), each listed from its smallest node in
    wait order, once."""
    seen, out = set(), []
    for start in sorted(edges):
        path, at = [], start
        while at in edges and at not in seen and at not in path:
            path.append(at)
            at = edges[at]
        if at in path:
            cyc = path[path.index(at):]
            i = cyc.index(min(cyc))
            out.append(cyc[i:] + cyc[:i])
        seen.update(path)
    return out


def cycle_breaker(cycle: List[str], seq_of: Dict[str, Any]) -> str:
    """The change of a cycle that proceeds: the oldest in the queue's order (lowest seq, then id)."""
    return min(cycle, key=lambda x: (seq_of.get(x, float("inf")), x))


def cycle_text(cycle: List[str], breaker: str, why: Dict[str, str]) -> str:
    """The cycle in words, each wait named, and which change proceeds."""
    steps = "; ".join(f"{x} {why.get(x) or 'waits'}" for x in cycle)
    return (f"HOLD CYCLE of {len(cycle)} changes ({steps}): none could ever go first; {breaker}, the oldest, proceeds "
            f"(its wait on {cycle[(cycle.index(breaker) + 1) % len(cycle)]} dropped)")


# A HELD VERIFICATION HOLDS NOTHING (2026-11-23: the 22-change queue formed no batch at all -- `batch: 0` -- since
# landing-check-py sat in a verify round held behind other changes, and THE OLDEST VERIFICATION PUSHES FIRST read it
# as verifying: every younger push waited for it and no backlog batch formed, while its own verification could not
# start). A change whose recorded hold is a wait for another change (an overlap, its stack parent) verifies nothing
# now: it holds no push and no batch until that wait ends (queuepush.py verifying).
CHANGE_WAITS = ("overlap", "stacked")


def waiting_on_changes(st: Any, entries: Iterable[Dict[str, Any]]) -> Set[str]:
    """The ids of entries whose recorded hold is a wait for another change (A HELD VERIFICATION HOLDS NOTHING)."""
    out: Set[str] = set()
    for e in entries:
        hold = ((st.item("change", e["id"]) or {}).get("data") or {}).get("hold") or {}
        if hold.get("kind") in CHANGE_WAITS and hold.get("blocker"):
            out.add(e["id"])
    return out


def push_waits(st: Any, e: Dict[str, Any]) -> bool:
    """Whether checked entry e's push waits for another change's verification (its recorded hold): not due."""
    return (((st.item("change", e["id"]) or {}).get("data") or {}).get("hold") or {}).get("kind") == "verify"


def push_due(st: Any, e: Dict[str, Any], t: float) -> bool:
    """Whether checked entry e pushes at the next change tick: no retry backoff, and no verification it waits for."""
    return e.get("status") == "checked" and not float(e.get("retry_after") or 0) > t and not push_waits(st, e)


def recorded_hold_edges(st: Any, todo: List[Dict[str, Any]]) -> Any:
    """({change: the change its recorded hold names}, {change: that hold's why}, {change: its item}) among todo, the
    waits a cycle break dropped left out."""
    ids = {e["id"] for e in todo}
    edges: Dict[str, str] = {}
    why: Dict[str, str] = {}
    items: Dict[str, Any] = {}
    for e in todo:
        it = st.item("change", e["id"]) or {}
        hold = (it.get("data") or {}).get("hold") or {}
        if waits_on_another(e, hold.get("blocker"), ids):
            edges[e["id"]], why[e["id"]], items[e["id"]] = hold["blocker"], hold.get("why") or "", it
    return edges, why, items


def waits_on_another(e: Dict[str, Any], blocker: Optional[str], ids: Set[str]) -> bool:
    """Whether change e's recorded blocker is another change among ids whose wait no cycle break dropped."""
    return blocker in ids and blocker != e["id"] and blocker not in (e.get("cycle_ignore") or [])


def hold_record(cur: Dict[str, Any], kind: Optional[str], why: Optional[str], blocker: Optional[str],
                files: Any, batch: Any, t: float) -> Optional[Dict[str, Any]]:
    """The hold to record on a change's item (None: no hold now); it keeps its `since` while the kind and blocker stay
    the same as the recorded `cur`."""
    if not why:
        return None
    same = cur.get("kind") == kind and cur.get("blocker") == blocker
    return {"kind": kind, "why": why, "blocker": blocker, "files": files, "batch": batch,
            "since": cur.get("since") if same else t}
