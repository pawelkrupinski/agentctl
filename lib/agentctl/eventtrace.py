# agentctl, part `eventtrace`: an item's events read as one trace: the backfill merged in by provenance, walked into its stays (segments) and path
# A real module (lib/agentctl/__init__.py MODULES): its own imports, the standard library's and MODULES' only.

import re
from dataclasses import dataclass, field
from typing import Any, Iterable, Optional
from agentctl.statemachines import is_terminal


def ends_item(item_type: str, state: str) -> bool:
    """Whether `state` ends an item of item_type: its machine's terminal states, or a file-held type's ends
    (statemachines.terminal_states; review 2026-11-26: a type-blind set read a red item's live `red` as an end)."""
    return is_terminal(item_type, state)


SKIP_KINDS = re.compile(r"\.illegal$|^policy\.unapproved$")


@dataclass
class Segment:
    """A span of one item in one state: [start, end), why it was there (cause), who put it there, own or derived."""
    item_type: str
    item_id: str
    state: str
    start: float
    end: float
    cause: Optional[str]
    actor: Optional[str]
    origin: str  # own | exact | inferred | none

    @property
    def length(self) -> float:
        return max(0.0, self.end - self.start)


@dataclass
class ItemTrace:
    """One item's merged events and the segments they make."""
    item_type: str
    item_id: str
    events: list[dict[str, Any]] = field(default_factory=list)
    segments: list[Segment] = field(default_factory=list)
    path: list[str] = field(default_factory=list)


def merge(events: Iterable[dict[str, Any]], derived: Optional[dict[str, list[dict[str, Any]]]] = None) -> list[dict[str, Any]]:
    """The events with the backfill merged in: an event without its own cause takes the derived one (origin = its
    confidence), the derived opening events added; every row's `origin` says which (own | exact | inferred | none)."""
    derived = derived or {}
    back = {r["seq"]: r for r in derived.get("backfill") or ()}
    out = [_attributed(dict(ev), back.get(ev.get("seq"))) for ev in events]
    out += [{**d, "seq": None, "severity": "info", "text": f"(derived from {d['source']})", "refs": {},
             "origin": d["confidence"]} for d in derived.get("derived") or ()]
    out.sort(key=lambda e: (e.get("at") or 0, e.get("seq") or 0))
    return out


def _attributed(ev: dict[str, Any], b: Optional[dict[str, Any]]) -> dict[str, Any]:
    """Event ev with its origin, and the backfill row b's cause and actor where its own are missing."""
    b = b or {}
    if ev.get("cause"):
        ev["origin"] = "own"
    elif b.get("cause"):
        ev.update(cause=b["cause"], origin=b["confidence"], source=b["source"])
    else:
        ev["origin"] = "none"
    ev["actor"] = ev.get("actor") or b.get("actor")
    return ev


def traces(events: Iterable[dict[str, Any]], now: float, item_type: Optional[str] = None) -> dict[tuple[str, str], ItemTrace]:
    """Each item's trace from merged events (merge), its open stay ended at `now`."""
    by: dict[tuple[str, str], ItemTrace] = {}
    for ev in events:
        typ, iid = ev.get("item_type"), ev.get("item_id")
        if not typ or not iid or (item_type and typ != item_type) or SKIP_KINDS.search(ev.get("kind") or ""):
            continue
        by.setdefault((typ, iid), ItemTrace(typ, iid)).events.append(ev)
    for tr in by.values():
        _segments(tr, now)
    return by


Said = tuple[Optional[str], Optional[str], str]  # why an item is in a state: (cause, actor, origin)
UNSAID: Said = (None, None, "none")


@dataclass
class Stay:
    """One stay of an item in a state: [start, end) (end None: still there), why it was there, the event that ended it."""
    state: str
    start: float
    end: Optional[float]
    said: Said
    left_by: Optional[dict[str, Any]]


def _said(ev: dict[str, Any]) -> Said:
    return ev.get("cause"), ev.get("actor"), ev.get("origin", "none")


def stays(events: Iterable[dict[str, Any]], state: str, since: float, said: Said = UNSAID,
          reminders: bool = True) -> list[Stay]:
    """THE STAY WALKER (review 2026-11-26, C 0: history/analyze and the stats records each walked an item's events):
    the item in `state` since `since`, then each of its moves in order -- an opening (no from-state) says nothing of a
    begun item's states. A move ends the stay and begins the next; a reminder (a self-move) ends it too when
    `reminders` (a stay split into segments, each with the cause last said), else it is no new stay. A reminder with no
    cause keeps the stay's. The last stay is open (end None)."""
    out: list[Stay] = []
    for ev in events:
        frm, to = ev.get("from_state"), ev.get("to_state")
        if frm is None or to is None or (not reminders and frm == to == state):
            continue
        out.append(Stay(state, since, ev["at"], said, ev))
        if ev.get("cause") or to != frm:
            said = _said(ev)
        state, since = str(to), ev["at"]
    return out + [Stay(state, since, None, said, None)]


def _walked(events: list[dict[str, Any]]) -> list[Stay]:
    """An item's stays from its first move: an opening's own state and what it says, else the state a move left. An
    event naming no state (an `agentctl` pin, a lease archived) is no move: an item of only such has no stays."""
    moves = [ev for ev in events if ev.get("from_state") is not None or ev.get("to_state") is not None]
    if not moves:
        return []
    first = moves[0]
    if first.get("from_state") is None:
        return stays(moves[1:], str(first.get("to_state") or ""), float(first["at"]), _said(first))
    return stays(moves, str(first["from_state"]), float(first["at"]))


def _path(walked: list[Stay]) -> list[str]:
    """The states an item went through, a reminder's split stay once."""
    return [s.state for i, s in enumerate(walked) if not i or walked[i - 1].state != s.state]


def _segments(tr: ItemTrace, now: float) -> None:
    """The trace's path and segments: each stay of positive length, the open one ended at `now` unless its state ends
    the item."""
    walked = _walked(tr.events)
    tr.path = _path(walked)
    for s in walked:
        end = now if s.end is None and not ends_item(tr.item_type, s.state) else s.end
        if end is not None and end > s.start:
            tr.segments.append(Segment(tr.item_type, tr.item_id, s.state, float(s.start), float(end), *s.said))
