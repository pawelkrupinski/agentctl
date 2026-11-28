# agentctl, part `cli`: the command line (main: the parser and the handlers' table).
# A real module (lib/agentctl/__init__.py MODULES): its own imports, the standard library's and MODULES' only.

import argparse
from functools import partial
import contextlib
import datetime as dt
import io
import sys
import time
from typing import Any, Callable
from agentctl.procrunner import ProcessRunner, wire_runner
from agentctl.clock import Clock, wire_clock
from agentctl.timefmt import age_since
from agentctl.ghapp import cmd_github_app
from agentctl.config import cmd_config
from agentctl.actors import process_actor
from agentctl.inboxentry import InboxWriter, wire_inbox_writer
from agentctl.hookoutput import check_failures
from agentctl.inbox import inbox_command, with_inbox
from agentctl.conditionregistry import ConditionRegistry
from agentctl.conditions import (
    condition_guarded, probe_gate_read, probe_git_fetch, probe_http, probe_tcp, probe_waiting_ok,
    probe_workflow_green)
from agentctl.serverclone import probe_clone_health
from agentctl.owned import cmd_wait, restart_gate
from agentctl.maint import cmd_orphans
from agentctl.queuestall import probe_queue_moving
from agentctl.crashloop import probe_fixer_crash_loop
from agentctl.vendordrift import probe_vendor_drift
from agentctl.selfcode import code_version
from agentctl.store import CurrentStore
from agentctl.timelinecmd import cmd_analyze, cmd_history
from agentctl.caches import Caches
from agentctl.ciruns import CiRuns
from agentctl.context import Context
from agentctl.machine import MachineProbe, process_machine
from agentctl.ciwhy import gate_from_snap, outside_from_snap
from agentctl.leases import cmd_lease
from agentctl.laptoprole import cmd_laptop
from agentctl.processstore import process_store
from agentctl.reverts import cmd_revert
from agentctl.issues import cmd_issue
from agentctl.alerts import alert_write
from agentctl.queueinfra import probe_infra_requeue, probe_push_credential
from agentctl.gov import cmd_suite
from agentctl.govcmd import cmd_checks, cmd_gov
from agentctl.cleanup import cmd_sweep
from agentctl.cleanuppass import cmd_cleanup
from agentctl.upkeep import probe_disk_free, probe_run_queue
from agentctl.branches import cmd_dispatch
from agentctl.localrecheck import recheck_local
from agentctl.redwaits import probe_quota_reset
from agentctl.redcrash import probe_red_newer_verdict
from agentctl.ask import cmd_decide
from agentctl.watchtick import cmd_watch
from agentctl.recovery import (
    cmd_recover, note_missing_tokens, probe_red_read, probe_route, probe_token, probe_tunnel, recover_clear_flag,
    recover_clear_hold, recover_tunnel_reset)
from agentctl.tools import cmd_env, cmd_prepush, cmd_queue_retired, cmd_state
from agentctl.storecmd import cmd_store
from agentctl.ciwhycmd import cmd_ci_why
from agentctl.inboxcli import cmd_inbox
from agentctl.landedchange import probe_change_source
from agentctl.superseded import cmd_supersede
from agentctl.park import cmd_park, cmd_unpark
from agentctl.orphans import probe_orphan_landed
from agentctl.maintain import cmd_maintain
from agentctl.statuspage import cmd_status
from agentctl.redcli import cmd_red
from agentctl.invariants import probe_invariant
from agentctl.watchdog import cmd_watchdog
from agentctl.processor import cmd_tick, cmd_waiting
from agentctl.queueenter import cmd_land_precheck
from agentctl.slots import cmd_land
from agentctl.cliparser import parser


# THE COMPOSITION ROOT: what every process wires before any command runs.
def condition_registry(clock: Clock, store: Any, ctx: Context) -> ConditionRegistry:
    """The process's ConditionRegistry: every probe, with the process's store and clock bound into those that read
    them (the watchdog's invariants, a change's source, a red's newer run, the queue's movement), the context into
    those reading CI runs, and the recovery actions."""
    return ConditionRegistry(
        [partial(probe_disk_free, machine=ctx.machine), partial(probe_run_queue, machine=ctx.machine),
         partial(probe_quota_reset, ctx=ctx), probe_waiting_ok, probe_workflow_green, probe_http, probe_tcp, probe_route,
         probe_tunnel, probe_gate_read, probe_token, probe_git_fetch, probe_clone_health, probe_red_read,
         partial(probe_orphan_landed, store=store), partial(probe_invariant, clock=clock, st=store, ctx=ctx),
         partial(probe_change_source, store=store), partial(probe_push_credential, st=store),
         partial(probe_infra_requeue, st=store), partial(probe_red_newer_verdict, store=store),
         partial(probe_queue_moving, store=store, now=clock.time),
         partial(probe_fixer_crash_loop, store=store, code=code_version), probe_vendor_drift],
        {"clear_hold": recover_clear_hold, "tunnel_reset": recover_tunnel_reset, "clear_flag": recover_clear_flag})


def process_context(clock: Clock, runner: ProcessRunner, inbox: InboxWriter, machine: MachineProbe) -> Context:
    """The process's Context (docs/design/contexts.md): its one CI runs reader (CiRuns: what was read of GitHub's runs
    and jobs), new for each main, and the clock, process runner, inbox writer and machine main wired."""
    return Context(runs=CiRuns(), clock=clock, runner=runner, inbox=inbox, machine=machine, caches=Caches())


def wire_collaborators(clock: Clock, store: Any) -> InboxWriter:
    """The process's clock and the inbox writer, wired with main's; at import, with the wall's clock and the process's
    store, for a use before any main. Returns the inbox writer wired (main's context carries it)."""
    wire_clock(clock)
    inbox = InboxWriter(condition_guarded(lambda rec: alert_write(rec, store)), clock.time)
    wire_inbox_writer(inbox)
    return inbox


wire_collaborators(Clock(), CurrentStore(process_store))


def with_store(command: Callable[..., Any], store: Any) -> Callable[[argparse.Namespace], Any]:
    """The command that takes the process's store as its second argument, as the handler table calls it: with the args."""
    return lambda a: command(a, store)


def waited_on(args: list[str]) -> bool:
    """A command someone waits on at a terminal (`status`, `land status`): its store calls are bounded and never
    retried (server.py process_store waited_on); `status --line` reads no store at all."""
    words = [w for w in args if not w.startswith("-")]
    return words[:1] == ["status"] or words[:2] == ["land", "status"]


# ---------------------------------------------------------------- main
def main(argv: list[str] | None = None, clock: Clock | None = None, runner: ProcessRunner | None = None,
         store: Any = None) -> Any:
    """The entry: wires the process's collaborators (the clock, the process runner and the store: a test passes its
    own) and runs the command."""
    clock = clock or Clock()
    args = sys.argv[1:] if argv is None else argv
    store = store if store is not None else CurrentStore(
        partial(process_store, waited_on=True) if waited_on(args) else process_store, clock.time, actor=process_actor(args))
    inbox = wire_collaborators(clock, store)
    runner = runner or ProcessRunner()
    wire_runner(runner)
    machine = process_machine()
    ctx = process_context(clock, runner, inbox, machine)
    registry = condition_registry(clock, store, ctx)
    if (sys.argv[1:] if argv is None else argv) == ["--selftest"]:
        return selftest(ctx)
    a = parser().parse_args(argv)
    handler: Callable[[argparse.Namespace], Any] = {"maintain": with_store(partial(cmd_maintain, machine=machine), store), "reap": lambda a: cmd_orphans(argparse.Namespace(kill=not a.dry_run, safe=True), machine),
               "red": lambda a: cmd_red(a, clock, store, registry, ctx=ctx), "decide": with_store(partial(cmd_decide, ctx=ctx), store), "config": cmd_config, "issue": with_store(cmd_issue, store), "revert": with_store(cmd_revert, store), "dispatch": cmd_dispatch, "gov": lambda a: cmd_gov(a, machine), "suite": cmd_suite, "checks": lambda a: cmd_checks(a, machine), "inbox": with_store(cmd_inbox, store), "recover": lambda a: cmd_recover(a, store, registry), "watchdog": lambda a: cmd_watchdog(a, clock, store, registry, ctx=ctx), "lease": with_store(cmd_lease, store), "laptop": with_store(cmd_laptop, store), "status": with_store(partial(cmd_status, ctx=ctx), store), "queue": cmd_queue_retired, "tick": lambda a: cmd_tick(a, clock, store, registry, ctx=ctx), "store": lambda a: cmd_store(a, ctx=ctx), "github-app": cmd_github_app, "waiting": lambda a: cmd_waiting(a, clock, store, registry, ctx=ctx), "wait": cmd_wait, "state": cmd_state, "prepush-check": cmd_prepush, "cleanup": cmd_cleanup, "orphans": lambda a: cmd_orphans(a, machine), "supersede": with_store(cmd_supersede, store), "park": with_store(cmd_park, store), "unpark": cmd_unpark, "watch": lambda a: cmd_watch(a, clock, store, ctx=ctx), "sweep": with_store(partial(cmd_sweep, machine=machine), store), "ci-why": with_store(partial(cmd_ci_why, ctx=ctx), store), "land": lambda a: cmd_land(a, clock, store, ctx=ctx), "land-precheck": with_store(partial(cmd_land_precheck, ctx=ctx), store), "env": cmd_env, "history": lambda a: cmd_history(a, store, clock.time), "analyze": lambda a: cmd_analyze(a, store, clock.time)}[a.cmd]
    command = inbox_command(a)
    try:
        return with_inbox(command, handler, a) if command else handler(a)
    finally:
        note_missing_tokens(store)  # what a missing token left to note: a condition, in the command's store


def selftest(ctx: Context) -> int:
    """Fast smoke check of the code a service would re-exec onto: the parser builds, the failure parsers and the
    snapshot readers work on fixtures, a fixture gate reads green."""
    snap = {"at": ctx.clock.time(), "repo": "x/y", "outside_error": None, "outside": [],
            "gate": [{"name": "Main", "decided": {"databaseId": 1, "number": 1, "status": "completed",
                                                  "conclusion": "success"}, "newer": None, "since": None, "count": 0,
                      "runs": []}]}
    assert gate_from_snap(snap, ctx=ctx)[0][0] == "Main" and outside_from_snap(snap, ctx=ctx) == []
    assert set(check_failures("! pre-push sbt FAILED (3s)\n")) == {"sbt"}
    assert age_since("2026-01-01T00:00:00Z", dt.datetime(2026, 1, 1, 1, 5, tzinfo=dt.timezone.utc)) == "1h 5m"
    assert callable(recheck_local) and callable(restart_gate)
    try:
        with contextlib.redirect_stdout(io.StringIO()):
            main(["ci-why", "--help"])
    except SystemExit as e:
        assert e.code == 0
    print("selftest ok")
    return 0
