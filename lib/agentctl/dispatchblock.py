# agentctl, part `dispatchblock`: what blocks a fixer's dispatch here: no claude CLI, an interactive session, a fixer or land in this cwd
# A real module (lib/agentctl/__init__.py MODULES): its own imports, the standard library's and MODULES' only.

from agentctl.clock import epoch_now
import os
import re
import subprocess
from agentctl.procrunner import process_runner
from agentctl.core import HOME, process_cache, reds_dir
from agentctl.statefile import read_record
from agentctl.procs import lsof_cwds, ps
from agentctl.fixerrecords import own_fixer_record_path
from agentctl.worktrees import worktree_of
from agentctl.fixerknobs import knob_claude, knob_claude_settings
from agentctl.redknobs import knob_red_fixer


def in_fixer():
    """True inside a dispatched fixer (its session's own record), or a process agentctl spawned with a fixer's
    environment and no Claude session of its own."""
    if own_fixer_record_path():
        return True
    return not os.environ.get("CLAUDE_CODE_SESSION_ID") and bool(knob_red_fixer())


def land_cwds():
    """{worktree: pid} of every running `agentctl land`."""
    out = {}
    for pid, _pp, _e, cmd in ps():
        if re.search(r"agentctl land\b", cmd) and pid != os.getpid():
            cwd = lsof_cwds([pid]).get(pid)
            wt = worktree_of(cwd) if cwd else None
            if wt:
                out[os.path.realpath(wt)] = pid
    return out


_blocker_cache = process_cache({})


def cached_dispatch_blocker(ttl=60):
    """dispatch_blocker runs `claude --help`: once a minute per process is enough for a display."""
    key = (reds_dir(), os.path.exists(reds_dir("DISPATCH_OFF")), knob_claude(),
           knob_claude_settings())
    hit = _blocker_cache.get(key)
    if hit and epoch_now() - hit[0] < ttl:
        return hit[1]
    v = dispatch_blocker()
    _blocker_cache[key] = (epoch_now(), v)
    return v


def short_flow(name):
    return "main" if name == "Main" else "convergence" if "convergence" in name.lower() else name.lower()


def dispatch_blocker():
    """None when a headless fixer may be dispatched, else what stops it (and what the owner must run)."""
    if os.path.exists(reds_dir("DISPATCH_OFF")):
        return "dispatch is OFF (`~/bin/agentctl red auto on` turns it on)"
    cli = claude_cli()
    if not cli:
        return "no `claude` CLI found ($AGENTCTL_CLAUDE, PATH, ~/.local/bin/claude)"
    try:
        r = process_runner().run([cli, "--help"], capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.TimeoutExpired) as e:
        return f"`{cli} --help` failed ({e})"
    if r.returncode != 0 or "--permission-mode" not in r.stdout or "--bg" not in r.stdout:
        return f"`{cli} --help` did not offer --bg and --permission-mode (exit {r.returncode})"
    mode = interactive_mode()
    if mode != "auto":
        return (f"interactive sessions run in permission mode {mode!r}, and a headless fixer gets no wider: it needs "
                "\"auto\". The owner enables it by setting permissions.defaultMode to \"auto\" in ~/.claude/settings.json")
    return None


def claude_cli():
    if knob_claude():  # set: that one only (the tests' fake)
        return knob_claude() if os.access(knob_claude(), os.X_OK) else None
    for c in (shutil_which("claude"), os.path.join(HOME, ".local", "bin", "claude")):
        if c and os.access(c, os.X_OK):
            return c
    return None


def shutil_which(name):
    import shutil
    return shutil.which(name)


def interactive_mode():
    """The permission mode interactive sessions start in (~/.claude/settings.json's permissions.defaultMode)."""
    p = knob_claude_settings() or os.path.join(HOME, ".claude", "settings.json")
    rec = read_record(p) or {}
    return (rec.get("permissions") or {}).get("defaultMode") or "default"
