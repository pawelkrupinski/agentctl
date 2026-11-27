# agentctl, part `inboxentry`: the inbox entry: its directory, the kinds a verdict names, who wrote it, and inbox_write, which hands a built entry to the writer the composition root wired (the alert router).
# A real module (lib/agentctl/__init__.py MODULES): its own imports, the standard library's and MODULES' only.

from agentctl.filelog import note_event
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
from agentctl.statefile import read_record
from agentctl.procs import safe_cwd
from agentctl.fixerrecords import agent_name
from agentctl.watchknobs import knob_inbox_dir
from agentctl.fixerknobs import knob_owners_dir
from agentctl.core import claude_session_id


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
    return claude_session_id() or f"{os.environ.get('USER', '?')}@{cwd or safe_cwd()}"


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


def claude_pid_env() -> int | None:
    """$CLAUDE_PID as a number, or None when unset or not one."""
    pid = os.environ.get("CLAUDE_PID") or ""
    return int(pid) if pid.isdigit() else None


_inbox_seq = itertools.count()  # a clock that stands still (a test's) must not give two entries one id


class InboxWriter:
    """THE INBOX WRITER: an entry built here is routed and merged by the alert model (`route`: alerts.py alert_write,
    which reads the store, the reds and their owners -- far above this module) and stamped with `now`, the process's
    clock. cli, the composition root, builds the one a process uses: the process's Context carries it (`ctx.inbox`), and
    wire_inbox_writer makes it the one inbox_write reaches for the writers no context reaches yet. Unrouted -- a module
    loaded on its own -- nothing is written (review 2026-11-26: it was written as a plain file, a second write path with
    no routing and no merge): a RuntimeError says the root was skipped."""

    def __init__(self, route: Callable[[dict[str, Any]], Any] | None, now: Callable[[], float]) -> None:
        self.route, self.now = route, now

    def deliver(self, rec: dict[str, Any]) -> Any:
        """rec into the inbox through the route: the id of the entry that carries it (its opening an event of the
        offline log, filelog.py). RuntimeError when there is no route (the composition root, cli, was skipped)."""
        if self.route is None:
            raise RuntimeError(f"no inbox writer wired (cli.wire_collaborators): entry {rec['id']} not written")
        got = self.route(rec)
        note_event("inbox", str(got or rec["id"]), None, "unread", "notified", at=rec.get("at"), detail=rec.get("kind"),
                   text=str(rec.get("verdict") or "")[:160], key="opened")
        return got

    def write(self, command: str, verdict: Any, verdicts: Any, tail: Any, exit_code: Any, final: bool = True,
              cwd: str | None = None, reply: bool = False, item: Any = None) -> Any:
        """One inbox entry, written atomically; returns its id (None when the inbox cannot be written: never fatal).
        A CONDITION_KINDS entry only from open_condition (conditions.py: the route refuses it otherwise);
        reply=True: a command's own verdict to the agent that ran it (with_inbox), not a standing condition. item: the
        alert model's item (alerts.py; derived from the verdict when None). Routed and merged by the route (alerts'
        alert_write): the id returned is the entry that carries it (its item's live alert when one is unread)."""
        now = dt.datetime.fromtimestamp(self.now(), dt.timezone.utc)
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
            return self.deliver(rec)
        except OSError as e:
            err(f"could not write the inbox entry ({e})")
            return None


# The process's inbox writer, for a writer no context reaches: unrouted, stamped by the process's clock, until wired.
_process_inbox: list[InboxWriter] = [InboxWriter(None, epoch_now)]


def wire_inbox_writer(inbox: InboxWriter) -> None:
    _process_inbox[:] = [inbox]


def write_inbox_entry(rec: dict[str, Any]) -> Any:
    """rec into the inbox through the process's writer (InboxWriter.deliver)."""
    return _process_inbox[0].deliver(rec)


def inbox_write(command: str, verdict: Any, verdicts: Any, tail: Any, exit_code: Any, final: bool = True,
                cwd: str | None = None, reply: bool = False, item: Any = None) -> Any:
    """One inbox entry through the process's writer (InboxWriter.write)."""
    return _process_inbox[0].write(command, verdict, verdicts, tail, exit_code, final=final, cwd=cwd, reply=reply,
                                   item=item)


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


