# agentctl, part `remote`: the remote check: a change's check run on CI from a check branch, polled to its verdict
# A real module (lib/agentctl/__init__.py MODULES): its own imports, the standard library's and MODULES' only.

import os
import re
from typing import Any
from agentctl.core import env_num, err, fmt_age, git, host_role, remote, slug
from agentctl.timefmt import parse_time
from agentctl.classify import check_push_failure, credential_failure
from agentctl.store import LeaseLost
from agentctl.forge import api, forge_actions_run, forge_actions_runs, forge_delete_branch, forge_find_run
from agentctl.waitingdir import save_entry, waiting_state, wlog
from agentctl.procs import Terminated
from agentctl.leases import lock_beat
from agentctl.wait import fetch_failed_log, job_log_lines, relevant_lines
from agentctl.owned import backoff, cancel_run
from agentctl.dispatches import record_dispatch, save_dispatch
from agentctl.queue import VERIFY_AREAS_MODE, VERIFY_MODE
from agentctl.remotejobs import REMOTE_WORKFLOW, jobs_verdict, remote_progress, remote_read_bounds, verdict_jobs


REMOTE_TIMEOUT = 2 * 3600  # s past its push a run may be cancelled -- only when idle REMOTE_IDLE (RUN PROGRESS, below)
REMOTE_IDLE = 30 * 60  # s with no job of the run moving after which a run past REMOTE_TIMEOUT is cancelled
REMOTE_TIMEOUT_MAX = 4 * 3600  # s past its push a run is cancelled however its jobs move
REMOTE_START = 15 * 60  # no run for the pushed branch by then: no verdict (checked locally instead)


def remote_checks_supported(tree):
    return os.path.exists(os.path.join(tree, ".github", "workflows", REMOTE_WORKFLOW))


def remote_matches(e, base, head, mode):
    """The entry's recorded remote check, when it is the check of exactly this tip on this base in this mode."""
    rem = e.get("remote") or {}
    if rem.get("tip") == head and rem.get("base") == base and rem.get("mode") == mode and rem.get("head") == e.get("head") \
            and rem.get("status") not in ("cancelled", "skipped", "abandoned"):
        return rem
    return None


# A RUN NO SCHEDULER CAN SEE IS NO CHECK (2026-11-20, phase7-houses: precommit-check run 44682642375 sat `queued` from
# 05:41Z, its check jobs never started, while later runs' jobs ran: GitHub's own run listing -- `actions/runs?status=
# queued` and `?status=in_progress` -- left it out, and ci-runner's runner scheduler, which reads exactly those listings,
# never started a runner with its pin label. agentctl waited for REMOTE_TIMEOUT (2 h) on it, the change's deadline
# laps meanwhile). A run whose check jobs have none started and which neither listing names for REMOTE_GHOST is
# cancelled and given up (no verdict: re-checked, keeping its place), never waited on for hours.
REMOTE_GHOST = 10 * 60  # s ($AGENTCTL_REMOTE_GHOST)
REMOTE_LISTED_PAGES = 5  # pages of 100 of each status read before a ghost check says it cannot tell


def _status_lists(o_r: Any, status: Any, rid: Any) -> Any:
    """True: GitHub's listing of runs in this status names rid; False: it does not (paged to its end); None: more pages
    than REMOTE_LISTED_PAGES, so absent from what was read proves nothing (never a ghost)."""
    for page in range(1, REMOTE_LISTED_PAGES + 1):
        runs = (api(o_r, "GET", "actions/runs", {"status": status, "per_page": 100, "page": page}) or {}) \
            .get("workflow_runs") or []
        if any(str(r.get("id")) == str(rid) for r in runs):
            return True
        if len(runs) < 100:
            return False
    return None


def remote_run_listed(o_r, rid):
    """GitHub's queued or in-progress run listing names run rid (what the runner scheduler reads); True when it cannot
    be read (unsure: never a ghost)."""
    try:
        return any(_status_lists(o_r, status, rid) is not False for status in ("queued", "in_progress"))
    except (Exception, SystemExit):
        return True


def _check_job_started(jobs: Any) -> Any:
    """Some job but the plan's has left the queue (and was not skipped)."""
    return any(j.get("name") != "plan" and j.get("status") != "queued" and j.get("conclusion") != "skipped"
               for j in jobs or [])


def remote_ghost(o_r, rem, jobs, t):
    """rem with `unlisted_at` kept while its run waits with no check job started and GitHub's listings leave it out (a
    GHOST, above); status `abandoned` (+ `ghost`) past REMOTE_GHOST of that."""
    if _check_job_started(jobs) or not jobs or remote_run_listed(o_r, rem["run"]):
        return {k: v for k, v in rem.items() if k != "unlisted_at"}
    rem = {**rem, "unlisted_at": rem.get("unlisted_at") or t}
    if t - float(rem["unlisted_at"]) >= env_num("AGENTCTL_REMOTE_GHOST", REMOTE_GHOST):
        rem.update(status="abandoned", ghost=True, decided_at=t,
                   out=f"{REMOTE_WORKFLOW} run {rem['run']} queued {int((t - float(rem['unlisted_at'])) // 60)} min with "
                       f"no check job started, and GitHub's queued/in-progress listing omits it (ci-runner's runner "
                       f"scheduler cannot see it): cancelled, checked again")
    return rem


def remote_jobs_summary(jobs):
    """The run's jobs as `land status <id>` prints them: name, status, conclusion, started."""
    return [{"name": j.get("name"), "status": j.get("status"), "conclusion": j.get("conclusion"),
             "started_at": j.get("started_at")} for j in jobs or []]


def remote_poll(o_r, rem, now):
    """STATELESS: the remote check's state from the forge alone -- the run (found by its check/ branch and tip when its id
    is not known yet), its status, and for a decided run the verdict: decided by its verdict jobs while it still runs
    (VERDICT_JOBS above), else by its conclusion. Returns the updated record (a copy), its stamps at `now`; raises when the
    forge cannot be read."""
    rem = dict(rem)
    if rem.get("run") is None:
        hit = remote_run_found(o_r, rem)
        if not hit:
            return rem
        rem["run"] = hit["id"]
    run = forge_actions_run(o_r, rem["run"])
    status = run.get("status")
    rem["run_status"] = status
    rem, jobs = remote_progressed(o_r, rem, status, run, now)
    name = f"{REMOTE_WORKFLOW} run {rem['run']} (#{run.get('run_number')})"
    if rem.get("status"):
        return rem
    if status == "waiting":  # queued: a ghost no runner will ever take (above)?
        rem = remote_ghost(o_r, rem, jobs, now)
        if rem.get("status"):
            return rem
    if status in ("running", "waiting"):
        return remote_decided_by_jobs(o_r, rem, run, name, jobs, now)
    if status in ("success", "failure", "cancelled", "skipped"):
        remote_decided_by_run(o_r, rem, run, name, status, now)
    return rem


def remote_progressed(o_r: str, rem: Any, status: Any, run: Any, now: float) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """(the record with the progress of a run still going, its jobs): the jobs are the progress mark and the verdict."""
    jobs = []
    if status in ("queued", "running", "waiting", "in_progress"):
        jobs = (api(o_r, "GET", f"actions/runs/{rem['run']}/jobs", {"per_page": 100}) or {}).get("jobs") or []
        if rem.get("job_bounds") is None:
            rem["job_bounds"] = remote_read_bounds(o_r, rem["tip"])
        rem = remote_progress(rem, run, jobs, now, rem["job_bounds"])
        rem["jobs"] = remote_jobs_summary(jobs)
    return rem, jobs


def remote_run_found(o_r: str, rem: Any) -> Any:
    """The run agentctl's own push started, found by its workflow, branch and tip, or None."""
    # listing: none -- the run agentctl's own push started is found by its workflow, branch and tip (A LISTING IS
    # NEVER PROOF OF ABSENCE, forge.py); the newest 50 of a busy repo left it out
    hit = forge_find_run(o_r, REMOTE_WORKFLOW, rem["branch"], rem["tip"])
    if hit:
        return hit
    # a forge whose runs API ignores the filters: the newest 50, as before, as a floor
    # listing: the floor only; not found in it is "not started yet", bounded by REMOTE_START, never "none"
    return next((x for x in forge_actions_runs(o_r, workflow=REMOTE_WORKFLOW, limit=50)
                 if x.get("head_sha") == rem["tip"]
                 and x.get("head_branch") in (rem["branch"], f"refs/heads/{rem['branch']}")), None)


def remote_decided_by_jobs(o_r: str, rem: Any, run: Any, name: str, jobs: Any, now: float) -> Any:
    """The record with the verdict its verdict jobs give while the run still runs (and what it did not wait for), or as
    it was when they have not decided."""
    if rem.get("verdict_jobs") is None or rem.get("verdict_expect") is None:
        rem["verdict_jobs"], rem["verdict_expect"] = verdict_jobs(o_r, rem["tip"])
    if not rem["verdict_jobs"]:
        return rem
    verdict, failed, going = jobs_verdict(jobs, rem["verdict_jobs"], rem["verdict_expect"])
    if verdict is None:
        return rem
    rem["status"], rem["decided_at"] = verdict, now
    rest = f"; not waited for: {', '.join(j.get('name', '?') for j in going)}" if going else ""
    if verdict == "success":
        rem["out"] = f"{name}: success (its verdict jobs: {', '.join(rem['verdict_jobs'])}{rest})"
    else:
        out = [f"{name} failed: {run.get('html_url')}"]
        for j in failed:
            out += [f"-- {j.get('name', '?')}"] + job_log_lines(o_r, rem["run"], {**j, "databaseId": j.get("id")}, 25)
        rem["out"] = "\n".join(out)
    return rem


def remote_decided_by_run(o_r: str, rem: Any, run: Any, name: str, status: Any, now: float) -> None:
    """Record the verdict of a finished run (its conclusion) on rem, with a failed run's relevant log lines."""
    rem["status"], rem["decided_at"] = status, now
    if status == "success":
        rem["out"] = f"{name}: success"
    elif status == "failure":
        out = [f"{name} failed: {run.get('html_url')}"]
        try:
            for job, ls in relevant_lines(fetch_failed_log(o_r, rem["run"]), 25).items():
                out += [f"-- {job}"] + ls
        except Exception as ex:
            out.append(f"(its failed log could not be read: {str(ex)[:120]})")
        rem["out"] = "\n".join(out)
    else:
        rem["out"] = f"{REMOTE_WORKFLOW} run {rem['run']} {status}"


def save_remote(d, e, rem, st):
    """The remote check's record in the entry, saved at once (a replacement checker adopts it); `remote_run` mirrors the
    run for the push's hook and `waiting status`."""
    e["remote"] = rem
    if rem.get("run") is not None:
        e["remote_run"] = rem["run"]
    else:
        e.pop("remote_run", None)
    if d and e.get("seq") is not None:
        save_entry(d, e)
    remote_mirror(e.get("id"), rem, st)


# WHAT A CHECKING CHANGE WAITS ON (2026-11-20: `land status <id>` said only "OPEN: checking for 25 min" for five changes
# whose checks ran, queued or were lost on ci-runner): the remote check's run, branch, jobs and progress go on the change
# item too, which every host's `land status` reads.
def remote_mirror(eid, rem, st):
    """rem's summary on change item eid (`check`), when it changed; quiet when the store cannot take it."""
    if not eid:
        return
    keep = ("branch", "run", "run_status", "status", "pushed_at", "progress_at", "unlisted_at", "jobs", "decided_at")
    summary = {k: rem.get(k) for k in keep if rem.get(k) is not None}
    try:
        it = st.item("change", eid)
        if it and (it.get("data") or {}).get("check") != summary:
            st.update_item("change", eid, it["version"], {"check": summary})
    except Exception:
        pass  # the next poll mirrors it


def remote_check_lines(c, t):
    """`land status <id>`'s lines for a checking change's remote check summary c (remote_mirror)."""
    if not c:
        return []
    return [remote_check_head(c, t)] + [remote_job_line(j, t) for j in c.get("jobs") or [] if j.get("conclusion") != "skipped"]


def remote_check_head(c: Any, t: Any) -> Any:
    return (f"check: {REMOTE_WORKFLOW} " + (f"run {c['run']}" if c.get("run") else "run not started yet")
            + f" on {c.get('branch')}, pushed {fmt_age(t - float(c.get('pushed_at') or t))} ago"
            + (f", {c['run_status']}" if c.get("run_status") else "")
            + (f", last progress {fmt_age(t - float(c['progress_at']))} ago" if c.get("progress_at") else "")
            + (f"; not in GitHub's run listing for {fmt_age(t - float(c['unlisted_at']))} (no runner can take it)"
               if c.get("unlisted_at") else ""))


def remote_job_line(j: Any, t: Any) -> Any:
    try:
        since = fmt_age(t - parse_time(j["started_at"]).timestamp()) if j.get("started_at") else None
    except (ValueError, TypeError):
        since = None
    if j.get("status") == "completed":
        what = j.get("conclusion")
    elif j.get("status") == "in_progress" and since:
        what = f"running {since}"
    else:
        what = "waiting for a runner slot" if j.get("status") == "queued" else j.get("status")
    return f"  {j.get('name')}: {what}"


def delete_check_branch(o_r, branch):
    try:
        forge_delete_branch(o_r, branch)
    except Exception as ex:
        err(f"could not delete {branch} ({str(ex)[:120]}): `agentctl sweep` lists it")


def remote_mode_prefix(e, base, mode):
    """The check branch's mode prefix (remote_check): `fast-`, `verify-<full base sha>-` (`verifyareas-...`: with the
    area tests) or none."""
    if mode == "--fast-only":
        return "fast-"
    if mode == VERIFY_MODE:  # MODULE OVERLAP (queue.py): the area tests too, on a share script naming the mode
        return f"{VERIFY_AREAS_MODE if e.get('verify_areas') else 'verify'}-{e.get('full_base') or base}-"
    return ""


# THE CHECK BRANCH'S PUSH RIDES OUT A BLIP (2026-11-17: deploy-speed and ref-scrapers were re-queued "its checks
# stalled (on ci-runner (no verdict))" 13 s into a round -- GitHub's activity log shows no check/ branch created then,
# so the push itself failed -- and each lost 20 min to the infra backoff and the check-slot queue behind it): a failed
# push is tried again CHECK_PUSH_DELAYS apart before the round gives up, and what git said reaches the re-queue.
CHECK_PUSH_DELAYS = (5, 20)


def check_branch_push(repo, head, branch, sleep):
    """git push of head to the check branch, tried again through a transient failure; the last result."""
    for delay in (*CHECK_PUSH_DELAYS, None):
        r = git(repo, "push", "--quiet", "--force", remote(), f"{head}:refs/heads/{branch}", check=False)
        if r.returncode == 0 or delay is None or credential_failure(r.stderr):  # no wait brings a credential back
            return r
        err(f"push of {branch} failed ({(r.stderr or '').strip()[:160]}): again in {delay} s")
        sleep(delay)


# A DECIDED RUN IS SEEN WITHIN REMOTE_POLL_CAP (2026-11-21: in 11-19..21 a round's verdict came a mean 1.4 min, p90 3 min,
# after its run's last job ended -- the poll had backed off to every 120 s; ~1 min a round, at ~2 rounds a landing).
# Every read is a conditional GET (forge.py): a poll that finds nothing changed is a 304, free of the App's quota.
REMOTE_POLL_CAP = 30  # s


def remote_check(repo, e, base, head, mode, clock, st, own=None, red=False):
    """(ok, output) of the change's checks on ci-runner; ok None when no verdict came (the caller checks locally).
    The run belongs to the ENTRY, not to this process: its record (`remote` in the entry) is saved the moment the branch is
    pushed and again when the run is found, so a checker that dies or is replaced mid-wait strands nothing -- the next one
    ADOPTS the run (no new push, no new run) and the processor re-derives its state from GitHub every cycle. Its time
    bounds count from the push, not from this process's start."""
    o_r = slug(repo)
    d = entry_dir(repo)
    rem, failed = remote_check_started(repo, e, base, head, mode, clock, st, d, red)
    if failed:
        return failed
    delays = backoff(15, REMOTE_POLL_CAP)
    while True:
        rem = remote_check_polled(o_r, e, rem, clock, st, d)
        lost = remote_check_beat(e, rem, clock)
        if lost:
            return lost
        verdict = remote_check_decided(o_r, e, rem, clock, st, d)
        if verdict:
            return verdict
        clock.sleep(next(delays))


def entry_dir(repo: str) -> str | None:
    """The waiting directory of repo's queue, or None when it cannot be found."""
    try:
        return waiting_state(repo)[0]
    except (Exception, SystemExit):
        return None


def remote_check_started(repo: str, e: Any, base: str, head: str | None, mode: Any, clock: Any, st: Any, d: str | None, red: bool) -> tuple[Any, Any]:
    """(the remote record, None): the one the entry's check already has (adopted) or the check branch just pushed; (None,
    (None, why)) when the push failed (no verdict: the caller checks locally)."""
    rem = remote_matches(e, base, head, mode)
    if rem and d:
        wlog(d, f"{e['id']}: adopting its remote check (run {rem.get('run') or 'not found yet'}, pushed "
                f"{int((clock.time() - rem['pushed_at']) // 60)} min ago)")
    return (rem, None) if rem else remote_check_pushed(repo, e, base, head, mode, clock, st, d, red)


def remote_check_pushed(repo: str, e: Any, base: str, head: str | None, mode: Any, clock: Any, st: Any, d: str | None, red: bool) -> tuple[Any, Any]:
    """(the new remote record, None) once the check branch of entry e is pushed and saved with its owner recorded; (None,
    (None, why)) when the push failed (no verdict: the caller checks locally)."""
    # the branch NAMES the hook's mode for precommit-check.yml: check/fast-<id> runs `--fast-only` (the batch's
    # combined check, as on the laptop), check/verify-<full base sha>-<id> `--verify-only <full base>..HEAD` (a
    # change's combined tree, queue.py's THE RULE), any other check/<id> `--checks-only`. A gate-red fix's (`red`)
    # starts check/red-: its jobs ask for movies-check-red-fix, the check lane ci-runner's scheduler serves first
    # (movies runner_scheduler.py CHECK_LANES; 2026-11-17, a red fix still checking 41 min after it was queued)
    branch = f"check/{'red-' if red else ''}{remote_mode_prefix(e, base, mode)}{e['id']}"
    r = check_branch_push(repo, head, branch, clock.sleep)
    if r.returncode != 0:
        return None, (None, check_push_failure(branch, r.stderr, host_role()))
    rem = {"branch": branch, "head": e.get("head"), "base": base, "tip": head, "mode": mode, "run": None,
           "pushed_at": clock.time(), "status": None}
    save_remote(d, e, rem, st)
    try:
        record_dispatch(branch=branch, workflow=REMOTE_WORKFLOW, worktree=e.get("worktree"), sha=head,
                        agent="land queue", pid=os.getpid(), purpose=f"pre-commit check of queue entry {e['id']}")
    except (Exception, SystemExit) as ex:
        err(f"remote check of {e['id']}: owner not recorded ({str(ex)[:120]})")
    return rem, None


def remote_check_beat(e: Any, rem: Any, clock: Any) -> tuple[None, str] | None:
    """None while this checker holds its lease (the CI run is the check's work: its status, and while it runs the minute,
    is the progress mark); else (None, why), a new checker adopts its run."""
    try:
        lock_beat(f"remote:{rem.get('run')}:{rem.get('status') or rem.get('run_status') or 'pending'}:"
                  f"{int(clock.time() // 60)}")
    except LeaseLost as ex:
        err(f"remote check of {e['id']}: {ex}; a new checker adopts its run")
        return None, str(ex)
    return None


def remote_check_polled(o_r: str, e: Any, rem: Any, clock: Any, st: Any, d: str | None) -> Any:
    """The remote record after one read of GitHub: a found run recorded as the dispatch's, a changed record saved."""
    try:
        new = remote_poll(o_r, rem, clock.time())
        if new.get("run") is not None and rem.get("run") is None:
            try:
                save_dispatch({"branch": rem["branch"], "run_id": new["run"]})
            except OSError:
                pass
        if new != rem:
            rem = new
            save_remote(d, e, rem, st)
    except Terminated:
        raise
    except (Exception, SystemExit) as ex:  # GitHub down for a while: keep reading until the bound
        err(f"remote check of {e['id']}: {str(ex)[:160]}")
    return rem


def remote_check_decided(o_r: str, e: Any, rem: Any, clock: Any, st: Any, d: str | None) -> tuple[bool | None, str] | None:
    """(ok, output) once the remote check is over -- collected, never started in REMOTE_START, or timed out -- the check
    branch deleted; None while it runs."""
    if rem.get("status"):
        if rem.get("ghost"):
            cancel_run(o_r, rem["run"], "a ghost run: no runner scheduler can see it")
        rem["collected_at"] = clock.time()
        save_remote(d, e, rem, st)
        delete_check_branch(o_r, rem["branch"])
        if rem["status"] in ("success", "failure"):
            return rem["status"] == "success", rem["out"]
        return None, rem["out"]
    why = remote_check_stuck(o_r, rem, clock)
    if why is None:
        return None
    rem.update(status="abandoned", collected_at=clock.time())
    save_remote(d, e, rem, st)
    delete_check_branch(o_r, rem["branch"])
    return None, why


def remote_check_stuck(o_r: str, rem: Any, clock: Any) -> str | None:
    """Why the remote check is given up -- no run started in REMOTE_START, or undecided past the timeouts (its run
    cancelled) -- or None while it may run on."""
    age = clock.time() - rem["pushed_at"]
    if rem.get("run") is None and age > REMOTE_START:
        return f"no {REMOTE_WORKFLOW} run started for {rem['branch']} in {REMOTE_START // 60} min"
    idle = clock.time() - float(rem.get("progress_at") or rem["pushed_at"])
    if age > REMOTE_TIMEOUT_MAX or (age > REMOTE_TIMEOUT and idle > REMOTE_IDLE):
        cancel_run(o_r, rem["run"], "the remote check timed out")
        return (f"{REMOTE_WORKFLOW} run {rem['run']} undecided after {int(age // 60)} min, "
                f"no progress for {int(idle // 60)} min")
    return None