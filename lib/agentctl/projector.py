# agentctl, part `projector`: the store's projectors, run by the server's tick: change items from the queue, inbox entries and alerts from the events, one live notification per escalation, the lease archive.
# A real module (lib/agentctl/__init__.py MODULES): its own imports, the standard library's and MODULES' only.

import datetime as dt
import json
import os
import re
import time
from typing import Any
from contextlib import suppress
from agentctl.core import attempt, env_num, err
from agentctl.timefmt import iso_at
from agentctl.notifyurl import notification_url
from agentctl.waitingdir import waiting_entries, wlog
from agentctl.procs import safe_cwd
from agentctl.inboxentry import inbox_dir, inbox_kind
from agentctl.notifier import notify, notify_cause, notify_fix
from agentctl.statemachines import ENTRY_STATE, StaleVersion, TerminalState, machine_state, projected_transition
from agentctl.store import LEASE_ARCHIVE_AFTER
from agentctl.queueslots import change_item_fields
from agentctl.redask import ASK_REMIND_MAX, ask_interval
from agentctl.alerts import alert_write
from agentctl.alertroute import event_audience


def project_changes(d, store):
    """Each queue entry's change item: created in its state, moved (a transition, with its event) when the entry's
    status moved. Returns the items."""
    st = store
    out = []
    every = {x["id"]: x for x in waiting_entries(d, open_only=False)}
    for e in every.values():
        state, w = ENTRY_STATE.get(e.get("status")), None
        if e.get("status") == "joined":  # a pre-step-8 join: the change follows the waiting entry that carries it
            w = every.get(e.get("waiting_id") or "")
            state = ENTRY_STATE.get((w or {}).get("status"), "landed" if not w else "waiting")
        if not state:
            continue
        cur = st.put_item("change", e["id"], state, {**change_item_fields(e),
                                                     "submitted_at": st.clock(),
                                                     "red_accum0": float(st.meta_get("red_accum", 0) or 0)})
        if cur["state"] != state and not machine_state("change", cur["state"]).terminal:
            data, event = projected_transition(e, w, cur["state"], state)
            with suppress(StaleVersion, TerminalState):
                st.transition("change", e["id"], state, cur["version"], data=data, event=event)
            cur = st.item("change", e["id"])
        out.append(cur)
    return out


# ---------------------------------------------------------------- events -> the inbox (design §8.2; phase 1 step 7)
# The PROJECTOR writes one inbox entry, in today's format, for each event with an audience or a severity other than
# info -- its verdict line (refs.verdict) or its text, the audience's worktree as its cwd, its tail and exit; the entry
# id carries the event's seq, so a repeated pass writes nothing twice. The NOTIFIER sends a macOS notification for
# an item's live severity=escalate event (notify_escalations: one per item, notified_at once delivered). Both run in the processor's loop; `agentctl inbox`, `inbox wait`,
# the orchestrator's routing and the status line read the inbox as before.
def _event_id(ev: dict[str, Any]) -> str:
    """The inbox entry id of an event: its time, its seq and its kind, so a repeated pass writes nothing twice."""
    return (dt.datetime.fromtimestamp(ev["at"], dt.timezone.utc).strftime("%Y%m%dT%H%M%S%f")
            + f"-ev{ev['seq']}-{re.sub(r'[^a-z]+', '-', ev['kind'])}")


def _verdict_of(ev: dict[str, Any]) -> Any:
    """The line an event tells: its verdict, else its text, else its kind."""
    return (ev.get("refs") or {}).get("verdict") or ev.get("text") or ev["kind"]


def _owner_of(aud: dict[str, Any]) -> Any:
    """Whose entry it is: the audience's session, else its worktree."""
    return aud.get("session") or aud.get("worktree") or "?"


def _inbox_record(ev: dict[str, Any], eid: str) -> dict[str, Any]:
    """The inbox entry, in today's format, of event ev (its audience, its verdict line or text, its tail and exit)."""
    aud, refs = ev.get("audience") or {}, ev.get("refs") or {}
    verdict = _verdict_of(ev)
    final = refs.get("final", True)
    rec = {"id": eid, "time": iso_at(ev["at"]),
           "at": ev["at"], "command": refs.get("command") or ev["kind"].split(".")[0], "argv": [],
           "cwd": aud.get("worktree") or safe_cwd(), "pid": os.getpid(), "final": final,
           "exit": refs.get("exit", 0 if ev["severity"] == "info" else 1), "verdict": verdict,
           "kind": inbox_kind(verdict, final), "owner": _owner_of(aud),
           "claude_pid": aud.get("claude_pid"), "verdicts": [verdict], "tail": list(refs.get("tail") or [])[-25:],
           "event": ev["seq"], "entry": refs.get("entry"), "item_type": ev.get("item_type"),
           "item_id": ev.get("item_id")}
    if refs.get("alert_item"):  # one item, several audiences (reverts.py: the lander's and the orchestrator's)
        rec["alert_item"] = refs["alert_item"]
    return rec


def _new_inbox_record(ev: dict[str, Any]) -> tuple[dict[str, Any], str] | None:
    """(the inbox entry of event ev, the inbox directory), None when it has none to write: no audience and info only (or
    no_inbox: a reminder, ask.py, is a notification alone -- the owner's ONE inbox entry for the red was written when
    told), or already written."""
    aud, refs = ev.get("audience") or {}, ev.get("refs") or {}
    if (not aud and ev["severity"] == "info") or refs.get("no_inbox"):
        return None
    eid = _event_id(ev)
    d = inbox_dir()
    if os.path.exists(os.path.join(d, eid + ".json")):
        return None
    return _inbox_record(ev, eid), d


def _write_alert(st: Any, ev: dict[str, Any], rec: dict[str, Any], d: str) -> bool:
    """THE ALERT MODEL: ev's entry routed, merged into its item's live alert. False when it could not be written."""
    red = attempt(lambda: st.item("red", ev["item_id"]) if ev.get("item_type") == "red" and ev.get("item_id") else None,
                  None, catch=(Exception,))
    try:
        alert_write(rec, st=st, red=red, d=d)
        return True
    except OSError as ex:  # the cursor stays before it: written at the next cycle, never lost
        err(f"projector: could not write {rec['id']} ({ex})")
        return False


def project_events(store: Any) -> int:
    """Write the inbox entries of the events since the projector's cursor; returns how many."""
    st = store
    cur = int(st.meta_get("projector_seq", 0) or 0)
    n = 0
    for ev in st.events(after=cur):
        new = _new_inbox_record(ev)
        if new:
            if not _write_alert(st, ev, *new):
                break
            n += 1
        cur = ev["seq"]
    st.meta_set("projector_seq", cur)
    return n


NOTIFY_LOOKBACK = 500  # events before the notifier's cursor looked at again: the window W6 reads


NOTIFY_BURST = 3  # more items than this to tell in one pass (a catch-up after a sleep or an outage): ONE summary


def escalation_stale(it, ev):
    """The escalation no longer describes its item: the item is gone, closed, in another state than the event put it
    in (a red escalated UNCLAIMED and leased since), or re-entered that state after it (a new spell). Nothing to tell."""
    if not it:
        return True
    try:
        if machine_state(ev["item_type"], it["state"]).terminal:
            return True
    except (KeyError, ValueError):
        pass
    if ev.get("to_state") and it["state"] != ev["to_state"]:
        return True
    return float(it.get("entered_at") or 0) > float(ev["at"])


def notify_failure(store):
    """{at, cause} of the notifier's last failed delivery (both channels failed), or None: invariant WN's reading."""
    try:
        return json.loads(store.meta_get("notify_failed") or "null") or None
    except ValueError:
        return None


def _escalation_line(ev):
    return (ev.get("refs") or {}).get("verdict") or ev.get("text") or ev["kind"]


class EscalationPass:
    """One pass of the notifier over W6's window of events: what to tell, what to mark untold, and what was delivered."""

    def __init__(self, st: Any) -> None:
        self.st = st
        self.cur = int(st.meta_get("notifier_seq", 0) or 0)
        self.evs = st.events(after=max(0, self.cur - NOTIFY_LOOKBACK), limit=5000)
        self.t = st.clock()
        self.told = attempt(lambda: json.loads(st.meta_get("notify_items") or "{}") or {}, {}, catch=(ValueError,))
        self.items: dict[str, Any] = {}
        self.live: dict[str, Any] = {}
        self.covered: list[Any] = []

    def key_of(self, ev: dict[str, Any]) -> str | None:
        """The key of the item (or event) ev escalates, None when it is covered: its item has moved on (escalation_stale)."""
        if not (ev.get("item_type") and ev.get("item_id")):
            return f"event:{ev['seq']}"
        k = f"{ev['item_type']}:{ev['item_id']}"
        if k not in self.items:
            self.items[k] = self.st.item(ev["item_type"], ev["item_id"])
        if escalation_stale(self.items[k], ev):
            self.covered.append(ev)
            return None
        return k

    def collect(self) -> None:
        """The newest pending escalation of each item into live, every other one into covered."""
        for ev in self.evs:
            if ev["severity"] != "escalate" or ev.get("notified_at"):
                continue
            k = self.key_of(ev)
            if k is None:
                continue
            if event_audience(self.st, ev, self.items.get(k), self.t) != "owner":  # THE ALERT MODEL: only the owner's
                self.covered.append(ev)
                continue
            if k in self.live:
                self.covered.append(self.live[k])
            self.live[k] = ev

    def is_repeat(self, k: str, ev: dict[str, Any]) -> bool:
        """ev repeats the item's last told escalation: the same kind and state, in the same spell."""
        it, prev = self.items.get(k), self.told.get(k) or {}
        return bool(prev and ev["kind"] not in ("red.ask", "red.remind") and prev.get("kind") == ev["kind"]
                    and prev.get("state") == ev.get("to_state") and it is not None
                    and abs(float(prev.get("since") or 0) - float(it.get("entered_at") or 0)) < 1e-6)

    def to_tell(self) -> list[tuple[str, dict[str, Any], bool]]:
        """(key, event, repeat) of each live escalation due: a repeat waits for its reminder schedule (ask_interval)."""
        tell = []
        for k, ev in self.live.items():
            repeat = self.is_repeat(k, ev)
            if repeat and self.t - float((self.told.get(k) or {}).get("at") or 0) < ask_interval(
                    int((self.told.get(k) or {}).get("n") or 1)):
                self.covered.append(ev)
                continue
            tell.append((k, ev, repeat))
        return tell

    def deliver(self, tell: list[Any]) -> tuple[list[Any], int]:
        """(the escalations delivered, the notifications shown): more than NOTIFY_BURST are ONE summary."""
        if len(tell) > NOTIFY_BURST:
            text = f"{len(tell)} escalations: " + "; ".join(re.sub(r"\s+--\s+`.*$", "", _escalation_line(ev))[:70]
                                                            for _k, ev, _r in tell)
            ok = notify(f"agentctl: {len(tell)} escalations (catching up)", text[:220], group="agentctl-summary")
            return (tell if ok else []), (1 if ok else 0)
        done: list[Any] = []
        for k, ev, r in tell:
            url = attempt(lambda: notification_url(ev, self.st), None, catch=(Exception,))
            if not notify(f"agentctl: {ev['kind']}", _escalation_line(ev)[:220], url=url,
                          group=f"agentctl-{ev.get('item_type')}-{ev.get('item_id')}"):
                break
            done.append((k, ev, r))
        return done, len(done)

    def record(self, done: list[Any]) -> None:
        """Mark the delivered escalations notified and remember each as its item's last told one."""
        for k, ev, r in done:
            self.st.mark_notified(ev["seq"])
            it = self.items.get(k)
            self.told[k] = {"at": self.t, "kind": ev["kind"], "state": ev.get("to_state"),
                            "since": (it or {}).get("entered_at"),
                            "n": int((self.told.get(k) or {}).get("n") or 0) + 1 if r else 1}

    def note_failure(self, tell: list[Any], done: list[Any]) -> None:
        """notify_failed: both channels failed (invariant WN reads it); cleared once nothing is left undelivered."""
        st = self.st
        if len(done) < len(tell):
            st.meta_set("notify_failed", json.dumps({"at": self.t, "cause": notify_cause(), "fix": notify_fix(),
                                                      "pending": len(tell) - len(done)}))
        elif st.meta_get("notify_failed"):
            st.meta_set("notify_failed", "")

    def run(self) -> int:
        st = self.st
        self.collect()
        tell = self.to_tell()
        for ev in self.covered:
            st.mark_notified(ev["seq"])
        shown = 0
        if tell:
            done, shown = self.deliver(tell)
            self.record(done)
            self.note_failure(tell, done)
        elif st.meta_get("notify_failed"):
            st.meta_set("notify_failed", "")  # nothing left undelivered: no failure stands
        keep = env_num("AGENTCTL_ASK_REMIND_MAX", ASK_REMIND_MAX) * 2
        told = {k: v for k, v in self.told.items() if self.t - float(v.get("at") or 0) < keep}
        st.meta_set("notify_items", json.dumps(told))
        if self.evs and self.evs[-1]["seq"] > self.cur:
            st.meta_set("notifier_seq", self.evs[-1]["seq"])
        return shown


def notify_escalations(store: Any) -> int:
    """ONE LIVE NOTIFICATION PER ITEM (2026-11-03, W6: 152 escalations "not notified" -- five reds re-escalating RED
    UNCLAIMED every 10 min for 5 h, each told on its own, 10-20 s apiece, the notifier 3 h behind and telling the owner
    "no taker" about reds held for an hour). Each pass, over W6's window (not only past the cursor: an event the cursor
    passed unmarked is still handled):
      - an escalation whose item has moved on (escalation_stale: leased, cleared, closed, a new spell) is marked, untold;
      - of one item's pending escalations only the newest is told, the older ones are marked with it;
      - a repeat (same kind and state, same spell, as the item's last told one) waits for the reminder schedule
        (ask_interval: 1 h, 2 h, 4 h, ...) -- marked without a notification before that;
      - more than NOTIFY_BURST items to tell at once: ONE summary notification for all of them;
      - an event is marked only AFTER a delivery got through (notify() True). Both channels failing leaves them unmarked
        and records notify_failed {at, cause, fix}: invariant WN raises ONE condition naming the failed channel's fix;
        W6 defers to it.
    Returns how many notifications were shown."""
    return EscalationPass(store).run()


def maybe_archive_leases(d, st, every=3600):
    """Once an hour: the leases free for over a day go to the events (Store.archive_free_leases)."""
    if time.time() - float(st.meta_get("lease_archive_at", 0) or 0) < every:
        return 0
    st.meta_set("lease_archive_at", time.time())
    n = st.archive_free_leases()
    if n:
        wlog(d, f"tick: {n} lease(s) free for over {LEASE_ARCHIVE_AFTER // 3600} h archived into the events")
    return n
