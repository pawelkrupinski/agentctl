"""agentctl wait run: verdict-only output, failed-log tails, single waiter, --until landable, backoff."""
import contextlib, io, threading, itertools, json, os, subprocess, sys, tempfile, time, unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from test_agentctl import A, TOOL  # noqa: E402

FAKE = os.path.join(HERE, "fake_gh.py")


def job(name, status="completed", conclusion="success"):
    return {"name": name, "status": status, "conclusion": conclusion if status == "completed" else ""}


class FakeForge(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.d = os.path.join(self.tmp.name, "gh")
        os.makedirs(self.d)
        self.env = {"AGENTCTL_SCRATCH": os.path.join(self.tmp.name, "scratch"), "AGENTCTL_GH_CLI": FAKE,
                    "FAKE_GH_DIR": self.d, "GH_TOKEN": "dummy", "AGENTCTL_MIN_INTERVAL": "0.3", "AGENTCTL_ATTACH_POLL": "0.2",
                    "AGENTCTL_OWNERS_DIR": os.path.join(self.tmp.name, "owners"),
                    # the reds' caches (runsig/<run id>.json) per test: run 39's signatures cached by one ci-why test
                    # were another's in the same worker (main red 2026-10-31, tests.yml #53)
                    "AGENTCTL_REDS_DIR": os.path.join(self.tmp.name, "reds")}
        self.saved = {k: os.environ.get(k) for k in self.env}
        os.environ.update(self.env)
        self.saved_gh, A.GH_CLI = A.GH_CLI, FAKE

    def tearDown(self):
        A.GH_CLI = self.saved_gh
        for k, v in self.saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        self.tmp.cleanup()

    def put(self, name, obj):
        with open(os.path.join(self.d, name), "w") as f:
            f.write(obj if isinstance(obj, str) else json.dumps(obj))

    def run_json(self, rid, status, conclusion="", jobs=(), workflow="Main", branch="main"):
        self.put(f"run-{rid}.json", {"status": status, "conclusion": conclusion, "jobs": list(jobs),
                                     "workflowName": workflow, "headBranch": branch})

    def calls(self):
        p = os.path.join(self.d, "calls.log")
        if not os.path.exists(p):
            return []
        with open(p) as f:
            return f.read().splitlines()

    def tool(self, *argv):
        out, errb = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(errb):
            rc = A.main(list(argv))
        return rc, out.getvalue(), errb.getvalue()


class RelevantLinesTest(unittest.TestCase):
    def test_strong_markers_win_and_info_noise_is_dropped(self):
        log = "\n".join([
            "test\tRun\t2026-10-23T11:28:07.1Z 11:28:07 INFO  Refreshed X: listing INCOMPLETE (a page failed)",
            "test\tRun\t2026-10-23T11:28:07.2Z |-INFO in ch.qos.logback - Setting level of logger [x] to ERROR",
            "test\tRun\t2026-10-23T11:28:08.0Z \x1b[31m[info] - should match the snapshot *** FAILED ***\x1b[0m",
            "test\tRun\t2026-10-23T11:28:08.1Z [info]   Snapshot mismatch: 3 rows differ",
            "test\tRun\t2026-10-23T11:28:08.2Z [info]   regenerate with x",
            "lint\tRun\t2026-10-23T11:00:00.0Z something went wrong: error reading file",
        ])
        per = A.relevant_lines(log, 8)
        self.assertEqual(per["test"], ["[info] - should match the snapshot *** FAILED ***", "[info]   Snapshot mismatch: 3 rows differ",
                                       "[info]   regenerate with x"])
        self.assertEqual(per["lint"], ["something went wrong: error reading file"])
        self.assertEqual(A.relevant_lines(log, 1)["test"], ["[info]   regenerate with x"])

    def test_the_step_script_echo_is_never_a_failure_line(self):
        # run 41235658578 (2026-10-31): the runner echoes a step's script before running it, so the hook step's own
        # `echo "::error::the pre-push checks ran past 80 minutes..."` line was a STRONG line in every failed check's
        # verdict -- 69 NOT LANDED read as "stopped at the 80-minute cap" while no hook step ever ran past 50 min.
        # gh prints the escapes as caret text (`^[[36;1m`) when it refuses raw ones.
        step = "check (rest)\tThe pre-push hook's checks of the change\t2026-10-31T19:59:14.04Z "
        log = "\n".join(step + l for l in [
            '##[group]Run mkdir -p "$TMPDIR"',
            '^[[36;1mtimeout --kill-after=60 80m scripts/hooks/pre-push "$mode" "$range" || rc=$?^[[0m',
            '\x1b[36;1m[ "$rc" != 124 ] || echo "::error::the pre-push checks ran past 80 minutes and were stopped"\x1b[0m',
            "shell: /usr/bin/bash -e {0}",
            "env:",
            "  ERROR_MODE: fail",
            "##[endgroup]",
            "^[[33m! pre-push^[[0m scripttests FAILED (344s)",
            "##[error]Process completed with exit code 1.",
        ])
        per = A.relevant_lines(log, 25)
        self.assertEqual(per["check (rest)"], ["! pre-push scripttests FAILED (344s)",
                                               "##[error]Process completed with exit code 1."])
        self.assertNotIn("ran past", "\n".join(A.log_jobs(log)["check (rest)"]))

    def test_backoff_caps_at_five_minutes(self):
        self.assertEqual(list(itertools.islice(A.backoff(60, 300), 6)), [60, 90, 135, 202.5, 300, 300])


class WaitRunTest(FakeForge):
    def test_failed_run_prints_verdict_and_tail_only(self):
        self.run_json(7, "completed", "failure", [job("build"), job("test (rest)", conclusion="failure")])
        self.put("run-7.log", "test (rest)\tRun\t2026-10-23T11:28:07Z 11:28:07 INFO  boring\n"
                              "test (rest)\tRun\t2026-10-23T11:28:08Z [info] - a spec *** FAILED ***\n")
        rc, out, _ = self.tool("wait", "run", "7", "--repo", "movies")
        self.assertEqual(rc, 1)
        self.assertIn("run pawelkrupinski/movies#7: failure; failed: test (rest)", out)
        self.assertIn("a spec *** FAILED ***", out)
        self.assertNotIn("boring", out)

    def test_legacy_repo_first_form_and_multiple_ids(self):
        self.run_json(1, "completed", "success")
        self.run_json(2, "completed", "success")
        rc, out, _ = self.tool("wait", "run", "infra", "1", "2")
        self.assertEqual(rc, 0, out)
        self.assertIn("bitcashier/infra#1: success", out)
        self.assertIn("bitcashier/infra#2: success", out)

    def test_landable_stops_when_takes_rows_end(self):
        jobs = [job("takes (poland)"), job("takes (germany)"), job("request-bisect", "queued"), job("drop-measure-branch", "queued")]
        self.run_json(9, "in_progress", jobs=jobs, workflow="Identity model convergence", branch="measure/pawel/abc-1")
        self.put("api-repos_pawelkrupinski_movies_actions_runs_9_artifacts.json",
                 {"artifacts": [{"name": "identity-takes-pl"}, {"name": "convergence-diagnostics-pl"}]})
        self.put("art-9-identity-takes-pl.txt", "identity takes (pl): lost 0, changed 0, gained 0")
        rc, out, _ = self.tool("wait", "run", "9", "--repo", "movies", "--until", "landable", "--timeout", "5")
        self.assertEqual(rc, 0, out)
        self.assertIn("takes rows ended (2): all green", out)
        self.assertIn("[identity-takes-pl] identity takes (pl): lost 0", out)
        self.assertIn("scripts/identity-measure --run 9", out)
        # --until done waits for the whole run (its tail is still queued)
        rc, out, _ = self.tool("wait", "run", "9", "--repo", "movies", "--timeout", "1")
        self.assertEqual(rc, 2, out)

    def test_landable_on_an_ordinary_run_waits_for_completion(self):
        self.run_json(10, "in_progress", jobs=[job("takes (poland)")], workflow="Main", branch="main")
        rc, out, _ = self.tool("wait", "run", "10", "--until", "landable", "--timeout", "1")
        self.assertEqual(rc, 2, out)

    def test_attaches_to_a_live_waiter_without_polling(self):
        """An owner holding the target's lock + its verdict: the second caller reads the verdict and never calls GitHub."""
        key = "run-pawelkrupinski_movies-5"
        own = A.Owned("wait", key)
        holder = subprocess.Popen([sys.executable, "-c", "import fcntl, sys, time\n"
                                   "f = open(sys.argv[1], 'a+'); fcntl.flock(f, fcntl.LOCK_EX); print('held', flush=True); "
                                   "time.sleep(1.5)", own.lockpath], stdout=subprocess.PIPE, text=True)
        os.makedirs(os.path.dirname(own.lockpath), exist_ok=True)
        self.assertEqual(holder.stdout.readline().strip(), "held")
        with open(own.record, "w") as f:
            json.dump({"pid": holder.pid, "started": "t"}, f)
        with open(own.lockpath[: -len(".lock")] + ".verdict", "w") as f:
            json.dump({"ok": True, "line": "run pawelkrupinski/movies#5: success (from the holder)"}, f)
        self.run_json(5, "in_progress")
        try:
            rc, out, errs = self.tool("wait", "run", "5", "--repo", "movies", "--timeout", "10")
        finally:
            holder.wait()
        self.assertIn("attached to existing waiter", errs)
        self.assertIn("(from the holder)", out)
        self.assertEqual(self.calls(), [])

    def test_two_processes_one_poller(self):
        self.run_json(11, "in_progress")
        cmd = [sys.executable, TOOL, "wait", "run", "11", "--repo", "movies", "--timeout", "30"]
        # Each step waits for the state it needs, never a fixed sleep: under a loaded suite (the ~/bin pre-push, -j 8) a
        # second waiter that had not yet started when the run completed read the verdict itself and never attached.
        def until(cond, what):
            deadline = time.time() + 30
            while not cond():
                self.assertLess(time.time(), deadline, f"not within 30 s: {what}")
                time.sleep(0.05)
        first = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=os.environ)
        until(lambda: self.calls(), "the first waiter polls")
        err2 = os.path.join(self.d, "second.err")
        with open(err2, "w") as ef:
            second = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=ef, text=True, env=os.environ)
        until(lambda: "attached" in open(err2).read(), "the second waiter attaches")
        self.run_json(11, "completed", "success")
        o1, _ = first.communicate(timeout=180)
        o2, _ = second.communicate(timeout=180)
        e2 = open(err2).read()
        self.assertEqual(o1.strip(), "run pawelkrupinski/movies#11: success")
        self.assertEqual(o1, o2)
        self.assertIn("attached", e2)
        pollers = {l.split()[0] for l in self.calls()}
        self.assertEqual(pollers, {str(first.pid)})


class WaitMutationTest(unittest.TestCase):
    def run_one(self, name):
        return unittest.TextTestRunner(stream=io.StringIO(), verbosity=0).run(WaitRunTest(name))

    def test_landable_test_kills_a_mutant_that_never_recognises_measure_runs(self):
        real = A.is_measure_run
        A.is_measure_run = lambda d: False
        try:
            res = self.run_one("test_landable_stops_when_takes_rows_end")
        finally:
            A.is_measure_run = real
        self.assertTrue(res.failures or res.errors, "mutant survived")

    def test_attach_test_kills_a_mutant_that_ignores_the_lock(self):
        real = A.Waiter.acquire

        def mutant(self):
            self.owned = True
            return True
        A.Waiter.acquire = mutant
        try:
            res = self.run_one("test_attaches_to_a_live_waiter_without_polling")
        finally:
            A.Waiter.acquire = real
        self.assertTrue(res.failures or res.errors, "mutant survived")


if __name__ == "__main__":
    unittest.main()
