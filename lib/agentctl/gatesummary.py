# agentctl, part `gatesummary`: the published gate line, written by the tick (the status line reads it back while fresh)
# A real module (lib/agentctl/__init__.py MODULES): its own imports, the standard library's and MODULES' only.

from agentctl.clock import epoch_now
import re
from agentctl.cistate import state_update


SUMMARY_MAX_REDS = 3


GATE_SUMMARY_MAX_AGE = 180  # s: a published gate line older than this is never shown as the gate


GATE_LINE = re.compile(r"gate (RED|green)\b")


def publish_gate_summary(o_r, line, epoch=None):
    """The snapshot's gate line: {at, repo, line}. Only a read gate line is ever published (never UNREAD)."""
    if not GATE_LINE.match(line):
        return
    state_update(summary={"at": int(epoch or epoch_now()), "repo": o_r, "line": line})


_last_gate_publish = [0.0]
