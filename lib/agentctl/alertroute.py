# agentctl, part `alertroute`: an alert's kind and audience: which item an inbox entry is, whom it reaches (the owner, its author, a fixer), at what tier
# A real module (lib/agentctl/__init__.py MODULES): its own imports, the standard library's and MODULES' only.

import os
import re
import time
from agentctl.core import env_num
from agentctl.inboxentry import CONDITION_KINDS, INBOX_COMMANDS, inbox_kind
from agentctl.redfamilies import red_family
from agentctl.statemachines import machine_state
from agentctl.redrecord import lease_is_live, red_lease
from agentctl.owners import owner_alive


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
