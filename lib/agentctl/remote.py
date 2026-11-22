# agentctl, part `remote`: remote checks, the token hand-off, watches.
# Runs inside agentctl's one namespace, after the parts before it in lib/agentctl/__init__.py's PARTS (it uses their names).

# ---------------------------------------------------------------- remote checks: ci-runner's check tier
# The owner (2026-10-27, then decision 1): a queued change's pre-commit checks run on ci-runner's check tier (first only
# under the laptop's pressure; since the laptop's tick went, always: the reconciler is ci-runner's). The checked tip is pushed to `check/<entry id>` (the hook checks nothing
# for a check/ branch: it is a run's ref, never landed), whose push runs .github/workflows/precommit-check.yml: the same
# hook, `--checks-only <merge-base with main>..HEAD`, on the check tier's runner. agentctl follows that run (owned: the run
# is cancelled if the checker dies), reads the verdict -- the same (ok, output) shape as a local check, the failed jobs'
# relevant lines as output -- and deletes the branch. The push to main then carries PREPUSH_REMOTE_RUN=<run id>: the hook
# takes that run's pass for its checks only after GitHub confirms it (the workflow, success, the pushed commit itself).
REMOTE_WORKFLOW = "precommit-check.yml"
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


# THE VERDICT IS THE CHECK JOBS' (the owner, 2026-11-07, "do the structural fix"): batch #20's 13 changes sat 27+ min
# past their run's last check job, behind a `timings / report` job QUEUED for a light runner of a saturated ci-runner,
# because the verdict waited for the RUN to complete. The workflow declares its verdict's jobs itself -- a workflow-level
# `VERDICT_JOBS: <glob>, <glob>, ...` of job names (fnmatch), read at the very commit the run checks -- and the verdict
# is in once every job it names has concluded: a job outside it (a report, a publish) still queued or running holds
# nothing. Any job that concluded in failure fails the check at once, named or not. A workflow declaring none is decided
# by the run's own conclusion, as before. The hook's remote pass (movies' scripts/hooks/pre-push) reads the same line.
VERDICT_JOBS_LINE = re.compile(r"^[ \t]+VERDICT_JOBS:[ \t]*['\"]?([^'\"\n#]*)", re.M)
JOB_FAILED = ("failure", "timed_out", "startup_failure", "action_required")


EXPRESSION = re.compile(r"\$\{\{.*?\}\}")


def job_name_glob(template):
    """The fnmatch pattern of the listed names a job name template stands for (`${{ ... }}` matches anything)."""
    parts = EXPRESSION.split(template)
    return "*".join(re.sub(r"([*?\[])", r"[\1]", p) for p in parts)


def verdict_jobs(o_r, sha):
    """(the verdict's job-name globs, the name templates of the workflow's jobs they name), as REMOTE_WORKFLOW declares
    them at commit `sha`; ([], []) when it declares none, or when no job of its `jobs:` block can be read (then the run's
    own conclusion decides: an expectation that cannot be read never lets a success in early)."""
    import base64
    r = api(o_r, "GET", f"contents/.github/workflows/{REMOTE_WORKFLOW}", {"ref": sha}) or {}
    text = base64.b64decode(r.get("content") or "").decode(errors="replace")
    m = VERDICT_JOBS_LINE.search(text)
    globs = [g.strip() for g in m.group(1).split(",") if g.strip()] if m else []
    expect = [t for t in workflow_job_names(text) if any(fnmatch.fnmatchcase(t, g) for g in globs)]
    return (globs, expect) if expect else ([], [])


def jobs_verdict(jobs, globs, expect=()):
    """(verdict, failed jobs, jobs still going outside the verdict) of a run from its jobs (GitHub's REST shape): verdict
    `failure` once any job concluded in failure, `success` once at least one job matches `globs`, every one that does
    has concluded otherwise and every template of `expect` (the workflow's verdict jobs) is listed, else None
    (undecided)."""
    failed = [j for j in jobs if j.get("status") == "completed" and j.get("conclusion") in JOB_FAILED]
    named = [j for j in jobs if any(fnmatch.fnmatchcase(j.get("name") or "", g) for g in globs)]
    going = [j for j in jobs if j.get("status") != "completed" and j not in named]
    if failed:
        return "failure", failed, going
    listed = [j.get("name") or "" for j in jobs]
    created = all(any(n == t or fnmatch.fnmatchcase(n, job_name_glob(t)) or fnmatch.fnmatchcase(n, job_name_glob(t) + " / *")
                      for n in listed) for t in expect)
    if named and created and all(j.get("status") == "completed"
                                 and j.get("conclusion") in ("success", "skipped", "neutral") for j in named):
        return "success", [], going
    return None, [], going


# RUN PROGRESS (2026-11-17, ref-srp-54ebae: every check passed by 14:44, `check (page)` waited 23 min for a saturated
# check runner and was 14 min into its work when the 2-h bound, counted from the 13:19 push, cancelled the run: two
# hours of passes thrown away and the change checked again from scratch behind the same queue). The poll marks
# `progress_at` whenever the run's jobs move (a job queued, started or ended; the run's own update time); past
# REMOTE_TIMEOUT a run is cancelled only once idle REMOTE_IDLE, and past REMOTE_TIMEOUT_MAX whatever it does.
# A RUNNING JOB INSIDE ITS OWN EXPECTED DURATION IS PROGRESS (2026-11-18: a healthy run whose only remaining job is long
# -- one job past 30 min into its work, the others done -- moved nothing in its job list and read as idle). A job's
# expected duration (remote_job_bounds, read once per run): its own `timeout-minutes` at the checked commit (GitHub ends
# it there; inside it, it is alive by definition), else REMOTE_P90_HEADROOM x its recorded p90 (the CI collector's
# source:ci job rows in victoria-logs-ci, REMOTE_P90_WINDOW back, when the store is reachable), else REMOTE_JOB_CAP.
REMOTE_JOB_CAP = 2 * 3600       # s a running job with no timeout-minutes and no recorded p90 is taken as alive
REMOTE_P90_HEADROOM = 1.5       # x a job's recorded p90 it is taken as alive (no timeout-minutes of its own)
REMOTE_P90_WINDOW = 30 * 86400  # s of the collector's job rows a p90 is read from
REMOTE_P90_MIN = 5              # runs of a job before its p90 counts


def remote_job_bounds(timeouts, p90s):
    """{job name template or name: s a running job is expected to take at most}: each template's timeout-minutes
    (workflow_job_timeouts), else REMOTE_P90_HEADROOM x a listed name's p90 (p90s); a job in neither has
    REMOTE_JOB_CAP (remote_job_expected)."""
    out = {t: s for t, s in (timeouts or {}).items() if s}
    bounded = [job_name_glob(t) for t in out]
    for name, p90 in (p90s or {}).items():
        if p90 and not any(fnmatch.fnmatchcase(name, g) for g in bounded):
            out[name] = float(p90) * REMOTE_P90_HEADROOM
    return out


def remote_job_expected(bounds, name):
    """The s job `name` is expected to run at most (remote_job_bounds), else REMOTE_JOB_CAP."""
    if name in (bounds or {}):
        return bounds[name]
    for t, s in (bounds or {}).items():
        if EXPRESSION.search(t) and fnmatch.fnmatchcase(name or "", job_name_glob(t)):
            return s
    return REMOTE_JOB_CAP


def remote_job_p90s(since):
    """{job name: p90 duration_s} of REMOTE_WORKFLOW's finished jobs (the collector's source:ci rows), {} when the store
    is not reachable from here (stats_url) or answers nothing."""
    url = stats_url()
    if not url:
        return {}
    try:
        rows = stats_query(url, f'source:ci workflow_file:="{REMOTE_WORKFLOW}" kind:job conclusion:="success" '
                                f'| stats by (job) quantile(0.9, duration_s) p90, count() n', start=since)
    except Exception:  # the store down: the timeouts and the cap still bound
        return {}
    return {r["job"]: float(r["p90"]) for r in rows
            if r.get("job") and _num(r.get("p90")) and int(_num(r.get("n")) or 0) >= REMOTE_P90_MIN}


def remote_read_bounds(o_r, sha):
    """remote_job_bounds of REMOTE_WORKFLOW at commit sha and the recorded p90s; {} when neither can be read."""
    import base64
    try:
        r = api(o_r, "GET", f"contents/.github/workflows/{REMOTE_WORKFLOW}", {"ref": sha}) or {}
        timeouts = workflow_job_timeouts(base64.b64decode(r.get("content") or "").decode(errors="replace"))
    except Exception:
        timeouts = {}
    return remote_job_bounds(timeouts, remote_job_p90s(time.time() - REMOTE_P90_WINDOW))


def remote_progress(rem, run, jobs, t, bounds=None):
    """rem with progress_at moved to t when the run's jobs (or its update time) differ from the last poll's, or a job
    still runs inside its own expected duration (remote_job_expected over bounds)."""
    sig = [run.get("updated_at") or run.get("updatedAt")] + sorted(f"{j.get('name')}:{j.get('status')}" for j in jobs or [])
    if sig != rem.get("progress_sig"):
        return {**rem, "progress_sig": sig, "progress_at": t}
    for j in jobs or []:
        if j.get("status") != "in_progress" or not j.get("started_at"):
            continue
        try:
            started = parse_time(j["started_at"]).timestamp()
        except (ValueError, TypeError):
            continue
        if t - started < remote_job_expected(bounds, j.get("name")):
            return {**rem, "progress_at": t}
    return rem


# A RUN NO SCHEDULER CAN SEE IS NO CHECK (2026-11-20, phase7-houses: precommit-check run 44682642375 sat `queued` from
# 05:41Z, its check jobs never started, while later runs' jobs ran: GitHub's own run listing -- `actions/runs?status=
# queued` and `?status=in_progress` -- left it out, and ci-runner's runner scheduler, which reads exactly those listings,
# never started a runner with its pin label. agentctl waited for REMOTE_TIMEOUT (2 h) on it, the change's deadline
# laps meanwhile). A run whose check jobs have none started and which neither listing names for REMOTE_GHOST is
# cancelled and given up (no verdict: re-checked, keeping its place), never waited on for hours.
REMOTE_GHOST = 10 * 60  # s ($AGENTCTL_REMOTE_GHOST)
REMOTE_LISTED_PAGES = 5  # pages of 100 of each status read before a ghost check says it cannot tell


def remote_run_listed(o_r, rid):
    """GitHub's queued or in-progress run listing names run rid (what the runner scheduler reads); True when it cannot
    be read (unsure: never a ghost)."""
    try:
        for status in ("queued", "in_progress"):
            for page in range(1, REMOTE_LISTED_PAGES + 1):
                # listing: paged to its end; past REMOTE_LISTED_PAGES it cannot tell, which is never a ghost
                runs = (api(o_r, "GET", "actions/runs", {"status": status, "per_page": 100, "page": page}) or {}) \
                    .get("workflow_runs") or []
                if any(str(r.get("id")) == str(rid) for r in runs):
                    return True
                if len(runs) < 100:
                    break
            else:
                return True  # more than the pages read: absent from what was read proves nothing
        return False
    except (Exception, SystemExit):
        return True


def remote_ghost(o_r, rem, jobs, t):
    """rem with `unlisted_at` kept while its run waits with no check job started and GitHub's listings leave it out (a
    GHOST, above); status `abandoned` (+ `ghost`) past REMOTE_GHOST of that."""
    started = [j for j in jobs or [] if j.get("name") != "plan" and (j.get("status") != "queued")
               and j.get("conclusion") != "skipped"]
    if started or not jobs or remote_run_listed(o_r, rem["run"]):
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


def remote_poll(o_r, rem):
    """STATELESS: the remote check's state from the forge alone -- the run (found by its check/ branch and tip when its id
    is not known yet), its status, and for a decided run the verdict: decided by its verdict jobs while it still runs
    (VERDICT_JOBS above), else by its conclusion. Returns the updated record (a copy); raises when the forge cannot be
    read."""
    rem = dict(rem)
    if rem.get("run") is None:
        # listing: none -- the run agentctl's own push started is found by its workflow, branch and tip (A LISTING IS
        # NEVER PROOF OF ABSENCE, forge.py); the newest 50 of a busy repo left it out
        hit = forge_find_run(o_r, REMOTE_WORKFLOW, rem["branch"], rem["tip"])
        if not hit:  # a forge whose runs API ignores the filters: the newest 50, as before, as a floor
            # listing: the floor only; not found in it is "not started yet", bounded by REMOTE_START, never "none"
            hit = next((x for x in forge_actions_runs(o_r, workflow=REMOTE_WORKFLOW, limit=50)
                        if x.get("head_sha") == rem["tip"]
                        and x.get("head_branch") in (rem["branch"], f"refs/heads/{rem['branch']}")), None)
        if not hit:
            return rem
        rem["run"] = hit["id"]
    run = forge_actions_run(o_r, rem["run"])
    status = run.get("status")
    rem["run_status"] = status
    jobs = []
    if status in ("queued", "running", "waiting", "in_progress"):  # the jobs: the progress mark, and the verdict below
        jobs = (api(o_r, "GET", f"actions/runs/{rem['run']}/jobs", {"per_page": 100}) or {}).get("jobs") or []
        if rem.get("job_bounds") is None:
            rem["job_bounds"] = remote_read_bounds(o_r, rem["tip"])
        rem = remote_progress(rem, run, jobs, time.time(), rem["job_bounds"])
        rem["jobs"] = remote_jobs_summary(jobs)
    name = f"{REMOTE_WORKFLOW} run {rem['run']} (#{run.get('run_number')})"
    if rem.get("status"):
        return rem
    if status == "waiting":  # queued: a ghost no runner will ever take (above)?
        rem = remote_ghost(o_r, rem, jobs, time.time())
        if rem.get("status"):
            return rem
    if status in ("running", "waiting"):
        if rem.get("verdict_jobs") is None or rem.get("verdict_expect") is None:
            rem["verdict_jobs"], rem["verdict_expect"] = verdict_jobs(o_r, rem["tip"])
        if not rem["verdict_jobs"]:
            return rem
        verdict, failed, going = jobs_verdict(jobs, rem["verdict_jobs"], rem["verdict_expect"])
        if verdict is None:
            return rem
        rem["status"], rem["decided_at"] = verdict, time.time()
        rest = f"; not waited for: {', '.join(j.get('name', '?') for j in going)}" if going else ""
        if verdict == "success":
            rem["out"] = f"{name}: success (its verdict jobs: {', '.join(rem['verdict_jobs'])}{rest})"
        else:
            out = [f"{name} failed: {run.get('html_url')}"]
            for j in failed:
                out += [f"-- {j.get('name', '?')}"] + job_log_lines(o_r, rem["run"], {**j, "databaseId": j.get("id")}, 25)
            rem["out"] = "\n".join(out)
        return rem
    if status in ("success", "failure", "cancelled", "skipped"):
        rem["status"], rem["decided_at"] = status, time.time()
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
    return rem


def save_remote(d, e, rem):
    """The remote check's record in the entry, saved at once (a replacement checker adopts it); `remote_run` mirrors the
    run for the push's hook and `waiting status`."""
    e["remote"] = rem
    if rem.get("run") is not None:
        e["remote_run"] = rem["run"]
    else:
        e.pop("remote_run", None)
    if d and e.get("seq") is not None:
        save_entry(d, e)
    remote_mirror(e.get("id"), rem)


# WHAT A CHECKING CHANGE WAITS ON (2026-11-20: `land status <id>` said only "OPEN: checking for 25 min" for five changes
# whose checks ran, queued or were lost on ci-runner): the remote check's run, branch, jobs and progress go on the change
# item too, which every host's `land status` reads.
def remote_mirror(eid, rem):
    """rem's summary on change item eid (`check`), when it changed; quiet when the store cannot take it."""
    if not eid:
        return
    keep = ("branch", "run", "run_status", "status", "pushed_at", "progress_at", "unlisted_at", "jobs", "decided_at")
    summary = {k: rem.get(k) for k in keep if rem.get(k) is not None}
    try:
        st = open_store()
        it = st.item("change", eid)
        if it and (it.get("data") or {}).get("check") != summary:
            st.update_item("change", eid, it["version"], {"check": summary})
    except Exception:
        pass  # the next poll mirrors it


def remote_check_lines(c, t):
    """`land status <id>`'s lines for a checking change's remote check summary c (remote_mirror)."""
    if not c:
        return []
    out = [f"check: {REMOTE_WORKFLOW} " + (f"run {c['run']}" if c.get("run") else "run not started yet")
           + f" on {c.get('branch')}, pushed {fmt_age(t - float(c.get('pushed_at') or t))} ago"
           + (f", {c['run_status']}" if c.get("run_status") else "")
           + (f", last progress {fmt_age(t - float(c['progress_at']))} ago" if c.get("progress_at") else "")
           + (f"; not in GitHub's run listing for {fmt_age(t - float(c['unlisted_at']))} (no runner can take it)"
              if c.get("unlisted_at") else "")]
    for j in c.get("jobs") or []:
        if j.get("conclusion") == "skipped":
            continue
        try:
            since = fmt_age(t - parse_time(j["started_at"]).timestamp()) if j.get("started_at") else None
        except (ValueError, TypeError):
            since = None
        what = j.get("conclusion") if j.get("status") == "completed" else (
            f"running {since}" if j.get("status") == "in_progress" and since else "waiting for a runner slot"
            if j.get("status") == "queued" else j.get("status"))
        out.append(f"  {j.get('name')}: {what}")
    return out


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


def check_branch_push(repo, head, branch, sleep=time.sleep):
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


def remote_check(repo, e, base, head, mode, own=None, red=False):
    """(ok, output) of the change's checks on ci-runner; ok None when no verdict came (the caller checks locally).
    The run belongs to the ENTRY, not to this process: its record (`remote` in the entry) is saved the moment the branch is
    pushed and again when the run is found, so a checker that dies or is replaced mid-wait strands nothing -- the next one
    ADOPTS the run (no new push, no new run) and the processor re-derives its state from GitHub every cycle. Its time
    bounds count from the push, not from this process's start."""
    o_r = slug(repo)
    try:
        d = waiting_state(repo)[0]
    except (Exception, SystemExit):
        d = None
    rem = remote_matches(e, base, head, mode)
    if rem and d:
        wlog(d, f"{e['id']}: adopting its remote check (run {rem.get('run') or 'not found yet'}, pushed "
                f"{int((time.time() - rem['pushed_at']) // 60)} min ago)")
    if not rem:
        # the branch NAMES the hook's mode for precommit-check.yml: check/fast-<id> runs `--fast-only` (the batch's
        # combined check, as on the laptop), check/verify-<full base sha>-<id> `--verify-only <full base>..HEAD` (a
        # change's combined tree, queue.py's THE RULE), any other check/<id> `--checks-only`. A gate-red fix's (`red`)
        # starts check/red-: its jobs ask for movies-check-red-fix, the check lane ci-runner's scheduler serves first
        # (movies runner_scheduler.py CHECK_LANES; 2026-11-17, a red fix still checking 41 min after it was queued)
        branch = f"check/{'red-' if red else ''}{remote_mode_prefix(e, base, mode)}{e['id']}"
        r = check_branch_push(repo, head, branch)
        if r.returncode != 0:
            return None, check_push_failure(branch, r.stderr, host_role())
        rem = {"branch": branch, "head": e.get("head"), "base": base, "tip": head, "mode": mode, "run": None,
               "pushed_at": time.time(), "status": None}
        save_remote(d, e, rem)
        try:
            record_dispatch(branch=branch, workflow=REMOTE_WORKFLOW, worktree=e.get("worktree"), sha=head,
                            agent="land queue", pid=os.getpid(), purpose=f"pre-commit check of queue entry {e['id']}")
        except (Exception, SystemExit) as ex:
            err(f"remote check of {e['id']}: owner not recorded ({str(ex)[:120]})")
    delays = backoff(15, REMOTE_POLL_CAP)
    while True:
        try:
            new = remote_poll(o_r, rem)
            if new.get("run") is not None and rem.get("run") is None:
                try:
                    save_dispatch({"branch": rem["branch"], "run_id": new["run"]})
                except OSError:
                    pass
            if new != rem:
                rem = new
                save_remote(d, e, rem)
        except Terminated:
            raise
        except (Exception, SystemExit) as ex:  # GitHub down for a while: keep reading until the bound
            err(f"remote check of {e['id']}: {str(ex)[:160]}")
        try:  # the CI run is the check's work: its status (and, while it runs, the minute) is the progress mark
            lock_beat(f"remote:{rem.get('run')}:{rem.get('status') or rem.get('run_status') or 'pending'}:"
                      f"{int(time.time() // 60)}")
        except LeaseLost as ex:
            err(f"remote check of {e['id']}: {ex}; a new checker adopts its run")
            return None, str(ex)
        if rem.get("status"):
            if rem.get("ghost"):
                cancel_run(o_r, rem["run"], "a ghost run: no runner scheduler can see it")
            rem["collected_at"] = time.time()
            save_remote(d, e, rem)
            delete_check_branch(o_r, rem["branch"])
            if rem["status"] in ("success", "failure"):
                return rem["status"] == "success", rem["out"]
            return None, rem["out"]
        age = time.time() - rem["pushed_at"]
        if rem.get("run") is None and age > REMOTE_START:
            rem.update(status="abandoned", collected_at=time.time())
            save_remote(d, e, rem)
            delete_check_branch(o_r, rem["branch"])
            return None, f"no {REMOTE_WORKFLOW} run started for {rem['branch']} in {REMOTE_START // 60} min"
        idle = time.time() - float(rem.get("progress_at") or rem["pushed_at"])
        if age > REMOTE_TIMEOUT_MAX or (age > REMOTE_TIMEOUT and idle > REMOTE_IDLE):
            cancel_run(o_r, rem["run"], "the remote check timed out")
            rem.update(status="abandoned", collected_at=time.time())
            save_remote(d, e, rem)
            delete_check_branch(o_r, rem["branch"])
            return None, (f"{REMOTE_WORKFLOW} run {rem['run']} undecided after {int(age // 60)} min, "
                          f"no progress for {int(idle // 60)} min")
        time.sleep(next(delays))


def processor_owned(d, repo=None):
    """The processor's ownership: one per waiting state directory (one per repository)."""
    return Owned("waiting", os.path.basename(d.rstrip("/")) or "repo", ["waiting", "run", "--repo", repo or ""])


def waiting_processor_alive(d):
    return lock_held(processor_owned(d).lockpath)


def spawn_waiting_processor(repo):
    d, clone, _w = waiting_state(repo)
    if waiting_processor_alive(d):
        return
    repo = clone if os.path.isdir(clone) else repo  # the clone, not a joiner's worktree: that one closes after WATCH OK
    with open(os.path.join(d, "processor.log"), "a") as logf:
        spawn_detached([sys.executable, TOOL_PATH, "waiting", "run", "--repo", repo],
                         stdin=subprocess.DEVNULL, stdout=logf, stderr=logf, start_new_session=True, cwd=repo,
                         env=detached_env(repo))


class TokenDrop:
    """The processor's end of the hand-off."""

    def __init__(self, d, o_r):
        self.path, self.o_r, self.sock, self.thread = token_sock_path(d), o_r, None, None

    def open(self):
        import socket
        try:
            os.makedirs(os.path.dirname(self.path), mode=0o700, exist_ok=True)
            os.chmod(os.path.dirname(self.path), 0o700)
            if os.path.exists(self.path):
                os.unlink(self.path)
            s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            old = os.umask(0o177)
            try:
                s.bind(self.path)
            finally:
                os.umask(old)
            s.listen(4)
            self.sock = s
            import threading  # served on a thread of its own: no step of the main loop can leave a client waiting
            self.thread = threading.Thread(target=self.serve, name="token-socket", daemon=True)
            self.thread.start()
        except OSError as e:
            err(f"no token hand-off socket ({e}): the processor reads the vault itself")
        return self

    def held(self):
        return os.environ.get(token_env_name(self.o_r), "")

    def serve(self):
        import select
        while self.sock:
            try:
                ready, _, _ = select.select([self.sock], [], [], 1)
                if ready and self.sock:
                    self.take()
            except (OSError, ValueError):
                return

    def nap(self, secs):
        """Sleep secs (the socket is served by its own thread)."""
        end = time.time() + secs
        while True:
            left = end - time.time()
            if left <= 0:
                return
            time.sleep(min(left, 10))

    def take(self):
        try:
            conn, _ = self.sock.accept()
        except OSError:
            return
        with conn:
            conn.settimeout(5)
            try:
                tok = conn.recv(4096).decode().strip()
                if tok == TOKEN_REQUEST.decode().strip():  # a same-uid process (the socket is 0600) asks for the token
                    conn.sendall(self.held().encode())
                    return
            except (OSError, UnicodeDecodeError):
                return
            key = token_env_name(self.o_r)
            if TOKEN_SHAPE.fullmatch(tok) and os.environ.get(key) != tok:  # said once per real change, never per offer
                os.environ[key] = tok
                err("the store token was handed over: the gate is read again")
                token_back(token_ns(self.o_r))

    def close(self):
        """Stop serving, and wait for a hand-off already taken to finish: once close returns, no token lands in this
        process's environment (2026-11-16: a test's drop wrote $AGENTCTL_STORE_TOKEN after its tearDown had restored
        the environment, and test_forge, next in the same worker, saw that token)."""
        if self.sock:
            sock, self.sock = self.sock, None
            sock.close()
            try:
                os.unlink(self.path)
            except OSError:
                pass
        import threading  # the part's own import, as in open()
        thread, self.thread = self.thread, None
        if thread is not None and thread is not threading.current_thread():
            thread.join(5)


def offer_token(repo):
    """Hand this process's store token to the repo's waiting processor when it listens (see THE TOKEN HAND-OFF) AND
    lacks it: asked first (one socket round trip), so an interactive command hands over only on a real transition
    (2026-10-30: every `land` / `waiting status` handed it over, 175 "handed over" lines in 400). Never fatal; True when
    handed over."""
    try:
        d, _c, _w = waiting_state(repo)
        path = token_sock_path(d)
        if not os.path.exists(path):
            return False
        return bool(hand_token(path, token(slug(repo))))
    except (Exception, SystemExit):
        return False


def await_entry(repo, eid, close=None):
    """Block until the entry lands or is rejected: (rc, landed sha, pushed_at). Restarts a dead processor. OWNED (one
    awaiter per entry; on agentctl's new code it re-execs as `agentctl waiting await <entry>`, which then watches)."""
    own = Owned("await", eid, ["waiting", "await", eid, "--repo", repo] + (["--close", close] if close else []))
    if not own.acquire():
        h = own.holder or {}
        print(f"NOT LANDED (still waiting): entry {eid} is already awaited by pid {h.get('pid')} ({h.get('owner')})")
        return 2, None, None
    try:
        return _await_entry(repo, eid, own)
    finally:
        own.release()


def _await_entry(repo, eid, own):
    """The change's STORE ITEM decides (a RemoteStore on the laptop): an entry's JSON is only on the host whose queue
    runs it -- ci-runner's for every change since ONE QUEUE (2026-11-16: `waiting await` on the laptop crashed with
    FileNotFoundError on one). A joined change's item follows its waiting entry (project_changes)."""
    t0, delays = time.time(), backoff(15, 60)
    timeout = env_num("AGENTCTL_WAITING_TIMEOUT", WAITING_AWAIT_TIMEOUT)
    state, said = "not in the store yet", None
    while True:
        try:
            it = open_store().item("change", eid)
            if not it:
                state = "not in the store yet"
        except StoreUnavailable as ex:  # a blip: asked again at the next poll, bounded by the timeout below
            it, state = None, f"unknown (the store did not answer: {str(ex)[:80]})"
        if it:
            state, data = it["state"], it.get("data") or {}
            if state == "landed":
                sha = data.get("landed_sha") or data.get("tip") or data.get("head")
                print(f"LANDED {sha} (with the {data.get('batch', 1)} change(s) of `{WAITING_BRANCH}`, once main was "
                      "green)", flush=True)
                return 0, sha, data.get("pushed_at") or it.get("entered_at")
            if state == "superseded":  # its newer entry lands the change: exit 0, as its event says (supersede)
                why = str(data.get("why") or "")
                print(why if why.startswith("SUPERSEDED") else
                      f"SUPERSEDED by {data.get('superseded_by') or 'another entry'}: {why or 'superseded'}", flush=True)
                return 0, None, None
            if machine_state("change", state).terminal:  # rejected, cancelled: never hangs a follow
                why = str(data.get("why") or state)
                print(why if why.startswith("NOT LANDED") else f"NOT LANDED: {why}", flush=True)
                return 1, None, None
        if state != said:  # what it waits on, said once each time it changes (a store outage is never silent)
            err(f"awaiting {eid}: it is {state}")
            said = state
        if time.time() - t0 > timeout:
            print(f"NOT LANDED (still waiting): entry {eid} is {state} after {int(timeout // 3600)} h; "
                  "`agentctl land status`")
            return 2, None, None
        spawn_waiting_processor(repo)
        restart_if_changed([own])
        time.sleep(next(delays))


def assemble(repo, d, wt, entries, base, alone=False, autoresolve=False):
    """The batch's tip in check tree wt = base + each entry's commits, in order; a conflicting entry is rejected alone
    (alone: a backlog batch's queued change, left out and checked alone -- its own check says whether it applies),
    one already on main LANDED, a duplicate superseded. autoresolve (a backlog batch: its check is the full landing
    check): a source conflict of additions on both sides is resolved (waiting.py A SOURCE CONFLICT IS RESOLVED IN THE
    BATCH); one that is not leaves the member out, held behind the change it conflicts with (`resolve`: strict).
    Returns the kept entries (each with its tip in the batch)."""
    git(wt, "reset", "--hard", "-q", base)
    git(wt, "checkout", "-q", "--force", "--detach", base)
    kept = []
    for e in entries:
        how, x = apply_entry(d, wt, e, base, autoresolve=autoresolve)
        if how == "conflict" and alone:  # one whose generated files could not be resolved: held as before (strict)
            e["batch_alone"] = "regen" if any(" (generated: " in f for f in x) else "resolve" if autoresolve \
                else "conflict"
            save_entry(d, e)
            wlog(d, f"{e['id']}: does not apply in the backlog batch (conflicts in: {', '.join(x)}): checked alone")
            continue
        if how == "conflict":
            # policy: no-longer-applies
            queue_verdict(d, e, "rejected", f"NOT LANDED: main moved while it waited and the change no longer applies "
                                            f"({conflict_cause(wt, e.get('base'), base, x).lstrip('; ')}): rebase onto "
                                            f"{remote()}/main and land again (it keeps its place in the queue)",
                          conflict_files=conflict_paths(x))
            continue
        if how == "landed":  # all of it reached main meanwhile: LANDED, and nothing of it is pushed again
            already_landed(d, e, x)
            continue
        if how == "duplicate":  # an earlier entry carries the very same change: that one lands it
            owner = next((k for k in kept if x and git(wt, "merge-base", "--is-ancestor", x, k["tip"],
                                                        check=False).returncode == 0), None)
            supersede(d, e, owner["id"] if owner else "an earlier entry",
                      "the same change already waits" + (f" as {owner['id']}" if owner else ""))
            continue
        e["tip"] = git(wt, "rev-parse", "HEAD").strip()  # its tip IN THE BATCH: bisect and the push read it from disk
        save_entry(d, e)
        kept.append(e)
    return kept


def sbt_stop(tree):
    """A FRESH sbt server for the batch check: stop the tree's server and keeper (scripts/sbt-warm --stop), if any."""
    script = os.path.join(tree, "scripts", "sbt-warm")
    if os.access(script, os.X_OK):
        try:
            subprocess.run([script, "--stop"], cwd=tree, capture_output=True, text=True, timeout=180, env=tool_env())
        except subprocess.TimeoutExpired:
            err(f"sbt-warm --stop in {tree} took over 3 min")


STALL_SIGNAL = re.compile(r"(^|\s)(pre-push |sbt-warm )?STALLED:", re.M)


def batch_mode(hook):
    """The combined check's hook mode: `--fast-only` (compile of every test config, the fast tests, the touched files'
    lints and the checks a touched merge=union file selects: each change was checked in full alone, on an older main,
    and the rebase writes a union-merged file no member's check saw) when the hook has it, else the full `--range` dry run."""
    try:
        with open(hook) as f:
            return "--fast-only" if "--fast-only" in f.read() else "--range"
    except OSError:
        return "--range"


RED_PRECHECK_MODE = "--checks-only"  # a waiting batch's pre-check while main is red: every check, no push
BATCH_FULL_MODE = "--range"  # a backlog batch's check: the full landing check a change gets (check/batch-<tip> on CI)


def batch_check(repo, d, tree, base, tip, cls="check", depth="fast", runs=None, range_base=None):
    """The hook's dry run over base..tip in `tree` (depth fast: batch_mode's; full, a backlog batch: the full landing
    check, BATCH_FULL_MODE; verify, a push batch: the combined tree's VERIFY_MODE over range_base..tip, range_base the
    oldest member's full base), on a fresh sbt server, under the stall abort. (ok, output, stalled): a stall -- land's
    20 min, or the hook's exit 75 / `STALLED:` -- is retried once on a fresh server; stalled twice, `stalled` is True.
    runs: {tip: remote run id}, filled with this check's run (the push hands it to the hook)."""
    hook = precheck_hook(tree) or precheck_hook(repo)
    if not hook:
        return True, "", False
    # `red`: a waiting batch pre-checked while main is red, the checks but never the push (batch.py THE BATCH IS THE
    # MECHANISM)
    mode = {"full": BATCH_FULL_MODE, "verify": VERIFY_MODE, "red": RED_PRECHECK_MODE}.get(depth) or batch_mode(hook)
    rng_base = (range_base or base) if depth == "verify" else base
    if host_role() == "server" and remote_checks_supported(tree):  # ci-runner: the CI check tier (owner decision 1)
        # (a push batch's branch: check/verify-<range base>-batch-<tip>, the workflow's --verify-only <range base>..HEAD)
        e = {"id": f"batch-{tip[:10]}", "head": tip, **({"full_base": rng_base} if depth == "verify" else {})}
        # a gate red's fix in it (cls red): check/red-<...>, the lane ci-runner serves first, as a lone fix's check
        ok, out = remote_check(repo, e, base, tip, mode, red=cls == "red")
        if runs is not None and e.get("remote_run"):
            runs[tip] = e["remote_run"]
        return (ok, out, False) if ok is not None else (False, out, True)
    out = ""
    for attempt in (1, 2):
        git(tree, "checkout", "-q", "--force", "--detach", tip)
        sbt_stop(tree)
        wlog(d, f"checking {base[:10]}..{tip[:10]} (attempt {attempt}, fresh sbt server)")
        with check_slot(repo, f"waiting batch {tip[:10]}", cls) as slot:
            rc, out = run_watched([hook, mode, f"{rng_base}..{tip}"], tree, slot.env(tool_env()),
                                  os.path.join(d, f"check-{tip[:10]}"), f"batch-check-{attempt}", sampler=slot.sample)
            slot.done(rc, out)
        sbt_stop(tree)
        with open(os.path.join(d, "checks.log"), "a") as f:
            f.write(f"=== {ts()} {base[:10]}..{tip[:10]} attempt {attempt} rc {rc}\n{out}\n")
        if rc is None or rc == 75 or STALL_SIGNAL.search(out or ""):
            wlog(d, f"the check of {tip[:10]} STALLED (attempt {attempt})")
            continue
        return rc == 0, out, False
    return False, out, True


# WATCHES FROM ITEMS (design §5.1 `landed`, phase 1 step 9): a landed sha is a `watch` item; each tick evaluates it ONCE
# (no detached process, no loop): its Main run and convergence run (pick_main_run / pick_convergence_run), WATCH OK
# -> ok (the worktree closed when proven merged), WATCH RED -> red (its author told; the red machine attaches it), still
# undecided at its 3 h deadline -> unknown, an escalation (a stuck run). The verdicts reach the author's inbox as
# events. `land --follow` watches in-process (the agent waits on purpose) by CLAIMING the sha's item (WatchClaim): the
# tick leaves a live claim alone, and the follower's verdict goes on the item, so one sha is watched once.
WATCH_EVERY = 60


def start_watch(repo, sha, pushed_at, worktree=None, close=None, session=None, o_r=None, follower=None):
    """The landed sha's watch item: created once, a second start leaves it as it is (the sha's ONE watch, whichever
    host or follower starts it). follower: {followed_by, followed_until}, a `land --follow` that watches it itself
    (claim_watch)."""
    o_r = o_r or slug(repo)
    if not WATCH.get(o_r):
        return None
    return open_store().put_item("watch", sha, "watching", {"repo": main_clone(repo), "o_r": o_r, "pushed_at": pushed_at or time.time(),
                                                       "worktree": worktree or repo, "close": close, "session": session,
                                                       "next_at": 0, **(follower or {})})


def watch_clone(dd):
    """THIS host's clone of a watch item's repo. The item names its repo by o_r, its GitHub slug -- the same on every
    host -- and `repo`, the clone path of the host that made it, is used only where it is that repo here (review round
    7: a laptop follower's item, taken over by ci-runner's tick, named /Users/... there: every look failed, and the
    watch ended UNKNOWN at its deadline)."""
    o_r, p = dd["o_r"], dd.get("repo")
    if p and os.path.isdir(p):
        try:
            if slug(p) == o_r:
                return p
        except (Exception, SystemExit):
            pass
    c = red_repo_clone(o_r)
    if c:
        return c
    raise RuntimeError(f"no clone of {o_r} on {os.uname().nodename} (red-repos.json `clone`, {movies_repo_path()})")


def watch_event(it, verdict, severity="info", code=0):
    dd = it["data"]
    return {"text": verdict.splitlines()[0], "severity": severity,
            "audience": {"worktree": dd.get("worktree"), "session": dd.get("session")},
            "refs": {"verdict": verdict.splitlines()[0], "tail": verdict.splitlines()[1:25], "exit": code,
                     "command": "watch", "sha": it["id"], "close": dd.get("close")}}


def tick_watches(now_=None):
    """One evaluation of each due watch item. Returns what it decided."""
    t = now_ if now_ is not None else time.time()
    st = open_store()
    did = []
    for it in st.items("watch", states=("watching",)):
        dd = it["data"]
        if float(dd.get("followed_until") or 0) > t:  # a live `land --follow` watches it and writes its verdict here
            continue
        try:
            did += tick_watch(st, it, t)
        except (StaleVersion, TerminalState):  # its follower claimed or decided it meanwhile: it is that one's
            pass
    return did


def tick_watch(st, it, t):
    """One evaluation of a due watch item: what it decided (StaleVersion / TerminalState when a follower took it)."""
    dd = it["data"]
    if t > it["deadline"]:
        st.transition("watch", it["id"], "unknown", it["version"],
                      event=watch_event(it, f"WATCH UNKNOWN: Main or the convergence still undecided for {it['id'][:10]} "
                                            f"after {int(WATCH_TIMEOUT // 3600)} h: a stuck run (`agentctl ci-why`)",
                                        "escalate", 2))
        return [f"watch {it['id'][:10]}: unknown"]
    if float(dd.get("next_at") or 0) > t:
        return []
    flows = WATCH.get(dd["o_r"]) or {}
    try:
        verdict = watch_eval(watch_clone(dd), it["id"], dd["pushed_at"], dd["o_r"], flows, t)
    except (Exception, SystemExit) as e:  # unreadable now: tried again at the next look, the deadline bounds it
        err(f"watch {it['id'][:10]}: could not read the runs ({str(e)[:120]})")
        verdict = None
    if verdict is None:
        st.update_item("watch", it["id"], it["version"], {"next_at": t + env_num("AGENTCTL_WATCH_POLL", WATCH_EVERY)})
        return []
    finish_watch(st, it, *verdict)
    return [f"watch {it['id'][:10]}: {verdict[0]}"]


def watch_outcome(it):
    """A watch item's verdict: its state, or -- an `unknown` item a follower re-watched (WatchClaim) -- that
    follower's `outcome`, kept on the item (a terminal state is never left)."""
    return (it.get("data") or {}).get("outcome") or it["state"]


def finish_watch(st, it, state, line, red_runs, told=False):
    """The watch item's verdict (ok / red / unknown), kept on it for a later follower (`verdict`); its event goes to
    its author's inbox unless told (a follower printed it to the author already); on ok its worktree closed, on red
    the red machine takes each red run, its author offered first. An `unknown` item a follower re-watched keeps the
    re-watch's verdict as its `outcome`, the claim given up with it."""
    dd = it["data"]
    if it["state"] == "unknown":
        st.update_item("watch", it["id"], it["version"], {"verdict": line, "outcome": state, "followed_by": None,
                                                          "followed_until": 0})
    else:
        ev = watch_event(it, line, code={"ok": 0, "red": 3}.get(state, 2))
        if told:
            ev["audience"] = {}
        st.transition("watch", it["id"], state, it["version"], data={"verdict": line}, event=ev)
    if state == "ok" and not told and dd.get("close") and os.path.isdir(dd["close"]):
        err(close_worktree(dd["close"]))
    for wf, n in red_runs:
        try:
            red_attach(st, wf, n, author={"session": dd.get("session"), "worktree": dd.get("worktree")})
        except (Exception, SystemExit) as e:
            err(f"watch {it['id'][:10]}: could not attach {wf} #{n} ({str(e)[:120]})")


def watch_look(repo, sha, o_r, flows, done, pushed_at, since, now_, on_run=None):
    """ONE look at the runs that contain sha (the tick's watch item and `land --follow`'s own watch alike): each
    undecided flow's run is picked; a completed (not cancelled) one goes into `done` as (run, green?), a convergence
    that has not started CONV_START s after `since` as (None, None). on_run(kind, run or None) hears each pick.
    Returns {kind: (run id, status, updatedAt)} of the runs followed (the stall judge's state)."""
    conv_start = env_num("AGENTCTL_WATCH_CONV_START", WATCH_CONV_START)
    seen = {}
    for kind, (name, wfile) in flows.items():
        if kind in done:
            continue
        runs = main_runs(o_r, wfile)
        run = pick_main_run(repo, sha, runs) if kind == "main" else pick_convergence_run(repo, sha, runs, pushed_at, o_r)
        if run is None:
            if kind != "main" and now_ - float(since) >= conv_start:
                done[kind] = (None, None)
                if on_run:
                    on_run(kind, None)
            continue
        seen[kind] = (run.get("databaseId"), run.get("status"), run.get("updatedAt"))
        if run.get("status") == "completed" and not is_cancelled(run):
            done[kind] = (run, run.get("conclusion") == "success")
        if on_run:
            on_run(kind, run)
    return seen


def watch_verdict(o_r, sha, flows, done):
    """('red', lines, [(workflow, number)]) when a decided run is red, ('ok', line, []) when every flow is decided
    green (or the convergence never started), else None."""
    reds = [(k, r) for k, (r, ok) in done.items() if ok is False]
    if reds:
        lines = []
        for kind, r in reds:
            try:
                failed = failed_jobs(o_r, r["databaseId"])
            except (Exception, SystemExit) as e:  # the verdict stands without the job names
                err(f"could not read the jobs of run {r['databaseId']} ({str(e)[:120]})")
                failed = []
            lines.append(f"WATCH RED: {flows[kind][0]} #{r.get('number')} {', '.join(failed) or r.get('conclusion')} "
                         f"(it contains your {sha[:10]}: if your change broke it, fix forward now with `Fixes-Red: "
                         f"{r.get('number')}`)")
        return "red", "\n".join(lines), [(flows[k][0], r.get("number")) for k, r in reds]
    if len(done) == len(flows):
        parts = [f"{flows[k][0].split()[0] if k == 'main' else k} " +
                 (f"#{r.get('number')} green" if r else "none started") for k, (r, ok) in done.items()]
        return "ok", "WATCH OK: " + ", ".join(sorted(parts, key=lambda x: not x.startswith("Main"))), []
    return None


def watch_eval(repo, sha, pushed_at, o_r, flows, now_):
    """The tick's look at a watch item: ('ok', line, []), ('red', lines, reds) or None (undecided)."""
    done = {}
    watch_look(repo, sha, o_r, flows, done, pushed_at, pushed_at, now_)
    v = watch_verdict(o_r, sha, flows, done)
    return (v[0], v[1] + f" ({sha[:10]})", v[2]) if v and v[0] == "ok" else v


def bisect(repo, d, batch, base, bwt, depth="fast", runs=None, failed_out=None, range_base=None, cls="check"):
    """The batch failed: the first entry whose prefix fails (binary search over the prefixes' tips, in the batch's tree
    bwt). (index, output) of the culprit; (None, output) when the whole batch passes on its own again (flaky or infra).
    A backlog or push batch (failed_out: its check's output) is not checked whole again first: each check is a landing
    check's 30-40 min or a verify round's ~10, and the culprit's confirmation below still clears a flaky failure."""
    lo, hi, out_hi = 0, len(batch) - 1, ""
    check = lambda tip: batch_check(repo, d, bwt, base, tip, cls=cls, depth=depth, runs=runs, range_base=range_base)
    if depth in ("full", "verify") and failed_out is not None:
        out_hi = failed_out
    else:
        ok, out, stalled = check(batch[hi]["tip"])
        if ok or stalled:
            return None, out
        out_hi = out
    while lo < hi:
        mid = (lo + hi) // 2
        ok, out, stalled = check(batch[mid]["tip"])
        if stalled:
            return None, out
        if ok:
            lo = mid + 1
        else:
            hi, out_hi = mid, out
    # one flaky failure rejects nobody (audit #10): the culprit's prefix is checked once more before it is rejected
    ok, out, stalled = check(batch[lo]["tip"])
    if ok or stalled:
        wlog(d, f"bisect: the prefix through {batch[lo]['id']} passed when checked again: flaky, nobody rejected")
        return None, out
    return lo, out_hi


def unread_note(d, reds, entries):
    """The gate unread while changes wait: once per unread spell, an UNKNOWN inbox entry (they would wait unseen)."""
    if GATE_UNREAD not in reds or condition_item_of(open_store(), "gate-unread-waiting"):
        return
    wlog(d, f"the gate is unread ({gate_unread_why[0]}): taken as red")
    open_condition("UNKNOWN", f"UNKNOWN: the waiting processor cannot read the gate ({gate_unread_why[0]}); {len(entries)} "
                              "change(s) wait, taken as red. Hand it the token: `~/bin/agentctl land status` from a "
                              "session with the vault unlocked",
                   run_probe("gate_read", {"since": time.time()}), [], key="gate-unread-waiting", command="waiting",
                   exit_code=2, cwd=entries[0].get("worktree") if entries else None)
