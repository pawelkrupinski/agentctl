"""A FIX-PENDING RED'S VERIFY DEADLINE IS ITS WORKFLOW'S NEXT RUN (2026-11-19). Two reds outside the gate, replayed:

  - Record scrape fixtures #198 (50bc15cd7369): its fix 123dd6524 landed 04:23Z; the workflow runs nightly at 23:31Z,
    yet a fixed 3 h timer (and the landing's own self-move, which raised its level) showed it `fix-pending escalated`
    at 07:23Z, and a verify run of the paid-proxy recording was dispatched at 04:34Z (#200).
  - Alert backtest #4 (5254bffda9d2): its fix change was cancelled and re-queued a minute later, but the red followed it
    to an 8-day-old change of the same (reused) worktree -- the change items had no `submitted_at` -- whose phantom tip
    was no fix: "UNVERIFIED: INVARIANT: no fix on its way for 0 min in fix-pending"."""
import calendar, os, sys, tempfile, time, unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from test_agentctl import A, Repo  # noqa: E402

RECORD = """name: Record scrape fixtures
on:
    schedule:
        - cron: '31 23 * * *'
    repository_dispatch:
        types: [scheduled-record-scrape-fixtures]
    workflow_dispatch:
jobs:
    record:
        runs-on: x
        steps:
            - run: scripts/record.sh
"""


def utc(s):
    return float(calendar.timegm(time.strptime(s, "%Y-%m-%dT%H:%M:%SZ")))


def gh_run(n, created, updated, status="completed", conclusion="success", sha="0" * 40, event="repository_dispatch"):
    return {"databaseId": 1000 + n, "number": n, "status": status, "conclusion": conclusion, "headSha": sha,
            "createdAt": created, "updatedAt": updated, "event": event, "workflowName": "Record scrape fixtures"}


class VerifyWaitBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        t = os.path.realpath(self.tmp.name)
        keys = ("AGENTCTL_STATE_ROOT", "AGENTCTL_REDS_DIR", "AGENTCTL_INBOX_DIR")
        saved = {k: os.environ.get(k) for k in keys}
        self.addCleanup(lambda: [os.environ.pop(k, None) if v is None else os.environ.__setitem__(k, v)
                                 for k, v in saved.items()])
        for k, sub in zip(keys, ("state", "reds", "inbox")):
            os.environ[k] = os.path.join(t, sub)
        self.r = Repo(t)
        self.t = [0.0]
        self.st = A.Store(path=os.path.join(t, "store.db"), clock=lambda: self.t[0])
        self.runs, self.posts = [], []
        for name, fake in (("gh_json", self.fake_gh), ("api", self.fake_api), ("record_dispatch", lambda **k: None)):
            saved_fn = getattr(A, name)
            setattr(A, name, fake)
            self.addCleanup(setattr, A, name, saved_fn)
        A.ci_runs().clear()
        self.addCleanup(A.ci_runs().clear)

    def fake_gh(self, o_r, *args):  # GitHub's run list (the seam): the runs this test put there
        assert args[:2] == ("run", "list"), args
        return list(self.runs)

    def fake_api(self, o_r, method, path, body=None, **k):
        self.posts.append((method, path))
        return {}


class ScheduledFixWaitsForItsSlotTest(VerifyWaitBase):
    """Record scrape fixtures #198, 50bc15cd7369."""

    def setUp(self):
        super().setUp()
        self.r.write(".github/workflows/record-scrape-fixtures.yml", RECORD)
        self.r.write("scripts/record.sh", "echo record\n")
        # the paid-proxy recording opts out of VERIFY PROMPTLY BY DEFAULT (red.py): its slot decides its fix
        self.r.write(A.RED_WORKFLOWS_FILE, "# workflow\tverify_dispatch\treplay\twhy\n"
                                           "Record scrape fixtures\tno\t-\tevery cinema through paid proxies\n")
        self.r.commit("record")
        self.r.write("scripts/record.sh", "echo record with its own lane\n")
        self.sha = self.r.commit("record: the consumer on a lane below its producer\n\nFixes-Red: 198")
        self.r.push()
        # three finished nights: 1 h 06, 2 h 44, 1 h 30 -- p90 2 h 44
        self.runs = [gh_run(196, "2026-11-17T23:46:04Z", "2026-11-18T00:52:58Z"),
                     gh_run(197, "2026-11-18T04:38:35Z", "2026-11-18T04:39:11Z", conclusion="cancelled", event="schedule"),
                     gh_run(198, "2026-11-18T23:46:04Z", "2026-11-19T02:30:19Z", conclusion="failure"),
                     gh_run(195, "2026-11-16T23:46:04Z", "2026-11-17T01:16:04Z")]
        self.landed = utc("2026-11-19T04:23:00Z")
        self.t[0] = self.landed
        self.st.put_item("red", "50bc15cd7369", "fix-pending", {
            "key": "50bc15cd7369", "workflow": "Record scrape fixtures", "gate": False, "red_kind": "outside",
            "jobs": ["enrichment (united-kingdom)", "order-independence (united-kingdom)"], "kind": "test",
            "line": "order-independence (united-kingdom) failed", "runs": ["198", "44423518717"], "opened_at": 0,
            "last_run": {"number": 198, "databaseId": 44423518717},
            "fix": {"entry": "20261119-041244-movies-wt-owner-outside-gate-b06246", "status": "landed",
                    "sha": self.sha, "at": utc("2026-11-19T04:04:01Z"), "verify_since": self.landed}})

    def facts(self):  # its Fixes-Red commit, landed on main
        return {"entries": [], "landed": [(self.sha, utc("2026-11-19T04:04:01Z"), {"198"}, "fix")], "repo": self.r.path}

    def step(self, at, latest=None):
        self.t[0] = utc(at)
        it = self.st.item("red", "50bc15cd7369")
        facts = self.facts()
        return A.red_fix_pending(self.st, it, it["data"]["fix"], False, {}, {"latest": latest or {}}, facts,
                                 self.r.path, A.RED_PRIMARY, self.t[0])

    def item(self):
        return self.st.item("red", "50bc15cd7369")

    def test_its_deadline_is_the_next_slot_plus_its_runs_p90_plus_slack_and_it_rests_quietly(self):
        out = self.step("2026-11-19T04:23:15Z")
        self.assertIn("awaiting next scheduled run at 23:31Z", " ".join(out))
        it = self.item()
        by = utc("2026-11-19T23:31:00Z") + (utc("2026-11-19T02:30:19Z") - utc("2026-11-18T23:46:04Z")) + A.RED_VERIFY_SLACK
        self.assertEqual((it["state"], it["level"], it["deadline"]), ("fix-pending", 0, by))
        self.assertEqual(it["data"]["fix"]["next_run_at"], utc("2026-11-19T23:31:00Z"))
        self.assertEqual({e["severity"] for e in self.st.item_events("red", it["id"])}, {"info"})
        self.assertIn("awaiting next scheduled run at 23:31Z", A.red_view(self.st, it, self.t[0])[1])
        # 07:23Z, where the fixed 3 h timer escalated it, and 12:00Z: nothing to say, nobody to tell
        self.assertEqual(self.step("2026-11-19T07:23:30Z"), [])
        self.assertEqual(self.step("2026-11-19T12:00:00Z"), [])
        it = self.item()
        self.assertEqual((it["state"], it["level"]), ("fix-pending", 0))
        self.assertEqual(A.red_enforce(self.st, self.facts(), self.t[0]), [],
                         "a fix waiting for its slot is a fix on its way: no INVARIANT")
        self.assertEqual({e["severity"] for e in self.st.item_events("red", it["id"])}, {"info"})

    def test_a_landing_takes_back_the_level_its_self_move_raised(self):
        it = self.item()
        self.st.transition("red", it["id"], "fix-pending", it["version"], event={"cause": "agent"})  # the old landing move: escalate, level 1
        self.assertEqual(self.item()["level"], 1)
        self.step("2026-11-19T04:23:15Z")
        self.assertEqual(self.item()["level"], 0)

    def test_a_fix_pending_self_move_is_quiet_unless_it_says_escalate(self):
        it = self.item()
        A.red_move(self.st, it, "fix-pending", "fix x landed (123dd6524e): verifying", data={})
        self.assertEqual(self.item()["level"], 0)

    def test_a_scheduled_workflow_whose_row_says_no_is_never_dispatched_to_verify(self):
        self.step("2026-11-19T04:23:15Z")
        out = self.step("2026-11-19T05:00:00Z")  # past RED_FIX_RUN_START: where #200 was dispatched
        self.assertEqual((self.posts, out, self.item()["level"]), ([], [], 0))
        self.assertIn("says verify_dispatch no: never dispatched to verify",
                      A.red_verify_dispatch(A.RED_PRIMARY, self.r.path, "Record scrape fixtures", self.sha))
        self.assertEqual(self.posts, [])

    def test_with_no_row_a_scheduled_workflow_is_dispatched_to_verify_as_its_fix_lands(self):
        """VERIFY PROMPTLY BY DEFAULT (the owner, 2026-11-21: Order independence #94's fix waited a day for its cron):
        no row is yes, and the dispatch goes in the tick that sees the fix landed -- never after RED_FIX_RUN_START."""
        self.r.write(A.RED_WORKFLOWS_FILE, "# workflow\tverify_dispatch\treplay\twhy\n")
        self.r.commit("no row for the recording")
        self.r.push()
        out = self.step("2026-11-19T04:23:15Z")
        self.assertEqual(self.posts, [("POST", "actions/workflows/record-scrape-fixtures.yml/dispatches")], out)
        self.assertIn("dispatched Record scrape fixtures on main to verify", " ".join(out))
        # 2026-11-21, Order independence #99: `red status` still said "awaiting next scheduled run at 02:40Z" with
        # its verify run (#100) already dispatched: the view names the dispatch, never the cron slot
        text, short = A.red_view(self.st, self.item(), self.t[0])
        self.assertRegex(text, r"verify run dispatched \d\d:\d\dZ")
        self.assertNotIn("awaiting next scheduled run", text + short)

    def test_its_run_decides_it(self):
        self.step("2026-11-19T04:23:15Z")
        latest = {"Record scrape fixtures": {"number": 201, "databaseId": 1201, "conclusion": "success",
                                             "headSha": self.sha, "updatedAt": "2026-11-20T01:00:00Z"}}
        t = utc("2026-11-20T01:00:30Z")
        self.t[0] = t
        it = self.item()
        out = A.red_fix_pending(self.st, it, it["data"]["fix"], True, {}, {"latest": latest},
                                self.facts(), self.r.path, A.RED_PRIMARY, t)
        self.assertIn("FIX OK", " ".join(out))
        self.assertEqual(self.item()["state"], "verified")

    def test_past_its_deadline_a_run_still_deciding_it_holds_it_quietly(self):
        self.step("2026-11-19T04:23:15Z")
        self.runs.insert(0, gh_run(201, "2026-11-19T23:46:04Z", "2026-11-19T23:46:04Z", status="queued",
                                   conclusion="", sha=self.sha))
        A.ci_runs().clear()
        by = self.item()["deadline"]
        out = self.step(time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(by + 30)))
        self.assertIn("awaits Record scrape fixtures #201 (queued)", " ".join(out))
        self.assertEqual((self.item()["state"], self.item()["level"]), ("fix-pending", 0))

    def test_past_its_deadline_with_no_run_dispatched_it_goes_back_to_a_fixer_escalated(self):
        """A MISSED DEADLINE IS NEVER SILENT (2026-11-21, Order independence #94 past `verified by 06:27Z`)."""
        self.step("2026-11-19T04:23:15Z")
        by = self.item()["deadline"]
        out = self.step(time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(by + 30)))
        it = self.item()
        self.assertIn("no Record scrape fixtures run was dispatched for its 23:31Z slot",
                      it["data"]["history"][-1]["outcome"], out)
        self.assertEqual(it["state"], "offered")
        self.assertIn("escalate", {e["severity"] for e in self.st.item_events("red", it["id"])})

    def test_past_its_deadline_a_slot_run_that_ended_without_deciding_it_escalates(self):
        self.step("2026-11-19T04:23:15Z")
        self.runs.insert(0, gh_run(201, "2026-11-19T23:46:04Z", "2026-11-19T23:50:00Z", conclusion="cancelled"))
        A.ci_runs().clear()
        by = self.item()["deadline"]
        out = self.step(time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(by + 30)))
        self.assertIn("its 23:31Z run #201 ended cancelled without deciding it",
                      self.item()["data"]["history"][-1]["outcome"], out)
        self.assertEqual(self.item()["state"], "offered")

    def test_it_gives_up_to_a_fixer_one_decide_interval_past_its_deadline(self):
        self.step("2026-11-19T04:23:15Z")
        by = self.item()["deadline"]
        self.step(time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(by + 30)))
        self.step(time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(by + A.RED_FIX_DECIDE + 60)))
        self.assertEqual(self.item()["state"], "offered")

    def test_cron_next(self):
        t = utc("2026-11-19T04:23:00Z")
        self.assertEqual(A.cron_next(["31 23 * * *"], t), utc("2026-11-19T23:31:00Z"))
        self.assertEqual(A.cron_next(["7 2 * * 0"], t), utc("2026-11-22T02:07:00Z"))  # Sunday
        self.assertEqual(A.cron_next(["*/15 * * * *"], t), utc("2026-11-19T04:30:00Z"))
        self.assertEqual(A.cron_next(["0 6 1 * *"], t), utc("2026-12-01T06:00:00Z"))
        self.assertEqual(A.cron_next(["0 6 1 * 1"], t), utc("2026-11-23T06:00:00Z"))  # day of month OR day of week
        self.assertEqual(A.cron_next(["41 12 * * *"], utc("2026-11-19T07:04:33Z")), utc("2026-11-19T12:41:00Z"))
        self.assertIsNone(A.cron_next(["not a cron"], t))
        self.assertEqual(A.workflow_crons(RECORD), ["31 23 * * *"])


class CancelledFixFollowsNoOlderChangeTest(VerifyWaitBase):
    """Alert backtest #4, 5254bffda9d2: the 03:59:06Z INVARIANT."""

    def stamp(self, eid):
        return calendar.timegm(time.strptime(eid[:15], "%Y%m%d-%H%M%S"))  # a change id's stamp is UTC (ids.stamp)

    def test_a_cancelled_fix_waits_for_its_requeue_not_for_an_old_change_of_its_worktree(self):
        wt = "/Users/x/projects/movies-wt-owner-outside-gate"
        old, cancelled, requeued = ("20261111-182635-movies-wt-owner-outside-gate-130",
                                    "20261119-035758-movies-wt-owner-outside-gate-8b34c0",
                                    "20261119-035951-movies-wt-owner-outside-gate-f4c153")
        self.t[0] = self.stamp(old) + 3600
        ch = self.st.put_item("change", old, "queued", {"worktree": wt, "head": "1" * 40})  # no submitted_at
        self.st.transition("change", old, "landed", ch["version"], data={"tip": "b6d3d8235b" + "0" * 30}, event={"cause": "agent"})
        self.t[0] = self.stamp(cancelled) + 60
        ch = self.st.put_item("change", cancelled, "queued", {"worktree": wt, "head": "0be3d7d23f" + "0" * 30})
        self.st.put_item("red", "5254bffda9d2", "fix-pending", {
            "key": "5254bffda9d2", "workflow": "Alert backtest", "gate": False, "red_kind": "outside", "jobs": ["backtest"],
            "kind": "test", "line": "backtest failed", "runs": ["4"], "opened_at": self.stamp(old) + 7 * 86400,
            "last_run": {"number": 4, "databaseId": 4},
            "fix": {"entry": cancelled, "status": "checking", "author": {"worktree": wt}}})
        self.st.transition("change", cancelled, "cancelled", ch["version"], event={"cause": "agent"})
        facts = {"entries": [{"id": cancelled, "status": "cancelled", "fixes": ["4"], "worktree": wt,
                              "created": "2026-11-19T03:57:58Z"}], "landed": [], "repo": None}

        def tick():
            it = self.st.item("red", "5254bffda9d2")
            return A.red_fix_pending(self.st, it, A.red_fix_of(it, facts, self.st), False, {}, {"latest": {}}, facts,
                                     None, A.RED_PRIMARY, self.t[0]) + A.red_enforce(self.st, facts, self.t[0])

        self.t[0] += 7  # 03:59:06Z
        self.assertEqual(tick(), [], "no follow to the 11-11 change, no INVARIANT inside the re-queue grace")
        it = self.st.item("red", "5254bffda9d2")
        self.assertEqual((it["state"], it["data"]["fix"]["entry"], it.get("level")), ("fix-pending", cancelled, 0))
        self.t[0] = self.stamp(requeued) + 15  # its worktree queued it again
        self.st.put_item("change", requeued, "queued", {"worktree": wt, "head": "0be3d7d23f" + "0" * 30})
        facts["entries"].append({"id": requeued, "status": "queued", "fixes": ["4"], "worktree": wt,
                                 "created": "2026-11-19T03:59:51Z"})
        tick()
        it = self.st.item("red", "5254bffda9d2")
        self.assertEqual((it["state"], it["data"]["fix"]["entry"]), ("fix-pending", requeued))
        self.assertNotIn("INVARIANT", " ".join(str(h.get("outcome")) for h in it["data"].get("history") or []))

    def test_a_rejected_fix_goes_back_to_a_fixer_with_the_rejections_reason(self):
        """2026-11-22, Identity takes #349: its fix change ended rejected (the landing check's itrest row: the GitOps
        manifests missing) and red status and the inbox said only "ended rejected": the reason was in the change item
        alone. The red goes back to a fixer with the reason in its history, where red status and the prompt read."""
        wt = "/Users/x/projects/movies-wt-owner-convergence"
        eid = "20261122-082612-movies-wt-owner-convergence-2ba8fd"
        why = ("NOT LANDED: the pre-push checks failed (on ci-runner); failing with this change only: should hold G1's "
               "heap-sizing ergonomics at JDK 25's values")
        self.t[0] = self.stamp(eid) + 60
        ch = self.st.put_item("change", eid, "queued", {"worktree": wt, "head": "45a09c7219" + "0" * 30})
        self.st.put_item("red", "9a333c1f0ccc", "fix-pending", {
            "key": "9a333c1f0ccc", "workflow": "Alert backtest", "gate": False, "red_kind": "outside",
            "jobs": ["backtest"], "kind": "test", "line": "takes moved", "runs": ["349"],
            "opened_at": self.stamp(eid) - 3600, "last_run": {"number": 349, "databaseId": 349},
            "fix": {"entry": eid, "status": "checking", "author": {"worktree": wt}}})
        self.st.transition("change", eid, "rejected", ch["version"], data={"why": why}, event={"cause": "agent"})
        facts = {"entries": [{"id": eid, "status": "rejected", "fixes": ["349"], "worktree": wt,
                              "created": "2026-11-22T07:26:12Z"}], "landed": [], "repo": None}
        self.t[0] += A.RED_FIX_REQUEUE_GRACE + 1
        it = self.st.item("red", "9a333c1f0ccc")
        out = A.red_fix_pending(self.st, it, A.red_fix_of(it, facts, self.st), False, {}, {"latest": {}}, facts,
                                None, A.RED_PRIMARY, self.t[0])
        it = self.st.item("red", "9a333c1f0ccc")
        self.assertEqual(it["state"], "offered", out)
        self.assertIn("heap-sizing ergonomics", it["data"]["history"][-1]["outcome"])
        self.assertIn("heap-sizing ergonomics", " ".join(out))

    def test_a_change_without_submitted_at_was_submitted_at_its_id_stamp(self):
        eid = "20261119-035758-movies-wt-owner-outside-gate-8b34c0"
        self.assertEqual(A.change_submitted_at({"id": eid, "data": {}}), self.stamp(eid))
        self.assertEqual(A.change_submitted_at({"id": eid, "data": {"submitted_at": 5.0}}), 5.0)
        self.assertIsNone(A.change_submitted_at({"id": "direct", "data": {}}))


if __name__ == "__main__":
    unittest.main()


class MainMovedUnderAnOutsideRedTest(VerifyWaitBase):
    """2026-11-22, Order independence #105: 9a7130036 (tools-only, rightly no `Fixes-Red:`) fixed it, while the red sat
    fix-pending on an unrelated change linked only by editing its workflow file; no run was dispatched to see main fixed."""

    OI = ("name: Order independence\non:\n    workflow_dispatch:\njobs:\n    swift:\n        runs-on: x\n"
          "        steps:\n            - run: scripts/ci/ratchet_diff.py\n")

    def setUp(self):
        super().setUp()
        self.r.write(".github/workflows/order-independence.yml", self.OI)
        self.r.write("scripts/ci/ratchet_diff.py", "print('ratchet')\n")
        self.head = self.r.commit("the red run's head")
        self.r.push()
        self.t[0] = utc("2026-11-22T05:20:00Z")
        self.st.put_item("red", "ec12a4564dbc.9", "fix-pending", {
            "key": "ec12a4564dbc", "workflow": "Order independence", "gate": False, "red_kind": "outside",
            "jobs": ["swift"], "job": "swift", "kind": "test", "line": "POLLUTE", "runs": ["105"], "opened_at": 0,
            "last_run": {"number": 105, "databaseId": 44985132691, "headSha": self.head},
            "fix": {"entry": "20261122-101950-movies-oi-structural-fdfee6", "status": "checking",
                    "linked": "its workflow's own files"}})

    def verify(self):
        return A.red_main_verify(self.st, self.st.item("red", "ec12a4564dbc.9"), self.r.path, A.RED_PRIMARY, self.t[0])

    def test_main_moving_under_it_dispatches_its_workflow_to_verify_once_per_head(self):
        self.assertEqual(self.verify(), [], "main has not moved since its run: nothing to verify")
        self.r.write("scripts/ci/ratchet_diff.py", "import sys\nsys.dont_write_bytecode = True\nprint('ratchet')\n")
        fix = self.r.commit("ratchet-diff: write no bytecode for its sibling ratchets")
        self.r.push()
        out = self.verify()
        self.assertEqual(len(out), 1, out)
        self.assertIn("main moved since its run", out[0])
        self.assertTrue(any(m == "POST" and "order-independence.yml/dispatches" in p for m, p in self.posts), self.posts)
        mv = self.st.item("red", "ec12a4564dbc.9")["data"]["main_verify"]
        self.assertEqual(mv["reach"], fix)
        self.t[0] += A.RED_MAIN_VERIFY_EVERY + 1
        self.assertEqual(self.verify(), [], "once per main head")

    def test_a_gate_red_or_one_with_its_own_landed_fix_is_left_to_its_fix(self):
        self.r.write("scripts/ci/ratchet_diff.py", "print('moved')\n")
        self.r.commit("moved")
        self.r.push()
        it = self.st.item("red", "ec12a4564dbc.9")
        self.st.update_item("red", it["id"], it["version"], {"gate": True})
        self.assertEqual(self.verify(), [])
        it = self.st.item("red", "ec12a4564dbc.9")
        self.st.update_item("red", it["id"], it["version"], {"gate": False, "fix": {"status": "landed", "sha": "x"}})
        self.assertEqual(self.verify(), [])
        self.assertEqual(self.posts, [])
