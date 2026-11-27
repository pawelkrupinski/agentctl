# agentctl, part `runverdict`: a CI run's verdict for `wait`: done, ok and why (a measure run done once its takes rows ended), a file's presence, other loops polling a run
# A real module (lib/agentctl/__init__.py MODULES): its own imports, the standard library's and MODULES' only.

import os
import re
from typing import Any
from agentctl.procrunner import process_runner
import datetime as dt
from agentctl.core import scratch
from agentctl.forge import forge_argv, forge_env, gh_json
from agentctl.workflows import job_inner
from agentctl.procs import ps
from agentctl.joblog import failure_detail


MEASURE_WORKFLOWS = {"Identity takes", "Identity model convergence"}  # takes rows: Identity takes (2026-11-08); a --full measure starts it from convergence


def fetch_takes_verdicts(o_r: Any, run_id: Any) -> dict[str, str]:
    """{artifact: verdict.txt text} of a measure run's identity-takes-<cc> artifacts (the takes rows' verdict files)."""
    env = forge_env(o_r)
    arts = gh_json(o_r, "api", f"repos/{o_r}/actions/runs/{run_id}/artifacts").get("artifacts", [])
    out = {}
    for a in arts:
        name = a.get("name", "")
        if not name.startswith("identity-takes-"):
            continue
        d = os.path.join(scratch(), "agentctl-wait", f"art-{run_id}-{name}")
        os.makedirs(d, exist_ok=True)
        process_runner().run(forge_argv(o_r, "run", "download", str(run_id), "-n", name, "-D", d), capture_output=True, env=env,
                       timeout=300)
        p = os.path.join(d, "verdict.txt")
        out[name] = open(p, errors="replace").read().strip() if os.path.exists(p) else "(no verdict.txt in the artifact)"
    return out


def is_measure_run(d: Any) -> bool:
    return d.get("workflowName") in MEASURE_WORKFLOWS and (d.get("headBranch") or "").startswith("measure/")


def _takes_ended(o_r: Any, run_id: Any, head: str, takes: Any, tail: Any) -> tuple[bool, bool, str]:
    """A measure run whose takes rows ended: every row's verdict file, the failed rows' detail."""
    badj = [j for j in takes if j.get("conclusion") not in ("success", "skipped")]
    bad = [j["name"] for j in badj]
    lines = [f"{head}: takes rows ended ({len(takes)}): " + ("all green" if not bad else "failed: " + ", ".join(bad))]
    try:
        for name, text in sorted(fetch_takes_verdicts(o_r, run_id).items()):
            lines += [f"  [{name}] {l}" for l in text.splitlines()]
    except Exception as e:
        lines.append(f"  (could not read the verdict files: {e})")
    lines.append(f"  collect the LANDABLE verdict: scripts/identity-measure --run {run_id}")
    lines += failure_detail(o_r, run_id, badj, tail)
    return True, not bad and bool(takes), "\n".join(lines)


def _measure_verdict(o_r: Any, run_id: Any, d: Any, head: str, tail: Any) -> tuple[bool, bool | None, str]:
    """(done, ok, text) of a measure run: done once its takes rows ended (Identity takes names its rows
    `leg (<cc>) / takes (<cc>)`: the job's own name (job_inner) after the caller's)."""
    takes = [j for j in d.get("jobs") or [] if job_inner(j.get("name", "")).startswith("takes")]
    if d.get("status") == "completed" or (takes and all(j.get("status") == "completed" for j in takes)):
        return _takes_ended(o_r, run_id, head, takes, tail)
    n_done = sum(1 for j in takes if j.get("status") == "completed")
    return False, None, f"{head}: {d.get('status')} (takes rows {n_done}/{len(takes)} ended)"


def run_verdict(o_r: Any, run_id: Any, until: str = "done", tail: Any = 0) -> tuple[bool, bool | None, str]:
    """(done, ok, text) for a CI run. until=landable: a measure run is done once its takes rows ended."""
    d = gh_json(o_r, "run", "view", str(run_id), "--json", "status,conclusion,jobs,workflowName,headBranch")
    head = f"run {o_r}#{run_id}"
    if until == "landable" and is_measure_run(d):
        return _measure_verdict(o_r, run_id, d, head, tail)
    if d.get("status") != "completed":
        return False, None, f"{head}: {d.get('status')}"
    concl = d.get("conclusion") or "unknown"
    failedj = [j for j in d.get("jobs") or [] if j.get("conclusion") in ("failure", "cancelled", "timed_out")]
    failed = [j["name"] for j in failedj]
    line = f"{head}: {concl}" + (f"; failed: {', '.join(failed)}" if failed else "")
    return True, concl in ("success", "skipped", "neutral"), "\n".join([line] + failure_detail(o_r, run_id, failedj, tail))


def file_verdict(path: str, contains: str | None) -> tuple[bool, bool | None, str]:
    if not os.path.exists(path):
        return False, None, f"file {path}: absent"
    if contains:
        try:
            with open(path, errors="replace") as f:
                if not re.search(contains, f.read(), re.M):
                    return False, None, f"file {path}: present, no match for {contains!r}"
        except OSError as e:
            return False, None, f"file {path}: unreadable ({e})"
    st = os.stat(path)
    when = dt.datetime.fromtimestamp(st.st_mtime, dt.timezone.utc).strftime("%H:%M:%SZ")
    return True, True, f"file {path}: present ({st.st_size} bytes, mtime {when})"


def foreign_loops_on(run_id: Any) -> list[int]:
    """Other (non-agentctl) processes already polling this run id."""
    me = os.getpid()
    out = []
    for pid, ppid, etime, cmd in ps():
        if pid in (me, os.getppid()) or "agentctl" in cmd:
            continue
        if re.search(rf"run view {run_id}\b|runs/{run_id}\b", cmd) and "sleep" in cmd:
            out.append(pid)
    return out
