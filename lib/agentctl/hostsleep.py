# agentctl, part `hostsleep`: the host's sleep: a no-sleep assertion held for a lease's life, the sleep log read, a lease lapse told from a crash
# A real module (lib/agentctl/__init__.py MODULES): its own imports, the standard library's and MODULES' only.

import os
import re
import subprocess
from typing import Any
from agentctl.procrunner import process_runner
from agentctl.clock import epoch_now
from agentctl.ids import dt
from agentctl.timefmt import iso_epoch
from agentctl.govknobs import knob_awake_pid, knob_caffeinate, knob_sleep_log, knob_sleep_min
from agentctl.core import spawn_detached
from agentctl.owners import OWNER_TTL


# THE HOST SLEEPING IS NO CRASH (2026-11-01, core/movies main #2046 `fixer-crash-loop`: both family owners' wrappers froze
# in a macOS maintenance sleep -- 03:05:44 to 03:21:11, longer than OWNER_TTL -- so their heartbeat leases lapsed
# ("lease expired"), the wrappers woke to "heartbeat lease was lost" and exited, the dispatcher restarted them, and three
# such cycles blocked the red on the owner with the harness' "cause" being nothing but a closed lid). A wrapper now holds
# a `caffeinate -s` assertion for its life (keep_awake), and a lease lapse that overlaps a host sleep is neutral to the
# breaker (host_slept) and says so in the evidence. `-s` holds only on AC power (owner 2026-11-01: "prevent laptop going
# to sleep when agentctl is running and power supply is on"); on battery the laptop sleeps and host_slept covers it.
# The service (waiting run --service) holds the same assertion, so a plugged-in laptop never sleeps under agentctl.
LEASE_LAPSE = re.compile(r"(lease expired|ORPHANED|STOPPED: the (red's|owner's) (heartbeat )?lease was lost)")


_SLEEP_CACHE = [0.0, []]


def keep_awake(pid: int | None = None) -> "subprocess.Popen[Any] | None":
    """Hold a no-system-sleep assertion, valid on AC power only, for `pid` (default: this process) so a held lease is
    never starved by the laptop's sleep. Once per pid: an exec'd restart keeps the assertion it already has. Best
    effort: the Popen, or None (already held, no caffeinate, $AGENTCTL_CAFFEINATE empty)."""
    exe = knob_caffeinate()
    who = str(pid or os.getpid())
    if not exe or not os.access(exe, os.X_OK) or knob_awake_pid() == who:
        return None
    os.environ["AGENTCTL_AWAKE_PID"] = who
    try:
        return spawn_detached([exe, "-s", "-w", who], stdin=subprocess.DEVNULL,
                              stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
    except OSError:
        return None


def _sleep_log_text() -> Any:
    """`pmset -g log`'s text (or $AGENTCTL_SLEEP_LOG's file); "" when unreadable."""
    try:
        path = knob_sleep_log()
        if path:
            return open(path).read()
        return process_runner().run(["pmset", "-g", "log"], capture_output=True, text=True, timeout=30).stdout
    except (OSError, subprocess.SubprocessError):
        return ""


def sleep_intervals():
    """[(start, end)] epoch seconds of the host's recent sleeps, from `pmset -g log` (or $AGENTCTL_SLEEP_LOG, a file in
    its format). [] when unreadable. Cached for a minute."""
    now = epoch_now()
    if now - _SLEEP_CACHE[0] < 60 and not knob_sleep_log():
        return _SLEEP_CACHE[1]
    text = _sleep_log_text()
    out, start = [], None
    for m in re.finditer(r"^(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d [+-]\d{4}) (Sleep|Wake|DarkWake)\b", text, re.M):
        try:
            at = dt.datetime.strptime(m.group(1), "%Y-%m-%d %H:%M:%S %z").timestamp()
        except ValueError:
            continue
        if m.group(2) == "Sleep":
            start = at
        elif start is not None:
            out.append((start, at))
            start = None
    if not knob_sleep_log():
        _SLEEP_CACHE[:] = [now, out]
    return out


def host_slept(end, window):
    """Seconds the host slept in [end - window, end] (end: epoch seconds)."""
    lo = end - window
    return sum(max(0.0, min(b, end) - max(a, lo)) for a, b in sleep_intervals())


def lapse_in_sleep(h):
    """True for a launch whose lease merely lapsed (no work seen) while the host slept: not a crash."""
    o = str(h.get("outcome") or "")
    if not LEASE_LAPSE.search(o):
        return False
    end = iso_epoch(h.get("at"))
    if end is None:
        return False
    return host_slept(end, OWNER_TTL + 300) >= knob_sleep_min()
