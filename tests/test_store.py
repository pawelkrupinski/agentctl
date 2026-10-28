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


class LeaseHistoryTest(unittest.TestCase):
    """The owner (2026-10-29): `lease list` showed ~20 long-free leases beside the 6 live ones."""

    def setUp(self):
        self.t = [1_000_000.0]
        self.st = A.Store(path=os.path.join(tempfile.mkdtemp(), "store.db"), clock=lambda: self.t[0])
        self.saved_root = os.environ.get("AGENTCTL_STATE_ROOT")
        os.environ["AGENTCTL_STATE_ROOT"] = tempfile.mkdtemp()  # cmd_lease's own Store()

    def tearDown(self):
        if self.saved_root is None:
            os.environ.pop("AGENTCTL_STATE_ROOT", None)
        else:
            os.environ["AGENTCTL_STATE_ROOT"] = self.saved_root

    def test_a_lease_free_over_a_day_is_archived_into_the_events_and_a_live_or_recent_one_stays(self):
        old = self.st.acquire("red/old", "fixer", 120, 300)
        self.st.release("red/old", old["epoch"], old["secret"], outcome="GAVE UP: x")
        self.t[0] += 23 * 3600
        recent = self.st.acquire("red/recent", "fixer", 120, 300)
        self.st.release("red/recent", recent["epoch"], recent["secret"], outcome="LANDED abc")
        self.st.acquire("land/r", "me", 7200, 9000)
        self.t[0] += 2 * 3600
        self.assertEqual(self.st.archive_free_leases(), 1)
        self.assertEqual(sorted(r["name"] for r in self.st.list()), ["land/r", "red/recent"])
        [ev] = [e for e in self.st.events() if e["kind"] == "lease.archived"]
        self.assertEqual((ev["item_id"], ev["refs"]["meta"]["outcome"]), ("red/old", "GAVE UP: x"))
        self.assertEqual(self.st.archive_free_leases(), 0)
        again = self.st.acquire("red/old", "fixer", 120, 300)  # the name is free to take again
        self.assertNotIn("held", again)

    def test_lease_list_shows_the_live_and_the_lately_freed_unless_all(self):
        import contextlib, io, types
        now = A.time.time()
        st = A.Store()
        a = st.acquire("t/live", "me", 600, 900)
        b = st.acquire("t/recent", "me", 600, 900)
        st.release("t/recent", b["epoch"], b["secret"])
        c = st.acquire("t/old", "me", 1, 900)
        st.release("t/old", c["epoch"], c["secret"])
        conn = st.db()
        conn.execute("UPDATE leases SET expires_at=? WHERE name='t/old'", (now - 2 * 3600,))
        conn.close()
        for all_, want in ((False, ["t/live", "t/recent"]), (True, ["t/live", "t/old", "t/recent"])):
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                A.cmd_lease(types.SimpleNamespace(prefix="t/", all=all_))
            self.assertEqual(sorted(l.split()[0] for l in out.getvalue().splitlines() if l.startswith("t/")), want)
            self.assertEqual("not shown: --all" in out.getvalue(), not all_)
        self.assertTrue(a)


class CheckLeaseTest(unittest.TestCase):
    """Check slots and a change's check are leases (phase 1 step 4): a checker that stops progressing frees both."""
    def test_slots_are_leases_and_a_live_checker_blocks_a_second(self):
        d = tempfile.mkdtemp()
        os.environ["AGENTCTL_CHECK_TREES"] = "2"
        try:
            a, b = A.check_tree_slot("/r", d), A.check_tree_slot("/r", d)
            self.assertEqual((a[0], b[0]), (1, 2))
            self.assertIsNone(A.check_tree_slot("/r", d))  # both busy
            a[1].release()
            c = A.check_tree_slot("/r", d)
            self.assertEqual(c[0], 1)
            for x in (b, c):
                x[1].release()
        finally:
            os.environ.pop("AGENTCTL_CHECK_TREES")
        one = A.checker_lease("e9")
        self.assertTrue(one.acquire())
        self.assertTrue(A.checker_alive("e9"))
        self.assertFalse(A.checker_lease("e9").acquire())  # a second checker of the same entry steps aside
        self.assertEqual(one.renew("p1"), "ok")
        one.release()
        self.assertIsNone(A.checker_alive("e9"))


if __name__ == "__main__":
    unittest.main()
