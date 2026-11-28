# agentctl, part `caches`: THE PROCESS'S MEMOS, OWNED: each cache a call tree keeps between calls is a field of ONE value
# cli (the composition root) makes and hands down on the Context, never a module-level mutable (core.process_cache's
# registry shrinks as each moves here). A test builds its own, so no test sees another's memo.
# A real module (lib/agentctl/__init__.py MODULES): its own imports, the standard library's and MODULES' only.

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class StatusMemos:
    """What the status reads keep between refreshes (the status page's server, `red status`): one dict per concept."""
    assembled: dict[str, Any] = field(default_factory=dict)  # "at", "data": the model status_cached assembled last
    last: dict[str, Any] = field(default_factory=dict)  # source name -> (finished at, result, took s) of its last read
    reading: dict[str, Any] = field(default_factory=dict)  # source name -> the thread reading it
    worktrees: dict[str, Any] = field(default_factory=dict)  # worktree -> {"head", "commits", ..., "at"}: its last git read
    worktree_reads: dict[str, Any] = field(default_factory=dict)  # worktree -> the thread reading it
    said: dict[str, Any] = field(default_factory=dict)  # transcript -> ((size, mtime), (text, at)): what it said last
    leased: dict[str, Any] = field(default_factory=dict)  # agent id -> entry, of the lease holders the last Agents read saw
    registry: dict[str, Any] = field(default_factory=dict)  # "at", "reg": the live registry read last
    tails: dict[str, Any] = field(default_factory=dict)  # transcript -> ((size, mtime), its tail's info)
    metas: dict[str, Any] = field(default_factory=dict)  # a subagent's .meta.json -> (mtime, its JSON)
    blocker: dict[Any, Any] = field(default_factory=dict)  # (dispatch flag, claude knobs) -> (when, what stops a fixer dispatch): a minute

    def clear(self) -> None:
        """Every memo emptied (a simulated seed's fresh process)."""
        for memo in vars(self).values():
            memo.clear()


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
    route_seen: dict[str, Any] = field(default_factory=dict)  # "route": the default route at the network watch's last look
    batch_said: dict[str, Any] = field(default_factory=dict)  # "lines": the batch tick's lines last said in the journal
    tick_facts: dict[str, Any] = field(default_factory=dict)  # "memo": the queue facts of the tick under way (None: none is)
    bounded: dict[str, Any] = field(default_factory=dict)  # a bounded step's name -> the thread it runs in (core.run_bounded)
    step_errors: dict[str, Any] = field(default_factory=dict)  # a tick step -> its last error (None: it ended well)
    status_page: dict[str, Any] = field(default_factory=dict)  # the status page this process serves: its url and server
    status: StatusMemos = field(default_factory=StatusMemos)  # what the status reads keep between refreshes
    overlap_known: dict[Any, Any] = field(default_factory=dict)  # two heads (or main and a head) -> their overlap, asked once
    behind_said: dict[Any, Any] = field(default_factory=dict)  # (entry id, what) -> the batch or change it waits behind, said once
    retired: dict[Any, Any] = field(default_factory=dict)  # a retired-check read (main, a file, the flows, a run) -> (when, answer)
    policy_said: dict[Any, Any] = field(default_factory=dict)  # (policy id, subject) -> True: its unapproved warning recorded
    landed_looked: dict[str, float] = field(default_factory=dict)  # change id -> when it was last looked for on main
    code_restart: dict[str, Any] = field(default_factory=dict)  # "checked_at", "refused", "smoked": the re-exec check's memo


