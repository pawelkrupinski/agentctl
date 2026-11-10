# agentctl, part `forge`: THE FORGE agentctl reads CI from -- GitHub, through the real `gh` CLI and api.github.com
# (`gh api`). Forgejo was switched off on 2026-10-31; GitHub is the only forge.
# Runs inside agentctl's one namespace, after the parts before it in lib/agentctl/__init__.py's PARTS (it uses their names).

# ---------------------------------------------------------------- forge: the read side
# Every run list, run view, failed log, artifact, dispatch, cancel and actions-API read goes through the functions below:
#   gh_json / forge_argv / forge_env    the CLI: GH_CLI (gh's own auth: $GH_TOKEN or `gh auth login`; on ci-runner the
#                                       repo's App installation token, ghapp.py)
#   api                                 `gh api` under repos/<o_r>/
#   forge_branches / forge_branch_sha / forge_delete_branch / forge_actions_runs / forge_actions_run /
#   forge_release_asset / forge_run_url
#                                       the calls whose answers are folded into agentctl's run record (run_record)
# The push remote is core's remote(); the landing identity on ci-runner is ghapp.py's (github-app).
GH_CLI = os.environ.get("AGENTCTL_GH_CLI", "gh")  # the GitHub CLI
GITHUB_WEB = "https://github.com"


def forge_env(o_r, env=None):
    """The CLI's environment: gh's own auth ($GH_TOKEN as set, else `gh auth login`'s; on ci-runner the repo's App
    installation token), and never a prompt or an update check. Every call is one gh process (or, with `env`, a token
    handed to a child process), tallied against its caller (gh_tally)."""
    gh_tally("token handoff" if env is not None else None)
    env = dict(os.environ if env is None else env)
    env.update(GH_PROMPT_DISABLED="1", GH_NO_UPDATE_NOTIFIER="1", NO_COLOR="1")
    if ghapp_enabled():  # ci-runner: the repo's App installation token (ghapp.py), never a person's
        env["GH_TOKEN"] = ghapp_token(o_r)
    return env


# THE QUOTA TALLY (2026-11-10): the kinowo-lander installation's 5,000 requests an hour ran out on 11-09 23:00-23:50Z, and
# at a quiet 03:27-03:31Z on 11-10 it spent ~2,500 an hour, ~960 of them the gate refresh's `gh run list` reads
# (dcde282). Nothing said where the rest went: ci-runner hides other users' processes, and no call was logged. Every
# gh process (forge_env) and every REST GET answered 304 (free) is now counted by the agentctl function that asked
# and what it ran, and each process prints its tally every GH_TALLY_SECONDS:
#   agentctl: github calls (pid P, 300 s): 41 gh processes, 12 of them 304 (free): 20 red_tick>run_jobs: gh run view ...
GH_TALLY_SECONDS = 300
GH_TALLY_PLUMBING = {"forge_env", "gh_tally", "gh_json", "api", "_gh_api_once", "forge_run_list", "forge_actions_runs",
                     "forge_actions_run", "forge_branches", "forge_branch_sha", "forge_delete_branch",
                     "forge_release_asset", "tool_env", "drift_env",
                     "<lambda>", "<listcomp>", "<dictcomp>", "<genexpr>", "wrapper", "inner"}
_gh_tally_lock = threading.Lock()
_gh_tally_state = {"since": time.time(), "calls": {}, "free": 0}
_gh_tally_kind = threading.local()


def gh_tally_site():
    """`outer>inner`: the two nearest agentctl functions above the forge plumbing that made the call."""
    names, f = [], sys._getframe(1)
    while f is not None and len(names) < 2:
        if f.f_code.co_name not in GH_TALLY_PLUMBING and f.f_code.co_name != "<module>":
            names.append(f.f_code.co_name)
        f = f.f_back
    return ">".join(reversed(names)) or "?"


def gh_tally(kind=None, free=False, now_=None):
    """Count one gh process (or, `free`, mark the last one a 304) against its caller, and print the tally once
    GH_TALLY_SECONDS have passed since the last. `kind` names what ran; None: what gh_json/_gh_api_once said."""
    kind = kind or getattr(_gh_tally_kind, "what", None) or "gh"
    t = time.time() if now_ is None else now_
    with _gh_tally_lock:
        st = _gh_tally_state
        if free:
            st["free"] += 1
        else:
            key = f"{gh_tally_site()}: {kind}"
            st["calls"][key] = st["calls"].get(key, 0) + 1
        if t - st["since"] < GH_TALLY_SECONDS:
            return
        calls, freed, span = st["calls"], st["free"], t - st["since"]
        st.update(since=t, calls={}, free=0)
    if calls:
        top = sorted(calls.items(), key=lambda kv: -kv[1])
        err(f"github calls (pid {os.getpid()}, {span:.0f} s): {sum(calls.values())} gh processes, {freed} of them 304 "
            f"(free): " + "; ".join(f"{n} {k}" for k, n in top[:12]) + (f"; {len(top) - 12} more sites" if len(top) > 12 else ""))


@contextlib.contextmanager
def gh_tally_as(what):
    """What the gh process forge_env is about to count runs (gh_json's subcommand, a REST path's shape)."""
    before, _gh_tally_kind.what = getattr(_gh_tally_kind, "what", None), what
    try:
        yield
    finally:
        _gh_tally_kind.what = before


def gh_tally_shape(path):
    """A REST path or gh argument with its ids and shas folded: runs/123/jobs -> runs/N/jobs."""
    return re.sub(r"\b[0-9a-f]{40}\b", "SHA", re.sub(r"\b\d+\b", "N", path.split("?")[0]))[:80]


def forge_argv(o_r, *args):
    """[gh, *args, -R o_r]: `gh api` takes no -R (its path names the repo), every other gh command does."""
    if args and args[0] == "api":
        return [GH_CLI, *args]
    return [GH_CLI, *args, "-R", o_r]


def forge_run_url(o_r, number=None, run=None):
    """A run's web page: by run id (the run's own url when it carries one)."""
    run = run or {}
    if run.get("url") or run.get("html_url"):
        return run.get("url") or run.get("html_url")
    rid = run.get("databaseId") or run.get("id")
    return f"{GITHUB_WEB}/{o_r}/actions/runs/{rid}" if rid not in (None, "") else None


# -- `gh api` as api()'s GitHub side
class ForgeHTTPError(RuntimeError):
    """`gh api` refused: its stderr (gh names the status there: `HTTP 404`)."""


# CONDITIONAL GETS (simplification-2026-10-31 item 4): every agent and the runners page share one 5,000-an-hour budget,
# and a GET GitHub answers 304 Not Modified to (If-None-Match: the ETag it sent last) costs none of it. Every GET keeps
# its last answer and ETag under <cache>/etag/; an unchanged resource is read from there.
def etag_path(url):
    import hashlib
    return state_root("etag", hashlib.sha1(url.encode()).hexdigest() + ".json")


def _split_response(text):
    """(status code, {header lower: value}, body) of `gh api -i` output."""
    head, _, body = text.replace("\r\n", "\n").partition("\n\n")
    lines = head.splitlines()
    m = re.match(r"HTTP/\S+ (\d+)", lines[0]) if lines else None
    hdrs = {k.strip().lower(): v.strip() for k, _, v in (l.partition(":") for l in lines[1:]) if k}
    return (int(m.group(1)) if m else None), hdrs, body


def _gh_api_once(o_r, method, path, params=None, body=None):
    url = f"repos/{o_r}/{path}" + ("?" + urllib.parse.urlencode(params) if params else "")
    argv = [GH_CLI, "api", "-X", method, "-H", "Accept: application/vnd.github+json", url]
    cached = read_record(etag_path(url)) if method == "GET" else None
    if method == "GET":
        argv[2:2] = ["-i"] + (["-H", f"If-None-Match: {cached['etag']}"] if cached and cached.get("etag") else [])
    if body is not None:
        argv += ["--input", "-"]
    with gh_tally_as(f"api {method} {gh_tally_shape(path)}"):
        env = forge_env(o_r)
    r = subprocess.run(argv, capture_output=True, text=True, env=env, timeout=API_TIMEOUT,
                       input=json.dumps(body) if body is not None else None)
    if method != "GET":
        if r.returncode != 0:
            raise ForgeHTTPError(f"gh api {method} {url}: {(r.stderr or r.stdout).strip()[:300]}")
        return json.loads(r.stdout) if r.stdout.strip() else None
    status, hdrs, text = _split_response(r.stdout)
    if status == 304 and cached is not None:
        gh_tally(free=True)
        return cached.get("body")
    if r.returncode != 0:
        raise ForgeHTTPError(f"gh api {method} {url}: {(r.stderr or text or r.stdout).strip()[:300]}")
    out = json.loads(text) if text.strip() else None
    if hdrs.get("etag"):
        try:
            write_json(etag_path(url), {"etag": hdrs["etag"], "body": out})
        except OSError:
            pass
    return out


# -- `gh run list` as conditional REST reads (2026-11-10)
# THE INSTALLATION QUOTA: on ci-runner every read of movies is the kinowo-lander App installation's, 5,000 an hour, shared
# with the external scheduler on monitoring-1 (movies infra/nix/files/scheduled_dispatch.py). It was exhausted on
# 2026-11-09 ~23:00-23:50Z, and the nightly recording's slot was lost; at 03:27-03:31Z on 11-10, with the fleet quiet,
# it was still spending ~42 requests a minute (~2,500 an hour, sampled from GET /rate_limit). The biggest share is the
# gate refresh: every 45 s, and within a tick of every push to main, five `gh run list` reads, and `gh run list` sends
# no If-None-Match (every call is charged) and costs two or three requests each: a `workflows/<file>` lookup before a
# `--workflow` list, a `workflows` page to name the runs, and a page per 100 runs. Read here instead, through api()'s
# conditional GET, a list GitHub has not changed since the last read is a 304 and free, and the lookups are gone.
# Only what this answers exactly is answered here: a --workflow that is a file name (a NAME needs gh's lookup), and the
# options below; anything else returns None and gh answers it.
RUN_LIST_FIELDS = {"databaseId": "id", "number": "run_number", "status": "status", "headSha": "head_sha",
                   "url": "html_url", "displayTitle": "display_title", "workflowName": "name",
                   "createdAt": "created_at", "updatedAt": "updated_at", "startedAt": "run_started_at",
                   "event": "event", "headBranch": "head_branch", "attempt": "run_attempt",
                   "workflowDatabaseId": "workflow_id"}
RUN_LIST_STATUSES = {"queued", "completed", "in_progress", "requested", "waiting", "pending", "action_required",
                     "cancelled", "failure", "neutral", "skipped", "stale", "startup_failure", "success", "timed_out"}


def _run_list_options(args):
    """{option: value} of a `run list` argv gh_json may answer from REST, or None."""
    opts, i, rest = {}, 0, list(args[2:])
    names = {"--workflow": "workflow", "-w": "workflow", "--branch": "branch", "-b": "branch", "--limit": "limit",
             "-L": "limit", "--json": "json", "--status": "status", "-s": "status"}
    while i < len(rest):
        name = names.get(rest[i])
        if name is None or i + 1 >= len(rest) or name in opts:
            return None
        opts[name] = rest[i + 1]
        i += 2
    if "json" not in opts or any(f not in RUN_LIST_FIELDS and f != "conclusion" for f in opts["json"].split(",")):
        return None
    if "workflow" in opts and not opts["workflow"].endswith((".yml", ".yaml")):
        return None
    if "status" in opts and opts["status"] not in RUN_LIST_STATUSES:
        return None
    try:
        opts["limit"] = int(opts.get("limit", 20))
    except ValueError:
        return None
    return opts if opts["limit"] > 0 else None


def forge_run_list(o_r, args):
    """`gh run list ...` (args from `run`), as gh prints it with --json, read through api(): None when it is not one
    this answers (gh is asked instead). A workflow file not on the default branch raises gh's own words for it."""
    opts = _run_list_options(args)
    if opts is None:
        return None
    path = (f"actions/workflows/{urllib.parse.quote(opts['workflow'], safe='')}/runs" if "workflow" in opts
            else "actions/runs")
    params = {"exclude_pull_requests": "true"}
    params.update({k: opts[k] for k in ("branch", "status") if k in opts})
    fields, runs, page = opts["json"].split(","), [], 1
    while len(runs) < opts["limit"]:
        per = min(100, opts["limit"]) if opts["limit"] <= 100 else 100
        try:
            answer = api(o_r, "GET", path, dict(params, per_page=per, page=page)) or {}
        except ForgeHTTPError as e:
            if "workflow" in opts and re.search(r"HTTP 404|Not Found", str(e)):
                raise RuntimeError(f"gh run list: HTTP 404: workflow {opts['workflow']} not found on the default "
                                   f"branch (https://api.github.com/repos/{o_r}/{path})") from None
            raise RuntimeError(f"gh run list: {e}") from None
        batch = answer.get("workflow_runs") or []
        runs += batch
        if len(batch) < per:
            break
        page += 1

    def shaped(r):
        out = {f: r.get(RUN_LIST_FIELDS[f]) for f in fields if f in RUN_LIST_FIELDS}
        if "conclusion" in fields:
            out["conclusion"] = r.get("conclusion") or ""  # gh prints a pending run's conclusion as ""
        return out

    return [shaped(r) for r in runs[:opts["limit"]]]


# -- GitHub's runs as agentctl's run record
def github_run_status(status, conclusion):
    """A GitHub run's (status, conclusion) as one run status (success, failure, cancelled, skipped, running, waiting)."""
    if status != "completed":
        return "running" if status == "in_progress" else "waiting"
    return {"success": "success", "neutral": "success", "skipped": "skipped", "cancelled": "cancelled",
            "stale": "cancelled", "action_required": "waiting"}.get(conclusion, "failure")


def run_record(r):
    """A GitHub actions run as the record agentctl reads: GitHub's own fields, its status folded into one word, and
    workflow_id the workflow FILE's basename."""
    return {"id": r.get("id"), "run_number": r.get("run_number"), "head_sha": r.get("head_sha"),
            "head_branch": r.get("head_branch"), "status": github_run_status(r.get("status"), r.get("conclusion")),
            "html_url": r.get("html_url"), "event": r.get("event"),
            "workflow_id": os.path.basename(r.get("path") or "") or r.get("workflow_id")}


def forge_actions_runs(o_r, workflow=None, run_number=None, limit=50):
    """The newest action runs (run_record), optionally of one workflow file or with one run number."""
    path = f"actions/workflows/{urllib.parse.quote(workflow, safe='')}/runs" if workflow else "actions/runs"
    runs = [run_record(r) for r in
            (api(o_r, "GET", path, {"per_page": 100 if run_number is not None else min(int(limit), 100)}) or {})
            .get("workflow_runs") or []]
    if run_number is not None:  # GitHub filters runs by no number: the newest 100, matched here
        runs = [r for r in runs if str(r["run_number"]) == str(run_number)]
    return runs[:int(limit)]


def forge_actions_run(o_r, rid):
    """One action run (run_record); {} when GitHub has none."""
    r = api(o_r, "GET", f"actions/runs/{rid}") or {}
    return run_record(r) if r else {}


def forge_branches(o_r, prefix=""):
    """[(name, sha, commit time)] of the repo's branches starting with prefix (the time costs one commit read per
    matching branch, so only matching ones are read)."""
    out, page, per = [], 1, 100
    while True:
        bs = api(o_r, "GET", "branches", {"per_page": per, "page": page}) or []
        for b in bs:
            if not b["name"].startswith(prefix):
                continue
            sha = b["commit"]["sha"]
            c = api(o_r, "GET", f"commits/{sha}") or {}
            when = ((c.get("commit") or {}).get("committer") or {}).get("date")
            out.append((b["name"], sha, parse_time(when)))
        if len(bs) < per:
            return out
        page += 1


def forge_branch_sha(o_r, name):
    return api(o_r, "GET", f"branches/{urllib.parse.quote(name, safe='')}")["commit"]["sha"]


def forge_delete_branch(o_r, name):
    """Delete a branch through the API (its ref)."""
    api(o_r, "DELETE", f"git/refs/heads/{urllib.parse.quote(name, safe='/')}")


def forge_release_asset(o_r, tag, name):
    """A release asset's bytes."""
    r = subprocess.run([GH_CLI, "release", "download", tag, "-p", name, "-O", "-", "-R", o_r], capture_output=True,
                       env=forge_env(o_r), timeout=60)
    if r.returncode != 0:
        raise ForgeHTTPError(f"gh release download {tag} {name}: {r.stderr.decode(errors='replace').strip()[:200]}")
    return r.stdout
