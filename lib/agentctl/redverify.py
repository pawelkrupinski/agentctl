# agentctl, part `redverify`: verifying a landed fix: the scheduled slot, the verify dispatch, a flake check.
# A real module (lib/agentctl/__init__.py MODULES): its own imports, the standard library's and MODULES' only.

import glob
from typing import Any
from agentctl.timefmt import hhmmz, iso_at, parse_time
from agentctl.statefile import read_record
from agentctl.configfile import env_num
from agentctl.core import git, reds_dir, remote
from agentctl.trailers import trailer_values
from agentctl.inboxentry import inbox_write
from agentctl.redrecord import red_what
from agentctl.red import RED_VERIFY_HOLD, red_verify_run_on_way, workflow_main_runs
from agentctl.redmove import red_move
from agentctl.rundispatch import red_verify_dispatch
from agentctl.blameopen import PATHISH
from agentctl.runsigs import cause_of, name_causes
from agentctl.redopen import red_offer, red_open
from agentctl.redfixcheck import fix_reaches_red


def red_verify_scheduled(store, it, f, sha, o_r, repo, t):
    """A scheduled fix (red_verify_plan) at its deadline with no decided run containing it: a run of its workflow
    still undecided that will decide it holds it again, quietly; else the slot passed without verifying it --
    escalated (the scheduler never dispatched it, or its run ended without deciding it)."""
    d, wf = it["data"], it["data"].get("workflow")
    r = red_verify_run_on_way(o_r, repo, f)
    if r:
        line = (f"{red_what(d)}: fix {sha[:10]} awaits {wf} #{r.get('number')} ({r.get('status')}), the run that "
                f"decides it")
        red_move(store, it, "fix-pending", line, data={"fix": {**f, "verify_by": t + RED_VERIFY_HOLD}},
                 deadline_s=RED_VERIFY_HOLD, severity="info")
        return [line]
    since = [r for r in workflow_main_runs(o_r, f.get("verify_file")) if f.get("verify_file")
             and r.get("createdAt") and parse_time(r["createdAt"]).timestamp() >= float(f.get("next_run_at") or 0) - 60]
    why = (f"its {hhmmz(f['next_run_at'])} run #{since[-1].get('number')} ended {since[-1].get('conclusion')} "
           f"without deciding it" if since else f"no {wf} run was dispatched for its {hhmmz(f['next_run_at'])} slot")
    # A MISSED DEADLINE IS NEVER SILENT (the owner, 2026-11-21: Order independence #94 passed its `verified by 06:27Z`
    # with nobody at work): no run decided it by then -- back to a fixer (its live holder first), escalated
    return red_fix_unverifiable(store, it, f"fix {sha[:10]} unverified past its deadline "
                                           f"{hhmmz(f.get('verify_by') or it['deadline'])}: {why}", t)


def red_verify_await(store, it, f, plan, t):
    """Into the quiet wait for a scheduled fix's run: its deadline is the plan's `by`, its level back to 0 (a landing
    is no escalation)."""
    d = it["data"]
    fix = {**f, "next_run_at": plan["slot"], "verify_by": plan["by"], "verify_file": plan["file"]}
    line = (f"{red_what(d)}: fix {str(f.get('sha'))[:10]} awaiting next scheduled run at {hhmmz(plan['slot'])} "
            f"(verified by {hhmmz(plan['by'])})")
    red_move(store, it, "fix-pending", line, data={"fix": fix}, deadline_s=max(60.0, plan["by"] - t), severity="info",
             **({"level": 0} if it.get("level") else {}))
    return [line]


def red_fix_since(it):
    """When a fix-pending red's verification began: the tick that saw its queued fix land (a fix waits in the queue
    behind a red gate for hours, untested), else entering fix-pending."""
    f = it["data"].get("fix") or {}
    return max(float(it["entered_at"]), float(f.get("verify_since") or 0))


def red_fix_unverifiable(store, it, why, t):
    """A fix-pending that cannot rest any longer: offered again (escalated), its fix set aside so it does not snap back
    to fix-pending, the reason in its history -- the dispatcher gives it a fixer."""
    d = it["data"]
    f = d.get("fix") or {}
    h = {"at": iso_at(t), "holder": f.get("entry") or (f.get("sha") or "")[:10] or None, "outcome": f"UNVERIFIED: {why}"}
    upd = {"fix": None, "history": (d.get("history") or []) + [h]}
    if f.get("sha"):
        upd["unverified"] = (d.get("unverified") or []) + [f["sha"]]
    store.close_offer(f"red:{it['id']}", "unverified")
    red_offer(store, it, f"{red_what(d)}: {why}: offered again", escalate=True, data=upd)
    return [f"re-offered {red_what(d)} ({why[:80]})"]


# MAIN MOVED UNDER AN OUTSIDE RED: VERIFY IT (2026-11-22, Order independence #105: 9a7130036 fixed it -- a tools-only
# commit, rightly with no `Fixes-Red:` -- while the red sat fix-pending on an unrelated change that edited its workflow
# file, and its fixer had been stopped hours before; nothing dispatched a run that would have seen main fixed). An
# outside-gate red with no LANDED fix of its own, whose job reads something main changed since the red's run
# (fix_reaches_red over each commit since its head), has its workflow dispatched on main to verify -- once per main
# head, at most every RED_MAIN_VERIFY_EVERY, and only where a verify dispatch is allowed (red_verify_dispatch:
# red-workflows.tsv). A green run clears it as any decided run does; a red one says main has not fixed it.
RED_MAIN_VERIFY_EVERY = 3 * 3600
RED_MAIN_VERIFY_STATES = ("offered", "leased", "fix-pending", "blocked-external")


def _main_verify_due(it: Any, repo: Any, t: Any) -> bool:
    """Is it an open red that no gate, landed fix or recent verify of its own covers (a main verify every
    RED_MAIN_VERIFY_EVERY at most)?"""
    d = it["data"]
    if d.get("gate") or it["state"] not in RED_MAIN_VERIFY_STATES or not repo:
        return False
    if (d.get("fix") or {}).get("status") == "landed":
        return False  # its own landed fix's verify (red_fix_pending) is on its way
    mv = d.get("main_verify") or {}
    return t - float(mv.get("at") or 0) >= env_num("AGENTCTL_RED_MAIN_VERIFY_EVERY", RED_MAIN_VERIFY_EVERY)


def _main_moved(repo: Any, d: Any) -> Any:
    """(main's head, the commits between the red run's head and it) when main moved since the red run and was not
    verified at that head already; None otherwise."""
    head = (d.get("last_run") or {}).get("headSha")
    r = git(repo, "rev-parse", f"{remote()}/main", check=False)
    main = r.stdout.strip() if r.returncode == 0 else ""
    if not head or not main or main == head or (d.get("main_verify") or {}).get("main") == main:
        return None
    r = git(repo, "rev-list", "--no-merges", f"{head}..{main}", check=False)
    return main, (r.stdout.split() if r.returncode == 0 else [])


def red_main_verify(store: Any, it: Any, repo: Any, o_r: Any, t: Any) -> Any:
    d = it["data"]
    if not _main_verify_due(it, repo, t):
        return []
    moved = _main_moved(repo, d)
    if not moved:
        return []
    main, shas = moved
    reach = next((x for x in shas if fix_reaches_red(repo, x, d)), None)
    if not reach:
        return []
    line = red_verify_dispatch(o_r, repo, d.get("workflow"), main, (d.get("last_run") or {}).get("databaseId"))
    store.update_item("red", it["id"], it["version"], {"main_verify": {"at": t, "main": main, "reach": reach,
                                                                       "verify": line}})
    out = f"{red_what(d)}: main moved since its run ({len(shas)} commit(s), {reach[:10]} reaching its job): {line}"
    inbox_write("red", out, [out], [], 0)
    return [out]


def _flake_pass(d: Any, snap: Any) -> Any:
    """The workflow's latest run when a test red of a gate or outside workflow passed on it, else None."""
    if d.get("kind") != "test" or d.get("red_kind") not in ("gate", "outside"):
        return None
    last = ((snap or {}).get("latest") or {}).get(d.get("workflow")) or {}
    return last if last.get("conclusion") == "success" else None


def _unfixed_pass(repo: Any, d: Any, last: Any, facts: Any) -> bool:
    """Passed with no commit between that can have fixed it (and the commits between were readable)."""
    rec = {"runs": d.get("runs"), "job": d.get("job"), "line": d.get("line"), "workflow": d.get("workflow")}
    vals = sorted(set().union(*[v for _s, _c, v, _t in facts.get("landed") or []])) if facts.get("landed") else []
    fixed_by, known = fix_between(repo, rec, (d.get("last_run") or {}).get("headSha"), last.get("headSha"),
                                  name_causes(None, vals), run_workflows())
    return known and not fixed_by


def red_flake_check(store: Any, it: Any, snap: Any, repo: Any, facts: Any) -> Any:
    """A test red that cleared with no commit between that can have fixed it: a FLAKE, its own red (determinism tier,
    lowest priority), open until a fix for it lands and a run proves it."""
    d = it["data"]
    last = _flake_pass(d, snap)
    if not last or not _unfixed_pass(repo, d, last, facts):
        return []
    ob = {"key": d["key"], "workflow": d["workflow"], "jobs": d.get("jobs"), "job": d.get("job"), "kind": "test",
          "line": d.get("line"), "red_kind": "flake", "gate": False, "names": set(d.get("runs") or []),
          "run": d.get("last_run") or {}, "runs_list": []}
    flake = red_open(store, ob, None, store.clock())
    line = (f"RED FLAKE: {red_what(d)} passed on #{last.get('number')} and no commit between can have fixed it: a flake "
            "-- make it deterministic (never rerun to green); it stays a red until a fix lands")
    inbox_write("red", line, [line], [d.get("line") or ""], 1)
    return [line] + ([f"opened flake {flake['id']}"] if flake else [])


def run_workflows():
    """{run number or id: workflow} of every run whose signatures are known."""
    out = {}
    for p in glob.glob(reds_dir("runsig", "*.json")):
        rec = read_record(p)
        if rec:
            for n in (rec.get("run"), rec.get("number")):
                if n is not None:
                    out[str(n)] = rec.get("workflow")
    return out


def fix_between(repo, rec, red_head, green_head, causes, workflows):
    """The commit between a red run's head and the next passing run's head that FIXED this red rather than it passing
    by chance (the owner, 2026-10-27: live drift #2051 was called a flake, and 03d646e63, `Fixes-Red: 2049`, which fixed
    the same failure in another workflow, had landed between): a `Fixes-Red:` naming one of its runs, a run with the
    same cause (any workflow) or a run of the same workflow, or a change to a file its failure names, or to CI's own
    workflows and actions -- a `Fixes-Red:` beside `Red-Unrelated:` names the red only to pass the gate, and fixes
    nothing (`landed_fix_commits`). (sha or None, known): known False when the commits between cannot be read here."""
    if not (repo and red_head and green_head):
        return None, False
    if red_head == green_head:
        return None, True
    chunks = _commits_between(repo, red_head, green_head)
    if chunks is None:
        return None, False
    named = _named_paths(rec)
    for chunk in chunks:
        sha, vals, files = _log_chunk(chunk)
        if _names_this_red(vals, rec, causes, workflows) or _touches_failure_or_ci(files, named):
            return sha, True
    return None, True


def _named_paths(rec: Any) -> set[str]:
    """The paths the red's failure line names."""
    return {m.group(1) for m in PATHISH.finditer(rec.get("line") or "")}


def _commits_between(repo: Any, red_head: Any, green_head: Any) -> Any:
    """The log of red_head..green_head as one chunk per commit (sha, trailers, files), or None when it cannot be read."""
    r = git(repo, "log", f"{red_head}..{green_head}", "--format=%x00%H%x09%(trailers:key=Red-Unrelated,valueonly,separator=%x2C)"
            "%x09%(trailers:key=Fixes-Red,valueonly,separator=%x2C)", "--name-only", check=False)
    return r.stdout.split("\0")[1:] if r.returncode == 0 else None


def _log_chunk(chunk: str) -> tuple[str, set[str], list[str]]:
    """One commit of fix_between's log: (sha, the Fixes-Red values it counts -- none beside Red-Unrelated --, files)."""
    head, _, files = chunk.partition("\n")
    sha, _, rest = head.partition("\t")
    unrelated, _, vals = rest.partition("\t")
    return sha, (set() if unrelated.strip() else trailer_values(vals)), files.split()


def _names_this_red(vals: set[str], rec: Any, causes: Any, workflows: Any) -> bool:
    """Do the Fixes-Red values name one of the red's runs, a run with its cause, or a run of its workflow?"""
    cause = cause_of(rec.get("job"), rec.get("line"))
    return bool(vals & set(map(str, rec.get("runs") or [])) or (cause and any(cause in causes.get(v, ()) for v in vals))
                or any(workflows.get(v) == rec.get("workflow") for v in vals))


def _touches_failure_or_ci(files: list[str], named: set[str]) -> bool:
    """Is one of the files CI's own workflows or actions, or one the failure names?"""
    return any(f.startswith((".github/workflows/", ".github/actions/")) or any(f.endswith(n) or n.endswith(f) for n in named)
               for f in files)
