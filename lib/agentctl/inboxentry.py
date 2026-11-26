# agentctl, part `inboxentry`: the inbox's entries: its directory, the kinds a verdict names, who wrote it, the condition kinds only open_condition writes, and inbox_write, which hands a built entry to the writer the composition root wired (the alert router).
# A real module (lib/agentctl/__init__.py MODULES): its own imports, the standard library's and MODULES' only.

from agentctl.ids import stamp
from agentctl.clock import epoch_now
import datetime as dt
import glob
import itertools
import os
import re
import sys
import time
from typing import Any, Callable
from agentctl.core import err, state_root
from agentctl.timefmt import iso_at
from agentctl.statefile import read_record, write_json
from agentctl.procs import safe_cwd
from agentctl.fixerrecords import agent_name
from agentctl.watchknobs import knob_inbox_dir
from agentctl.fixerknobs import knob_owners_dir


# ---------------------------------------------------------------- inbox
# The owner (2026-10-27): three agents ended their turns while their land/watch still ran, and the WATCH verdicts went
# nowhere. Every `land`, `watch`, `wait` and `waiting await` writes its verdict to an inbox when it ends -- normally, on
# a signal, or on a crash -- and land's WAITING line too (it may wait for hours): one JSON file per verdict under
# ~/.cache/agentctl/inbox/ ($AGENTCTL_INBOX_DIR; beside $AGENTCTL_OWNERS_DIR when only that is set). The orchestrating
# session reads it with `agentctl inbox` whether or not the agent that started the command is still alive. A restart
# on new code (os.execv) writes nothing: the resumed process writes the verdict. Only a SIGKILL leaves no entry (its
# owner record stays, and `agentctl orphans` names it).
INBOX_COMMANDS = {"land", "watch", "wait", "waiting await"}


def inbox_dir() -> str:
    if knob_inbox_dir():
        return knob_inbox_dir()
    if knob_owners_dir():
        return os.path.join(os.path.dirname(os.path.abspath(knob_owners_dir())), "inbox")
    return state_root("inbox")


def caller_owner(cwd: str | None = None) -> str:
    """Who is calling, as a record's `owner`: the Claude session, else `<user>@<cwd>` (the one copy: Owned's record and
    every inbox entry)."""
    return os.environ.get("CLAUDE_CODE_SESSION_ID") or f"{os.environ.get('USER', '?')}@{cwd or safe_cwd()}"


INBOX_KINDS = [("REVERTED", r"REVERTED: "), ("REVERT_NOTICE", r"REVERT (NOTICE|UNACKED): "),  # reverts.py: whole revert messages
               ("INVARIANT", r"INVARIANT "), ("CLOSED", r"CLOSED"), ("UNLANDED_ORPHAN", r"UNLANDED ORPHAN:"), ("RECOVERED", r"RECOVERED:"), ("RELANDING", r"RELANDING:"), ("NETWORK", r"NETWORK:"), ("PARKED", r"PARKED DIGEST:|UNPARKED "), ("ORPHAN_FIXER", r"ORPHAN FIXER DISPATCHED"), ("ORPHAN_SUBMITTED", r"ORPHAN SUBMITTED:"),  # first: they quote others
               ("LANDER_REVIVED", r"LANDER REVIVED"), ("LAUNCH_FAILED", r"LAUNCH FAILED"),
               ("BRANCH_RED", r"BRANCH RED"), ("RED_UNCLAIMED", r"RED UNCLAIMED"), ("RED_BUDGET", r"RED BUDGET"), ("RED_AUTHOR", r"RED AUTHOR"), ("RED_ESCALATE", r"RED ESCALATE"), ("BLOCKED", r"RED BLOCKED ON OWNER"),
               ("RED_DISPATCHED", r"RED DISPATCHED"), ("RED_REVERT", r"RED REVERT"), ("RED_BLAME", r"RED BLAME"), ("RED_RELAND", r"RED RELAND"), ("RED_FLAKE", r"RED FLAKE"), ("FIXER_LANDED", r"RED FIXER [^:]*: LANDED "), ("DIAGNOSED", r"RED FIXER [^:]*: DIAGNOSED:"),
               ("FIXER_FAILED", r"RED FIXER "), ("RED_CLEARED", r"RED CLEARED"), ("FIX_FAILED", r"FIX FAILED"), ("FIX_NOT_COUNTED", r"FIX NOT COUNTED"), ("FIX_OK", r"FIX OK"), ("FIX_UNVERIFIED", r"FIX UNVERIFIED"),
               ("RED", r"WATCH RED"), ("OK", r"WATCH OK"), ("STALLED", r"WATCH STALLED|STALLED:|.*\bstalled\b"),
               # a crash, a signal or an exit with no verdict: whatever it watched is unknown now
               # an agent's command killed (a reap, a restart) or misused: its own news, never an unknown state
               ("ENDED", r"ended by signal"), ("USAGE", r"exited: .*needs numeric|exited: usage:"),
               ("RED_STARVED", r"RED STARVED"),
               ("UNKNOWN", r"WATCH UNKNOWN|UNKNOWN:|crashed:|exited:|ended by signal|exit \d+ with no verdict"),
               ("NOT_LANDED", r"NOT LANDED"), ("PRECHECK_REFUSED", r"PRECHECK REFUSED "), ("SUPERSEDED", r"SUPERSEDED "), ("LANDED", r"LANDED "), ("WAITING", r"WAITING "), ("QUEUED", r"QUEUED "), ("HELD", r"HELD:"),
               ("CHECK", r"CHECK CAP")]


def inbox_kind(verdict: Any, final: bool = True) -> str:
    """An entry's kind, from its verdict line: what `inbox wait --kinds` selects on."""
    v = verdict or ""
    if not final:
        return "QUEUED" if v.startswith("QUEUED ") else "WAITING"
    for kind, pat in INBOX_KINDS:
        if re.match(pat, v):
            return kind
    return "OTHER"


# THE INBOX WRITER SEAM: an entry built here is routed and merged by the alert model (alerts.py alert_write, which reads
# the store, the reds and their owners: far above this module), wired at the composition root (cli). Unwired -- a
# module loaded on its own -- the entry is written as it is.
_inbox_writers: list[Callable[[dict[str, Any]], Any]] = []


# The time an entry is stamped with is the process's clock, wired beside the writer (the wall's until wired).
_inbox_times: list[Callable[[], float]] = [epoch_now]
_inbox_seq = itertools.count()  # a clock that stands still (a test's) must not give two entries one id


def wire_inbox_writer(writer: Callable[[dict[str, Any]], Any], now: Callable[[], float] = epoch_now) -> None:
    _inbox_writers[:] = [writer]
    _inbox_times[:] = [now]


def write_inbox_entry(rec: dict[str, Any]) -> Any:
    """rec into the inbox: through the wired writer, else as its own file; the id of the entry that carries it."""
    if _inbox_writers:
        return _inbox_writers[0](rec)
    d = inbox_dir()
    os.makedirs(d, exist_ok=True)
    write_json(os.path.join(d, rec["id"] + ".json"), rec)
    return rec["id"]


def claude_pid_env() -> int | None:
    """$CLAUDE_PID as a number, or None when unset or not one."""
    pid = os.environ.get("CLAUDE_PID") or ""
    return int(pid) if pid.isdigit() else None


def inbox_write(command: str, verdict: Any, verdicts: Any, tail: Any, exit_code: Any, final: bool = True,
                cwd: str | None = None, reply: bool = False, item: Any = None) -> Any:
    """One inbox entry, written atomically; returns its id (None when the inbox cannot be written: never fatal).
    A CONDITION_KINDS entry only from open_condition (ConditionBypass otherwise: it would have no probe to clear it);
    reply=True: a command's own verdict to the agent that ran it (with_inbox), not a standing condition. item: the alert
    model's item (alerts.py; derived from the verdict when None). Routed and merged by the wired writer (alerts'
    alert_write): the id returned is the entry that carries it (its item's live alert when one is unread)."""
    if final and not reply and not _OPENING[0] and inbox_kind(verdict, final) in CONDITION_KINDS:
        raise ConditionBypass(f"a {inbox_kind(verdict, final)} inbox entry outside open_condition: {str(verdict)[:160]}")
    now = dt.datetime.fromtimestamp(_inbox_times[0](), dt.timezone.utc)
    eid = (f"{stamp('event', now.timestamp())}-{os.getpid()}-{next(_inbox_seq):06d}-{re.sub(r'[^a-z]+', '-', command)}"
           + ("" if final else "-waiting"))
    here = safe_cwd()
    rec = {"id": eid, "time": iso_at(now.timestamp()), "at": now.timestamp(), "command": command, "argv": sys.argv[1:],
           "cwd": cwd or here, "pid": os.getpid(), "final": final, "exit": exit_code, "verdict": verdict,
           "kind": inbox_kind(verdict, final),
           "owner": caller_owner(here),
           "claude_pid": claude_pid_env(),
           "agent": agent_name(), "verdicts": verdicts, "tail": tail, "reply": bool(reply)}
    if item:
        rec["alert_item"] = item
    try:
        return write_inbox_entry(rec)
    except OSError as e:
        err(f"could not write the inbox entry ({e})")
        return None


def inbox_entries() -> list[dict[str, Any]]:
    d = inbox_dir()
    acked_dir = os.path.join(d, "acked")
    acked = set(os.listdir(acked_dir)) if os.path.isdir(acked_dir) else set()
    out = []
    for p in glob.glob(os.path.join(d, "*.json")):
        rec = read_record(p)
        if rec is not None and rec.get("id"):
            rec["read"] = rec["id"] in acked
            out.append(rec)
    return sorted(out, key=lambda r: (r.get("at") or 0, r["id"]))


# ---------------------------------------------------------------- recovery: every non-green condition clears itself
# The owner (2026-10-28): "make this self-recover", "i want a general solution. recovery mechanism", "the way it got
# red / blocked i want it to go green as well", "structural". ONE registry, one JSON per open CONDITION in
# recovery_dir(). Every non-green state the status line or the inbox shows -- an escalation-kind inbox entry
# (CONDITION_KINDS: RED UNCLAIMED / BUDGET / ESCALATE, RED BLOCKED ON OWNER, STALLED, UNKNOWN, NETWORK), an owner block
# (`red block`), the gate UNREAD, an infra NOT LANDED -- is opened by open_condition(kind, text, probe, on_recover) and
# ONLY there: inbox_write refuses those kinds anywhere else (ConditionBypass), a blocked claim cannot be written outside
# it, and test_recovery's lint fails on a bypass in the source.
# THE RULE: the probe is the check that found the problem -- a FAILED ProbeResult, which only run_probe makes -- so the
# check that turned it red is the check that turns it green. The duties thread re-runs each open condition's probe
# (recovery_tick; per-condition backoff; never `secrets`). When it passes the condition closes itself: its inbox entries
# are acked, the gate is marked dirty, the status line refreshes, its on_recover actions run (release a block and
# dispatch the red's workflow to verify it, kick the tunnel, land a change again), and one RECOVERED note is written.
# Nobody acks a solved problem. `agentctl recover status` lists the open conditions.
CONDITION_KINDS = ("INVARIANT", "UNLANDED_ORPHAN", "STALLED", "UNKNOWN", "NETWORK")


_OPENING = [0]  # > 0 inside open_condition: the one place a condition-kind inbox entry or a block may be written


class ConditionBypass(RuntimeError):
    """A non-green condition written without open_condition (and so without a probe that can clear it): a bug."""
