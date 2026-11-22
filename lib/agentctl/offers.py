# agentctl, part `offers`: the store's offers table: open work a taker may take, one open offer per item; take() closes it and acquires its lease
# A real module (lib/agentctl/__init__.py MODULES): its own imports, the standard library's and MODULES' only.

import json
import sqlite3
from typing import Any, Callable


Offer = dict[str, Any]  # an offers row, its picture decoded


class OfferTable:
    """The offers table (design §6.2; step 10), mixed into Store, which owns the database (its schema holds the table),
    the clock and the leases: db(), clock and _acquire_in come from it."""

    clock: Callable[[], float]

    def db(self) -> sqlite3.Connection:
        raise NotImplementedError

    def _acquire_in(self, c: sqlite3.Connection, name: str, holder: str, ttl: float, max_hold: float,
                    progress: str = "", item: str | None = None, meta: Any = None) -> dict[str, Any]:
        raise NotImplementedError

    def _offer_row(self, r: sqlite3.Row | None) -> Offer | None:
        if not r:
            return None
        d = dict(r)
        d["picture"] = json.loads(d["picture"] or "{}")
        return d

    def offer(self, kind: str, item: str, lease: str, tier: str, priority: int, deadline: float,
              picture: dict[str, Any]) -> int:
        """Open (or refresh) THE open offer of an item. Returns its id. The reconciler's only."""
        c = self.db()
        try:
            c.execute("BEGIN IMMEDIATE")
            r = c.execute("SELECT id FROM offers WHERE item=? AND closed_at IS NULL", (item,)).fetchone()
            if r:
                c.execute("UPDATE offers SET kind=?, lease=?, tier=?, priority=?, deadline=?, picture=? WHERE id=?",
                          (kind, lease, tier, priority, deadline, json.dumps(picture), r["id"]))
                oid = r["id"]
            else:
                oid = c.execute("INSERT INTO offers (kind, item, lease, tier, priority, deadline, picture, opened_at) "
                                "VALUES (?,?,?,?,?,?,?,?)", (kind, item, lease, tier, priority, deadline,
                                                             json.dumps(picture), self.clock())).lastrowid
            c.execute("COMMIT")
            return int(oid)
        finally:
            c.close()

    def offers(self, kind: str | None = None) -> list[Offer]:
        """The open offers, by priority (gate > fix > outside > flake), oldest first."""
        c = self.db()
        try:
            rows = c.execute("SELECT * FROM offers WHERE closed_at IS NULL" + (" AND kind=?" if kind else "")
                             + " ORDER BY priority, opened_at", ((kind,) if kind else ())).fetchall()
            return [o for r in rows if (o := self._offer_row(r))]
        finally:
            c.close()

    def offer_of(self, item: str) -> Offer | None:
        c = self.db()
        try:
            return self._offer_row(c.execute("SELECT * FROM offers WHERE item=? AND closed_at IS NULL", (item,)).fetchone())
        finally:
            c.close()

    def offer_reason(self, oid: int, reason: str | None) -> None:
        """Why nobody took it yet (capacity, dispatch OFF): its escalation says so."""
        c = self.db()
        try:
            c.execute("UPDATE offers SET reason=? WHERE id=?", (reason, oid))
        finally:
            c.close()

    def close_offer(self, item: str, why: str) -> None:
        c = self.db()
        try:
            c.execute("UPDATE offers SET closed_at=?, closed_why=? WHERE item=? AND closed_at IS NULL",
                      (self.clock(), why, item))
        finally:
            c.close()

    def take(self, oid: int, holder: str, ttl: float, max_hold: float, meta: Any = None) -> dict[str, Any]:
        """Atomically: the offer closed AND its lease acquired. The lease plus the picture, {'held': ...} when the lease
        is held, or {'gone': ...} when the offer is closed."""
        c = self.db()
        try:
            c.execute("BEGIN IMMEDIATE")
            off = self._offer_row(c.execute("SELECT * FROM offers WHERE id=?", (oid,)).fetchone())
            if not off or off["closed_at"]:
                c.execute("COMMIT")
                return {"gone": oid}
            r = self._acquire_in(c, off["lease"], holder, ttl, max_hold, "taken", off["item"], meta)
            if "held" not in r:
                c.execute("UPDATE offers SET closed_at=?, closed_why='taken', taken_by=? WHERE id=?",
                          (self.clock(), holder, oid))
                r["picture"] = off["picture"]
            c.execute("COMMIT")
            return r
        finally:
            c.close()
