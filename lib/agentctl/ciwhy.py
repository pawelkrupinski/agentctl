# agentctl, part `ciwhy`: ci-why, the gate, the snapshot.
# Runs inside agentctl's one namespace, after the parts before it in lib/agentctl/__init__.py's PARTS (it uses their names).

# ---------------------------------------------------------------- ci-why
# THE GATE IS movies' GATING CHECKS REGISTRY (issue 20261030T095941-7267ea; the owner, 2026-11-19). Which workflows gate
# a push -- THE GATE (the owner, 2026-10-27: "if the latest main and convergence runs are green, just push then"; since
# 2026-11-08 also Identity takes) -- and which of their jobs a red counts for is ONE file on movies' main,
# GATING_CHECKS_FILE (scripts/ci/gating_checks.py), the file movies' pre-push red gate reads too. agentctl reads it from
# origin/main as it reads red-workflows.tsv, and keeps no list of its own. Its `workflow` rows are the gate workflows;
# a `job` row is a run number (proven: its first green run), `aggregate` (fails only because a job it waits on did) or
# `unproven` (a new check: it REPORTS until its first green run). A gate run red only in unproven jobs and the aggregates
# following them -- or any red of an unproven gate workflow -- is REPORT ONLY: it holds no push, it is no gate red (its
# reds are outside the gate's), and ci-why says so. Only movies (GATED_REPOS) puts a red gate in front of its landings.
GATING_CHECKS_FILE = ".github/gating-checks.tsv"  # kind, workflow (file), name, proven (#run | aggregate | unproven), note
GATED_REPOS = ("pawelkrupinski/movies",)
_gating_rows = process_cache({})      # (repo, main sha) -> rows
_report_only_mem = process_cache({})  # run id -> (bool, why): a decided run's jobs never change


class GateRegistryUnread(RuntimeError):
    """The gating checks registry could not be read and none was read before: the gate is unread (never green)."""


def gated_repo(o_r):
    return o_r in GATED_REPOS


def gating_checks_parse(text):
    """The registry's rows [{kind, workflow, name, proven, note}] (gating_checks.py load's, comments and header out)."""
    rows = []
    for line in (text or "").splitlines():
        if not line.strip() or line.startswith("#") or line.startswith("kind\t"):
            continue
        cols = line.split("\t")
        if len(cols) < 4:
            continue
        rows.append({"kind": cols[0], "workflow": cols[1], "name": cols[2], "proven": cols[3].strip(),
                     "note": cols[4] if len(cols) > 4 else ""})
    return rows


def gating_checks_path():
    return state_root("gating-checks.json")


GATING_API_TTL = 300  # s a registry (or workflow names) read through GitHub's API, with no clone here, is kept


def gating_checks(o_r):
    """The registry's rows at main: from the repo's clone at origin/main (cached per main sha), else -- no clone here --
    through GitHub's contents API (kept GATING_API_TTL). Unreadable now (no file, a git or API error): the rows last read
    (kept in the state root: a stale registry is an earlier main's, never none); never read: raises GateRegistryUnread."""
    repo = scan_repo_for(o_r)
    rows, main, why = [], None, "no clone of it here"
    if repo and os.path.isdir(repo):
        main = git(repo, "rev-parse", f"refs/remotes/{remote()}/main", check=False).stdout.strip()
        if main and (repo, main) in _gating_rows:
            return _gating_rows[(repo, main)]
        r = git(repo, "show", f"{main}:{GATING_CHECKS_FILE}", check=False) if main else None
        rows = gating_checks_parse(r.stdout) if r is not None and r.returncode == 0 else []
        if rows:
            _gating_rows[(repo, main)] = rows
        why = f"{GATING_CHECKS_FILE} unread at {remote()}/main {(main or '?')[:10]}"
    else:
        hit = _gating_rows.get(("api", o_r))
        if hit and time.time() - hit[0] < GATING_API_TTL:
            return hit[1]
        try:
            import base64
            c = gh_json(o_r, "api", f"repos/{o_r}/contents/{GATING_CHECKS_FILE}?ref=main")
            rows = gating_checks_parse(base64.b64decode(c.get("content") or "").decode("utf-8", "replace"))
            main = "api"
        except (Exception, SystemExit) as e:  # SystemExit: NoToken
            why = f"no clone of it here, and {GATING_CHECKS_FILE} unread through the API ({str(e)[:120]})"
        if rows:
            _gating_rows[("api", o_r)] = (time.time(), rows)
    if rows:
        try:
            kept = read_record(gating_checks_path()) or {}
            if kept.get(o_r, {}).get("rows") != rows:
                kept[o_r] = {**kept.get(o_r, {}), "main": main, "rows": rows, "at": ts()}
                write_json(gating_checks_path(), kept)
        except OSError:
            pass
        return rows
    kept = ((read_record(gating_checks_path()) or {}).get(o_r) or {}).get("rows")
    if kept:
        return kept
    raise GateRegistryUnread(f"{o_r}'s gating checks registry: {why}")


def gate_flow_names(o_r, files=()):
    """{workflow file: name} for files (the registry's): main's own workflow files in the clone (main_workflow_files);
    those it does not name, GitHub's workflows list (kept GATING_API_TTL); still unnamed, the names last read (kept
    beside the registry's rows)."""
    names = {f: n for n, f in main_workflow_files(o_r).items()}
    if any(f not in names for f in files):
        hit = _gating_rows.get(("names", o_r))
        if not (hit and time.time() - hit[0] < GATING_API_TTL):
            try:
                flows = gh_json(o_r, "api", f"repos/{o_r}/actions/workflows").get("workflows") or []
                api = {os.path.basename(str(w.get("path") or "")): w.get("name") for w in flows if w.get("name")}
            except (Exception, SystemExit):
                api = {}
            hit = (time.time(), api)
            if api:
                _gating_rows[("names", o_r)] = hit
        names = {**hit[1], **names}
    names = {f: names[f] for f in files if names.get(f)}
    try:
        kept = read_record(gating_checks_path()) or {}
        old = (kept.get(o_r) or {}).get("names") or {}
        if any(old.get(f) != n for f, n in names.items()):
            kept.setdefault(o_r, {})["names"] = {**old, **names}
            write_json(gating_checks_path(), kept)
        return {**{f: old[f] for f in files if old.get(f)}, **names}
    except OSError:
        return names


def gate_flows(o_r):
    """[(name, file)] of a gated repo's gate workflows: the registry's `workflow` rows, named as main names their files
    (gate_flow_names; the file itself when nothing names it)."""
    files = [r["workflow"] for r in gating_checks(o_r) if r["kind"] == "workflow" and r["proven"] != "aggregate"]
    names = gate_flow_names(o_r, files)
    return [(names.get(f) or f, f) for f in files]


def registry_name(name):
    """The registry's name of a run's job: github.com names a called workflow's job `<caller job> / <job>`."""
    return str(name or "").split(" / ")[-1]


def gate_report_only_jobs(rows, wfile, jobs):
    """(True, why) when every failed job of a red run of gate workflow wfile is unproven -- or an aggregate following
    one -- and one is; or when wfile's own row is unproven: gating_checks.py report_only, the hook's rule exactly. A
    failed job with NO row is unproven, as the hook reads it (movies' rule: a new check gates only once it has been
    green), and `why` names the missing row -- every gate job should have one (the registry's lint)."""
    wrow = next((r for r in rows if r["kind"] == "workflow" and r["workflow"] == wfile), None)
    if wrow and wrow["proven"] == "unproven":
        return True, f"report only: {wfile} is unproven ({wrow.get('note') or 'no green run yet'})"
    state = {r["name"]: r["proven"] for r in rows if r["kind"] == "job" and r["workflow"] == wfile}
    failed = [j.get("name") for j in jobs or [] if j.get("conclusion") in RED_CONCLUSIONS]
    if not failed:
        return False, "no failed job"
    proven = {n: state.get(n, state.get(registry_name(n))) for n in failed}
    missing = [n for n in failed if proven[n] is None]
    proven = {n: p or "unproven" for n, p in proven.items()}
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
        if rid and rid in _report_only_mem:
            return _report_only_mem[rid]
        jobs = gh_json(o_r, "run", "view", rid, "--json", "jobs").get("jobs") or []
    except (Exception, SystemExit) as e:  # SystemExit: NoToken
        return False, f"unread ({str(e)[:120]}): it gates"
    out = gate_report_only_jobs(rows, wfile, jobs)
    if "no row in" in out[1] and rid not in _report_only_mem:  # said once per run: every gate job should have a row
        err(f"{name} run {rid}: {out[1].split('; ', 1)[-1]}")
    if rid:
        _report_only_mem[rid] = out
    return out


# THE WATCHED REPOS (the owner, 2026-10-30, ci-failures recommendation 4: infra's Stage NixOS closures was red 29 h and
# core's Main Pipeline 21 h with no fixer, because the red tick read movies only). Which repos the red machine watches
# is config: red-repos.json beside the entry ($AGENTCTL_RED_REPOS names another file), one entry per repo --
# its gate workflows (the newest decided run of each is what opens and clears its reds), its family (every red of the
# repo goes to that ONE fixer; red-families.json says how the family is held) and its push policy: "land" (the fixer
# lands with `agentctl land`) or "owner-only" (an infra push is a deploy: the fixer prepares the fix on a local
# branch and files the owner a one-liner with `agentctl red owner-push`; it never pushes or deploys). The PRIMARY
# repo (movies) keeps everything it had -- its reds' keys, its outside workflows, its local checks, its landing gate
# (its gating checks registry above, which stays movies' alone: watching a repo's reds does not put a red gate in front
# of its landings). The other repos are read through their gate workflows only.
RED_PRIMARY = GATED_REPOS[0]
RED_REPOS_DEFAULT = [{"repo": RED_PRIMARY, "push": "land", "red_workflows": True}]
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
        if repo == RED_PRIMARY and not gate:
            pass  # the primary's gate is its gating checks registry (gate_flows), never a list here
        elif not gate or not all(isinstance(g, list) and len(g) == 2 and all(isinstance(v, str) and v for v in g)
                               for g in gate):
            raise ValueError(f"{repo}: `gate` must be [[<workflow name>, <workflow file>], ...]")
        push = x.get("push") or "land"
        if push not in RED_PUSH_POLICIES:
            raise ValueError(f"{repo}: `push` must be one of {', '.join(RED_PUSH_POLICIES)}, not {push!r}")
        clone = x.get("clone")
        keeps = x.get("red_workflows", False)
        if not isinstance(keeps, bool):
            raise ValueError(f"{repo}: `red_workflows` is true or false, not {keeps!r}")
        out.append({"repo": repo, "gate": [tuple(g) for g in gate], "family": x.get("family"), "push": push,
                    "clone": os.path.expanduser(clone) if clone else None,
                    "generated": generated_parse(repo, x.get("generated")), "red_workflows": keeps})
    if len({c["repo"] for c in out}) != len(out):
        raise ValueError("a repo is listed twice")
    if not any(c["repo"] == RED_PRIMARY for c in out):
        out.insert(0, red_repos_parse({"repos": RED_REPOS_DEFAULT})[0])
    return sorted(out, key=lambda c: c["repo"] != RED_PRIMARY)


def generated_parse(repo, raw):
    """A repo's GENERATED FILES (waiting.py): [{paths: [glob], regen: [argv] or None, timeout: s}] from its `generated`
    entries (`{"paths": [...], "regen": "<command run in the tree>", "timeout": <s, optional>, "merge": "rows" | "regen"}`;
    no `regen`: a table merged row by row only, its rows hand-written -- a row both sides changed is the author's
    conflict; `merge: regen`: a SNAPSHOT -- HTML, JSON, a rendered text -- never merged by rows, regenerated over the
    combined tree whenever both sides changed it: waiting.py SNAPSHOTS ARE REGENERATED, NEVER MERGED). Raises
    ValueError."""
    out = []
    for g in raw or []:
        paths, regen = (g or {}).get("paths"), (g or {}).get("regen")
        if not paths or not all(isinstance(p, str) and p and not p.startswith("/") for p in paths) \
                or (regen is not None and (not isinstance(regen, str) or not regen.strip())):
            raise ValueError(f"{repo}: a `generated` entry needs `paths: [<repo path or glob>, ...]` and, when it has "
                             f"one, `regen: <command>`: {g!r}")
        timeout = g.get("timeout")
        if timeout is not None and not (isinstance(timeout, (int, float)) and timeout > 0):
            raise ValueError(f"{repo}: a `generated` entry's `timeout` is seconds > 0: {g!r}")
        merge = g.get("merge", "rows")
        if merge not in ("rows", "regen") or (merge == "regen" and regen is None):
            raise ValueError(f"{repo}: a `generated` entry's `merge` is `rows` (the default) or `regen` (a SNAPSHOT: "
                             f"never merged by rows, always regenerated: it needs `regen`): {g!r}")
        out.append({"paths": list(paths), "regen": shlex.split(regen) if regen is not None else None, "timeout": timeout,
                    **({"merge": "regen"} if merge == "regen" else {})})
    return out


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
    """[(name, file)] of a watched repo's gate workflows: the registry's for a gated repo (gate_flows), its config's for
    the others."""
    if gated_repo(o_r):
        return gate_flows(o_r)
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
    workflows to read ([(name, file)]); default a gated repo's registry (gate_flows), else every workflow with a recent
    run on main."""
    flows = flows or (gate_flows(o_r) if gated_repo(o_r) else None)
    if not flows:
        # listing: only names the workflows to read; each is then read on its own (main_runs)
        recent = gh_json(o_r, "run", "list", "--branch", "main", "--limit", "40", "--json", RUN_FIELDS)
        flows = [(n, n) for n in dict.fromkeys(r["workflowName"] for r in recent)]
    out = []
    for name, wfile in flows:
        runs = main_runs(o_r, wfile)
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
            # listing: samples of a workflow's run lengths (the stuck limit); fewer samples widen it, never narrow it
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
    gated = {n for n, _ in red_gate_flows(o_r)}
    # listing: the newest 200 across workflows; a workflow absent from it is listed on its own below
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
                # listing: the workflow's own newest 30, read because the cross-workflow 200 left it out
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


RUN_VIEW_FIELDS = "jobs,headSha,url,number,status,conclusion,workflowName,workflowDatabaseId,attempt,createdAt,updatedAt"


def run_failed_jobs(d):
    """The failed jobs of a run view. A completed run's job GitHub left undecided (status in_progress, conclusion null:
    core #767, 2026-11-04) is failed when one of its steps failed."""
    done = d.get("status") == "completed"
    return [j for j in d.get("jobs") or [] if j.get("conclusion") in RED_CONCLUSIONS
            or (done and not j.get("conclusion") and failed_steps(j))]


def run_failed_attempt(o_r, rid, d):
    """(the run view to explain, its attempt when not the latest). THE FAILED ATTEMPT, NOT THE LATEST (issues
    20261108T070310-133a19, 20261119T024846-892cf2): a rerun replaces the run's jobs with its own -- green, or still
    running -- so the failure that made the red is read from the newest earlier attempt that has one."""
    latest = int(d.get("attempt") or 1)
    if run_failed_jobs(d) or latest <= 1:
        return d, None
    for n in range(latest - 1, 0, -1):
        try:
            dn = gh_json(o_r, "run", "view", str(rid), "--attempt", str(n), "--json", RUN_VIEW_FIELDS)
        except (Exception, SystemExit) as e:
            err(f"could not read run {rid}'s attempt {n}: {str(e)[:160]}")
            return d, None
        if run_failed_jobs(dn) or dn.get("conclusion") in RED_CONCLUSIONS:
            return dn, n
    return d, None


def job_annotations(o_r, job):
    """The failure-level check-run annotations of a job (GitHub's own words for a job with no log: `The job was not
    acquired by Runner of type hosted ...`, iOS #526), [] when none or unread."""
    jid = job.get("databaseId")
    if not jid:
        return []
    try:
        notes = gh_json(o_r, "api", f"repos/{o_r}/check-runs/{jid}/annotations") or []
    except (Exception, SystemExit):
        return []
    return [str(a.get("message") or a.get("title") or "").strip()[:300] for a in notes
            if isinstance(a, dict) and a.get("annotation_level") == "failure" and (a.get("message") or a.get("title"))]


def jobs_never_created(wf_jobs, run_jobs):
    """The names of the workflow's jobs (remote.workflow_jobs) the run has no job of (a matrix's or a called
    workflow's jobs start with the job's name; a name built from an expression is matched by what precedes it)."""
    names = [str(j.get("name") or "") for j in run_jobs]
    missing = []
    for wj in wf_jobs:
        key = str(wj.get("name") or "").split("${{")[0].strip()
        if key and not any(n == key or n.startswith(key + " ") for n in names):
            missing.append(wj["name"])
    return missing


GITHUB_STATUS_URL = "https://www.githubstatus.com/api/v2/incidents.json"


def github_incidents(since, until):
    """githubstatus.com incidents naming Actions that overlap since..until (+1 h): ['<name> (<shortlink>)']; [] when
    unread ($AGENTCTL_GITHUBSTATUS_URL names another source; empty turns it off)."""
    url = os.environ.get("AGENTCTL_GITHUBSTATUS_URL", GITHUB_STATUS_URL)
    if not url or not since:
        return []
    try:
        with urllib.request.urlopen(url, timeout=8) as f:
            incidents = json.load(f).get("incidents") or []
        lo = dt.datetime.fromisoformat(str(since).replace("Z", "+00:00"))
        hi = dt.datetime.fromisoformat(str(until or since).replace("Z", "+00:00")) + dt.timedelta(hours=1)
    except (Exception, SystemExit):
        return []
    out = []
    for i in incidents:
        words = " ".join([str(i.get("name"))] + [str(c.get("name")) for c in i.get("components") or []])
        try:
            start = dt.datetime.fromisoformat(str(i.get("created_at")))
            end = dt.datetime.fromisoformat(str(i["resolved_at"])) if i.get("resolved_at") else None
        except (ValueError, KeyError):
            continue
        if "Actions" in words and start <= hi and (end is None or end >= lo):
            out.append(f"{i.get('name')} ({i.get('shortlink') or i.get('id')})")
    return out


def never_created_lines(o_r, d):
    """(job label, why) when every job a failed run created passed: GitHub failed the run itself (core #911,
    2026-11-09: promote-image never created during an Actions incident). The jobs the workflow file at the run's
    commit names that the run lacks, and the githubstatus.com Actions incidents of its window."""
    missing = []
    try:
        path = (gh_json(o_r, "api", f"repos/{o_r}/actions/workflows/{d['workflowDatabaseId']}") or {}).get("path")
        body = gh_json(o_r, "api", f"repos/{o_r}/contents/{path}?ref={d.get('headSha')}") if path else {}
        import base64
        txt = base64.b64decode((body or {}).get("content") or "").decode(errors="replace")
        missing = jobs_never_created(workflow_jobs(txt), d.get("jobs") or [])
    except (Exception, SystemExit) as e:
        err(f"could not read run's workflow file ({str(e)[:120]})")
    incidents = github_incidents(d.get("createdAt"), d.get("updatedAt"))
    why = ("GitHub-side: every job the run created passed, yet the run failed"
           + (f"; never created: {', '.join(missing)}" if missing else "; a job GitHub never created?")
           + (f"; githubstatus: {'; '.join(incidents)}" if incidents else "") + " -- not a code red")
    label = f"never created: {', '.join(missing)}" if missing else "run failed with every job green"
    return label, why


def explain_run(o_r, run, tail, repo=None):
    """Lines explaining one red run: commit, failed jobs, their log tails, infra vs test."""
    lines, num, _per_job = explain_run_jobs(o_r, run, tail, repo)
    return lines, num


def explain_run_jobs(o_r, run, tail, repo=None):
    """(lines, run number, [(failed job, infra|unclear|test, its first relevant line)])."""
    rid = run["databaseId"]
    latest = gh_json(o_r, "run", "view", str(rid), "--json", RUN_VIEW_FIELDS)
    d, attempt = run_failed_attempt(o_r, rid, latest)
    num = d.get("number") or run.get("number")
    failed = run_failed_jobs(d)
    annotated = {}  # a cancelled job's (or a log-less failed one's) failure annotations: GitHub's own cause
    if not failed and d.get("status") == "completed" and d.get("conclusion") in RED_CONCLUSIONS:
        for j in [j for j in d.get("jobs") or [] if j.get("conclusion") == "cancelled"][:MAX_JOB_LOG_FETCHES]:
            notes_ = job_annotations(o_r, j)
            if notes_:
                failed.append(j)
                annotated[j["name"]] = notes_
    unread, notes = [], []  # why the failed log, and a job's own, could not be read: no log was read, so none is 0-length
    no_verdict = jobs_no_verdict(d.get("jobs"))
    logged = [j for j in failed if j["name"] not in annotated]
    try:
        log = failed_jobs_log(o_r, rid, logged, attempt, notes) if logged and not no_verdict else ""
    except Exception as e:
        log = ""
        unread.append(str(e))
        err(f"could not read run {rid}'s failed log: {e}")
    strong_jobs = set()
    per, raws, timed = relevant_lines(log, tail, strong_jobs), raw_tails(log), timed_out_jobs(log)
    lines = [f"  run id {rid} (#{num}) {d.get('url') or run.get('url')}",
             f"  commit {commit_line(o_r, d.get('headSha') or run.get('headSha'), repo)}"]
    if attempt:
        lines.append(f"  read attempt {attempt}, the failed one: attempt {latest.get('attempt')} (the latest) is "
                     f"{latest.get('conclusion') or latest.get('status')}")
    kinds, fetched, per_job, regressions = [], 0, [], []
    for j in failed:
        if j["name"] in annotated:
            jl = annotated[j["name"]]
            kind, why = classify_job(j, jl, (0, []))
            if kind == "test" and not failed_steps(j):  # a job that ran no step tested nothing
                kind = "unclear"
            kinds.append(kind)
            per_job.append((j["name"], kind, jl[0]))
            lines.append(f"  [{kind}] {j['name']} ({j.get('conclusion')}): {why}")
            lines += [f"      annotation: {l}" for l in jl]
            continue
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
        if not jl and not setup and not unread and fetched < MAX_JOB_LOG_FETCHES:
            fetched += 1
            jl = job_annotations(o_r, j)  # no log line at all (0 steps, no runner): GitHub's annotations
        kind, why = classify_job(j, jl, raws.get(j["name"], (0, [])), j["name"] in timed or any(STEP_TIMED_OUT in l for l in jl))
        # THE ORDER VERDICT (classify.py): a control pass's REGRESSION / ORDER line keys the red and says what it is
        ov = order_verdict(l for jn, l in log_entries(log) if jn == j["name"]) if log and kind == "test" else None
        if ov:
            jl = [ov] + [l for l in jl if ORDER_VERDICT.search(l) is None]
            why = f"{why}; {'REGRESSION, not an order dependence' if is_regression(ov) else 'an order dependence (or a flake)'}"
            regressions += [j["name"]] if is_regression(ov) else []
        if unread and not jl and not setup:
            kind, why = "unclear", (f"LOG UNREADABLE ({unread[-1]}): no log was read, so nothing says infra or test -- "
                                    f"read `gh run view {rid} --log-failed` again; never rerun on this alone")
        cut = next((n for n in notes if n.startswith("LOG TRUNCATED") and f" {j['name']}'s " in f" {n}"), None)
        if cut and kind == "unclear":
            why = f"{cut}: the log was cut short, nothing in it says infra or test -- read again, never rerun on this alone"
            note = note or f"      ({cut})"
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
    if not failed and d.get("status") == "completed" and d.get("conclusion") in RED_CONCLUSIONS:
        label, why = never_created_lines(o_r, d)
        kinds.append("infra")
        per_job.append((label, "infra", why))
        lines.append(f"  [infra] {label}: {why}")
    for n in notes:
        if n.startswith("LOG UNREADABLE") and not any(n in l for l in lines):
            lines.append(f"      ({n}; read again next time)")
    rerun = f"rerun: {GH_CLI.replace(HOME, '~')} run rerun {rid} --failed -R {o_r}"
    if no_verdict:
        verdict = ("NO VERDICT (an incomplete run: only `verdict complete` failed -- a takes or takes-drift row was "
                   "skipped, cancelled or missing; not a test failure, no fixer: the next complete run decides)")
    elif any(l.startswith("  [test]") and ": TIMEOUT: " in l for l in lines):
        verdict = "TIMEOUT (a step ran past its own timeout-minutes: a code/config red, not a rerun)"
    elif "test" in kinds and regressions:
        verdict = (f"TEST FAILURE: REGRESSION, not an order dependence ({', '.join(regressions)}: the failing tests fail in "
                   "default order too, at the same commit -- a change broke them)")
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


# A RUN NUMBER IS WHAT EVERY RED PRINTS (issue 20261031T085600-c1cbf8: `ci-why 2939` for red Main #2939 explained run
# id 2939, another run). GitHub's run ids are ten digits and more; a run number is a workflow's own count. So a target
# below RUN_ID_MIN is a run NUMBER, resolved on main -- the gate's workflows first, then a red run -- and the others
# that carry it are named; anything larger is a run id.
RUN_ID_MIN = 10 ** 8


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
    try:
        # listing: a run number's hits in the newest 200; each gate workflow not hit is asked on its own below
        take(gh_json(o_r, "run", "list", "--branch", "main", "--limit", "200", "--json", RUN_FIELDS))
    except (Exception, SystemExit) as e:
        err(f"ci-why: could not list main's runs ({str(e)[:120]})")
    for name, wfile in red_gate_flows(o_r):
        if any(r.get("workflowName") == name for r in hits):
            continue
        try:
            newest = main_runs(o_r, wfile, limit=1)
            gap = int(newest[0]["number"]) - int(n) if newest else -1
            if 0 <= gap < 1000:
                take(main_runs(o_r, wfile, limit=min(1000, gap + 50)))
        except (Exception, SystemExit, ValueError, KeyError):
            continue
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


def ci_why(o_r, tail=6, run_id=None, repo=None, summary=False, snap=None):
    """(red, text). run_id: explain that one run; else the gating workflows' newest decided runs on main, each red row
    followed by who holds it (`fixing: <its state, holder or offer>`; never NOBODY). summary: text is the status line's one line."""
    if run_id:
        rid, said = run_target(o_r, run_id)
        try:
            lines, _ = explain_run(o_r, {"databaseId": int(rid)}, tail, repo)
        except RuntimeError as e:
            if "HTTP 404" not in str(e):
                raise
            lines = [f"  no such run: {str(e)[:200]}"]
        return True, f"run {o_r}#{rid}\n" + "".join(f"  {l}\n" for l in said) + "\n".join(lines)
    out, red, rows, units_of, parts, untracked = [], False, [], {}, [], {}
    gate = gate_from_snap(snap) if snap else gate_runs(o_r)
    others, others_err = [], None
    if snap:
        others, others_err = outside_from_snap(snap), snap.get("outside_error")
    elif gated_repo(o_r):
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
        rows = [(n, d, _gate_lists.get(n, []), not gate_report_only(o_r, n, d)[0]) for n, d, _nw, _s in gate]
        units = gate_red_units(o_r, rows, [(n, r, _outside_lists.get(n, []), False) for n, r, _s, _c in others],
                               rereads=0 if summary else None)
        units += local_units(lrepo) + predict_units(o_r, bsnap)
        for u in units:
            units_of.setdefault((u["workflow"], u["run"].get("databaseId")), []).append(u)
        store = Store()
        states = red_view_of_units(store, units)
        flakes_ = [it for it in red_active(store).values() if it["data"].get("red_kind") == "flake"]
        untracked = red_untracked_runs(store, o_r, units)
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
    never = [o for o in others if (o[0], o[1].get("databaseId")) in untracked]  # never green on main: untracked
    others = [o for o in others if o not in never]

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
        report, why = gate_report_only(o_r, name, decided)
        if report:  # red only in checks the registry calls unproven: it reports, it holds no push
            out.append(f"{name}: RED, REPORT ONLY (run {decided['databaseId']} #{decided.get('number')}, "
                       f"{decided['conclusion']}): {why} ({GATING_CHECKS_FILE}) -- holds no push")
            out += fixing(name, decided)[0]
            parts.append(f"{short_flow(name)} report-only")
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
    if never:
        out.append("Untracked (never green on main: no red, no fixer, no alert until its first green run):")
        for name, r, _since, _count in never:
            out.append(f"  {name}: newest run {r['databaseId']} #{r.get('number')}: "
                       f"{untracked[(name, r.get('databaseId'))]}")
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
    if gated_repo(o_r):
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
