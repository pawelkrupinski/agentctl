"""Test-side readers of agentctl's status line and machine deadlines: the status line is the header's `line` field
(`status --line` prints it), and the deadline walk is a property of the machines table the tests assert."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from test_agentctl import A  # noqa: E402


def status_line(now_=None):
    """The header's line, as `status --line` prints it (no git, no GitHub, state.json and at most one GET /v1/state)."""
    t = now_ if now_ is not None else A.epoch_now()
    return A.status_header(t, probes=False)["line"]


def deadline_walk(typ, state):
    """The deadline-only path from `state`: (reached terminal?, cycle escalates?)."""
    seen = []
    while state not in seen:
        seen.append(state)
        spec = A.MACHINES[typ][state]
        if spec.terminal:
            return True, False
        state = spec.on_deadline
    cycle = seen[seen.index(state):]
    return False, any(A.MACHINES[typ][x].escalate for x in cycle)
