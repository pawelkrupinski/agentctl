"""INVARIANT W1 (invariants.inv_w1: no unlanded change older than its state's table allows), characterised with the
change rows, the gate's red rows, the conditions, the change progress and the batches faked: no open change, a young one,
a batch that carries a waiting one, each reason an old one is off (stranded, no item, its progress, the red gate, a
hold that recovers itself) and the hard bound. `run` takes the module so a copy of the code can be driven the same way."""
import contextlib
import copy
import os
import sys
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from test_agentctl import A  # noqa: E402,F401 (puts lib on the path)

NOW, HOUR = 1_000_000.0, 3600.0


class Clock:
    def __repr__(self):
        return "CLOCK"

    def time(self):
        return NOW


def run(module, script):
    """inv_w1(clock, st) with the script's answers: ((ok, text), the calls it made). Rows are (id, state, age in hours)."""
    log = []

    def rec(name, ret=None):
        def fake(*a, **k):
            log.append(f"{name} {a} {sorted(k.items())}")
            return ret(*a, **k) if callable(ret) else ret
        return fake

    rows = [{"id": i, "state": s, "since": NOW - age * HOUR, **({"created": NOW - made * HOUR} if made is not None else {})}
            for i, s, age, *rest in script.get("rows", []) for made in [rest[0] if rest else None]]
    items = copy.deepcopy(script.get("items", {}))

    class Store:
        def __repr__(self):
            return "ST"

        def item(self, kind, ident):
            return items.get(ident, {"id": ident})

        def items(self, kind, states):
            return copy.deepcopy(script.get("batches", []))

    patches = {
        "movies_repo_path": lambda: "/repo", "close_landed_changes": rec("close_landed_changes"),
        "change_rows": lambda st: rows, "w1_gate_reds": rec("w1_gate_reds", lambda o_r, t: script.get("gate", (0.0, ""))),
        "open_conditions": lambda st: script.get("conds", []),
        "change_progress": rec("change_progress", lambda st, it, t: script.get("progress", {}).get(it["id"])),
        "hold_recovers_itself": lambda hold: script.get("recovers", False),
        "change_hold_visible": rec("change_hold_visible", "HOLD"),
    }
    with contextlib.ExitStack() as stack:
        for name, fake in patches.items():
            stack.enter_context(mock.patch.object(module, name, fake))
        stack.enter_context(mock.patch.object(module.os.path, "isdir", lambda p: script.get("repo", True)))
        return module.inv_w1(Clock(), Store(), {}), log


def B(bid, state, members, deadline=0):
    return {"id": bid, "state": state, "data": {"members": members}, "deadline": deadline}


BATCH_ITEM = {"id": "b1", "state": "checking", "deadline": NOW + HOUR, "data": {"members": ["c1"]}}
SCENARIOS = {
    "no repo, no change": dict(repo=False),
    "no open change": dict(rows=[("c1", "landed", 9)]),
    "a young change": dict(rows=[("c1", "queued", 1)]),
    "an old queued change that progresses": dict(rows=[("c1", "queued", 3)]),
    "an old change with a reason": dict(rows=[("c1", "queued", 3)], progress={"c1": "no checker is alive"}),
    "no item": dict(rows=[("c1", "queued", 3)], items={"c1": None}, progress={"c1": "x"}),
    "stranded": dict(rows=[("c1", "queued", 3)], conds=[{"key": "change-stranded-c1"}]),
    "stranded for another change": dict(rows=[("c1", "queued", 3)], conds=[{"key": "change-stranded-c2"}]),
    "hard bound": dict(rows=[("c1", "checked", 9)]),
    "hard bound, a hold recovers itself": dict(rows=[("c1", "checked", 9)], recovers=True),
    "hard bound counts from the red's start": dict(rows=[("c1", "checked", 7), ("c2", "checked", 9)], gate=(3 * HOUR, "Main #7")),
    "reason, a hold recovers itself": dict(rows=[("c1", "checked", 9)], recovers=True, progress={"c1": "r"}),
    "waiting, gate green": dict(rows=[("c1", "waiting", 3)], gate=(0.0, "")),
    "waiting, gate unread": dict(rows=[("c1", "waiting", 3)], gate=(0.0, None)),
    "waiting, gate red since long": dict(rows=[("c1", "waiting", 5)], gate=(3 * HOUR, "Main #7 since 08:00Z")),
    "waiting, gate red lately": dict(rows=[("c1", "waiting", 3)], gate=(1 * HOUR, "Main #7 since 08:00Z")),
    "waiting, red gate, a reason": dict(rows=[("c1", "waiting", 5)], gate=(3 * HOUR, "Main #7"), progress={"c1": "r"}),
    "waiting, green gate, a reason": dict(rows=[("c1", "waiting", 3)], gate=(0.0, ""), progress={"c1": "r"}),
    "queued under a red gate": dict(rows=[("c1", "queued", 5)], gate=(3 * HOUR, "Main #7")),
    "waiting, carried by a batch in time": dict(rows=[("c1", "waiting", 4)], batches=[B("b1", "checking", ["c1"], NOW + HOUR)]),
    "waiting, carried past its deadline": dict(rows=[("c1", "waiting", 4)], batches=[B("b1", "checking", ["c1"], NOW - 1)]),
    "checking, carried past its deadline": dict(rows=[("c1", "checking", 4)], batches=[B("b1", "bisecting", ["c1"], NOW - 1)]),
    "waiting, carried by a blocked batch": dict(rows=[("c1", "waiting", 4)], batches=[B("b1", "blocked-red", ["c1"], NOW + HOUR)]),
    "waiting, a batch of others": dict(rows=[("c1", "waiting", 4)], batches=[B("b1", "checking", ["c9"], NOW + HOUR)]),
    "queued, a batch names it": dict(rows=[("c1", "queued", 4)], batches=[B("b1", "checking", ["c1"], NOW - 1)]),
    "two changes, the first fine": dict(rows=[("c1", "queued", 1), ("c2", "queued", 3)], progress={"c2": "bad"}),
    "two bad, the oldest first": dict(rows=[("c2", "queued", 3), ("c1", "queued", 5)], progress={"c1": "bad1", "c2": "bad2"}),
    "oldest within the table": dict(rows=[("c1", "queued", 3), ("c2", "queued", 1)]),
    "a batch carries the older, the newer is off": dict(rows=[("c1", "waiting", 4), ("c2", "queued", 3)], progress={"c2": "r2"},
                                                         batches=[B("b1", "checking", ["c1"], NOW + HOUR)]),
}


def generate(rng, n):
    out = []
    for _ in range(n):
        ids = [f"c{i}" for i in range(rng.randint(0, 4))]
        rows = [(i, rng.choice(["queued", "checking", "checked", "waiting", "landed"]), rng.choice([0.5, 1, 2, 3, 5, 7, 9])) for i in ids]
        out.append(dict(rows=rows, repo=rng.choice([True, True, False]),
                        items={i: rng.choice([None, {"id": i}]) for i in ids},
                        progress={i: rng.choice([None, "r-" + i]) for i in ids},
                        conds=[{"key": f"change-stranded-{i}"} for i in ids if rng.random() < .2],
                        gate=rng.choice([(0.0, ""), (0.0, None), (3 * HOUR, "Main #7"), (0.5 * HOUR, "Main #8"), (9 * HOUR, "Main #9")]),
                        recovers=rng.choice([False, True]),
                        batches=[B(f"b{k}", rng.choice(["checking", "blocked-red", "held", "pushing"]),
                                   rng.sample(ids, rng.randint(0, len(ids))), NOW + rng.choice([-HOUR, HOUR])) for k in range(rng.randint(0, 2))]))
    return out


class InvariantW1Test(unittest.TestCase):
    def test_every_verdict_is_what_it_was(self):
        module = __import__("agentctl.invariants", fromlist=["x"])
        for name, script in SCENARIOS.items():
            with self.subTest(name):
                self.assertEqual(run(module, script), EXPECTED[name], name)


    def test_a_requeued_change_is_aged_from_its_own_entry_not_the_place_it_kept(self):
        # 2026-11-25: an entry queued minutes before, in the place of a predecessor rejected 19 h earlier (queue age
        # `since` carried over), was reported "19 h old"; the age is the entry's own creation
        module = __import__("agentctl.invariants", fromlist=["x"])
        kept = dict(rows=[("c1", "checked", 19, 0.1)])
        self.assertEqual(run(module, kept)[0][0], True, run(module, kept)[0])
        self.assertEqual(run(module, dict(rows=[("c1", "checked", 19)]))[0][0], False)  # no creation read: the queue age

    def test_a_change_item_carries_its_entry_creation_beside_the_queue_age_it_inherited(self):
        from agentctl.changerows import _change_row
        from agentctl.queueslots import change_item_fields
        e = {"id": "x", "created": "2026-11-25T22:17:40Z", "queued_since": 1000.0}
        data = change_item_fields(e)
        row = _change_row({"id": "x", "state": "checked", "deadline": 0, "level": 0, "data": data, "entered_at": 5.0})
        self.assertEqual((row["since"], row["created"] > 1_000_000), (1000.0, True))


def results():
    module = __import__("agentctl.invariants", fromlist=["x"])
    return {n: run(module, s) for n, s in SCENARIOS.items()}


EXPECTED = {'no repo, no change': ((True, 'no change waits'), []),
 'no open change': ((True, 'no change waits'), ["close_landed_changes ('/repo', CLOCK) [('looked', {}), ('store', ST)]"]),
 'a young change': ((True, "oldest unlanded change c1 (queued) is 1 h old, within what its state's table allows"),
                    ["close_landed_changes ('/repo', CLOCK) [('looked', {}), ('store', ST)]",
                     "w1_gate_reds ('pawelkrupinski/movies', 1000000.0) []"]),
 'an old queued change that progresses': ((True,
                                           "oldest unlanded change c1 (queued) is 3 h old, within what its state's "
                                           'table allows'),
                                          ["close_landed_changes ('/repo', CLOCK) [('looked', {}), ('store', ST)]",
                                           "w1_gate_reds ('pawelkrupinski/movies', 1000000.0) []",
                                           "change_progress (ST, {'id': 'c1'}, 1000000.0) []"]),
 'an old change with a reason': ((False, 'unlanded change c1 (queued: no checker is alive) is 3 h old'),
                                 ["close_landed_changes ('/repo', CLOCK) [('looked', {}), ('store', ST)]",
                                  "w1_gate_reds ('pawelkrupinski/movies', 1000000.0) []",
                                  "change_progress (ST, {'id': 'c1'}, 1000000.0) []"]),
 'no item': ((False, 'unlanded change c1 (queued: no item) is 3 h old'),
             ["close_landed_changes ('/repo', CLOCK) [('looked', {}), ('store', ST)]",
              "w1_gate_reds ('pawelkrupinski/movies', 1000000.0) []"]),
 'stranded': ((False, 'unlanded change c1 (queued: STRANDED, no queue entry carries it) is 3 h old'),
              ["close_landed_changes ('/repo', CLOCK) [('looked', {}), ('store', ST)]",
               "w1_gate_reds ('pawelkrupinski/movies', 1000000.0) []"]),
 'stranded for another change': ((True,
                                  "oldest unlanded change c1 (queued) is 3 h old, within what its state's table "
                                  'allows'),
                                 ["close_landed_changes ('/repo', CLOCK) [('looked', {}), ('store', ST)]",
                                  "w1_gate_reds ('pawelkrupinski/movies', 1000000.0) []",
                                  "change_progress (ST, {'id': 'c1'}, 1000000.0) []"]),
 'hard bound': ((False,
                 'unlanded change c1 (checked: past 3 x its bound, though its state still says it progresses) is 9 h '
                 'old'),
                ["close_landed_changes ('/repo', CLOCK) [('looked', {}), ('store', ST)]",
                 "w1_gate_reds ('pawelkrupinski/movies', 1000000.0) []",
                 "change_progress (ST, {'id': 'c1'}, 1000000.0) []",
                 "change_hold_visible (ST, {'id': 'c1'}, {}, 1000000.0) []"]),
 'hard bound, a hold recovers itself': ((True,
                                         "oldest unlanded change c1 (checked) is 9 h old, within what its state's "
                                         'table allows'),
                                        ["close_landed_changes ('/repo', CLOCK) [('looked', {}), ('store', ST)]",
                                         "w1_gate_reds ('pawelkrupinski/movies', 1000000.0) []",
                                         "change_progress (ST, {'id': 'c1'}, 1000000.0) []",
                                         "change_hold_visible (ST, {'id': 'c1'}, {}, 1000000.0) []"]),
 "hard bound counts from the red's start": ((False,
                                             'unlanded change c2 (checked: past 3 x its bound, though its state '
                                             'still says it progresses) is 9 h old, the gate red for 3 h (Main #7)'),
                                            ["close_landed_changes ('/repo', CLOCK) [('looked', {}), ('store', ST)]",
                                             "w1_gate_reds ('pawelkrupinski/movies', 1000000.0) []",
                                             "change_progress (ST, {'id': 'c2'}, 1000000.0) []",
                                             "change_hold_visible (ST, {'id': 'c2'}, {}, 1000000.0) []"]),
 'reason, a hold recovers itself': ((False, 'unlanded change c1 (checked: r) is 9 h old'),
                                    ["close_landed_changes ('/repo', CLOCK) [('looked', {}), ('store', ST)]",
                                     "w1_gate_reds ('pawelkrupinski/movies', 1000000.0) []",
                                     "change_progress (ST, {'id': 'c1'}, 1000000.0) []"]),
 'waiting, gate green': ((False,
                          'unlanded change c1 (waiting: waiting for green while the gate is green) is 3 h old'),
                         ["close_landed_changes ('/repo', CLOCK) [('looked', {}), ('store', ST)]",
                          "w1_gate_reds ('pawelkrupinski/movies', 1000000.0) []",
                          "change_progress (ST, {'id': 'c1'}, 1000000.0) []"]),
 'waiting, gate unread': ((False,
                           'unlanded change c1 (waiting: waiting for green, the gate unread (no CI snapshot)) is 3 h '
                           'old'),
                          ["close_landed_changes ('/repo', CLOCK) [('looked', {}), ('store', ST)]",
                           "w1_gate_reds ('pawelkrupinski/movies', 1000000.0) []",
                           "change_progress (ST, {'id': 'c1'}, 1000000.0) []"]),
 'waiting, gate red since long': ((False,
                                   'unlanded change c1 (waiting: held by the red gate: Main #7 since 08:00Z) is 5 h '
                                   'old, the gate red for 3 h (Main #7 since 08:00Z)'),
                                  ["close_landed_changes ('/repo', CLOCK) [('looked', {}), ('store', ST)]",
                                   "w1_gate_reds ('pawelkrupinski/movies', 1000000.0) []",
                                   "change_progress (ST, {'id': 'c1'}, 1000000.0) []"]),
 'waiting, gate red lately': ((False,
                               'unlanded change c1 (waiting: held by the red gate: Main #7 since 08:00Z) is 3 h old, '
                               'the gate red for 1 h (Main #7 since 08:00Z)'),
                              ["close_landed_changes ('/repo', CLOCK) [('looked', {}), ('store', ST)]",
                               "w1_gate_reds ('pawelkrupinski/movies', 1000000.0) []",
                               "change_progress (ST, {'id': 'c1'}, 1000000.0) []"]),
 'waiting, red gate, a reason': ((False,
                                  'unlanded change c1 (waiting: r; held by the red gate: Main #7) is 5 h old, the '
                                  'gate red for 3 h (Main #7)'),
                                 ["close_landed_changes ('/repo', CLOCK) [('looked', {}), ('store', ST)]",
                                  "w1_gate_reds ('pawelkrupinski/movies', 1000000.0) []",
                                  "change_progress (ST, {'id': 'c1'}, 1000000.0) []"]),
 'waiting, green gate, a reason': ((False, 'unlanded change c1 (waiting: r) is 3 h old'),
                                   ["close_landed_changes ('/repo', CLOCK) [('looked', {}), ('store', ST)]",
                                    "w1_gate_reds ('pawelkrupinski/movies', 1000000.0) []",
                                    "change_progress (ST, {'id': 'c1'}, 1000000.0) []"]),
 'queued under a red gate': ((True,
                              "oldest unlanded change c1 (queued) is 5 h old, within what its state's table allows"),
                             ["close_landed_changes ('/repo', CLOCK) [('looked', {}), ('store', ST)]",
                              "w1_gate_reds ('pawelkrupinski/movies', 1000000.0) []",
                              "change_progress (ST, {'id': 'c1'}, 1000000.0) []"]),
 'waiting, carried by a batch in time': ((True,
                                          "oldest unlanded change c1 (waiting) is 4 h old, within what its state's "
                                          'table allows'),
                                         ["close_landed_changes ('/repo', CLOCK) [('looked', {}), ('store', ST)]",
                                          "w1_gate_reds ('pawelkrupinski/movies', 1000000.0) []"]),
 'waiting, carried past its deadline': ((False,
                                         'unlanded change c1 (waiting: carried by batch b1 (checking, past its '
                                         'deadline)) is 4 h old'),
                                        ["close_landed_changes ('/repo', CLOCK) [('looked', {}), ('store', ST)]",
                                         "w1_gate_reds ('pawelkrupinski/movies', 1000000.0) []",
                                         "change_progress (ST, {'id': 'c1'}, 1000000.0) []"]),
 'checking, carried past its deadline': ((False,
                                          'unlanded change c1 (checking: carried by batch b1 (bisecting, past its '
                                          'deadline)) is 4 h old'),
                                         ["close_landed_changes ('/repo', CLOCK) [('looked', {}), ('store', ST)]",
                                          "w1_gate_reds ('pawelkrupinski/movies', 1000000.0) []"]),
 'waiting, carried by a blocked batch': ((False,
                                          'unlanded change c1 (waiting: carried by batch b1 (blocked-red)) is 4 h '
                                          'old'),
                                         ["close_landed_changes ('/repo', CLOCK) [('looked', {}), ('store', ST)]",
                                          "w1_gate_reds ('pawelkrupinski/movies', 1000000.0) []",
                                          "change_progress (ST, {'id': 'c1'}, 1000000.0) []"]),
 'waiting, a batch of others': ((False,
                                 'unlanded change c1 (waiting: waiting for green while the gate is green) is 4 h '
                                 'old'),
                                ["close_landed_changes ('/repo', CLOCK) [('looked', {}), ('store', ST)]",
                                 "w1_gate_reds ('pawelkrupinski/movies', 1000000.0) []",
                                 "change_progress (ST, {'id': 'c1'}, 1000000.0) []"]),
 'queued, a batch names it': ((True,
                               "oldest unlanded change c1 (queued) is 4 h old, within what its state's table allows"),
                              ["close_landed_changes ('/repo', CLOCK) [('looked', {}), ('store', ST)]",
                               "w1_gate_reds ('pawelkrupinski/movies', 1000000.0) []",
                               "change_progress (ST, {'id': 'c1'}, 1000000.0) []"]),
 'two changes, the first fine': ((False, 'unlanded change c2 (queued: bad) is 3 h old'),
                                 ["close_landed_changes ('/repo', CLOCK) [('looked', {}), ('store', ST)]",
                                  "w1_gate_reds ('pawelkrupinski/movies', 1000000.0) []",
                                  "change_progress (ST, {'id': 'c2'}, 1000000.0) []"]),
 'two bad, the oldest first': ((False, 'unlanded change c1 (queued: bad1) is 5 h old'),
                               ["close_landed_changes ('/repo', CLOCK) [('looked', {}), ('store', ST)]",
                                "w1_gate_reds ('pawelkrupinski/movies', 1000000.0) []",
                                "change_progress (ST, {'id': 'c1'}, 1000000.0) []"]),
 'oldest within the table': ((True,
                              "oldest unlanded change c1 (queued) is 3 h old, within what its state's table allows"),
                             ["close_landed_changes ('/repo', CLOCK) [('looked', {}), ('store', ST)]",
                              "w1_gate_reds ('pawelkrupinski/movies', 1000000.0) []",
                              "change_progress (ST, {'id': 'c1'}, 1000000.0) []"]),
 'a batch carries the older, the newer is off': ((False, 'unlanded change c2 (queued: r2) is 3 h old'),
                                                 ["close_landed_changes ('/repo', CLOCK) [('looked', {}), ('store', ST)]",
                                                  "w1_gate_reds ('pawelkrupinski/movies', 1000000.0) []",
                                                  "change_progress (ST, {'id': 'c2'}, 1000000.0) []"])}
