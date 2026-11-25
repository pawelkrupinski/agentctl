"""Scenarios of issues that already pass on current code (classification 2026-11-19), kept as guards."""
import json, os, subprocess, sys, unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from test_agentctl import A  # noqa: E402
from test_red import run  # noqa: E402
from test_reds import RedsBase  # noqa: E402


class AlreadyFixedGuardTest(RedsBase):
    def land(self, msg, name="repo-wt-fix"):
        wt = self.r.worktree(name, branch=name)
        self.r.write("fix.txt", f"{name}\n", at=wt)
        sha = self.r.commit(msg, at=wt)
        self.r.push(at=wt, ref=f"{sha}:main")
        return wt, sha

    def queue_run(self, sha, status="queued"):
        self.put("runs.json", [run(50, 23, "Main", "main.yml", sha, status=status, created=A.iso_at(self.t[0]))]
                 + json.load(open(os.path.join(self.d, "runs.json"))))


    def outside_capture_red(self):
        """Identity capture #2104 red on main (an outside red: no gate run ever proves its fix)."""
        self.put("runs.json", [run(40, 20, "Main", "main.yml", self.sha), run(39, 19, "Identity model convergence",
                                                                             "identity-model-convergence.yml", self.sha),
                               run(70, 2104, "Identity capture", "identity-capture.yml", self.sha, conclusion="failure")])
        self.put("run-70.json", {"jobs": [{"name": "capture (uk)", "status": "completed", "conclusion": "failure",
                                           "steps": [{"name": "Run", "conclusion": "failure"}]}], "headSha": self.sha,
                                 "url": "u", "number": 2104, "status": "completed", "conclusion": "failure",
                                 "workflowName": "Identity capture"})
        self.put("run-70.log", "capture (uk)\tRun\t2026-10-27T08:00:00Z [info] - CaptureSpec *** FAILED ***\n")
        self.r.write(".github/workflows/identity-capture.yml",
                     "name: Identity capture\non:\n  schedule:\n    - cron: '0 3 * * 1'\n  workflow_dispatch:\n")
        self.r.commit("the workflow")
        self.r.push()
        self.tick()
        return self.only()

    def queued_entry(self, fixes_msg, status, path="capture.txt"):
        wt = self.r.worktree("repo-wt-queued", branch="repo-wt-queued")
        self.r.write(path, "queued\n", at=wt)
        sha = self.r.commit(fixes_msg, at=wt)
        d = A.waiting_state(self.r.path)[0]
        e, _ = A.queue_change(A.CurrentStore(A.process_store), d, {"worktree": wt, "branch": "repo-wt-queued", "head": sha, "commits": [sha]})
        e["status"] = status
        A.save_entry(d, e)
        return e


    def queued_entry(self, fixes_msg, status, path="capture.txt"):
        wt = self.r.worktree("repo-wt-queued", branch="repo-wt-queued")
        self.r.write(path, "queued\n", at=wt)
        sha = self.r.commit(fixes_msg, at=wt)
        d = A.waiting_state(self.r.path)[0]
        e, _ = A.queue_change(A.CurrentStore(A.process_store), d, {"worktree": wt, "branch": "repo-wt-queued", "head": sha, "commits": [sha]})
        e["status"] = status
        A.save_entry(d, e)
        return e


    def test_20261112T154224_e9d803_a_trailered_fix_on_main_with_its_proving_run_queued_is_fix_pending(self):
        self.tick()
        import subprocess
        other = os.path.join(self.tmp.name, "laptop")
        subprocess.run(["git", "clone", "-q", self.r.bare, other], check=True)
        self.r.write("fix.txt", "from the laptop\n", at=other)
        sha = self.r.commit("fix FooSpec\n\nFixes-Red: 20", at=other)
        subprocess.run(["git", "-C", other, "push", "-q", "origin", "HEAD:main"], check=True)
        self.queue_run(sha, "queued")
        for _ in range(3):
            self.tick(advance=120)
        it = self.only()
        self.assertEqual((it["state"], (it["data"].get("fix") or {}).get("sha")), ("fix-pending", sha))

    def test_20261101T042721_dc0549_an_outside_reds_queued_fixes_red_change_holds_it_waiting_then_checking(self):
        it = self.outside_capture_red()
        e = self.queued_entry("fix CaptureSpec\n\nFixes-Red: 2104", "waiting")
        for status in ("waiting", "checking"):
            d = A.waiting_state(self.r.path)[0]
            e["status"] = status
            A.save_entry(d, e)
            self.tick(advance=120)
            it = self.only()
            self.assertEqual((it["state"], (it["data"].get("fix") or {}).get("entry")), ("fix-pending", e["id"]), status)
            self.assertEqual(self.st.offers("fixer"), [])

    def test_20261103T232907_ca63b2_a_fixers_landed_sha_with_its_gate_run_in_progress_is_not_told_it_did_not_fix_it(self):
        self.tick()
        key = self.only()["data"]["key"]
        wt, sha = self.land("fix FooSpec, no trailer")  # the fixer's own LANDED names it; nothing else links it
        lease = self.st.acquire(A.red_lease(key), "laptop:claim:fixer@" + wt, 1800, 3600,
                                meta={"kind": "claim", "worktree": wt})
        self.tick(advance=5)
        self.assertEqual(self.only()["state"], "leased")
        self.queue_run(sha, "in_progress")  # the gate's run on the LANDED sha, still running
        self.st.release(A.red_lease(key), lease["epoch"], lease["secret"], outcome=f"LANDED {sha}")
        self.tick(advance=5)
        it = self.only()
        self.assertEqual((it["state"], (it["data"].get("fix") or {}).get("sha")), ("fix-pending", sha),
                         "nothing has disproved the fix: wait for the run on it")


if __name__ == "__main__":
    unittest.main()
