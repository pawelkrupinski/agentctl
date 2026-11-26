# agentctl, part `ciwhy`: the gate's runs read: main's runs per workflow, decided or not, the red streak, a run resolved by id or number; the CI snapshot
# A real module (lib/agentctl/__init__.py MODULES): its own imports, the standard library's and MODULES' only.

from agentctl.clock import epoch_now
import datetime as dt
import itertools
import json
import re
import time
from typing import Any, Callable
from contextlib import suppress
from agentctl.core import attempt, err, fmt_age, git, git_ok, process_cache, state_root
from agentctl.statefile import read_record, write_json
from agentctl.forge import RED_CONCLUSIONS, RUN_FIELDS, gh_json
from agentctl.classify import SETUP_STEP
from agentctl.repos import gated_repo, repo_path_for
from agentctl.workflows import STEP_TIMED_OUT
from agentctl.gating import GATING_CHECKS_FILE, gate_flows, gating_checks
from agentctl.redrepos import red_gate_flows
from agentctl.cistate import CI_STATE_MAX_AGE, state_read, state_update
from agentctl.forge import run_id_of_number


_report_only_mem = process_cache({})  # run id -> (bool, why): a decided run's jobs never change


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
    if rid in _report_only_mem:
        return _report_only_mem[rid]
    try:
        rows = gating_checks(o_r)
        wfile = next((f for n, f in gate_flows(o_r) if n == name), name)
        jobs = gh_json(o_r, "run", "view", rid, "--json", "jobs").get("jobs") or []
    except (Exception, SystemExit) as e:  # SystemExit: NoToken
        return False, f"unread ({str(e)[:120]}): it gates"
    return remember_report_only(name, rid, gate_report_only_jobs(rows, wfile, jobs))


def remember_report_only(name: str, rid: str, out: tuple[bool, str]) -> tuple[bool, str]:
    """out, kept for run rid; a missing registry row is said once per run: every gate job should have one."""
    if "no row in" in out[1] and rid not in _report_only_mem:
        err(f"{name} run {rid}: {out[1].split('; ', 1)[-1]}")
    if rid:
        _report_only_mem[rid] = out
    return out


# A WORKFLOW NOT ON MAIN HAS NO RUNS ON MAIN (2026-11-08 16:35-18:10: agentctl's gate list named identity-takes.yml before
# the movies commit adding the file reached main; GitHub answered each list of it `HTTP 404: workflow ... not found on
# the default branch`, so the whole gate read failed every cycle -- the status line said `gate UNREAD (as of 16:44)` and
# every production landing waited on a gate taken as red for 1.5 h). That 404 is GitHub's own answer, not an unread
# gate: the workflow has no run on main, so it decides nothing until its file lands. Any other error still raises.
WORKFLOW_NOT_ON_MAIN = re.compile(r"HTTP 404: workflow \S+ not found on the default branch")
_not_on_main_said = process_cache(set())  # said once per workflow file per process, not every cycle


def main_runs(o_r, wfile, limit=30):
    """A workflow's runs on main, newest first; [] when GitHub says its file is not on the default branch."""
    try:
        # listing: the newest `limit` of one workflow on main; callers read its newest decided run, never an absence
        return gh_json(o_r, "run", "list", "--workflow", wfile, "--branch", "main", "--limit", str(limit),
                       "--json", RUN_FIELDS)
    except RuntimeError as e:
        if not WORKFLOW_NOT_ON_MAIN.search(str(e)):
            raise
        if wfile not in _not_on_main_said:
            _not_on_main_said.add(wfile)
            err(f"{wfile} is not on main yet: no run of it gates anything until it lands")
        return []


def commit_line(o_r, sha, repo=None):
    repo = repo or repo_path_for(o_r)
    if repo and git_ok(repo, "cat-file", "-e", f"{sha}^{{commit}}"):
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
_no_verdict_mem = process_cache({})


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
    """A completed failed run that is incomplete (jobs_no_verdict): read once per run, kept (a decided run never
    changes); an unreadable run is a verdict as listed (never cached)."""
    if r.get("status") != "completed" or r.get("conclusion") != "failure" or not r.get("databaseId"):
        return False
    rid = str(r["databaseId"])
    if rid in _no_verdict_mem:
        return _no_verdict_mem[rid]
    path = state_root("no-verdict-runs.json")
    known = read_record(path) or {}
    if rid in known:
        _no_verdict_mem[rid] = bool(known[rid])
        return _no_verdict_mem[rid]
    try:
        d = gh_json(o_r, "run", "view", rid, "--json", "jobs")
    except (Exception, SystemExit):
        return False
    nv = _no_verdict_mem[rid] = jobs_no_verdict(d.get("jobs"))
    with suppress(OSError):
        known = read_record(path) or {}
        known[rid] = nv
        if len(known) > 500:
            known = dict(list(known.items())[-500:])
        write_json(path, known)
    return nv


Run = dict[str, Any]  # a workflow run as gh lists it (RUN_FIELDS)


class MainRuns:
    """ONE repository's runs on main, as gate_runs / outside_gate / a snapshot last listed them (newest first), by
    workflow. Review 2026-11-26 (B F1): these were three process globals keyed by workflow NAME alone, imported by
    private name into 8 modules; red_gate_snap filled them with another repo's runs and put the primary's back in a
    `finally` (names repeat across repos), so a reader in the meantime -- the gate-publish thread, or the tick's own --
    saw the other repo's runs. Now each repository has its own (main_runs_of), and nothing is saved or restored."""

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


_main_runs: dict[str, MainRuns] = process_cache({})


def main_runs_of(o_r: str) -> MainRuns:
    """The repository's MainRuns (made empty on first ask)."""
    return _main_runs.setdefault(o_r, MainRuns())


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
    recent = gh_json(o_r, "run", "list", "--branch", "main", "--limit", "40", "--json", RUN_FIELDS)
    return [(n, n) for n in dict.fromkeys(r["workflowName"] for r in recent)]


def gate_runs(o_r, flows=None):
    """[(workflow, newest decided run on main | None, newer unfinished run | None, red_streak of its runs)]. flows: the
    workflows to read ([(name, file)]); default a gated repo's registry (gate_flows), else every workflow with a recent
    run on main."""
    out = []
    for name, wfile in flows_to_read(o_r, flows):
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


def red_for(since, now=None):
    """'3d 4h' / '5h 12m' / '7m' since an ISO time, or '?' when it cannot be read."""
    try:
        t = dt.datetime.fromisoformat(str(since).replace("Z", "+00:00"))
    except ValueError:
        return "?"
    if now is None:  # the process's clock (the tests wire theirs; review 2026-11-26: $AGENTCTL_NOW was a second one)
        now = dt.datetime.fromtimestamp(epoch_now(), dt.timezone.utc)
    mins = max(0, int((now - t).total_seconds() // 60))
    d, h, m = mins // 1440, mins % 1440 // 60, mins % 60
    return f"{d}d {h}h" if d else f"{h}h {m}m" if h else f"{m}m"


# A RUN NUMBER IS WHAT EVERY RED PRINTS (issue 20261031T085600-c1cbf8: `ci-why 2939` for red Main #2939 explained run
# id 2939, another run). GitHub's run ids are ten digits and more; a run number is a workflow's own count. So a target
# below RUN_ID_MIN is a run NUMBER, resolved on main -- the gate's workflows first, then a red run -- and the others
# that carry it are named; anything larger is a run id.
RUN_ID_MIN = 10 ** 8


def paged_to_number(o_r: Any, wfile: str, n: Any) -> list[Run]:
    """The runs of workflow file wfile on main paged to where run number n sits ([] when it is 1000+ back or newer)."""
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
    attempt(lambda: take(gh_json(o_r, "run", "list", "--branch", "main", "--limit", "200", "--json", RUN_FIELDS)),
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


def ci_state_write(snap):
    state_update(ci=snap)
    return snap


def ci_state_load(o_r: str, now: float | None = None) -> dict[str, Any] | None:
    """The snapshot when it is for o_r and at most CI_STATE_MAX_AGE old, else None (unread)."""
    try:
        snap = state_read().get("ci") or {}
        age = (now or epoch_now()) - float(snap["at"])
    except (KeyError, TypeError, ValueError):
        return None
    return snap if snap.get("repo") == o_r and -5 <= age <= CI_STATE_MAX_AGE else None


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


def serving_runners(q: dict[str, Any], want: set[str]) -> dict[str, Any]:
    """{name: runner} of the queue snapshot's runners whose labels include all of `want`; {} when a label cannot be read."""
    runners = ((q.get("limits") or {}).get("runners") or {}).items()
    return attempt(lambda: {n: r for n, r in runners if want <= set(json.loads(r.get("labels") or "[]"))},
                   {}, catch=(ValueError, TypeError))


def slot_usage(q: dict[str, Any], serving: dict[str, Any]) -> tuple[int, int]:
    """(busy slots, capacity) of the `serving` runners in queue snapshot q."""
    cap = sum(int(r.get("capacity") or 0) for r in serving.values())
    return sum(1 for x in q.get("running") or [] if x.get("runner") in serving), cap


def waiting_ahead(waits: list[dict[str, Any]], mine: list[dict[str, Any]], label: str) -> tuple[int, int]:
    """(how many jobs on `label` have waited longer than the run's own, the longest wait) in seconds."""
    waited = [int(w.get("secs") or 0) for w in waits if w.get("label") == label]
    own = max(int(w.get("secs") or 0) for w in mine if w.get("label") == label)
    return sum(1 for x in waited if x > own), max(waited)


def label_full_says(q: dict[str, Any], waits: list[dict[str, Any]], mine: list[dict[str, Any]], label: str) -> str | None:
    """'<labels> full (busy/cap busy, M ahead, oldest Xm)' when every runner serving `label` is up with all its slots
    busy, else None (a free slot, a runner down, or a label that cannot be read). mine: the run's waiting jobs."""
    want = attempt(lambda: set(json.loads(label)), None, catch=(ValueError, TypeError))
    serving = serving_runners(q, want) if want is not None else {}
    if want is None or not serving or not all(r.get("state") == "running" for r in serving.values()):
        return None
    busy, cap = slot_usage(q, serving)
    if busy < cap:
        return None
    ahead, oldest = waiting_ahead(waits, mine, label)
    return f"{', '.join(sorted(want))} full ({busy}/{cap} busy, {ahead} ahead, oldest {fmt_age(oldest)})"


def waiting_jobs(q: dict[str, Any] | None, o_r: Any, number: Any) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """(every waiting job of queue snapshot q, those of run #number of o_r)."""
    waits = (q or {}).get("waiting_jobs") or []
    return waits, [w for w in waits if w.get("repo") == o_r and str(w.get("run")) == str(number)] if number is not None else []


def queue_full(o_r: Any, number: Any, q: dict[str, Any] | None) -> str | None:
    """'queued: <label> full (N/N busy, M ahead, oldest Xm)' when run #number of o_r waits only on labels whose every
    serving runner is up with all its slots busy, else None (a free slot, a runner down, or nothing known: stuck)."""
    waits, mine = waiting_jobs(q, o_r, number)
    if not mine:
        return None
    says = [label_full_says(q, waits, mine, label) for label in sorted({str(w.get("label")) for w in mine})]
    return None if None in says else "queued: " + "; ".join(says)


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
    runs = gh_json(o_r, "run", "list", "--branch", "main", "--limit", "200", "--json", RUN_FIELDS)
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
