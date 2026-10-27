"""NEVER AN EMPTY COMMIT (2026-10-27): overlapping queue entries -- an agent queued a commit, queued it again, both
ran -- landed FOUR empty duplicate commits on movies main (317fba9b3, 1fddd8951, 1d554f051, 693e1be78/82f4d9f55): the
later entry's commit had already landed through the earlier one, and its rebase left an empty commit that was pushed.
Now: an entry already on main is LANDED (already on main via <commit>) with nothing pushed; a partly applied one keeps
only its non-empty commits; re-queueing supersedes the older entry; and the squash guard refuses an empty commit unless
it is a deliberate one with an `Allow-Empty:` trailer."""
import json, os, sys, unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from test_agentctl import A, Base, sh  # noqa: E402
from test_queue import QueueTest  # noqa: E402


def empty_commits_on_main(t):
    out = []
    for c in sh(t.r.root, "git", "--git-dir", t.r.bare, "rev-list", "--no-merges", "main").split():
        parents = sh(t.r.root, "git", "--git-dir", t.r.bare, "rev-list", "--parents", "-n", "1", c).split()[1:]
        if parents and sh(t.r.root, "git", "--git-dir", t.r.bare, "rev-parse", f"{c}^{{tree}}") == \
                sh(t.r.root, "git", "--git-dir", t.r.bare, "rev-parse", f"{parents[0]}^{{tree}}"):
            out.append(c)
    return out


class NoEmptyQueueTest(QueueTest):
    def key(self, eid):
        return eid.rsplit("-", 1)[-1]

    def test_requeueing_supersedes_the_older_entry_and_only_the_newest_runs(self):
        wt, _ = self.agent("a", "web/src/main/A.scala", "a\n", msg="a: one")
        e1, _ = self.queued(wt)
        e2, _ = self.queued(wt)
        es = self.entries()
        self.assertEqual(es[self.key(e1)]["status"], "superseded")
        self.assertEqual(es[self.key(e1)]["superseded_by"], e2)
        sup = [x for x in self.inbox() if x["kind"] == "SUPERSEDED"]
        self.assertEqual(len(sup), 1)
        self.assertIn(f"SUPERSEDED {e1} by {e2}", sup[0]["verdict"])
        self.assertEqual(sup[0]["cwd"], wt)
        self.assertEqual(self.check(e1), 0)
        self.assertEqual(self.pushed(), [], "the superseded entry is never checked or pushed")
        self.assertEqual(self.check(e2), 0)
        self.assertEqual(self.remote_log().count("a: one"), 1)
        self.assertEqual(empty_commits_on_main(self), [])

    def test_another_worktree_is_not_superseded(self):
        wa, _ = self.agent("a", "web/src/main/A.scala", "a\n")
        wb, _ = self.agent("b", "web/src/main/B.scala", "b\n")
        e1, _ = self.queued(wa)
        self.queued(wb)
        self.assertEqual(self.entries()[self.key(e1)]["status"], "queued")

    def test_an_entry_already_on_main_is_landed_via_that_commit_and_nothing_is_pushed(self):
        wa, _ = self.agent("a", "web/src/main/A.scala", "a\n", msg="a: one")
        wb, _ = self.agent("b", "web/src/main/A.scala", "a\n", msg="a: one, again")  # the same patch
        e1, _ = self.queued(wa)
        e2, _ = self.queued(wb)
        self.assertEqual(self.check(e1), 0)
        landed_via = sh(self.r.root, "git", "--git-dir", self.r.bare, "rev-parse", "main")
        n_pushes = len(self.pushed())
        self.assertEqual(self.check(e2), 0)
        self.assertEqual(len(self.pushed()), n_pushes, "no push for a change that is on main already")
        e = self.entries()[self.key(e2)]
        self.assertEqual((e["status"], e["landed_sha"]), ("landed", landed_via))
        v = [x for x in self.inbox() if x["kind"] == "LANDED" and x["cwd"] == wb]
        self.assertEqual(len(v), 1)
        self.assertRegex(v[0]["verdict"], rf"^LANDED [0-9a-f]{{40}} \(already on main via {landed_via}\)")
        self.assertEqual(empty_commits_on_main(self), [])
        self.assertNotIn("a: one, again", self.remote_log())

    def test_a_partly_applied_entry_keeps_only_its_non_empty_commits(self):
        wa, _ = self.agent("a", "web/src/main/A.scala", "a\n", msg="a: one")
        wb, x = self.agent("b", "web/src/main/A.scala", "a\n", msg="b: same as a")
        self.r.write("worker/src/main/Y.scala", "y\n", at=wb)
        y = self.r.commit("b: y", at=wb)
        e1, _ = self.queued(wa)
        e2, _ = self.queued(wb)
        self.check(e1)
        self.assertEqual(self.check(e2), 0)
        e = self.entries()[self.key(e2)]
        self.assertEqual(e["status"], "landed")
        self.assertEqual(len(e["commits"]), 1)
        self.assertEqual(sh(wb, "git", "log", "-1", "--format=%s", e["commits"][0]), "b: y")
        self.assertEqual([c for c, _v in e["dropped"]], [sh(wb, "git", "rev-parse", "HEAD~1")])
        self.assertEqual(self.remote_log()[:2], ["b: y", "a: one"])
        self.assertEqual(empty_commits_on_main(self), [])

    def test_legacy_duplicate_queued_entries_run_only_the_newest(self):
        wt, _ = self.agent("a", "web/src/main/A.scala", "a\n")
        e1, _ = self.queued(wt)
        d, _c, _w = A.waiting_state(self.r.path)
        old = self.entries()[self.key(e1)]
        dup = dict(old, id=old["id"][:-1] + "9", seq=9)  # queued before re-queueing replaced: both still queued
        dup.pop("branch")
        A.save_entry(d, dup)
        os.environ["AGENTCTL_CHECK_TREES"] = "0"  # no tree free: nothing is started, the dedupe is all that runs
        self.addCleanup(os.environ.pop, "AGENTCTL_CHECK_TREES", None)
        A.spawn_checks(self.r.path, d)
        es = self.entries()
        self.assertEqual((es[self.key(e1)]["status"], es["9"]["status"]), ("superseded", "queued"))


class NoEmptyWaitingTest(QueueTest):
    def test_a_waiting_entry_whose_change_reached_main_is_landed_not_pushed_again(self):
        self.reds = {"7"}
        os.environ["AGENTCTL_QUEUE"] = "0"
        wa, _ = self.agent("a", "web/src/main/A.scala", "a\n", msg="a: one")
        wb, _ = self.agent("b", "worker/src/main/B.scala", "b\n", msg="b: two")
        self.assertEqual(self.land(wa)[0], 0)
        self.assertEqual(self.land(wb)[0], 0)
        # meanwhile someone lands a's very change on main
        other, _ = self.agent("c", "web/src/main/A.scala", "a\n", msg="a: one, by hand")
        sh(other, "git", "push", "-q", "forgejo", "HEAD:main")
        via = sh(other, "git", "rev-parse", "HEAD")
        self.reds = set()
        A.waiting_run(self.r.path, once=True)
        es = self.entries()
        self.assertEqual((es["1"]["status"], es["1"]["landed_sha"]), ("landed", via))
        self.assertEqual(es["2"]["status"], "landed")
        self.assertEqual(self.remote_log()[:2], ["b: two", "a: one, by hand"])
        self.assertEqual(empty_commits_on_main(self), [])
        self.assertIn(f"already on main via {via}", [x for x in self.inbox() if x["cwd"] == wa][-1]["verdict"])
        self.assertEqual([x for x in self.inbox() if x["kind"] == "NOT_LANDED"], [])

    def test_joining_again_supersedes_the_older_waiting_entry(self):
        self.reds = {"7"}
        os.environ["AGENTCTL_QUEUE"] = "0"
        wa, _ = self.agent("a", "web/src/main/A.scala", "a\n", msg="a: one")
        self.assertEqual(self.land(wa)[0], 0)
        self.r.write("web/src/main/A2.scala", "a2\n", at=wa)
        self.r.commit("a: two", at=wa)
        self.assertEqual(self.land(wa)[0], 0)
        es = self.entries()
        self.assertEqual((es["1"]["status"], es["2"]["status"]), ("superseded", "waiting"))
        self.assertEqual(self.waiting_log(), ["a: two", "a: one"], "the newest entry carries both commits, once")
        self.reds = set()
        A.waiting_run(self.r.path, once=True)
        self.assertEqual(self.remote_log()[:2], ["a: two", "a: one"])
        self.assertEqual(empty_commits_on_main(self), [])

    def test_the_same_change_from_another_worktree_joins_nothing_new(self):
        self.reds = {"7"}
        os.environ["AGENTCTL_QUEUE"] = "0"
        wa, _ = self.agent("a", "web/src/main/A.scala", "a\n", msg="a: one")
        wb, _ = self.agent("b", "web/src/main/A.scala", "a\n", msg="a: one, copied")
        self.assertEqual(self.land(wa)[0], 0)
        rc, out = self.land(wb)
        self.assertEqual(rc, 0, out)
        self.assertIn("the same change already waits", out)
        self.assertEqual(self.waiting_log(), ["a: one"], "no empty commit on `waiting`")


class EmptyCommitGuardTest(Base):
    def check(self, cwd=None):
        return self.run_tool("prepush-check", cwd=cwd or self.r.path)

    def test_an_empty_commit_is_refused(self):
        sh(self.r.path, "git", "commit", "-q", "--allow-empty", "-m", "a duplicate that became empty")
        rc, out = self.check()
        self.assertEqual(rc, 1, out)
        self.assertIn("EMPTY commit", out)
        self.assertIn("a duplicate that became empty", out)

    def test_a_deliberate_empty_commit_with_the_trailer_passes(self):
        sh(self.r.path, "git", "commit", "-q", "--allow-empty", "-m",
           "retrigger CI\n\nAllow-Empty: a deliberate CI trigger")
        rc, out = self.check()
        self.assertEqual(rc, 0, out)

    def test_hook_mode_refuses_a_push_with_an_empty_commit(self):
        self.r.write("f1.txt", "real\n")
        self.r.commit("real change")
        sh(self.r.path, "git", "commit", "-q", "--allow-empty", "-m", "empty")
        head = sh(self.r.path, "git", "rev-parse", "HEAD")
        remote = sh(self.r.path, "git", "rev-parse", "forgejo/main")
        rc, out = self.run_tool("prepush-check", "--hook", "forgejo", "url",
                                stdin=f"refs/heads/main {head} refs/heads/main {remote}\n", cwd=self.r.path)
        self.assertEqual(rc, 1, out)
        self.assertIn("1 EMPTY commit", out)


def load_tests(loader, tests, pattern):
    """Only this file's own cases (the queue fixture's tests run in test_queue)."""
    out = unittest.TestSuite()
    for cls in (NoEmptyQueueTest, NoEmptyWaitingTest, EmptyCommitGuardTest):
        own = [n for n in vars(cls) if n.startswith("test_")]
        out.addTests(cls(n) for n in sorted(own))
    return out


if __name__ == "__main__":
    unittest.main()
