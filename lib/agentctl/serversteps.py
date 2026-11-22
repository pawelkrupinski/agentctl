# agentctl, part `serversteps`: the reconciler's tick on ci-runner: its steps in order, the red-time gauge and the quiet deadlines
# A real module (lib/agentctl/__init__.py MODULES): its own imports, the standard library's and MODULES' only.

from typing import Any, Callable, Sequence
from agentctl.core import reap_children
from agentctl.store import StaleVersion, TerminalState, machine_state, open_store
from agentctl.waitingdir import wlog
from agentctl.stats import maybe_stats_pass
from agentctl.clones import heal_enabled
from agentctl.reverts import tick_reverts
from agentctl.conditions import maybe_recovery_tick
from agentctl.cistate import snapshot_gate_reds
from agentctl.projector import maybe_archive_leases, project_changes
from agentctl.queuesubmit import sweep_land_refs
from agentctl.watchtick import tick_watches
from agentctl.queueclose import close_landed_changes, stranded_changes
from agentctl.queuepush import tick_changes
from agentctl.status import tick_issues
from agentctl.redtick import maybe_publish_gate, maybe_red_tick
from agentctl.serverclone import clone_heal_tick
from agentctl.landhold import tick_land_hold
from agentctl.batch import say_batch, tick_batch


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


def tick_quiet_deadlines(st=None, now_=None):
    """The reconciler's step: each lander record past its 14 days goes `gone` (blame reads landings that recent) and
    each forgotten explorer flip `closed`, an info event apiece (no inbox entry, no notification). Returns what it did."""
    st = st or open_store()
    t = now_ if now_ is not None else st.clock()
    did = []
    for typ in QUIET_DEADLINE_TYPES:
        for it in st.items(typ):
            spec = machine_state(typ, it["state"])
            if spec.terminal or it["deadline"] > t:
                continue
            try:
                st.transition(typ, it["id"], spec.on_deadline, it["version"],
                              event={"text": f"{typ} {it['id']}: {it['state']} past its deadline -> {spec.on_deadline}",
                                     "severity": "info"})
            except (StaleVersion, TerminalState):
                continue
            did.append(f"{typ} {it['id']}: {spec.on_deadline}")
    return did


def server_steps(repo: str, d: str, shared: Callable[[Callable[[], Any]], Callable[[], Any]]) -> Sequence[tuple[str, Callable[[], Any]]]:
    """The reconciler's tick on ci-runner (the one writer): the changes, the batch, the watches, the gate, the reds,
    the recovery registry. Never the laptop's duties (events -> inbox, the notifier, the network and tunnel watches, the
    fixers, upkeep): those are sync_steps."""
    return (("clone health", lambda: [wlog(d, f"tick: {x}") for x in [clone_heal_tick(repo)] if x]
             if heal_enabled(repo) else None),
            ("change items", lambda: project_changes(d)),
            ("landed changes", shared(lambda: [wlog(d, f"tick: {x}") for x in close_landed_changes(repo, d)])),
            ("stranded changes", lambda: stranded_changes(repo, d)),
            ("change tick", shared(lambda: [wlog(d, f"tick: {x}") for x in tick_changes(repo, d)])),
            ("land hold", tick_land_hold),
            ("batch tick", shared(lambda: say_batch(d, tick_batch(repo, d)))),
            ("watches", lambda: [wlog(d, f"tick: {x}") for x in tick_watches()]),
            ("land refs", shared(lambda: [wlog(d, f"tick: {x}") for x in sweep_land_refs(repo, d)])),
            ("lease archive", lambda: maybe_archive_leases(d)),
            ("children", reap_children),
            ("gate summary", lambda: maybe_publish_gate(repo)),
            ("red time", lambda: track_red_time(open_store(), gate_is_red_now())),
            ("red tick", lambda: maybe_red_tick(repo)),
            ("recovery", maybe_recovery_tick),
            ("issues", lambda: [wlog(d, f"tick: {x}") for x in tick_issues()]),
            ("reverts", lambda: [wlog(d, f"tick: {x}") for x in tick_reverts(repo)]),  # reverts.py: told their landers
            ("quiet deadlines", lambda: [wlog(d, f"tick: {x}") for x in tick_quiet_deadlines()]),
            ("stats", shared(lambda: maybe_stats_pass(repo, d))))
