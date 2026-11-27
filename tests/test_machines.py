"""The machines (design §5.5, reconciler phase 1 step 5): every state has a finite deadline and an automatic exit in its
own machine; with no external progress at all, every state ends in a terminal state or cycles through an escalation;
items change state only through Store.transition (compare-and-set, never out of a terminal state, its event in the
same transaction)."""
import os, re, sys, tempfile, unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from test_agentctl import A, TOOL, code_source  # noqa: E402
from fake_clock import on_clock  # noqa: E402


class MachinesTest(unittest.TestCase):
    def test_every_state_has_a_finite_deadline_and_an_exit_in_its_machine(self):
        for typ, states in A.MACHINES.items():
            for name, spec in states.items():
                self.assertTrue(0 < spec.deadline < float("inf"), (typ, name))
                self.assertIn(spec.on_deadline, states, (typ, name))
                for x in spec.exits:
                    self.assertIn(x, states, (typ, name, x))

    def test_without_progress_every_state_ends_terminal_or_escalates_forever(self):
        for typ, states in A.MACHINES.items():
            for name in states:
                terminal, escalates = A.deadline_walk(typ, name)
                self.assertTrue(terminal or escalates, f"{typ}.{name} can time out into silence")

    def test_a_deadline_loops_back_into_its_own_state_only_in_a_wait_state_which_escalates(self):
        """The simplification plan's state-machine check: a working state whose deadline re-entered itself spun silently
        (the `assembling` batch of 2026-10-31). Only a state waiting on something outside the machine may, escalating."""
        for typ, states in A.MACHINES.items():
            for name, spec in states.items():
                if spec.terminal or spec.on_deadline != name:
                    continue
                self.assertTrue(spec.wait, f"{typ}.{name}: its deadline loops back into it, but it is no wait state")
                self.assertTrue(spec.escalate, f"{typ}.{name}: a wait state that loops must escalate")

    def test_transition_is_compare_and_set_never_out_of_terminal_and_writes_its_event(self):
        t = [1000.0]
        st = A.Store(path=os.path.join(tempfile.mkdtemp(), "store.db"), clock=lambda: t[0])
        it = st.put_item("change", "e1", "queued", {"worktree": "/w"})
        self.assertEqual((it["state"], it["version"], it["deadline"]), ("queued", 1, 1000.0 + 15 * 60))
        self.assertEqual(st.put_item("change", "e1", "checking")["state"], "queued")  # idempotent create
        v = st.transition("change", "e1", "checking", 1, data={"tip": "abc"}, event={"cause": "agent"})
        self.assertEqual(v, 2)
        with self.assertRaises(A.StaleVersion):
            st.transition("change", "e1", "checked", 1, event={"cause": "agent"})  # a racing writer loses cleanly
        st.transition("change", "e1", "rejected", 2, event={"cause": "agent"})
        with self.assertRaises(A.TerminalState):
            st.transition("change", "e1", "queued", 3, event={"cause": "agent"})  # audit S2: never out of a terminal state
        with self.assertRaises(ValueError):
            st.transition("change", "e1", "nonsense", 3, event={"cause": "agent"})
        evs = [e for e in st.events() if e["from_state"] is not None]  # the moves (an opening comes from no state)
        self.assertEqual([(e["kind"], e["from_state"], e["to_state"]) for e in evs],
                         [("change.checking", "queued", "checking"), ("change.rejected", "checking", "rejected")])
        self.assertEqual(st.item("change", "e1")["data"], {"worktree": "/w", "tip": "abc"})
        st.put_item("change", "e3", "queued")
        dl = st.item("change", "e3")["deadline"]
        self.assertEqual(st.update_item("change", "e3", 1, {"note": "x"}), 2)  # data only: no state, deadline, event
        it = st.item("change", "e3")
        self.assertEqual((it["state"], it["deadline"], it["data"]), ("queued", dl, {"note": "x"}))
        with self.assertRaises(A.StaleVersion):
            st.update_item("change", "e3", 1, {})

    def test_a_self_transition_of_an_escalating_state_escalates_and_raises_its_level(self):
        st = A.Store(path=os.path.join(tempfile.mkdtemp(), "store.db"))
        st.put_item("change", "e2", "queued")
        st.transition("change", "e2", "queued", 1, event={"cause": "agent"})
        it = st.item("change", "e2")
        self.assertEqual(it["level"], 1)
        self.assertEqual(st.events()[-1]["severity"], "escalate")

    def test_only_transition_writes_an_items_state(self):
        src = code_source()
        writes = [m.start() for m in re.finditer(r"UPDATE items SET state", src)]
        self.assertEqual(len(writes), 1)
        body = src[src.index("    def transition(self, typ, iid, to,"):]
        self.assertIn("UPDATE items SET state", body[:body.index("\n    def ", 10)])


class DeadlineTickTest(unittest.TestCase):
    """2026-11-12 (issue 20261111T124954-ec04bd): `issue` and `lander` had deadlines nothing fired, so
    AgentctlItemsPastDeadline counted them for days. Every machine names the step that moves it past its deadline."""
    def test_every_machine_names_the_tick_that_fires_its_deadline(self):
        self.assertEqual(set(A.DEADLINE_TICKS), set(A.MACHINES), "a machine with no deadline tick")
        for typ, tick in A.DEADLINE_TICKS.items():
            self.assertTrue(callable(getattr(A, tick, None)), (typ, tick))

    def test_lander_records_and_forgotten_flips_retire_quietly_at_their_deadline(self):
        t = [1_000_000.0]
        st = A.Store(path=os.path.join(tempfile.mkdtemp(), "store.db"), clock=lambda: t[0])
        st.put_item("lander", "c-old", "recorded", {"change": "c-old"})
        st.put_item("explorer-flip", "f-old", "open", {})
        t[0] += A.LANDER_KEEP - 60
        st.put_item("lander", "c-new", "recorded", {"change": "c-new"})
        self.assertEqual(A.tick_quiet_deadlines(st), [])
        t[0] += 120
        self.assertEqual(sorted(A.tick_quiet_deadlines(st)), ["explorer-flip f-old: closed", "lander c-old: gone"])
        self.assertEqual(A.tick_quiet_deadlines(st), [], "a second tick moves nothing")
        self.assertEqual([st.item("lander", i)["state"] for i in ("c-old", "c-new")], ["gone", "recorded"])
        self.assertEqual(st.item("explorer-flip", "f-old")["state"], "closed")
        moved = [e for e in st.events() if e["item_id"] in ("c-old", "f-old") and e["from_state"] is not None]
        self.assertEqual([e["severity"] for e in moved], ["info", "info"], "a retirement never notifies")
        past = sum(1 for typ in A.MACHINES for it in st.items(typ)
                   if not A.machine_state(typ, it["state"]).terminal and it["deadline"] < t[0])
        self.assertEqual(past, 0)

    def test_the_server_tick_runs_it(self):
        self.assertIn("quiet deadlines", [n for n, _ in A.server_steps("/r", tempfile.mkdtemp(), lambda f: f, *on_clock(A.Clock()))])


class OneLoopTest(unittest.TestCase):
    def test_the_processor_is_one_loop_and_the_batch_no_branch(self):
        """Step 8: no duties thread, no in-process watchdog, no waiting round, no `waiting` branch or worktree."""
        for gone in ("start_duties", "start_watchdog", "waiting_round", "push_batch", "ensure_waiting_tree",
                     "drop_waiting_tree", "batch_tree", "batch_state", "rebuild"):
            self.assertFalse(hasattr(A, gone), gone)
        src = code_source()
        self.assertNotIn("threading.Thread(target=loop", src)
        for s_ in ("blocked-red", "assembling", "checking", "bisecting", "pushing", "held", "landed"):
            self.assertIn(s_, A.MACHINES["batch"])


class ProjectionTest(unittest.TestCase):
    def test_the_queue_entries_project_to_change_items_with_their_transitions(self):
        root = tempfile.mkdtemp()
        st = A.Store(path=os.path.join(root, "store.db"))
        d = os.path.join(root, "w")
        os.makedirs(os.path.join(d, "entries"))
        A.write_json(os.path.join(d, "entries", "e1.json"), {"id": "e1", "seq": 1, "status": "queued"})
        A.write_json(os.path.join(d, "entries", "e2.json"), {"id": "e2", "seq": 2, "status": "landed"})
        self.assertEqual({i["id"]: i["state"] for i in A.project_changes(d, st)}, {"e1": "queued", "e2": "landed"})
        A.write_json(os.path.join(d, "entries", "e1.json"), {"id": "e1", "seq": 1, "status": "checking"})
        self.assertEqual(A.project_changes(d, st)[0]["state"], "checking")
        self.assertEqual([e["kind"] for e in st.events() if e["from_state"] is not None], ["change.checking"])

    def test_an_entry_of_a_retired_status_is_decided_never_an_open_change(self):
        """ONE CHANGE, ONE RECORD (docs/design/one-change-model.md, step 1): `joined` (no writer since 6d206ed,
        2026-11-18) and `migrated` (the laptop outbox's "sent") are no change states. An entry file still carrying one
        is decided: no item is made or moved for it -- the projector made a joined entry whose waiting entry was gone
        a LANDED item with no sha -- and the queue never picks it."""
        root = tempfile.mkdtemp()
        st = A.Store(path=os.path.join(root, "store.db"))
        d = os.path.join(root, "w")
        os.makedirs(os.path.join(d, "entries"))
        for i, status in ((1, "joined"), (2, "migrated"), (3, "bogus")):
            A.write_json(os.path.join(d, "entries", f"q{i}.json"),
                         {"id": f"q{i}", "seq": i, "status": status, "waiting_id": "gone", "worktree": "/wt/q"})
        A.write_json(os.path.join(d, "entries", "w1.json"), {"id": "w1", "seq": 4, "status": "waiting", "worktree": "/wt/w"})
        self.assertEqual({i["id"]: i["state"] for i in A.project_changes(d, st)}, {"w1": "waiting"})
        self.assertIsNone(st.item("change", "q1"))
        self.assertEqual([e["id"] for e in A.waiting_entries(d)], ["w1"])
        self.assertEqual(A.queued_entries(d), [])
        self.assertEqual([s for s in ("queued", "checking", "checked", "pushing", "waiting", "joined", "migrated", "bogus", None)
                          if A.entry_final(s)], ["joined", "migrated", "bogus", None])

    def test_a_change_decided_on_its_item_first_keeps_that_state_on_its_entry(self):
        """IS (tests/queue_sim.py): a change cancelled or superseded on its item alone (`land cancel` through the store,
        a stranded supersede) and then rejected by its checker read `rejected` on its entry and `cancelled` on its
        item -- two answers to one question. The first decision stands on both: the entry takes the item's state."""
        root = tempfile.mkdtemp()
        st = A.Store(path=os.path.join(root, "store.db"))
        d = os.path.join(root, "w")
        os.makedirs(os.path.join(d, "entries"))
        e = {"id": "e1", "seq": 1, "status": "checking", "worktree": "/wt/a", "head": "h"}
        it = st.put_item("change", "e1", "checking", {"worktree": "/wt/a"})
        st.transition("change", "e1", "cancelled", it["version"], data={"why": "cancelled by `agentctl land cancel`"},
                      event={"cause": "agent"})
        A.set_change(st, d, e, "rejected", cause="check-failed-test", why="NOT LANDED: the pre-push checks failed")
        disk = A.read_record(os.path.join(d, "entries", "e1.json"))
        self.assertEqual((st.item("change", "e1")["state"], e["status"], disk["status"]), ("cancelled",) * 3)
        self.assertEqual(disk["why"], "cancelled by `agentctl land cancel`")

    def test_a_final_status_on_disk_stands_against_a_racing_open_write(self):
        """save_entry's race rule (audit S2), characterised before the files go: a decided entry on disk keeps its
        status and verdict when a racing writer saves the entry open."""
        d = tempfile.mkdtemp()
        os.makedirs(os.path.join(d, "entries"))
        A.save_entry(d, {"id": "e1", "seq": 1, "status": "superseded", "why": "by e2", "superseded_by": "e2"})
        A.save_entry(d, {"id": "e1", "seq": 1, "status": "checked", "tip": "t"})
        e = A.read_record(os.path.join(d, "entries", "e1.json"))
        self.assertEqual((e["status"], e["why"], e["superseded_by"], e["tip"]), ("superseded", "by e2", "e2", "t"))

if __name__ == "__main__":
    unittest.main()
