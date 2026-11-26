# agentctl, part `procrunner`: the process runner agentctl starts every command through: ProcessRunner, wired at cli
# A real module (lib/agentctl/__init__.py MODULES): its own imports, the standard library's and MODULES' only.

import subprocess
from typing import Any, Sequence


RUN_TIMEOUT = 60  # s a run may take unless its caller says otherwise (timeout=N; None: unbounded, for a command that runs long)


class ProcessRunner:
    """subprocess.run and Popen: THE PROCESS RUNNER IS PASSED IN (CLAUDE.md, "Design"). cli, the composition root, wires
    the one a process uses (wire_runner); core.git, run_cmd, git_ok, spawn_detached, netretry.net_run and every module
    starting a process go through process_runner(), so a test hands a fake one (tests/fake_runner.py FakeRunner)
    instead of patching a module."""

    def run(self, argv: Sequence[str], **kw: Any) -> "subprocess.CompletedProcess[Any]":
        """subprocess.run, BOUNDED by default (RUN_TIMEOUT: a hung `ps` or `ssh` never blocks the watchdog); a command
        that legitimately runs long passes timeout=None (or its own)."""
        kw.setdefault("timeout", RUN_TIMEOUT)
        return subprocess.run(argv, **kw)

    def popen(self, argv: Sequence[str], **kw: Any) -> "subprocess.Popen[Any]":
        return subprocess.Popen(argv, **kw)


_runner: list[ProcessRunner] = [ProcessRunner()]


def wire_runner(runner: ProcessRunner) -> None:
    """Make runner the one every process_runner() returns (the composition root, once per process; a test, its fake)."""
    _runner[:] = [runner]


def process_runner() -> ProcessRunner:
    return _runner[0]
