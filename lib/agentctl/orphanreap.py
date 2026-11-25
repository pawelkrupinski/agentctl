# agentctl, part `orphanreap`: orphaned work reaped: a red's holder no longer at work loses its leases, an idle agentctl-launched session is stopped
# A real module (lib/agentctl/__init__.py MODULES): its own imports, the standard library's and MODULES' only.

from typing import Any
import glob
import os
from agentctl.core import env_num, fmt_age, on_ci_runner, pid_alive, reds_dir
from agentctl.statefile import mtime, read_record
from agentctl.procs import proc_alive, socket_host
from agentctl.fixerrecords import fixer_records
from agentctl.inboxentry import inbox_write
from agentctl.fixersession import RED_STALL, bg_rows, bg_session, bg_stop, bg_turn_over, bg_waiting
from agentctl.dispatchblock import claude_cli
from agentctl.redrecord import lease_is_live
from agentctl.leases import lease_meta
from agentctl.owners import owner_record_path
from agentctl.redmove import tail_lines


# ---------------------------------------------------------------- PROOF OF WORK: orphaned leases (2026-10-31)
# A lease is held by WORK, never by a living process. 2026-10-31 (issue 20261031T203744-4a4a59): the main-tests owner's
# session stopped at 20:12 (its turn ended `blocked`), while its wrapper (pid 51249) waited for `done` and kept renewing
# the owner's heartbeat; every red of the family was handed to it, held "leased ... none at work" and never fixed. The
# wrappers now end a turn whose session ended or stalled (red_owner, red_fixer). And whatever a wrapper does -- older
# code, a hang -- the laptop's tick checks every laptop-held fixer and owner lease against the evidence of work: the
# wrapper's process, its session's row in `claude agents`, the transcript's and the worktree's progress. A holder not at
# work for RED_ORPHAN_GRACE loses its leases at once (revoked `ORPHANED: <why>`): the red is offered again, and a
# wrapper still running reads its next renewal as lost and exits. `red status` shows such a lease as orphaned, never
# "leased ... none at work".
RED_ORPHAN_GRACE = 5 * 60    # an ended session's turn still held this long is orphaned ($AGENTCTL_RED_ORPHAN_GRACE)


def owner_work(fam, t, holder_pid=None):
    """(True|False|None, why): is family fam's owner at work? None when this machine cannot tell. holder_pid: the
    wrapper the lease itself names (its meta's pid): a record of another spell judges nothing of it."""
    rec = read_record(owner_record_path(fam)) or {}
    pid = rec.get("pid")
    if not pid:
        return None, "no owner record on this machine"
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
    if not proc_alive(pid, rec.get("process_start")):
        return False, f"its wrapper (pid {pid}) is gone"
    if not rec.get("in_turn", bool(rec.get("bg_id"))):  # older records have no in_turn: a recorded session is a turn
        return True, "between turns"
    grace = env_num("AGENTCTL_RED_ORPHAN_GRACE", RED_ORPHAN_GRACE)
    idle = t - max(float(rec.get("progress_at") or 0), mtime(rec.get("transcript")))
    bid = rec.get("bg_id")
    if bid:
        ent = bg_session(claude_cli(), bid)
        if ent == {}:
            return None, "`claude agents` unread"
        if (ent is None or bg_turn_over(ent, rec.get("transcript"), t)) and idle > grace:
            return False, (f"its session {bid} is {(ent or {}).get('state') or 'gone'} while its wrapper (pid {pid}) "
                           f"still holds the turn, {fmt_age(idle)} without progress")
    if idle > env_num("AGENTCTL_RED_STALL", RED_STALL) + grace:
        return False, f"turn {rec.get('turn')}: no progress (transcript) for {fmt_age(idle)}"
    return True, f"turn {rec.get('turn')} at work"


def wrapper_last_words(m, n=3):
    """The last lines a fixer's wrapper wrote to its stderr (its lease meta's wrapper_log), joined; "" when none."""
    path = m.get("wrapper_log")
    lines = [x.strip() for x in tail_lines(path, 40)] if path and os.path.isfile(path) else []
    return " | ".join([x for x in lines if x][-n:])[:600]


def _wrapper_gone(m: Any) -> Any:
    """Why a dispatched fixer's wrapper is gone (its pid dead; its last words), or None."""
    pid = m.get("pid")
    if not pid or pid_alive(int(pid)):
        return None
    words = wrapper_last_words(m)
    return f"its wrapper (pid {pid}) is gone" + (f"; its last words: {words}" if words else "")


def _session_ended(row: Any, t: Any, m: Any, bid: Any, ent: Any) -> Any:
    """Why a dispatched fixer's bg session `bid` (entry ent) is over -- gone or its turn ended, no progress for
    RED_ORPHAN_GRACE -- or None."""
    idle = t - max(float(row.get("renewed_at") or row.get("acquired_at") or 0), mtime(m.get("log")))
    if (ent is None or bg_turn_over(ent, m.get("log"), t)) and idle > env_num("AGENTCTL_RED_ORPHAN_GRACE", RED_ORPHAN_GRACE):
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


def holder_work(row, t, cache=None):
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
                and proc_alive(r.get("pid"), r.get("process_start"))):
            live.add(r["bg_id"])
    live.update(r["bg_id"] for r in fixer_records() if r.get("alive") and r.get("bg_id"))
    return live


def bg_spent(rows, live, t):
    """The rows of `claude agents --json --all` that are agentctl's spent sessions: launched by it (BG_LABELS), waiting
    idle with their process alive (bg_waiting), not a live wrapper's current turn, and older than BG_SPENT_GRACE."""
    grace = env_num("AGENTCTL_BG_SPENT_GRACE", BG_SPENT_GRACE)
    out = []
    for x in rows:
        if not isinstance(x, dict) or x.get("kind") != "background":
            continue
        if not str(x.get("name") or "").startswith(BG_LABELS):  # never a session agentctl did not launch
            continue
        bid = str(x.get("id") or "")
        if not bid or bid in live or any(str(x.get("sessionId") or "").startswith(b) for b in live):
            continue
        if not bg_waiting(x) or t - float(x.get("startedAt") or 0) / 1000 < grace:
            continue
        out.append(x)
    return out


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
