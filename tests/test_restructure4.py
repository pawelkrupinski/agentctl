"""RESTRUCTURE ROUND 4: each function split into named stages keeps its behaviour. The digests (fixtures/
restructure4-digests.json, recorded from the code BEFORE the split with `R4_RECORD=1`) cover thousands of seeded cases
each, collaborators faked (golden_cases.py): every call, its order and the result."""
import os, sys, unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from test_agentctl import A  # noqa: E402
from golden_cases import Probe, check  # noqa: E402


def entry(rng, i):
    e = {"id": f"e{i}", "seq": rng.randint(1, 9), "status": rng.choice(["queued", "checking", "checked", "pushing", "landed"]),
         "worktree": rng.choice(["w1", "w2"]), "branch": rng.choice(["b", ""])}
    for key, gen in (("backlog_batch", lambda: True), ("retry_after", lambda: rng.randint(0, 100)),
                     ("regen_hold", lambda: {"files": ["f"], "cmd": "c"}), ("why", lambda: "infra: x")):
        if rng.random() < 0.25:
            e[key] = gen()
    return e


def maybe(rng, value, p=0.5):
    return value if rng.random() < p else None


class TickChangesPassTest(unittest.TestCase):
    def build(self, case):
        r = case.rng
        A._behind_said.clear()
        entries = [entry(r, i) for i in range(r.randint(1, 4))]
        st = Probe(case, "st", put_item=lambda rng, *a: {"state": rng.choice(["queued", "checking", "checked", "pushing"]),
                                                          "deadline": rng.randint(0, 100)})
        slot = lambda rng, *a, **k: maybe(rng, (1, Probe(case, "slot")), 0.7)  # noqa: E731
        batch = lambda rng, *a: maybe(rng, {"id": "bt", "state": "assembling"}, 0.3)  # noqa: E731
        for name, gen in (
                ("change_item_fields", lambda rng, e: {"f": e["id"]}), ("set_change", lambda rng, *a, **k: None),
                ("classify_change", lambda rng, *a: {"red_fix": rng.random() < 0.3}),
                ("record_change_kind", lambda rng, *a: None), ("note_hold", lambda rng, *a, **k: None),
                ("_push_checked", lambda rng, *a: [f"pushed{rng.randint(0, 9)}"] * rng.randint(0, 2)),
                ("checker_alive", lambda rng, *a: rng.random() < 0.3), ("requeue_infra", lambda rng, *a: None),
                ("lap_change_deadline", lambda rng, *a: None),
                ("main_conflict", lambda rng, *a: maybe(rng, ("a" * 12, ["p"]), 0.25)),
                ("queue_verdict", lambda rng, *a, **k: None), ("conflict_cause", lambda rng, *a: " c"),
                ("conflict_paths", lambda rng, *a: ["p"]), ("remote", lambda rng: "origin"),
                ("queued_waiting_on_changes", lambda rng, *a: ([f"w{rng.randint(0, 3)}"] * rng.randint(0, 1), rng.random() < 0.4)),
                ("regen_hold_overdue", lambda rng, *a: rng.random() < 0.5),
                ("retry_hold", lambda rng, e: ("retry", "why")), ("batch_goes_first", batch),
                ("behind_batch", lambda rng, *a: [f"bb{rng.randint(0, 3)}"] * rng.randint(0, 1)),
                ("check_tree_slot", slot), ("check_tree_slot_free", lambda rng, *a: rng.random() < 0.5),
                ("policy_blocks", lambda rng, *a: rng.random() < 0.6), ("spawn_executor", lambda rng, *a: None),
                ("hhmmz", lambda rng, x: f"h{x}")):
            case.fake(name, gen)
        ids = [e["id"] for e in entries]
        pick = lambda: {i for i in ids if r.random() < 0.3}  # noqa: E731
        kinds = {e["id"]: {"red_fix": r.random() < 0.3} for e in entries if r.random() < 0.5}
        return ("repo", "d", st, "clock", r.randint(0, 100), {}, kinds, r.choice(["push", "check"]), entries, entries,
                pick(), pick(), r.choice([None, 3, 8]), {})

    def test_tick_changes_pass(self):
        check(self, A, "_tick_changes_pass", self.build, lambda A: A._tick_changes_pass)


def item(rng, i, states):
    return {"id": f"i{i}", "state": rng.choice(states), "entered_at": rng.randint(3000, 5000),
            "data": rng.choice([None, {}, {"landed_sha": f"s{rng.randint(0, 3)}", "submitted_at": rng.randint(2000, 4000)}])}


def raising(rng, p, value):
    if rng.random() < p:
        raise RuntimeError("down")
    return value


class StatsPassTest(unittest.TestCase):
    def build(self, case):
        r, ticks = case.rng, [0]

        def monotonic(rng):
            ticks[0] += rng.randint(0, 6)
            return ticks[0]
        clock = Probe(case, "clock", time=lambda rng: 5000, monotonic=monotonic)
        items = lambda rng, typ, states=None: [item(rng, i, states or ["landed", "queued", "rejected", "ok", "red", "verified"])  # noqa: E731
                                              for i in range(rng.randint(0, 3))]
        st = Probe(case, "st", meta_get=lambda rng, k, default=None: maybe(rng, rng.randint(1000, 4000), 0.4) if k == "stats_since" else maybe(rng, 1, 0.2),
                   meta_set=lambda rng, *a: None, items=items)
        for name, gen in (
                ("stats_url", lambda rng: maybe(rng, "http://u", 0.85)),
                ("env_num", lambda rng, name, default: rng.choice([1, 3, 20, 900]) if "SETTLE" in name else rng.choice([2, 4, 150]) if "PER_PASS" in name else rng.choice([5, 40])),
                ("waiting_entries", lambda rng, d, open_only=True: [{"id": f"i{rng.randint(0, 3)}"} for _ in range(rng.randint(0, 2))]),
                ("item_events", lambda rng, st, typ, iid: [{"to_state": "x"}]),
                ("check_runs_of", lambda rng, *a: raising(rng, 0.2, [{"run_id": rng.randint(0, 2)}])),
                ("change_paths", lambda rng, *a: ["p"]),
                ("landing_records", lambda rng, *a: [{"record": "landing", "backfill": rng.random() < 0.5}] * rng.randint(1, 2)),
                ("watch_record", lambda rng, *a: {"record": "watch", "backfill": rng.random() < 0.5}),
                ("red_record", lambda rng, *a: {"record": "red", "backfill": False}),
                ("owner_decision_record", lambda rng, *a: maybe(rng, {"record": "od", "backfill": True}, 0.4)),
                ("condition_record", lambda rng, *a: {"record": "cond", "backfill": False}),
                ("stats_post", lambda rng, url, recs: raising(rng, 0.15, None)),
                ("stats_echo", lambda rng, *a: None), ("change_waits", lambda rng, *a: [("i1", 1, 2)]),
                ("gate_runs_pass", lambda rng, *a: rng.randint(0, 3)), ("err", lambda rng, m: None)):
            case.fake(name, gen)
        return (clock, st, "repo", maybe(r, "dir", 0.8), maybe(r, "http://given", 0.5), None)

    def test_stats_pass(self):
        check(self, A, "stats_pass", self.build, lambda A: A.stats_pass)


if __name__ == "__main__":
    unittest.main()
