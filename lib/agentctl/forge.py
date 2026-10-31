# agentctl, part `forge`: THE FORGE agentctl reads CI from -- Forgejo (the forgejo-gh shim and /api/v1) or GitHub (the
# real `gh` CLI and api.github.com through `gh api`).
# Runs inside agentctl's one namespace, after the parts before it in lib/agentctl/__init__.py's PARTS (it uses their names).

# ---------------------------------------------------------------- forge: the read side (github-return plan §4.1, W5a)
# ONE SWITCH, `forge` in agentctl.toml ($AGENTCTL_FORGE): forgejo (the default until Phase 3 flips it) or github. Every
# run list, run view, failed log, artifact, dispatch, cancel and actions-API read goes through the functions below,
# which pick the forge at EVERY call (a test or a config change applies at once):
#   gh_json / forge_argv / forge_env    the CLI: FORGEJO_GH (with the vault's Forgejo token, through the tunnel retry)
#                                       or GH_CLI (gh's own auth: $GH_TOKEN or `gh auth login`; no tunnel)
#   api                                 /api/v1 over urllib, or `gh api` (the same repo-relative paths where they agree)
#   forge_branches / forge_branch_sha / forge_delete_branch / forge_actions_runs / forge_actions_run /
#   forge_workflow_file / forge_release_asset / forge_run_url
#                                       the calls whose paths or answers differ: the GitHub side answers in the
#                                       Forgejo shape the callers were written against
# The push remote is core's remote(); the landing identity on ci-runner is ghapp.py's (github-app).
FORGE_DEFAULT = "github"  # the forge agentctl reads runs, logs, artifacts and dispatches through (Forgejo was switched off 2026-10-31)
FORGES = ("forgejo", "github")
GH_CLI = os.environ.get("AGENTCTL_GH_CLI", "gh")  # the GitHub CLI forge = github runs
GITHUB_WEB = "https://github.com"
FORGEJO_WEB = "http://localhost:3000"


def forge():
    """'forgejo' or 'github' ($AGENTCTL_FORGE / agentctl.toml `forge`); an unknown name stops the command."""
    f = (os.environ.get("AGENTCTL_FORGE", FORGE_DEFAULT) or FORGE_DEFAULT).strip().lower()
    if f not in FORGES:
        raise SystemExit(f"agentctl: forge = {f!r} is not one of {', '.join(FORGES)} ($AGENTCTL_FORGE / agentctl.toml)")
    return f


def on_github():
    return forge() == "github"


def forge_cli():
    """The CLI that reads this forge's runs: gh or the forgejo-gh shim."""
    return GH_CLI if on_github() else FORGEJO_GH


def forge_cli_name():
    return "gh" if on_github() else "forgejo-gh"


def forge_env(o_r, env=None):
    """The CLI's environment: Forgejo's token from the vault as GH_TOKEN (what the shim reads); on GitHub gh's own auth
    ($GH_TOKEN as set, else `gh auth login`'s) and never a prompt or an update check."""
    env = dict(os.environ if env is None else env)
    if on_github():
        env.update(GH_PROMPT_DISABLED="1", GH_NO_UPDATE_NOTIFIER="1", NO_COLOR="1")
        if ghapp_enabled():  # ci-runner: the repo's App installation token (ghapp.py), never a person's
            env["GH_TOKEN"] = ghapp_token(o_r)
        return env
    env["GH_TOKEN"] = token(o_r)
    return env


def forge_argv(o_r, *args):
    """[cli, *args, -R o_r]: `gh api` takes no -R (its path names the repo), the shim and every other gh command do."""
    if on_github() and args and args[0] == "api":
        return [GH_CLI, *args]
    return [forge_cli(), *args, "-R", o_r]


def forge_retry(fn):
    """A forge read: through the tunnel retry on Forgejo (localhost:3000 is the tunnel), plainly on GitHub."""
    return fn() if on_github() else tunnel_retry(fn)


def forge_run_url(o_r, number=None, run=None):
    """A run's web page: Forgejo links by run NUMBER, GitHub by run id (the run's own url when it carries one)."""
    if on_github():
        run = run or {}
        if run.get("url") or run.get("html_url"):
            return run.get("url") or run.get("html_url")
        rid = run.get("databaseId") or run.get("id")
        return f"{GITHUB_WEB}/{o_r}/actions/runs/{rid}" if rid not in (None, "") else None
    return f"{FORGEJO_WEB}/{o_r}/actions/runs/{number}" if number not in (None, "") else None


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


# -- GitHub's answers in the Forgejo shape
def github_run_status(status, conclusion):
    """A GitHub run's (status, conclusion) as Forgejo's one run status (success, failure, cancelled, skipped, running,
    waiting)."""
    if status != "completed":
        return "running" if status == "in_progress" else "waiting"
    return {"success": "success", "neutral": "success", "skipped": "skipped", "cancelled": "cancelled",
            "stale": "cancelled", "action_required": "waiting"}.get(conclusion, "failure")


def github_run_as_forgejo(r):
    return {"id": r.get("id"), "index_in_repo": r.get("run_number"), "run_number": r.get("run_number"),
            "commit_sha": r.get("head_sha"), "prettyref": r.get("head_branch"),
            "status": github_run_status(r.get("status"), r.get("conclusion")), "html_url": r.get("html_url"),
            "workflow_id": os.path.basename(r.get("path") or "") or r.get("workflow_id"), "event": r.get("event"),
            "event_payload": None}


def forge_actions_runs(o_r, workflow=None, run_number=None, limit=50):
    """The newest action runs, Forgejo-shaped ({id, index_in_repo, commit_sha, prettyref, status, html_url}), optionally
    of one workflow file or with one run number."""
    if not on_github():
        params = {"limit": limit}
        if workflow:
            params["workflow_id"] = workflow
        if run_number is not None:
            params["run_number"] = int(run_number)
        return (api(o_r, "GET", "actions/runs", params) or {}).get("workflow_runs") or []
    path = f"actions/workflows/{urllib.parse.quote(workflow, safe='')}/runs" if workflow else "actions/runs"
    runs = [github_run_as_forgejo(r) for r in
            (api(o_r, "GET", path, {"per_page": 100 if run_number is not None else min(int(limit), 100)}) or {})
            .get("workflow_runs") or []]
    if run_number is not None:  # GitHub filters runs by no number: the newest 100, matched here
        runs = [r for r in runs if str(r["run_number"]) == str(run_number)]
    return runs[:int(limit)]


def forge_actions_run(o_r, rid):
    """One action run, Forgejo-shaped (with Forgejo's event_payload; None on GitHub, which serves no inputs)."""
    r = api(o_r, "GET", f"actions/runs/{rid}") or {}
    return github_run_as_forgejo(r) if on_github() and r else r


def forge_branches(o_r, prefix=""):
    """[(name, sha, commit time)] of the forge's branches starting with prefix (GitHub: the time costs one commit read
    per matching branch, so only matching ones are read)."""
    out, page, per = [], 1, 50 if not on_github() else 100
    while True:
        params = {"limit": per, "page": page} if not on_github() else {"per_page": per, "page": page}
        bs = api(o_r, "GET", "branches", params) or []
        for b in bs:
            if not b["name"].startswith(prefix):
                continue
            if on_github():
                sha = b["commit"]["sha"]
                c = api(o_r, "GET", f"commits/{sha}") or {}
                when = ((c.get("commit") or {}).get("committer") or {}).get("date")
            else:
                sha, when = b["commit"]["id"], b["commit"]["timestamp"]
            out.append((b["name"], sha, parse_time(when)))
        if len(bs) < per:
            return out
        page += 1


def forge_branch_sha(o_r, name):
    c = api(o_r, "GET", f"branches/{urllib.parse.quote(name, safe='')}")["commit"]
    return c["sha"] if on_github() else c["id"]


def forge_delete_branch(o_r, name):
    """Delete a branch through the API (GitHub deletes the ref)."""
    q = urllib.parse.quote(name, safe="")
    if on_github():
        api(o_r, "DELETE", f"git/refs/heads/{urllib.parse.quote(name, safe='/')}")
    else:
        api(o_r, "DELETE", f"branches/{q}")


def forge_workflow_file(o_r, view):
    """The workflow FILE (basename) of a `run view --json workflowDatabaseId` answer: the shim serves the file there,
    GitHub the workflow's numeric id (read once more for its path)."""
    wf = view.get("workflowDatabaseId")
    if not on_github() or wf in (None, "") or str(wf).endswith((".yml", ".yaml")):
        return wf
    try:
        return os.path.basename((gh_json(o_r, "api", f"repos/{o_r}/actions/workflows/{wf}") or {}).get("path") or "") or None
    except (Exception, SystemExit):
        return None


def forge_release_asset(o_r, tag, name):
    """A release asset's bytes."""
    if on_github():
        r = subprocess.run([GH_CLI, "release", "download", tag, "-p", name, "-O", "-", "-R", o_r], capture_output=True,
                           env=forge_env(o_r), timeout=60)
        if r.returncode != 0:
            raise ForgeHTTPError(f"gh release download {tag} {name}: {r.stderr.decode(errors='replace').strip()[:200]}")
        return r.stdout
    base = API.split("/api/")[0]
    url = f"{base}/{o_r}/releases/download/{urllib.parse.quote(tag)}/{name}"
    req = urllib.request.Request(url, headers={"Authorization": f"token {token(o_r)}"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        return resp.read()
