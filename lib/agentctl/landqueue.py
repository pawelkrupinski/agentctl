# agentctl, part `landqueue`: the land queue's commands: land status | hold | release | cancel, and the processor's children (a checker, the service, await)
# A real module (lib/agentctl/__init__.py MODULES): its own imports, the standard library's and MODULES' only.

import argparse
import os
import signal
import time
from typing import Any
from agentctl.clock import Clock
from agentctl.core import fmt_age, git, git_ok, host_role
from agentctl.timefmt import hhmmz, ts
from agentctl.statefile import read_json_file, read_record
from agentctl.store import open_store
from agentctl.repos import movies_repo_path
from agentctl.waitingdir import ENTRY_FINAL, waiting_entries, waiting_state
from agentctl.procs import safe_cwd
from agentctl.owned import lock_held
from agentctl.clones import main_clone
from agentctl.queueslots import entry_owned, set_change
from agentctl.queue import hold_text, land_hold
from agentctl.queuerecheck import queued_entries
from agentctl.changekind import HOLD_SHOWN
from agentctl.waiting import GATE_UNREAD, cancel_entry_remote, cancel_submitted, gate_unread_why
from agentctl.queuesubmit import landed_sha_of
from agentctl.remotestore import store_url
from agentctl.landhold import LAND_HOLD_DEFAULT, hold_by, land_hold_release, land_hold_set
from agentctl.redseen import gate_reds
from agentctl.remote import remote_check_lines
from agentctl.watchtick import watch_landed
from agentctl.queueclose import change_rows, check_entry
from agentctl.recovery import recover_clear_hold
from agentctl.batchitem import status_batch
from agentctl.processorlink import await_entry, offer_token, spawn_waiting_processor, waiting_processor_alive
from agentctl.tickservice import waiting_service
from agentctl.batch import batch_progress_path, batch_row_ids
from agentctl.batchcheck import check_batch
from agentctl.statemachines import machine_state


def waiting_repo(a: argparse.Namespace) -> str:
    """The clone a `waiting` / `land` queue command acts on: --repo, else the checkout it runs in."""
    here = a.repo or safe_cwd()
    if a.wcmd == "await":
        return main_clone(here)
    if git_ok(here, "rev-parse", "--show-toplevel"):
        return git(here, "rev-parse", "--show-toplevel").strip()
    return main_clone(movies_repo_path())  # outside any git checkout (e.g. ~/bitcashier): the movies clone's queue, as `await` resolves it


def queue_command(a: argparse.Namespace) -> int:
    """The land queue's commands (`land status | hold | release | cancel`, and the processor's children: a change's or
    the batch's checker, the service): every `waiting` subcommand but `run` (the processor) and `await` (cmd_waiting)."""
    repo = waiting_repo(a)
    d, _c, _wt = waiting_state(repo)
    if a.wcmd == "check":  # one queued change's checker (the processor starts it)
        return check_entry(main_clone(repo), a.entry)
    if a.wcmd == "check-batch":  # the batch's executor (the processor starts it)
        return check_batch(main_clone(repo), a.entry)
    if a.wcmd == "service":
        return waiting_service(a.action, repo)
    if a.wcmd == "pause":  # `land hold`: an item in the store (the reconciler's, wherever this runs)
        return land_hold_command(a)
    if a.wcmd == "resume":
        return land_release_command(repo, d)
    if a.wcmd == "status":
        return land_status_command(a, repo, d)
    if a.wcmd == "cancel":
        return land_cancel_command(a, repo, d)
    return 2


def land_await(a: argparse.Namespace, repo: str, clock: Clock) -> int:
    """`waiting await`: land's wait on its entry, resumed after a restart on new code."""
    a.inbox_cwd = a.close or a.repo
    repo = main_clone(repo)
    os.chdir(repo)
    rc, sha, pushed_at = await_entry(repo, a.entry, clock)
    return watch_landed(repo, sha, pushed_at, clock, close=a.close) if rc == 0 and sha else rc


def land_hold_command(a: argparse.Namespace) -> int:
    """`land hold [--for M] [--why W]`: the hold item, in the store."""
    st = open_store()
    secs = int(float(getattr(a, "hold_for", None) or LAND_HOLD_DEFAULT // 60) * 60)
    it = land_hold_set(st, secs, getattr(a, "hold_why", None), hold_by())
    print(f"held: the processor takes no batch until `agentctl land release` or "
          f"{time.strftime('%H:%M', time.localtime(it['deadline']))} (then the hold expires); changes still queue")
    return 0


def land_release_command(repo: str, d: str) -> int:
    """`land release`: the hold gone, the processor started and the token offered."""
    st = open_store()
    gone = land_hold_release(st, hold_by())
    try:
        os.unlink(os.path.join(d, "PAUSED"))  # the pre-store hold file, if an old version left one
    except FileNotFoundError:
        pass
    recover_clear_hold(None, {"d": d})  # a held batch is assembled again at once
    spawn_waiting_processor(repo)
    offer_token(repo)
    print("released" + (f" ({', '.join(gone)})" if gone else " (no hold was in force)")
          + ": the processor takes the next batch")
    return 0


def land_status_command(a: argparse.Namespace, repo: str, d: str) -> int:
    """`land status [<id>]`: one change, open or decided (A VERDICT IS DURABLE), else the whole queue."""
    if getattr(a, "entry", None):
        return land_status_entry(open_store(), a.entry, time.time())
    # ONE QUEUE: the store's change items, on either host
    st = open_store()
    rows = change_rows(st)
    print(f"queue: {store_url() or 'this host'}")
    print(f"processor: {'running' if waiting_processor_alive(d) else 'not running'}")
    hold = land_hold(st)
    if hold:
        print(f"land: {hold_text(hold)}")
    bt = status_batch(st, d)
    if bt:
        print(batch_status_line(bt))
    offer_token(repo)
    print(gate_status_line(gate_reds(repo)))
    print(f"changes: {len(rows)}")
    in_batch = batch_row_ids(bt, rows)
    for r in rows:
        print(land_status_line(r, bt, in_batch, time.time()))
    land_status_tail(d, rows)
    return 0


def batch_status_line(bt: dict[str, Any]) -> str:
    dd = bt["data"]
    return (f"batch {bt['id']}: {bt['state']}" + (f", {len(dd.get('members') or [])} change(s) at "
            f"{str(dd.get('tip'))[:10]} on {str(dd.get('base'))[:10]}" if dd.get("tip") else "")
            + f", deadline in {int((bt['deadline'] - time.time()) // 60)} min")


def gate_status_line(reds: set[str]) -> str:
    return "gate: " + (f"UNREAD, taken as red ({gate_unread_why[0]})" if GATE_UNREAD in reds else
                       f"RED ({', '.join(sorted(reds))})" if reds else "green")


def land_status_tail(d: str, rows: list[dict[str, Any]]) -> None:
    """The status's last lines: a stall at a green gate, an empty queue, a laptop's local entries nothing runs."""
    prog = read_json_file(batch_progress_path(d)) if host_role() == "server" else {}
    if prog.get("ticks"):
        print(f"stall at green: {prog['ticks']} tick(s): {prog.get('why')}")
    if not rows:
        print("  (no change queued or waiting)")
    local = [e for e in waiting_entries(d, open_only=False) if e.get("status") not in ENTRY_FINAL]
    if host_role() == "sync" and local:
        print(f"LOCAL QUEUE: {len(local)} entr(ies) in {d}, which nothing runs; the sync moves them to ci-runner's "
              f"queue: {', '.join(e['id'] for e in local)}")


def land_cancel_command(a: argparse.Namespace, repo: str, d: str) -> int:
    """`land cancel <id>`: on the laptop through the store API (ONE QUEUE: ci-runner's); here the entry, its checker
    and its CI run."""
    if host_role() == "sync":
        return cancel_through_store(a.entry)
    queued = [e for e in queued_entries(d) if a.entry in e["id"]]
    if len(queued) == 1:
        return cancel_queued(d, queued[0])
    return cancel_waiting(a.entry, repo, d)


def cancel_through_store(entry: str) -> int:
    hits = [r["id"] for r in change_rows() if entry in r["id"]]
    if len(hits) != 1:
        print(f"no single open change matches {entry!r}: `agentctl land status`")
        return 1
    open_store().cancel_change(hits[0])
    print(f"cancelled {hits[0]}")
    return 0


def cancel_queued(d: str, e: dict[str, Any]) -> int:
    """A queued entry cancelled, with its remote item and its checker (its CI run is cancelled with it)."""
    set_change(d, e, "cancelled", event={"text": f"{e['id']}: cancelled"},
               why="cancelled by `agentctl land cancel`", decided=ts())
    cancel_entry_remote(e, "its queue entry was cancelled")
    h = read_record(entry_owned(e["id"]).record) or {}
    if lock_held(entry_owned(e["id"]).lockpath) and h.get("pid"):
        os.kill(int(h["pid"]), signal.SIGTERM)  # its checker: its CI run is cancelled with it
    print(f"cancelled {e['id']}")
    return 0


def cancel_waiting(entry: str, repo: str, d: str) -> int:
    """A waiting entry cancelled (the next batch is assembled without it), or a stranded item no entry carries."""
    hits = [e for e in waiting_entries(d) if entry in e["id"]]
    if not hits:  # an item no entry carries (stranded): the item alone
        ids = [r["id"] for r in change_rows() if entry in r["id"]]
        if len(ids) == 1:
            cancel_submitted(ids[0], repo=repo)
            print(f"cancelled {ids[0]}")
            return 0
    if len(hits) != 1:
        print(f"no single waiting change matches {entry!r}: `agentctl land status`")
        return 1
    e = hits[0]
    set_change(d, e, "cancelled", event={"text": f"{e['id']}: cancelled"}, why="cancelled by `agentctl land cancel`",
               decided=ts())  # the next batch is assembled without it
    print(f"cancelled {e['id']}")
    return 0


def land_status_line(r: dict[str, Any], bt: dict[str, Any] | None, in_batch: set[str], t: float) -> str:
    """`land status`'s line for change row r (change_rows): its state, its KIND when it is a red fix, the batch it is
    in, its hold, and whether it is past its deadline -- no held change is silent (tests/queue_sim.py checks it)."""
    left = int(r["deadline"] - t)
    return (f"  {r['id']}  {r['state']}  {r['worktree']}  {str(r['head'] or '')[:10]}  deadline in {left // 60} min"
            + (f"  [{r['kind']}]" if r.get("kind") else "")
            + (f"  (in batch {bt['id']}: {bt['state']})" if bt and r["id"] in in_batch else "")
            + (f"  held: {r['hold']}" if r.get("hold") else "")
            + (f"  AUTO-RESOLVED in the batch: {r['resolved']}" if r.get("resolved") else "")
            + (f"  PAST ITS DEADLINE (level {r['level']})" if left < 0 else ""))


def land_status_entry(st: Any, entry: str, t: float) -> int:
    """`land status <entry>`: ONE change -- the item whose id is `entry`, else the one whose id contains it -- open or
    decided: its state, worktree and head, then its verdict (LANDED <sha> / NOT LANDED: <why>) or what it waits on.
    A VERDICT IS DURABLE (queueuntil.py): an agent whose wait died reads it here after the fact. 1 when none or several
    match."""
    items = st.items("change")
    hits = [it for it in items if it["id"] == entry] or [it for it in items if entry in it["id"]]
    if len(hits) != 1:
        print(f"no single change matches {entry!r}" + (f": {', '.join(it['id'] for it in hits[:8])}" if hits else "")
              + " (`agentctl land status` lists the open ones)")
        return 1
    it = hits[0]
    d = it.get("data") or {}
    print(f"{it['id']}  {it['state']}  {d.get('worktree')}  {str(d.get('head') or '')[:10]}")
    if it["state"] == "landed":
        print(f"LANDED {d.get('landed_sha') or landed_sha_of(it['id'], d)} (queued {it['id']})")
    elif machine_state("change", it["state"]).terminal:
        why = str(d.get("why") or f"{it['id']} {it['state']}")
        print(why if why.startswith(("NOT LANDED", "SUPERSEDED")) else f"NOT LANDED: {why}")
    else:
        hold = (d.get("hold") or {}).get("why") if it["state"] in HOLD_SHOWN else None
        print(f"OPEN: {it['state']} for {fmt_age(t - float(it.get('entered_at') or t))}"
              + (f", held: {hold}" if hold else "") + f"; deadline in {int((it['deadline'] - t) // 60)} min")
        if it["state"] == "checking":
            for line in remote_check_lines(d.get("check"), t) or [
                    "check: no remote check recorded (a local check tree, or its executor is starting)"]:
                print(line)
    for r in d.get("rechecks") or []:  # MINIMAL RECHECKS (queuerecheck.py): why each round after the first ran, or did not
        print(f"recheck: {hhmmz(r.get('at') or 0)} {r.get('kind')} on {r.get('main') or '?'}: {r.get('why')}")
    return 0
