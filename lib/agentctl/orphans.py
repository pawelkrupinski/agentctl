# agentctl, part `orphans`: orphaned work: a worktree with commits nothing lands, found, offered to a fixer or escalated
# A real module (lib/agentctl/__init__.py MODULES): its own imports, the standard library's and MODULES' only.

from agentctl.clock import epoch_now, wall_epoch
from typing import Any, Callable, Optional, TypeGuard
import json
import os
import re
from contextlib import suppress
from agentctl.landstate import landed_state
from agentctl.core import attempt, branch_of, hashlib_sha1, remote, commit_known
from agentctl.procrunner import process_runner
from agentctl.repos import movies_repo_path
from agentctl.trailers import do_not_land, held_commit
from agentctl.waitingdir import WAITING_BRANCH, ChangeRecords, waiting_state
from agentctl.procs import process_cwds
from agentctl.inboxentry import inbox_entries
from agentctl.worktrees import gitdir_activity, worktrees
from agentctl.runowners import internal_tree
from agentctl.sessionpaths import session_worktrees
from agentctl.fixerblockers import land_cwds
from agentctl.recovery import reland_tree_path
from agentctl.superseded import superseded_mark, worktree_change_items
from agentctl.park import parked_mark
from agentctl.blamerecords import blame_land_refusal, blame_records
from agentctl.fixerknobs import knob_orphan_idle


def orphan_key(repo: str, wt: str) -> tuple[Any, Optional[str]]:
    """The change's identity across rebases: its commits' patch-ids (not HEAD, which every rebase moves)."""
    main = f"refs/remotes/{remote()}/main"
    plus = list(landed_state(wt, main, "HEAD").left)  # landed by patch or by content: not the orphan's (unknown: none)
    ids = []
    for sha in plus:  # bytes: a commit may carry binary content
        show = process_runner().run(["git", "-C", wt, "show", sha], capture_output=True, timeout=60).stdout
        r = process_runner().run(["git", "patch-id", "--stable"], input=show, capture_output=True, timeout=60)
        ids.append((r.stdout.decode(errors="replace").split() or [sha])[0])
    return plus, hashlib_sha1("\n".join(sorted(ids)))[:16] if ids else None


def land_entries_by_cwd(entries: Optional[list[dict[str, Any]]] = None) -> dict[str, list[dict[str, Any]]]:
    """{realpath of a cwd: its land/waiting inbox entries, oldest first}."""
    out: dict[str, list[dict[str, Any]]] = {}
    for e in inbox_entries() if entries is None else entries:
        if e.get("command") in ("land", "waiting"):
            out.setdefault(os.path.realpath(str(e.get("cwd") or "")), []).append(e)
    return out


def land_entries_once() -> Callable[[], dict[str, list[dict[str, Any]]]]:
    """land_entries_by_cwd, read on first use and kept for the caller's pass: the inbox is thousands of files (7849 on
    2026-11-07, ~2.4 s a read), and an orphan pass asks it of every orphan twice or more (review 2026-11-07)."""
    memo: list[dict[str, list[dict[str, Any]]]] = []

    def get() -> dict[str, list[dict[str, Any]]]:
        if not memo:
            memo.append(land_entries_by_cwd())
        return memo[0]
    return get


def last_land_entry(wt: str, lands: Any = None) -> Optional[dict[str, Any]]:
    """The newest final land entry in the inbox for this worktree, or None. lands: a land_entries_once reader."""
    es = [e for e in (lands or land_entries_once())().get(os.path.realpath(wt)) or [] if e.get("final", True)]
    return es[-1] if es else None


def live_lease_worktrees(store: Any) -> set[str]:
    """The worktrees a live lease names (a fixer's, an interactive claim's): somebody works there."""
    out, t = set(), epoch_now()
    for r in store.list():
        if r["holder"] and r["expires_at"] > t:
            wt = (json.loads(r["meta"] or "{}") or {}).get("worktree")
            if wt:
                out.add(os.path.realpath(wt))
    return out


def orphan_candidates(repo: str, store: Any, now_: Optional[float] = None, dead: Optional[list[Any]] = None,
                      refused: Optional[list[Any]] = None, lands: Any = None) -> list[Any]:
    """[(worktree, commits, key, last verdict)] of the orphans now. A worktree holding a withdrawn or refuted blame
    revert (blame_land_refusal) is never one: it goes to `dead` [(worktree, why)] when given; one whose work is not for
    landing (orphan_refusal) goes to `refused` [(worktree, key, why)]. lands: the pass's land_entries_once reader."""
    t = now_ if now_ is not None else wall_epoch()  # worktree activity is read off file mtimes and git times
    lands = lands or land_entries_once()
    cwds, landers, live, sessions = worktrees_in_use(repo, store, t)
    records = blame_records(store)
    live |= queued_worktrees(repo, store)
    out = []
    for w in worktrees(repo):
        p = w.get("worktree")
        if not candidate_tree(p, w, repo) or owned_or_set_aside(os.path.realpath(p), landers, live, cwds, sessions) \
                or not idle(repo, p, t):
            continue
        orphan = orphan_of(repo, p, store, records, dead, refused, lands)
        if orphan:
            out.append(orphan)
    return out


def worktrees_in_use(repo: str, store: Any, t: float) -> tuple[set[str], set[str], set[str], dict[Any, Any]]:
    """(the processes' cwds, the landers' cwds, the worktrees a live lease names, those a live session works in)."""
    catch = (Exception, SystemExit)
    cwds = attempt(lambda: {os.path.realpath(c) for c in process_cwds().values()}, set(), catch=catch)
    landers = attempt(lambda: {os.path.realpath(w) for w in land_cwds()}, set(), catch=catch)
    live = live_lease_worktrees(store)
    sessions: dict[Any, Any] = attempt(lambda: session_worktrees([w.get("worktree") for w in worktrees(repo)], t), {},
                                       catch=catch)
    return cwds, landers, live, sessions


def queued_worktrees(repo: str, store: Any) -> set[str]:
    """The worktrees of the changes in the land queue (queued, checking, waiting): none of them is orphaned."""
    with suppress(Exception, SystemExit):
        d, _c, _w = waiting_state(repo)
        return {os.path.realpath(e.get("worktree") or "") for e in ChangeRecords(store, d).every()
                if e.get("status") in ("queued", "checking", "waiting")}
    return set()


def candidate_tree(p: Any, w: dict[str, Any], repo: str) -> TypeGuard[str]:
    """A worktree of somebody's work: not bare, not the repo itself, not one of agentctl's own trees."""
    return (isinstance(p, str) and not w.get("bare") and os.path.realpath(p) != os.path.realpath(repo)
            and not internal_tree(p) and not p.endswith((f"-wt-{WAITING_BRANCH}", "-wt-batch")) and os.path.isdir(p))


def owned_or_set_aside(rp: str, landers: set[str], live: set[str], cwds: set[str], sessions: dict[Any, Any]) -> bool:
    """Somebody works in it (a land, a live lease, a process, a live Claude session), or it is set aside: its work went
    into another change (superseded_duty closes it when that lands), or it is parked for a reason (park_duty
    re-surfaces it when its condition is met)."""
    if rp in landers or rp in live or any(c == rp or c.startswith(rp + "/") for c in cwds):
        return True
    return bool(superseded_mark(rp) or parked_mark(rp) or rp in sessions)


def idle(repo: str, p: str, t: float) -> bool:
    """Nobody touched its git dir for ORPHAN_IDLE ($AGENTCTL_ORPHAN_IDLE)."""
    return bool(t - (gitdir_activity(repo, p) or 0) >= knob_orphan_idle())


def orphan_of(repo: str, p: str, store: Any, records: Any, dead: Optional[list[Any]], refused: Optional[list[Any]],
              lands: Any) -> Optional[tuple[str, Any, str, Optional[str]]]:
    """(worktree, commits, key, last verdict) when its commits are not on main and may be submitted, else None."""
    commits, key = orphan_key(repo, p)
    if not (commits and key):
        return None
    why = blame_land_refusal(p, commits, store, p, records=records)
    if why:  # 2026-11-01: movies-wt-revert-629 submitted as an orphan after its blame was withdrawn
        if dead is not None:
            dead.append((p, why))
        return None
    why = orphan_refusal(p, commits)
    if why:  # 2026-11-06: es-gate-check (check/*, held by the owner) rebased and queued as an orphan
        if refused is not None:
            refused.append((p, key, why))
        return None
    return p, commits, key, orphan_verdict(p, lands)


# NOT FOR LANDING (2026-11-06 22:51Z, es-gate-check: a check/* worktree holding a gate re-enable the owner holds was
# queued as an orphan). Nothing submits, unasked, work that says it is not for landing: a branch named check/*,
# measure/*, experiment/*, probe/*, prototype/*, spike/*, wip-* or wip/*, or a commit with a `Hold: <why>` trailer
# (held_commit; `land --release-hold` is the owner's go). Such a worktree is said once (`orphan <name>: not submitted
# (...)`) and never escalated: it is not stranded, it is kept.
ORPHAN_NOT_FOR_LANDING = re.compile(r"^((check|measure|experiment|probe|prototype|spike)/|wip[-/])", re.I)


def orphan_branch_refusal(branch: Optional[str]) -> Optional[str]:
    if ORPHAN_NOT_FOR_LANDING.match(branch or ""):
        return f"branch {branch} is not for landing"
    return None


def orphan_refusal(wt: str, commits: Any) -> Optional[str]:
    """Why orphan duty must never submit this worktree's commits (not for landing), or None."""
    return orphan_branch_refusal(branch_of(wt)) or held_commit(wt, commits) or do_not_land(wt, commits, branch_of(wt))


def orphan_land_entry(wt: str, lands: Any = None) -> Optional[dict[str, Any]]:
    """The newest final land entry for the orphan: from its own worktree or the throwaway tree its re-land ran in."""
    lands = lands or land_entries_once()
    es = [e for e in (last_land_entry(wt, lands), last_land_entry(reland_tree_path(wt), lands)) if e]
    return max(es, key=lambda e: float(e.get("at") or 0)) if es else None


def orphan_verdict(wt: str, lands: Any = None) -> Optional[str]:
    """The newest final land verdict for the orphan (orphan_land_entry's), or None."""
    e = orphan_land_entry(wt, lands)
    return str(e.get("verdict") or "") if e else None


def probe_orphan_landed(a: dict[str, Any], store: Any) -> tuple[bool, str]:
    """The orphan's change is on main (by patch), its worktree is gone, or somebody works in it again."""
    wt = a["worktree"]
    if not os.path.isdir(wt):
        return True, f"{wt} is gone"
    repo = a.get("repo") or movies_repo_path()
    commits, key = orphan_key(repo, wt)
    if key != a.get("key"):
        return True, "its commits are on main" if key is None else "its change moved on (new commits)"
    why = orphan_released(wt, commits, store)
    if why:
        return True, why
    return False, f"{len(commits)} commit(s) still not on main, nobody in it"


def orphan_released(wt: str, commits: Any, store: Any) -> Optional[str]:
    """Why the orphan needs nobody any more, though its commits are not on main, or None."""
    rp = os.path.realpath(wt)
    sup = superseded_mark(rp)
    if sup:
        return f"superseded by {sup['by']}"
    dead = blame_land_refusal(wt, commits, store, wt)
    if dead:
        return f"never landed: {dead}"
    kept = orphan_refusal(wt, commits)
    if kept:
        return f"not for landing: {kept}"
    if rp in live_lease_worktrees(store):
        return "a live claim works in it"
    park = parked_mark(rp)
    if park:
        return f"parked: {park['reason']}"
    owner = session_worktrees([rp]).get(rp)
    if owner:
        return f"a live session works in it ({owner})"
    cover = orphan_covered_by(wt, worktree_change_items(wt, store))  # orphan_duty's rule (a): an open change carries it
    return f"covered by {cover}" if cover else None


def orphan_covered_by(wt: str, items: dict[str, Any]) -> Optional[str]:
    """The id of an open change item whose head carries every commit of the orphan (by patch or content), else None."""
    for it in items.get("open") or []:
        head = (it.get("data") or {}).get("head")
        if not head or not commit_known(wt, head):
            continue
        if landed_state(wt, head, "HEAD").landed:  # unknown (git cannot compare): not covered
            found: str = it["id"]
            return found
    return None


