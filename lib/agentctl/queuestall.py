# agentctl, part `queuestall`: the queue's dead-man switch -- open changes and no change moving names its blocker
# A real module (lib/agentctl/__init__.py MODULES): its own imports, the standard library's and MODULES' only.
#
# THE 41-HOUR STALL (2026-11-23 07:45 to 11-25 02:11, the transition analysis' sink 1): no change moved at all while 4+
# changes sat 14-19 h "queued ... held" behind Main #2877, and no condition named it (`waiting-rounds` opened and closed
# 25 times in 28 days, never for this). Now the server's tick looks every round: changes are open and NO change item
# moved (entered a state: a reminder does not count) for knobs.QUEUE_STALL_AFTER s (60 min: the owner, 2026-11-26) ($AGENTCTL_QUEUE_STALL_AFTER) opens ONE condition, `QUEUE STALLED`,
# whose text names the head blocker -- the oldest open change, its state and age, what holds it, and the open gate red
# -- so the owner is asked once and the stall's time is attributable. Its probe (queue_moving) closes it when a change
# moves again or none is open. An alert, never a hold: it blocks nothing.

from typing import Any, Optional

from agentctl.statemachines import machine_state
from agentctl.conditions import open_condition, run_probe
from agentctl.core import fmt_age
from agentctl.queueknobs import QUEUE_STALL_AFTER, knob_queue_stall_after

def _open_changes(changes: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [c for c in changes if not machine_state("change", c["state"]).terminal]


def head_blocker(changes: list[dict[str, Any]], reds: list[dict[str, Any]], now: float) -> str:
    """The blocker at the head of the queue, in words: the oldest open change (its state, age, hold) and the gate red."""
    oldest = min(_open_changes(changes), key=lambda c: float(c.get("entered_at", now)))
    return (f"{oldest['id']} {oldest['state']} for {fmt_age(now - float(oldest.get('entered_at', now)))}"
            f"{_held_words(oldest)}{_gate_words(reds)}")


def _held_words(change: dict[str, Any]) -> str:
    hold = (change.get("data") or {}).get("hold") or {}
    if not hold:
        return ""
    return f", held: {hold.get('why')}" + (f" (by {hold['blocker']})" if hold.get("blocker") else "")


def _gate_words(reds: list[dict[str, Any]]) -> str:
    gate = [r for r in reds if (r.get("data") or {}).get("gate") and not machine_state("red", r["state"]).terminal]
    return "; the gate is red: " + ", ".join(_red_words(r) for r in gate[:3]) if gate else ""


def _red_words(r: dict[str, Any]) -> str:
    d = r.get("data") or {}
    return f"{d.get('workflow') or '?'} #{(d.get('last_run') or {}).get('number')} ({r['id']}, {r['state']})"


def queue_stall(changes: list[dict[str, Any]], reds: list[dict[str, Any]], now: float,
                after: float = QUEUE_STALL_AFTER) -> Optional[str]:
    """The stall's text when changes are open and none moved for `after` s, else None."""
    if not _open_changes(changes):
        return None
    last = max(float(c.get("entered_at") or 0) for c in changes)
    if now - last < after:
        return None
    return (f"QUEUE STALLED: {len(_open_changes(changes))} change(s) open and none moved for {fmt_age(now - last)}; "
            f"the head blocker: {head_blocker(changes, reds, now)}")


def probe_queue_moving(store: Any, now: float) -> tuple[bool, str]:
    """The queue is moving: no change is open, or one moved within the bound."""
    stall = queue_stall(store.items("change"), store.items("red"), now, knob_queue_stall_after())
    return stall is None, stall or "a change moved (or none is open)"


def queue_stall_step(store: Any) -> list[str]:
    """The tick's look: the condition opened (or refreshed) while the queue is stalled; its line, if any. An alert
    only: it holds and rejects nothing."""
    # policy: queue-stall
    probe = run_probe("queue_moving", {})
    if probe.ok is not False:
        return []
    open_condition("STALLED", probe.detail, probe, [], key="queue-stall", command="land", store=store)
    return [probe.detail]
