"""PROPERTIES OF THE MACHINES (2026-11-20, the owner: "fix agentctl issues structurally"; 46 of 146 closed issues were a
state that moved, or sat, where no table said it could). Over random event sequences, seeded so a failure replays:

- Store.transition takes exactly the moves MACHINES lists: a listed exit (or a self-move) succeeds and bumps the
  version; anything else raises IllegalTransition (TerminalState out of a terminal state), changes nothing, and leaves
  one `<type>.illegal` event naming the move;
- a deadline's own move (on_deadline) is a legal move, so the reconciler's timeout never needs an illegal one;
- no reachable state is STUCK: every one ends in a terminal state or cycles through an escalation with no progress;
- every recorded hold kind (policy.py HOLDS) has a registered policy, a finite bound and a recovery, and a red fix
  only waits on its own resources."""
import os, random, sys, tempfile, unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from test_agentctl import A  # noqa: E402

SEQUENCES = int(os.environ.get("AGENTCTL_TEST_MACHINE_SEQUENCES") or 120)
STEPS = 30


def legal(typ, frm, to):
    spec = A.MACHINES[typ][frm]
    if spec.terminal:
        return to == frm
    return to == frm or to in spec.exits


class TransitionPropertyTest(unittest.TestCase):
    def store(self):
        t = [1000.0]
        return A.Store(path=os.path.join(tempfile.mkdtemp(prefix="agentctl-props-"), "store.db"), clock=lambda: t[0]), t

    def test_random_event_sequences_take_exactly_the_listed_moves(self):
        st, clock = self.store()
        for typ, states in sorted(A.MACHINES.items()):
            names = sorted(states)
            for n in range(SEQUENCES // len(A.MACHINES) + 1):
                rng = random.Random(f"{typ}-{n}")
                start = rng.choice([s for s in names if not states[s].terminal])
                iid = f"{typ}-{n}"
                st.put_item(typ, iid, start)
                for step in range(STEPS):
                    cur = st.item(typ, iid)
                    to = rng.choice(names)
                    clock[0] += rng.choice((1, 60, 3600))
                    ok = legal(typ, cur["state"], to)
                    try:
                        v = st.transition(typ, iid, to, cur["version"], event={"cause": "agent", "text": f"step {step}"})
                    except A.TerminalState:
                        self.assertTrue(states[cur["state"]].terminal, (typ, cur["state"], to))
                        self.assertFalse(ok)
                        continue
                    except A.IllegalTransition:
                        self.assertFalse(ok, f"{typ}: {cur['state']} -> {to} is listed, yet refused")
                        after = st.item(typ, iid)
                        self.assertEqual((after["state"], after["version"]), (cur["state"], cur["version"]),
                                         "a refused move changed the item")
                        evs = [e for e in st.item_events(typ, iid) if e["kind"] == f"{typ}.illegal"
                               and (e["from_state"], e["to_state"]) == (cur["state"], to)]
                        self.assertEqual(len(evs), 1, "a refused move is recorded once")
                        continue
                    self.assertTrue(ok, f"{typ}: {cur['state']} -> {to} is not listed, yet taken")
                    self.assertEqual(v, cur["version"] + 1)
                    self.assertEqual(st.item(typ, iid)["state"], to)
                    if states[to].terminal:
                        break

    def test_every_deadline_move_is_itself_a_legal_move(self):
        for typ, states in A.MACHINES.items():
            for name, spec in states.items():
                if spec.terminal:
                    continue
                self.assertTrue(legal(typ, name, spec.on_deadline),
                                f"{typ}.{name}: its deadline moves it to {spec.on_deadline}, which it may not enter")

    def test_no_reachable_state_is_stuck(self):
        """From every state a random walk reaches, the deadline-only path ends terminal or escalates forever."""
        for typ, states in A.MACHINES.items():
            seen = set()
            for n in range(SEQUENCES):
                rng = random.Random(f"walk-{typ}-{n}")
                s = rng.choice(sorted(states))
                for _ in range(STEPS):
                    seen.add(s)
                    spec = states[s]
                    if spec.terminal:
                        break
                    s = rng.choice(list(spec.exits) + [spec.on_deadline])
            for s in seen:
                terminal, escalates = A.deadline_walk(typ, s)
                self.assertTrue(terminal or escalates, f"{typ}.{s} is reachable and can stall in silence")


class HoldTableTest(unittest.TestCase):
    def test_every_hold_kind_has_a_policy_a_bound_and_a_recovery(self):
        for kind, h in A.HOLDS.items():
            self.assertEqual(kind, h.kind)
            self.assertIn(h.policy, A.POLICIES, f"hold {kind}: its policy is not registered")
            self.assertTrue(0 < h.bound < float("inf"), kind)
            self.assertIn(h.recovery, ("rederive", "escalate", "requeue", "recover"), kind)

    def test_a_red_fix_waits_only_on_its_own_resources(self):
        """A FIX OF ANY OPEN RED GOES FIRST: the holds a red fix may take are resource waits, never other work."""
        for kind, h in A.HOLDS.items():
            if h.red_fix_ok:
                self.assertTrue(A.POLICIES[h.policy].mechanism, f"{kind}: a red fix may wait on a rule ({h.policy})")

    def test_an_unknown_hold_kind_is_refused(self):
        with self.assertRaises(ValueError):
            A.hold_kind("nonsense")


if __name__ == "__main__":
    unittest.main()
