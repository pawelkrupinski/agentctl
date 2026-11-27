# agentctl, part `checkrows`: each check run's CI rows (its jobs) -- start, end, conclusion -- linked to its change
# A real module (lib/agentctl/__init__.py MODULES): its own imports, the standard library's and MODULES' only.
#
# PER-ROW RUNTIME (2026-11-26, the transition analysis: 29.4 d in `checking` was one number per change; a row's name
# appeared only in a reject's tail, so which CI row the time went to could not be told). When a change's remote check is
# decided, its run's jobs become ROWS on the change item (`check_runs`, one record per run: the run, the tip it checked,
# its verdict, and each row's runner wait, start, end, seconds and conclusion; the newest CHECK_RUNS_KEPT). A run decided
# before this (its id on the item as `check.run`) is fetched once from GitHub by the backfill (backfill_check_runs:
# bounded per pass, paced, cached as a derived row). `agentctl analyze` ranks the rows, `agentctl history` lists a
# change's.

import collections
from typing import Any, Iterable, Optional

from agentctl.timefmt import iso_epoch
from agentctl.timeline import dist, hum

CHECK_RUNS_KEPT = 10  # check-run records one change item keeps (a change re-checked more often keeps its newest)


def _span(a: Optional[float], b: Optional[float]) -> Optional[float]:
    """b - a in seconds, when both are known and in order."""
    return round(b - a, 1) if a is not None and b is not None and b >= a else None


def job_row(j: dict[str, Any]) -> dict[str, Any]:
    """One job as a row: its name, conclusion, created / start / end epochs, runtime and runner wait."""
    created, start, end = (iso_epoch(j.get(k)) for k in ("created_at", "started_at", "completed_at"))
    return {"name": j.get("name"), "conclusion": j.get("conclusion"), "status": j.get("status"), "created": created,
            "start": start, "end": end, "seconds": _span(start, end), "runner_wait": _span(created, start)}


def job_rows(jobs: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    """The rows of a run's jobs (GitHub's job objects or remote_jobs_summary's): a skipped row is no row; a row not
    started or not ended has no seconds."""
    return [job_row(j) for j in jobs or () if j.get("conclusion") != "skipped"]


def check_run_record(rem: dict[str, Any], jobs: Iterable[dict[str, Any]]) -> dict[str, Any]:
    """A decided remote check's record: its run, branch, the tip it checked, its verdict and decision time, its rows."""
    return {"run": rem.get("run"), "branch": rem.get("branch"), "tip": rem.get("tip"), "status": rem.get("status"),
            "decided_at": rem.get("decided_at"), "rows": job_rows(jobs)}


def with_run(runs: Optional[list[dict[str, Any]]], rec: dict[str, Any]) -> list[dict[str, Any]]:
    """The change's check-run records with rec in (one per run: a run seen again replaces its record), newest kept."""
    kept = [r for r in runs or () if r.get("run") != rec.get("run")]
    return (kept + [rec])[-CHECK_RUNS_KEPT:]


def change_runs(items: Iterable[dict[str, Any]], fetched: Any = None
                ) -> list[tuple[str, dict[str, Any]]]:
    """(change id, run record) of every check run the change items hold, and of the runs the backfill fetched
    (`fetched`: run id -> record with its change) the items do not hold themselves."""
    held = [(it["id"], r) for it in items for r in (it.get("data") or {}).get("check_runs") or ()]
    return with_fetched(held, fetched)


def with_fetched(held: list[tuple[str, dict[str, Any]]], fetched: Any = None) -> list[tuple[str, dict[str, Any]]]:
    """`held` (change id, run record) and the backfill's fetched runs (run id -> record with its change) it lacks."""
    seen = {str(r.get("run")) for _change, r in held}
    return held + [(str(rec.get("change")), rec) for run, rec in (fetched or {}).items() if str(run) not in seen]


class RowTally:
    """The runtimes, runner waits, failures and slowest change of each CI row name."""

    def __init__(self) -> None:
        self.took: dict[str, list[float]] = collections.defaultdict(list)
        self.waited: dict[str, list[float]] = collections.defaultdict(list)
        self.failed: collections.Counter[str] = collections.Counter()
        self.slowest: dict[str, tuple[float, str]] = {}

    def add(self, change: str, row: dict[str, Any]) -> None:
        name = str(row.get("name"))
        self.failed[name] += row.get("conclusion") == "failure"
        if row.get("runner_wait") is not None:
            self.waited[name].append(float(row["runner_wait"]))
        if row.get("seconds") is None:
            return
        took = float(row["seconds"])
        self.took[name].append(took)
        if took > self.slowest.get(name, (-1.0, ""))[0]:
            self.slowest[name] = (took, change)

    def report(self) -> dict[str, dict[str, Any]]:
        names = set(self.took) | set(self.waited) | set(self.failed)
        rows: dict[str, dict[str, Any]] = {n: {"runtime": dist(self.took[n]), "runner_wait": dist(self.waited[n]), "failed": self.failed[n],
                    "slowest": self.slowest[n][1] if n in self.slowest else None} for n in names}
        return dict(sorted(rows.items(), key=lambda kv: -float(kv[1]["runtime"]["total"])))


def rows_report(runs: Iterable[tuple[str, dict[str, Any]]], since: float = 0.0) -> dict[str, Any]:
    """Per CI row (job name): its runtime and runner-wait distributions, failures and the slowest change; over the runs
    decided at or after `since`."""
    tally, n = RowTally(), 0
    for change, rec in runs:
        if float(rec.get("decided_at") or 0) < since:
            continue
        n += 1
        for row in rec.get("rows") or ():
            tally.add(change, row)
    return {"runs": n, "rows": tally.report()}


def rows_lines(rep: dict[str, Any], k: int = 15) -> list[str]:
    """The rows report as text: one line a CI row, by total runtime."""
    out = [f"check rows ({rep['runs']} check runs): row  n  p50  p90  total  runner-wait p50  failed  slowest"]
    for name, d in list(rep["rows"].items())[:k]:
        rt, rw = d["runtime"], d["runner_wait"]
        out.append(f"  {name}  {rt['n']}  {hum(rt['p50'])}  {hum(rt['p90'])}  {hum(rt['total'])}  {hum(rw['p50'])}  "
                   f"{d['failed']}  {d['slowest'] or '-'}")
    return out


def change_rows_lines(runs: Iterable[dict[str, Any]]) -> list[str]:
    """`history`'s lines for one change's check runs: each run, then each row's conclusion and runtime."""
    out = []
    for r in runs:
        out.append(f"  check run {r.get('run')} ({r.get('status')}, tip {str(r.get('tip') or '')[:12]})")
        out += [f"    {row.get('name')}: {row.get('conclusion') or row.get('status')}"
                + (f" in {hum(float(row['seconds']))}" if row.get("seconds") is not None else "")
                + (f", waited {hum(float(row['runner_wait']))} for a runner" if row.get("runner_wait") else "")
                for row in r.get("rows") or ()]
    return out
