# agentctl, part `store`: the store: one SQLite database of leases, offers, items and their events, behind open_store
# A real module (lib/agentctl/__init__.py MODULES): its own imports, the standard library's and MODULES' only.

import json
import os
import re
import time
from typing import Any, Callable
from agentctl.core import env_num, state_root
from agentctl.offers import OfferTable
from agentctl.statemachines import (
    ARCHIVE, IllegalTransition, MACHINES, StaleVersion, StateSpec, TerminalState, change_exit_event, machine_state)


# ---------------------------------------------------------------- the store (design §3, §6; phase 1 step 1)
# ONE sqlite database at the state root ($AGENTCTL_STATE_ROOT/store.db, WAL, BEGIN IMMEDIATE for writers): leases now;
# items, events and offers as the later steps move into it. Its clock is the only clock leases use.
class LeaseLost(RuntimeError):
    """A lease this process held is gone (expired and taken, past its max hold, or released): stop its work."""


# A SILENT MACHINE'S LEASE CLOCKS PAUSE (2026-11-03: the family owners restarted 22 / 11 / 3 times, ended "STOPPED: the
# owner's heartbeat lease was lost" -- the laptop asleep on battery, or the tunnel to this store down (Errno 61 every
# ~30 min that day), past OWNER_TTL: every laptop lease lapsed at once and the reconciler re-offered their reds). The
# store records each machine's last contact (contact(): every store RPC the laptop's agentctl makes says which machine
# it is). A lease whose holder's machine (its holder's prefix, `laptop:`) has been silent past CONTACT_GAP has its clock
# paused from then on: read (get, list, renew, acquire, take) as extended by the silence beyond CONTACT_GAP, and on the
# machine's next contact extended by that for real -- so a lease live when its machine went quiet is still live when it
# comes back, and nobody takes it meanwhile. A machine still talking (its sync loop beats every minute) pauses nothing:
# a dead wrapper's leases on a laptop that is awake lapse as before. Bounded: CONTACT_PAUSE_MAX, then they lapse.
CONTACT_GAP = 120               # s of a machine's silence before its leases' clocks pause ($AGENTCTL_CONTACT_GAP)
CONTACT_PAUSE_MAX = 12 * 3600   # s a silent machine's leases are paused at most ($AGENTCTL_CONTACT_PAUSE_MAX)
CONTACT_WRITE_EVERY = 10       # s: a machine's contact is written at most this often
CONTACT_MACHINE = re.compile(r"[a-z][a-z0-9-]{0,31}")


def lease_machine(holder):
    """The machine a holder names (`laptop:owner:x` -> laptop), or None."""
    m = str(holder or "").split(":", 1)[0]
    return m if ":" in str(holder or "") and CONTACT_MACHINE.fullmatch(m) else None


class Store(OfferTable):
    """Leases (design §6): a holder plus a TTL, renewed only on real progress (a new progress mark), capped by an
    absolute max hold; an expired lease is free. Every acquire bumps the epoch (a fencing token) and draws a secret that
    renew and release need."""

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

    def __init__(self, path=None, clock=None):
        self.path = path or state_root("store.db")
        self.clock = clock or time.time

    def db(self):
        import sqlite3
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        c = sqlite3.connect(self.path, timeout=30, isolation_level=None)
        c.row_factory = sqlite3.Row
        c.execute("PRAGMA journal_mode=WAL")
        for q in self.SCHEMA:
            c.execute(q)
        return c

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
        since = float(r["v"]) + env_num("AGENTCTL_CONTACT_GAP", CONTACT_GAP)
        if now_ <= since:
            return 0.0, None
        return min(now_ - since, env_num("AGENTCTL_CONTACT_PAUSE_MAX", CONTACT_PAUSE_MAX)), since

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
            meta = json.loads(row["meta"] or "{}")
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
                meta = json.loads(row["meta"] or "{}")
                meta["note"] = note
                c.execute("UPDATE leases SET meta=? WHERE name=? AND epoch=?", (json.dumps(meta), name, epoch))
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
                meta = json.loads(row["meta"] or "{}")
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
                meta = json.loads(row["meta"] or "{}")
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
                          (json.dumps({**json.loads(row["meta"] or "{}"), **meta}), name, epoch))
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
    def _item(self, c, typ, iid):
        r = c.execute("SELECT * FROM items WHERE type=? AND id=?", (typ, iid)).fetchone()
        if not r:
            return None
        d = dict(r)
        d["data"] = json.loads(d["data"] or "{}")
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
            c.execute("COMMIT")
            return self._item(c, typ, iid)
        finally:
            c.close()

    def transition(self, typ, iid, to, expect_version, data=None, event=None, deadline_s=None, level=None):
        """THE way an item changes state: compare-and-set on its version (StaleVersion on a miss), never out of a
        terminal state (TerminalState), its deadline from MACHINES, its data merged, and its event written in the same
        transaction (kind `<type>.<to>` unless given; deduped). A self-transition (an escalation) bumps the version, keeps
        entered_at and re-deadlines; deadline_s overrides the state's deadline (e.g. a gate red's shorter offer), and
        level its level (a quiet self-move that takes back an escalation the item never owed). Returns the new version."""
        spec, level_ = machine_state(typ, to), level
        c = self.db()
        try:
            c.execute("BEGIN IMMEDIATE")
            cur = self._item(c, typ, iid)
            if not cur or cur["version"] != expect_version:
                c.execute("ROLLBACK")
                raise StaleVersion(f"{typ} {iid}: version {cur and cur['version']}, expected {expect_version}")
            if machine_state(typ, cur["state"]).terminal and to != cur["state"]:
                c.execute("ROLLBACK")
                raise TerminalState(f"{typ} {iid} is {cur['state']}: terminal")
            if to != cur["state"] and to not in machine_state(typ, cur["state"]).exits:
                # THE TABLE IS THE LAW (2026-11-20): a move MACHINES does not list is refused, and recorded (one
                # `<type>.illegal` event per item and move) so the path that tried it is found, never silently taken
                c.execute("ROLLBACK")
                self._record_illegal(typ, iid, cur["state"], to, (event or {}).get("text"))
                raise IllegalTransition(f"{typ} {iid}: {cur['state']} -> {to} is no move of machine {typ!r} "
                                        f"(its exits: {', '.join(machine_state(typ, cur['state']).exits) or 'none'})")
            now_ = self.clock()
            same = to == cur["state"]
            # a self-transition whose event says severity `info` re-deadlines without escalating (a red's culprit decided:
            # blame.py), never a level up
            quiet = same and (event or {}).get("severity") == "info"
            level = cur["level"] + 1 if same and spec.escalate and not quiet else (cur["level"] if same else 0)
            level = level if level_ is None else int(level_)
            merged = {**cur["data"], **(data or {})}
            c.execute("UPDATE items SET state=?, version=?, entered_at=?, deadline=?, level=?, data=? WHERE type=? AND id=?",
                      (to, cur["version"] + 1, now_ if not same else cur["entered_at"],
                       now_ + (spec.deadline if deadline_s is None else float(deadline_s)), level,
                       json.dumps(merged), typ, iid))
            ev = dict(event or {})
            if typ == "change" and spec.terminal and not same:
                ev = change_exit_event(iid, to, merged, ev)
            c.execute("INSERT OR IGNORE INTO events (at, kind, item_type, item_id, from_state, to_state, severity, "
                      "audience, text, refs, dedupe) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                      (now_, ev.get("kind") or f"{typ}.{to}", typ, iid, cur["state"], to,
                       ev.get("severity") or ("escalate" if same and spec.escalate else "info"),
                       json.dumps(ev.get("audience") or {}), ev.get("text") or "", json.dumps(ev.get("refs") or {}),
                       ev.get("dedupe") or f"{typ}:{iid}:{to}:{cur['version'] + 1}"))
            c.execute("COMMIT")
            return cur["version"] + 1
        finally:
            c.close()

    def _record_illegal(self, typ, iid, frm, to, why):
        c = self.db()
        try:
            c.execute("INSERT OR IGNORE INTO events (at, kind, item_type, item_id, from_state, to_state, severity, "
                      "audience, text, refs, dedupe) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                      (self.clock(), f"{typ}.illegal", typ, iid, frm, to, "info", "{}",
                       f"REFUSED {typ} {iid}: {frm} -> {to} is no move of its machine"
                       + (f" (it would have said: {str(why)[:200]})" if why else ""), "{}",
                       f"{typ}:{iid}:illegal:{frm}:{to}"))
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

    def events(self, after=0, limit=500):
        c = self.db()
        try:
            out = []
            for r in c.execute("SELECT * FROM events WHERE seq>? ORDER BY seq LIMIT ?", (after, limit)):
                d = dict(r)
                for k in ("audience", "refs"):
                    d[k] = json.loads(d[k] or "{}")
                out.append(d)
            return out
        finally:
            c.close()

    def item_events(self, typ, iid):
        """One item's events, in order."""
        c = self.db()
        try:
            out = []
            for r in c.execute("SELECT * FROM events WHERE item_type=? AND item_id=? ORDER BY seq", (typ, iid)):
                d = dict(r)
                for k in ("audience", "refs"):
                    d[k] = json.loads(d[k] or "{}")
                out.append(d)
            return out
        finally:
            c.close()

    def archive_free_leases(self, older=None):
        """Free leases (released, revoked, lapsed) free for over LEASE_ARCHIVE_AFTER become one `lease.archived` event
        each (history: holder, epoch, outcome) and leave the table, which keeps only what is or was lately held."""
        cut = self.clock() - (LEASE_ARCHIVE_AFTER if older is None else older)
        c = self.db()
        n = 0
        try:
            c.execute("BEGIN IMMEDIATE")
            for r in c.execute("SELECT * FROM leases WHERE expires_at < ?", (cut,)).fetchall():
                meta = json.loads(r["meta"] or "{}")
                c.execute("INSERT OR IGNORE INTO events (at, kind, item_type, item_id, severity, audience, text, refs, "
                          "dedupe) VALUES (?,?,?,?,?,?,?,?,?)",
                          (self.clock(), "lease.archived", "lease", r["name"], "info", "{}",
                           f"{r['name']} epoch {r['epoch']}: {meta.get('outcome') or 'lapsed'}",
                           json.dumps({"holder": r["holder"], "epoch": r["epoch"], "acquired_at": r["acquired_at"],
                                       "free_since": r["expires_at"], "item": r["item"], "meta": meta}),
                           f"lease:{r['name']}:{r['epoch']}:archived"))
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

# THE STORE SEAM: the store a process uses is chosen once, at the composition root (cli: wire_store(process_store), the
# remote store when a store URL is set), not by the Store class (its constructor is the file store, nothing else).
# Until it is wired -- a part loaded on its own -- open_store() opens the file store.
_store_opener: list[Callable[..., Any]] = []


def wire_store(opener: Callable[..., Any]) -> None:
    """Make opener(clock=None) the store every open_store() returns (the composition root, once)."""
    _store_opener[:] = [opener]


def open_store(clock: Callable[[], float] | None = None) -> Any:
    """The process's state store: the wired opener's, else the file store."""
    return (_store_opener[0] if _store_opener else Store)(clock=clock)


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
