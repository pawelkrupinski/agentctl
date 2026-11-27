# agentctl, part `ciwhy`: the gate's runs read: main's runs per workflow, decided or not, the red streak, a run resolved by id or number
# A real module (lib/agentctl/__init__.py MODULES): its own imports, the standard library's and MODULES' only.

import itertools
import json
import re
import time
from typing import Any, Callable
from contextlib import suppress
from agentctl.core import attempt, err, git, process_cache, state_root, commit_known
from agentctl.statefile import read_record, write_json
from agentctl.forge import gh_json, RED_CONCLUSIONS, RUN_FIELDS, run_id_of_number
from agentctl.classify import SETUP_STEP
from agentctl.repos import gated_repo, repo_path_for
from agentctl.workflows import STEP_TIMED_OUT
from agentctl.gating import GATING_CHECKS_FILE, gate_flows, gating_checks
from agentctl.redrepos import red_gate_flows
from agentctl.ciruns import ci_runs, main_runs, main_runs_of, Run


_missing_row_said = process_cache(set())  # a red gate run whose failed job has no registry row: said once per run


def registry_name(name):
    """The registry's name of a run's job: github.com names a called workflow's job `<caller job> / <job>`."""
    return str(name or "").split(" / ")[-1]


def workflow_unproven_why(rows: Any, wfile: str) -> str | None:
    """'report only: <wfile> is unproven (<note>)' when the registry holds the gate workflow's own row as unproven."""
    wrow = next((r for r in rows if r["kind"] == "workflow" and r["workflow"] == wfile), None)
    if wrow and wrow["proven"] == "unproven":
        return f"report only: {wfile} is unproven ({wrow.get('note') or 'no green run yet'})"
    return None


def failed_job_states(rows: Any, wfile: str, failed: list[Any]) -> tuple[dict[Any, str], list[Any]]:
    """({failed job: its registry state, a job with no row read as unproven}, [the failed jobs with no row])."""
    state = {r["name"]: r["proven"] for r in rows if r["kind"] == "job" and r["workflow"] == wfile}
    proven = {n: state.get(n, state.get(registry_name(n))) for n in failed}
    missing = [n for n in failed if proven[n] is None]
    return {n: p or "unproven" for n, p in proven.items()}, missing


def gate_report_only_jobs(rows, wfile, jobs):
    """(True, why) when every failed job of a red run of gate workflow wfile is unproven -- or an aggregate following
    one -- and one is; or when wfile's own row is unproven: gating_checks.py report_only, the hook's rule exactly. A
    failed job with NO row is unproven, as the hook reads it (movies' rule: a new check gates only once it has been
    green), and `why` names the missing row -- every gate job should have one (the registry's lint)."""
    unproven_why = workflow_unproven_why(rows, wfile)
    if unproven_why:
        return True, unproven_why
    failed = [j.get("name") for j in jobs or [] if j.get("conclusion") in RED_CONCLUSIONS]
    if not failed:
        return False, "no failed job"
    proven, missing = failed_job_states(rows, wfile, failed)
    return failed_jobs_verdict(proven, failed, missing)


def failed_jobs_verdict(proven: dict[Any, str], failed: list[Any], missing: list[Any]) -> tuple[bool, str]:
    """gate_report_only_jobs' answer from the failed jobs' registry states (`proven`) and those with no row."""
    unproven = [n for n in failed if proven[n] == "unproven"]
    gating = [n for n in failed if proven[n] not in ("unproven", "aggregate")]
    said = (f"; no row in {GATING_CHECKS_FILE}: {', '.join(missing)} (read as unproven -- add its row)"
            if missing else "")
    if gating:
        return False, "gates: " + ", ".join(gating) + said
    if not unproven:
        return False, "only aggregates failed: " + ", ".join(failed)
    return True, "report only, unproven: " + ", ".join(unproven) + said


def gate_report_only(o_r, name, run):
    """(True, why) when gate workflow `name`'s red run holds no push (gate_report_only_jobs); (False, why) when it gates
    -- also when its jobs or the registry cannot be read (an unread red gates, as the hook's exit 2 does)."""
    if not gated_repo(o_r) or not run or run.get("conclusion") not in RED_CONCLUSIONS:
        return False, "not a red gate run"
    rid = str(run.get("databaseId") or "")
    try:
        rows = gating_checks(o_r)
        wfile = next((f for n, f in gate_flows(o_r) if n == name), name)
        jobs = ci_runs().run_jobs(o_r, rid)
    except (Exception, SystemExit) as e:  # SystemExit: NoToken
        return False, f"unread ({str(e)[:120]}): it gates"
    return remember_report_only(name, rid, gate_report_only_jobs(rows, wfile, jobs))


def remember_report_only(name: str, rid: str, out: tuple[bool, str]) -> tuple[bool, str]:
    """out; a missing registry row is said once per run: every gate job should have one."""
    if "no row in" in out[1] and rid not in _missing_row_said:
        err(f"{name} run {rid}: {out[1].split('; ', 1)[-1]}")
        _missing_row_said.add(rid)
    return out


def commit_line(o_r, sha, repo=None):
    repo = repo or repo_path_for(o_r)
    if repo and commit_known(repo, sha):
        return git(repo, "log", "-1", "--format=%h %an <%ae> %ad: %s", "--date=format:%m-%d %H:%M", sha).strip()
    try:
        c = gh_json(o_r, "api", f"repos/{o_r}/git/commits/{sha}")
        a = c.get("author") or c.get("commit", {}).get("author", {})
        msg = c.get("message") or c.get("commit", {}).get("message", "")
        return f"{sha[:9]} {a.get('name')} <{a.get('email')}>: {msg.splitlines()[0] if msg else ''}"
    except Exception:
        return sha[:9]


def timed_out_jobs(log_text):
    """The jobs of a --log-failed output whose log carries bounded-step's timed-out-step warning."""
    return {raw.split("\t", 1)[0] for raw in log_text.splitlines() if STEP_TIMED_OUT in raw and "\t" in raw}


def job_is_phantom(j):
    """Failed before it ever ran: no start time, failing in a setup step (or with no step at all)."""
    if j.get("conclusion") not in ("failure", "timed_out") or j.get("startedAt"):
        return False
    bad = [s for s in j.get("steps") or [] if s.get("conclusion") in ("failure", "timed_out")]
    return not bad or bool(SETUP_STEP.search(bad[0]["name"]))


# AN INCOMPLETE RUN IS NO VERDICT (movies 4da98c308, 2026-10-30: convergence's `verdict complete` job fails a main run
# whose takes or takes-drift rows were skipped, cancelled or missing -- "no false green"; since 2026-11-08 the job lives
# in Identity takes, with the rows, and a drift-only explorer dispatch on main fails only it, by design). A failed run whose ONLY real
# failure (jobs that never started aside) is that job decided nothing: like a cancelled run it says nothing to the gate -- no
# red, no streak, no fixer -- and ci-why names it NO VERDICT. Recovery is the next complete run.
VERDICT_COMPLETE_JOB = re.compile(r"(^|/\s*)verdict complete$")
# the workflows with a `verdict complete` job (only they are read): Identity takes; convergence for its runs before the split
NO_VERDICT_FLOWS = {"Identity takes", "Identity model convergence"}


def job_is_verdict_report(j):
    """The job is a `verdict complete` (it reports on the other rows, it tests nothing)."""
    return bool(VERDICT_COMPLETE_JOB.search(str(j.get("name") or "")))


def jobs_no_verdict(jobs):
    """True when the failed jobs that really ran are all `verdict complete` (and there is one)."""
    real = [j for j in jobs or [] if j.get("conclusion") in RED_CONCLUSIONS and not job_is_phantom(j)]
    return bool(real) and all(job_is_verdict_report(j) for j in real)


# A `verdict complete` BESIDE A REAL FAILURE IS NO RED OF ITS OWN (2026-10-31): it fails because a row did not decide,
# so next to a leg's failure it only repeats it -- yet its own log's error line read as [test], made the run a TEST
# FAILURE even when the leg was infra, and opened a red (and a fixer) keyed on `verdict complete`. It is [incomplete]
# whatever else failed: the run's verdict and its reds are the other jobs' alone.


def run_no_verdict(o_r, r):
    """A completed failed run that is incomplete (jobs_no_verdict): its jobs as the reader keeps them, else the verdict
    another process wrote (no-verdict-runs.json), else read once (and written there); an unreadable run is a verdict
    as listed (never written)."""
    if r.get("status") != "completed" or r.get("conclusion") != "failure" or not r.get("databaseId"):
        return False
    rid = str(r["databaseId"])
    kept = ci_runs().kept_jobs(o_r, rid)
    if kept is not None:
        return jobs_no_verdict(kept)
    path = state_root("no-verdict-runs.json")
    known = read_record(path) or {}
    if rid in known:
        return bool(known[rid])
    try:
        jobs = ci_runs().run_jobs(o_r, rid)
    except (Exception, SystemExit):
        return False
    nv = jobs_no_verdict(jobs)
    with suppress(OSError):
        known = read_record(path) or {}
        known[rid] = nv
        if len(known) > 500:
            known = dict(list(known.items())[-500:])
        write_json(path, known)
    return nv


def newest_decided(runs: list[Run], no_verdict: Callable[[Run], bool]) -> tuple[Run | None, Run | None]:
    """(the newest decided run of `runs` (newest first) that has a verdict, the newest decided run with none). A run
    for which no_verdict(run) holds is an incomplete one: it says nothing, and the search goes on past it."""
    nov = None
    for r in runs:
        if not is_decided(r):
            continue
        if not no_verdict(r):
            return r, nov
        nov = nov or r
    return None, nov


def flows_to_read(o_r: Any, flows: Any) -> Any:
    """[(name, file)] gate_runs reads: `flows`, else a gated repo's registry, else every workflow with a recent run on main."""
    flows = flows or (gate_flows(o_r) if gated_repo(o_r) else None)
    if flows:
        return flows
    # listing: only names the workflows to read; each is then read on its own (main_runs)
    recent = ci_runs().recent_main(o_r, 40)
    return [(n, n) for n in dict.fromkeys(r["workflowName"] for r in recent)]


def gate_runs(o_r, flows=None):
    """[(workflow, newest decided run on main | None, newer unfinished run | None, red_streak of its runs)]. flows: the
    workflows to read ([(name, file)]); default a gated repo's registry (gate_flows), else every workflow with a recent
    run on main."""
    out = []
    for name, wfile in flows_to_read(o_r, flows):
        # listing: the newest 30 of a gate workflow on main; its newest decided run is read, never an absence
        runs = main_runs(o_r, wfile)
        nv = (lambda r: run_no_verdict(o_r, r)) if name in NO_VERDICT_FLOWS else (lambda r: False)
        decided, nov = newest_decided(runs, nv)
        newer = next((r for r in runs if r.get("status") != "completed"), None)
        if newer and decided and newer["databaseId"] < decided["databaseId"]:
            newer = None
        main_runs_of(o_r).gate[name], main_runs_of(o_r).no_verdict[name] = runs, nov
        out.append((name, decided, newer, red_streak(runs, skip=nv)))
    return out


def is_decided(r: Run) -> bool:
    """A run that decided: completed green or red (a cancelled, skipped or running one says nothing)."""
    return r.get("status") == "completed" and r.get("conclusion") in ("success",) + RED_CONCLUSIONS


def decided_runs(runs: list[Run] | None) -> list[Run]:
    return [r for r in runs or [] if is_decided(r)]


def streak_runs(runs: list[Run] | None, skip: Callable[[Run], bool] | None = None) -> list[Run]:
    """The red runs of the streak `runs` (one workflow's on main, newest first) ends on: its decided runs up to the
    newest success. skip(run): a failed run that decided nothing (run_no_verdict), left out like a cancelled one."""
    return [r for r in itertools.takewhile(lambda r: r.get("conclusion") in RED_CONCLUSIONS, decided_runs(runs))
            if not (skip and skip(r))]


def red_streak(runs: list[Run] | None, skip: Callable[[Run], bool] | None = None) -> tuple[str | None, int]:
    """(since, count) of the red streak that `runs` (one workflow's on main, newest first) ends on, `since` the oldest
    failure's completion (when CI first said red). (None, 0) when green."""
    streak = streak_runs(runs, skip)
    if not streak:
        return None, 0
    oldest = streak[-1]
    return oldest.get("updatedAt") or oldest.get("createdAt"), len(streak)


# A RUN NUMBER IS WHAT EVERY RED PRINTS (issue 20261031T085600-c1cbf8: `ci-why 2939` for red Main #2939 explained run
# id 2939, another run). GitHub's run ids are ten digits and more; a run number is a workflow's own count. So a target
# below RUN_ID_MIN is a run NUMBER, resolved on main -- the gate's workflows first, then a red run -- and the others
# that carry it are named; anything larger is a run id.
RUN_ID_MIN = 10 ** 8


def paged_to_number(o_r: Any, wfile: str, n: Any) -> list[Run]:
    """The runs of workflow file wfile on main paged to where run number n sits ([] when it is 1000+ back or newer)."""
    # listing: the newest one, then paged to where #n sits; a number further back is none of these
    newest = main_runs(o_r, wfile, limit=1)
    gap = int(newest[0]["number"]) - int(n) if newest else -1
    return main_runs(o_r, wfile, limit=min(1000, gap + 50)) if 0 <= gap < 1000 else []


def runs_of_number(o_r, n):
    """The runs on main whose run NUMBER is n, newest list first: the newest 200 on main, else each gate workflow's
    run list paged to where #n sits (a workflow's numbers count down its list)."""
    n = str(n)
    seen, hits = set(), []

    def take(runs):
        for r in runs or []:
            if str(r.get("number")) == n and r.get("databaseId") not in seen:
                seen.add(r.get("databaseId"))
                hits.append(r)
    # listing: a run number's hits in the newest 200; each gate workflow not hit is asked on its own below
    attempt(lambda: take(ci_runs().recent_main(o_r, 200)),
            None, catch=(Exception, SystemExit), what="ci-why: could not list main's runs")
    for name, wfile in red_gate_flows(o_r):
        if not any(r.get("workflowName") == name for r in hits):
            attempt(lambda: take(paged_to_number(o_r, wfile, n)), None,
                    catch=(Exception, SystemExit, ValueError, KeyError))
    gate = {g for g, _ in red_gate_flows(o_r)}
    return sorted(hits, key=lambda r: (r.get("workflowName") not in gate, r.get("conclusion") not in RED_CONCLUSIONS))


def run_target(o_r, target):
    """(run id, [lines saying how it was read]) of ci-why's target: a run id, or a run NUMBER (above)."""
    t = str(target).lstrip("#")
    if int(t) >= RUN_ID_MIN:
        return t, []
    hits = runs_of_number(o_r, t)
    if not hits:  # no run carries that number: an id, as ever (a short id is a test's, or another forge's)
        return t, [f"no run on main numbered #{t} among the newest: read as a run id"]
    r, others = hits[0], hits[1:]
    said = [f"#{t} is {r.get('workflowName')} #{t}, run id {r['databaseId']} (a run NUMBER: the id is the number in "
            f"its URL)"]
    if others:
        said.append("#" + t + " also numbers: " + ", ".join(f"{o.get('workflowName')} (run id {o['databaseId']})"
                                                        for o in others[:5]) + " -- `agentctl ci-why <run id>`")
    return r["databaseId"], said


def gate_snap_rows(o_r: str, gate: list[tuple[Any, Any, Any, Any]]) -> list[dict[str, Any]]:
    """The snapshot's `gate` rows of gate_runs' units (the inverse of gate_from_snap): each with its listed runs."""
    mine = main_runs_of(o_r)
    return [{"name": n, "decided": d, "newer": nw, "since": st[0], "count": st[1],
             "runs": mine.gate.get(n, []), "no_verdict": mine.no_verdict.get(n)} for n, d, nw, st in gate]


def gate_from_snap(snap):
    out = []
    for g in snap["gate"]:
        main_runs_of(snap["repo"]).gate[g["name"]] = g["runs"]
        main_runs_of(snap["repo"]).no_verdict[g["name"]] = g.get("no_verdict")
        out.append((g["name"], g["decided"], g["newer"], (g["since"], g["count"])))
    return out


def outside_from_snap(snap):
    out = []
    for g in snap["outside"]:
        main_runs_of(snap["repo"]).outside[g["name"]] = g["runs"]
        out.append((g["name"], g["decided"], g["since"], g["count"]))
    return out


def run_in_lists(o_r: str, n: Any) -> Any:
    """(workflow, run) of a known run of the repository's main a number or id names, else None."""
    known = [(name, r) for name, runs in main_runs_of(o_r).lists() for r in runs]
    for key in ("number", "databaseId"):  # a run NUMBER first (the repo's convention), an id only failing that
        found = next(((name, r) for name, r in known if str(r.get(key)) == n), None)
        if found:
            return found
    return None


def run_listed_or_viewed(o_r: Any, n: Any) -> Any:
    """The run on main a number or id names: from the newest 200, else by its id, else viewed."""
    # listing: a number not in the newest 200 falls back to its run id (run_id_of_number), then says unknown
    runs = ci_runs().recent_main(o_r, 200)
    r = next((r for r in runs if str(r.get("number")) == n), None)
    if r is None:
        rid = run_id_of_number(o_r, n) if n.isdigit() else None
        r = next((r for r in runs if str(r.get("databaseId")) == str(rid or n)), None)
        if r is None:
            r = gh_json(o_r, "run", "view", str(rid or n), "--json", RUN_FIELDS)
    return r


def resolve_run(o_r, n):
    """(workflow, run) of the run on main a number or id names, else (None, None)."""
    n = str(n).lstrip("#")
    known = run_in_lists(o_r, n)
    if known:
        return known
    try:
        r = run_listed_or_viewed(o_r, n)
        return r.get("workflowName"), r
    except (Exception, SystemExit) as e:
        err(f"red: could not look up run {n} ({str(e)[:120]}): claimed by its number only")
        return None, None
