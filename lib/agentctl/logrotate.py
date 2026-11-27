# agentctl, part `logrotate`: log rotation: every agentctl log over its size is shortened in place, its newest part kept as .1 (maintain's logs section runs it)
# A real module (lib/agentctl/__init__.py MODULES): its own imports, the standard library's and MODULES' only.

import glob
import os
from typing import Any, Iterator
from agentctl.filelock import FileLock
from agentctl.tickknobs import knob_log_rotate_keep, knob_log_rotate_max
from agentctl.core import HOME, reds_dir, state_root, tilde


LOG_ROTATE_PRUNE = ("work", "target", ".git")


def logs_under(root: str) -> Iterator[str]:
    """The `.log` files below root, leaving out the pruned directories (work trees, targets, `.git`, `*.d`)."""
    for d, dirs, files in os.walk(root):
        dirs[:] = [x for x in dirs if x not in LOG_ROTATE_PRUNE and not x.endswith(".d")]
        yield from (os.path.join(d, f) for f in files if f.endswith(".log"))


def agentctl_logs():
    """Every agentctl log (see above), by real path."""
    out = set()
    for root in {os.path.realpath(state_root()), os.path.realpath(reds_dir())}:
        out |= set(logs_under(root))
    out |= set(glob.glob(os.path.join(HOME, "Library", "Logs", "agentctl-*.log")))
    return sorted(p for p in out if os.path.isfile(p) and not os.path.islink(p))


def _truncate_keeping_late(path: Any, pos: Any) -> None:
    """Truncate the log; the instant's appends after pos are written back at the new end."""
    with open(path, "r+b") as f:
        f.seek(pos)
        late = f.read()
        f.truncate(0)
    if not late:
        return
    fd = os.open(path, os.O_WRONLY | os.O_APPEND)
    try:
        os.write(fd, late)
    finally:
        os.close(fd)


def rotate_log(path, max_bytes=None, keep=None):
    """Rotate one log when it is over max_bytes (see LOGS ROTATE BY SIZE): its newest max_bytes become .1, older parts
    shift, the file is truncated in place (no line lost to an O_APPEND writer but those appended in the instant between
    the last read and the truncate). Returns what it did, or None."""
    max_bytes = int(max_bytes or knob_log_rotate_max())
    keep = max(1, int(keep or knob_log_rotate_keep()))
    try:
        size = os.path.getsize(path)
    except OSError:
        return None
    if size <= max_bytes:
        return None
    with open(path, "rb") as f:
        start = size - max_bytes
        f.seek(start)
        data = f.read(max_bytes)
        i = data.find(b"\n")
        if 0 <= i < len(data) - 1:  # from a line start: the partial first line goes with the older bytes
            start, data = start + i + 1, data[i + 1:]
        data += f.read()  # what was appended meanwhile
        pos = f.tell()
    gone = os.path.exists(f"{path}.{keep}")
    for n in range(keep - 1, 0, -1):
        if os.path.exists(f"{path}.{n}"):
            os.replace(f"{path}.{n}", f"{path}.{n + 1}")
    tmp = f"{path}.1.tmp"
    with open(tmp, "wb") as f:
        f.write(data)
    os.replace(tmp, f"{path}.1")
    _truncate_keeping_late(path, pos)
    return (f"rotated {tilde(path)} ({size >> 20} MB): its newest {len(data) >> 10} KB kept as .1"
            + (f", {start >> 20} MB older dropped" if start > (1 << 20) else "") + (f", .{keep} dropped" if gone else ""))


def rotate_logs():
    """Rotate every agentctl log over the size (one rotator at a time: a lock in the state root). Returns the lines."""
    lock = FileLock(state_root("log-rotate.lock"))
    if not lock.try_acquire():
        return []
    try:
        out = []
        for p in agentctl_logs():
            try:
                line = rotate_log(p)
            except OSError as e:
                line = f"could not rotate {tilde(p)}: {e.strerror or e}"
            if line:
                out.append(line)
        return out
    finally:
        lock.release()
