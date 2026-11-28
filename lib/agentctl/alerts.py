# agentctl, part `alerts`: THE ALERT MODEL -- who must act on what: the alert index (written, acked, closed, reconciled each tick) and `inbox wait`
# A real module (lib/agentctl/__init__.py MODULES): its own imports, the standard library's and MODULES' only.

from agentctl.statefile import read_record, unlink_file, write_json
from agentctl.clock import epoch_now
from agentctl.filelock import FileLock
from agentctl.watchknobs import knob_alert_quiet_max, knob_alert_reconcile
from agentctl.core import attempt, print_error
from agentctl.inboxentry import inbox_dir, inbox_entries
from agentctl.alertroute import ALERT_AUTHOR_VERDICT_KINDS, alert_kind_of, alert_norm, alert_route, alert_tier
from agentctl.alertclear import alert_probe
from agentctl.texthash import hashlib_sha1
import contextlib
import os
import re
import time
from typing import Any


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
ALERT_SEEN_MAX = 2000       # merged entry ids remembered (a re-projected event is merged once)


# ---------------------------------------------------------------- the index
def alert_home(d=None):
    """Beside the inbox, never in it (its readers list *.json and every file there)."""
    return (d or inbox_dir()).rstrip("/") + ".alerts"


def alert_index_path(d=None):
    return os.path.join(alert_home(d), "index.json")


@contextlib.contextmanager
def alert_lock(d=None):
    with FileLock(os.path.join(alert_home(d), "lock")):
        yield


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
    unlink_file(os.path.join(d, "acked", eid))


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
    t = epoch_now() if t is None else t
    if alert_acked(d, eid) and t - float(e.get("at") or 0) > knob_alert_quiet_max():
        return None  # read and quiet for a day: its item's next event is news again
    return e


def _unread_by_item(d: Any) -> Any:
    """{item: [its unread entries]}, each routed and stamped (audience, item, counters); an `info` entry written as it is."""
    groups: dict[Any, list[Any]] = {}
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
    return groups


def _fold_item(d: Any, es: Any) -> Any:
    """An item's unread entries folded into the newest (the older ones acked, counted in it), written; that entry."""
    es.sort(key=lambda e: (e.get("at") or 0, e["id"]))
    live = es[-1]
    if len(es) > 1:
        live.update(count=sum(int(e.get("count") or 1) for e in es), first_at=es[0].get("first_at") or es[0].get("at"),
                    folded=[e["id"] for e in es[:-1]][-50:])
        for e in es[:-1]:
            alert_ack(d, e["id"])
    write_json(os.path.join(d, live["id"] + ".json"), live)
    return live


def alert_migrate(d):
    """THE SWITCH-OVER: every unread entry routed and stamped (audience, item); one item's older unread alerts folded
    into its newest (acked, counted in it). Returns the new index."""
    idx = {"v": ALERT_INDEX_VERSION, "items": {}, "seen": [], "migrated_at": epoch_now()}
    groups = _unread_by_item(d)
    for item, es in groups.items():
        idx["items"][item] = _fold_item(d, es)["id"]
    idx["folded"] = sum(len(es) - 1 for es in groups.values())
    alert_index_save(d, idx)
    return idx


def alert_index(d):
    """The index, migrating the inbox the first time (call under alert_lock)."""
    idx = alert_index_load(d)
    return idx if idx is not None else alert_migrate(d)


# ---------------------------------------------------------------- the write path
def alert_routed(rec: Any, st: Any, red: Any, condition: Any) -> Any:
    """rec with its audience, item and counters set (routed unless it names both), its tier when a red is given."""
    t = rec.get("at") or epoch_now()
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


def alert_write(rec: Any, st: Any, red: Any = None, condition: Any = None, d: Any = None) -> Any:
    """THE ONE WAY AN ENTRY ENTERS THE INBOX: routed (audience, item), merged into its item's live alert when there is
    one, closing what its verdict closes. Returns the id of the entry that carries it (the live one when merged)."""
    d = d or inbox_dir()
    os.makedirs(d, exist_ok=True)
    t = rec.get("at") or epoch_now()
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


def _family_item_match(family: Any, rec: Any) -> Any:
    """Whether an index item is one a closing verdict of `family` closes."""
    item = rec.get("item") or ""
    if family == "land":
        return lambda i: i == item
    if family == "watch":
        base = item.split(":watch", 1)[0] + ":watch"
        return lambda i: i == base or i.startswith(base + ":")
    red_item = f"red:{rec['item_id']}" if rec.get("item_id") else None  # red: the run it names, and its red item
    return lambda i: i == item or i == red_item


def alert_close_family(d, idx, rec, family, why):
    """A closing verdict: its item's live alerts of that family are closed (acked, `closed` recorded)."""
    match = _family_item_match(family, rec)
    for i in [i for i in idx.get("items") or {} if match(i)]:
        alert_close(d, idx, i, why)


def alert_close(d, idx, item, why):
    eid = (idx.get("items") or {}).pop(item, None)
    if not eid:
        return False
    p = os.path.join(d, eid + ".json")
    e = read_record(p)
    if e and not e.get("closed"):
        e["closed"] = {"at": epoch_now(), "why": why}
        write_json(p, e)
    alert_ack(d, eid)
    return True


def _close_entry(d: Any, idx: Any, live: Any, e: Any, why: Any) -> None:
    """Close entry e: through its index item when it has one, else by writing its record closed and acking it."""
    if e["id"] in live:
        alert_close(d, idx, live[e["id"]], why)
        return
    rec = {k: v for k, v in e.items() if k != "read"}
    rec["closed"] = {"at": epoch_now(), "why": why}
    write_json(os.path.join(d, e["id"] + ".json"), rec)
    alert_ack(d, e["id"])


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
            _close_entry(d, idx, live, e, why)
            n += 1
        alert_index_save(d, idx)
    return n


def _reconciled_at(d: str) -> float:
    """When the index was last reconciled (0 when never, or unreadable)."""
    return attempt(lambda: float((read_record(alert_index_path(d)) or {}).get("reconciled_at") or 0), 0,
                   catch=(TypeError, ValueError))


class AlertReconcile:
    """ONE PASS over the alert index: each live alert's item probed, then closed, re-stamped or left; what it did, a line
    each. ctx: one read per pass of what several probes ask."""

    def __init__(self, d: str, store: Any, t: float, idx: dict[str, Any]) -> None:
        self.d, self.store, self.t, self.idx = d, store, t, idx
        self.ctx = {"probed": idx.get("probed") or {}}

    def run(self) -> list[str]:
        lines = [self.reconcile(item, eid) for item, eid in list((self.idx.get("items") or {}).items())]
        return [line for line in lines if line]

    def gone(self, e: Any, eid: str) -> bool:
        """An alert whose entry is gone, or that was read and has been quiet for a day: nothing live."""
        return e is None or (alert_acked(self.d, eid) and self.t - float(e.get("at") or 0)
                             > knob_alert_quiet_max())

    def reconcile(self, item: str, eid: str) -> str | None:
        p = os.path.join(self.d, eid + ".json")
        e = read_record(p)
        if self.gone(e, eid):
            self.idx["items"].pop(item, None)
            return None
        try:
            why, aud = alert_probe(self.store, item, e, self.t, self.ctx)
        except Exception as ex:  # cannot tell now: it stays as it is
            print_error(f"alerts: {item}: {str(ex)[:120]}")
            return None
        if why:
            alert_close(self.d, self.idx, item, why)
            return f"closed {item}: {why}"
        if aud and aud != e.get("audience"):
            return self.restamp(p, e, eid, item, aud)
        return None

    def restamp(self, p: str, e: dict[str, Any], eid: str, item: str, aud: str) -> str:
        """The alert's audience changed: re-stamped, and surfaced again when it became the owner's."""
        if aud == "owner":
            e["surfaced"] = int(e.get("surfaced") or 1) + 1
        e["audience"] = aud
        write_json(p, e)
        if aud == "owner":
            alert_unack(self.d, eid)
        return f"{item}: now the {aud}'s"


def reconcile_alerts(store, force=False):
    """The processor's pass: every live alert's item probed -- cleared: closed (acked, `closed` recorded); its audience
    changed (a red nobody holds became the owner's; a self-healing condition outlived its bound): re-stamped, surfaced
    again when it became the owner's. Returns what it did."""
    d = inbox_dir()
    t = epoch_now()
    last = _reconciled_at(d)
    if not force and t - last < knob_alert_reconcile():
        return []
    with alert_lock(d):
        idx = alert_index(d)
        reconcile = AlertReconcile(d, store, t, idx)
        out = reconcile.run()
        idx["reconciled_at"] = t
        idx["probed"] = {k: v for k, v in reconcile.ctx["probed"].items() if k in (idx.get("items") or {})}
        alert_index_save(d, idx)
    return out


