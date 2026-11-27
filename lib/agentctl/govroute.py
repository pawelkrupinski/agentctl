# agentctl, part `govroute`: where heavy work runs: here when it fits, else ci-runner's measure lane when free; replayed admissions
# A real module (lib/agentctl/__init__.py MODULES): its own imports, the standard library's and MODULES' only.

from typing import Any
from agentctl.forge import runner_queue
from agentctl.govtickets import gov_tickets
from agentctl.govbudget import GOV_MEM_GB
from agentctl.govusage import GOV_JOB_CORES, gov_job_cores
from agentctl.govfits import gov_fits
from agentctl.gov import GOV_CLASSES, gov_machine, gov_waiters
from agentctl.percentile import pct


# THE ROUTE of a heavy job (the owner, 2026-11-18: "stop the laptop being the bottleneck"): a whole-module test run or
# a commit-gate proof (scripts/fail-before: two full runs) the governor would HOLD now goes to ci-runner's measure lane
# (scripts/identity-ci-run's identity-run.yml, which runs sbt there) when that lane is free -- no job waiting on its
# label in ci-runners-page's last good read (redseen.py's runner_queue) -- instead of queueing on the laptop. A red fix is
# never held, so it stays local; so does anything the lane cannot be read for.
MEASURE_LANE = "movies-measure"


def measure_lane_free(q=None):
    """(free?, why) of ci-runner's measure lane: free when the runners page is fresh and no job waits on its label."""
    q = runner_queue() if q is None else q
    if not q:
        return False, "ci-runners-page has no fresh read of ci-runner"
    waiting = [w for w in q.get("waiting_jobs") or [] if MEASURE_LANE in str(w.get("label") or "")]
    if waiting:
        return False, f"{len(waiting)} job(s) already wait on {MEASURE_LANE}"
    return True, f"no job waits on {MEASURE_LANE}"


def gov_route(job, cls="agent", mem=None, kind="job", q=None):
    """("ci" | "local", why) for a job about to ask the governor: ci when it would be held now and the lane is free."""
    if cls == "red":
        return "local", "a red fix is never held"
    cores = gov_job_cores(job, cls, kind)
    mem = GOV_MEM_GB.get(cls, 5.0) if mem is None else float(mem)
    ahead = [w for w in gov_waiters() if GOV_CLASSES.get(w.get("class"), 9) <= GOV_CLASSES.get(cls, 9)]
    if ahead:
        held = f"{len(ahead)} waiter(s) ahead of it"
    else:
        ok, held, _ = gov_fits(cls, mem, gov_tickets(), gov_machine(), kind, cores)
        if ok:
            return "local", f"the governor admits it now ({cores:g} cores)"
    free, lane = measure_lane_free(q)
    if free:
        return "ci", f"the governor would hold it ({held}); {lane}"
    return "local", f"the governor would hold it ({held}) but {lane}: it waits for the governor"


# THE REPLAY (`agentctl gov replay`): the day's admissions (admits.jsonl) again under the cores rule, to show what it
# would have admitted. Each admission is an arrival at at - waited, held until its end record (gov_log_end) or, where
# none was written (before 2026-11-18), for --hold minutes; its cores are its record's, else its job's by label. The
# replay admits oldest first while the charged cores + its own fit the cap (one at a time when none is charged).
def replay_cores(r):
    if r.get("cores") is not None:
        return float(r["cores"])
    return GOV_JOB_CORES[replay_job(r)]


def replay_job(r: dict[str, Any]) -> str:
    """The GOV_JOB_CORES key of an admission record without its own cores: its job by label, class and kind."""
    label, cls = r.get("label") or "", r.get("class")
    if "what-if" in label:
        return "what-if"
    if "identity-slice" in label:
        return "slice"
    if cls == "measure":
        return "measure"
    return "check" if r.get("kind") == "check" or cls == "check" else "job"


def replay_jobs(rows: list[dict[str, Any]], hold: float) -> list[dict[str, Any]]:
    """The admissions of the log's rows as jobs: label, cores, arrival, admission and duration (to its end record, else
    `hold` seconds)."""
    ends = {r["id"]: r["end"] for r in rows if "end" in r and r.get("id")}
    jobs = []
    for r in rows:
        if "end" in r or "at" not in r:
            continue
        start = float(r["at"])
        dur = ends.get(r.get("id"), start + hold) - start
        jobs.append({"label": r.get("label") or "?", "cores": replay_cores(r), "arrive": start - float(r.get("waited") or 0),
                     "admitted": start, "dur": max(dur, 1.0)})
    return jobs


def live_at(js: list[dict[str, Any]], t: float, key: str) -> list[dict[str, Any]]:
    """The jobs running at t, from the moment their `key` says."""
    return [j for j in js if j.get(key) is not None and j[key] <= t < j[key] + j["dur"]]


def peak_cores(js: list[dict[str, Any]], key: str) -> float:
    """The most cores running at once, counted at each job's `key` moment."""
    return max((sum(x["cores"] for x in live_at(js, j[key], key)) for j in js if j.get(key) is not None), default=0.0)


class CoresAdmission:
    """The new rule, event by event: arrivals in order (oldest first, none overtakes the head), each admitted once the
    charged cores leave it room. `run` returns every job with its `new` admission time (None where it never got in)."""

    def __init__(self, jobs: list[dict[str, Any]], cap: float) -> None:
        self.cap = cap
        self.queue = sorted((dict(j, new=None) for j in jobs), key=lambda j: j["arrive"])
        self.pending = list(self.queue)
        self.running: list[dict[str, Any]] = []
        self.events = sorted({j["arrive"] for j in self.queue})
        self.t: float | None = None

    def fits(self, j: dict[str, Any]) -> bool:
        charged = sum(x["cores"] for x in self.running)
        return bool(j["cores"] == 0 or charged == 0 or charged + j["cores"] <= self.cap)

    def admit_due(self) -> None:
        """Admit the arrived jobs, oldest first, until one does not fit."""
        for j in [p for p in self.pending if p["arrive"] <= self.t]:
            if not self.fits(j):
                break
            j["new"] = self.t
            self.running.append(j)
            self.pending.remove(j)

    def plan_next_event(self) -> bool:
        """Add the next moment worth looking at (a job ending, a later arrival); False when a job waits and none is coming."""
        nxt = [x["new"] + x["dur"] for x in self.running] + [p["arrive"] for p in self.pending if p["arrive"] > self.t]
        if self.pending and not nxt:
            return False
        if nxt:
            self.events = sorted(set(self.events) | {min(nxt)})
        return True

    def run(self) -> list[dict[str, Any]]:
        while self.pending:
            self.t = self.events.pop(0) if self.events else self.t
            self.running = [j for j in self.running if j["new"] + j["dur"] > self.t]
            self.admit_due()
            if not self.plan_next_event():
                break
        return self.queue


def labelled(js: list[dict[str, Any]]) -> list[tuple[str, float]]:
    return [(j["label"], j["cores"]) for j in js]


def gov_replay(rows: list[dict[str, Any]], cap: float, at: float, hold: float = 30 * 60) -> dict[str, Any]:
    """{old: [(label, cores)] live at `at` as admitted, new: [...] live at `at` under the cores rule, waiting: [...],
    old_peak / new_peak: the most cores live at once}."""
    jobs = replay_jobs(rows, hold)
    queue = CoresAdmission(jobs, cap).run()
    return {"old": labelled(live_at(jobs, at, "admitted")),
            "new": labelled(live_at(queue, at, "new")),
            "waiting": labelled([j for j in queue if j["arrive"] <= at and (j["new"] is None or j["new"] > at)]),
            "old_peak": peak_cores(jobs, "admitted"), "new_peak": peak_cores(queue, "new"),
            "new_wait_p50": pct([j["new"] - j["arrive"] for j in queue if j["new"] is not None], .5) or 0.0,
            "old_wait_p50": pct([j["admitted"] - j["arrive"] for j in jobs], .5) or 0.0}
