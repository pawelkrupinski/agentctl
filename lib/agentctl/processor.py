# agentctl, part `processor`: the processor: its loop of ticks, `agentctl tick` and the old `waiting` command's names
# A real module (lib/agentctl/__init__.py MODULES): its own imports, the standard library's and MODULES' only.

import argparse
import contextlib
import os
import signal
from typing import Any, Callable, Sequence
from agentctl.statefile import write_json
from agentctl.selfcode import code_version
from agentctl.statemachines import MACHINES, machine_state
from agentctl.clock import Clock, epoch_now
from agentctl.tickknobs import knob_step_join, knob_tick
from agentctl.core import (
    NoToken, SHORT, _bounded, attempt, err, git, git_ok, host_role, process_cache, run_bounded, slug)
from agentctl.repos import movies_repo_path
from agentctl.procs import safe_cwd
from agentctl.clones import clone_shared
from agentctl.beats import write_beat
from agentctl.sdnotify import SD_READY, sd_notify
from agentctl.waitingdir import ChangeRecords, waiting_state
from agentctl.storetoken import _IN_PROCESSOR
from agentctl.cistate import state_update
from agentctl.conditions import open_condition, run_probe
from agentctl.serverclone import ensure_server_clone
from agentctl.owned import restart_if_changed, start_rev
from agentctl.processorlink import TokenDrop, processor_owned
from agentctl.hostsleep import keep_awake
from agentctl.landhold import hold_row, land_hold
from agentctl.reverts import revert_brief
from agentctl.changekind import HOLD_SHOWN
from agentctl.ask import owner_asks
from agentctl.landqueue import land_await, queue_checker, queue_command, waiting_repo
from agentctl.serversteps import server_steps
from agentctl.statuspage import status_page_start
from agentctl.syncsteps import sync_steps
from agentctl.changerows import change_rows


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
        return self.once and (self.role == "sync" or not ChangeRecords(self.store, self.d).open())

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
    launchd agent."""
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
                           action=a.action)
    if a.tcmd == "service" and not a.action:
        print("usage: agentctl tick service install|uninstall|status")
        return 2
    return cmd_waiting(w, clock, store)


def cmd_waiting(a: argparse.Namespace, clock: Clock, store: Any) -> int:
    if a.wcmd == "run":
        return waiting_run(waiting_repo(a), clock, store, once=a.once, service=getattr(a, "service", False))
    if a.wcmd == "await":  # land's wait on its entry, resumed after a restart on new code
        return land_await(a, waiting_repo(a), clock, store)
    if a.wcmd in ("check", "check-batch"):
        return queue_checker(a, clock, store)
    return queue_command(a, store)


ROUND_ERRORS_MAX = 3


_step_errors = process_cache({})


def run_step(name: str, step: Callable[[], Any]) -> bool | None:
    """One tick step, bounded (see above). True: done; False: it failed (now or, still running before, since);
    None: still running in its thread."""
    def work():
        try:
            step()
            _step_errors[name] = None
        except NoToken:  # token_missing said it, on its transition: a step never repeats it every tick
            _step_errors[name] = STEP_NO_TOKEN
        except (Exception, SystemExit) as e:  # one step's error is that step's, never the loop's (audit #9)
            _step_errors[name] = str(e).replace("agentctl: ", "")[:200]
    t = _bounded.get(f"step:{name}")
    if t and t.is_alive():
        return None
    if _step_errors.get(name):  # it failed after the tick stopped waiting for it
        step_failed(name, _step_errors.pop(name))
    if not run_bounded(f"step:{name}", work, knob_step_join()):
        err(f"{name}: still running after {knob_step_join():.0f} s; the tick goes on (it ends "
            "in its own thread)")
        return None
    why = _step_errors.pop(name, None)
    if why:
        step_failed(name, why)
        return False
    return True


STEP_NO_TOKEN = "no token"


def step_failed(name, why):
    if why is not STEP_NO_TOKEN:
        err(f"{name} failed: {why}")


def _open_items(st: Any, typ: str) -> list[Any]:
    """The store's items of machine `typ` not yet terminal."""
    return [i for i in st.items(typ) if typ in MACHINES and not machine_state(typ, i["state"]).terminal]


def _brief(i: Any) -> dict[str, Any]:
    """One open item as the snapshot shows it: its state, deadline, level, worktree and (a held one's) why."""
    data = i["data"] or {}
    hold = {"hold": data["hold"].get("why")} if i["state"] in HOLD_SHOWN and data.get("hold") else {}
    return {"id": i["id"], "state": i["state"], "deadline": i["deadline"], "level": i["level"],
            "worktree": data.get("worktree"), **hold}


def _briefs(st: Any, typ: str, brief: Any = _brief) -> list[Any]:
    """The briefs of machine `typ`'s open items; none for a machine this build has not got."""
    return [brief(i) for i in _open_items(st, typ)] if typ in MACHINES else []


def write_snapshot(d: str, store: Any) -> None:
    """The tick's part of state.json: the open items and the tick's own time and code version."""
    st = store
    rows = change_rows(st)  # ONE QUEUE: the store's items, never a host's entry files
    state_update(tick_at=epoch_now(), version=code_version(), rev=start_rev(),
                 changes=[_brief(i) for i in _open_items(st, "change")], batch=[_brief(i) for i in _open_items(st, "batch")],
                 reds=_briefs(st, "red"), conditions=_briefs(st, "condition"),
                 reverts=_briefs(st, "revert", revert_brief),
                 asks=owner_asks(st) if "red" in MACHINES else [],  # THE OWNER'S PENDING DECISIONS (ask.py)
                 hold=hold_row(land_hold(st)),
                 waiting=[r["worktree"] for r in rows if r["state"] == "waiting"],
                 queued=[r["worktree"] for r in rows if r["state"] != "waiting"])
