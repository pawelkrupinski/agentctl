"""PROOF OF WORK (issue 20261031T203744-4a4a59): a red lease is held by work, never by a living process. The main-tests
owner's session ended its turn `blocked` at 20:12 (after a 403 from the store) while its wrapper, waiting for `done`,
kept renewing the owner's heartbeat: the family's reds stayed "leased ... none at work" and nothing fixed them."""
import json, os, subprocess, sys, time, unittest, urllib.error
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from test_agentctl import A  # noqa: E402
from test_owner import OwnerBase, FAM  # noqa: E402


class FakeSessions:
    """`claude agents` rows written where tests/fake_claude.py reads them."""

    @staticmethod
    def row(bid, state, sid=None):
        d = os.path.join(os.environ["FAKE_CLAUDE_OUT"], "agents")
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, f"{bid}.json"), "w") as f:
            json.dump({"id": bid, "sessionId": sid or f"{bid}-0000", "name": "s", "kind": "background",
                       "state": state}, f)


class BgEndedTest(OwnerBase):
    def test_which_rows_are_ended_turns(self):
        self.assertTrue(A.bg_ended({"state": "done"}))
        self.assertTrue(A.bg_ended({"state": "stopped"}))
        self.assertTrue(A.bg_ended({"state": "blocked", "pid": None}), "the 2026-10-31 row: blocked, no process")
        self.assertFalse(A.bg_ended({"state": "blocked", "pid": os.getpid()}), "a live process: judged by progress")
        self.assertFalse(A.bg_ended({"state": "working"}))
        self.assertFalse(A.bg_ended({}), "unread: no verdict")
        self.assertFalse(A.bg_ended(None), "not listed: the wrapper's missing count decides")


class OwnerWrapperTest(OwnerBase):
    def lease_outcome(self, name):
        return str(A.lease_meta(self.st.get(name)).get("outcome") or "")

    def test_a_turn_that_ends_blocked_releases_its_reds_at_once(self):
        """`blocked` (its turn over, waiting for input that never comes) is an ended turn, not work."""
        os.environ["FAKE_CLAUDE_END_STATE"] = "blocked"
        os.environ["FAKE_CLAUDE_RESULT"] = "I could not record it as blocked."
        self.addCleanup(os.environ.pop, "FAKE_CLAUDE_END_STATE", None)
        self.addCleanup(os.environ.pop, "FAKE_CLAUDE_RESULT", None)
        self.tick(dispatch=True)
        name = A.red_lease(self.only()["data"]["key"])
        self.assertTrue(self.wait(lambda: "blocked" in self.lease_outcome(name)), self.lease_outcome(name))
        self.assertTrue(self.lease_outcome(name).startswith("ended with no LANDED"))
        self.assertFalse(A.lease_is_live(self.st.get(name), time.time()))
        self.assertTrue(self.wait(lambda: self.rec().get("in_turn") is False))

    def test_a_turn_with_no_progress_is_stalled_its_session_stopped_and_its_reds_released(self):
        os.environ.update(FAKE_CLAUDE_SLEEP="60", AGENTCTL_RED_STALL="1")
        self.addCleanup(os.environ.__setitem__, "FAKE_CLAUDE_SLEEP", "0")
        self.addCleanup(os.environ.pop, "AGENTCTL_RED_STALL", None)
        self.tick(dispatch=True)
        name = A.red_lease(self.only()["data"]["key"])
        self.assertTrue(self.wait(lambda: self.lease_outcome(name).startswith("STALLED")), self.lease_outcome(name))
        self.assertTrue(self.wait(lambda: all(s.get("stopped") or s["state"] != "working" for s in self.sessions())))
        self.assertTrue(A.red_crash_streak({"history": [{"dispatched": True, "outcome": self.lease_outcome(name)}]}),
                        "a stall is a launch with no result: the crash-loop breaker counts it")


class ReapTest(OwnerBase):
    """The laptop's tick (red_reap_orphans) on a fake clock and fake sessions: whatever a wrapper does -- the old code
    of pid 51249 included -- a holder with no work loses its leases."""

    def setUp(self):
        super().setUp()
        self.now = time.time()
        self.transcript = os.path.join(self.tmp.name, "t.jsonl")
        open(self.transcript, "w").write("{}\n")
        os.utime(self.transcript, (self.now - 3600, self.now - 3600))  # last written an hour ago

    def old_owner(self, state, in_turn=None, **kw):
        """An owner record as pid 51249's code writes it (no in_turn), its wrapper alive (this process)."""
        rec = {"family": FAM, "pid": os.getpid(), "process_start": A.process_start(os.getpid()), "bg_id": "039e6fb5",
               "session": "039e6fb5-48da", "transcript": self.transcript, "turn": 1, "worktree": self.tmp.name, **kw}
        if in_turn is not None:
            rec["in_turn"] = in_turn
        os.makedirs(A.owner_dir(FAM), exist_ok=True)
        A.write_json(A.owner_record_path(FAM), rec)
        FakeSessions.row("039e6fb5", state)
        hb = self.st.acquire(A.owner_lease(FAM), A.owner_holder(FAM), 900, 86400, meta={"kind": "owner", "family": FAM})
        reds = [self.st.acquire(f"red/{k}", A.owner_holder(FAM), 900, 86400, meta={"kind": "owned", "family": FAM})
                for k in ("4c51fe998db3", "ec55553a8060")]
        return hb, reds

    def test_an_owner_whose_session_ended_while_its_wrapper_lives_loses_every_lease(self):
        hb, reds = self.old_owner("blocked")
        self.t[0] = self.now
        acts = A.red_reap_orphans(self.st, self.now)
        self.assertEqual(len(acts), 3, acts)
        for name in [A.owner_lease(FAM), "red/4c51fe998db3", "red/ec55553a8060"]:
            row = self.st.get(name)
            self.assertFalse(A.lease_is_live(row, self.now), name)
            self.assertIn("ORPHANED", A.lease_meta(row)["outcome"])
            self.assertIn("blocked", A.lease_meta(row)["outcome"])
        # the wrapper's next beat -- old code or new -- reads lost and exits
        self.assertEqual(self.st.renew(A.owner_lease(FAM), hb["epoch"], hb["secret"], "beat 1", 900), "lost")
        self.assertTrue(A.RED_CRASH_OUTCOME.match(A.lease_meta(self.st.get("red/4c51fe998db3"))["outcome"]))

    def test_an_owner_at_work_keeps_its_leases(self):
        self.old_owner("working")
        os.utime(self.transcript, (self.now - 30, self.now - 30))
        self.assertEqual(A.red_reap_orphans(self.st, self.now), [])
        self.assertTrue(A.lease_is_live(self.st.get("red/4c51fe998db3"), self.now))

    def test_an_ended_session_within_the_grace_is_not_yet_orphaned(self):
        self.old_owner("done")
        os.utime(self.transcript, (self.now - 10, self.now - 10))  # its wrapper ends the turn within a poll
        self.assertEqual(A.red_reap_orphans(self.st, self.now), [])

    def test_an_owner_between_turns_is_not_orphaned(self):
        self.old_owner("done", in_turn=False)
        self.assertEqual(A.red_reap_orphans(self.st, self.now), [])

    def test_a_working_session_with_no_progress_past_the_stall_is_orphaned(self):
        self.old_owner("working", in_turn=True, progress_at=self.now - 3 * 3600)
        os.utime(self.transcript, (self.now - 3 * 3600, self.now - 3 * 3600))
        self.assertEqual(len(A.red_reap_orphans(self.st, self.now)), 3)

    def test_an_owner_whose_wrapper_died_is_orphaned(self):
        p = subprocess.Popen([sys.executable, "-c", "pass"])
        p.wait()
        self.old_owner("working", pid=p.pid)
        os.utime(self.transcript, (self.now, self.now))
        acts = A.red_reap_orphans(self.st, self.now)
        self.assertEqual(len(acts), 3, acts)
        self.assertIn("is gone", acts[0])

    def test_a_fixer_whose_wrapper_died_is_orphaned_and_its_session_stopped(self):
        p = subprocess.Popen([sys.executable, "-c", "pass"])
        p.wait()
        FakeSessions.row("abc46699", "working")
        self.st.acquire("red/k1", "laptop:fixer:red-k1-1", 900, 3600,
                        meta={"kind": "fixer", "pid": p.pid, "bg_id": "abc46699", "log": self.transcript})
        acts = A.red_reap_orphans(self.st, self.now)
        self.assertEqual(len(acts), 1, acts)
        self.assertFalse(A.lease_is_live(self.st.get("red/k1"), self.now))
        row = json.load(open(os.path.join(os.environ["FAKE_CLAUDE_OUT"], "agents", "abc46699.json")))
        self.assertTrue(row.get("stopped"), "a session nobody holds is stopped")

    def test_a_fixer_whose_session_ended_blocked_is_orphaned(self):
        FakeSessions.row("abc46699", "blocked")
        self.t[0] = self.now - 3600
        self.st.acquire("red/k1", "laptop:fixer:red-k1-1", 7200, 7200,
                        meta={"kind": "fixer", "pid": os.getpid(), "bg_id": "abc46699", "log": self.transcript})
        self.assertEqual(len(A.red_reap_orphans(self.st, self.now)), 1)

    def test_claims_and_other_machines_holders_are_never_judged_here(self):
        self.st.acquire("red/c1", "laptop:claim:me@/wt", 900, 3600, meta={"kind": "claim", "worktree": "/wt"})
        self.st.acquire("red/c2", "ci-runner:fixer:x", 900, 3600, meta={"kind": "fixer", "pid": 999999})
        self.assertEqual(A.red_reap_orphans(self.st, self.now), [])

    def test_the_dispatcher_waits_for_an_orphaned_owners_wrapper_to_exit(self):
        self.old_owner("blocked")
        A.red_reap_orphans(self.st, self.now)
        self.assertIn("is ending", A.owner_still_ending(self.st, FAM, self.now))
        rec = A.read_record(A.owner_record_path(FAM))
        A.write_json(A.owner_record_path(FAM), {**rec, "ended": "x"})
        self.assertIsNone(A.owner_still_ending(self.st, FAM, self.now))


class StatusTest(OwnerBase):
    def test_red_status_says_orphaned_never_leased_with_none_at_work(self):
        m = {"reds": [], "families": [{"family": FAM, "open": 2, "holder": A.owner_holder(FAM), "worktree": "/wt",
                                       "at_work": False, "work": "its session 039e6fb5 is blocked"}],
             "dispatcher": {"alive": 0, "cap": 5}}
        out = A.render_red_status(m)
        self.assertIn("ORPHANED LEASE", out)
        self.assertNotIn("none at work", out)

    def test_an_owner_holding_the_familys_reds_is_its_holder(self):
        os.environ["FAKE_CLAUDE_SLEEP"] = "60"
        self.addCleanup(os.environ.__setitem__, "FAKE_CLAUDE_SLEEP", "0")
        self.tick(dispatch=True)
        self.assertTrue(self.wait(lambda: self.rec().get("session")))
        [f] = [f for f in A.status_families(self.st, self.t[0]) if f["family"] == FAM]
        self.assertEqual(f["holder"], A.owner_holder(FAM))
        self.assertTrue(f["at_work"], f)


class StoreAuthTest(OwnerBase):
    def test_a_403_is_store_auth_failed_not_a_store_that_did_not_answer(self):
        err = urllib.error.HTTPError("http://x/v1/store/renew", 403, "Forbidden", {}, None)
        err.read = lambda: b'{"error": "Forbidden", "message": "this token may not call /v1/store/renew"}'
        rs = A.RemoteStore("http://x", tok="old")
        with mock.patch.object(A.urllib.request, "urlopen", side_effect=err):
            with self.assertRaises(A.StoreAuthFailed) as cm:
                rs.renew("red/k", 1, "s", "m", 60)
        self.assertIn("HTTP 403", str(cm.exception))
        self.assertIsInstance(cm.exception, A.StoreUnavailable, "every StoreUnavailable handler still sees it")

    def test_reauth_takes_a_fresh_token_or_none(self):
        self.addCleanup(os.environ.pop, "AGENTCTL_STORE_TOKEN", None)
        os.environ["AGENTCTL_STORE_TOKEN"] = "old-token"
        with mock.patch.object(A, "token_from_processor", return_value="fresh-token"):
            self.assertEqual(A.store_reauth(), "fresh-token")
        self.assertEqual(os.environ["AGENTCTL_STORE_TOKEN"], "fresh-token")
        A._tokens.clear()
        with mock.patch.object(A, "token_from_processor", return_value="fresh-token"):
            self.assertIsNone(A.store_reauth(), "the same refused token is no fresh one")

    def test_every_fixer_and_owner_launch_carries_the_store_token(self):
        self.addCleanup(os.environ.pop, "AGENTCTL_STORE_TOKEN", None)
        os.environ["AGENTCTL_STORE_TOKEN"] = "store-token"
        A._tokens.clear()
        for o_r in ("pawelkrupinski/movies", "bitcashier/infra"):
            self.assertEqual(A.fixer_env(o_r, 1).get("AGENTCTL_STORE_TOKEN"), "store-token", o_r)


if __name__ == "__main__":
    unittest.main()
