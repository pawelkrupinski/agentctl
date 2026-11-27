# agentctl, part `gatepublish`: the gate line published: one gate read per cycle, the red tick on it, the summary line, the branch reds settled and routed
# A real module (lib/agentctl/__init__.py MODULES): its own imports, the standard library's and MODULES' only.

import os
from typing import Any
from agentctl.clock import wall_epoch
from agentctl.tickknobs import knob_assign_bound, knob_gate_publish, knob_step_bound
from agentctl.core import attempt, err, run_bounded, slug
from agentctl.gating import gate_dirty_path
from agentctl.cistate import ci_state_write
from agentctl.gatesummary import _last_gate_publish, publish_gate_summary
from agentctl.recordingpins import refresh_pinned_recording
from agentctl.branches import route_branch_reds, settle_branch_reds
from agentctl.cireport import ci_state_refresh, ci_why
from agentctl.redtick import red_tick_repo


def gate_publish_cycle(repo: str, store: Any) -> None:
    """One publish: the gate read once, the red tick on that snapshot, the summary line published, the branch reds
    settled and routed."""
    try:
        o_r = slug(repo)
        snap = ci_state_refresh(o_r, write=False, store=store)  # ONE read of GitHub per cycle; everything below reads it
        refresh_pinned_recording(o_r, repo, store)  # what PREDICTS MAIN RED compares a branch run's recording with
        ci_state_write(snap)
        # THE RED TICK ON THIS SNAPSHOT before its line is published: every red it shows is an item, offered or
        # held (never NOBODY). Bounded: a dispatch's worktree checkout finishes on the tick's own thread
        if not run_bounded("red-tick", lambda: red_tick_repo(repo, store, snap), knob_assign_bound()):
            err("red tick: still running; the gate line is published meanwhile")
        _red, line = ci_why(o_r, summary=True, snap=snap, repo=repo, store=store)
        publish_gate_summary(o_r, line.splitlines()[-1], epoch=snap["at"])
    except (Exception, SystemExit) as e:
        err(f"gate summary: skipped ({str(e).replace('agentctl: ', '')[:200]})")
        return
    for failed, lines in (("not settled", lambda: settle_branch_reds(o_r, snap, store)),
                          ("not routed", lambda: route_branch_reds(o_r, snap))):
        try:
            for text in lines():
                err(f"branch reds: {text}")
        except (Exception, SystemExit) as e:
            err(f"branch reds: {failed} ({str(e).replace('agentctl: ', '')[:200]})")


def maybe_publish_gate(repo, store):
    """The processor's loop, at most every GATE_PUBLISH_INTERVAL s: read the gate (it holds the token) and publish the
    summary line for the status line. Never fatal; a failed read leaves the old file to age out (fail-closed)."""
    # a push to main since the last publish (mark_gate_dirty): publish now, not at the 45 s mark
    marked = attempt(lambda: os.path.getmtime(gate_dirty_path()) > _last_gate_publish[0], False, catch=(OSError,))
    if not marked and wall_epoch() - _last_gate_publish[0] < knob_gate_publish():
        return
    _last_gate_publish[0] = wall_epoch()  # the wall's: it is compared with a file's mtime above

    if not run_bounded("gate-publish", lambda: gate_publish_cycle(repo, store), knob_step_bound()):
        err("gate summary: still reading from an earlier cycle (the loop goes on)")
