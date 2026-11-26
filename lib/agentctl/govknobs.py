# agentctl, part `govknobs`: the governor and machine knobs: tickets, budgets, load and swap probes, sbt servers, keep-awake
# A real module (lib/agentctl/__init__.py MODULES): its own imports, the standard library's and MODULES' only.

import os
from agentctl.knobread import env_num, env_text


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


# A CLONE NEVER TRIPS SYSTEMD (2026-10-29: the reconciler is Type=notify, TimeoutStartSec=300, WatchdogSec=120; movies
# is a ~650 MB pack). While a clone runs, every CLONE_KEEPALIVE s: before READY, EXTEND_TIMEOUT_USEC (the start
# timeout pushed out past the next keepalive); after it, WATCHDOG=1; both with a STATUS= line. The clone itself is
# bounded by CLONE_TIMEOUT, so the extensions end.
CLONE_KEEPALIVE = 20


GOV_POLL = 2.0


# CHECK SLOTS AND A CHANGE'S CHECK ARE LEASES (design §6, §7.1; phase 1 step 4): `slot/check/<repo>/<i>` (one per
# check tree) and `check/<entry>` (its checker), TTL CHECK_LEASE_TTL renewed on the check's real progress
# (run_watched's output and CPU; a remote check's run status), max hold CHECK_LEASE_MAX. A checker that stops
# progressing loses both: the slot frees, and the entry's next checker adopts its remote run. No lock files.
CHECK_LEASE_TTL = 10 * 60


SLEEP_MIN = 120      # s of sleep inside a lapse's window that count as "the host was asleep" ($AGENTCTL_SLEEP_MIN)


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


GOV_CPU_FACTOR = 1.0    # charged cores (or the load) + a job's own may reach this x the cores ($AGENTCTL_GOV_CPU_FACTOR)


def knob_laptop_swap_window() -> float:
    return env_num("AGENTCTL_LAPTOP_SWAP_WINDOW", LAPTOP_SWAP_WINDOW)


def knob_laptop_free_pct() -> float:
    return env_num("AGENTCTL_LAPTOP_FREE_PCT", LAPTOP_FREE_PCT)


def knob_laptop_pressure_level() -> float:
    return env_num("AGENTCTL_LAPTOP_PRESSURE_LEVEL", LAPTOP_PRESSURE_LEVEL)


def knob_laptop_swap_growth_mb() -> float:
    return env_num("AGENTCTL_LAPTOP_SWAP_GROWTH_MB", LAPTOP_SWAP_GROWTH_MB)


def knob_check_lease_ttl() -> float:
    return env_num("AGENTCTL_CHECK_LEASE_TTL", CHECK_LEASE_TTL)


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


def knob_clone_keepalive() -> float:
    return env_num("AGENTCTL_CLONE_KEEPALIVE", CLONE_KEEPALIVE)


def knob_gov_cpu_factor() -> float:
    return env_num("AGENTCTL_GOV_CPU_FACTOR", GOV_CPU_FACTOR)


def knob_gov_poll() -> float:
    return env_num("AGENTCTL_GOV_POLL", GOV_POLL)


def knob_jvm_ps() -> str:
    return env_text("AGENTCTL_JVM_PS")  # a fixture file of ps rows standing for the process table (tests)


def knob_jvm_cwd() -> str:
    return env_text("AGENTCTL_JVM_CWD")  # a fixture file of `pid cwd` rows (tests)


def knob_jvm_kept() -> str:
    return env_text("AGENTCTL_JVM_KEPT")  # a fixture file of kept checkouts (tests)


def knob_sbt_reap() -> str:
    return os.environ.get("AGENTCTL_SBT_REAP", "")  # 0: never reap this machine's sbt servers (tests)


def knob_caffeinate() -> str:
    return os.environ.get("AGENTCTL_CAFFEINATE", "/usr/bin/caffeinate")  # the keep-awake tool; empty: none


def knob_awake_pid() -> str:
    return env_text("AGENTCTL_AWAKE_PID")  # the process that already holds the keep-awake (a child's inherited copy)


def knob_sleep_log() -> str:
    return env_text("AGENTCTL_SLEEP_LOG")  # a file standing for `pmset -g log` (tests)


def knob_gov_busy_probe() -> str:
    return env_text("AGENTCTL_GOV_BUSY_PROBE")  # a command printing the busy cores (tests)


def knob_gov_load_probe() -> str:
    return env_text("AGENTCTL_GOV_LOAD_PROBE")  # a command printing the load, read as the busy cores (tests)


def knob_gov_ps_probe() -> str:
    return env_text("AGENTCTL_GOV_PS_PROBE")  # a file of ps `pcpu args` rows (tests)


def knob_laptop_pressure() -> str | None:
    return os.environ.get("AGENTCTL_LAPTOP_PRESSURE")  # set (tests): the machine's pressure, `-` or empty for none


# WAITING IS CHEAP (2026-11-19 15:35Z: ~13 sbt-warm waiters each re-read every waiter's liveness -- two `ps` per waiter
# -- and the machine's memory -- four more processes -- every GOV_POLL: ~170 short processes a second, a load of ~60 on 18
# cores that held them all on the load factor, a loop feeding itself). Now a waiter behind the head re-reads the queue
# only when the CHANGE MARK moves (gov_bump: a waiter or ticket leaving) or its backoff (GOV_POLL doubling to
# GOV_IDLE_MAX) runs out, a stat of one file meanwhile; and a waiter's liveness is its pid (a signal 0, no process) with
# its start time read once per GOV_ALIVE_TTL.
GOV_IDLE_MAX = 30.0     # s a waiter behind the head waits at most between two reads of the queue


GOV_ADMIT_MAX = 2 * 3600  # a check waits this long for the governor at most ($AGENTCTL_GOV_ADMIT_MAX)


LAPTOP_LOAD = 2.0             # holds other work while the 1-min load is over this x the cores


# A SECOND SUITE SLOT (2026-11-26): at one, two lanes' pushes queued each pre-push step (suite, stress lane, simulator
# gate) behind the other's -- 20.3 h of suite waits on the cap in 7 days, a step's wait up to 84 min. The cores rule
# (govfits' CPU refusal: busy + the suite's charge within the cores) still decides whether the second may start, so
# two run only when the cores are free; a third queues.
SUITE_CAP = 2  # agentctl's own test-suite runs at once ($AGENTCTL_SUITE_CAP): more agents queue, never pile up


GOV_RESERVE_GB = 1.0


GOV_FREE_NEED_GB = 2.5  # free memory a new JVM needs NOW (beyond the reserve); its heap grows into the budget's share later


# THE CAPACITY (the owner, 2026-11-19: "if cpu is starved then pause until we have enough resources and resume when we
# have too much"): BUSY is max(the live tickets' charged cores, the cores processes use now -- ps's %CPU, less the
# governor's own waiters and idle Claude sessions; never the load average, which read 46 on 18 cores while processes
# used 5.2). Over GOV_STARVED x the cores the laptop is STARVED: every class but red pauses, its waiters blocking with no
# CPU; under GOV_RESUME x it is ok again (between the two it keeps its state: no thrashing at one line); under GOV_SPARE x
# it has SPARE capacity. `agentctl gov capacity` prints it in one line.
GOV_STARVED = 1.0       # x the cores: over it, starved ($AGENTCTL_GOV_STARVED)


GOV_RESUME = 0.8        # x the cores: under it, admitting again ($AGENTCTL_GOV_RESUME)


GOV_SPARE = 0.5         # x the cores: under it, spare ($AGENTCTL_GOV_SPARE)


LAPTOP_ROLE_TTL = 10 * 60          # s a silent holder keeps the role ($AGENTCTL_LAPTOP_ROLE_TTL)


# ---------------------------------------------------------------- the land queue: agents never wait on a land
# The owner (2026-10-27): "agents never wait on a land". `land` rebases the change onto origin/main, runs the squash
# guard, enqueues it and returns at once: `QUEUED <id> (<sha>)`. The always-on `waiting` processor (launchd
# net.bitcashier.agentctl-waiting) runs each queued change's checks in a CHECK TREE of its own (a pool of detached
# worktrees <clone>-wt-check-<n>, kept warm between checks, seeded from the shared build cache when cold), admitted by
# the governor (a check; red for a Fixes-Red fix of a red run, which goes first) -- or on ci-runner's check tier when the
# governor reports pressure (see "remote checks") -- then, under the landing lock: main green (or a change that may land
# while red), the checked tip is pushed (`LANDED`, and a detached watch); main red, it joins `waiting` (`WAITING`). Main
# moved meanwhile: rebased in the tree and checked again. Every verdict goes to the inbox with the agent's worktree.
# `land --follow` keeps the old in-process land (checks, push, watch) for scripts; a red fixer queues and waits
# (fixer_land).
QUEUE_POOL = 4          # check trees, so at most this many queued changes are checked at once ($AGENTCTL_CHECK_TREES);


DISK_MIN_GB = 50  # the Data volume's free space below which the upkeep runs at once, then WD opens ($AGENTCTL_DISK_MIN_GB)


GOV_EVICT_SETTLE = 3  # s to wait after evicting a ticket for its memory to free ($AGENTCTL_GOV_EVICT_SETTLE)


def knob_check_trees() -> float:
    return env_num("AGENTCTL_CHECK_TREES", QUEUE_POOL)


def knob_disk_min_gb() -> float:
    return env_num("AGENTCTL_DISK_MIN_GB", DISK_MIN_GB)


def knob_gov_admit_max() -> float:
    return env_num("AGENTCTL_GOV_ADMIT_MAX", GOV_ADMIT_MAX)


def knob_gov_evict_settle() -> float:
    return env_num("AGENTCTL_GOV_EVICT_SETTLE", GOV_EVICT_SETTLE)


def knob_gov_free_need_gb() -> float:
    return env_num("AGENTCTL_GOV_FREE_NEED_GB", GOV_FREE_NEED_GB)


def knob_gov_idle_max() -> float:
    return env_num("AGENTCTL_GOV_IDLE_MAX", GOV_IDLE_MAX)


def knob_gov_reserve_gb() -> float:
    return env_num("AGENTCTL_GOV_RESERVE_GB", GOV_RESERVE_GB)


def knob_gov_resume() -> float:
    return env_num("AGENTCTL_GOV_RESUME", GOV_RESUME)


def knob_gov_spare() -> float:
    return env_num("AGENTCTL_GOV_SPARE", GOV_SPARE)


def knob_gov_starved() -> float:
    return env_num("AGENTCTL_GOV_STARVED", GOV_STARVED)


def knob_laptop_load() -> float:
    return env_num("AGENTCTL_LAPTOP_LOAD", LAPTOP_LOAD)


def knob_laptop_role_ttl() -> float:
    return env_num("AGENTCTL_LAPTOP_ROLE_TTL", LAPTOP_ROLE_TTL)


def knob_suite_cap() -> float:
    return env_num("AGENTCTL_SUITE_CAP", SUITE_CAP)


def knob_checks_dir() -> str:
    return env_text("AGENTCTL_CHECKS_DIR")  # the directory of the check slots' files (default checks under the state root)


def knob_max_checks() -> str:
    return env_text("AGENTCTL_MAX_CHECKS")  # the check slots' cap, overriding the budget's


def knob_disk_path() -> str:
    return env_text("AGENTCTL_DISK_PATH")  # the path whose free space the disk checks read (default: the home directory)


def knob_gov_budget_gb(ram_gb: float) -> float:
    return env_num("AGENTCTL_GOV_BUDGET_GB", round(0.6 * ram_gb, 1))  # the memory in GB the governor hands out; default 0.6 x the machine's RAM