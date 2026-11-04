"""ONE FIXER LIFECYCLE and AN ATTEMPT IS A FIXER THAT TRIED AND FAILED TO FIX (2026-11-04).

A week of alerts (10-28..11-04): FIXER_FAILED 204, RED_ESCALATE 261. The owners' restart fixes of 11-03 (activity-based
renewal, a lapsed heartbeat taken again) were the per-family OWNER wrappers' only; the per-red FIXERS still ended
"STOPPED: the red's lease was lost" (13 on 11-04) -- most because their red had CLEARED under them -- each a RED FIXER
failure and a new attempt. And every give-up counted as an attempt: #728 escalated 58 times on 11-02, each owner turn
ending "GAVE UP: ... the approved flip-unification stage 5 (a resolver rule change, outside red fixing)"."""
import inspect, json, os, sys, time, unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from test_agentctl import A  # noqa: E402
from test_owner import OwnerBase, FAM  # noqa: E402
from test_reds import RedsBase  # noqa: E402


def fixer_entries():
    d = A.inbox_dir()
    out = []
    for f in sorted(os.listdir(d)) if os.path.isdir(d) else []:
        if f.endswith(".json"):
            e = json.load(open(os.path.join(d, f)))
            if str(e.get("verdict") or "").startswith("RED FIXER "):
                out.append(e)
    return out


class OneLifecycleTest(unittest.TestCase):
    def test_both_wrappers_keep_leases_and_judge_progress_with_the_same_code(self):
        for f in (A.red_fixer, A.red_owner):
            src = inspect.getsource(f)
            self.assertFalse("st.renew(" in src, f"{f.__name__} renews a lease by hand: HeldLease.renew only")
            self.assertFalse("fixer_mark(" in src, f"{f.__name__} judges progress by hand: TurnProgress only")
            self.assertIn("HeldLease(", src)
            self.assertIn("TurnProgress(", src)

    def test_lease_loss_reads_a_lapse_a_cleared_red_and_a_lost_lease_apart(self):
        row = {"epoch": 3, "holder": "laptop:fixer:red-k-1", "expires_at": 10, "max_until": 1000, "meta": "{}"}
        self.assertEqual(A.lease_loss(row, 3, "laptop:fixer:red-k-1", 20), ("lapsed", None))
        cleared = dict(row, holder="", meta=json.dumps({"outcome": "red cleared", "revoked": True}))
        self.assertEqual(A.lease_loss(cleared, 3, "laptop:fixer:red-k-1", 20), ("done", "red cleared"))
        orphaned = dict(row, holder="", meta=json.dumps({"outcome": "ORPHANED: x", "revoked": True}))
        self.assertEqual(A.lease_loss(orphaned, 3, "laptop:fixer:red-k-1", 20)[0], "lost")
        self.assertEqual(A.lease_loss(dict(row, epoch=4), 3, "laptop:fixer:red-k-1", 20)[0], "lost")
        self.assertEqual(A.lease_loss(row, 3, "laptop:fixer:red-k-1", 2000)[0], "lost", "past its max hold")


class FixerLeaseTest(RedsBase):
    def setUp(self):
        super().setUp()
        os.environ.update(FAKE_CLAUDE_SLEEP="60", AGENTCTL_RED_RENEW="0.3")
        self.addCleanup(os.environ.pop, "AGENTCTL_RED_RENEW", None)
        self.addCleanup(os.environ.__setitem__, "FAKE_CLAUDE_SLEEP", "0")

    def wait(self, cond, limit=30):
        t0 = time.time()
        while not cond() and time.time() - t0 < limit:
            time.sleep(0.1)
        return cond()

    def rec(self):
        return (A.fixer_records() or [{}])[0]

    def start(self):
        self.tick(dispatch=True)
        it = self.only()
        name = A.red_lease(it["data"]["key"])
        self.assertTrue(self.wait(lambda: self.rec().get("transcript")))
        return it, name

    def test_a_fixer_whose_red_cleared_under_it_ends_quietly_never_a_fixer_failure(self):
        """2026-11-04 14:09Z: red eeaba52d8837 cleared (a fix landed by another hand); its fixer-3 ended "STOPPED: the
        red's lease was lost" -- a RED FIXER failure in the owner's inbox."""
        _it, name = self.start()
        self.st.revoke(name, "red cleared")
        self.assertTrue(self.wait(lambda: self.rec().get("ended")))
        self.assertTrue(self.rec()["verdict"].startswith("ENDED: red cleared"), self.rec()["verdict"])
        self.assertEqual(fixer_entries(), [], "no RED FIXER entry: nothing failed")

    def test_a_fixer_whose_lease_merely_lapsed_takes_it_again_and_goes_on(self):
        """The laptop asleep or the store out of reach past RED_FIXER_TTL, nobody took the red: the living wrapper
        takes its lease again (as an owner takes its heartbeat), its session never stopped."""
        _it, name = self.start()
        before = self.st.get(name)
        c = self.st.db()
        c.execute("UPDATE leases SET expires_at=? WHERE name=?", (time.time() - 1, name))
        c.close()
        self.assertTrue(self.wait(lambda: (self.st.get(name) or {}).get("epoch") == before["epoch"] + 1, limit=10),
                        self.st.get(name))
        row = self.st.get(name)
        self.assertEqual(row["holder"], before["holder"])
        self.assertTrue(A.lease_is_live(row, time.time()))
        self.assertEqual(A.lease_meta(row).get("kind"), "fixer", "its meta kept")
        self.assertFalse(self.rec().get("ended"), self.rec().get("verdict"))
        self.assertTrue(self.wait(lambda: self.rec().get("epoch") == before["epoch"] + 1, limit=5))


class OwnerRedLeaseLapseTest(OwnerBase):
    def test_an_owners_red_lease_that_merely_lapsed_is_taken_again_never_dropped(self):
        os.environ["FAKE_CLAUDE_SLEEP"] = "60"
        self.addCleanup(os.environ.__setitem__, "FAKE_CLAUDE_SLEEP", "0")
        self.tick(dispatch=True)
        name = A.red_lease(self.only()["data"]["key"])
        self.assertTrue(self.wait(lambda: self.rec().get("in_turn") and self.rec().get("bg_id")))
        before = self.st.get(name)
        c = self.st.db()
        c.execute("UPDATE leases SET expires_at=? WHERE name=?", (time.time() - 1, name))
        c.close()
        self.assertTrue(self.wait(lambda: (self.st.get(name) or {}).get("epoch") == before["epoch"] + 1, limit=10))
        self.assertEqual(self.st.get(name)["holder"], A.owner_holder(FAM))
        self.assertEqual(A.owner_leases(FAM)[name]["epoch"], before["epoch"] + 1, "still the owner's")


if __name__ == "__main__":
    unittest.main()
