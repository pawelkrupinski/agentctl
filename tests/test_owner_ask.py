"""WAITING ON YOU (the owner, 2026-11-03: "make it so blocked always communicates with me and reminds me of the
decision"). convergence #728 read `BLOCKED: owner-decision down` for 15 h after the owner's decided land had failed;
Nix update #1 sat BLOCKED ON OWNER with no reminder. Each owner-blocked red carries its question (shown, never a bare
cause), the owner is told once and reminded at a bounded, backing-off interval, `agentctl decide` records the answer
and re-dispatches with it, and a failed decided action comes back to the owner with its failure."""
import glob, json, os, sys, unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from test_agentctl import A  # noqa: E402
from test_red_crash_loop import CrashLoopBase  # noqa: E402

DIAG = "DIAGNOSED: the 2 Titans flips are resolver design; decide: baseline the 2 Titans flips, or change the resolver"
O_R = "pawelkrupinski/movies"


class AskBase(CrashLoopBase):
    def setUp(self):
        super().setUp()
        for k in ("AGENTCTL_ASK_REMIND_FIRST", "AGENTCTL_ASK_REMIND_MAX"):
            self.saved_env[k] = os.environ.pop(k, None)

    def decision_blocked(self, key="k"):
        it, _acts = self.launch(self.red(key=key), outcome=DIAG)
        self.assertEqual(it["state"], "blocked-external")
        return it

    def owner_cause_blocked(self, key="nix"):
        """Nix update #1's shape: a fixer's `red block --owner-cause ... --cmd` (the block lease), the red then moved."""
        it = self.red(key=key, workflow="Nix update", red_kind="outside", kind="infra",
                      last_run={"number": 1, "databaseId": 501})
        self.st.acquire(A.block_lease(key), "owner:dashboard", A.BLOCK_TTL, A.BLOCK_MAX, progress="p",
                        meta={"kind": "block", "owner_cause": "dashboard", "cmd": "open github.com/apps/sync-bot and "
                              "grant it movies-gitops", "keys": [key], "until": "Nix update is green",
                              "text": "BLOCKED ON OWNER: ..."})
        return A.red_move(self.st, it, "blocked-external", "blocked", data={"block": A.block_lease(key)})

    def events(self, kinds=None):
        return [e for e in self.st.events(after=0, limit=5000)
                if e["severity"] == "escalate" and (kinds is None or e["kind"] in kinds)]

    def remind(self, minutes):
        """`minutes` of ticks: the blocks renewed as the tick's red_renew_blocks does (their probe still failing), the
        reminders run once at the end."""
        left = minutes * 60
        while True:
            for row in self.st.list("block/"):
                if A.lease_is_live(row, self.t[0]):
                    self.st.renew(row["name"], row["epoch"], row["secret"], f"held@{self.t[0]}", A.BLOCK_TTL)
            step = min(left, 300)
            self.t[0] += step
            left -= step
            if left <= 0:
                return A.red_owner_reminders(self.st, self.t[0])

    def view(self, it):
        return A.red_view(self.st, self.st.item("red", it["id"]), self.t[0])


class QuestionShownTest(AskBase):
    def test_a_decision_block_reads_waiting_on_you_with_its_question_never_owner_decision_down(self):
        text, short = self.view(self.decision_blocked())
        self.assertTrue(short.startswith("WAITING ON YOU: baseline the 2 Titans flips, or change the resolver -- answer:"), short)
        self.assertNotIn("owner-decision down", short)
        self.assertIn('agentctl decide k "<your answer>"', text)

    def test_an_owner_command_block_reads_waiting_on_you_with_the_exact_command(self):
        _text, short = self.view(self.owner_cause_blocked())
        self.assertTrue(short.startswith("WAITING ON YOU: run `! open github.com/apps/sync-bot and grant it"), short)

    def test_ci_why_rows_carry_the_question(self):
        it = self.decision_blocked()
        blk = A.red_block_of(it, self.st)
        self.assertEqual(A.red_ask(it, blk)["q"], "baseline the 2 Titans flips, or change the resolver")

    def test_a_spent_quota_is_no_question_for_the_owner(self):
        it = self.red(key="q")
        blk = {"name": "block/q", "meta": {"wait_cause": "quota", "until": "the reset"}}
        self.assertIsNone(A.red_ask(it, blk))


class LongQuestionTest(AskBase):
    Q = " ".join(f"word{i}" for i in range(120))[:600]

    def test_a_600_char_question_reaches_the_owner_whole_and_the_short_line_cuts_at_a_word(self):
        it = self.decision_blocked()
        blk = self.st.item("red", it["id"])
        d = blk["data"]
        ask = {"q": self.Q, "how": "answer: `! agentctl decide k \"x\"`", "until": None}
        self.assertIn(self.Q, A.red_ask_line(d, ask))
        self.assertEqual(A.trim_words(self.Q).split(" ")[-1][-1], "\u2026")
        self.assertIn(A.trim_words(self.Q)[:-1].split(" ")[-1], self.Q.split(" "))


class ReminderTest(AskBase):
    def test_told_once_then_reminded_at_1h_2h_4h_4h_and_never_per_tick(self):
        self.decision_blocked()
        told = self.events()
        self.assertEqual(len(told), 1, "the block's own escalation is the one telling")
        self.assertIn("WAITING ON YOU: baseline the 2 Titans flips", told[0]["refs"]["verdict"])
        self.assertEqual(self.remind(1), [], "no second telling on the next tick")
        self.assertEqual(self.remind(58), [])
        gaps = []
        for _ in range(4):
            mins = 0
            while not self.remind(5):
                mins += 5
            gaps.append(mins + 5)
        # the first reminder comes 60 min after the telling (the 59 min above counted), then 2 h, 4 h, 4 h
        self.assertEqual(gaps, [5, 120, 240, 240])
        rem = self.events({"red.remind"})
        self.assertEqual(len(rem), 4)
        self.assertTrue(all(e["refs"].get("no_inbox") for e in rem))
        self.assertTrue(rem[0]["refs"]["verdict"].startswith("RED BLOCKED ON OWNER: REMINDER 1: WAITING ON YOU:"))

    def test_the_projector_writes_one_inbox_entry_and_the_notifier_shows_every_reminder(self):
        self.owner_cause_blocked()
        shown = []
        saved = A.notify
        A.notify = lambda title, text, **kw: shown.append(text) or True
        try:
            self.assertEqual(len(self.remind(0)), 1, "an owner-command block is told by the tick")
            A.project_events(self.st)
            A.notify_escalations(self.st)
            self.assertEqual(self.remind(1), [])
            self.remind(61)
            A.project_events(self.st)
            A.notify_escalations(self.st)
        finally:
            A.notify = saved
        entries = [json.load(open(p)) for p in glob.glob(os.path.join(os.environ["AGENTCTL_INBOX_DIR"], "*.json"))]
        self.assertEqual([e["kind"] for e in entries], ["BLOCKED"], entries)
        self.assertIn("WAITING ON YOU", entries[0]["verdict"])
        self.assertEqual(len(shown), 2, shown)  # the telling and one reminder
        self.assertIn("REMINDER 1", shown[1])

    def test_a_decision_block_holds_a_week_not_a_day(self):
        # its 24 h lapse re-offered it to fixers who could not decide it: the RED ESCALATE / GAVE UP spam
        it = self.decision_blocked()
        row = self.st.get(A.red_block_of(it, self.st)["name"])
        self.assertGreaterEqual(A.RED_DECISION_BLOCK_MAX, 7 * 86400)
        self.assertIn("in 7 d", A.lease_meta(row)["text"])


class DecideTest(AskBase):
    def test_decide_stores_the_answer_lifts_the_block_and_reads_in_progress(self):
        it = self.decision_blocked()
        rc, out = A.red_decide(self.st, O_R, "k", "baseline the 2 Titans flips")
        self.assertEqual(rc, 0, out)
        self.assertIn("IN PROGRESS", out[0])
        it = self.st.item("red", it["id"])
        self.assertEqual(it["data"]["decision"]["status"], "in-progress")
        self.assertIsNone(A.red_block_of(it, self.st))
        self.assertEqual(A.red_owner_reminders(self.st, self.t[0]), [], "answered: no more reminders")
        it = A.red_offer(self.st, it, "offered again", escalate=False)
        _text, short = self.view(it)
        self.assertTrue(short.startswith("IN PROGRESS (decided: baseline the 2 Titans flips)"), short)
        self.assertEqual(A.red_picture(it["data"])["decision"]["answer"], "baseline the 2 Titans flips")

    def test_the_fixer_is_briefed_with_the_answer(self):
        u = {"workflow": "Identity model convergence", "job": "j", "kind": "test", "line": "drift",
             "run": {"number": 728, "databaseId": 9728}, "decision": {"question": "baseline?", "answer": "yes, both"}}
        prompt = A.fixer_prompt(u, "/w", "the brief")
        self.assertIn("THE OWNER DECIDED this red", prompt)
        self.assertIn("'yes, both'", prompt)

    def test_decide_refuses_a_red_not_waiting_on_the_owner(self):
        self.red(key="free")
        rc, out = A.red_decide(self.st, O_R, "free", "x")
        self.assertEqual(rc, 1)
        self.assertIn("is not waiting on you", out[0])

    def test_a_failed_decided_action_comes_back_to_the_owner_with_its_failure(self):
        # convergence #728: decided, the land NOT LANDED on its pre-push checks -- and nobody told the owner for 15 h
        it = self.decision_blocked()
        A.red_decide(self.st, O_R, "k", "baseline the 2 Titans flips")
        it = A.red_offer(self.st, self.st.item("red", it["id"]), "offered again")
        n0 = len(self.events())
        it, _acts = self.launch(it, outcome="NOT LANDED: pre-push check identity-drift failed")
        self.assertEqual(it["state"], "blocked-external")
        _text, short = self.view(it)
        self.assertTrue(short.startswith("WAITING ON YOU: you decided 'baseline the 2 Titans flips'; the action "
                                         "failed: NOT LANDED: pre-push check identity-drift failed"), short)
        self.assertEqual(it["data"]["decision"]["status"], "failed")
        self.assertEqual(len(self.events()) - n0, 1, "told once, at once")
        self.assertEqual(self.remind(1), [])

    def test_a_landed_decided_action_is_no_failure(self):
        it = self.decision_blocked()
        A.red_decide(self.st, O_R, "k", "baseline")
        it = A.red_offer(self.st, self.st.item("red", it["id"]), "offered again")
        it, _acts = self.launch(it, outcome="LANDED abc1234def")
        self.assertNotEqual(it["state"], "blocked-external")

    def test_no_red_escalate_while_the_owners_decision_is_acted_on(self):
        it = self.red(key="deep", tier="deep", attempts=50)
        self.st.update_item("red", it["id"], it["version"], {"decision": {"answer": "a", "status": "in-progress"}})
        n0 = len(self.events())
        A.red_offer(self.st, self.st.item("red", it["id"]), "re-offered")
        self.assertEqual(len(self.events()) - n0, 0)


class SelfRecoveryTest(AskBase):
    def test_the_blocks_probe_passing_ends_the_question_and_its_reminders(self):
        it = self.decision_blocked()
        row = self.st.get(A.red_block_of(it, self.st)["name"])
        self.st.revoke(row["name"], "PROBE PASSED: Identity model convergence #2400 green")
        self.assertEqual(self.remind(120), [])
        self.assertNotIn("WAITING ON YOU", self.view(it)[1])


if __name__ == "__main__":
    unittest.main()
