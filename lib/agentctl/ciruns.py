# agentctl, part `ciruns`: THE ONE READER OF CI RUNS: a workflow's runs on main, main's newest runs across workflows, a
# run's jobs (CiRuns), and what the gate's reads last listed per repository (MainRuns); cli builds the process's one.
# A real module (lib/agentctl/__init__.py MODULES): its own imports, the standard library's and MODULES' only.

import re
from typing import Any, Callable
from agentctl.clock import epoch_now
from agentctl.core import print_error
from agentctl.forge import RUN_FIELDS, gh_json

Run = dict[str, Any]  # a workflow run as gh lists it (RUN_FIELDS)
Job = dict[str, Any]  # a run's job as `gh run view --json jobs` prints it


# A WORKFLOW NOT ON MAIN HAS NO RUNS ON MAIN (2026-11-08 16:35-18:10: agentctl's gate list named identity-takes.yml before
# the movies commit adding the file reached main; GitHub answered each list of it `HTTP 404: workflow ... not found on
# the default branch`, so the whole gate read failed every cycle -- the status line said `gate UNREAD (as of 16:44)` and
# every production landing waited on a gate taken as red for 1.5 h). That 404 is GitHub's own answer, not an unread
# gate: the workflow has no run on main, so it decides nothing until its file lands. Any other error still raises.
# Review 2026-11-26 (B F2): only the gate's read knew this; the red verify plan, the quota probe, the never-green check,
# an outside red's newest run and the outside list each read a workflow's runs on main their own way, and raised (or
# said an error) on it. Every one now reads through CiRuns.main_runs.
WORKFLOW_NOT_ON_MAIN = re.compile(r"HTTP 404: workflow \S+ not found on the default branch")


class MainRuns:
    """ONE repository's runs on main, as gate_runs / outside_gate / a snapshot last listed them (newest first), by
    workflow. Review 2026-11-26 (B F1): these were three process globals keyed by workflow NAME alone, imported by
    private name into 8 modules; red_gate_snap filled them with another repo's runs and put the primary's back in a
    `finally` (names repeat across repos), so a reader in the meantime -- the gate-publish thread, or the tick's own --
    saw the other repo's runs. Now each repository has its own (CiRuns.main_runs_of), and nothing is saved or restored."""

    def __init__(self) -> None:
        self.gate: dict[str, list[Run]] = {}      # a gate workflow -> its runs on main
        self.outside: dict[str, list[Run]] = {}   # a workflow outside the gate -> its runs on main
        self.no_verdict: dict[str, Run | None] = {}  # a gate workflow -> its newest no-verdict run newer than its decided one

    def runs_of(self, name: str) -> list[Run]:
        """A workflow's listed runs: the gate's, else outside the gate's, else none."""
        return self.gate.get(name) or self.outside.get(name) or []

    def lists(self) -> list[tuple[str, list[Run]]]:
        """(workflow, runs) of every listed workflow, the gate's first."""
        return list(self.gate.items()) + list(self.outside.items())


class CiRuns:
    """THE ONE READER of CI runs (review 2026-11-26, B F2: "a workflow's runs on main" was read in 8 places and "a run's
    jobs" in 8, with 6 caches of their own). What it reads is GitHub's answer through gh_json; what it keeps:
    - a listing asked with `fresh_for` (seconds) is answered from the last one of the same (repo, workflow, limit,
      status) while it is younger than that -- one memo for what were _verify_runs, _flow_runs and _flow_history;
    - a run's jobs once every job completed (a decided run's jobs never change) -- one memo for what were
      _run_jobs_mem, notifyurl's _run_jobs and redretired's job reads (and what _no_verdict_mem and _report_only_mem
      kept of the same jobs);
    - each repository's MainRuns.
    An unread answer raises (gh's error, NoToken): the caller decides what unread means for it. clear() forgets it all
    (the tests between cases: it is one of the process's caches)."""

    def __init__(self, now: Callable[[], float] | None = None) -> None:
        self._clock = now  # None: the process's clock, read at each call
        self._listed: dict[tuple[str, str, int, str | None], tuple[float, list[Run]]] = {}
        self._jobs: dict[tuple[str, str], list[Job]] = {}
        self._main: dict[str, MainRuns] = {}
        self._not_on_main_said: set[tuple[str, str]] = set()

    def _now(self) -> float:
        return self._clock() if self._clock else epoch_now()

    def __len__(self) -> int:
        """How many things it keeps (0: nothing read yet, or cleared)."""
        return len(self._listed) + len(self._jobs) + len(self._main) + len(self._not_on_main_said)

    def clear(self) -> None:
        self._listed.clear()
        self._jobs.clear()
        self._main.clear()
        self._not_on_main_said.clear()

    def main_runs(self, o_r: str, wfile: str, limit: int = 30, status: str | None = None,
                  fresh_for: float = 0) -> list[Run]:
        """Workflow (file or name) wfile's newest `limit` runs on main (of `status`, when given), newest first; [] when
        GitHub says its file is not on the default branch (WORKFLOW_NOT_ON_MAIN). fresh_for: answered from the last such
        listing while it is younger than that many seconds."""
        key = (o_r, wfile, limit, status)
        at, kept = self._listed.get(key, (0.0, None))
        if fresh_for and kept is not None and self._now() - at < fresh_for:
            return kept
        filters = ("--status", status) if status else ()
        try:
            # listing: the newest `limit` of one workflow on main; each caller says how it reads a run left out
            runs = gh_json(o_r, "run", "list", "--workflow", wfile, "--branch", "main", *filters, "--limit", str(limit),
                           "--json", RUN_FIELDS) or []
        except RuntimeError as e:
            if not WORKFLOW_NOT_ON_MAIN.search(str(e)):
                raise
            self._say_not_on_main(o_r, wfile)
            runs = []
        if fresh_for:
            self._listed[key] = (self._now(), runs)
        return runs

    def last_listed(self, o_r: str, wfile: str, limit: int = 30, status: str | None = None) -> list[Run] | None:
        """The last listing main_runs kept of these (fresh_for), however old, else None: what a caller whose read just
        failed may stand on until the next one."""
        kept = self._listed.get((o_r, wfile, limit, status))
        return kept[1] if kept else None

    def _say_not_on_main(self, o_r: str, wfile: str) -> None:
        """Said once per repository and workflow file per process, not every cycle."""
        if (o_r, wfile) not in self._not_on_main_said:
            self._not_on_main_said.add((o_r, wfile))
            print_error(f"{wfile} is not on main yet: no run of it gates anything until it lands")

    def recent_main(self, o_r: str, limit: int) -> list[Run]:
        """Main's newest `limit` runs across every workflow, newest first."""
        # listing: the newest `limit` across workflows; each caller says how it reads a run left out
        return gh_json(o_r, "run", "list", "--branch", "main", "--limit", str(limit), "--json", RUN_FIELDS) or []

    def run_jobs(self, o_r: str, rid: Any) -> list[Job]:
        """Run rid's jobs (its latest attempt's), kept once every job completed."""
        key = (o_r, str(rid))
        if key in self._jobs:
            return self._jobs[key]
        jobs = (gh_json(o_r, "run", "view", str(rid), "--json", "jobs") or {}).get("jobs") or []
        if jobs and all(j.get("status") == "completed" for j in jobs):
            self._jobs[key] = jobs
        return jobs

    def kept_jobs(self, o_r: str, rid: Any) -> list[Job] | None:
        """Run rid's jobs when they are kept (a decided run read before), else None: nothing is read."""
        return self._jobs.get((o_r, str(rid)))

    def main_runs_of(self, o_r: str) -> MainRuns:
        """The repository's MainRuns (made empty on first ask)."""
        return self._main.setdefault(o_r, MainRuns())
