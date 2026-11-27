# agentctl, module `landstate`: IS THIS CHANGE ON MAIN: THE ONE ANSWER, with its three outcomes.
# A real module (lib/agentctl/__init__.py MODULES): its own imports, the standard library's and MODULES' only.

from dataclasses import dataclass
from enum import Enum
from typing import Optional

from agentctl.core import cherry_landed, git, commit_known, is_ancestor
from agentctl.patches import equivalent_on


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
