# agentctl, part `inbox`: the inbox.
# A real module (lib/agentctl/__init__.py MODULES): its own imports, the standard library's and MODULES' only.

import re
import sys
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


def with_inbox(command, handler, a):
    """Run handler(a) with its stdout teed; whatever way it ends, write its verdict to the inbox."""
    waiting_line = []

    def on_line(line):
        if line.startswith(("WAITING ", "QUEUED ")):
            inbox_write(command, line, [line], tee.tail, None, final=False)
            waiting_line.append(line)
    tee = VerdictTee(sys.stdout, on_line)
    errs = VerdictTee(sys.stderr)  # its last lines name a cause when stdout has no verdict
    sys.stdout, sys.stderr, code, crash = tee, errs, 1, None
    try:
        code = handler(a)
        return code
    except SystemExit as e:
        code = e.code if isinstance(e.code, int) else (0 if e.code is None else 1)
        if code >= 128:
            crash = f"ended by signal {code - 128}"
        elif e.code is not None and not isinstance(e.code, int):  # SystemExit("agentctl: ..."): the message is the cause
            crash = f"exited: {str(e.code).replace('agentctl: ', '')}"
        raise
    except BaseException as e:
        crash = f"crashed: {type(e).__name__}: {e}"
        raise
    finally:
        sys.stdout, sys.stderr = tee.stream, errs.stream
        tee.finish()
        errs.finish()
        last = next((l for l in reversed(tee.tail) if l.strip()), None)
        last_err = next((l.replace("agentctl: ", "") for l in reversed(errs.tail) if l.strip()), None)
        if tee.verdicts:
            verdict = crash or tee.verdicts[-1]
        elif crash:
            verdict = crash
        elif code:  # never a bare exit: what it said last, on stderr first
            verdict = f"exit {code} with no verdict line; it said last: {last_err or last or 'nothing'}"
        else:
            verdict = last or last_err or "exit 0 with no output"
        if not (crash is None and code == 0 and waiting_line and verdict == waiting_line[-1]):  # land ended AT WAITING:
            inbox_write(command, verdict, tee.verdicts, tee.tail, code if crash is None else (code or 1),  # written once
                        cwd=getattr(a, "inbox_cwd", None), reply=True)


def entry_line(e):
    where = tilde(e.get("cwd") or "")
    state = f"exit {e.get('exit')}" if e.get("final", True) else "still running"
    return f"{e['id']} {'read  ' if e['read'] else 'UNREAD'} {e.get('command')} {where} {state}: {e.get('verdict')}"


