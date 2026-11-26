# agentctl, part `govpool`: THE GOVERNED FAN-OUT: every parallel runner's width, from a governor ticket it holds or the one it runs under
# A real module (lib/agentctl/__init__.py MODULES): its own imports, the standard library's and MODULES' only.

import contextlib
import json
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
#   bin/gov-sbt call too), held on a descriptor this process keeps open until the pool ends (the kernel frees it however
#   the process ends). Its width is what it asked for -- or, a pool that can run narrower (`floor`), what the governor
#   granted it (THE GRANTED WIDTH, govknobs.py: --min-cores; the free cores up to what it wants), read from its ticket.
# - UNDER A TICKET ($AGENTCTL_GOV_WIDTH, set by whoever holds one: a check's hook, a governed pool for its workers): it
#   takes NONE -- a nested call never waits on its own parent, so it never deadlocks -- and its width is the smaller of
#   what it asks and what the ticket above it allows.
# - Its workers run with $AGENTCTL_GOV_WIDTH=1 (Fanout.env): each holds one core of this pool's ticket, so a fan-out
#   inside one of them runs one wide and takes no ticket either.
# - The governor's wait is bounded on its own (GOV_WAIT, $AGENTCTL_GOV_WAIT, 30 min) and is NEVER part of a step's run
#   bound (2026-11-26: a self-push refused because the stress lane waited 686 s for cores against a 300 s kill bound,
#   no test failing): devtools/govstep.py takes the ticket, then starts the step's clock. Past the wait the pool raises
#   GovernorBusy ("governor busy: waited N s for M cores (holders: ...)"), exit 75 through run_main: load, not a test
#   failure, never retried silently. The governor FAILING (not waiting) leaves the pool ONE wide -- never its full width
#   ticketless (2026-11-25: a suite that waited 900 s ran its 8 workers beside everything the governor held).
# - No governor here (another machine's checkout, $AGENTCTL_SUITE_GOVERNOR=-): its full width, as before.
WIDTH_ENV = "AGENTCTL_GOV_WIDTH"
GOV_WAIT = 1800
GOV_WAIT_ENV = "AGENTCTL_GOV_WAIT"
BUSY_EXIT = 75  # EX_TEMPFAIL: the governor never admitted the pool; no code was judged


class GovernorBusy(Exception):
    """The governor did not admit the pool within its wait: the message names the wait, the cores and the holders."""


def gov_wait(env: Mapping[str, str]) -> float:
    try:
        return float(env.get(GOV_WAIT_ENV) or GOV_WAIT)
    except ValueError:
        return GOV_WAIT


def run_main(main: Callable[[], int]) -> int:
    """A runner's exit code: `main`'s, or BUSY_EXIT with the refusal printed when the governor never admitted it."""
    try:
        return main()
    except GovernorBusy as busy:
        print(busy, file=sys.stderr, flush=True)
        return BUSY_EXIT


def holders(gov: str, env: Mapping[str, str]) -> str:
    """The governor's holders, one line each (`gov status`), best effort."""
    try:
        out = subprocess.run([gov, "gov", "status"], capture_output=True, text=True, timeout=30, env=dict(env)).stdout
    except (OSError, subprocess.TimeoutExpired):
        return "unknown"
    return "; ".join(" ".join(line.split()) for line in out.splitlines()[1:6] if line.startswith("  ")) or "none listed"


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


def admit_args(gov: str, fd: int, ticket: str, cls: str, want: int, job: str, label: str,
               floor: Optional[int] = None) -> list[str]:
    return [gov, "gov", "admit", "--fd", str(fd), "--ticket", ticket, "--class", cls, "--mem", "1", "--cores",
            str(want), *(["--min-cores", str(floor)] if floor is not None else []), "--job", job, "--label", label,
            "--pid", str(os.getpid())]


def granted_width(ticket: str, want: int) -> int:
    """THE GRANTED WIDTH (govknobs.py): the cores the governor charged the pool's ticket (its record beside it), at most
    what it asked; what it asked when the record names none (an older governor)."""
    try:
        with open(ticket[:-len(".lock")] + ".json") as f:
            cores = json.load(f).get("cores")
    except (OSError, ValueError, AttributeError):
        return want
    return max(1, min(want, int(cores))) if isinstance(cores, (int, float)) and cores else want


def gov_dir_path(env: Mapping[str, str] = os.environ) -> str:
    """THE governor's directory (not created here): $AGENTCTL_GOV_DIR, else `gov` under the state root
    ($AGENTCTL_STATE_ROOT, else ~/.cache/agentctl). govtickets.gov_dir, cpu.py and `governed` all read it here."""
    root = env.get("AGENTCTL_STATE_ROOT") or os.path.expanduser("~/.cache/agentctl")
    return env.get("AGENTCTL_GOV_DIR") or os.path.join(root, "gov")


def take_ticket(gov: str, gov_dir: str, cls: str, want: int, job: str, label: str, wait: float,
                run: Callable[..., "subprocess.CompletedProcess[bytes]"], env: Mapping[str, str],
                floor: Optional[int] = None) -> Optional[tuple[int, str]]:
    """(descriptor, ticket path) once the governor admits the pool, or None (refused or failed); GovernorBusy past `wait`."""
    os.makedirs(gov_dir, exist_ok=True)
    ticket = os.path.join(gov_dir, f"t-{job}-{os.getpid()}-{time.monotonic_ns()}.lock")  # wall: waits on and times real processes and threads
    fd = os.open(ticket, os.O_WRONLY | os.O_CREAT | os.O_APPEND)
    try:
        if run(admit_args(gov, fd, ticket, cls, want, job, label, floor), pass_fds=(fd,), timeout=wait,
               env=dict(env)).returncode == 0:
            return fd, ticket
    except subprocess.TimeoutExpired:
        give_back((fd, ticket))
        raise GovernorBusy(f"governor busy: waited {int(wait)} s for {want} cores (holders: {holders(gov, env)})")
    except OSError:
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
             wait: Optional[float] = None, run: Callable[..., "subprocess.CompletedProcess[bytes]"] = subprocess.run,
             say: Callable[[str], object] = sys.stderr.write, floor: Optional[int] = None) -> Iterator[Fanout]:
    """A parallel runner's width for `want` workers, under a ticket of the governor for as long as it runs. floor: a
    pool that can run narrower (THE GRANTED WIDTH) -- never a time-bounded one, like the suite (its bound is set for its
    full width: granted 1 core at load 17, the pre-push suite would only time out)."""
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
    held = take_ticket(gov, gov_dir_path(env), cls, want, job, label, gov_wait(env) if wait is None else wait, run, env,
                       floor)
    if held is None:
        say(f"{job}: the governor refused or failed: no ticket for {want} cores, running one wide\n")
    try:
        yield Fanout(granted_width(held[1], want), held[1], "admitted") if held else Fanout(1, None, "no ticket: one wide")
    finally:
        give_back(held)
