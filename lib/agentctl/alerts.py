# agentctl, part `alerts`: THE ALERT MODEL -- who must act on what.
# Runs inside agentctl's one namespace, after the parts before it in lib/agentctl/__init__.py's PARTS (it uses their names).

# ---------------------------------------------------------------- the alert model (the owner, 2026-11-04)
# 1524 owner-facing alerts in 7 days over 614 items: ~35 needed the owner, ~560 were real but an author's or a fixer's,
# ~930 noise ("fix root causes, make structural fixes"). THE ROOT CAUSE: alerts were emitted per EVENT, from many call
# sites (queue, slots, red, fixer, watchdog...), with no single model of WHO must act on WHAT -- so every retry, offer
# deadline, watch fan-out or tick re-alerted the owner. NOW every inbox entry goes through ONE router (alert_route) on
# its way in -- inbox_write and the event projector both call alert_write -- and:
#   1. every alert is keyed by an ITEM (a red, a change's worktree, a condition, a run an author watched...) and has an
#      AUDIENCE: `author` (the change's/worktree's session), `fixer` (the red's holder, the machinery that self-repairs),
#      `owner`, or `info` (no alert). Only owner-audience alerts reach the owner's `inbox wait`, the notifier and the
#      ESCALATED count: decisions (BLOCKED ON OWNER / WAITING ON YOU), and what nobody else can act on after a bounded
#      attempt (a red nobody holds through ALERT_RED_OWNER_LEVEL offer rounds, no family owner alive; a condition still
#      open past its bound).
#   2. ONE LIVE ALERT PER ITEM: a new event for an item with an unread alert updates that entry in place (its verdict, its
#      count, its time) instead of adding one. It is SURFACED again (shown to its audience once more) only when its
#      audience becomes the owner's or its red's escalation tier changes. It closes by itself when its item's probe
#      clears (reconcile_alerts: the red cleared or taken, the condition closed, the change landed, the worktree gone)
#      or when a closing verdict for its item arrives (LANDED, WATCH OK, FIX OK...): the self-recovering rule.
#      Reminders (1 h / 2 h / 4 h, notify_escalations) only for owner audience.
#   3. the index (inbox.alerts/index.json, beside the inbox: item -> live entry id) is written under a lock; the first use MIGRATES: every unread
#      entry is routed and stamped, and the older unread entries of one item are folded into its newest (acked, counted),
#      so the switch-over floods nobody.
ALERT_INDEX_VERSION = 1
ALERT_OWNER_KINDS = {"BLOCKED", "RED_BUDGET", "UNLANDED_ORPHAN", "ORPHAN_SUBMITTED", "REVERT_NOTICE"}  # a decision, what its bounded fixer
# left undone, recovery queueing somebody's work (its owner may need to stop it), or a revert to route (reverts.py)
ALERT_AUTHOR_KINDS = {"NOT_LANDED", "PRECHECK_REFUSED", "RED", "RED_AUTHOR", "FIX_FAILED", "FIX_NOT_COUNTED", "BRANCH_RED", "RED_REVERT",
                      "REVERTED", "RED_BLAME", "RED_RELAND", "ENDED", "USAGE", "UNKNOWN", "STALLED"}
# a change's final verdict to its author (review round 7: routed `info`, a LANDED reached no `inbox wait --audience
# author` -- 2b8927680's author waited 3000 s for it). Each is an entry of its own, under its change's id: it closes
# its worktree's live land alert and is never one itself (a later change's NOT LANDED would merge into it, over its
# id). A command's own reply (with_inbox) stays `info`: it printed it.
ALERT_AUTHOR_VERDICT_KINDS = {"LANDED", "SUPERSEDED"}
ALERT_FIXER_KINDS = {"FIXER_FAILED", "DIAGNOSED", "RED_DISPATCHED", "ORPHAN_FIXER", "LAUNCH_FAILED", "RED_FLAKE",
                     "LANDER_REVIVED", "RELANDING", "FIX_UNVERIFIED", "NETWORK"}
ALERT_RED_KINDS = {"RED_UNCLAIMED", "RED_STARVED", "RED_ESCALATE"}  # whose audience depends on who holds the red
ALERT_RED_OWNER_LEVEL = 2  # offer rounds a red goes untaken (nobody holding it, no family owner) before it is the owner's
# a condition the machinery repairs itself is the owner's only once it outlives its bound (the hysteresis's second half)
ALERT_CONDITION_OWNER_AFTER = {"INVARIANT": 1800, "STALLED": 3600, "NETWORK": 3600, "UNKNOWN": 3600}
ALERT_SELF_HEALING_INVARIANTS = {"W5", "WM", "W7"}  # heartbeat gaps: the processor/maintain/watchdog beat again
ALERT_CLOSES = {"LANDED": ("land",), "SUPERSEDED": ("land",), "OK": ("watch",), "FIX_OK": ("red",),
                "RED_CLEARED": ("red",), "FIXER_LANDED": ("red",)}
ALERT_RECONCILE_EVERY = 60  # s between reconcile passes ($AGENTCTL_ALERT_RECONCILE)
ALERT_QUIET_MAX = 24 * 3600  # s a READ alert stays its item's live one after its last event ($AGENTCTL_ALERT_QUIET_MAX)
ALERT_SEEN_MAX = 2000       # merged entry ids remembered (a re-projected event is merged once)
RUN_REF = re.compile(r"((?:[\w.-]+/)?[A-Za-z][\w .()/-]*? #\d+)")


def alert_kind_of(rec):
    return rec.get("kind") or inbox_kind(rec.get("verdict"), rec.get("final", True))


def alert_is_reply(rec):
    """A command's own verdict to the agent that ran it (with_inbox). Old entries carry no flag: a land/watch/wait
    command's crash, signal or bare exit is one."""
    if "reply" in rec:
        return bool(rec["reply"])
    return rec.get("command") in INBOX_COMMANDS and bool(re.match(r"(crashed:|exited:|ended by signal|exit \d+ with no "
                                                                  r"verdict)", str(rec.get("verdict") or "")))


def alert_run_ref(verdict):
    """The run a red-ish line names (`Main #2123`, `core Main Pipeline #699`), or None."""
    body = str(verdict or "").split(": ", 1)[-1] if ": " in str(verdict or "") else str(verdict or "")
    m = RUN_REF.search(body)
    return re.sub(r"\s+", " ", m.group(1)).strip() if m else None


def alert_item(rec):
    """The ITEM an entry is about: what keeps it to one live alert, and what its self-close probes."""
    if rec.get("alert_item"):
        return rec["alert_item"]
    kind, v = alert_kind_of(rec), str(rec.get("verdict") or "")
    if rec.get("item_type") and rec.get("item_id") and rec["item_type"] != "change":
        return f"{rec['item_type']}:{rec['item_id']}"
    cwd = rec.get("cwd") or ""
    wt = os.path.realpath(cwd) if cwd and os.path.isabs(cwd) else cwd
    if kind == "INVARIANT":
        m = re.match(r"INVARIANT (\w+)", v)
        return f"condition:invariant-{m.group(1)}" if m else "condition:invariant"
    if kind in ("NOT_LANDED", "LANDED", "SUPERSEDED") or (kind in ("WAITING", "QUEUED", "HELD") and rec.get("command") == "land"):
        return f"wt:{wt}:land" if wt else f"change:{rec.get('entry')}"
    if kind in ("RED", "OK", "STALLED") and rec.get("command") in ("land", "watch"):
        run = alert_run_ref(v) if kind == "RED" else None
        return f"wt:{wt}:watch" + (f":{run}" if run else "")
    if kind in ("ENDED", "USAGE", "UNKNOWN") and alert_is_reply(rec):
        return f"wt:{wt}:cmd:{rec.get('command')}"
    run = alert_run_ref(v) if kind not in ("OTHER",) else None
    if run and (kind.startswith(("RED", "FIX")) or kind in ALERT_FIXER_KINDS | ALERT_RED_KINDS | {"BLOCKED", "DIAGNOSED"}):
        return f"run:{run}"
    if kind == "UNLANDED_ORPHAN":
        m = re.search(r"(~?/\S+)", v)
        return f"orphan:{m.group(1).rstrip(':,;')}" if m else f"text:{alert_norm(v)}"
    return f"text:{wt}:{alert_norm(v)}"


def alert_norm(v):
    return re.sub(r"[0-9a-f]{7,40}|\d+", "N", str(v or "").splitlines()[0] if v else "")[:120]


def alert_red_audience(rec, red, st, t):
    """Who must act on a red escalation (UNCLAIMED, STARVED, ESCALATE): the fixer machinery while anyone holds the red,
    its family's owner session is alive, or it has had fewer than ALERT_RED_OWNER_LEVEL offer rounds; the owner once
    nobody can take it. ESCALATE (a deep investigation offered: a dispatch follows) is the fixers' always."""
    kind = alert_kind_of(rec)
    if red is None:
        return "fixer"  # no item to judge by (an old entry): the next escalation of a live red routes it properly
    try:
        if machine_state("red", red["state"]).terminal:
            return "info"
    except (KeyError, ValueError):
        pass
    if kind == "RED_ESCALATE":
        return "fixer"
    d = red.get("data") or {}
    try:
        if lease_is_live(st.get(red_lease(d.get("key") or red["id"])), t):
            return "fixer"
        fam = red_family(d)
        if fam and owner_alive(st, fam, t):
            return "fixer"
    except Exception:  # the store cannot tell now: the fixers', the next round asks again
        return "fixer"
    return "owner" if int(red.get("level") or 0) >= env_num("AGENTCTL_ALERT_RED_OWNER_LEVEL", ALERT_RED_OWNER_LEVEL) \
        else "fixer"


def alert_condition_audience(kind, rec, opened_at, t):
    """A condition's: the owner's at once unless the machinery repairs it itself (a heartbeat invariant, a tunnel, the
    network, an unreadable gate), then only once open past its bound."""
    v = str(rec.get("verdict") or "")
    if kind == "INVARIANT":
        m = re.match(r"INVARIANT (\w+)", v)
        if not m or m.group(1) not in ALERT_SELF_HEALING_INVARIANTS:
            return "owner"
    bound = ALERT_CONDITION_OWNER_AFTER.get(kind)
    if bound is None:
        return "owner"
    return "owner" if opened_at and t - float(opened_at) >= env_num(f"AGENTCTL_ALERT_{kind}_AFTER", bound) else "fixer"


def alert_route(rec, st=None, t=None, red=None, condition=None):
    """(audience, item) of an inbox record. st: the store to judge reds by (None: judge by the record alone);
    condition: the open condition's data when the record is a condition's entry."""
    t = time.time() if t is None else t
    kind = alert_kind_of(rec)
    item = alert_item(rec)
    if not rec.get("final", True):
        return "info", item
    if kind == "BLOCKED" or "WAITING ON YOU" in str(rec.get("verdict") or ""):
        return "owner", item
    if alert_is_reply(rec) and kind not in ALERT_OWNER_KINDS:
        return ("author" if kind in ALERT_AUTHOR_KINDS | ALERT_FIXER_KINDS | ALERT_RED_KINDS | {"INVARIANT"} else "info"), item
    if kind in ALERT_RED_KINDS:
        if red is None and st is not None and item.startswith("red:"):
            try:
                red = st.item("red", item[4:])
            except Exception:
                red = None
        return alert_red_audience(rec, red, st, t), item
    if kind in CONDITION_KINDS and kind not in ALERT_OWNER_KINDS:
        opened = (condition or {}).get("opened_at") or rec.get("first_at") or rec.get("at")
        return alert_condition_audience(kind, rec, opened, t), item
    if kind in ALERT_OWNER_KINDS:
        return "owner", item
    if kind in ALERT_AUTHOR_KINDS | ALERT_AUTHOR_VERDICT_KINDS:
        return "author", item
    if kind in ALERT_FIXER_KINDS:
        return "fixer", item
    return "info", item


def alert_audience(rec):
    """An entry's audience: its stamp, else routed by the record alone (an entry written before the model)."""
    return rec.get("audience") or alert_route(rec)[0]


def alert_tier(rec, red):
    """The escalation tier a red alert stands at (a change surfaces it again)."""
    d = (red or {}).get("data") or {}
    return f"{alert_kind_of(rec)}/{d.get('tier') or ''}"


# ---------------------------------------------------------------- the index
def alert_home(d=None):
    """Beside the inbox, never in it (its readers list *.json and every file there)."""
    return (d or inbox_dir()).rstrip("/") + ".alerts"


def alert_index_path(d=None):
    return os.path.join(alert_home(d), "index.json")


@contextlib.contextmanager
def alert_lock(d=None):
    import fcntl
    h = alert_home(d)
    os.makedirs(h, exist_ok=True)
    with open(os.path.join(h, "lock"), "a") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(f, fcntl.LOCK_UN)


def alert_index_load(d):
    idx = read_record(alert_index_path(d)) or {}
    return idx if idx.get("v") == ALERT_INDEX_VERSION else None


def alert_index_save(d, idx):
    idx["seen"] = (idx.get("seen") or [])[-ALERT_SEEN_MAX:]
    write_json(alert_index_path(d), idx)


def alert_acked(d, eid):
    return os.path.exists(os.path.join(d, "acked", eid))


def alert_ack(d, eid):
    os.makedirs(os.path.join(d, "acked"), exist_ok=True)
    open(os.path.join(d, "acked", eid), "a").close()


def alert_unack(d, eid):
    try:
        os.unlink(os.path.join(d, "acked", eid))
    except OSError:
        pass


def alert_live(d, idx, item, t=None):
    """The entry of the item's live alert, or None. Reading an alert does not end it: while its item is open (in the
    index, its last event within ALERT_QUIET_MAX) a new event updates it -- read or not -- so an owner who read RED
    UNCLAIMED is not told again by each repeat. Only its probe clearing (or a closing verdict) ends it."""
    eid = (idx.get("items") or {}).get(item)
    if not eid:
        return None
    e = read_record(os.path.join(d, eid + ".json"))
    if e is None:
        return None
    t = time.time() if t is None else t
    if alert_acked(d, eid) and t - float(e.get("at") or 0) > env_num("AGENTCTL_ALERT_QUIET_MAX", ALERT_QUIET_MAX):
        return None  # read and quiet for a day: its item's next event is news again
    return e


def alert_migrate(d):
    """THE SWITCH-OVER: every unread entry routed and stamped (audience, item); one item's older unread alerts folded
    into its newest (acked, counted in it). Returns the new index."""
    idx = {"v": ALERT_INDEX_VERSION, "items": {}, "seen": [], "migrated_at": time.time()}
    groups = {}
    for e in inbox_entries():
        if e["read"]:
            continue
        aud, item = (e.get("audience"), e.get("item")) if e.get("audience") and e.get("item") else alert_route(e)
        e = {k: v for k, v in e.items() if k != "read"}
        e.update(audience=aud, item=item)
        e.setdefault("count", 1)
        e.setdefault("surfaced", 1)
        e.setdefault("first_at", e.get("at"))
        if aud == "info":
            write_json(os.path.join(d, e["id"] + ".json"), e)
            continue
        groups.setdefault(item, []).append(e)
    folded = 0
    for item, es in groups.items():
        es.sort(key=lambda e: (e.get("at") or 0, e["id"]))
        live = es[-1]
        if len(es) > 1:
            live.update(count=sum(int(e.get("count") or 1) for e in es), first_at=es[0].get("first_at") or es[0].get("at"),
                        folded=[e["id"] for e in es[:-1]][-50:])
            for e in es[:-1]:
                alert_ack(d, e["id"])
                folded += 1
        write_json(os.path.join(d, live["id"] + ".json"), live)
        idx["items"][item] = live["id"]
    idx["folded"] = folded
    alert_index_save(d, idx)
    return idx


def alert_index(d):
    """The index, migrating the inbox the first time (call under alert_lock)."""
    idx = alert_index_load(d)
    return idx if idx is not None else alert_migrate(d)


# ---------------------------------------------------------------- the write path
def alert_write(rec, st=None, red=None, condition=None, d=None):
    """THE ONE WAY AN ENTRY ENTERS THE INBOX: routed (audience, item), merged into its item's live alert when there is
    one, closing what its verdict closes. Returns the id of the entry that carries it (the live one when merged)."""
    d = d or inbox_dir()
    os.makedirs(d, exist_ok=True)
    t = rec.get("at") or time.time()
    if not rec.get("audience") or not rec.get("item"):
        aud, item = alert_route(rec, st=st, t=t, red=red, condition=condition)
        rec.setdefault("audience", aud)
        rec.setdefault("item", item)
    rec.setdefault("first_at", rec.get("at"))
    rec.setdefault("count", 1)
    rec.setdefault("surfaced", 1)
    if red is not None:
        rec.setdefault("tier", alert_tier(rec, red))
    with alert_lock(d):
        idx = alert_index(d)
        if rec["id"] in (idx.get("seen") or []):
            return (idx.get("items") or {}).get(rec["item"]) or rec["id"]
        ask = alert_ask_digest(rec)
        if ask:  # ONE ENTRY PER ASK: the same question under another item (a red's next spell) merges into the live one
            prior = (idx.get("asks") or {}).get(ask)
            if prior and prior != rec["item"] and alert_live(d, idx, prior, t) is not None \
                    and not alert_acked(d, idx["items"][prior]):
                rec["item"] = prior
            idx.setdefault("asks", {})[ask] = rec["item"]
            idx["asks"] = {k: v for k, v in idx["asks"].items() if v in (idx.get("items") or {}) or k == ask}
        kind = alert_kind_of(rec)
        for family in ALERT_CLOSES.get(kind, ()):
            alert_close_family(d, idx, rec, family, f"{kind}: {str(rec.get('verdict'))[:120]}")
        alone = rec["audience"] == "info" or kind in ALERT_AUTHOR_VERDICT_KINDS  # never merged, never a live alert
        live = alert_live(d, idx, rec["item"], t) if not alone else None
        if live:
            surface = (rec["audience"] == "owner" and live.get("audience") != "owner") or \
                (rec["audience"] == "owner" and rec.get("tier") and rec.get("tier") != live.get("tier"))
            live.update({k: rec[k] for k in ("verdict", "kind", "tail", "exit", "audience", "time", "at") if k in rec})
            if rec.get("tier"):
                live["tier"] = rec["tier"]
            live["count"] = int(live.get("count") or 1) + 1
            live["verdicts"] = ((live.get("verdicts") or []) + [rec.get("verdict")])[-10:]
            live["merged"] = ((live.get("merged") or []) + [rec["id"]])[-50:]
            # the merged entry's change keeps its name on the live alert (2026-11-17 `…-ref-scrapers-2a9c4e`: its NOT
            # LANDED merged into an earlier change's alert, and `agentctl inbox <its id>` found nothing)
            if rec.get("entry") and rec["entry"] not in ([live.get("entry")] + (live.get("entries") or [])):
                live["entries"] = ((live.get("entries") or []) + [rec["entry"]])[-50:]
            if surface:
                live["surfaced"] = int(live.get("surfaced") or 1) + 1
            write_json(os.path.join(d, live["id"] + ".json"), live)
            if alert_acked(d, live["id"]) and (surface or rec["audience"] == "author"):
                alert_unack(d, live["id"])  # shown again: the owner's at a new level; an author's every new verdict
            idx.setdefault("seen", []).append(rec["id"])
            alert_index_save(d, idx)
            return live["id"]
        write_json(os.path.join(d, rec["id"] + ".json"), rec)
        if not alone:
            idx.setdefault("items", {})[rec["item"]] = rec["id"]
        idx.setdefault("seen", []).append(rec["id"])
        alert_index_save(d, idx)
        return rec["id"]


# THE SAME ASK, DELIVERED ONCE (the owner, 2026-11-19: one ask arrived as several unread entries). An owner question is
# keyed by its content too -- the question after WAITING ON YOU, its numbers and reminder count aside -- so a red
# re-offered into a new spell, or its ask re-raised under another item, updates the live entry instead of adding one.
ASK_TEXT = re.compile(r"WAITING ON YOU:\s*(?:REMINDER \d+:\s*)?(.*?)(?:\s+--\s|$)", re.S)


def alert_ask_digest(rec):
    """The content key of an owner ask (None for anything else)."""
    if rec.get("audience") != "owner":
        return None
    m = ASK_TEXT.search(str(rec.get("verdict") or ""))
    if not m or not m.group(1).strip():
        return None
    return hashlib_sha1(alert_norm(" ".join(m.group(1).split())))[:16]


def alert_close_family(d, idx, rec, family, why):
    """A closing verdict: its item's live alerts of that family are closed (acked, `closed` recorded)."""
    item = rec.get("item") or ""
    if family == "land":
        hits = [i for i in idx.get("items") or {} if i == item]
    elif family == "watch":
        base = item.split(":watch", 1)[0] + ":watch"
        hits = [i for i in idx.get("items") or {} if i == base or i.startswith(base + ":")]
    else:  # red: the run it names, and its red item
        hits = [i for i in idx.get("items") or {} if i == item or (rec.get("item_id") and i == f"red:{rec['item_id']}")]
    for i in hits:
        alert_close(d, idx, i, why)


def alert_close(d, idx, item, why):
    eid = (idx.get("items") or {}).pop(item, None)
    if not eid:
        return False
    p = os.path.join(d, eid + ".json")
    e = read_record(p)
    if e and not e.get("closed"):
        e["closed"] = {"at": time.time(), "why": why}
        write_json(p, e)
    alert_ack(d, eid)
    return True


def alert_close_naming(pattern, why, d=None):
    """Close every unread alert whose verdict matches `pattern` (a question answered after its red closed). Returns
    how many."""
    d = d or inbox_dir()
    n = 0
    with alert_lock(d):
        idx = alert_index(d)
        live = {eid: item for item, eid in (idx.get("items") or {}).items()}
        for e in inbox_entries():
            if e["read"] or not pattern.search(str(e.get("verdict") or "")):
                continue
            if e["id"] in live:
                alert_close(d, idx, live[e["id"]], why)
            else:
                rec = {k: v for k, v in e.items() if k != "read"}
                rec["closed"] = {"at": time.time(), "why": why}
                write_json(os.path.join(d, e["id"] + ".json"), rec)
                alert_ack(d, e["id"])
            n += 1
        alert_index_save(d, idx)
    return n


# ---------------------------------------------------------------- the probes: each live alert closes itself
def alert_probe(st, item, e, t, ctx=None):
    """(closed why or None, the audience now or None): what the item's own state says of its live alert."""
    if item.startswith("red:"):
        it = st.item("red", item[4:])
        if it is None:
            return "its red is gone", None
        if machine_state("red", it["state"]).terminal:
            return f"its red is {it['state']}", None
        kind = alert_kind_of(e)
        if kind == "BLOCKED" and it["state"] != "blocked-external":  # WAITING ON YOU, but its red waits on nobody now
            return f"its red is {it['state']} now (no longer waiting on you)", None  # (resumed, decided, its fix landed)
        if kind == "BLOCKED" and red_ask(it, red_block_of(it, st)) is None:
            # AN ASK IS RETRACTED WITH ITS CONDITION (2026-11-19: `decide vault-movies here`, run off the inbox and the
            # owner-decisions hook, said "no red waits on the movies vault"): its block lapsed or no longer asks the
            # owner, while the red still read blocked-external for a tick -- closed now, not when the red moves on
            return "its question is no longer asked (its block lapsed or waits on no owner action)", None
        if kind in ("RED_UNCLAIMED", "RED_STARVED") and it["state"] != "offered":
            return f"its red is {it['state']} now (taken)", None
        if kind in ALERT_RED_KINDS:
            return None, alert_red_audience(e, it, st, t)
        return None, None
    if item.startswith("condition:"):
        key = item[len("condition:"):]
        cond = next((c for c in open_conditions(st) if c.get("key") == key or c.get("id") == key), None)
        if cond is None:
            return "its condition closed", None
        kind = alert_kind_of(e)
        if kind in CONDITION_KINDS and kind not in ALERT_OWNER_KINDS:
            return None, alert_condition_audience(kind, e, cond.get("opened_at"), t)
        return None, None
    if item.startswith("change:"):
        it = st.item("change", item[len("change:"):])
        if it and it["state"] in ("landed", "superseded", "cancelled"):
            return f"its change is {it['state']}", None
        return None, None
    if item.startswith(TRUST_ASK_PREFIX):  # the owner was asked to trust a clone (fixer.py trust_asks)
        path = item[len(TRUST_ASK_PREFIX):]
        return ("claude trusts it now" if not claude_trust_gap(path) else None), None
    if item.startswith("wt:"):
        wt = item[3:].split(":", 1)[0]
        if wt and os.path.isabs(wt) and not os.path.isdir(wt):
            return "its worktree is gone", None
    if item.startswith("revert-owner:"):  # a revert notice's orchestrator / escalation alert (reverts.py)
        item = "revert:" + item[len("revert-owner:"):].split("/", 1)[0]
    typ, _, iid = item.partition(":")
    if typ in MACHINES and iid:
        # ANY STORE ITEM WITH A STATE (2026-11-19: issue 55799d's escalation stayed unread a day after the issue was
        # closed -- no probe knew `issue:`): an issue, a hold, an explorer flip, a branch red, a batch... -- its alert
        # retires once the item is terminal or gone from the store
        it = st.item(typ, iid)
        if it is None:
            return f"its {typ} is gone", None
        if machine_state(typ, it["state"]).terminal:
            return f"its {typ} is {it['state']}", None
    return alert_probe_cleared(st, item, e, t, ctx if ctx is not None else {}), None


# ---------------------------------------------------------------- an owner alert whose condition has cleared
# The owner, 2026-11-06: "why is there always 30 unread in inbox" -- 44 owner alerts, nearly all for what had cleared
# days before: 20 RED BLOCKED ON OWNER whose reds were closed, 17 UNLANDED ORPHAN of Forgejo-era worktrees, 3 NETWORK
# long recovered, 3 WATCH UNKNOWN whose commits later runs had decided, a HELD batch that moved on. Their items were
# keyed before the probes knew them (`run:`, `text:`, `orphan:`), so nothing ever closed them. Now each such alert's
# CONDITION is probed from its kind and verdict, whatever its item, and closed with the reason once it has cleared.
# What cannot be told (no red named, no gate read, git failing) stays as it is: never closed on a guess.
ALERT_ORPHAN_PROBE_EVERY = 600  # s between git probes of one orphan's worktree ($AGENTCTL_ALERT_ORPHAN_PROBE)
ALERT_NETWORK_GRACE = 600       # s a NETWORK alert stays before "no NETWORK condition open" closes it
ALERT_RED_PROBED = {"BLOCKED", "RED_BUDGET", "DIAGNOSED", "FIXER_FAILED", "FIX_FAILED", "FIX_UNVERIFIED",
                    "FIX_NOT_COUNTED", "RED_DISPATCHED", "LAUNCH_FAILED", "RED_FLAKE", "RED_AUTHOR", "RED_REVERT",
                    "RED_BLAME", "RED_RELAND"} | ALERT_RED_KINDS


def alert_ctx_get(ctx, name, load):
    """One read per reconcile pass of what several alerts' probes ask (the open reds, conditions, batches, the gate)."""
    if name not in ctx:
        ctx[name] = load()
    return ctx[name]


def alert_open_reds(st):
    return st.items("red", [s for s, spec in MACHINES["red"].items() if not spec.terminal])


def alert_red_named(d, verdict):
    """Does verdict name red d: its `decide <key>`, or `<workflow> #<run>` for ANY run the red carries -- one red item
    spans every run failing its way (red_attach_obs moves last_run on), and an alert names the run it was written
    against (review 2026-11-07: a FIX FAILED for #2128 closed as 'its red is closed' once the open red was at #2140)."""
    key = str(d.get("key") or "")
    if key and re.search(r"\bdecide " + re.escape(key) + r"\b", verdict):
        return True
    prefix = red_what(d).split(" [", 1)[0].rsplit(" #", 1)[0]  # '[<repo> ]<workflow>'
    nums = {str((d.get("last_run") or {}).get("number") or "")} | {str(r) for r in d.get("runs") or []}
    return any(re.search(re.escape(f"{prefix} #{n}") + r"(?!\d)", verdict) for n in nums if n and n != "None")


def alert_gate():
    return ((read_record(state_path()) or {}).get("ci") or {}).get("gate") or []


def alert_gate_decided_after(gate, at):
    """True when every gate workflow has a run decided that was created after `at` (main is linear: it covers every
    commit landed before); None when the gate snapshot (alert_gate) cannot be read."""
    if not gate:
        return None
    for g in gate:
        created = ((g.get("decided") or {}).get("createdAt")) or ""
        c = iso_epoch(created)
        if c is None:
            return False
        if c <= at:
            return False
    return True


def alert_probe_cleared(st, item, e, t, ctx):
    """Why the alert's condition has cleared, or None (still live, or nothing to tell by)."""
    kind, v = alert_kind_of(e), str(e.get("verdict") or "")
    at = float(e.get("first_at") or e.get("at") or t)
    if kind in ALERT_RED_PROBED and not item.startswith("red:"):
        if not (re.search(r" #\d+", v) or re.search(r"\bdecide \w+", v)):
            return None
        reds = alert_ctx_get(ctx, "reds", lambda: alert_open_reds(st))
        return None if any(alert_red_named(r["data"], v) for r in reds) else "its red is closed (no open red names it)"
    if kind == "UNLANDED_ORPHAN" or item.startswith("orphan:"):
        m = re.search(r"(~?/\S+)", item[len("orphan:"):] if item.startswith("orphan:") else v)
        if not m:
            return None
        wt = os.path.expanduser(m.group(1).rstrip(":,;"))
        if not os.path.isdir(wt):
            return "its worktree is gone"
        probed = ctx.setdefault("probed", {})
        if t - float(probed.get(item) or 0) < env_num("AGENTCTL_ALERT_ORPHAN_PROBE", ALERT_ORPHAN_PROBE_EVERY):
            return None
        probed[item] = t
        commits, left = cherry_landed(wt, f"refs/remotes/{remote()}/main", "HEAD")
        return "its commits are on main" if commits is not None and not left else None
    if v.startswith("WATCH UNKNOWN") or item.startswith("watch:"):
        gate = alert_ctx_get(ctx, "gate", alert_gate)
        return "a later gate run has decided (its commit is covered)" if alert_gate_decided_after(gate, at) else None
    if kind == "NETWORK":
        if t - at < env_num("AGENTCTL_ALERT_NETWORK_GRACE", ALERT_NETWORK_GRACE):
            return None
        conds = alert_ctx_get(ctx, "conditions", lambda: open_conditions(st))
        return None if any(c.get("kind") == "NETWORK" for c in conds) else "the network is back (no NETWORK condition open)"
    if v.startswith("HELD:") or item.startswith("batch:"):
        held = alert_ctx_get(ctx, "held", lambda: st.items("batch", ["held"]))
        m = re.match(r"HELD: the batch check of ([0-9a-f]{7,})", v)
        bid = item[len("batch:"):] if item.startswith("batch:") else (re.match(r"HELD: the batch (\S+) ", v) or [None, None])[1]
        if m:
            live = any(str((b["data"] or {}).get("tip") or "").startswith(m.group(1)) for b in held)
        elif bid:
            live = any(b["id"] == bid for b in held)
        else:
            return None
        return None if live else "its batch moved on (no longer held)"
    return None


def reconcile_alerts(store=None, force=False):
    """The processor's pass: every live alert's item probed -- cleared: closed (acked, `closed` recorded); its audience
    changed (a red nobody holds became the owner's; a self-healing condition outlived its bound): re-stamped, surfaced
    again when it became the owner's. Returns what it did."""
    d = inbox_dir()
    st = store or Store()
    t = time.time()
    try:
        last = float((read_record(alert_index_path(d)) or {}).get("reconciled_at") or 0)
    except (TypeError, ValueError):
        last = 0
    if not force and t - last < env_num("AGENTCTL_ALERT_RECONCILE", ALERT_RECONCILE_EVERY):
        return []
    out = []
    with alert_lock(d):
        idx = alert_index(d)
        ctx = {"probed": idx.get("probed") or {}}  # one read per pass of what several probes ask
        for item, eid in list((idx.get("items") or {}).items()):
            p = os.path.join(d, eid + ".json")
            e = read_record(p)
            if e is None or (alert_acked(d, eid) and t - float(e.get("at") or 0)
                             > env_num("AGENTCTL_ALERT_QUIET_MAX", ALERT_QUIET_MAX)):
                idx["items"].pop(item, None)  # gone, or read and quiet for a day: nothing live
                continue
            try:
                why, aud = alert_probe(st, item, e, t, ctx)
            except Exception as ex:  # cannot tell now: it stays as it is
                err(f"alerts: {item}: {str(ex)[:120]}")
                continue
            if why:
                alert_close(d, idx, item, why)
                out.append(f"closed {item}: {why}")
            elif aud and aud != e.get("audience"):
                if aud == "owner":
                    e["surfaced"] = int(e.get("surfaced") or 1) + 1
                e["audience"] = aud
                write_json(p, e)
                if aud == "owner":
                    alert_unack(d, eid)
                out.append(f"{item}: now the {aud}'s")
        idx["reconciled_at"] = t
        idx["probed"] = {k: v for k, v in ctx["probed"].items() if k in (idx.get("items") or {})}
        alert_index_save(d, idx)
    return out


def event_audience(st, ev, it, t=None):
    """The audience of a store event (the notifier's question): routed like the inbox entry it projects."""
    refs = ev.get("refs") or {}
    verdict = refs.get("verdict") or ev.get("text") or ev["kind"]
    rec = {"verdict": verdict, "kind": inbox_kind(verdict, refs.get("final", True)), "final": refs.get("final", True),
           "item_type": ev.get("item_type"), "item_id": ev.get("item_id"), "at": ev["at"], "command": refs.get("command")}
    if ev.get("item_type") == "red":
        return alert_route(rec, st=st, t=t, red=it)[0]
    if ev.get("item_type") == "condition":
        d = (it or {}).get("data") or {}
        rec["verdict"] = d.get("text") or verdict
        rec["kind"] = d.get("kind") or rec["kind"]
        return alert_route(rec, t=t, condition=d)[0] if rec["kind"] in CONDITION_KINDS else "owner"
    if ev.get("item_type") == "change":
        return "owner"  # a change stuck in the pipeline: nobody but the owner unsticks the pipeline
    aud = alert_route(rec, st=st, t=t)[0]
    return "owner" if aud == "info" else aud
