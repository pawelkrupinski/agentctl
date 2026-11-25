# agentctl, part `statusline`: the status line and the gate text: what the published summary says, its stale forms, the alarms
# A real module (lib/agentctl/__init__.py MODULES): its own imports, the standard library's and MODULES' only.

import os
import re
import time
from typing import Any
from agentctl.gatesummary import GATE_LINE, GATE_SUMMARY_MAX_AGE
from agentctl.queue import hold_text
from agentctl.storestate import NETWORK_SHOWN_FOR


# ---- ONE STATUS MODEL (docs/simplification-2026-10.md item 4): status_data() is THE assembled status (what /api/status
# returns); `status --line`, `status`, `red status`, `ci-why --summary --cached` and the page render from it (or from
# the sections they need: status_data(only=...)). No second formatter reads state.json or the store for itself.
def gate_summary_line(sm: Any, o_r: Any) -> Any:
    """(epoch, line) of a published gate summary: the line only when it reads as a gate line of o_r's repository."""
    try:
        epoch = int(sm["at"])
    except (KeyError, TypeError, ValueError):
        epoch = None
    line = sm.get("line") if epoch and GATE_LINE.match(sm.get("line") or "") and (not o_r or sm.get("repo") == o_r) else None
    return epoch, line


def status_gate_text(d, t, o_r=None, unreachable=False):
    """THE gate text, from the published summary in state.json d: the line itself while fresh; stale or missing, what
    it last said marked with its age -- `gate ?` when ci-runner's store does not answer (no verdict), `gate NETWORK`
    on a lossy uplink, else `gate UNREAD`. o_r: only that repository's line counts."""
    epoch, line = gate_summary_line(d.get("summary") or {}, o_r)
    if line and -5 <= t - epoch <= GATE_SUMMARY_MAX_AGE:
        return line
    return gate_stale_text(d, t, epoch, line, unreachable)


def gate_stale_text(d: Any, t: Any, epoch: Any, line: Any, unreachable: Any) -> Any:
    """`gate ?` / `gate NETWORK` / `gate UNREAD` with what the stale summary last said and when."""
    asof = time.strftime("%H:%M", time.localtime(epoch)) if epoch else "never"
    rest = line[5:] if line else None
    if unreachable:
        return f"gate ? (as of {asof}: {rest})" if rest else "gate ?"
    net = d.get("network") or {}
    if net.get("at") and t - float(net["at"]) < NETWORK_SHOWN_FOR:
        return f"gate NETWORK (lossy uplink: {int(float(net.get('loss') or 0) * 100)}% loss{gate_last_said(asof, rest)})"
    return f"gate UNREAD (as of {asof}: {rest or 'no fresh published gate'})"


def gate_last_said(asof: Any, rest: Any) -> Any:
    return f"; as of {asof}: {rest}" if rest else ""


def status_clock(x: Any) -> Any:
    return time.strftime('%H:%M', time.localtime(float(x)))


def store_unreachable_alarm(h: Any, t: Any) -> Any:
    """`STORE UNREACHABLE since HH:MM (url: why)`: no verdict on ci-runner's reconciler (never STALLED)."""
    se, tick = h.get("store_error") or {}, h["tick_at"]
    since = status_clock(float(h.get("fetched_at") or 0) or se.get("since") or t)
    return (f"STORE UNREACHABLE since {since} ({h['store_url']}" + (f": {se['why'][:80]}" if se.get("why") else "") + ")"
            + (f"; ci-runner ticked at {status_clock(tick)}" if tick else ""))


def status_line_alarm(h: Any, t: Any) -> Any:
    """`STORE UNREACHABLE since HH:MM` / `RECONCILER STALLED since HH:MM` / `RECONCILER not running`, or None."""
    tick = h["tick_at"]
    if h["store_unreachable"]:
        return store_unreachable_alarm(h, t)
    if tick and t - float(tick) <= 120:
        return None
    return ((f"RECONCILER STALLED since {status_clock(tick)}" if tick else "RECONCILER not running")
            + (" (ci-runner)" if h["store_url"] else ""))


def render_status_line(h, t):
    """The status line: the gate text, the waiting and checking changes; `RECONCILER STALLED since HH:MM` when the
    tick is over 2 min old, `STORE UNREACHABLE since HH:MM` when ci-runner's store has not answered for a minute."""
    short = lambda wts: ", ".join(re.sub(r"^.*-wt-", "", os.path.basename(w or "")) for w in wts)
    alarm = status_line_alarm(h, t)
    parts = [alarm, h["gate_text"]] if alarm else [h["gate_text"]]
    hold = h.get("hold")
    if hold and float(hold.get("until") or 0) > t:  # the store's land hold (expired ones lift at the next tick)
        parts.append("LAND " + hold_text(hold))
    if h.get("reverts"):  # a reverted change its lander has not taken back up (reverts.py)
        parts.append(f"REVERTED unacked: {len(h['reverts'])}")
    waiting, queued = h["waiting"], h["queued"]
    parts.append(f"waiting: {len(waiting)}" + (f" — {short(waiting)}" if waiting else "")
                 + (f" · checking: {len(queued)} — {short(queued)}" if queued else ""))
    return " · ".join(parts)
