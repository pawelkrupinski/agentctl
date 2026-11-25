"""A FINISHED TURN WHOSE PROCESS LIVES ON (2026-11-01, convergence #579 and Verify #1 blocked as `fixer-crash-loop` with
nothing crashed): claude keeps a background session's process after its turn (`claude bg-spare`), its row `blocked` /
`idle` with a live pid. The wrappers read that as "maybe waiting on its own work", never read the turn's GAVE UP /
DIAGNOSED, and each red's lease lapsed as "lease expired" -- three of those, and the breaker blocked the red on the owner.
The transcript decides now (bg_turn, bg_turn_over); and a session waiting on its own background task (`agentctl wait`)
is at work, its lease renewed by the wait (bg_wait_mark)."""
import json, os, signal, subprocess, sys, time, unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from test_agentctl import A  # noqa: E402
from test_owner import FAM, OwnerBase  # noqa: E402
from test_reds import RedsBase  # noqa: E402


def entry(role, content, **extra):
    return json.dumps({"type": role, "message": {"role": role, "content": content}, **extra}) + "\n"


def text(role, t):
    return entry(role, [{"type": "text", "text": t}])


def bg_started(tid):
    return entry("user", [{"type": "tool_result", "tool_use_id": "u1",
                           "content": f"Command running in background with ID: {tid}. Output is being written to: /x"}],
                 toolUseResult={"backgroundTaskId": tid})


def notified(tid):
    return text("user", f"<task-notification>\n<task-id>{tid}</task-id>\n<status>completed</status>\n</task-notification>")


class TurnTest(unittest.TestCase):
    def setUp(self):
        import tempfile
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = os.path.join(self.tmp.name, "t.jsonl")
        self.n = 0

    def write(self, *lines):
        self.n += 1
        with open(self.path, "w") as f:
            f.write("".join(lines))
        os.utime(self.path, (1_800_000_000 + self.n, 1_800_000_000 + self.n))  # a new signature: never the cache's

    def test_the_turns_answer_is_what_comes_after_its_latest_input(self):
        # a resumed session's transcript holds the turn before: its verdict is never this turn's
        self.write(text("user", "turn 1"), text("assistant", "DIAGNOSED: old"), text("user", "turn 2"))
        self.assertEqual(A.bg_turn(self.path)["final"], "")
        self.write(text("user", "turn 1"), text("assistant", "DIAGNOSED: old"), text("user", "turn 2"),
                   text("assistant", "x\nGAVE UP: new"))
        self.assertEqual(A.fixer_verdict(A.bg_turn(self.path)["final"]), "GAVE UP: new")

    def test_pending_tasks_are_this_turns_neither_reported_nor_stopped(self):
        self.write(text("user", "turn 1"), bg_started("bold1"), text("user", "turn 2"), bg_started("b1"),
                   bg_started("b2"), notified("b1"))
        self.assertEqual(A.bg_turn(self.path)["pending"], ["b2"], "bold1: an earlier turn's, gone with its process")
        self.write(text("user", "turn 2"), bg_started("b2"),
                   entry("assistant", [{"type": "tool_use", "name": "TaskStop", "input": {"task_id": "b2"}}]))
        self.assertEqual(A.bg_turn(self.path)["pending"], [])

    def test_a_waiting_session_with_a_live_process_ends_its_turn_by_its_transcript(self):
        live = {"state": "blocked", "status": "idle", "pid": os.getpid()}
        self.write(text("user", "go"), text("assistant", "done.\nGAVE UP: cannot reproduce"))
        self.assertTrue(A.bg_turn_over(live, self.path), "the #579 row: a verdict said, its process alive")
        self.assertFalse(A.bg_turn_over({**live, "state": "working"}, self.path), "still working")
        self.write(text("user", "go"), bg_started("bw"), text("assistant", "waiting for my CI run"))
        self.assertFalse(A.bg_turn_over(live, self.path, now=time.time() + 3600), "waiting on its own task: at work")
        self.assertTrue(A.bg_wait_mark(live, self.path, 100, 10))
        self.assertNotEqual(A.bg_wait_mark(live, self.path, 100, 10), A.bg_wait_mark(live, self.path, 110, 10),
                            "each renewal period of the wait is progress")
        self.write(text("user", "go"), text("assistant", "hmm"))
        self.assertFalse(A.bg_turn_over(live, self.path, now=os.path.getmtime(self.path) + 5), "within the grace")
        self.assertTrue(A.bg_turn_over(live, self.path, now=os.path.getmtime(self.path) + A.BG_IDLE_GRACE + 1),
                        "no verdict, nothing pending, quiet: ended with no verdict")
        self.assertEqual(A.bg_wait_mark(live, self.path, 100, 10), "")

    def test_a_meta_entry_is_no_prompt_and_a_subagent_is_a_pending_task(self):
        # 2026-11-25 04:04Z, Alert backtest #28 (outside-gate owner session 3b1ecbbd): the session ended its turn
        # "waiting for the beacon agent and the mutation-test timing", two shells of its own running. Claude wrote a
        # system-reminder user entry (isMeta) before a task notification; read as a new prompt, it emptied the pending
        # tasks, and the wrapper ended the turn as "no verdict" -- a crash for the breaker -- while it was at work.
        live = {"state": "working", "pid": os.getpid()}
        meta = entry("user", "<system-reminder>\n<tools-used-before-summary>Monitor</tools-used-before-summary>\n"
                     "</system-reminder>", isMeta=True)
        agent = entry("user", [{"type": "tool_result", "tool_use_id": "u2",
                                "content": [{"type": "text", "text": "Async agent launched successfully.\nagentId: a9f"}]}],
                      toolUseResult={"isAsync": True, "status": "async_launched", "agentId": "a9f"})
        closed = json.dumps({"type": "system", "subtype": "turn_duration"}) + "\n"
        self.write(text("user", "go"), bg_started("b1"), agent, text("assistant", "waiting on both"), closed)
        self.assertEqual(A.bg_turn(self.path)["pending"], ["b1", "a9f"], "a backgrounded subagent is its own task")
        self.write(text("user", "go"), bg_started("b1"), agent, text("assistant", "waiting"), closed, meta,
                   notified("a9f"), text("assistant", "That's interim: still waiting on b1"), closed)
        turn = A.bg_turn(self.path)
        self.assertEqual(turn["pending"], ["b1"], "the meta entry before a notification starts no turn")
        self.assertFalse(A.bg_turn_over(live, self.path, now=time.time() + 3600), "waiting on its own shell: at work")
        self.write(text("user", "go"), text("assistant", "x\nGAVE UP: cannot reproduce"), meta, closed)
        self.assertEqual(A.fixer_verdict(A.bg_turn(self.path)["final"]), "GAVE UP: cannot reproduce",
                         "a meta entry after the answer never hides its verdict")

    def test_a_session_that_wrote_no_transcript_yet_has_not_ended_its_turn(self):
        # 2026-11-25 02:59Z, Alert backtest #28 (outside-gate owner session 82fecbea): a resume of a ~985k-token
        # session, its row `blocked` with a live process while claude had not yet written its transcript. With no
        # transcript the quiet read from mtime 0, so the wrapper ended the turn at its first look ("ended with no
        # LANDED / GAVE UP line (session 82fecbea: blocked)") and stopped it: a crash for the breaker.
        live = {"state": "blocked", "pid": os.getpid()}
        missing = os.path.join(self.tmp.name, "none.jsonl")
        for path in (None, missing):
            self.assertFalse(A.bg_turn_over(live, path, now=time.time() + 3600), "starting: its stall budget judges it")
        self.assertTrue(A.bg_turn_over({"state": "done"}, None), "a row that says it ended still ends it")

    def test_a_blocked_sessions_ending_carries_claudes_reason(self):
        # 2026-11-24 13:57Z, Alert backtest #26 (sessions fa0ec19f, 66835cfd): `blocked` on "Prompt is too long ·
        # automatic compaction failed: You've hit your weekly limit", recorded as a bare "(session ...: blocked)"
        jobs = os.path.join(self.tmp.name, "jobs")
        os.makedirs(os.path.join(jobs, "fa0ec19f"))
        os.environ["AGENTCTL_CLAUDE_JOBS"] = jobs
        self.addCleanup(os.environ.pop, "AGENTCTL_CLAUDE_JOBS", None)
        why = ("Prompt is too long · automatic compaction failed: You've hit your weekly limit · resets 2am "
               "(Europe/Warsaw) · out of usage credits")
        with open(os.path.join(jobs, "fa0ec19f", "state.json"), "w") as f:
            json.dump({"state": "blocked", "detail": why}, f)
        self.assertEqual(A.bg_failure("fa0ec19f"), why)
        self.assertTrue(A.claude_limit(f"ended with no LANDED / GAVE UP line (session fa0ec19f: blocked: "
                                       f"{A.bg_failure('fa0ec19f')})"), "so the red's history reads it LIMITED")
        with open(os.path.join(jobs, "fa0ec19f", "state.json"), "w") as f:
            json.dump({"state": "blocked", "detail": "blocked"}, f)
        self.assertEqual(A.bg_failure("fa0ec19f"), "", "a bare state is no reason")


class LingerMixin:
    """The fake session's process lives on after its turn (claude's spare); killed at the test's end."""

    def linger(self, **env):
        os.environ.update(FAKE_CLAUDE_LINGER="1", **env)
        for k in ["FAKE_CLAUDE_LINGER", *env]:
            self.addCleanup(os.environ.pop, k, None)
        self.addCleanup(self.kill_sessions, os.path.join(os.environ["FAKE_CLAUDE_OUT"], "agents"))

    def kill_sessions(self, d):
        for n in os.listdir(d) if os.path.isdir(d) else []:
            try:
                pid = json.load(open(os.path.join(d, n))).get("pid")
                if pid:
                    os.killpg(pid, signal.SIGTERM)
            except (OSError, ValueError, TypeError):
                pass

    def outcome(self, name):
        return str(A.lease_meta(self.st.get(name)).get("outcome") or "")


class OwnerTurnTest(LingerMixin, OwnerBase):
    def test_an_owner_turn_ending_with_its_process_alive_releases_its_reds_with_the_verdict(self):
        self.linger(FAKE_CLAUDE_RESULT="GAVE UP: cannot reproduce the flip")
        self.tick(dispatch=True)
        name = A.red_lease(self.only()["data"]["key"])
        self.assertTrue(self.wait(lambda: self.outcome(name).startswith("GAVE UP"), limit=40), self.outcome(name))
        self.assertTrue(self.wait(lambda: not any(s.get("pid") for s in self.sessions())),
                        "the turn's session (its process alive: claude's spare) is stopped with the turn")
        self.assertEqual(A.red_crash_streak({"history": [{"dispatched": True, "outcome": self.outcome(name)}]}), [],
                         "a verdict is a result: never a crash")

    def test_an_owner_waiting_on_its_own_background_task_keeps_its_reds_and_is_not_stalled(self):
        os.environ.update(AGENTCTL_RED_STALL="1")
        self.addCleanup(os.environ.pop, "AGENTCTL_RED_STALL", None)
        self.linger(FAKE_CLAUDE_BG_WAIT="4")
        self.tick(dispatch=True)
        name = A.red_lease(self.only()["data"]["key"])
        self.assertTrue(self.wait(lambda: self.outcome(name).startswith("LANDED"), limit=40), self.outcome(name))


class SpentSessionTest(LingerMixin, OwnerBase):
    """2026-11-02: each owner turn `--resume`d the last turn's session while claude's spare kept its process running, so
    claude started a COPY each turn and nobody stopped the old one: 11 idle `red owner · convergence` sessions, 4 GB."""

    def test_n_turns_leave_exactly_one_session_each_stopped_when_its_turn_ends(self):
        self.linger()
        self.tick(dispatch=True)
        self.assertTrue(self.wait(lambda: self.rec().get("turn") == 1 and not self.rec().get("in_turn"), limit=40))
        for n, (rid, num) in enumerate([(41, 21), (42, 22)], start=2):
            self.main_red(rid, num, line=f"[info] - Spec{n} should work *** FAILED ***")
            self.tick(dispatch=True, advance=1)
            self.assertTrue(self.wait(lambda: self.rec().get("turn") == n and not self.rec().get("in_turn"), limit=40),
                            self.rec())
        owners = [s for s in self.sessions() if s["name"].startswith("red owner")]
        self.assertEqual(len({s["sessionId"] for s in owners}), 1, "every turn resumed the SAME session, never a copy")
        self.assertEqual([s for s in owners if s.get("pid")], [], "no spare left running after the turns")

    def spare(self, name):
        """A finished session whose process lives on (claude's spare), launched outside any wrapper."""
        env = {**os.environ, "FAKE_CLAUDE_LINGER": "1"}
        env.pop("FAKE_CLAUDE_BG_WAIT", None)
        r = subprocess.run([os.environ["AGENTCTL_CLAUDE"], "--bg", "x", "-n", name], capture_output=True, text=True,
                           env=env)
        bid = A.BG_STARTED.search(r.stdout).group(1)
        self.assertTrue(self.wait(lambda: (A.bg_session(A.claude_cli(), bid) or {}).get("state") == "blocked"))
        return bid

    def test_the_reaper_stops_spent_agentctl_sessions_never_a_live_turn_nor_a_session_it_did_not_launch(self):
        self.linger(FAKE_CLAUDE_BG_WAIT="60")  # the owner's live turn: waiting on its own task, its process alive
        self.tick(dispatch=True)
        self.assertTrue(self.wait(lambda: self.rec().get("in_turn") and (A.bg_session(
            A.claude_cli(), self.rec().get("bg_id") or "-") or {}).get("state") == "blocked", limit=40))
        live = self.rec()["bg_id"]
        old = self.spare(f"red owner · {FAM}")        # an earlier turn's leftover
        mine = self.spare("probe · not agentctl's")   # someone else's session
        acts = A.bg_reap_spent(time.time())
        self.assertEqual([a.split()[3] for a in acts], [old], acts)
        rows = {s["id"]: s for s in self.sessions()}
        self.assertTrue(rows[old].get("stopped"))
        self.assertTrue(rows[live].get("pid") and not rows[live].get("stopped"), "the live turn is never touched")
        self.assertTrue(rows[mine].get("pid") and not rows[mine].get("stopped"), "nor a session agentctl did not launch")

    def test_spent_is_idle_with_a_live_process_past_the_grace(self):
        me, t = os.getpid(), 10_000
        row = {"id": "aaaa1111", "sessionId": "aaaa1111-x", "kind": "background", "name": "red fixer #5 · main",
               "state": "blocked", "pid": me, "startedAt": 1_000_000}
        self.assertEqual(A.bg_spent([row], set(), t), [row])
        self.assertEqual(A.bg_spent([{**row, "state": "working"}], set(), t), [], "at work")
        self.assertEqual(A.bg_spent([{**row, "pid": None}], set(), t), [], "no process: nothing to free")
        self.assertEqual(A.bg_spent([row], {"aaaa1111"}, t), [], "a live wrapper's turn")
        self.assertEqual(A.bg_spent([{**row, "startedAt": (t - 5) * 1000}], set(), t), [], "just started")
        self.assertEqual(A.bg_spent([{**row, "kind": "interactive"}], set(), t), [])


class FixerTurnTest(LingerMixin, RedsBase):
    def test_a_fixer_turn_ending_with_its_process_alive_releases_its_red_with_the_verdict(self):
        self.linger(FAKE_CLAUDE_RESULT="DIAGNOSED: a closed-day notice became an empty card")
        self.tick(dispatch=True)
        name = A.red_lease(self.only()["data"]["key"])
        t0 = time.time()
        while not self.outcome(name).startswith("DIAGNOSED") and time.time() - t0 < 40:
            time.sleep(0.2)
        self.assertTrue(self.outcome(name).startswith("DIAGNOSED"), self.outcome(name))


if __name__ == "__main__":
    unittest.main()
