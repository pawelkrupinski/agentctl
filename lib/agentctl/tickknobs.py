# agentctl, part `tickknobs`: the tick and upkeep knobs: the tick and its steps, upkeep, maintenance, cleanup, polls
# A real module (lib/agentctl/__init__.py MODULES): its own imports, the standard library's and MODULES' only.

from agentctl.knobread import env_num
from agentctl.knobread import env_text


TICK = 5  # seconds between ticks ($AGENTCTL_TICK); every step is bounded or rate-limited inside


# THE KNOBS: one typed accessor each (its default and doc beside it; the config reference is generated from these
# reads, so a read here is a knob listed). The code calls the accessor, never the environment -- tests/test_knobs.py.
STEP_BOUND = 120  # s one tick step may run before it is abandoned (the red tick's steps and the sync steps)


def knob_tick() -> float:
    return env_num("AGENTCTL_TICK", TICK)


def knob_step_bound() -> float:
    return env_num("AGENTCTL_STEP_BOUND", STEP_BOUND)


# A CLEANUP IS BOUNDED PER TREE AND AS A WHOLE (2026-11-22: the upkeep's `cleanup --execute --no-remote` ran past its
# 1800 s bound at 03:58Z and was killed in the middle of a `git worktree remove`). Each tree's own git reads get
# CLEANUP_ITEM_TIMEOUT -- a tree that cannot be decided in that time is kept, said, and looked at next pass -- the
# fetch is the unattended bounded_fetch (180 s, not git()'s 600 s), and the whole pass stops by itself at its
# --deadline: no tree is looked at past it, and no removal starts with less than CLEANUP_REMOVE_RESERVE left.
CLEANUP_ITEM_TIMEOUT = 120  # $AGENTCTL_CLEANUP_ITEM_TIMEOUT


CLEANUP_REMOVE_RESERVE = 300  # $AGENTCTL_CLEANUP_REMOVE_RESERVE


FETCH_TIMEOUT = 180  # an unattended fetch's bound ($AGENTCTL_FETCH_TIMEOUT)


# CACHE EXPIRY (docs/simplification-2026-10.md, Agent B item 3): the state root collects a log per land (and its
# .checks.out / .push.out / .baseline.out), the phase-2 apply logs, sbt thread dumps, a dir per red run (its fixer
# records and claude logs), the baseline caches and the internal-tree tags. Each maintain pass removes what is older
# than CACHE_EXPIRE_DAYS: a file by its mtime, a red run's dir only when all of it is that old and none of its fixers is
# still running, a tag only when its worktree is gone. Nothing outside the state root, nothing a protected-folder
# check refuses (allowed_path: never ~/Desktop and the like), no symlink is followed.
CACHE_EXPIRE_DAYS = 7


MAINTAIN_SWEEP_HOUR = 9  # local time: the daily sweep runs at the first maintain pass from 09:00 ($AGENTCTL_SWEEP_HOUR)


# ---------------------------------------------------------------- owned: every long-running mode
# The owner (2026-10-26), after a day of strays: a zombie processor blocking the next one, duplicate waiters, a processor
# still running old code after a fix, orphaned shells, abandoned CI runs, landing slots held by dead loops. So every
# long-running mode -- `wait`, land's watch and its wait on `waiting`, the `waiting` processor -- runs OWNED:
#   * one flock per (kind, key) under ~/.cache/agentctl/owners/ ($AGENTCTL_OWNERS_DIR): the kernel frees it when the
#     process dies, however it dies, so a second instance can never run beside a live one and never waits on a dead one;
#   * beside it a record (<kind>-<key>.json): owner (the Claude session, else user@cwd), pid, the process's start time,
#     argv, cwd and the code version (agentctl's sha256); a SIGTERM/SIGHUP/SIGINT ends it with a verdict (Terminated).
#     (Owners once cancelled the CI runs they started; d073975, 2026-10-27, gave a remote check's run to its change
#     entry instead -- an owner's end cancelled a run its replacement still needed -- and the path is gone.)
#   * at each poll it compares agentctl's file with the version it started on and, when a fix has landed, re-execs
#     itself on the new code with the same pid;
#   * `agentctl orphans` reads the records: a dead process's record, an owner on old code, an owner whose cwd is gone.
OWNERS_RESTART_CHECK = 30  # seconds between code-version checks ($AGENTCTL_RESTART_CHECK)


# PARKED (the owner, 2026-11-03: "fix root cause and prevent structurally" -- movies-wt-bare-borrow (a design branch the
# owner is still deciding on), -flip-stage5 (an unproven design kept for reference), -order-rows (blocked on another
# change landing first) escalated UNLANDED ORPHAN over and over: an orphan was "author gone + commits not on main", with
# no way to say "this work is parked for a reason"). `agentctl park <worktree> --reason R` with one condition:
#   --until-landed <change id | commit>   re-surfaces once that change LANDED (or its re-submission did) or the commit
#                                        is on main by content;
#   --until <date>                        re-surfaces at that time;
#   --owner-decision                      only `agentctl unpark` (a person) ends it.
# A parked worktree gets no orphan action and no escalation (an open UNLANDED ORPHAN clears: "parked: R"); `agentctl
# status` shows `parked: N` with the reasons. Each maintain pass reads every condition again (self-recovering): met, the
# mark is RELEASED and the orphan rules apply afresh (its orphan records dropped, so it gets its one submit -- it lands).
# A parked worktree older than PARK_DIGEST_DAYS is named in ONE weekly digest line (an inbox entry that wakes nobody).
PARK_DIGEST_DAYS = 14


SYNC_MIRROR_EVERY = 10   # s between the laptop's copies of /v1/state into state.json


# EVERY STEP OF THE TICK IS BOUNDED (2026-10-29, RECONCILER STALLED 05:37-05:41: during a tunnel hang the gate's
# forge reads -- gh calls of up to 90 s each, one kickstart and a retry, ten workflows -- held the loop for minutes,
# and state.json's tick_at aged past 2 min). A step runs in a thread of its own and the tick waits at most STEP_JOIN s
# for it; one still running is not started again until it ends (run_bounded), and the tick goes on to the next step.
# Its error, when it ends, is reported at the next look. The steps' writes are store transitions (compare-and-set) and
# atomic entry files, so a slow step overlapping the next tick's other steps is safe.
STEP_JOIN = 20


# ---------------------------------------------------------------- upkeep: the tick keeps the laptop's disk (2026-10-29)
# The owner's coordinator, 2026-10-29: the Data volume filled up (739 MB free) -- 109 proven-merged idle worktrees
# (mostly finished fixers' movies-wt-red-*) piled up because nothing ran `cleanup` on its own, and the test suite had
# leaked 3,009 `agentctl-*` temp dirs. Now, at most every UPKEEP_EVERY s (and at once when the disk runs low), the
# laptop's processor runs:
#   - `cleanup --execute --no-remote`, the STANDING-OK scope only (clean, idle, proven-merged worktrees and their
#     branches; every deletion re-verified and logged in agentctl-deleted.log);
#   - each ended fixer's worktree whose red is closed (no open red item with its key) closed by close_worktree (only
#     when proven merged, clean and unused);
#   - the test suite's own leftovers: `agentctl-*` dirs in the user's temp dir and `agentctl-sock-*` in /tmp older than
#     TEST_TMP_MAX_AGE (a run removes its own at exit; these are killed runs').
UPKEEP_EVERY = 3600


# LOGS ROTATE BY SIZE (2026-10-30: the laptop processor's service.log had grown to 141 MB, never rotated). Every
# agentctl log -- each *.log under the state root and the reds dir (a check's own run output, in *.d / work / target,
# is its check's and expires with it), and launchd's ~/Library/Logs/agentctl-*.log -- over LOG_ROTATE_MAX bytes is
# rotated by COPY-TRUNCATE: its newest LOG_ROTATE_MAX bytes (from a line start) become <log>.1, the older parts shift
# to .2 .. .LOG_ROTATE_KEEP (the oldest goes), and the file itself is truncated IN PLACE. In place because its writers
# (launchd's StandardOutPath, the processor's appends) hold it open with O_APPEND: they go on writing at its new end,
# where a rename would leave them writing into .1. The status page's /log serves .1's tail before a short current
# file's, and a live pane reads a truncated file again from 0. Run by `maintain` (every 15 min) and the upkeep.
LOG_ROTATE_MAX = 10 << 20  # bytes over which an agentctl log is rotated (copy-truncate; its newest part kept as .1)


LOG_ROTATE_KEEP = 3  # rotated parts kept per log (<log>.1 newest .. <log>.N oldest)


ASSIGN_BOUND = 20  # s the red tick on the gate snapshot may run before it is abandoned ($AGENTCTL_ASSIGN_BOUND)


ATTACH_POLL = 15  # s between a wait's looks at an attached session ($AGENTCTL_ATTACH_POLL)


MIN_INTERVAL = 60  # s the least poll interval of `agentctl wait` ($AGENTCTL_MIN_INTERVAL)


GATE_PUBLISH = 45  # s between the gate line's publishes (a push to main publishes at once) ($AGENTCTL_GATE_PUBLISH)


DISPATCH_FIND = 12  # polls for a just-dispatched run to be listed ($AGENTCTL_DISPATCH_FIND)


DISPATCH_FIND_POLL = 5  # s between those polls ($AGENTCTL_DISPATCH_FIND_POLL)


def knob_assign_bound() -> float:
    return env_num("AGENTCTL_ASSIGN_BOUND", ASSIGN_BOUND)


def knob_attach_poll() -> float:
    return env_num("AGENTCTL_ATTACH_POLL", ATTACH_POLL)


def knob_cache_expire_days() -> float:
    return env_num("AGENTCTL_CACHE_EXPIRE_DAYS", CACHE_EXPIRE_DAYS)


def knob_cleanup_item_timeout() -> float:
    return env_num("AGENTCTL_CLEANUP_ITEM_TIMEOUT", CLEANUP_ITEM_TIMEOUT)


def knob_cleanup_remove_reserve() -> float:
    return env_num("AGENTCTL_CLEANUP_REMOVE_RESERVE", CLEANUP_REMOVE_RESERVE)


def knob_dispatch_find() -> float:
    return env_num("AGENTCTL_DISPATCH_FIND", DISPATCH_FIND)


def knob_dispatch_find_poll() -> float:
    return env_num("AGENTCTL_DISPATCH_FIND_POLL", DISPATCH_FIND_POLL)


def knob_fetch_timeout() -> float:
    return env_num("AGENTCTL_FETCH_TIMEOUT", FETCH_TIMEOUT)


def knob_gate_publish() -> float:
    return env_num("AGENTCTL_GATE_PUBLISH", GATE_PUBLISH)


def knob_log_rotate_keep() -> float:
    return env_num("AGENTCTL_LOG_ROTATE_KEEP", LOG_ROTATE_KEEP)


def knob_log_rotate_max() -> float:
    return env_num("AGENTCTL_LOG_ROTATE_MAX", LOG_ROTATE_MAX)


def knob_min_interval() -> float:
    return env_num("AGENTCTL_MIN_INTERVAL", MIN_INTERVAL)


def knob_park_digest_days() -> float:
    return env_num("AGENTCTL_PARK_DIGEST_DAYS", PARK_DIGEST_DAYS)


def knob_restart_check() -> float:
    return env_num("AGENTCTL_RESTART_CHECK", OWNERS_RESTART_CHECK)


def knob_step_join() -> float:
    return env_num("AGENTCTL_STEP_JOIN", STEP_JOIN)


def knob_sweep_hour() -> float:
    return env_num("AGENTCTL_SWEEP_HOUR", MAINTAIN_SWEEP_HOUR)


def knob_sync_mirror() -> float:
    return env_num("AGENTCTL_SYNC_MIRROR", SYNC_MIRROR_EVERY)


def knob_upkeep_every() -> float:
    return env_num("AGENTCTL_UPKEEP_EVERY", UPKEEP_EVERY)


def knob_launch_agents() -> str:
    return env_text("AGENTCTL_LAUNCH_AGENTS")  # the launchd agents' directory (default ~/Library/LaunchAgents)
