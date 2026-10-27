"""THE LAND QUEUE (the owner, 2026-10-27: "agents never wait on a land"): `land` rebases, guards and returns at once with
QUEUED <id>; the processor's checker runs the change's checks in a check tree of its own, within the governor, then
pushes it (green), puts it on `waiting` (red) or rejects it, every verdict to the inbox with the agent's worktree; under
pressure the checks run on ci-runner's check tier and the push carries the remote run for the hook to confirm."""
import os, sys, unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from test_agentctl import A, sh  # noqa: E402
from test_waiting import WaitingTest  # noqa: E402


class QueueTest(WaitingTest):
    def setUp(self):
        super().setUp()
        os.environ["AGENTCTL_QUEUE"] = "1"
        self.reds = set()
        # the push's hook records what the processor handed it
        hooks = os.path.join(self.tmp.name, "hooks")
        with open(os.path.join(hooks, "pre-push"), "w") as f:
            f.write(f'#!/bin/sh\ncat > /dev/null\necho "push remote=${{PREPUSH_REMOTE_RUN:-}}" >> "{self.pushes}"\n')
        self.saved_q = (A.gov_pressure, A.remote_check)

    def tearDown(self):
        A.gov_pressure, A.remote_check = self.saved_q
        os.environ["AGENTCTL_QUEUE"] = "0"
        super().tearDown()

    def queued(self, wt):
        rc, out = self.land(wt)
        self.assertEqual(rc, 0, out)
        self.assertRegex(out, r"QUEUED \S+-\d+ \([0-9a-f]{10}")
        eid = out.split("QUEUED ", 1)[1].split()[0]
        return eid, out

    def check(self, eid):
        return A.check_entry(self.r.path, eid)

    def pushed(self):
        return open(self.pushes).read().splitlines() if os.path.exists(self.pushes) else []

    def test_land_returns_at_queued_and_the_checker_lands_it_on_green(self):
        wt, sha = self.agent("a", "web/src/main/A.scala", "a\n", msg="a: one")
        eid, out = self.queued(wt)
        self.assertEqual(self.calls_of("--range"), [], "no check ran in land")
        self.assertEqual(self.entries()[eid.rsplit("-", 1)[-1]]["status"], "queued")
        self.assertEqual([e["kind"] for e in self.inbox()], ["QUEUED"])
        self.assertEqual(self.check(eid), 0)
        self.assertEqual(len(self.calls_of("--range")), 1, "its checks ran in the checker")
        self.assertEqual(self.remote_log()[0], "a: one")
        landed = [e for e in self.inbox() if e["kind"] == "LANDED"]
        self.assertEqual(len(landed), 1)
        self.assertEqual(landed[0]["cwd"], wt, "the verdict names the agent's worktree")
        self.assertEqual(len(self.watches), 1, "a detached watch")
        self.assertEqual(self.entries()[eid.rsplit("-", 1)[-1]]["status"], "landed")
        _d, clone, _w = A.waiting_state(self.r.path)
        self.assertTrue(os.path.isdir(f"{clone}-wt-check-1"), "checked in a check tree, kept warm")

    def test_red_main_the_checked_change_joins_waiting(self):
        self.reds = {"7"}
        wt, sha = self.agent("a", "web/src/main/A.scala", "a\n")
        eid, _ = self.queued(wt)
        self.assertEqual(self.check(eid), 0)
        self.assertEqual(len(self.calls_of("--checks-only")), 1)
        es = self.entries()
        self.assertEqual(es[eid.rsplit("-", 1)[-1]]["status"], "joined")
        waiting = [e for e in es.values() if e["status"] == "waiting"]
        self.assertEqual([e["worktree"] for e in waiting], [wt], "the waiting entry is the agent's")
        self.assertEqual(self.pushed(), [])
        self.assertIn("WAITING", [e["kind"] for e in self.inbox()])

    def test_failing_checks_are_not_landed_in_the_inbox(self):
        wt, _ = self.agent("a", "BROKEN", "x\n")
        eid, _ = self.queued(wt)
        self.assertEqual(self.check(eid), 1)
        bad = [e for e in self.inbox() if e["kind"] == "NOT_LANDED"]
        self.assertEqual(len(bad), 1)
        self.assertIn("pre-push checks failed", bad[0]["verdict"])
        self.assertEqual(bad[0]["cwd"], wt)
        self.assertEqual(self.pushed(), [])

    def test_main_moving_after_queued_is_rebased_in_the_check_tree(self):
        wt, _ = self.agent("a", "web/src/main/A.scala", "a\n", msg="a: one")
        eid, _ = self.queued(wt)
        other, _ = self.agent("b", "worker/src/main/B.scala", "b\n", msg="b: two")
        sh(other, "git", "push", "-q", "forgejo", "HEAD:main")
        self.assertEqual(self.check(eid), 0)
        self.assertEqual(self.remote_log()[:2], ["a: one", "b: two"])

    def test_a_conflict_with_the_moved_main_is_not_landed(self):
        wt, _ = self.agent("a", "web/src/main/Shared.scala", "mine\n")
        eid, _ = self.queued(wt)
        other, _ = self.agent("b", "web/src/main/Shared.scala", "theirs\n")
        sh(other, "git", "push", "-q", "forgejo", "HEAD:main")
        self.assertEqual(self.check(eid), 1)
        self.assertIn("no longer applies", [e for e in self.inbox() if e["kind"] == "NOT_LANDED"][0]["verdict"])

    def test_under_pressure_the_checks_run_on_ci_runner_and_the_push_names_the_run(self):
        self.r.write(".github/workflows/precommit-check.yml", "name: Pre-commit check\n")
        self.r.commit("the remote check workflow")
        self.r.push()
        os.remove(self.pushes)
        wt, _ = self.agent("a", "web/src/main/A.scala", "a\n")
        eid, _ = self.queued(wt)
        A.gov_pressure = lambda: "memory pressure 82%"
        seen = []

        def remote(repo, e, base, head, mode, own=None):
            seen.append((base, head, mode))
            e["remote_run"] = 4242
            return True, "precommit-check.yml run 4242 (#77): success"
        A.remote_check = remote
        self.assertEqual(self.check(eid), 0)
        self.assertEqual(len(seen), 1)
        self.assertEqual(self.calls_of("--range"), [], "nothing checked on the laptop")
        self.assertEqual(self.pushed(), ["push remote=4242"])
        self.assertIn("on ci-runner (memory pressure 82%)", [e for e in self.inbox() if e["kind"] == "LANDED"][0]["verdict"])

    def test_a_remote_check_without_a_verdict_is_checked_locally(self):
        self.r.write(".github/workflows/precommit-check.yml", "name: Pre-commit check\n")
        self.r.commit("the remote check workflow")
        self.r.push()
        os.remove(self.pushes)
        wt, _ = self.agent("a", "web/src/main/A.scala", "a\n")
        eid, _ = self.queued(wt)
        A.gov_pressure = lambda: "load 40 on 14 cores"
        A.remote_check = lambda repo, e, base, head, mode, own=None: (None, "no run started")
        self.assertEqual(self.check(eid), 0)
        self.assertEqual(len(self.calls_of("--range")), 1)
        self.assertEqual(self.pushed(), ["push remote="])

    def fake_forgejo(self, status, head):
        calls = []

        def api(o_r, method, path, params=None):
            calls.append((method, path))
            if path == "actions/runs":
                return {"workflow_runs": [{"id": 99, "commit_sha": head, "prettyref": "check/e1"}]}
            if path == "actions/runs/99":
                return {"id": 99, "index_in_repo": 7, "status": status, "html_url": "http://f/runs/7"}
            return None
        saved = (A.api, A.fetch_failed_log, A.slug)
        A.api, A.slug = api, (lambda repo: "o/movies")
        A.fetch_failed_log = lambda o_r, rid: "check\trun\t[error] BROKEN is in the tree\n"
        self.addCleanup(lambda: setattr(A, "api", saved[0]) or setattr(A, "fetch_failed_log", saved[1])
                        or setattr(A, "slug", saved[2]))
        return calls

    def test_remote_check_pushes_a_check_branch_reads_the_verdict_and_deletes_the_branch(self):
        head = sh(self.r.path, "git", "rev-parse", "HEAD").strip()
        calls = self.fake_forgejo("success", head)
        ok, out = A.remote_check(self.r.path, {"id": "e1"}, "base", head, "--checks-only")
        self.assertTrue(ok, out)
        self.assertIn("check/e1", sh(self.r.root, "git", "--git-dir", self.r.bare, "branch", "--list", "check/*"),
                      "pushed for the run (deleted through the API, faked here)")
        self.assertIn(("DELETE", "branches/check%2Fe1"), calls)
        calls = self.fake_forgejo("failure", head)
        ok, out = A.remote_check(self.r.path, {"id": "e1"}, "base", head, "--checks-only")
        self.assertFalse(ok)
        self.assertIn("[error] BROKEN is in the tree", out)
        self.assertIn(("DELETE", "branches/check%2Fe1"), calls)

    def test_follow_keeps_the_in_process_land(self):
        wt, sha = self.agent("a", "web/src/main/A.scala", "a\n")
        rc, out = self.land(wt, "--follow")
        self.assertIn(f"LANDED {sha}", out)
        self.assertNotIn("QUEUED", out)

    def test_cancel_drops_a_queued_change(self):
        wt, _ = self.agent("a", "web/src/main/A.scala", "a\n")
        eid, _ = self.queued(wt)
        rc, out = self.run_tool("waiting", "cancel", eid, cwd=self.r.path)
        self.assertEqual(rc, 0, out)
        self.assertEqual(self.check(eid), 0)
        self.assertEqual(self.pushed(), [], "a cancelled change is never checked or pushed")


def load_tests(loader, tests, pattern):
    """Only this file's own cases (QueueTest borrows WaitingTest's fixture, not its tests)."""
    return unittest.TestSuite(QueueTest(n) for n in sorted(vars(QueueTest)) if n.startswith("test_"))


if __name__ == "__main__":
    unittest.main()
