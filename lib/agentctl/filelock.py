# agentctl, part `filelock`: THE FILE LOCK: one flock type (exclusive or shared, blocking, tried, or bounded by a timeout) with a holder note
# A real module (lib/agentctl/__init__.py MODULES): its own imports, the standard library's and MODULES' only.

import fcntl
import os
from typing import IO, Callable, Optional

from agentctl.clock import Clock

DEFAULT_POLL = 0.05  # how often a bounded wait tries again, by default


class LockTimeout(OSError):
    """A bounded wait for a lock ran out; `holder` is the note its holder left (who has it)."""

    def __init__(self, path: str, holder: str) -> None:
        super().__init__(f"lock {path} not free in time" + (f" (held by {holder})" if holder else ""))
        self.holder = holder


class FileLock:
    """An flock on `path` (created, parent too, when missing), released by release() or by its holder's death.
    `note` is written into the file while held (who has it: `holder()` reads it back for a waiter's message).
    `timeout` (seconds, polled on `clock` every `poll` seconds) bounds acquire(); without one acquire() blocks, and try_acquire() never waits.
    Used as a context manager it holds the lock for the block and raises LockTimeout past its timeout."""

    def __init__(self, path: str, *, shared: bool = False, note: str = "", timeout: Optional[float] = None,
                 clock: Optional[Clock] = None, on_wait: Optional[Callable[[float], None]] = None, poll: float = DEFAULT_POLL) -> None:
        self.path, self.shared, self.note, self.timeout, self.clock = path, shared, note, timeout, clock
        self.poll, self.on_wait = poll, on_wait  # called with the seconds waited so far at each poll (a waiter's progress line)
        self._file: Optional[IO[str]] = None

    @property
    def held(self) -> bool:
        return self._file is not None

    def _mode(self) -> int:
        return fcntl.LOCK_SH if self.shared else fcntl.LOCK_EX

    def holder(self) -> str:
        """The note the lock's holder left ('' when none or unreadable)."""
        try:
            with open(self.path) as f:
                return f.read().strip()
        except OSError:
            return ""

    def _open(self) -> IO[str]:
        parent = os.path.dirname(self.path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        return open(self.path, "a+")

    def _locked(self, f: IO[str]) -> None:
        self._file = f
        if self.note and not self.shared:
            f.seek(0)
            f.truncate()
            f.write(self.note)
            f.flush()

    def try_acquire(self) -> bool:
        """Take the lock if it is free now; False when another process holds it."""
        f = self._open()
        try:
            fcntl.flock(f, self._mode() | fcntl.LOCK_NB)
        except OSError:  # BlockingIOError: held (a filesystem without flock errors the same way: treat as held)
            f.close()
            return False
        self._locked(f)
        return True

    def acquire(self) -> bool:
        """Take the lock: wait for it (up to `timeout`, then False); without a timeout block until it is free."""
        if self.timeout is None:
            f = self._open()
            try:
                fcntl.flock(f, self._mode())
            except BaseException:
                f.close()
                raise
            self._locked(f)
            return True
        if self.clock is None:
            raise ValueError("a bounded FileLock needs its clock")
        start = self.clock.monotonic()
        while not self.try_acquire():
            waited = self.clock.monotonic() - start
            if waited >= self.timeout:
                return False
            if self.on_wait:
                self.on_wait(waited)
            self.clock.sleep(self.poll)
        return True

    def release(self) -> None:
        f, self._file = self._file, None
        if f is not None:
            try:
                fcntl.flock(f, fcntl.LOCK_UN)
            finally:
                f.close()

    def __enter__(self) -> "FileLock":
        if not self.acquire():
            raise LockTimeout(self.path, self.holder())
        return self

    def __exit__(self, *exc: object) -> None:
        self.release()


def lock_held(path: str) -> bool:
    """Whether a live process holds the flock at path (tried without blocking, released at once)."""
    if not os.path.exists(path):
        return False
    probe = FileLock(path)
    if probe.try_acquire():
        probe.release()
        return False
    return True
