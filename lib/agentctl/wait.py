# agentctl, part `wait`: wait.
# Runs inside agentctl's one namespace, after the parts before it in lib/agentctl/__init__.py's PARTS (it uses their names).

# ---------------------------------------------------------------- wait
# gh prints a log's escapes as caret text (`^[[36;1m`) when it refuses raw ones: both are colours
ANSI = re.compile(r"(?:\x1b|\^\[)\[[0-9;]*[A-Za-z]")
LOG_TS = re.compile(r"^\d{4}-\d\d-\d\dT[\d:.]+Z ")
MEASURE_WORKFLOWS = {"Identity takes", "Identity model convergence"}  # takes rows: Identity takes (2026-11-08); a --full measure starts it from convergence
# A STEP'S SCRIPT IS NO OUTPUT (2026-11-04): the runner echoes each `run:` step's script (cyan bold, `36;1m`) inside a
# `##[group]Run <first line>` ... `##[endgroup]` header, with its shell and env, before running it. precommit-check.yml's
# hook step holds `[ "$rc" != 124 ] || echo "::error::the pre-push checks ran past 80 minutes and were stopped"`, so
# that SOURCE line was a strong `::error::` line in every failed check's verdict -- often its only one, crowding out the
# hook's `! pre-push scripttests FAILED` -- and 69 NOT LANDED in 7 days read as "stopped at the 80-minute cap" while no
# hook step ran past 50 min (273 precommit-check runs). The header block, and any stray echoed line (a --log-failed
# slice can start inside it), is skipped by every reader of a failed log.
STEP_ECHO = re.compile(r"^(?:\S+Z )?(?:\x1b|\^\[)\[36;1m")
STEP_HEADER_MAX = 500  # an unterminated header (a truncated slice) stops hiding lines after this many


def log_entries(log_text):
    """(job, line) of a `gh run view --log` / `--log-failed` output (job<TAB>step<TAB>line, or bare lines: job `?`),
    timestamps and colours off, blank lines and the runner's step-script headers (above) left out."""
    header = {}
    for raw in (log_text or "").splitlines():
        parts = raw.split("\t", 2)
        job, line = (parts[0], parts[2]) if len(parts) == 3 else ("?", raw)
        echoed = bool(STEP_ECHO.match(line.lstrip("﻿")))
        line = LOG_TS.sub("", ANSI.sub("", line.lstrip("﻿"))).rstrip()
        if line.startswith("##[group]Run "):
            header[job] = 0
            continue
        if job in header:
            header[job] += 1
            if line.startswith("##[endgroup]") or header[job] > STEP_HEADER_MAX:
                header.pop(job)
            continue
        if echoed or not line:
            continue
        yield job, line


def relevant_lines(log_text, tail, strong_jobs=None):
    """{job: [last `tail` relevant lines]} from `gh run view --log-failed` output (job<TAB>step<TAB>line).
    Strong markers (*** FAILED, [error], ::error::, assertion, exit code) win; plain fail/error words only when a job
    has none, and never from INFO/DEBUG log lines. strong_jobs, a set: the jobs with a STRONG line are added to it."""
    strong, weak, follow = {}, {}, {}
    for job, line in log_entries(log_text):
        if not line:
            continue
        if follow.get(job):  # the 2 lines after a scalatest `*** FAILED ***` carry its reason
            follow[job] -= 1
            strong.setdefault(job, []).append(line[:300])
            continue
        if "*** FAILED ***" in line:
            follow[job] = 2
        if NOT_A_FAILURE.search(line):
            continue
        if STRONG.search(line) and not re.search(r"Setting level of logger|-INFO in ch\.qos", line):
            strong.setdefault(job, []).append(line[:300])
        elif WEAK.search(line) and not NOISE.search(line):
            weak.setdefault(job, []).append(line[:300])
    if strong_jobs is not None:
        strong_jobs.update(strong)
    return {j: (strong.get(j) or weak.get(j))[-tail:] for j in set(strong) | set(weak)}


# A DRIFT / FLIP ROW'S RED IS ITS LISTINGS (2026-11-01): the first error line of a takes-drift row ("0 divergence(s) ... and
# N flip(s) on the way ... seed S ... reproduce: IdentityDrift uk --seed S") is the same for any listing on any seed, so
# every new unrelated flip was the same red and "refuted" the fixes landed for the earlier ones (#624, a Ladykillers
# flip, marked #595's Dracula fix and #549's slice refuted). The step log names each listing as
# `<venue> | <raw title>: <takes...>` under `## <cc> flips: ...`; the red is keyed by those, not the seed or the count.
DRIFT_LINE = re.compile(r"\b(?:divergence|flip)\(s\)")
# IdentityDrift.scala prints `## <cc> flips: ...` and `## <cc> known flips: ...` (two summary lines), then the rows:
# `      FIXED, remove its row: ...` and `      KNOWN: <row>` (the baseline's: never the red), then each reason's
# `  N listing(s)  flip: <reason>` and its fresh rows `      <venue> | <title>: <film> [<take>] -> ... [seed S]`, <film>
# `none` or `tmdb:N` (#629: `Keswick Alhambra | Scottish Ballet: These Violent Delights: none [1 boot: ...`).
FLIP_HEADER = re.compile(r"^## ([a-z]{2,}(?:-[a-z]+)*)\b(.*)$")
# The differential gate (movies scripts/ci/drift_diff.py, 2026-11-04) prints rows that are never the red too: a flip the
# base shows as well (`pre-existing (backlog): ...`), one the head no longer has (`removed: ...`), a TEMPORARY-listed one
# (`allow-listed (...): ...`), and the nightly explorer's finds (`explored: ...`, scripts/ci/identity-drift-check.sh).
FLIP_ROW = re.compile(r"^\s{4,}(?!FIXED\b|KNOWN:|pre-existing\b|removed:|allow-listed\b|explored:)(\S.*? \| .+)$")
FLIP_TAKES = re.compile(r": (?=(?:none|[a-z]+:\S+) \[|[a-z]+:\S|\[)")
FLIP_KEY_MAX = 5  # listings named in a key; more are a stable hash of the whole set


def flip_listings(log):
    """The sorted set of `cc:venue|title` (lowercased, spaces collapsed) the report of a takes-drift job names: the rows
    under each `## <cc>` header (flips and divergences; never a `KNOWN:` baseline row or a `FIXED,` one)."""
    out, cc = set(), None
    for raw in log.splitlines():
        line = LOG_TS.sub("", ANSI.sub("", raw.split("\t", 2)[2] if raw.count("\t") >= 2 else raw)).rstrip()
        h = FLIP_HEADER.match(line)
        if h:
            cc = h.group(1)
            continue
        m = FLIP_ROW.match(line)
        if m and cc:
            body = re.sub(r"\s*\[seed \d+\]\s*$", "", m.group(1))
            venue, _, rest = body.partition(" | ")
            title = FLIP_TAKES.split(rest, 1)[0]
            out.add(f"{cc}:{' '.join(venue.split())}|{' '.join(title.split())}".lower())
    return sorted(out)


def flip_key_line(listings):
    """The red's key line for a set of flipped listings: the listings themselves, or past FLIP_KEY_MAX a stable hash of
    them (letters only: norm_line reads hex runs and digits as noise)."""
    import hashlib
    line = "flips: " + "; ".join(listings)
    if len(listings) <= FLIP_KEY_MAX and len(line) <= 150:
        return line
    h = hashlib.sha1("\n".join(listings).encode()).hexdigest()[:16]
    return f"flips: {len(listings)} listings #" + "".join(chr(ord("g") + int(c, 16)) for c in h)


# A JOB LOG HOLDS ANSI COLOURS, and gh refuses to print a response with terminal escape sequences unless
# --allow-escape-sequences is passed (2026-11-01: #629's flipped listings went unread, LOG UNREADABLE, so it was keyed
# by its seed line and "refuted" the fixes of two other flips). A gh too old to know the flag never refused: without it.
ESCAPE_FLAG = "--allow-escape-sequences"


# gh's refusal of a body with escape sequences (when the flag is missing): never a log, wherever it lands
ESCAPE_REFUSAL = re.compile(r"\A\s*the response contains terminal escape sequences")


def fetch_job_log_api(o_r, job_id):
    """A job's whole log through the REST API (`gh run view --log` can hit stream cancels, and truncates: below); raises
    with stderr. gh's escape-sequence refusal is a failed read, never the log (a 99-byte "log", 2026-11-19)."""
    env = forge_env(o_r)
    flag = [ESCAPE_FLAG]
    for attempt in (1, 2, 3):
        r = subprocess.run(forge_argv(o_r, "api", *flag, f"repos/{o_r}/actions/jobs/{job_id}/logs"), capture_output=True,
                           text=True, env=env, timeout=300)
        if r.returncode == 0 and not ESCAPE_REFUSAL.match(r.stdout or ""):
            return r.stdout
        if flag and "unknown flag" in (r.stderr or "") and ESCAPE_FLAG in (r.stderr or ""):
            flag = []
    raise RuntimeError((r.stderr.strip().splitlines() or r.stdout.strip().splitlines()
                        or [f"exit {r.returncode}"])[-1][:200])


def drift_key(o_r, job, first_line):
    """(key line, note) of a failed takes-drift job: the flipped listings' key line, or (None, why) when the job is no
    drift row, or its log was not read ("LOG UNREADABLE": never cached, read again) or names no listing (fallback)."""
    if not (job.get("databaseId") and first_line and DRIFT_LINE.search(first_line)):
        return None, None
    try:
        found = flip_listings(fetch_job_log_api(o_r, job["databaseId"]))
    except Exception as e:
        return None, (f"      (LOG UNREADABLE: {job['name']}'s flipped listings: {str(e)[:160]}; keyed by its seed line "
                      "like any drift row until they are read, read again next time)")
    if not found:
        return None, f"      (no flipped listing named in {job['name']}'s log: keyed by its first error line)"
    return flip_key_line(found), None


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
    """A log gh serves, read twice at most. A read it failed (the ci-runner actions API resetting or refusing
    the connection under load) raises with its stderr: never an empty log, which ci-why read as a 0-length one, INFRA
    (convergence #2473, run 3656: the takes-drift rows' flip failures handed to a diagnose-only fixer)."""
    env = forge_env(o_r)
    for attempt in (1, 2):
        r = subprocess.run(forge_argv(o_r, "run", "view", *args), capture_output=True, text=True, env=env, timeout=300)
        if r.returncode == 0:
            return r.stdout
    raise RuntimeError((r.stderr.strip().splitlines() or [f"exit {r.returncode}"])[-1][:200])


def fetch_failed_log(o_r, run_id, attempt=None):
    return read_log(o_r, str(run_id), "--log-failed", *(["--attempt", str(attempt)] if attempt else []))


# GH'S RUN-ZIP LOGS ARE CUT AND UNMAPPED (2026-11-07..19, issues 20261107T063515-8778ed, 20261110T080020-243b55,
# 20261110T090248-247413, 20261105T080337-49c5ae): `gh run view --log-failed` and `--log --job` read the run's log zip,
# which no longer maps a line to its step (every line `UNKNOWN STEP`, so --log-failed prints the WHOLE job log, not the
# failed step's) and stops at ~1.3-2.6 MB: core #934's `it 2/2` read 2090 of 5832 lines and ended mid-compile, before
# its `*** FAILED ***` (UNCLEAR); #939's red was keyed on a PASSING `✓` line of a passed step near the log's head; #799's
# on teardown chatter. A job whose slice gh could not map is read whole through REST (jobs/<id>/logs: the full log) and
# cut to its failed steps by their own start and end times.
UNMAPPED_STEP = "UNKNOWN STEP"
STEP_TS = re.compile(r"^\ufeff?(\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d)")


def failed_steps(job):
    return [s for s in (job or {}).get("steps") or [] if s.get("conclusion") in ("failure", "timed_out")]


def step_window_lines(log, steps):
    """The lines of a REST job log (`<ts> <line>`) inside the windows of `steps` (startedAt..completedAt, to the
    second): [(step name, line)]; a line with no timestamp goes with the line before it. [] when no step has times."""
    wins = [(str(s.get("startedAt") or "")[:19], str(s.get("completedAt") or "")[:19], s.get("name") or "?")
            for s in steps if s.get("startedAt") and s.get("completedAt")]
    if not wins:
        return []
    out, cur = [], None
    for raw in (log or "").splitlines():
        m = STEP_TS.match(raw)
        if m:
            cur = next((n for a, b, n in wins if a <= m.group(1) <= b), None)
        if cur is not None:
            out.append((cur, raw))
    return out


def log_last_ts(log):
    return next((m.group(1) for m in (STEP_TS.match(l) for l in reversed((log or "").splitlines())) if m), None)


def job_failed_slice(o_r, job):
    """(lines as --log-failed prints them for `job`, note) from its whole REST log: its failed steps' windows, or the
    whole log when the steps carry no times. note: why the read falls short (truncated, unread), else None."""
    log = fetch_job_log_api(o_r, job["databaseId"])
    steps = failed_steps(job)
    picked = step_window_lines(log, steps) or [(steps[0]["name"] if steps else "-", l) for l in log.splitlines()]
    note = None
    ends, done = log_last_ts(log), max((str(s.get("completedAt") or "")[:19] for s in steps), default="")
    end_at, done_at = iso_epoch(ends), iso_epoch(done)
    if end_at is not None and done_at is not None and done_at - end_at > 120:  # the log stops minutes before its failed step ended
        note = f"LOG TRUNCATED: {job['name']}'s log ends {ends}Z, its failed step ended {done}Z"
    return [f"{job['name']}\t{st}\t{l}" for st, l in picked], note


def failed_jobs_log(o_r, run_id, jobs, attempt=None, notes=None):
    """The --log-failed text of a run's failed `jobs`, every job gh could not map (its lines all UNKNOWN STEP, or
    none) read through REST instead (above). notes, a list: each job's shortfall is appended."""
    try:
        text = fetch_failed_log(o_r, run_id, attempt)
    except Exception as e:
        text, gh_err = "", e
    else:
        gh_err = None
    by_job = {}
    for raw in text.splitlines():
        parts = raw.split("\t", 2)
        by_job.setdefault(parts[0] if len(parts) == 3 else "?", []).append(raw)
    out, read_any, rest_reads = [], gh_err is None, 0
    for j in jobs:
        mine = by_job.pop(j.get("name"), [])
        mapped = any(r.split("\t", 2)[1] != UNMAPPED_STEP for r in mine if r.count("\t") >= 2)
        failed_ = j.get("conclusion") in ("failure", "timed_out") or failed_steps(j)  # never a cancelled sibling's
        if mapped or not j.get("databaseId") or not failed_ or rest_reads >= MAX_JOB_LOG_FETCHES:
            out += mine
            continue
        rest_reads += 1
        try:
            lines, note = job_failed_slice(o_r, j)
            out += lines
            read_any = True
            if note and notes is not None:
                notes.append(note)
        except Exception as e:
            out += mine
            if notes is not None and not mine:
                notes.append(f"LOG UNREADABLE: {j.get('name')}: {str(e)[:160]}")
    for rest in by_job.values():
        out += rest
    if not read_any and gh_err is not None:
        raise gh_err
    return "\n".join(out) + ("\n" if out else "")


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
    """A job's whole log: REST's (complete), else gh's run-zip read (cut at ~2 MB, see above)."""
    try:
        return fetch_job_log_api(o_r, job_id)
    except Exception:
        return read_log(o_r, str(run_id), "--log", "--job", str(job_id))


ANNOTATION = re.compile(r"::error\b[^:]*::")
MAX_JOB_LOG_FETCHES = 5


def job_log_lines(o_r, run_id, job, tail, unread=None, annotated=None):
    """Relevant lines from a job's WHOLE log. The per-step slices (what --log-failed prints) are shifted by a
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
    clean = [l for _job, l in log_entries(log)]
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
    clean = [l for _job, l in log_entries(log)]
    if not any("blocked by:" in l or re.search(r"pre-push\S* .*FAILED", l) for l in clean):
        return []
    hits = [l[:300] for l in clean if HOOK_BLOCK.search(l)]
    return hits[:tail * 4] if hits else []


def failure_detail(o_r, run_id, failed, tail):
    """failed: job dicts (name, databaseId) or names."""
    if not failed or tail <= 0:
        return []
    try:
        per = relevant_lines(failed_jobs_log(o_r, run_id, [j for j in failed if isinstance(j, dict)]), tail)
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
    """(done, ok, text) for a CI run. until=landable: a measure run is done once its takes rows ended."""
    d = gh_json(o_r, "run", "view", str(run_id), "--json", "status,conclusion,jobs,workflowName,headBranch")
    jobs = d.get("jobs") or []
    head = f"run {o_r}#{run_id}"
    if until == "landable" and is_measure_run(d):
        # Identity takes names its rows `leg (<cc>) / takes (<cc>)`: the job's own name (job_inner) after the caller's
        takes = [j for j in jobs if job_inner(j.get("name", "")).startswith("takes")]
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
