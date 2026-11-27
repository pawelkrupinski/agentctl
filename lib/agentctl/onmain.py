# agentctl, part `onmain`: IS THIS COMMIT ON MAIN -- ONE CONCEPT (2026-11-27, was ancestry + landstate + patches + maincontain):
# the landed check (landed_state: ancestor, then patch-equivalent), a patch's identity (patch ids, empty commits),
# containment of a run's head (contains, contains_local) and MainContainment (a clone's git or the forge's compare).
# The ancestry test itself is core.is_ancestor / core.commit_known, beside the git runner every caller holds.
# A real module (lib/agentctl/__init__.py MODULES): its own imports, the standard library's and MODULES' only.

import os
from dataclasses import dataclass
from enum import Enum
from typing import Optional
from agentctl.clock import epoch_now
from agentctl.core import (
    attempt, cherry_landed, commit_known, content_twin, git, git_ok, is_ancestor, process_cache, remote, run_cmd)
from agentctl.clones import fetch_main
from agentctl.forge import gh_json
from agentctl.redfixread import RED_MAIN_REFETCH
from agentctl.trailers import commit_trailer_values


class Landed(Enum):
    LANDED = "landed"
    NOT_LANDED = "not landed"
    UNKNOWN = "unknown"  # git cannot tell (the head is not in this clone, main's ref is missing): never read as either


@dataclass(frozen=True)
class LandState:
    """Whether a head's work is on main. `how`: "ancestor" or "patch-equivalent" (landed only; a content twin counts as
    patch-equivalent). `commits` / `left`: the head's commits not in main's history (git cherry) and those of them with
    no equivalent on main -- what a NOT_LANDED change still owes. `via`: the main commit carrying the head (the head
    itself as an ancestor), asked for with with_via (a patch-id scan of main) and set for LANDED only."""
    state: Landed
    how: Optional[str] = None
    commits: tuple[str, ...] = ()
    left: tuple[str, ...] = ()
    via: Optional[str] = None

    @property
    def landed(self) -> bool:
        return self.state is Landed.LANDED

    @property
    def unknown(self) -> bool:
        return self.state is Landed.UNKNOWN


LANDED, NOT_LANDED, UNKNOWN = Landed.LANDED, Landed.NOT_LANDED, Landed.UNKNOWN


def landed_state(repo: str, main: str, head: str = "HEAD", *, after: Optional[str] = None,
                 with_via: bool = False) -> LandState:
    """THE LANDED CHECK: the work of `head` against `main` (a ref) in `repo`, by ancestry, then by content (git cherry:
    patch-equivalent, else a content twin on main). `after`: only the commits after it count (git cherry's limit).
    UNKNOWN when the head is no commit here or git cherry cannot compare; a caller decides what that means for it,
    and says so."""
    if not commit_known(repo, head):
        return LandState(UNKNOWN)
    if is_ancestor(repo, head, main):
        return LandState(LANDED, "ancestor", via=head if with_via else None)
    commits, left = cherry_landed(repo, main, head, *((after,) if after else ()))
    if commits is None or left is None:
        return LandState(UNKNOWN)
    if not commits or left:
        return LandState(NOT_LANDED, commits=tuple(commits), left=tuple(left))
    return LandState(LANDED, "patch-equivalent", tuple(commits), via=equivalent_on(repo, head, main) if with_via else None)


# NEVER AN EMPTY COMMIT (2026-10-27). An agent queued a commit, queued it again, and both entries ran: the later one's
# commit had already landed through the earlier one, its rebase (a cherry-pick with --keep-redundant-commits) left an
# EMPTY commit, and that was pushed -- four times on movies main (317fba9b3, 1fddd8951, 1d554f051, 693e1be78/82f4d9f55).
# Now every rebuild of an entry drops a commit that is patch-equivalent to its new base, or empty after the pick; an
# entry left with nothing is LANDED (already on main via <commit>); re-queueing supersedes the older entry; and the
# squash guard refuses any push carrying an empty commit unless it is a deliberate one with an `Allow-Empty:` trailer.
ALLOW_EMPTY_TRAILER = "Allow-Empty"


def deliberate_empty(repo: str, c: str) -> bool:
    """A commit made empty on purpose: `git commit --allow-empty` with an `Allow-Empty: <why>` trailer."""
    return bool(commit_trailer_values(repo, c, ALLOW_EMPTY_TRAILER))


def is_empty_commit(repo: str, c: str) -> bool:
    """A non-merge commit whose tree is its parent's (a root commit is never empty here)."""
    parents = git(repo, "rev-list", "--parents", "-n", "1", c).split()[1:]
    if len(parents) != 1:
        return False
    return bool(git(repo, "rev-parse", f"{c}^{{tree}}").strip() == git(repo, "rev-parse", f"{parents[0]}^{{tree}}").strip())


def patch_id(repo: str, c: str) -> Optional[str]:
    diff = git(repo, "show", "--format=", "--no-color", "--no-ext-diff", c, check=False).stdout
    if not diff.strip():
        return None
    r = run_cmd(["git", "patch-id", "--stable"], cwd=repo, input=diff)
    words = r.stdout.split()
    return str(words[0]) if words else None


def equivalent_on(repo: str, c: str, upstream: str, limit: int = 400) -> Optional[str]:
    """The commit of `upstream` (since its merge-base with c) that carries c's patch, or None (`git cherry`'s test,
    naming the commit), else its content twin (content_twin: same subject, author, date and changed lines)."""
    pid = patch_id(repo, c)
    if not pid:
        return None
    mb = git(repo, "merge-base", c, upstream, check=False).stdout.strip()
    rng = f"{mb}..{upstream}" if mb else upstream
    for u in git(repo, "rev-list", "--no-merges", "-n", str(limit), rng, check=False).stdout.split():
        if patch_id(repo, u) == pid:
            return str(u)
    twin: Optional[str] = content_twin(repo, c, upstream)
    return twin


def upstream_patch_ids(repo: str, since_commit: str, upstream: str, limit: int = 400) -> dict[str, str]:
    """{patch-id: commit} of `upstream`'s non-merge commits since its merge-base with `since_commit`, in one pass."""
    mb = git(repo, "merge-base", since_commit, upstream, check=False).stdout.strip()
    rng = f"{mb}..{upstream}" if mb else upstream
    log = git(repo, "log", "-p", "--no-merges", "--no-color", "--no-ext-diff", "-n", str(limit), "--format=commit %H",
              rng, check=False).stdout
    if not log.strip():
        return {}
    r = run_cmd(["git", "patch-id", "--stable"], cwd=repo, input=log)
    return {pid: c for pid, c in (l.split() for l in r.stdout.splitlines() if len(l.split()) == 2)}


def contains_local(repo: str, sha: str, head: str | None) -> bool | None:
    """True / False when both commits are here, else None."""
    if not head:
        return None
    if head == sha:
        return True
    if not commit_known(repo, head):
        return None
    return is_ancestor(repo, sha, head)


def last_fetch(repo: str) -> float:
    """When the clone last fetched (its FETCH_HEAD's mtime), 0 when never or unreadable."""
    path = git(repo, "rev-parse", "--git-path", "FETCH_HEAD", check=False).stdout.strip()
    try:
        return os.path.getmtime(os.path.join(repo, path)) if path else 0.0
    except OSError:  # never fetched: fetch now
        return 0.0


def contains(repo: str, sha: str, head: str | None, refetch_after: float) -> bool:
    """Whether a run's head commit contains sha, fetching main when head is unknown here -- at most once per head per
    refetch_after seconds (review 2026-11-26: a head that never arrives, force-pushed away, fetched main on every
    land-watch tick). The clone's own FETCH_HEAD says when it last fetched: no process state to keep."""
    if not head:
        return False
    if head == sha:
        return True
    if not commit_known(repo, head) and epoch_now() - last_fetch(repo) >= refetch_after:
        git(repo, "fetch", "--quiet", remote(), "main", check=False)
    return is_ancestor(repo, sha, head)


# ONE ON-MAIN CHECK (2026-11-26). Four copies answered it: redfixes.red_sha_on_main (the clone), red_sha_on_main_api
# (the forge, for a repo with no clone here: issue 20261102T085329-930a4a), red_main_equivalent (the main commit carrying
# a landed fix: itself or its re-cut) and dispatches.commit_on_main (ancestor or patch-equivalent). Characterised
# (tests/test_main_containment.py), they disagreed three ways, resolved here:
#  - A COMMIT MAIN HAS BUT THE CLONE HAS NOT FETCHED: red_sha_on_main fetched main and said yes; commit_on_main never
#    fetched and said "not on main" (a branch red called an orphan for it). Every route fetches, at most once per sha
#    per RED_MAIN_REFETCH.
#  - UNKNOWN IS NOT "NO": with no main ref (or git unable to compare) commit_on_main said False, red_sha_on_main False,
#    red_main_equivalent "sha" (unsure counts); the forge's compare failing was cached as False for RED_MAIN_REFETCH.
#    `contains` and `landed` answer None for "cannot tell", never cached; each caller says what None means for it.
#  - (no disagreement) a re-cut -- the patch on main under another sha -- is not CONTAINED (no ancestor) but is LANDED
#    and CARRIED by its twin: three questions, three methods.
_fetched: dict[tuple[str, str], float] = process_cache({})  # (clone, sha) -> when main was fetched for it


_api_seen: dict[tuple[str, str], tuple[bool, float]] = process_cache({})  # (o/r, sha) -> (on main, when asked)


_carried: dict[tuple[str, str, str], str | None] = process_cache({})  # (clone, sha, main) -> the main commit carrying it


def main_ref() -> str:
    return f"refs/remotes/{remote()}/main"


class MainContainment:
    """Main of one repository, asked through a clone (`repo`: git) or, with none here, the forge (`o_r`: its compare)."""

    def __init__(self, repo: str | None = None, o_r: str | None = None) -> None:
        self.repo, self.o_r = repo, o_r

    def contains(self, sha: str | None) -> bool | None:
        """Whether main's history holds sha: True, False, or None (cannot tell: no clone and no forge to ask, no main
        ref, a fetch or a compare that failed). The clone's main is fetched once per RED_MAIN_REFETCH when it does not
        show sha yet (a fixer's LANDED comes seconds after its push)."""
        if not sha:
            return None
        if self.repo:
            return self._git_contains(self.repo, sha)
        return self._api_contains(self.o_r, sha) if self.o_r else None

    def _git_contains(self, repo: str, sha: str) -> bool | None:
        if not git_ok(repo, "rev-parse", "--verify", "-q", main_ref()):
            return None
        if is_ancestor(repo, sha, main_ref()):
            return True
        if epoch_now() - _fetched.get((repo, sha), 0) < RED_MAIN_REFETCH:
            return False
        _fetched[(repo, sha)] = epoch_now()
        if not fetch_main(repo)[0]:
            return None
        return is_ancestor(repo, sha, main_ref())

    def _api_contains(self, o_r: str, sha: str) -> bool | None:
        got = _api_seen.get((o_r, sha))
        if got and (got[0] or epoch_now() - got[1] < RED_MAIN_REFETCH):
            return got[0]
        try:
            c = gh_json(o_r, "api", f"repos/{o_r}/compare/{sha}...main") or {}
        except (Exception, SystemExit):  # SystemExit: NoToken
            return None  # cannot tell: asked again next time, never remembered as "not on main"
        on = c.get("status") in ("ahead", "identical")
        _api_seen[(o_r, sha)] = (on, epoch_now())
        return on

    def landed(self, sha: str | None) -> bool | None:
        """Whether sha's work is on main: contained, or its patch there (landstate: patch-equivalent or a content twin).
        False for a commit the clone does not hold even after fetching main; None when it cannot tell."""
        on, repo = self.contains(sha), self.repo
        if on is not False or not repo or not sha:
            return on
        if not commit_known(repo, sha):
            return False  # main fetched (contains) and no such commit: on no main
        state = attempt(lambda: landed_state(repo, main_ref(), sha), None, catch=(Exception, SystemExit))
        return None if state is None or state.unknown else state.landed

    def carrying(self, sha: str | None) -> str | None:
        """The commit of main carrying sha: sha itself when main contains it, else the main commit with its patch (or
        content twin); None when main carries neither -- it never landed. sha as given when this cannot tell (no clone,
        no main ref, git unsure): unsure counts."""
        if not self.repo or not sha:
            return sha
        on = self.contains(sha)
        if on is None:
            return sha
        if on:
            return sha
        if not commit_known(self.repo, sha):
            return None
        key = (self.repo, sha, git(self.repo, "rev-parse", main_ref(), check=False).stdout.strip())
        if key not in _carried:
            _carried[key] = self._carrier(self.repo, sha)
        return _carried[key]

    def _carrier(self, repo: str, sha: str) -> str | None:
        state = landed_state(repo, main_ref(), sha, after=f"{sha}~1")
        if state.unknown:
            return sha  # git cannot tell (a root commit): unsure counts
        if not state.landed:
            return None
        return attempt(lambda: equivalent_on(repo, sha, main_ref()) or sha, sha, catch=(Exception, SystemExit))
