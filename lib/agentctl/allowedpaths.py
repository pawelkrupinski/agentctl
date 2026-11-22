# agentctl, part `allowedpaths`: the paths an agent may write: the allowed roots, the protected directories, the paths a sweep skips.
# A real module (lib/agentctl/__init__.py MODULES): its own imports, the standard library's and MODULES' only.

import os
from agentctl.core import HOME, err, process_cache


# BACKGROUND CODE TOUCHES ONLY THESE ROOTS. A path learned from another process (its cwd) or from a git registry
# (`worktree list`) that lies elsewhere -- ~/Desktop, ~/Documents, ~/Downloads -- is never stat'ed or opened: macOS asks
# "Python wants to access files in your Desktop folder" for every such touch. Skipped paths are logged once.
def allowed_roots() -> list[str]:
    h = HOME
    roots = [f"{h}/projects", f"{h}/bitcashier", f"{h}/.cache/agentctl", f"{h}/.cache", f"{h}/Library/Logs", "/tmp", "/private/tmp",
             "/var/folders", "/private/var/folders", os.path.realpath(os.environ.get("TMPDIR") or "/tmp")]
    return roots + [x for x in os.environ.get("AGENTCTL_ALLOWED_ROOTS", "").split(":") if x]


_skipped_paths: set[str] = process_cache(set())


PROTECTED_DIRS = ("Desktop", "Documents", "Downloads")  # under HOME: never touched, even below an allowed root


def allowed_path(path: str | None) -> bool:
    """True when path (a string; never stat'ed here) is under an allowed root, or a sibling worktree of ~/bin
    (~/bin, ~/bin-wt-*). Otherwise False, logged once."""
    if not path:
        return False
    n = os.path.normpath(path)
    h = HOME
    protected = any(n == f"{h}/{d}" or n.startswith(f"{h}/{d}/") for d in PROTECTED_DIRS)  # whatever root holds it
    ok = not protected and (n == f"{h}/bin" or n.startswith(f"{h}/bin/") or n.startswith(f"{h}/bin-wt-")
                            or any(n == r or n.startswith(r.rstrip("/") + "/") for r in allowed_roots()))
    if not ok and n not in _skipped_paths:
        _skipped_paths.add(n)
        err(f"skipped {n}: outside the roots background code may touch")
    return ok
