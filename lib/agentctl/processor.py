# agentctl, part `processor`: the processor: its loop of ticks, `agentctl tick` and the old `waiting` command's names
# A real module (lib/agentctl/__init__.py MODULES): its own imports, the standard library's and MODULES' only.

import argparse
import contextlib
import os
import signal
import sys
from typing import Any, Callable, Sequence
from agentctl.clock import Clock
from agentctl.core import SHORT, _bounded, attempt, err, git, git_ok, host_role, slug
from agentctl.statefile import write_json
from agentctl.repos import movies_repo_path
from agentctl.waitingdir import waiting_entries, waiting_state
from agentctl.storetoken import _IN_PROCESSOR
from agentctl.procs import safe_cwd
from agentctl.owned import restart_if_changed
from agentctl.clones import clone_shared
from agentctl.conditions import open_condition, run_probe
from agentctl.queuerecheck import queued_entries
from agentctl.beats import write_beat
from agentctl.sdnotify import SD_READY, sd_notify
from agentctl.serverclone import ensure_server_clone
from agentctl.tickrun import ROUND_ERRORS_MAX, run_step
from agentctl.redcrash import keep_awake
from agentctl.snapshot import write_snapshot
from agentctl.landqueue import land_await, queue_checker, queue_command, waiting_repo
from agentctl.statuspage import status_page_start
from agentctl.syncsteps import sync_steps
from agentctl.serversteps import server_steps
from agentctl.processorlink import TokenDrop, processor_owned
from agentctl.tickknobs import knob_tick


WAITING_RENAMED = {"status": "land status", "pause": "land hold", "resume": "land release", "cancel": "land cancel <id>",
                   "run": "tick", "service": "tick service"}


ALIASES_UNTIL = "2026-11-05"  # the old command names (design §14.5) answer until then, each naming its new one
# TODO(2026-11-05): drop `waiting` and WAITING_RENAMED. NOT BEFORE the laptop's launchd job stops calling `waiting run
# --service` (as installed 2026-10-29): the owner reinstalls it with `agentctl tick service install` (which kickstarts
# it and needs the token handed over again), then the alias goes -- removing it first would stop the sync.


class ProcessorLoop:
    """The loop's state (the failed rounds in a row, whether the stall is noted) and a method per stage of a round: the
    steps, the stall note, the round's files, the pause."""

    def __init__(self, repo: str, d: str, clock: Clock, store: Any, own: Any, drop: Any, steps: Sequence[Any],
                 role: str, once: bool, argv: list[str]) -> None:
        self.repo, self.d, self.clock, self.store, self.own, self.drop = repo, d, clock, store, own, drop
        self.steps, self.role, self.once, self.argv = steps, role, once, argv
        self.errors, self.stalled_noted = 0, False

    def run_steps(self) -> bool:
        """Every step of the round, bounded; whether any failed."""
        write_beat("processor")
        if not any(t.is_alive() for k, t in _bounded.items() if k.startswith("step:")):
            restart_if_changed([self.own], self.argv)  # never under a step still at work in its thread (a push, say)
        failed = False
        for name, step in self.steps:
            failed = run_step(name, step) is False or failed
            write_beat("processor")
            sd_notify("WATCHDOG=1")  # each bounded step is progress: systemd's WatchdogSec never cuts a long tick
        return failed

    def note_stall(self) -> None:
        """NEVER FATAL (2026-10-29 22:31: the network dropped, every step failed, and this condition's own store
        call -- the store is remote on the laptop -- raised StoreUnavailable out of the loop: the processor
        died, launchd restarted it, and the restart holds no token until the owner hands it over).
        Not noted now: tried again next round."""
        try:
            open_condition("STALLED", f"STALLED: the waiting processor's ticks failed {self.errors} times in a row; "
                                      f"see {self.d}/processor.log and service.log",
                           run_probe("waiting_ok", {"d": self.d, "since": self.clock.time()}), [], key="waiting-rounds",
                           command="waiting", store=self.store)
            self.stalled_noted = True
        except (Exception, SystemExit) as e:
            err(f"STALLED could not be noted ({str(e)[:160]}); the loop goes on")

    def settle(self, failed: bool) -> None:
        """What a round leaves: the failures in a row (and the stall note they earn), the round file, the snapshot."""
        self.errors = self.errors + 1 if failed else 0
        if self.errors == 0:
            self.stalled_noted = False
        if self.errors >= ROUND_ERRORS_MAX and not self.stalled_noted:
            self.note_stall()
        if not failed:
            write_json(os.path.join(self.d, "round-ok.json"), {"at": self.clock.time()})
        if self.role != "sync":  # the sync's state.json is ci-runner's, mirrored
            try:
                write_snapshot(self.d, self.store)
            except Exception as e:
                err(f"the snapshot could not be written: {e}")
        sd_notify("WATCHDOG=1")

    def drained(self) -> bool:
        return self.once and (self.role == "sync" or (not queued_entries(self.d) and not waiting_entries(self.d)))

    def run(self) -> int:
        while not TICK_STOP[0]:  # SIGTERM (systemd's stop): between ticks, never inside one
            self.settle(self.run_steps())
            if self.drained():
                return 0
            self.drop.nap(knob_tick())
        return 0


def waiting_run(repo: str, clock: Clock, store: Any, once: bool = False, service: bool = False) -> int:
    """THE processor: ONE loop of ticks (design §3). On ci-runner (server_steps): the change items (executors,
    pushes), the batch, the watches, the gate line, the red tick, the recovery registry. On the laptop (sync_steps):
    the outbox, the state mirror, events -> inbox and the notifier, claims, the fixer dispatcher, the network and
    tunnel watches, upkeep. Nothing in it blocks for long (checks run in executors with leases); it re-execs
    onto agentctl's new code between ticks; the external watchdog (W5) watches its beat."""
    d, _c, _wt = waiting_state(repo)
    own = processor_owned(d, repo)
    if not own.acquire():
        return 0
    argv = ["waiting", "run", "--repo", repo] + (["--once"] if once else []) + (["--service"] if service else [])
    with contextlib.ExitStack() as stack:
        stack.callback(own.release)
        o_r = attempt(lambda: slug(repo), SHORT["movies"], catch=(Exception, SystemExit))
        _IN_PROCESSOR[0] = True
        drop = TokenDrop(d, o_r, clock).open()
        stack.callback(drop.close)
        role = host_role()
        if service:
            keep_awake()  # a plugged-in laptop never sleeps under the service (no-op off macOS)
        if service and role != "server":
            status_page_start()  # the read-only status page, a daemon thread (see "the status page"); never fatal

        def shared(fn: Callable[[], Any]) -> Callable[[], Any]:  # a step that uses the clone holds it shared: a repair's swap waits for it
            def run() -> Any:
                with clone_shared(repo):
                    return fn()
            return run
        steps = sync_steps(repo, d, o_r, clock, store) if role == "sync" else server_steps(repo, d, shared, clock, store)
        return ProcessorLoop(repo, d, clock, store, own, drop, steps, role, once, argv).run()


TICK_STOP = [False]  # set by SIGTERM in `agentctl tick`: the loop returns between ticks


def cmd_tick(a: argparse.Namespace, clock: Clock, store: Any) -> int:
    """`agentctl tick`: the processor (the reconciler's loop of ticks); `tick service install|uninstall|status`: it as the
    launchd agent (design §14.5: `waiting run` / `waiting service`, renamed)."""
    if host_role() == "server":  # ci-runner: its own clone, systemd's notify protocol, a clean stop between ticks
        repo = ensure_server_clone(a.repo, store)
        signal.signal(signal.SIGTERM, lambda *_: TICK_STOP.__setitem__(0, True))
        store.db().close()  # the database exists (its schema) before systemd hears READY
        sd_notify("READY=1")
        SD_READY[0] = True
    else:
        repo = a.repo or (git(safe_cwd(), "rev-parse", "--show-toplevel", check=False).stdout.strip()
                          if git_ok(safe_cwd(), "rev-parse", "--show-toplevel") else movies_repo_path())
    w = argparse.Namespace(wcmd="service" if a.tcmd == "service" else "run", repo=repo, once=a.once, service=not a.once,
                           action=a.action, renamed=True)
    if a.tcmd == "service" and not a.action:
        print("usage: agentctl tick service install|uninstall|status")
        return 2
    return cmd_waiting(w, clock, store)


def cmd_waiting(a: argparse.Namespace, clock: Clock, store: Any) -> int:
    if a.wcmd in WAITING_RENAMED and not getattr(a, "renamed", False) and \
            (a.wcmd not in ("run", "service") or sys.stdin.isatty()):
        err(f"`agentctl waiting {a.wcmd}` is now `agentctl {WAITING_RENAMED[a.wcmd]}` (the old name answers until "
            f"{ALIASES_UNTIL})")
    if a.wcmd == "run":
        return waiting_run(waiting_repo(a), clock, store, once=a.once, service=getattr(a, "service", False))
    if a.wcmd == "await":  # land's wait on its entry, resumed after a restart on new code
        return land_await(a, waiting_repo(a), clock, store)
    if a.wcmd in ("check", "check-batch"):
        return queue_checker(a, clock, store)
    return queue_command(a, store)
