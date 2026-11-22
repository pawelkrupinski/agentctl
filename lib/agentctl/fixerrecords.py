# agentctl, part `fixerrecords`: the dispatched fixers' records: each fixer's record and session, this process's own record, and its agent name.
# A real module (lib/agentctl/__init__.py MODULES): its own imports, the standard library's and MODULES' only.

import glob
import os
import re
from agentctl.core import reds_dir
from agentctl.statefile import read_record
from agentctl.procs import proc_alive
import time
from typing import Any
from agentctl.statefile import mtime


def fixer_name(rec: dict[str, Any]) -> str:
    """A dispatched fixer's name, from its record: 'red-fixer-<run>' (its first), 'red-fixer-<run>-<n>' (its n-th)."""
    stem = os.path.basename(str(rec.get("path") or "")).rsplit(".", 1)[0]
    n = stem[len("fixer-"):] if stem.startswith("fixer-") else ""
    return f"red-fixer-{rec.get('run')}" + (f"-{n}" if n else "")


def agent_name() -> str | None:
    """This process's agent name: a dispatched fixer's, from ITS OWN fixer record (own_fixer_record_path: the session
    decides); $AGENTCTL_AGENT_NAME only in a process with no Claude session (one agentctl spawned itself, e.g. the
    wrapper's or a revert's land). Never an inherited environment: background sessions inherit the claude daemon's,
    the wrapper's that spawned it (2026-10-29: every background fixer, and the owner's sessions, released reds as
    'red-fixer-2473'). None otherwise."""
    own = own_fixer_record_path()
    if own:
        rec = read_record(own) or {}
        if rec.get("run") is not None:
            return fixer_name({**rec, "path": own})
    if not os.environ.get("CLAUDE_CODE_SESSION_ID"):
        return os.environ.get("AGENTCTL_AGENT_NAME") or None
    return None


def fixer_records(max_age: float | None = None, now_: float | None = None) -> list[dict[str, Any]]:
    """Every dispatched fixer's record, with its path and whether its process is alive; max_age: only the records
    written in the last max_age s before now_ (an old record is never opened, nor its pid checked)."""
    out = []
    for p in glob.glob(reds_dir("*", "fixer*.json")):
        if max_age is not None and (now_ if now_ is not None else time.time()) - mtime(p) > max_age:
            continue
        r = read_record(p)
        if r:
            r["path"] = p
            r["alive"] = not r.get("ended") and proc_alive(r.get("pid"), r.get("process_start"))
            out.append(r)
    return out


def own_fixer_record_path() -> str | None:
    """This session's own fixer record, or None. The session id decides -- its binding (bind_fixer_session), else a
    record naming it; with a session, the environment never does: a `claude --bg` session can start in a spare
    process spawned with ANOTHER fixer's environment (2026-10-29: #2426's fixer carried #2432's AGENTCTL_FIXER_RECORD
    and wrote its notes onto #2432's lease), so the environment's record counts only when its session is this one, or
    when no record names this session (a record not yet told its session id)."""
    sid = os.environ.get("CLAUDE_CODE_SESSION_ID")
    env = os.environ.get("AGENTCTL_FIXER_RECORD")
    if sid:
        bound = (read_record(fixer_session_path(sid)) or {}).get("record")
        rec = read_record(bound) if bound else None
        if rec and not rec.get("ended") and record_session_is(rec, sid, unset=True):
            return bound
        mine = [r for r in fixer_records() if r.get("session") == sid and not r.get("ended")]
        if mine:
            return str(max(mine, key=lambda r: r.get("started") or "")["path"])
        return None  # a session no record or binding names is no fixer, whatever its environment says
    return env or None


def fixer_session_path(sid: str) -> str:
    return reds_dir("sessions", f"{re.sub(r'[^A-Za-z0-9_.-]', '_', sid)}.json")


def record_session_is(rec: dict[str, Any], sid: str, unset: bool = False) -> bool:
    """Fixer record rec is session sid's: it names sid, or (its wrapper's first poll not yet done) the bg session it
    launched, whose id sid begins with (issue 20261119T025305-21375d); unset: a record naming no session yet too."""
    have = rec.get("session")
    return have == sid or (unset and have is None) or \
        bool(sid and rec.get("bg_id") and not rec.get("transcript") and str(sid).startswith(str(rec["bg_id"])))


def fixer_keys(r: dict[str, Any]) -> set[str]:
    """The red keys a fixer record holds or held: its own red's, its lease's and its run's other reds' leases'."""
    names = [r.get("lease")] + [x.get("name") for x in r.get("extra_leases") or []]
    keys = {n.split("/", 1)[1] for n in names if n and n.startswith("red/")}
    return keys | ({r["key"]} if r.get("key") else set())
