"""Long-lived family owners (docs/simplification-2026-10.md item 6): one persistent session per family, one worktree and
a notebook kept across reds and restarts, the family's reds through its inbox as the next turn of the SAME session, its
lease a heartbeat, restarted only when it died."""
import json, os, signal, subprocess, sys, time, unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from test_agentctl import A  # noqa: E402
from harness import case_context  # noqa: E402
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
        self.assertTrue(A.lease_is_live(self.st.get(A.owner_lease(FAM)), A.epoch_now()))
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

    def test_a_session_that_never_spoke_is_never_resumed_and_its_failure_is_named(self):
        """2026-11-21, Order independence #99 (issue 20261121T191539-21372d): an owner turn's session failed before its
        first message; the owner resumed it next turn, claude refused (`source session ... not found`), the refusal's
        own id was resumed the turn after, and so on: five crashes in a row, a HARNESS FAULT reading only "ended with no
        LANDED / GAVE UP line (session ...: failed)". The next turn starts a fresh session, and the crash's verdict
        carries claude's own error."""
        jobs = os.path.join(self.tmp.name, "jobs")
        once = os.path.join(self.tmp.name, "silent-once")
        with open(once, "w") as f:
            f.write("API Error: Can't reach the API server (ENOTFOUND)")
        for k, v in (("AGENTCTL_CLAUDE_JOBS", jobs), ("FAKE_CLAUDE_SILENT_ONCE", once)):
            self.addCleanup(os.environ.pop, k, None)
            os.environ[k] = v
        self.tick(dispatch=True)
        name = A.red_lease(self.only()["data"]["key"])
        outcome = lambda: str(A.lease_meta(self.st.get(name) or {}).get("outcome") or "")
        self.assertTrue(self.wait(lambda: outcome().startswith("ended with no")), self.owner_log())
        dead = self.rec()["session"]
        self.assertIn(f"failed: API Error: Can't reach the API server (ENOTFOUND)", outcome(),
                      "the crash names claude's own error")
        for _ in range(20):  # offered again: the owner's next turn
            if self.rec().get("turn") == 2:
                break
            self.tick(dispatch=True, advance=60)
            self.wait(lambda: self.rec().get("turn") == 2, limit=1)
        self.assertTrue(self.wait(lambda: outcome().startswith("LANDED")), (outcome(), self.owner_log()))
        self.assertNotEqual(self.rec()["session"], dead)
        argv = self.argv_of(self.rec()["session"])
        self.assertNotIn("--resume", argv, "a session with no conversation is never resumed: a fresh one")

    def test_a_turn_closed_under_a_working_row_ends_at_once_never_at_the_stall(self):
        """2026-11-21 21:53Z (convergence, session 0f6f7ecd) and 22:55Z (outside-gate, 0dedcd39): the session answered
        its verdict and claude closed the turn (`turn_duration`), but its row stayed `working` with its process alive.
        The wrapper read only `blocked` / `idle` rows' transcripts and held the turn until RED_STALL (30 min) stopped
        it; the orphan reaper then revoked its heartbeat ("its session ... is stopped while its wrapper ... still holds
        the turn"). The wrapper now ends that turn within a poll or two."""
        os.environ["FAKE_CLAUDE_STAY_WORKING"] = "1"
        self.addCleanup(os.environ.pop, "FAKE_CLAUDE_STAY_WORKING", None)
        self.tick(dispatch=True)
        name = A.red_lease(self.only()["data"]["key"])
        outcome = lambda: str(A.lease_meta(self.st.get(name) or {}).get("outcome") or "")
        self.assertTrue(self.wait(outcome, limit=20), self.owner_log())
        self.assertTrue(outcome().startswith("LANDED"), outcome())
        self.assertTrue(self.wait(lambda: not self.rec().get("in_turn"), limit=5),
                        "the turn is over: the reaper reads `between turns`")
        self.assertTrue(A.lease_is_live(self.st.get(A.owner_lease(FAM)), A.epoch_now()), "the owner lives on")

    def test_a_wrapper_whose_worktree_was_deleted_still_reads_its_session(self):
        """2026-11-22 04:27Z (outside-gate, wrapper pid 3227, session c07162d7): the owner's worktree -- the wrapper's
        cwd -- was deleted and made again mid-turn. claude refuses to start in a deleted directory, so each `claude
        agents` the wrapper ran read nothing, and the session's `done` went unseen until the orphan reaper revoked the
        turn. agentctl runs claude from a directory that exists."""
        agents = os.path.join(os.environ["FAKE_CLAUDE_OUT"], "agents")
        os.makedirs(agents, exist_ok=True)
        with open(os.path.join(agents, "c07162d7.json"), "w") as f:
            json.dump({"id": "c07162d7", "sessionId": "c07162d7-c31d", "kind": "background", "state": "done"}, f)
        gone = os.path.join(self.tmp.name, "deleted-worktree")
        os.makedirs(gone)
        self.addCleanup(os.chdir, os.getcwd())
        os.chdir(gone)
        os.rmdir(gone)
        self.assertEqual(A.bg_session(A.claude_cli(), "c07162d7").get("state"), "done")

    def test_a_dead_owner_is_started_once_however_many_passes_find_it_dead(self):
        """2026-11-21 22:12:33Z and 22:12:36Z: after its ORPHANED end the convergence owner was started twice, 3 s apart
        (pids 6921, 7139): two of the family's reds re-offered in two dispatcher passes, each finding no live heartbeat
        -- the first wrapper had not taken it yet -- and the second wrapper ended DUPLICATE. The dispatcher now takes
        the owner's heartbeat itself before it starts the wrapper (one start per family per epoch): the next pass finds
        the owner alive and hands it the red."""
        started = []
        saved = A.spawn_detached

        def spawn(argv, **kw):  # a wrapper slow to start: it never reaches its own heartbeat
            p = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"], start_new_session=True)
            started.append(p)
            self.addCleanup(lambda: (p.kill(), p.wait()))
            return p
        A.spawn_detached = spawn
        self.addCleanup(setattr, A, "spawn_detached", saved)
        self.tick(dispatch=True)
        self.assertEqual(len(started), 1)
        self.main_red(41, 21, line="[info] - BarSpec should baz *** FAILED ***")
        self.tick(dispatch=True, advance=3)
        self.assertEqual(len(started), 1, "one start per family: the second pass hands its red to the starting owner")
        name = A.red_lease(self.only()["data"]["key"])  # the second red (the first cleared with its run)
        self.assertEqual(self.st.get(name)["holder"], A.owner_holder(FAM))
        self.assertIn(name, A.owner_leases(FAM))
        hb = self.st.get(A.owner_lease(FAM))
        self.assertTrue(A.lease_is_live(hb, self.t[0]), "the heartbeat is the starting wrapper's, taken for it")
        self.assertEqual(self.rec()["pid"], started[0].pid)

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
        self.assertFalse(A.lease_is_live(self.st.get("red/kx"), A.epoch_now()), "revoked: its next beat drops it")
        self.assertFalse(os.path.exists(os.path.join(wt, "RED-STOP.md")))
        self.assertTrue(A.red_history(self.st.get("red/kx"), A.epoch_now())["dispatched"],
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
        ended_turn = A.epoch_now()
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
        rows = {r["family"]: r for r in A.owners_status(self.st, A.epoch_now())}
        self.assertTrue(rows[FAM]["alive"])
        self.assertFalse(rows["convergence"]["alive"])


def transcript(path, *entries):
    """A fake transcript: each entry a (type, text, usage tokens or None, sidechain) row."""
    with open(path, "w") as f:
        for kind, text, tokens, side in entries:
            m = {"role": kind, "content": [{"type": "text", "text": text}]}
            if tokens is not None:
                m["usage"] = {"input_tokens": 2, "cache_read_input_tokens": tokens - 5, "cache_creation_input_tokens": 3}
            f.write(json.dumps({"type": kind, "message": m, **({"isSidechain": True} if side else {})}) + "\n")
    return path


class SessionRolloverTest(OwnerBase):
    """A SESSION PAST ITS CONTEXT LIMIT STARTS FRESH (2026-11-25): the outside-gate owner, resumed as the same session
    for 134 turns, reached ~985k tokens of context; each resume reloaded it all. Past AGENTCTL_CONTEXT_FRESH (300k) the
    owner's next turn is a fresh session with a handover from agentctl's own records."""

    def owner_rec(self, tokens, wt=None):
        wt = wt or self.r.path
        path = transcript(os.path.join(self.tmp.name, "old.jsonl"), ("user", "go", None, False),
                          ("assistant", "decided: bisect FooSpec first", tokens, False),
                          ("assistant", "a subagent's own context", 5, True))
        return {"session": "0ld5e55i0n-1", "transcript": path, "worktree": wt, "notebook": f"{wt}/RED-NOTEBOOK.md",
                "verdicts": ["turn 3: DIAGNOSED FooSpec races its clock"], "last_end": "IDLE"}

    def test_the_context_is_the_last_turns_input_cache_read_and_cache_creation(self):
        rec = self.owner_rec(412345)
        self.assertEqual(A.context_tokens(rec["transcript"]), 412345, "a sidechain's figures are not the session's")
        big = os.path.join(self.tmp.name, "big.jsonl")  # the figures far from the end: read back in larger chunks
        transcript(big, ("assistant", "x", 777, False), *[("user", "y" * 2000, None, False)] * 400)
        self.assertEqual(A.context_tokens(big), 777)

    def test_above_the_limit_the_owner_starts_fresh_with_a_handover_of_its_reds_and_worktree(self):
        self.tick(dispatch=False)
        key = self.only()["data"]["key"]
        with open(os.path.join(self.r.path, "wip.txt"), "w") as f:
            f.write("half done")
        rec = self.owner_rec(412345)
        text = A.owner_rollover(self.st, FAM, rec, [A.red_lease(key)])
        self.assertIsNotNone(text, "past 300k: a fresh session")
        self.assertIn("A FRESH SESSION, HANDED OVER", text)
        self.assertIn(f"red {key}", text, "the reds in hand")
        self.assertIn(self.r.path.replace(A.HOME, "~"), text, "the worktree")
        self.assertIn("wip.txt", text, "its dirty files")
        self.assertIn("DIAGNOSED FooSpec races its clock", text, "the last verdicts")
        self.assertIn("decided: bisect FooSpec first", text, "the old session's last messages")
        self.assertLess(len(text), A.HANDOVER_MAX + 1)
        self.assertIsNone(rec["session"], "never resumed")
        self.assertEqual([(r["old"], r["tokens"]) for r in rec["rollovers"]], [("0ld5e55i0n-1", 412345)])
        A.session_seen(rec, "n3w-5e55i0n")
        self.assertEqual(rec["rollovers"][-1]["new"], "n3w-5e55i0n")

    def test_below_the_limit_or_with_no_readable_transcript_the_session_resumes(self):
        rec = self.owner_rec(120000)
        self.assertIsNone(A.owner_rollover(self.st, FAM, rec, []))
        self.assertEqual((rec["session"], rec["context_tokens"]), ("0ld5e55i0n-1", 120000), "resumed, its context read")
        for path in (os.path.join(self.tmp.name, "missing.jsonl"),
                     transcript(os.path.join(self.tmp.name, "nousage.jsonl"), ("assistant", "hi", None, False))):
            rec = dict(self.owner_rec(1), transcript=path)
            self.assertIsNone(A.owner_rollover(self.st, FAM, rec, []), path)
            self.assertEqual((rec["session"], rec["context_tokens"]), ("0ld5e55i0n-1", None))

    def test_a_fixer_resuming_a_session_past_the_limit_starts_fresh_with_the_handover(self):
        rec = {"resume": True, "resumed_from": "0ld5e55i0n-1", "resume_note": "revive the lander", "worktree": self.r.path,
               "workflow": "Main", "run": 40, "line": FAIL_LINE}
        found = os.path.join(A.claude_projects_root(), "fake")  # where bg_transcript finds a session's transcript
        os.makedirs(found, exist_ok=True)
        os.replace(self.owner_rec(500000)["transcript"], os.path.join(found, "0ld5e55i0n-1.jsonl"))
        self.assertEqual(A.fixer_rollover(self.st, rec), rec["resume_note"])
        self.assertFalse(rec["resume"])
        self.assertIn("revive the lander", rec["resume_note"])
        self.assertIn("A FRESH SESSION, HANDED OVER", rec["resume_note"])
        self.assertEqual(rec["rollover"]["tokens"], 500000)
        os.replace(self.owner_rec(1000)["transcript"], os.path.join(found, "sm4ll.jsonl"))
        small = {"resume": True, "resumed_from": "sm4ll", "worktree": self.r.path, "resume_note": "go on"}
        self.assertEqual(A.fixer_rollover(self.st, small), "go on")
        self.assertTrue(small["resume"])

    def test_the_owners_next_turn_past_the_limit_is_a_fresh_session_shown_in_red_status(self):
        os.environ.update(FAKE_CLAUDE_SLEEP="1", FAKE_CLAUDE_CONTEXT="400000")
        self.addCleanup(os.environ.__setitem__, "FAKE_CLAUDE_SLEEP", "0")
        self.addCleanup(os.environ.pop, "FAKE_CLAUDE_CONTEXT", None)
        self.tick(dispatch=True)
        self.assertTrue(self.wait(lambda: self.rec().get("session")))
        old = self.rec()["session"]
        self.main_red(41, 21, line="[info] - BarSpec should baz *** FAILED ***")
        self.tick(dispatch=True, advance=1)
        self.assertTrue(self.wait(lambda: self.rec().get("turn") == 2, limit=40), self.owner_log())
        self.assertTrue(self.wait(lambda: self.rec().get("session") not in (None, old)), self.owner_log())
        new = self.rec()["session"]
        argv = self.argv_of(new)
        self.assertNotIn("--resume", argv)
        self.assertIn("A FRESH SESSION, HANDED OVER", argv[argv.index("--bg") + 1])
        self.assertEqual([(r["old"], r["new"], r["tokens"]) for r in self.rec()["rollovers"]], [(old, new, 400000)])
        text = A.red_status_text(self.st, verbose=True, ctx=case_context())
        self.assertIn(f"rollover {self.rec()['rollovers'][0]['at']}: {old[:8]} -> {new[:8]} (400000 tokens)", text)

    def test_the_owners_next_turn_below_the_limit_resumes_with_its_context_read(self):
        os.environ.update(FAKE_CLAUDE_SLEEP="1", FAKE_CLAUDE_CONTEXT="1000")
        self.addCleanup(os.environ.__setitem__, "FAKE_CLAUDE_SLEEP", "0")
        self.addCleanup(os.environ.pop, "FAKE_CLAUDE_CONTEXT", None)
        self.tick(dispatch=True)
        self.assertTrue(self.wait(lambda: self.rec().get("session")))
        sid = self.rec()["session"]
        self.main_red(41, 21, line="[info] - BarSpec should baz *** FAILED ***")
        self.tick(dispatch=True, advance=1)
        self.assertTrue(self.wait(lambda: self.rec().get("turn") == 2, limit=40), self.owner_log())
        self.assertEqual((self.rec()["session"], self.rec().get("context_tokens")), (sid, 1000))
        argv = self.argv_of(sid)
        self.assertEqual(argv[argv.index("--resume") + 1], sid)


if __name__ == "__main__":
    unittest.main()
