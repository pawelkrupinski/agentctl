# agentctl, part `maincontain`: IS THIS COMMIT ON MAIN -- one answer, by a clone (git) or by the forge (its API).
# A real module (lib/agentctl/__init__.py MODULES): its own imports, the standard library's and MODULES' only.

from agentctl.clock import epoch_now
from agentctl.core import attempt, git, git_ok, process_cache, remote, commit_known, is_ancestor
from agentctl.clones import fetch_main
from agentctl.forge import gh_json
from agentctl.landstate import landed_state
from agentctl.patches import equivalent_on
from agentctl.redfixread import RED_MAIN_REFETCH


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
