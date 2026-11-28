# agentctl, part `caches`: THE PROCESS'S MEMOS, OWNED: each cache a call tree keeps between calls is a field of ONE value
# cli (the composition root) makes and hands down on the Context, never a module-level mutable (core.process_cache's
# registry shrinks as each moves here). A test builds its own, so no test sees another's memo.
# A real module (lib/agentctl/__init__.py MODULES): its own imports, the standard library's and MODULES' only.

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class Caches:
    """One dict per concept, empty at birth; what a field maps is said where it is read."""
    identity_known: dict[Any, Any] = field(default_factory=dict)  # (entry id, head) -> identity_change's verdict
    trailer_fixes: dict[Any, Any] = field(default_factory=dict)  # commit -> the runs its Fixes-Red trailers name that count
    link_reads: dict[Any, Any] = field(default_factory=dict)  # (red key, sha) -> red_link_reads' answer
    never_green_at: dict[Any, Any] = field(default_factory=dict)  # (repo, workflow[, job]) -> when GitHub last said never green
    others_said: dict[Any, Any] = field(default_factory=dict)  # repo -> (why, when said): a skipped repo's line
