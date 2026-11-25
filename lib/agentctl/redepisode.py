# agentctl, part `redepisode`: THE RUNS OF AN OPEN GATE RED'S EPISODE, read from the CI snapshot and the cached run signatures.
# A real module (lib/agentctl/__init__.py MODULES): its own imports, the standard library's and MODULES' only.

from typing import Any
from agentctl.core import SHORT, reds_dir
from agentctl.statefile import read_record
from agentctl.classify import norm_line
from agentctl.ciwhy import ci_state_load, streak_runs
from agentctl.runsigs import classifier_version, run_names


# AN EPISODE IS THE RUNS FAILED SINCE THE LAST GREEN, FAILING THE SAME WAY (movies' CLAUDE.md: `Fixes-Red:` names "any red
# run of the current episode"; 2026-11-25, batch #124: the red item's own names had lost a run the fix named, and the fix
# waited for the green only it could bring). The red item's names are what its ticks attached; the episode is read here
# straight from the gate's snapshot -- the workflow's red streak -- by each run's CACHED signature (no read of the forge:
# a run whose signature is not cached under this classifier is not counted, it never guesses).
def red_episode_names(d: dict[str, Any], snap: Any = None) -> set[str]:
    """The names (numbers and ids) of the gate runs, in the snapshot's red streak of red data d's workflow, whose cached
    signature has d's failing job and line; empty for a red outside the gate or with no snapshot."""
    snap = ci_state_load(d.get("repo") or SHORT["movies"]) if snap is None else snap
    row = gate_row(d, snap)
    version, line = classifier_version(), norm_line(d.get("line"))
    out: set[str] = set()
    for r in streak_runs((row or {}).get("runs")):
        if run_failed_so(r, version, d["job"], line):
            out |= run_names(r)
    return out


def gate_row(d: dict[str, Any], snap: Any) -> dict[str, Any] | None:
    """The snapshot's gate row of red data d's workflow, when d is a gate red with a failing job."""
    row = next((g for g in (snap or {}).get("gate") or [] if g.get("name") == d.get("workflow")), None)
    return row if d.get("gate") and d.get("job") else None


def run_failed_so(r: dict[str, Any], version: str, job: str, line: str) -> bool:
    """Whether run r's cached signature (read under classifier `version`) names job with this normalised line."""
    rec = read_record(reds_dir("runsig", f"{r.get('databaseId')}.json"))
    return rec is not None and rec.get("classifier") == version \
        and any(x.get("job") == job and norm_line(x.get("line")) == line for x in rec.get("sigs") or [])
