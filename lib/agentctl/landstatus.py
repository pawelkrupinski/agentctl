# agentctl, part `landstatus`: `land status`: the queue, its processor, hold, batch, gate and changes
# A real module (lib/agentctl/__init__.py MODULES): its own imports, the standard library's and MODULES' only.

import sys
from typing import Any
import argparse
from agentctl.store import StoreUnavailable
from agentctl.context import Context
from agentctl.processorlink import offer_token
from agentctl.remotestore import store_unreachable_text
from agentctl.redseen import gate_reds
from agentctl.changerows import change_rows
from agentctl.batch import batch_row_ids
from agentctl.landstatusentry import land_status_entry
from agentctl.landstatusline import gate_status_line, land_status_line, land_status_tail
from agentctl.landstatusline import land_status_header


def land_status_command(a: argparse.Namespace, repo: str, d: str, st: Any, *, ctx: Context) -> int:
    """`land status [<id>]`: one change, open or decided (A VERDICT IS DURABLE), else the whole queue."""
    try:  # the store's reads are bounded (cli waited_on): one that does not answer fails the command fast, saying which
        if getattr(a, "entry", None):
            return land_status_entry(st, a.entry, ctx.clock.time())
        # ONE QUEUE: the store's change items, on either host
        rows = change_rows(st)
    except StoreUnavailable as e:
        print(store_unreachable_text(e), file=sys.stderr)
        return 3
    bt = land_status_header(d, st)
    offer_token(repo)
    print(gate_status_line(gate_reds(repo, ctx=ctx)))
    print(f"changes: {len(rows)}")
    in_batch = batch_row_ids(bt, rows)
    for r in rows:
        print(land_status_line(r, bt, in_batch, ctx.clock.time()))
    land_status_tail(d, rows)
    return 0
