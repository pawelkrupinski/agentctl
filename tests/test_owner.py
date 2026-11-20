"""Long-lived family owners (docs/simplification-2026-10.md item 6): one persistent session per family, one worktree and
a notebook kept across reds and restarts, the family's reds through its inbox as the next turn of the SAME session, its
lease a heartbeat, restarted only when it died."""
import json, os, signal, subprocess, sys, time, unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from test_agentctl import A  # noqa: E402
from test_reds import RedsBase  # noqa: E402
from test_red import FAIL_LINE  # noqa: E402

FAM = "main-tests"


class OwnerBase(RedsBase):
    def setUp(self):
        super().setUp()
        fams = os.path.join(self.tmp.name, "families.json")
        with open(fams, "w") as f:
            json.dump({"families": [{"name": FAM, "owner": True, "workflows": ["Main"], "kinds": ["test"]},
                                    {"name": "convergence", "owner": True,
                                     "workflows": ["Identity model convergence"]}]}, f)
        self.addCleanup(os.environ.__setitem__, "AGENTCTL_RED_FAMILIES", os.environ["AGENTCTL_RED_FAMILIES"])
        os.environ.update(AGENTCTL_RED_FAMILIES=fams, AGENTCTL_RED_RENEW="0.2")
        self.addCleanup(os.environ.pop, "AGENTCTL_RED_RENEW", None)
        self.addCleanup(self.stop_owner)

    def stop_owner(self):
        rec = A.read_record(A.owner_record_path(FAM)) or {}
        if rec.get("pid") and not rec.get("ended"):
            try:
                os.kill(rec["pid"], signal.SIGTERM)
            except OSError:
                pass
            self.wait(lambda: (A.read_record(A.owner_record_path(FAM)) or {}).get("ended") or not A.pid_alive(rec["pid"]))
            try:
                os.waitpid(rec["pid"], os.WNOHANG)
            except ChildProcessError:
                pass

    def wait(self, cond, limit=30):
        t0 = time.time()
        while not cond() and time.time() - t0 < limit:
            time.sleep(0.1)
        return cond()

    def rec(self):
        return A.read_record(A.owner_record_path(FAM)) or {}

    def owner_log(self):
        """The owner wrapper's own log, the last lines: what it did, for a failed wait's message."""
        try:
            with open(self.rec().get("log") or "") as f:
                return f"owner record {self.rec()}; its log:\n" + "".join(f.readlines()[-30:])
        except OSError as e:
            return f"owner record {self.rec()}; no log ({e})"

    def sessions(self):
        r = subprocess.run([os.environ["AGENTCTL_CLAUDE"], "agents", "--json", "--all"], capture_output=True, text=True)
        return json.loads(r.stdout)

    def argv_of(self, sid):
        return json.load(open(os.path.join(os.environ["FAKE_CLAUDE_OUT"], f"{sid[:8]}.json")))["argv"]


class OwnerTest(OwnerBase):
    def test_the_family_red_starts_its_owner_and_a_turn_verdict_releases_the_turns_leases_not_the_owner(self):
        self.tick(dispatch=True)
        it = self.only()
        row = self.st.get(A.red_lease(it["data"]["key"]))
        self.assertEqual((row["holder"], A.lease_meta(row)["kind"]), (A.owner_holder(FAM), "owned"))
        self.assertTrue(self.wait(lambda: self.rec().get("session")))
        rec = self.rec()
        self.assertTrue(rec["worktree"].endswith(f"-wt-owner-{FAM}"), rec["worktree"])
        self.assertTrue(os.path.exists(os.path.join(rec["worktree"], A.OWNER_NOTEBOOK)))
        [s] = self.sessions()
        self.assertEqual(s["name"], f"red owner · {FAM}")
        prompt = self.argv_of(s["sessionId"])[self.argv_of(s["sessionId"]).index("--bg") + 1]
        self.assertIn("LONG-LIVED OWNER", prompt)
        self.assertIn(A.OWNER_NOTEBOOK, prompt)
        self.assertIn("FAST REPRODUCER FIRST", prompt)
        with open(A.FIXER_BRIEF) as f:
            self.assertIn(A.fixer_brief_for(f.read().strip(), A.RED_PRIMARY), prompt,
                          "the owner's prompt carries docs/fixer-brief.md word for word (its repo's section only)")
        self.assertNotIn("FIRST run `~/bin/agentctl red claim", prompt, "its leases are held for it")
        # the turn ends LANDED: that red's lease is released with the verdict; the owner lives on, heartbeating
        self.assertTrue(self.wait(lambda: str(A.lease_meta(self.st.get(row["name"])).get("outcome") or "")
                                  .startswith("LANDED")))
        self.assertTrue(A.lease_is_live(self.st.get(A.owner_lease(FAM)), time.time()))
        self.assertIsNone(self.rec().get("ended"))
        self.assertEqual(A.owner_leases(FAM), {})

    def test_a_new_red_of_the_family_is_the_next_turn_of_the_same_session(self):
        os.environ["FAKE_CLAUDE_SLEEP"] = "1"
        self.addCleanup(os.environ.__setitem__, "FAKE_CLAUDE_SLEEP", "0")
        self.tick(dispatch=True)
        self.assertTrue(self.wait(lambda: self.rec().get("session")))
        sid = self.rec()["session"]
        # another Main run fails another way: a second red of the family, handed to the live owner
        self.main_red(41, 21, line="[info] - BarSpec should baz *** FAILED ***")
        self.tick(dispatch=True, advance=1)
        self.assertTrue(self.wait(lambda: self.rec().get("turn") == 2, limit=40))
        self.assertEqual(self.rec()["session"], sid, "the same session, resumed")
        argv = self.argv_of(sid)
        self.assertEqual(argv[argv.index("--resume") + 1], sid)
        self.assertIn("NEXT TURN", argv[argv.index("--bg") + 1])
        self.assertEqual(len({s["sessionId"] for s in self.sessions()}), 1)

    def test_a_turn_past_its_wall_time_is_stopped_and_its_leases_released_the_owner_lives_on(self):
        """An owner turn was unbounded: a hung session held its family's reds `leased` for up to OWNER_MAX (7 days)."""
        os.environ["FAKE_CLAUDE_SLEEP"] = "60"
        self.addCleanup(os.environ.__setitem__, "FAKE_CLAUDE_SLEEP", "0")
        os.environ["AGENTCTL_RED_FIXER_WALL"] = "2"
        self.addCleanup(os.environ.pop, "AGENTCTL_RED_FIXER_WALL", None)
        self.tick(dispatch=True)
        row = self.st.get(A.red_lease(self.only()["data"]["key"]))
        self.assertTrue(self.wait(lambda: str(A.lease_meta(self.st.get(row["name"])).get("outcome") or "")
                                  .startswith("TIMED OUT")))
        self.assertIsNone(self.rec().get("ended"), "one turn timed out; the owner goes on")

    def test_a_red_cleared_under_its_owner_never_stops_the_owner(self):
        """red_stop_holder wrote RED-STOP.md into the owner's ONE permanent worktree: every later turn stopped at once."""
        wt = os.path.join(self.tmp.name, "owner-wt")
        os.makedirs(wt)
        self.st.acquire("red/kx", A.owner_holder(FAM), 600, 3600, meta={"kind": "owned", "worktree": wt})
        self.assertEqual(A.red_stop_holder(self.st, "kx", "cleared", "red cleared"), A.owner_holder(FAM))
        self.assertFalse(A.lease_is_live(self.st.get("red/kx"), time.time()), "revoked: its next beat drops it")
        self.assertFalse(os.path.exists(os.path.join(wt, "RED-STOP.md")))
        self.assertTrue(A.red_history(self.st.get("red/kx"), time.time())["dispatched"],
                        "an owner's turn counts for the crash-loop breaker as a fixer's does")

    def test_an_owner_that_died_is_restarted_in_its_worktree_with_its_notebook_and_session(self):
        os.environ["FAKE_CLAUDE_SLEEP"] = "60"
        self.addCleanup(os.environ.__setitem__, "FAKE_CLAUDE_SLEEP", "0")
        self.tick(dispatch=True)
        self.assertTrue(self.wait(lambda: self.rec().get("session")))
        first = self.rec()
        with open(first["notebook"], "a") as f:
            f.write("- reproducer: FooSpec -z bar fails in 3 s\n")
        # its session ended too (a session still at work is adopted instead: test_owner_churn.OwnerRestartTest)
        subprocess.run([os.environ["AGENTCTL_CLAUDE"], "stop", first["session"][:8]], capture_output=True)
        os.kill(first["pid"], signal.SIGKILL)  # it dies: no release, its leases lapse
        self.wait(lambda: not A.pid_alive(first["pid"]) or os.waitpid(first["pid"], os.WNOHANG)[0])
        it = self.only()
        c = self.st.db()
        # lapsed on the TICKS' clock (self.t), not the wall clock: on a slow runner the wall clock is well past self.t,
        # and a lease that lapsed at wall-1 was still live at self.t+2 (CI run 3969)
        c.execute("UPDATE leases SET expires_at=? WHERE holder=?", (self.t[0] - 1, A.owner_holder(FAM)))
        c.close()
        # the red's lease lapsed: offered again, and its owner, dead, started again -- by the ticks, as the reconciler
        # ticks every cycle: two fixed ticks raced the dispatcher under load (issue 20261101T012328-65259e, a 2-cpu
        # python:3.14 run 2026-11-19 18:2x: no restart within 30 s)
        def restarted():
            self.tick(dispatch=True, advance=1)
            return self.rec().get("pid") not in (None, first["pid"])
        self.assertTrue(self.wait(restarted))
        rec = self.rec()
        self.assertEqual((rec["worktree"], rec["notebook"], rec["restarts"]), (first["worktree"], first["notebook"], 1))
        self.assertIn("reproducer: FooSpec", open(rec["notebook"]).read())
        self.assertTrue(self.wait(lambda: self.rec().get("turn") == 2), "the turn count goes on")
        argv = self.argv_of(first["session"])
        self.assertEqual(argv[argv.index("--resume") + 1], first["session"], "the same session goes on")
        self.assertEqual(self.st.get(A.red_lease(it["data"]["key"]))["holder"], A.owner_holder(FAM))

    def test_the_idle_clock_runs_from_a_turns_end_never_its_start(self):
        """OWNER_IDLE was counted from the turn's START: a turn longer than it ended the owner `IDLE` the moment it
        ended -- with no time left for the family's next red to reach it."""
        os.environ.update(FAKE_CLAUDE_SLEEP="3", AGENTCTL_OWNER_IDLE="2")
        self.addCleanup(os.environ.__setitem__, "FAKE_CLAUDE_SLEEP", "0")
        self.addCleanup(os.environ.pop, "AGENTCTL_OWNER_IDLE", None)
        self.tick(dispatch=True)
        name = A.red_lease(self.only()["data"]["key"])
        self.assertTrue(self.wait(lambda: str(A.lease_meta(self.st.get(name)).get("outcome") or "").startswith("LANDED")))
        ended_turn = time.time()
        time.sleep(1)
        self.assertIsNone(self.rec().get("ended"), "idle for 1 s of its 2: alive")
        self.assertTrue(self.wait(lambda: self.rec().get("ended")))
        self.assertTrue(str(self.rec().get("verdict")).startswith("IDLE"), self.rec().get("verdict"))
        self.assertGreaterEqual(A.parse_time(self.rec()["ended"]).timestamp(), int(ended_turn) + 1)

    def test_every_owner_is_briefed_mechanism_only_for_identity_flips(self):
        owner = {"family": "convergence", "notebook": "/wt/RED-NOTEBOOK.md", "reds": ["x"]}
        u = {"workflow": "Identity model convergence", "job": "leg (poland)", "kind": "test", "line": "drift",
             "run": {"number": 7, "databaseId": 9}}
        p = A.fixer_prompt(u, "/wt", "/brief", owner=owner)
        self.assertIn("MECHANISM ONLY", p)
        self.assertIn("FAST REPRODUCER FIRST", p)
        self.assertIn("MECHANISM ONLY", A.fixer_prompt(u, "/wt", "/brief", owner={**owner, "family": FAM}))

    def test_status_shows_each_owner(self):
        self.tick(dispatch=True)
        self.assertTrue(self.wait(lambda: self.rec().get("session")))
        rows = {r["family"]: r for r in A.owners_status(self.st, time.time())}
        self.assertTrue(rows[FAM]["alive"])
        self.assertFalse(rows["convergence"]["alive"])


if __name__ == "__main__":
    unittest.main()
