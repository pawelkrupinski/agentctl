"""THE BATCH TIP'S DRIFT CHECK (G5, convergence-failures-2026-10-29 §4: E4's 13-commit and E9's 24-commit batches masked
which identity change broke convergence). A batch with more than one identity-model commit runs movies'
scripts/identity-drift-ci on its tip before the push; no script: a logged no-op; DRIFT RED splits the batch."""
import os, shutil, stat, sys, tempfile, unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from test_agentctl import A, Repo, sh  # noqa: E402

FAKE_DRIFT = """#!/bin/sh
echo "$@" >> "$FAKE_DRIFT_LOG"
n=$(wc -l < "$FAKE_DRIFT_LOG" | tr -d ' ')
rc=$(echo "$FAKE_DRIFT_RCS" | cut -d, -f"$n")
[ -n "$rc" ] || rc=0
case $rc in 0) echo "DRIFT CLEAN" ;; 1) echo "DRIFT RED: takes-drift-1 (pl)" ;; *) echo "NO VERDICT: dispatch refused" ;; esac
exit "$rc"
"""


class DriftBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        t = os.path.realpath(self.tmp.name)
        self.saved = {k: os.environ.get(k) for k in ("AGENTCTL_STATE_ROOT", "AGENTCTL_REDS_DIR", "AGENTCTL_INBOX_DIR",
                                                     "FAKE_DRIFT_LOG", "FAKE_DRIFT_RCS")}
        os.environ["AGENTCTL_STATE_ROOT"] = os.path.join(t, "state")
        os.environ["AGENTCTL_REDS_DIR"] = os.path.join(t, "reds")
        os.environ["AGENTCTL_INBOX_DIR"] = os.path.join(t, "inbox")
        os.environ["FAKE_DRIFT_LOG"] = os.path.join(t, "drift.log")
        os.environ["FAKE_DRIFT_RCS"] = ""
        self.r = Repo(t)
        self.base = sh(self.r.path, "git", "rev-parse", "HEAD")
        self.d = os.path.join(t, "wstate")
        os.makedirs(os.path.join(self.d, "entries"))
        self.st = A.Store(path=os.path.join(t, "store.db"))

    def tearDown(self):
        for k, v in self.saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        self.tmp.cleanup()

    def script(self):
        p = os.path.join(self.r.path, "scripts", "identity-drift-ci")
        self.r.write("scripts/identity-drift-ci", FAKE_DRIFT)
        os.chmod(p, os.stat(p).st_mode | stat.S_IXUSR)
        return self.r.commit("drift script")

    def change(self, path, n=1):
        for i in range(n):
            self.r.write(path, f"v{i} {path}\n")
            self.r.commit(f"change {path} {i}")
        return sh(self.r.path, "git", "rev-parse", "HEAD")

    def entry(self, eid, seq, commits, tip):
        e = {"id": eid, "seq": seq, "commits": commits, "tip": tip, "status": "waiting", "worktree": self.r.path,
             "created": A.ts()}
        A.save_entry(self.d, e)
        return e

    def calls(self):
        p = os.environ["FAKE_DRIFT_LOG"]
        if not os.path.exists(p):
            return []
        with open(p) as f:
            return f.read().splitlines()


class IdentityPathsTest(DriftBase):
    def test_the_identity_model_paths(self):
        hits = {}
        for p in ("identity/src/main/scala/Take.scala", "worker/src/main/scala/services/identity/Resolve.scala",
                  "worker/src/test/scala/scripts/IdentityDriftCheck.scala", "worker/src/main/scala/web/Page.scala",
                  "worker/src/test/scala/services/identity/ResolveSpec.scala", "docs/identity.md"):
            hits[p] = bool(A.identity_model_commits(self.r.path, [self.change(p)]))
        self.assertEqual([p for p, v in hits.items() if v],
                         ["identity/src/main/scala/Take.scala", "worker/src/main/scala/services/identity/Resolve.scala",
                          "worker/src/test/scala/scripts/IdentityDriftCheck.scala"])


class DriftCheckTest(DriftBase):
    def test_one_identity_commit_runs_no_drift_check(self):
        self.script()
        base = sh(self.r.path, "git", "rev-parse", "HEAD")
        self.change("web/a.txt")
        tip = self.change("identity/a.scala")
        self.assertEqual(A.batch_drift_check(self.r.path, self.d, self.r.path, base, tip), (None, ""))
        self.assertEqual(self.calls(), [])

    def test_no_script_at_the_tip_is_a_logged_no_op(self):
        tip = self.change("identity/a.scala", n=2)
        self.assertEqual(A.batch_drift_check(self.r.path, self.d, self.r.path, self.base, tip), (None, ""))
        with open(os.path.join(self.d, "processor.log")) as f:
            self.assertIn("no scripts/identity-drift-ci at its tip", f.read())

    def test_two_identity_commits_run_it_on_the_tip_and_read_its_verdict(self):
        self.script()
        tip = self.change("identity/a.scala", n=2)
        os.environ["FAKE_DRIFT_RCS"] = "0,1,2"
        self.assertTrue(A.batch_drift_check(self.r.path, self.d, self.r.path, self.base, tip)[0])
        self.assertIs(A.batch_drift_check(self.r.path, self.d, self.r.path, self.base, tip, against_main=True)[0], False)
        self.assertIsNone(A.batch_drift_check(self.r.path, self.d, self.r.path, self.base, tip)[0])  # no verdict: go on
        self.assertEqual(self.calls(), [tip, f"{tip} --against main", tip])


class SplitTest(DriftBase):
    def batch(self, members, tip):
        return self.st.put_item("batch", "k#1", "checking", {"members": [e["id"] for e in members], "tip": tip,
                                                              "base": self.base, "tree": 0, "cls": "check"})

    def test_the_script_gets_python3_and_the_token_the_reconcilers_path_lacks(self):
        """2026-10-30 batch #4: ci-runner's reconciler PATH has no python3, so movies' lock_take (python3 flock) never
        took the measure-push lock: 3600 s, then 'could not push measure/ci-lander/...' and no verdict. ci-lander may
        push that branch (only main is protected); the script lacked its tools, not a permission."""
        empty = os.path.join(self.tmp.name, "nobin")
        os.makedirs(empty)
        env = A.drift_env(self.r.path, {"PATH": empty})
        py = shutil.which("python3", path=env["PATH"])
        self.assertTrue(py)
        self.assertEqual(os.path.realpath(py), os.path.realpath(sys.executable))
        self.assertEqual(env["KINOWO_FORGE"], "github")  # forge-gh reads GitHub (the App token on ci-runner)
        self.assertEqual(env["PATH"].split(":")[0], empty, "its own PATH first; the shim only appended")
        here = os.path.dirname(os.path.realpath(sys.executable))
        if os.access(os.path.join(here, "python3"), os.X_OK):  # a python3 on the PATH already: the PATH as it was
            self.assertEqual(A.drift_env(self.r.path, {"PATH": here})["PATH"], here)
        # and the drift check runs the script with it
        seen = []
        saved = A.drift_env
        A.drift_env = lambda repo, env=None: seen.append(repo) or saved(repo, env)
        try:
            self.script()
            self.change("identity/a.scala")
            tip = self.change("identity/b.scala")
            A.batch_drift_check(self.r.path, self.d, self.r.path, self.base, tip)
        finally:
            A.drift_env = saved
        self.assertEqual(seen, [self.r.path])

    def test_drift_red_with_several_identity_changes_splits_them_one_per_batch(self):
        self.script()
        s0 = sh(self.r.path, "git", "rev-parse", "HEAD")
        a = self.entry("a", 1, [s0], s0)
        i1 = self.change("identity/one.scala")
        b = self.entry("b", 2, [i1], i1)
        w = self.change("web/w.txt")
        c = self.entry("c", 3, [w], w)
        i2 = self.change("identity/two.scala")
        e = self.entry("e", 4, [i2], i2)
        it = self.batch([a, b, c, e], i2)
        A.batch_drift_split(self.r.path, self.d, self.st, it, [a, b, c, e], self.r.path, "DRIFT RED: x")
        self.assertEqual(self.st.item("batch", "k#1")["state"], "assembling")
        es = {x["id"]: x for x in A.waiting_entries(self.d)}
        self.assertEqual(sorted(k for k, x in es.items() if x.get("drift_split")), ["b", "e"])
        nxt = A.drift_serialize(self.r.path, [es[k] for k in ("a", "b", "c", "e")])
        self.assertEqual([x["id"] for x in nxt], ["a", "b", "c"])  # the non-identity part and ONE identity change
        self.assertEqual(self.calls(), [])  # no extra drift run for a split

    def test_a_lone_identity_change_red_twice_is_rejected_and_once_lands(self):
        self.script()
        s0 = sh(self.r.path, "git", "rev-parse", "HEAD")
        a = self.entry("a", 1, [s0], s0)
        i1 = self.change("identity/one.scala", n=2)
        b = self.entry("b", 2, [i1], i1)
        it = self.batch([a, b], i1)
        os.environ["FAKE_DRIFT_RCS"] = "1"
        A.batch_drift_split(self.r.path, self.d, self.st, it, [a, b], self.r.path, "DRIFT RED: x")
        self.assertEqual(self.st.item("batch", "k#1")["state"], "assembling")
        self.assertEqual(A.read_record(os.path.join(self.d, "entries", "b.json"))["status"], "rejected")
        # the same, but its second run is clean: a flaky first run rejects nobody
        os.remove(os.environ["FAKE_DRIFT_LOG"])
        b2 = self.entry("b2", 3, [i1], i1)
        it = self.st.put_item("batch", "k#2", "checking", {"members": ["a", "b2"], "tip": i1, "base": self.base,
                                                            "tree": 0, "cls": "check"})
        os.environ["FAKE_DRIFT_RCS"] = "0"
        A.batch_drift_split(self.r.path, self.d, self.st, it, [a, b2], self.r.path, "DRIFT RED: x")
        self.assertEqual(self.st.item("batch", "k#2")["state"], "pushing")

    def test_without_a_split_mark_the_batch_is_as_it_was(self):
        es = [{"id": "x", "commits": [self.change("identity/a.scala")]},
              {"id": "y", "commits": [self.change("identity/b.scala")]}]
        self.assertEqual(A.drift_serialize(self.r.path, es), es)


if __name__ == "__main__":
    unittest.main()
