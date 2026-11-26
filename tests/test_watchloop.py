"""THE FOLLOWER'S WATCH LOOP (watchtick._watch), characterised with the runs read, the verdict, ci-why and the clock faked:
what each ending prints and gives the claim (green, red with its ci-why, no token, unknown at the bound with GitHub
unreachable or not, stalled), a failed read backed off and recovered, a run started late. `run_watch` takes the module so a
copy of the code can be driven the same way."""
import contextlib
import importlib
import io
import os
import sys
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from test_agentctl import A  # noqa: E402,F401 (puts lib on the path)

FLOWS = {"main": ("Main", "main.yml"), "conv": ("Convergence", "conv.yml")}


class StepClock:
    def __init__(self):
        self.now = 1000.0

    def time(self):
        return self.now

    def sleep(self, n):
        self.now += n


class FakeClaim:
    def __init__(self, log):
        self.log = log

    def finish(self, state, line, red_runs):
        self.log.append(f"claim finish {state} {line!r} {list(red_runs)}")

    def renew(self):
        self.log.append("claim renew")


def run_watch(module, script):
    """_watch(...) over `module`: looks (one per pass: a dict of seen runs, an exception, or a (seen, runs started, done
    kinds) tuple) and verdicts (one per pass) as scripted; (the return, the log, the printed text)."""
    log, clock = [], StepClock()
    looks, verdicts = iter(script["looks"]), iter(script.get("verdicts", []))

    def watch_look(repo, sha, o_r, flows, done, pushed_at, t0, now, on_run):
        got = next(looks)
        log.append(f"look t={now - 1000.0:g}")
        if isinstance(got, BaseException):
            raise got
        seen, started, finished = got if isinstance(got, tuple) else (got, [], [])
        for kind in finished:
            done[kind] = ({"databaseId": 1}, True)
        for kind, run in started:
            on_run(kind, run)
        return seen

    def watch_verdict(o_r, sha, flows, done):
        got = next(verdicts, None)
        log.append(f"verdict {sorted(done)} {got}")
        return got

    out = io.StringIO()
    env = {"AGENTCTL_WATCH_TIMEOUT": str(script.get("timeout", 600)), "AGENTCTL_WATCH_STALL_MIN": str(script.get("stall", 5)),
           "AGENTCTL_WATCH_POLL": "60", "AGENTCTL_WATCH_ERROR_POLL": "30"}
    with contextlib.ExitStack() as stack:
        for name, fake in {
            "watch_look": watch_look, "watch_verdict": watch_verdict,
            "err": lambda text: log.append(f"err {text}"),
            "ci_why": lambda o_r, n, repo=None, store=None: log.append(f"ci_why {o_r} {n}") or (0, "THE WHY"),
            "restart_if_changed": lambda owners: log.append(f"restart {owners}"),
            "iso_at": lambda t: t - 1000.0, "age_since": lambda t: f"{t:g}s after the start",
        }.items():
            stack.enter_context(mock.patch.object(module, name, fake))
        stack.enter_context(mock.patch.dict(os.environ, env))
        stack.enter_context(contextlib.redirect_stdout(out))
        claim = FakeClaim(log) if script.get("claim") else None
        rc = module._watch("REPO", "abcdef0123456789", 900.0, "o/r", FLOWS, "OWN", clock, script.get("fixes", ()), claim,
                           store=None)
    return rc, log, out.getvalue()


BOOM = RuntimeError("503 from GitHub")
RUN = {"databaseId": 7, "number": 12, "status": "in_progress", "headSha": "abcdef0123456789", "conclusion": None}
SCENARIOS = {
    "green at once": dict(looks=[{}], verdicts=[("ok", "WATCH OK: green")], claim=True),
    "green, fixes named": dict(looks=[{}], verdicts=[("ok", "WATCH OK: green")], fixes=("run 1", "run 2")),
    "red": dict(looks=[{}], verdicts=[("red", "WATCH RED: main", [("main.yml", 5)])], claim=True),
    "no token": dict(looks=[A.NoToken("agentctl: no vault")], claim=True),
    "runs started, then green": dict(looks=[({"main": (7, "queued")}, [("main", RUN), ("conv", None)], []),
                                            ({"main": (7, "completed")}, [("main", RUN)], ["main"])],
                                     verdicts=[None, ("ok", "WATCH OK")]),
    "bound reached": dict(looks=[{}] * 12, timeout=300, stall=60 * 24),
    "bound reached with a run seen": dict(looks=[{"main": (7, "queued")}] * 12, timeout=300, stall=60 * 24, claim=True),
    "stalled": dict(looks=[{"main": (7, "queued")}] * 12, timeout=6000, stall=3, claim=True),
    "progress delays the stall": dict(looks=[{"main": (7, "queued")}, {"main": (7, "queued")}, {"main": (7, "running")},
                                             {"main": (7, "running")}, {"main": (7, "running")}, {"main": (7, "done")}],
                                      verdicts=[None] * 5 + [("ok", "WATCH OK")], timeout=6000, stall=3),
    "unreachable, then answers": dict(looks=[BOOM, BOOM, {}], verdicts=[None, None, ("ok", "WATCH OK")], timeout=6000),
    "unreachable at the bound": dict(looks=[BOOM] * 20, timeout=200, claim=True),
    "unreachable never stalls": dict(looks=[BOOM] * 20, timeout=900, stall=1),
}


class WatchLoopTest(unittest.TestCase):
    def test_every_scenario_does_what_it_did(self):
        module = importlib.import_module("agentctl.watchtick")
        for name, script in SCENARIOS.items():
            with self.subTest(name):
                self.assertEqual(run_watch(module, script), EXPECTED[name], name)


EXPECTED = {'green at once': (0,
                   ['err watching Main and the convergence for abcdef0123 (up to 10 min; `--no-watch` skips this)',
                    'look t=0',
                    "verdict [] ('ok', 'WATCH OK: green')",
                    "claim finish ok 'WATCH OK: green' []"],
                   'WATCH OK: green\n'),
 'green, fixes named': (0,
                        ['err watching Main and the convergence for abcdef0123 (up to 10 min; `--no-watch` skips '
                         "this); its Fixes-Red run 1, run 2 is verified by the red's own item (`agentctl red "
                         'status`)',
                         'look t=0',
                         "verdict [] ('ok', 'WATCH OK: green')"],
                        'WATCH OK: green\n'),
 'red': (3,
         ['err watching Main and the convergence for abcdef0123 (up to 10 min; `--no-watch` skips this)',
          'look t=0',
          "verdict [] ('red', 'WATCH RED: main', [('main.yml', 5)])",
          "claim finish red 'WATCH RED: main' [('main.yml', 5)]",
          'ci_why o/r 4'],
         'WATCH RED: main\nTHE WHY\n'),
 'no token': (2,
              ['err watching Main and the convergence for abcdef0123 (up to 10 min; `--no-watch` skips this)',
               'look t=0',
               "claim finish unknown 'WATCH UNKNOWN: no token (no vault); check `agentctl ci-why` for abcdef0123' "
               '[]'],
              'WATCH UNKNOWN: no token (no vault); check `agentctl ci-why` for abcdef0123\n'),
 'runs started, then green': (0,
                              ['err watching Main and the convergence for abcdef0123 (up to 10 min; `--no-watch` '
                               'skips this)',
                               'look t=0',
                               'err Main: following run #12 (in_progress, head abcdef0123)',
                               'verdict [] None',
                               "restart ['OWN']",
                               'look t=60',
                               "verdict ['main'] ('ok', 'WATCH OK')"],
                              'conv: none started within 60 min of the push\nMain #12: None\nWATCH OK\n'),
 'bound reached': (2,
                   ['err watching Main and the convergence for abcdef0123 (up to 5 min; `--no-watch` skips this)',
                    'look t=0',
                    'verdict [] None',
                    "restart ['OWN']",
                    'look t=60',
                    'verdict [] None',
                    "restart ['OWN']",
                    'look t=150',
                    'verdict [] None',
                    "restart ['OWN']",
                    'look t=285',
                    'verdict [] None',
                    "restart ['OWN']",
                    'look t=487.5',
                    'verdict [] None'],
                   'WATCH UNKNOWN: Main, Convergence undecided after 5 min; check `agentctl ci-why` later\n'),
 'bound reached with a run seen': (2,
                                   ['err watching Main and the convergence for abcdef0123 (up to 5 min; `--no-watch` '
                                    'skips this)',
                                    'look t=0',
                                    'verdict [] None',
                                    'claim renew',
                                    "restart ['OWN']",
                                    'look t=60',
                                    'verdict [] None',
                                    'claim renew',
                                    "restart ['OWN']",
                                    'look t=150',
                                    'verdict [] None',
                                    'claim renew',
                                    "restart ['OWN']",
                                    'look t=285',
                                    'verdict [] None',
                                    'claim renew',
                                    "restart ['OWN']",
                                    'look t=487.5',
                                    'verdict [] None',
                                    "claim finish unknown 'WATCH UNKNOWN: Main, Convergence undecided after 5 min; "
                                    "check `agentctl ci-why` later' []"],
                                   'WATCH UNKNOWN: Main, Convergence undecided after 5 min; check `agentctl ci-why` '
                                   'later\n'),
 'stalled': (2,
             ['err watching Main and the convergence for abcdef0123 (up to 100 min; `--no-watch` skips this)',
              'look t=0',
              'verdict [] None',
              'claim renew',
              "restart ['OWN']",
              'look t=60',
              'verdict [] None',
              'claim renew',
              "restart ['OWN']",
              'look t=150',
              'verdict [] None',
              'claim renew',
              "restart ['OWN']",
              'look t=285',
              'verdict [] None',
              "claim finish unknown 'WATCH STALLED: no change in Main run 7 queued, Convergence no run for 3 min; "
              "check `agentctl ci-why` later' []"],
             'WATCH STALLED: no change in Main run 7 queued, Convergence no run for 3 min; check `agentctl ci-why` '
             'later\n'),
 'progress delays the stall': (2,
                               ['err watching Main and the convergence for abcdef0123 (up to 100 min; `--no-watch` '
                                'skips this)',
                                'look t=0',
                                'verdict [] None',
                                "restart ['OWN']",
                                'look t=60',
                                'verdict [] None',
                                "restart ['OWN']",
                                'look t=150',
                                'verdict [] None',
                                "restart ['OWN']",
                                'look t=285',
                                'verdict [] None',
                                "restart ['OWN']",
                                'look t=487.5',
                                'verdict [] None'],
                               'WATCH STALLED: no change in Main run 7 running, Convergence no run for 3 min; check '
                               '`agentctl ci-why` later\n'),
 'unreachable, then answers': (0,
                               ['err watching Main and the convergence for abcdef0123 (up to 100 min; `--no-watch` '
                                'skips this)',
                                'look t=0',
                                'err watch: could not read the runs (503 from GitHub) [1 failed, unreachable for 0s '
                                'after the start]',
                                'verdict [] None',
                                "restart ['OWN']",
                                'look t=30',
                                'err watch: could not read the runs (503 from GitHub) [2 failed, unreachable for 0s '
                                'after the start]',
                                'verdict [] None',
                                "restart ['OWN']",
                                'look t=75',
                                'err watch: GitHub answers again after 0s after the start (2 failed reads)',
                                "verdict [] ('ok', 'WATCH OK')"],
                               'WATCH OK\n'),
 'unreachable at the bound': (2,
                              ['err watching Main and the convergence for abcdef0123 (up to 3 min; `--no-watch` '
                               'skips this)',
                               'look t=0',
                               'err watch: could not read the runs (503 from GitHub) [1 failed, unreachable for 0s '
                               'after the start]',
                               'verdict [] None',
                               'claim renew',
                               "restart ['OWN']",
                               'look t=30',
                               'err watch: could not read the runs (503 from GitHub) [2 failed, unreachable for 0s '
                               'after the start]',
                               'verdict [] None',
                               'claim renew',
                               "restart ['OWN']",
                               'look t=75',
                               'err watch: could not read the runs (503 from GitHub) [3 failed, unreachable for 0s '
                               'after the start]',
                               'verdict [] None',
                               'claim renew',
                               "restart ['OWN']",
                               'look t=142.5',
                               'err watch: could not read the runs (503 from GitHub) [4 failed, unreachable for 0s '
                               'after the start]',
                               'verdict [] None',
                               'claim renew',
                               "restart ['OWN']",
                               'look t=243.75',
                               'err watch: could not read the runs (503 from GitHub) [5 failed, unreachable for 0s '
                               'after the start]',
                               'verdict [] None',
                               'claim finish unknown "WATCH UNKNOWN: GitHub\'s runs could not be read for 0s after '
                               'the start (5 failed reads, the last: 503 from GitHub); Main, Convergence undecided '
                               'after 3 min; check `agentctl ci-why` later" []'],
                              "WATCH UNKNOWN: GitHub's runs could not be read for 0s after the start (5 failed "
                              'reads, the last: 503 from GitHub); Main, Convergence undecided after 3 min; check '
                              '`agentctl ci-why` later\n'),
 'unreachable never stalls': (2,
                              ['err watching Main and the convergence for abcdef0123 (up to 15 min; `--no-watch` '
                               'skips this)',
                               'look t=0',
                               'err watch: could not read the runs (503 from GitHub) [1 failed, unreachable for 0s '
                               'after the start]',
                               'verdict [] None',
                               "restart ['OWN']",
                               'look t=30',
                               'err watch: could not read the runs (503 from GitHub) [2 failed, unreachable for 0s '
                               'after the start]',
                               'verdict [] None',
                               "restart ['OWN']",
                               'look t=75',
                               'err watch: could not read the runs (503 from GitHub) [3 failed, unreachable for 0s '
                               'after the start]',
                               'verdict [] None',
                               "restart ['OWN']",
                               'look t=142.5',
                               'err watch: could not read the runs (503 from GitHub) [4 failed, unreachable for 0s '
                               'after the start]',
                               'verdict [] None',
                               "restart ['OWN']",
                               'look t=243.75',
                               'err watch: could not read the runs (503 from GitHub) [5 failed, unreachable for 0s '
                               'after the start]',
                               'verdict [] None',
                               "restart ['OWN']",
                               'look t=395.625',
                               'err watch: could not read the runs (503 from GitHub) [6 failed, unreachable for 0s '
                               'after the start]',
                               'verdict [] None',
                               "restart ['OWN']",
                               'look t=623.438',
                               'err watch: could not read the runs (503 from GitHub) [7 failed, unreachable for 0s '
                               'after the start]',
                               'verdict [] None',
                               "restart ['OWN']",
                               'look t=923.438',
                               'err watch: could not read the runs (503 from GitHub) [8 failed, unreachable for 0s '
                               'after the start]',
                               'verdict [] None'],
                              "WATCH UNKNOWN: GitHub's runs could not be read for 0s after the start (8 failed "
                              'reads, the last: 503 from GitHub); Main, Convergence undecided after 15 min; check '
                              '`agentctl ci-why` later\n')}
