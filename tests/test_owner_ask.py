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


class StaleBlockTest(AskBase):
    """2026-11-07: Verify scrape recording #14's reds read "BLOCKED ON OWNER: ... re-pin movies runner scheduler to
    362196b15a" hours after the owner had re-pinned past it: a turn's verdict with no block lease, so no probe could
    ever clear it. It reads with its age and how to strike it, and `red unblock` strikes it or ends a live block."""

    def stale(self, key="vsr"):
        h = {"at": A.iso_at(self.t[0]), "holder": "laptop:owner:convergence",
             "outcome": "BLOCKED ON OWNER: `cd ~/bitcashier/infra && ... re-pin movies runner scheduler to 362196b15a`"}
        it = self.red(key=key, workflow="Verify scrape recording", history=[h])
        self.t[0] += 4 * 3600 + 60
        return A.red_move(self.st, it, "fix-pending", "fix landed", data={"fix": {"status": "landed", "sha": "709e77cd8"}})

    def note(self, key):
        return next(r["note"] for r in A.status_reds(self.st, self.t[0]) if r["key"] == key)

    def test_a_blocked_verdict_no_block_holds_reads_with_its_age_and_unblock_strikes_it(self):
        self.stale()
        note = self.note("vsr")
        self.assertIn("re-pin movies runner scheduler", note)
        self.assertIn("said 4 h ago, no block holds it", note)
        self.assertIn('agentctl red unblock vsr --why', note)
        self.assertEqual(A.red_unblock(self.st, O_R, "vsr", "")[0], 2, "a reason is required")
        rc, lines = A.red_unblock(self.st, O_R, "vsr", "re-pinned; ci-runner runs scheduler 36ae3557f4", who="me")
        self.assertEqual(rc, 0, lines)
        self.assertIn("its BLOCKED ON OWNER verdict struck", lines[0])
        self.assertEqual(self.note("vsr"), "UNBLOCKED: re-pinned; ci-runner runs scheduler 36ae3557f4")
        it = self.st.item("red", "vsr")
        self.assertEqual(it["state"], "fix-pending", "its fix still waits for its run")
        rc, lines = A.red_unblock(self.st, O_R, "vsr", "again", who="me")
        self.assertIn("not blocked on the owner", lines[0])
        self.assertEqual(len(self.st.item("red", "vsr")["data"]["history"]), 2)

    def test_unblock_ends_a_live_block_lease_released_never_escalated(self):
        it = self.owner_cause_blocked()
        self.assertIsNotNone(A.red_block_of(it, self.st))
        self.assertIsNone(A.block_verdict_note(it, self.t[0], self.st))  # a real block speaks for itself
        rc, lines = A.red_unblock(self.st, O_R, "nix", "the owner granted it", who="me")
        self.assertEqual(rc, 0, lines)
        self.assertIsNone(A.red_block_of(self.st.item("red", "nix"), self.st))
        self.assertTrue(str(A.lease_meta(self.st.get(A.block_lease("nix"))).get("outcome")).startswith("RELEASED"))


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

    def test_decide_accepts_the_answer_for_an_open_red_in_any_state(self):
        # Main #2128: the question was asked for #2125; a newer run moved the red to "offered: deep" and the owner's
        # answer was refused ("is not waiting on you") and the question lost
        it = self.decision_blocked()
        A.red_ask_mark(self.st, it, self.t[0])
        self.st.revoke("block/k", "PROBE PASSED: a newer run did not attach")
        A.red_offer(self.st, self.st.item("red", it["id"]), "its block ended: offered again", data={"cause": None, "block": None})
        self.assertIsNone(A.red_block_of(self.st.item("red", it["id"]), self.st))
        rc, out = A.red_decide(self.st, O_R, "k", "baseline them")
        self.assertEqual(rc, 0, out)
        d = self.st.item("red", it["id"])["data"]
        self.assertEqual(d["decision"]["status"], "in-progress")
        self.assertEqual(d["decision"]["answer"], "baseline them")
        self.assertTrue(d["decision"]["question"], "the question is kept with the answer")
        self.assertIn("IN PROGRESS (decided: baseline them)", self.view(self.st.item("red", it["id"]))[1])

    def test_decide_on_a_red_closed_since_says_it_is_resolved_and_acks_its_alert(self):
        """The owner, 2026-11-06: `decide 971723aec893 "yes: fix the stuck limit as proposed"` answered "names no open
        red" -- its red had cleared days before, and its WAITING ON YOU alert stayed unread."""
        it = self.decision_blocked()
        line = 'RED BLOCKED ON OWNER: WAITING ON YOU: x -- Answer: `! ~/bin/agentctl decide k "<your answer>"`'
        eid = A.alert_write({"id": "20261105T000000000000-1-red", "at": self.t[0], "command": "red", "cwd": "/",
                             "final": True, "exit": 1, "verdict": line, "kind": "BLOCKED", "verdicts": [line],
                             "tail": [], "audience": "owner", "item": "run:x -- Main #1"})
        A.red_move(self.st, self.st.item("red", it["id"]), "cleared", "RED CLEARED: a newer run is green")
        rc, out = A.red_decide(self.st, O_R, "k", "yes")
        self.assertEqual(rc, 0, out)
        self.assertIn("already resolved (red closed", out[0])
        self.assertTrue(A.alert_acked(A.inbox_dir(), eid), "its alert acknowledged")
        self.assertIn("already resolved", A.read_record(os.path.join(A.inbox_dir(), eid + ".json"))["closed"]["why"])

    def test_decide_retries_a_stale_version_never_a_traceback(self):
        it = self.decision_blocked()
        real, hits = self.st.update_item, [0]

        def racing(typ, iid, version, upd, *a, **kw):
            if typ == "red" and hits[0] < 2:  # the ticking store moves the red under the decide, twice
                hits[0] += 1
                cur = self.st.item("red", iid)
                real(typ, iid, cur["version"], {"touched": hits[0]})
            return real(typ, iid, version, upd, *a, **kw)

        self.st.update_item = racing
        try:
            rc, out = A.red_decide(self.st, O_R, "k", "go")
        finally:
            self.st.update_item = real
        self.assertEqual(rc, 0, out)
        self.assertEqual(self.st.item("red", it["id"])["data"]["decision"]["answer"], "go")

    def test_a_newer_run_failing_the_same_way_is_attached_despite_a_stale_version(self):
        # the lost attach read as "no longer this failure" to the block's probe and discarded the open question
        it = self.decision_blocked()
        real, hits = self.st.update_item, [0]

        def racing(typ, iid, version, upd, *a, **kw):
            if hits[0] < 1:
                hits[0] += 1
                cur = self.st.item("red", iid)
                real(typ, iid, cur["version"], {"touched": 1})
            return real(typ, iid, version, upd, *a, **kw)

        ob = {"names": ["2128"], "jobs": ["takes-drift-1 (pl)"], "run": {"number": 2128, "databaseId": 9999}}
        self.st.update_item = racing
        try:
            got = A.red_attach_obs(self.st, self.st.item("red", it["id"]), ob)
        finally:
            self.st.update_item = real
        self.assertEqual(got["data"]["last_run"]["number"], 2128)
        self.assertEqual(got["state"], "blocked-external")

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
