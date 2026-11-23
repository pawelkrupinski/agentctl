"""`land --until-done` (the owner, 2026-11-18): agents kept ending badly -- stopping after a rebase without landing,
partial or repeated reports, worktrees left behind, `git branch -D` by hand after a landing rebased their commits. The
mode owns the whole tail: it submits (or finds its queue entry again, by worktree and head), waits for the verdict,
bounded, and then either says exactly why it did not land and the next command (exit 1), or proves every local commit's
patch is on origin/main, removes the clean, own worktree and its branch and prints `DONE <sha> ... worktree removed`."""
import contextlib, fcntl, os, subprocess, sys, unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from test_agentctl import A, sh  # noqa: E402
import test_queue  # noqa: E402 (a module, so the loader runs QueueTest's own tests there only)


class UntilDoneTest(test_queue.QueueTest):
    def setUp(self):
        super().setUp()
        self.reds = set()
        self.d = A.waiting_state(self.r.path)[0]
        self.sleeps = 0
        self.before_drive = None
        self.clock.on_sleep = self.drive_queue  # the await's poll: the processor moves the queue meanwhile
        self.saved_cwds = A.process_cwds
        A.process_cwds = lambda: {}  # no process of this machine works in the test's worktrees

    def tearDown(self):
        A.process_cwds = self.saved_cwds
        super().tearDown()

    def drive_queue(self, _secs):
        """One processor step per poll: the executor's checks of each queued change, then the tick's push."""
        self.sleeps += 1
        if self.before_drive:
            hook, self.before_drive = self.before_drive, None
            hook()
        for e in A.waiting_entries(self.d, open_only=False):
            if e["status"] in ("queued", "checking"):
                A.check_entry(self.r.path, e["id"], self.clock)
            elif e["status"] == "checked":
                A.push_change(self.r.path, self.d, e, A.Clock())

    def until_done(self, wt, *extra):
        return self.land(wt, "--until-done", *extra)

    def branches(self):
        return sh(self.r.path, "git", "branch", "--format=%(refname:short)").split()

    def test_it_lands_then_removes_the_worktree_and_the_branch_its_rebase_left_unmerged(self):
        wt, sha = self.agent("a", "web/src/main/A.scala", "a\n", msg="a: one")
        # main moves after the QUEUED line, before the check: the change lands REBASED, its local commits never merged
        # by ancestry -- the branch needed `git branch -D` by hand
        other, _ = self.agent("m", "docs/m.md", "m\n", msg="main moves")
        self.before_drive = lambda: sh(other, "git", "push", "-q", "origin", "HEAD:main")
        rc, out = self.until_done(wt)
        self.assertEqual(rc, 0, out)
        self.assertIn("QUEUED ", out)
        self.assertRegex(out.strip().splitlines()[-1], r"^DONE [0-9a-f]{40} .*worktree removed")
        landed = sh(self.r.root, "git", "--git-dir", self.r.bare, "rev-parse", "main")
        self.assertIn(f"DONE {landed}", out, "the sha on main, not the local one")
        self.assertNotEqual(landed, sha)
        self.assertFalse(os.path.isdir(wt), "the worktree is removed")
        self.assertNotIn("a", self.branches(), "its branch deleted after the patch check")
        self.assertIn("m", self.branches(), "another agent's branch is untouched")

    def test_not_landed_prints_the_reason_and_the_next_command_and_keeps_the_worktree(self):
        wt, _ = self.agent("b", "web/src/main/BROKEN.scala", "b\n", msg="b: broken")
        rc, out = self.until_done(wt)
        self.assertEqual(rc, 1, out)
        self.assertIn("NOT LANDED: the pre-push checks failed", out)
        self.assertIn("next: fix", out)
        self.assertIn("~/bin/agentctl land --until-done", out.strip().splitlines()[-1])
        self.assertTrue(os.path.isdir(wt))
        self.assertIn("b", self.branches())

    def test_a_conflict_names_the_rebase_as_the_next_command(self):
        wt, _ = self.agent("c", "web/src/main/Shared.scala", "mine\n", msg="c: shared")
        other, _ = self.agent("o", "web/src/main/Shared.scala", "theirs\n", msg="o: shared")
        self.before_drive = lambda: sh(other, "git", "push", "-q", "origin", "HEAD:main")
        rc, out = self.until_done(wt)
        self.assertEqual(rc, 1, out)
        self.assertIn("no longer applies", out)
        self.assertIn("git -C", out)
        self.assertIn("rebase origin/main", out)
        self.assertTrue(os.path.isdir(wt))

    def test_called_again_after_an_interruption_it_resumes_its_entry_and_never_queues_twice(self):
        wt, _ = self.agent("r", "web/src/main/R.scala", "r\n", msg="r: resume")
        self.clock.on_sleep = None  # nothing moves: the deadline ends the first call
        rc, out = self.until_done(wt, "--deadline", "0")
        self.assertEqual(rc, 2, out)
        self.assertIn("NOT DONE", out)
        self.assertIn("land --until-done", out, "it says how to resume")
        eid = out.split("QUEUED ", 1)[1].split()[0]
        self.clock.on_sleep = self.drive_queue
        rc, out = self.until_done(wt)
        self.assertEqual(rc, 0, out)
        self.assertIn(f"RESUMING {eid}", out)
        self.assertNotIn("QUEUED ", out, "the same change is never submitted twice")
        self.assertEqual(len(A.waiting_entries(self.d, open_only=False)), 1)
        self.assertRegex(out.strip().splitlines()[-1], r"^DONE ")
        self.assertFalse(os.path.isdir(wt))
        rc, out = self.land(self.r.path, "status")  # and a third call from a gone worktree is no crash: nothing to do
        self.assertEqual(rc, 0, out)

    def test_a_verdict_decided_while_nothing_waited_is_given_by_the_next_call_and_by_land_status(self):
        """2026-11-19: subagents' `--until-done` waits died with a Claude session restart and they handed back "queued,
        waiting". The verdict is durable: decided while no call waited, the next call prints it (a rejection is never
        queued again unchanged), and `land status <entry>` gives it after the fact."""
        wt, _ = self.agent("v", "web/src/main/BROKEN.scala", "v\n", msg="v: broken")
        self.clock.on_sleep = None
        rc, out = self.until_done(wt, "--deadline", "0")
        self.assertEqual(rc, 2, out)
        eid = out.split("QUEUED ", 1)[1].split()[0]
        for _ in range(3):  # the processor decides it while no --until-done waits
            self.drive_queue(0)
        self.assertEqual(A.open_store().item("change", eid)["state"], "rejected")
        A.project_events()  # its exit event's inbox entry names the worktree and the change
        told = [x for x in self.inbox() if x.get("item_id") == eid and x["kind"] == "NOT_LANDED"]
        self.assertEqual([x["cwd"] for x in told], [wt], self.inbox())
        rc, out = self.until_done(wt)
        self.assertEqual(rc, 1, out)
        self.assertIn("NOT LANDED: the pre-push checks failed", out)
        self.assertNotIn("QUEUED ", out, "a decided change is reported, never submitted again unchanged")
        self.assertEqual(len(A.waiting_entries(self.d, open_only=False)), 1)
        rc, out = self.land(self.r.path, "status", eid.rsplit("-", 1)[-1])
        self.assertEqual(rc, 0, out)
        self.assertIn(f"{eid}  rejected", out)
        self.assertIn("NOT LANDED: the pre-push checks failed", out)
        self.assertIn(wt, out)

    def test_a_rejection_decided_before_main_moved_is_checked_again_not_replayed(self):
        """2026-11-21, movies-devpanel-oauth: rejected at 17:02 (a main-side bug, fixed on main since), a new
        `land --until-done` at 17:35 at the same head replayed that NOT LANDED although main had moved: the verdict no
        longer held, and the agent rebased by hand. A verdict decided on another main is checked again."""
        wt, _ = self.agent("mv", "web/src/main/BROKEN.scala", "v\n", msg="mv: broken")
        self.clock.on_sleep = None
        rc, out = self.until_done(wt, "--deadline", "0")
        self.assertEqual(rc, 2, out)
        eid = out.split("QUEUED ", 1)[1].split()[0]
        for _ in range(3):  # decided while no --until-done waits
            self.drive_queue(0)
        self.assertEqual(A.open_store().item("change", eid)["state"], "rejected")
        other, _ = self.agent("mm", "docs/mm.md", "mm\n", msg="main moves")
        sh(other, "git", "push", "-q", "origin", "HEAD:main")
        self.clock.on_sleep = self.drive_queue
        rc, out = self.until_done(wt)
        self.assertNotIn("DECIDED", out, "a verdict decided on an older main is no verdict on this one")
        self.assertIn("QUEUED ", out, "the change is checked again")
        self.assertEqual(len(A.waiting_entries(self.d, open_only=False)), 2)

    def test_a_rejection_a_waiter_already_received_is_not_replayed_as_undelivered(self):
        """The same lane: its first `--until-done` had printed the NOT LANDED at 17:02, yet the next call said it was
        DECIDED "while no --until-done waited". A verdict once delivered is never replayed: the next call checks
        the change again."""
        wt, _ = self.agent("dl", "web/src/main/BROKEN.scala", "d\n", msg="dl: broken")
        rc, out = self.until_done(wt)
        self.assertEqual(rc, 1, out)
        self.assertIn("NOT LANDED: the pre-push checks failed", out)
        eid = out.split("QUEUED ", 1)[1].split()[0]
        self.assertTrue(A.open_store().item("change", eid)["data"].get("delivered_at"), "its waiter took delivery")
        rc, out = self.until_done(wt)
        self.assertNotIn("while no --until-done waited", out)
        self.assertIn("QUEUED ", out, "the change is checked again")
        self.assertEqual(len(A.waiting_entries(self.d, open_only=False)), 2)

    def test_land_status_of_a_landed_entry_gives_its_sha(self):
        wt, _ = self.agent("s", "web/src/main/S.scala", "s\n", msg="s: status")
        rc, out = self.until_done(wt, "--keep-worktree")
        self.assertEqual(rc, 0, out)
        eid = out.split("QUEUED ", 1)[1].split()[0]
        landed = sh(self.r.root, "git", "--git-dir", self.r.bare, "rev-parse", "main")
        rc, out = self.land(self.r.path, "status", eid)
        self.assertEqual(rc, 0, out)
        self.assertIn(f"LANDED {landed}", out)
        rc, out = self.land(self.r.path, "status", "no-such-entry")
        self.assertEqual(rc, 1, out)

    def test_already_landed_it_only_cleans_up(self):
        wt, _ = self.agent("l", "web/src/main/L.scala", "l\n", msg="l: landed")
        rc, out = self.until_done(wt, "--keep-worktree")
        self.assertEqual(rc, 0, out)
        self.assertIn("worktree kept (--keep-worktree)", out)
        self.assertTrue(os.path.isdir(wt))
        rc, out = self.until_done(wt)
        self.assertEqual(rc, 0, out)
        self.assertNotIn("QUEUED ", out)
        self.assertRegex(out.strip().splitlines()[-1], r"^DONE .*worktree removed")
        self.assertFalse(os.path.isdir(wt))

    def test_a_dirty_worktree_is_never_removed(self):
        wt, _ = self.agent("d", "web/src/main/D.scala", "d\n", msg="d: dirty after")

        def dirty():
            with open(os.path.join(wt, "notes.txt"), "w") as f:
                f.write("untracked work\n")
        self.before_drive = dirty
        rc, out = self.until_done(wt)
        self.assertEqual(rc, 3, out)
        self.assertRegex(out, r"LANDED [0-9a-f]{40}")
        self.assertIn("not clean", out)
        self.assertTrue(os.path.isfile(os.path.join(wt, "notes.txt")))
        self.assertIn("d", self.branches())

    def test_a_foreign_worktree_is_never_removed(self):
        wt, _ = self.agent("f", "web/src/main/F.scala", "f\n", msg="f: foreign")
        os.environ["CLAUDE_CODE_SESSION_ID"] = "session-one"
        rc, out = self.until_done(wt, "--deadline", "0")
        self.assertEqual(rc, 2, out)
        os.environ["CLAUDE_CODE_SESSION_ID"] = "session-two"  # another agent calls it in that worktree
        rc, out = self.until_done(wt)
        self.assertEqual(rc, 3, out)
        self.assertIn("not this session's", out)
        self.assertTrue(os.path.isdir(wt))

    def holder(self, wt, session):
        """A real process working in wt (a kept server's stand-in) whose environment carries `session`; the machine's
        cwd scan reports it there while it lives."""
        env = {k: v for k, v in os.environ.items() if k != "CLAUDE_CODE_SESSION_ID"}
        if session:
            env["CLAUDE_CODE_SESSION_ID"] = session
        p = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(600)"], cwd=wt, env=env)
        self.addCleanup(lambda: (p.poll() is None and p.kill(), p.wait()))
        A.process_cwds = lambda: {p.pid: os.path.realpath(wt)} if p.poll() is None else {}
        return p

    def own_session(self, sid):
        saved = os.environ.get("CLAUDE_CODE_SESSION_ID")
        os.environ["CLAUDE_CODE_SESSION_ID"] = sid
        self.addCleanup(lambda: os.environ.pop("CLAUDE_CODE_SESSION_ID") if saved is None
                        else os.environ.__setitem__("CLAUDE_CODE_SESSION_ID", saved))

    def test_its_own_server_holding_the_worktree_is_stopped_then_the_worktree_removed(self):
        # 2026-11-18: LANDED 94a33a968, no DONE -- the agent's kept sbt server still worked in the worktree
        self.own_session("session-mine")
        wt, _ = self.agent("s", "web/src/main/S.scala", "s\n", msg="s: server kept")
        server = self.holder(wt, "session-mine")
        rc, out = self.until_done(wt)
        self.assertEqual(rc, 0, out)
        self.assertIn(f"stopped pid {server.pid} of this session", out)
        self.assertIsNotNone(server.poll(), "the caller's server is stopped")
        self.assertRegex(out.strip().splitlines()[-1], r"^DONE .*worktree removed")
        self.assertFalse(os.path.isdir(wt))

    def test_a_foreign_process_holding_the_worktree_is_named_never_stopped(self):
        self.own_session("session-mine")
        os.environ["AGENTCTL_FREE_WAIT"] = "1"
        self.addCleanup(os.environ.pop, "AGENTCTL_FREE_WAIT")
        wt, _ = self.agent("g", "web/src/main/G.scala", "g\n", msg="g: someone else in it")
        for session in ("session-other", None):  # another session's, and one with no session at all
            with self.subTest(session=session):
                other = self.holder(wt, session)
                rc, out = self.until_done(wt)
                self.assertEqual(rc, 4, out)
                self.assertRegex(out, r"LANDED [0-9a-f]{40}")
                self.assertIn("NOT DONE", out)
                self.assertIn(f"pid {other.pid}: ", out, "it names the holder")
                self.assertIn(f"kill {other.pid}", out)
                self.assertIn("land --until-done", out.strip().splitlines()[-1], "and the command to finish")
                self.assertIsNone(other.poll(), "another's process is never signalled")
                self.assertTrue(os.path.isdir(wt))
                self.assertIn("g", self.branches())
                other.kill()
                other.wait()
        rc, out = self.until_done(wt)  # once it is gone, the same command finishes
        self.assertEqual(rc, 0, out)
        self.assertFalse(os.path.isdir(wt))

    def test_the_root_checkout_is_never_removed(self):
        self.r.write("web/src/main/Root.scala", "r\n")
        self.r.commit("root: a change")
        rc, out = self.until_done(self.r.path)
        self.assertEqual(rc, 3, out)
        self.assertIn("root checkout", out)
        self.assertTrue(os.path.isdir(self.r.path))

    def test_another_entrys_precheck_refusal_while_it_waits_is_never_its_verdict(self):
        """2026-11-18: an agent's `--until-done` (split-credit) was read as reporting another change's precheck
        refusal (pure-move). It follows ITS entry only: entry B, of the same repo, refused and cancelled while A waits,
        is no verdict of A's; A keeps waiting and reports only its own LANDED."""
        spawned, saved_spawn = [], A.fast_precheck_spawn
        A.fast_precheck_spawn = lambda argv, **kw: spawned.append(argv)
        self.addCleanup(setattr, A, "fast_precheck_spawn", saved_spawn)
        wt_a, _ = self.agent("a", "web/src/main/A.scala", "a\n", msg="a: mine")
        wt_b, _ = self.agent("b", "web/src/main/B.scala", "b\n", msg="b: theirs")
        self.r.write("scripts/land-precheck", "#!/bin/sh\necho 'PRECHECK FAIL: lints (3 s)'\nexit 1\n", at=wt_b)
        os.chmod(os.path.join(wt_b, "scripts/land-precheck"), 0o755)
        self.r.commit("b: a precheck that refuses", at=wt_b)
        refused = []

        def b_refused_meanwhile():
            rc, out = self.land(wt_b)  # B queues beside A, and its detached precheck refuses it
            self.assertEqual(rc, 0, out)
            refused.append(out.split("QUEUED ", 1)[1].split()[0])
            rc, out = self.run_tool(*spawned[0][2:], cwd=wt_b)
            self.assertEqual(rc, 1, out)
            self.assertIn(f"NOT LANDED: the fast precheck refused {refused[0]}", out)
        self.before_drive = b_refused_meanwhile
        rc, out = self.until_done(wt_a)
        self.assertEqual(rc, 0, out)
        self.assertTrue(refused, "B was refused while A waited")
        eid_a = out.split("QUEUED ", 1)[1].split()[0]
        self.assertNotIn(refused[0], out, "nothing of B's in A's output")
        self.assertNotIn("NOT LANDED", out)
        self.assertRegex(out, rf"LANDED [0-9a-f]{{40}} \(queued {eid_a}\)")
        self.assertRegex(out.strip().splitlines()[-1], rf"^DONE [0-9a-f]{{40}} \(queued {eid_a}\)")
        self.assertTrue(os.path.isdir(wt_b), "B's worktree is B's")

    def test_a_second_land_into_the_same_output_file_does_not_start(self):
        """The 2026-11-18 cause: two subagents of one session ran `land --until-done > $SCRATCH/land.log` -- the same
        file -- and one read the other's refusal there as its own. A land whose output file another running land
        writes does not start, and says so in no verdict's words; a land into a file of its own runs."""
        wt, _ = self.agent("o", "web/src/main/O.scala", "o\n", msg="o: shared log")
        log = os.path.join(self.tmp.name, "land.log")
        with open(log, "w") as first:  # the earlier land, still running, writing this file
            fcntl.flock(first.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            for mode in ("--until-done", "--follow"):
                with self.subTest(mode=mode):
                    rc = self.land_into(log, wt, mode)
                    text = open(log).read()
                    self.assertEqual(rc, 5, text)
                    self.assertIn("did not start", text)
                    self.assertIn(os.path.realpath(log), text)
                    self.assertFalse([l for l in text.splitlines() if A.VERDICT_LINE.match(l)], text)
                    self.assertEqual(A.waiting_entries(self.d, open_only=False), [], "nothing queued")
        rc = self.land_into(log, wt, "--until-done")  # the earlier land gone: the file is this one's
        self.assertEqual(rc, 0, open(log).read())
        self.assertRegex(open(log).read().strip().splitlines()[-1], r"^DONE ")

    def land_into(self, path, wt, *argv):
        """`land` run in wt with stdout and stderr redirected to the file `path`, as a shell's `> path 2>&1`."""
        old = os.getcwd()
        with open(path, "a") as f:
            try:
                os.chdir(wt)
                with contextlib.redirect_stdout(f), contextlib.redirect_stderr(f):
                    return A.main(["land", *argv], clock=self.clock)
            finally:
                os.chdir(old)


test_queue.own_tests_only(UntilDoneTest, test_queue.QueueTest)  # its fixture, not QueueTest's tests

if __name__ == "__main__":
    unittest.main()
