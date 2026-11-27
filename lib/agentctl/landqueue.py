# agentctl, part `landqueue`: the land queue's commands: land status | hold | release | cancel, and the processor's children (a checker, the service, await)
# A real module (lib/agentctl/__init__.py MODULES): its own imports, the standard library's and MODULES' only.

import argparse
import os
import sys
import time
from typing import Any
from agentctl.statefile import read_json_file, unlink_file
from agentctl.statemachines import machine_state
from agentctl.clock import Clock, epoch_now
from agentctl.timefmt import hhmmz, ts
from agentctl.core import fmt_age, git, git_ok, host_role
from agentctl.repos import movies_repo_path
from agentctl.procs import safe_cwd
from agentctl.clones import main_clone
from agentctl.waitingdir import ChangeRecords, waiting_state
from agentctl.store import StoreUnavailable
from agentctl.processorlink import await_entry, offer_token, spawn_waiting_processor, waiting_processor_alive
from agentctl.batchitem import status_batch
from agentctl.remotestore import store_unreachable_text, store_url
from agentctl.queueslots import set_change
from agentctl.landhold import LAND_HOLD_DEFAULT, hold_by, hold_text, land_hold, land_hold_release, land_hold_set
from agentctl.tickservice import waiting_service
from agentctl.changekind import HOLD_SHOWN
from agentctl.waiting import GATE_UNREAD, cancel_entry_remote, cancel_submitted, stop_checker, unread_why
from agentctl.queuesubmit import landed_sha_of
from agentctl.remote import remote_check_lines
from agentctl.redseen import gate_reds
from agentctl.batch import batch_row_ids
from agentctl.queueclose import change_rows, check_entry
from agentctl.watchtick import watch_landed
from agentctl.recovery import recover_clear_hold
from agentctl.batchcheck import check_batch
from agentctl.invariants import change_carrying_batch


def waiting_repo(a: argparse.Namespace) -> str:
    """The clone a `waiting` / `land` queue command acts on: --repo, else the checkout it runs in."""
    here = a.repo or safe_cwd()
    if a.wcmd == "await":
        return main_clone(here)
    if git_ok(here, "rev-parse", "--show-toplevel"):
        return git(here, "rev-parse", "--show-toplevel").strip()
    return main_clone(movies_repo_path())  # outside any git checkout (e.g. ~/bitcashier): the movies clone's queue, as `await` resolves it


def queue_command(a: argparse.Namespace, store: Any) -> int:
    """The land queue's commands (`land status | hold | release | cancel`, and the processor's children: a change's or
    the service): every `waiting` subcommand but `run` (the processor), `await` and the checkers (cmd_waiting)."""
    repo = waiting_repo(a)
    d, _c, _wt = waiting_state(repo)
    if a.wcmd == "service":
        return waiting_service(a.action, repo)
    if a.wcmd == "pause":  # `land hold`: an item in the store (the reconciler's, wherever this runs)
        return land_hold_command(a, store)
    if a.wcmd == "resume":
        return land_release_command(repo, d, store)
    if a.wcmd == "status":
        return land_status_command(a, repo, d, store)
    if a.wcmd == "cancel":
        return land_cancel_command(a, repo, d, store)
    return 2


def queue_checker(a: argparse.Namespace, clock: Clock, store: Any) -> int:
    """`waiting check <id>` (one queued change's checker) or `waiting check-batch <id>` (the batch's executor): the
    processor starts them, detached."""
    repo = main_clone(waiting_repo(a))
    return check_entry(repo, a.entry, clock, store) if a.wcmd == "check" else check_batch(repo, a.entry, clock, store)


def land_await(a: argparse.Namespace, repo: str, clock: Clock, store: Any) -> int:
    """`waiting await`: land's wait on its entry, resumed after a restart on new code."""
    a.inbox_cwd = a.close or a.repo
    repo = main_clone(repo)
    os.chdir(repo)
    rc, sha, pushed_at = await_entry(repo, a.entry, clock, store)
    return watch_landed(repo, sha, pushed_at, clock, store, close=a.close) if rc == 0 and sha else rc


def land_hold_command(a: argparse.Namespace, st: Any) -> int:
    """`land hold [--for M] [--why W]`: the hold item, in the store."""
    secs = int(float(getattr(a, "hold_for", None) or LAND_HOLD_DEFAULT // 60) * 60)
    it = land_hold_set(st, secs, getattr(a, "hold_why", None), hold_by())
    print(f"held: the processor takes no batch until `agentctl land release` or "
          f"{time.strftime('%H:%M', time.localtime(it['deadline']))} (then the hold expires); changes still queue")
    return 0


def land_release_command(repo: str, d: str, st: Any) -> int:
    """`land release`: the hold gone, the processor started and the token offered."""
    gone = land_hold_release(st, hold_by())
    unlink_file(os.path.join(d, "PAUSED"))
    recover_clear_hold(None, {"d": d}, st)  # a held batch is assembled again at once
    spawn_waiting_processor(repo)
    offer_token(repo)
    print("released" + (f" ({', '.join(gone)})" if gone else " (no hold was in force)")
          + ": the processor takes the next batch")
    return 0


def land_status_command(a: argparse.Namespace, repo: str, d: str, st: Any) -> int:
    """`land status [<id>]`: one change, open or decided (A VERDICT IS DURABLE), else the whole queue."""
    try:  # the store's reads are bounded (cli waited_on): one that does not answer fails the command fast, saying which
        if getattr(a, "entry", None):
            return land_status_entry(st, a.entry, epoch_now())
        # ONE QUEUE: the store's change items, on either host
        rows = change_rows(st)
    except StoreUnavailable as e:
        print(store_unreachable_text(e), file=sys.stderr)
        return 3
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
        print(land_status_line(r, bt, in_batch, epoch_now()))
    land_status_tail(d, rows)
    return 0


def batch_status_line(bt: dict[str, Any]) -> str:
    dd = bt["data"]
    return (f"batch {bt['id']}: {bt['state']}" + (f", {len(dd.get('members') or [])} change(s) at "
            f"{str(dd.get('tip'))[:10]} on {str(dd.get('base'))[:10]}" if dd.get("tip") else "")
            + f", deadline in {int((bt['deadline'] - epoch_now()) // 60)} min")


def gate_status_line(reds: set[str]) -> str:
    return "gate: " + (f"UNREAD, taken as red ({unread_why(reds)})" if GATE_UNREAD in reds else
                       f"RED ({', '.join(sorted(reds))})" if reds else "green")


def land_status_tail(d: str, rows: list[dict[str, Any]]) -> None:
    """The status's last lines: an empty queue, a laptop's local entries nothing runs."""
    if not rows:
        print("  (no change queued or waiting)")
    local = ChangeRecords(None, d).open()  # the laptop's outbox
    if host_role() == "sync" and local:
        print(f"LOCAL QUEUE: {len(local)} entr(ies) in {d}, which nothing runs; the sync moves them to ci-runner's "
              f"queue: {', '.join(e['id'] for e in local)}")


def land_cancel_command(a: argparse.Namespace, repo: str, d: str, st: Any) -> int:
    """`land cancel <id>`: on the laptop through the store API (ONE QUEUE: ci-runner's); here the entry, its checker
    and its CI run."""
    if host_role() == "sync":
        return cancel_through_store(a.entry, st)
    queued = [e for e in ChangeRecords(st, d).queued() if a.entry in e["id"]]
    if len(queued) == 1:
        return cancel_queued(st, d, queued[0])
    return cancel_waiting(a.entry, repo, d, st)


def cancel_through_store(entry: str, st: Any) -> int:
    hits = [r["id"] for r in change_rows(st) if entry in r["id"]]
    if len(hits) != 1:
        print(f"no single open change matches {entry!r}: `agentctl land status`")
        return 1
    st.cancel_change(hits[0])
    print(f"cancelled {hits[0]}")
    return 0


def cancel_queued(st: Any, d: str, e: dict[str, Any]) -> int:
    """A queued entry cancelled, with its remote item and its checker (its CI run is cancelled with it)."""
    set_change(st, d, e, "cancelled", cause="agent", event={"text": f"{e['id']}: cancelled"},
               why="cancelled by `agentctl land cancel`", decided=ts())
    cancel_entry_remote(st, e, "its queue entry was cancelled")
    stop_checker(e)
    print(f"cancelled {e['id']}")
    return 0


def cancel_waiting(entry: str, repo: str, d: str, st: Any) -> int:
    """A waiting entry cancelled (the next batch is assembled without it), or a stranded item no entry carries."""
    hits = [e for e in ChangeRecords(st, d).waiting() if entry in e["id"]]
    if not hits:  # an item no entry carries (stranded): the item alone
        ids = [r["id"] for r in change_rows(st) if entry in r["id"]]
        if len(ids) == 1:
            cancel_submitted(st, ids[0], repo=repo)
            print(f"cancelled {ids[0]}")
            return 0
    if len(hits) != 1:
        print(f"no single waiting change matches {entry!r}: `agentctl land status`")
        return 1
    e = hits[0]
    set_change(st, d, e, "cancelled", cause="agent", event={"text": f"{e['id']}: cancelled"}, why="cancelled by `agentctl land cancel`",
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


def check_lines(st: Any, it: dict[str, Any], t: float) -> list[str]:
    """What a checking change's check is: its batch's, for a backlog batch's member (no remote check of its own --
    2026-11-23: ci-row-scope-py's in batch #107 read as a dead check), else its remote check's lines."""
    b = change_carrying_batch(st, it["id"])
    if b:
        return [f"check: in batch {b['id']} ({b['state']}): the batch's check is its check"]
    return list(remote_check_lines((it.get("data") or {}).get("check"), t) or [
        "check: no remote check recorded (a local check tree, or its executor is starting)"])


def _matching_changes(items: list[dict[str, Any]], entry: str) -> list[dict[str, Any]]:
    """The change whose id is `entry`, else every one whose id contains it."""
    return [it for it in items if it["id"] == entry] or [it for it in items if entry in it["id"]]


def _print_open_status(st: Any, it: dict[str, Any], d: dict[str, Any], t: float) -> None:
    """What an open change waits on: its state and age, its hold, its deadline, and (checking) its check."""
    hold = (d.get("hold") or {}).get("why") if it["state"] in HOLD_SHOWN else None
    print(f"OPEN: {it['state']} for {fmt_age(t - float(it.get('entered_at') or t))}"
          + (f", held: {hold}" if hold else "") + f"; deadline in {int((it['deadline'] - t) // 60)} min")
    if it["state"] == "checking":
        print("\n".join(check_lines(st, it, t)))


def _print_status_verdict(st: Any, it: dict[str, Any], d: dict[str, Any], t: float) -> None:
    """A change's verdict: LANDED <sha>, its NOT LANDED / SUPERSEDED line when decided, else what it waits on."""
    if it["state"] == "landed":
        print(f"LANDED {d.get('landed_sha') or landed_sha_of(it['id'], d)} (queued {it['id']})")
    elif machine_state("change", it["state"]).terminal:
        why = str(d.get("why") or f"{it['id']} {it['state']}")
        print(why if why.startswith(("NOT LANDED", "SUPERSEDED")) else f"NOT LANDED: {why}")
    else:
        _print_open_status(st, it, d, t)


def land_status_entry(st: Any, entry: str, t: float) -> int:
    """`land status <entry>`: ONE change -- the item whose id is `entry`, else the one whose id contains it -- open or
    decided: its state, worktree and head, then its verdict (LANDED <sha> / NOT LANDED: <why>) or what it waits on.
    A VERDICT IS DURABLE (queueuntil.py): an agent whose wait died reads it here after the fact. 1 when none or several
    match."""
    hits = _matching_changes(st.items("change"), entry)
    if len(hits) != 1:
        print(f"no single change matches {entry!r}" + (f": {', '.join(it['id'] for it in hits[:8])}" if hits else "")
              + " (`agentctl land status` lists the open ones)")
        return 1
    it = hits[0]
    d = it.get("data") or {}
    print(f"{it['id']}  {it['state']}  {d.get('worktree')}  {str(d.get('head') or '')[:10]}")
    _print_status_verdict(st, it, d, t)
    for r in d.get("rechecks") or []:  # MINIMAL RECHECKS (queuerecheck.py): why each round after the first ran, or did not
        print(f"recheck: {hhmmz(r.get('at') or 0)} {r.get('kind')} on {r.get('main') or '?'}: {r.get('why')}")
    return 0
