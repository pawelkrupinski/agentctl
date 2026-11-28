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
    zombies_seen: set[int] = field(default_factory=set)  # the zombie children the last reap saw (waited for at a second look)
    main_fetched: dict[Any, Any] = field(default_factory=dict)  # (clone, missing heads) -> when main was last fetched for them
    lander_landed_at: dict[Any, Any] = field(default_factory=dict)  # repo -> when its landed changes were last noted
    regen_answered: dict[Any, Any] = field(default_factory=dict)  # (change id, head, round) -> True: its `agentctl land` started
    missing_row_said: set[str] = field(default_factory=set)  # a red gate run whose failed job has no registry row: said once per run
    kept_known: dict[Any, Any] = field(default_factory=dict)  # (entry id, tip, checked base, main) -> verdict_kept's answer
    fix_replay_proofs: dict[Any, Any] = field(default_factory=dict)  # (base, head) -> the replays' proofs, run once per base and head
    flow_bounds: dict[Any, Any] = field(default_factory=dict)  # (main sha, workflow path) -> the workflow's timeout bound
    production: dict[str, bool] = field(default_factory=dict)  # sha -> whether it touches a production path (a commit never changes)
    ran_at: dict[str, float] = field(default_factory=dict)  # a throttled step ("upkeep", "recovery", ...) -> when it last ran
    route_seen: list[Any] = field(default_factory=lambda: ["unseen"])  # the default route at the network watch's last look
    batch_said: list[Any] = field(default_factory=lambda: [None])  # the batch tick's lines last said in the journal

