# agentctl, part `backfill`: the attribution of the history written before causes.py, as DERIVED rows
# A real module (lib/agentctl/__init__.py MODULES): its own imports, the standard library's and MODULES' only.
#
# THE ORIGINAL EVENTS ARE NEVER REWRITTEN (the owner, 2026-11-26). What the history before the attribution columns can
# still tell -- a move's cause and actor, the opening events nobody wrote (a change's queue entry, a condition's open,
# a watch's start) -- is derived into two tables of their own, each row marked with its SOURCE and a CONFIDENCE:
# `exact` (read from structured data: a state, an item's recorded time, a ref) or `inferred` (parsed from text).
#   event_backfill  (seq -> actor, cause, detail, source, confidence): for an event whose own cause is NULL
#   derived_events  (a whole row): an event the history lacks
# `agentctl history` and `agentctl analyze` merge them in (timeline.py). backfill_store is idempotent (INSERT OR
# IGNORE on the event's seq / the derived row's dedupe) and re-runnable: maintain's `backfill` step runs it on the
# server, against the live store, after each new batch of events.

import re
import sqlite3
from typing import Any, Callable, Iterable, Optional

from agentctl.causes import HOLD_CAUSE, red_move_cause, reject_cause
from agentctl.statefile import json_object

BACKFILL_SCHEMA = (
    "CREATE TABLE IF NOT EXISTS event_backfill (seq INTEGER PRIMARY KEY, actor TEXT, cause TEXT, detail TEXT, "
    "source TEXT NOT NULL, confidence TEXT NOT NULL)",
    "CREATE TABLE IF NOT EXISTS derived_events (dedupe TEXT PRIMARY KEY, at REAL NOT NULL, kind TEXT NOT NULL, "
    "item_type TEXT, item_id TEXT, from_state TEXT, to_state TEXT, actor TEXT, cause TEXT, detail TEXT, "
    "source TEXT NOT NULL, confidence TEXT NOT NULL)",
)

Derived = Optional[tuple[str, str, str]]  # (cause, source, confidence)

# a change's moves by state alone (exact: the state is structured), and by its text (inferred), first match wins
CHANGE_STATE_CAUSE = {"pushing": "pushing", "landed": "landed", "superseded": "superseded", "checked": "check-passed"}
CHANGE_TEXT_CAUSE = (
    (re.compile(r"QUEUED AGAIN.*infra|stalled|fetch of main failed|could not reach"), "check-failed-infra"),
    (re.compile(r"REGEN NEEDED|regenerat"), "regen-hold"),
    (re.compile(r"batch"), "batch"),
    (re.compile(r"main moved|since its checks|combined tree"), "main-moved"),
    (re.compile(r"main is green|open red|red gate|WAITING|green"), "gate-red"),
    (re.compile(r"cancel"), "agent"),
    (re.compile(r"check slot|tree"), "check-slot"),
    (re.compile(r"waits for|held"), "overlap-hold"),
    (re.compile(r"push"), "pushing"),
    (re.compile(r"checking"), "check-running"),
)
HOLD_TEXT = re.compile(r"held: .*?\b(" + "|".join(sorted(HOLD_CAUSE, key=len, reverse=True)) + r")\b")


def _change_cause(ev: dict[str, Any]) -> Derived:
    """A change event's cause: a reject by THE classifier, a reminder by its hold, a state by itself, else its text."""
    to, text = ev.get("to_state"), str(ev.get("text") or "")
    if to == "rejected":
        return reject_cause(text, (ev.get("refs") or {}).get("tail") or ()), "event text and tail", "inferred"
    if to == ev.get("from_state"):
        return _reminder_cause(text)
    by_state = _state_cause(str(to), text)
    return by_state or _text_cause(text, "event text")


def _state_cause(to: str, text: str) -> Derived:
    if to in CHANGE_STATE_CAUSE:
        return CHANGE_STATE_CAUSE[to], "event state", "exact"
    if to == "waiting":
        return "gate-red", "event state (waiting is the red gate's)", "inferred"
    if to == "checking" and text.rstrip().endswith(": checking"):
        return "check-running", "event state", "exact"
    return None


def _text_cause(text: str, source: str) -> Derived:
    return next(((cause, source, "inferred") for pat, cause in CHANGE_TEXT_CAUSE if pat.search(text)), None)


def _reminder_cause(text: str) -> Derived:
    hit = HOLD_TEXT.search(text)
    if hit:
        return HOLD_CAUSE[hit.group(1)], "reminder text (hold kind)", "inferred"
    return _text_cause(text, "reminder text")


# the other machines' moves by their states alone (exact): (cause of a move into `to`, its source)
STATE_CAUSE: dict[str, Callable[[str, str], str]] = {
    "red": red_move_cause,
    "watch": lambda frm, to: "timeout" if to == "unknown" else "ci-verdict",
    "condition": lambda frm, to: "probe",
    "batch": lambda frm, to: "gate-red" if to == "blocked-red" else "batch",
}


def derive_cause(ev: dict[str, Any]) -> Derived:
    """(cause, source, confidence) of an event written without one, or None when nothing tells."""
    typ, frm, to = str(ev.get("item_type")), str(ev.get("from_state")), ev.get("to_state")
    if typ == "change":
        return _change_cause(ev)
    if frm == to or not to or typ not in STATE_CAUSE:
        return None
    return STATE_CAUSE[typ](frm, str(to)), f"{typ} machine states", "exact"


def derive_actor(ev: dict[str, Any], cause: Optional[str]) -> Optional[str]:
    """Who moved it, as far as the history says: an agent's command (cancel, claim) its agent, the rest the reconciler
    (the only writer of change, batch, watch and condition moves before the attribution columns)."""
    if cause in ("agent", "owner"):
        return None  # whose agent: the text rarely says
    return "reconciler" if ev.get("item_type") in ("change", "batch", "watch", "condition", "red") else None


OPENED_AT = {"change": ("submitted_at", "queued"), "condition": ("opened_at", "open"), "watch": ("pushed_at", "watching")}
OPENED_CAUSE_OF = {"change": "submitted", "condition": "probe", "watch": "landed"}


def derived_openings(items: Iterable[tuple[str, dict[str, Any]]], opened: set[tuple[str, str]],
                     first_at: dict[tuple[str, str], float]) -> list[dict[str, Any]]:
    """The opening event of each (type, item) the history has none of (`opened`: those it has), at the time its own
    data recorded (exact: a structured field of the item) -- never after the item's first event (`first_at`: a
    rederive overwrote a change's submitted_at, 2026-11-26's analysis: 220 changes)."""
    out = []
    for typ, it in items:
        key, state = OPENED_AT.get(typ, (None, None))
        at = _epoch((it.get("data") or {}).get(key)) if key else None
        first = first_at.get((typ, it["id"]))  # an item with no event at all predates the feed: nothing to open
        if at is None or first is None or (typ, it["id"]) in opened or at > first:
            continue
        out.append({"dedupe": f"{typ}:{it['id']}:opened", "at": at, "kind": f"{typ}.{state}", "item_type": typ,
                    "item_id": it["id"], "from_state": None, "to_state": state, "actor": None,
                    "cause": OPENED_CAUSE_OF[typ], "detail": None, "source": f"items.data.{key}", "confidence": "exact"})
    return out


def _epoch(v: Any) -> Optional[float]:
    try:
        return float(v) if v is not None and v != "" else None
    except (TypeError, ValueError):
        return None


def backfill_rows(events: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    """The event_backfill rows of the events written without a cause."""
    out = []
    for ev in events:
        if ev.get("cause"):
            continue
        got = derive_cause(ev)
        actor = derive_actor(ev, got[0] if got else None)
        if got or actor:
            cause, source, conf = got or (None, "item type", "inferred")
            out.append({"seq": ev["seq"], "actor": actor, "cause": cause, "detail": None, "source": source,
                        "confidence": conf})
    return out


def ensure_backfill_tables(c: sqlite3.Connection) -> None:
    for q in BACKFILL_SCHEMA:
        c.execute(q)


def backfill_store(open_db: Callable[[], sqlite3.Connection], page: int = 5000) -> dict[str, int]:
    """Derive and write every backfill row the store lacks (idempotent: a second run writes nothing). Returns the
    counts written: {"attributed": n, "opened": m}."""
    c = open_db()
    try:
        ensure_backfill_tables(c)
        done = int(c.execute("SELECT COALESCE(MAX(seq), 0) FROM event_backfill").fetchone()[0])
        n = 0
        while True:
            rows = [dict(r) for r in c.execute("SELECT * FROM events WHERE seq>? ORDER BY seq LIMIT ?", (done, page))]
            if not rows:
                break
            for r in rows:
                r["refs"] = json_object(r.get("refs"))
            new = backfill_rows(rows)
            c.execute("BEGIN IMMEDIATE")
            c.executemany("INSERT OR IGNORE INTO event_backfill VALUES (:seq, :actor, :cause, :detail, :source, "
                          ":confidence)", new)
            c.execute("COMMIT")
            n, done = n + len(new), rows[-1]["seq"]
        opened = {(r[0], r[1]) for r in c.execute("SELECT item_type, item_id FROM events WHERE from_state IS NULL")}
        items = [(r["type"], {"id": r["id"], "data": json_object(r["data"])})
                 for r in c.execute("SELECT type, id, data FROM items WHERE type IN ('change', 'condition', 'watch')")]
        first_at = {(r[0], r[1]): float(r[2]) for r in
                    c.execute("SELECT item_type, item_id, MIN(at) FROM events GROUP BY item_type, item_id")}
        openings = derived_openings(items, opened, first_at)
        c.execute("BEGIN IMMEDIATE")
        before = c.total_changes
        c.executemany("INSERT OR IGNORE INTO derived_events VALUES (:dedupe, :at, :kind, :item_type, :item_id, "
                      ":from_state, :to_state, :actor, :cause, :detail, :source, :confidence)", openings)
        m = c.total_changes - before
        c.execute("COMMIT")
        return {"attributed": n, "opened": m}
    finally:
        c.close()


def derived_rows(open_db: Callable[[], sqlite3.Connection]) -> dict[str, list[dict[str, Any]]]:
    """Both derived tables, whole (empty before the first backfill)."""
    c = open_db()
    try:
        ensure_backfill_tables(c)
        return {"backfill": [dict(r) for r in c.execute("SELECT * FROM event_backfill ORDER BY seq")],
                "derived": [dict(r) for r in c.execute("SELECT * FROM derived_events ORDER BY at")]}
    finally:
        c.close()
