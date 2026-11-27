# agentctl, part `store`: the store: one SQLite database of leases, offers, items and their events, passed in (CurrentStore)
# A real module (lib/agentctl/__init__.py MODULES): its own imports, the standard library's and MODULES' only.

import json
import os
from contextlib import closing
import re
import sqlite3
import time
from typing import Any, Callable, cast
from agentctl.statefile import json_object
from agentctl.statemachines import (
    ARCHIVE, IllegalTransition, MACHINES, StaleVersion, StateSpec, TerminalState, change_exit_event, check_move,
    level_after, machine_state)
from agentctl.clock import epoch_now, stopwatch
from agentctl.leasetable import LeaseLost
from agentctl.offers import OfferTable
from agentctl.core import err
from agentctl.eventtrace import EventQuery, query_rows
from agentctl.eventrows import insert_event, migrate_events, move_event, opened_event, record_rows
from agentctl.backfill import (
    backfill_check_runs, backfill_store, check_run_rows, derived_rows, ensure_backfill_tables)
from agentctl.paths import state_root


# ---------------------------------------------------------------- the store (design §3, §6; phase 1 step 1)
# ONE sqlite database at the state root ($AGENTCTL_STATE_ROOT/store.db, WAL, BEGIN IMMEDIATE for writers): leases now;
# items, events and offers as the later steps move into it. Its clock is the only clock leases use.
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
              # one item's history (stats.py reads it per finished item); a type's window and a kind's rows
              # (eventtrace.query_rows: history and analyze), created in place on a store older than them
              "CREATE INDEX IF NOT EXISTS events_item ON events (item_type, item_id, seq)",
              "CREATE INDEX IF NOT EXISTS events_type_at ON events (item_type, at)",
              "CREATE INDEX IF NOT EXISTS events_kind ON events (kind, at)")

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
                insert_event(c, opened_event(typ, iid, state, now_, (data or {}).get("opened_by")))
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
                insert_event(c, opened_event(typ, f"{prefix}{n}", state, now_, data.get("opened_by")))
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
        except Exception as e:  # the refusal stands whether or not it is recorded
            err(f"the refusal of {typ} {iid} was not recorded ({str(e)[:160]})")
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

    def merge_item(self, typ: str, iid: str, data: dict[str, Any]) -> dict[str, Any] | None:
        """Merge data into an existing item WITHOUT a new version: no state, entered_at or deadline change, no event, so
        no writer's compare-and-set is disturbed -- the queue's copy of a change's entry (waitingdir.ChangeRecords,
        docs/design/one-change-model.md step 3). The item as merged; None when there is no such item."""
        c = self.db()
        try:
            c.execute("BEGIN IMMEDIATE")
            cur = self._item(c, typ, iid)
            if cur:
                c.execute("UPDATE items SET data=? WHERE type=? AND id=?", (json.dumps({**cur["data"], **data}), typ, iid))
            c.execute("COMMIT")
            return self._item(c, typ, iid) if cur else None
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

    def record_events(self, rows: list[dict[str, Any]]) -> int:  # file-held state's moves (filelog.py, eventrows.py)
        return record_rows(self.db, rows)

    def derived(self) -> dict[str, Any]:  # the backfill's derived rows, whole (backfill.py; a client before v2's reads)
        return derived_rows(self.db)

    def trace_events(self, query: dict[str, Any]) -> list[dict[str, Any]]:
        """The event rows an EventQuery (its wire form) reads, merged by provenance, through the indexes (eventtrace)."""
        with closing(self.db()) as c:
            ensure_backfill_tables(c)
            return query_rows(c, EventQuery.of(query))

    def derived_check_runs(self) -> dict[str, Any]:  # the old check runs' fetched rows (backfill.py), by run
        return check_run_rows(self.db)

    def change_check_runs(self, since: float) -> list[list[Any]]:
        """[change id, run record] of every check run the change items hold that was decided at or after `since`: the runs
        alone, read inside the database (json_each), not every change item (analyze's check rows)."""
        with closing(self.db()) as c:
            rows = c.execute("SELECT items.id, j.value FROM items, json_each(items.data, '$.check_runs') AS j "
                             "WHERE items.type='change' AND COALESCE(json_extract(j.value, '$.decided_at'), 0) >= ? "
                             "ORDER BY items.id, j.key", (since,)).fetchall()
        return [[r[0], json.loads(r[1])] for r in rows]

    def backfill_check_runs(self, fetch: Any, now: float, pause: Any) -> int:  # old runs' rows (backfill.py; server)
        return backfill_check_runs(self.db, fetch, now, pause)

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


class CurrentStore:
    """THE STORE IS PASSED IN (CLAUDE.md, "Design"): cli, the composition root, hands it to each command, tick and
    checker. Each call goes to the store opener(clock) gives then (cli: process_store, the remote store when a store URL
    is set, else the file store): a long-running process follows `store use URL` (2026-10-29: a fixer renewed its leases
    on the old store for hours). clock: the store's time, None for the wall's."""

    def __init__(self, opener: Callable[..., Any], clock: Callable[[], float] | None = None,
                 actor: str | None = None) -> None:
        self._opener, self._clock, self._actor = opener, clock, actor

    @property
    def actor(self) -> str | None:
        """The process's actor (actors.py) this store stamps on what it writes; None for none."""
        return self._actor

    def __getattr__(self, name: str) -> Any:
        found = getattr(self._opener(clock=self._clock), name)
        if not self._actor:
            return found
        # the process's actor (actors.py) on every move it writes that names none of its own, and on every item it
        # opens (`opened_by` of its data: the opening event's actor, which an older store keeps as a plain field)
        if name == "transition":
            return lambda *a, event=None, **k: found(*a, event={"actor": self._actor, **(event or {})}, **k)
        if name == "put_item":
            return lambda typ, iid, state, data=None: found(typ, iid, state, {"opened_by": self._actor, **(data or {})})
        if name == "open_singleton":
            return lambda typ, state, data, *a, **k: found(typ, state, {"opened_by": self._actor, **data}, *a, **k)
        return found


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


class StoreLacksCall(StoreUnavailable):
    """THE STORE IS OLDER THAN THIS CLIENT (2026-11-26: the laptop ran ~/bin's head, ci-runner stayed pinned to an
    older rev while agentctl's CI was red, and every sync tick re-sent its 88 KB `record_events` page to a store that
    answered 404). The store answered that it has no such call: no verdict on the data, which the caller keeps for
    later. RemoteStore remembers it (LACKING_RECHECK) and answers again without the network until then (`remembered`)."""

    def __init__(self, msg: str, remembered: bool = False) -> None:
        super().__init__(msg)
        self.remembered = remembered


class StoreAuthFailed(StoreUnavailable):
    """The store refused the caller's credential (HTTP 401/403): never transient. A wrapper holding leases takes a fresh
    store token once and, refused again, ends at once -- its leases released (or revoked by the tick) with `store auth
    failed: <detail>` -- instead of 'asking again at the next beat' forever (2026-10-31: an owner session started
    before the store-only-token switch got 403s while its wrapper kept its reds leased)."""
