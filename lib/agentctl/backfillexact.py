# agentctl, part `backfillexact`: an inferred backfill row made EXACT when the store's own structured history proves it
# A real module (lib/agentctl/__init__.py MODULES): its own imports, the standard library's and MODULES' only.
#
# THE BACKFILL'S INFERRED ROWS (backfill.py) read a cause from an event's text. Three kinds of them name something the
# store recorded on its own, in states and items, so they are checked against it and, when it agrees, marked `exact`
# with the proof as their source (the 2026-11-26 copy: 17 d of `waiting` and 11 d of held `queued` time were inferred):
#   gate-red      a change entering `waiting` while a GATE red item was open (its own events: opened .. cleared)
#   overlap-hold  a held change's reminder naming the change (or batch) that holds it, which its own events show open --
#   lock-wait     in the state the reminder names, when it names one -- at that moment
#   batch         a change `checking in ... batch B` that B's item lists among its members
# An original event is never touched; a row the history does not prove stays inferred.

import collections
from bisect import bisect_right
import re
import sqlite3
from typing import Any, Optional

from agentctl.statefile import json_object

HOLDER = re.compile(r"waits for (?:batch )?(\S+?)'?(?: \(([a-z-]+)\))?(?:[:,;]|$| which| \()")
IN_BATCH = re.compile(r"checking in (?:backlog|push) batch (\S+)")
GATE_DONE = ("cleared", "verified")


def item_moves(c: sqlite3.Connection) -> dict[tuple[str, str], list[tuple[float, str]]]:
    """Each item's real moves in order: (time, the state it entered)."""
    moves: dict[tuple[str, str], list[tuple[float, str]]] = collections.defaultdict(list)
    for typ, iid, at, to in c.execute("SELECT item_type, item_id, at, to_state FROM events WHERE (from_state IS NULL "
                                      "OR from_state != to_state) AND to_state IS NOT NULL ORDER BY seq"):
        moves[(typ, iid)].append((float(at), to))
    return moves


def _closed_at(mv: list[tuple[float, str]]) -> float:
    return next((t for t, s in mv if s in GATE_DONE), float("inf"))


class History:
    """Each item's real moves in order (time, state), and the gate reds' open windows, from the events table."""

    def __init__(self, c: sqlite3.Connection) -> None:
        self.moves = item_moves(c)
        gate = {r[0] for r in c.execute("SELECT id, data FROM items WHERE type='red'") if json_object(r[1]).get("gate")}
        self.windows = [(iid, mv[0][0], _closed_at(mv)) for (typ, iid), mv in self.moves.items()
                        if typ == "red" and iid in gate and mv]
        self.members = {r[0]: set(json_object(r[1]).get("members") or ())
                        for r in c.execute("SELECT id, data FROM items WHERE type='batch'")}

    def state_at(self, typ: str, iid: str, at: float) -> Optional[str]:
        """The state item (typ, iid) was in at `at`, from its own moves (None before its first)."""
        mv = self.moves.get((typ, iid)) or []
        i = bisect_right([t for t, _s in mv], at)
        return mv[i - 1][1] if i else None

    def gate_red_at(self, at: float) -> Optional[str]:
        return next((iid for iid, a, b in self.windows if a <= at < b), None)


TERMINAL = {"landed", "rejected", "cancelled", "superseded", "landed-already"}


def _holder_proof(h: History, text: str, at: float) -> Optional[tuple[str, str]]:
    """(holder, proof) when the reminder's holder was open -- in the state it names -- at that moment."""
    m = HOLDER.search(text)
    if not m:
        return None
    holder, named = m.group(1), m.group(2)
    for typ in ("change", "batch"):
        was = h.state_at(typ, holder, at)
        if was and was not in TERMINAL and (named is None or named == was):
            return holder, f"reminder text; {typ} {holder} was {was} then (its own events)"
    return None


def _gate_proof(h: History, ev: dict[str, Any]) -> Optional[tuple[str, str]]:
    red = h.gate_red_at(float(ev["at"])) if ev.get("to_state") == "waiting" else None
    return (red, f"gate red {red} open then (its own events)") if red else None


def _hold_proof(h: History, ev: dict[str, Any]) -> Optional[tuple[str, str]]:
    if ev.get("from_state") != ev.get("to_state"):
        return None
    return _holder_proof(h, str(ev.get("text") or ""), float(ev["at"]))


def _batch_proof(h: History, ev: dict[str, Any]) -> Optional[tuple[str, str]]:
    m = IN_BATCH.search(str(ev.get("text") or "")) if ev.get("to_state") == "checking" else None
    if m and ev.get("item_id") in h.members.get(m.group(1), ()):
        return m.group(1), f"batch {m.group(1)} lists it as a member (its item)"
    return None


PROOFS = {"gate-red": _gate_proof, "overlap-hold": _hold_proof, "lock-wait": _hold_proof, "batch": _batch_proof}


def exact_proof(h: History, ev: dict[str, Any], cause: str) -> Optional[tuple[str, str]]:
    """(detail, source) proving a change event's inferred cause from the store's structured history, or None."""
    prove = PROOFS.get(cause)
    return prove(h, ev) if prove and ev.get("item_type") == "change" else None


def upgrade_inferred(c: sqlite3.Connection) -> int:
    """Mark exact every inferred backfill row the history proves (idempotent: an exact row is never read again).
    Returns the rows upgraded."""
    rows = c.execute("SELECT b.seq, b.cause, e.at, e.item_type, e.item_id, e.from_state, e.to_state, e.text FROM "
                     "event_backfill b JOIN events e USING (seq) WHERE b.confidence='inferred' AND b.cause IN "
                     "('gate-red', 'overlap-hold', 'lock-wait', 'batch')").fetchall()
    if not rows:
        return 0
    h = History(c)
    keys = ("seq", "cause", "at", "item_type", "item_id", "from_state", "to_state", "text")
    done = []
    for r in rows:
        ev = dict(zip(keys, r))
        proof = exact_proof(h, ev, ev["cause"])
        if proof:
            done.append((proof[0], proof[1], ev["seq"]))
    c.execute("BEGIN IMMEDIATE")
    c.executemany("UPDATE event_backfill SET confidence='exact', detail=?, source=? WHERE seq=? AND "
                  "confidence='inferred'", done)
    c.execute("COMMIT")
    return len(done)
