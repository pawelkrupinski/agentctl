"""THE BATCH EXECUTOR (batchcheck.check_batch), characterised branch by branch with every collaborator faked: the leases
and the slot, the check and its infra retry, the stall, the drift check and split, the bisect and its culprit's
judgement (rejected, checked alone, the red pre-check's variants). Each scenario's log is what the executor did, in
order; `run_check_batch` takes the module so a copy of the code can be driven the same way."""
import contextlib
import copy
import importlib
import os
import sys
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from test_agentctl import A  # noqa: E402,F401 (puts lib on the path)

REDGATE_OUT = "NOT LANDED: main is red: only a fix"
INFRA_OUT = "[error] sbt server disconnected"


class FakeBatchStore:
    def __init__(self, log, script):
        self.log, self.script = log, script

    def item(self, kind, ident):
        if kind == "change":
            return self.script.get("change")
        return copy.deepcopy(self.script["item"])

    def transition(self, kind, ident, state, version, event=None, data=None):
        self.log.append(f"transition {kind} {ident} {state} {event}")


class FakeHold:
    def __init__(self, log, name, ok):
        self.log, self.name, self.ok = log, name, ok

    def acquire(self):
        self.log.append(f"acquire {self.name} {self.ok}")
        return self.ok

    def release(self):
        self.log.append(f"release {self.name}")


class FakeClock:
    def time(self):
        return 1000.0


def run_check_batch(module, script):
    """check_batch(repo, bid, clock, st) over `module` with the script's answers; (result, the log of what it did)."""
    log = []
    checks = iter(script.get("checks", []))
    members = copy.deepcopy(script.get("members", []))

    def record(name, ret=None, show=lambda *a, **k: (a, k)):
        def fake(*a, **k):
            log.append(f"{name} {show(*a, **k)}")
            return ret(*a, **k) if callable(ret) else ret
        return fake

    def batch_move(st, it, to, data=None, text=None, severity=None):
        log.append(f"batch_move {to} {data} {text!r} {severity}")
        out = copy.deepcopy(script["item"])
        out["state"] = to
        out["data"].update(data or {})
        return out

    def batch_check(repo, d, tree, base, tip, clock, st, cls, depth, runs, range_base=None):
        log.append(f"batch_check {tree} {base} {tip} {cls} {depth} {runs} {range_base}")
        return next(checks)

    patches = {
        "waiting_state": lambda repo: ("D", "CLONE", "W"),
        "LeaseHold": lambda name, ttl, mx, store=None: FakeHold(log, name, script.get("lease", True)),
        "tree_slot": lambda st, d, i, mx: FakeHold(log, f"slot-{i}", script.get("slot", True)),
        "batch_members": lambda d, it: members,
        "batch_check": batch_check,
        "batch_move": batch_move,
        "batch_drift_check": lambda repo, d, tree, base, tip, red: (log.append(f"drift {red}"), (script["drift"], "dout"))[1],
        "batch_drift_split": lambda repo, d, st, it, mem, tree, out: log.append(f"drift_split {out!r}") or 1,
        "bisect": lambda repo, d, mem, base, tree, clock, st, depth, runs, failed, range_base=None, cls=None:
            log.append(f"bisect {depth} {failed!r} {range_base} {cls}") or script["bisect"],
        "culprit_own_failure": lambda repo, d, e, base, out, now: log.append(f"own {e['id']} {out!r} {now}") or script["own"],
        "queue_verdict": record("verdict", show=lambda st, d, e, kind, text, lines: (e["id"], kind, text, lines)),
        "save_entry": record("save", show=lambda d, e: sorted(e.items())),
        "wlog": record("wlog", show=lambda d, text: text),
        "batch_fail": record("fail", show=lambda st, d, it, why, n=0: (why, n)),
        "red_prechecked": record("prechecked", show=lambda d, st, it, mem, tip, base, how, failed=None:
                                 ([e["id"] for e in mem], tip, base, how, failed)),
    }
    with contextlib.ExitStack() as stack:
        for name, fake in patches.items():
            stack.enter_context(mock.patch.object(module, name, fake))
        result = module.check_batch("REPO", "b1", FakeClock(), FakeBatchStore(log, script))
    return result, log


def member(name, **extra):
    return {"id": name, "tip": f"tip-{name}", "worktree": f"wt-{name}", **extra}


def item(state="checking", depth=None, cls=None, **data):
    base = {"tree": "7", "base": "base0123456789", "tip": "tip0123456789"}
    base.update(data)
    if depth:
        base["depth"] = depth
    if cls:
        base["cls"] = cls
    return {"id": "b1", "state": state, "version": 3, "data": base}


MEMBERS = [member("a"), member("b"), member("c")]
PASS = (True, "ok", False)
FAIL = (False, "[error] a test failed", False)
SCENARIOS = {
    "gone": dict(item=item("assembling")),
    "no lease": dict(item=item(), lease=False),
    "no slot": dict(item=item(), slot=False),
    "no members": dict(item=item()),
    "stalled": dict(item=item(), members=MEMBERS, checks=[(False, "", True)]),
    "passed, drift ok": dict(item=item(), members=MEMBERS, checks=[PASS], drift=True),
    "passed, no drift verdict": dict(item=item(depth="full", cls="red"), members=MEMBERS, checks=[PASS], drift=None),
    "passed, drift red": dict(item=item(), members=MEMBERS, checks=[PASS], drift=False),
    "red pre-check passed": dict(item=item(depth="red"), members=MEMBERS, checks=[PASS]),
    "gate red": dict(item=item(), members=MEMBERS, checks=[(False, REDGATE_OUT, False)]),
    "infra twice, bisected": dict(item=item(), members=MEMBERS, checks=[(False, INFRA_OUT, False)] * 2,
                                  bisect=(None, ""), own=True),
    "infra once, then pass": dict(item=item(), members=MEMBERS, checks=[(False, INFRA_OUT, False), PASS], drift=True),
    "bisecting, no culprit": dict(item=item("bisecting", failed_out="old"), members=MEMBERS, bisect=(None, "")),
    "bisecting, no culprit, red": dict(item=item("bisecting", depth="red"), members=MEMBERS, bisect=(None, "")),
    "culprit first, rejected": dict(item=item(), members=MEMBERS, checks=[FAIL], bisect=(0, "x\ny"), own=True),
    "culprit later, rejected": dict(item=item(), members=MEMBERS, checks=[FAIL], bisect=(2, "x\ny"), own=True),
    "full, not its own": dict(item=item(depth="full"), members=MEMBERS, checks=[FAIL], bisect=(1, "z"), own=False),
    "full, its own": dict(item=item(depth="full"), members=MEMBERS, checks=[FAIL], bisect=(1, "z"), own=True),
    "full, regenerated": dict(item=item(depth="full"), members=[member("a"), member("b", regenerated=["x.tsv"])],
                              checks=[FAIL], bisect=(1, "z"), own=True),
    "full, autoresolved": dict(item=item(depth="full"), members=[member("a", autoresolved=[{"file": "f.py", "hunks": ["h1"]}])],
                               checks=[FAIL], bisect=(0, "z"), own=True,
                               change={"id": "a", "state": "queued", "version": 2}),
    "red, culprit not its own": dict(item=item(depth="red"), members=MEMBERS, checks=[FAIL], bisect=(2, "z"), own=False),
    "red, first culprit not its own": dict(item=item(depth="red"), members=MEMBERS, checks=[FAIL], bisect=(0, "z"), own=False),
    "red, culprit its own": dict(item=item(depth="red"), members=MEMBERS, checks=[FAIL], bisect=(1, "z"), own=True),
}


class BatchCheckTest(unittest.TestCase):
    def test_every_scenario_does_what_it_did(self):
        module = importlib.import_module("agentctl.batchcheck")
        for name, script in SCENARIOS.items():
            with self.subTest(name):
                result, log = run_check_batch(module, script)
                self.assertEqual((result, log), EXPECTED[name], name)


EXPECTED = {'gone': (0, []),
 'no lease': (0, ['acquire check/batch-b1 False']),
 'no slot': (0, ['acquire check/batch-b1 True', 'acquire slot-7 False', 'release slot-7', 'release check/batch-b1']),
 'no members': (0,
                ['acquire check/batch-b1 True',
                 'acquire slot-7 True',
                 "batch_move assembling None 'batch b1: its changes are gone' None",
                 'release slot-7',
                 'release check/batch-b1']),
 'stalled': (0,
             ['acquire check/batch-b1 True',
              'acquire slot-7 True',
              'batch_check CLONE-wt-check-7 base0123456789 tip0123456789 check fast {} None',
              "batch_move held {'why': 'stalled'} 'HELD: the batch check of tip0123456 (3 change(s)) stalled twice "
              "on a fresh sbt server; checked again in 30 min' escalate",
              'release slot-7',
              'release check/batch-b1']),
 'passed, drift ok': (0,
                      ['acquire check/batch-b1 True',
                       'acquire slot-7 True',
                       'batch_check CLONE-wt-check-7 base0123456789 tip0123456789 check fast {} None',
                       'drift False',
                       "batch_move pushing {'runs': {}} 'batch b1: tip0123456 passed its check and its drift check' "
                       'None',
                       'release slot-7',
                       'release check/batch-b1']),
 'passed, no drift verdict': (0,
                              ['acquire check/batch-b1 True',
                               'acquire slot-7 True',
                               'batch_check CLONE-wt-check-7 base0123456789 tip0123456789 red full {} None',
                               'drift True',
                               "batch_move pushing {'runs': {}} 'batch b1: tip0123456 passed its check' None",
                               'release slot-7',
                               'release check/batch-b1']),
 'passed, drift red': (1,
                       ['acquire check/batch-b1 True',
                        'acquire slot-7 True',
                        'batch_check CLONE-wt-check-7 base0123456789 tip0123456789 check fast {} None',
                        'drift False',
                        "drift_split 'dout'",
                        'release slot-7',
                        'release check/batch-b1']),
 'red pre-check passed': (0,
                          ['acquire check/batch-b1 True',
                           'acquire slot-7 True',
                           'batch_check CLONE-wt-check-7 base0123456789 tip0123456789 check red {} None',
                           "prechecked (['a', 'b', 'c'], 'tip0123456789', 'base0123456789', 'its red pre-check "
                           "passed', None)",
                           'release slot-7',
                           'release check/batch-b1']),
 'gate red': (0,
              ['acquire check/batch-b1 True',
               'acquire slot-7 True',
               'batch_check CLONE-wt-check-7 base0123456789 tip0123456789 check fast {} None',
               "batch_move assembling None 'batch b1: the gate went red' None",
               'release slot-7',
               'release check/batch-b1']),
 'infra twice, bisected': (0,
                           ['acquire check/batch-b1 True',
                            'acquire slot-7 True',
                            'batch_check CLONE-wt-check-7 base0123456789 tip0123456789 check fast {} None',
                            'wlog batch b1: tip0123456 failed on infra (sbt server disconnected): checked again, not '
                            'bisected',
                            'batch_check CLONE-wt-check-7 base0123456789 tip0123456789 check fast {} None',
                            "batch_move bisecting {'failed_out': '[error] sbt server disconnected'} 'batch b1: "
                            "tip0123456 failed its check; bisecting' None",
                            "bisect fast '[error] sbt server disconnected' None check",
                            "fail ('it failed, then every prefix passed on its own (flaky or infra)', 3)",
                            'release slot-7',
                            'release check/batch-b1']),
 'infra once, then pass': (0,
                           ['acquire check/batch-b1 True',
                            'acquire slot-7 True',
                            'batch_check CLONE-wt-check-7 base0123456789 tip0123456789 check fast {} None',
                            'wlog batch b1: tip0123456 failed on infra (sbt server disconnected): checked again, not '
                            'bisected',
                            'batch_check CLONE-wt-check-7 base0123456789 tip0123456789 check fast {} None',
                            'drift False',
                            "batch_move pushing {'runs': {}} 'batch b1: tip0123456 passed its check and its drift "
                            "check' None",
                            'release slot-7',
                            'release check/batch-b1']),
 'bisecting, no culprit': (0,
                           ['acquire check/batch-b1 True',
                            'acquire slot-7 True',
                            "bisect fast 'old' None check",
                            "fail ('it failed, then every prefix passed on its own (flaky or infra)', 3)",
                            'release slot-7',
                            'release check/batch-b1']),
 'bisecting, no culprit, red': (0,
                                ['acquire check/batch-b1 True',
                                 'acquire slot-7 True',
                                 'bisect red None None check',
                                 "prechecked ([], None, 'base0123456789', 'its red pre-check failed, then every "
                                 "prefix passed (flaky or infra)', ['base0123456789', 'a', 'b', 'c'])",
                                 'release slot-7',
                                 'release check/batch-b1']),
 'culprit first, rejected': (1,
                             ['acquire check/batch-b1 True',
                              'acquire slot-7 True',
                              'batch_check CLONE-wt-check-7 base0123456789 tip0123456789 check fast {} None',
                              "batch_move bisecting {'failed_out': '[error] a test failed'} 'batch b1: tip0123456 "
                              "failed its check; bisecting' None",
                              "bisect fast '[error] a test failed' None check",
                              "verdict ('a', 'rejected', 'NOT LANDED: the checks failed with your change in the "
                              "batch (entry a)\\n  x\\n  y', ['x', 'y'])",
                              "batch_move assembling None 'batch b1: a rejected' None",
                              'release slot-7',
                              'release check/batch-b1']),
 'culprit later, rejected': (1,
                             ['acquire check/batch-b1 True',
                              'acquire slot-7 True',
                              'batch_check CLONE-wt-check-7 base0123456789 tip0123456789 check fast {} None',
                              "batch_move bisecting {'failed_out': '[error] a test failed'} 'batch b1: tip0123456 "
                              "failed its check; bisecting' None",
                              "bisect fast '[error] a test failed' None check",
                              "verdict ('c', 'rejected', 'NOT LANDED: the checks failed with your change in the "
                              "batch (entry c)\\n  x\\n  y', ['x', 'y'])",
                              "batch_move pushing {'members': ['a', 'b'], 'tip': 'tip-b', 'runs': {}} 'batch b1: c "
                              "rejected; the 2 change(s) before it land' None",
                              'release slot-7',
                              'release check/batch-b1']),
 'full, not its own': (0,
                       ['acquire check/batch-b1 True',
                        'acquire slot-7 True',
                        'batch_check CLONE-wt-check-7 base0123456789 tip0123456789 check full {} None',
                        "batch_move bisecting {'failed_out': '[error] a test failed'} 'batch b1: tip0123456 failed "
                        "its check; bisecting' None",
                        "bisect full '[error] a test failed' None check",
                        "own b 'z' 1000.0",
                        "save [('batch_alone', 'b1'), ('id', 'b'), ('tip', 'tip-b'), ('worktree', 'wt-b')]",
                        "wlog batch b1: b's prefix fails, but not shown to be by its own fault: checked alone",
                        "batch_move pushing {'members': ['a'], 'tip': 'tip-a', 'runs': {}} 'batch b1: b checked "
                        "alone; the 1 change(s) before it land' None",
                        'release slot-7',
                        'release check/batch-b1']),
 'full, its own': (1,
                   ['acquire check/batch-b1 True',
                    'acquire slot-7 True',
                    'batch_check CLONE-wt-check-7 base0123456789 tip0123456789 check full {} None',
                    "batch_move bisecting {'failed_out': '[error] a test failed'} 'batch b1: tip0123456 failed its "
                    "check; bisecting' None",
                    "bisect full '[error] a test failed' None check",
                    "own b 'z' 1000.0",
                    "verdict ('b', 'rejected', 'NOT LANDED: the checks failed with your change in the batch (entry "
                    "b)\\n  z', ['z'])",
                    "batch_move pushing {'members': ['a'], 'tip': 'tip-a', 'runs': {}} 'batch b1: b rejected; the 1 "
                    "change(s) before it land' None",
                    'release slot-7',
                    'release check/batch-b1']),
 'full, regenerated': (0,
                       ['acquire check/batch-b1 True',
                        'acquire slot-7 True',
                        'batch_check CLONE-wt-check-7 base0123456789 tip0123456789 check full {} None',
                        "batch_move bisecting {'failed_out': '[error] a test failed'} 'batch b1: tip0123456 failed "
                        "its check; bisecting' None",
                        "bisect full '[error] a test failed' None check",
                        "save [('batch_alone', 'regen'), ('id', 'b'), ('regenerated', ['x.tsv']), ('tip', 'tip-b'), "
                        "('worktree', 'wt-b')]",
                        "wlog batch b1: b's prefix fails, but not shown to be by its own fault (its x.tsv resolved "
                        'in the batch): checked alone',
                        "batch_move pushing {'members': ['a'], 'tip': 'tip-a', 'runs': {}} 'batch b1: b checked "
                        "alone; the 1 change(s) before it land' None",
                        'release slot-7',
                        'release check/batch-b1']),
 'full, autoresolved': (0,
                        ['acquire check/batch-b1 True',
                         'acquire slot-7 True',
                         'batch_check CLONE-wt-check-7 base0123456789 tip0123456789 check full {} None',
                         "batch_move bisecting {'failed_out': '[error] a test failed'} 'batch b1: tip0123456 failed "
                         "its check; bisecting' None",
                         "bisect full '[error] a test failed' None check",
                         "save [('batch_alone', 'resolve'), ('id', 'a'), ('tip', 'tip-a'), ('worktree', 'wt-a')]",
                         "wlog batch b1: a's prefix fails, but not shown to be by its own fault -- the batch's "
                         'AUTO-RESOLUTION of f.py is the culprit (h1): held behind the change it conflicts with, as '
                         'before: checked alone',
                         'transition change a queued {\'text\': "batch b1: a\'s prefix fails, but not shown to be by '
                         "its own fault -- the batch's AUTO-RESOLUTION of f.py is the culprit (h1): held behind the "
                         'change it conflicts with, as before: checked alone", \'audience\': {\'worktree\': '
                         "'wt-a'}, 'severity': 'info'}",
                         "batch_move assembling None 'batch b1: a checked alone (its auto-resolution failed the "
                         "batch)' None",
                         'release slot-7',
                         'release check/batch-b1']),
 'red, culprit not its own': (0,
                              ['acquire check/batch-b1 True',
                               'acquire slot-7 True',
                               'batch_check CLONE-wt-check-7 base0123456789 tip0123456789 check red {} None',
                               "batch_move bisecting {'failed_out': '[error] a test failed'} 'batch b1: tip0123456 "
                               "failed its check; bisecting' None",
                               "bisect red '[error] a test failed' None check",
                               "own c 'z' 1000.0",
                               "prechecked (['a', 'b'], 'tip-b', 'base0123456789', 'c on fail only where main fails "
                               "too', ['base0123456789', 'a', 'b', 'c'])",
                               'release slot-7',
                               'release check/batch-b1']),
 'red, first culprit not its own': (0,
                                    ['acquire check/batch-b1 True',
                                     'acquire slot-7 True',
                                     'batch_check CLONE-wt-check-7 base0123456789 tip0123456789 check red {} None',
                                     "batch_move bisecting {'failed_out': '[error] a test failed'} 'batch b1: "
                                     "tip0123456 failed its check; bisecting' None",
                                     "bisect red '[error] a test failed' None check",
                                     "own a 'z' 1000.0",
                                     "prechecked ([], None, 'base0123456789', 'a on fail only where main fails too', "
                                     "['base0123456789', 'a', 'b', 'c'])",
                                     'release slot-7',
                                     'release check/batch-b1']),
 'red, culprit its own': (1,
                          ['acquire check/batch-b1 True',
                           'acquire slot-7 True',
                           'batch_check CLONE-wt-check-7 base0123456789 tip0123456789 check red {} None',
                           "batch_move bisecting {'failed_out': '[error] a test failed'} 'batch b1: tip0123456 "
                           "failed its check; bisecting' None",
                           "bisect red '[error] a test failed' None check",
                           "own b 'z' 1000.0",
                           "verdict ('b', 'rejected', 'NOT LANDED: the checks failed with your change in the batch "
                           "(entry b)\\n  z', ['z'])",
                           "prechecked (['a'], 'tip-a', 'base0123456789', 'b rejected', None)",
                           'release slot-7',
                           'release check/batch-b1'])}
