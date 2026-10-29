# agentctl, part `store`: the store, the machines, events.
# Runs inside agentctl's one namespace, after the parts before it in lib/agentctl/__init__.py's PARTS (it uses their names).

# ---------------------------------------------------------------- the store (design §3, §6; phase 1 step 1)
# ONE sqlite database at the state root ($AGENTCTL_STATE_ROOT/store.db, WAL, BEGIN IMMEDIATE for writers): leases now;
# items, events and offers as the later steps move into it. Its clock is the only clock leases use.
class LeaseLost(RuntimeError):
    """A lease this process held is gone (expired and taken, past its max hold, or released): stop its work."""


class Store:
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
              # offers (design §6.2; step 10): open work a taker may take; take() closes it AND acquires its lease
              "CREATE TABLE IF NOT EXISTS offers (id INTEGER PRIMARY KEY AUTOINCREMENT, kind TEXT NOT NULL, item TEXT NOT NULL, "
              "lease TEXT NOT NULL, tier TEXT, priority INTEGER, deadline REAL, picture TEXT, opened_at REAL, closed_at REAL, "
              "closed_why TEXT, taken_by TEXT, reason TEXT)")

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
        return dict(r) if r else None

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

    # -- offers (design §6.2; step 10) ------------------------------------------------------------------------------
    def _offer_row(self, r):
        if not r:
            return None
        d = dict(r)
        d["picture"] = json.loads(d["picture"] or "{}")
        return d

    def offer(self, kind, item, lease, tier, priority, deadline, picture):
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
            return oid
        finally:
            c.close()

    def offers(self, kind=None):
        """The open offers, by priority (gate > fix > outside > flake), oldest first."""
        c = self.db()
        try:
            rows = c.execute("SELECT * FROM offers WHERE closed_at IS NULL" + (" AND kind=?" if kind else "")
                             + " ORDER BY priority, opened_at", ((kind,) if kind else ())).fetchall()
            return [self._offer_row(r) for r in rows]
        finally:
            c.close()

    def offer_of(self, item):
        c = self.db()
        try:
            return self._offer_row(c.execute("SELECT * FROM offers WHERE item=? AND closed_at IS NULL", (item,)).fetchone())
        finally:
            c.close()

    def offer_reason(self, oid, reason):
        """Why nobody took it yet (capacity, dispatch OFF): its escalation says so."""
        c = self.db()
        try:
            c.execute("UPDATE offers SET reason=? WHERE id=?", (reason, oid))
        finally:
            c.close()

    def close_offer(self, item, why):
        c = self.db()
        try:
            c.execute("UPDATE offers SET closed_at=?, closed_why=? WHERE item=? AND closed_at IS NULL",
                      (self.clock(), why, item))
        finally:
            c.close()

    def take(self, oid, holder, ttl, max_hold, meta=None):
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

    def transition(self, typ, iid, to, expect_version, data=None, event=None, deadline_s=None):
        """THE way an item changes state: compare-and-set on its version (StaleVersion on a miss), never out of a
        terminal state (TerminalState), its deadline from MACHINES, its data merged, and its event written in the same
        transaction (kind `<type>.<to>` unless given; deduped). A self-transition (an escalation) bumps the version, keeps
        entered_at and re-deadlines; deadline_s overrides the state's deadline (e.g. a gate red's shorter offer).
        Returns the new version."""
        spec = machine_state(typ, to)
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
            now_ = self.clock()
            same = to == cur["state"]
            level = cur["level"] + 1 if same and spec.escalate else (cur["level"] if same else 0)
            merged = {**cur["data"], **(data or {})}
            c.execute("UPDATE items SET state=?, version=?, entered_at=?, deadline=?, level=?, data=? WHERE type=? AND id=?",
                      (to, cur["version"] + 1, now_ if not same else cur["entered_at"],
                       now_ + (spec.deadline if deadline_s is None else float(deadline_s)), level,
                       json.dumps(merged), typ, iid))
            ev = dict(event or {})
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
            return [dict(r) for r in c.execute("SELECT * FROM leases WHERE name LIKE ? ORDER BY name", (prefix + "%",))]
        finally:
            c.close()


# ---------------------------------------------------------------- the machines (design §5; phase 1 step 5)
# Every state of every machine has a finite deadline and an automatic exit (`on_deadline`, a state of the same
# machine); a deadline-only walk from any state ends in a terminal state or cycles through an escalation (the
# table-driven test). Terminal states archive after 14 d. The "red" and "condition" machines join with steps 10-12.
class StaleVersion(RuntimeError):
    """A transition's compare-and-set lost: the item moved since it was read."""


class TerminalState(RuntimeError):
    """A transition out of a terminal state."""


class StateSpec:
    __slots__ = ("deadline", "on_deadline", "exits", "terminal", "escalate")

    def __init__(self, deadline, on_deadline, exits=(), terminal=False, escalate=False):
        self.deadline, self.on_deadline, self.exits = float(deadline), on_deadline, tuple(exits)
        self.terminal, self.escalate = terminal, escalate


ARCHIVE = 14 * 86400
M = 60
H = 3600
MACHINES = {
    # §5.1 a submitted change
    "change": {
        "queued": StateSpec(15 * M, "queued", ("checking", "superseded", "cancelled", "landed"), escalate=True),
        "checking": StateSpec(2 * H, "queued", ("checked", "rejected", "queued")),
        "checked": StateSpec(5 * M, "checked", ("pushing", "waiting", "queued"), escalate=True),
        "waiting": StateSpec(6 * H, "waiting", ("landed", "rejected", "queued"), escalate=True),
        "pushing": StateSpec(300, "checked", ("landed", "checked")),
        "landed": StateSpec(ARCHIVE, "landed", (), terminal=True),
        "rejected": StateSpec(ARCHIVE, "rejected", (), terminal=True),
        "superseded": StateSpec(ARCHIVE, "superseded", (), terminal=True),
        "cancelled": StateSpec(ARCHIVE, "cancelled", (), terminal=True),
    },
    # step 9: a landed sha's watch of Main and the convergence (no detached process: the tick evaluates it)
    "watch": {
        "watching": StateSpec(3 * H, "unknown", ("ok", "red", "unknown")),
        "ok": StateSpec(ARCHIVE, "ok", (), terminal=True),
        "red": StateSpec(ARCHIVE, "red", (), terminal=True),
        "unknown": StateSpec(ARCHIVE, "unknown", (), terminal=True),
    },
    # §5.2 + §14.1 the batch: a computed item, no branch
    "batch": {
        "blocked-red": StateSpec(6 * H, "blocked-red", ("assembling",), escalate=True),
        "assembling": StateSpec(300, "assembling", ("checking", "held", "landed"), escalate=True),
        "checking": StateSpec(2 * H, "held", ("pushing", "bisecting", "assembling", "held")),
        "bisecting": StateSpec(4 * H, "held", ("pushing", "assembling", "held")),
        "pushing": StateSpec(300, "assembling", ("landed", "assembling")),
        "held": StateSpec(30 * M, "assembling", ("assembling",), escalate=True),
        "landed": StateSpec(ARCHIVE, "landed", (), terminal=True),
    },
}

# §5.4 a condition (the recovery registry folded in), phase 1 step 12: open escalates 10 min after opening, then every
# 2 h (recovery_tick's re-deadline); only its own probe passing closes it
MACHINES["condition"] = {
    "open": StateSpec(10 * 60, "open", ("closed",), escalate=True),
    "closed": StateSpec(ARCHIVE, "closed", (), terminal=True),
}
# §5.3 a red (keyed by its failing test or step), phase 1 step 10; -> verified outside fix-pending: red_callable_evidence
MACHINES["red"] = {
    "red": StateSpec(60, "offered", ("offered", "fix-pending", "leased", "blocked-external", "cleared", "verified")),
    "offered": StateSpec(RED_OFFER["other"], "offered", ("leased", "fix-pending", "blocked-external", "cleared",
                                                         "verified"), escalate=True),
    "leased": StateSpec(RED_CLAIM_MAX, "offered", ("fix-pending", "offered", "blocked-external", "cleared", "verified")),
    "fix-pending": StateSpec(RED_FIX_DECIDE, "fix-pending", ("offered", "verified", "cleared"), escalate=True),
    "blocked-external": StateSpec(7 * 86400, "offered", ("offered", "fix-pending", "leased", "cleared", "verified")),
    "verified": StateSpec(ARCHIVE, "verified", (), terminal=True),
    "cleared": StateSpec(ARCHIVE, "cleared", (), terminal=True),
}


# an orphan branch red (settle_branch_reds): the deadlines only retire a forgotten one
MACHINES["branch-red"] = {
    "open": StateSpec(ARCHIVE, "gone", ("orphaned", "gone")),
    "orphaned": StateSpec(ARCHIVE, "gone", ("open", "gone")),
    "gone": StateSpec(ARCHIVE, "gone", (), terminal=True),
}


def machine_state(typ, state):
    try:
        return MACHINES[typ][state]
    except KeyError:
        raise ValueError(f"no state {state!r} in machine {typ!r}") from None


def deadline_walk(typ, state):
    """The deadline-only path from `state`: (reached terminal?, cycle escalates?)."""
    seen = []
    while state not in seen:
        seen.append(state)
        spec = MACHINES[typ][state]
        if spec.terminal:
            return True, False
        state = spec.on_deadline
    cycle = seen[seen.index(state):]
    return False, any(MACHINES[typ][x].escalate for x in cycle)


# the change items, projected from today's queue entries (read-only until step 6 makes the tick their writer)
ENTRY_STATE = {"queued": "queued", "checking": "checking", "checked": "checked", "pushing": "pushing",
               "joined": "waiting", "waiting": "waiting", "landed": "landed",
               "rejected": "rejected", "superseded": "superseded", "cancelled": "cancelled"}


def project_changes(d, store=None):
    """Each queue entry's change item: created in its state, moved (a transition, with its event) when the entry's
    status moved. Returns the items."""
    st = store or Store()
    out = []
    every = {x["id"]: x for x in waiting_entries(d, open_only=False)}
    for e in every.values():
        state = ENTRY_STATE.get(e.get("status"))
        if e.get("status") == "joined":  # a pre-step-8 join: the change follows the waiting entry that carries it
            w = every.get(e.get("waiting_id") or "")
            state = ENTRY_STATE.get((w or {}).get("status"), "landed" if not w else "waiting")
        if not state:
            continue
        cur = st.put_item("change", e["id"], state, {"worktree": e.get("worktree"), "head": e.get("head"),
                                                     "submitted_at": st.clock(),
                                                     "red_accum0": float(st.meta_get("red_accum", 0) or 0)})
        if cur["state"] != state and not machine_state("change", cur["state"]).terminal:
            try:
                st.transition("change", e["id"], state, cur["version"],
                              event={"text": f"{e['id']}: {cur['state']} -> {state}", "refs": {"entry": e["id"]}})
            except (StaleVersion, TerminalState):
                pass
            cur = st.item("change", e["id"])
        out.append(cur)
    return out


# ---------------------------------------------------------------- events -> the inbox (design §8.2; phase 1 step 7)
# The PROJECTOR writes one inbox entry, in today's format, for each event with an audience or a severity other than
# info -- its verdict line (refs.verdict) or its text, the audience's worktree as its cwd, its tail and exit; the entry
# id carries the event's seq, so a repeated pass writes nothing twice. The NOTIFIER sends a macOS notification for
# each severity=escalate event once (notified_at). Both run in the processor's loop; `agentctl inbox`, `inbox wait`,
# the orchestrator's routing and the status line read the inbox as before.
def project_events(store=None):
    """Write the inbox entries of the events since the projector's cursor; returns how many."""
    st = store or Store()
    cur = int(st.meta_get("projector_seq", 0) or 0)
    n = 0
    for ev in st.events(after=cur):
        cur = ev["seq"]
        aud, refs = ev.get("audience") or {}, ev.get("refs") or {}
        if not aud and ev["severity"] == "info":
            continue
        verdict = refs.get("verdict") or ev.get("text") or ev["kind"]
        eid = (dt.datetime.fromtimestamp(ev["at"], dt.timezone.utc).strftime("%Y%m%dT%H%M%S%f")
               + f"-ev{ev['seq']}-{re.sub(r'[^a-z]+', '-', ev['kind'])}")
        d = inbox_dir()
        path = os.path.join(d, eid + ".json")
        if os.path.exists(path):
            continue
        final = refs.get("final", True)
        rec = {"id": eid, "time": dt.datetime.fromtimestamp(ev["at"], dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
               "at": ev["at"], "command": refs.get("command") or ev["kind"].split(".")[0], "argv": [],
               "cwd": aud.get("worktree") or safe_cwd(), "pid": os.getpid(), "final": final,
               "exit": refs.get("exit", 0 if ev["severity"] == "info" else 1), "verdict": verdict,
               "kind": inbox_kind(verdict, final), "owner": aud.get("session") or aud.get("worktree") or "?",
               "claude_pid": aud.get("claude_pid"), "verdicts": [verdict], "tail": list(refs.get("tail") or [])[-25:],
               "event": ev["seq"]}
        try:
            os.makedirs(d, exist_ok=True)
            write_json(path, rec)
            n += 1
        except OSError as ex:
            err(f"projector: could not write {eid} ({ex})")
            break
    st.meta_set("projector_seq", cur)
    return n


def notify_escalations(store=None):
    """One macOS notification per severity=escalate event (notified_at marks it); returns how many."""
    st = store or Store()
    n = 0
    for ev in st.events(after=int(st.meta_get("notifier_seq", 0) or 0)):
        st.meta_set("notifier_seq", ev["seq"])
        if ev["severity"] != "escalate" or ev.get("notified_at"):
            continue
        st.mark_notified(ev["seq"])
        notify(f"agentctl: {ev['kind']}", str((ev.get("refs") or {}).get("verdict") or ev.get("text") or "")[:220])
        n += 1
    return n


def change_event(e, verdict, tail=(), code=1, final=True, severity="info", **refs):
    """The event a change's transition carries to its author (the projector makes it their inbox entry)."""
    return {"text": verdict.splitlines()[0], "severity": severity,
            "audience": {"worktree": e.get("worktree"), "session": e.get("session"), "claude_pid": e.get("claude_pid")},
            "refs": {"verdict": verdict.splitlines()[0], "tail": list(tail)[-25:], "exit": code, "final": final,
                     "command": "land", "entry": e["id"], **refs}}


LEASE_ARCHIVE_AFTER = 24 * 3600  # a lease free this long is archived into the events (history), off the table
LEASE_LIST_RECENT = 3600         # `lease list` shows the live leases and those free for less than this (--all: every one)


def maybe_archive_leases(d, every=3600):
    """Once an hour: the leases free for over a day go to the events (Store.archive_free_leases)."""
    st = Store()
    if time.time() - float(st.meta_get("lease_archive_at", 0) or 0) < every:
        return 0
    st.meta_set("lease_archive_at", time.time())
    n = st.archive_free_leases()
    if n:
        wlog(d, f"tick: {n} lease(s) free for over {LEASE_ARCHIVE_AFTER // 3600} h archived into the events")
    return n


def cmd_lease(a):
    """`agentctl lease list [prefix] [--all]` (debugging; the leases' holders, epochs, expiry, notes): the live ones and
    those free for under an hour; --all every one still in the table."""
    st, now_ = Store(), time.time()
    rows = st.list(a.prefix or "")
    hidden = 0 if a.all else sum(1 for r in rows if not (r["holder"] and r["expires_at"] > now_)
                                 and now_ - r["expires_at"] > LEASE_LIST_RECENT)
    if not a.all:
        rows = [r for r in rows if (r["holder"] and r["expires_at"] > now_) or now_ - r["expires_at"] <= LEASE_LIST_RECENT]
    for r in rows:
        live = r["holder"] and r["expires_at"] > now_
        meta = json.loads(r["meta"] or "{}")
        print(f"{r['name']}  {'LIVE' if live else 'free'}  {r['holder'] or '-'}  epoch {r['epoch']}  "
              f"expires in {int(r['expires_at'] - now_)} s  max {int(r['max_until'] - now_)} s"
              + (f"  -- {meta.get('note')}" if meta.get("note") else ""))
    if not rows:
        print("no leases")
    if hidden:
        print(f"({hidden} free for over {LEASE_LIST_RECENT // 60} min not shown: --all)")
    return 0
