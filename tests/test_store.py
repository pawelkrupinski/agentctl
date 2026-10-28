"""The store's leases (design §6, reconciler phase 1 step 1): renewed on progress only, capped by a max hold, fenced by
an epoch and a secret; an expired lease is free."""
import os, sys, tempfile, unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from test_agentctl import A  # noqa: E402


class StoreTest(unittest.TestCase):
    def setUp(self):
        self.t = [1000.0]
        self.st = A.Store(path=os.path.join(tempfile.mkdtemp(), "store.db"), clock=lambda: self.t[0])

    def test_acquire_renew_on_progress_only_max_hold_release_fencing(self):
        a = self.st.acquire("land/r", "me", 120, 300, progress="p0")
        self.assertIn("held", self.st.acquire("land/r", "you", 120, 300))
        self.t[0] += 60
        self.assertEqual(self.st.renew("land/r", a["epoch"], a["secret"], "p0", 120), "no_progress")  # not extended
        self.t[0] += 61
        b = self.st.acquire("land/r", "you", 120, 300)  # expired after 120 s without progress: free
        self.assertEqual(b["epoch"], a["epoch"] + 1)
        self.assertEqual(self.st.renew("land/r", a["epoch"], a["secret"], "p1", 120), "lost")  # fenced out
        self.assertEqual(self.st.release("land/r", a["epoch"], a["secret"]), "lost")
        for i in range(10):  # progress renews, up to the max hold
            self.t[0] += 60
            r = self.st.renew("land/r", b["epoch"], b["secret"], f"q{i}", 120)
        self.assertEqual(r, "lost")  # past 300 s
        c = self.st.acquire("land/r", "me", 120, 300)
        self.assertEqual(self.st.release("land/r", c["epoch"], c["secret"]), "ok")
        self.assertNotIn("held", self.st.acquire("land/r", "you", 120, 300))
        self.assertIn("held", self.st.acquire("land/r", "me", 120, 300))


if __name__ == "__main__":
    unittest.main()
