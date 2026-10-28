"""The machines (design §5.5, reconciler phase 1 step 5): every state has a finite deadline and an automatic exit in its
own machine; with no external progress at all, every state ends in a terminal state or cycles through an escalation;
items change state only through Store.transition (compare-and-set, never out of a terminal state, its event in the
same transaction)."""
import os, re, sys, tempfile, unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from test_agentctl import A, TOOL  # noqa: E402


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

    def test_transition_is_compare_and_set_never_out_of_terminal_and_writes_its_event(self):
        t = [1000.0]
        st = A.Store(path=os.path.join(tempfile.mkdtemp(), "store.db"), clock=lambda: t[0])
        it = st.put_item("change", "e1", "queued", {"worktree": "/w"})
        self.assertEqual((it["state"], it["version"], it["deadline"]), ("queued", 1, 1000.0 + 15 * 60))
        self.assertEqual(st.put_item("change", "e1", "checking")["state"], "queued")  # idempotent create
        v = st.transition("change", "e1", "checking", 1, data={"tip": "abc"})
        self.assertEqual(v, 2)
        with self.assertRaises(A.StaleVersion):
            st.transition("change", "e1", "checked", 1)  # a racing writer loses cleanly
        st.transition("change", "e1", "rejected", 2)
        with self.assertRaises(A.TerminalState):
            st.transition("change", "e1", "queued", 3)  # audit S2: never out of a terminal state
        with self.assertRaises(ValueError):
            st.transition("change", "e1", "nonsense", 3)
        evs = st.events()
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
        st.transition("change", "e2", "queued", 1)
        it = st.item("change", "e2")
        self.assertEqual(it["level"], 1)
        self.assertEqual(st.events()[-1]["severity"], "escalate")

    def test_only_transition_writes_an_items_state(self):
        with open(TOOL) as f:
            src = f.read()
        writes = [m.start() for m in re.finditer(r"UPDATE items SET state", src)]
        self.assertEqual(len(writes), 1)
        body = src[src.index("    def transition(self, typ, iid, to,"):]
        self.assertIn("UPDATE items SET state", body[:body.index("\n    def ", 10)])


class OneLoopTest(unittest.TestCase):
    def test_the_processor_is_one_loop_and_the_batch_no_branch(self):
        """Step 8: no duties thread, no in-process watchdog, no waiting round, no `waiting` branch or worktree."""
        for gone in ("start_duties", "start_watchdog", "waiting_round", "push_batch", "ensure_waiting_tree",
                     "drop_waiting_tree", "batch_tree", "batch_state", "rebuild"):
            self.assertFalse(hasattr(A, gone), gone)
        with open(TOOL) as f:
            src = f.read()
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
        self.assertEqual([e["kind"] for e in st.events()], ["change.checking"])


if __name__ == "__main__":
    unittest.main()
