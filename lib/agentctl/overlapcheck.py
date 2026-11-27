# agentctl, part `overlapcheck`: overlapping changes are checked as one: which queued changes share files or conflict, the hold and the author's CONFLICTS notice
# A real module (lib/agentctl/__init__.py MODULES): its own imports, the standard library's and MODULES' only.

from typing import Any, Optional
from agentctl.context import Context
from agentctl.statemachines import StaleVersion, machine_state
from agentctl.holdgraph import HOLDER_IN_FLIGHT, as_own, dependency_between
from agentctl.core import git, process_cache, remote
from agentctl.generated import generated_entry, order_sensitive, without_derived, without_generated
from agentctl.waitingdir import ChangeRecords, wlog
from agentctl.conflicts import conflict_hunks, hunks_resolve, own_commits_conflict, union_safe
from agentctl.queueslots import checker_alive
from agentctl.queuerecheck import same_change_source
from agentctl.identitychange import shared_paths
from agentctl.changekind import classify_change, facts_for
from agentctl.redseen import gate_reds


# OVERLAPPING CHANGES ARE CHECKED AS ONE (the coordinator, 2026-11-18: B1 against the CI-workflows change, the scraper
# catalog against the scrapers refactor, the cycle-breaking change against both -- each a check, then `NOT LANDED: no
# longer applies on origin/main ... main moved during its checks`, a rebase and a full recheck). A queued change that
# shares a FILE with a change queued before it and still in flight (queued, checking, checked, pushing, or waiting for
# a green gate -- the last only when they conflict, below):
# - both queued, both batchable, and they assemble together (git merge-tree of their heads is clean): nothing held --
#   THE BACKLOG BATCH checks them as one tree;
# - else its check waits for the older one: once that lands, it is checked ONCE, on top of it (no check thrown away, no
#   full recheck); once that is rejected or cancelled, it goes on as before -- for generated files alone only when
#   their entry is declared order-sensitive (ONLY A VOIDED CHECK HOLDS, below);
# - and when the two cannot be assembled together (a textual conflict), its author is told AT ONCE, at its first tick
#   in the queue: `CONFLICTS <id>: ...` to the inbox, the files named -- rebase onto the older change's branch, or wait
#   for it to land and rebase onto main. Its check waits meanwhile (one is never spent on a tree that cannot land).
# And a queued change that no longer applies on main itself (main moved since its base) is NOT LANDED at its next
# tick, never after a wait for a check tree (main_conflict).
# A red fix never waits (told of a conflict all the same).
# A WAITING CHANGE HOLDS NOTHING IT ASSEMBLES WITH (2026-11-20, Main #2716 red): metrics-packages and phase7-houses sat `held: its check
# waits for movies-misc-cx (waiting)`. A waiting change lands only with the batch at green, so a change held behind it
# was held until green -- a cycle for a red fix, pointless serialisation for the rest -- and cancelling misc-cx moved
# the hold to the next waiting change. A younger change that ASSEMBLES with a waiting one (git merge-tree clean) is
# checked on main now and, waiting too, meets it in the batch at green, assembled in order. Only one that cannot be
# assembled with it waits (its author told: CONFLICTS -- a check of it is spent on a tree that cannot land), and a red
# fix never does. overlapcheck.py overlap_hold, pair_conflict; tests: tests/test_queue_sim.py CollisionTest (the three
# collisions of 2026-11-17 replayed), tests/test_backlog_batch.py OverlapTest.
OVERLAP_OLDER = ("queued", "checking", "checked", "pushing", "waiting")


_overlap_known = process_cache({})  # (older id, head, younger id, head) -> (shared files, conflicting files or None)


def pair_conflict(repo, older, younger):
    """The files changes `older` and `younger` conflict in when assembled together (git merge-tree of their heads:
    exit 1 lists them), or None: they merge cleanly, or git cannot tell (an old git, a head missing)."""
    r = git(repo, "merge-tree", "--write-tree", "--name-only", "--no-messages", older["head"], younger["head"],
            check=False)
    if r.returncode != 1:
        return None
    return [x for x in r.stdout.splitlines()[1:] if x.strip()] or ["(a file both change)"]


# THE BATCH TIERS (the owner, 2026-11-21: "make agentctl batch even more, merge / fix conflicts"): two queued changes
# that overlap are one backlog batch when (1) git merges them cleanly, (2) the overlap is in generated files (rows
# merged, else regenerated; a snapshot always regenerated, its output explained by the sides), or (3) their source
# conflict is additions on both sides (pair_resolvable here; resolved in the batch's assembly, whose full check is the
# verification); else (4) the younger waits and its author is told, the hunks and the fix named (conflict_hunks).
# AGENTCTL.md THE BATCH TIERS; waiting.py SNAPSHOTS ARE REGENERATED, NEVER MERGED and A SOURCE CONFLICT IS RESOLVED IN
# THE BATCH.
def pair_resolvable(repo, older, younger, files):
    """The files of `files` (where the two changes' heads conflict) that the backlog batch's assembly resolves
    (THE BATCH TIERS): UNION_SAFE prose, and source whose conflicting hunks are additions on both sides
    (waiting.py hunks_resolve, over the heads' merge base)."""
    mb = git(repo, "merge-base", older["head"], younger["head"], check=False).stdout.strip()
    out = []
    for f in files:
        if union_safe(f):
            out.append(f)
            continue
        got = [git(repo, "show", f"{x}:{f}", check=False) for x in (mb, older["head"], younger["head"])] if mb else []
        if got and all(r.returncode == 0 for r in got) and hunks_resolve(*(r.stdout for r in got)) is not None:
            out.append(f)
    return out


def overlap_key(e):
    """An entry's part of overlap_of's cache key: its id, head and how many commits it is read with (holdgraph.py
    as_own: a stacked change's own)."""
    return e["id"], e.get("head"), len(e.get("commits") or [])


def held_files(repo: Optional[str], paths: list[str], strict: bool) -> list[str]:
    """The files of paths (both changes change them) that hold the younger (ONLY A VOIDED CHECK HOLDS): a source; with
    strict (a change whose generated files could not be resolved in a batch, checked alone) an ORDER-SENSITIVE
    generated file too. Every other generated file -- a derived one always -- is combined over the merged tree."""
    return [p for p in paths if not generated_entry(repo, p) or strict and order_sensitive(repo, p)]


def conflicting(repo: Optional[str], files: Optional[list[str]], strict: bool) -> Optional[list[str]]:
    """The files of a textual conflict that are one: no generated file's, or with strict none but a derived file's."""
    return (without_derived(repo, files) if strict else without_generated(repo, files)) or None


def overlap_of(repo, older, younger, strict=False):
    """(the files both change, the files they conflict in or None, the conflicting files the batch resolves), per pair
    of heads (cached in the process); the repo's generated files are neither (GENERATED FILES ARE NO OVERLAP,
    waiting.py) unless strict (a change whose generated files or source conflict could not be resolved in a batch):
    then its order-sensitive generated files are shared ones, and every textual conflict but a derived file's is one."""
    key = (*overlap_key(older), *overlap_key(younger), strict)
    if key not in _overlap_known:
        shared = shared_paths(repo, older, younger)
        shared = held_files(repo, shared, strict)
        conflict = conflicting(repo, pair_conflict(repo, older, younger), strict) if shared else None
        resolvable = [] if strict or not conflict else pair_resolvable(repo, older, younger, conflict)
        conflict = [f for f in conflict or [] if f not in resolvable] or None
        _overlap_known[key] = (shared, conflict, resolvable)
    return _overlap_known[key]


# ONLY A VOIDED CHECK HOLDS (2026-11-25; generated.py). A change sharing only generated files with one queued before
# it is no overlap (the backlog batch takes both); with that one IN FLIGHT, past the batch they could share, it waits
# only for an entry declared `order_sensitive: true` -- never a derived file, nor behind a `waiting` change, nor a red
# fix -- and meets the others in the next batch. Replaces A GENERATED OVERLAP WAITS FOR A CHECK IN FLIGHT (2026-11-21:
# every generated file but a `holds: false` one). A DERIVED FILE HOLDS NOTHING (2026-11-23): regenerated over the tree
# it lands in, its push's new round regenerating it (queuediff.py _push_change). Tests: tests/test_conflict_holds.py.
IN_FLIGHT = HOLDER_IN_FLIGHT


def ordered_overlap(repo: Optional[str], older: dict[str, Any], younger: dict[str, Any]) -> list[str]:
    """The generated files both changes change whose registry entry is declared ORDER-SENSITIVE (ONLY A VOIDED CHECK
    HOLDS): they hold the younger behind a check in flight."""
    both = shared_paths(repo, older, younger)
    return [f for f in both if order_sensitive(repo, f)]


def main_conflict(repo, e):
    """(main, the files) when queued change e no longer applies on the current main (main moved since its base and
    they conflict: git merge-tree), else None; asked once per main and head."""
    main = git(repo, "rev-parse", f"{remote()}/main", check=False).stdout.strip()
    if not main or main == e.get("base") or not e.get("head"):
        return None
    key = ("main", main, e["id"], e.get("head"))
    if key not in _overlap_known:
        files = pair_conflict(repo, {"id": "main", "head": main}, e)
        files = without_generated(repo, files) if files else files  # regenerated in its rebase
        # the heads' merge sees commits main carries under other shas (a stacked change's landed parent): judged again
        # as its rebase picks it (conflicts.py A CHANGE RE-APPLIED ONTO MAIN IS JUDGED BY ITS OWN COMMITS)
        _overlap_known[key] = own_commits_conflict(repo, main, e.get("commits") or []) if files else files
    files = _overlap_known[key]
    return (main, files) if files else None


def overlap_candidate(o, e, stacks):
    """Whether entry o may hold queued change e as an older change it overlaps."""
    # A KEPT PLACE NEVER JUMPS A CHECK IN FLIGHT (2026-11-20, the simulated 2026-11-19 seed 0: a change queued again
    # after a conflict took its first entry's seq, 10, and so was "older" than a change it overlaps that was already
    # checking, seq 20 -- checked beside it, then fully rechecked once that landed). A younger-by-seq change counts
    # too once it is past queued: its check (or push) runs now, before this one could.
    if o["seq"] >= e["seq"] and o.get("status") == "queued":
        return False
    if o["id"] == e["id"] or o.get("status") not in OVERLAP_OLDER \
            or same_change_source(o, e["worktree"], e.get("branch") or ""):  # its own older version: superseded
        return False
    # a broken cycle's wait, or a parent and its stacked change: a dependency, never an overlap
    let_go = set(e.get("cycle_ignore") or []) | set(e.get("hold_expired") or [])  # a broken cycle's, an expired hold's
    return o["id"] not in let_go and not dependency_between(o, e, stacks)


class OverlapScan:
    """overlap_hold's walk over the older entries: what it has found so far (`first`), and the entry's backlog batching
    (`together`, read once, when an overlap with a queued entry first asks)."""

    def __init__(self, st: Any, repo: Any, d: Any, e: Any, t: Any, facts: Any, stacks: Any, *, ctx: Context) -> None:
        self.ctx = ctx
        self.st, self.repo, self.d, self.e, self.t, self.facts, self.stacks = st, repo, d, e, t, facts, stacks
        self.mine = as_own(e, stacks)
        self.together: Any = None
        self.first: Any = None

    def run(self, entries: Any) -> Any:
        """The (older entry, shared files, conflicting files or None) the change waits for, or None."""
        ids = {x["id"] for x in entries}
        entries = list(entries) + [w for w in ChangeRecords(self.st, self.d).waiting() if w["id"] not in ids]  # and those waiting for green
        for o in sorted(entries, key=lambda x: x["seq"]):
            if not overlap_candidate(o, self.e, self.stacks):
                continue
            found = self.meet(o)
            if found:
                return found
        return self.first

    def meet(self, o: Any) -> Any:
        """The hold to return at once when entry o overlaps the change and cannot be assembled with it, else None (`first`
        notes the overlap that merely holds)."""
        theirs = as_own(o, self.stacks)
        shared, conflict, resolvable = overlap_of(self.repo, theirs, self.mine,
                                                  strict=self.e.get("batch_alone") in ("regen", "resolve"))
        if not shared:
            self.note_ordered(o, theirs)
            return None
        if conflict is None and self.batched_together(o):
            return None  # the next backlog batch checks them as one (its assembly resolves the `resolvable`)
        conflict = conflict or resolvable or None  # resolved only in a batch of the two: else it cannot be assembled
        if conflict:  # the one it cannot be assembled with is the one its author hears of
            return o, shared, conflict
        if o.get("status") != "waiting":  # A WAITING CHANGE HOLDS NOTHING it assembles with: they meet in the batch
            self.first = self.first or (o, shared, conflict)
        return None

    def note_ordered(self, o: Any, theirs: Any) -> None:
        """ONLY A VOIDED CHECK HOLDS: entry o, in flight, shares only files whose order matters: they meet in the next batch."""
        ordered = ordered_overlap(self.repo, theirs, self.mine) if o.get("status") in IN_FLIGHT else []
        if ordered:
            self.first = self.first or (o, ordered, None)

    def batched_together(self, o: Any) -> bool:
        """Both the change and queued entry o are taken by the next backlog batch."""
        if o.get("status") != "queued" or o.get("backlog_batch"):
            return False
        if self.together is None:
            self.together = backlog_batchable(self.st, self.repo, self.d, self.e, self.t, self.facts, ctx=self.ctx)
        return bool(self.together and backlog_batchable(self.st, self.repo, self.d, o, self.t, self.facts, ctx=self.ctx))


OverlapHold = tuple[dict[str, Any], list[str], Any]  # (the older entry, the files both change, the conflicting ones)


def overlap_hold(st: Any, repo: str, d: str, e: dict[str, Any], entries: list[dict[str, Any]], facts: Any, t: float,
                 stacks: dict[str, dict[str, Any]] | None = None, *, ctx: Context) -> OverlapHold | None:
    """(older entry, shared files, conflicting files or None) queued change e waits for (OVERLAPPING CHANGES ARE
    CHECKED AS ONE), or None. stacks: {id: the entry it is stacked on} (holdgraph.py A STACKED CHANGE WAITS FOR ITS
    PARENT): a parent and its stacked change are never each other's overlap, and each change's files are its own
    commits'. A blocker e's cycle_ignore names (A HOLD CYCLE IS BROKEN) or hold_expired names (AN IDLE HOLDER'S HOLD
    EXPIRES, queuehold.py) holds it no more."""
    if e.get("backlog_batch"):
        return None
    return OverlapScan(st, repo, d, e, t, facts, stacks or {}, ctx=ctx).run(entries)


def conflict_text(e: Any, o: Any, conflict: Any, waits: bool, hunks: Any) -> str:
    """What a change held behind an older one it cannot be assembled with tells its author."""
    return (f"CONFLICTS {e['id']}: it cannot be assembled with {o['id']} ({o.get('worktree')}), queued before it "
            f"(both change {', '.join(conflict[:3])})"
            + (f": its check waits for {o['id']}" if waits else ": a red fix, it is checked now all the same")
            + f"; if {o['id']} lands first, this change no "
            f"longer applies -- rebase onto it (its branch, or {remote()}/main once it landed) and land again, or "
            "`agentctl land cancel` this one"
            + (f". The hunks: {'; '.join(hunks)}" if hunks else "")
            + (f". Fix now: `cd {e.get('worktree')} && git rebase {o['branch']}`, resolve those hunks, "
               "`~/bin/agentctl land`" if o.get("branch") and e.get("worktree") else ""))


def tell_conflict(st: Any, d: Any, e: Any, o: Any, conflict: Any, waits: bool, repo: Any) -> Optional[str]:
    """The change's author told of its conflict with o, once (on its item, in the entry and the log): the text, or None
    when the item moved meanwhile (the next tick tells it)."""
    it = st.item("change", e["id"])
    hunks = conflict_hunks(repo, o, e, conflict) if repo else []
    text = conflict_text(e, o, conflict, waits, hunks)
    if it and not machine_state("change", it["state"]).terminal:
        try:
            st.transition("change", e["id"], it["state"], it["version"],
                          event={"text": text, "audience": {"worktree": e.get("worktree")}, "severity": "info"})
        except StaleVersion:
            return None
    e["conflict_told"] = o["id"]
    ChangeRecords(st, d).save(e)
    wlog(d, text)
    return text


def hold_for_overlap(st, d, e, o, shared, conflict, waits=True, repo=None):
    """The change tick's lines for a change held behind an older one it overlaps (waits; a red fix's: not held); its
    author told once of a conflict."""
    did = []
    if conflict and e.get("conflict_told") != o["id"]:
        text = tell_conflict(st, d, e, o, conflict, waits, repo)
        if text is None:
            return did
        did.append(text)
    if waits and _behind_said.get((e["id"], "overlap")) != o["id"]:
        _behind_said[(e["id"], "overlap")] = o["id"]
        did.append(f"{e['id']}: its check waits for {o['id']} ({o.get('status')}): both change {shared[0]}"
                   + (f" (+{len(shared) - 1} more)" if len(shared) > 1 else ""))
    return did


def backlog_batchable(st: Any, repo: str, d: str, e: dict[str, Any], t: float, facts: dict[str, Any] | None = None, *, ctx: Context) -> bool:
    """Whether queued entry e may join a backlog batch now (THE BACKLOG BATCH, above): its KIND lets it (no red fix,
    no --no-batch, no batch_alone, no identity change it cannot tell: changekind.py) and its state does (queued, no verify round, no
    retry due, no own run or live checker)."""
    rem = e.get("remote") or {}
    if e.get("status") != "queued" or e.get("backlog_batch") or e.get("verify") or float(e.get("retry_after") or 0) > t:
        return False
    if rem.get("run") is not None and not rem.get("collected_at"):  # its own run: a checker adopts it
        return False
    if checker_alive(st, e["id"]):
        return False
    return kind_of(st, repo, e, facts, t, ctx=ctx)["backlog_batch"] is None


def kind_of(st, repo, e, facts, t, *, ctx):
    """Entry e's kind (changekind.py classify_change) under facts (queue_facts of the gate's reds when None), read as
    it is now: a flag set since the tick began (push_alone, batch_alone, known_red) counts."""
    return classify_change(e, facts if facts is not None else facts_for(gate_reds(repo, ctx=ctx), st), t, repo, ctx=ctx)


_behind_said = process_cache({})  # (entry id, what) -> batch id: said once per batch
