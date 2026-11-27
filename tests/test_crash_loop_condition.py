"""THE FIXER CRASH LOOP AS A CONDITION (crashloop.py; the transition analysis' item 6: Main #2877, Alert backtest #26 and
Tool self-tests #533 each launched three fixers that died at launch, and the loop was only the red's block and an
agentctl issue). N launches of one subject's fixer dying within M s of one cause open ONE CRASH_LOOP condition, routed
by the escalation table (owner-only for an owner cause, else an Opus agent briefed with the launches); while it is
open that fixer is not launched again; it closes when the subject clears, a launch lives, or a fix is deployed."""
import os, sys, tempfile, unittest
from functools import partial

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from test_agentctl import A  # noqa: E402
from harness import case_context  # noqa: E402

VAULT = "ended with no LANDED / GAVE UP (session ab12cd34: blocked)"
VAULT_NOTES = "waiting for Touch ID: secrets unlock movies"
OVERLOADED = "claude: model overloaded"


class CrashLoopConditionBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.t = [1_800_000_000.0]
        keys = ("AGENTCTL_STATE_ROOT", "AGENTCTL_INBOX_DIR", "AGENTCTL_RED_CRASH_LOOP", "AGENTCTL_RECOVERY_DIR",
                "AGENTCTL_CRASH_LOOP_LAUNCHES", "AGENTCTL_CRASH_LOOP_QUICK")
        self.saved_env = {k: os.environ.get(k) for k in keys}
        for k in keys:
            os.environ.pop(k, None)
        os.environ["AGENTCTL_STATE_ROOT"] = os.path.join(self.tmp.name, "state")
        os.environ["AGENTCTL_INBOX_DIR"] = os.path.join(self.tmp.name, "inbox")
        os.environ["AGENTCTL_RECOVERY_DIR"] = os.path.join(self.tmp.name, "rec")
        os.environ["AGENTCTL_RED_CRASH_LOOP"] = "99"  # the red ladder's own breaker out of the way: the condition alone
        self.st = A.Store(path=os.path.join(self.tmp.name, "store.db"), clock=lambda: self.t[0])
        self.code = ["c0de0001"]
        self.n = 0

    def tearDown(self):
        for k, v in self.saved_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        self.tmp.cleanup()

    def red(self, key="k"):
        return self.st.put_item("red", key, "leased", {
            "key": key, "workflow": "Main", "job": "ci / test (worker)", "kind": "test", "line": "x", "gate": True,
            "red_kind": "gate", "runs": ["2877"], "attempts": 0, "history": [], "tier": "fresh",
            "last_run": {"number": 2877, "databaseId": 92877}})

    def launch(self, it, notes=OVERLOADED, outcome=VAULT, seconds=30):
        """One dispatched fixer on the red that ended after `seconds`: the red tick's red_lease_ended."""
        self.n += 1
        key = it["data"]["key"]
        took = self.st.acquire(A.red_lease(key), f"laptop:fixer:red-{key}-{self.n}", A.RED_FIXER_TTL, A.RED_FIXER_MAX,
                               meta={"kind": "fixer", "worktree": f"/w/red-{self.n}"})
        self.t[0] += seconds
        self.st.release(A.red_lease(key), took["epoch"], took["secret"], outcome=outcome, notes=notes)
        it = self.st.item("red", it["id"])
        if it["state"] != "leased":
            it = A.red_move(self.st, it, "leased", "taken again")
        A.red_lease_ended(self.st, it, self.st.get(A.red_lease(key)), None, self.t[0], ctx=case_context())
        self.t[0] += 60
        return self.st.item("red", it["id"])

    def step(self):
        check = partial(A.probe_fixer_crash_loop, store=self.st, code=lambda: self.code[0])
        return A.crash_loop_step(self.st, check)

    def loops(self):
        return [it for it in self.st.items("condition", ["open"]) if it["data"]["kind"] == "CRASH_LOOP"]

    def probe_now(self, cond):
        a = cond["data"]["probe"]["args"]
        return A.probe_fixer_crash_loop(a, store=self.st, code=lambda: self.code[0])


class OpensTest(CrashLoopConditionBase):
    def test_three_quick_launches_of_one_cause_open_one_crash_loop_routed_to_an_opus_agent_with_the_evidence(self):
        it = self.red()
        for _ in range(3):
            it = self.launch(it)
        self.assertEqual(len(self.step()), 1)
        [c] = self.loops()
        self.assertEqual(c["data"]["key"], "crash-loop-red-k")
        self.assertIn("3 fixer launches in a row died within 120 s", c["data"]["text"])
        route = A.escalation_route(c["data"])
        self.assertEqual((route.mode, route.model), (A.TO_AGENT, "opus"))
        self.assertTrue(any("lived 30 s" in x for x in c["data"]["evidence"]), c["data"]["evidence"])
        self.assertEqual(self.step(), [])  # one condition, not one a tick
        self.assertEqual(len(self.loops()), 1)

    def test_an_owner_only_cause_is_the_owners(self):
        it = self.red()
        for _ in range(3):
            it = self.launch(it, notes=VAULT_NOTES)
        self.step()
        [c] = self.loops()
        self.assertEqual(A.escalation_route(c["data"]).mode, A.TO_OWNER)
        self.assertIn("WAITING ON YOU", c["data"]["text"])

    def test_mixed_causes_or_a_launch_that_lived_open_nothing(self):
        it = self.red("mixed")
        for notes in ("claude: model overloaded", "claude: disk full", "claude: bad config"):
            it = self.launch(it, notes=notes)
        it = self.red("lived")
        for seconds in (30, 30 * 60, 30):
            it = self.launch(it, seconds=seconds)
        self.assertEqual(self.step(), [])
        self.assertEqual(self.loops(), [])

    def test_an_escalation_whose_fixer_never_starts_is_a_crash_loop_of_its_condition(self):
        st = self.st
        c = st.put_item("condition", "invariant-W2", "open", {
            "key": "invariant-W2", "kind": "INVARIANT", "text": "INVARIANT W2 violated", "opened_at": self.t[0],
            "escalation": {"tries": [{"try": n, "started": self.t[0] + n * 600, "ended": self.t[0] + n * 600 + 2,
                                      "outcome": "NOT STARTED: `claude --bg` exit 1: no such option --bg"}
                                     for n in (1, 2, 3)], "state": "offered"}})
        self.t[0] += 3600
        self.step()
        [loop] = self.loops()
        self.assertEqual(loop["data"]["subject"], f"condition:{c['id']}")
        self.assertIn("no such option", loop["data"]["text"])


class HoldTest(CrashLoopConditionBase):
    def crash_loop(self):
        it = self.red()
        for _ in range(3):
            it = self.launch(it)
        self.step()
        return it

    def test_while_open_neither_dispatcher_launches_that_fixer(self):
        self.crash_loop()
        opened = self.st.items("condition", ["open"])
        self.assertIn("crash-loops at launch", A.crash_loop_hold(opened, "red:k"))
        self.assertIsNone(A.crash_loop_hold(opened, "red:other"))
        why = A.offer_waits_why(self.st, self.t[0], {"item": "red:k", "tier": "fresh"}, {"gate": True}, {"data": {}}, None)
        self.assertIn("crash-loops at launch", why)
        off = {"item": "red:k", "tier": "CRASH_LOOP", "picture": {}}
        self.assertIn("crash-loops at launch", A.offer_waits(self.st, self.t[0], None, off, A.DISPATCH_TABLE["CRASH_LOOP"],
                                                             None, opened))

    def test_the_escalation_brief_carries_the_launches(self):
        self.crash_loop()
        [c] = self.loops()
        brief = A.escalation_brief(self.st, A.escalation_route(c["data"]), "CRASH_LOOP", c, "/repo")
        self.assertIn("ITS EVIDENCE:", brief)
        self.assertIn("lived 30 s", brief)


class ClosesTest(CrashLoopConditionBase):
    def test_a_launch_that_lives_closes_it(self):
        it = self.red()
        for _ in range(3):
            it = self.launch(it)
        self.step()
        [c] = self.loops()
        self.assertFalse(self.probe_now(c)[0])
        self.launch(it, seconds=40 * 60)
        self.assertTrue(self.probe_now(c)[0])

    def test_the_red_clearing_closes_it(self):
        it = self.red()
        for _ in range(3):
            it = self.launch(it)
        self.step()
        [c] = self.loops()
        it = self.st.item("red", "k")
        self.st.transition("red", "k", "cleared", it["version"], event={"cause": "ci-verdict", "text": "green"})
        self.assertTrue(self.probe_now(c)[0])

    def test_a_deployed_fix_lets_one_launch_through_and_the_same_death_opens_it_again(self):
        it = self.red()
        for _ in range(3):
            it = self.launch(it)
        self.step()
        [c] = self.loops()
        self.code[0] = "c0de0002"
        ok, why = self.probe_now(c)
        self.assertTrue(ok, why)
        self.assertIn("code changed", why)
        self.st.transition("condition", c["id"], "closed", c["version"], event={"cause": "probe", "text": "closed"})
        self.assertEqual(self.step(), [])  # no new launch since: not opened again before the one launch it let through
        self.launch(it)
        self.assertEqual(len(self.step()), 1)


if __name__ == "__main__":
    unittest.main()
