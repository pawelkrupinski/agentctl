# agentctl, part `ciwhy`: ci-why, the gate, the snapshot.
# Runs inside agentctl's one namespace, after the parts before it in lib/agentctl/__init__.py's PARTS (it uses their names).

# ---------------------------------------------------------------- ci-why
# Exactly the workflows movies' scripts/hooks/pre-push check_redgate reads (GatingWorkflows / GatingWorkflowFiles).
# THE GATE (the owner, 2026-10-27: "if the latest main and convergence runs are green, just push then, remember that"):
# exactly the newest decided Main run and the newest decided Identity model convergence run on main -- and, since the
# owner's 2026-11-08 split of convergence's takes rows into their own workflow, the newest decided Identity takes run
# (dispatched by convergence's preflight on the same ref). Verify scrape recording no longer gates; ci-why lists it, and
# every other red, as information only.
GATING = {"pawelkrupinski/movies": [("Main", "main.yml"), ("Identity model convergence", "identity-model-convergence.yml"),
                                    ("Identity takes", "identity-takes.yml")]}

# THE WATCHED REPOS (the owner, 2026-10-30, ci-failures recommendation 4: infra's Stage NixOS closures was red 29 h and
# core's Main Pipeline 21 h with no fixer, because the red tick read movies only). Which repos the red machine watches
# is config: red-repos.json beside the entry ($AGENTCTL_RED_REPOS names another file), one entry per repo --
# its gate workflows (the newest decided run of each is what opens and clears its reds), its family (every red of the
# repo goes to that ONE fixer; red-families.json says how the family is held) and its push policy: "land" (the fixer
# lands with `agentctl land`) or "owner-only" (an infra push is a deploy: the fixer prepares the fix on a local
# branch and files the owner a one-liner with `agentctl red owner-push`; it never pushes or deploys). The PRIMARY
# repo (movies) keeps everything it had -- its reds' keys, its outside workflows, its local checks, its landing gate
# (GATING above, which stays movies' alone: watching a repo's reds does not put a red gate in front of its landings).
# The other repos are read through their gate workflows only.
RED_PRIMARY = "pawelkrupinski/movies"
RED_REPOS_DEFAULT = [{"repo": RED_PRIMARY, "gate": [list(g) for g in GATING[RED_PRIMARY]], "push": "land"}]
RED_PUSH_POLICIES = ("land", "owner-only")
_red_repos_cache = process_cache({})


def red_repos_path():
    return os.environ.get("AGENTCTL_RED_REPOS") or os.path.join(os.path.dirname(TOOL_PATH), "red-repos.json")


def red_repos_parse(raw):
    """The watched repos from red-repos.json's parsed JSON: [{repo, gate: [(name, file)], family, push, clone}], the
    primary first (added when the file lacks it). Raises ValueError on a malformed entry: a typo must not quietly
    stop a repo being watched."""
    out = []
    for x in (raw or {}).get("repos") or []:
        repo = x.get("repo")
        if not isinstance(repo, str) or not re.fullmatch(r"[\w.-]+/[\w.-]+", repo):
            raise ValueError(f"a repo entry needs `repo: <owner>/<name>`: {x!r}")
        gate = x.get("gate") or []
        if not gate or not all(isinstance(g, list) and len(g) == 2 and all(isinstance(v, str) and v for v in g)
                               for g in gate):
            raise ValueError(f"{repo}: `gate` must be [[<workflow name>, <workflow file>], ...]")
        push = x.get("push") or "land"
        if push not in RED_PUSH_POLICIES:
            raise ValueError(f"{repo}: `push` must be one of {', '.join(RED_PUSH_POLICIES)}, not {push!r}")
        clone = x.get("clone")
        out.append({"repo": repo, "gate": [tuple(g) for g in gate], "family": x.get("family"), "push": push,
                    "clone": os.path.expanduser(clone) if clone else None})
    if len({c["repo"] for c in out}) != len(out):
        raise ValueError("a repo is listed twice")
    if not any(c["repo"] == RED_PRIMARY for c in out):
        out.insert(0, red_repos_parse({"repos": RED_REPOS_DEFAULT})[0])
    return sorted(out, key=lambda c: c["repo"] != RED_PRIMARY)


def red_repos():
    """The watched repos (red_repos_parse), re-read when the file changes; the primary alone when it is missing or
    unreadable (said once per change)."""
    path = red_repos_path()
    try:
        m = os.path.getmtime(path)
    except OSError:
        m = None
    hit = _red_repos_cache.get(path)
    if hit and hit[0] == m:
        return hit[1]
    repos = red_repos_parse({"repos": RED_REPOS_DEFAULT})
    if m is not None:
        try:
            with open(path) as f:
                repos = red_repos_parse(json.load(f))
        except (OSError, ValueError) as e:
            err(f"red repos: {path} unreadable ({str(e)[:200]}): only {RED_PRIMARY} is watched")
    _red_repos_cache[path] = (m, repos)
    return repos


def red_repo_conf(o_r):
    return next((c for c in red_repos() if c["repo"] == o_r), None)


def red_is_other(o_r):
    """True for a watched repo other than the primary (red-repos.json). Any other name -- the primary, or a repo nobody
    configured (the tests' `o/r`) -- is treated as the primary, exactly as before 2026-10-30."""
    return o_r != RED_PRIMARY and red_repo_conf(o_r) is not None


def red_repo_of(d):
    """The repo a red (its data, its picture, a fixer record) belongs to: its `repo`, else the primary (every red from
    before 2026-10-30 is movies')."""
    return (d or {}).get("repo") or RED_PRIMARY


def red_gate_flows(o_r):
    """[(name, file)] of a watched repo's gate workflows: GATING's for the primary, its config's for the others."""
    if o_r in GATING:
        return GATING[o_r]
    c = red_repo_conf(o_r)
    return list(c["gate"]) if c else []


def red_owner_only(o_r):
    """True for a repo whose fixer never pushes (red-repos.json `"push": "owner-only"`)."""
    c = red_repo_conf(o_r)
    return bool(c and c["push"] == "owner-only")


def red_repo_short(o_r):
    return o_r.split("/", 1)[-1]


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


def red_repo_clone(o_r):
    """This machine's clone of o_r: its config's `clone` when it is a git checkout, else a known clone (REPOS); None."""
    c = red_repo_conf(o_r) or {}
    p = c.get("clone")
    if p and os.path.isdir(p) and git_ok(p, "rev-parse", "--git-dir"):
        return p
    try:
        return repo_path_for(o_r)
    except (Exception, SystemExit):
        return None
RUN_FIELDS = "databaseId,number,status,conclusion,headSha,url,displayTitle,workflowName,createdAt,updatedAt,event"
RED_CONCLUSIONS = ("failure", "timed_out")


def repo_path_for(o_r):
    # ci-runner's reconciler has none of REPOS (HOME is its state root): its own clone is movies' scan repo, or
    # main_workflow_files reads nothing there -- no workflow outside the newest 200 runs is listed and no callable-only
    # red is ever proven (2026-10-29: Report run timings #38 stayed offered under f8f51e8)
    for p in REPOS + [server_repo()]:
        if os.path.isdir(p) and git_ok(p, "remote", "get-url", remote()):
            try:
                if slug(p) == o_r:
                    return p
            except (SystemExit, RuntimeError):
                pass
    return None


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


# scripts/ci/bounded-step.sh --reap's warning: a step outlived its own `timeout-minutes` (the runner gives up on it and
# goes on, so its log just stops). Identity rule ablation #2400 read as UNCLEAR with it in its log.
STEP_TIMED_OUT = "ending what a timed-out step left running"


def timed_out_jobs(log_text):
    """The jobs of a --log-failed output whose log carries bounded-step's timed-out-step warning."""
    return {raw.split("\t", 1)[0] for raw in log_text.splitlines() if STEP_TIMED_OUT in raw and "\t" in raw}


def job_is_phantom(j):
    """Failed before it ever ran: no start time, failing in a setup step (or with no step at all)."""
    if j.get("conclusion") not in ("failure", "timed_out") or j.get("startedAt"):
        return False
    bad = [s for s in j.get("steps") or [] if s.get("conclusion") in ("failure", "timed_out")]
    return not bad or bool(SETUP_STEP.search(bad[0]["name"]))


_gate_lists, _outside_lists = process_cache({}), process_cache({})  # workflow -> the runs on main gate_runs / outside_gate last listed (newest first)

# AN INCOMPLETE RUN IS NO VERDICT (movies 4da98c308, 2026-10-30: convergence's `verdict complete` job fails a main run
# whose takes or takes-drift rows were skipped, cancelled or missing -- "no false green"; since 2026-11-08 the job lives
# in Identity takes, with the rows, and a drift-only explorer dispatch on main fails only it, by design). A failed run whose ONLY real
# failure (jobs that never started aside) is that job decided nothing: like a cancelled run it says nothing to the gate -- no
# red, no streak, no fixer -- and ci-why names it NO VERDICT. Recovery is the next complete run.
VERDICT_COMPLETE_JOB = re.compile(r"(^|/\s*)verdict complete$")
# the workflows with a `verdict complete` job (only they are read): Identity takes; convergence for its runs before the split
NO_VERDICT_FLOWS = {"Identity takes", "Identity model convergence"}
_gate_no_verdict = process_cache({})  # workflow -> its newest no-verdict run newer than its decided one (gate_runs), or None
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
    try:
        known = read_record(path) or {}
        known[rid] = nv
        if len(known) > 500:
            known = dict(list(known.items())[-500:])
        write_json(path, known)
    except OSError:
        pass
    return nv


def gate_runs(o_r, flows=None):
    """[(workflow, newest decided run on main | None, newer unfinished run | None, red_streak of its runs)]. flows: the
    workflows to read ([(name, file)]); default the repo's GATING row, else every workflow with a recent run on main."""
    flows = flows or GATING.get(o_r)
    if not flows:
        recent = gh_json(o_r, "run", "list", "--branch", "main", "--limit", "40", "--json", RUN_FIELDS)
        flows = [(n, n) for n in dict.fromkeys(r["workflowName"] for r in recent)]
    out = []
    for name, wfile in flows:
        runs = gh_json(o_r, "run", "list", "--workflow", wfile, "--branch", "main", "--limit", "30", "--json", RUN_FIELDS)
        decided, nov = None, None
        nv = (lambda r: run_no_verdict(o_r, r)) if name in NO_VERDICT_FLOWS else (lambda r: False)
        for r in runs:
            if r.get("status") == "completed" and r.get("conclusion") in ("success", "failure", "timed_out"):
                if nv(r):
                    nov = nov or r  # the newest incomplete run: no verdict, it says nothing
                    continue
                decided = r
                break
        newer = next((r for r in runs if r.get("status") != "completed"), None)
        if newer and decided and newer["databaseId"] < decided["databaseId"]:
            newer = None
        _gate_lists[name], _gate_no_verdict[name] = runs, nov
        out.append((name, decided, newer, red_streak(runs, skip=nv)))
    return out


def red_streak(runs, skip=None):
    """(since, count) of the red streak that `runs` (one workflow's on main, newest first) ends on: its decided runs up
    to the newest success, `since` the oldest failure's completion (when CI first said red). (None, 0) when green.
    skip(run): a failed run that decided nothing (run_no_verdict), left out like a cancelled one."""
    decided = (r for r in runs if r.get("status") == "completed" and r.get("conclusion") in ("success",) + RED_CONCLUSIONS)
    streak = [r for r in itertools.takewhile(lambda r: r.get("conclusion") in RED_CONCLUSIONS, decided)
              if not (skip and skip(r))]
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
    if now is None:  # $AGENTCTL_NOW: the tests' clock
        now = (dt.datetime.fromisoformat(os.environ["AGENTCTL_NOW"].replace("Z", "+00:00")) if os.environ.get("AGENTCTL_NOW")
               else dt.datetime.now(dt.timezone.utc))
    mins = max(0, int((now - t).total_seconds() // 60))
    d, h, m = mins // 1440, mins % 1440 // 60, mins % 60
    return f"{d}d {h}h" if d else f"{h}h {m}m" if h else f"{m}m"


OUTSIDE_FLOW_RECHECK = 1800  # a workflow outside the newest-200 window is listed on its own this often (audit #7)
_flow_files = process_cache({})   # main sha -> {workflow name: file}
_flow_meta = process_cache({})    # main sha -> {"callable": {name whose only trigger is workflow_call}, "paths": {name: path},
#                                  "callers": {called file's basename: [caller names]}}
_flow_runs = process_cache({})    # workflow file -> (listed at, its runs on main)


def main_workflow_files(o_r):
    """{workflow name: file} of the workflow files on origin/main (cached per main sha); {} when unreadable."""
    repo = scan_repo_for(o_r)
    if not repo or not os.path.isdir(repo):
        return {}
    main = git(repo, "rev-parse", f"refs/remotes/{remote()}/main", check=False).stdout.strip()
    if not main:
        return {}
    if main not in _flow_files:
        out, meta = {}, {"callable": set(), "paths": {}, "callers": {}}
        for d in (".github/workflows",):
            r = git(repo, "ls-tree", "--name-only", main, f"{d}/", check=False)
            for path in r.stdout.split():
                if path.endswith((".yml", ".yaml")):
                    txt = git(repo, "show", f"{main}:{path}", check=False).stdout
                    m = re.search(r"^name:\s*[\"']?(.+?)[\"']?\s*$", txt, re.M)
                    name = m.group(1) if m else os.path.basename(path)
                    out[name] = os.path.basename(path)
                    meta["paths"][name] = path
                    if workflow_triggers(txt) == {"workflow_call"}:
                        meta["callable"].add(name)
                    for called in re.findall(r"^\s*(?:-\s*)?uses:\s*\./\.github/workflows/([\w.-]+)\s*$", txt, re.M):
                        meta["callers"].setdefault(called, []).append(name)
        _flow_files.clear()
        _flow_files[main] = out
        _flow_meta.clear()
        _flow_meta[main] = meta
    return _flow_files[main]


_flow_history, _flow_bounds = process_cache({}), process_cache({})


def stuck_history(o_r, name):
    """The workflow's own last 30 finished runs on main (stuck_limit's, for a workflow the newest-200 list holds too few
    of): listed at most every OUTSIDE_FLOW_RECHECK; [] when its file is unknown or the list fails."""
    wfile = main_workflow_files(o_r).get(name)
    if not wfile:
        return []
    at, runs = _flow_history.get(wfile, (0.0, None))
    if runs is None or time.time() - at >= OUTSIDE_FLOW_RECHECK:
        try:
            runs = gh_json(o_r, "run", "list", "--workflow", wfile, "--branch", "main", "--status", "completed",
                           "--limit", "30", "--json", RUN_FIELDS)
            _flow_history[wfile] = (time.time(), runs)
        except (Exception, SystemExit) as e:
            err(f"stuck limit: could not list {wfile} ({str(e)[:120]})")
            runs = runs or []
    return runs


def workflow_timeout_bound(txt):
    """Seconds a run of this workflow file can take at most by its jobs' own `timeout-minutes` (summed: jobs may run
    one after another). A job setting none counts GitHub's 360-minute default (review 2026-11-07: it counted 0 beside
    a job that set one); a reusable-workflow call (`uses:`, which takes none) counts 0. None without a jobs section."""
    lines = [l for l in txt.splitlines() if l.strip() and not l.lstrip().startswith("#")]
    try:
        i = next(n for n, l in enumerate(lines) if re.match(r"^jobs:\s*$", l))
    except StopIteration:
        return None
    ind = lambda l: len(l) - len(l.lstrip())  # noqa: E731
    job_ind = prop_ind = None
    jobs = []  # per job: [its timeout-minutes in s or None, a reusable call]
    for l in lines[i + 1:]:
        n = ind(l)
        if n == 0:
            break
        if job_ind is None:
            job_ind = n
        if n == job_ind:
            prop_ind = None
            jobs.append([None, False])
            continue
        if prop_ind is None and n > job_ind:
            prop_ind = n
        if n != prop_ind or not jobs:
            continue
        m = re.match(r"^\s*timeout-minutes:\s*(\d+)\s*$", l)
        if m:
            jobs[-1][0] = int(m.group(1)) * 60
        elif re.match(r"^\s*uses:", l):
            jobs[-1][1] = True
    total = sum(t if t is not None else 0 if uses else GITHUB_JOB_TIMEOUT for t, uses in jobs)
    return total or GITHUB_JOB_TIMEOUT

def stuck_timeout(o_r, name):
    """workflow_timeout_bound of the workflow's file on origin/main (cached per main sha), or None."""
    repo = scan_repo_for(o_r)
    path = main_flow_meta(o_r)["paths"].get(name)
    if not repo or not path:
        return None
    main = git(repo, "rev-parse", f"refs/remotes/{remote()}/main", check=False).stdout.strip()
    if (main, path) not in _flow_bounds:
        if len(_flow_bounds) > 200:
            _flow_bounds.clear()
        txt = git(repo, "show", f"{main}:{path}", check=False).stdout
        _flow_bounds[(main, path)] = workflow_timeout_bound(txt) if txt else None
    return _flow_bounds[(main, path)]


def main_flow_meta(o_r):
    """main_workflow_files' by-products for origin/main: callable-only names, each name's path, each file's callers."""
    main_workflow_files(o_r)
    return next(iter(_flow_meta.values()), {"callable": set(), "paths": {}, "callers": {}})


def main_callable_flows(o_r):
    """The workflows on origin/main whose only trigger is workflow_call: they never run on their own, so no newer run
    of theirs can ever clear a red run of theirs on main (red_callable_evidence does)."""
    return main_flow_meta(o_r)["callable"]


def workflow_triggers(txt):
    """The trigger names of a workflow file's `on:` (a scalar, a [list], a block of keys or of `- items`)."""
    lines = txt.splitlines()
    for i, line in enumerate(lines):
        m = re.match(r"^[\"']?on[\"']?:\s*(.*?)\s*(#.*)?$", line)
        if not m:
            continue
        inline = m.group(1)
        if inline:
            while re.search(r"\{[^{}]*\}", inline[1:]):  # a {flow: mapping}: its keys, not their values
                inline = inline[0] + re.sub(r"\{[^{}]*\}", "", inline[1:])
            return set(re.findall(r"[A-Za-z_]+", re.sub(r"\[[^\]]*\]", "", inline) if inline[0] == "{" else inline))
        names, indent = set(), None
        for sub in lines[i + 1:]:
            if not sub.strip() or sub.lstrip().startswith("#"):
                continue
            k = re.match(r"^(\s+)(?:-\s*)?[\"']?([A-Za-z_]+)[\"']?\s*(:|$)", sub)
            if not k:
                break  # the next top-level key
            if indent is None:
                indent = len(k.group(1))
            if len(k.group(1)) == indent:
                names.add(k.group(2))
        return names
    return set()


def outside_gate(o_r):
    """[(workflow, newest decided run, since, count)] for each workflow on main OUTSIDE the gate whose newest decided
    run is red: not blocking a push, but red, and for how long (the owner, 2026-10-27: a red nobody is shown is a red
    that gets ignored). EVERY workflow file on main is read (audit #7): those in the newest 200 runs from that list,
    the others listed on their own (at most every OUTSIDE_FLOW_RECHECK)."""
    gated = {n for n, _ in GATING.get(o_r, [])}
    runs = gh_json(o_r, "run", "list", "--branch", "main", "--limit", "200", "--json", RUN_FIELDS)
    lists = {}
    for r in runs:
        if r.get("workflowName"):
            lists.setdefault(r["workflowName"], []).append(r)
    for name, wfile in main_workflow_files(o_r).items():
        if name in lists or name in gated:
            continue
        at, mine = _flow_runs.get(wfile, (0.0, None))
        if mine is None or time.time() - at >= OUTSIDE_FLOW_RECHECK:
            try:
                mine = gh_json(o_r, "run", "list", "--workflow", wfile, "--branch", "main", "--limit", "30",
                               "--json", RUN_FIELDS)
                _flow_runs[wfile] = (time.time(), mine)
            except Exception as e:  # this workflow unread this time: the others stand
                err(f"outside the gate: could not list {wfile} ({str(e)[:120]})")
                mine = mine or []
        if mine:
            lists[name] = mine
    out = []
    for name, mine in lists.items():
        if name in gated:
            continue
        _outside_lists[name] = mine
        since, count = red_streak(mine)
        if count:
            decided = next(r for r in mine if r.get("status") == "completed" and r.get("conclusion") in RED_CONCLUSIONS)
            out.append((name, decided, since, count))
    return sorted(out, key=lambda x: str(x[2]))


def explain_run(o_r, run, tail, repo=None):
    """Lines explaining one red run: commit, failed jobs, their log tails, infra vs test."""
    lines, num, _per_job = explain_run_jobs(o_r, run, tail, repo)
    return lines, num


def explain_run_jobs(o_r, run, tail, repo=None):
    """(lines, run number, [(failed job, infra|unclear|test, its first relevant line)])."""
    rid = run["databaseId"]
    d = gh_json(o_r, "run", "view", str(rid), "--json",
                "jobs,headSha,url,number,status,conclusion,workflowName,workflowDatabaseId")
    num = d.get("number") or run.get("number")
    failed = [j for j in d.get("jobs") or [] if j.get("conclusion") in ("failure", "timed_out")]
    unread = []  # why the failed log, and a job's own, could not be read: no log was read, so none is 0-length
    no_verdict = jobs_no_verdict(d.get("jobs"))
    try:
        log = fetch_failed_log(o_r, rid) if failed and not no_verdict else ""
    except Exception as e:
        log = ""
        unread.append(str(e))
        err(f"could not read run {rid}'s failed log: {e}")
    strong_jobs = set()
    per, raws, timed = relevant_lines(log, tail, strong_jobs), raw_tails(log), timed_out_jobs(log)
    lines = [f"  run id {rid} (#{num}) {d.get('url') or run.get('url')}",
             f"  commit {commit_line(o_r, d.get('headSha') or run.get('headSha'), repo)}"]
    kinds, fetched, per_job = [], 0, []
    for j in failed:
        if no_verdict or job_is_verdict_report(j):  # reporting, never a failure of the code (see above)
            per_job.append((j["name"], "incomplete", ""))
            lines.append(f"  [incomplete] {j['name']}: a takes or takes-drift row was skipped, cancelled or missing"
                         + ("" if no_verdict else " -- no verdict of its own: it follows the other failed jobs"))
            continue
        jl, strong = per.get(j["name"]) or [], j["name"] in strong_jobs
        if fetched < MAX_JOB_LOG_FETCHES and "precommit" in str(d.get("workflowName", "")).lower().replace("-", ""):
            hl = hook_lines(o_r, rid, j, tail)  # the hook's own verdict beats a passing subtest's tail
            if hl:
                fetched, jl, strong = fetched + 1, hl, True
        bad = [st for st in j.get("steps") or [] if st.get("conclusion") in ("failure", "timed_out")]
        setup = bool(bad) and bool(SETUP_STEP.search(bad[0]["name"]))
        note = None
        if not strong and not setup and fetched < MAX_JOB_LOG_FETCHES:
            # no STRONG line in the step's slice (a shifted slice, or only weak words): the job's own `::error`
            # annotations, in its whole log, say what failed
            fetched += 1
            was, ann = len(unread), []
            whole = job_log_lines(o_r, rid, j, tail, unread, ann)
            if jl and len(unread) > was:  # keyed by a weak line only because the annotations went unread: never cached
                note = (f"      (LOG UNREADABLE: {j['name']}'s whole log, which holds its ::error annotations: "
                        f"{unread[-1][:160]}; read again next time)")
            jl = choose_job_lines(jl, strong, whole, bool(ann and ann[0]))
        kind, why = classify_job(j, jl, raws.get(j["name"], (0, [])), j["name"] in timed or any(STEP_TIMED_OUT in l for l in jl))
        if unread and not jl and not setup:
            kind, why = "unclear", (f"LOG UNREADABLE ({unread[-1]}): no log was read, so nothing says infra or test -- "
                                    f"read `gh run view {rid} --log-failed` again; never rerun on this alone")
        kinds.append(kind)
        # a job with no error line is known by its failing STEP (a red is its failing test or step, never its job name)
        key_line, drift_note = drift_key(o_r, j, jl[0] if jl else "")
        per_job.append((j["name"], kind, key_line or (jl[0] if jl else (f"step failed: {bad[0]['name']}" if bad else ""))))
        lines.append(f"  [{kind}] {j['name']}: {why}")
        if key_line:
            lines.append(f"      red key: {key_line}")
        lines += [f"      {l}" for l in jl]
        if drift_note:
            lines.append(drift_note)
        if note:
            lines.append(note)
    rerun = f"rerun: {GH_CLI.replace(HOME, '~')} run rerun {rid} --failed -R {o_r}"
    if no_verdict:
        verdict = ("NO VERDICT (an incomplete run: only `verdict complete` failed -- a takes or takes-drift row was "
                   "skipped, cancelled or missing; not a test failure, no fixer: the next complete run decides)")
    elif any(l.startswith("  [test]") and ": TIMEOUT: " in l for l in lines):
        verdict = "TIMEOUT (a step ran past its own timeout-minutes: a code/config red, not a rerun)"
    elif "test" in kinds:
        verdict = "TEST FAILURE"
    elif "unclear" in kinds:
        verdict = f"UNCLEAR (a failed step logged no FAILED/error line: read `gh run view {rid} --log-failed`; if infra, {rerun})"
    else:
        verdict = f"INFRA-SHAPED ({rerun})" if kinds else "no failed job listed"
    if not kinds and d.get("status") not in (None, "completed"):  # undecided: running, or waiting for a runner slot
        verdict = (queue_full(o_r, num, runner_queue()) if not any(j.get("startedAt") for j in d.get("jobs") or [])
                   else None) or f"undecided ({d.get('status')})"
    lines.insert(0, f"  verdict: {verdict}")
    return lines, num, per_job


def ci_why(o_r, tail=6, run_id=None, repo=None, summary=False, snap=None):
    """(red, text). run_id: explain that one run; else the gating workflows' newest decided runs on main, each red row
    followed by who holds it (`fixing: <its state, holder or offer>`; never NOBODY). summary: text is the status line's one line."""
    if run_id:
        lines, _ = explain_run(o_r, {"databaseId": int(run_id)}, tail, repo)
        return True, f"run {o_r}#{run_id}\n" + "\n".join(lines)
    out, red, rows, units_of, parts = [], False, [], {}, []
    gate = gate_from_snap(snap) if snap else gate_runs(o_r)
    others, others_err = [], None
    if snap:
        others, others_err = outside_from_snap(snap), snap.get("outside_error")
    elif o_r in GATING:
        try:
            others = outside_gate(o_r)
        except Exception as e:  # the gate's verdict stands without them
            others_err = e
    lrepo = scan_repo_for(o_r, repo)
    lstale = local_stale(lrepo)
    bsnap = snap or ci_state_load(o_r)  # the branch reds come from the snapshot only (the processor writes it)
    explained = {}
    for name, decided, newer, _streak in gate:
        if decided and decided.get("conclusion") in RED_CONCLUSIONS:
            if summary:  # THE ONE-LINE SUMMARY NEVER RE-READS A DECIDED RUN'S LOGS (2026-10-29: ci-runner's gate summary
                # read convergence #2473's failed log and five job logs, ~8 s each, every 45 s -- "gate summary: still
                # running after 20 s" at every tick). Its line shows no log line; the run's signatures are cached once
                # per run and classifier (run_sigs), which is all the red units below read.
                run_sigs(o_r, decided, name)
                continue
            lines, num, per_job = explain_run_jobs(o_r, decided, tail, repo)
            explained[name] = (lines, num)
            if not any("LOG UNREADABLE" in l for l in lines):
                save_run_sigs(o_r, decided, name, per_job)
    states, units, flakes_ = {}, [], []
    try:  # who holds each red: its item in the store (the tick acts; ci-why only renders)
        rows = [(n, d, _gate_lists.get(n, []), True) for n, d, _nw, _s in gate]
        units = gate_red_units(o_r, rows, [(n, r, _outside_lists.get(n, []), False) for n, r, _s, _c in others],
                               rereads=0 if summary else None)
        units += local_units(lrepo) + predict_units(o_r, bsnap)
        for u in units:
            units_of.setdefault((u["workflow"], u["run"].get("databaseId")), []).append(u)
        store = Store()
        states = red_view_of_units(store, units)
        flakes_ = [it for it in red_active(store).values() if it["data"].get("red_kind") == "flake"]
    except (Exception, SystemExit) as e:  # who is fixing is information: the gate's verdict stands without it
        err(f"red: could not tell who is fixing ({str(e)[:160]})")

    def fixing(name, run, indent="  "):
        st = states.get((name, run.get("databaseId")))
        return red_lines(st, indent), st

    def short(st):
        return st["short"] if st else "?"

    # A RUN ITS CALLERS PROVED FIXED is no red, though it stays its callable-only workflow's newest run on main for good
    proven = [o for o in others if (fixing(o[0], o[1])[1] or {}).get("state") == "verified"]
    others = [o for o in others if o not in proven and not red_run_retired(o_r, o[0], o[1])]

    for name, decided, newer, (since, count) in gate:
        pending = f"; newer run {newer['databaseId']} (#{newer.get('number')}) {newer.get('status')} may clear it" if newer else ""
        nov = _gate_no_verdict.get(name)
        if nov and not summary:
            out.append(f"{name}: newest run {nov['databaseId']} #{nov.get('number')}: NO VERDICT (incomplete: only "
                       "`verdict complete` failed -- a takes row skipped, cancelled or missing; no red, no fixer); the "
                       "gate reads the newest complete run, and the next complete run decides")
        if not decided:
            out.append(f"{name}: no decided run on main")
            parts.append(f"{short_flow(name)} ?")
            continue
        if decided["conclusion"] == "success":
            out.append(f"{name}: GREEN (run {decided['databaseId']} #{decided.get('number')})")
            parts.append(f"{short_flow(name)} green")
            continue
        retired = red_run_retired(o_r, name, decided)
        if retired:  # its check is gone from main: no run can clear it, it gates nothing and no fixer is offered it
            out.append(f"{name}: RETIRED (run {decided['databaseId']} #{decided.get('number')}, {decided['conclusion']}): "
                       f"{retired} -- no red")
            parts.append(f"{short_flow(name)} retired")
            continue
        red = True
        streak = f"; red for {red_for(since)} ({count} failed run{'s' if count != 1 else ''})" if count else ""
        out.append(f"{name}: RED (run {decided['databaseId']} #{decided.get('number')}, {decided['conclusion']}{streak}){pending}")
        flines, finfos = fixing(name, decided)
        out += flines
        if summary:
            parts.append(f"{short_flow(name)} #{decided.get('number')} \u2192 {short(finfos)}")
            continue
        lines, num = explained.get(name) or explain_run(o_r, decided, tail, repo)
        out += lines
        out.append(f"  clears when: a later {name} run on main is green, or every pushed commit touching a production path "
                   f"carries `Fixes-Red: {num}` (or `Fixes-Red: {decided['databaseId']}`) or names another run of this red episode "
                   f"failing the same way; tools-only commits land while red")
        parts.append(f"{short_flow(name)} #{decided.get('number')} \u2192 {short(finfos)}")
    lus = local_units(lrepo)
    local = [u for u in units_of.get((LOCAL_FLOW, (lus or [{"run": {}}])[0]["run"].get("databaseId")), [])]
    if lstale:
        out.append(f"{LOCAL_FLOW}: stale, rechecking (recorded on {str(local_red().get('main'))[:10]}; "
                   f"{remote()}/main is {lstale[:10]} now)")
        parts.append("local \u2192 stale, rechecking")
    if local:
        lr = local_red()
        main = str(lr.get("main"))[:10]
        out.append(f"{LOCAL_FLOW}: RED on plain {remote()}/main {main} ({', '.join(u['job'] for u in local)}: pre-push fails "
                   f"there without any change) -- claim: `agentctl red claim local`")
        flines, finfos = fixing(LOCAL_FLOW, local[0]["run"])
        out += flines
        for u in local:
            out.append(f"  [{u['job']}] {u['line'][:160]}")
        parts.append(f"local checks \u2192 {short(finfos)}")
    if flakes_:
        out.append("Flaky (passed on the next run with no fix; a red until a fix lands and a run proves it):")
        for it in flakes_:
            out.append(f"  {red_what(it['data'])}: {str(it['data'].get('line'))[:120]}")
            out.append(f"    fixing: {red_view(Store(), it)[0]}")
    head = f"ci-why {o_r}: " + ("main is RED for the gate" if red else "main is green for the gate")
    if others_err is not None:
        out.append(f"outside the gate: could not list main's runs ({others_err})")
    if others:
        out.append(f"Outside the gate (red, not blocking a push; oldest first):")
        for name, r, since, count in others:
            out.append(f"  {name}: RED for {red_for(since)} since {since} ({count} failed run{'s' if count != 1 else ''}; "
                       f"newest run {r['databaseId']} #{r.get('number')}) -- `agentctl ci-why {r['databaseId']}`")
            out += fixing(name, r, "    ")[0]
    if proven:
        out.append("Proven fixed outside the gate (no red; it runs only when called, so its newest run on main stays red):")
        for name, r, _since, _count in proven:
            out.append(f"  {name}: newest run {r['databaseId']} #{r.get('number')} -- `agentctl ci-why {r['databaseId']}`")
            out += fixing(name, r, "    ")[0]
    blines = branch_lines(bsnap)
    if blines:
        out += blines
        for u in units_of.values():
            for x in u:
                if x.get("predicts"):
                    out += fixing(x["workflow"], x["run"], "    ")[0]
                    break
    if summary:
        line = "gate RED" if red else "gate green"
        if red:
            line += " (" + "; ".join(p_ for p_ in parts if not p_.startswith("local")) + ")"
        reds_, causes = [], {}
        for name, r, _since, _count in others:
            st = fixing(name, r)[1]
            cause = (st or {}).get("cause") if (st or {}).get("state") == "blocked-external" else None
            if cause and cause not in (DECISION_CAUSE, CRASH_LOOP_CAUSE):  # ONE line per outage, naming the reds it
                # blocks; a decision or the fixers' crash loop is no outage: its red reads WAITING ON YOU: <question>
                causes.setdefault(cause, (st.get("until"), [], (st.get("ask"), st.get("ask_how"))))[1].append(name)
                continue
            reds_.append(f"{name} #{r.get('number')} \u2192 {short(st)}")
        reds_ = [f"{c} down \u2192 blocks: {', '.join(ns)}" + (f" (auto-clears when {u_})" if u_ else "")
                 + (f" -- WAITING ON YOU: {trim_words(q_[0])} -- {q_[1]}" if q_ and q_[0] else "") for c, (u_, ns, q_) in causes.items()] + reds_
        reds_ += [p_ for p_ in parts if p_.startswith("local")]
        if len(reds_) > SUMMARY_MAX_REDS:
            reds_ = reds_[:SUMMARY_MAX_REDS] + [f"+{len(reds_) - SUMMARY_MAX_REDS} more"]
        if reds_:
            line += " \u00b7 reds: " + "; ".join(reds_)
        bsum = branch_summary(bsnap)
        if not reds_ and not bsum and not red and others_err is None:
            line += " \u00b7 no reds"
        if bsum:
            line += " \u00b7 " + bsum
        return red, line
    return red, "\n".join([head] + out)


# THE ONE CI-STATE SNAPSHOT (the owner, 2026-10-27: the watch, the gate summary, ci-why's outside list and the red patrol
# each polled the forge on their own and disagreed). The processor reads every main workflow once per cycle, writes
# ci-state.json atomically; the gate verdict, the outside-the-gate reds, the published summary and the patrol read it.
# One older than CI_STATE_MAX_AGE is UNREAD (never green).
CI_STATE_MAX_AGE = 180


# THE SNAPSHOT (design §9, phase 1 step 13): ONE file, state.json at the state root, written by the processor -- the
# gate's read (`ci`), the published gate line (`summary`), and each tick the items (changes, the batch, reds,
# conditions), `tick_at` and the code version. The status line and `ci-why --summary --cached` read only it; their
# staleness words come from its times. ci-state.json, gate-summary.txt and the statusline.stale marker are gone.
_state_lock = __import__("threading").Lock()


def state_path():
    return os.environ.get("AGENTCTL_STATE_FILE") or state_root("state.json")


def state_read():
    try:
        with open(state_path()) as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def state_update(**parts):
    """Merge parts into state.json, atomically (one writer at a time: a lock file, and a lock for this process's
    threads)."""
    path = state_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with _state_lock, open(path + ".lock", "a+") as lk:
        fcntl.flock(lk, fcntl.LOCK_EX)
        d = state_read()
        d.update(parts)
        tmp = f"{path}.{os.getpid()}.tmp"
        with open(tmp, "w") as f:
            json.dump(d, f)
        os.replace(tmp, path)
    return d


def ci_state_refresh(o_r, write=True):
    """Read the gate and every other workflow on main once, write the snapshot (unless write=False: ci_state_write
    later), return it. Raises when the gate cannot be read (nothing is written: the old snapshot ages out)."""
    gate = gate_runs(o_r)
    snap = {"at": time.time(), "repo": o_r, "outside_error": None, "outside": [],
            "gate": [{"name": n, "decided": d, "newer": nw, "since": st[0], "count": st[1],
                      "runs": _gate_lists.get(n, []), "no_verdict": _gate_no_verdict.get(n)} for n, d, nw, st in gate]}
    if o_r in GATING:
        try:
            snap["outside"] = [{"name": n, "decided": r, "since": since, "count": count,
                                "runs": _outside_lists.get(n, [])} for n, r, since, count in outside_gate(o_r)]
        except Exception as e:
            snap["outside_error"] = str(e)[:200]
        snap["callable_only"] = sorted(main_callable_flows(o_r))  # their reds wait for red_callable_evidence
    try:  # the branch runs: measure/*, check/*, red fixers' and every recorded dispatch's (after the main lists)
        prev = (state_read().get("ci") or {}).get("branches") or []
        snap["branches"] = branch_state(o_r, scan_repo_for(o_r), prev)
    except (Exception, SystemExit) as e:
        snap["branches_error"] = str(e)[:200]
    latest = {}  # each workflow's newest decided run on main: what workflow_green probes read
    for name, runs in list(_outside_lists.items()) + list(_gate_lists.items()):
        d = next((r for r in runs if r.get("status") == "completed" and not is_cancelled(r)
                  and not (name in NO_VERDICT_FLOWS and run_no_verdict(o_r, r))), None)
        if d:
            latest[name] = {k: d.get(k) for k in ("number", "databaseId", "conclusion", "headSha", "updatedAt")}
    snap["latest"] = latest
    snap["stuck"] = stuck_runs({**_outside_lists, **_gate_lists}, o_r=o_r)  # audit #8: undecided far past their workflow's p90
    if write:
        ci_state_write(snap)
    return snap


def ci_state_write(snap):
    state_update(ci=snap)
    return snap


def ci_state_load(o_r, now=None):
    """The snapshot when it is for o_r and at most CI_STATE_MAX_AGE old, else None (unread)."""
    try:
        snap = state_read().get("ci") or {}
        age = (now or time.time()) - float(snap["at"])
    except (KeyError, TypeError, ValueError):
        return None
    return snap if snap.get("repo") == o_r and -5 <= age <= CI_STATE_MAX_AGE else None


def gate_from_snap(snap):
    out = []
    for g in snap["gate"]:
        _gate_lists[g["name"]], _gate_no_verdict[g["name"]] = g["runs"], g.get("no_verdict")
        out.append((g["name"], g["decided"], g["newer"], (g["since"], g["count"])))
    return out


def outside_from_snap(snap):
    out = []
    for g in snap["outside"]:
        _outside_lists[g["name"]] = g["runs"]
        out.append((g["name"], g["decided"], g["since"], g["count"]))
    return out
