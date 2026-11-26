"""`agentctl gov <subcommand>` (govcmd.cmd_gov), characterised for every subcommand with the governor's collaborators faked:
each prints its line(s) and returns its code (admit and route by their answer, an unknown subcommand 2). `run_gov` takes the
module so a copy of the code can be driven the same way."""
import argparse
import contextlib
import importlib
import io
import os
import sys
import tempfile
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from test_agentctl import A  # noqa: E402,F401 (puts lib on the path)

NOW = 1_000_000.0


class FakeTime:
    @staticmethod
    def time():
        return NOW


def admit_row(cls, waited, at=NOW - 100):
    return {"class": cls, "waited": waited, "at": at, "end": at}


def run_gov(module, script):
    """cmd_gov(Namespace(**args)) over `module`: (the return, the log of calls, the printed text)."""
    log, out = [], io.StringIO()

    def record(name, ret):
        def fake(*a, **k):
            log.append(f"{name} {a} {sorted(k.items())}")
            return ret(*a, **k) if callable(ret) else ret
        return fake

    usage = script.get("usage", [])
    with tempfile.TemporaryDirectory() as gdir, contextlib.ExitStack() as stack:
        if script.get("evictions"):
            with open(os.path.join(gdir, "gov.log"), "w") as f:
                f.write("a started\n" + " evicted x\n" * script["evictions"])
        patches = {
            "gov_admit": record("admit", script.get("admitted")),
            "gov_record_usage": record("record_usage", script.get("recorded")),
            "gov_job_cores": record("job_cores", 3.5),
            "gov_route": record("route", script.get("route", ("ci", "free"))),
            "admit_rows": lambda: script.get("rows", []),
            "gov_replay": record("replay", script.get("replayed")),
            "machine_cores": lambda: 8,
            "idle_servers": lambda: script.get("idle", []),
            "gov_evict": record("evict", script.get("evicted")),
            "gov_capacity": lambda *a: {"state": "busy", "busy": 6.25, "cores": 8, "charged": 5, "measured": 6.0},
            "gov_tickets": lambda: [1, 2, 3],
            "gov_waiters": lambda: [1],
            "machine_load1": lambda: 2.5,
            "gov_status": lambda: print("STATUS") or 0,
            "cmd_checks": lambda ns: print(f"checks {ns.ccmd} {ns.days}") or script.get("checks_rc", 0),
            "gov_usage": lambda days: usage,
            "gov_measured_cores": lambda job, rows: None if job == "unmeasured" else 2.0,
            "gov_dir": lambda: gdir,
            "time": FakeTime, "epoch_now": FakeTime.time,
        }
        for name, fake in patches.items():
            stack.enter_context(mock.patch.object(module, name, fake))
        stack.enter_context(contextlib.redirect_stdout(out))
        rc = module.cmd_gov(argparse.Namespace(**script["args"]))
    return rc, log, out.getvalue()


BASE = dict(cls="sbt", mem=4, label=None, kind="job", fd=3, ticket=None, try_=False, force=False, pid=77, cwd="/wt",
            cores=None, job="testUnit", min_cores=None)
USAGE = [{"job": "testUnit"}, {"job": "testUnit"}, {"job": "unmeasured"}, {"label": "no job"}]
ADMIT_ROWS = [admit_row("check", 10), admit_row("check", 120), admit_row("red", 0), admit_row("check", 5, at=NOW - 10 * 86400)]
REPLAYED = {"old": [("a", 2.0)], "new": [("a", 2.0), ("b", 1.5)], "waiting": [("c", 4.0)], "old_peak": 6.0, "new_peak": 5.5,
            "old_wait_p50": 120.0, "new_wait_p50": 30.0}
SCENARIOS = {
    "probe": dict(args=dict(gcmd="probe")),
    "admit, admitted": dict(args=dict(gcmd="admit", **BASE), admitted={"id": 1}),
    "admit, refused, try": dict(args=dict(gcmd="admit", **{**BASE, "try_": True, "label": "L", "cores": 2.0}), admitted=None),
    "usage, recorded": dict(args=dict(gcmd="usage", job="j", wall=120.0, cpu=300.0, load1=2.0, label=None, meter="m"),
                            recorded={"cores": 2.5}),
    "usage, too short": dict(args=dict(gcmd="usage", job="j", wall=5.0, cpu=1.0, load1=2.0, label="x", meter=None), recorded=None),
    "cores": dict(args=dict(gcmd="cores", job="testUnit", cls="sbt")),
    "route, ci": dict(args=dict(gcmd="route", job="j", cls="sbt", mem=4), route=("ci", "ci-runner is free")),
    "route, local": dict(args=dict(gcmd="route", job="j", cls="sbt", mem=4), route=("local", "busy")),
    "replay": dict(args=dict(gcmd="replay", at="2026-01-01T00:00:00Z", cores=None, hold=15.0), replayed=REPLAYED,
                   rows=[{"at": 1767225600.0}, {"end": 1767225600.0 + 3600}, {"at": 5.0}]),
    "replay, no waiting": dict(args=dict(gcmd="replay", at="2026-01-01T00:00:00+00:00", cores=4, hold=1.0),
                               replayed={**REPLAYED, "new": [], "old": [], "waiting": []}),
    "evict, dry run, idle": dict(args=dict(gcmd="evict", dry_run=True), idle=[(900.0, "/root/a", 1), (60.0, "/root/b", 2)]),
    "evict, dry run, none": dict(args=dict(gcmd="evict", dry_run=True)),
    "evict, evicted": dict(args=dict(gcmd="evict", dry_run=False), evicted="/root/a"),
    "evict, none idle": dict(args=dict(gcmd="evict", dry_run=False), evicted=None),
    "capacity": dict(args=dict(gcmd="capacity")),
    "status": dict(args=dict(gcmd="status")),
    "stats": dict(args=dict(gcmd="stats", days=7), rows=ADMIT_ROWS, usage=USAGE, evictions=2, checks_rc=0),
    "stats, nothing": dict(args=dict(gcmd="stats", days=1), checks_rc=5),
    "unknown": dict(args=dict(gcmd="nope")),
}


class GovCmdTest(unittest.TestCase):
    def test_every_scenario_does_what_it_did(self):
        module = importlib.import_module("agentctl.govcmd")
        for name, script in SCENARIOS.items():
            with self.subTest(name):
                self.assertEqual(run_gov(module, script), EXPECTED[name], name)


EXPECTED = {'probe': (0, [], 'agentctl governor: admit cores route width grant\n'),
 'admit, admitted': (0,
                     ["admit ('sbt', 4, '') [('cores', None), ('cwd', '/wt'), ('fd', 3), ('force', False), ('job', "
                      "'testUnit'), ('kind', 'job'), ('min_cores', None), ('pid', 77), ('ticket', None), "
                      "('wait', True)]"],
                     ''),
 'admit, refused, try': (1,
                         ["admit ('sbt', 4, 'L') [('cores', 2.0), ('cwd', '/wt'), ('fd', 3), ('force', False), "
                          "('job', 'testUnit'), ('kind', 'job'), ('min_cores', None), ('pid', 77), ('ticket', None), "
                          "('wait', False)]"],
                         ''),
 'usage, recorded': (0, ["record_usage ('j', 120.0, 300.0, 2.0, '', 'm') []"], 'j: 2.5 cores over 120 s\n'),
 'usage, too short': (0, ["record_usage ('j', 5.0, 1.0, 2.0, 'x', None) []"], 'j: under 20 s, not recorded\n'),
 'cores': (0, ["job_cores ('testUnit', 'sbt') []"], '3.5\n'),
 'route, ci': (0, ["route ('j', 'sbt', 4) []"], 'ci: ci-runner is free\n'),
 'route, local': (1, ["route ('j', 'sbt', 4) []"], 'local: busy\n'),
 'replay': (0,
            ["replay ([{'at': 1767225600.0}, {'end': 1767229200.0}], 8.0, 1767225600.0, 900.0) []"],
            'replay at 2026-01-01T00:00:00Z (cap 8 cores; a ticket with no end record held 15 min):\n'
            '  as admitted:    1 live, 2 cores: a 2\n'
            '  the cores rule: 2 live, 3.5 cores: a 2, b 1.5; 1 waiting: c 4\n'
            '  peak cores live: 6 as admitted, 5.5 under the rule; p50 wait 2 min as admitted, 0 min under the '
            'rule\n'),
 'replay, no waiting': (0,
                        ['replay ([], 4.0, 1767225600.0, 60.0) []'],
                        'replay at 2026-01-01T00:00:00+00:00 (cap 4 cores; a ticket with no end record held 1 min):\n'
                        '  as admitted:    0 live, 0 cores: -\n'
                        '  the cores rule: 0 live, 0 cores: -; 0 waiting: -\n'
                        '  peak cores live: 6 as admitted, 5.5 under the rule; p50 wait 2 min as admitted, 0 min '
                        'under the rule\n'),
 'evict, dry run, idle': (0, [], 'would evict /root/a (unused 15 min)\nwould evict /root/b (unused 1 min)\n'),
 'evict, dry run, none': (0, [], ''),
 'evict, evicted': (0, ["evict ('by hand (agentctl gov evict)',) []"], 'evicted /root/a\n'),
 'evict, none idle': (0, ["evict ('by hand (agentctl gov evict)',) []"], 'no idle sbt server to evict\n'),
 'capacity': (0, [], 'busy: 6.2 of 8 cores busy (5 charged, 6.0 measured, load 2.5); 3 admitted, 1 waiting\n'),
 'status': (0, [], 'STATUS\n'),
 'stats': (0,
           ["job_cores ('testUnit',) []", "job_cores ('unmeasured',) []"],
           'checks stats 7\n'
           '  class admits  p50 wait  p90 wait waited>1m\n'
           '    red      1        0s        0s         0\n'
           '  check      2       10s      120s         1\n'
           'cores testUnit: 2 runs, charged 3.5 (measured p75)\n'
           'cores unmeasured: 1 runs, charged 3.5 (default)\n'
           'evictions (all time): 2\n'),
 'stats, nothing': (5, [], 'checks stats 1\n  class admits  p50 wait  p90 wait waited>1m\nevictions (all time): 0\n'),
 'unknown': (2, [], '')}
