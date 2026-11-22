# agentctl, part `workflows`: GitHub Actions workflows: a workflow file's jobs and triggers (read by indentation, no YAML parser), a job's own name, the reaper step's name.
# A real module (lib/agentctl/__init__.py MODULES): its own imports, the standard library's and MODULES' only.

import re
from typing import Any


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


def fix_workflow_jobs(text: str) -> dict[str, tuple[str, str]]:
    """{job key: (its block's text, its `name:`)} of a workflow file, by indentation (no YAML parser here)."""
    lines = text.splitlines()
    try:
        start = next(i for i, l in enumerate(lines) if re.match(r"^jobs:\s*$", l))
    except StopIteration:
        return {}
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
    out = {}
    for k, ls in jobs.items():
        body = "\n".join(ls)
        nm = re.search(r"^\s+name:\s*['\"]?([^'\"\n]+)", body, re.M)
        out[k] = (body, (nm.group(1).strip() if nm else k))
    return out


def job_inner(name: Any) -> str:
    """A job's own name: the part after ` / ` of GitHub's `<caller> / <job>` (convergence_reuse.py's jobs_by_name)."""
    caller, sep, inner = str(name).partition(" / ")
    return inner if sep else caller


LOCAL_FLOW = "Local checks"
