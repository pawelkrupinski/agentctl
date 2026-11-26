# agentctl, part `statemachines`: the machines (design §5): every item type's states, each with its deadline, its automatic exit and the states it may go to; a change's exits and their events
# A real module (lib/agentctl/__init__.py MODULES): its own imports, the standard library's and MODULES' only.

from typing import Any


# ---------------------------------------------------------------- the machines (design §5; phase 1 step 5)
# Every state of every machine has a finite deadline and an automatic exit (`on_deadline`, a state of the same
# machine); a deadline-only walk from any state ends in a terminal state or cycles through an escalation (the
# table-driven test). Terminal states archive after 14 d. The "red" and "condition" machines join with steps 10-12.
class StaleVersion(RuntimeError):
    """A transition's compare-and-set lost: the item moved since it was read."""


class TerminalState(RuntimeError):
    """A transition out of a terminal state."""


class IllegalTransition(RuntimeError):
    """A transition MACHINES does not list (from a state to one outside its exits): refused and recorded."""


class StateSpec:
    """A state: its deadline, the state that deadline moves it to, the other states it may go to. A deadline LOOPS BACK
    INTO ITS OWN STATE only in a WAIT state (`wait=True`: waiting on something outside the machine -- a checker slot, the
    land lease, a fixer, the gate -- re-escalating each time) and never in a state that does work: a working state
    whose deadline re-entered itself spun silently (2026-10-31: an `assembling` batch that built nothing, re-assembled
    every 5 min for an hour while three changes waited behind it). test_machines holds the rule."""
    __slots__ = ("deadline", "on_deadline", "exits", "terminal", "escalate", "wait")

    def __init__(self, deadline, on_deadline, exits=(), terminal=False, escalate=False, wait=False):
        self.deadline, self.on_deadline, self.exits = float(deadline), on_deadline, tuple(exits)
        self.terminal, self.escalate, self.wait = terminal, escalate, wait


ARCHIVE = 14 * 86400


M = 60


H = 3600


MACHINES = {
    # §5.1 a submitted change
    # THE WORLD'S EXITS (2026-11-20, when transition() began refusing what this table does not list): any open change
    # but a pushing one may end landed (its content found on main: close_landed_changes), superseded (its worktree
    # submitted again) or cancelled (its agent's `land cancel`), and any but a pushing one rejected (NOT LANDED: it no
    # longer applies, its checks failed, a refusal). A push in flight ends only by its own verdict.
    "change": {
        "queued": StateSpec(15 * M, "queued", ("checking", "superseded", "cancelled", "landed", "rejected"),
                            escalate=True, wait=True),
        "checking": StateSpec(2 * H, "queued", ("checked", "rejected", "queued", "superseded", "cancelled", "landed")),
        # checked -> checking: a push batch's member, verified with the others on the current main (batch.py)
        "checked": StateSpec(5 * M, "checked", ("pushing", "waiting", "queued", "checking", "superseded", "cancelled",
                                                "landed", "rejected"), escalate=True, wait=True),
        # waiting -> checking: a member of the backlog batch formed at green (batch.py batch_step, full depth)
        "waiting": StateSpec(6 * H, "waiting", ("landed", "rejected", "queued", "superseded", "cancelled", "checking"),
                             escalate=True, wait=True),
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
        # assembling WORKS (it builds the tip): past its deadline it is held, and assembles again after the hold
        # assembling -> pushing: every member already checked on this main (the batch's tip needs no check of its own)
        # assembling -> blocked-red: assembled while red, nothing in it may land on red now (THE BATCH IS THE MECHANISM)
        "assembling": StateSpec(300, "held", ("checking", "held", "landed", "pushing", "blocked-red"), escalate=True),
        # -> blocked-red: a waiting batch's red pre-check ends (batch.py THE BATCH IS THE MECHANISM)
        "blocked-red": StateSpec(6 * H, "blocked-red", ("assembling", "checking"), escalate=True, wait=True),
        "checking": StateSpec(2 * H, "held", ("pushing", "bisecting", "assembling", "held", "blocked-red")),
        "bisecting": StateSpec(4 * H, "held", ("pushing", "assembling", "held", "blocked-red")),
        # pushing -> held: three failed landings hold the batch with a condition (batch.py)
        "pushing": StateSpec(300, "assembling", ("landed", "assembling", "held")),
        "held": StateSpec(30 * M, "assembling", ("assembling",), escalate=True),
        "landed": StateSpec(ARCHIVE, "landed", (), terminal=True),
    },
}


# FILE-HELD STATE'S ENDS (filelog.py: the governor's tickets, the inbox, an idle sbt server, an escalation, a waiting
# entry): items no store machine holds, whose last state ends them all the same. With MACHINES, the one source of
# which states end an item (review 2026-11-26, C 1.1: six hand-kept lists disagreed -- `landed-already` in one only,
# `owner` and `closed` in another, and a type-blind set that ended a red item at its live state `red`).
FILE_HELD_ENDS = {"gov-ticket": ("released",), "inbox": ("acked",), "sbt-server": ("evicted",),
                  "escalation": ("cleared", "owner"),  # `owner`: handed to the owner (escalations.py)
                  "entry": tuple(n for n, spec in MACHINES["change"].items() if spec.terminal)}  # a change's statuses


def terminal_states(typ: str) -> tuple[str, ...]:
    """The states that end an item of type typ, in its machine's order: its machine's terminal states (MACHINES, read
    at the call: a module registers its machine when imported), else its file-held ends; () for a type with neither."""
    machine = MACHINES.get(typ)
    if machine is not None:
        return tuple(n for n, spec in machine.items() if spec.terminal)
    return FILE_HELD_ENDS.get(typ, ())


def is_terminal(typ: str, state: Any) -> bool:
    """Whether `state` ends an item of type typ (terminal_states)."""
    return state in terminal_states(typ)


def open_states(typ: Any) -> list[str]:
    """The states of machine typ that are not terminal: what an item still open can be in."""
    return [name for name, spec in MACHINES[typ].items() if not spec.terminal]


def open_items(st: Any, typ: Any) -> list[Any]:
    """THE STORE FILTERS THE OPEN ITEMS (2026-11-26: `items("change")` was 4.1 MB, 1990 changes nearly all decided, read
    whole by every status, land status and sync step over a 40 KB/s tunnel: each read took 3 min and the queue stalled).
    The states go to the store (`items(typ, states)`, which every store version takes), so only open items cross."""
    return list(st.items(typ, open_states(typ)))


def machine_state(typ: Any, state: Any) -> Any:
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


def joined_verdict(q, w, to):
    """The verdict line of queued change q that joined waiting entry w, at w's final state `to`, from w's `why` -- ONE
    builder for the change's own transition (joined_verdicts) and its projection (projected_transition), so both say
    the same; None when w has no why."""
    if not w.get("why"):
        return None
    line = str(w["why"]).splitlines()[0]
    if to == "superseded":  # its why is the bare reason: the verdict line says SUPERSEDED, as supersede's does
        line = f"SUPERSEDED by {w.get('superseded_by') or 'another entry'}: {line}"
    return f"{line} (queued {q['id']}, joined `waiting` as {w['id']})"


def projected_transition(e, w, frm, to):
    """(data, event) of a projected change transition. A joined change's terminal one carries its waiting entry's
    verdict (a batch's LANDED, a NOT LANDED) and landed sha under ITS OWN id, so a watcher on the id `land` printed
    reads them there (2026-11-16: ...-ac68b8 got only `waiting -> landed`, its batch's LANDED went to the waiting
    entry's id)."""
    verdict = joined_verdict(e, w, to) if w and machine_state("change", to).terminal else None
    if not verdict:
        return None, {"text": f"{e['id']}: {frm} -> {to}", "refs": {"entry": e["id"]}}
    sha = {"landed_sha": w["landed_sha"]} if to == "landed" and w.get("landed_sha") else {}
    at = {"pushed_at": w["pushed_at"]} if sha and w.get("pushed_at") else {}
    return ({"why": verdict, **({"tip": sha["landed_sha"]} if sha else {}), **sha, **at},
            change_event(e, verdict, code=0 if to in ("landed", "superseded") else 1, **sha))


# A CHANGE NEVER LEAVES THE QUEUE SILENTLY (2026-10-30: `…-76336e`'s NOT LANDED reached the inbox without its item id,
# so a search by id found nothing and it looked vanished; and terminal moves written without an audience -- a cancel
# through the store API, a landed-by-content close, a supersede of a stranded item, a mirrored entry's move -- reached
# nobody's inbox at all). Every terminal transition of a change (landed, rejected, superseded, cancelled) carries an
# event addressed to its author: Store.transition completes whatever the caller gave -- the audience from the item's
# worktree, refs.entry (the item id, which the projector copies into the inbox entry), a verdict line. An item with no
# worktree still gets an entry: severity warn (non-info: the projector writes it, the owner reads it).
CHANGE_EXIT_VERDICT = {"landed": "LANDED", "rejected": "NOT LANDED", "superseded": "SUPERSEDED",
                       "cancelled": "CANCELLED"}


def _exit_audience(ev: Any, data: Any) -> Any:
    """The audience of a change's terminal event: its own, completed with the change's worktree, session and pid."""
    aud = {k: v for k, v in (ev.get("audience") or {}).items() if v}
    for k in ("worktree", "session", "claude_pid"):
        if not aud.get(k) and data.get(k):
            aud[k] = data[k]
    return aud


def _exit_text(iid: Any, to: Any, data: Any) -> Any:
    """The line of a terminal change event that carries none: the verdict word, the id, why."""
    return f"{CHANGE_EXIT_VERDICT.get(to, to.upper())} {iid}" + (f": {data['why']}" if data.get("why") else "")


def change_exit_event(iid: Any, to: Any, data: Any, ev: Any) -> Any:
    """The event of a change's terminal transition to `to`, completed so that it reaches its author's inbox."""
    ev = dict(ev)
    aud = _exit_audience(ev, data)
    refs = dict(ev.get("refs") or {})
    refs.setdefault("entry", iid)
    text = ev.get("text") or refs.get("verdict") or _exit_text(iid, to, data)
    refs.setdefault("verdict", text.splitlines()[0])
    refs.setdefault("final", True)
    refs.setdefault("command", "land")
    refs.setdefault("exit", 0 if to == "landed" else 1)
    ev.update(audience=aud, refs=refs, text=text)
    if not aud.get("worktree") and (ev.get("severity") or "info") == "info":
        ev["severity"] = "warn"
    return ev


def _exit_events(store: Any) -> Any:
    """{change id: its terminal-transition events}, every one the store holds."""
    evs = {}
    for ev in store.events(after=0, limit=10 ** 9):
        if ev.get("item_type") == "change" and ev.get("to_state") in CHANGE_EXIT_VERDICT:
            evs.setdefault(ev["item_id"], []).append(ev)
    return evs


def _reaches_inbox(ev: Any, state: Any) -> Any:
    """Whether event ev is a transition to `state` that reaches an inbox (an audience worktree or a non-info severity)."""
    return ev.get("to_state") == state and bool((ev.get("audience") or {}).get("worktree") or ev["severity"] != "info")


def change_exits_unannounced(store, since=0):
    """THE INVARIANT's check: the terminal change items (entered their state at or after `since`) whose terminal
    transition has no event that reaches an inbox (an audience or a non-info severity). Empty is healthy.
    THE ITEMS ARE READ FIRST (2026-11-17 10:14 `…-ref-scrapers-2a9c4e`, 2026-11-16 11:32 `…-keeperflake-054603`: the
    events were read first, ~11k over the tunnel in 3 s, and a change that exited meanwhile was named -- its event,
    committed with its transition, came after that read): a terminal item's event is in the same transaction, so a
    later events read always holds it."""
    exits = [it for it in store.items("change") if it["state"] in CHANGE_EXIT_VERDICT and it["entered_at"] >= since]
    evs = _exit_events(store) if exits else {}
    return [it["id"] for it in exits
            if not any(_reaches_inbox(e, it["state"]) for e in evs.get(it["id"], []))]


def change_event(e, verdict, tail=(), code=1, final=True, severity="info", **refs):
    """The event a change's transition carries to its author (the projector makes it their inbox entry)."""
    return {"text": verdict.splitlines()[0], "severity": severity,
            "audience": {"worktree": e.get("worktree"), "session": e.get("session"), "claude_pid": e.get("claude_pid")},
            "refs": {"verdict": verdict.splitlines()[0], "tail": list(tail)[-25:], "exit": code, "final": final,
                     "command": "land", "entry": e["id"], **refs}}


def check_move(typ: Any, iid: Any, cur: Any, to: Any, expect_version: Any) -> Any:
    """Raise StaleVersion / TerminalState / IllegalTransition unless the item at `cur` may move to `to`."""
    if not cur or cur["version"] != expect_version:
        raise StaleVersion(f"{typ} {iid}: version {cur and cur['version']}, expected {expect_version}")
    if machine_state(typ, cur["state"]).terminal and to != cur["state"]:
        raise TerminalState(f"{typ} {iid} is {cur['state']}: terminal")
    if to != cur["state"] and to not in machine_state(typ, cur["state"]).exits:
        # THE TABLE IS THE LAW (2026-11-20): a move MACHINES does not list is refused and recorded (transition)
        raise IllegalTransition(f"{typ} {iid}: {cur['state']} -> {to} is no move of machine {typ!r} "
                                f"(its exits: {', '.join(machine_state(typ, cur['state']).exits) or 'none'})")


def level_after(cur: Any, spec: Any, to: Any, event: Any, level: Any) -> Any:
    """The level after the move: a self-transition escalates (not on severity `info`: blame.py), any other resets it."""
    up = 1 if spec.escalate and (event or {}).get("severity") != "info" else 0
    return int(level) if level is not None else cur["level"] + up if to == cur["state"] else 0
