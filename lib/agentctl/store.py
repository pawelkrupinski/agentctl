# agentctl, part `store`: the store: one SQLite database of leases, offers, items and their events, passed in (CurrentStore)
# A real module (lib/agentctl/__init__.py MODULES): its own imports, the standard library's and MODULES' only.

import json
import os
from contextlib import closing
import re
import sqlite3
from agentctl.clock import epoch_now, stopwatch
import time
from typing import Any, Callable, cast
from agentctl.knobs import knob_contact_gap, knob_contact_pause_max
from agentctl.core import state_root
from agentctl.statefile import json_object
from agentctl.offers import OfferTable
from agentctl.backfill import backfill_store, derived_rows
from agentctl.eventrows import insert_event, migrate_events, move_event, opened_event
from agentctl.statemachines import (
    ARCHIVE, IllegalTransition, MACHINES, check_move, level_after, StaleVersion, StateSpec, TerminalState, change_exit_event, machine_state)


# ---------------------------------------------------------------- the store (design §3, §6; phase 1 step 1)
# ONE sqlite database at the state root ($AGENTCTL_STATE_ROOT/store.db, WAL, BEGIN IMMEDIATE for writers): leases now;
# items, events and offers as the later steps move into it. Its clock is the only clock leases use.
class LeaseLost(RuntimeError):
    """A lease this process held is gone (expired and taken, past its max hold, or released): stop its work."""


CONTACT_WRITE_EVERY = 10       # s: a machine's contact is written at most this often
CONTACT_MACHINE = re.compile(r"[a-z][a-z0-9-]{0,31}")


def lease_machine(holder):
    """The machine a holder names (`laptop:owner:x` -> laptop), or None."""
    m = str(holder or "").split(":", 1)[0]
    return m if ":" in str(holder or "") and CONTACT_MACHINE.fullmatch(m) else None


# THE FIRST OPENERS OF A NEW DATABASE RACE ITS WAL SWITCH (2026-11-26, tests/test_seq_atomic.py, 20 of 200 fresh stores
# opened by 4 threads at once): `PRAGMA journal_mode=WAL` takes the file's exclusive lock and answers `database is
# locked` at once -- the connection's busy timeout does not cover it -- so a writer died before its first statement.
# WAL is the file's own once set: an opener that loses the race retries the switch within the connect timeout.
def wal_mode(c: sqlite3.Connection, bound: float = 30) -> None:
    sw = stopwatch(bound)
    while True:
        try:
            c.execute("PRAGMA journal_mode=WAL")
            return
        except sqlite3.OperationalError as e:
            if "locked" not in str(e) or sw.expired():
                raise
            time.sleep(0.01)  # wall: another opener holds the switch for a moment


class Store(OfferTable):
    """Leases (design §6): a holder plus a TTL, renewed only on real progress (a new progress mark), capped by an
    absolute max hold; an expired lease is free. Every acquire bumps the epoch (a fencing token) and draws a secret that
    renew and release need."""

    needs_token = False  # a local database: no credential (RemoteStore.needs_token: a call goes out with the caller's)

    SCHEMA = ("CREATE TABLE IF NOT EXISTS leases (name TEXT PRIMARY KEY, holder TEXT NOT NULL, epoch INTEGER NOT NULL, "
              "secret TEXT NOT NULL, acquired_at REAL, renewed_at REAL, expires_at REAL, max_until REAL, progress TEXT, "
              "item TEXT, meta TEXT)",
              "CREATE TABLE IF NOT EXISTS meta (k TEXT PRIMARY KEY, v TEXT)",
              # items (design §5): one row per machine item; `version` is the compare-and-set token of every transition
              "CREATE TABLE IF NOT EXISTS items (type TEXT NOT NULL, id TEXT NOT NULL, state TEXT NOT NULL, "
              "version INTEGER NOT NULL, entered_at REAL NOT NULL, deadline REAL NOT NULL, level INTEGER NOT NULL DEFAULT 0, "
              "data TEXT, PRIMARY KEY (type, id))",
              # events (design §8.1): seq is THE cursor; dedupe makes a repeated tick a no-op
              "CREATE TABLE IF NOT EXISTS events (seq INTEGER PRIMARY KEY AUTOINCREMENT, at REAL NOT NULL, kind TEXT NOT NULL, "
              "item_type TEXT, item_id TEXT, from_state TEXT, to_state TEXT, severity TEXT NOT NULL DEFAULT 'info', "
              "audience TEXT, text TEXT, refs TEXT, dedupe TEXT UNIQUE, notified_at REAL)",
              # offers (design §6.2; step 10; offers.py): open work a taker may take; take() closes it AND acquires its lease
              "CREATE TABLE IF NOT EXISTS offers (id INTEGER PRIMARY KEY AUTOINCREMENT, kind TEXT NOT NULL, item TEXT NOT NULL, "
              "lease TEXT NOT NULL, tier TEXT, priority INTEGER, deadline REAL, picture TEXT, opened_at REAL, closed_at REAL, "
              "closed_why TEXT, taken_by TEXT, reason TEXT)",
              # one item's history (stats.py reads it per finished item)
              "CREATE INDEX IF NOT EXISTS events_item ON events (item_type, item_id, seq)")

    def __init__(self, path: str | None = None, clock: Callable[[], float] | None = None) -> None:
        self.path = path or state_root("store.db")
        self.clock = clock or epoch_now

    def db(self) -> sqlite3.Connection:
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        c = sqlite3.connect(self.path, timeout=30, isolation_level=None)
        c.row_factory = sqlite3.Row
        wal_mode(c)
        self.prepare(c)
        return c

    @classmethod
    def prepare(cls, c: sqlite3.Connection) -> None:
        """The schema on connection c: every table, and the events' attribution columns a store older than them lacks."""
        for q in cls.SCHEMA:
            c.execute(q)
        migrate_events(c)

    def _row(self, c, name):
        r = c.execute("SELECT * FROM leases WHERE name=?", (name,)).fetchone()
        return self._paused(c, dict(r)) if r else None

    def _pause_of(self, c, machine, now_):
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

    def _paused(self, c, row, now_=None):
        """The row as its paused clock reads it (A SILENT MACHINE'S LEASE CLOCKS PAUSE): a lease live when its
        machine's pause began is extended by the pause so far."""
        if not row or not row.get("holder"):
            return row
        p, since = self._pause_of(c, lease_machine(row["holder"]), self.clock() if now_ is None else now_)
        if p and row["expires_at"] > since:
            row = dict(row, expires_at=row["expires_at"] + p, max_until=row["max_until"] + p, paused=p)
        return row

    def contact(self, machine):
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

    def acquire(self, name, holder, ttl, max_hold, progress="", item=None, meta=None):
        """{'epoch', 'secret', 'expires_at', 'max_until'} when acquired, else {'held': the live lease}."""
        c = self.db()
        try:
            c.execute("BEGIN IMMEDIATE")
            r = self._acquire_in(c, name, holder, ttl, max_hold, progress, item, meta)
            c.execute("COMMIT")
            return r
        finally:
            c.close()

    def _acquire_in(self, c, name, holder, ttl, max_hold, progress="", item=None, meta=None):
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

    def renew(self, name, epoch, secret, progress, ttl, note=None):
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

    def note(self, name, epoch, secret, note):
        """Say what the holder does now (waiters print it); no renewal."""
        c = self.db()
        try:
            row = self._row(c, name)
            if row and row["epoch"] == epoch and row["secret"] == secret:
                meta = json.dumps({**json_object(row["meta"]), "note": note})
                c.execute("UPDATE leases SET meta=? WHERE name=? AND epoch=?", (meta, name, epoch))
        finally:
            c.close()

    def release(self, name, epoch, secret, outcome=None, notes=None):
        """'ok' (the lease is free; its outcome and notes stay in its meta for the tick), or 'lost'."""
        c = self.db()
        try:
            c.execute("BEGIN IMMEDIATE")
            row = self._row(c, name)
            ok = bool(row and row["epoch"] == epoch and row["secret"] == secret and row["holder"])
            if ok:
                meta = json_object(row["meta"])
                if outcome is not None:
                    meta.update(outcome=outcome, notes=notes or "", released_at=self.clock())
                c.execute("UPDATE leases SET holder='', expires_at=?, meta=? WHERE name=?",
                          (self.clock(), json.dumps(meta), name))
            c.execute("COMMIT")
            return "ok" if ok else "lost"
        finally:
            c.close()

    def revoke(self, name, outcome):
        """The reconciler takes a lease back (a refuted fix, a cleared red, a fix submitted): free now, its holder's next
        renewal answers lost. True when it was live."""
        c = self.db()
        try:
            c.execute("BEGIN IMMEDIATE")
            row = self._row(c, name)
            live = bool(row and row["holder"] and row["expires_at"] > self.clock())
            if live:
                meta = json_object(row["meta"])
                meta.update(outcome=outcome, revoked=True, released_at=self.clock())
                c.execute("UPDATE leases SET holder='', expires_at=?, meta=? WHERE name=?",
                          (self.clock(), json.dumps(meta), name))
            c.execute("COMMIT")
            return live
        finally:
            c.close()

    def set_meta(self, name, epoch, secret, meta):
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

    def get(self, name):
        c = self.db()
        try:
            return self._row(c, name)
        finally:
            c.close()

    # -- items and their transitions (design §5; phase 1 step 5) -----------------------------------------------------
    def _item(self, c: Any, typ: str, iid: str) -> dict[str, Any] | None:
        r = c.execute("SELECT * FROM items WHERE type=? AND id=?", (typ, iid)).fetchone()
        if not r:
            return None
        d = dict(r)
        d["data"] = json_object(d["data"])
        return d

    def item(self, typ, iid):
        c = self.db()
        try:
            return self._item(c, typ, iid)
        finally:
            c.close()

    def items(self, typ, states=None):
        c = self.db()
        try:
            rows = [self._item(c, typ, r["id"]) for r in c.execute("SELECT id FROM items WHERE type=? ORDER BY id", (typ,))]
        finally:
            c.close()
        return [r for r in rows if states is None or r["state"] in states]

    def put_item(self, typ, iid, state, data=None):
        """Create the item in `state` (version 1); an existing item is left as it is. Returns the item."""
        spec = machine_state(typ, state)
        c = self.db()
        try:
            c.execute("BEGIN IMMEDIATE")
            if not self._item(c, typ, iid):
                now_ = self.clock()
                c.execute("INSERT INTO items VALUES (?,?,?,?,?,?,?,?)",
                          (typ, iid, state, 1, now_, now_ + spec.deadline, 0, json.dumps(data or {})))
                insert_event(c, opened_event(typ, iid, state, now_))
            c.execute("COMMIT")
            return self._item(c, typ, iid)
        finally:
            c.close()

    def open_singleton(self, typ: str, state: str, data: dict[str, Any], seq_key: str, prefix: str,
                       deadline_s: float | None = None) -> tuple[dict[str, Any], bool]:
        """INSERT-IF-ABSENT for an item there is at most one of in force (the land hold): in ONE transaction, the item
        of typ in `state` whose deadline is still ahead (the latest), else a new one `<prefix><n>`, n minted from seq_key
        in that transaction, its deadline deadline_s (default: the state's). Returns (the item, whether it is new)."""
        c = self.db()
        try:
            c.execute("BEGIN IMMEDIATE")
            now_ = self.clock()
            row = c.execute("SELECT id FROM items WHERE type=? AND state=? AND deadline>? ORDER BY deadline DESC LIMIT 1",
                            (typ, state, now_)).fetchone()
            if row is None:
                n = c.execute("INSERT INTO meta VALUES (?,'1') ON CONFLICT(k) DO UPDATE SET v=v+1 RETURNING v", (seq_key,)).fetchone()[0]
                secs = machine_state(typ, state).deadline if deadline_s is None else deadline_s
                c.execute("INSERT INTO items VALUES (?,?,?,?,?,?,?,?)",
                          (typ, f"{prefix}{n}", state, 1, now_, now_ + secs, 0, json.dumps(data)))
                insert_event(c, opened_event(typ, f"{prefix}{n}", state, now_))
            c.execute("COMMIT")
            return cast(dict[str, Any], self._item(c, typ, row["id"] if row else f"{prefix}{n}")), row is None
        finally:
            c.close()

    def transition(self, typ, iid, to, expect_version, data=None, event=None, deadline_s=None, level=None):
        """THE way an item changes state: compare-and-set on its version (StaleVersion on a miss), never out of a
        terminal state (TerminalState), its deadline from MACHINES, its data merged, and its event written in the same
        transaction (kind `<type>.<to>` unless given; deduped). A self-transition (an escalation) bumps the version, keeps
        entered_at and re-deadlines; deadline_s overrides the state's deadline, level its level. Returns the new version."""
        spec = machine_state(typ, to)
        c = self.db()
        try:
            c.execute("BEGIN IMMEDIATE")
            cur = self._item(c, typ, iid)
            try:
                check_move(typ, iid, cur, to, expect_version)
            except Exception as refusal:
                c.execute("ROLLBACK")
                if isinstance(refusal, IllegalTransition):  # recorded once the write lock is back
                    self._record_illegal(typ, iid, cur["state"], to, (event or {}).get("text"))
                raise
            now_, same = self.clock(), to == cur["state"]
            level = level_after(cur, spec, to, event, level)
            merged = {**cur["data"], **(data or {})}
            c.execute("UPDATE items SET state=?, version=?, entered_at=?, deadline=?, level=?, data=? WHERE type=? AND id=?",
                      (to, cur["version"] + 1, now_ if not same else cur["entered_at"],
                       now_ + (spec.deadline if deadline_s is None else float(deadline_s)), level,
                       json.dumps(merged), typ, iid))
            self._insert_move_event(c, now_, typ, iid, cur, to, spec, merged, event)
            c.execute("COMMIT")
            return cur["version"] + 1
        finally:
            c.close()

    def _insert_move_event(self, c: Any, now_: Any, typ: Any, iid: Any, cur: Any, to: Any, spec: Any, merged: Any, event: Any) -> Any:
        """The event a move writes: the caller's (a change's exit event completed when it ends), deduped per version."""
        ev = dict(event or {})
        if typ == "change" and spec.terminal and to != cur["state"]:
            ev = change_exit_event(iid, to, merged, ev)
        insert_event(c, {**move_event(typ, iid, cur["state"], to, cur["version"] + 1, spec.escalate, ev), "at": now_})

    def _record_illegal(self, typ, iid, frm, to, why):
        c = self.db()
        try:
            said = f" (it would have said: {str(why)[:200]})" if why else ""
            insert_event(c, {"at": self.clock(), "kind": f"{typ}.illegal", "item_type": typ, "item_id": iid,
                             "from_state": frm, "to_state": to,
                             "text": f"REFUSED {typ} {iid}: {frm} -> {to} is no move of its machine{said}",
                             "dedupe": f"{typ}:{iid}:illegal:{frm}:{to}"})
        except Exception:
            pass  # the refusal stands whether or not it is recorded
        finally:
            c.close()

    def update_item(self, typ, iid, expect_version, data):
        """Merge data into the item (compare-and-set on its version): no state, entered_at or deadline change, no
        event. Returns the new version (StaleVersion on a miss)."""
        c = self.db()
        try:
            c.execute("BEGIN IMMEDIATE")
            cur = self._item(c, typ, iid)
            if not cur or cur["version"] != expect_version:
                c.execute("ROLLBACK")
                raise StaleVersion(f"{typ} {iid}: version {cur and cur['version']}, expected {expect_version}")
            c.execute("UPDATE items SET version=?, data=? WHERE type=? AND id=?",
                      (cur["version"] + 1, json.dumps({**cur["data"], **(data or {})}), typ, iid))
            c.execute("COMMIT")
            return cur["version"] + 1
        finally:
            c.close()

    def mark_notified(self, seq):
        c = self.db()
        try:
            c.execute("UPDATE events SET notified_at=? WHERE seq=? AND notified_at IS NULL", (self.clock(), seq))
        finally:
            c.close()

    def meta_get(self, k, default=None):
        c = self.db()
        try:
            r = c.execute("SELECT v FROM meta WHERE k=?", (k,)).fetchone()
            return r["v"] if r else default
        finally:
            c.close()

    def meta_set(self, k, v):
        c = self.db()
        try:
            c.execute("INSERT OR REPLACE INTO meta VALUES (?,?)", (k, str(v)))
        finally:
            c.close()

    def next_seq(self, key: str) -> int:
        """The next number of the counter `key` (meta), minted by ONE statement: atomic across the writers."""
        with closing(self.db()) as c:
            return int(c.execute("INSERT INTO meta VALUES (?,'1') ON CONFLICT(k) DO UPDATE SET v=v+1 RETURNING v", (key,)).fetchone()[0])

    def _event_rows(self, sql: str, params: tuple[Any, ...]) -> list[dict[str, Any]]:
        """The events a query selects, their audience and refs read from JSON."""
        c = self.db()
        try:
            out = []
            for r in c.execute(sql, params):
                d = dict(r)
                for k in ("audience", "refs"):
                    d[k] = json_object(d[k])
                out.append(d)
            return out
        finally:
            c.close()

    def events(self, after=0, limit=500):  # the feed after cursor `after`
        return self._event_rows("SELECT * FROM events WHERE seq>? ORDER BY seq LIMIT ?", (after, limit))

    def derived(self) -> dict[str, list[dict[str, Any]]]:  # the backfill's derived rows (backfill.py)
        return derived_rows(self.db)

    def backfill(self) -> dict[str, int]:  # derive what the history lacks (backfill.py; idempotent)
        return backfill_store(self.db)

    def item_events(self, typ, iid):
        """One item's events, in order."""
        return self._event_rows("SELECT * FROM events WHERE item_type=? AND item_id=? ORDER BY seq", (typ, iid))

    def archive_free_leases(self, older=None):
        """Free leases (released, revoked, lapsed) free for over LEASE_ARCHIVE_AFTER become one `lease.archived` event
        each (history: holder, epoch, outcome) and leave the table, which keeps only what is or was lately held."""
        cut = self.clock() - (LEASE_ARCHIVE_AFTER if older is None else older)
        c = self.db()
        n = 0
        try:
            c.execute("BEGIN IMMEDIATE")
            for r in c.execute("SELECT * FROM leases WHERE expires_at < ?", (cut,)).fetchall():
                meta = json_object(r["meta"])
                insert_event(c, {"at": self.clock(), "kind": "lease.archived", "item_type": "lease", "item_id": r["name"],
                                 "text": f"{r['name']} epoch {r['epoch']}: {meta.get('outcome') or 'lapsed'}",
                                 "refs": {"holder": r["holder"], "epoch": r["epoch"], "acquired_at": r["acquired_at"],
                                          "free_since": r["expires_at"], "item": r["item"], "meta": meta},
                                 "actor": r["holder"], "dedupe": f"lease:{r['name']}:{r['epoch']}:archived"})
                c.execute("DELETE FROM leases WHERE name=? AND epoch=?", (r["name"], r["epoch"]))
                n += 1
            c.execute("COMMIT")
            return n
        finally:
            c.close()

    def list(self, prefix=""):
        c = self.db()
        try:
            return [self._paused(c, dict(r))
                    for r in c.execute("SELECT * FROM leases WHERE name LIKE ? ORDER BY name", (prefix + "%",))]
        finally:
            c.close()

class CurrentStore:
    """THE STORE IS PASSED IN (CLAUDE.md, "Design"): cli, the composition root, hands it to each command, tick and
    checker. Each call goes to the store opener(clock) gives then (cli: process_store, the remote store when a store URL
    is set, else the file store): a long-running process follows `store use URL` (2026-10-29: a fixer renewed its leases
    on the old store for hours). clock: the store's time, None for the wall's."""

    def __init__(self, opener: Callable[..., Any], clock: Callable[[], float] | None = None,
                 actor: str | None = None) -> None:
        self._opener, self._clock, self._actor = opener, clock, actor

    def __getattr__(self, name: str) -> Any:
        found = getattr(self._opener(clock=self._clock), name)
        if name != "transition" or not self._actor:
            return found
        # the process's actor (actors.py) on every move it writes that names none of its own
        return lambda *a, event=None, **k: found(*a, event={"actor": self._actor, **(event or {})}, **k)


# §5.4 a condition (the recovery registry folded in), phase 1 step 12: open escalates 10 min after opening, then every
# 2 h (recovery_tick's re-deadline); only its own probe passing closes it
MACHINES["condition"] = {
    "open": StateSpec(10 * 60, "open", ("closed",), escalate=True, wait=True),
    "closed": StateSpec(ARCHIVE, "closed", (), terminal=True),
}


LEASE_ARCHIVE_AFTER = 24 * 3600  # a lease free this long is archived into the events (history), off the table


def store_errors() -> dict[str, type[Exception]]:
    return {"StaleVersion": StaleVersion, "TerminalState": TerminalState, "LeaseLost": LeaseLost,
            "IllegalTransition": IllegalTransition}


class StoreUnavailable(RuntimeError):
    """The remote store did not answer (the tunnel, ci-runner, the service): no verdict either way."""


class StoreAuthFailed(StoreUnavailable):
    """The store refused the caller's credential (HTTP 401/403): never transient. A wrapper holding leases takes a fresh
    store token once and, refused again, ends at once -- its leases released (or revoked by the tick) with `store auth
    failed: <detail>` -- instead of 'asking again at the next beat' forever (2026-10-31: an owner session started
    before the store-only-token switch got 403s while its wrapper kept its reds leased)."""
