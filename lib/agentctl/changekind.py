# agentctl, part `changekind`: THE CHANGE CLASSIFIER -- what kind of change a queued change is, decided in one place.
# A real module (lib/agentctl/__init__.py MODULES): its own imports, the standard library's and MODULES' only.

import time
from contextlib import suppress
from agentctl.core import fmt_age
from agentctl.redrecord import RED_TERMINAL, red_names
from agentctl.queueslots import checker_alive
from agentctl.fixchange import fixes_named
from agentctl.issues import issue_open
from agentctl.identitychange import identity_change
from agentctl.statemachines import StaleVersion, machine_state


# ---------------------------------------------------------------- ONE CLASSIFIER (the owner, 2026-11-19: "fix that structurally")
# 08010c (`Fixes-Red: 6`, Alert backtest #6, a red OUTSIDE the gate) sat ~2 h before it landed as e266788ae6: checked
# at 08:32Z, it waited behind two combined-tree verifications, was left out of push batch #47, then waited behind that
# batch's whole verify round -- `checked`, past its deadline, nothing said. c74b64f patched the one predicate that called
# it ordinary (queue.red_fix, given the gate's reds only); the STRUCTURAL fault was that "what kind of change is this"
# was decided ad hoc in a dozen places -- red_fix with the gate's reds here and every open red there, the governor's
# class (gov_class_of: the gate's alone), the batch order (`0 if e.get("fixes")`: any trailer at all), the watchdog's WR
# (the CI snapshot's gate reds), the identity-alone, --no-batch, batch_alone and push_alone flags each read where a
# decision needed them, and four parsers of the `Fixes-Red:` trailer that split its values four ways. Any one of them
# could disagree with the others, and they did.
#
# Now ONE function, classify_change, reads a change entry (or its item's data) against the QUEUE FACTS (queue_facts:
# the gate's current reds and every red the store has open, with its kind) and returns its KIND: which reds it fixes and
# each one's kind (gate / outside / flake / stuck-run), whether it fixes an open red at all (red_fix) or a gate red
# (gate_fix), whether it is an identity change, why it may not join a backlog or a push batch, whether its combined-tree
# verification runs now, and its PRIORITY class (PRIORITY, below: AGENTCTL.md's table is generated from it). The tick
# classifies every open change once per pass and records the kind on its item (`change_kind`), refreshed whenever a
# fact changes it (a red opens or closes, the gate flips, main moves a verification round in or out); every scheduling,
# batching, push, hold, governor, watchdog and status decision reads that kind. The trailer itself is parsed in ONE
# place (trailer_values, branches.py), and a change's named reds read in one (fixes_named).
#
# Where the queue ORDERS changes, a fix of ANY open red goes first (red_fix); where the GATE decides -- a change that must
# wait for green, a known red, the hook's red gate -- only a fix of the gate's reds (gate_fix) does.
PRIORITY = (  # THE ORDER the queue takes changes in, first first; within a class, queue order (seq)
    ("red-fix", "its `Fixes-Red:` names an open red (the gate's or any other the store has open): checked in the red-fix "
                "tree, pushed alone, before any batch, verification or ordinary change; never a batch member, never "
                "held behind an overlap"),
    ("verify", "its combined-tree verification round runs now: its push goes before any younger change's "
               "(THE OLDEST VERIFICATION PUSHES FIRST)"),
    ("ordinary", "every other change: batched where it may be (a backlog batch, a push batch), else checked alone"),
)
PRIORITY_RANK = {name: i for i, (name, _why) in enumerate(PRIORITY)}
CHANGE_KIND_VERSION = 1
PRIORITY_DOC_BEGIN = "<!-- change-kind priority table: generated from changekind.py PRIORITY; do not edit -->"
PRIORITY_DOC_END = "<!-- end of the change-kind priority table -->"


def priority_table_markdown():
    """AGENTCTL.md's priority table, from PRIORITY (tests/test_change_kind.py fails when the doc drifts)."""
    rows = [f"| {i + 1} | `{name}` | {why} |" for i, (name, why) in enumerate(PRIORITY)]
    return "\n".join([PRIORITY_DOC_BEGIN, "| order | class | what puts a change in it |", "|---|---|---|", *rows,
                      PRIORITY_DOC_END])


def red_kind_of(d):
    """A red item's kind as a change's classifier names it: `gate` (it holds every production push), else its own
    red_kind (`outside`, `flake`, `stuck-run`), `outside` when it records none."""
    return "gate" if d.get("gate") else str(d.get("red_kind") or "outside")


def queue_facts(gate, st):
    """THE FACTS a change is classified against: {"gate": the gate's current red names (gate_reds), "open": {name: [red
    id, red kind]} for every red the store has open}. An unreadable store gives no open reds: the gate's still count."""
    open_ = {}
    try:
        items = st.items("red")
    except Exception:  # an unreadable store orders nothing: the gate's reds still do
        items = []
    for it in items:
        if it["state"] in RED_TERMINAL:
            continue
        d = it.get("data") or {}
        for n in sorted(str(x) for x in red_names(d) if x):
            open_.setdefault(n.lstrip("#"), [it["id"], red_kind_of(d)])
    return {"gate": {str(x) for x in gate or ()}, "open": open_}


def gate_facts(gate):
    """Facts with the gate's reds alone (no store read): for the decisions only the gate makes."""
    return {"gate": {str(x) for x in gate or ()}, "open": {}}


def classify_change(e, facts, t=None, repo=None):
    """THE KIND of change entry e (or an item's data) under facts (queue_facts). repo: also ask whether a queued change
    is an identity change (batch.identity_change, cached per entry and head) when nothing else already keeps it out of
    a backlog batch; otherwise `identity` is None (unasked)."""
    t = t if t is not None else time.time()
    names = fixes_named(e)
    gate, open_ = facts.get("gate") or set(), facts.get("open") or {}
    reds = []
    for n in sorted(names):
        if n in gate or n in open_:
            rid, kind = (open_.get(n) or [None, None])[:2]
            reds.append({"name": n, "red": rid, "kind": "gate" if n in gate else kind})
    red_fix = bool(reds)
    gate_fix = bool(names & gate)
    backlog_why = ("red-fix" if red_fix else "no-batch" if e.get("no_batch")
                   else f"batch-alone: {e['batch_alone']}" if e.get("batch_alone") else None)
    identity = None
    if backlog_why is None and repo is not None and e.get("id") and e.get("status") == "queued":
        identity = identity_change(repo, e)
        # EVERY CHANGE IS A MEMBER (2026-11-20, batch.py): an identity change joins a backlog batch, its identity area
        # in the batch's union; only one whose kind cannot be told stays out
        backlog_why = "identity-unknown" if identity is None else None
    push_why = ("red-fix" if red_fix else "no-batch" if e.get("no_batch")
                else f"batch-alone: {e['batch_alone']}" if e.get("batch_alone")
                else f"push-alone: {e['push_alone']}" if e.get("push_alone")
                else "known-red" if e.get("known_red") else None)
    verify = bool(e.get("verify")) and e.get("status") in ("queued", "checking") and not float(e.get("retry_after") or 0) > t
    priority = "red-fix" if red_fix else "verify" if verify else "ordinary"
    return {"v": CHANGE_KIND_VERSION, "fixes": sorted(names), "reds": reds, "red_fix": red_fix, "gate_fix": gate_fix,
            "identity": identity, "backlog_batch": backlog_why, "push_batch": push_why, "verify": verify,
            "priority": priority, "rank": PRIORITY_RANK[priority]}


def kind_order(kinds, e):
    """The queue's sort key for entry e: its priority class (PRIORITY), then queue order."""
    return (kinds[e["id"]]["rank"] if e["id"] in kinds else PRIORITY_RANK["ordinary"], e["seq"])


def kind_text(k):
    """One line of what a change is, for `land status`: its red fixes and their kinds, else nothing."""
    if not k or not k.get("red_fix"):
        return ""
    return "red fix of " + ", ".join(f"{r['name']} ({r['kind']})" for r in k.get("reds") or [])


def record_change_kind(st, e, kind):
    """The kind on change e's item (`change_kind`), written only when it changed (a fact moved); never fails the tick."""
    try:
        it = st.item("change", e["id"])
        if not it or machine_state("change", it["state"]).terminal:
            return
        if (it.get("data") or {}).get("change_kind") == kind:
            return
        st.update_item("change", e["id"], it["version"], {"change_kind": kind})
    except StaleVersion:
        pass  # the next tick records it


def classify_queue(repo, d, entries, facts, t, st, record=True):
    """{entry id: kind} for entries, each recorded on its item (record_change_kind) when st is given."""
    kinds = {}
    for e in entries:
        kinds[e["id"]] = classify_change(e, facts, t, repo)
        if st is not None and record:
            record_change_kind(st, e, kinds[e["id"]])
    return kinds


# A CHANGE PAST ITS DEADLINE WITH NO RECORDED HOLD IS agentctl's BUG (the owner, 2026-11-19). Every legitimate wait records
# why on the change's item (note_hold: an overlap, a batch, a push batch, a verification, busy trees, the governor, a
# retry); a queued or checked change past its state's deadline with none is the queue failing to say -- the 08010c
# shape. It is filed ONCE as an agentctl issue (its maintainer's queue, never an owner page), at once by the tick that
# laps its deadline and by the watchdog's pass besides (no 2-h W1 wait), and the change is marked `unheld_issue`.
UNHELD_TAG = "[change-unheld"


def change_hold_visible(st, it, e, t):
    """What a queued or checked change waits on now, as `land status` would say it, or None: its recorded hold, its
    retry, the governor, or its executor running (a checker alive: a queued change being started)."""
    hold = change_hold_now(it, e or {}, t)
    if hold:
        return hold
    if (e or {}).get("backlog_batch"):
        return {"kind": "batch", "why": f"a member of batch {e['backlog_batch']}", "batch": e["backlog_batch"]}
    if it["state"] == "queued" and checker_alive(st, it["id"]):
        return {"kind": "starting", "why": "its executor is starting"}
    return None


def change_past_deadline(it, t):
    """Whether a queued or checked change has waited past its state's own deadline (from entering the state: the tick
    re-deadlines each lap, so the item's deadline alone never stays past)."""
    if it["state"] not in HOLD_SHOWN:
        return False
    return t - float(it.get("entered_at") or t) > machine_state("change", it["state"]).deadline


def file_unheld_change(st, it, t, by):
    """File the agentctl issue for change item it, past its deadline with no recorded hold, once (an open one, or the
    item's `unheld_issue`, is reused). Returns the issue id."""
    d = it.get("data") or {}
    if d.get("unheld_issue"):
        return d["unheld_issue"]
    tag = f"{UNHELD_TAG} {it['id']}]"
    iid = next((i["id"] for i in st.items("issue", ("open",))
                if str((i.get("data") or {}).get("text") or "").startswith(tag)), None)
    if iid is None:
        k = d.get("change_kind") or {}
        text = (f"{tag} change {it['id']} ({d.get('worktree')}) is {it['state']} "
                f"{fmt_age(t - float(it.get('entered_at') or t))}, past its deadline, and nothing recorded why it waits "
                f"(kind: {k.get('priority') or '?'}{'; ' + kind_text(k) if kind_text(k) else ''}). Every wait records "
                "its hold (queuebatch.py note_hold); find the path that held it silently in a ~/bin-wt-<name> worktree, "
                "test-first in tests/queue_sim.py, and close this issue with the fix's sha. "
                f"`agentctl land status`; the change's events: `agentctl store events change {it['id']}`")
        iid = issue_open(st, text, by, now_=t)["id"]
    with suppress(StaleVersion, Exception):
        cur = st.item("change", it["id"])
        if cur and not machine_state("change", cur["state"]).terminal:
            st.update_item("change", it["id"], cur["version"], {"unheld_issue": iid})
    return iid


def unheld_changes(st, entries, t):
    """[(item, entry)] of the open queued or checked changes past their deadline with no visible hold."""
    out = []
    for it in st.items("change", list(HOLD_SHOWN)):
        e = entries.get(it["id"]) or {}
        if change_past_deadline(it, t) and not change_hold_visible(st, it, e, t):
            out.append((it, e))
    return out


HOLD_SHOWN = ("queued", "checked")  # the states whose item's `hold` says why it waits (a check, a push)


def change_hold_now(it, e, t):
    """The hold a queued change waits on now: its recorded one, its retry, or the governor's slot; None: nothing."""
    if e.get("retry_after", 0) > t:
        return {"kind": "retry", "why": "its retry (an infra failure of its last check)"}
    if e.get("waiting_on"):
        return {"kind": "governor", "why": f"waiting on {e['waiting_on']}"}
    return (it.get("data") or {}).get("hold")
