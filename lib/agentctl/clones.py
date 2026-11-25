# agentctl, part `clones`: the clones agentctl works in: a repository's main clone, fetching its main, a clone suspected corrupt and its healing switch.
# A real module (lib/agentctl/__init__.py MODULES): its own imports, the standard library's and MODULES' only.

import os
from typing import Any
import re
import time
from agentctl.core import git, git_ok, remote
from agentctl.statefile import write_json
from agentctl.repos import server_repo
import contextlib
import fcntl
import threading


def _clone_of_existing(path: Any) -> Any:
    """The main clone of a worktree that exists: its git common dir's checkout, else its own top level."""
    r = git(path, "rev-parse", "--path-format=absolute", "--git-common-dir", check=False)
    if r.returncode == 0:
        common = os.path.realpath(r.stdout.strip())
        clone = os.path.dirname(common) if os.path.basename(common) == ".git" else common
        if os.path.isdir(clone):
            return clone
    return git(path, "rev-parse", "--show-toplevel").strip()


def main_clone(path: str) -> Any:
    """The main clone a worktree belongs to (its git common dir's checkout): what a DETACHED process works from, since
    the worktree it was started for may be removed under it (2026-10-27: a watch crashed with `git rev-parse
    --show-toplevel in ~/projects/movies-wt-gate-closed: cannot change to ...` after its agent removed the worktree at
    LANDED). A path that is gone resolves by name: <clone>-wt-<name> -> <clone>."""
    if path and os.path.isdir(path):
        return _clone_of_existing(path)
    m = re.match(r"^(.*?)-wt-[^/]+/?$", path or "")
    if m and os.path.isdir(m.group(1)):
        return m.group(1)
    raise RuntimeError(f"{path}: gone, and no main clone found for it")


# THE CLONE HEALS ITSELF (2026-10-29: `git fetch --refetch` could not repair the half clone either: "shallow roots
# are not allowed to be updated"). A fetch of main failing with a corruption signature marks the clone suspect; the
# processor's "clone health" step re-probes it (clone_health: verified, and a fetch of main works) and, when it is
# broken, clones afresh into a temp directory and swaps it in under the clone lock (exclusive: every step and checker
# that uses the clone holds it shared). The broken copy is kept as <repo>.broken-<ts> (the newest one only, older ones
# deleted after a successful swap); the check and waiting worktrees are re-linked to the new clone. A CLONE condition
# says so, and the same probe, passing, closes it. Only the server's own clone ($AGENTCTL_MOVIES_REMOTE set): never a
# person's checkout.
CORRUPT_GIT = re.compile(r"did not send all necessary objects|Could not read [0-9a-f]{7,}|missing (blob|tree|commit|tag) "
                         r"object|is missing|failed to traverse parents|shallow roots are not allowed|bad object|"
                         r"bad tree object|unable to read [0-9a-f]{7,}|is corrupt|object file .* is empty|"
                         r"inflate: data stream error|invalid object|unable to parse object", re.I)


def heal_enabled(repo: str) -> bool:
    return bool(os.environ.get("AGENTCTL_MOVIES_REMOTE")) and \
        os.path.realpath(repo) == os.path.realpath(server_repo())


def clone_side(repo: str, what: str) -> str:
    parent, base = os.path.split(os.path.abspath(repo))
    return os.path.join(parent, f".{base}.{what}")


def note_clone_suspect(repo: str, error: str) -> Any:
    """A git error on the clone that looks like corruption: the clone health step looks at it next tick."""
    if heal_enabled(repo) and CORRUPT_GIT.search(error or ""):
        write_json(clone_side(repo, "suspect.json"), {"at": time.time(), "error": (error or "").strip()[-2000:]})
        return True
    return False


def fetch_main(repo: str) -> Any:
    """git fetch <the forge remote> main in repo: (ok, git's stderr). A corruption signature marks the clone suspect."""
    r = git(repo, "fetch", "--quiet", remote(), "main", check=False)
    out = (r.stderr or "").strip()
    if r.returncode == 0 and git_ok(repo, "rev-parse", "--verify", "-q", f"{remote()}/main"):
        return True, out
    if not out:
        out = f"exit {r.returncode}" + ("" if r.returncode else f"; {remote()}/main does not resolve")
    note_clone_suspect(repo, out)
    return False, out


_clone_lock_depth = threading.local()


@contextlib.contextmanager
def clone_shared(repo):
    """Use the clone: shared under its lock (the swap takes it exclusive). A no-op where it never heals, and nested."""
    depth = getattr(_clone_lock_depth, "n", 0)
    if depth or not heal_enabled(repo):
        _clone_lock_depth.n = depth + 1
        try:
            yield
        finally:
            _clone_lock_depth.n = depth
        return
    os.makedirs(os.path.dirname(clone_side(repo, "x")), exist_ok=True)
    with open(clone_side(repo, "clone-gate.lock"), "a+") as gate, open(clone_side(repo, "clone.lock"), "a+") as lk:
        fcntl.flock(gate, fcntl.LOCK_EX)  # a swap waiting for the lock holds the gate: no new user starves it
        try:
            fcntl.flock(lk, fcntl.LOCK_SH)
        finally:
            fcntl.flock(gate, fcntl.LOCK_UN)
        _clone_lock_depth.n = 1
        try:
            yield
        finally:
            _clone_lock_depth.n = 0
            fcntl.flock(lk, fcntl.LOCK_UN)
