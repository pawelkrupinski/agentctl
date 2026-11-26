# agentctl, part `orphanreap`: orphaned work reaped: a red's holder no longer at work loses its leases, an idle agentctl-launched session is stopped
# A real module (lib/agentctl/__init__.py MODULES): its own imports, the standard library's and MODULES' only.

from typing import Any
import glob
import os
from agentctl.configfile import env_num
from agentctl.core import fmt_age, on_ci_runner, pid_alive, reds_dir
from agentctl.clock import wall_epoch
from agentctl.statefile import mtime, read_record
from agentctl.procs import record_alive, socket_host
from agentctl.fixerrecords import fixer_records
from agentctl.inboxentry import inbox_write
from agentctl.fixersession import bg_rows, bg_session, bg_stop, bg_turn_over, bg_waiting
from agentctl.dispatchblock import claude_cli
from agentctl.redrecord import lease_is_live
from agentctl.leases import lease_meta
from agentctl.owners import owner_record_path
from agentctl.redmove import tail_lines
from agentctl.configfile import knob_red_orphan_grace, knob_red_stall


def _wrapper_verdict(rec: Any, pid: Any, holder_pid: Any) -> Any:
    """(at work?, why) when the owner record shows the wrapper itself gone or never the lease's own; None when it lives."""
    if holder_pid and str(holder_pid) != str(pid):
        # THE RECORD IS ANOTHER SPELL'S (2026-11-15 11:34:52, Order independence #74 and Main #2475): the leases of
        # two live wrappers in their turns (outside-gate pid 37262, main-tests) were revoked "ORPHANED ... its wrapper
        # ended (IDLE: no red of <family> for 6 h)" -- the verdicts of the previous spells (main-tests' ended
        # 2026-11-14), read from records that were not the holders'. A lease is judged by the wrapper it names.
        if pid_alive(int(holder_pid)):
            return None, f"its wrapper (pid {holder_pid}) lives; the owner record here is another spell's (pid {pid})"
        return False, f"its wrapper (pid {holder_pid}) is gone"
    if rec.get("ended"):
        return False, f"its wrapper ended ({str(rec.get('verdict') or '?')[:160]})"
    if not record_alive(rec):
        return False, f"its wrapper (pid {pid}) is gone"
    return None


def _idle(t: float, stamped: float | None, path: str | None) -> float:
    """Seconds without progress: the age of the newest of a stamp the store or a record made (`t`'s clock, the process's)
    and the file's mtime (the wall's: a file's time is real time, never the injected clock's)."""
    written = mtime(path)
    ages = ([t - float(stamped)] if stamped else []) + ([wall_epoch() - written] if written else [])
    return min(ages) if ages else t


def _session_verdict(rec: Any, pid: Any, t: Any, idle: Any, grace: Any) -> Any:
    """(at work?, why) when the background session is unreadable, or over while the wrapper still holds the turn; else None."""
    bid = rec.get("bg_id")
    if not bid:
        return None
    ent = bg_session(claude_cli(), bid)
    if ent == {}:
        return None, "`claude agents` unread"
    if (ent is None or bg_turn_over(ent, rec.get("transcript"))) and idle > grace:
        return False, (f"its session {bid} is {(ent or {}).get('state') or 'gone'} while its wrapper (pid {pid}) "
                       f"still holds the turn, {fmt_age(idle)} without progress")
    return None


def _turn_verdict(rec: Any, pid: Any, t: Any) -> Any:
    """(at work?, why) of an owner in its turn: its session, then the transcript's progress."""
    grace = knob_red_orphan_grace()
    idle = _idle(t, rec.get("progress_at"), rec.get("transcript"))
    verdict = _session_verdict(rec, pid, t, idle, grace)
    if verdict:
        return verdict
    if idle > knob_red_stall() + grace:
        return False, f"turn {rec.get('turn')}: no progress (transcript) for {fmt_age(idle)}"
    return True, f"turn {rec.get('turn')} at work"


def owner_work(fam: Any, t: Any, holder_pid: Any = None) -> Any:
    """(True|False|None, why): is family fam's owner at work? None when this machine cannot tell. holder_pid: the
    wrapper the lease itself names (its meta's pid): a record of another spell judges nothing of it."""
    rec = read_record(owner_record_path(fam)) or {}
    pid = rec.get("pid")
    if not pid:
        return None, "no owner record on this machine"
    verdict = _wrapper_verdict(rec, pid, holder_pid)
    if verdict:
        return verdict
    if not rec.get("in_turn", bool(rec.get("bg_id"))):  # older records have no in_turn: a recorded session is a turn
        return True, "between turns"
    return _turn_verdict(rec, pid, t)


def wrapper_last_words(m, n: int = 3):
    """The last lines a fixer's wrapper wrote to its stderr (its lease meta's wrapper_log), joined; "" when none."""
    path = m.get("wrapper_log")
    lines = [x.strip() for x in tail_lines(path, 40)] if path and os.path.isfile(path) else []
    return " | ".join([x for x in lines if x][-n:])[:600]


def _wrapper_gone(m: Any) -> Any:
    """Why a dispatched fixer's wrapper is gone (its pid dead; its last words), or None."""
    pid = m.get("pid")
    if not pid or record_alive(m):
        return None
    words = wrapper_last_words(m)
    return f"its wrapper (pid {pid}) is gone" + (f"; its last words: {words}" if words else "")


def _session_ended(row: Any, t: Any, m: Any, bid: Any, ent: Any) -> Any:
    """Why a dispatched fixer's bg session `bid` (entry ent) is over -- gone or its turn ended, no progress for
    RED_ORPHAN_GRACE -- or None."""
    idle = _idle(t, row.get("renewed_at") or row.get("acquired_at"), m.get("log"))
    if (ent is None or bg_turn_over(ent, m.get("log"))) and idle > knob_red_orphan_grace():
        return f"its session {bid} is {(ent or {}).get('state') or 'gone'}, {fmt_age(idle)} without progress"
    return None


def fixer_work(row, t):
    """(True|False|None, why): is a dispatched fixer's lease held by work? Its wrapper alive, its session not ended
    (beyond RED_ORPHAN_GRACE since the lease's last progress). A dead wrapper's why carries its last words (stderr)."""
    m = lease_meta(row)
    gone = _wrapper_gone(m)
    if gone:
        return False, gone
    bid = m.get("bg_id")
    if not bid:
        return None, "its session is starting"
    ent = bg_session(claude_cli(), bid)
    if ent == {}:
        return None, "`claude agents` unread"
    ended = _session_ended(row, t, m, bid, ent)
    if ended:
        return False, ended
    return True, "at work"


def _judged_elsewhere(m: Any) -> Any:
    """Why a laptop lease's holder is not this machine's to judge (its host, its worktree), or None."""
    # A PID IS JUDGED ONLY WHERE IT RUNS (2026-11-18 23:42:21Z, core Main Pipeline #1053): a fixer another machine
    # signed in as the laptop dispatched lost its lease 53 s after it was taken -- this tick found no pid 83138 among
    # its own processes. Every "laptop:" holder names its host in its meta; an older wrapper's (no host) is ours only
    # when its worktree is here.
    if m.get("host") and m["host"] != socket_host():
        return f"held on {m['host']}, judged there"
    if not m.get("host") and m.get("worktree") and m.get("kind") == "fixer" and not os.path.isdir(m["worktree"]):
        return "its worktree is not on this machine"
    return None


def _cached_owner_work(row: Any, m: Any, t: Any, cache: Any) -> Any:
    """owner_work of the lease's family, judged once per family and pid in `cache`."""
    fam = m.get("family") or row["name"].split("/", 1)[1]
    k = ("owner", fam, m.get("pid"))
    if k not in cache:
        cache[k] = owner_work(fam, t, m.get("pid"))
    return cache[k]


def holder_work(row, t, cache = None):
    """(True|False|None, why) for a live lease's holder: an owner's (its heartbeat or a red it holds) or a dispatched
    fixer's. None for what this machine cannot judge: a claim (renewed by its worktree's activity), a sibling (lives
    with its primary), a holder on another machine, or anything read on ci-runner."""
    if on_ci_runner() or not str(row.get("holder") or "").startswith("laptop:"):
        return None, "not this machine's"
    cache = {} if cache is None else cache
    m = lease_meta(row)
    elsewhere = _judged_elsewhere(m)
    if elsewhere:
        return None, elsewhere
    if row["name"].startswith("owner/") or m.get("kind") in ("owner", "owned"):
        return _cached_owner_work(row, m, t, cache)
    if m.get("kind") == "fixer":
        return fixer_work(row, t)
    return None, "not judged here"


def red_reap_orphans(store, t):
    """The laptop's tick: every live laptop-held fixer and owner lease whose holder is not at work is revoked
    (`ORPHANED: <why>`) -- the red offered again at once, its wrapper's next renewal lost. An orphaned fixer's session,
    if any still runs, is stopped (its wrapper is gone)."""
    acts, cache = [], {}
    for row in list(store.list("owner/")) + list(store.list("red/")):
        if not lease_is_live(row, t):
            continue
        ok, why = holder_work(row, t, cache)
        if ok is not False:
            continue
        line = revoke_orphan(store, row, why)
        if line:
            acts.append(line)
    return acts + bg_reap_spent(t)


def revoke_orphan(store: Any, row: Any, why: str) -> str | None:
    """Revoke the orphaned lease `row` (stopping its background session when its wrapper is gone) and tell the inbox;
    the line told, or None when the store did not revoke it."""
    if not store.revoke(row["name"], f"ORPHANED: {row['holder']} holds it with no work ({why})"):
        return None
    m = lease_meta(row)
    if "wrapper" in why and "gone" in why and m.get("bg_id"):
        bg_stop(claude_cli(), m["bg_id"])
    line = f"RED ORPHANED LEASE: {row['name']} ({row['holder']}): {why}: revoked, offered again"
    inbox_write("red", line, [line], [], 1)
    return line


# ---------------------------------------------------------------- SPENT SESSIONS (2026-11-02)
# claude keeps a background session's process after its turn (`claude bg-spare`), and `claude --bg --resume <id>` of a
# session whose process still runs starts a COPY under a new id. The owner's wrapper stopped no finished turn: each
# turn resumed the last one's still-running session as a copy, and the spares piled up -- 11 idle `red owner ·
# convergence` sessions in 40 min, ~25 claude processes, 4 GB. The wrapper now stops each turn's session when the turn
# ends (end_turn: the next `--resume` continues the SAME session, under the same id, as the owner's design says); and
# whatever older code or a crash leaves, the laptop's tick stops every agentctl-launched session (by its label) that
# waits idle with its process alive and is not the current turn of a live wrapper. `claude stop` keeps the
# conversation (`claude attach` reopens it); sessions agentctl did not launch are never touched.
BG_LABELS = ("red owner · ", "red fixer #", "orphan · ", "lander · ")  # owner_turn's -n and fixer_label's


BG_SPENT_GRACE = 120  # s a session must have run before the reaper judges it ($AGENTCTL_BG_SPENT_GRACE)


def bg_live_turns():
    """The background ids that are the current turn of a live wrapper (an owner in its turn, a fixer running)."""
    live = set()
    for p in glob.glob(reds_dir("owners", "*", "owner.json")):
        r = read_record(p) or {}
        if (r.get("bg_id") and not r.get("ended") and r.get("in_turn", True)
                and record_alive(r)):
            live.add(r["bg_id"])
    live.update(r["bg_id"] for r in fixer_records() if r.get("alive") and r.get("bg_id"))
    return live


def bg_spent(rows, live, t):
    """The rows of `claude agents --json --all` that are agentctl's spent sessions: launched by it (BG_LABELS), waiting
    idle with their process alive (bg_waiting), not a live wrapper's current turn, and older than BG_SPENT_GRACE."""
    grace = env_num("AGENTCTL_BG_SPENT_GRACE", BG_SPENT_GRACE)
    return [x for x in rows if _agentctl_background(x) and _outside(x, live)
            and bg_waiting(x) and t - float(x.get("startedAt") or 0) / 1000 >= grace]


def _agentctl_background(x):
    """A background row launched by agentctl (never a session it did not launch)."""
    return isinstance(x, dict) and x.get("kind") == "background" and str(x.get("name") or "").startswith(BG_LABELS)


def _outside(x, live):
    """The row has an id and is neither a live wrapper's turn (by id) nor its session's (by prefix)."""
    bid = str(x.get("id") or "")
    return bool(bid) and bid not in live and not any(str(x.get("sessionId") or "").startswith(b) for b in live)


def bg_reap_spent(t):
    """The laptop's tick: every spent agentctl session stopped (bg_spent). Lines for what it did."""
    if on_ci_runner():
        return []
    cli = claude_cli()
    rows = bg_rows(cli)
    if rows is None:
        return []
    acts = []
    for x in bg_spent(rows, bg_live_turns(), t):
        bg_stop(cli, x["id"])
        acts.append(f"RED SESSION SPENT: {x['id']} ({x.get('name')}) idle with its process alive, no wrapper's turn: "
                    "stopped")
    return acts
