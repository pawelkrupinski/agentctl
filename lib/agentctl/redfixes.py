# agentctl, part `redfixes`: a red's candidate fixes: queued and landed Fixes-Red changes, refuted and set-aside ones, a fix on main.
# A real module (lib/agentctl/__init__.py MODULES): its own imports, the standard library's and MODULES' only.

import re
import time
from contextlib import suppress
from typing import Any
from agentctl.core import attempt, cherry_landed, git, git_ok, process_cache, remote
from agentctl.timefmt import parse_time, ts
from agentctl.forge import gh_json
from agentctl.trailers import trailer_values
from agentctl.waitingdir import waiting_entries, waiting_state
from agentctl.redrecord import red_names
from agentctl.clones import fetch_main
from agentctl.redfixread import (
    FIX_OPEN, RED_MAIN_REFETCH, fix_check, fix_reaches_red, landed_fix_commits, red_unrelated)
from agentctl.patches import equivalent_on, patch_id
from agentctl.fixchange import fixes_named, red_fix_fresh
from agentctl.redlink import red_change_link


def commit_fixes(repo, sha, store):
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


def red_fix_facts(repo, store):
    """What links a red to its fix: the queue's Fixes-Red changes, the Fixes-Red commits on main, and every run any
    spell of each red key was seen in (key_runs: a red whose own item lost a run -- a duplicate spell, an import --
    still meets the change that names that run)."""
    entries, untrailered = [], []
    try:
        d, _c, _w = waiting_state(repo)
        for e in waiting_entries(d, open_only=False):
            recent = attempt(lambda: e.get("status") in FIX_OPEN or (e.get("status") == "landed" and time.time() - parse_time(
                e.get("landed_at") or e.get("updated") or e.get("created") or ts()).timestamp() < 7 * 86400), False,
                catch=(ValueError, TypeError))
            fx = entry_fixes(repo, e, store) if recent or e.get("fixes") else None
            if fx and not red_unrelated(repo, e):
                entries.append({**e, "fixes": fx})
            elif not fx and e.get("status") in FIX_OPEN:  # no Fixes-Red: linked only by the files it edits
                untrailered.append(e)
    except (Exception, SystemExit) as ex:  # AN UNREAD QUEUE IS NO EMPTY QUEUE (red_fix_held): said, never "nothing queued"
        queue_unread = str(ex)[:200] or type(ex).__name__
    else:
        queue_unread = None
    landed = attempt(lambda: landed_fix_commits(store, repo), [], catch=(Exception, SystemExit))
    key_runs, key_aside = {}, {}
    with suppress(Exception):
        for it in (store.items("red") if store is not None else []):
            k = it["data"].get("key") or it["id"]
            key_runs.setdefault(k, set()).update(map(str, it["data"].get("runs") or []))
            key_aside.setdefault(k, set()).update(set(it["data"].get("refuted") or [])
                                                  | set(it["data"].get("unverified") or []))
    # THE FIXES' CONTENT (simplification-2026-10-31 item 3): the patch-id of every fix commit a red could take and of
    # every fix it refuted or set aside, so a rebased copy of a refuted fix -- another sha, the same change -- counts no
    # more than the original (red_fix_of). A sha whose patch cannot be read is matched by sha alone.
    shas = {e.get("landed_sha") for e in entries if e.get("landed_sha")} | {row[0] for row in landed}
    with suppress(Exception):
        for it in (store.items("red") if store is not None else []):
            shas |= set(it["data"].get("refuted") or []) | set(it["data"].get("unverified") or [])
    patches = {}
    for sha in shas:
        pid = attempt(lambda: patch_id(repo, sha), None, catch=(Exception, SystemExit))
        if pid:
            patches[sha] = pid
    landed_at = {}
    for e in entries:
        sha = e.get("landed_sha")
        if sha and not e.get("landed_at") and repo:
            r = git(repo, "log", "-1", "--format=%ct", sha, check=False)
            if r.returncode == 0 and r.stdout.strip().isdigit():
                landed_at[sha] = float(r.stdout.strip())
    return {"entries": entries, "untrailered": untrailered, "landed": landed, "key_runs": {k: sorted(v) for k, v in key_runs.items()},
            "key_aside": {k: sorted(v) for k, v in key_aside.items() if v}, "landed_at": landed_at,
            "patches": patches, "repo": repo, "queue_unread": queue_unread}


def red_refuted(d):
    """The red's refuted fixes, each once, in the order they were refuted. A list that took one sha many times (784badc4406e.3,
    2026-11-11: 2 shas stored 593 times) counts its distinct fixes, never its entries."""
    return list(dict.fromkeys(d.get("refuted") or []))


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


def red_fix_candidates(d, facts):
    """Every fix that may be on its way for red data d, best first: the queue's changes naming one of its runs (the
    newest first), the `Fixes-Red:` commits landed on main since it opened (the newest first), then its own fixers'
    `LANDED <sha>` verdicts since it opened (the newest first). Unjudged: red_fix_of drops the ones set aside."""
    names = red_names(d) | set((facts.get("key_runs") or {}).get(d.get("key")) or [])
    since = float(d.get("opened_at") or 0) - 3600
    repo = facts.get("repo")

    def its_own(named, shas):  # PER RED: named by its key, or a fix its own failing job reads (fix_reaches_red)
        return d.get("key") in named or any(fix_reaches_red(repo, x, d) for x in shas if x) or not any(shas)
    for e in sorted(facts.get("entries") or [], key=lambda e: e.get("created") or "", reverse=True):
        named = fixes_named(e) & names
        if not named or not its_own(named, list(e.get("commits") or []) + [e.get("landed_sha")]):
            continue
        if e.get("status") in FIX_OPEN:
            yield {"entry": e["id"], "status": e["status"], "author": {"worktree": e.get("worktree"),
                                                                       "session": e.get("session")}}
        elif e.get("status") == "landed" and e.get("landed_sha"):
            at = parse_time(e.get("landed_at") or e.get("updated") or e.get("created") or ts()).timestamp()
            if not e.get("landed_at") and (facts.get("landed_at") or {}).get(e["landed_sha"]) is not None:
                at = float(facts["landed_at"][e["landed_sha"]])  # its commit's time: a file's `updated` moves on
            if at >= since:  # landed before this spell opened: the run that opened it already outlived it
                yield {"entry": e["id"], "status": "landed", "sha": e["landed_sha"], "at": at}
    for e in [] if d.get("gate") else facts.get("untrailered") or []:  # A QUEUED CHANGE EDITING THE RED'S OWN
        # WORKFLOW FILE (issue 20261106T102105-327f4c: family-shards fixed exactly Identity family explorer #2's shard
        # timeouts in its workflow, with no trailer, while fixers were sent whose only fix would conflict with it).
        # NEVER A GATE RED'S, NOR BY A SCRIPT ALONE (2026-11-20, Main #2716 and Order independence #89): metrics-packages,
        # no trailer, edited scripts/ci/coupling_ratchet.py -- read by both failing jobs, as by most -- and was taken
        # for both reds' fix "landing on red (Fixes-Red)"; Main's own fixer was stopped for it while it waited on that
        # very gate. A gate red's fix carries `Fixes-Red:` (nothing else lands while it is red); untrailered, only the
        # change to its failing step, or to the code only that step runs (redlink.py), links a change to an outside red.
        if {str(x) for x in e.get("diagnoses") or []} & {str(x) for x in names}:
            continue  # DIAGNOSTICS ARE NO FIX: a `Diagnoses-Red:` change never links its red, whatever it edits
        link = red_change_link(repo, d, e.get("commits") or [])  # what it changes of the failing step (redlink.py)
        if link:
            yield {"entry": e["id"], "status": e["status"], "linked": link,
                   "author": {"worktree": e.get("worktree"), "session": e.get("session")}}
    for sha, ct, vals, _s in facts.get("landed") or []:
        if vals & names and ct >= since and its_own(vals & names, [sha]):
            yield {"entry": None, "status": "landed", "sha": sha, "at": ct}
    for h in reversed(d.get("history") or []):
        m = RED_LANDED_VERDICT.match(str(h.get("outcome") or ""))
        at = attempt(lambda: parse_time(h.get("at")).timestamp() if m and h.get("at") else None, None,
                     catch=(ValueError, TypeError))
        if m and at is not None and at >= since and its_own(set(), [m.group(1)]):  # an owner's turn held every red
            yield {"entry": None, "status": "landed", "sha": m.group(1), "at": at, "via": "its fixer's LANDED"}


def red_run_on_way(d, snap, repo):
    """(run, sha): an undecided run on main of red d's workflow, newer than its failing run, containing a commit since
    that run's head that reaches its failing job (fix_reaches_red) -- a fix landed with no Fixes-Red, which that run
    will prove or refute. None when there is none (or the clone cannot tell)."""
    own = d.get("last_run") or {}
    base = own.get("headSha")
    if not repo or not base or not snap:
        return None
    runs = next((g.get("runs") or [] for g in (snap.get("gate") or []) + (snap.get("outside") or [])
                 if g.get("name") == d.get("workflow")), [])
    for r in runs:
        if r.get("status") == "completed" or r.get("headBranch", "main") != "main" or not r.get("headSha") \
                or int(r.get("databaseId") or 0) <= int(own.get("databaseId") or 0) or r["headSha"] == base:
            continue
        got = git(repo, "rev-list", "--max-count=50", f"{base}..{r['headSha']}", check=False)
        for sha in (got.stdout.split() if got.returncode == 0 else []):
            if fix_reaches_red(repo, sha, d):
                return r, sha
    return None


_landed_fetched = process_cache({})


def red_sha_on_main(repo, sha):
    """sha is on <remote>/main in clone repo -- main fetched (at most once per RED_MAIN_REFETCH for that sha) when the
    clone does not show it yet: a fixer's LANDED comes seconds after its push. Never in a clone that tracks no main."""
    ref = f"refs/remotes/{remote()}/main"
    if not repo or not sha or not git_ok(repo, "rev-parse", "--verify", "-q", ref):
        return False
    if git_ok(repo, "merge-base", "--is-ancestor", sha, ref):
        return True
    if time.time() - _landed_fetched.get((repo, sha), 0) < RED_MAIN_REFETCH:
        return False
    _landed_fetched[(repo, sha)] = time.time()
    return fetch_main(repo)[0] and git_ok(repo, "merge-base", "--is-ancestor", sha, ref)


# A REPO WITH NO CLONE HERE IS ASKED (issue 20261102T085329-930a4a: ci-runner holds no clone of bitcashier/core, so
# red_sha_on_main read False for every fixer's LANDED and the red was offered again after each -- 12 leases, 11
# attempts): its forge's compare of the sha with main says whether main contains it.
_api_on_main = process_cache({})


def red_sha_on_main_api(o_r, sha):
    """main of o_r contains sha, by the forge's compare (asked again at most once per RED_MAIN_REFETCH while it does
    not); False when it cannot tell."""
    if not o_r or not sha:
        return False
    got = _api_on_main.get((o_r, sha))
    if got and (got[0] or time.time() - got[1] < RED_MAIN_REFETCH):
        return got[0]
    try:
        c = gh_json(o_r, "api", f"repos/{o_r}/compare/{sha}...main") or {}
        on = c.get("status") in ("ahead", "identical")
    except (Exception, SystemExit):
        on = False
    _api_on_main[(o_r, sha)] = (on, time.time())
    return on


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
# sha is resolved against main (red_main_equivalent): kept when main contains it, followed to the main commit carrying
# its patch (a re-cut on landing), and no fix at all when main carries neither; and a fix that landed before this spell
# opened is no fix for it (the run that opened it already contained it), however its entry reads. A red already
# fix-pending on such a sha is corrected the same way (red_fix_pending).
_main_equivalents = process_cache({})


def red_main_equivalent(repo, sha):
    """The commit of <remote>/main that carries landed fix sha: sha itself when main contains it, else the main commit
    with its patch (or content twin); None when main carries neither -- it never landed. sha as given when the clone
    cannot tell (no repo or main ref, git unsure)."""
    ref = f"refs/remotes/{remote()}/main"
    if not repo or not sha or not git_ok(repo, "rev-parse", "--verify", "-q", ref):
        return sha
    if red_sha_on_main(repo, sha):
        return sha
    if not git_ok(repo, "cat-file", "-e", f"{sha}^{{commit}}"):
        return None  # main fetched (red_sha_on_main) and still no such commit: it is on no main
    key = (repo, sha, git(repo, "rev-parse", ref, check=False).stdout.strip())
    if key not in _main_equivalents:
        commits, left = cherry_landed(repo, ref, sha, f"{sha}~1")
        if commits is None:
            _main_equivalents[key] = sha  # git cannot tell (a root commit): unsure counts
        elif left:
            _main_equivalents[key] = None
        else:
            _main_equivalents[key] = attempt(lambda: equivalent_on(repo, sha, ref) or sha, sha,
                                             catch=(Exception, SystemExit))
    return _main_equivalents[key]


def red_fix_on_main(fix, repo):
    """A landed fix with its sha on main (red_main_equivalent: `recut` names the sha it was given), None when main never
    got it; any other fix as given."""
    if not fix or fix.get("status") != "landed" or not fix.get("sha"):
        return fix
    on = red_main_equivalent(repo, fix["sha"])
    if on is None:
        return None
    return fix if on == fix["sha"] else {**fix, "sha": on, "recut": fix["sha"]}


def _counted_fix(fix: Any, facts: Any, d: Any, store: Any, since: Any) -> Any:
    """A fix candidate as red_fix_of counts it (corrected by its store item and by main), or None when it counts no more."""
    if fix.get("via") and not (red_sha_on_main(facts.get("repo"), fix["sha"]) if facts.get("repo") else
                               red_sha_on_main_api(d.get("repo"), fix["sha"])):
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


