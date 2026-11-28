# agentctl, part `landprecheck`: the fast precheck of a queued change, in the detached process or the foreground
# A real module (lib/agentctl/__init__.py MODULES): its own imports, the standard library's and MODULES' only.

import os
from typing import Any
from agentctl.timefmt import argparse
from agentctl.core import git, print_error
from agentctl.paths import scratch
from agentctl.inboxentry import inbox_write
from agentctl.hookoutput import precheck_reason
from agentctl.context import Context
from agentctl.land import run_watched
from agentctl.queuesubmit import FAST_PRECHECK, FAST_PRECHECK_BOUND, note_precheck
from agentctl.landqueue import queue_command


def _refusal_told(a: Any, verdict: Any, lines: Any) -> Any:
    """Tell the change's author the precheck's verdict (inbox and stdout); exit code 1."""
    inbox_write("land", verdict, [verdict], [l for l in lines if l.strip()][-40:], 1, cwd=a.repo)
    print(verdict, flush=True)
    return 1


def _cancel_refused(a: Any, store: Any, lines: Any, last: Any, *, ctx: Context) -> Any:
    """The precheck refused change a.entry (`last`: its closing line): cancel it and tell its author. Exit code 1 when
    the verdict is NOT LANDED or the cancel failed, 0 when the change was decided already."""
    cancel = argparse.Namespace(wcmd="cancel", repo=a.repo, entry=a.entry, hold_for=None, hold_why=None)
    # NOT LANDED only when the cancel took: the landing check runs alongside and may have decided first (review
    # 2026-11-07: a change that LANDED was told NOT LANDED, "its queue entry is cancelled").
    try:
        rc = queue_command(cancel, store, ctx=ctx)
    except (Exception, SystemExit) as e:  # the store unreachable: the change is still queued, its check judges it
        print_error(f"could not cancel {a.entry}: {e}")
        verdict = (f"PRECHECK REFUSED {a.entry} ({a.head[:10]}): {last}; it could not be cancelled ({str(e)[:120]}), so "
                   f"the landing check still judges it -- `agentctl land cancel {a.entry}` to drop it, fix it in {a.repo}")
        return _refusal_told(a, verdict, lines)
    if rc != 0:
        print(f"the fast precheck refused {a.entry}, but it is already decided (no open change to cancel): its own "
              "verdict stands", flush=True)
        return 0
    verdict = (f"NOT LANDED: the fast precheck refused {a.entry} ({a.head[:10]}): {last}; its queue entry is cancelled. "
               f"Fix it in {a.repo}, then land again")
    return _refusal_told(a, verdict, lines)


def cmd_land_precheck(a, store, *, ctx):
    """The detached half of start_fast_precheck: run the repo's precheck, and on a refusal cancel the queued change."""
    # Its own process group, ended WHOLE at the bound (review 2026-11-07: subprocess.run killed only the script, so what
    # it started -- sbt, a test JVM -- lived on, and kept the output pipe, and so this wait, open): run_watched.
    t0 = ctx.clock.time()
    rc, out = run_watched([os.path.join(a.repo, FAST_PRECHECK), "--base", a.base, "--head", a.head], a.repo, None,
                          os.path.join(scratch(), f"agentctl-precheck-{a.entry}"), "precheck",
                          stall=FAST_PRECHECK_BOUND, wall=FAST_PRECHECK_BOUND)
    note_precheck(a.entry, ctx.clock.time() - t0, rc, out, store)
    if rc is None:
        print(f"{out or ''}PRECHECK UNCHECKED: past its {FAST_PRECHECK_BOUND} s bound, ended with its children: "
              "could not check, its landing check judges it", flush=True)
        return 0
    lines = (out or "").splitlines()
    print("\n".join(lines), flush=True)
    if rc != 1:
        return 0
    last = next((l for l in reversed(lines) if l.startswith("PRECHECK ")), lines[-1] if lines else "exit 1")
    why = precheck_reason(lines)
    last = f"{last} -- {why}" if why else last
    return _cancel_refused(a, store, lines, last, ctx=ctx)


def foreground_precheck(repo, main_ref, head, eid, a, store, *, ctx):
    """The fast precheck of queued change eid in THIS process (a land that waits for its verdict): 1 when it refused
    (its NOT LANDED or PRECHECK REFUSED line the last printed), else 0."""
    if not getattr(a, "no_fast_precheck", False) and os.access(os.path.join(repo, FAST_PRECHECK), os.X_OK):
        base = git(repo, "rev-parse", main_ref).strip()
        if cmd_land_precheck(argparse.Namespace(entry=eid, repo=repo, base=base, head=head), store, ctx=ctx) == 1:
            return 1
    return 0
