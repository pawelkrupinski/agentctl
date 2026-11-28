# agentctl, part `stuckruns`: the undecided runs past their workflow's own stuck limit (queued behind busy runners or waiting on a dispatched run are not stuck)
# A real module (lib/agentctl/__init__.py MODULES): its own imports, the standard library's and MODULES' only.

from agentctl.timefmt import parse_time
from agentctl.core import attempt
from agentctl.runqueue import queue_full
from agentctl.forge import runner_queue, stuck_durations
from agentctl.context import Context
from agentctl.outsideflows import stuck_history, stuck_timeout
from agentctl.red import STUCK_FACTOR, STUCK_FLOOR
from agentctl.percentile import pct
import time
from typing import Any, Callable



# THE STUCK LIMIT IS THE WORKFLOW'S OWN (the owner, 2026-11-06, deciding red 971723aec893: six false stuck reds in a
# week). Record scrape fixtures #165/#172 (daily, 32-65 min a run) and Identity drift explorer #1 (its first run) were
# judged against the 30-minute floor because the newest-200 run list held none of their finished runs. Now a workflow
# with fewer than STUCK_MIN_HISTORY finished runs in the list has its own last 30 finished runs read (history_of, only
# once one of its runs is past the floor); with fewer still, its jobs' timeout-minutes bound it (timeout_of). A run
# waiting on a run it dispatched (a workflow_dispatch run of another workflow created after its first job started,
# still undecided) is judged by that child: `waits_on`, no red -- the child is judged on its own. A run whose unfinished
# jobs all wait for runners whose every slot is busy is `queued`, not stuck, whatever ran before.
STUCK_MIN_HISTORY = 5


UNSTARTED = ("queued", "waiting", "pending", "requested")


# A SCHEDULED BOT WAITS ITS TURN (issue 20261108T135045-06bcab: Scala Steward #145, a daily run queued behind a
# CPU-saturated ci-runner -- checks first, by design -- was stuck at 1 h): a run its schedule started that has not
# started a job yet waits its turn behind checks, so its limit is at least STUCK_SCHEDULED; once a job runs, the
# workflow's own limit judges it.
STUCK_SCHEDULED = 6 * 3600


STUCK_SCHEDULED_EVENTS = ("schedule", "repository_dispatch")  # movies' scheduler dispatches the crons (repository_dispatch)


def unseen_history(runs: list[Any], history: Any) -> list[Any]:
    """The runs of a workflow's own history that its run list does not already hold as finished."""
    seen = {r.get("databaseId") for r in runs if r.get("status") == "completed"}
    return [r for r in history or [] if r.get("databaseId") not in seen]


def stuck_limit(name: str, runs: list[Any], history_of: Any, timeout_of: Any, how: dict[str, Any] | None = None) -> float:
    """max(STUCK_FACTOR x the p90 of the workflow's finished runs, STUCK_FLOOR): from the list, else its own history;
    with too few of those, its jobs' timeout-minutes bound (when readable). how: a dict told where it came from."""
    durs = stuck_durations(runs)
    if len(durs) < STUCK_MIN_HISTORY and history_of:
        durs += stuck_durations(unseen_history(runs, history_of(name)))
    limit = max(STUCK_FACTOR * (pct(durs, 90) if durs else 0), STUCK_FLOOR)
    src = f"from {len(durs)} runs" if durs else "the floor"
    if len(durs) < STUCK_MIN_HISTORY and timeout_of:
        bound = timeout_of(name)
        if bound and bound > limit:
            limit, src = bound, f"its jobs' timeouts ({len(durs)} runs)"
    if how is not None:
        how["src"] = src
    return limit


def dispatched_since(r: dict[str, Any], since: float) -> bool:
    """Is run r an undecided workflow_dispatch run created at or after `since` (epoch seconds)?"""
    if r.get("event") != "workflow_dispatch" or r.get("status") == "completed" or not r.get("createdAt"):
        return False
    return attempt(lambda: parse_time(r["createdAt"]).timestamp() >= since, False, catch=(ValueError, TypeError))


def stuck_child(lists: dict[str, Any], name: str, since: float) -> str | None:
    """'<workflow> #N' of an undecided workflow_dispatch run of another workflow created at or after `since` (the run
    its parent dispatched and waits on), or None."""
    for other, runs in lists.items():
        found = next((r for r in runs if other != name and dispatched_since(r, since)), None)
        if found:
            return f"{other} #{found.get('number')}"
    return None


def stuck_runs(lists: dict[str, Any], now_: float | None = None, o_r: Any = None, jobs_of: Any = None, queue: Any = None, history_of: Any = None, timeout_of: Any = None, *, ctx: Context) -> list[dict[str, Any]]:
    """[{name, run, age, limit[, queued | waits_on]}]: per workflow, its undecided runs older than its stuck limit
    (stuck_limit). A run's age counts from its first job's start (Convergence bisect #2385, run 3568: an hour queued
    behind a measurement holding every `measure` slot was called a stuck red): a run whose unfinished jobs all wait,
    only on labels whose every runner is up with all slots busy, carries `queued`; one waiting on a run it dispatched,
    `waits_on`. Neither is a red."""
    t = now_ if now_ is not None else ctx.clock.time()
    jobs_of = jobs_of or (lambda r: run_jobs(o_r, r, ctx=ctx))
    if history_of is None and o_r:
        history_of = lambda name: stuck_history(o_r, name, ctx=ctx)  # noqa: E731
    if timeout_of is None and o_r:
        timeout_of = lambda name: stuck_timeout(o_r, name, ctx.caches.flow_bounds)  # noqa: E731
    queued_of = lambda r: queue_full(o_r, r.get("number"), runner_queue(t) if queue is None else queue)  # noqa: E731
    limit_of = lambda name, runs, how: stuck_limit(name, runs, history_of, timeout_of, how)  # noqa: E731
    return [s for name, runs in lists.items()
            for s in stuck_in_workflow(lists, name, runs, t, jobs_of, queued_of, limit_of)]


def unfinished_age(r: dict[str, Any], t: float) -> float | None:
    """Seconds since run r was created when it is undecided, else None (also when its creation time cannot be read)."""
    if r.get("status") == "completed" or not r.get("createdAt"):
        return None
    return attempt(lambda: t - parse_time(r["createdAt"]).timestamp(), None, catch=(ValueError, TypeError))


def stuck_in_workflow(lists: dict[str, Any], name: str, runs: list[Any], t: float, jobs_of: Callable[[Any], Any], queued_of: Callable[[Any], Any], limit_of: Callable[..., float]) -> list[dict[str, Any]]:
    """stuck_runs' entries of one workflow. Its limit is read only once one of its runs is past the floor: the history
    and the timeouts file cost a read."""
    limit, how, out = None, {}, []
    for r in runs:
        age = unfinished_age(r, t)
        if age is None or age <= STUCK_FLOOR:
            continue
        if limit is None:
            limit = limit_of(name, runs, how)
        entry = age > limit and stuck_entry(lists, name, r, jobs_of(r), t, age, (limit, how.get("src")), queued_of)
        if entry:
            out.append(entry)
    return out


def waiting_for_runners(jobs: list[dict[str, Any]] | None) -> bool:
    """Do the run's jobs, once read, all wait (every unfinished one is not started)?"""
    open_jobs = [j for j in jobs or [] if j.get("status") != "completed"]
    return jobs is not None and bool(open_jobs) and all(j.get("status") in UNSTARTED for j in open_jobs)


def scheduled_limit(r: dict[str, Any], starts: list[float], lim: float) -> float:
    """`lim`, raised to STUCK_SCHEDULED for a scheduled bot that has not started a job yet (it waits its turn)."""
    return max(lim, STUCK_SCHEDULED) if not starts and r.get("event") in STUCK_SCHEDULED_EVENTS else lim


def stuck_entry(lists: dict[str, Any], name: str, r: dict[str, Any], jobs: list[dict[str, Any]] | None, t: float, age: float, limit_src: tuple[float, Any], queued_of: Callable[[Any], Any]) -> dict[str, Any] | None:
    """The stuck_runs entry of run r (past `limit_src`, (limit, where it came from)) once its jobs are read, or None
    when they show it is not stuck: its age counts from its first job's start, and a scheduled bot not started yet
    waits its turn (STUCK_SCHEDULED)."""
    limit, src = limit_src
    lim = limit
    starts = job_starts(jobs)
    if starts:
        age = t - starts[0]
    queued, lim = (queued_of(r), scheduled_limit(r, starts, lim)) if waiting_for_runners(jobs) else (None, lim)
    # nothing running: queued, not stuck, when every runner that could take it is busy
    if age <= lim:
        return None
    waits_on = stuck_child(lists, name, starts[0]) if starts and not queued else None
    s = {"name": name, "run": {k: r.get(k) for k in ("number", "databaseId", "headSha", "createdAt")},
         "age": age, "limit": lim, "limit_from": "its schedule" if lim != limit else src}
    return {**s, "queued": queued} if queued else {**s, "waits_on": waits_on} if waits_on else s


def job_starts(jobs: list[dict[str, Any]] | None) -> list[float]:
    """The start times (epoch seconds) of the jobs that have started, earliest first."""
    return sorted(parse_time(j["startedAt"]).timestamp() for j in jobs or [] if j.get("startedAt"))


def run_jobs(o_r: Any, run: dict[str, Any], *, ctx: Context) -> list[dict[str, Any]] | None:
    """The run's jobs ([{name, status, startedAt, ...}]), or None when they cannot be read."""
    try:
        return ctx.runs.run_jobs(o_r, run.get("databaseId"))
    except (Exception, SystemExit):
        return None
