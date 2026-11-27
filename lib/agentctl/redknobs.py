# agentctl, part `redknobs`: the red knobs: the red tick, its fixers, asks, blame and decisions; the reds', dispatches' and recovery directories
# A real module (lib/agentctl/__init__.py MODULES): its own imports, the standard library's and MODULES' only.

from agentctl.knobread import env_num, env_text


RED_STALL = 30 * 60   # a turn with no progress (transcript, worktree) this long is stalled: stopped ($AGENTCTL_RED_STALL)


RED_FIXER_WALL = 3 * 3600    # a dispatched fixer's maximum wall time ($AGENTCTL_RED_FIXER_WALL)


RED_RENEW_EVERY = 60         # a fixer's wrapper renews its lease this often


# THE CRASH-LOOP BREAKER (G9, convergence-failures-2026-10-29 §4: in E9 fixers were launched 52 times over 9 reds, #2335
# 18 and #2375 16 times, mostly `exit 1 with no LANDED / GAVE UP line`): RED_CRASH_LOOP dispatched fixers in a row that
# ended with NO RESULT (a crash, an exit with no verdict line, a lease lost or expired without progress, a time-out, a
# start that never got going) and the same tier is not dispatched blindly again. ONE escalation with the evidence (each
# launch's outcome and log tail), and the red moves UP: to the deep tier (crash_loop.stage `deep`) -- or, when the deep
# tier itself crash-looped, onto an OWNER BLOCK (block/<key>, cause `fixer-crash-loop`): the fixers, not the code, are
# what fails, so this is the one owner block a test red may have. It recovers by itself: its probe passes when a newer
# decided run of the red's workflow no longer fails this way (or the run is green), and it lapses after
# RED_CRASH_BLOCK_MAX (offered again, the streak counted afresh from crash_loop.mark). A launch with a result (LANDED,
# NOT LANDED, GAVE UP, BLOCKED ON OWNER, DIAGNOSED, a refuted fix) ends the streak; a release by hand is no launch.
RED_CRASH_LOOP = 3                 # $AGENTCTL_RED_CRASH_LOOP
CRASH_LOOP_LAUNCHES = 3            # $AGENTCTL_CRASH_LOOP_LAUNCHES: launches in a row dying quickly of one cause open CRASH_LOOP (crashloop.py)
CRASH_LOOP_QUICK = 120             # $AGENTCTL_CRASH_LOOP_QUICK: s a launch ending this soon after its start never got going


RED_CRASH_BLOCK_MAX = 24 * 3600    # $AGENTCTL_RED_CRASH_BLOCK_MAX


# A DIAGNOSIS THAT NEEDS THE OWNER (2026-11-01, convergence #579: "resolver design ... The gate needs your design
# call"): DIAGNOSED was no end -- the red went back to its fixer (the family's owner), who diagnosed it again, turn after
# turn. A DIAGNOSED verdict that names the owner's decision (`DIAGNOSED: <cause>; decide: <the decision>`), or a second
# DIAGNOSED in a row (the implementer briefed with the first could not make the fix either: crashes and lapses between
# them are skipped), puts the red on an OWNER-DECISION BLOCK (block/<key>, cause `owner-decision`) whose text carries
# the diagnosis and the decision -- never a crash, never the crash-loop breaker's. Like the crash-loop block it is one
# a test red may hold, and it clears itself: a newer run of the red's workflow no longer failing this way, or
# RED_DECISION_BLOCK_MAX (offered again); the owner lifts it at once with `agentctl red release <key> --any`.
RED_DECISION_BLOCK_MAX = 7 * 86400   # an owner-decision block lapses after this: the red offered again (2026-11-03: was


RED_OLD_RUN = 2 * 86400  # an outside red whose newest run is older than this is checked by a fresh run before it is fixed


# ---------------------------------------------------------------- PROOF OF WORK: orphaned leases (2026-10-31)
# A lease is held by WORK, never by a living process. 2026-10-31 (issue 20261031T203744-4a4a59): the main-tests owner's
# session stopped at 20:12 (its turn ended `blocked`), while its wrapper (pid 51249) waited for `done` and kept renewing
# the owner's heartbeat; every red of the family was handed to it, held "leased ... none at work" and never fixed. The
# wrappers now end a turn whose session ended or stalled (red_owner, red_fixer). And whatever a wrapper does -- older
# code, a hang -- the laptop's tick checks every laptop-held fixer and owner lease against the evidence of work: the
# wrapper's process, its session's row in `claude agents`, the transcript's and the worktree's progress. A holder not at
# work for RED_ORPHAN_GRACE loses its leases at once (revoked `ORPHANED: <why>`): the red is offered again, and a
# wrapper still running reads its next renewal as lost and exits. `red status` shows such a lease as orphaned, never
# "leased ... none at work".
RED_ORPHAN_GRACE = 5 * 60    # an ended session's turn still held this long is orphaned ($AGENTCTL_RED_ORPHAN_GRACE)


def knob_red_crash_loop() -> float:
    return env_num("AGENTCTL_RED_CRASH_LOOP", RED_CRASH_LOOP)


def knob_crash_loop_launches() -> int:
    return int(env_num("AGENTCTL_CRASH_LOOP_LAUNCHES", CRASH_LOOP_LAUNCHES))


def knob_crash_loop_quick() -> float:
    return env_num("AGENTCTL_CRASH_LOOP_QUICK", CRASH_LOOP_QUICK)


def knob_red_orphan_grace() -> float:
    return env_num("AGENTCTL_RED_ORPHAN_GRACE", RED_ORPHAN_GRACE)


def knob_red_stall() -> float:
    return env_num("AGENTCTL_RED_STALL", RED_STALL)


def knob_red_renew() -> float:
    return env_num("AGENTCTL_RED_RENEW", RED_RENEW_EVERY)


def knob_red_fixer_wall() -> float:
    return env_num("AGENTCTL_RED_FIXER_WALL", RED_FIXER_WALL)


def knob_red_crash_block_max() -> float:
    return env_num("AGENTCTL_RED_CRASH_BLOCK_MAX", RED_CRASH_BLOCK_MAX)


def knob_red_decision_block_max() -> float:
    return env_num("AGENTCTL_RED_DECISION_BLOCK_MAX", RED_DECISION_BLOCK_MAX)


def knob_red_old_run() -> float:
    return env_num("AGENTCTL_RED_OLD_RUN", RED_OLD_RUN)


STREAK_REREADS = 3  # stale or missing streak run signatures read again per tick ($AGENTCTL_STREAK_REREADS)


def knob_streak_rereads() -> int:
    """The tick's re-reads of stale streak run signatures (redepisode.streak_budget)."""
    return int(env_num("AGENTCTL_STREAK_REREADS", STREAK_REREADS))


def knob_reds_dir() -> str:
    return env_text("AGENTCTL_REDS_DIR")  # the reds' directory (default reds under the state root)


def knob_dispatch_dir() -> str:
    return env_text("AGENTCTL_DISPATCH_DIR")  # the dispatches' directory (default beside the reds')


def knob_red_fixer() -> str:
    return env_text("AGENTCTL_RED_FIXER")  # a red fixer's own family (set in a fixer's environment)


RED_FIX_DECIDE = 3 * 3600


# THE STATE MACHINE IS TOTAL (the owner, 2026-10-29): no red state waits on an event that may never come. A fix-pending
# whose fix is not proven by RED_FIX_GIVEUP after it entered fix-pending (a landed fix no run checks, a verify run an
# on-demand workflow never gets, a fix id that is None) is offered again with that fix set aside (`unverified`): a
# fixer decides, never a resting state.
RED_FIX_GIVEUP = 2 * RED_FIX_DECIDE


# A NIGHTLY'S FIX WAITS FOR THE NIGHT (2026-11-13, Verify scrape recording #26): a landed fix of a red OUTSIDE the gate
# is proven only by its workflow's next run -- a nightly's is up to a day away, and no push brings it sooner (its verify
# dispatch may be refused). Such a fix holds its red, quietly, up to RED_FIX_NEXT_RUN: the next decided run containing
# it verifies (green) or refutes it (the same failure: back to a fixer, never to the owner).
RED_FIX_NEXT_RUN = 30 * 3600  # s an outside red's landed fix waits unproven for its workflow's next run


# A SCHEDULED WORKFLOW'S FIX WAITS FOR ITS SLOT (2026-11-19, Record scrape fixtures #198, 50bc15cd7369): its fix
# 123dd6524 landed at 04:23Z; the workflow runs nightly at 23:31Z (its `on.schedule` cron, dispatched by the movies
# scheduler, infra/nix/files/scheduled_dispatch.py, which reads the same crons), so no run could contain the fix before
# ~23:31Z -- yet a fixed RED_FIX_DECIDE timer showed it "fix-pending escalated" at 07:23Z, and the landing itself had
# raised its level. Now a landed fix of a red outside the gate whose workflow has a schedule is VERIFIED BY the first
# slot after the landing plus the workflow's p90 duration (its recent finished runs) plus RED_VERIFY_SLACK, and until
# then it rests quietly: "awaiting next scheduled run at HH:MMZ", no level, no inbox line. At that deadline a run
# containing the fix still queued or running holds it (re-deadlined, quiet); none is an escalation: the slot passed with
# no run verifying it, or the scheduler never dispatched it.
# A VERIFY DISPATCH IS PER WORKFLOW (the owner, 2026-11-19): Record scrape fixtures scrapes every cinema through paid
# proxies -- its 04:34Z verify dispatch (#200) was not cheap. A workflow WITH a schedule is dispatched to verify a fix
# unless the repo's ONE per-workflow policy registry, RED_WORKFLOWS_FILE on main, says `verify_dispatch` no (VERIFY
# PROMPTLY BY DEFAULT, 2026-11-21: no row is yes, and the dispatch goes at once); a `no` workflow waits for its slot. A
# workflow with no schedule is dispatched as before (no other run will come). The same row's `replay` is the command a fixer runs before landing (FIX REPLAY, below).
RED_VERIFY_SLACK = 2 * 3600     # s past a slot's expected end before its fix is called unverified ($AGENTCTL_RED_VERIFY_SLACK)


RED_TICK_INTERVAL = 15       # seconds between the processor's red ticks ($AGENTCTL_RED_TICK)


# A DECIDED REVERT THE FIXERS DID NOT LAND IS LANDED BY AGENTCTL (the owner, 2026-11-22: "fix this structurally, so
# it recovers by itself"; Identity takes #349: the owner decided "revert f64d8a73a" at ~07:00Z, the fixer's revert was
# rejected at 07:52Z and the gate stayed red, every land waiting behind it). A gate red whose in-progress decision is
# `revert <sha>`, offered again RED_DECIDED_REVERT_MAX after the owner decided it, goes to the revert tier: agentctl's
# own revert of that commit (red_revert: Fixes-Red, a specific `Fix-Replay: none`, landed by `agentctl land` under the
# normal checks), once -- `reverted` set, the ladder goes on as before if it does not land.
RED_DECIDED_REVERT_MAX = 45 * 60


# A JOINED RED SHOWS PROGRESS OR GOES ON (the owner, 2026-11-08). A red joined to its family holder's lease (the
# dispatcher's join: `joined_at` on the lease) waits on a session that may be deep in its own red. With no progress on
# THAT red within RED_JOIN_IDLE of joining -- a commit carrying its `Fixes-Red:` in the holder's worktree, a fix on its
# way (the red's `fix`, or the red in fix-pending), a note on it (`red claim <it> --note` inside the fixer) -- it is
# RELEASED (no attempt counted) and offered again, never joined to that holder again (`family_released`).
RED_JOIN_IDLE = 30 * 60  # s a red joined to its family holder's lease may go with no progress on it before it is released


# A RED IN AN OWNER'S HAND SHOWS ITS OWN PROGRESS (the owner, 2026-11-22: Alert backtest #21, 5254bffda9d2.4, held by
# the outside-gate owner from 16:05Z with no diagnosis or commit for ~3 h). The owner wrapper renews every red lease it
# holds with each beat of a turn not stalled: the TURN's progress (its transcript, its worktree), the family's -- turn 85
# ran 16:38-18:54Z on its other red, and "lapses ... without progress" moved on with each beat. A red held under an
# owner's lease with no progress on IT -- a DIAGNOSED or LANDED verdict in its history, a commit in the owner's worktree
# whose `Fixes-Red:` or `Diagnoses-Red:` names it, a note on it (`red claim <it> --note`) -- for RED_HELD_IDLE since the
# later of its lease's start and that progress is released STALLED (an attempt), the reason in the inbox, and goes on
# up the ladder like any ended lease.
RED_HELD_IDLE = 45 * 60  # s a red in its owner's hand may go with no progress of its own before it is released


# ONE RED MODEL (docs/simplification-2026-10.md item 5; replaces the gate/other/overflow capacity classes, the borrow
# and the separate one-run and one-family rules). A red is (workflow, failing job, signature) -- `key` hashes the
# workflow and the signature (the normalized first error line; the job when none was read), `jobs` are every job
# failing that way. Every red belongs to ONE QUEUE FAMILY:
# its configured family (red-families.json: convergence, ...), else its run (`run:<workflow>#<number>`). The open
# fixer offers are ONE PRIORITY QUEUE (gate < fix < outside < flake, then a test failure first, then the oldest), and
# the dispatcher keeps EXACTLY ONE FIXER PER FAMILY: a family whose fixer is at work gets each new red under that
# fixer's lease (a sibling, told in RED-INBOX.md); a family with none gets one fixer, its other open offers taken with
# it, while fewer than RED_FIXERS fixers are alive in all. One family never holds two slots, so no class reserves are
# needed for fairness; an offer that waits RED_STARVE with no slot escalates as STARVED (then every cycle). Every wait
# names what it waits for and when.
RED_FIXERS = 5           # live fixers at once, every family together ($AGENTCTL_RED_FIXERS)


RED_STARVE = 45 * 60     # an offer waiting this long for a slot is STARVED ($AGENTCTL_RED_STARVE)


# THE OWNER (2026-11-22): "teach agentctl to react to parts of workflow failing and notifying / spinning the agents right
# away". Main takes ~20 min and the identity runs longer; a job can fail in minute 3 while the rest runs on. Each red
# tick (every gate publish, ~45 s) reads the jobs of the in-progress runs on main of the gate and the outside
# workflows (one GET of actions/runs/<id>/jobs?filter=latest per run, conditional: an unchanged list is a free 304).
# A job concluded `failure` / `timed_out` is read the way ci-why reads a completed run's (explain_run_jobs: the one
# classifier), and only when the set of failed jobs changed. A TEST failure (test or compile, in the commit's code)
# is an EARLY RED: an ordinary red item with `early` in its data, opened, offered and dispatched now. An infra-shaped
# or unclear one opens nothing: the completed run decides it, as before.
# What an early red never does: blame or revert (red_tier, red_open: only a completed run's verdict decides a
# culprit), or hold a push (the gate reads completed runs). Its end: the run completes failing (SETTLED in place, one
# red: the completed observation of the same key attaches to it and its culprit search opens then; a key the
# completed read words otherwise stays beside it, joined to the same fixer by its queue family), or does not fail
# (cancelled, superseded, a rerun clearing it: WITHDRAWN, its fixer told to stand down, never a revert).
EARLY_MAX_RUNS = 6            # in-progress runs read per tick, the gate's first ($AGENTCTL_EARLY_MAX_RUNS)


RED_FIX_REQUEUE_GRACE = 900  # $AGENTCTL_RED_FIX_REQUEUE_GRACE


RED_LIVENESS_MAX = 60 * 60  # the gate red this long with a red fix stalled: the status line says so ($AGENTCTL_RED_LIVENESS_MAX)


FIX_REPLAY_TIMEOUT = 30 * 60  # s one side of agentctl's replay may run ($AGENTCTL_FIX_REPLAY_TIMEOUT)


# -- RETIRED CHECKS (agentctl issue 20261031T210634-c53375): core 2b4d9c905 removed every dev/sandbox target, and the
# reds of their last runs (Playwright Tests (deployed) #1080/#1081, Run Post Deployment Tests #1362) could never clear:
# no run of a retired check comes again. A red whose check no longer exists on main is CLEARED by the same probe that
# would re-raise it -- main's workflow files, read through the API at main's head: its workflow gone (by name and by
# the run's file), its job gone (no current job produces its name, and no job of the same key holds its matrix row), or
# the input branch its job took gone (a step it ran under an `if:` on inputs/matrix, absent from the job now); or
# STALE: its workflow made no run on main for RED_STALE_DAYS while main moved (workflow_call-only workflows excepted:
# red_callable_evidence settles those). Conservative: a job renamed in place (same key, no matrix row) still matches,
# and anything unreadable or unparseable keeps the red (fail safe).
RED_STALE_DAYS = 14          # $AGENTCTL_RED_STALE_DAYS


UNKICKED_AFTER = 60 * 60  # a convergence-relevant main push with no convergence (or Identity takes) run on main by then is a red


RED_OTHERS_EVERY = 60        # seconds between the other watched repos' red ticks: one gate read each ($AGENTCTL_RED_OTHERS_EVERY)


RED_SKIP_SAY = 600  # s between the lines of another repo whose tick keeps failing for the same reason


EVER_GREEN_RECHECK = 10 * 60          # s before a never-green answer is asked of GitHub again


# MAIN MOVED UNDER AN OUTSIDE RED: VERIFY IT (2026-11-22, Order independence #105: 9a7130036 fixed it -- a tools-only
# commit, rightly with no `Fixes-Red:` -- while the red sat fix-pending on an unrelated change that edited its workflow
# file, and its fixer had been stopped hours before; nothing dispatched a run that would have seen main fixed). An
# outside-gate red with no LANDED fix of its own, whose job reads something main changed since the red's run
# (fix_reaches_red over each commit since its head), has its workflow dispatched on main to verify -- once per main
# head, at most every RED_MAIN_VERIFY_EVERY, and only where a verify dispatch is allowed (red_verify_dispatch:
# red-workflows.tsv). A green run clears it as any decided run does; a red one says main has not fixed it.
RED_MAIN_VERIFY_EVERY = 3 * 3600


def knob_early_max_runs() -> float:
    return env_num("AGENTCTL_EARLY_MAX_RUNS", EARLY_MAX_RUNS)


def knob_ever_green_recheck() -> float:
    return env_num("AGENTCTL_EVER_GREEN_RECHECK", EVER_GREEN_RECHECK)


def knob_fix_replay_timeout() -> float:
    return env_num("AGENTCTL_FIX_REPLAY_TIMEOUT", FIX_REPLAY_TIMEOUT)


def knob_red_decided_revert_max() -> float:
    return env_num("AGENTCTL_RED_DECIDED_REVERT_MAX", RED_DECIDED_REVERT_MAX)


def knob_red_fixers() -> float:
    return env_num("AGENTCTL_RED_FIXERS", RED_FIXERS)


def knob_red_fix_decide() -> float:
    return env_num("AGENTCTL_RED_FIX_DECIDE", RED_FIX_DECIDE)


def knob_red_fix_giveup() -> float:
    return env_num("AGENTCTL_RED_FIX_GIVEUP", RED_FIX_GIVEUP)


def knob_red_fix_next_run() -> float:
    return env_num("AGENTCTL_RED_FIX_NEXT_RUN", RED_FIX_NEXT_RUN)


def knob_red_fix_requeue_grace() -> float:
    return env_num("AGENTCTL_RED_FIX_REQUEUE_GRACE", RED_FIX_REQUEUE_GRACE)


def knob_red_held_idle() -> float:
    return env_num("AGENTCTL_RED_HELD_IDLE", RED_HELD_IDLE)


def knob_red_join_idle() -> float:
    return env_num("AGENTCTL_RED_JOIN_IDLE", RED_JOIN_IDLE)


def knob_red_liveness_max() -> float:
    return env_num("AGENTCTL_RED_LIVENESS_MAX", RED_LIVENESS_MAX)


def knob_red_main_verify_every() -> float:
    return env_num("AGENTCTL_RED_MAIN_VERIFY_EVERY", RED_MAIN_VERIFY_EVERY)


def knob_red_others_every() -> float:
    return env_num("AGENTCTL_RED_OTHERS_EVERY", RED_OTHERS_EVERY)


def knob_red_skip_say() -> float:
    return env_num("AGENTCTL_RED_SKIP_SAY", RED_SKIP_SAY)


def knob_red_stale_days() -> float:
    return env_num("AGENTCTL_RED_STALE_DAYS", RED_STALE_DAYS)


def knob_red_starve() -> float:
    return env_num("AGENTCTL_RED_STARVE", RED_STARVE)


def knob_red_tick() -> float:
    return env_num("AGENTCTL_RED_TICK", RED_TICK_INTERVAL)


def knob_red_verify_slack() -> float:
    return env_num("AGENTCTL_RED_VERIFY_SLACK", RED_VERIFY_SLACK)


def knob_unkicked_after() -> float:
    return env_num("AGENTCTL_UNKICKED_AFTER", UNKICKED_AFTER)


def knob_brief_src() -> str:
    return env_text("AGENTCTL_BRIEF_SRC")  # a file to use instead of the tracked docs/fixer-brief.md in fixers' prompts (an override): none by default


def knob_pinned_recording() -> str:
    return env_text("AGENTCTL_PINNED_RECORDING")  # the pinned recording's id the identity checks use


def knob_red_families() -> str:
    return env_text("AGENTCTL_RED_FAMILIES")  # the red families' file (default red-families.json beside the tool)


def knob_drift_ci() -> str:
    return env_text("AGENTCTL_DRIFT_CI")  # the drift CI script (default scripts/identity-drift-ci of the repository)


def knob_brief() -> str:
    return env_text("AGENTCTL_BRIEF")  # the agents' brief file (default ~/bin/briefs/movies-agent-preamble.md)


def knob_red_repos() -> str:
    return env_text("AGENTCTL_RED_REPOS")  # the red repositories' file (default red-repos.json beside the tool)


def knob_red_repo() -> str:
    return env_text("AGENTCTL_RED_REPO")  # the one repository reds are read for (default: the movies repository)
