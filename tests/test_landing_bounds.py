"""Bounded retries and bounded holds (the owner, 2026-10-28: "review and fix landing code ... so things don't get missed
or stalled", "fix root cause in a structural way"): a push that hangs after its transport died is killed within its
bound and is infra, a landing lock whose holder stopped working is freed, a batch that cannot land is HELD with a
condition after 3 tries, bisect never rejects on one flaky failure, an infra re-queue resets `attempts`, and the
governor's wait for a check is bounded."""
import contextlib, fcntl, io, json, os, subprocess, sys, tempfile, time, unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from test_agentctl import A  # noqa: E402
from fake_clock import FakeClock, on_clock  # noqa: E402


class PollClock:
    """THE LEASE TTL ON AN INJECTED CLOCK: the store and the stall judge read one time that moves a poll per poll, never
    the wall's. A 2-3 s TTL on the wall's time lost the lease whenever the loaded suite delayed one poll past it (the
    delayed-poll cases below prove it): the test then judged the machine's load, not the renewal. The time moves in
    run_watched's sampler (called once a poll, before the renewal); `gate` (a file) is created once it passes
    `open_at`, so a step that waits for it is quiet for exactly that long in this time."""
    def __init__(self, step, gate=None, open_at=None, delay=None):
        self.t, self.step, self.gate, self.open_at, self.delay = 1000.0, step, gate, open_at, delay

    def now(self):  # the store's and the stall judge's: reading the time never moves it
        return self.t

    def poll(self):  # run_watched's sampler: one step a poll (after `delay`, the wall's seconds, on the first)
        if self.delay:
            time.sleep(self.delay)
            self.delay = None
        self.t += self.step
        if self.gate and self.t >= self.open_at and not os.path.exists(self.gate):
            open(self.gate, "w").close()
        return self.t


def wait_for(gate):
    """A step that waits, quiet, until `gate` exists."""
    return f"while [ ! -e {gate} ]; do sleep 0.1; done"



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


    def test_a_silent_busy_child_in_a_process_group_of_its_own_is_progress(self):
        """ISSUE 20261108T232313-7384d0: twice 'NOT LANDED: stalled push ... no output and no CPU for 20 min' while the
        hook's scripttests ran -- run-script-tests runs each test in a process group of its own (stall-watch's
        KINOWO_STALL_GROUPS), so the push's group CPU never saw them. The whole process TREE is the step's work."""
        d = tempfile.mkdtemp()
        script = os.path.join(d, "hook.py")
        with open(script, "w") as f:
            f.write("import os, subprocess, sys, time\n"
                    "c = subprocess.Popen([sys.executable, '-c', 'import time\\nt=time.time()\\n"
                    "while time.time()-t<8: pass'], start_new_session=True)\n"
                    "c.wait()\nprint('done')\n")
        # THE STALL JUDGE ON AN INJECTED CLOCK: one second a poll, never the wall's -- under a laptop load of 70 the
        # real seconds passed while the busy child waited for a CPU, and it read as stalled. Each poll sees the child
        # (another session, the step's tree) running or runnable: progress, whatever the load.
        ticks = iter(range(10 ** 6))
        rc, out = A.run_watched([sys.executable, script], d, dict(os.environ), os.path.join(d, "log"), "push",
                                stall=3, clock=lambda: float(next(ticks)))
        self.assertEqual(rc, 0, out)
        self.assertIn("done", out)

    def test_a_remote_side_internal_server_error_is_a_transient_push_failure_named_as_the_remotes(self):
        """ISSUE 20261104T104042-83b1c0: the remote's `fatal error in commit_refs` / `[remote rejected] ... (Internal Server
        Error)` is the forge's incident, not the change's or the hook's: it is classified with the other transient push
        failures (so the queue's retry says why, `the push failed (exit 1)` hides it) and never RACE or REDGATE."""
        out = ("remote: fatal error in commit_refs\n"
               "To github.com:o/movies.git\n ! [remote rejected] abc123 -> main (Internal Server Error)\n"
               "error: failed to push some refs to 'github.com:o/movies.git'\n")
        self.assertTrue(A.TRANSIENT_PUSH.search(out), "an Internal Server Error from the remote is not recognised")


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


class PushHookTest(unittest.TestCase):
    """2026-11-16 ~20:18 CET: movies 040934987's first land lost its landing lease 3 min into the push and ended "stalled
    push ... no output and no CPU for 20 min". The check outside the lock had passed, but a gate run completed meanwhile,
    so the push's pre-push hook could not reuse that pass and ran its checks again: minutes with no output and its CPU
    out of the push's process group (a kept sbt server, a Docker container), past the lease's 120 s TTL. The hook
    bounds itself (movies: stall-watch, 1200 s), so while it runs the push renews its leases and is judged by the
    hook's bound (the check outside the lock's); only the transport after it gets the strict push bound."""
    def hook_push(self, d, hook_secs, after="echo pushed", hook_body=None):
        os.makedirs(os.path.join(d, "hooks"))
        hook = os.path.join(d, "hooks", "pre-push")
        with open(hook, "w") as f:
            f.write(f"#!/bin/sh\necho checking\n{hook_body or f'sleep {hook_secs}'}\n")
        os.chmod(hook, 0o755)
        script = os.path.join(d, "push.sh")
        with open(script, "w") as f:
            f.write(f"#!/bin/sh\n{hook} origin url || exit 1\n{after}\n")
        os.chmod(script, 0o755)
        return script

    def test_a_quiet_pre_push_hook_keeps_the_landing_lease_past_its_ttl_and_the_push_bound(self):
        self.quiet_hook_keeps_the_lease()

    def test_a_quiet_hook_keeps_the_landing_lease_when_the_machine_delays_a_poll_past_its_ttl(self):
        self.quiet_hook_keeps_the_lease(delay=3)

    def quiet_hook_keeps_the_lease(self, delay=None):
        d = tempfile.mkdtemp()
        gate = os.path.join(d, "go")
        clock = PollClock(0.75, gate, 1000.0 + 7, delay)  # the hook quiet for 7 s: past the TTL and stall=3
        st = A.Store(path=os.path.join(d, "store.db"), clock=clock.now)
        h = A.LeaseHold("land/x", 2, 300, store=st)
        self.assertTrue(h.acquire())
        A._landing[0] = h
        try:
            rc, out = A.run_watched([self.hook_push(d, 0, hook_body=wait_for(gate))], d, dict(os.environ),
                                    os.path.join(d, "log"), "push", stall=3, wall=120, hook_stall=60, clock=clock.now,
                                    sampler=clock.poll)
            self.assertEqual(rc, 0, f"a quiet hook within its own bound ended the push: {out}")
            self.assertIn("pushed", out)
            self.assertEqual(h.renew("after"), "ok", "the landing lease is still this lander's")
            self.assertGreater(clock.now() - 1000.0, 2 * 2, "quiet past twice the lease's TTL")
        finally:
            A._landing[0] = None
            h.release()

    def test_a_transport_hanging_after_the_hook_still_loses_the_lease(self):
        d = tempfile.mkdtemp()
        st = A.Store(path=os.path.join(d, "store.db"))
        h = A.LeaseHold("land/x", 2, 300, store=st)
        self.assertTrue(h.acquire())
        A._landing[0] = h
        try:
            t0 = time.monotonic()
            rc, out = A.run_watched([self.hook_push(d, 0, "exec sleep 300")], d, dict(os.environ),
                                    os.path.join(d, "log"), "push", stall=600, wall=900, hook_stall=600)
        finally:
            A._landing[0] = None
            h.release()
        self.assertIsNone(rc)
        self.assertLess(time.monotonic() - t0, 60)
        self.assertIn("lease land/x", A.stall_reason(out))

    def test_a_stalled_verdict_says_why_it_ended_not_a_bound_it_never_used(self):
        d = tempfile.mkdtemp()
        rc, out = A.run_watched(["sleep", "30"], d, dict(os.environ), os.path.join(d, "log"), "push", stall=1)
        self.assertIsNone(rc)
        self.assertEqual(A.stall_reason(out), "no output and no CPU for 1 s")
        o = io.StringIO()
        with contextlib.redirect_stdout(o):
            A.stalled("push (git push and its pre-push hook)", out, os.path.join(d, "log"))
        self.assertIn("NOT LANDED: stalled push (git push and its pre-push hook): no output and no CPU for 1 s, so it was "
                      "ended", o.getvalue())
        self.assertNotIn("20 min", o.getvalue())

    def test_every_push_is_judged_by_the_hooks_bound_while_its_hook_runs(self):
        self.assertEqual(A.push_bounds()["hook_stall"], A.LAND_STALL_MIN * 60)


class QuietStepTest(unittest.TestCase):
    """2026-10-30: the batch's drift check (identity-drift-ci: a header, then silence while its CI run goes on) lost the
    executor's leases at their 10-min TTL every time -- no verdict, and its check tree went to a change."""
    def test_a_quiet_ok_step_renews_at_every_poll_and_a_plain_one_only_on_output_or_cpu(self):
        self.assertEqual(A.beat_mark("push", 10, 2.7, 1), A.beat_mark("push", 10, 2.7, 2))
        self.assertNotEqual(A.beat_mark("drift", 10, 2.7, 1, True), A.beat_mark("drift", 10, 2.7, 2, True))

    def test_a_quiet_ok_step_keeps_its_lease_past_the_ttl(self):
        self.quiet_step_keeps_the_lease()

    def test_a_quiet_ok_step_keeps_its_lease_when_the_machine_delays_a_poll_past_the_ttl(self):
        self.quiet_step_keeps_the_lease(delay=4)

    def quiet_step_keeps_the_lease(self, delay=None):
        d = tempfile.mkdtemp()
        gate = os.path.join(d, "go")
        clock = PollClock(2.0, gate, 1000.0 + 6, delay)  # quiet for 6 s of the clock: twice the TTL, within stall=8
        st = A.Store(path=os.path.join(d, "store.db"), clock=clock.now)
        h = A.LeaseHold("check/batch-x#1", 3, 300, store=st)
        self.assertTrue(h.acquire())
        try:
            rc, _out = A.run_watched(["sh", "-c", wait_for(gate)], d, dict(os.environ), os.path.join(d, "log"),
                                     "drift", stall=8, quiet_ok=True, clock=clock.now, sampler=clock.poll)
            self.assertEqual(rc, 0, "quiet for twice the TTL, within its stall bound: it ran to the end")
            self.assertEqual(h.renew("after"), "ok", "the lease is still this holder's")
            self.assertGreaterEqual(clock.now() - 1000.0, 2 * 3, "quiet for twice the lease's TTL")
        finally:
            h.release()


class ExitedWhileJudgedTest(unittest.TestCase):
    """2026-11-22 (a pre-push at load 290): a quiet step's verdict came after it had exited by itself; ending its group
    then raised PermissionError (macOS's EPERM for a group of unreaped exits) out of run_watched."""
    def test_a_step_that_exits_while_it_is_judged_ended_by_itself(self):
        d = tempfile.mkdtemp()
        calls, t = [0], [0.0]

        def cpu(pgid):
            calls[0] += 1
            if calls[0] == 2:  # the step exits while this poll judges it
                deadline = time.time() + 30
                while time.time() < deadline and subprocess.run(["ps", "-o", "stat=", "-p", str(pgid)],
                                                                capture_output=True, text=True).stdout.strip()[:1] \
                        not in ("Z", ""):
                    time.sleep(0.05)
            return 0.0

        def clock():
            t[0] += 100.0  # every reading is past the stall bound
            return t[0]
        rc, _out = A.run_watched(["sleep", "1"], d, dict(os.environ), os.path.join(d, "log"), "check", stall=0.8,
                                 cpu=cpu, clock=clock)
        self.assertEqual(rc, 0, "it exited by itself: no stall, no group to end")

    def test_ending_a_group_whose_members_all_exited_is_no_error(self):
        p = subprocess.Popen(["true"], start_new_session=True)
        deadline = time.time() + 30
        while time.time() < deadline and subprocess.run(["ps", "-o", "stat=", "-p", str(p.pid)], capture_output=True,
                                                        text=True).stdout.strip()[:1] not in ("Z", ""):
            time.sleep(0.05)
        saved = A.os.killpg

        def eperm(pgid, sig):
            raise PermissionError(1, "Operation not permitted")
        A.os.killpg = eperm
        try:
            A._end_group(p)  # raised PermissionError before 2026-11-22
        finally:
            A.os.killpg = saved
        self.assertIsNotNone(p.poll())


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
            st = A.process_store()
            subprocess.run(["git", "init", "-q", d], check=True)
            name = A.land_lease_name(d)
            st.acquire(name, "other:land:1", 600, 3600)
            t0 = time.monotonic()
            with A.land_lock_if_free(d, A.CurrentStore(A.process_store)) as held:
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
            culprit, _out = A.bisect(d, d, batch, "base", d, FakeClock(), A.CurrentStore(A.process_store))
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
        saved_wlog = A.wlog
        A.wlog = lambda d_, m: None
        try:
            waits = []
            for _ in range(4):
                A.requeue_infra(st, d, e, "the push failed on infra (closed by remote host)", time.time())
                waits.append(round(e["retry_after"] - time.time()))
                self.assertEqual(e["status"], "queued")
                st.transition("change", "e1", "checking", st.item("change", "e1")["version"])
                e["status"] = "checking"
        finally:
            A.wlog = saved_wlog
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

    def test_land_says_not_landed_when_the_governor_never_admits_its_check(self):
        import contextlib, io

        @contextlib.contextmanager
        def never(*a, **k):
            raise A.GovTimeout("the governor did not admit check work (land x) in 120 min: no memory")
            yield
        saved = A.check_slot
        A.check_slot = never
        out = io.StringIO()
        try:
            with tempfile.TemporaryDirectory() as d, contextlib.redirect_stdout(out):
                rc = A.dry_run(d, "/bin/true", "a" * 40, os.path.join(d, "log"), 1, *on_clock(A.Clock()))
        finally:
            A.check_slot = saved
        self.assertEqual(rc, 1)
        self.assertIn("NOT LANDED: the governor did not admit", out.getvalue())


if __name__ == "__main__":
    unittest.main()
