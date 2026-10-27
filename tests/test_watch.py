"""agentctl land's watch after LANDED: Main's run for the sha (or the newest containing it), the FIRST convergence run
created after the push that contains it, green / red / superseded / none started."""
import datetime as dt, os, sys, time, unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from test_agentctl import A, Base  # noqa: E402

MOVIES = "pawelkrupinski/movies"


def iso(t):
    return dt.datetime.fromtimestamp(t, dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def run(rid, sha, status="completed", conclusion="success", created=None):
    return {"databaseId": rid, "number": rid, "status": status, "conclusion": conclusion if status == "completed" else "",
            "headSha": sha, "createdAt": iso(created if created is not None else time.time()), "url": f"u/{rid}"}


class WatchTest(Base):
    def setUp(self):
        super().setUp()
        self.saved = A.gh_json, A.ci_why, A.time.sleep
        A.time.sleep = lambda s: None
        A.ci_why = lambda o_r, tail=6, run_id=None, repo=None: (True, "ci-why: main is RED for the gate")
        self.main, self.conv, self.jobs = [], [], {}
        self.listed = []

        def gh_json(o_r, *args):
            if args[:2] == ("run", "list"):
                wfile = args[args.index("--workflow") + 1]
                self.listed.append(wfile)
                return list(self.main if wfile == "main.yml" else self.conv)
            if args[:2] == ("run", "view"):
                return {"jobs": self.jobs.get(int(args[2]), [])}
            raise AssertionError(args)
        A.gh_json = gh_json
        for k in ("AGENTCTL_WATCH_TIMEOUT", "AGENTCTL_WATCH_CONV_START", "AGENTCTL_WATCH_POLL"):
            os.environ.pop(k, None)
        self.r.write("f1.txt", "landed\n")
        self.sha = self.r.commit("landed")
        self.r.push()
        self.r.write("f2.txt", "a later push\n")
        self.later = self.r.commit("later")
        self.r.push()
        self.pushed_at = time.time()

    def tearDown(self):
        A.gh_json, A.ci_why, A.time.sleep = self.saved
        for k in ("AGENTCTL_WATCH_TIMEOUT", "AGENTCTL_WATCH_CONV_START", "AGENTCTL_WATCH_POLL"):
            os.environ.pop(k, None)
        super().tearDown()

    def watch(self):
        import contextlib, io
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
            rc = A.watch_landed(self.r.path, self.sha, self.pushed_at, o_r=MOVIES)
        return rc, out.getvalue()

    def test_green_main_and_convergence(self):
        self.main = [run(11, self.sha)]
        self.conv = [run(21, self.sha)]
        rc, out = self.watch()
        self.assertEqual(rc, 0, out)
        self.assertIn("WATCH OK: Main #11 green, convergence #21 green", out)

    def test_red_main_names_the_failed_jobs_and_ci_why(self):
        self.main = [run(11, self.sha, conclusion="failure")]
        self.jobs[11] = [{"name": "test (worker-identity)", "conclusion": "failure"}, {"name": "lint", "conclusion": "success"}]
        self.conv = [run(21, self.sha, status="in_progress")]
        rc, out = self.watch()
        self.assertEqual(rc, 3, out)
        self.assertIn("WATCH RED: Main #11 test (worker-identity)", out)
        self.assertNotIn("lint", out.split("WATCH RED")[1].splitlines()[0])
        self.assertIn("Fixes-Red: 11", out)
        self.assertIn("ci-why: main is RED", out)

    def test_red_convergence(self):
        self.main = [run(11, self.sha)]
        self.conv = [run(21, self.sha, conclusion="failure")]
        self.jobs[21] = [{"name": "takes (poland)", "conclusion": "failure"}]
        rc, out = self.watch()
        self.assertEqual(rc, 3, out)
        self.assertIn("WATCH RED: Identity model convergence #21 takes (poland)", out)

    def test_superseded_main_run_follows_the_newest_run_containing_the_sha(self):
        # the sha's own run cancelled by a newer push; an older red run that does NOT contain it is not blamed
        older = A.git(self.r.path, "rev-parse", f"{self.sha}~1").strip()
        self.main = [run(13, self.later), run(12, self.sha, conclusion="cancelled"), run(10, older, conclusion="failure")]
        self.conv = [run(21, self.later)]
        rc, out = self.watch()
        self.assertEqual(rc, 0, out)
        self.assertIn("Main #13 green", out)

    def test_superseded_and_the_newer_run_red_is_red(self):
        self.main = [run(13, self.later, conclusion="failure"), run(12, self.sha, conclusion="cancelled")]
        self.conv = [run(21, self.later)]
        rc, out = self.watch()
        self.assertEqual(rc, 3, out)
        self.assertIn("WATCH RED: Main #13", out)

    def test_convergence_is_the_first_run_after_the_push_containing_the_sha(self):
        before = self.pushed_at - 3600
        older = A.git(self.r.path, "rev-parse", f"{self.sha}~1").strip()
        self.main = [run(11, self.sha)]
        # #20 before the push (red, not ours), #22 after but not containing it, #23 the first that does, #24 later
        self.conv = [run(24, self.later, conclusion="failure"), run(23, self.later),
                     run(22, older, created=self.pushed_at + 10, conclusion="failure"),
                     run(20, self.sha, created=before, conclusion="failure")]
        rc, out = self.watch()
        self.assertEqual(rc, 0, out)
        self.assertIn("convergence #23 green", out)

    def test_convergence_not_triggered_is_said_not_waited_for(self):
        os.environ["AGENTCTL_WATCH_CONV_START"] = "0"
        self.main = [run(11, self.sha)]
        self.conv = [run(20, self.sha, created=self.pushed_at - 3600)]
        rc, out = self.watch()
        self.assertEqual(rc, 0, out)
        self.assertIn("convergence: none started within 0 min", out)
        self.assertIn("WATCH OK: Main #11 green, convergence none started", out)

    def test_undecided_after_the_bound_is_could_not_tell(self):
        os.environ["AGENTCTL_WATCH_TIMEOUT"] = "0"
        self.main = [run(11, self.sha, status="in_progress")]
        self.conv = [run(21, self.sha)]
        rc, out = self.watch()
        self.assertEqual(rc, 2, out)
        self.assertIn("WATCH UNKNOWN: Main undecided", out)

    def test_no_change_in_the_runs_for_the_bound_is_a_named_stall(self):
        self.main = [run(70, self.sha, status="in_progress")]  # it never moves (a runner lost mid-run)
        self.conv = [run(71, self.sha, status="queued", created=self.pushed_at + 1)]
        os.environ["AGENTCTL_WATCH_STALL_MIN"] = "0"
        try:
            rc, out = self.watch()
        finally:
            del os.environ["AGENTCTL_WATCH_STALL_MIN"]
        self.assertEqual(rc, 2, out)
        self.assertIn("WATCH STALLED: no change in Main run 70 in_progress", out)

    def test_api_failures_back_off_and_end_unknown_only_at_the_bound(self):
        def broken(o_r, *args):
            raise RuntimeError("connection refused")
        A.gh_json = broken
        sleeps = []
        A.time.sleep = sleeps.append
        os.environ["AGENTCTL_WATCH_TIMEOUT"] = "0.3"
        rc, out = self.watch()
        self.assertEqual(rc, 2, out)
        self.assertGreater(len(sleeps), 10, "no give-up after 10 failed reads: it reads again until the bound")
        self.assertEqual(sleeps[:3], [30, 45, 67.5])
        self.assertEqual(max(sleeps), 300)
        self.assertIn("WATCH UNKNOWN: Forgejo's runs could not be read for 0m", out)
        self.assertIn("connection refused", out.splitlines()[-1])
        self.assertNotIn("STALLED", out, "a failed read says nothing about the runs")

    def test_forgejo_back_after_failures_is_read_again(self):
        calls = []
        self.main = [run(11, self.sha)]
        self.conv = [run(21, self.sha)]
        good = A.gh_json

        def flaky(o_r, *args):
            calls.append(args)
            if len(calls) <= 25:
                raise RuntimeError("tunnel down")
            return good(o_r, *args)
        A.gh_json = flaky
        rc, out = self.watch()
        self.assertEqual(rc, 0, out)
        self.assertIn("WATCH OK: Main #11 green, convergence #21 green", out)
        self.assertIn("Forgejo answers again", out)

    def test_no_token_ends_at_once_with_the_cause(self):
        def no_token(o_r, *args):
            raise A.NoToken("agentctl: could not read the movies FORGEJO_API_TOKEN from the vault (locked)")
        A.gh_json = no_token
        rc, out = self.watch()
        self.assertEqual(rc, 2, out)
        self.assertIn("WATCH UNKNOWN: no Forgejo token (could not read the movies FORGEJO_API_TOKEN", out)

    def fix_scenario(self, new_jobs):
        """#2047 red in test (worker-identity); the fix lands; Main #2059 is the first run containing it."""
        self.main = [run(2059, self.sha, conclusion="success" if all(c == "success" for c in new_jobs.values()) else "failure"),
                     run(2047, "0" * 40, conclusion="failure")]
        self.conv = [run(21, self.sha)]
        self.jobs[2047] = [{"name": "test (worker-identity)", "conclusion": "failure"}, {"name": "lint", "conclusion": "success"}]
        self.jobs[2059] = [{"name": n, "conclusion": c} for n, c in new_jobs.items()]
        import contextlib, io
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
            rc = A.watch_landed(self.r.path, self.sha, self.pushed_at, o_r=MOVIES, fixes=["2047"])
        fix = [e for e in A.inbox_entries() if e["verdict"].startswith("FIX")]
        return rc, out.getvalue(), fix

    def test_a_fix_that_fails_again_in_the_same_job_is_fix_failed(self):
        rc, out, fix = self.fix_scenario({"test (worker-identity)": "failure", "lint": "success"})
        self.assertEqual(rc, 3, out)
        line = f"FIX FAILED: Main #2059 still fails test (worker-identity) (fix {self.sha[:10]} claimed #2047)"
        self.assertIn(line, out)
        self.assertEqual([(e["verdict"], e["kind"]) for e in fix][-1:], [(line, "FIX_FAILED")])

    def test_a_fix_whose_jobs_all_pass_is_fix_ok(self):
        rc, out, fix = self.fix_scenario({"test (worker-identity)": "success", "lint": "success"})
        self.assertEqual(rc, 0, out)
        self.assertIn("FIX OK: #2047 cleared by #2059", out)
        self.assertEqual(fix[-1]["kind"], "FIX_OK")
        self.assertTrue(out.rstrip().endswith("FIX OK: #2047 cleared by #2059") or "WATCH OK" in out)

    def test_the_fixes_red_trailer_of_the_landed_commit_is_read(self):
        self.r.write("f3.txt", "the fix\n")
        self.sha = self.r.commit("fix the worker-identity test\n\nFixes-Red: 2047")
        self.r.push()
        rc, out, fix = self.fix_scenario({"test (worker-identity)": "failure"})
        self.assertIn("FIX FAILED: Main #2059 still fails test (worker-identity)", out)

    def test_other_repos_watch_nothing(self):
        import contextlib, io
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            rc = A.watch_landed(self.r.path, self.sha, self.pushed_at, o_r="bitcashier/infra")
        self.assertEqual(rc, 0)
        self.assertEqual(self.listed, [])


class LandWatchTest(Base):
    """land runs the watch after LANDED, outside the landing lock; --no-watch skips it."""

    def setUp(self):
        super().setUp()
        self.saved = A.watch_landed, A.LAND_LOCK_DIR
        A.LAND_LOCK_DIR = os.path.join(self.tmp.name, "locks")
        self.calls = []

        def fake(repo, sha, pushed_at, o_r=None, close=None, fixes=()):
            # the lock is free while watching: another lander could take it now
            import fcntl
            with open(A.land_lock_path(repo), "a+") as f:
                fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.calls.append((sha, pushed_at))
            print("WATCH RED: Main #5 test")
            return 3
        A.watch_landed = fake

    def tearDown(self):
        A.watch_landed, A.LAND_LOCK_DIR = self.saved
        super().tearDown()

    def test_land_ends_at_landed_and_watches_detached(self):
        spawned, saved = [], A.spawn_watch_of
        A.spawn_watch_of = lambda repo, sha, pushed_at, close=None, fixes=(): spawned.append(sha)
        try:
            self.r.write("f1.txt", "mine\n")
            sha = self.r.commit("mine")
            rc, out = self.run_tool("land", cwd=self.r.path)
        finally:
            A.spawn_watch_of = saved
        self.assertEqual(rc, 0, out)
        self.assertIn(f"LANDED {sha}", out)
        self.assertEqual(spawned, [sha], "the watch is detached: its verdict goes to the inbox")
        self.assertEqual(self.calls, [], "land itself does not watch")

    def test_land_follow_watches_after_landed_and_returns_its_verdict(self):
        self.r.write("f1.txt", "mine\n")
        sha = self.r.commit("mine")
        rc, out = self.run_tool("land", "--follow", cwd=self.r.path)
        self.assertEqual(rc, 3, out)
        self.assertIn(f"LANDED {sha}", out)
        self.assertEqual(self.calls[0][0], sha)
        self.assertIsNotNone(self.calls[0][1])
        self.assertLess(out.index("LANDED"), out.index("WATCH RED"))

    def test_no_watch_exits_at_landed(self):
        self.r.write("f1.txt", "mine\n")
        self.r.commit("mine")
        rc, out = self.run_tool("land", "--no-watch", cwd=self.r.path)
        self.assertEqual(rc, 0, out)
        self.assertEqual(self.calls, [])


if __name__ == "__main__":
    unittest.main()
