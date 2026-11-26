# agentctl, part `watchknobs`: the watching knobs: the watchdog and invariants, alerts, the inbox, notifications, the watch, stats and status
# A real module (lib/agentctl/__init__.py MODULES): its own imports, the standard library's and MODULES' only.

import os
from agentctl.knobread import env_num, env_text


WATCH_TIMEOUT = 3 * 3600          # either run undecided by then: exit 2, could not tell


WATCH_CONV_START = 3600           # no convergence (or takes) run containing the sha started by then: said, not waited for


# WATCHES FROM ITEMS (design §5.1 `landed`, phase 1 step 9): a landed sha is a `watch` item; each tick evaluates it ONCE
# (no detached process, no loop): its Main run and convergence run (pick_main_run / pick_convergence_run), WATCH OK
# -> ok (the worktree closed when proven merged), WATCH RED -> red (its author told; the red machine attaches it), still
# undecided at its 3 h deadline -> unknown, an escalation (a stuck run). The verdicts reach the author's inbox as
# events. `land --follow` watches in-process (the agent waits on purpose) by CLAIMING the sha's item (WatchClaim): the
# tick leaves a live claim alone, and the follower's verdict goes on the item, so one sha is watched once.
WATCH_EVERY = 60


# THE AGENTS' DETAILS ARE BOUNDED PER AGENT AND CACHED PER WORKTREE HEAD (2026-10-29: the section was "no answer within
# 16 s" on every refresh -- the processor runs at launchd's background QoS, where three git calls in each of 10 movies
# worktrees, one after the other, took 117 s; the same pass in a shell took 3 s). Each agent's details are read in a
# thread of their own, all at once; the page waits STATUS_AGENT_BOUND s for each and shows a slower one from its last
# read (marked stale) while the read finishes in the background for the next refresh (one read per worktree at a time).
# The commits and their files are read again only when the worktree's HEAD (read from its files, no git) moved; `git
# status` at most every STATUS_DIRTY_TTL s. Logs and transcripts are read by seeking from their end.
STATUS_AGENT_BOUND = 4      # s the page waits for one agent's details ($AGENTCTL_STATUS_AGENT_BOUND)


W5_MAX = 300            # the processor's duties beat (its bounded steps take up to 2 min)


def knob_watch_conv_start() -> float:
    return env_num("AGENTCTL_WATCH_CONV_START", WATCH_CONV_START)


def knob_watch_timeout() -> float:
    return env_num("AGENTCTL_WATCH_TIMEOUT", WATCH_TIMEOUT)


def knob_watch_poll() -> float:
    return env_num("AGENTCTL_WATCH_POLL", WATCH_EVERY)


def knob_w5_max() -> float:
    return env_num("AGENTCTL_W5_MAX", W5_MAX)


def knob_status_agent_bound() -> float:
    return env_num("AGENTCTL_STATUS_AGENT_BOUND", STATUS_AGENT_BOUND)


def knob_inbox_dir() -> str:
    return env_text("AGENTCTL_INBOX_DIR")  # the inbox's directory (default inbox under the state root)


# ---------------------------------------------------------------- an owner alert whose condition has cleared
# The owner, 2026-11-06: "why is there always 30 unread in inbox" -- 44 owner alerts, nearly all for what had cleared
# days before: 20 RED BLOCKED ON OWNER whose reds were closed, 17 UNLANDED ORPHAN of Forgejo-era worktrees, 3 NETWORK
# long recovered, 3 WATCH UNKNOWN whose commits later runs had decided, a HELD batch that moved on. Their items were
# keyed before the probes knew them (`run:`, `text:`, `orphan:`), so nothing ever closed them. Now each such alert's
# CONDITION is probed from its kind and verdict, whatever its item, and closed with the reason once it has cleared.
# What cannot be told (no red named, no gate read, git failing) stays as it is: never closed on a guess.
ALERT_ORPHAN_PROBE_EVERY = 600  # s between git probes of one orphan's worktree ($AGENTCTL_ALERT_ORPHAN_PROBE)


ALERT_NETWORK_GRACE = 600       # s a NETWORK alert stays before "no NETWORK condition open" closes it


ALERT_RED_OWNER_LEVEL = 2  # offer rounds a red goes untaken (nobody holding it, no family owner) before it is the owner's


ALERT_RECONCILE_EVERY = 60  # s between reconcile passes ($AGENTCTL_ALERT_RECONCILE)


INBOX_WAIT_POLL = 1.0  # seconds between stat()s of the inbox directory ($AGENTCTL_INBOX_POLL)


# ---------------------------------------------------------------- the invariant watchdog (design §10, phase 1 step 2)
# A separate, cheap, dumb launchd job (net.bitcashier.agentctl-watchdog, every 60 s): `agentctl watchdog` reads today's
# files only -- the queue entries, the published gate line, the processor's beat file, the inbox -- and checks the
# invariants the reconciler must converge to. A violation is a condition (INVARIANT, its probe the invariant itself:
# it closes when the invariant holds again) and a macOS notification; on W5 (the processor's duties stopped) it also
# kickstarts the processor (silent over awake time; the kick keeps its token), at most once per WATCHDOG_KICK_GAP. It
# shares no code path with the processor beyond the registry and the read-only readers.
W1_MAX = 2 * 3600       # the oldest unlanded change ($AGENTCTL_W1_MAX); while main is red, counted from the red's start


W2_MAX = 10 * 60        # a gate red with nobody on it


W6_MAX = 5 * 60         # a due escalation not notified


W8_WINDOW = 3600         # W8 looks at the change items that closed within this ($AGENTCTL_W8_WINDOW)


W7_MAX = 5 * 60         # phase 2: an event ci-runner wrote that the laptop's sync has not projected (60x its 5 s tick)


WM_MAX = 45 * 60        # maintain's last pass (launchd every 15 min)


WB_MAX = 10 * 60


WF_PCT = 70  # % of kern.maxfiles open system-wide past which WF is violated ($AGENTCTL_WF_PCT)


LIVE_POLL = 1.0                 # s between looks at a quiet file ($AGENTCTL_LIVE_POLL)


# WHAT THE OWNER DOES ABOUT A FAILED DELIVERY, per channel and cause (2026-11-22: WN told the owner to "allow
# terminal-notifier" -- not installed -- for an osascript killed by its 10 s timeout: the laptop at load ~150, osascript's
# cold start (XProtect, trustd) took the whole 10 s and was killed at its TCC request; the same command takes 0.5 s on an
# idle machine). Each channel's failure carries its own fix; WN names them.
NOTIFY_TIMEOUT = 30  # s, each channel: an overloaded laptop's osascript cold start takes 10 s and more


WR_MAX = 40 * 60        # a red fix still unlanded this long after it was queued ($AGENTCTL_WR_MAX; a CI-tier check ~17 min)


WA_UNPINNED_MAX = 15 * 60  # s the newest green run may stay unpinned ($AGENTCTL_WA_UNPINNED_MAX)


WA_UNDECIDED_MAX = 60 * 60  # s main's head may go without a decided run ($AGENTCTL_WA_UNDECIDED_MAX)


WA_READ_EVERY = 300        # s a gh list is kept ($AGENTCTL_WA_READ_EVERY)


STATS_EVERY = 60            # s between passes ($AGENTCTL_STATS_EVERY)


STATS_PER_PASS = 150        # records a pass sends at most (the backfill spreads over passes)


STATS_SETTLE = 15 * 60      # s after an exit before its record is built: the collector exports a run <= 5 min after it ends


STATS_PASS_BOUND = 40       # s a pass works at most (a tick step)


STATUS_SOURCE_BOUND = 8         # s a source may take ($AGENTCTL_STATUS_BOUND); the agents' source twice that


WATCHDOG_SLEEP_GAP = 180  # s since the watchdog's own last pass (launchd: every 60 s) that mean the laptop slept


KICK_HANDOFF_WAIT = 30    # s a kick waits for launchd's new processor to listen before handing it the token


WATCHDOG_MISSES = 2  # consecutive passes a self-healing invariant (W5, WM, W7) fails before its condition opens


# ONE EPISODE, ONE NOTIFICATION (2026-11-23: W1 told "unlanded change ...-what-if-py-edef9e (queued: nothing holds it,
# yet it has not started)" every few minutes -- its condition closed whenever the next pass found the hold recorded
# again, and each re-open notified). An invariant's violation is told once per SUBJECT (the change, red or run it names:
# its detail less the ages and counts that move pass to pass) within WATCHDOG_RETELL; the condition still opens and
# closes with the facts, `status` and the conditions say it each pass. Tests: tests/test_watchdog_episode.py.
WATCHDOG_RETELL = 6 * 3600  # s ($AGENTCTL_WATCHDOG_RETELL)


WATCH_STALL_MIN = 120  # a watch with no change in its runs for this long ends WATCH STALLED


# ONE WATCH PER SHA (review round 6): the sha's watch is its store item (start_watch, created once). ci-runner's batch
# cannot see a laptop follower's await lock, so it makes the item too: a `land --follow` CLAIMS the item (followed_by,
# a lease it renews each look; the tick leaves a live claim alone) and writes its verdict there (finish_watch); a
# second follower, on any host, reads that verdict instead of watching GitHub again; a lapsed claim is taken over. An
# item that ended `unknown` (a stuck run past its 3 h) is claimed and re-watched the same way, its verdict kept as its
# `outcome` (watch_outcome): a terminal item keeps its state.
WATCH_FOLLOW_LEASE = 600  # s a follower's claim on a sha's watch item lasts unrenewed; then the tick (or another follower) takes it


WATCH_ERROR_POLL = 30  # s of the first retry delay after the watch could not read the runs ($AGENTCTL_WATCH_ERROR_POLL)


def knob_alert_network_grace() -> float:
    return env_num("AGENTCTL_ALERT_NETWORK_GRACE", ALERT_NETWORK_GRACE)


def knob_alert_orphan_probe() -> float:
    return env_num("AGENTCTL_ALERT_ORPHAN_PROBE", ALERT_ORPHAN_PROBE_EVERY)


def knob_alert_reconcile() -> float:
    return env_num("AGENTCTL_ALERT_RECONCILE", ALERT_RECONCILE_EVERY)


def knob_alert_red_owner_level() -> float:
    return env_num("AGENTCTL_ALERT_RED_OWNER_LEVEL", ALERT_RED_OWNER_LEVEL)


def knob_inbox_poll() -> float:
    return env_num("AGENTCTL_INBOX_POLL", INBOX_WAIT_POLL)


def knob_kick_handoff_wait() -> float:
    return env_num("AGENTCTL_KICK_HANDOFF_WAIT", KICK_HANDOFF_WAIT)


def knob_live_poll() -> float:
    return env_num("AGENTCTL_LIVE_POLL", LIVE_POLL)


def knob_notify_timeout() -> float:
    return env_num("AGENTCTL_NOTIFY_TIMEOUT", NOTIFY_TIMEOUT)


def knob_stats_every() -> float:
    return env_num("AGENTCTL_STATS_EVERY", STATS_EVERY)


def knob_stats_pass_bound() -> float:
    return env_num("AGENTCTL_STATS_PASS_BOUND", STATS_PASS_BOUND)


def knob_stats_per_pass() -> float:
    return env_num("AGENTCTL_STATS_PER_PASS", STATS_PER_PASS)


def knob_stats_settle() -> float:
    return env_num("AGENTCTL_STATS_SETTLE", STATS_SETTLE)


def knob_status_bound() -> float:
    return env_num("AGENTCTL_STATUS_BOUND", STATUS_SOURCE_BOUND)


def knob_w1_max() -> float:
    return env_num("AGENTCTL_W1_MAX", W1_MAX)


def knob_w2_max() -> float:
    return env_num("AGENTCTL_W2_MAX", W2_MAX)


def knob_w6_max() -> float:
    return env_num("AGENTCTL_W6_MAX", W6_MAX)


def knob_w7_max() -> float:
    return env_num("AGENTCTL_W7_MAX", W7_MAX)


def knob_w8_window() -> float:
    return env_num("AGENTCTL_W8_WINDOW", W8_WINDOW)


def knob_watchdog_misses() -> float:
    return env_num("AGENTCTL_WATCHDOG_MISSES", WATCHDOG_MISSES)


def knob_watchdog_retell() -> float:
    return env_num("AGENTCTL_WATCHDOG_RETELL", WATCHDOG_RETELL)


def knob_watchdog_sleep_gap() -> float:
    return env_num("AGENTCTL_WATCHDOG_SLEEP_GAP", WATCHDOG_SLEEP_GAP)


def knob_watch_error_poll() -> float:
    return env_num("AGENTCTL_WATCH_ERROR_POLL", WATCH_ERROR_POLL)


def knob_watch_follow_lease() -> float:
    return env_num("AGENTCTL_WATCH_FOLLOW_LEASE", WATCH_FOLLOW_LEASE)


def knob_watch_stall_min() -> float:
    return env_num("AGENTCTL_WATCH_STALL_MIN", WATCH_STALL_MIN)


def knob_wa_read_every() -> float:
    return env_num("AGENTCTL_WA_READ_EVERY", WA_READ_EVERY)


def knob_wa_undecided_max() -> float:
    return env_num("AGENTCTL_WA_UNDECIDED_MAX", WA_UNDECIDED_MAX)


def knob_wa_unpinned_max() -> float:
    return env_num("AGENTCTL_WA_UNPINNED_MAX", WA_UNPINNED_MAX)


def knob_wb_max() -> float:
    return env_num("AGENTCTL_WB_MAX", WB_MAX)


def knob_wf_pct() -> float:
    return env_num("AGENTCTL_WF_PCT", WF_PCT)


def knob_wm_max() -> float:
    return env_num("AGENTCTL_WM_MAX", WM_MAX)


def knob_wr_max() -> float:
    return env_num("AGENTCTL_WR_MAX", WR_MAX)


def knob_live_globs() -> str:
    return env_text("AGENTCTL_LIVE_GLOBS")  # the live sessions' transcript globs (default: the Claude projects')


def knob_notifier() -> str:
    return env_text("AGENTCTL_NOTIFIER")  # the notification tool (default: terminal-notifier on PATH, else osascript)


def knob_notify() -> str:
    return env_text("AGENTCTL_NOTIFY")  # a command run to notify the owner (set: used instead of the notifier)


def knob_status_page() -> str:
    return env_text("AGENTCTL_STATUS_PAGE")  # 0, off or no: the status page is not served


def knob_status_port() -> str:
    return env_text("AGENTCTL_STATUS_PORT")  # the status page's port (default: the first free of its range)


def knob_alert_after(kind: str, default: float) -> float:
    """The seconds a red of this kind (a name of the alert routes) may wait before it goes to the owner: $AGENTCTL_ALERT_<KIND>_AFTER."""
    return env_num(f"AGENTCTL_ALERT_{kind}_AFTER", default)


ALERT_QUIET_MAX = 24 * 3600  # s a READ alert stays its item's live one after its last event ($AGENTCTL_ALERT_QUIET_MAX)


WA_RED_MAX = 30 * 60       # s agentctl's tests.yml may stay red on main ($AGENTCTL_WA_RED_MAX)


def knob_alert_quiet_max() -> float:
    return env_num("AGENTCTL_ALERT_QUIET_MAX", ALERT_QUIET_MAX)


def knob_wa_red_max() -> float:
    return env_num("AGENTCTL_WA_RED_MAX", WA_RED_MAX)


WP_RSS_BY_NAME = "idea=8"  # comma list of name=GB: the RSS limit of the process of that name, others WP_RSS_GB (IntelliJ's JVM sits at 4-5 GB)


def knob_wp_rss_by_name() -> str:
    return os.environ.get("AGENTCTL_WP_RSS_BY_NAME", WP_RSS_BY_NAME)
