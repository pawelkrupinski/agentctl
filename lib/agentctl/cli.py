# agentctl, part `cli`: the command line (main: the parser and the handlers' table).
# A real module (lib/agentctl/__init__.py MODULES): its own imports, the standard library's and MODULES' only.

import argparse
import contextlib
import datetime as dt
import io
import sys
import time
from typing import Any, Callable
from agentctl.store import CurrentStore, wire_store
from agentctl.ghapp import cmd_github_app
from agentctl.config import cmd_config
from agentctl.storetoken import wire_token_missing_note
from agentctl.leases import cmd_lease
from agentctl.inboxentry import wire_inbox_writer
from agentctl.owned import cmd_wait, restart_gate
from agentctl.reverts import cmd_revert
from agentctl.inbox import inbox_command, with_inbox
from agentctl.maint import cmd_orphans
from agentctl.cleanup import cmd_cleanup, cmd_sweep
from agentctl.conditions import cmd_recover, wire_probes, wire_recover_actions
from agentctl.upkeep import probe_disk_free
from agentctl.queue import probe_infra_requeue, probe_push_credential
from agentctl.issues import cmd_issue
from agentctl.alerts import alert_write
from agentctl.laptoprole import cmd_laptop
from agentctl.gov import cmd_suite
from agentctl.govcmd import cmd_checks, cmd_gov
from agentctl.ciwhy import gate_from_snap, outside_from_snap, red_for
from agentctl.serverclone import probe_clone_health
from agentctl.branches import cmd_dispatch
from agentctl.localrecheck import check_failures, recheck_local
from agentctl.redcrash import probe_red_newer_verdict
from agentctl.redvault import probe_quota_reset
from agentctl.ask import cmd_decide
from agentctl.watchtick import cmd_watch
from agentctl.queueclose import probe_change_source
from agentctl.recovery import (
    cmd_inbox, probe_gate_read, probe_git_fetch, probe_http, probe_route, probe_tcp, probe_token, probe_tunnel,
    probe_waiting_ok, probe_workflow_green, recover_clear_flag, recover_clear_hold, recover_tunnel_reset,
    token_unavailable_note)
from agentctl.tools import cmd_env, cmd_prepush, cmd_queue_retired, cmd_state
from agentctl.superseded import cmd_supersede
from agentctl.park import cmd_park, cmd_unpark
from agentctl.orphans import probe_orphan_landed
from agentctl.maintain import cmd_maintain
from agentctl.invariants import probe_invariant
from agentctl.watchdog import cmd_watchdog
from agentctl.server import cmd_store, process_store
from agentctl.status import cmd_ci_why
from agentctl.statuspage import cmd_status
from agentctl.redtick import cmd_red, probe_red_read
from agentctl.batch import probe_batch_progress
from agentctl.queueenter import cmd_land_precheck
from agentctl.slots import cmd_land
from agentctl.clock import Clock
from agentctl.procrunner import ProcessRunner, wire_runner
from agentctl.cliparser import parser
from agentctl.processor import cmd_tick, cmd_waiting


# THE COMPOSITION ROOT: what every process wires before any command runs.
wire_store(process_store)
wire_token_missing_note(token_unavailable_note)
wire_inbox_writer(alert_write)
wire_probes({"disk_free": probe_disk_free, "quota_reset": probe_quota_reset,
             "waiting_ok": probe_waiting_ok, "orphan_landed": probe_orphan_landed, "workflow_green": probe_workflow_green,
             "http": probe_http, "tcp": probe_tcp, "route": probe_route, "tunnel": probe_tunnel,
             "gate_read": probe_gate_read, "token": probe_token, "git_fetch": probe_git_fetch,
             "clone_health": probe_clone_health, "change_source": probe_change_source,
             "push_credential": probe_push_credential,
             "infra_requeue": probe_infra_requeue, "red_newer_verdict": probe_red_newer_verdict,
             "batch_progress": probe_batch_progress, "red_read": probe_red_read})
wire_recover_actions({"clear_hold": recover_clear_hold, "tunnel_reset": recover_tunnel_reset,
                      "clear_flag": recover_clear_flag})


def wire_collaborators(clock: Clock, store: Any) -> None:
    """What reads the process's clock and store from a registry (the `invariant` probe: the watchdog's invariants)
    wired with main's; at import, with the wall's clock and the process's store, for a probe run before any main."""
    wire_probes({"invariant": lambda a: probe_invariant(a, clock, store)})


wire_collaborators(Clock(), CurrentStore())


# ---------------------------------------------------------------- main
def main(argv: list[str] | None = None, clock: Clock | None = None, runner: ProcessRunner | None = None,
         store: Any = None) -> Any:
    """The entry: wires the process's collaborators (the clock, the process runner and the store: a test passes its
    own) and runs the command."""
    clock = clock or Clock()
    store = store if store is not None else CurrentStore(clock.time)
    wire_collaborators(clock, store)
    wire_runner(runner or ProcessRunner())
    if (sys.argv[1:] if argv is None else argv) == ["--selftest"]:
        return selftest()
    a = parser().parse_args(argv)
    handler: Callable[[argparse.Namespace], Any] = {"maintain": cmd_maintain, "reap": lambda a: cmd_orphans(argparse.Namespace(kill=not a.dry_run, safe=True)),
               "red": lambda a: cmd_red(a, clock, store), "decide": cmd_decide, "config": cmd_config, "issue": cmd_issue, "revert": cmd_revert, "dispatch": cmd_dispatch, "gov": cmd_gov, "suite": cmd_suite, "checks": cmd_checks, "inbox": cmd_inbox, "recover": cmd_recover, "watchdog": lambda a: cmd_watchdog(a, clock, store), "lease": cmd_lease, "laptop": cmd_laptop, "status": cmd_status, "queue": cmd_queue_retired, "tick": lambda a: cmd_tick(a, clock, store), "store": cmd_store, "github-app": cmd_github_app, "waiting": lambda a: cmd_waiting(a, clock, store), "wait": cmd_wait, "state": cmd_state, "prepush-check": cmd_prepush, "cleanup": cmd_cleanup, "orphans": cmd_orphans, "supersede": cmd_supersede, "park": cmd_park, "unpark": cmd_unpark, "watch": lambda a: cmd_watch(a, clock, store), "sweep": cmd_sweep, "ci-why": cmd_ci_why, "land": lambda a: cmd_land(a, clock, store), "land-precheck": lambda a: cmd_land_precheck(a, store), "env": cmd_env}[a.cmd]
    command = inbox_command(a)
    return with_inbox(command, handler, a) if command else handler(a)


def selftest():
    """Fast smoke check of the code a service would re-exec onto: the parser builds, the failure parsers and the
    snapshot readers work on fixtures, a fixture gate reads green."""
    snap = {"at": time.time(), "repo": "x/y", "outside_error": None, "outside": [],
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
