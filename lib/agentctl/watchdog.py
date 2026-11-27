# agentctl, part `watchdog`: `agentctl watchdog`, the cheap launchd pass over the invariants (invariants.py): their conditions, the processor kicked awake.
# A real module (lib/agentctl/__init__.py MODULES): its own imports, the standard library's and MODULES' only.

from functools import partial
from agentctl.conditionregistry import ConditionRegistry
from agentctl.statefile import write_json
from agentctl.clock import epoch_now, wall_epoch
from agentctl.watchknobs import (
    W5_MAX, knob_kick_handoff_wait, knob_w5_max, knob_watchdog_misses, knob_watchdog_retell,
    knob_watchdog_sleep_gap)
from agentctl.core import attempt, pid_alive, state_root
from agentctl.repos import movies_repo_path
from agentctl.notifier import notify
from agentctl.launchd import SERVICE_LABEL, launchctl
from agentctl.beats import beat_age, beat_path, write_beat
from agentctl.waitingdir import ChangeRecords, waiting_state
from agentctl.storetoken import TOKEN_SHAPE, hand_token, processor_sock, token_request
from agentctl.conditions import close_condition, open_condition, open_conditions, run_probe
from agentctl.context import Context
from agentctl.alertroute import ALERT_SELF_HEALING_INVARIANTS
from agentctl.changekind import file_unheld_change, unheld_changes
from agentctl.invariants import INVARIANTS, probe_invariant, watchdog_state
from agentctl.agewords import fmt_age
import json
import os
import re
import time
from typing import Any, Dict


WATCHDOG_KICK_GAP = 600


def awake_since(st, now_=None):
    """When the laptop last woke, as the watchdog saw it: a pass whose own previous pass is older than
    WATCHDOG_SLEEP_GAP slept through the gap (launchd runs no job while the laptop sleeps) and starts the clock again.
    0 when the watchdog has never seen a gap (no evidence of sleep). Call before this pass's write_beat."""
    t = now_ if now_ is not None else epoch_now()
    gap = beat_age("watchdog")
    if gap is not None and gap > knob_watchdog_sleep_gap():
        return t
    return float(st.get("awake_since") or 0)


def kick_processor() -> str:
    """Kickstart the processor WITHOUT losing its store token (2026-11-15 16:58: a W5 kick left the new process
    tokenless, nothing landed for 1 h 38 min until the owner handed it over). The old processor is asked for the token
    on its socket first -- served on a thread of its own, it answers even when its loop is stuck -- then, once the old
    pid is gone, the token goes to the process launchd starts, over the same 0600 socket. In this process's memory
    only, never on disk. A processor that does not answer holds nothing to keep: the plain kick, as before."""
    path = processor_sock()
    held = attempt(lambda: token_request(path) if os.path.exists(path) else "", "", catch=(OSError,))
    old_pid = attempt(lambda: int(open(beat_path("processor")).read().split()[1]), None,
                      catch=(OSError, ValueError, IndexError))
    launchctl("kickstart", "-k", f"gui/{os.getuid()}/{SERVICE_LABEL}")
    if not TOKEN_SHAPE.fullmatch(held):
        return "it held no token"
    end = wall_epoch() + knob_kick_handoff_wait()
    while wall_epoch() < end:
        if not (old_pid and old_pid != os.getpid() and pid_alive(old_pid)):
            if hand_token(path, held) is not None:
                return "its token handed to the new processor"
        time.sleep(0.5)  # wall: waits on and times real processes and threads
    return f"the new processor did not listen within {fmt_age(knob_kick_handoff_wait())}: its token is lost"


def watchdog_unheld(st, entries=None, now_=None):
    """THE WATCHDOG'S HOLD CHECK (changekind.py, A CHANGE PAST ITS DEADLINE WITH NO RECORDED HOLD): every queued or
    checked change past its state's deadline with nothing recorded holding it is agentctl's bug, filed ONCE as an
    agentctl issue -- never an owner page, never a wait for W1's 2 h. Returns one line per change filed (or already
    filed)."""
    t = now_ if now_ is not None else st.clock()
    if entries is None:
        try:
            d, _c, _w = waiting_state(movies_repo_path())
            entries = {e["id"]: e for e in ChangeRecords(st, d).every()}
        except (Exception, SystemExit):
            entries = {}
    return [f"{it['id']} ({it['state']}, past its deadline, nothing recorded holding it): agentctl issue "
            f"{file_unheld_change(st, it, t, 'agentctl watchdog')}" for it, _e in unheld_changes(st, entries, t)]


EPISODE_ID = re.compile(r"\b\d{8}-\d{6}-[\w.-]+")


def episode_subject(detail: str) -> str:
    """What an invariant's violation is about: the first queue id it names, else its text with the numbers out."""
    m = EPISODE_ID.search(detail or "")
    return m.group(0) if m else re.sub(r"\d+(?:\.\d+)?", "#", detail or "")


def new_episode(st: Dict[str, Any], wid: str, detail: str, t: float) -> bool:
    """Whether invariant wid's violation `detail` is a new episode -- another subject, or the last telling of this one
    over WATCHDOG_RETELL ago --, recorded in the watchdog's state `st` (told: {wid: [subject, at]}) when it is."""
    told = dict(st.get("told") or {})
    subject = episode_subject(detail)
    last = told.get(wid) or [None, 0]
    if last[0] == subject and t - float(last[1]) < knob_watchdog_retell():
        return False
    told[wid] = [subject, t]
    st["told"] = told
    write_json(state_root("watchdog.json"), {**watchdog_state(), "told": told})
    return True


def tell_violation(st: Dict[str, Any], wid: str, detail: str, cond: object, heals: bool, t: float) -> None:
    """Notify invariant wid's violation as its condition opens -- not a self-healing one's (told once past its bound),
    and once per episode (new_episode)."""
    if not cond and not heals and new_episode(st, wid, detail, t):
        notify(f"agentctl watchdog: {wid}", detail[:220])


def cmd_watchdog(a: Any, clock: Any, store: Any, registry: ConditionRegistry, *, ctx: Context) -> int:
    """One watchdog pass: every invariant checked; a violation opens its INVARIANT condition (and notifies), a restored
    one closes it; W5 kickstarts the processor. Prints one line per invariant. clock and store: cli's (the invariants'
    probe is wired with the same)."""
    st = watchdog_state()
    woke = awake_since(st, clock.time())
    write_beat("watchdog")
    misses = dict(st.get("misses") or {})
    violated = [check_invariant(wid, st, misses, woke, clock, store, registry, ctx=ctx) for wid in INVARIANTS]
    print_unheld(store, clock)
    write_json(state_root("watchdog.json"), {**watchdog_state(), "misses": misses, "awake_since": woke})
    return 1 if any(violated) else 0


def check_invariant(wid: Any, st: Any, misses: Any, woke: Any, clock: Any, store: Any,
                    registry: ConditionRegistry, *, ctx: Context) -> Any:
    """One invariant probed and printed: a violation counted and told, a restored one's condition closed, a silent
    processor kicked (W5). True when it is violated."""
    r = run_probe(partial(probe_invariant, clock=clock, st=store, ctx=ctx), {"id": wid})
    key = f"invariant-{wid}"
    cond = next((c for c in open_conditions(store) if c["key"] == key), None)
    print(f"{wid}: {probe_word(r.ok)} -- {r.detail}")
    if r.ok is False:
        misses[wid] = int(misses.get(wid) or 0) + 1
        report_violation(wid, r, cond, st, misses[wid], clock, store)
        if wid == "W5":
            kick_silent_processor(st, woke, clock)
    elif r.ok:
        misses.pop(wid, None)
        if cond:
            close_condition(cond, r, store, registry)
    return r.ok is False


def probe_word(ok: Any) -> Any:
    return "ok" if ok else "cannot tell" if ok is None else "VIOLATED"


def report_violation(wid: Any, r: Any, cond: Any, st: Any, missed: Any, clock: Any, store: Any) -> Any:
    """HYSTERESIS (THE ALERT MODEL, 2026-11-04: W5/WM/W7 fired 31 times in a week on a single missed beat, each cleared
    by itself): a self-healing invariant opens its condition only at WATCHDOG_MISSES consecutive misses, and is told to
    the owner only once open past its bound (alerts.py: the notifier, ESCALATED). An open one is RE-DESCRIBED each pass
    (2026-11-16: invariant-W1.6 named a change landed 6 h before, while another one violated W1): its text, escalations
    and the ESCALATED line say what violates it now."""
    heals = wid in ALERT_SELF_HEALING_INVARIANTS
    if cond or not heals or missed >= knob_watchdog_misses():
        open_condition("INVARIANT", f"INVARIANT {wid} violated: {r.detail}", r, [], key=f"invariant-{wid}",
                       command="watchdog", store=store)
        tell_violation(st, wid, r.detail, cond, heals, clock.time())


def kick_silent_processor(st: Any, woke: Any, clock: Any) -> Any:
    """Only for a processor silent past 3 x W5_MAX of AWAKE time (a beat the laptop slept through is no silence:
    2026-11-15 16:58, a dark wake), at most every WATCHDOG_KICK_GAP; the kick keeps its token."""
    silent = min(beat_age("processor") or 0, clock.time() - woke)
    if silent > 3 * knob_w5_max() and clock.time() - float(st.get("kicked_at") or 0) >= WATCHDOG_KICK_GAP:
        st["kicked_at"] = clock.time()
        write_json(state_root("watchdog.json"), {**watchdog_state(), "kicked_at": st["kicked_at"]})
        print(f"W5: kickstarted {SERVICE_LABEL} ({kick_processor()})")


def print_unheld(store: Any, clock: Any) -> Any:
    try:
        for line in watchdog_unheld(store, now_=clock.time()):
            print(f"unheld: {line}")
    except (Exception, SystemExit) as e:  # the store unreadable: the invariants above already say so
        print(f"unheld: cannot tell ({str(e)[:160]})")
