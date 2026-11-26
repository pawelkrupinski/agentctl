# agentctl, part `redfixes`: a red's candidate fixes: queued and landed Fixes-Red changes, refuted and set-aside ones, a fix on main.
# A real module (lib/agentctl/__init__.py MODULES): its own imports, the standard library's and MODULES' only.

from agentctl.clock import epoch_now
import re
import time
from contextlib import suppress
from typing import Any
from agentctl.timefmt import parse_time, ts
from agentctl.core import attempt, git, process_cache
from agentctl.trailers import trailer_values
from agentctl.waitingdir import waiting_entries, waiting_state
from agentctl.patches import patch_id
from agentctl.inboxentry import inbox_write
from agentctl.redrecord import red_names, red_what
from agentctl.redfixread import FIX_OPEN, red_unrelated
from agentctl.fixchange import fixes_named, red_fix_fresh
from agentctl.redlink import red_change_link
from agentctl.redfixcheck import fix_check, fix_reaches_red, landed_fix_commits
from agentctl.maincontain import MainContainment


def commit_fixes(repo: str, sha: str, store: Any) -> set[str]:
    """The `Fixes-Red:` values of commit sha that count as fixes (fix_check)."""
    v = git(repo, "log", "-1", "--format=%(trailers:key=Fixes-Red,valueonly,separator=%x2C)", sha, check=False)
    vals = trailer_values(v.stdout if v.returncode == 0 else "")
    return fix_check(repo, sha, vals, store)[0] if vals else set()


_trailer_fixes = process_cache({})


def _commit_fixes(repo: Any, c: Any, store: Any) -> Any:
    """The runs commit c's Fixes-Red trailers name that count (cached per commit)."""
    if c not in _trailer_fixes:
        r = git(repo, "log", "-1", "--format=%(trailers:key=Fixes-Red,valueonly,separator=%x2C)", c, check=False)
        vals = trailer_values(r.stdout) if r.returncode == 0 else set()
        _trailer_fixes[c] = sorted(fix_check(repo, c, vals, store)[0]) if vals else []  # only those that count
    return _trailer_fixes[c]


def entry_fixes(repo, e, store):
    """The runs a queued change says it fixes: its `fixes` (recorded at submission from its Fixes-Red trailers), else
    read again from its commits' trailers (cached per commit) -- an entry that lost the field still links its red."""
    if e.get("fixes"):
        return sorted(fixes_named(e))
    out = []
    for c in e.get("commits") or []:
        out += _commit_fixes(repo, c, store)
    return out


def _recent_entry(e: Any) -> Any:
    """Queue entry e is open, or landed in the last 7 days (an unreadable stamp: not recent)."""
    return attempt(lambda: e.get("status") in FIX_OPEN or (e.get("status") == "landed" and epoch_now() - parse_time(
        e.get("landed_at") or e.get("updated") or e.get("created") or ts()).timestamp() < 7 * 86400), False,
        catch=(ValueError, TypeError))


def _open_untrailered(e: Any, fx: Any) -> Any:
    """An open entry naming no fix: linked only by the files it edits."""
    return not fx and e.get("status") in FIX_OPEN


def _queue_facts(repo: Any, store: Any) -> Any:
    """(the queue's changes that name fixes, those that name none but are open, why the queue could not be read)."""
    entries: list[Any] = []
    untrailered: list[Any] = []
    try:
        d, _c, _w = waiting_state(repo)
        for e in waiting_entries(d, open_only=False):
            fx = entry_fixes(repo, e, store) if _recent_entry(e) or e.get("fixes") else None
            if fx and not red_unrelated(repo, e):
                entries.append({**e, "fixes": fx})
            elif _open_untrailered(e, fx):
                untrailered.append(e)
    except (Exception, SystemExit) as ex:  # AN UNREAD QUEUE IS NO EMPTY QUEUE (red_fix_held): said, never "nothing queued"
        return entries, untrailered, str(ex)[:200] or type(ex).__name__
    return entries, untrailered, None


def _each_red(store: Any, visit: Any) -> None:
    """visit(item) for each red item of the store (an unreadable store: those read so far)."""
    with suppress(Exception):
        for it in (store.items("red") if store is not None else []):
            visit(it)


def _set_aside_of(data: Any) -> Any:
    return set(data.get("refuted") or []) | set(data.get("unverified") or [])


class KeyedReds:
    """What the store's red items say per red key: every run any spell of it was seen in, the fixes set aside."""

    def __init__(self) -> None:
        self.runs: dict[Any, set[str]] = {}
        self.aside: dict[Any, set[Any]] = {}

    def add(self, it: Any) -> None:
        k = it["data"].get("key") or it["id"]
        self.runs.setdefault(k, set()).update(map(str, it["data"].get("runs") or []))
        self.aside.setdefault(k, set()).update(_set_aside_of(it["data"]))


def _patch_ids(repo: Any, shas: Any) -> Any:
    """{sha: patch-id} of the shas whose patch can be read (a sha whose patch cannot be read is matched by sha alone)."""
    patches = {}
    for sha in shas:
        pid = attempt(lambda: patch_id(repo, sha), None, catch=(Exception, SystemExit))
        if pid:
            patches[sha] = pid
    return patches


def _landed_times(repo: Any, entries: Any) -> Any:
    """{sha: commit time} of the landed entries' shas that carry no landed_at."""
    landed_at = {}
    for e in entries:
        sha = e.get("landed_sha")
        if sha and not e.get("landed_at") and repo:
            r = git(repo, "log", "-1", "--format=%ct", sha, check=False)
            if r.returncode == 0 and r.stdout.strip().isdigit():
                landed_at[sha] = float(r.stdout.strip())
    return landed_at


def red_fix_facts(repo, store):
    """What links a red to its fix: the queue's Fixes-Red changes, the Fixes-Red commits on main, and every run any
    spell of each red key was seen in (key_runs: a red whose own item lost a run -- a duplicate spell, an import --
    still meets the change that names that run)."""
    entries, untrailered, queue_unread = _queue_facts(repo, store)
    landed = attempt(lambda: landed_fix_commits(store, repo), [], catch=(Exception, SystemExit))
    keyed = KeyedReds()
    _each_red(store, keyed.add)
    # THE FIXES' CONTENT (simplification-2026-10-31 item 3): the patch-id of every fix commit a red could take and of
    # every fix it refuted or set aside, so a rebased copy of a refuted fix -- another sha, the same change -- counts no
    # more than the original (red_fix_of). A sha whose patch cannot be read is matched by sha alone.
    shas = {e.get("landed_sha") for e in entries if e.get("landed_sha")} | {row[0] for row in landed}
    _each_red(store, lambda it: shas.update(_set_aside_of(it["data"])))
    patches = _patch_ids(repo, shas)
    landed_at = _landed_times(repo, entries)
    return {"entries": entries, "untrailered": untrailered, "landed": landed, "key_runs": {k: sorted(v) for k, v in keyed.runs.items()},
            "key_aside": {k: sorted(v) for k, v in keyed.aside.items() if v}, "landed_at": landed_at,
            "patches": patches, "repo": repo, "queue_unread": queue_unread}


def red_refuted(d):
    """The red's refuted fixes, each once, in the order they were refuted. A list that took one sha many times (784badc4406e.3,
    2026-11-11: 2 shas stored 593 times) counts its distinct fixes, never its entries."""
    return list(dict.fromkeys(d.get("refuted") or []))


def fix_failed_said(d: dict[str, Any], sha: str, last: dict[str, Any]) -> str:
    """The FIX FAILED line of red data d (its fix `sha` still fails in run `last`), said to the inbox; the line."""
    line = f"FIX FAILED: {red_what(d)}: #{last.get('number')} still fails this way (fix {sha[:10]})"
    inbox_write("red", line, [line], [d.get("line") or ""], 1)
    return line


def red_refuted_with(d: dict[str, Any], sha: str) -> list[str]:
    """The red's refuted fixes (red_refuted), fix `sha` among them."""
    return red_refuted({"refuted": (d.get("refuted") or []) + [sha]})


def red_set_aside(d, facts):
    """(shas, patch-ids) of the fixes this red -- or any spell of its key (facts' key_aside) -- refuted or set aside
    unverified: a candidate fix is one of them when its sha (or a prefix) is, or its CONTENT is -- the same patch-id
    under another sha (a rebase, a cherry-pick)."""
    shas = set(d.get("refuted") or []) | set(d.get("unverified") or []) \
        | set((facts.get("key_aside") or {}).get(d.get("key")) or [])
    patches = facts.get("patches") or {}
    return shas, {patches[x] for x in shas if patches.get(x)}


def red_fix_is_set_aside(sha, aside, facts):
    """A candidate fix sha is one the red set aside: by sha, or by CONTENT -- its patch-id read here when the facts did
    not read it (a sha the store supplied: a change's landed tip). 2026-11-12, b059f05c70fa: 19482f5c08 set aside
    unverified, its pre-rebase copy b6d3d8235 (the same patch, a change item's tip) parked the red again for 6 h."""
    shas, pids = aside
    if sha in shas or any(sha.startswith(x) for x in shas):
        return True
    if not pids:
        return False
    patches = facts.setdefault("patches", {})
    if sha not in patches and facts.get("repo"):
        try:
            patches[sha] = patch_id(facts["repo"], sha)
        except (Exception, SystemExit):
            patches[sha] = None
    return patches.get(sha) in pids


RED_LANDED_VERDICT = re.compile(r"^LANDED ([0-9a-f]{7,40})\b")


class FixCandidates:
    """The fixes that may be on their way for one red, best first: a method per source (the queue's changes, the
    untrailered ones editing the red's own workflow, the commits landed on main, its fixers' verdicts)."""

    def __init__(self, d: Any, facts: Any) -> None:
        self.d, self.facts = d, facts
        self.names = red_names(d) | set((facts.get("key_runs") or {}).get(d.get("key")) or [])
        self.since = float(d.get("opened_at") or 0) - 3600
        self.repo = facts.get("repo")

    def its_own(self, named: Any, shas: Any) -> Any:  # PER RED: named by its key, or a fix its own failing job reads (fix_reaches_red)
        return self.d.get("key") in named or any(fix_reaches_red(self.repo, x, self.d) for x in shas if x) or not any(shas)

    def all(self) -> Any:
        yield from self.queued()
        yield from self.untrailered()
        yield from self.landed()
        yield from self.verdicts()

    def queued(self) -> Any:
        """The queue's changes naming one of the red's runs (the newest first), open or landed since the red opened."""
        for e in sorted(self.facts.get("entries") or [], key=lambda e: e.get("created") or "", reverse=True):
            named = fixes_named(e) & self.names
            if not named or not self.its_own(named, list(e.get("commits") or []) + [e.get("landed_sha")]):
                continue
            fix = self.queued_fix(e)
            if fix:
                yield fix

    def queued_fix(self, e: Any) -> Any:
        if e.get("status") in FIX_OPEN:
            return {"entry": e["id"], "status": e["status"], "author": {"worktree": e.get("worktree"),
                                                                        "session": e.get("session")}}
        if e.get("status") == "landed" and e.get("landed_sha"):
            at = self.landed_at(e)
            if at >= self.since:  # landed before this spell opened: the run that opened it already outlived it
                return {"entry": e["id"], "status": "landed", "sha": e["landed_sha"], "at": at}
        return None

    def landed_at(self, e: Any) -> Any:
        """When landed entry e landed: its commit's time when the entry names none (a file's `updated` moves on)."""
        at = parse_time(e.get("landed_at") or e.get("updated") or e.get("created") or ts()).timestamp()
        known = (self.facts.get("landed_at") or {}).get(e["landed_sha"])
        return float(known) if not e.get("landed_at") and known is not None else at

    def untrailered(self) -> Any:
        """A QUEUED CHANGE EDITING THE RED'S OWN WORKFLOW FILE (issue 20261106T102105-327f4c: family-shards fixed exactly
        Identity family explorer #2's shard timeouts in its workflow, with no trailer, while fixers were sent whose only
        fix would conflict with it). NEVER A GATE RED'S, NOR BY A SCRIPT ALONE (2026-11-20, Main #2716 and Order
        independence #89): metrics-packages, no trailer, edited scripts/ci/coupling_ratchet.py -- read by both failing
        jobs, as by most -- and was taken for both reds' fix "landing on red (Fixes-Red)"; Main's own fixer was stopped
        for it while it waited on that very gate. A gate red's fix carries `Fixes-Red:` (nothing else lands while it is
        red); untrailered, only the change to its failing step, or to the code only that step runs (redlink.py), links
        a change to an outside red."""
        for e in [] if self.d.get("gate") else self.facts.get("untrailered") or []:
            if {str(x) for x in e.get("diagnoses") or []} & {str(x) for x in self.names}:
                continue  # DIAGNOSTICS ARE NO FIX: a `Diagnoses-Red:` change never links its red, whatever it edits
            link = red_change_link(self.repo, self.d, e.get("commits") or [])  # what it changes of the failing step (redlink.py)
            if link:
                yield {"entry": e["id"], "status": e["status"], "linked": link,
                       "author": {"worktree": e.get("worktree"), "session": e.get("session")}}

    def landed(self) -> Any:
        """The `Fixes-Red:` commits landed on main since the red opened (the newest first)."""
        for sha, ct, vals, _s in self.facts.get("landed") or []:
            if vals & self.names and ct >= self.since and self.its_own(vals & self.names, [sha]):
                yield {"entry": None, "status": "landed", "sha": sha, "at": ct}

    def verdicts(self) -> Any:
        """Its own fixers' `LANDED <sha>` verdicts since it opened (the newest first)."""
        for h in reversed(self.d.get("history") or []):
            m = RED_LANDED_VERDICT.match(str(h.get("outcome") or ""))
            at = attempt(lambda: parse_time(h.get("at")).timestamp() if m and h.get("at") else None, None,
                         catch=(ValueError, TypeError))
            if m and at is not None and at >= self.since and self.its_own(set(), [m.group(1)]):  # an owner's turn held every red
                yield {"entry": None, "status": "landed", "sha": m.group(1), "at": at, "via": "its fixer's LANDED"}


def red_fix_candidates(d, facts):
    """Every fix that may be on its way for red data d, best first: the queue's changes naming one of its runs (the
    newest first), the `Fixes-Red:` commits landed on main since it opened (the newest first), then its own fixers'
    `LANDED <sha>` verdicts since it opened (the newest first). Unjudged: red_fix_of drops the ones set aside."""
    yield from FixCandidates(d, facts).all()


def _workflow_runs(snap: Any, workflow: Any) -> Any:
    """The snapshot's runs on main of the workflow (gate first, then outside), newest first."""
    return next((g.get("runs") or [] for g in (snap.get("gate") or []) + (snap.get("outside") or [])
                 if g.get("name") == workflow), [])


def _newer_undecided(r: Any, own: Any, base: Any) -> Any:
    """Run r is an undecided run on main, newer than the failing run (own, whose head is base) and of another head."""
    return not (r.get("status") == "completed" or r.get("headBranch", "main") != "main" or not r.get("headSha")
                or int(r.get("databaseId") or 0) <= int(own.get("databaseId") or 0) or r["headSha"] == base)


def _reaching_commit(repo: Any, base: Any, head: Any, d: Any) -> Any:
    """The first commit since base up to head that reaches red d's failing job (fix_reaches_red), else None."""
    got = git(repo, "rev-list", "--max-count=50", f"{base}..{head}", check=False)
    return next((sha for sha in (got.stdout.split() if got.returncode == 0 else []) if fix_reaches_red(repo, sha, d)), None)


def red_run_on_way(d, snap, repo):
    """(run, sha): an undecided run on main of red d's workflow, newer than its failing run, containing a commit since
    that run's head that reaches its failing job (fix_reaches_red) -- a fix landed with no Fixes-Red, which that run
    will prove or refute. None when there is none (or the clone cannot tell)."""
    own = d.get("last_run") or {}
    base = own.get("headSha")
    if not repo or not base or not snap:
        return None
    for r in _workflow_runs(snap, d.get("workflow")):
        sha = _reaching_commit(repo, base, r["headSha"], d) if _newer_undecided(r, own, base) else None
        if sha:
            return r, sha
    return None


# ONE SET-ASIDE FIX HIDES NO OTHER (2026-11-13, Verify scrape recording #26, 784badc4406e.4): the queue entry of a refuted
# fix whose file still read `waiting` (b6d3d8235, naming #23, a run of the same red key) was THE candidate red_fix_of
# returned; the store turned it into its landed, set-aside sha and red_fix_current dropped it -- and with it 8d06b1a5ab,
# landed with `Fixes-Red: 26`, which was never looked at. The red was re-offered and its fixer put "hold it as fixed by
# 8d06b1a5ab" on the owner. Every candidate is judged in turn (corrected by the store when one is given); the first one
# still standing is the fix.
# A LANDED FIX IS A COMMIT ON MAIN (2026-11-16, Order independence #78, b3f51c5166b0.2): "fix landed in b6d3d8235,
# awaiting a run containing it" -- b6d3d8235 was a change item's `tip`, the pre-rebase commit of its check tree; the
# landing re-cut it as 19482f5c08 (Verify scrape recording #23's Filmweb fix, 2026-11-11), so no main ever held it. The
# change's entry FILE still read `joined` and also named "40" (a run of an earlier spell of this key), so five days
# later the new spell took it as a fix on its way, and the store turned it into the landed phantom. Now a landed fix's
# sha is resolved against main (maincontain.MainContainment.carrying): kept when main contains it, followed to the main commit carrying
# its patch (a re-cut on landing), and no fix at all when main carries neither; and a fix that landed before this spell
# opened is no fix for it (the run that opened it already contained it), however its entry reads. A red already
# fix-pending on such a sha is corrected the same way (red_fix_pending).
def red_fix_on_main(fix, repo):
    """A landed fix with its sha on main (MainContainment.carrying: `recut` names the sha it was given), None when main
    never got it; any other fix as given."""
    if not fix or fix.get("status") != "landed" or not fix.get("sha"):
        return fix
    on = MainContainment(repo).carrying(fix["sha"])
    if on is None:
        return None
    return fix if on == fix["sha"] else {**fix, "sha": on, "recut": fix["sha"]}


def _counted_fix(fix: Any, facts: Any, d: Any, store: Any, since: Any) -> Any:
    """A fix candidate as red_fix_of counts it (corrected by its store item and by main), or None when it counts no more."""
    if fix.get("via") and MainContainment(facts.get("repo"), d.get("repo")).contains(fix["sha"]) is not True:
        return None  # a fixer's LANDED counts once its commit is on main (a verdict alone proves nothing)
    if store is not None:
        fix = red_fix_fresh(store, fix)
    fix = red_fix_on_main(fix, facts.get("repo"))
    if fix and fix.get("status") == "landed" and fix.get("at") is not None and float(fix["at"]) < since:
        return None  # landed before this spell opened: the run that opened it already outlived it
    return fix


def red_fix_of(it, facts, store=None):
    """The fix on its way for this red, or None: {entry, status, sha?, at?}. A refuted or unverified fix -- by sha or
    by content -- counts no more, and is never the reason a later fix is not seen. store: each candidate corrected by
    its change's store item first (red_fix_fresh). A landed fix is a commit on main (red_fix_on_main) that landed
    since this spell opened."""
    d = it["data"]
    aside = red_set_aside(d, facts)
    since = float(d.get("opened_at") or 0) - 3600
    for candidate in red_fix_candidates(d, facts):
        fix = _counted_fix(candidate, facts, d, store, since)
        if fix and not (fix.get("sha") and red_fix_is_set_aside(fix["sha"], aside, facts)):
            return fix
    return None


def red_entry_status(facts, eid):
    return next((e.get("status") for e in facts.get("entries") or [] if e.get("id") == eid), None)


def fixes_of(repo, commits, store):
    """The run ids/numbers the commits' Fixes-Red: trailers name -- those that count as fixes (fix_check)."""
    out = set()
    for c in commits:
        out |= commit_fixes(repo, c, store)  # only those that count: touching what the red's job reads (fix_check)
    return out


