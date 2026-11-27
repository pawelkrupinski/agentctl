# agentctl, part `inbox`: the verdict-capturing wrapper of the commands whose verdict goes to the inbox (VerdictTee, InboxRun, with_inbox), `agentctl inbox` and an entry's line
# A real module (lib/agentctl/__init__.py MODULES): its own imports, the standard library's and MODULES' only.

import re
import sys
from typing import Any, Callable, List, Optional
from agentctl.core import tilde
from agentctl.inboxentry import INBOX_COMMANDS, inbox_write


VERDICT_LINE = re.compile(r"^(LANDED \S+|NOT LANDED|WAITING \S+|QUEUED \S+|WATCH (OK|RED|UNKNOWN|STALLED)\b|run \S+#\d+: |file \S+: )")
INBOX_TAIL = 15


class VerdictTee:
    """stdout, with every line also kept: the verdict lines, and the last INBOX_TAIL lines for context."""

    def __init__(self, stream, on_line=None):
        self.stream, self.on_line, self.partial = stream, on_line, ""
        self.verdicts, self.tail = [], []

    def write(self, s):
        n = self.stream.write(s)
        self.partial += s
        while "\n" in self.partial:
            line, self.partial = self.partial.split("\n", 1)
            self._line(line)
        return n

    def _line(self, line):
        self.tail = (self.tail + [line])[-INBOX_TAIL:]
        if VERDICT_LINE.match(line):
            self.verdicts.append(line)
            if self.on_line:
                self.on_line(line)

    def finish(self):
        if self.partial:
            self._line(self.partial)
            self.partial = ""

    def flush(self):
        return self.stream.flush()

    def __getattr__(self, name):
        return getattr(self.stream, name)


WAIT_KINDS_DEFAULT = "INVARIANT,NETWORK,RED_STARVED,RED,BRANCH_RED,FIX_FAILED,FIX_NOT_COUNTED,NOT_LANDED,PRECHECK_REFUSED,STALLED,UNKNOWN,RED_UNCLAIMED,RED_BUDGET,RED_AUTHOR,RED_ESCALATE,BLOCKED,FIXER_FAILED,DIAGNOSED,UNLANDED_ORPHAN,ORPHAN_SUBMITTED,RED_REVERT,LAUNCH_FAILED,REVERTED,REVERT_NOTICE"


def inbox_command(a):
    """The inbox's name for a parsed command, or None when it writes no entry. `land status|hold|release|cancel` write
    none: a queue subcommand answers its caller there and then, and a monitor polling `land status` wrote 2,692 entries
    in three days, acked by hand (2026-11-16). Only a verdict that may outlive the agent that started it goes there."""
    if a.cmd == "land" and getattr(a, "lcmd", None):
        return None
    name = a.cmd + (f" {a.wcmd}" if a.cmd == "waiting" else "")
    return name if name in INBOX_COMMANDS else None


def system_exit_code(e: SystemExit) -> int:
    """A SystemExit's exit code as a number: None is 0, a message is 1."""
    return e.code if isinstance(e.code, int) else (0 if e.code is None else 1)


def exit_crash(e: SystemExit) -> Optional[str]:
    """How a SystemExit ended the command, when it did not end it cleanly: a signal (code 128+n), or a message
    (SystemExit("agentctl: ..."): the message is the cause); else None."""
    code = system_exit_code(e)
    if code >= 128:
        return f"ended by signal {code - 128}"
    if e.code is not None and not isinstance(e.code, int):
        return f"exited: {str(e.code).replace('agentctl: ', '')}"
    return None


def last_line(lines: List[str]) -> Optional[str]:
    """The last non-blank of `lines`, or None."""
    return next((l for l in reversed(lines) if l.strip()), None)


class InboxRun:
    """One command run with its stdout and stderr teed: its verdict, however it ended, is written to the inbox once."""

    def __init__(self, command: str, a: Any) -> None:
        self.command, self.a = command, a
        self.waiting_line: List[str] = []
        self.tee = VerdictTee(sys.stdout, self.on_line)
        self.errs = VerdictTee(sys.stderr)  # its last lines name a cause when stdout has no verdict
        self.code: Any = 1  # a crash that is no SystemExit leaves it: the command ended 1
        self.crash: Optional[str] = None

    def on_line(self, line: str) -> None:
        if line.startswith(("WAITING ", "QUEUED ")):
            inbox_write(self.command, line, [line], self.tee.tail, None, final=False)
            self.waiting_line.append(line)

    def run(self, handler: Callable[[Any], Any]) -> Any:
        sys.stdout, sys.stderr = self.tee, self.errs
        try:
            self.code = handler(self.a)
            return self.code
        except SystemExit as e:
            self.code, self.crash = system_exit_code(e), exit_crash(e)
            raise
        except BaseException as e:
            self.crash = f"crashed: {type(e).__name__}: {e}"
            raise
        finally:
            self.finish()

    def verdict(self) -> str:
        """The inbox verdict: the crash, else the last verdict line, else what it said (unstated)."""
        if self.tee.verdicts:
            return self.crash or self.tee.verdicts[-1]
        return self.crash or self.unstated()

    def unstated(self) -> str:
        """A verdict for a run that printed no verdict line: never a bare exit, what it said last, on stderr first."""
        last = last_line(self.tee.tail)
        last_err = (last_line(self.errs.tail) or "").replace("agentctl: ", "") or None
        if self.code:  # (a run with a crash never gets here: its verdict is the crash)
            return f"exit {self.code} with no verdict line; it said last: {last_err or last or 'nothing'}"
        return last or last_err or "exit 0 with no output"

    def finish(self) -> None:
        sys.stdout, sys.stderr = self.tee.stream, self.errs.stream
        self.tee.finish()
        self.errs.finish()
        verdict = self.verdict()
        if not (self.code == 0 and self.waiting_line and verdict == self.waiting_line[-1]):
            # land ended AT WAITING: its entry is written once
            inbox_write(self.command, verdict, self.tee.verdicts, self.tee.tail, self.code,
                        cwd=getattr(self.a, "inbox_cwd", None), reply=True)


def with_inbox(command: str, handler: Callable[[Any], Any], a: Any) -> Any:
    """Run handler(a) with its stdout teed; whatever way it ends, write its verdict to the inbox."""
    return InboxRun(command, a).run(handler)


def entry_line(e):
    where = tilde(e.get("cwd") or "")
    state = f"exit {e.get('exit')}" if e.get("final", True) else "still running"
    return f"{e['id']} {'read  ' if e['read'] else 'UNREAD'} {e.get('command')} {where} {state}: {e.get('verdict')}"


