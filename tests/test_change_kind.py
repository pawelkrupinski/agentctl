"""ONE CLASSIFIER (changekind.py; the owner, 2026-11-19: "fix that structurally"). A change's KIND -- which open reds it
fixes and their kinds, gate fix or not, why it may not join a batch, its priority class -- is decided by one function,
and every decision reads it. These are its unit tests and the decisions whose predicates disagreed before it (the
queue's own scheduling is tested in tests/test_backlog_batch.py and tests/queue_sim.py)."""
import os, sys, tempfile, time, unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from test_agentctl import A  # noqa: E402


class ChangeKindTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.st = A.Store(path=os.path.join(self.tmp.name, "store.db"))

    def tearDown(self):
        self.tmp.cleanup()

    def red(self, rid, runs, gate=False, kind="outside", state="offered"):
        self.st.put_item("red", rid, state, {"key": rid, "runs": list(runs), "gate": gate, "red_kind": kind,
                                            "workflow": "W"})

    def test_a_fix_names_its_open_reds_with_their_kinds(self):
        self.red("r-out", ["6"])
        self.red("r-flake", ["8"], kind="flake")
        self.red("r-closed", ["9"], state="offered")
        it = self.st.item("red", "r-closed")
        self.st.transition("red", "r-closed", "cleared", it["version"])
        facts = A.queue_facts({"7"}, self.st)
        k = A.classify_change({"fixes": ["#6", "7", "8", "9", "404"]}, facts)
        self.assertEqual(k["reds"], [{"name": "6", "red": "r-out", "kind": "outside"},
                                     {"name": "7", "red": None, "kind": "gate"},
                                     {"name": "8", "red": "r-flake", "kind": "flake"}])
        self.assertEqual((k["red_fix"], k["gate_fix"], k["priority"]), (True, True, "red-fix"))
        self.assertEqual(k["backlog_batch"], "red-fix")
        self.assertEqual(k["push_batch"], "red-fix")
        self.assertIn("6 (outside)", A.kind_text(k))

    def test_a_trailer_naming_only_closed_or_unknown_reds_is_an_ordinary_change(self):
        """Before: the backlog batch put ANY change with a `Fixes-Red:` first (`0 if e.get("fixes")`)."""
        k = A.classify_change({"fixes": ["404"], "status": "queued"}, A.queue_facts(set(), self.st))
        self.assertEqual((k["red_fix"], k["gate_fix"], k["priority"], k["backlog_batch"]),
                         (False, False, "ordinary", None))

    def test_a_fix_naming_a_run_of_the_open_episode_counts_though_the_red_item_dropped_it(self):
        """2026-11-25: the open gate red's item names #8 only; #7 failed the SAME way (its cached signature) since the last
        green #5, so a fix naming #7 fixes the red (its episode). #6 failed another way and #4 is before the green: a fix
        naming either is no fix of it and still waits."""
        os.environ["AGENTCTL_STATE_FILE"] = os.path.join(self.tmp.name, "state.json")
        os.environ["AGENTCTL_REDS_DIR"] = os.path.join(self.tmp.name, "reds")
        self.addCleanup(lambda: [os.environ.pop(k, None) for k in ("AGENTCTL_STATE_FILE", "AGENTCTL_REDS_DIR")])

        def run(n, ok=False):
            return {"number": n, "databaseId": 1000 + n, "status": "completed",
                    "conclusion": "success" if ok else "failure"}

        def signed(n, line):
            A.write_json(A.reds_dir("runsig", f"{1000 + n}.json"), {"classifier": A.classifier_version(),
                         "sigs": [{"job": "Unit tests", "kind": "test", "line": line}]})
        runs = [run(8), run(7), run(6), run(5, ok=True), run(4)]
        for n, line in ((8, "Failed: A"), (7, "Failed: A"), (6, "Failed: B"), (4, "Failed: A")):
            signed(n, line)
        A.state_update(ci={"repo": "pawelkrupinski/movies", "at": A.epoch_now(), "gate": [
            {"name": "Main", "decided": runs[0], "newer": [], "since": "2026-11-25T10:00:00Z", "count": 3, "runs": runs}]})
        self.st.put_item("red", "k8", "leased", {"key": "k8", "workflow": "Main", "gate": True, "red_kind": "test",
                                                "job": "Unit tests", "line": "Failed: A", "runs": ["8"]})
        facts = A.queue_facts({"8"}, self.st)
        by = lambda n: A.classify_change({"fixes": [n]}, facts)
        self.assertTrue(by("7")["gate_fix"], "a run of the same episode and signature")
        self.assertFalse(by("6")["red_fix"], "a run failing another way")
        self.assertFalse(by("4")["red_fix"], "a run of the older episode")

    def test_the_gate_decides_with_the_gate_s_reds_alone(self):
        self.red("r-out", ["6"])
        k = A.classify_change({"fixes": ["6"]}, A.queue_facts(set(), self.st))
        self.assertEqual((k["red_fix"], k["gate_fix"]), (True, False), "a red fix, no gate red's fix: it may still wait")

    def test_why_a_change_may_not_join_a_batch(self):
        facts = A.gate_facts(set())
        why = lambda **e: (A.classify_change({"status": "queued", **e}, facts)["backlog_batch"],
                           A.classify_change({"status": "checked", **e}, facts)["push_batch"])
        self.assertEqual(why(no_batch=True), ("no-batch", "no-batch"))
        self.assertEqual(why(batch_alone="regen"), ("batch-alone: regen", "batch-alone: regen"))
        self.assertEqual(why(push_alone="drift"), (None, "push-alone: drift"))
        self.assertEqual(why(known_red={"keys": []}), (None, "known-red"))
        self.assertEqual(why(), (None, None))

    def test_a_verify_round_running_now_is_the_verify_class(self):
        t = A.epoch_now()
        k = A.classify_change({"verify": True, "status": "queued"}, A.gate_facts(set()), t)
        self.assertEqual((k["priority"], k["rank"]), ("verify", 1))
        k = A.classify_change({"verify": True, "status": "queued", "retry_after": t + 60}, A.gate_facts(set()), t)
        self.assertEqual(k["priority"], "ordinary", "its retry is not due: no round runs now")

    def test_the_priority_classes_are_the_table_s_order(self):
        self.assertEqual([n for n, _w in A.PRIORITY], ["red-fix", "verify", "ordinary"])
        self.assertEqual(A.PRIORITY_RANK, {"red-fix": 0, "verify": 1, "ordinary": 2})

    def test_one_trailer_parser(self):
        """Before: `Fixes-Red: 6 7` was run "6 7" to the queue (commas only) and runs 6 and 7 to the red machine."""
        self.assertEqual(A.trailer_values("6 7"), {"6", "7"})
        self.assertEqual(A.trailer_values("#6, 7;8\n"), {"6", "7", "8"})
        self.assertEqual(A.trailer_values(""), set())
        self.assertEqual(A.fixes_red_in_message("subject\n\nbody\n\nFixes-Red: #6, 7\nFixes-Red: 8\n"), {"6", "7", "8"})
        self.assertEqual(A.fixes_named({"fixes": ["#6", 7, " "]}), {"6", "7"})

    def test_the_governor_admits_a_fix_of_any_open_red_as_red(self):
        """Before: gov_class_of read the gate's reds alone -- a fix of a red outside the gate queued behind the
        governor like an ordinary check."""
        self.red("r-out", ["6"])
        saved = A.commit_fixes, A.touches_production
        A.commit_fixes = lambda repo, c, store: {"6"}
        A.touches_production = lambda repo, c: True
        try:
            self.assertEqual(A.gov_class_of("/nonexistent", ["c1"], set(), self.st), "red")
            self.assertEqual(A.gov_class_of("/nonexistent", [], set(), self.st), "check")
        finally:
            A.commit_fixes, A.touches_production = saved


class FactsPerTickTest(unittest.TestCase):
    """THE QUEUE FACTS ARE READ ONCE PER TICK (changekind.py facts_tick): the review found queue_facts recomputed per
    call -- every must_wait_why, batch order, classification and push read the store's red items again."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.st = A.Store(path=os.path.join(self.tmp.name, "store.db"))
        self.st.put_item("red", "k7", "leased", {"key": "k7", "workflow": "Main", "gate": True, "runs": ["7", "8"]})
        self.reads = 0
        real = self.st.items

        def counted(typ, *a, **k):
            self.reads += typ == "red"
            return real(typ, *a, **k)
        self.st.items = counted

    def test_inside_a_tick_one_read_per_gate_reading_outside_every_call_reads(self):
        with A.facts_tick():
            for _ in range(5):
                self.assertIn("7", A.facts_for({"8"}, self.st)["open"])
            A.facts_for({"9"}, self.st)  # another gate reading: its own read
            with A.facts_tick():  # a nested scope shares the tick's
                A.facts_for({"8"}, self.st)
        self.assertEqual(self.reads, 2)
        A.facts_for({"8"}, self.st)
        A.facts_for({"8"}, self.st)
        self.assertEqual(self.reads, 4, "outside a tick, afresh")

    def test_the_queue_ticks_read_the_facts_once_each(self):
        """Over a simulated red with a waiting fix (queue_sim.REDFIX_OLDER_RUN), each tick's change and batch ticks
        read the facts at most once each: before, up to 8 reads a tick."""
        import queue_sim
        per_tick, real, sim = {}, A.queue_facts, {}

        def counting(gate, st):
            if A._tick_memo.get("memo") is not None:  # inside a queue tick
                per_tick[sim["it"].tick] = per_tick.get(sim["it"].tick, 0) + 1
            return real(gate, st)
        A.queue_facts = counting
        self.addCleanup(setattr, A, "queue_facts", real)
        s = queue_sim.Sim(0, calm=True, trees=2, script=list(queue_sim.REDFIX_OLDER_RUN))
        sim["it"] = s
        s.run()
        self.assertTrue(per_tick)
        self.assertLessEqual(max(per_tick.values()), 2, per_tick)


class WatchdogKindTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.st = A.Store(path=os.path.join(self.tmp.name, "store.db"))

    def tearDown(self):
        self.tmp.cleanup()

    def test_wr_names_a_slow_fix_of_a_red_outside_the_gate(self):
        """Before: WR read the CI snapshot's gate reds alone -- 08010c, unlanded 2 h, was never flagged."""
        os.environ["AGENTCTL_UNHELD_GRACE"] = "0"  # c-fix's lack of a hold is stuck at once (UNHELD_GRACE passed)
        self.addCleanup(os.environ.pop, "AGENTCTL_UNHELD_GRACE", None)
        self.st.put_item("red", "r-out", "fix-pending", {"key": "r-out", "runs": ["6"], "gate": False,
                                                          "red_kind": "outside"})
        self.st.put_item("change", "c-fix", "checked", {"fixes": ["6"], "submitted_at": A.epoch_now() - 3600})
        self.st.put_item("change", "c-wait", "waiting", {"fixes": ["6"], "submitted_at": A.epoch_now()})
        ok, why = A.inv_wr(A.Clock(), st=self.st, reds=set())
        self.assertFalse(ok, why)
        self.assertIn("c-fix (red fix of 6 (outside)) is checked", why)
        self.assertNotIn("c-wait", why, "only a GATE red's fix may not wait for green")

    def test_a_red_gate_with_a_stalled_red_fix_shows_one_line_naming_the_rule_and_the_change(self):
        """2026-11-25: the gate sat red for hours, its fix `waiting` for green, and `status` said nothing of it. Over
        RED_LIVENESS_MAX (60 min, $AGENTCTL_RED_LIVENESS_MAX) red with a red fix that does not move: ONE line with the
        change and the rule blocking it, in `status` and `red status`; none while the gate is young or the fix moves."""
        t = A.epoch_now()
        snap = lambda ago: {"repo": "pawelkrupinski/movies", "at": t, "gate": [{
            "name": "Main", "decided": {"number": 8, "conclusion": "failure"}, "newer": [], "count": 1, "runs": [],
            "since": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(t - ago))}]}
        self.st.put_item("red", "k8", "leased", {"key": "k8", "workflow": "Main", "gate": True, "runs": ["8"]})
        self.st.put_item("change", "c-fix", "waiting", {"fixes": ["8"], "submitted_at": t - 100,
                                                        "wait_rule": "abc1234567 names 9, no run of the open gate red"})
        self.assertIsNone(A.red_fix_liveness(self.st, t, snap(50 * 60)), "red for 50 min: not yet")
        line = A.red_fix_liveness(self.st, t, snap(80 * 60))
        self.assertEqual(line.count("\n"), 0)
        self.assertIn("the gate has been red 1 h", line)
        self.assertIn("c-fix (red fix of 8 (gate)) is waiting for main to turn green: abc1234567 names 9, no run of the "
                      "open gate red", line)
        self.assertEqual(A.with_liveness("the status line\nchanges: 1", self.st, t), "the status line\nchanges: 1",
                         "no snapshot read: no line")
        it = self.st.item("change", "c-fix")
        self.st.transition("change", "c-fix", "queued", it["version"])
        self.assertIsNone(A.red_fix_liveness(self.st, t, snap(80 * 60)), "its fix is queued, not stalled")

    def test_a_stale_hold_is_re_derived_by_the_tick_never_paged(self):
        """THE HOLD TABLE's recovery (policy.py HOLDS, 2026-11-20): a hold whose blocker ended, or past its kind's own
        bound, describes a world that moved -- the lap drops it (the tick derives it afresh), quietly, no issue."""
        t = A.epoch_now()
        for eid, hold in (("c-gone", {"kind": "overlap", "why": "its check waits for x", "blocker": "x-gone", "since": t}),
                          ("c-old", {"kind": "backlog-batch", "why": "the next backlog batch", "since": t - 3 * 3600})):
            self.st.put_item("change", eid, "queued", {"worktree": f"/w/{eid}", "hold": hold})
            it = self.st.item("change", eid)
            self.st.transition("change", eid, "queued", it["version"], deadline_s=-1)  # past its deadline
            A.lap_change_deadline(self.st, {"id": eid, "worktree": f"/w/{eid}"}, "queued", t)
            it = self.st.item("change", eid)
            # NEVER UNHELD BETWEEN TWO READINGS (queuepush.py): kept, its clock restarted, until the tick records anew
            self.assertEqual((it["data"]["hold"]["why"], it["data"]["hold"]["since"]), (hold["why"], t), eid)
            evs = [e for e in self.st.item_events("change", eid) if e["kind"] == "change.rederived"]
            self.assertEqual(len(evs), 1, eid)
            self.assertEqual(evs[0]["severity"], "info")
            self.assertEqual(evs[0]["audience"], {}, "never an inbox entry")
        self.assertEqual(self.st.items("issue", ("open",)), [], "a stale hold is no agentctl bug")

    def test_a_change_past_its_deadline_with_no_recorded_hold_is_filed_once_as_an_agentctl_issue(self):
        t = A.epoch_now()
        self.st.put_item("change", "c-silent", "checked", {"worktree": "/w/silent"})
        self.st.put_item("change", "c-held", "checked", {"worktree": "/w/held", "hold": {"kind": "batch", "why": "x"}})
        self.st.put_item("change", "c-young", "queued", {"worktree": "/w/young"})
        later = t + 10 * 60  # past `checked`'s 5 min, inside `queued`'s 15
        lines = A.watchdog_unheld(self.st, entries={}, now_=later)
        self.assertEqual(len(lines), 1, lines)
        self.assertIn("c-silent", lines[0])
        issues = self.st.items("issue", ("open",))
        self.assertEqual(len(issues), 1)
        self.assertTrue(issues[0]["data"]["text"].startswith("[change-unheld c-silent]"))
        self.assertEqual(self.st.item("change", "c-silent")["data"]["unheld_issue"], issues[0]["id"])
        A.watchdog_unheld(self.st, entries={}, now_=later + 60)
        self.assertEqual(len(self.st.items("issue", ("open",))), 1, "filed once")
        self.assertEqual([e for e in self.st.events(0) if e.get("severity") == "escalate"], [],
                         "the owner is not paged")

    def test_a_batch_member_or_a_retry_is_a_visible_hold(self):
        t = A.epoch_now()
        it = self.st.put_item("change", "c-m", "queued", {})
        self.assertIsNotNone(A.change_hold_visible(self.st, it, {"backlog_batch": "b#1"}, t))
        self.assertIsNotNone(A.change_hold_visible(self.st, it, {"retry_after": t + 60}, t))
        self.assertIsNone(A.change_hold_visible(self.st, it, {}, t))


if __name__ == "__main__":
    unittest.main()
