# agentctl, part `cliselftest`: the fast smoke check of the code a service would re-exec onto
# A real module (lib/agentctl/__init__.py MODULES): its own imports, the standard library's and MODULES' only.

import contextlib
import datetime as dt
import io
from typing import Any, Callable
from agentctl.timefmt import age_since
from agentctl.hookoutput import check_failures
from agentctl.owned import restart_gate
from agentctl.context import Context
from agentctl.ciwhy import gate_from_snap, outside_from_snap
from agentctl.localrecheck import recheck_local


def selftest(ctx: Context, main: Callable[[list[str]], Any]) -> int:
    """Fast smoke check of the code a service would re-exec onto: the parser builds, the failure parsers and the
    snapshot readers work on fixtures, a fixture gate reads green."""
    snap = {"at": ctx.clock.time(), "repo": "x/y", "outside_error": None, "outside": [],
            "gate": [{"name": "Main", "decided": {"databaseId": 1, "number": 1, "status": "completed",
                                                  "conclusion": "success"}, "newer": None, "since": None, "count": 0,
                      "runs": []}]}
    assert gate_from_snap(snap, ctx=ctx)[0][0] == "Main" and outside_from_snap(snap, ctx=ctx) == []
    assert set(check_failures("! pre-push sbt FAILED (3s)\n")) == {"sbt"}
    assert age_since("2026-01-01T00:00:00Z", dt.datetime(2026, 1, 1, 1, 5, tzinfo=dt.timezone.utc)) == "1h 5m"
    assert callable(recheck_local) and callable(restart_gate)
    try:
        with contextlib.redirect_stdout(io.StringIO()):
            main(["ci-why", "--help"])
    except SystemExit as e:
        assert e.code == 0
    print("selftest ok")
    return 0
