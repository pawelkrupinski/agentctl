# agentctl, part `gatesummary`: the published gate line: written by the tick, read back while fresh
# A real module (lib/agentctl/__init__.py MODULES): its own imports, the standard library's and MODULES' only.

import re
import time
from agentctl.cistate import state_read, state_update


SUMMARY_MAX_REDS = 3


GATE_SUMMARY_MAX_AGE = 180  # s: a published gate line older than this is never shown as the gate


GATE_LINE = re.compile(r"gate (RED|green)\b")


def publish_gate_summary(o_r, line, epoch=None):
    """The snapshot's gate line: {at, repo, line}. Only a read gate line is ever published (never UNREAD)."""
    if not GATE_LINE.match(line):
        return
    state_update(summary={"at": int(epoch or time.time()), "repo": o_r, "line": line})


def published_summary():
    """(epoch, repo, line) the processor last published, or (None, None, None)."""
    sm = state_read().get("summary") or {}
    try:
        return int(sm["at"]), sm.get("repo"), sm.get("line")
    except (KeyError, TypeError, ValueError):
        return None, None, None


def cached_gate_summary(o_r, now=None):
    """(line, published_epoch). line is None for a missing, malformed, other-repo, future-dated or too-old line."""
    epoch, repo_, line = published_summary()
    if epoch is None:
        return None, None
    age = (now or time.time()) - epoch
    if repo_ != o_r or not GATE_LINE.match(line) or age < -5 or age > GATE_SUMMARY_MAX_AGE:
        return None, epoch
    return line, epoch


_last_gate_publish = [0.0]
