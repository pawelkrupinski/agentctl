# agentctl, part `watchdog`: `agentctl watchdog`, the cheap launchd pass over the invariants (invariants.py): their conditions, the processor kicked awake.
# A real module (lib/agentctl/__init__.py MODULES): its own imports, the standard library's and MODULES' only.

import json
import os
import time
from agentctl.core import env_num, fmt_age, pid_alive, state_root
from agentctl.statefile import write_json
from agentctl.store import open_store
from agentctl.repos import movies_repo_path
from agentctl.waitingdir import waiting_entries, waiting_state
from agentctl.storetoken import TOKEN_SHAPE, hand_token, processor_sock, token_request
from agentctl.conditions import close_condition, open_condition, open_conditions, run_probe
from agentctl.notifier import notify
from agentctl.changekind import file_unheld_change, unheld_changes
from agentctl.alerts import ALERT_SELF_HEALING_INVARIANTS
from agentctl.launchd import SERVICE_LABEL, launchctl
from agentctl.beats import beat_age, beat_path, write_beat
from agentctl.invariants import INVARIANTS, W5_MAX, watchdog_state


WATCHDOG_KICK_GAP = 600
WATCHDOG_SLEEP_GAP = 180  # s since the watchdog's own last pass (launchd: every 60 s) that mean the laptop slept
KICK_HANDOFF_WAIT = 30    # s a kick waits for launchd's new processor to listen before handing it the token


WATCHDOG_MISSES = 2  # consecutive passes a self-healing invariant (W5, WM, W7) fails before its condition opens


def awake_since(st, now_=None):
    """When the laptop last woke, as the watchdog saw it: a pass whose own previous pass is older than
    WATCHDOG_SLEEP_GAP slept through the gap (launchd runs no job while the laptop sleeps) and starts the clock again.
    0 when the watchdog has never seen a gap (no evidence of sleep). Call before this pass's write_beat."""
    t = now_ if now_ is not None else time.time()
    gap = beat_age("watchdog")
    if gap is not None and gap > env_num("AGENTCTL_WATCHDOG_SLEEP_GAP", WATCHDOG_SLEEP_GAP):
        return t
    return float(st.get("awake_since") or 0)


def kick_processor():
    """Kickstart the processor WITHOUT losing its store token (2026-11-15 16:58: a W5 kick left the new process
    tokenless, nothing landed for 1 h 38 min until the owner handed it over). The old processor is asked for the token
    on its socket first -- served on a thread of its own, it answers even when its loop is stuck -- then, once the old
    pid is gone, the token goes to the process launchd starts, over the same 0600 socket. In this process's memory
    only, never on disk. A processor that does not answer holds nothing to keep: the plain kick, as before."""
    path = processor_sock()
    try:
        held = token_request(path) if os.path.exists(path) else ""
    except OSError:
        held = ""
    try:
        old_pid = int(open(beat_path("processor")).read().split()[1])
    except (OSError, ValueError, IndexError):
        old_pid = None
    launchctl("kickstart", "-k", f"gui/{os.getuid()}/{SERVICE_LABEL}")
    if not TOKEN_SHAPE.fullmatch(held):
        return "it held no token"
    end = time.time() + env_num("AGENTCTL_KICK_HANDOFF_WAIT", KICK_HANDOFF_WAIT)
    while time.time() < end:
        if not (old_pid and old_pid != os.getpid() and pid_alive(old_pid)):
            if hand_token(path, held) is not None:
                return "its token handed to the new processor"
        time.sleep(0.5)
    return f"the new processor did not listen within {fmt_age(env_num('AGENTCTL_KICK_HANDOFF_WAIT', KICK_HANDOFF_WAIT))}: its token is lost"


def watchdog_unheld(store=None, entries=None, now_=None):
    """THE WATCHDOG'S HOLD CHECK (changekind.py, A CHANGE PAST ITS DEADLINE WITH NO RECORDED HOLD): every queued or
    checked change past its state's deadline with nothing recorded holding it is agentctl's bug, filed ONCE as an
    agentctl issue -- never an owner page, never a wait for W1's 2 h. Returns one line per change filed (or already
    filed)."""
    st = store or open_store()
    t = now_ if now_ is not None else time.time()
    if entries is None:
        try:
            d, _c, _w = waiting_state(movies_repo_path())
            entries = {e["id"]: e for e in waiting_entries(d, open_only=False)}
        except (Exception, SystemExit):
            entries = {}
    return [f"{it['id']} ({it['state']}, past its deadline, nothing recorded holding it): agentctl issue "
            f"{file_unheld_change(st, it, t, 'agentctl watchdog')}" for it, _e in unheld_changes(st, entries, t)]


def cmd_watchdog(a):
    """One watchdog pass: every invariant checked; a violation opens its INVARIANT condition (and notifies), a restored
    one closes it; W5 kickstarts the processor. Prints one line per invariant."""
    st = watchdog_state()
    woke = awake_since(st)
    write_beat("watchdog")
    rc = 0
    misses = dict(st.get("misses") or {})
    for wid in INVARIANTS:
        r = run_probe("invariant", {"id": wid})
        key = f"invariant-{wid}"
        cond = next((c for c in open_conditions() if c["key"] == key), None)
        print(f"{wid}: {'ok' if r.ok else 'cannot tell' if r.ok is None else 'VIOLATED'} -- {r.detail}")
        if r.ok is False:
            rc = 1
            misses[wid] = int(misses.get(wid) or 0) + 1
            # HYSTERESIS (THE ALERT MODEL, 2026-11-04: W5/WM/W7 fired 31 times in a week on a single missed beat, each
            # cleared by itself): a self-healing invariant opens its condition only at WATCHDOG_MISSES consecutive
            # misses, and is told to the owner only once open past its bound (alerts.py: the notifier, ESCALATED)
            heals = wid in ALERT_SELF_HEALING_INVARIANTS
            # an open one is RE-DESCRIBED each pass (2026-11-16: invariant-W1.6 named a change landed 6 h before,
            # while another one violated W1): its text, escalations and the ESCALATED line say what violates it now
            if cond or not heals or misses[wid] >= env_num("AGENTCTL_WATCHDOG_MISSES", WATCHDOG_MISSES):
                open_condition("INVARIANT", f"INVARIANT {wid} violated: {r.detail}", r, [], key=key, command="watchdog")
                if not cond and not heals:
                    notify(f"agentctl watchdog: {wid}", r.detail[:220])
            # only for a processor silent past 3 x W5_MAX of AWAKE time (a beat the laptop slept through is no
            # silence: 2026-11-15 16:58, a dark wake), at most every WATCHDOG_KICK_GAP; the kick keeps its token
            silent = min(beat_age("processor") or 0, time.time() - woke)
            if wid == "W5" and silent > 3 * env_num("AGENTCTL_W5_MAX", W5_MAX) \
                    and time.time() - float(st.get("kicked_at") or 0) >= WATCHDOG_KICK_GAP:
                st["kicked_at"] = time.time()
                write_json(state_root("watchdog.json"), {**watchdog_state(), "kicked_at": st["kicked_at"]})
                print(f"W5: kickstarted {SERVICE_LABEL} ({kick_processor()})")
        elif r.ok and cond:
            close_condition(cond, r)
        if r.ok:
            misses.pop(wid, None)
    try:
        for line in watchdog_unheld():
            print(f"unheld: {line}")
    except (Exception, SystemExit) as e:  # the store unreadable: the invariants above already say so
        print(f"unheld: cannot tell ({str(e)[:160]})")
    write_json(state_root("watchdog.json"), {**watchdog_state(), "misses": misses, "awake_since": woke})
    return rc
