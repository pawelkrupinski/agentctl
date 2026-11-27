# agentctl, part `changekind`: THE CHANGE CLASSIFIER -- what kind of change a queued change is, decided in one place.
# A real module (lib/agentctl/__init__.py MODULES): its own imports, the standard library's and MODULES' only.

from typing import Any
from contextlib import contextmanager, suppress
from collections.abc import Iterator
from agentctl.statemachines import StaleVersion, is_terminal, machine_state, open_items
from agentctl.core import process_cache
from agentctl.redrecord import episode_unread, red_episode_names, red_names
from agentctl.issues import issue_open
from agentctl.queueslots import checker_alive
from agentctl.redland import may_land_on_red, red_kind_of
from agentctl.identitychange import identity_change
from agentctl.agewords import fmt_age


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
# wait for green, a known red, the hook's red gate -- only a fix of a GATE red (gate_fix: any name of the red, the gate's newest runs or an older run of it) does.
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


def queue_facts(gate: Any, st: Any) -> dict[str, Any]:
    """THE FACTS a change is classified against: {"gate": the gate's current red names (gate_reds), "open": {name: [red
    id, red kind]} for every red the store has open: the item's names and, for a gate red, the runs of its EPISODE (redepisode.py);
    "episode_unread": {workflow: why} of the gate's episodes that could not be read (only their newest run counts).
    An unreadable store gives no open reds: the gate's still count."""
    open_ = {}
    try:
        items = open_items(st, "red")
    except Exception:  # an unreadable store orders nothing: the gate's reds still do
        items = []
    for it in items:
        if is_terminal("red", it["state"]):
            continue
        d = it.get("data") or {}
        for n in sorted(str(x) for x in red_names(d) | red_episode_names(d) if x):
            open_.setdefault(n.lstrip("#"), [it["id"], red_kind_of(d)])
    return {"gate": {str(x) for x in gate or ()}, "open": open_, "episode_unread": dict(episode_unread)}


# THE QUEUE FACTS ARE READ ONCE PER TICK (2026-11-26; the review found them recomputed per call: every must_wait_why,
# batch order, classification and push read the store's red items and each red's episode again -- per waiting entry).
# tick_changes and tick_batch each run inside facts_tick(); facts_for(reds, st) answers from that tick's read, one per
# database and gate reading. Outside a tick (a checker, `land`, the status page, a test) every call reads afresh. Nothing
# in a queue tick moves a red item (the red tick, later in the pass, does), so the tick's facts stay true through it.
_tick_memo: dict[str, dict[Any, Any] | None] = process_cache({})


@contextmanager
def facts_tick() -> Iterator[None]:
    """The scope of one tick's facts (nested scopes share the outer one's)."""
    outer = "memo" in _tick_memo and _tick_memo["memo"] is not None
    if not outer:
        _tick_memo["memo"] = {}
    try:
        yield
    finally:
        if not outer:
            _tick_memo["memo"] = None


def facts_for(reds: Any, st: Any) -> dict[str, Any]:
    """queue_facts(reds, st), read once per tick (facts_tick); afresh outside one. Never mutate the answer: it is shared."""
    memo = _tick_memo.get("memo")
    if memo is None:
        return queue_facts(reds, st)
    key = (getattr(st, "path", None) or id(st), frozenset(str(x) for x in reds or ()))  # one database, one read
    if key not in memo:
        memo[key] = queue_facts(reds, st)
    return memo[key]


def gate_facts(gate):
    """Facts with the gate's reds alone (no store read): for the decisions only the gate makes."""
    return {"gate": {str(x) for x in gate or ()}, "open": {}}


def _batch_barrier(e: dict[str, Any], red_fix: bool) -> str | None:
    """What keeps a change out of any batch: a red fix, the entry's own `no_batch`, or its `batch_alone`; else None."""
    return ("red-fix" if red_fix else "no-batch" if e.get("no_batch")
            else f"batch-alone: {e['batch_alone']}" if e.get("batch_alone") else None)


def _backlog_barrier(e: dict[str, Any], red_fix: bool, repo: Any) -> tuple[str | None, Any]:
    """(what keeps the change out of a backlog batch, its identity answer): when nothing else does and repo is given,
    a queued change is asked whether it is an identity change (batch.identity_change, cached per entry and head)."""
    why = _batch_barrier(e, red_fix)
    if why is not None or repo is None or not e.get("id") or e.get("status") != "queued":
        return why, None
    identity = identity_change(repo, e)
    # EVERY CHANGE IS A MEMBER (2026-11-20, batch.py): an identity change joins a backlog batch, its identity area
    # in the batch's union; only one whose kind cannot be told stays out
    return ("identity-unknown" if identity is None else None), identity


def _push_barrier(e: dict[str, Any], red_fix: bool) -> str | None:
    """What keeps a change out of a push batch: the batch barriers, its `push_alone`, or a known red."""
    return _batch_barrier(e, red_fix) or (f"push-alone: {e['push_alone']}" if e.get("push_alone")
                                          else "known-red" if e.get("known_red") else None)


def _verify_due(e: dict[str, Any], t: float) -> bool:
    """Whether the entry asks for a verification, is in a state to take one and is not waiting out a retry."""
    return bool(e.get("verify")) and e.get("status") in ("queued", "checking") and not float(e.get("retry_after") or 0) > t


def classify_change(e, facts, t=None, repo=None, store=None, *, ctx):
    """THE KIND of change entry e (or an item's data) under facts (queue_facts). Its red fix and gate fix are THE
    RED-LANDING RULE's (redland.may_land_on_red): with repo, read from the entry's commits' counting trailers. repo: also
    ask whether a queued change is an identity change (batch.identity_change, cached per entry and head) when nothing
    else already keeps it out of a backlog batch; otherwise `identity` is None (unasked)."""
    t = t if t is not None else ctx.clock.time()
    landing = may_land_on_red(facts, e, repo, store=store, ctx=ctx)
    names, reds = landing.names, list(landing.reds)
    red_fix, gate_fix = landing.red_fix, landing.gate_fix
    backlog_why, identity = _backlog_barrier(e, red_fix, repo)
    push_why = _push_barrier(e, red_fix)
    verify = _verify_due(e, t)
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


def classify_queue(repo, d, entries, facts, t, st, record=True, *, ctx):
    """{entry id: kind} for entries, each recorded on its item (record_change_kind) when st is given."""
    kinds = {}
    for e in entries:
        kinds[e["id"]] = classify_change(e, facts, t, repo, st, ctx=ctx)
        if st is not None and record:
            record_change_kind(st, e, kinds[e["id"]])
    return kinds


# A CHANGE PAST ITS DEADLINE WITH NO RECORDED HOLD IS agentctl's BUG (the owner, 2026-11-19). Every legitimate wait records
# why on the change's item (note_hold: an overlap, a batch, a push batch, a verification, busy trees, the governor, a
# retry); a queued or checked change past its state's deadline with none is the queue failing to say -- the 08010c
# shape. It is filed ONCE as an agentctl issue (its maintainer's queue, never an owner page), at once by the tick that
# laps its deadline and by the watchdog's pass besides (no 2-h W1 wait), and the change is marked `unheld_issue`.
UNHELD_TAG = "[change-unheld"


def change_hold_visible(st: Any, it: Any, e: Any, t: Any) -> Any:
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


def _unheld_issue_text(tag: Any, it: Any, d: Any, t: Any) -> Any:
    k = d.get("change_kind") or {}
    return (f"{tag} change {it['id']} ({d.get('worktree')}) is {it['state']} "
            f"{fmt_age(t - float(it.get('entered_at') or t))}, past its deadline, and nothing recorded why it waits "
            f"(kind: {k.get('priority') or '?'}{'; ' + kind_text(k) if kind_text(k) else ''}). Every wait records "
            "its hold (overlapcheck.py note_hold); find the path that held it silently in a ~/bin-wt-<name> worktree, "
            "test-first in tests/queue_sim.py, and close this issue with the fix's sha. "
            f"`agentctl land status`; the change's events: `agentctl store events change {it['id']}`")


def file_unheld_change(st: Any, it: Any, t: Any, by: Any) -> Any:
    """File the agentctl issue for change item it, past its deadline with no recorded hold, once (an open one, or the
    item's `unheld_issue`, is reused). Returns the issue id."""
    d = it.get("data") or {}
    if d.get("unheld_issue"):
        return d["unheld_issue"]
    tag = f"{UNHELD_TAG} {it['id']}]"
    iid = next((i["id"] for i in st.items("issue", ("open",))
                if str((i.get("data") or {}).get("text") or "").startswith(tag)), None)
    if iid is None:
        iid = issue_open(st, _unheld_issue_text(tag, it, d, t), by, now_=t)["id"]
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
