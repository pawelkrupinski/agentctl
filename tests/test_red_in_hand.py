"""THE RED STAYS IN HAND (the owner, 2026-11-21: "structural fix for the order independence being abandoned"). Order
independence #94: a853b2ee4 said `Fixes-Red: 94` and only added diagnostics; agentctl moved the red to fix-pending,
released its fixer, and waited ~24 h for the 02:40Z cron -- nobody at work, its `verified by 06:27Z` passed silently.
Replayed over the red machine:
- VERIFY PROMPTLY BY DEFAULT: a workflow with workflow_dispatch and no `verify_dispatch no` row is dispatched to verify
  as its fix lands (tests/test_verify_wait.py covers the scheduled slot);
- A FIX WITH NO REPLAY KEEPS ITS FIXER: a Fixes-Red without an agentctl-run Fix-Replay (base=FAIL head=PASS) leaves
  the holder's lease, the red fix-unproven, and a refutation goes back to that holder;
- DIAGNOSTICS ARE NO FIX: `Diagnoses-Red:` never links the red; it dispatches a run and its verdict goes to the holder;
- A MISSED DEADLINE IS NEVER SILENT: a dispatched verification that never came re-offers the red, escalated."""
import os, sys, unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from test_agentctl import A  # noqa: E402
from test_verify_wait import VerifyWaitBase, utc  # noqa: E402

ORDER = """name: Order independence
on:
    schedule:
        - cron: '40 2 * * *'
    workflow_dispatch:
        inputs:
            seed:
                required: false
jobs:
    shuffle:
        runs-on: x
        steps:
            - run: scripts/order.sh
"""
KEY = "oi94"
CHANGE = "20261121-024000-movies-wt-oi94-fixer-a1b2c3"


class InHandBase(VerifyWaitBase):
    def setUp(self):
        super().setUp()
        self.r.write(".github/workflows/order-independence.yml", ORDER)
        self.r.write("scripts/order.sh", "echo order\n")
        self.r.commit("order")
        self.r.write("scripts/order.sh", "echo order, with more logging\n")
        self.sha = self.r.commit("order: log the seed\n\nFixes-Red: 94")
        self.r.push()
        self.wt = self.r.worktree("wt-oi94-fixer", branch="oi94-fixer")
        self.t[0] = utc("2026-11-21T02:00:00Z")
        self.st.put_item("red", KEY, "leased", {
            "key": KEY, "workflow": "Order independence", "gate": False, "red_kind": "outside", "jobs": ["shuffle"],
            "kind": "test", "line": "shuffle failed", "runs": ["94", "9094"], "opened_at": self.t[0] - 3600,
            "last_run": {"number": 94, "databaseId": 9094}})
        self.st.acquire(A.red_lease(KEY), "laptop:fixer:oi94", 4 * 3600, 48 * 3600,
                        meta={"kind": "fixer", "worktree": self.wt})
        for name, fake in (("main_workflow_files", lambda o_r: {}), ("red_callable_evidence", lambda *a: None),
                           ("red_retired", lambda *a: None)):
            saved = getattr(A, name)
            setattr(A, name, fake)
            self.addCleanup(setattr, A, name, saved)

    def land(self, replays=None, diagnoses=None, fixes=("94",)):
        ch = self.st.put_item("change", CHANGE, "queued", {"worktree": self.wt, "head": self.sha,
                                                          **({"replays": replays} if replays else {}),
                                                          **({"diagnoses": list(diagnoses)} if diagnoses else {}),
                                                          **({"fixes": list(fixes)} if fixes else {})})
        self.st.transition("change", CHANGE, "landed", ch["version"], data={"landed_sha": self.sha}, event={"cause": "agent"})
        self.entry = {"id": CHANGE, "status": "landed", "landed_sha": self.sha, "fixes": list(fixes or []),
                      "diagnoses": list(diagnoses or []), "worktree": self.wt, "created": "2026-11-21T02:00:00Z",
                      "landed_at": "2026-11-21T02:00:00Z", "commits": [self.sha]}

    def facts(self):
        return {"entries": [self.entry] if getattr(self, "entry", None) and self.entry["fixes"] else [],
                "untrailered": [self.entry] if getattr(self, "entry", None) and not self.entry["fixes"] else [],
                "landed": [], "repo": self.r.path}

    def step(self, at=None, obs=None, latest=None, gone=False):
        if at:
            self.t[0] = utc(at)
        it = self.st.item("red", KEY)
        return A.red_step(self.st, it, obs, {"latest": latest or {}}, self.facts(), self.r.path, A.RED_PRIMARY,
                          self.t[0])

    def item(self):
        return self.st.item("red", KEY)

    def live(self):
        return A.lease_is_live(self.st.get(A.red_lease(KEY)), self.t[0])

    def note(self):
        p = os.path.join(self.wt, "RED-NOTE.md")
        return open(p).read() if os.path.exists(p) else ""


class FixWithNoReplayKeepsItsFixerTest(InHandBase):
    def test_a853b2ee4_replayed_an_unproven_fix_leaves_the_fixer_holding_the_red(self):
        self.land()
        out = self.step("2026-11-21T02:01:00Z")
        it = self.item()
        self.assertEqual(it["state"], "fix-pending", out)
        self.assertTrue(it["data"]["fix"]["unproven"])
        self.assertTrue(self.live(), "its fixer is not released")
        self.assertFalse(os.path.exists(os.path.join(self.wt, "RED-STOP.md")))
        self.assertIn("the red stays yours, fix-unproven", self.note())
        self.assertIn("fix-unproven", A.red_view(self.st, it, self.t[0])[1])

    def test_a_fix_agentctl_replayed_base_fail_head_pass_releases_the_fixer_as_before(self):
        self.land(replays=[{"cmd": "scripts/order.sh", "base": "FAIL", "head": "PASS"}])
        self.step("2026-11-21T02:01:00Z")
        it = self.item()
        self.assertEqual(it["state"], "fix-pending")
        self.assertFalse(it["data"]["fix"].get("unproven"))
        self.assertFalse(self.live())

    def test_an_unproven_fix_refuted_by_its_run_goes_back_to_the_same_fixer(self):
        self.land()
        self.step("2026-11-21T02:01:00Z")
        latest = {"Order independence": {"number": 95, "databaseId": 9095, "conclusion": "failure", "headSha": self.sha,
                                         "updatedAt": "2026-11-21T04:00:00Z"}}
        obs = {KEY: {"run": {"databaseId": 9095, "number": 95}}}
        out = self.step("2026-11-21T04:01:00Z", obs=obs, latest=latest)
        it = self.item()
        self.assertIn("FIX FAILED", " ".join(out))
        self.assertEqual((it["state"], it["data"]["holder"]), ("leased", "laptop:fixer:oi94"))
        self.assertTrue(self.live(), "never released for a new fixer")
        self.assertIn(self.sha, it["data"]["refuted"])
        self.assertIn("the red is still yours", self.note())


class DiagnosticsAreNoFixTest(InHandBase):
    def test_a_diagnoses_red_change_never_links_and_dispatches_a_run_whose_verdict_goes_to_the_holder(self):
        self.land(diagnoses=["94"], fixes=())
        self.r.write(".github/workflows/order-independence.yml", ORDER + "# more logging\n")  # its workflow's own file
        out = self.step("2026-11-21T02:01:00Z")
        it = self.item()
        self.assertEqual(it["state"], "leased", "no fix-pending for diagnostics")
        self.assertIsNone(it["data"].get("fix"))
        self.assertEqual(self.posts, [("POST", "actions/workflows/order-independence.yml/dispatches")], out)
        self.assertIn(f"your diagnostics {CHANGE} landed: dispatched Order independence", self.note())
        self.step("2026-11-21T02:02:00Z")
        self.assertEqual(len(self.posts), 1, "once per change")
        latest = {"Order independence": {"number": 95, "databaseId": 9095, "conclusion": "failure", "status": "completed",
                                         "headSha": self.sha, "updatedAt": "2026-11-21T03:30:00Z"}}
        self.step("2026-11-21T03:31:00Z", latest=latest)
        self.assertIn(f"Order independence #95 after your diagnostics {CHANGE}: failure", self.note())
        self.assertTrue(self.live())


class MissedDeadlineIsNeverSilentTest(InHandBase):
    def test_a_dispatched_verification_that_never_came_re_offers_the_red_escalated(self):
        self.land(replays=[{"cmd": "x", "base": "FAIL", "head": "PASS"}])
        self.step("2026-11-21T02:01:00Z")
        self.step("2026-11-21T02:02:00Z")  # verified promptly by default: dispatched at once (its slot is 02:40Z)
        f = self.item()["data"]["fix"]
        self.assertTrue(str(f.get("verify")).startswith("dispatched Order independence"), f)
        self.t[0] = self.item()["deadline"] + 30
        it = self.item()
        A.red_fix_pending(self.st, it, it["data"]["fix"], False, {}, {"latest": {}}, self.facts(), self.r.path,
                          A.RED_PRIMARY, self.t[0])
        it = self.item()
        self.assertEqual(it["state"], "offered")
        self.assertIn("escalate", {e["severity"] for e in self.st.item_events("red", KEY)})


class TrailerTest(InHandBase):
    def test_diagnoses_red_is_read_and_carried_on_the_change_item_with_its_replays(self):
        self.r.write("scripts/order.sh", "echo order, logging the shard\n")
        sha = self.r.commit("order: log the shard\n\nDiagnoses-Red: 94")
        self.assertEqual(A.diagnoses_of(self.r.path, [sha]), {"94"})
        self.assertEqual(A.diagnoses_of(self.r.path, [self.sha]), set(), "a Fixes-Red is no diagnosis")
        fields = A.change_item_fields({"worktree": self.wt, "head": sha, "diagnoses": ["94"],
                                       "replays": [{"cmd": "x", "base": "FAIL", "head": "PASS"}]})
        self.assertEqual((fields["diagnoses"], fields["replays"][0]["head"]), (["94"], "PASS"))


if __name__ == "__main__":
    unittest.main()
