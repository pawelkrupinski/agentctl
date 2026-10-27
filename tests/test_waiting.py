"""agentctl waiting: while main is red a production change waits on ONE `waiting` branch (checked on the combined tip),
and the processor lands the whole branch in one push once main is green. Tools-only and Fixes-Red changes bypass it."""
import os, stat, sys, unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from test_agentctl import A, Base, sh  # noqa: E402

REPO_HOOK = """#!/bin/sh
# supports --checks-only (the checks without the red gate)
echo "$*" >> "{calls}"
if git ls-files | grep -q BROKEN; then echo "[error] BROKEN is in the tree"; exit 1; fi
exit 0
"""


class WaitingTest(Base):
    def setUp(self):
        super().setUp()
        t = self.tmp.name
        self.saved = (A.LAND_LOCK_DIR, A.WAITING_DIR, A.gate_reds, A.spawn_waiting_processor, A.await_entry,
                      A.time.sleep, A.watch_landed)
        A.LAND_LOCK_DIR, A.WAITING_DIR = os.path.join(t, "locks"), os.path.join(t, "waiting-state")
        self.reds = {"7"}
        A.gate_reds = lambda repo: set(self.reds)
        A.spawn_waiting_processor = lambda repo: None
        self.awaited = []
        A.await_entry = lambda repo, eid, close=None: (self.awaited.append(eid), (9, None, None))[1]
        A.time.sleep = lambda s: None
        A.watch_landed = lambda repo, sha, pushed_at, o_r=None, close=None: 0
        os.environ.pop("PREPUSH_SKIP", None)
        self.calls, self.pushes = os.path.join(t, "hook-calls"), os.path.join(t, "pushes")
        hooks = os.path.join(t, "hooks")
        os.makedirs(hooks)
        with open(os.path.join(hooks, "pre-push"), "w") as f:
            f.write(f'#!/bin/sh\ncat > /dev/null\necho push >> "{self.pushes}"\n')
        os.chmod(os.path.join(hooks, "pre-push"), 0o755)
        sh(self.r.path, "git", "config", "core.hooksPath", hooks)
        self.r.write("scripts/hooks/pre-push", REPO_HOOK.format(calls=self.calls))
        os.chmod(os.path.join(self.r.path, "scripts/hooks/pre-push"), 0o755)
        self.r.write("web/src/main/Shared.scala", "line 1\n")
        self.r.commit("the repo's hook and a shared source")
        self.r.push()
        for p in (self.pushes, self.calls):
            if os.path.exists(p):
                os.remove(p)

    def tearDown(self):
        (A.LAND_LOCK_DIR, A.WAITING_DIR, A.gate_reds, A.spawn_waiting_processor, A.await_entry, A.time.sleep,
         A.watch_landed) = self.saved
        super().tearDown()

    def agent(self, name, path, text, msg=None):
        wt = self.r.worktree(f"wt-{name}", branch=name)
        self.r.write(path, text, at=wt)
        return wt, self.r.commit(msg or f"{name}: {path}", at=wt)

    def land(self, wt):
        return self.run_tool("land", cwd=wt)

    def remote_log(self):
        return sh(self.r.root, "git", "--git-dir", self.r.bare, "log", "--format=%s", "main").splitlines()

    def waiting_log(self):
        return sh(self.r.path, "git", "log", "--format=%s", "forgejo/main..waiting").splitlines()

    def pushes_count(self):
        return len(open(self.pushes).read().split()) if os.path.exists(self.pushes) else 0

    def test_red_then_green_lands_every_waiting_change_in_one_push(self):
        wt1, _ = self.agent("a", "web/src/main/A.scala", "a\n")
        wt2, _ = self.agent("b", "worker/src/main/B.scala", "b\n")
        rc, out = self.land(wt1)
        self.assertEqual(rc, 9, out)
        self.assertRegex(out, r"WAITING [0-9a-f]{40} \(1 change queued on waiting")
        rc, out = self.land(wt2)
        self.assertIn("(2 changes queued on waiting", out)
        self.assertEqual(self.pushes_count(), 0, "nothing reaches main while it is red")
        self.assertEqual(self.waiting_log(), ["b: worker/src/main/B.scala", "a: web/src/main/A.scala"])
        self.assertIn("--checks-only", open(self.calls).read(), "the combined tip is checked, without the red gate")
        A.waiting_run(self.r.path, once=True)  # still red: nothing moves
        self.assertEqual(self.pushes_count(), 0)
        self.reds = set()
        A.waiting_run(self.r.path, once=True)
        self.assertEqual(self.pushes_count(), 1, "ONE push for the whole branch")
        self.assertEqual(self.remote_log()[:2], ["b: worker/src/main/B.scala", "a: web/src/main/A.scala"])
        self.assertFalse(A.git_ok(self.r.path, "rev-parse", "--verify", "-q", "refs/heads/waiting"), "the branch is deleted")
        d, _c, wt = A.waiting_state(self.r.path)
        self.assertFalse(os.path.isdir(wt))
        e1, e2 = A.waiting_entries(d, open_only=False)
        self.assertEqual([e1["status"], e2["status"]], ["landed", "landed"])
        rc, sha, _ = self.saved[4](self.r.path, e1["id"])  # the real await_entry: the waiting land's verdict
        self.assertEqual(rc, 0)
        self.assertIn(sha, sh(self.r.root, "git", "--git-dir", self.r.bare, "rev-list", "main"))

    def test_a_waiting_land_goes_on_to_landed_and_the_watch(self):
        A.await_entry = lambda repo, eid, close=None: (print("LANDED abc (with …)"), (0, "abc", 1.0))[1]
        watched = []
        A.watch_landed = lambda repo, sha, pushed_at, o_r=None, close=None: (watched.append(sha), 3)[1]
        wt, _ = self.agent("a", "web/src/main/A.scala", "a\n")
        rc, out = self.land(wt)
        self.assertEqual(rc, 3, out)
        self.assertEqual(watched, ["abc"])
        self.assertLess(out.index("WAITING"), out.index("LANDED"))

    def test_a_conflict_rejects_only_the_culprit(self):
        wt1, _ = self.agent("a", "web/src/main/Shared.scala", "line 1 by a\n")
        wt2, _ = self.agent("b", "web/src/main/Shared.scala", "line 1 by b\n")
        self.land(wt1)
        rc, out = self.land(wt2)
        self.assertEqual(rc, 1, out)
        self.assertIn("NOT LANDED: main is red, and the change conflicts", out)
        self.assertIn("web/src/main/Shared.scala", out)
        self.assertEqual(self.waiting_log(), ["a: web/src/main/Shared.scala"])

    def test_a_failing_check_rejects_only_that_change(self):
        wt1, _ = self.agent("a", "web/src/main/A.scala", "a\n")
        wt2, _ = self.agent("b", "web/src/main/BROKEN.scala", "b\n")
        self.land(wt1)
        rc, out = self.land(wt2)
        self.assertEqual(rc, 1, out)
        self.assertIn("NOT LANDED: the checks failed on `waiting`", out)
        self.assertIn("BROKEN is in the tree", out)
        self.assertEqual(self.waiting_log(), ["a: web/src/main/A.scala"])

    def test_main_moving_while_waiting_rebases_the_branch(self):
        wt1, _ = self.agent("a", "web/src/main/A.scala", "a\n")
        self.land(wt1)
        self.r.write("docs/x.md", "someone else\n")
        self.r.commit("main moved")
        self.r.push()
        os.remove(self.pushes)
        self.reds = set()
        A.waiting_run(self.r.path, once=True)
        self.assertEqual(self.remote_log()[:2], ["a: web/src/main/A.scala", "main moved"])
        self.assertEqual(self.pushes_count(), 1)

    def test_main_moving_into_a_conflict_rejects_that_change_and_lands_the_rest(self):
        wt1, _ = self.agent("a", "web/src/main/Shared.scala", "line 1 by a\n")
        wt2, _ = self.agent("b", "web/src/main/B.scala", "b\n")
        self.land(wt1)
        self.land(wt2)
        self.r.write("web/src/main/Shared.scala", "line 1 on main\n")
        self.r.commit("main changed the shared line")
        self.r.push()
        self.reds = set()
        A.waiting_run(self.r.path, once=True)
        d, _c, _w = A.waiting_state(self.r.path)
        a, b = A.waiting_entries(d, open_only=False)
        self.assertEqual((a["status"], b["status"]), ("rejected", "landed"))
        self.assertIn("no longer applies", a["why"])
        self.assertEqual(self.remote_log()[0], "b: web/src/main/B.scala")

    def test_an_enqueue_rebuilds_waiting_on_the_current_main_before_judging_a_conflict(self):
        # `waiting` was built on an older main; main has since changed Shared.scala (a tools-only push, say), and the
        # new change, rebased on the CURRENT main, edits that same line again. Against the old base its pick
        # conflicts; against the rebuilt one it applies (movies, 2026-10-27: a false conflict on bounded-step-test.sh).
        wt1, _ = self.agent("a", "web/src/main/A.scala", "a\n")
        self.land(wt1)
        self.r.write("web/src/main/Shared.scala", "line 1 on main\n")
        self.r.commit("main changed the shared line")
        self.r.push()
        wt2 = self.r.worktree("wt-b", branch="b")
        self.r.write("web/src/main/Shared.scala", "line 1 by b\n", at=wt2)
        self.r.commit("b: Shared.scala", at=wt2)
        rc, out = self.land(wt2)
        self.assertNotIn("conflicts", out)
        self.assertIn("(2 changes queued on waiting", out)
        main = sh(self.r.path, "git", "rev-parse", "forgejo/main")
        self.assertEqual(sh(self.r.path, "git", "merge-base", "waiting", "forgejo/main"), main,
                         "waiting sits on the current main")
        self.assertEqual(self.waiting_log(), ["b: Shared.scala", "a: web/src/main/A.scala"])

    def test_an_enqueue_onto_a_stale_waiting_rejects_only_entries_that_no_longer_apply(self):
        wt1, _ = self.agent("a", "web/src/main/Shared.scala", "line 1 by a\n")
        self.land(wt1)
        self.r.write("web/src/main/Shared.scala", "line 1 on main\n")
        self.r.commit("main changed the shared line")
        self.r.push()
        wt2, _ = self.agent("b", "web/src/main/B.scala", "b\n")
        rc, out = self.land(wt2)
        self.assertIn("(1 change queued on waiting", out)
        d, _c, _w = A.waiting_state(self.r.path)
        a, b = A.waiting_entries(d, open_only=False)
        self.assertEqual((a["status"], b["status"]), ("rejected", "waiting"))
        self.assertIn("no longer applies", a["why"])

    def test_tools_only_bypasses_waiting_while_red(self):
        wt, sha = self.agent("t", "scripts/x.sh", "echo\n")
        rc, out = self.land(wt)
        self.assertEqual(rc, 0, out)
        self.assertIn(f"LANDED {sha}", out)
        self.assertNotIn("WAITING", out)
        self.assertEqual(self.remote_log()[0], "t: scripts/x.sh")

    def test_a_fixes_red_fix_bypasses_waiting(self):
        wt, sha = self.agent("f", "web/src/main/A.scala", "fix\n", msg="fix\n\nFixes-Red: 7")
        rc, out = self.land(wt)
        self.assertEqual(rc, 0, out)
        self.assertIn(f"LANDED {sha}", out)
        self.assertNotIn("WAITING", out)

    def test_green_main_pushes_directly(self):
        self.reds = set()
        wt, sha = self.agent("g", "web/src/main/A.scala", "a\n")
        rc, out = self.land(wt)
        self.assertEqual(rc, 0, out)
        self.assertIn(f"LANDED {sha}", out)
        self.assertNotIn("WAITING", out)

    def test_status_and_cancel(self):
        wt1, _ = self.agent("a", "web/src/main/A.scala", "a\n")
        wt2, _ = self.agent("b", "web/src/main/B.scala", "b\n")
        self.land(wt1)
        self.land(wt2)
        rc, out = self.run_tool("waiting", "status", cwd=self.r.path)
        self.assertEqual(out.count("commit(s), tip"), 2, out)
        self.assertIn("gate: RED (7)", out)
        d, _c, _w = A.waiting_state(self.r.path)
        first = A.waiting_entries(d)[0]["id"]
        rc, out = self.run_tool("waiting", "cancel", first, cwd=self.r.path)
        self.assertEqual(rc, 0, out)
        self.assertEqual(self.waiting_log(), ["b: web/src/main/B.scala"])
        self.assertEqual([e["status"] for e in A.waiting_entries(d, open_only=False)], ["cancelled", "waiting"])

    def test_the_queue_points_at_waiting(self):
        rc, out = self.run_tool("queue", "add", cwd=self.r.path)
        self.assertEqual(rc, 2)
        self.assertIn("waiting", out)


if __name__ == "__main__":
    unittest.main()
