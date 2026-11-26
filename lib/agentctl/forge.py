# agentctl, part `forge`: THE FORGE agentctl reads CI from -- GitHub, through the real `gh` CLI and api.github.com
# (`gh api`). Forgejo was switched off on 2026-10-31; GitHub is the only forge.
# A real module (lib/agentctl/__init__.py MODULES): its own imports, the standard library's and MODULES' only.

from agentctl.clock import epoch_now
import contextlib
import json
import os
import re
import subprocess
import time
import urllib.parse
import urllib.request
from typing import Any

from agentctl.procrunner import process_runner
from agentctl.core import err, process_cache, state_root
from agentctl.timefmt import parse_time
from agentctl.statefile import read_record, write_json
from agentctl.ghapp import ghapp_enabled, ghapp_token
from agentctl.netretry import net_run
from agentctl.core import HOME
from agentctl.ghtally import gh_tally, gh_tally_as, gh_tally_shape
from agentctl.storeknobs import knob_gh_cli, knob_runners_page_cache


# ---------------------------------------------------------------- forge: the read side
# Every run list, run view, failed log, artifact, dispatch, cancel and actions-API read goes through the functions below:
#   gh_json / forge_argv / forge_env    the CLI: GH_CLI (gh's own auth: $GH_TOKEN or `gh auth login`; on ci-runner the
#                                       repo's App installation token, ghapp.py)
#   api                                 `gh api` under repos/<o_r>/
#   forge_branches / forge_branch_sha / forge_delete_branch / forge_actions_runs / forge_actions_run /
#   forge_release_asset / forge_run_url
#                                       the calls whose answers are folded into agentctl's run record (run_record)
# The push remote is core's remote(); the landing identity on ci-runner is ghapp.py's (github-app).
GH_CLI = knob_gh_cli()  # the GitHub CLI
GITHUB_WEB = "https://github.com"


def forge_env(o_r: Any, env: Any = None) -> dict[str, str]:
    """The CLI's environment: gh's own auth ($GH_TOKEN as set, else `gh auth login`'s; on ci-runner the repo's App
    installation token), and never a prompt or an update check. Every call is one gh process (or, with `env`, a token
    handed to a child process), tallied against its caller (gh_tally)."""
    gh_tally("token handoff" if env is not None else None, o_r=o_r)
    env = dict(os.environ if env is None else env)
    env.update(GH_PROMPT_DISABLED="1", GH_NO_UPDATE_NOTIFIER="1", NO_COLOR="1")
    if ghapp_enabled():  # ci-runner: the repo's App installation token (ghapp.py), never a person's
        env["GH_TOKEN"] = ghapp_token(o_r)
    return env


def forge_argv(o_r: Any, *args: Any) -> list[str]:
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


def _gh_api_run(method: str, path: str, argv: list[str], env: dict[str, str],
                body: object) -> "subprocess.CompletedProcess[Any]":
    """One `gh api` run: a GET tried again through a blip (net_run); a mutation never is -- it may have been done
    before the line broke."""
    kw: dict[str, Any] = dict(capture_output=True, text=True, env=env, timeout=API_TIMEOUT,
                              input=json.dumps(body) if body is not None else None)
    return net_run(f"gh api GET {gh_tally_shape(path)}", argv, **kw) if method == "GET" else process_runner().run(argv, **kw)


def _gh_api_argv(method: str, url: str, cached: Any, body: object) -> list[str]:
    """`gh api`'s argv: a GET asks with its cached ETag (and wants the headers), a body is read from stdin."""
    argv = [GH_CLI, "api", "-X", method, "-H", "Accept: application/vnd.github+json", url]
    if method == "GET":
        argv[2:2] = ["-i"] + (["-H", f"If-None-Match: {cached['etag']}"] if cached and cached.get("etag") else [])
    if body is not None:
        argv += ["--input", "-"]
    return argv


def _gh_api_read(url: str, r: "subprocess.CompletedProcess[Any]", cached: Any) -> Any:
    """A GET's answer: the cache's on a 304, else the body, kept with its ETag."""
    status, hdrs, text = _split_response(r.stdout)
    if status == 304 and cached is not None:
        gh_tally(free=True)
        return cached.get("body")
    if r.returncode != 0:
        raise ForgeHTTPError(f"gh api GET {url}: {(r.stderr or text or r.stdout).strip()[:300]}")
    out = json.loads(text) if text.strip() else None
    if hdrs.get("etag"):
        with contextlib.suppress(OSError):
            write_json(etag_path(url), {"etag": hdrs["etag"], "body": out})
    return out


def _gh_api_once(o_r, method, path, params=None, body=None):
    url = f"repos/{o_r}/{path}" + ("?" + urllib.parse.urlencode(params) if params else "")
    cached = read_record(etag_path(url)) if method == "GET" else None
    argv = _gh_api_argv(method, url, cached, body)
    with gh_tally_as(f"api {method} {gh_tally_shape(path)}"):
        env = forge_env(o_r)
    r = _gh_api_run(method, path, argv, env, body)
    if method == "GET":
        return _gh_api_read(url, r, cached)
    if r.returncode != 0:
        raise ForgeHTTPError(f"gh api {method} {url}: {(r.stderr or r.stdout).strip()[:300]}")
    return json.loads(r.stdout) if r.stdout.strip() else None


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


def _run_list_opts_parsed(rest: Any) -> Any:
    """{option: value} of a `run list` argv's flags (its --workflow, --branch, --limit, --json, --status, and their
    short forms), or None on an unknown flag, a missing value or a repeated option."""
    opts, i = {}, 0
    names = {"--workflow": "workflow", "-w": "workflow", "--branch": "branch", "-b": "branch", "--limit": "limit",
             "-L": "limit", "--json": "json", "--status": "status", "-s": "status"}
    while i < len(rest):
        name = names.get(rest[i])
        if name is None or i + 1 >= len(rest) or name in opts:
            return None
        opts[name] = rest[i + 1]
        i += 2
    return opts


def _run_list_values_ok(opts: Any) -> Any:
    """Whether the parsed `run list` options name only what REST can answer: known --json fields, a workflow file,
    a known --status."""
    return ("json" in opts and all(f in RUN_LIST_FIELDS or f == "conclusion" for f in opts["json"].split(","))
            and ("workflow" not in opts or opts["workflow"].endswith((".yml", ".yaml")))
            and ("status" not in opts or opts["status"] in RUN_LIST_STATUSES))


def _run_list_options(args: Any) -> Any:
    """{option: value} of a `run list` argv gh_json may answer from REST, or None."""
    opts = _run_list_opts_parsed(list(args[2:]))
    if opts is None or not _run_list_values_ok(opts):
        return None
    try:
        opts["limit"] = int(opts.get("limit", 20))
    except ValueError:
        return None
    return opts if opts["limit"] > 0 else None


# A RUN'S `name` IS ITS RUN-NAME (2026-11-11..12): GitHub's REST run carries the workflow's `run-name` as `name` when it
# has one, so every Identity takes run ("Identity takes (convergence run <id>)") read as a workflow of its own: outside
# the gate, its red keyed to one run that no later run could verify or clear (b059f05c70fa), stuck limits read for a
# name with no history (issues 20261111T121954, 20261111T182427, 20261112T011344). gh names a run by its workflow; so
# does this, from the repo's workflows page (a conditional GET, read again at most every WORKFLOW_NAMES_TTL s).
WORKFLOW_NAMES_TTL = 600
_workflow_names = process_cache({})


def _workflow_name_index(flows: Any) -> Any:
    return {k: w["name"] for w in flows if w.get("name") for k in (w.get("id"), w.get("path")) if k}


def forge_workflow_names(o_r):
    """{workflow id: name, workflow path: name} of o_r's workflows; {} when the page cannot be read (a run then keeps
    REST's own `name`)."""
    at, names = _workflow_names.get(o_r, (0.0, None))
    if names is None or epoch_now() - at >= WORKFLOW_NAMES_TTL:
        try:
            flows = (api(o_r, "GET", "actions/workflows", {"per_page": 100}) or {}).get("workflows") or []
            names = _workflow_name_index(flows)
        except (Exception, SystemExit) as e:
            err(f"forge: could not read {o_r}'s workflows ({str(e)[:120]}): runs keep their run-names")
            names = names or {}
        _workflow_names[o_r] = (epoch_now(), names)
    return names


def _run_list_page(o_r: Any, path: str, opts: Any, params: dict[str, Any]) -> list[Any]:
    """One page of runs; a REST failure as gh's own words (a workflow file not on the default branch: its 404)."""
    try:
        answer = api(o_r, "GET", path, params) or {}
    except ForgeHTTPError as e:
        if "workflow" in opts and re.search(r"HTTP 404|Not Found", str(e)):
            raise RuntimeError(f"gh run list: HTTP 404: workflow {opts['workflow']} not found on the default "
                               f"branch (https://api.github.com/repos/{o_r}/{path})") from None
        raise RuntimeError(f"gh run list: {e}") from None
    batch: list[Any] = answer.get("workflow_runs") or []
    return batch


def _run_list_pages(o_r: Any, opts: Any) -> tuple[list[Any], bool]:
    """(the runs of the pages read until opts' limit, whether they are everything that matched)."""
    path = (f"actions/workflows/{urllib.parse.quote(opts['workflow'], safe='')}/runs" if "workflow" in opts
            else "actions/runs")
    params = {"exclude_pull_requests": "true"}
    params.update({k: opts[k] for k in ("branch", "status") if k in opts})
    runs: list[Any] = []
    page = 1
    while len(runs) < opts["limit"]:
        per = min(100, opts["limit"])
        batch = _run_list_page(o_r, path, opts, dict(params, per_page=per, page=page))
        runs += batch
        if len(batch) < per:
            return runs, len(runs) <= opts["limit"]
        page += 1
    return runs, False


def _run_list_shaped(r: Any, fields: list[str], names: Any) -> Any:
    """One REST run as gh prints it with --json fields."""
    out = {f: r.get(RUN_LIST_FIELDS[f]) for f in fields if f in RUN_LIST_FIELDS}
    if "workflowName" in fields:  # the WORKFLOW's name, as gh names it: REST's `name` is the run's run-name
        out["workflowName"] = names.get(r.get("workflow_id")) or names.get(r.get("path")) or r.get("name")
    if "conclusion" in fields:
        out["conclusion"] = r.get("conclusion") or ""  # gh prints a pending run's conclusion as ""
    return out


def forge_run_list(o_r, args):
    """`gh run list ...` (args from `run`), as gh prints it with --json, read through api(): None when it is not one
    this answers (gh is asked instead). A workflow file not on the default branch raises gh's own words for it."""
    opts = _run_list_options(args)
    if opts is None:
        return None
    runs, complete = _run_list_pages(o_r, opts)
    fields = opts["json"].split(",")
    names = forge_workflow_names(o_r) if "workflowName" in fields else {}
    out = RunList(_run_list_shaped(r, fields, names) for r in runs[:opts["limit"]])
    out.complete = complete
    return out


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


# A LISTING IS NEVER PROOF OF ABSENCE (2026-11-20, the owner: "fix agentctl issues structurally"; 28 closed issues
# trusted what GitHub listed -- a run past the newest 50 or 200, a queued run GitHub's own listing left out, a job list
# from one attempt, a log cut short). A listing says whether it holds EVERYTHING that matched (RunList.complete: its
# last page came back short); absent from an incomplete one is "cannot tell", never "none". A run agentctl itself
# started is looked up by its own identity -- its workflow, branch and head sha, filtered by GitHub -- never by scanning
# the newest N (forge_find_run). tests/test_listings.py fails any run listing in lib/agentctl/ whose call site does not
# say, in a `# listing:` line, how it treats a run the listing left out.
class RunList(list):
    """A list of runs that knows whether it is everything that matched (`complete`)."""
    complete = False


def forge_actions_runs(o_r, workflow=None, run_number=None, limit=50):
    """The newest action runs (run_record), optionally of one workflow file or with one run number: a RunList, complete
    when GitHub's page held every match."""
    path = f"actions/workflows/{urllib.parse.quote(workflow, safe='')}/runs" if workflow else "actions/runs"
    per = 100 if run_number is not None else min(int(limit), 100)
    raw = (api(o_r, "GET", path, {"per_page": per}) or {}).get("workflow_runs") or []
    runs = [run_record(r) for r in raw]
    if run_number is not None:  # GitHub filters runs by no number: the newest 100, matched here
        runs = [r for r in runs if str(r["run_number"]) == str(run_number)]
    out = RunList(runs[:int(limit)])
    out.complete = len(raw) < per and len(runs) <= int(limit)
    return out


def forge_find_run(o_r, workflow, branch, head_sha):
    """The newest run of workflow file `workflow` on `branch` at `head_sha` (run_record), found by GitHub's own filters
    -- never a scan of the newest N, which a busy repo pushes it out of -- or None when GitHub has none."""
    path = f"actions/workflows/{urllib.parse.quote(workflow, safe='')}/runs"
    raw = (api(o_r, "GET", path, {"branch": branch, "head_sha": head_sha, "per_page": 10}) or {}).get("workflow_runs") or []
    hits = [run_record(r) for r in raw if r.get("head_sha") == head_sha
            and r.get("head_branch") in (branch, f"refs/heads/{branch}")]
    return max(hits, key=lambda r: int(r.get("id") or 0)) if hits else None


def forge_actions_run(o_r, rid):
    """One action run (run_record); {} when GitHub has none."""
    r = api(o_r, "GET", f"actions/runs/{rid}") or {}
    return run_record(r) if r else {}


def _commit_time(o_r: Any, sha: Any) -> Any:
    c = api(o_r, "GET", f"commits/{sha}") or {}
    return parse_time(((c.get("commit") or {}).get("committer") or {}).get("date"))


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
            out.append((b["name"], sha, _commit_time(o_r, sha)))
        if len(bs) < per:
            return out
        page += 1


def forge_branch_sha(o_r: str, name: str) -> str:
    return str(api(o_r, "GET", f"branches/{urllib.parse.quote(name, safe='')}")["commit"]["sha"])


def forge_delete_branch(o_r: str, name: str) -> None:
    """Delete a branch through the API (its ref)."""
    api(o_r, "DELETE", f"git/refs/heads/{urllib.parse.quote(name, safe='/')}")


def forge_release_asset(o_r, tag, name):
    """A release asset's bytes."""
    r = process_runner().run([GH_CLI, "release", "download", tag, "-p", name, "-O", "-", "-R", o_r], capture_output=True,
                       env=forge_env(o_r), timeout=60)
    if r.returncode != 0:
        raise ForgeHTTPError(f"gh release download {tag} {name}: {r.stderr.decode(errors='replace').strip()[:200]}")
    return r.stdout


def gh_json(o_r: str, *args: str) -> Any:
    """A JSON read through gh (lib/agentctl/forge.py). A `run list` is answered from the REST API with conditional
    GETs instead (forge_run_list), when it can be, and so is a bare `api repos/<o_r>/...` read."""
    listed = forge_run_list(o_r, args) if args[:2] == ("run", "list") else None
    if listed is not None:
        return listed
    # A bare `gh api repos/<o_r>/...` read is a GET: through api()'s conditional GET, free (304) when unchanged
    # (2026-11-10's tally: branch_exists, the retire reads and ci-why's commit reads sent no If-None-Match)
    if len(args) == 2 and args[0] == "api" and args[1].startswith(f"repos/{o_r}/"):
        return _gh_api_once(o_r, "GET", args[1][len(f"repos/{o_r}/"):])
    with gh_tally_as("gh " + " ".join(gh_tally_shape(a) for a in args[:3])):
        env = forge_env(o_r)
    r = net_run(f"gh {' '.join(args[:3])}", forge_argv(o_r, *args), capture_output=True, text=True, env=env,
                timeout=GH_JSON_TIMEOUT)
    if r.returncode != 0:
        raise RuntimeError(f"gh {' '.join(args)}: {r.stderr.strip()[:300]}")
    return json.loads(r.stdout)


GH_JSON_TIMEOUT = 90  # a read must fail, not hang a step


def api(o_r, method, path, params=None, body=None):
    """GitHub's REST API under repos/<o_r>/, through `gh api` (forge.py)."""
    return _gh_api_once(o_r, method, path, params, body)


API_TIMEOUT = 60


RUN_FIELDS = "databaseId,number,status,conclusion,headSha,url,displayTitle,workflowName,createdAt,updatedAt,event"


RED_CONCLUSIONS = ("failure", "timed_out")


RUNNERS_PAGE_MAX_AGE = 5 * 60  # ci-runners-page reads GitHub's runners and jobs every ~15 s


def runner_queue(now_=None):
    """ci-runners-page's last good read of ci-runner (`running` tasks per runner, `waiting_jobs` per label, each runner's
    `limits`), or None when it is missing or older than RUNNERS_PAGE_MAX_AGE."""
    path = knob_runners_page_cache() or f"{HOME}/.cache/ci-runners-page/last-good.json"
    q = read_record(path)
    try:
        fresh = (now_ if now_ is not None else epoch_now()) - float(q["ok"]) <= RUNNERS_PAGE_MAX_AGE
    except (OSError, ValueError, KeyError, TypeError):
        return None
    return q if fresh else None


# A RUN THAT DID NOTHING IS NO SAMPLE (issues 20261101T101427-e15fe1, 20261108T205920-0e4015): Order independence #27
# took 31 s with every real job skipped, and Identity takes' first runs failed in ~3 min, so the p90 fell to the floor
# and healthy 40-min runs were stuck reds. A finished run shorter than STUCK_SAMPLE_MIN tells nothing of how long the
# workflow takes: it is left out of the p90, and the stuck line says how many runs the limit came from.
STUCK_SAMPLE_MIN = 5 * 60


def stuck_durations(runs: list[dict[str, Any]]) -> list[float]:
    """The durations of the finished runs that ran (STUCK_SAMPLE_MIN or longer)."""
    durs: list[float] = []
    for r in runs:
        if r.get("status") == "completed" and r.get("createdAt") and r.get("updatedAt"):
            try:
                dur = parse_time(r["updatedAt"]).timestamp() - parse_time(r["createdAt"]).timestamp()
            except (ValueError, TypeError):
                continue
            if dur >= STUCK_SAMPLE_MIN:
                durs.append(dur)
    return durs


UNDECIDED = ("cancelled", "skipped", "neutral", "")


def is_cancelled(r: dict[str, Any]) -> bool:
    return r.get("status") == "completed" and (r.get("conclusion") or "") in UNDECIDED


def run_id_of_number(o_r, n):
    """The run id whose run NUMBER (run_number, the #N of every link) is n, or None."""
    try:
        # listing: the newest 100 matched by number (GitHub filters by none); None is 'not found', callers fall back
        runs = forge_actions_runs(o_r, run_number=n, limit=5)
    except (Exception, SystemExit):
        return None
    hit = next((x for x in runs if str(x.get("run_number")) == str(n)), None)
    return hit and hit.get("id")
