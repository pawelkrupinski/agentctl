# agentctl, part `moviestools`: what agentctl reads from the movies tools it calls, read from the tools themselves.
# A real module (lib/agentctl/__init__.py MODULES): its own imports, the standard library's only.
#
# ONE PLACE FOR THE INTERFACE (2026-11-22, the shell-to-python migration's phase 3 freezes it): what agentctl knows of a
# movies tool -- its checks, its words -- is derived here from the tool's own text, never a copy that drifts.

import re


def hook_checks(hook_path: str) -> list[str]:
    """The check words a repo's pre-push hook knows: each `check_<name>()` it defines and each word its code tests with
    `skipped <word>` (comments aside). [] when there is no hook. A local recheck skips all of them but the failing ones
    (slots.recheck_local; 2026-11-22: a hand-copied list named three checks the hook had dropped and missed fifteen)."""
    try:
        with open(hook_path) as f:
            text = f.read()
    except OSError:
        return []
    code = "\n".join(line for line in text.splitlines() if not line.lstrip().startswith("#"))
    names = set(re.findall(r"^check_([a-z0-9_]+)\s*\(\)", code, re.M)) | set(re.findall(r"\bskipped ([a-z0-9_]+)\b", code))
    return sorted(names)
