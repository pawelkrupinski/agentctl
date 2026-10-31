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
    installation token), and never a prompt or an update check."""
    env = dict(os.environ if env is None else env)
    env.update(GH_PROMPT_DISABLED="1", GH_NO_UPDATE_NOTIFIER="1", NO_COLOR="1")
    if ghapp_enabled():  # ci-runner: the repo's App installation token (ghapp.py), never a person's
        env["GH_TOKEN"] = ghapp_token(o_r)
    return env


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


def _gh_api_once(o_r, method, path, params=None, body=None):
    url = f"repos/{o_r}/{path}" + ("?" + urllib.parse.urlencode(params) if params else "")
    argv = [GH_CLI, "api", "-X", method, "-H", "Accept: application/vnd.github+json", url]
    if body is not None:
        argv += ["--input", "-"]
    r = subprocess.run(argv, capture_output=True, text=True, env=forge_env(o_r), timeout=API_TIMEOUT,
                       input=json.dumps(body) if body is not None else None)
    if r.returncode != 0:
        raise ForgeHTTPError(f"gh api {method} {url}: {(r.stderr or r.stdout).strip()[:300]}")
    return json.loads(r.stdout) if r.stdout.strip() else None


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
