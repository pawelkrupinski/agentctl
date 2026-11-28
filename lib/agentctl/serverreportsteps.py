# agentctl, part `serverreportsteps`: the server tick's closing steps: the timeline, the quiet deadlines and the stats pass
# A real module (lib/agentctl/__init__.py MODULES): its own imports, the standard library's and MODULES' only.

from typing import Any
from typing import Callable, Sequence
from agentctl.statemachines import StaleVersion, TerminalState, machine_state
from agentctl.inboxentry import inbox_write
from agentctl.clock import Clock
from agentctl.repos import slug
from agentctl.waitingdir import logged, wlog
from agentctl.stats import maybe_stats_pass
from agentctl.timelinecmd import timeline_step
from agentctl.checkrun import remote_run_jobs


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


def owner_summary(line: str, sinks: list[str]) -> None:
    """The weekly time-sinks summary, to the owner's inbox."""
    inbox_write("analyze", line, [line], sinks, 0)


def report_steps(repo: str, d: str, shared: Callable[[Callable[[], Any]], Callable[[], Any]], clock: Clock,
                 store: Any) -> Sequence[tuple[str, Callable[[], Any]]]:
    """The tick's closing steps: the timeline, the quiet deadlines and the stats pass."""
    return (("timeline", lambda: logged(d, timeline_step(store, clock.time(), owner_summary,
                                                         lambda run: remote_run_jobs(slug(repo), run), clock.sleep))),
            ("quiet deadlines", lambda: [wlog(d, f"tick: {x}") for x in tick_quiet_deadlines(store)]),
            ("stats", shared(lambda: maybe_stats_pass(repo, d, clock, store))))
