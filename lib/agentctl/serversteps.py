# agentctl, part `serversteps`: the reconciler's tick on ci-runner: its steps in order, the red-time gauge and the quiet deadlines
# A real module (lib/agentctl/__init__.py MODULES): its own imports, the standard library's and MODULES' only.

from typing import Any, Callable, Sequence
from agentctl.conditionregistry import ConditionRegistry
from agentctl.statemachines import StaleVersion, TerminalState, machine_state
from agentctl.clock import Clock
from agentctl.core import reap_children
from agentctl.repos import slug
from agentctl.filelog import drain_event_log
from agentctl.waitingdir import wlog
from agentctl.inboxentry import inbox_write
from agentctl.cistate import snapshot_gate_reds
from agentctl.stats import maybe_stats_pass
from agentctl.timelinecmd import timeline_step
from agentctl.context import Context
from agentctl.landhold import tick_land_hold
from agentctl.serverhousekeeping import archive_leases_step, expire_entry_files_step
from agentctl.serverclone import clone_health_lines
from agentctl.queuestall import queue_stall_step
from agentctl.reverts import tick_reverts
from agentctl.issues import tick_issues
from agentctl.queuesubmit import sweep_land_refs
from agentctl.checkrun import remote_run_jobs
from agentctl.crashloop import crash_loop_step
from agentctl.watchtick import tick_watches
from agentctl.recovery import maybe_recovery_tick
from agentctl.redtick import maybe_red_tick
from agentctl.gatepublish import maybe_publish_gate
from agentctl.landedchange import close_landed_changes, stranded_changes
from agentctl.batch import say_batch
from agentctl.queuepush import tick_changes
from agentctl.batchtick import tick_batch


def gate_is_red_now() -> bool:
    return bool(snapshot_gate_reds())


def track_red_time(st, red, now_=None):
    """meta red_accum: the seconds main has been red, summed (the W1 gauge leaves them out of a change's age)."""
    now_ = now_ if now_ is not None else st.clock()
    last = float(st.meta_get("red_accum_at", 0) or 0)
    acc = float(st.meta_get("red_accum", 0) or 0)
    if red and last:
        acc += max(0.0, min(now_ - last, 600))
        st.meta_set("red_accum", acc)
    st.meta_set("red_accum_at", now_)
    return acc


# WHO MOVES EACH MACHINE PAST ITS DEADLINE (2026-11-12, issue 20261111T124954-ec04bd: `issue` and `lander` had a
# deadline and no tick, so AgentctlItemsPastDeadline fired for days). Every machine is named here with the step that
# fires its on_deadline; test_machines refuses a machine that is not. QUIET_DEADLINE_TYPES are the machines whose
# deadline exit is a quiet retirement nobody else ticks: tick_quiet_deadlines moves them.
DEADLINE_TICKS = {"change": "tick_changes", "batch": "tick_batch", "watch": "tick_watches", "hold": "tick_land_hold",
                  "red": "red_tick", "branch-red": "settle_branch_reds", "condition": "recovery_tick",
                  "issue": "tick_issues", "lander": "tick_quiet_deadlines", "explorer-flip": "tick_quiet_deadlines",
                  "revert": "tick_reverts"}


QUIET_DEADLINE_TYPES = tuple(k for k, v in DEADLINE_TICKS.items() if v == "tick_quiet_deadlines")


def _expire(st: Any, typ: Any, it: Any, t: Any) -> Any:
    """Move item it to its deadline state when it is past it (an info event); the line saying so, else None."""
    spec = machine_state(typ, it["state"])
    if spec.terminal or it["deadline"] > t:
        return None
    try:
        st.transition(typ, it["id"], spec.on_deadline, it["version"],
                      event={"cause": "timeout", "text": f"{typ} {it['id']}: {it['state']} past its deadline -> {spec.on_deadline}",
                             "severity": "info"})
    except (StaleVersion, TerminalState):
        return None
    return f"{typ} {it['id']}: {spec.on_deadline}"


def tick_quiet_deadlines(st, now_=None):
    """The reconciler's step: each lander record past its 14 days goes `gone` (blame reads landings that recent) and
    each forgotten explorer flip `closed`, an info event apiece (no inbox entry, no notification). Returns what it did."""
    t = now_ if now_ is not None else st.clock()
    did = []
    for typ in QUIET_DEADLINE_TYPES:
        for it in st.items(typ):
            line = _expire(st, typ, it, t)
            if line:
                did.append(line)
    return did


def logged(d: str, lines: list[str]) -> list[str]:
    """A step's lines, each said in the processor's log."""
    for x in lines:
        wlog(d, f"tick: {x}")
    return lines


def owner_summary(line: str, sinks: list[str]) -> None:
    """The weekly time-sinks summary, to the owner's inbox."""
    inbox_write("analyze", line, [line], sinks, 0)


def server_steps(repo: str, d: str, shared: Callable[[Callable[[], Any]], Callable[[], Any]],
                 clock: Clock, store: Any, registry: ConditionRegistry, *, ctx: Context) -> Sequence[tuple[str, Callable[[], Any]]]:
    """The reconciler's tick on ci-runner (the one writer): the changes, the batch, the watches, the gate, the reds,
    the recovery registry. Never the laptop's duties (events -> inbox, the notifier, the network and tunnel watches, the
    fixers, upkeep): those are sync_steps. clock, store and registry (the conditions' probes and actions): the process's
    (cli's), every step's time, store and registry."""
    return (("clone health", lambda: logged(d, clone_health_lines(repo, store, registry, ctx.caches.ran_at))),
            ("landed changes", shared(lambda: [wlog(d, f"tick: {x}") for x in close_landed_changes(
                repo, clock, store, d, looked=ctx.caches.landed_looked)])),
            ("stranded changes", lambda: stranded_changes(repo, d, store)),
            ("change tick", shared(lambda: [wlog(d, f"tick: {x}") for x in tick_changes(repo, d, clock, store, ctx=ctx)])),
            ("land hold", lambda: tick_land_hold(store)),
            ("batch tick", shared(lambda: say_batch(d, tick_batch(repo, d, clock, store, ctx=ctx), ctx.caches.batch_said))),
            ("watches", lambda: [wlog(d, f"tick: {x}") for x in tick_watches(clock, store, ctx=ctx)]),
            ("land refs", shared(lambda: [wlog(d, f"tick: {x}") for x in sweep_land_refs(repo, d, store, ctx.caches.ran_at)])),
            ("lease archive", lambda: archive_leases_step(d, store)),
            ("entry files", lambda: expire_entry_files_step(d, clock, store)),
            ("children", lambda: reap_children(ctx.caches.zombies_seen, ctx.runner.detached)),
            ("gate summary", lambda: maybe_publish_gate(repo, store, registry, ctx=ctx)),
            ("red time", lambda: track_red_time(store, gate_is_red_now())),
            ("red tick", lambda: maybe_red_tick(repo, store, registry, ctx=ctx)),
            ("recovery", lambda: maybe_recovery_tick(store, registry, clock.time(), ctx.caches.ran_at)
),
            ("issues", lambda: [wlog(d, f"tick: {x}") for x in tick_issues(store)]),
            ("reverts", lambda: [wlog(d, f"tick: {x}") for x in tick_reverts(repo, store)]),  # reverts.py: told their landers
            ("queue stall", lambda: logged(d, queue_stall_step(store, clock.time))),
            ("crash loops", lambda: logged(d, crash_loop_step(store, registry.check("fixer_crash_loop")))),  # crashloop.py
            ("file events", lambda: drain_event_log(store)),  # filelog.py: this host's file-held state's moves
            ("timeline", lambda: logged(d, timeline_step(store, clock.time(), owner_summary,
                                                         lambda run: remote_run_jobs(slug(repo), run), clock.sleep))),
            ("quiet deadlines", lambda: [wlog(d, f"tick: {x}") for x in tick_quiet_deadlines(store)]),
            ("stats", shared(lambda: maybe_stats_pass(repo, d, clock, store))))
