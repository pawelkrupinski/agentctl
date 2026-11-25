# agentctl, part `workflows`: GitHub Actions workflows: a workflow file's jobs and triggers (read by indentation, no YAML parser), a job's own name, the reaper step's name.
# A real module (lib/agentctl/__init__.py MODULES): its own imports, the standard library's and MODULES' only.

import re
from typing import Any
from agentctl.core import git


# scripts/ci/bounded-step.sh --reap's warning: a step outlived its own `timeout-minutes` (the runner gives up on it and
# goes on, so its log just stops). Identity rule ablation #2400 read as UNCLEAR with it in its log.
STEP_TIMED_OUT = "ending what a timed-out step left running"


def workflow_triggers(txt: str) -> set[str]:
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


def _workflow_job_lines(lines: list[str], start: int) -> dict[str, Any]:
    """{job key: the lines of its block} of the `jobs:` section opening at lines[start], by indentation."""
    jobs: dict[str, Any] = {}
    ind: int | None = None
    cur: str | None = None
    for l in lines[start + 1:]:
        if l.strip() and not l.startswith((" ", "\t")):
            break
        m = re.match(r"^(\s+)([\w-]+):\s*$", l)
        if m and (ind is None or len(m.group(1)) == ind):
            ind, cur = len(m.group(1)), m.group(2)
            jobs[cur] = []
            continue
        if cur:
            jobs[cur].append(l)
    return jobs


def fix_workflow_jobs(text: str) -> dict[str, tuple[str, str]]:
    """{job key: (its block's text, its `name:`)} of a workflow file, by indentation (no YAML parser here)."""
    lines = text.splitlines()
    try:
        start = next(i for i, l in enumerate(lines) if re.match(r"^jobs:\s*$", l))
    except StopIteration:
        return {}
    out = {}
    for k, ls in _workflow_job_lines(lines, start).items():
        body = "\n".join(ls)
        nm = re.search(r"^\s+name:\s*['\"]?([^'\"\n]+)", body, re.M)
        out[k] = (body, (nm.group(1).strip() if nm else k))
    return out


def job_inner(name: Any) -> str:
    """A job's own name: the part after ` / ` of GitHub's `<caller> / <job>` (convergence_reuse.py's jobs_by_name)."""
    caller, sep, inner = str(name).partition(" / ")
    return inner if sep else caller


LOCAL_FLOW = "Local checks"


def workflow_file_of(repo: str, sha: str, name: str) -> tuple[str | None, str]:
    """(path in the tree, text) of the workflow file whose `name:` is `name`, at sha."""
    for d in (".github/workflows",):
        r = git(repo, "ls-tree", "--name-only", sha, f"{d}/", check=False)
        for path in r.stdout.split():
            txt = git(repo, "show", f"{sha}:{path}", check=False).stdout
            if re.search(rf"^name:\s*[\"']?{re.escape(name)}[\"']?\s*$", txt, re.M):
                return path, txt
    return None, ""


GITHUB_JOB_TIMEOUT = 360 * 60  # GitHub's own timeout of a job that sets none


# A JOB GITHUB HAS NOT CREATED YET IS NOT IN THE RUN'S JOB LIST (measured 2026-11-08: precommit-check run 42463101933's
# `check (page)`, which needs every other check, was created 1 s after `check (rest)` concluded; Main's publish-* jobs
# are absent from a running Main's list). Judged by the listed jobs alone, every named job had concluded in that second
# and the verdict was a success without the page checks. So the workflow's own jobs say which verdict jobs must exist:
# each job of its `jobs:` block whose name (its `name:`, else its key) a VERDICT_JOBS glob names must be listed (a
# `${{ ... }}` in the name matches any text, a reusable workflow's jobs are `<name> / <inner>`) before a success counts.
JOB_KEY_LINE = re.compile(r"^( +)([A-Za-z_][\w-]*):[ \t]*(?:#.*)?$")


JOB_NAME_LINE = re.compile(r"^( +)name:[ \t]*(.*?)[ \t]*$")


JOB_TIMEOUT_LINE = re.compile(r"^ +timeout-minutes:[ \t]*(\d+)[ \t]*(?:#.*)?$")


def workflow_jobs(text: str) -> list[dict[str, Any]]:
    """The jobs of a workflow, as its text declares them: [{name, timeout_s}] -- name its template (a job without
    `name:` is named by its key), timeout_s its own `timeout-minutes` in s or None."""
    jobs: list[dict[str, Any]] = []
    lines = text.splitlines()
    try:
        i = next(n for n, l in enumerate(lines) if re.match(r"^jobs:[ \t]*(?:#.*)?$", l)) + 1
    except StopIteration:
        return []
    job: dict[str, Any] | None = None
    job_indent = -1  # the jobs' indent, once the first job key sets it
    props_indent: int | None = None
    for line in lines[i:]:
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        indent = len(line) - len(line.lstrip(" "))
        if indent == 0:
            break
        key = JOB_KEY_LINE.match(line)
        if job_indent < 0 and key:
            job_indent = indent
        if indent == job_indent and key:
            job, props_indent = {"name": key.group(2), "timeout_s": None}, None
            jobs.append(job)
            continue
        if job is None or indent <= job_indent:
            continue
        if props_indent is None:
            props_indent = indent
        if indent != props_indent:
            continue
        named, bound = JOB_NAME_LINE.match(line), JOB_TIMEOUT_LINE.match(line)
        if named:
            value = named.group(2)
            quoted = re.match(r"""^(['"])(.*?)\1""", value)
            job["name"] = quoted.group(2) if quoted else value.split(" #", 1)[0].rstrip()
        elif bound:
            job["timeout_s"] = int(bound.group(1)) * 60
    return jobs


def workflow_job_names(text: str) -> list[str]:
    """The name templates of a workflow's jobs, as its text declares them (a job without `name:` is named by its key)."""
    return [j["name"] for j in workflow_jobs(text)]


def workflow_job_timeouts(text: str) -> dict[str, int | None]:
    """{name template: its timeout-minutes in s, or None} of a workflow's jobs."""
    return {j["name"]: j["timeout_s"] for j in workflow_jobs(text)}
