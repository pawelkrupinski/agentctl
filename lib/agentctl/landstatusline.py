# agentctl, part `landstatusline`: the lines of `land status`: the batch, the gate, each change and the tail
# A real module (lib/agentctl/__init__.py MODULES): its own imports, the standard library's and MODULES' only.

from typing import Any
from agentctl.clock import epoch_now
from agentctl.core import host_role
from agentctl.waitingdir import ChangeRecords
from agentctl.waiting import GATE_UNREAD, unread_why
from agentctl.landhold import hold_text, land_hold
from agentctl.processorlink import waiting_processor_alive
from agentctl.batchitem import status_batch
from agentctl.remotestore import store_url


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


def land_status_header(d: str, st: Any) -> dict[str, Any] | None:
    """The first lines of `land status`: the queue, its processor, the hold and the batch; the batch's item, or None."""
    print(f"queue: {store_url() or 'this host'}")
    print(f"processor: {'running' if waiting_processor_alive(d) else 'not running'}")
    hold = land_hold(st)
    if hold:
        print(f"land: {hold_text(hold)}")
    bt = status_batch(st, d)
    if bt:
        print(batch_status_line(bt))
    return bt

