# agentctl, part `claudefiles`: Claude Code's own files: its projects (transcripts) and background jobs roots, a background session's transcript, a fixer's final verdict.
# A real module (lib/agentctl/__init__.py MODULES): its own imports, the standard library's and MODULES' only.

import glob
import json
import os
import re
from agentctl.core import HOME


# -- 1. the lander: who ran `agentctl land`
def claude_projects_root() -> str:
    return os.environ.get("AGENTCTL_CLAUDE_PROJECTS") or os.path.join(HOME, ".claude", "projects")


FIXER_VERDICT = re.compile(r"^\W*(LANDED [0-9a-f]{7,40}|WAITS ON [0-9a-f]{7,40}.*|NOT LANDED\b.*|GAVE UP:.*|"
                           r"BLOCKED ON OWNER:.*|DIAGNOSED:.*)", re.M)


def fixer_verdict(text: str) -> str | None:
    """The fixer's verdict: the last verdict line of its final answer (stream-json's `result`), else of its output."""
    final = None
    for line in text.splitlines():
        if line.startswith("{"):
            try:
                o = json.loads(line)
            except ValueError:
                continue
            if o.get("type") == "result":
                final = o.get("result") or ""
    for t in ([final] if final else []) + [text]:
        found = FIXER_VERDICT.findall(t)
        if found:
            return str(found[-1]).strip()
    return None


def claude_jobs_root() -> str:
    return os.environ.get("AGENTCTL_CLAUDE_JOBS") or os.path.join(HOME, ".claude", "jobs")


def bg_transcript(session: str) -> str | None:
    hits = glob.glob(os.path.join(claude_projects_root(), "*", f"{session}.jsonl"))
    return hits[0] if hits else None
