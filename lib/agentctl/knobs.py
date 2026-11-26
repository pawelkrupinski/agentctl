# agentctl, part `knobs`: the knobs: one typed accessor each, its default and doc beside it
# A real module (lib/agentctl/__init__.py MODULES): its own imports, the standard library's and MODULES' only.

import os
from typing import Any


MAX_FOREIGN = 2  # files a push may revert to an older main before prepush-check refuses ($AGENTCTL_MAX_FOREIGN)


# A MISSING TOKEN IS SAID ON ITS TRANSITION (2026-10-30: the laptop processor's service.log held ~842,000 lines of
# "could not read the movies token" -- every step of every tick, and the inbox note's own failure, said it
# again). A long-lived process says it once when the token goes missing (and notes the condition then), then at most a
# summary every TOKEN_MISSING_SUMMARY s, and once more when it is back. Its callers' NoToken is quiet (run_step).
TOKEN_MISSING_SUMMARY = 600  # s between the summaries of a still-missing token (said on its transition first)


# A FIXER IS A BACKGROUND CLAUDE SESSION (the owner, 2026-10-29: "I want fixers to be visible in ui"): `claude --bg -n
# <label>` starts it, so `claude agents` lists it by its label and the owner can `claude attach | logs | stop` it. The
# wrapper tracks it by the id --bg prints, never a child pid: `claude agents --json --all` says working / done, its
# transcript (~/.claude/projects/*/<session>.jsonl) is its progress and holds its final answer, `claude stop` ends it.
BG_POLL = 5  # seconds between the wrapper's looks at its session ($AGENTCTL_BG_POLL)


RED_STALL = 30 * 60   # a turn with no progress (transcript, worktree) this long is stalled: stopped ($AGENTCTL_RED_STALL)


# STUCK SBT SERVERS (2026-10-27): a worktree's one server ran an orphaned `--serve` (its owner's shell died) for hours,
# and every later command queued behind it at 0% CPU. Besides a gone worktree, orphans/sweep reap a server whose
# serving command's sbt-warm or owner is dead (scripts/sbt-warm's $State/<key>.serving), and one with a command
# running (an sbtn client in its checkout) whose process tree used no CPU for SBT_STALL_MIN minutes, judged across
# runs from ~/.cache/agentctl/sbt-cpu.json. A thread dump (jcmd Thread.print) is saved first, under
# ~/.cache/agentctl/sbt-dumps/.
SBT_STALL_MIN = 20


# PROGRESS IS A RATE (2026-11-07, measured): idle server JVMs burn 0.04-0.12 CPU s/min in background threads, busy ones
# 5-155 s/min. A fixed tolerance on the CPU total since idling began was crossed by that background burn within one
# 15-min pass, so neither the stall nor the idle rule could ever fire; CPU since the previous pass is judged per minute.
SBT_PROGRESS_CPU_PER_MIN = 1.0  # CPU s/min since the previous pass above which an sbt server counts as working


# IDLE DETACHED SERVERS (2026-11-07): two lone servers (core-wt-red-822/824, -Xmx6000M each) sat idle for 3 h while
# swap stood at 40.1/40.96 GB and every other build crawled; no rule matched them (one server per checkout, under the
# hog CPU floor, no command running). A detached server (PPID 1) with no client and no CPU for SBT_IDLE_MIN is reaped,
# after SBT_IDLE_PRESSURE_MIN once swap free falls under SWAP_FREE_FLOOR_MB -- never one sbt-warm --keep keeps.
# Ending an idle server costs only the next command's warm-up.
SBT_IDLE_MIN = 120  # min a detached sbt server with no client may sit idle before the reaper ends it


SBT_IDLE_PRESSURE_MIN = 30  # that limit while swap free is under the floor


SWAP_FREE_FLOOR_MB = 2048  # swap free (MB) below which the shorter idle limit applies


TICK = 5  # seconds between ticks ($AGENTCTL_TICK); every step is bounded or rate-limited inside


# NO STALLED HOLDER (the owner, 2026-10-27): one land's hook hung 2.5 h in sbt-warm and blocked every lander behind the
# lock. A step land runs (the hook's dry run, the push) that makes NO PROGRESS -- its output does not grow and its
# process group uses no CPU -- for LAND_STALL_MIN minutes ($AGENTCTL_LAND_STALL_MIN) is ended (its process group,
# TERM then KILL) and land says `NOT LANDED: stalled <step>`. And the long checks run BEFORE the lock: under it, only a
# fetch, a check that main has not moved since, and the push (whose hook reuses the dry run's pass). Waiters print
# how long the holder has held the lock, and what it is doing, every LAND_WAIT_REPORT_MIN minutes.
LAND_STALL_MIN = 20


STALL_CPU_SECONDS = 1.0  # CPU that counts as progress per window: a watchdog's polls are not work


ASK_REMIND_MAX = 4 * 3600     # ...then doubling up to this ($AGENTCTL_ASK_REMIND_MAX)


RED_FIXER_WALL = 3 * 3600    # a dispatched fixer's maximum wall time ($AGENTCTL_RED_FIXER_WALL)


RED_RENEW_EVERY = 60         # a fixer's wrapper renews its lease this often


WATCH_TIMEOUT = 3 * 3600          # either run undecided by then: exit 2, could not tell


WATCH_CONV_START = 3600           # no convergence (or takes) run containing the sha started by then: said, not waited for


# WATCHES FROM ITEMS (design §5.1 `landed`, phase 1 step 9): a landed sha is a `watch` item; each tick evaluates it ONCE
# (no detached process, no loop): its Main run and convergence run (pick_main_run / pick_convergence_run), WATCH OK
# -> ok (the worktree closed when proven merged), WATCH RED -> red (its author told; the red machine attaches it), still
# undecided at its 3 h deadline -> unknown, an escalation (a stuck run). The verdicts reach the author's inbox as
# events. `land --follow` watches in-process (the agent waits on purpose) by CLAIMING the sha's item (WatchClaim): the
# tick leaves a live claim alone, and the follower's verdict goes on the item, so one sha is watched once.
WATCH_EVERY = 60


BLAME_MAX = 90 * 60              # s a red may wait for its culprit to be decided before a fresh fixer gets it ($AGENTCTL_BLAME_MAX)


# A CLONE NEVER TRIPS SYSTEMD (2026-10-29: the reconciler is Type=notify, TimeoutStartSec=300, WatchdogSec=120; movies
# is a ~650 MB pack). While a clone runs, every CLONE_KEEPALIVE s: before READY, EXTEND_TIMEOUT_USEC (the start
# timeout pushed out past the next keepalive); after it, WATCHDOG=1; both with a STATUS= line. The clone itself is
# bounded by CLONE_TIMEOUT, so the extensions end.
CLONE_KEEPALIVE = 20


# THE AGENTS' DETAILS ARE BOUNDED PER AGENT AND CACHED PER WORKTREE HEAD (2026-10-29: the section was "no answer within
# 16 s" on every refresh -- the processor runs at launchd's background QoS, where three git calls in each of 10 movies
# worktrees, one after the other, took 117 s; the same pass in a shell took 3 s). Each agent's details are read in a
# thread of their own, all at once; the page waits STATUS_AGENT_BOUND s for each and shows a slower one from its last
# read (marked stale) while the read finishes in the background for the next refresh (one read per worktree at a time).
# The commits and their files are read again only when the worktree's HEAD (read from its files, no git) moved; `git
# status` at most every STATUS_DIRTY_TTL s. Logs and transcripts are read by seeking from their end.
STATUS_AGENT_BOUND = 4      # s the page waits for one agent's details ($AGENTCTL_STATUS_AGENT_BOUND)


ALERT_QUIET_MAX = 24 * 3600  # s a READ alert stays its item's live one after its last event ($AGENTCTL_ALERT_QUIET_MAX)


WA_RED_MAX = 30 * 60       # s agentctl's tests.yml may stay red on main ($AGENTCTL_WA_RED_MAX)


GOV_POLL = 2.0


# CHECK SLOTS AND A CHANGE'S CHECK ARE LEASES (design §6, §7.1; phase 1 step 4): `slot/check/<repo>/<i>` (one per
# check tree) and `check/<entry>` (its checker), TTL CHECK_LEASE_TTL renewed on the check's real progress
# (run_watched's output and CPU; a remote check's run status), max hold CHECK_LEASE_MAX. A checker that stops
# progressing loses both: the slot frees, and the entry's next checker adopts its remote run. No lock files.
CHECK_LEASE_TTL = 10 * 60


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


RED_CRASH_BLOCK_MAX = 24 * 3600    # $AGENTCTL_RED_CRASH_BLOCK_MAX


SLEEP_MIN = 120      # s of sleep inside a lapse's window that count as "the host was asleep" ($AGENTCTL_SLEEP_MIN)


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


# THE LAPTOP'S PRESSURE, read as it is NOW. Two movies reds sat `waiting: laptop pressure (swap 17.7 of 19.0 GB used)`
# for want of a fixer (2026-10-31) while memory_pressure said 58% free at load 5: macOS keeps pages swapped out hours
# ago counted as swap USED until they are touched, so swap's level -- as a share (7c3c342 dropped that) or in GB -- is
# no signal. The signals are macOS's free % (kern.memorystatus_level: memory_pressure's "free percentage", one cheap
# sysctl; Linux: MemAvailable of MemTotal), the kernel's own pressure level (kern.memorystatus_vm_pressure_level: 1
# normal, 2 warn, 4 critical), the 1-min load, and swap only while it GROWS (over the last LAPTOP_SWAP_WINDOW, from
# samples kept in <gov>/swap-samples.json). Under pressure the governor holds every class but red (gov_fits): NOTHING
# STOPS A GATE FIX, EVER (the owner, 2026-10-31) -- the red dispatcher never reads it, and a red fix's ticket never
# waits. `agentctl gov status` and `agentctl red status` show the reading with its numbers.
LAPTOP_FREE_PCT = 20          # holds other work while macOS's memory free % is under this


LAPTOP_PRESSURE_LEVEL = 2     # holds other work at this kernel pressure level or over (1 normal, 2 warn, 4 critical)


LAPTOP_SWAP_GROWTH_MB = 1024  # holds other work while swap grew this much (MB) within LAPTOP_SWAP_WINDOW


LAPTOP_SWAP_WINDOW = 600      # seconds of swap samples the growth is read over


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


BLAME_PROBE_WALL = 2 * 3600      # s one probe may run before it is no verdict ($AGENTCTL_BLAME_PROBE_WALL)


GOV_CPU_FACTOR = 1.0    # charged cores (or the load) + a job's own may reach this x the cores ($AGENTCTL_GOV_CPU_FACTOR)


# NO SILENT WAIT AT GREEN (2026-10-30). Changes waiting while the gate is green, with no hold and no batch checking,
# bisecting, pushing or held, are a stall: counted per tick (batch-progress.json in the waiting dir) and, past
# WAITING_GREEN_TICKS ticks, a STALLED condition. Its probe (batch_progress) reads the same count, so the tick that
# finds the batch moving again -- or nothing waiting, or the gate red -- is the one that clears it.
WAITING_GREEN_TICKS = 100  # ticks (~10 min) changes may wait at a green gate with no batch moving: then STALLED


# ORPHANED WORK (the owner, 2026-10-28: "review and fix landing code ... so things don't get missed or stalled"; the
# audit found movies-wt-workflow-prune rejected 6x and idle 15 h, identity-impact / unit-before-push / red-local idle
# 12-19 h, flipflop / nodrift-cadence / unify / mongo-csot never landed at all): every maintain pass, a worktree whose
# commits are not on origin/main (by patch) and whose author is gone -- no process in it, no land, no live claim,
# idle ORPHAN_IDLE, no live session that ever named it -- gets ONE action: a re-land (its last verdict infra, unknown or
# none; only with LAND INTENT, never work NOT FOR LANDING, and from a throwaway tree, never the author's: see below and
# reland_worktree), or a dispatched fixer (a real failure: the checks rejected it). Still orphaned after that: UNLANDED ORPHAN, a condition whose probe clears
# when its commits are on main, or somebody works in it again. Nothing is ever deleted.
ORPHAN_IDLE = 2 * 3600


W5_MAX = 300            # the processor's duties beat (its bounded steps take up to 2 min)


# THE KNOBS: one typed accessor each (its default and doc beside it; the config reference is generated from these
# reads, so a read here is a knob listed). The code calls the accessor, never the environment -- tests/test_knobs.py.
STEP_BOUND = 120  # s one tick step may run before it is abandoned (the red tick's steps and the sync steps)


def knob_alert_quiet_max() -> float:
    return env_num("AGENTCTL_ALERT_QUIET_MAX", ALERT_QUIET_MAX)


def knob_orphan_idle() -> float:
    return env_num("AGENTCTL_ORPHAN_IDLE", ORPHAN_IDLE)


def knob_wa_red_max() -> float:
    return env_num("AGENTCTL_WA_RED_MAX", WA_RED_MAX)


def knob_max_foreign() -> float:
    return env_num("AGENTCTL_MAX_FOREIGN", MAX_FOREIGN)


def knob_watch_conv_start() -> float:
    return env_num("AGENTCTL_WATCH_CONV_START", WATCH_CONV_START)


def knob_watch_timeout() -> float:
    return env_num("AGENTCTL_WATCH_TIMEOUT", WATCH_TIMEOUT)


def knob_watch_poll() -> float:
    return env_num("AGENTCTL_WATCH_POLL", WATCH_EVERY)


def knob_tick() -> float:
    return env_num("AGENTCTL_TICK", TICK)


def knob_laptop_swap_window() -> float:
    return env_num("AGENTCTL_LAPTOP_SWAP_WINDOW", LAPTOP_SWAP_WINDOW)


def knob_laptop_free_pct() -> float:
    return env_num("AGENTCTL_LAPTOP_FREE_PCT", LAPTOP_FREE_PCT)


def knob_laptop_pressure_level() -> float:
    return env_num("AGENTCTL_LAPTOP_PRESSURE_LEVEL", LAPTOP_PRESSURE_LEVEL)


def knob_laptop_swap_growth_mb() -> float:
    return env_num("AGENTCTL_LAPTOP_SWAP_GROWTH_MB", LAPTOP_SWAP_GROWTH_MB)


def knob_w5_max() -> float:
    return env_num("AGENTCTL_W5_MAX", W5_MAX)


def knob_blame_probe_wall() -> float:
    return env_num("AGENTCTL_BLAME_PROBE_WALL", BLAME_PROBE_WALL)


def knob_status_agent_bound() -> float:
    return env_num("AGENTCTL_STATUS_AGENT_BOUND", STATUS_AGENT_BOUND)


def knob_check_lease_ttl() -> float:
    return env_num("AGENTCTL_CHECK_LEASE_TTL", CHECK_LEASE_TTL)


def knob_waiting_green_ticks() -> float:
    return env_num("AGENTCTL_WAITING_GREEN_TICKS", WAITING_GREEN_TICKS)


def knob_red_crash_loop() -> float:
    return env_num("AGENTCTL_RED_CRASH_LOOP", RED_CRASH_LOOP)


def knob_red_orphan_grace() -> float:
    return env_num("AGENTCTL_RED_ORPHAN_GRACE", RED_ORPHAN_GRACE)


def knob_red_stall() -> float:
    return env_num("AGENTCTL_RED_STALL", RED_STALL)


def knob_step_bound() -> float:
    return env_num("AGENTCTL_STEP_BOUND", STEP_BOUND)


def knob_red_renew() -> float:
    return env_num("AGENTCTL_RED_RENEW", RED_RENEW_EVERY)


def knob_bg_poll() -> float:
    return env_num("AGENTCTL_BG_POLL", BG_POLL)


def knob_red_fixer_wall() -> float:
    return env_num("AGENTCTL_RED_FIXER_WALL", RED_FIXER_WALL)


def knob_ask_remind_max() -> float:
    return env_num("AGENTCTL_ASK_REMIND_MAX", ASK_REMIND_MAX)


def knob_blame_max() -> float:
    return env_num("AGENTCTL_BLAME_MAX", BLAME_MAX)


def knob_token_summary() -> float:
    return env_num("AGENTCTL_TOKEN_SUMMARY", TOKEN_MISSING_SUMMARY)


def knob_sbt_progress_cpu_per_min() -> float:
    return env_num("AGENTCTL_SBT_PROGRESS_CPU_PER_MIN", SBT_PROGRESS_CPU_PER_MIN)


def knob_swap_free_floor_mb() -> float:
    return env_num("AGENTCTL_SWAP_FREE_FLOOR_MB", SWAP_FREE_FLOOR_MB)


def knob_sbt_idle_pressure_min() -> float:
    return env_num("AGENTCTL_SBT_IDLE_PRESSURE_MIN", SBT_IDLE_PRESSURE_MIN)


def knob_sbt_idle_min() -> float:
    return env_num("AGENTCTL_SBT_IDLE_MIN", SBT_IDLE_MIN)


def knob_sbt_stall_min() -> float:
    return env_num("AGENTCTL_SBT_STALL_MIN", SBT_STALL_MIN)


def knob_sleep_min() -> float:
    return env_num("AGENTCTL_SLEEP_MIN", SLEEP_MIN)


def knob_red_crash_block_max() -> float:
    return env_num("AGENTCTL_RED_CRASH_BLOCK_MAX", RED_CRASH_BLOCK_MAX)


def knob_red_decision_block_max() -> float:
    return env_num("AGENTCTL_RED_DECISION_BLOCK_MAX", RED_DECISION_BLOCK_MAX)


def knob_red_old_run() -> float:
    return env_num("AGENTCTL_RED_OLD_RUN", RED_OLD_RUN)


def knob_clone_keepalive() -> float:
    return env_num("AGENTCTL_CLONE_KEEPALIVE", CLONE_KEEPALIVE)


def knob_gov_cpu_factor() -> float:
    return env_num("AGENTCTL_GOV_CPU_FACTOR", GOV_CPU_FACTOR)


def knob_gov_poll() -> float:
    return env_num("AGENTCTL_GOV_POLL", GOV_POLL)


def knob_stall_cpu_seconds() -> float:
    return env_num("AGENTCTL_STALL_CPU_SECONDS", STALL_CPU_SECONDS)


def knob_land_stall_min() -> float:
    return env_num("AGENTCTL_LAND_STALL_MIN", LAND_STALL_MIN)


def env_num(name: str, default: float) -> float:
    try:
        return float(os.environ[name]) if os.environ.get(name) else default
    except ValueError:
        return default


# A SILENT MACHINE'S LEASE CLOCKS PAUSE (2026-11-03: the family owners restarted 22 / 11 / 3 times, ended "STOPPED: the
# owner's heartbeat lease was lost" -- the laptop asleep on battery, or the tunnel to this store down (Errno 61 every
# ~30 min that day), past OWNER_TTL: every laptop lease lapsed at once and the reconciler re-offered their reds). The
# store records each machine's last contact (contact(): every store RPC the laptop's agentctl makes says which machine
# it is). A lease whose holder's machine (its holder's prefix, `laptop:`) has been silent past CONTACT_GAP has its clock
# paused from then on: read (get, list, renew, acquire, take) as extended by the silence beyond CONTACT_GAP, and on the
# machine's next contact extended by that for real -- so a lease live when its machine went quiet is still live when it
# comes back, and nobody takes it meanwhile. A machine still talking (its sync loop beats every minute) pauses nothing:
# a dead wrapper's leases on a laptop that is awake lapse as before. Bounded: CONTACT_PAUSE_MAX, then they lapse.
CONTACT_GAP = 120               # s of a machine's silence before its leases' clocks pause ($AGENTCTL_CONTACT_GAP)


CONTACT_PAUSE_MAX = 12 * 3600   # s a silent machine's leases are paused at most ($AGENTCTL_CONTACT_PAUSE_MAX)


def knob_contact_gap() -> float:
    return env_num("AGENTCTL_CONTACT_GAP", CONTACT_GAP)


STREAK_REREADS = 3  # stale or missing streak run signatures read again per tick ($AGENTCTL_STREAK_REREADS)


def knob_streak_rereads() -> int:
    """The tick's re-reads of stale streak run signatures (redepisode.streak_budget)."""
    return int(env_num("AGENTCTL_STREAK_REREADS", STREAK_REREADS))


def knob_contact_pause_max() -> float:
    return env_num("AGENTCTL_CONTACT_PAUSE_MAX", CONTACT_PAUSE_MAX)
