"""ONE SOURCE OF THE TERMINAL STATES (review 2026-11-26, C 1.1): which states end an item was spelled in six lists --
timeline's type-blind set, backfillexact's TERMINAL and GATE_DONE, statsepisodes' RED_CLOSED, redrecord's RED_TERMINAL,
statsrecords' CHANGE_EXITS -- and they disagreed. statemachines.terminal_states reads MACHINES (and the file-held
types' ends); every reader asks it, so a state added to a machine is an end everywhere at once."""
import os, sys, unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from test_agentctl import A  # noqa: E402


class TerminalStatesTest(unittest.TestCase):
    def test_each_machine_ends_in_its_own_terminal_states(self):
        for typ, machine in A.MACHINES.items():
            self.assertEqual(A.terminal_states(typ), tuple(n for n, s in machine.items() if s.terminal), typ)
        self.assertEqual(A.terminal_states("red"), ("verified", "cleared"))
        self.assertEqual(A.terminal_states("change"), ("landed", "rejected", "superseded", "cancelled"))

    def test_file_held_types_end_as_their_writers_say(self):
        self.assertEqual(A.terminal_states("gov-ticket"), ("released",))
        self.assertEqual(A.terminal_states("entry"), A.terminal_states("change"))
        self.assertTrue(A.is_terminal("escalation", "owner"))
        self.assertFalse(A.is_terminal("inbox", "unread"))
        self.assertEqual(A.terminal_states("lease"), (), "a type with no machine and no ends never ends")

    def test_a_state_is_terminal_only_for_its_own_type(self):
        self.assertTrue(A.is_terminal("watch", "red"))
        self.assertFalse(A.is_terminal("red", "red"), "a red item's live state")
        self.assertFalse(A.ends_item("red", "red"))

    def with_terminal(self, typ, state):
        """MACHINES[typ] with one more terminal state, for this test."""
        saved = A.MACHINES[typ]
        A.MACHINES[typ] = {**saved, state: A.StateSpec(A.ARCHIVE, state, (), terminal=True)}
        self.addCleanup(A.MACHINES.__setitem__, typ, saved)

    def test_a_terminal_state_added_to_a_machine_ends_items_in_every_reader(self):
        self.with_terminal("change", "withdrawn")
        self.with_terminal("red", "retired")
        it = {"id": "c1", "state": "withdrawn", "entered_at": 50.0, "data": {"submitted_at": 10.0}}
        evs = [{"item_type": "change", "from_state": "queued", "to_state": "withdrawn", "at": 50.0, "text": "x"}]
        _evs, _start, exit_ev, _end = A.item_span(it, evs, "change", "submitted_at", A.terminal_states("change"))
        self.assertIs(exit_ev, evs[0], "the stats' change exit")
        self.assertEqual(A.change_exit(it, {}, exit_ev)[0], "withdrawn")
        self.assertTrue(A.ends_item("change", "withdrawn"), "history and analyze")
        self.assertTrue(A.ends_item("red", "retired"))
        self.assertEqual(A._closed_at([(1.0, "offered"), (5.0, "retired")]), 5.0, "the backfill's gate-red window")


if __name__ == "__main__":
    unittest.main()
