# agentctl, part `govusage`: a job's cores: its class's default, else what its recorded runs measured; each run's usage recorded
# A real module (lib/agentctl/__init__.py MODULES): its own imports, the standard library's and MODULES' only.

from agentctl.clock import epoch_now
from typing import Any
import json
import os
from agentctl.core import pct
from agentctl.machine import machine_cores, machine_load1
from agentctl.govtickets import gov_dir
from agentctl.statefile import note_failure


# A job's expected cores, by its name (scripts/jvm-gate.sh passes --job: sbt-warm classifies its command); measured
# history (usage.jsonl) replaces the number once a job has GOV_USAGE_MIN uncontended runs. `job`: a JVM of unknown work.
GOV_JOB_CORES = {"testUnit": 4.0, "test": 4.0, "what-if": 4.0, "measure": 4.0, "check": 4.0, "compile": 3.0,
                 "job": 3.0, "testOnly": 2.0, "slice": 2.0, "suite": 2.0, "serve": 1.0, "keeper": 0.0}


GOV_USAGE_MIN = 3       # uncontended runs of a job before its measured cores replace the default


GOV_USAGE_DAYS = 14


GOV_USAGE_MIN_WALL = 20  # seconds: a shorter call says nothing about a job's cores


# How a counted run's CPU was measured: its process tree, sampled (movies' jvm-gate.sh cpu meter). The runs before it
# read the server's `ps -S`, which on macOS adds no exited child's CPU: a testUnit's forked test JVMs went uncounted, so
# it measured ~0.5 cores while its fork ran at 5, and the governor admitted eight at once (2026-11-25: load 83 on 18
# cores, 10.5 charged). Those runs, and an older client's, are not counted.
GOV_USAGE_METER = "tree"


def gov_usage(days=GOV_USAGE_DAYS, now=None):
    """usage.jsonl's records of the last `days`: {at, job, wall, cpu, cores, load1, machine_cores}."""
    cutoff = (now or epoch_now()) - days * 86400
    out = []
    try:
        with open(os.path.join(gov_dir(), "usage.jsonl")) as f:
            for line in f:
                try:
                    r = json.loads(line)
                except ValueError:
                    continue
                if r.get("at", 0) >= cutoff:
                    out.append(r)
    except FileNotFoundError:
        pass
    except OSError as e:
        note_failure("usage log unreadable", e)
    return out


def uncontended_run(r: Any, job: Any) -> bool:
    """Whether usage record r is a metered (GOV_USAGE_METER) run of `job` that measured cores, long enough, with the load
    at its end within the cores."""
    return bool(r.get("job") == job and r.get("meter") == GOV_USAGE_METER and r.get("cores") is not None and float(r.get("wall") or 0) >= GOV_USAGE_MIN_WALL
                and r.get("load1") is not None and float(r["load1"]) <= float(r.get("machine_cores") or machine_cores()))


def gov_measured_cores(job, usage=None):
    """The job's p75 cores over its uncontended metered runs (uncontended_run: a starved run uses fewer cores than it
    wants), to the half core; None under GOV_USAGE_MIN such runs."""
    runs = [float(r["cores"]) for r in (gov_usage() if usage is None else usage) if uncontended_run(r, job)]
    if len(runs) < GOV_USAGE_MIN:
        return None
    return min(float(machine_cores()), max(0.5, round(2 * pct(runs, .75)) / 2))


def gov_job_cores(job=None, cls="agent", kind="job"):
    """The cores a ticket is charged: its job's measured cores, else its default; with no job named, a check's (agentctl's
    own pre-commit check: the hook's sbt) or the suite's, else none (a caller from before cores were charged: memory
    only, as it was)."""
    if not job:
        job = "check" if kind == "check" else "suite" if cls == "suite" else None
        if not job:
            return 0.0
    if job == "keeper":
        return 0.0
    measured = gov_measured_cores(job)
    return measured if measured is not None else GOV_JOB_CORES.get(job, GOV_JOB_CORES["job"])


def gov_record_usage(job, wall, cpu, load1=None, label="", meter=""):
    """One run's cores (CPU seconds over wall seconds) for the job's measured history, with how it was measured
    (`meter`: only GOV_USAGE_METER's are counted); a run under GOV_USAGE_MIN_WALL is not recorded."""
    if wall < GOV_USAGE_MIN_WALL:
        return None
    rec = {"at": epoch_now(), "job": job, "label": label, "wall": round(wall, 1), "cpu": round(cpu, 1),
           "cores": round(cpu / wall, 2), "load1": machine_load1() if load1 is None else load1,
           "machine_cores": machine_cores(), "meter": meter}
    with open(os.path.join(gov_dir(), "usage.jsonl"), "a") as f:
        f.write(json.dumps(rec) + "\n")
    return rec
