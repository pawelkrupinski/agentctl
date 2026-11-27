# agentctl, part `alertprobe`: whether an open alert still stands: its red, condition or gate probed, cleared when what it said is over
# A real module (lib/agentctl/__init__.py MODULES): its own imports, the standard library's and MODULES' only.

from typing import Any, Callable
import os
import re
from agentctl.core import remote
from agentctl.landstate import landed_state
from agentctl.timefmt import iso_epoch
from agentctl.statefile import read_record
from agentctl.conditions import CONDITION_KINDS
from agentctl.fixersession import TRUST_ASK_PREFIX, claude_trust_gap
from agentctl.cistate import state_path
from agentctl.statemachines import MACHINES, machine_state
from agentctl.redrecord import red_what
from agentctl.leases import red_block_of
from agentctl.conditions import open_conditions
from agentctl.redask import red_ask
from agentctl.alertroute import (
    ALERT_OWNER_KINDS, ALERT_RED_KINDS, alert_condition_audience, alert_kind_of, alert_red_audience)
from agentctl.watchknobs import knob_alert_network_grace, knob_alert_orphan_probe


# ---------------------------------------------------------------- the probes: each live alert closes itself
Probed = tuple[str | None, Any]  # (why the alert is closed or None, the audience now or None)


def _red_closed_why(st: Any, it: dict[str, Any], e: dict[str, Any]) -> str | None:
    """Why an alert of kind e's, on the open red it, is over though the red is not."""
    kind, state = alert_kind_of(e), it["state"]
    if kind == "BLOCKED" and state != "blocked-external":  # WAITING ON YOU, but its red waits on nobody now
        return f"its red is {state} now (no longer waiting on you)"  # (resumed, decided, its fix landed)
    if kind == "BLOCKED" and red_ask(it, red_block_of(it, st)) is None:
        # AN ASK IS RETRACTED WITH ITS CONDITION (2026-11-19: `decide vault-movies here`, run off the inbox and the
        # owner-decisions hook, said "no red waits on the movies vault"): its block lapsed or no longer asks the
        # owner, while the red still read blocked-external for a tick -- closed now, not when the red moves on
        return "its question is no longer asked (its block lapsed or waits on no owner action)"
    if kind in ("RED_UNCLAIMED", "RED_STARVED") and state != "offered":
        return f"its red is {state} now (taken)"
    return None


def _probe_red(st: Any, item: str, e: dict[str, Any], t: float) -> Probed:
    it = st.item("red", item[4:])
    if it is None:
        return "its red is gone", None
    if machine_state("red", it["state"]).terminal:
        return f"its red is {it['state']}", None
    why = _red_closed_why(st, it, e)
    if why:
        return why, None
    return None, (alert_red_audience(e, it, st, t) if alert_kind_of(e) in ALERT_RED_KINDS else None)


def _probe_condition(st: Any, item: str, e: dict[str, Any], t: float) -> Probed:
    key = item[len("condition:"):]
    cond = next((c for c in open_conditions(st) if c.get("key") == key or c.get("id") == key), None)
    if cond is None:
        return "its condition closed", None
    kind = alert_kind_of(e)
    if kind in CONDITION_KINDS and kind not in ALERT_OWNER_KINDS:
        return None, alert_condition_audience(kind, e, cond.get("opened_at"), t)
    return None, None


def _probe_change(st: Any, item: str, e: dict[str, Any], t: float) -> Probed:
    it = st.item("change", item[len("change:"):])
    return (f"its change is {it['state']}" if it and it["state"] in ("landed", "superseded", "cancelled") else None), None


def _probe_trust(st: Any, item: str, e: dict[str, Any], t: float) -> Probed:
    """The owner was asked to trust a clone (fixersession.py trust_asks)."""
    return ("claude trusts it now" if not claude_trust_gap(item[len(TRUST_ASK_PREFIX):]) else None), None


def _probe_worktree(st: Any, item: str, e: dict[str, Any], t: float) -> Probed | None:
    wt = item[3:].split(":", 1)[0]
    return ("its worktree is gone", None) if wt and os.path.isabs(wt) and not os.path.isdir(wt) else None


def _probe_store_item(st: Any, item: str) -> Probed | None:
    """ANY STORE ITEM WITH A STATE (2026-11-19: issue 55799d's escalation stayed unread a day after the issue was
    closed -- no probe knew `issue:`): an issue, a hold, an explorer flip, a branch red, a batch... -- its alert
    retires once the item is terminal or gone from the store."""
    typ, _, iid = item.partition(":")
    if typ not in MACHINES or not iid:
        return None
    it = st.item(typ, iid)
    if it is None:
        return f"its {typ} is gone", None
    return (f"its {typ} is {it['state']}", None) if machine_state(typ, it["state"]).terminal else None


ALERT_PROBES = (("red:", _probe_red), ("condition:", _probe_condition), ("change:", _probe_change),
                (TRUST_ASK_PREFIX, _probe_trust), ("wt:", _probe_worktree))  # by the item's prefix; None goes on


def alert_probe(st: Any, item: str, e: dict[str, Any], t: float, ctx: dict[str, Any] | None = None) -> Probed:
    """(closed why or None, the audience now or None): what the item's own state says of its live alert."""
    for prefix, probe in ALERT_PROBES:
        got = probe(st, item, e, t) if item.startswith(prefix) else None
        if got is not None:
            return got
    if item.startswith("revert-owner:"):  # a revert notice's orchestrator / escalation alert (reverts.py)
        item = "revert:" + item[len("revert-owner:"):].split("/", 1)[0]
    return _probe_store_item(st, item) or (alert_probe_cleared(st, item, e, t, ctx if ctx is not None else {}), None)


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


def red_run_numbers(d: Any) -> set[str]:
    """The run numbers (as text) red data d carries: its last run's and every run it spans."""
    return {str((d.get("last_run") or {}).get("number") or "")} | {str(r) for r in d.get("runs") or []}


def alert_red_named(d, verdict):
    """Does verdict name red d: its `decide <key>`, or `<workflow> #<run>` for ANY run the red carries -- one red item
    spans every run failing its way (red_attach_obs moves last_run on), and an alert names the run it was written
    against (review 2026-11-07: a FIX FAILED for #2128 closed as 'its red is closed' once the open red was at #2140)."""
    key = str(d.get("key") or "")
    if key and re.search(r"\bdecide " + re.escape(key) + r"\b", verdict):
        return True
    prefix = red_what(d).split(" [", 1)[0].rsplit(" #", 1)[0]  # '[<repo> ]<workflow>'
    return any(re.search(re.escape(f"{prefix} #{n}") + r"(?!\d)", verdict) for n in red_run_numbers(d) if n and n != "None")


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


class AlertClearing:
    """The condition an alert names, probed from its kind and verdict: one `applies_*` / probe pair per condition (a red,
    an orphan's worktree, a watch's gate, the network, a held batch), the first that applies deciding."""

    def __init__(self, st: Any, item: str, e: dict[str, Any], t: float, ctx: dict[str, Any]) -> None:
        self.st, self.item, self.t, self.ctx = st, item, t, ctx
        self.kind, self.verdict = alert_kind_of(e), str(e.get("verdict") or "")
        self.at = float(e.get("first_at") or e.get("at") or t)

    def applies_red(self) -> bool:
        return self.kind in ALERT_RED_PROBED and not self.item.startswith("red:")

    def red_closed(self) -> str | None:
        v = self.verdict
        if not (re.search(r" #\d+", v) or re.search(r"\bdecide \w+", v)):
            return None
        reds = alert_ctx_get(self.ctx, "reds", lambda: alert_open_reds(self.st))
        return None if any(alert_red_named(r["data"], v) for r in reds) else "its red is closed (no open red names it)"

    def applies_orphan(self) -> bool:
        return self.kind == "UNLANDED_ORPHAN" or self.item.startswith("orphan:")

    def _orphan_worktree(self) -> str | None:
        m = re.search(r"(~?/\S+)", self.item[len("orphan:"):] if self.item.startswith("orphan:") else self.verdict)
        return os.path.expanduser(m.group(1).rstrip(":,;")) if m else None

    def orphan_landed(self) -> str | None:
        wt = self._orphan_worktree()
        if wt is None:
            return None
        if not os.path.isdir(wt):
            return "its worktree is gone"
        probed = self.ctx.setdefault("probed", {})
        if self.t - float(probed.get(self.item) or 0) < knob_alert_orphan_probe():
            return None
        probed[self.item] = self.t
        state = landed_state(wt, f"refs/remotes/{remote()}/main", "HEAD")
        return "its commits are on main" if state.landed else None  # unknown (git cannot compare): not resolved

    def applies_watch(self) -> bool:
        return self.verdict.startswith("WATCH UNKNOWN") or self.item.startswith("watch:")

    def gate_decided(self) -> str | None:
        gate = alert_ctx_get(self.ctx, "gate", alert_gate)
        return "a later gate run has decided (its commit is covered)" if alert_gate_decided_after(gate, self.at) else None

    def applies_network(self) -> bool:
        return self.kind == "NETWORK"

    def network_back(self) -> str | None:
        if self.t - self.at < knob_alert_network_grace():
            return None
        conds = alert_ctx_get(self.ctx, "conditions", lambda: open_conditions(self.st))
        return None if any(c.get("kind") == "NETWORK" for c in conds) else "the network is back (no NETWORK condition open)"

    def applies_held(self) -> bool:
        return self.verdict.startswith("HELD:") or self.item.startswith("batch:")

    def _batch_named(self) -> Callable[[dict[str, Any]], bool] | None:
        """Whether a held batch is the one the alert names (by its tip or its id), None when it names none."""
        tip = re.match(r"HELD: the batch check of ([0-9a-f]{7,})", self.verdict)
        if tip:
            return lambda b: str((b["data"] or {}).get("tip") or "").startswith(tip.group(1))
        named = re.match(r"HELD: the batch (\S+) ", self.verdict)
        bid = self.item[len("batch:"):] if self.item.startswith("batch:") else (named[1] if named else None)
        return (lambda b: b["id"] == bid) if bid else None

    def batch_moved(self) -> str | None:
        held = alert_ctx_get(self.ctx, "held", lambda: self.st.items("batch", ["held"]))
        named = self._batch_named()
        if named is None:
            return None
        return None if any(named(b) for b in held) else "its batch moved on (no longer held)"

    def cleared(self) -> str | None:
        cases = ((self.applies_red, self.red_closed), (self.applies_orphan, self.orphan_landed),
                 (self.applies_watch, self.gate_decided), (self.applies_network, self.network_back),
                 (self.applies_held, self.batch_moved))
        return next((probe() for applies, probe in cases if applies()), None)


def alert_probe_cleared(st: Any, item: str, e: dict[str, Any], t: float, ctx: dict[str, Any]) -> str | None:
    """Why the alert's condition has cleared, or None (still live, or nothing to tell by)."""
    return AlertClearing(st, item, e, t, ctx).cleared()
