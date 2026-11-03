"""THE OWNERS CHURNED (2026-11-03): `red status -v` read "owner convergence: ALIVE, turn 1, 22 restart(s)", "owner
main-tests: 11 restart(s)", "owner infra: ended: STOPPED: the owner's heartbeat lease was lost"; convergence red
8fbdbe0d5a86 went "lease expired" / "STALLED: no progress (transcript, worktree) for 30 min" every 15-30 min and took
~8 h through several owners. Four causes, one test class each:
  - the progress mark saw the transcript and the worktree only: a sub-agent's transcript, a command's output and the job
    directory (a bisect's log, a measure collector) moved nothing, nor did a background task of a `working` session;
  - the red leases renewed only on a new mark, with a 15 min TTL under a 30 min stall budget: they lapsed under a turn
    the wrapper still judged at work;
  - a restart stopped the session mid-turn and started it over (turn 0, the fresh task prompt);
  - the laptop asleep or the store's tunnel down lapsed every laptop lease at once (the store's clock ran on);
and the holder's worktree read "?" with an empty "with" list."""
import json, os, signal, subprocess, sys, time, unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from test_agentctl import A  # noqa: E402
from test_owner import OwnerBase, FAM  # noqa: E402


class SessionActivityTest(unittest.TestCase):
    def setUp(self):
        import tempfile
        self.tmp = tempfile.TemporaryDirectory()
        self.saved = {k: os.environ.get(k) for k in ("AGENTCTL_CLAUDE_TMP", "AGENTCTL_CLAUDE_JOBS")}
        os.environ.update(AGENTCTL_CLAUDE_TMP=os.path.join(self.tmp.name, "ctmp"),
                          AGENTCTL_CLAUDE_JOBS=os.path.join(self.tmp.name, "jobs"))
        self.sid = "f3a6d061-0c89-40f3-b8a8-364c1029a1cc"
        proj = os.path.join(self.tmp.name, "projects", "-Users-pawel-projects-movies-wt-owner-convergence")
        os.makedirs(proj)
        self.transcript = os.path.join(proj, f"{self.sid}.jsonl")
        open(self.transcript, "w").write("{}\n")
        self.wt = os.path.join(self.tmp.name, "wt")
        os.makedirs(self.wt)

    def tearDown(self):
        for k, v in self.saved.items():
            os.environ.pop(k, None) if v is None else os.environ.__setitem__(k, v)
        self.tmp.cleanup()

    def mark(self):
        return A.fixer_mark({"log": self.transcript, "bg_id": self.sid[:8]}, self.wt)

    def grow(self, path):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "a") as f:
            f.write("x" * 100 + "\n")

    def test_a_sub_agents_transcript_growing_is_progress(self):
        before = self.mark()
        self.grow(os.path.join(self.transcript[:-6], "subagents", "agent-aec915cc226ebd749.jsonl"))
        self.assertNotEqual(self.mark(), before)

    def test_a_bisect_writing_under_the_job_dir_is_progress(self):
        """22:28 on 2026-11-03: $CLAUDE_JOB_DIR/tmp/bisect.log grew every few seconds, the transcript did not."""
        before = self.mark()
        self.grow(os.path.join(self.tmp.name, "jobs", self.sid[:8], "tmp", "bisect.log"))
        self.assertNotEqual(self.mark(), before)

    def test_a_commands_output_growing_is_progress(self):
        before = self.mark()
        self.grow(os.path.join(self.tmp.name, "ctmp", "-Users-pawel-projects-movies-wt-owner-convergence", self.sid,
                               "tasks", "baw73ff9f.output"))
        self.assertNotEqual(self.mark(), before)

    def test_a_working_session_waiting_on_its_own_background_task_is_at_work(self):
        """09:16-09:51 on 2026-11-03: the owner backgrounded its CI measure, its row read `working`: STALLED at 30 min."""
        with open(self.transcript, "a") as f:
            f.write(json.dumps({"type": "user", "message": {"content": [
                {"type": "tool_result", "content": "Command running in background with ID: bofwiys7j."}]}}) + "\n")
        ent = {"id": "f3a6d061", "state": "working", "status": "busy", "pid": os.getpid()}
        a, b = A.bg_wait_mark(ent, self.transcript, 1000, 60), A.bg_wait_mark(ent, self.transcript, 1061, 60)
        self.assertTrue(a and b and a != b, (a, b))
        self.assertEqual(A.bg_wait_mark({**ent, "state": "done"}, self.transcript, 1000, 60), "", "an ended turn")


class OwnerLeaseRenewTest(OwnerBase):
    def test_a_quiet_turn_inside_its_stall_budget_keeps_its_reds_leases_renewed(self):
        os.environ["FAKE_CLAUDE_SLEEP"] = "60"  # the session works without a word: its transcript does not move
        self.addCleanup(os.environ.__setitem__, "FAKE_CLAUDE_SLEEP", "0")
        self.tick(dispatch=True)
        name = A.red_lease(self.only()["data"]["key"])
        self.assertTrue(self.wait(lambda: self.rec().get("in_turn") and self.rec().get("bg_id")))
        time.sleep(1.0)
        first = self.st.get(name)["renewed_at"]
        self.assertTrue(self.wait(lambda: self.st.get(name)["renewed_at"] > first + 0.5, limit=5),
                        "renewed by the wrapper's beat while the turn is not stalled")

class OwnerRestartTest(OwnerBase):
    def kill_wrapper(self):
        first = self.rec()
        os.kill(first["pid"], signal.SIGKILL)  # it dies: no release, its leases lapse
        self.wait(lambda: not A.pid_alive(first["pid"]) or os.waitpid(first["pid"], os.WNOHANG)[0])
        c = self.st.db()
        c.execute("UPDATE leases SET expires_at=? WHERE holder=?", (self.t[0] - 1, A.owner_holder(FAM)))
        c.close()
        self.tick(dispatch=True, advance=1)   # the red's lease lapsed: offered again
        self.tick(dispatch=True, advance=1)   # ...and its owner, dead, started again
        self.assertTrue(self.wait(lambda: self.rec().get("pid") not in (None, first["pid"])))
        return first

    def launches(self):
        return sorted(f for f in os.listdir(os.environ["FAKE_CLAUDE_OUT"]) if f.endswith(".json"))

    def test_a_restart_adopts_the_session_still_at_work_never_stops_it(self):
        os.environ["FAKE_CLAUDE_SLEEP"] = "60"
        self.addCleanup(os.environ.__setitem__, "FAKE_CLAUDE_SLEEP", "0")
        self.tick(dispatch=True)
        self.assertTrue(self.wait(lambda: self.rec().get("session") and self.rec().get("bg_id")))
        launched = self.launches()
        [s0] = [s for s in self.sessions() if s["id"] == self.rec()["bg_id"]]
        first = self.kill_wrapper()
        self.assertTrue(self.wait(lambda: self.rec().get("bg_id") == first["bg_id"] and self.rec().get("in_turn")),
                        self.rec())
        self.assertEqual(self.launches(), launched, "no new launch: the same session goes on")
        [s] = [s for s in self.sessions() if s["id"] == first["bg_id"]]
        self.assertFalse(s.get("stopped"), "never stopped mid-turn")
        self.assertEqual(s.get("pid"), s0.get("pid"), "the same process: its work in flight goes on")
        self.assertNotIn("--resume", self.argv_of(first["session"]), "never started over")
        self.assertEqual(self.rec()["turn"], first["turn"], "the turn goes on, never reset")

    def test_a_restart_resumes_the_session_told_to_continue_never_start_over(self):
        os.environ["FAKE_CLAUDE_SLEEP"] = "60"
        self.addCleanup(os.environ.__setitem__, "FAKE_CLAUDE_SLEEP", "0")
        self.tick(dispatch=True)
        self.assertTrue(self.wait(lambda: self.rec().get("session") and self.rec().get("bg_id")))
        sid, bid = self.rec()["session"], self.rec()["bg_id"]
        subprocess.run([os.environ["AGENTCTL_CLAUDE"], "stop", bid], capture_output=True)  # its session is gone too
        self.kill_wrapper()
        self.assertTrue(self.wait(lambda: "--resume" in self.argv_of(sid)))
        argv = self.argv_of(sid)
        self.assertEqual(argv[argv.index("--resume") + 1], sid)
        self.assertIn("RESUMED AFTER A RESTART, NEVER START OVER", argv[argv.index("--bg") + 1])
        self.assertTrue(self.wait(lambda: self.rec().get("turn") == 2), "the turn count goes on (1, then 2)")


if __name__ == "__main__":
    unittest.main()
