"""THE ALERT PROBES (alertclear.alert_probe, alert_probe_cleared), characterised with the store, the conditions, the gate
snapshot, git and the audience rules faked and worktrees real temporary directories: for each kind of item what closes
its alert (or says whom it is for), and for each kind and verdict what clears the condition it names. `run` takes the
module so a copy of the code can be driven the same way."""
import contextlib
import copy
import os
import sys
import tempfile
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from test_agentctl import A  # noqa: E402,F401 (puts lib on the path, loads every machine)
from golden_cases import cherry_state  # noqa: E402

RED = {"key": "k1", "what": "movies Main", "last_run": {"number": 2140}, "runs": [2128, 2140]}
ITEMS = {
    "red:r1": {"state": "offered", "data": RED}, "red:r2": {"state": "leased", "data": RED},
    "red:r3": {"state": "blocked-external", "data": RED}, "red:r4": {"state": "verified", "data": RED},
    "change:c1": {"state": "landed"}, "change:c2": {"state": "queued"}, "change:c3": {"state": "cancelled"},
    "revert:abc": {"state": "open"}, "issue:i1": {"state": "closed"}, "issue:i2": {"state": "open"},
    "batch:b1": {"state": "landed"}, "batch:b2": {"state": "checking"}, "batch:b9": {"state": "held"}, "watch:w1": {"state": "watching"},
}
OPEN_REDS = [{"id": "r1", "data": RED}]
HELD = [{"id": "b9", "data": {"tip": "abcdef1234"}}, {"id": "b8", "data": None}]
GATE_LATER, GATE_EARLIER = "2026-11-25T12:00:00Z", "2026-11-25T08:00:00Z"


def run(module, script):
    """alert_probe(st, item, e, t, ctx) with the script's answers: (its return, the ctx after, the calls made)."""
    log = []
    with tempfile.TemporaryDirectory() as tmp, contextlib.ExitStack() as stack:
        base = module.iso_epoch("2026-11-25T10:00:00Z")
        wt = os.path.join(tmp, "wt")
        if script.get("wt", True):
            os.makedirs(wt)

        def sub(x):
            return x.replace("WT", wt) if isinstance(x, str) else x

        def shown(x):
            return str(x).replace(tmp, "TMP")

        def rec(name, ret=None):
            def fake(*a, **k):
                log.append(shown(f"{name} {a} {sorted(k.items())}"))
                return ret(*a, **k) if callable(ret) else ret
            return fake

        class Store:
            def __repr__(self):
                return "ST"

            def item(self, kind, ident):
                log.append(f"item {kind} {ident}")
                return copy.deepcopy(script.get("items", ITEMS).get(f"{kind}:{ident}"))

            def items(self, kind, states):
                log.append(f"items {kind} {sorted(states)}")
                return copy.deepcopy(OPEN_REDS if kind == "red" and script.get("reds", True) else [] if kind == "red" else HELD)

        patches = {
            "red_ask": rec("red_ask", lambda it, blk: script.get("ask")), "red_block_of": rec("red_block_of", "BLK"),
            "open_conditions": rec("open_conditions", script.get("conds", [])),
            "claude_trust_gap": rec("claude_trust_gap", script.get("gap")), "remote": lambda: "origin",
            "landed_state": rec("landed_state", lambda *a, **k: cherry_state(A, script.get("cherry", (["c"], [])))),
            "alert_red_audience": rec("alert_red_audience", lambda e, it, st, t: ("red-audience", it["state"])),
            "alert_condition_audience": rec("alert_condition_audience", lambda kind, e, opened, t: ("cond-audience", kind)),
            "alert_gate": rec("alert_gate", script.get("gate", [])), "red_what": lambda d: d["what"],
        }
        for name, fake in patches.items():
            stack.enter_context(mock.patch.object(module, name, fake))
        e = {k: sub(v) for k, v in script.get("e", {}).items()}
        if "first_at" in e:
            e["first_at"] = base + e["first_at"]
        ctx = copy.deepcopy(script.get("ctx"))
        if ctx and "probed" in ctx:
            ctx["probed"] = {sub(k): base + v for k, v in ctx["probed"].items()}
        got = module.alert_probe(Store(), sub(script["item"]), e, base + script.get("t", 1000), ctx)
        return shown(got), shown(ctx), log


def S(item, kind=None, verdict=None, first=None, **more):
    e = {k: v for k, v in (("kind", kind), ("verdict", verdict), ("first_at", first)) if v is not None}
    return dict(item=item, e=e, **more)


GATE = lambda created: [{"decided": {"createdAt": created}}]  # noqa: E731

SCENARIOS = {
    "red gone": S("red:nope"), "red terminal": S("red:r4"),
    "blocked, red moved on": S("red:r2", "BLOCKED"), "blocked, ask retracted": S("red:r3", "BLOCKED"),
    "blocked, ask stands": S("red:r3", "BLOCKED", ask="Q"),
    "unclaimed, taken": S("red:r2", "RED_UNCLAIMED"), "starved, taken": S("red:r2", "RED_STARVED"),
    "unclaimed, offered": S("red:r1", "RED_UNCLAIMED"), "escalate": S("red:r2", "RED_ESCALATE"),
    "red alert of another kind": S("red:r2", "RED_FLAKE"), "red alert without a kind": S("red:r2"),
    "condition gone": S("condition:k9", "NETWORK"),
    "condition by key": S("condition:k1", "NETWORK", conds=[{"key": "k1", "opened_at": 5}]),
    "condition by id": S("condition:i1", "STALLED", conds=[{"key": "x", "id": "i1", "opened_at": 6}]),
    "condition of an owner kind": S("condition:k1", "UNLANDED_ORPHAN", conds=[{"key": "k1"}]),
    "condition of another kind": S("condition:k1", "BLOCKED", conds=[{"key": "k1"}]),
    "change landed": S("change:c1"), "change superseded or cancelled": S("change:c3"), "change queued": S("change:c2"),
    "change unknown": S("change:zz"),
    "trust given": S("trust:/clone"), "trust still asked": S("trust:/clone", gap=True),
    "worktree gone": S("wt:/nope/x:1", wt=False), "worktree there": S("wt:WT:1"), "wt relative": S("wt:rel:1"),
    "wt empty": S("wt::1"),
    "revert owner of a gone item": S("revert-owner:zz/esc"), "revert owner of an open one": S("revert-owner:abc/esc"),
    "revert owner item keeps its rewritten key for the clearing": S("revert-owner:abc/esc", "UNLANDED_ORPHAN", "ORPHAN at WT: 1", ctx={}),
    "issue closed": S("issue:i1"), "issue open": S("issue:i2"), "issue gone": S("issue:zz"),
    "batch landed": S("batch:b1"), "watch alive": S("watch:w1", verdict="x"),
    "no colon": S("plain"), "typed but empty id": S("issue:"),
    "named red, none open": S("run:5", "FIX_FAILED", "FIX FAILED for movies Main #9999", reds=False),
    "named red by run, one open": S("run:5", "FIX_FAILED", "FIX FAILED for movies Main #2128 here"),
    "named red by run prefix digits": S("run:5", "FIX_FAILED", "FIX FAILED for movies Main #21401"),
    "named red by key": S("run:5", "BLOCKED", "BLOCKED: decide k1 now"),
    "named red by another key": S("run:5", "BLOCKED", "BLOCKED: decide k12 now"),
    "red kind naming nothing": S("run:5", "DIAGNOSED", "no number here"),
    "red kind, item is a red": S("red:r2", "DIAGNOSED", "FIX FAILED #1"),
    "orphan, worktree gone": S("orphan:WT", "UNLANDED_ORPHAN", wt=False),
    "orphan, no path": S("orphan:none", "UNLANDED_ORPHAN"),
    "orphan, commits on main": S("orphan:WT", "UNLANDED_ORPHAN"),
    "orphan, commits left": S("orphan:WT", "UNLANDED_ORPHAN", cherry=(["c"], ["d"])),
    "orphan, git failing": S("orphan:WT", "UNLANDED_ORPHAN", cherry=(None, [])),
    "orphan probed lately": S("orphan:WT", "UNLANDED_ORPHAN", ctx={"probed": {"orphan:WT": 900}}, t=1000),
    "orphan probed long ago": S("orphan:WT", "UNLANDED_ORPHAN", ctx={"probed": {"orphan:WT": -5000}}, t=1000),
    "orphan named in the verdict": S("run:7", "UNLANDED_ORPHAN", "ORPHAN at WT: 3 commits"),
    "orphan named by a tilde": S("run:7", "UNLANDED_ORPHAN", "ORPHAN at ~/nowhere-zzz: 3 commits"),
    "orphan kind without a path": S("run:7", "UNLANDED_ORPHAN", "no path"),
    "watch unknown, covered": S("run:8", None, "WATCH UNKNOWN abc", first=-100, gate=GATE(GATE_LATER)),
    "watch unknown, not covered": S("run:8", None, "WATCH UNKNOWN abc", first=500, gate=GATE(GATE_EARLIER)),
    "watch unknown, gate unread": S("run:8", None, "WATCH UNKNOWN abc", gate=[]),
    "watch unknown, undecided": S("run:8", None, "WATCH UNKNOWN abc", gate=[{"decided": None}]),
    "watch item": S("watch:zz", verdict="whatever"),
    "network inside the grace": S("run:9", "NETWORK", t=100),
    "network, still open": S("run:9", "NETWORK", first=-5000, t=1000, conds=[{"kind": "NETWORK"}]),
    "network, back": S("run:9", "NETWORK", first=-5000, t=1000, conds=[{"kind": "STALLED"}]),
    "held by tip, live": S("run:10", None, "HELD: the batch check of abcdef1 stalled"),
    "held by tip, moved on": S("run:10", None, "HELD: the batch check of 1234567 stalled"),
    "held by batch id, live": S("run:10", None, "HELD: the batch b9 sits"),
    "held by batch id, gone": S("run:10", None, "HELD: the batch b0 sits"),
    "held, naming neither": S("run:10", None, "HELD: other words"),
    "batch item, live": S("batch:b9", None, "x"), "batch item, gone": S("batch:b0", None, "x"),
    "nothing known": S("run:11", "INFO", "all fine"),
}


def generate(rng, n):
    items = ["red:r1", "red:r2", "red:r3", "red:r4", "red:nope", "condition:k1", "condition:zz", "change:c1", "change:c2",
             "trust:/clone", "wt:WT:1", "wt:/nope:1", "revert-owner:abc/x", "revert-owner:zz/x", "issue:i1", "issue:i2",
             "batch:b1", "batch:b9", "watch:w1", "orphan:WT", "orphan:zz", "run:5", "text:x", "plain", "wt::1"]
    kinds = [None, "BLOCKED", "RED_UNCLAIMED", "RED_STARVED", "RED_ESCALATE", "NETWORK", "UNLANDED_ORPHAN", "RED_FLAKE",
             "DIAGNOSED", "INVARIANT", "INFO", "STALLED"]
    verdicts = [None, "FIX FAILED #2128", "FIX FAILED #3", "decide k1 now", "decide k12", "WATCH UNKNOWN a",
                "HELD: the batch check of abcdef1 x", "HELD: the batch b9 y", "HELD: the batch b0 y", "ORPHAN at WT: 3",
                "ORPHAN ~/zz-none: 1", "HELD: odd", "plain"]
    out = []
    for _ in range(n):
        s = S(rng.choice(items), rng.choice(kinds), rng.choice(verdicts), t=rng.choice([100, 1000, 5000]),
              gap=rng.choice([None, True]), ask=rng.choice([None, "Q"]), wt=rng.choice([True, True, False]),
              reds=rng.choice([True, False]), gate=rng.choice([[], GATE(GATE_LATER), GATE(GATE_EARLIER), [{"decided": None}]]),
              conds=rng.choice([[], [{"kind": "NETWORK"}], [{"key": "k1", "opened_at": 3}], [{"id": "zz"}]]),
              cherry=rng.choice([(["c"], []), (["c"], ["d"]), (None, [])]),
              ctx=rng.choice([None, {}, {"probed": {"orphan:WT": -50}}, {"probed": {"orphan:WT": -5000}}]))
        if rng.random() < 0.3:
            s["e"]["first_at"] = rng.choice([-20000, 0, 500])
        out.append(s)
    return out


class AlertProbeTest(unittest.TestCase):
    def test_every_probe_does_what_it_did(self):
        module = __import__("agentctl.alertclear", fromlist=["x"])
        for name, script in SCENARIOS.items():
            with self.subTest(name):
                self.assertEqual(run(module, script), EXPECTED[name], name)


def results():
    module = __import__("agentctl.alertclear", fromlist=["x"])
    return {n: run(module, s) for n, s in SCENARIOS.items()}


EXPECTED = {'red gone': ("('its red is gone', None)", 'None', ['item red nope']),
 'red terminal': ("('its red is verified', None)", 'None', ['item red r4']),
 'blocked, red moved on': ("('its red is leased now (no longer waiting on you)', None)", 'None', ['item red r2']),
 'blocked, ask retracted': ("('its question is no longer asked (its block lapsed or waits on no owner action)', "
                            'None)',
                            'None',
                            ['item red r3',
                             "red_block_of ({'state': 'blocked-external', 'data': {'key': 'k1', 'what': 'movies "
                             "Main', 'last_run': {'number': 2140}, 'runs': [2128, 2140]}}, ST) []",
                             "red_ask ({'state': 'blocked-external', 'data': {'key': 'k1', 'what': 'movies Main', "
                             "'last_run': {'number': 2140}, 'runs': [2128, 2140]}}, 'BLK') []"]),
 'blocked, ask stands': ('(None, None)',
                         'None',
                         ['item red r3',
                          "red_block_of ({'state': 'blocked-external', 'data': {'key': 'k1', 'what': 'movies Main', "
                          "'last_run': {'number': 2140}, 'runs': [2128, 2140]}}, ST) []",
                          "red_ask ({'state': 'blocked-external', 'data': {'key': 'k1', 'what': 'movies Main', "
                          "'last_run': {'number': 2140}, 'runs': [2128, 2140]}}, 'BLK') []"]),
 'unclaimed, taken': ("('its red is leased now (taken)', None)", 'None', ['item red r2']),
 'starved, taken': ("('its red is leased now (taken)', None)", 'None', ['item red r2']),
 'unclaimed, offered': ("(None, ('red-audience', 'offered'))",
                        'None',
                        ['item red r1',
                         "alert_red_audience ({'kind': 'RED_UNCLAIMED'}, {'state': 'offered', 'data': {'key': 'k1', "
                         "'what': 'movies Main', 'last_run': {'number': 2140}, 'runs': [2128, 2140]}}, ST, "
                         '1795601800.0) []']),
 'escalate': ("(None, ('red-audience', 'leased'))",
              'None',
              ['item red r2',
               "alert_red_audience ({'kind': 'RED_ESCALATE'}, {'state': 'leased', 'data': {'key': 'k1', 'what': "
               "'movies Main', 'last_run': {'number': 2140}, 'runs': [2128, 2140]}}, ST, 1795601800.0) []"]),
 'red alert of another kind': ('(None, None)', 'None', ['item red r2']),
 'red alert without a kind': ('(None, None)', 'None', ['item red r2']),
 'condition gone': ("('its condition closed', None)", 'None', ['open_conditions (ST,) []']),
 'condition by key': ("(None, ('cond-audience', 'NETWORK'))",
                      'None',
                      ['open_conditions (ST,) []',
                       "alert_condition_audience ('NETWORK', {'kind': 'NETWORK'}, 5, 1795601800.0) []"]),
 'condition by id': ("(None, ('cond-audience', 'STALLED'))",
                     'None',
                     ['open_conditions (ST,) []',
                      "alert_condition_audience ('STALLED', {'kind': 'STALLED'}, 6, 1795601800.0) []"]),
 'condition of an owner kind': ('(None, None)', 'None', ['open_conditions (ST,) []']),
 'condition of another kind': ('(None, None)', 'None', ['open_conditions (ST,) []']),
 'change landed': ("('its change is landed', None)", 'None', ['item change c1']),
 'change superseded or cancelled': ("('its change is cancelled', None)", 'None', ['item change c3']),
 'change queued': ('(None, None)', 'None', ['item change c2']),
 'change unknown': ('(None, None)', 'None', ['item change zz']),
 'trust given': ("('claude trusts it now', None)", 'None', ["claude_trust_gap ('/clone',) []"]),
 'trust still asked': ('(None, None)', 'None', ["claude_trust_gap ('/clone',) []"]),
 'worktree gone': ("('its worktree is gone', None)", 'None', []),
 'worktree there': ('(None, None)', 'None', []),
 'wt relative': ('(None, None)', 'None', []),
 'wt empty': ('(None, None)', 'None', []),
 'revert owner of a gone item': ("('its revert is gone', None)", 'None', ['item revert zz']),
 'revert owner of an open one': ('(None, None)', 'None', ['item revert abc']),
 'revert owner item keeps its rewritten key for the clearing': ("('its commits are on main', None)",
                                                                "{'probed': {'revert:abc': 1795601800.0}}",
                                                                ['item revert abc',
                                                                 "landed_state ('TMP/wt', "
                                                                 "'refs/remotes/origin/main', 'HEAD') []"]),
 'issue closed': ("('its issue is closed', None)", 'None', ['item issue i1']),
 'issue open': ('(None, None)', 'None', ['item issue i2']),
 'issue gone': ("('its issue is gone', None)", 'None', ['item issue zz']),
 'batch landed': ("('its batch is landed', None)", 'None', ['item batch b1']),
 'watch alive': ('(None, None)', 'None', ['item watch w1', 'alert_gate () []']),
 'no colon': ('(None, None)', 'None', []),
 'typed but empty id': ('(None, None)', 'None', []),
 'named red, none open': ("('its red is closed (no open red names it)', None)",
                          'None',
                          ["items red ['blocked-external', 'dispatch-off', 'fix-pending', 'leased', 'offered', "
                           "'red']"]),
 'named red by run, one open': ('(None, None)',
                                'None',
                                ["items red ['blocked-external', 'dispatch-off', 'fix-pending', 'leased', 'offered', "
                                 "'red']"]),
 'named red by run prefix digits': ("('its red is closed (no open red names it)', None)",
                                    'None',
                                    ["items red ['blocked-external', 'dispatch-off', 'fix-pending', 'leased', "
                                     "'offered', 'red']"]),
 'named red by key': ('(None, None)',
                      'None',
                      ["items red ['blocked-external', 'dispatch-off', 'fix-pending', 'leased', 'offered', 'red']"]),
 'named red by another key': ("('its red is closed (no open red names it)', None)",
                              'None',
                              ["items red ['blocked-external', 'dispatch-off', 'fix-pending', 'leased', 'offered', "
                               "'red']"]),
 'red kind naming nothing': ('(None, None)', 'None', []),
 'red kind, item is a red': ('(None, None)', 'None', ['item red r2']),
 'orphan, worktree gone': ("('its worktree is gone', None)", 'None', []),
 'orphan, no path': ('(None, None)', 'None', []),
 'orphan, commits on main': ("('its commits are on main', None)",
                             'None',
                             ["landed_state ('TMP/wt', 'refs/remotes/origin/main', 'HEAD') []"]),
 'orphan, commits left': ('(None, None)',
                          'None',
                          ["landed_state ('TMP/wt', 'refs/remotes/origin/main', 'HEAD') []"]),
 'orphan, git failing': ('(None, None)', 'None', ["landed_state ('TMP/wt', 'refs/remotes/origin/main', 'HEAD') []"]),
 'orphan probed lately': ('(None, None)', "{'probed': {'orphan:TMP/wt': 1795601700.0}}", []),
 'orphan probed long ago': ("('its commits are on main', None)",
                            "{'probed': {'orphan:TMP/wt': 1795601800.0}}",
                            ["landed_state ('TMP/wt', 'refs/remotes/origin/main', 'HEAD') []"]),
 'orphan named in the verdict': ("('its commits are on main', None)",
                                 'None',
                                 ["landed_state ('TMP/wt', 'refs/remotes/origin/main', 'HEAD') []"]),
 'orphan named by a tilde': ("('its worktree is gone', None)", 'None', []),
 'orphan kind without a path': ('(None, None)', 'None', []),
 'watch unknown, covered': ("('a later gate run has decided (its commit is covered)', None)",
                            'None',
                            ['alert_gate () []']),
 'watch unknown, not covered': ('(None, None)', 'None', ['alert_gate () []']),
 'watch unknown, gate unread': ('(None, None)', 'None', ['alert_gate () []']),
 'watch unknown, undecided': ('(None, None)', 'None', ['alert_gate () []']),
 'watch item': ("('its watch is gone', None)", 'None', ['item watch zz']),
 'network inside the grace': ('(None, None)', 'None', []),
 'network, still open': ('(None, None)', 'None', ['open_conditions (ST,) []']),
 'network, back': ("('the network is back (no NETWORK condition open)', None)", 'None', ['open_conditions (ST,) []']),
 'held by tip, live': ('(None, None)', 'None', ["items batch ['held']"]),
 'held by tip, moved on': ("('its batch moved on (no longer held)', None)", 'None', ["items batch ['held']"]),
 'held by batch id, live': ('(None, None)', 'None', ["items batch ['held']"]),
 'held by batch id, gone': ("('its batch moved on (no longer held)', None)", 'None', ["items batch ['held']"]),
 'held, naming neither': ('(None, None)', 'None', ["items batch ['held']"]),
 'batch item, live': ('(None, None)', 'None', ['item batch b9', "items batch ['held']"]),
 'batch item, gone': ("('its batch is gone', None)", 'None', ['item batch b0']),
 'nothing known': ('(None, None)', 'None', [])}
