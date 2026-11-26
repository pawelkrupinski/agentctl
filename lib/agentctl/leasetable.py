# agentctl, part `leasetable`: the store's leases table: a holder plus a TTL, a fencing epoch, a paused clock for a silent machine
# A real module (lib/agentctl/__init__.py MODULES): its own imports, the standard library's and MODULES' only.

import json
import re
import sqlite3
from typing import Any, Callable
from agentctl.knobs import knob_contact_gap, knob_contact_pause_max
from agentctl.statefile import json_object


Lease = dict[str, Any]  # a leases row, its pause applied


class LeaseLost(RuntimeError):
    """A lease this process held is gone (expired and taken, past its max hold, or released): stop its work."""


CONTACT_WRITE_EVERY = 10       # s: a machine's contact is written at most this often
CONTACT_MACHINE = re.compile(r"[a-z][a-z0-9-]{0,31}")


def lease_machine(holder: str | None) -> str | None:
    """The machine a holder names (`laptop:owner:x` -> laptop), or None."""
    m = str(holder or "").split(":", 1)[0]
    return m if ":" in str(holder or "") and CONTACT_MACHINE.fullmatch(m) else None


class LeaseTable:
    """The leases table (design §6), mixed into Store, which owns the database (its schema holds the table) and the
    clock: db() and clock come from it. Leases: a holder plus a TTL, renewed only on real progress (a new progress mark),
    capped by an absolute max hold; an expired lease is free. Every acquire bumps the epoch (a fencing token) and draws
    a secret that renew and release need."""

    clock: Callable[[], float]

    def db(self) -> sqlite3.Connection:
        raise NotImplementedError

    def _row(self, c: sqlite3.Connection, name: str) -> Lease | None:
        r = c.execute("SELECT * FROM leases WHERE name=?", (name,)).fetchone()
        return self._paused(c, dict(r)) if r else None

    def _pause_of(self, c: sqlite3.Connection, machine: str | None, now_: float) -> tuple[float, float | None]:
        """(seconds the machine's lease clocks are paused now, the instant the pause began) -- (0, None) when it is
        in contact or never made one."""
        if not machine:
            return 0.0, None
        r = c.execute("SELECT v FROM meta WHERE k=?", (f"contact:{machine}",)).fetchone()
        if not r:
            return 0.0, None
        since = float(r["v"]) + knob_contact_gap()
        if now_ <= since:
            return 0.0, None
        return min(now_ - since, knob_contact_pause_max()), since

    def _paused(self, c: sqlite3.Connection, row: Lease | None, now_: float | None = None) -> Lease | None:
        """The row as its paused clock reads it (A SILENT MACHINE'S LEASE CLOCKS PAUSE): a lease live when its
        machine's pause began is extended by the pause so far."""
        if not row or not row.get("holder"):
            return row
        p, since = self._pause_of(c, lease_machine(row["holder"]), self.clock() if now_ is None else now_)
        if p and row["expires_at"] > since:
            row = dict(row, expires_at=row["expires_at"] + p, max_until=row["max_until"] + p, paused=p)
        return row

    def contact(self, machine: str | None) -> float:
        """The machine talks to the store: a pause of its leases' clocks ends, each lease it covered extended by it
        for real. The seconds applied (0 when there was none)."""
        if not CONTACT_MACHINE.fullmatch(str(machine or "")):
            return 0
        c = self.db()
        try:
            r = c.execute("SELECT v FROM meta WHERE k=?", (f"contact:{machine}",)).fetchone()
            if r and 0 <= self.clock() - float(r["v"]) < CONTACT_WRITE_EVERY:  # heard lately: no write per RPC
                return 0
            c.execute("BEGIN IMMEDIATE")
            now_ = self.clock()
            p, since = self._pause_of(c, machine, now_)
            if p:
                c.execute("UPDATE leases SET expires_at=expires_at+?, max_until=max_until+? "
                          "WHERE holder LIKE ? AND expires_at>?", (p, p, f"{machine}:%", since))
            c.execute("INSERT OR REPLACE INTO meta VALUES (?,?)", (f"contact:{machine}", str(now_)))
            c.execute("COMMIT")
            return p
        finally:
            c.close()

    def acquire(self, name: str, holder: str, ttl: float, max_hold: float, progress: str = "", item: str | None = None,
                meta: Any = None) -> dict[str, Any]:
        """{'epoch', 'secret', 'expires_at', 'max_until'} when acquired, else {'held': the live lease}."""
        c = self.db()
        try:
            c.execute("BEGIN IMMEDIATE")
            r = self._acquire_in(c, name, holder, ttl, max_hold, progress, item, meta)
            c.execute("COMMIT")
            return r
        finally:
            c.close()

    def _acquire_in(self, c: sqlite3.Connection, name: str, holder: str, ttl: float, max_hold: float, progress: str = "",
                    item: str | None = None, meta: Any = None) -> dict[str, Any]:
        import secrets as _secrets
        now_ = self.clock()
        row = self._row(c, name)
        if row and row["holder"] and row["expires_at"] > now_:
            return {"held": {k: row[k] for k in ("holder", "epoch", "expires_at", "max_until", "progress", "meta")}}
        epoch = (row["epoch"] if row else 0) + 1
        secret = _secrets.token_hex(16)
        max_until = now_ + max_hold
        exp = min(now_ + ttl, max_until)
        c.execute("INSERT OR REPLACE INTO leases VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                  (name, holder, epoch, secret, now_, now_, exp, max_until, progress, item, json.dumps(meta or {})))
        return {"epoch": epoch, "secret": secret, "expires_at": exp, "max_until": max_until, "ttl": ttl}

    def renew(self, name: str, epoch: int, secret: str, progress: str, ttl: float, note: str | None = None) -> str:
        """'ok' (extended: the progress mark changed), 'no_progress' (not extended), or 'lost'."""
        c = self.db()
        try:
            c.execute("BEGIN IMMEDIATE")
            now_ = self.clock()
            row = self._row(c, name)
            if not row or row["epoch"] != epoch or row["secret"] != secret or not row["holder"] \
                    or row["expires_at"] <= now_ or now_ >= row["max_until"]:
                c.execute("COMMIT")
                return "lost"
            if progress == row["progress"]:
                c.execute("COMMIT")
                return "no_progress"
            meta = json_object(row["meta"])
            if note is not None:
                meta["note"] = note
            c.execute("UPDATE leases SET progress=?, renewed_at=?, expires_at=?, meta=? WHERE name=?",
                      (progress, now_, min(now_ + ttl, row["max_until"]), json.dumps(meta), name))
            c.execute("COMMIT")
            return "ok"
        finally:
            c.close()

    def note(self, name: str, epoch: int, secret: str, note: str) -> None:
        """Say what the holder does now (waiters print it); no renewal."""
        c = self.db()
        try:
            row = self._row(c, name)
            if row and row["epoch"] == epoch and row["secret"] == secret:
                meta = json.dumps({**json_object(row["meta"]), "note": note})
                c.execute("UPDATE leases SET meta=? WHERE name=? AND epoch=?", (meta, name, epoch))
        finally:
            c.close()

    def release(self, name: str, epoch: int, secret: str, outcome: str | None = None, notes: Any = None) -> str:
        """'ok' (the lease is free; its outcome and notes stay in its meta for the tick), or 'lost'."""
        c = self.db()
        try:
            c.execute("BEGIN IMMEDIATE")
            row = self._row(c, name)
            ok = bool(row and row["epoch"] == epoch and row["secret"] == secret and row["holder"])
            if ok and row:
                meta = json_object(row["meta"])
                if outcome is not None:
                    meta.update(outcome=outcome, notes=notes or "", released_at=self.clock())
                c.execute("UPDATE leases SET holder='', expires_at=?, meta=? WHERE name=?",
                          (self.clock(), json.dumps(meta), name))
            c.execute("COMMIT")
            return "ok" if ok else "lost"
        finally:
            c.close()

    def revoke(self, name: str, outcome: str) -> bool:
        """The reconciler takes a lease back (a refuted fix, a cleared red, a fix submitted): free now, its holder's next
        renewal answers lost. True when it was live."""
        c = self.db()
        try:
            c.execute("BEGIN IMMEDIATE")
            row = self._row(c, name)
            live = bool(row and row["holder"] and row["expires_at"] > self.clock())
            if live and row:
                meta = json_object(row["meta"])
                meta.update(outcome=outcome, revoked=True, released_at=self.clock())
                c.execute("UPDATE leases SET holder='', expires_at=?, meta=? WHERE name=?",
                          (self.clock(), json.dumps(meta), name))
            c.execute("COMMIT")
            return live
        finally:
            c.close()

    def set_meta(self, name: str, epoch: int, secret: str, meta: dict[str, Any]) -> None:
        """Merge into the holder's lease meta (its worktree, session, pid): no renewal."""
        c = self.db()
        try:
            c.execute("BEGIN IMMEDIATE")
            row = self._row(c, name)
            if row and row["epoch"] == epoch and row["secret"] == secret:
                c.execute("UPDATE leases SET meta=? WHERE name=? AND epoch=?",
                          (json.dumps({**json_object(row["meta"]), **meta}), name, epoch))
            c.execute("COMMIT")
        finally:
            c.close()

    def get(self, name: str) -> Lease | None:
        c = self.db()
        try:
            return self._row(c, name)
        finally:
            c.close()

    def list(self, prefix: str = "") -> list[Lease | None]:
        c = self.db()
        try:
            return [self._paused(c, dict(r))
                    for r in c.execute("SELECT * FROM leases WHERE name LIKE ? ORDER BY name", (prefix + "%",))]
        finally:
            c.close()
