"""A RED FIX NEVER WAITS FOR GREEN (the owner, 2026-11-16: "Red fix should never wait for green build (obv)").

2026-11-16, Main #2515 (red e1be6bd9fd46): its one-commit fix took ~4 h to land.
  - The main-tests owner's `agentctl land` skipped the queue (a dispatched fixer landed in-process) and ran the whole
    pre-push hook on the laptop: its identitydrift step dispatched all ten drift rows into the starved measure queue
    and waited 75 min for no verdict; the fixer's own `timeout` killed the land twice (40 and 90 min) and its 180-min
    turn ran out. The missing `Drift-Slice:` trailer the 45-s fast precheck refuses was never seen.
  - The same change, landed through the queue at 08:34Z, waited 5 + 17 min for a check slot behind ordinary changes.
So: a fixer's land QUEUES like every land (the CI tier, the fast precheck -- run in the foreground for a fixer, so its
refusal is the fixer's NOT LANDED in seconds), then waits for its verdict; a red fix takes a check slot no ordinary
change may take, goes before an open batch, is never sent to `waiting` by a known red, and the watchdog escalates one
that waits anyway (WR)."""
import os, sys, unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from test_agentctl import A  # noqa: E402
from fake_clock import on_clock  # noqa: E402
import test_queue  # noqa: E402
from test_waiting import assert_queued_line_names_no_verdict  # noqa: E402


class RedFixLandsFastTest(test_queue.QueueTest):
    def setUp(self):
        super().setUp()
        self.saved_env = {k: os.environ.get(k) for k in ("AGENTCTL_RED_FIXER", "CLAUDE_CODE_SESSION_ID")}
        self.saved_fast = A.fast_precheck_spawn
        self.spawned = []
        A.fast_precheck_spawn = lambda argv, **kw: self.spawned.append(argv)

    def tearDown(self):
        A.fast_precheck_spawn = self.saved_fast
        for k, v in self.saved_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        super().tearDown()

    def as_fixer(self):
        self.red()
        os.environ["AGENTCTL_RED_FIXER"] = "7"
        os.environ.pop("CLAUDE_CODE_SESSION_ID", None)
        self.assertTrue(A.in_fixer())

    def red(self):
        self.reds = {"7"}  # Main #7 is red; `Fixes-Red: 7` names it

    def entry(self, eid):
        d, _c, _w = A.waiting_state(self.r.path)
        return A.read_record(os.path.join(d, "entries", f"{eid}.json"))

    def only_entry_id(self):
        d, _c, _w = A.waiting_state(self.r.path)
        es = A.waiting_entries(d, open_only=False)
        self.assertEqual(len(es), 1, es)
        return es[0]["id"]

    def drive_while_awaiting(self):
        """The processor, run whenever the fixer's await sleeps: the queued change's checks, then its push."""
        def sleep(_s):
            d, _c, _w = A.waiting_state(self.r.path)
            for e in A.waiting_entries(d, open_only=False):
                if e["status"] in ("queued", "checking"):
                    A.check_entry(self.r.path, e["id"], *on_clock(self.clock))
                elif e["status"] == "checked":
                    A.push_change(self.r.path, d, e, *on_clock(A.Clock()))
        self.clock.on_sleep = sleep

    # -- the fixer's land ---------------------------------------------------------------------------------------------
    def test_a_fixers_land_queues_like_any_land_and_ends_at_its_verdict(self):
        self.as_fixer()
        wt, _ = self.agent("fix", "web/src/main/A.scala", "a\n", msg="the fix\n\nFixes-Red: 7")
        self.drive_while_awaiting()
        rc, out = self.land(wt)
        self.assertEqual(rc, 0, out)
        self.assertIn("QUEUED ", out, "the fixer's change went through the queue")
        assert_queued_line_names_no_verdict(self, out)
        self.assertNotIn("END YOUR TURN", out, "a fixer's land waits for the verdict itself")
        self.assertNotIn("agentctl: checking", out, "land itself ran no hook dry run (the queue's checker did)")
        self.assertRegex(out.strip().splitlines()[-1], r"^LANDED [0-9a-f]{40}", "the wrapper reads LANDED")
        self.assertEqual(self.remote_log()[0], "the fix")

    def test_a_fixers_land_runs_the_fast_precheck_first_and_its_refusal_is_the_verdict(self):
        """09:33 local: the precheck refused the missing Drift-Slice: trailer in 45 s -- the fixer, inside a 90-min
        local hook, never saw it."""
        self.as_fixer()
        wt, _ = self.agent("fix", "web/src/main/A.scala", "a\n", msg="the fix\n\nFixes-Red: 7")
        self.r.write("scripts/land-precheck", "#!/bin/sh\necho 'PRECHECK FAIL: rules (1 s)'\nexit 1\n", at=wt)
        os.chmod(os.path.join(wt, "scripts/land-precheck"), 0o755)
        self.r.commit("the fix's precheck", at=wt)
        self.clock.on_sleep = lambda s: self.fail("a refused fix waits for nothing")
        rc, out = self.land(wt)
        self.assertEqual(rc, 1, out)
        self.assertEqual(self.spawned, [], "a fixer's precheck runs in the foreground, not detached")
        last = out.strip().splitlines()[-1]
        self.assertTrue(last.startswith("NOT LANDED: the fast precheck refused"), last)
        self.assertIn("PRECHECK FAIL: rules", last)
        self.assertEqual(self.entry(self.only_entry_id())["status"], "cancelled")
        self.assertEqual(self.pushed(), [])

    def test_a_fixers_rejected_change_says_why(self):
        self.as_fixer()
        wt, _ = self.agent("fix", "BROKEN", "x\n", msg="the fix\n\nFixes-Red: 7")
        self.drive_while_awaiting()
        rc, out = self.land(wt)
        self.assertEqual(rc, 1, out)
        last = out.strip().splitlines()[-1]
        self.assertTrue(last.startswith("NOT LANDED: the pre-push checks failed"), last)

    def test_a_fixers_await_is_bounded(self):
        self.as_fixer()
        os.environ["AGENTCTL_FIXER_LAND_WAIT"] = "0"
        try:
            wt, _ = self.agent("fix", "web/src/main/A.scala", "a\n", msg="the fix\n\nFixes-Red: 7")
            self.clock.on_sleep = None  # nothing drives the queue
            rc, out = self.land(wt)
        finally:
            os.environ.pop("AGENTCTL_FIXER_LAND_WAIT", None)
        self.assertEqual(rc, 1, out)
        last = out.strip().splitlines()[-1]
        self.assertTrue(last.startswith("NOT LANDED (still queued):"), last)
        self.assertIn("its red's fix", last)

    def test_the_fixer_prompt_says_land_queues_and_waits_and_never_to_wrap_or_run_the_hook(self):
        u = {"workflow": "Main", "run": {"number": 2515, "databaseId": 43806871866}, "job": "ci / test", "kind": "test"}
        p = A.fixer_prompt(u, "/wt", "/brief")
        self.assertIn("QUEUES your fix", p)
        self.assertIn("Never wrap it in `timeout`", p)
        self.assertIn("never run scripts/hooks/pre-push yourself", p)
        self.assertNotIn("land --follow", p)

    # -- the queue ----------------------------------------------------------------------------------------------------
    def busy_slots(self):
        d, _c, _w = A.waiting_state(self.r.path)
        held = []
        for i in range(1, A.QUEUE_POOL + 1):
            h = A.tree_slot(d, i)
            self.assertTrue(h.acquire())
            held.append(h)
        return d, held

    def tick(self, d):
        started = []
        saved = A.spawn_executor
        A.spawn_executor = lambda repo, d_, eid: started.append(eid)
        try:
            return A.tick_changes(self.r.path, d, *on_clock(A.Clock())), started
        finally:
            A.spawn_executor = saved

    def test_a_red_fix_takes_a_check_slot_no_ordinary_change_may_take(self):
        """08:58-09:15Z: 7d2a7fe09's verify round sat `queued for 15 min, waiting on a check slot`."""
        self.red()
        d, held = self.busy_slots()
        try:
            # (tools only: a production change joins the waiting batch unchecked while red, THE BATCH IS THE MECHANISM)
            wo, _ = self.agent("plain", "scripts/b.sh", "b\n", msg="an ordinary change")
            eo, _ = self.queued(wo)
            wf, _ = self.agent("fix", "web/src/main/A.scala", "a\n", msg="the fix\n\nFixes-Red: 7")
            ef, _ = self.queued(wf)
            _did, started = self.tick(d)
            self.assertEqual(started, [ef], "the fix starts in the red-fix slot; the ordinary change waits")
            A.check_entry(self.r.path, eo, *on_clock(self.clock))
            self.assertEqual(self.entry(eo)["status"], "queued", "an ordinary change never takes the red-fix slot")
            A.check_entry(self.r.path, ef, *on_clock(self.clock))
            self.assertEqual(self.entry(ef)["status"], "checked", "the fix's executor took the red-fix slot")
        finally:
            for h in held:
                h.release()

    def test_a_checked_red_fix_pushes_before_an_open_batch(self):
        self.red()
        d, _c, _w = A.waiting_state(self.r.path)
        wa, _ = self.agent("a", "web/src/main/C.scala", "c\n", msg="a: waits for green")
        ea, _ = self.queued(wa)
        self.check(ea)  # red: it joins `waiting`
        st = A.open_store()
        A.new_batch(st, d, "checking")
        wf, _ = self.agent("fix", "web/src/main/A.scala", "a\n", msg="the fix\n\nFixes-Red: 7")
        ef, _ = self.queued(wf)
        A.check_entry(self.r.path, ef, *on_clock(self.clock))
        self.assertEqual(self.entry(ef)["status"], "checked")
        did, _ = self.tick(d)
        self.assertNotIn("waits for batch", " ".join(did))
        self.assertEqual(self.entry(ef)["status"], "landed", "the red's fix goes first")

    def test_a_red_fix_failing_only_as_main_fails_lands_rather_than_waiting_for_green(self):
        self.red()
        d, _c, _w = A.waiting_state(self.r.path)
        wf, _ = self.agent("fix", "web/src/main/A.scala", "a\n", msg="the fix\n\nFixes-Red: 7")
        ef, _ = self.queued(wf)
        A.check_entry(self.r.path, ef, *on_clock(self.clock))
        e = self.entry(ef)
        e["known_red"] = {"keys": ["Local checks"], "where": ["plain main"], "base": e.get("checked_base")}
        A.save_entry(d, e)
        A.push_change(self.r.path, d, e, *on_clock(A.Clock()))
        self.assertEqual(self.entry(ef)["status"], "landed", "a red fix is never parked on `waiting` for green")

    def test_an_ordinary_change_failing_only_as_main_fails_still_waits(self):
        self.red()
        d, _c, _w = A.waiting_state(self.r.path)
        wo, _ = self.agent("plain", "web/src/main/A.scala", "a\n", msg="an ordinary change")
        eo, _ = self.queued(wo)
        A.check_entry(self.r.path, eo, *on_clock(self.clock))
        e = self.entry(eo)
        e["known_red"] = {"keys": ["Local checks"], "where": ["plain main"], "base": e.get("checked_base")}
        A.save_entry(d, e)
        A.push_change(self.r.path, d, e, *on_clock(A.Clock()))
        self.assertEqual(self.entry(eo)["status"], "waiting")

    # -- the invariant ------------------------------------------------------------------------------------------------
    def test_watchdog_wr_escalates_a_red_fix_waiting_on_green_or_queued_too_long(self):
        st = A.open_store()
        st.put_item("change", "c-fix", "waiting", {"fixes": ["7"], "submitted_at": A.time.time()})
        ok, why = A.inv_wr(A.Clock(), st=st, reds={"7"})
        self.assertFalse(ok, why)
        self.assertIn("c-fix", why)
        self.assertIn("waiting for main to turn green", why)
        st2 = A.Store(path=os.path.join(self.tmp.name, "store2.db"))
        st2.put_item("change", "c-slow", "queued", {"fixes": ["7"], "submitted_at": A.time.time() - 3600})
        st2.put_item("change", "c-plain", "waiting", {"submitted_at": A.time.time() - 3600})
        ok, why = A.inv_wr(A.Clock(), st=st2, reds={"7"})
        self.assertFalse(ok, why)
        self.assertIn("c-slow", why)
        self.assertNotIn("c-plain", why, "an ordinary change may wait for green")
        ok, why = A.inv_wr(A.Clock(), st=st2, reds=set())
        self.assertTrue(ok, "main green: no red fix to hold")


if __name__ == "__main__":
    unittest.main()
