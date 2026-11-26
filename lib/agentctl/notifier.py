# agentctl, part `notifier`: THE DESKTOP NOTIFIER: one notification through terminal-notifier or osascript, each channel bounded, and why the last reached nobody
# A real module (lib/agentctl/__init__.py MODULES): its own imports, the standard library's and MODULES' only.

import os
import re
import shlex
import subprocess
import sys
from agentctl.procrunner import process_runner
from agentctl.configfile import env_num
from agentctl.core import HOME


def applescript_str(text):
    return '"' + str(text).replace("\\", "\\\\").replace('"', '\\"') + '"'


def notifier_bin():
    """terminal-notifier (clickable), from $AGENTCTL_NOTIFIER, PATH or the nix profile; None when absent."""
    import shutil
    return os.environ.get("AGENTCTL_NOTIFIER") or shutil.which("terminal-notifier") \
        or next((p for p in (f"{HOME}/.nix-profile/bin/terminal-notifier",) if os.path.exists(p)), None)


def notifier_argv(tn, title, text, url=None, group=None):
    """terminal-notifier's argv: a click runs `open -a 'Google Chrome' URL` (Chrome forced); -group replaces the older
    notification of the same item instead of stacking."""
    argv = [tn, "-title", title, "-message", text, "-sound", "Basso"]
    if group:
        argv += ["-group", str(group)]
    if url:
        argv += ["-execute", f"open -a 'Google Chrome' {shlex.quote(url)}"]
    return argv


# WHAT THE OWNER DOES ABOUT A FAILED DELIVERY, per channel and cause (2026-11-22: WN told the owner to "allow
# terminal-notifier" -- not installed -- for an osascript killed by its 10 s timeout: the laptop at load ~150, osascript's
# cold start (XProtect, trustd) took the whole 10 s and was killed at its TCC request; the same command takes 0.5 s on an
# idle machine). Each channel's failure carries its own fix; WN names them.
NOTIFY_TIMEOUT = 30  # s, each channel: an overloaded laptop's osascript cold start takes 10 s and more


NOTIFY_FIX_TIMEOUT = ("{ch} timed out: the laptop was overloaded (`agentctl gov status`, `uptime`); the notifier tries "
                      "again next pass")


NOTIFY_FIX = {"terminal-notifier": "allow terminal-notifier in System Settings > Notifications",
              "osascript": "allow Script Editor (osascript's notifications) in System Settings > Notifications",
              "none": "no notification channel on this host: set $AGENTCTL_NOTIFY"}


_notify_cause = ["", ""]  # (cause, fix) of the last notify() that reached nobody


def notify_cause():
    """Why the last notify() reached nobody ("" when it got through)."""
    return _notify_cause[0]


def notify_fix():
    """What the owner does about the last notify() that reached nobody ("" when it got through)."""
    return _notify_cause[1]


def _delivered(ok, failures=()):
    """failures: (channel, what it said, fix) per channel that failed."""
    failures = list(failures) or [("none", "no channel", NOTIFY_FIX["none"])]
    _notify_cause[:] = ["", ""] if ok else ["; ".join(f"{c}: {w}" for c, w, _f in failures),
                                             "; ".join(dict.fromkeys(f for _c, _w, f in failures))]
    return ok


def _proc_said(r):
    """A failed channel's exit code and the last line it printed (stderr's, else stdout's): the real cause, short."""
    def last(b):
        return next((x.strip() for x in reversed((b or b"").decode(errors="replace").splitlines()) if x.strip()), "")
    said = re.sub(r"\s+", " ", last(r.stderr) or last(r.stdout))[:140]
    return f"exit {r.returncode}" + (f": {said}" if said else "")


def _run_said(e):
    """A channel that never exited: "timed out after N s", or the OS's error -- never the exception's str, whose
    argv (the whole notification text) buries the cause (2026-11-22: WN's "Command '['osascript', ..." cut at 140)."""
    if isinstance(e, subprocess.TimeoutExpired):
        return f"timed out after {e.timeout:g} s"
    return (getattr(e, "strerror", None) or type(e).__name__) + (f": {e.filename}" if getattr(e, "filename", None) else "")


def _channel(ch, argv, fix):
    """Runs one channel: None when it got through, else (channel, what it said, the owner's fix)."""
    try:
        r = process_runner().run(argv, capture_output=True, timeout=env_num("AGENTCTL_NOTIFY_TIMEOUT", NOTIFY_TIMEOUT))
    except subprocess.TimeoutExpired as e:
        return ch, _run_said(e), NOTIFY_FIX_TIMEOUT.format(ch=ch)
    except (OSError, subprocess.SubprocessError) as e:
        return ch, _run_said(e), fix
    return None if r.returncode == 0 else (ch, _proc_said(r), fix)


def notify(title, text, url=None, group=None):
    """A macOS notification, clickable (opens `url` in Chrome) through terminal-notifier; on ANY failure of it (absent,
    not allowed in System Settings, a timeout) the osascript fallback (no click). $AGENTCTL_NOTIFY: a command run with
    (title, text, url) instead (the tests'). Returns True only when one of them got through (exit 0); else False, the
    cause in notify_cause() and the owner's fix in notify_fix() -- the notifier records a delivery only on True
    (2026-11-03). Each channel is bounded by $AGENTCTL_NOTIFY_TIMEOUT (NOTIFY_TIMEOUT); none is retried."""
    cmd = os.environ.get("AGENTCTL_NOTIFY")
    if cmd:
        f = _channel(cmd, [cmd, title, text, url or ""], "fix the notification command ($AGENTCTL_NOTIFY)")
        return _delivered(f is None, [f] if f else ())
    if sys.platform != "darwin":
        return _delivered(False, [("none", "not macOS: no notification channel on this host", NOTIFY_FIX["none"])])
    failures = []
    tn = notifier_bin()
    if tn:
        f = _channel("terminal-notifier", notifier_argv(tn, title, text, url, group), NOTIFY_FIX["terminal-notifier"])
        if f is None:
            return _delivered(True)
        failures.append(f)
    f = _channel("osascript", ["osascript", "-e", f"display notification {applescript_str(text)} with title "
                               f"{applescript_str(title)} " 'sound name "Basso"'], NOTIFY_FIX["osascript"])
    if f is None:
        return _delivered(True)
    return _delivered(False, failures + [f])
