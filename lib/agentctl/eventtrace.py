# agentctl, part `eventtrace`: an item's events read as one trace: the backfill merged in by provenance, walked into its stays (segments) and path
# A real module (lib/agentctl/__init__.py MODULES): its own imports, the standard library's and MODULES' only.

import re
from dataclasses import dataclass, field
from typing import Any, Iterable, Optional
from agentctl.statemachines import MACHINES


TERMINAL = {"landed", "rejected", "cancelled", "superseded", "verified", "cleared", "ok", "red", "unknown", "closed",
            "released", "acked", "evicted", "owner"}  # the last four: file-held state (filelog.py; `owner`: an escalation
# handed to the owner, escalations.py)


def ends_item(item_type: str, state: str) -> bool:
    """Whether `state` ends an item of item_type: its machine's say (MACHINES), else -- a file-held type, or a state its
    machine does not list -- TERMINAL (review 2026-11-26: a red item's live `red` read as an end)."""
    spec = MACHINES.get(item_type, {}).get(state)
    return spec.terminal if spec is not None else state in TERMINAL


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


class _Walk:
    """One item's events walked into its segments and path: the state it is in, since when, and the stay's cause."""

    def __init__(self, tr: ItemTrace) -> None:
        self.tr, self.cur, self.start = tr, "", 0.0
        self.said: tuple[Optional[str], Optional[str], str] = (None, None, "none")

    def first(self, ev: dict[str, Any]) -> None:
        """The first event: its from-state began then (an opening: its own state, and what it says)."""
        frm, to = ev.get("from_state"), str(ev.get("to_state") or "")
        self.cur, self.start = (to if frm is None else str(frm)), float(ev["at"])
        self.tr.path.append(self.cur)
        if frm is None:
            self.said = (ev.get("cause"), ev.get("actor"), ev.get("origin", "none"))
        else:
            self.step(ev)

    def step(self, ev: dict[str, Any]) -> None:
        """A later event: the stay so far a segment; a move a new state; a reminder with a cause a new attribution."""
        frm, to, at = ev.get("from_state"), str(ev.get("to_state") or ""), float(ev["at"])
        if at > self.start:
            self.tr.segments.append(Segment(self.tr.item_type, self.tr.item_id, self.cur, self.start, at, *self.said))
        if to != self.cur:
            self.tr.path.append(to)
        self.cur, self.start = to, at
        if ev.get("cause") or to != frm:  # a reminder without a cause keeps the stay's cause
            self.said = (ev.get("cause"), ev.get("actor"), ev.get("origin", "none"))

    def end(self, now: float) -> None:
        if self.cur and not ends_item(self.tr.item_type, self.cur) and now > self.start:
            self.tr.segments.append(Segment(self.tr.item_type, self.tr.item_id, self.cur, self.start, now, *self.said))


def _segments(tr: ItemTrace, now: float) -> None:
    w = _Walk(tr)
    for ev in tr.events:
        if not w.cur:
            w.first(ev)
        elif ev.get("from_state") is not None:  # an opening after the item began says nothing of its states
            w.step(ev)
    w.end(now)
