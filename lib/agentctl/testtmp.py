# agentctl, part `testtmp`: the test suite's temp leftovers: where they are, when one is old and not in use, their sweep
# A real module (lib/agentctl/__init__.py MODULES): its own imports, the standard library's and MODULES' only.

from agentctl.clock import wall_epoch
import glob
import os
from typing import Optional
from agentctl.storetoken import token_sock_path


TEST_TMP_MAX_AGE = 3600


def user_temp_dir() -> str:
    try:
        return os.confstr("CS_DARWIN_USER_TEMP_DIR") or tempfile_dir()
    except (ValueError, OSError, AttributeError):
        return tempfile_dir()


def tempfile_dir() -> str:
    import tempfile
    return tempfile.gettempdir()


def _suite_root_in_use(p: str, t: float) -> bool:
    """A test suite root still in use: its runner alive (tests/run.py writes owner.pid), or the root or an entry of it
    changed within TEST_TMP_MAX_AGE."""
    try:
        with open(os.path.join(p, "owner.pid")) as f:
            os.kill(int(f.read().strip()), 0)
        return True
    except (FileNotFoundError, ProcessLookupError, ValueError):  # no owner file, a dead runner, a torn pid
        pass
    except PermissionError:  # a live process of another user
        return True
    try:
        newest = max([os.lstat(p).st_mtime] + [os.lstat(e.path).st_mtime for e in os.scandir(p)])
    except OSError:
        return True
    return t - newest <= TEST_TMP_MAX_AGE


def _sweepable(p: str, t: float, suite_root: bool) -> bool:
    """A real directory past TEST_TMP_MAX_AGE -- and, a suite root, not in use."""
    try:
        if not os.path.isdir(p) or os.path.islink(p) or t - os.lstat(p).st_mtime <= TEST_TMP_MAX_AGE:
            return False
    except OSError:
        return False
    return not (suite_root and _suite_root_in_use(p, t))


def sweep_test_temp(now_: Optional[float] = None, tmp_root: str = "/tmp") -> int:
    """The test suite's leftovers older than TEST_TMP_MAX_AGE: `agentctl-*` in the user's temp dir, `agentctl-sock-*` in
    /tmp (never the processor's own socket dir /tmp/agentctl-<uid>), and /tmp's suite roots `agentctl-t-*` (2026-11-22:
    174 of them, 197 MB, fed a runaway fseventsd) unless one is in use (_suite_root_in_use). Returns how many went."""
    import shutil
    t = now_ if now_ is not None else wall_epoch()  # against directory mtimes
    n = 0
    roots = set(glob.glob(os.path.join(tmp_root, "agentctl-t-*")))
    for pat in (os.path.join(user_temp_dir(), "agentctl-*"), os.path.join(tmp_root, "agentctl-sock-*"), *roots):
        for p in glob.glob(pat):
            if os.path.realpath(p) == os.path.realpath(os.path.dirname(token_sock_path("x"))):
                continue  # the processor's own socket dir (Linux: the user temp dir IS /tmp)
            if _sweepable(p, t, p in roots):
                shutil.rmtree(p, ignore_errors=True)
                n += 1
    return n
