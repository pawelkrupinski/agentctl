# agentctl, part `govbudget`: the governor's memory budget, each class's share of it, and the check cap it holds (one sbt server's worth a check), the check runs log and the slot holders
# A real module (lib/agentctl/__init__.py MODULES): its own imports, the standard library's and MODULES' only.

from agentctl.clock import epoch_now
import os
import json
from agentctl.knobs import env_num
from agentctl.core import state_root
from agentctl.statefile import read_record
from agentctl.machine import ram_gb
from agentctl.govtickets import gov_tickets


GOV_MEM_GB = {"red": 5.0, "check": 5.0, "agent": 5.0, "measure": 6.0, "hook": 1.0, "suite": 1.0}


def gov_budget():
    return env_num("AGENTCTL_GOV_BUDGET_GB", round(0.6 * ram_gb(), 1))


# ---------------------------------------------------------------- check slots
# The owner (2026-10-27): "just limit the precommit checks to a reasonable number", and "keep measuring to adjust that
# number". Every pre-commit check run (land's dry run, the `waiting` batch check, the known-red baseline) is admitted by
# THE GOVERNOR (above) as a check-kind ticket of class `check` (`red` for a change fixing a red run, which skips the
# cap): at most N run at once. Its ticket counts a JVM's 5 GB: the hook's `sbt -batch` takes no ticket of its own; an
# sbt server it starts through sbt-warm does (at the check's priority, KINOWO_GOV_CLASS), counted twice for a while. Its
# cores are the check's (GOV_JOB_CORES["check"]): the hook's sbt-warm calls take no cores ticket (KINOWO_GOV_CPU_HELD).
# N: $AGENTCTL_MAX_CHECKS if set, else the automatic cap (checks/cap.json, default 3, at most the budget's worth of sbt
# servers). A waiter prints why it waits. Each run appends to checks/runs.jsonl: its duration, its wait, the cap and the
# number of checks running, the load average, CPU % and memory pressure / swap sampled during it. After each run the cap
# is adjusted from the data (adjust_cap), at most once a day, each change logged and sent to the inbox.
CHECKS_DEFAULT_CAP = 3


def checks_dir():
    d = os.environ.get("AGENTCTL_CHECKS_DIR") or state_root("checks")
    os.makedirs(d, exist_ok=True)
    return d


def gov_max_checks():
    """The most checks the budget holds: one sbt server's worth of memory each."""
    return max(1, int(gov_budget() // GOV_MEM_GB["check"]))


def cap_state():
    return read_record(os.path.join(checks_dir(), "cap.json"), {"cap": CHECKS_DEFAULT_CAP})


def checks_cap():
    if os.environ.get("AGENTCTL_MAX_CHECKS"):
        return max(1, int(os.environ["AGENTCTL_MAX_CHECKS"]))
    return max(1, min(gov_max_checks(), int(cap_state().get("cap", CHECKS_DEFAULT_CAP))))


def slot_holders(cap=None):
    return [f"pid {t.get('pid')} {t.get('label')} since {t.get('since')} ({t.get('class')})"
            for t in gov_tickets() if t.get("kind") == "check"]


def check_runs(days=7, now=None):
    now = now or epoch_now()
    out = []
    try:
        with open(os.path.join(checks_dir(), "runs.jsonl")) as f:
            for line in f:
                try:
                    r = json.loads(line)
                except ValueError:
                    continue
                if r.get("at", 0) >= now - days * 86400:
                    out.append(r)
    except OSError:
        pass
    return out
