# agentctl, part `eventtrace`: the event model -- one row shape over the store's events, the backfill and the offline log, merged by provenance, read by an indexed query -- and an item's trace walked into its stays (segments) and path
# A real module (lib/agentctl/__init__.py MODULES): its own imports, the standard library's and MODULES' only.

import json
import re
import sqlite3
from dataclasses import dataclass, field
from typing import Any, Iterable, Optional
from agentctl.statefile import json_object
from agentctl.statemachines import MACHINES, is_terminal, terminal_states


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
    origin: str  # PROVENANCES: own | exact | inferred | file | none

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


# THE EVENT MODEL (review 2026-11-26, C "three stores, one row shape"): an item's history has three sources and ONE row
# shape (EVENT_FIELDS), each row saying where it came from (`origin`, PROVENANCES): the store's `events` (`own`: the
# cause written with the move; `none`: an old row without one), the backfill's rows (`exact` or `inferred`: an old
# event's cause overlaid, a missing opening derived; backfill.py) and this host's offline log not yet drained (`file`:
# filelog.py, its own cause, the store's copy of it once drained). ONE reader merges them (eventreader.read_events);
# the store answers an EventQuery -- an item, a type over a time window, or kinds -- through its indexes
# (query_rows), never with its whole feed and backfill.
EVENT_FIELDS = ("seq", "at", "kind", "item_type", "item_id", "from_state", "to_state", "actor", "cause", "detail",
                "severity", "audience", "text", "refs", "dedupe", "origin")
PROVENANCES = ("own", "exact", "inferred", "file", "none")


@dataclass(frozen=True)
class EventQuery:
    """Which rows a trace reads. With `kinds`: the rows of those kinds (at or after `since`, of the type or item given).
    Without: EVERY row of each item asked for -- one item (item_type and item_id), or each item of item_type active at
    or after `since` (a row in the window, or not yet ended), so its stays are whole."""
    item_type: Optional[str] = None
    item_id: Optional[str] = None
    since: Optional[float] = None
    kinds: tuple[str, ...] = ()

    def wire(self) -> dict[str, Any]:
        return {"item_type": self.item_type, "item_id": self.item_id, "since": self.since, "kinds": list(self.kinds)}

    @classmethod
    def of(cls, d: dict[str, Any]) -> "EventQuery":
        return cls(d.get("item_type"), d.get("item_id"), d.get("since"), tuple(d.get("kinds") or ()))

    def admits(self, row: dict[str, Any]) -> bool:
        """Whether a row is one this query reads (a window's items aside: the trace's own window decides those)."""
        if self.item_type and row.get("item_type") != self.item_type:
            return False
        if self.item_id and row.get("item_id") != self.item_id:
            return False
        return not self.kinds or (row.get("kind") in self.kinds and float(row.get("at") or 0) >= (self.since or 0))


def derived_event(d: dict[str, Any]) -> dict[str, Any]:
    """A backfill-derived opening (backfill.derived_events) as an event row."""
    return {**d, "seq": None, "severity": "info", "text": f"(derived from {d['source']})", "refs": {},
            "origin": d["confidence"]}


def file_event_row(r: dict[str, Any]) -> dict[str, Any]:
    """An offline-log line not yet drained (filelog.py) as an event row: its own cause, no seq yet."""
    return {**r, "seq": None, "origin": "file"}


def _sorted(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    rows.sort(key=lambda e: (e.get("at") or 0, e.get("seq") or 0))
    return rows


def with_file_rows(rows: list[dict[str, Any]], pending: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    """The store's rows and the offline log's lines it has not taken yet (by dedupe: a drained line is the store's)."""
    have = {r.get("dedupe") for r in rows}
    extra = [file_event_row(r) for r in pending if r.get("dedupe") not in have]
    return _sorted(rows + extra) if extra else rows


def merge(events: Iterable[dict[str, Any]], derived: Optional[dict[str, list[dict[str, Any]]]] = None) -> list[dict[str, Any]]:
    """The events with the backfill merged in: an event without its own cause takes the derived one (origin = its
    confidence), the derived opening events added; every row's `origin` says which (own | exact | inferred | none)."""
    derived = derived or {}
    back = {r["seq"]: r for r in derived.get("backfill") or ()}
    out = [_attributed(dict(ev), back.get(ev.get("seq"))) for ev in events]
    out += [derived_event(d) for d in derived.get("derived") or ()]
    return _sorted(out)


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


# ---------------------------------------------------------------- the store's side: an EventQuery through its indexes
# events_item (item_type, item_id, seq), events_type_at (item_type, at), events_kind (kind, at) and derived_events'
# (item_type, item_id) / (item_type, at): Store.SCHEMA and backfill.BACKFILL_SCHEMA create them in place.
_MOVE = "kind NOT LIKE '%.illegal' AND kind != 'policy.unapproved'"  # SKIP_KINDS: no move of the item


def _window_ids(c: sqlite3.Connection, typ: str, since: float) -> list[str]:
    """The items of type typ active at or after `since`: a row in the window, or -- a machine's item -- no move yet
    into a terminal state (a machine's terminal state is never left), or -- a file-held type -- any (they are few)."""
    if typ in MACHINES:
        ends = terminal_states(typ)
        open_sql = (f"SELECT item_id FROM events WHERE item_type=? GROUP BY item_id HAVING "
                    f"SUM(to_state IN ({','.join('?' * len(ends))}) AND {_MOVE}) = 0") if ends else \
            "SELECT DISTINCT item_id FROM events WHERE item_type=?"
        params: tuple[Any, ...] = (typ, since, typ, since, typ, *ends)
    else:
        open_sql, params = "SELECT DISTINCT item_id FROM events WHERE item_type=?", (typ, since, typ, since, typ)
    sql = ("SELECT item_id FROM events WHERE item_type=? AND at>=? UNION SELECT item_id FROM derived_events "
           f"WHERE item_type=? AND at>=? UNION {open_sql}")
    return [r[0] for r in c.execute(sql, params)]


def _where(q: EventQuery, c: sqlite3.Connection) -> tuple[str, tuple[Any, ...]]:
    """The rows' WHERE clause (for events and derived_events alike) and its parameters."""
    if q.kinds:
        sql, params = f"kind IN ({','.join('?' * len(q.kinds))}) AND at>=?", (*q.kinds, q.since or 0.0)
        if q.item_type:
            sql, params = sql + " AND item_type=?", (*params, q.item_type)
        if q.item_id:
            sql, params = sql + " AND item_id=?", (*params, q.item_id)
        return sql, params
    if q.item_id:
        return "item_type=? AND item_id=?", (q.item_type, q.item_id)
    if not q.item_type:
        return "1", ()
    if not q.since:
        return "item_type=?", (q.item_type,)
    ids = _window_ids(c, q.item_type, float(q.since))
    return f"item_type=? AND item_id IN (SELECT value FROM json_each(?))", (q.item_type, json.dumps(ids))


def query_rows(c: sqlite3.Connection, q: EventQuery) -> list[dict[str, Any]]:
    """The rows an EventQuery reads, merged by provenance (merge): the events, the backfill's overlay of theirs, the
    derived openings of the same items."""
    where, params = _where(q, c)
    events = []
    for r in c.execute(f"SELECT * FROM events WHERE {where} ORDER BY seq", params):
        d = dict(r)
        for k in ("audience", "refs"):
            d[k] = json_object(d[k])
        events.append(d)
    bare = [e["seq"] for e in events if not e.get("cause")]
    back = [dict(r) for r in c.execute("SELECT * FROM event_backfill WHERE seq IN (SELECT value FROM json_each(?))",
                                       (json.dumps(bare),))]
    derived = [dict(r) for r in c.execute(f"SELECT * FROM derived_events WHERE {where} ORDER BY at", params)]
    return merge(events, {"backfill": back, "derived": derived})
