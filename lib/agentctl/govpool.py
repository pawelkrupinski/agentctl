# agentctl, part `govpool`: THE GOVERNED FAN-OUT: every parallel runner's width, from a governor ticket it holds or the one it runs under
# A real module (lib/agentctl/__init__.py MODULES): its own imports, the standard library's and MODULES' only.

import contextlib
import os
import subprocess
import sys
import time
from typing import Callable, Iterator, Mapping, Optional


# THE GOVERNED FAN-OUT (the owner, 2026-11-25: "fix structurally" -- load 103 on 18 cores while `gov status` said
# 0 admitted: agentctl's pre-push suite and simulator gate, movies' mutation tests' `xargs -P 4`, 43 test processes,
# none holding a ticket). Every runner that starts work in parallel -- tests/run.py's workers, tests/sim_gate.py's
# chunks -- takes its width from here, the one place that asks the governor:
#
# - NOT UNDER A TICKET: it takes one (`agentctl gov admit`, the CLI movies' scripts/jvm-gate.sh and bitcashier's
#   bin/gov-sbt call too), charged the cores it wants, held on a descriptor this process keeps open until the pool ends
#   (the kernel frees it however the process ends). Its width is what it asked for.
# - UNDER A TICKET ($AGENTCTL_GOV_WIDTH, set by whoever holds one: a check's hook, a governed pool for its workers): it
#   takes NONE -- a nested call never waits on its own parent, so it never deadlocks -- and its width is the smaller of
#   what it asks and what the ticket above it allows.
# - Its workers run with $AGENTCTL_GOV_WIDTH=1 (Fanout.env): each holds one core of this pool's ticket, so a fan-out
#   inside one of them runs one wide and takes no ticket either.
# - The governor's wait is bounded (POOL_TICKET_WAIT, a hook's bound leaves it room); past it, or with the governor
#   failing, the pool runs ONE wide -- never its full width ticketless (2026-11-25: a suite that waited 900 s ran its
#   8 workers beside everything the governor held).
# - No governor here (another machine's checkout, $AGENTCTL_SUITE_GOVERNOR=-): its full width, as before.
WIDTH_ENV = "AGENTCTL_GOV_WIDTH"
POOL_TICKET_WAIT = 900


def held_width(env: Mapping[str, str]) -> Optional[int]:
    """The width the ticket this process runs under allows, or None when it runs under none."""
    try:
        w = int(env.get(WIDTH_ENV) or 0)
    except ValueError:
        return None
    return max(1, w) if w else None


class Fanout:
    """A pool's width and the ticket it holds (None: it rides one above it, or no governor is here)."""

    def __init__(self, width: int, ticket: Optional[str], why: str):
        self.width, self.ticket, self.why = width, ticket, why

    def env(self, base: Mapping[str, str]) -> dict[str, str]:
        """A worker's environment: one core of this pool's ticket."""
        return {**base, WIDTH_ENV: "1"}


def default_governor() -> str:
    """The agentctl beside this checkout's lib/ (a pre-push hook's detached tree has its own)."""
    return os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "agentctl")


def admit_args(gov: str, fd: int, ticket: str, cls: str, want: int, job: str, label: str) -> list[str]:
    return [gov, "gov", "admit", "--fd", str(fd), "--ticket", ticket, "--class", cls, "--mem", "1", "--cores",
            str(want), "--job", job, "--label", label, "--pid", str(os.getpid())]


def take_ticket(gov: str, gov_dir: str, cls: str, want: int, job: str, label: str, wait: float,
                run: Callable[..., "subprocess.CompletedProcess[bytes]"], env: Mapping[str, str]) -> Optional[tuple[int, str]]:
    """(descriptor, ticket path) once the governor admits the pool, or None (refused, failed or past `wait`)."""
    os.makedirs(gov_dir, exist_ok=True)
    ticket = os.path.join(gov_dir, f"t-{job}-{os.getpid()}-{time.monotonic_ns()}.lock")
    fd = os.open(ticket, os.O_WRONLY | os.O_CREAT | os.O_APPEND)
    try:
        if run(admit_args(gov, fd, ticket, cls, want, job, label), pass_fds=(fd,), timeout=wait,
               env=dict(env)).returncode == 0:
            return fd, ticket
    except (OSError, subprocess.TimeoutExpired):
        pass
    give_back((fd, ticket))
    return None


def give_back(held: Optional[tuple[int, str]]) -> None:
    if held:
        os.close(held[0])
        try:
            os.unlink(held[1])
        except OSError:
            pass


@contextlib.contextmanager
def governed(job: str, want: int, label: str, cls: str = "suite", env: Optional[Mapping[str, str]] = None,
             wait: float = POOL_TICKET_WAIT, run: Callable[..., "subprocess.CompletedProcess[bytes]"] = subprocess.run,
             say: Callable[[str], object] = sys.stderr.write) -> Iterator[Fanout]:
    """A parallel runner's width for `want` workers, under a ticket of the governor for as long as it runs."""
    env = os.environ if env is None else env
    want = max(1, want)
    above = held_width(env)
    if above is not None:
        yield Fanout(min(want, above), None, f"under a ticket ({WIDTH_ENV}={above})")
        return
    gov = env.get("AGENTCTL_SUITE_GOVERNOR") or default_governor()
    if gov == "-" or not os.access(gov, os.X_OK):
        yield Fanout(want, None, "no governor here")
        return
    gov_dir = env.get("AGENTCTL_GOV_DIR") or os.path.expanduser("~/.cache/agentctl/gov")
    held = take_ticket(gov, gov_dir, cls, want, job, label, wait, run, env)
    if held is None:
        say(f"{job}: no governor ticket for {want} cores within {int(wait)} s: running one wide\n")
    try:
        yield Fanout(want, held[1], "admitted") if held else Fanout(1, None, "no ticket: one wide")
    finally:
        give_back(held)
