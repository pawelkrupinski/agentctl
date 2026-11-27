"""WATCH A LANDED SHA (watchtick.watch_landed), characterised with the claim, the owner lock, the watch loop and the
worktree closing faked: nothing watched, another follower's verdict read, a claim still theirs at our bound, one taken
over, the owner lock refused (the claim given back or not), the loop's verdict and the close that follows green."""
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
from harness import case_context  # noqa: E402

FLOWS = {"main": ("Main", "main.yml")}
SHA = "abcdef0123456789"


def run_landed(module, script):
    """watch_landed(...) over `module`: (the return, the log, the printed text)."""
    log, out = [], io.StringIO()

    class Clock:
        def time(self):
            return 5000.0

    class Claim:
        def __init__(self, store, repo, sha, pushed_at, o_r, clock, close=None, ctx=None):
            log.append(f"claim {repo} {sha[:10]} {pushed_at} {o_r} {close}")

        def look(self):
            log.append("look")
            return script["look"]

        def wait_theirs(self, it):
            log.append("wait_theirs")
            return script["waited"]

        def release(self):
            log.append("release claim")

    class FakeOwned:
        holder = {"pid": 9, "owner": "me"}

        def __init__(self, kind, key, argv):
            log.append(f"owned {kind} {key} {argv}")

        def acquire(self):
            log.append("acquire")
            return script.get("acquired", True)

        def release(self):
            log.append("release owned")

    def fixes_of(repo, shas, store, ctx=None):
        if script.get("fixes_fail"):
            raise RuntimeError("no git")
        return set(script.get("trailers", ()))

    def watch(repo, sha, pushed_at, o_r, flows, own, clock, fixes=(), claim=None, *, store, ctx=None):
        log.append(f"_watch {pushed_at} {fixes} claim={claim is not None}")
        return script.get("rc", 0)

    patches = {"WatchClaim": Claim, "Owned": FakeOwned, "fixes_of": fixes_of, "_watch": watch,
               "close_worktree": lambda path: f"closed {path}", "slug": lambda repo: "o/r",
               "WATCH": {"o/r": FLOWS}}
    with contextlib.ExitStack() as stack:
        for name, fake in patches.items():
            stack.enter_context(mock.patch.object(module, name, fake))
        stack.enter_context(mock.patch.dict(os.environ, {"AGENTCTL_WATCH_TIMEOUT": "900"}))
        stack.enter_context(contextlib.redirect_stdout(out))
        rc = module.watch_landed("REPO", SHA, script.get("pushed_at"), Clock(), None, o_r=script.get("o_r"),
                                 close=script.get("close"), fixes=script.get("fixes", ()), ctx=case_context())
    return rc, log, out.getvalue()


def decided(outcome, verdict=None):
    return {"data": {"verdict": verdict, "outcome": outcome}, "state": outcome}


SCENARIOS = {
    "nothing watched": dict(o_r="other/x", look=(None, None)),
    "decided ok, with its verdict": dict(look=("decided", decided("ok", "WATCH OK: theirs"))),
    "decided red, no verdict text": dict(look=("decided", decided("red"))),
    "decided ok, closes": dict(look=("decided", decided("ok", "OK")), close="/wt/x"),
    "theirs, then decided": dict(look=("theirs", {"data": {}}), waited=("decided", decided("ok", "OK"))),
    "theirs, still theirs": dict(look=("theirs", {"data": {}}), waited=("theirs", {"data": {"followed_by": "h:1"}})),
    "theirs, then taken over": dict(look=("theirs", {"data": {}}), waited=("mine", {"data": {}}), rc=3),
    "mine, green, closes": dict(look=("mine", {"data": {}}), close="/wt/x", rc=0, pushed_at=77.0),
    "mine, red": dict(look=("mine", {"data": {}}), close="/wt/x", rc=3),
    "unclaimed: store down": dict(look=(None, None), rc=2, fixes=("b", "a"), trailers=("c", "a")),
    "unclaimed: fixes unreadable": dict(look=(None, None), fixes=("b", "a"), fixes_fail=True),
    "owner lock refused, mine": dict(look=("mine", {"data": {}}), acquired=False),
    "owner lock refused, unclaimed": dict(look=(None, None), acquired=False, close="/wt/x"),
}


class WatchLandedTest(unittest.TestCase):
    def test_every_scenario_does_what_it_did(self):
        module = importlib.import_module("agentctl.watchtick")
        for name, script in SCENARIOS.items():
            with self.subTest(name):
                self.assertEqual(run_landed(module, script), EXPECTED[name], name)


EXPECTED = {'nothing watched': (0, [], "watch: no CI watched for other/x (only movies' Main and convergence are)\n"),
 'decided ok, with its verdict': (0, ['claim REPO abcdef0123 5000.0 o/r None', 'look'], 'WATCH OK: theirs\n'),
 'decided red, no verdict text': (3,
                                  ['claim REPO abcdef0123 5000.0 o/r None', 'look'],
                                  "WATCH RED: abcdef0123 (its watch item's verdict)\n"),
 'decided ok, closes': (0, ['claim REPO abcdef0123 5000.0 o/r /wt/x', 'look'], 'OK\nclosed /wt/x\n'),
 'theirs, then decided': (0, ['claim REPO abcdef0123 5000.0 o/r None', 'look', 'wait_theirs'], 'OK\n'),
 'theirs, still theirs': (2,
                          ['claim REPO abcdef0123 5000.0 o/r None', 'look', 'wait_theirs'],
                          'WATCH UNKNOWN: abcdef0123 is watched by h:1, undecided after 15 min; check `agentctl '
                          'ci-why` later\n'),
 'theirs, then taken over': (3,
                             ['claim REPO abcdef0123 5000.0 o/r None',
                              'look',
                              'wait_theirs',
                              "owned watch o/r-abcdef0123456789 ['watch', '--repo', 'REPO', '--sha', "
                              "'abcdef0123456789', '--pushed-at', '5000.0']",
                              'acquire',
                              '_watch 5000.0 [] claim=True',
                              'release owned'],
                             ''),
 'mine, green, closes': (0,
                         ['claim REPO abcdef0123 77.0 o/r /wt/x',
                          'look',
                          "owned watch o/r-abcdef0123456789 ['watch', '--repo', 'REPO', '--sha', 'abcdef0123456789', "
                          "'--pushed-at', '77.0', '--close', '/wt/x']",
                          'acquire',
                          '_watch 77.0 [] claim=True',
                          'release owned'],
                         'closed /wt/x\n'),
 'mine, red': (3,
               ['claim REPO abcdef0123 5000.0 o/r /wt/x',
                'look',
                "owned watch o/r-abcdef0123456789 ['watch', '--repo', 'REPO', '--sha', 'abcdef0123456789', "
                "'--pushed-at', '5000.0', '--close', '/wt/x']",
                'acquire',
                '_watch 5000.0 [] claim=True',
                'release owned'],
               ''),
 'unclaimed: store down': (2,
                           ['claim REPO abcdef0123 5000.0 o/r None',
                            'look',
                            "owned watch o/r-abcdef0123456789 ['watch', '--repo', 'REPO', '--sha', "
                            "'abcdef0123456789', '--pushed-at', '5000.0', '--fixes', 'a', '--fixes', 'b', '--fixes', "
                            "'c']",
                            'acquire',
                            "_watch 5000.0 ['a', 'b', 'c'] claim=False",
                            'release owned'],
                           ''),
 'unclaimed: fixes unreadable': (0,
                                 ['claim REPO abcdef0123 5000.0 o/r None',
                                  'look',
                                  "owned watch o/r-abcdef0123456789 ['watch', '--repo', 'REPO', '--sha', "
                                  "'abcdef0123456789', '--pushed-at', '5000.0', '--fixes', 'a', '--fixes', 'b']",
                                  'acquire',
                                  "_watch 5000.0 ['a', 'b'] claim=False",
                                  'release owned'],
                                 ''),
 'owner lock refused, mine': (2,
                              ['claim REPO abcdef0123 5000.0 o/r None',
                               'look',
                               "owned watch o/r-abcdef0123456789 ['watch', '--repo', 'REPO', '--sha', "
                               "'abcdef0123456789', '--pushed-at', '5000.0']",
                               'acquire',
                               'release claim'],
                              'WATCH UNKNOWN: another agentctl (pid 9, me) already watches abcdef0123; its verdict '
                              'is its own (`agentctl orphans` lists owners)\n'),
 'owner lock refused, unclaimed': (2,
                                   ['claim REPO abcdef0123 5000.0 o/r /wt/x',
                                    'look',
                                    "owned watch o/r-abcdef0123456789 ['watch', '--repo', 'REPO', '--sha', "
                                    "'abcdef0123456789', '--pushed-at', '5000.0', '--close', '/wt/x']",
                                    'acquire'],
                                   'WATCH UNKNOWN: another agentctl (pid 9, me) already watches abcdef0123; its '
                                   'verdict is its own (`agentctl orphans` lists owners)\n')}
