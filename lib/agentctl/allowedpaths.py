# agentctl, part `allowedpaths`: the paths an agent may write: the allowed roots, the protected directories, the paths a sweep skips.
# A real module (lib/agentctl/__init__.py MODULES): its own imports, the standard library's and MODULES' only.

import os
from agentctl.core import HOME, print_error, process_cache
from agentctl.fixerknobs import knob_allowed_roots


# BACKGROUND CODE TOUCHES ONLY THESE ROOTS. A path learned from another process (its cwd) or from a git registry
# (`worktree list`) that lies elsewhere -- ~/Desktop, ~/Documents, ~/Downloads -- is never stat'ed or opened: macOS asks
# "Python wants to access files in your Desktop folder" for every such touch. Skipped paths are logged once.
def allowed_roots() -> list[str]:
    h = HOME
    roots = [f"{h}/projects", f"{h}/bitcashier", f"{h}/.cache/agentctl", f"{h}/.cache", f"{h}/Library/Logs", "/tmp", "/private/tmp",
             "/var/folders", "/private/var/folders", os.path.realpath(os.environ.get("TMPDIR") or "/tmp")]
    return roots + [x for x in knob_allowed_roots().split(":") if x]


_skipped_paths: set[str] = process_cache(set())


PROTECTED_DIRS = ("Desktop", "Documents", "Downloads")  # under HOME: never touched, even below an allowed root


def allowed_dir(path: str | None) -> bool:
    """path is a directory under the allowed roots (allowed_path): one background code may read and write in."""
    return bool(path) and os.path.isdir(str(path)) and allowed_path(path)


def under_bin(n: str) -> bool:
    """n (normalised) is ~/bin, below it, or a sibling worktree ~/bin-wt-*."""
    return n == f"{HOME}/bin" or n.startswith(f"{HOME}/bin/") or n.startswith(f"{HOME}/bin-wt-")


def allowed_path(path: str | None) -> bool:
    """True when path (a string; never stat'ed here) is under an allowed root, or a sibling worktree of ~/bin
    (~/bin, ~/bin-wt-*). Otherwise False, logged once."""
    if not path:
        return False
    n = os.path.normpath(path)
    h = HOME
    protected = any(n == f"{h}/{d}" or n.startswith(f"{h}/{d}/") for d in PROTECTED_DIRS)  # whatever root holds it
    ok = not protected and (under_bin(n) or any(n == r or n.startswith(r.rstrip("/") + "/") for r in allowed_roots()))
    if not ok and n not in _skipped_paths:
        _skipped_paths.add(n)
        print_error(f"skipped {n}: outside the roots background code may touch")
    return ok
