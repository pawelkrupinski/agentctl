# agentctl, part `alerts`: THE ALERT MODEL -- who must act on what: the alert index (written, acked, closed, reconciled each tick) and `inbox wait`
# A real module (lib/agentctl/__init__.py MODULES): its own imports, the standard library's and MODULES' only.

import contextlib
import os
import re
import time
from typing import Any
from agentctl.core import env_num, err, hashlib_sha1
from agentctl.statefile import read_record, write_json
from agentctl.inboxentry import inbox_dir, inbox_entries, inbox_kind
from agentctl.inbox import WAIT_KINDS_DEFAULT, entry_line
from agentctl.alertroute import (
    ALERT_AUTHOR_VERDICT_KINDS, alert_audience, alert_kind_of, alert_norm, alert_route, alert_tier)
from agentctl.alertprobe import alert_probe


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
ALERT_CLOSES = {"LANDED": ("land",), "SUPERSEDED": ("land",), "OK": ("watch",), "FIX_OK": ("red",),
                "RED_CLEARED": ("red",), "FIXER_LANDED": ("red",)}
ALERT_RECONCILE_EVERY = 60  # s between reconcile passes ($AGENTCTL_ALERT_RECONCILE)
ALERT_QUIET_MAX = 24 * 3600  # s a READ alert stays its item's live one after its last event ($AGENTCTL_ALERT_QUIET_MAX)
ALERT_SEEN_MAX = 2000       # merged entry ids remembered (a re-projected event is merged once)


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
def alert_routed(rec: Any, st: Any, red: Any, condition: Any) -> Any:
    """rec with its audience, item and counters set (routed unless it names both), its tier when a red is given."""
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
    return rec


def alert_merge_ask(d: Any, idx: Any, rec: Any, t: Any) -> Any:
    """ONE ENTRY PER ASK: the same question under another item (a red's next spell) merges into the live one."""
    ask = alert_ask_digest(rec)
    if not ask:
        return
    prior = (idx.get("asks") or {}).get(ask)
    if prior and prior != rec["item"] and alert_prior_open(d, idx, prior, t):
        rec["item"] = prior
    idx.setdefault("asks", {})[ask] = rec["item"]
    idx["asks"] = {k: v for k, v in idx["asks"].items() if v in (idx.get("items") or {}) or k == ask}


def alert_prior_open(d: Any, idx: Any, item: Any, t: Any) -> Any:
    """Whether the item has a live alert nobody has acknowledged."""
    return alert_live(d, idx, item, t) is not None and not alert_acked(d, idx["items"][item])


def alert_fold(live: Any, rec: Any) -> Any:
    """rec's newest verdict, tail and level folded into the item's live alert: one more count, the last verdicts and
    merged ids kept. The merged entry's change keeps its name on it (2026-11-17 `…-ref-scrapers-2a9c4e`: its NOT
    LANDED merged into an earlier change's alert, and `agentctl inbox <its id>` found nothing)."""
    live.update({k: rec[k] for k in ("verdict", "kind", "tail", "exit", "audience", "time", "at") if k in rec})
    if rec.get("tier"):
        live["tier"] = rec["tier"]
    live["count"] = int(live.get("count") or 1) + 1
    live["verdicts"] = ((live.get("verdicts") or []) + [rec.get("verdict")])[-10:]
    live["merged"] = ((live.get("merged") or []) + [rec["id"]])[-50:]
    entries = live.get("entries") or []
    if rec.get("entry") and rec["entry"] not in [live.get("entry")] + entries:
        live["entries"] = (entries + [rec["entry"]])[-50:]


def alert_merge_into(d: Any, live: Any, rec: Any) -> Any:
    """Fold rec into the item's live alert; the owner's alert is surfaced again (its count) at a new level. Returns the
    live alert's id."""
    surface = rec["audience"] == "owner" and (live.get("audience") != "owner"
                                              or (rec.get("tier") and rec.get("tier") != live.get("tier")))
    alert_fold(live, rec)
    if surface:
        live["surfaced"] = int(live.get("surfaced") or 1) + 1
    write_json(os.path.join(d, live["id"] + ".json"), live)
    if alert_acked(d, live["id"]) and (surface or rec["audience"] == "author"):
        alert_unack(d, live["id"])  # shown again: the owner's at a new level; an author's every new verdict
    return live["id"]


def alert_write(rec, st, red=None, condition=None, d=None):
    """THE ONE WAY AN ENTRY ENTERS THE INBOX: routed (audience, item), merged into its item's live alert when there is
    one, closing what its verdict closes. Returns the id of the entry that carries it (the live one when merged)."""
    d = d or inbox_dir()
    os.makedirs(d, exist_ok=True)
    t = rec.get("at") or time.time()
    alert_routed(rec, st, red, condition)
    with alert_lock(d):
        idx = alert_index(d)
        if rec["id"] in (idx.get("seen") or []):
            return (idx.get("items") or {}).get(rec["item"]) or rec["id"]
        alert_merge_ask(d, idx, rec, t)
        kind = alert_kind_of(rec)
        alert_close_families(d, idx, rec, kind)
        alone = rec["audience"] == "info" or kind in ALERT_AUTHOR_VERDICT_KINDS  # never merged, never a live alert
        live = None if alone else alert_live(d, idx, rec["item"], t)
        carried = alert_merge_into(d, live, rec) if live else alert_file_new(d, idx, rec, alone)
        idx.setdefault("seen", []).append(rec["id"])
        alert_index_save(d, idx)
        return carried


def alert_close_families(d: Any, idx: Any, rec: Any, kind: Any) -> Any:
    """Close the alerts this kind of verdict closes (ALERT_CLOSES)."""
    for family in ALERT_CLOSES.get(kind, ()):
        alert_close_family(d, idx, rec, family, f"{kind}: {str(rec.get('verdict'))[:120]}")


def alert_file_new(d: Any, idx: Any, rec: Any, alone: Any) -> Any:
    """A new inbox entry: its file, and the item's live alert unless it stands alone. Returns its id."""
    write_json(os.path.join(d, rec["id"] + ".json"), rec)
    if not alone:
        idx.setdefault("items", {})[rec["item"]] = rec["id"]
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


def reconcile_alerts(store, force=False):
    """The processor's pass: every live alert's item probed -- cleared: closed (acked, `closed` recorded); its audience
    changed (a red nobody holds became the owner's; a self-healing condition outlived its bound): re-stamped, surfaced
    again when it became the owner's. Returns what it did."""
    d = inbox_dir()
    st = store
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


INBOX_WAIT_POLL = 1.0  # seconds between stat()s of the inbox directory ($AGENTCTL_INBOX_POLL)


def inbox_wait(kinds=None, since_ack=False, timeout=0, verbose=False, forever=False, audience="owner"):
    """THE NOTIFIER: block until an unread entry of `kinds` exists, print it (every one there is), exit 0; 2 at the
    timeout. kinds None: WAIT_KINDS_DEFAULT, and every alert addressed to the owner whatever its kind (an issue's
    escalation is OTHER, and it is the owner's to act on).
    UNREAD IS PER ENTRY (2026-11-19: issue 55799d's owner alert sat unread a day): an entry not acknowledged wakes it,
    whatever was acknowledged after it. `--since-ack` once meant "newer than the newest ack", and the orchestrator acks
    one id at a time, so every later-acked entry hid an older unread one; it is accepted and means just that now. So
    every arming prints the whole unread backlog first: a re-armed watcher (a compaction, a restart) never misses it.
    Cheap: one stat() of the inbox directory (and of acked/) a second, the entries read again only when either changed
    (a new entry is an os.replace into the directory, an ack a file in acked/). No forge, no git.
    forever: never exits; streams each such entry once, one line each, as it appears (one background task a session).
    audience (THE ALERT MODEL, alerts.py): only entries addressed to it -- the owner's by default; "all" every one. An
    alert updated in place is streamed again only when it is SURFACED again (it became the owner's, a new tier)."""
    d = inbox_dir()
    os.makedirs(d, exist_ok=True)
    owner_any = kinds is None
    want, auds = wanted_kinds(kinds), wanted_audiences(audience)
    with alert_lock(d):
        alert_index(d)  # the first use migrates the backlog into the model: no flood
    poll = env_num("AGENTCTL_INBOX_POLL", INBOX_WAIT_POLL)
    t0, seen, printed = time.time(), None, set()
    while True:
        stamp = inbox_stamp(d)
        if stamp != seen:
            seen = stamp
            hits = unread_hits(want, owner_any, auds)
            if forever:
                stream_hits(hits, printed, verbose)
            elif hits:
                return inbox_routed(hits, verbose)
        if timeout and time.time() - t0 >= timeout and not forever:
            print(f"inbox: no unread {','.join(sorted(want))} entry within {int(timeout)} s")
            return 2
        time.sleep(poll)


def wanted_kinds(kinds: Any) -> set[str]:
    """The kinds `inbox wait --kinds` names, upper-cased with `_` for `-` (WAIT_KINDS_DEFAULT when none)."""
    return {k.strip().upper().replace("-", "_") for k in (kinds or WAIT_KINDS_DEFAULT).split(",") if k.strip()}


def wanted_audiences(audience: Any) -> set[str] | None:
    """The audiences `--audience` names, None for every one."""
    return None if audience in (None, "", "all") else {x.strip() for x in str(audience).split(",") if x.strip()}


def inbox_routed(hits: Any, verbose: bool) -> int:
    """Print every unread hit, and say what to do with them: the wait's exit 0."""
    for e in hits:
        print_inbox_entry(e, verbose)
    print(f"inbox: {len(hits)} to route; then `~/bin/agentctl inbox <id> --ack` and run `inbox wait` again")
    return 0


def inbox_stamp(d: str) -> tuple[int, int] | None:
    """What changed in the inbox directory: its mtime and its acked/ directory's (None when it cannot be read)."""
    try:
        return (os.stat(d).st_mtime_ns, os.stat(os.path.join(d, "acked")).st_mtime_ns
                if os.path.isdir(os.path.join(d, "acked")) else 0)
    except OSError:
        return None


def unread_hits(want: Any, owner_any: Any, auds: Any) -> list[dict[str, Any]]:
    """The unread entries of the audiences `auds` (None: all) that are of a wanted kind -- or, when any kind is wanted
    (`owner_any`), addressed to the owner."""
    return [e for e in inbox_entries() if not e["read"]
            and (auds is None or alert_audience(e) in auds)
            and ((e.get("kind") or inbox_kind(e.get("verdict"), e.get("final", True))) in want
                 or (owner_any and alert_audience(e) == "owner"))]


def stream_hits(hits: Any, printed: Any, verbose: bool) -> None:
    """Print each hit once, as it appears (`printed`: the (id, surfaced) pairs already said)."""
    for e in hits:
        mark = (e["id"], e.get("surfaced") or 1)
        if mark not in printed:
            printed.add(mark)
            print_inbox_entry(e, verbose, flush=True)


def print_inbox_entry(e: Any, verbose: bool, flush: bool=False) -> None:
    """The entry's line, and its tail under --verbose."""
    print(entry_line(e), flush=flush)
    for line in (e.get("tail") or []) if verbose else []:
        print(f"    {line}", flush=flush)
