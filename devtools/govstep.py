#!/usr/bin/env python3
"""devtools/govstep.py --job J --cores N --run SECONDS [--wait SECONDS] -- COMMAND...: run one bounded step of the
pre-push hook under a governor ticket, the step's clock starting WHEN THE TICKET IS GRANTED.

The hook used to bound each step by one kill timeout that also covered the wait for cores (2026-11-26: the stress lane
waited 686 s against a 300 s bound and the push was refused with no test failing). Here the wait has a bound of its own
($AGENTCTL_GOV_WAIT, default 30 min, govpool.GOV_WAIT) and the run has its bound after the grant. The command runs under
the ticket ($AGENTCTL_GOV_WIDTH = the width granted), so the runners inside take none and never wait twice.
Exit: the command's; RUN_BOUND_EXIT (124) when it outlived --run (killed); govpool.BUSY_EXIT (75) with "governor busy:
waited N s for M cores (holders: ...)" when no ticket came in time: load, not a test failure, never retried here."""
import argparse
import os
import subprocess
import sys
from typing import Sequence

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "lib"))
from agentctl.govpool import WIDTH_ENV, governed, run_main  # noqa: E402

RUN_BOUND_EXIT = 124
TERM_GRACE = 20


def run_step(command: Sequence[str], run_bound: float, env: dict[str, str]) -> int:
    """The command's exit code, or RUN_BOUND_EXIT after ending it (SIGTERM, then SIGKILL) past `run_bound` seconds."""
    proc = subprocess.Popen(list(command), env=env)  # the push's own process group: `agentctl land` reads its CPU
    try:
        return proc.wait(timeout=run_bound)
    except subprocess.TimeoutExpired:
        proc.terminate()
        try:
            proc.wait(timeout=TERM_GRACE)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()
        print(f"govstep: RUN BOUND -- {command[0]} ran past {int(run_bound)} s after its ticket was granted", flush=True)
        return RUN_BOUND_EXIT


def main(argv: Sequence[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--job", required=True)
    ap.add_argument("--cores", type=int, required=True)
    ap.add_argument("--run", type=float, required=True, help="the step's bound, counted from the grant")
    ap.add_argument("--wait", type=float, default=None, help="the ticket wait's bound (default $AGENTCTL_GOV_WAIT)")
    ap.add_argument("command", nargs=argparse.REMAINDER)
    a = ap.parse_args(argv)
    command = a.command[1:] if a.command[:1] == ["--"] else a.command
    if not command:
        ap.error("no command")
    with governed(a.job, a.cores, f"pre-push {a.job} {os.getcwd()}", wait=a.wait) as pool:
        return run_step(command, a.run, {**os.environ, WIDTH_ENV: str(pool.width)})


if __name__ == "__main__":
    sys.exit(run_main(main))
