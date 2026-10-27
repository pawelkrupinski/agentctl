"""The incident of 2026-10-27: gate red Main #2146 sat at `fixing: NOBODY (daily dispatch budget 6 spent)` for 85+ min.
4 of the day's 6 dispatches did nothing (2 `GAVE UP: already fixed on main`, a duplicate, a `BLOCKED ON OWNER` for a CI
read); the #2146 fixer ended `DIAGNOSED: a one-off Chrome startup crash` and released its claim; the RED UNCLAIMED entry
woke nobody. Gate reds are never budget-starved, wasted dispatches are refunded, a diagnosis keeps the claim and
dispatches its implementer, and unacked escalations notify past the orchestrator."""
import glob, json, os, subprocess, sys, time, unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from test_agentctl import A, TOOL  # noqa: E402
from test_red import RedBase, MOVIES  # noqa: E402


def fixer(tmp, run, verdict=None, gate=False, ended=True, alive=False, started=None, **kw):
    d = os.path.join(tmp, "reds", str(run))
    os.makedirs(d, exist_ok=True)
    rec = {"sig": f"s{run}", "run": run, "workflow": "W", "job": "j", "gate": gate, "started": started or A.ts(),
           "pid": os.getpid() if alive else 1 << 22, **kw}
    if ended:
        rec["ended"] = A.ts()
    if verdict:
        rec["verdict"] = verdict
    A.write_json(os.path.join(d, "fixer.json"), rec)
    return rec


class RefundTest(RedBase):
    def test_the_days_wasted_dispatches_are_refunded_and_gate_reds_do_not_count(self):
        t = self.tmp.name
        # the live records of 2026-10-27, verbatim
        fixer(t, 2050, "GAVE UP: nothing to fix — 03d646e63 (`Fixes-Red: 2049`, regression test included) already fixes "
                       "this on main; #2050 tested the older commit 73a6b8069.")
        fixer(t, 2076, "GAVE UP: no fix needed — red #2076 was a deterministic bug that `e2759b3e6`, already on main, "
                       "fixes, and run 3224 passes de/es/pl with it.")
        fixer(t, 2104, "BLOCKED ON OWNER: secrets unlock movies && ~/bitcashier/infra/bin/forgejo-gh run download 3252 "
                       "-n identity-capture-uk -R pawelkrupinski/movies -D /tmp/capture-u")
        fixer(t, "local", None, gate=True, ended=False)  # killed: no verdict, never ended
        fixer(t, 2146, "DIAGNOSED: a one-off Chrome startup crash (exit 133 / SIGTRAP)", gate=True)
        recs = {str(r["run"]): r for r in A.fixer_records()}
        self.assertEqual(A.dispatch_refund(recs["2050"]), "nothing to fix")
        self.assertEqual(A.dispatch_refund(recs["2076"]), "nothing to fix")
        self.assertIn("CI read", A.dispatch_refund(recs["2104"]))
        self.assertEqual(A.dispatch_refund(recs["local"]), "killed before a verdict")
        self.assertIsNone(A.dispatch_refund(recs["2146"]))
        self.assertEqual(A.dispatches_today(), 0)  # the 3 outside the gate refunded; gate reds never counted
        self.assertEqual(A.dispatches_today(gate=True), 1)

    def test_useful_or_real_endings_are_not_refunded(self):
        t = self.tmp.name
        fixer(t, 1, "LANDED abcdef1234")
        fixer(t, 2, "GAVE UP: FooSpec needs the owner's fixture")
        fixer(t, 3, "BLOCKED ON OWNER: bin/deploy ci-runner --apply")
        fixer(t, 4, "TIMED OUT after 180 min (maximum wall time)")
        fixer(t, 5, None, ended=False, alive=True)  # running
        for r in A.fixer_records():
            self.assertIsNone(A.dispatch_refund(r), r)
        self.assertEqual(A.dispatches_today(), 5)

    def test_superseded_and_duplicates_are_refunded(self):
        t = self.tmp.name
        fixer(t, 1, "STOPPED: STOP: fixed by another change")
        fixer(t, 2, "red cleared: Local checks pass on main")
        fixer(t, 3, "DUPLICATE: session 974d50a6 (~/projects/x) is on it")
        self.assertEqual(A.dispatches_today(), 0)

    def test_an_outside_red_past_the_daily_budget_is_one_red_budget_entry_that_escalates(self):
        fixer(self.tmp.name, 9, "LANDED abcdef1234")
        u = dict(self.units()[0], gate=False)
        rec = json.load(open(A.sig_path(u["sig"])))
        os.environ["AGENTCTL_RED_DAILY_CAP"] = "1"
        try:
            t = time.time()
            msg = A.maybe_dispatch(MOVIES, self.r.path, u, rec, t, why="x", group=[u])
            again = A.maybe_dispatch(MOVIES, self.r.path, u, rec, t, why="x", group=[u])
        finally:
            os.environ.pop("AGENTCTL_RED_DAILY_CAP")
        self.assertIn("SKIPPED (daily dispatch budget 1 spent)", msg)
        self.assertIsNone(again)
        budget = self.inbox("RED_BUDGET")
        self.assertEqual(len(budget), 1)
        self.assertTrue(budget[0]["verdict"].startswith("RED BUDGET: daily dispatch budget 1 spent (refunds excluded); "
                                                        "Main #20 [test (rest)] (outside the gate) not dispatched"))
        self.assertIn("RED_BUDGET", A.WAIT_KINDS_DEFAULT.split(","))
        self.assertEqual(A.inbox_escalations(t + 5 * 60), [])
        self.assertEqual(len(A.inbox_escalations(t + 11 * 60)), 1)

    def test_a_refunded_dispatch_frees_its_budget(self):
        fixer(self.tmp.name, 9, "GAVE UP: already fixed on main by 03d646e63")
        u = dict(self.units()[0], gate=False)
        rec = json.load(open(A.sig_path(u["sig"])))
        os.environ["AGENTCTL_RED_DAILY_CAP"] = "1"
        try:
            msg = A.maybe_dispatch(MOVIES, self.r.path, u, rec, time.time(), why="x", group=[u])
        finally:
            os.environ.pop("AGENTCTL_RED_DAILY_CAP")
        self.assertIn(" DISPATCHED pid", msg)
        self.wait_fixer()


class DuplicateTest(RedBase):
    def test_a_fixer_starting_on_a_red_someone_claimed_steps_aside_refunded(self):
        os.environ["AGENTCTL_CLAIM_PID"] = str(os.getpid())
        os.environ["AGENTCTL_AGENT_NAME"] = "agent-x"
        try:
            self.tool("red", "claim", "20", "--repo", "movies", cwd=self.r.path)
        finally:
            os.environ.pop("AGENTCTL_CLAIM_PID")
            os.environ.pop("AGENTCTL_AGENT_NAME")
        ok, _msg = A.red_dispatch(MOVIES, self.r.path, self.units()[0], why="no live author")  # the race: past the check
        self.assertTrue(ok)
        rec = None
        for _ in range(150):
            rec = A.fixer_records()[0]
            if rec.get("ended"):
                break
            time.sleep(0.2)
        self.assertTrue(rec["verdict"].startswith("DUPLICATE: agent-x"), rec)
        self.assertEqual(A.dispatch_refund(rec), "a duplicate")
        self.assertEqual(glob.glob(os.path.join(self.tmp.name, "claude-out", "*.json")), [])  # no claude ran
        self.assertEqual([c["who"] for c in A.all_claims()], ["agent-x"])  # the claim is untouched
        self.assertEqual(A.dispatches_today(gate=True), 0)


class DiagnosedTest(RedBase):
    DIAG = "DIAGNOSED: a one-off Chrome startup crash (exit 133 / SIGTRAP); durable fix: keep Chrome's stderr in CdpDriver"

    def setUp(self):
        super().setUp()
        self.saved_slug = A.slug
        A.slug = lambda repo: MOVIES
        self.main_red(40, 20, kind="infra")
        os.environ["FAKE_CLAUDE_RESULT"] = self.DIAG

    def tearDown(self):
        A.slug = self.saved_slug
        for k in ("FAKE_CLAUDE_RESULT", "FAKE_CLAUDE_RESULT_IMPLEMENT", "AGENTCTL_RED_GATE_MAX_FIXERS"):
            os.environ.pop(k, None)
        super().tearDown()

    def prompts(self):
        recs = sorted((json.load(open(p)) for p in glob.glob(os.path.join(self.tmp.name, "claude-out", "*.json"))),
                      key=lambda r: "was DIAGNOSED" in " ".join(r["argv"]))
        return [r["argv"][r["argv"].index("-p") + 1] for r in recs]

    def test_a_diagnosis_dispatches_its_implementer_at_once_with_the_diagnosis(self):
        os.environ["FAKE_CLAUDE_RESULT_IMPLEMENT"] = "LANDED abcdef1234"
        acts = self.patrol(time.time())
        self.assertTrue(any("DISPATCHED" in a and "infra-shaped: diagnose" in a for a in acts), acts)
        done = self.wait_fixer(n=2)
        self.assertEqual([e["kind"] for e in done], ["FIXER_FAILED", "FIXER_LANDED"])
        self.assertIn("DIAGNOSED: a one-off Chrome", done[0]["verdict"])
        diag, impl = self.prompts()
        self.assertIn("DIAGNOSE ONLY", diag)
        self.assertIn("an implementer is dispatched with it at once", diag)
        self.assertIn("was DIAGNOSED by a diagnose-only fixer", impl)
        self.assertIn("a one-off Chrome startup crash", impl)
        self.assertIn("the means to diagnose it next time", impl)
        self.assertIn("Fixes-Red: 20", impl)
        sig = json.load(open(A.sig_path(self.units()[0]["sig"])))
        self.assertEqual([d.get("implement") for d in sig["dispatches"]], [False, True])
        self.assertEqual(A.fresh_dispatches(sig), sig["dispatches"][:1])  # one attempt toward RED ESCALATE

    def test_a_diagnosis_under_a_cap_keeps_the_claim_and_the_patrol_dispatches_it_later(self):
        os.environ["FAKE_CLAUDE_RESULT_IMPLEMENT"] = "LANDED abcdef1234"
        os.environ["AGENTCTL_RED_GATE_MAX_FIXERS"] = "0"  # the wrapper's follow-up is capped (the first went by hand)
        A.red_dispatch(MOVIES, self.r.path, self.units()[0], why="by hand")
        self.wait_fixer()
        for _ in range(50):
            cs = A.all_claims()
            if cs and cs[0].get("state") == "diagnosed" and "gate fixer cap" in cs[0].get("note", ""):
                break
            time.sleep(0.2)
        [c] = A.all_claims()
        self.assertEqual(c["state"], "diagnosed")
        self.assertEqual(c["awaiting"], "an implementer")
        self.assertIsNone(A.claim_stale(c, time.time() + 6 * 3600))  # never stale
        out = self.why()[1]
        self.assertIn("fixing: red-fixer-20", out)
        self.assertIn("diagnosed: a one-off Chrome startup crash", out)
        self.assertIn("awaiting an implementer (gate fixer cap 0 reached", out)
        self.assertNotIn("NOBODY", out)
        self.assertIn("main #20 → red-fixer-20 diagnosed, awaiting an implementer", self.why("--summary")[1])
        self.assertEqual(self.inbox("RED_UNCLAIMED"), [])
        os.environ.pop("AGENTCTL_RED_GATE_MAX_FIXERS")
        acts = self.patrol(time.time())
        self.assertTrue(any(" DISPATCHED pid" in a and "diagnosed: a one-off" in a for a in acts), acts)
        self.assertFalse([c for c in A.all_claims() if c.get("state") == "diagnosed"])  # the implementer claims it
        done = self.wait_fixer(n=2)
        self.assertEqual(done[-1]["kind"], "FIXER_LANDED")
        self.assertIn("was DIAGNOSED", self.prompts()[-1])

    def test_an_implementer_that_only_diagnoses_again_escalates_and_keeps_the_claim(self):
        self.patrol(time.time())
        self.wait_fixer(n=2)
        for _ in range(50):
            if self.inbox("RED_ESCALATE"):
                break
            time.sleep(0.2)
        esc = self.inbox("RED_ESCALATE")
        self.assertEqual(len(esc), 1)
        self.assertIn("diagnosed twice, not fixed", esc[0]["verdict"])
        [c] = A.all_claims()
        self.assertEqual(c["state"], "diagnosed")
        self.assertTrue(c["awaiting"].startswith("a person"))
        self.assertNotIn("NOBODY", self.why()[1])
        self.assertEqual([a for a in self.patrol(time.time()) if "DISPATCHED" in a], [])


class EscalationTest(RedBase):
    def setUp(self):
        super().setUp()
        self.notes = os.path.join(self.tmp.name, "notes")
        script = os.path.join(self.tmp.name, "notify.sh")
        with open(script, "w") as f:
            f.write(f'#!/bin/sh\nprintf "%s|%s\\n" "$1" "$2" >> {self.notes}\n')
        os.chmod(script, 0o755)
        os.environ["AGENTCTL_NOTIFY"] = script

    def tearDown(self):
        os.environ["AGENTCTL_NOTIFY"] = "true"
        super().tearDown()

    def notified(self):
        return open(self.notes).read().splitlines() if os.path.exists(self.notes) else []

    def write(self, verdict, age_min):
        eid = A.inbox_write("red", verdict, [verdict], [], 1)
        p = os.path.join(A.inbox_dir(), eid + ".json")
        rec = json.load(open(p))
        rec["at"] -= age_min * 60
        A.write_json(p, rec)
        return eid

    def test_unacked_unclaimed_budget_and_stalled_past_10_min_notify_once_and_lead_the_status_line(self):
        old = self.write("RED UNCLAIMED: Main #2146 [ci]: NOBODY -- daily dispatch budget 6 spent -- `~/bin/agentctl "
                         "red claim 2146`, or assign an agent", 11)
        self.write("RED UNCLAIMED: Main #2147 [ci]: NOBODY -- x", 3)  # too young
        self.write("STALLED: the waiting processor's loop made no cycle in 900 s", 12)
        self.write("RED FIXER Main #2146: GAVE UP: x", 30)  # not an escalation kind
        acked = self.write("RED BUDGET: daily dispatch budget 6 spent (refunds excluded); W #1 [j]", 20)
        self.tool("inbox", acked, "--ack")
        rc, out = self.tool("inbox", "escalated")
        self.assertEqual(rc, 0)
        self.assertTrue(out.startswith("ESCALATED: 2 unacked ("), out)
        self.assertIn("RED UNCLAIMED: Main #2146 [ci]: NOBODY -- daily dispatch budget 6 spent", out)
        self.assertNotIn("assign an agent", out)
        notes = self.notified()
        self.assertEqual(len(notes), 2, notes)
        self.assertTrue(any(n.startswith("agentctl: RED_UNCLAIMED unacked|RED UNCLAIMED: Main #2146") for n in notes), notes)
        self.tool("inbox", "escalated")
        A.escalate_inbox()
        self.assertEqual(len(self.notified()), 2)  # once per entry
        self.tool("inbox", "--unread", "--ack")
        self.assertEqual(self.tool("inbox", "escalated")[1], "")  # acked: the status line is plain again

    def test_the_default_notifier_is_a_notification_never_a_dialog(self):
        os.environ.pop("AGENTCTL_NOTIFY")
        calls, saved = [], (A.subprocess.run, A.sys.platform)
        A.subprocess.run = lambda argv, **kw: calls.append(argv)
        A.sys.platform = "darwin"
        try:
            A.notify("agentctl: RED_UNCLAIMED unacked", 'RED UNCLAIMED: Main #1 "x" \\ y')
        finally:
            A.subprocess.run, A.sys.platform = saved
        self.assertEqual(calls[0][:2], ["osascript", "-e"])
        self.assertTrue(calls[0][2].startswith('display notification "RED UNCLAIMED: Main #1 \\"x\\" \\\\ y" with title '
                                               '"agentctl: RED_UNCLAIMED unacked"'), calls[0][2])
        self.assertNotIn("dialog", calls[0][2])

    def test_the_status_line_shows_the_escalation_in_bold(self):
        import tempfile
        home = tempfile.mkdtemp()
        cache = os.path.join(home, ".cache", "agentctl")
        os.makedirs(os.path.join(cache, "statusline.lock"))
        with open(os.path.join(cache, "statusline.txt"), "w") as f:
            f.write("ESCALATED: 1 unacked (RED UNCLAIMED: Main #2146 [ci]: NOBODY -- x) · gate RED (main #2146 → NOBODY (x))"
                    " · waiting: 0 · inbox: 1 unread\n")
        r = subprocess.run([os.path.join(HERE, "..", "claude-statusline")], input="{}", capture_output=True, text=True,
                           env=dict(os.environ, HOME=home))
        self.assertTrue(r.stdout.startswith("\x1b[1;31mESCALATED: 1 unacked (RED UNCLAIMED: Main #2146 [ci]: "), r.stdout)
        self.assertIn("refresh", open(os.path.join(HERE, "..", "claude-statusline")).read())
        self.assertIn("agentctl inbox escalated", open(os.path.join(HERE, "..", "claude-statusline")).read())


class WaitForeverTest(RedBase):
    def test_inbox_wait_forever_streams_one_line_per_entry_and_keeps_running(self):
        p = subprocess.Popen([sys.executable, TOOL, "inbox", "wait", "--forever"], stdout=subprocess.PIPE, text=True,
                             env=dict(os.environ, AGENTCTL_INBOX_POLL="0.1"))
        try:
            A.inbox_write("red", "RED UNCLAIMED: Main #1 [x]: NOBODY -- y", [], [], 1)
            first = p.stdout.readline()
            A.inbox_write("land", "NOT LANDED: the checks failed", [], [], 1)
            A.inbox_write("land", "LANDED abc1234 (not a wait kind)", [], [], 0)
            second = p.stdout.readline()
            time.sleep(0.5)
            self.assertIsNone(p.poll())  # still running
        finally:
            p.kill()
            rest = p.communicate()[0]
        self.assertIn("RED UNCLAIMED: Main #1", first)
        self.assertIn("NOT LANDED: the checks failed", second)
        self.assertEqual(rest, "")  # each once, and nothing but entries


if __name__ == "__main__":
    unittest.main()
