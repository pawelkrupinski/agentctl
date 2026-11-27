# agentctl, part `changelap`: a change's regeneration hold and its lap deadline: when a hold is overdue, the lap's deadline and text
# A real module (lib/agentctl/__init__.py MODULES): its own imports, the standard library's and MODULES' only.

from contextlib import suppress
from typing import Any
from agentctl.statemachines import StaleVersion, machine_state
from agentctl.queueknobs import knob_regen_recovery_wait
from agentctl.core import fmt_age
from agentctl.causes import hold_attribution
from agentctl.waitingdir import ChangeRecords, wlog
from agentctl.queuehold import change_hold_stuck, restart_hold, stuck_recovery
from agentctl.conflicts import CONFLICT_PLACE_KEPT
from agentctl.changekind import change_hold_visible, file_unheld_change


def _regen_round(e: Any, h: Any, it: Any, t: Any, files: str, cmd: Any) -> Any:
    """The overdue hold's first recovery: a new regeneration round on the change's item. (its update, its event, the text)"""
    rnd = int((it.get("data") or {}).get("regen_round") or 0) + 1
    h["recovery"] = {"at": t, "round": rnd}
    text = (f"REGEN RECOVERY {e['id']}: held {fmt_age(t - float(h['since']))} for its regeneration of {files}; "
            f"running it again (the laptop's sync starts `{cmd}`), round {rnd}")
    return {"regen_round": rnd}, {"kind": "regen.recover", "severity": "info", "text": text}, text


def _regen_escalation(e: Any, h: Any, rec: Any, it: Any, t: Any, files: str, cmd: Any) -> Any:
    """The hold still standing a wait after its re-run: the owner's one escalation. (its update, its event, the text)"""
    answered = int((it.get("data") or {}).get("regen_answered") or 0) >= int(rec.get("round") or 1)
    rec["escalated"] = t
    text = (f"REGEN HELD {e['id']}: held {fmt_age(t - float(h['since']))}: its {files} need a regen only the "
            f"laptop runs (both sides changed a row); run again {fmt_age(t - float(rec['at']))} ago, "
            + ("the laptop started it, yet it still holds" if answered else "the laptop never answered")
            + f" -- the laptop or its JVM is unreachable, or the regen fails: run `{cmd}` there, or "
            f"`agentctl land cancel {e['id']}`")
    return {}, {"kind": "regen.escalate", "severity": "escalate", "text": text}, text


def _recovery_settled(rec: Any, t: Any, wait: Any) -> bool:
    """The recovery was escalated already, or its re-run is within the wait."""
    return bool(rec and (rec.get("escalated") or t - float(rec.get("at") or t) < wait))


def _record_regen_step(st: Any, d: Any, e: Any, it: Any, step: Any, t: Any, wait: Any) -> None:
    """The recovery's step (its update, event and text) kept on the entry and on the change's item, and said."""
    update, ev, text = step
    e["retry_after"] = t + wait
    ChangeRecords(st, d).save(e)
    with suppress(StaleVersion):  # recorded on the entry; the item catches up next tick
        if update:
            st.update_item("change", e["id"], it["version"], update)
            it = st.item("change", e["id"])
        st.transition("change", e["id"], it["state"], it["version"], event=ev)
    wlog(d, text)


def _open_change_item(st, eid):
    """The change's item unless it is missing or ended (None)."""
    it = st.item("change", eid)
    return None if not it or machine_state("change", it["state"]).terminal else it


def regen_hold_overdue(st: Any, d: Any, e: Any, t: Any) -> bool:
    """The change tick's step for regen-held entry e (above): False while within its bound (the retry decides), else
    True -- held, never checked again nor rejected -- with the recovery's round or escalation done; lines to `did`."""
    h = e.get("regen_hold") or {}
    if not h or t - float(h.get("since") or t) < CONFLICT_PLACE_KEPT:
        return False
    rec = h.get("recovery") or {}
    wait = knob_regen_recovery_wait()
    if _recovery_settled(rec, t, wait):
        return True
    it = _open_change_item(st, e["id"])
    if not it:
        return True
    files, cmd = ", ".join(h.get("files") or []), h.get("cmd")
    step = _regen_escalation(e, h, rec, it, t, files, cmd) if rec else _regen_round(e, h, it, t, files, cmd)
    e["regen_hold"] = h
    _record_regen_step(st, d, e, it, step, t, wait)
    return True


def _rederive_stuck_hold(st: Any, e: Any, state: Any, it: Any, hold: Any, stuck: Any, t: Any) -> None:
    """SELF-HEALING (2026-11-20, THE HOLD TABLE): a hold that describes a world that moved -- its blocker ended, or
    its kind's bound passed -- is dropped, and the tick decides afresh (it records the hold again if one still
    applies, its clock restarted); said quietly on the change's events, never a page
    NEVER UNHELD BETWEEN TWO READINGS (2026-11-23, W1's "nothing holds it"): kept, its clock restarted."""
    restart_hold(st, e, t)
    with suppress(StaleVersion):
        it = st.item("change", e["id"]) or it
        st.transition("change", e["id"], state, it["version"],
                      event={"kind": "change.rederived", "severity": "info", "cause": "timeout",
                             "text": f"{e['id']}: {state} for {fmt_age(t - it['entered_at'])}; its hold "
                                     f"({hold.get('why')}) was stuck -- {stuck} -- so it is decided afresh"})


def _lap_text(e: Any, state: Any, it: Any, hold: Any, stuck: Any, note: Any, issue: Any, t: Any) -> str:
    """What a change past its deadline says: its age, its hold (or that nothing holds it), why it is stuck, a note."""
    return (f"{e['id']}: {state} for {fmt_age(t - it['entered_at'])}, "
            + (f"held: {hold['why']}" if hold else "nothing holds it")
            + (f" -- STUCK: {stuck}" if stuck else "") + (f" ({note})" if note else "")
            + (f" -- filed as agentctl issue {issue}" if issue else ""))


def _overdue_change(st: Any, e: Any, state: Any, t: Any) -> Any:
    """The change's item when it is still in `state` and past its deadline, else None."""
    it = st.item("change", e["id"])
    return it if it and it["state"] == state and t > it["deadline"] else None


# A LIVE HOLD'S LAPS THIN OUT (2026-11-26, the transition analysis' item 10: 1,074 quiet `queued for N, held: ...`
# laps in 28 days, the same line every 15 min). A quiet lap's next deadline is the change's age in its state -- 15,
# 30 min, 1, 2 h -- capped at QUIET_LAP_MAX: a long wait says itself a few times, never every quarter hour. A stuck or
# unheld lap keeps the state's own deadline. On the 2026-11-26 store copy: 1,074 laps -> 387.
QUIET_LAP_MAX = 2 * 3600


def quiet_lap_after(state: str, it: dict[str, Any], t: float) -> float:
    """The seconds until a quietly held change's next lap: its age in the state, between its deadline and QUIET_LAP_MAX."""
    least = float(machine_state("change", state).deadline)
    return min(max(least, t - float(it.get("entered_at") or t)), QUIET_LAP_MAX)


def lap_change_deadline(st: Any, e: Any, state: Any, t: Any, note: Any = None) -> Any:
    """A queued or checked change past its deadline: said and re-deadlined -- quietly while its hold is live, an
    escalation when it is stuck (change_hold_stuck: nothing holds it, its blocker is gone, held too long). note: what
    else the line says (a checked change's last push)."""
    it = _overdue_change(st, e, state, t)
    if not it:
        return
    hold = change_hold_visible(st, it, e, t)
    stuck = change_hold_stuck(st, hold, it, t)
    if stuck and stuck_recovery(hold, stuck) == "rederive":
        _rederive_stuck_hold(st, e, state, it, hold, stuck, t)
        return
    issue = None if hold else file_unheld_change(st, it, t, "agentctl (the change tick)")  # agentctl's bug: an issue,
    # never an owner page (changekind.py, A CHANGE PAST ITS DEADLINE WITH NO RECORDED HOLD); a stale version: the next
    # tick says it
    quiet = bool(hold) and not stuck
    with suppress(StaleVersion):
        it = st.item("change", e["id"]) or it
        st.transition("change", e["id"], state, it["version"],
                      event={"text": _lap_text(e, state, it, hold, stuck, note, issue, t),
                             **hold_attribution(hold),  # its "cause": the hold's kind, else its deadline
                             **_lap_reach(e, quiet, stuck, issue)},
                      deadline_s=quiet_lap_after(state, it, t) if quiet else None)


def _lap_reach(e: dict[str, Any], quiet: bool, stuck: str | None, issue: str | None) -> dict[str, Any]:
    """Who a lap's event reaches and how loud: a quiet one (its hold live) no audience, else its author; info unless
    it is stuck with no issue filed."""
    # A HELD LAP IS NO INBOX ENTRY (2026-11-19 08:39Z, 08:55Z: movies-fix-replay's "queued for 15 min / 30 min, held:
    # ..." reached the inbox every 15 min -- 93 of the day's 442 entries -- because the lap's event named the worktree
    # as its audience, and the projector writes every event with one). A live hold's lap is an event with no audience:
    # `land status` and the change's events say it; only a stuck or unheld lap goes to its author.
    return {**({} if quiet else {"audience": {"worktree": e.get("worktree")}}),
            **({} if stuck and not issue else {"severity": "info"})}
