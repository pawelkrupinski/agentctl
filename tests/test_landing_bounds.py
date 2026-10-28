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
        rc, out = A.run_watched([script], d, dict(os.environ), os.path.join(d, "log"), "push", stall=1)
        self.assertIsNone(rc)  # stalled: its group ended
        self.assertLess(time.monotonic() - t0, 30)
        self.assertEqual(A.land_failure_cause("NOT LANDED: the push failed (pre-push hook or remote)", out.splitlines())[0],
                         "infra")
        self.assertTrue(A.TRANSIENT_PUSH.search(out))
        rc, _out = A.run_watched(["sh", "-c", "while :; do echo tick; sleep 0.2; done"], d, dict(os.environ),
                                 os.path.join(d, "log2"), "push", stall=60, wall=2)
        self.assertIsNone(rc)  # busy forever: the wall bound ends it


class StaleLockTest(unittest.TestCase):
    def test_a_holder_that_stopped_beating_is_ended_and_the_lock_freed(self):
        d = tempfile.mkdtemp()
        path = os.path.join(d, "repo.lock")
        old = int(time.time()) - 3600
        code = ("import fcntl, time, sys  # agentctl lock holder\n"
                f"f = open({path!r}, 'a+'); fcntl.flock(f, fcntl.LOCK_EX)\n"
                f"f.seek(0); f.truncate(); f.write('pid %d /repo since x; now: push; beat {old}' % __import__('os').getpid()); f.flush()\n"
                "print('held', flush=True); time.sleep(300)\n")
        p = subprocess.Popen([sys.executable, "-c", code], stdout=subprocess.PIPE, text=True, start_new_session=True)
        try:
            self.assertEqual(p.stdout.readline().strip(), "held")
            with open(path) as f:
                holder = f.read()
            said = []
            self.assertTrue(A.break_stale_lock(path, holder, why_log=said.append))
            self.assertIn("has done nothing for", said[0])
            p.wait(timeout=20)
            with open(path, "a+") as f:
                fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)  # free now
            fresh = holder.replace(f"beat {old}", f"beat {int(time.time())}")
            self.assertFalse(A.break_stale_lock(path, fresh, why_log=said.append))  # a beating holder is left alone
        finally:
            if p.poll() is None:
                p.kill()


class BatchRetryTest(unittest.TestCase):
    def test_three_failed_landings_hold_the_batch_with_a_condition(self):
        d = tempfile.mkdtemp()
        saved = A.run_probe, A.open_condition
        opened = []
        A.open_condition = lambda kind, text, probe, acts, **kw: opened.append((kind, text, acts)) or {"key": kw.get("key")}
        try:
            self.assertEqual(A.batch_retry(d, "a" * 40, "its push failed", 3), "retry")
            self.assertEqual(A.batch_retry(d, "a" * 40, "its push failed", 3), "retry")
            self.assertEqual(A.batch_retry(d, "a" * 40, "its push failed", 3), "held")
        finally:
            A.run_probe, A.open_condition = saved
        self.assertGreater(A.batch_state(d)["held_until"], time.time())
        self.assertEqual(opened[0][0], "STALLED")
        self.assertIn("did not land 3 times", opened[0][1])
        self.assertEqual(opened[0][2], [("clear_hold", {"d": d})])
        self.assertEqual(A.batch_retry(d, None), "retry")  # landed: the count clears

    def test_bisect_rechecks_its_culprit_before_rejecting(self):
        d = tempfile.mkdtemp()
        batch = [{"id": str(i), "tip": f"t{i}"} for i in range(4)]
        results = iter([(False, "full fails", False), (False, "t1 fails", False), (False, "t0 fails", False),
                        (True, "t0 passes when checked again", False)])
        saved = A.batch_check, A.batch_tree, A.waiting_state
        A.batch_check = lambda *a, **k: next(results)
        A.batch_tree = lambda repo, tip: d
        A.waiting_state = lambda repo: (d, d, d)
        try:
            culprit, _out = A.bisect(d, d, batch, "base")
        finally:
            A.batch_check, A.batch_tree, A.waiting_state = saved
        self.assertIsNone(culprit)  # flaky: nobody rejected


class RequeueTest(unittest.TestCase):
    def test_an_infra_requeue_resets_attempts_and_is_bounded(self):
        d = tempfile.mkdtemp()
        os.makedirs(os.path.join(d, "entries"))
        e = {"id": "e1", "status": "checking", "attempts": 3}
        saved = A.save_entry, A.wlog
        A.save_entry = lambda d_, e_: None
        A.wlog = lambda d_, m: None
        try:
            for _ in range(A.QUEUE_INFRA_RETRIES):
                self.assertTrue(A.requeue_infra(d, e, "the push failed on infra (closed by remote host)"))
                self.assertEqual((e["status"], e["attempts"]), ("queued", 0))
            self.assertFalse(A.requeue_infra(d, e, "again"))  # bounded: the caller says NOT LANDED
        finally:
            A.save_entry, A.wlog = saved


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
