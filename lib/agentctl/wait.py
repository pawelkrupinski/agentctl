# agentctl, part `wait`: wait.
# Runs inside agentctl's one namespace, after the parts before it in lib/agentctl/__init__.py's PARTS (it uses their names).

# ---------------------------------------------------------------- wait
ANSI = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")
LOG_TS = re.compile(r"^\d{4}-\d\d-\d\dT[\d:.]+Z ")
STRONG = re.compile(r"\*\*\* FAILED|\[error\]|::error::|(?i:assertion|assert(ion)? failed)|\bError:|ENOENT|artifact already exists|(?i:exit code [1-9])|\bFAILED\b")
WEAK = re.compile(r"\bfail(ed|ure)?\b|\berror\b|exception|assert", re.I)
NOISE = re.compile(r"Setting level of logger|\|-INFO in ch\.qos|\berrors?[=:] ?0\b|\b0 (errors?|failed)\b|\b(INFO|DEBUG|TRACE)\b", re.I)
MEASURE_WORKFLOWS = {"Identity model convergence"}


def relevant_lines(log_text, tail, strong_jobs=None):
    """{job: [last `tail` relevant lines]} from `forgejo-gh run view --log-failed` output (job<TAB>step<TAB>line).
    Strong markers (*** FAILED, [error], ::error::, assertion, exit code) win; plain fail/error words only when a job
    has none, and never from INFO/DEBUG log lines. strong_jobs, a set: the jobs with a STRONG line are added to it."""
    strong, weak, follow = {}, {}, {}
    for raw in log_text.splitlines():
        parts = raw.split("\t", 2)
        job, line = (parts[0], parts[2]) if len(parts) == 3 else ("?", raw)
        line = LOG_TS.sub("", ANSI.sub("", line)).rstrip()
        if not line:
            continue
        if follow.get(job):  # the 2 lines after a scalatest `*** FAILED ***` carry its reason
            follow[job] -= 1
            strong.setdefault(job, []).append(line[:300])
            continue
        if "*** FAILED ***" in line:
            follow[job] = 2
        if STRONG.search(line) and not re.search(r"Setting level of logger|-INFO in ch\.qos", line):
            strong.setdefault(job, []).append(line[:300])
        elif WEAK.search(line) and not NOISE.search(line):
            weak.setdefault(job, []).append(line[:300])
    if strong_jobs is not None:
        strong_jobs.update(strong)
    return {j: (strong.get(j) or weak.get(j))[-tail:] for j in set(strong) | set(weak)}


def raw_tails(log_text, n=3):
    """{job: (line count, last n cleaned lines)} of a --log-failed output."""
    out = {}
    for raw in log_text.splitlines():
        parts = raw.split("\t", 2)
        job, line = (parts[0], parts[2]) if len(parts) == 3 else ("?", raw)
        line = LOG_TS.sub("", ANSI.sub("", line)).rstrip()
        cnt, last = out.get(job, (0, []))
        out[job] = (cnt + 1, (last + [line[:200]])[-n:])
    return out


def read_log(o_r, *args):
    """A log the forge's CLI serves (forgejo-gh or gh), read twice at most. A read it failed (the ci-runner actions API resetting or refusing
    the connection under load) raises with its stderr: never an empty log, which ci-why read as a 0-length one, INFRA
    (convergence #2473, run 3656: the takes-drift rows' flip failures handed to a diagnose-only fixer)."""
    env = forge_env(o_r)
    for attempt in (1, 2):
        r = subprocess.run(forge_argv(o_r, "run", "view", *args), capture_output=True, text=True, env=env, timeout=300)
        if r.returncode == 0:
            return r.stdout
    raise RuntimeError((r.stderr.strip().splitlines() or [f"exit {r.returncode}"])[-1][:200])


def fetch_failed_log(o_r, run_id):
    return read_log(o_r, str(run_id), "--log-failed")


def fetch_takes_verdicts(o_r, run_id):
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
        subprocess.run(forge_argv(o_r, "run", "download", str(run_id), "-n", name, "-D", d), capture_output=True, env=env,
                       timeout=300)
        p = os.path.join(d, "verdict.txt")
        out[name] = open(p, errors="replace").read().strip() if os.path.exists(p) else "(no verdict.txt in the artifact)"
    return out


def is_measure_run(d):
    return d.get("workflowName") in MEASURE_WORKFLOWS and (d.get("headBranch") or "").startswith("measure/")


def fetch_job_log(o_r, run_id, job_id):
    return read_log(o_r, str(run_id), "--log", "--job", str(job_id))


ANNOTATION = re.compile(r"::error\b[^:]*::")
MAX_JOB_LOG_FETCHES = 5


def job_log_lines(o_r, run_id, job, tail, unread=None, annotated=None):
    """Relevant lines from a job's WHOLE log. Forgejo's per-step slices (what --log-failed prints) are shifted by a
    few lines from the stored log, so a failed step's slice can hold only its neighbours' checkout/cache lines while
    its `::error` annotation and output sit just past it (run 1988, takes (united-kingdom)). `::error` annotations
    first, else the strong/weak markers. `unread`, a list: a failed read's error is appended to it. `annotated`, a
    list: True is appended when the lines are the job's annotations (or its step timeout), False otherwise."""
    jid = job.get("databaseId") if isinstance(job, dict) else None
    if not jid:
        return []
    try:
        log = fetch_job_log(o_r, run_id, jid)
    except Exception as e:
        if unread is not None:
            unread.append(str(e))
        return []
    clean = [LOG_TS.sub("", ANSI.sub("", (l.split("\t", 2)[2] if l.count("\t") >= 2 else l))).rstrip() for l in log.splitlines()]
    notes = [l[:300] for l in clean if ANNOTATION.search(l)]
    timed = [l[:300] for l in clean if STEP_TIMED_OUT in l]  # past the failed step's slice, in the reap step's
    if annotated is not None:
        annotated.append(bool(notes or timed))
    if notes or timed:
        return (timed[-1:] + [n for n in notes if n not in timed])[:tail]
    name = job.get("name", "?")
    return relevant_lines("\n".join(f"{name}\t-\t{l}" for l in clean), tail).get(name, [])


def choose_job_lines(slice_lines, slice_strong, whole, whole_annotated):
    """A failed job's lines -- its red's KEY is the first. A STRONG line in the failed step's slice wins; else the job's
    own `::error` annotations (read from its whole log); else the slice's WEAK lines; else the whole log's. One failure,
    three reds (Verify scrape recording, GermanyConvergenceSpec serving Bambi '(1989)' as a B-Movie): the slice's weak
    lines came first, so #2406 was keyed by a `WARN FallbackHttpFetch ... All 2 backends failed` line, #2481 by an
    `enrichment-fixtures: FileNotFoundException`, and only #2493 (an empty slice) by its `::error
    title=GermanyConvergenceSpec...` annotation: the waiting `Fixes-Red: 2406` fix never reached #2493's red."""
    if slice_strong:
        return slice_lines
    if whole_annotated and whole:
        return whole
    return slice_lines or whole


HOOK_BLOCK = re.compile(r"blocked by:|pre-push\S* .*FAILED|\u2717|\u2716|^\s*(FAIL\b|\u2500\u2500 FAIL)|\bexpected:|\bactual:")


def hook_lines(o_r, run_id, job, tail):
    """A precommit-check run's own verdict: the hook's `blocked by:` line and the failing checks' lines (its `FAIL`
    headers, `✗` rows, expected/actual), not the last lines of a passing subtest further down the log."""
    jid = job.get("databaseId") if isinstance(job, dict) else None
    if not jid:
        return []
    try:
        log = fetch_job_log(o_r, run_id, jid)
    except Exception:
        return []
    clean = [LOG_TS.sub("", ANSI.sub("", (l.split("\t", 2)[2] if l.count("\t") >= 2 else l))).rstrip() for l in log.splitlines()]
    if not any("blocked by:" in l or re.search(r"pre-push\S* .*FAILED", l) for l in clean):
        return []
    hits = [l[:300] for l in clean if HOOK_BLOCK.search(l)]
    return hits[:tail * 4] if hits else []


def failure_detail(o_r, run_id, failed, tail):
    """failed: job dicts (name, databaseId) or names."""
    if not failed or tail <= 0:
        return []
    try:
        per = relevant_lines(fetch_failed_log(o_r, run_id), tail)
    except Exception as e:
        return [f"  (could not read the failed log: {e})"]
    out, fetched = [], 0
    for jd in failed:
        job = jd["name"] if isinstance(jd, dict) else jd
        lines = per.get(job) or next((v for k, v in per.items() if k in job or job in k), [])
        if not lines and fetched < MAX_JOB_LOG_FETCHES and isinstance(jd, dict):
            fetched += 1
            lines = job_log_lines(o_r, run_id, jd, tail)
        out.append(f"  -- {job}: " + ("no FAILED/error/assert line in its log (0-length log or setup failure? see `agentctl ci-why`)" if not lines else f"last {len(lines)} relevant line(s)"))
        out += [f"     {l}" for l in lines]
    return out


def run_verdict(o_r, run_id, until="done", tail=0):
    """(done, ok, text) for a Forgejo run. until=landable: a measure run is done once its takes rows ended."""
    d = gh_json(o_r, "run", "view", str(run_id), "--json", "status,conclusion,jobs,workflowName,headBranch")
    jobs = d.get("jobs") or []
    head = f"run {o_r}#{run_id}"
    if until == "landable" and is_measure_run(d):
        takes = [j for j in jobs if j.get("name", "").startswith("takes")]
        if d.get("status") == "completed" or (takes and all(j.get("status") == "completed" for j in takes)):
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
        n_done = sum(1 for j in takes if j.get("status") == "completed")
        return False, None, f"{head}: {d.get('status')} (takes rows {n_done}/{len(takes)} ended)"
    if d.get("status") != "completed":
        return False, None, f"{head}: {d.get('status')}"
    concl = d.get("conclusion") or "unknown"
    failedj = [j for j in jobs if j.get("conclusion") in ("failure", "cancelled", "timed_out")]
    failed = [j["name"] for j in failedj]
    line = f"{head}: {concl}" + (f"; failed: {', '.join(failed)}" if failed else "")
    lines = [line] + failure_detail(o_r, run_id, failedj, tail)
    return True, concl in ("success", "skipped", "neutral"), "\n".join(lines)


def file_verdict(path, contains):
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


def foreign_loops_on(run_id):
    """Other (non-agentctl) processes already polling this run id."""
    me = os.getpid()
    out = []
    for pid, ppid, etime, cmd in ps():
        if pid in (me, os.getppid()) or "agentctl" in cmd:
            continue
        if re.search(rf"run view {run_id}\b|runs/{run_id}\b", cmd) and "sleep" in cmd:
            out.append(pid)
    return out
