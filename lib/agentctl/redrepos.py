# agentctl, part `redrepos`: a watched repo's red policy: another repo's or the primary's config, its gate workflows,
# owner-only pushes, run directories and keys; its red families (one fixer per family); its per-workflow policy on main
# A real module (lib/agentctl/__init__.py MODULES): its own imports, the standard library's and MODULES' only.

import re
from typing import Any, Callable
from typing import Optional
import os
from agentctl.core import err, git, git_ok, process_cache, remote, run_cmd
from agentctl.forge import forge_argv, forge_env
from agentctl.repos import gated_repo, RED_PRIMARY, red_repo_conf, red_repo_of, red_repo_short
from agentctl.gating import gate_flows
from agentctl.statefile import load_record
from agentctl.selfcode import TOOL_PATH
from agentctl.redknobs import knob_red_families
from agentctl.workflows import LOCAL_FLOW
from agentctl.classify import norm_line


def red_is_other(o_r):
    """True for a watched repo other than the primary (red-repos.json). Any other name -- the primary, or a repo nobody
    configured (the tests' `o/r`) -- is treated as the primary, exactly as before 2026-10-30."""
    return o_r != RED_PRIMARY and red_repo_conf(o_r) is not None


def red_gate_flows(o_r):
    """[(name, file)] of a watched repo's gate workflows: the registry's for a gated repo (gate_flows), its config's for
    the others."""
    if gated_repo(o_r):
        return gate_flows(o_r)
    c = red_repo_conf(o_r)
    return list(c["gate"]) if c else []


def red_owner_only(o_r: Any) -> Any:
    """True for a repo whose fixer never pushes (red-repos.json `"push": "owner-only"`)."""
    c = red_repo_conf(o_r)
    return bool(c and c["push"] == "owner-only")


def red_repo_tag(o_r, num):
    """A red run's directory name under reds/: the run number for the primary (as always), `<repo>-<number>` for the
    others -- run numbers are per repo, so infra #3455 and movies #3455 are two runs."""
    return f"{red_repo_short(o_r)}-{num}" if red_is_other(o_r) else str(num)


def red_repo_key(o_r, key):
    """A red's key in the store: as it always was for the primary; for another repo, its key bound to the repo, so the
    same workflow name and line in two repos are two reds."""
    if not red_is_other(o_r):
        return key
    import hashlib
    return hashlib.sha1(f"{o_r}\0{key}".encode()).hexdigest()[:12]


def read_failure(o_r: str, run_id: Any) -> str | None:
    """The failing steps' log of a run of repo o_r, as `gh run view --log-failed` prints it; None when it cannot say."""
    try:
        argv = forge_argv(o_r, "run", "view", str(run_id), "--log-failed")
        r = run_cmd(argv, timeout=90, env=forge_env(o_r))
    except OSError:  # no gh
        return None
    return r.stdout if r.returncode == 0 and r.stdout.strip() else None


def diagnosis(log: str | None) -> str:
    """What a red run's log says to do about it (tests/run.py prints `seed:`, `FAIL: <id>` and `REPLAY: <command>`): the
    failing tests, the seed and the command that replays the order; '' when the log has none of them."""
    if not log:
        return ""
    fails = list(dict.fromkeys(re.findall(r"FAIL: (\S+)", log)))
    seed = re.search(r"seed: (\d+)", log)
    replay = re.search(r"REPLAY: (python3 tests/run\.py[^\r\n]*)", log)
    parts = [f"failing: {', '.join(fails[:3])}{' ...' if len(fails) > 3 else ''}" if fails else "",
             f"seed {seed.group(1)}" if seed else "", f"replay: {replay.group(1).strip()}" if replay else ""]
    return "; ".join(p for p in parts if p)


def red_repo_note(o_r: str, run: dict[str, Any], failure: Callable[[str, Any], str | None] = read_failure) -> str:
    """The repo's own rules for its fixer (red-repos.json `note`: agentctl's -- how it pushes, never rerun or quarantine)
    followed by its failing run's diagnosis from the run's log (the failing tests, the seed, the REPLAY command); ''
    for a repo with no note."""
    note = (red_repo_conf(o_r) or {}).get("note")
    if not note:
        return ""
    why = diagnosis(failure(o_r, run.get("databaseId")))
    return f"{note}" + (f" THIS RED'S RUN, from its log: {why}." if why else "")


RED_WORKFLOWS_FILE = ".github/red-workflows.tsv"  # workflow, verify_dispatch (yes|no), replay (a command or -), why


def red_workflow_policies(repo: Optional[str], ref: Optional[str] = None) -> dict[str, dict[str, Any]]:
    """{workflow: {verify_dispatch: bool, replay: command or None, why}} from RED_WORKFLOWS_FILE at ref (main)."""
    txt = git(repo, "show", f"{ref or remote() + '/main'}:{RED_WORKFLOWS_FILE}", check=False).stdout if repo else ""
    out = {}
    for line in (txt or "").splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        cols = [c.strip() for c in line.split("\t")]
        cols += [""] * (4 - len(cols))
        out[cols[0]] = {"verify_dispatch": cols[1].lower() == "yes",
                        "replay": cols[2] if cols[2] not in ("", "-") else None, "why": cols[3]}
    return out


def red_workflows_on_main(repo: Optional[str], ref: Optional[str] = None) -> bool:
    """RED_WORKFLOWS_FILE exists at ref (main). Absent is not "no workflow has a replay": a repo whose red-repos.json
    entry says `red_workflows` refuses Fixes-Red landings while it is (fix_replay_refusal)."""
    return bool(repo and git_ok(repo, "cat-file", "-e", f"{ref or remote() + '/main'}:{RED_WORKFLOWS_FILE}"))


# VERIFY PROMPTLY BY DEFAULT (the owner, 2026-11-21: "structural fix for the order independence being abandoned" --
# Order independence #94's fix landed and waited ~24 h for its 02:40Z cron, no one at work): a workflow with no row
# in RED_WORKFLOWS_FILE MAY be dispatched to verify a landed fix -- at once, never after its next slot. A costly one
# opts out with a row, `verify_dispatch no` and why (more than ~60 runner-minutes a run, or a paid service).
def red_workflow_policy(repo: Optional[str], workflow: Any, ref: Optional[str] = None) -> dict[str, Any]:
    return red_workflow_policies(repo, ref).get(workflow) or {"verify_dispatch": True, "replay": None, "why": ""}


def verify_dispatchable(repo: Optional[str], workflow: Any) -> bool:
    """The registry says `workflow` may be dispatched to verify a fix."""
    return bool(red_workflow_policy(repo, workflow)["verify_dispatch"])


# RED FAMILIES (the owner, 2026-10-29: "give all convergence red work to one agent"): the reds of one family -- by
# workflow, or by a failing job's name (Verify scrape recording's `convergence (<cc>)` is convergence work too) -- go to
# ONE fixer. While it is alive, the dispatcher puts a new red of its family under its lease (a sibling it renews with
# its own) and tells it in RED-INBOX.md in its worktree, never a second fixer; when it ends, the next fixer of the
# family goes on in its worktree (previous_fixer_tree). The families are config: red-families.json beside agentctl
# ($AGENTCTL_RED_FAMILIES names another file). `"outside_gate": true` matches every red of a workflow the gate does
# not read (red_outside_gate; the owner, 2026-11-07: every non-gating red to ONE agent).
_families_cache = process_cache({})


def red_families():
    path = knob_red_families() or os.path.join(os.path.dirname(TOOL_PATH), "red-families.json")
    try:
        m = os.path.getmtime(path)
    except OSError:
        return []
    hit = _families_cache.get(path)
    if not hit or hit[0] != m:
        hit = _families_cache[path] = (m, load_families(path))
    return hit[1]


def load_families(path: str) -> list[dict[str, Any]]:
    """The families of the config file at path, their job patterns compiled; none when it is unreadable."""
    try:
        fams = load_record(path).get("families") or []
        return [dict(x, _jobs=[re.compile(j) for j in x.get("jobs") or []]) for x in fams if x.get("name")]
    except (OSError, ValueError, re.error) as e:
        err(f"red families: {path} unreadable ({e}): none apply")
        return []


def _family_names_red(fam: dict[str, Any], d: dict[str, Any], jobs: list[str]) -> bool:
    """Whether a family's workflows, job patterns or outside_gate flag name the red (its workflow, its jobs)."""
    return (d.get("workflow") in (fam.get("workflows") or []) or any(rx.search(j) for rx in fam["_jobs"] for j in jobs)
            or bool(fam.get("outside_gate") and red_outside_gate(d)))


def _family_applies(fam: dict[str, Any], d: dict[str, Any], jobs: list[str]) -> bool:
    """A family naming workflows, jobs or outside_gate applies when one names the red AND its kinds (if any) hold; one
    naming only kinds, when the red's kind is among them."""
    kinds = fam.get("kinds")
    kind_ok = not kinds or d.get("kind") in kinds
    if fam.get("workflows") or fam["_jobs"] or fam.get("outside_gate"):
        return _family_names_red(fam, d, jobs) and kind_ok
    return bool(kinds) and kind_ok


def red_family(d):
    """The family of a red (its data, its picture, a fixer record: workflow, job, jobs), or None."""
    if not d:
        return None
    o_r = red_repo_of(d)
    if o_r != RED_PRIMARY:  # ANOTHER WATCHED REPO: its red-repos.json family, every red of it (red-families.json's
        return (red_repo_conf(o_r) or {}).get("family") or None  # families are the primary's)
    jobs = [str(j) for j in (d.get("jobs") or []) + [d.get("job")] if j]
    return next((fam["name"] for fam in red_families() if _family_applies(fam, d, jobs)), None)  # the first that applies


def red_outside_gate(d):
    """A red of a workflow THE GATE does not read (the owner, 2026-11-07: "move all these to single agent"): the repo's
    gate is red_gate_flows (movies' gating checks registry: Main, Identity model convergence, Identity takes -- the set ci-why's "Outside the gate"
    leaves out); the local checks are the landing gate's own, never outside it. No workflow named: cannot tell, no."""
    wf = d.get("workflow")
    return bool(wf) and wf != LOCAL_FLOW and wf not in {n for n, _f in red_gate_flows(red_repo_of(d))}


def red_identity(d):
    """A red's identity fields: its signature (the normalized first error line, None when none was read) and its
    configured family (None: its run is its queue family)."""
    return {"signature": norm_line(d.get("line")) or None, "family": red_family(d)}
