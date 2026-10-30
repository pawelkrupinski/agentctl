"""Bounded retries and bounded holds (the owner, 2026-10-28: "review and fix landing code ... so things don't get missed
or stalled", "fix root cause in a structural way"): a push that hangs after its transport died is killed within its
bound and is infra, a landing lock whose holder stopped working is freed, a batch that cannot land is HELD with a
condition after 3 tries, bisect never rejects on one flaky failure, an infra re-queue resets `attempts`, and the
governor's wait for a check is bounded."""
import fcntl, json, os, subprocess, sys, tempfile, time, unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from test_agentctl import A  # noqa: E402


class PushBoundTest(unittest.TestCase):
    def test_a_push_hanging_after_its_transport_died_is_killed_within_its_bound_and_is_infra(self):
        d = tempfile.mkdtemp()
        script = os.path.join(d, "push.sh")
        with open(script, "w") as f:
            f.write("#!/bin/sh\necho 'Connection to localhost closed by remote host.'\nexec sleep 300\n")
        os.chmod(script, 0o755)
        t0 = time.monotonic()
        # stall 5 s, not 1: at load 25 the shell had not even printed its line within a second, so the output read
        # empty (2026-10-29)
        rc, out = A.run_watched([script], d, dict(os.environ), os.path.join(d, "log"), "push", stall=5)
        self.assertIsNone(rc)  # stalled: its group ended
        self.assertLess(time.monotonic() - t0, 90)  # generous: the full suite loads the machine
        self.assertTrue(A.TRANSIENT_PUSH.search(out))
        rc, _out = A.run_watched(["sh", "-c", "while :; do echo tick; sleep 0.2; done"], d, dict(os.environ),
                                 os.path.join(d, "log2"), "push", stall=60, wall=2)
        self.assertIsNone(rc)  # busy forever: the wall bound ends it


class LeaseTest(unittest.TestCase):
    """The store's leases (design §6) and the landing lease (step 3): a holder that stops progressing loses it."""
    def test_a_push_hanging_under_the_landing_lease_loses_it_and_ends(self):
        d = tempfile.mkdtemp()
        st = A.Store(path=os.path.join(d, "store.db"))
        h = A.LeaseHold("land/x", 2, 300, store=st)
        self.assertTrue(h.acquire())
        A._landing[0] = h
        script = os.path.join(d, "push.sh")
        with open(script, "w") as f:
            f.write("#!/bin/sh\necho 'Connection to localhost closed by remote host.'\nexec sleep 300\n")
        os.chmod(script, 0o755)
        try:
            t0 = time.monotonic()
            rc, _out = A.run_watched([script], d, dict(os.environ), os.path.join(d, "log"), "push", stall=600)
        finally:
            A._landing[0] = None
            h.release()
        self.assertIsNone(rc)  # the lease lapsed (no progress): its group ended, long before the stall bound
        self.assertLess(time.monotonic() - t0, 60)
        self.assertNotIn("held", st.acquire("land/x", "the batch", 120, 300))  # the next lander takes it at once


class UnboundTest(unittest.TestCase):
    def test_a_store_error_mid_wait_ends_the_child_group_and_propagates(self):
        """run_watched caught only LeaseLost: a StoreUnavailable escaped with the push still running, untracked."""
        with tempfile.TemporaryDirectory() as d:
            calls = []

            def boom():
                calls.append(1)
                if len(calls) == 2:
                    raise KeyError("a bug in the sampler")
            with self.assertRaises(KeyError):
                A.run_watched(["sh", "-c", "echo $$ > pid; exec sleep 60"], d, dict(os.environ), os.path.join(d, "log"),
                              "push", stall=5, sampler=boom)
            pid = int(open(os.path.join(d, "pid")).read())
            with self.assertRaises(ProcessLookupError):
                os.killpg(pid, 0)
            self.assertNotIn(pid, A.WATCHED_GROUPS)

    def test_the_ticks_push_never_waits_for_a_busy_landing_lease(self):
        with tempfile.TemporaryDirectory() as d:
            st = A.Store()
            subprocess.run(["git", "init", "-q", d], check=True)
            name = A.land_lease_name(d)
            st.acquire(name, "other:land:1", 600, 3600)
            t0 = time.monotonic()
            with A.land_lock(d, timeout=0) as held:
                self.assertFalse(held)
            self.assertLess(time.monotonic() - t0, 5)


class BatchRetryTest(unittest.TestCase):
    def test_three_failed_landings_hold_the_batch_with_a_condition(self):
        d = tempfile.mkdtemp()
        st = A.Store(path=os.path.join(d, "store.db"))
        saved = A.open_condition, A.wlog
        opened = []
        A.open_condition = lambda kind, text, probe, acts, **kw: opened.append((kind, text, acts)) or {"key": kw.get("key")}
        A.wlog = lambda d_, m: None
        try:
            it = A.new_batch(st, d, "pushing")
            for _ in range(2):
                it = A.batch_fail(st, d, it, "its push failed", 3)
                self.assertEqual(it["state"], "assembling")
                it = A.batch_move(st, it, "pushing")
            it = A.batch_fail(st, d, it, "its push failed", 3)
        finally:
            A.open_condition, A.wlog = saved
        self.assertEqual(it["state"], "held")
        self.assertEqual(opened[0][0], "STALLED")
        self.assertIn("did not land 3 times", opened[0][1])
        self.assertEqual(opened[0][2], [("clear_hold", {"d": d})])
        self.assertEqual(st.events()[-1]["severity"], "escalate")

    def test_bisect_rechecks_its_culprit_before_rejecting(self):
        d = tempfile.mkdtemp()
        batch = [{"id": str(i), "tip": f"t{i}"} for i in range(4)]
        results = iter([(False, "full fails", False), (False, "t1 fails", False), (False, "t0 fails", False),
                        (True, "t0 passes when checked again", False)])
        saved = A.batch_check
        A.batch_check = lambda *a, **k: next(results)
        try:
            culprit, _out = A.bisect(d, d, batch, "base", d)
        finally:
            A.batch_check = saved
        self.assertIsNone(culprit)  # flaky: nobody rejected


class RequeueTest(unittest.TestCase):
    def test_an_infra_requeue_backs_off_escalates_from_the_third_and_never_rejects(self):
        d = tempfile.mkdtemp()
        os.makedirs(os.path.join(d, "entries"))
        st = A.Store(path=os.path.join(d, "store.db"))
        e = {"id": "e1", "seq": 1, "status": "checking", "worktree": "/w"}
        A.write_json(os.path.join(d, "entries", "e1.json"), e)
        saved_store, saved_wlog = A.Store, A.wlog
        A.Store = lambda *a, **k: st
        A.wlog = lambda d_, m: None
        try:
            waits = []
            for _ in range(4):
                A.requeue_infra(d, e, "the push failed on infra (closed by remote host)")
                waits.append(round(e["retry_after"] - time.time()))
                self.assertEqual(e["status"], "queued")
                st.transition("change", "e1", "checking", st.item("change", "e1")["version"])
                e["status"] = "checking"
        finally:
            A.Store, A.wlog = saved_store, saved_wlog
        self.assertEqual([w // 60 for w in waits], [5, 10, 30, 60])
        sev = [x["severity"] for x in st.events() if x["to_state"] == "queued"]
        self.assertEqual(sev, ["info", "info", "escalate", "escalate"])


class GovBoundTest(unittest.TestCase):
    def test_the_governors_wait_for_a_check_is_bounded(self):
        saved = A.gov_fits, A.gov_waiters
        A.gov_fits = lambda *a, **k: (False, "no memory", False)
        try:
            with self.assertRaises(A.GovTimeout):
                A.gov_admit("check", 1, "t", kind="check", max_wait=0.5, report=lambda m: None)
        finally:
            A.gov_fits, A.gov_waiters = saved


if __name__ == "__main__":
    unittest.main()
