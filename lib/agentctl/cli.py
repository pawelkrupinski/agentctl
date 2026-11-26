# agentctl, part `cli`: the command line (main: the parser and the handlers' table).
# A real module (lib/agentctl/__init__.py MODULES): its own imports, the standard library's and MODULES' only.

import argparse
import contextlib
import datetime as dt
import io
import sys
import time
from typing import Any, Callable
from agentctl.procrunner import ProcessRunner, wire_runner
from agentctl.clock import Clock, epoch_now, wire_clock
from agentctl.ghapp import cmd_github_app
from agentctl.config import cmd_config
from agentctl.storetoken import wire_token_missing_note
from agentctl.inboxentry import wire_inbox_writer
from agentctl.owned import cmd_wait, restart_gate
from agentctl.inbox import inbox_command, with_inbox
from agentctl.maint import cmd_orphans
from agentctl.ciwhy import gate_from_snap, outside_from_snap, red_for
from agentctl.actors import process_actor
from agentctl.timelinecmd import cmd_analyze, cmd_history
from agentctl.store import CurrentStore
from agentctl.leases import cmd_lease
from agentctl.reverts import cmd_revert
from agentctl.cleanup import cmd_sweep
from agentctl.conditions import cmd_recover, wire_probes, wire_recover_actions
from agentctl.upkeep import probe_disk_free
from agentctl.queue import probe_infra_requeue, probe_push_credential
from agentctl.issues import cmd_issue
from agentctl.laptoprole import cmd_laptop
from agentctl.serverclone import probe_clone_health
from agentctl.branches import cmd_dispatch
from agentctl.redcrash import probe_red_newer_verdict
from agentctl.gov import cmd_suite
from agentctl.localrecheck import check_failures, recheck_local
from agentctl.govcmd import cmd_checks, cmd_gov
from agentctl.alerts import alert_write
from agentctl.ask import cmd_decide
from agentctl.watchtick import cmd_watch
from agentctl.recovery import (
    cmd_inbox, probe_gate_read, probe_git_fetch, probe_http, probe_route, probe_tcp, probe_token, probe_tunnel,
    probe_waiting_ok, probe_workflow_green, recover_clear_flag, recover_clear_hold, recover_tunnel_reset,
    token_unavailable_note)
from agentctl.tools import cmd_env, cmd_prepush, cmd_queue_retired, cmd_state
from agentctl.queueclose import probe_change_source
from agentctl.superseded import cmd_supersede
from agentctl.park import cmd_park, cmd_unpark
from agentctl.orphans import probe_orphan_landed
from agentctl.maintain import cmd_maintain
from agentctl.batch import probe_batch_progress
from agentctl.cleanuppass import cmd_cleanup
from agentctl.server import cmd_store, process_store
from agentctl.invariants import probe_invariant
from agentctl.watchdog import cmd_watchdog
from agentctl.status import cmd_ci_why
from agentctl.statuspage import cmd_status
from agentctl.queueenter import cmd_land_precheck
from agentctl.slots import cmd_land
from agentctl.cliparser import parser
from agentctl.redtick import cmd_red, probe_red_read
from agentctl.processor import cmd_tick, cmd_waiting
from agentctl.redquota import probe_quota_reset


# THE COMPOSITION ROOT: what every process wires before any command runs.
wire_probes({"disk_free": probe_disk_free, "quota_reset": probe_quota_reset,
             "waiting_ok": probe_waiting_ok, "workflow_green": probe_workflow_green,
             "http": probe_http, "tcp": probe_tcp, "route": probe_route, "tunnel": probe_tunnel,
             "gate_read": probe_gate_read, "token": probe_token, "git_fetch": probe_git_fetch,
             "clone_health": probe_clone_health,
             "push_credential": probe_push_credential,
             "infra_requeue": probe_infra_requeue,
             "batch_progress": probe_batch_progress, "red_read": probe_red_read})
wire_recover_actions({"clear_hold": recover_clear_hold, "tunnel_reset": recover_tunnel_reset,
                      "clear_flag": recover_clear_flag})


def wire_collaborators(clock: Clock, store: Any) -> None:
    """What a registry calls with the process's clock and store -- the probes that read them (the watchdog's
    invariants, a change's source, a red's newer run), the inbox writer, the missing token's note -- wired with main's;
    at import, with the wall's clock and the process's store, for a use before any main."""
    wire_clock(clock)
    wire_probes({"orphan_landed": lambda a: probe_orphan_landed(a, store), "invariant": lambda a: probe_invariant(a, clock, store),
                 "change_source": lambda a: probe_change_source(a, store),
                 "red_newer_verdict": lambda a: probe_red_newer_verdict(a, store)})
    wire_inbox_writer(lambda rec: alert_write(rec, store), clock.time)
    wire_token_missing_note(lambda ns, why: token_unavailable_note(ns, why, store))


wire_collaborators(Clock(), CurrentStore(process_store))


def with_store(command: Callable[..., Any], store: Any) -> Callable[[argparse.Namespace], Any]:
    """The command that takes the process's store as its second argument, as the handler table calls it: with the args."""
    return lambda a: command(a, store)


# ---------------------------------------------------------------- main
def main(argv: list[str] | None = None, clock: Clock | None = None, runner: ProcessRunner | None = None,
         store: Any = None) -> Any:
    """The entry: wires the process's collaborators (the clock, the process runner and the store: a test passes its
    own) and runs the command."""
    clock = clock or Clock()
    store = store if store is not None else CurrentStore(
        process_store, clock.time, actor=process_actor(sys.argv[1:] if argv is None else argv))
    wire_collaborators(clock, store)
    wire_runner(runner or ProcessRunner())
    if (sys.argv[1:] if argv is None else argv) == ["--selftest"]:
        return selftest()
    a = parser().parse_args(argv)
    handler: Callable[[argparse.Namespace], Any] = {"maintain": with_store(cmd_maintain, store), "reap": lambda a: cmd_orphans(argparse.Namespace(kill=not a.dry_run, safe=True)),
               "red": lambda a: cmd_red(a, clock, store), "decide": with_store(cmd_decide, store), "config": cmd_config, "issue": with_store(cmd_issue, store), "revert": with_store(cmd_revert, store), "dispatch": cmd_dispatch, "gov": cmd_gov, "suite": cmd_suite, "checks": cmd_checks, "inbox": with_store(cmd_inbox, store), "recover": with_store(cmd_recover, store), "watchdog": lambda a: cmd_watchdog(a, clock, store), "lease": with_store(cmd_lease, store), "laptop": with_store(cmd_laptop, store), "status": with_store(cmd_status, store), "queue": cmd_queue_retired, "tick": lambda a: cmd_tick(a, clock, store), "store": cmd_store, "github-app": cmd_github_app, "waiting": lambda a: cmd_waiting(a, clock, store), "wait": cmd_wait, "state": cmd_state, "prepush-check": cmd_prepush, "cleanup": cmd_cleanup, "orphans": cmd_orphans, "supersede": with_store(cmd_supersede, store), "park": with_store(cmd_park, store), "unpark": cmd_unpark, "watch": lambda a: cmd_watch(a, clock, store), "sweep": with_store(cmd_sweep, store), "ci-why": with_store(cmd_ci_why, store), "land": lambda a: cmd_land(a, clock, store), "land-precheck": with_store(cmd_land_precheck, store), "env": cmd_env, "history": lambda a: cmd_history(a, store, clock.time), "analyze": lambda a: cmd_analyze(a, store, clock.time)}[a.cmd]
    command = inbox_command(a)
    return with_inbox(command, handler, a) if command else handler(a)


def selftest():
    """Fast smoke check of the code a service would re-exec onto: the parser builds, the failure parsers and the
    snapshot readers work on fixtures, a fixture gate reads green."""
    snap = {"at": epoch_now(), "repo": "x/y", "outside_error": None, "outside": [],
            "gate": [{"name": "Main", "decided": {"databaseId": 1, "number": 1, "status": "completed",
                                                  "conclusion": "success"}, "newer": None, "since": None, "count": 0,
                      "runs": []}]}
    assert gate_from_snap(snap)[0][0] == "Main" and outside_from_snap(snap) == []
    assert set(check_failures("! pre-push sbt FAILED (3s)\n")) == {"sbt"}
    assert red_for("2026-01-01T00:00:00Z", dt.datetime(2026, 1, 1, 1, 5, tzinfo=dt.timezone.utc)) == "1h 5m"
    assert callable(recheck_local) and callable(restart_gate)
    try:
        with contextlib.redirect_stdout(io.StringIO()):
            main(["ci-why", "--help"])
    except SystemExit as e:
        assert e.code == 0
    print("selftest ok")
    return 0
