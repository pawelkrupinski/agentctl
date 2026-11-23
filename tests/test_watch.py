"""agentctl land's watch after LANDED: Main's run for the sha (or the newest containing it), the FIRST convergence run
created after the push that contains it, green / red / superseded / none started."""
import datetime as dt, os, sys, time, unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from test_agentctl import A, Base  # noqa: E402
from fake_clock import FakeClock  # noqa: E402

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
        self.main, self.conv, self.takes, self.jobs = [], [], None, {}
        self.listed = []

        def gh_json(o_r, *args):
            if args[:2] == ("run", "list"):
                wfile = args[args.index("--workflow") + 1]
                self.listed.append(wfile)
                if wfile == "identity-takes.yml" and self.takes is not None:
                    return list(self.takes)
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
            rc = A.watch_landed(self.r.path, self.sha, self.pushed_at, self.clock, o_r=MOVIES)
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

    def test_identity_takes_is_watched_like_the_convergence(self):
        """2026-11-08: the takes rows moved to Identity takes (dispatched by convergence's preflight), a gate workflow:
        the watch follows its FIRST run after the push containing the sha, and a red one is a WATCH RED."""
        self.main = [run(11, self.sha)]
        self.conv = [run(21, self.sha)]
        self.takes = [run(31, self.sha)]
        rc, out = self.watch()
        self.assertEqual(rc, 0, out)
        self.assertIn("WATCH OK: Main #11 green, convergence #21 green, takes #31 green", out)
        self.assertIn("identity-takes.yml", self.listed)
        self.sha = self.later  # another landed sha: the first one's watch is decided, its verdict kept (one per sha)
        self.main, self.conv = [run(12, self.sha)], [run(22, self.sha)]
        self.takes = [run(32, self.sha, conclusion="failure")]
        self.jobs[32] = [{"name": "leg (poland) / takes-drift-1 (poland)", "conclusion": "failure"}]
        rc, out = self.watch()
        self.assertEqual(rc, 3, out)
        self.assertIn("WATCH RED: Identity takes #32 leg (poland) / takes-drift-1 (poland)", out)

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
        # ON A CLOCK THE SLEEPS MOVE (2026-11-19: with a 0.3 s real-time bound and no-op sleeps, how many reads fit
        # was the machine's speed -- 10 in python:3.14 under load, and the assertion failed): each sleep advances it
        sleeps = self.clock.slept
        os.environ["AGENTCTL_WATCH_TIMEOUT"] = "3000"
        rc, out = self.watch()
        self.assertEqual(rc, 2, out)
        self.assertGreater(len(sleeps), 10, "no give-up after 10 failed reads: it reads again until the bound")
        self.assertEqual(sleeps[:3], [30, 45, 67.5])
        self.assertEqual(max(sleeps), 300)
        self.assertRegex(out, r"WATCH UNKNOWN: GitHub's runs could not be read for \d+")
        self.assertIn("connection refused", out.splitlines()[-1])
        self.assertNotIn("STALLED", out, "a failed read says nothing about the runs")

    def test_github_back_after_failures_is_read_again(self):
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
        self.assertIn("GitHub answers again", out)

    def test_no_token_ends_at_once_with_the_cause(self):
        def no_token(o_r, *args):
            raise A.NoToken("agentctl: could not read the movies AGENTCTL_STORE_TOKEN from the vault (locked)")
        A.gh_json = no_token
        rc, out = self.watch()
        self.assertEqual(rc, 2, out)
        self.assertIn("WATCH UNKNOWN: no token (could not read the movies AGENTCTL_STORE_TOKEN", out)

    def test_a_watch_whose_worktree_was_removed_after_landed_still_ends_with_its_verdict(self):
        # 2026-10-27: `crashed: RuntimeError: git rev-parse --show-toplevel in ~/projects/movies-wt-gate-closed`: the
        # agent removed its worktree at LANDED (as the brief says) while the detached watch still ran in it
        import contextlib, io
        wt = self.r.worktree("repo-wt-gone", base=self.sha)
        self.main = [run(11, self.sha)]
        self.conv = [run(12, self.sha, created=self.pushed_at + 5)]
        old, saved_slug = os.getcwd(), A.slug
        A.slug = lambda repo: MOVIES
        out = io.StringIO()
        try:
            os.chdir(wt)
            import subprocess
            subprocess.run(["git", "-C", self.r.path, "worktree", "remove", "--force", wt], check=True)
            self.assertFalse(os.path.exists(wt))
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
                rc = A.main(["watch", "--repo", wt, "--sha", self.sha, "--pushed-at", str(self.pushed_at), "--close", wt], clock=self.clock)
        finally:
            os.chdir(old)
            A.slug = saved_slug
        self.assertEqual(rc, 0, out.getvalue())
        self.assertIn("WATCH OK: Main #11 green, convergence #12 green", out.getvalue())
        self.assertNotIn("crashed", out.getvalue())
        self.assertEqual(A.main_clone(wt), os.path.realpath(self.r.path))

    def test_two_followers_of_one_landed_sha_make_one_watch(self):
        """Review round 6: on the laptop `land --follow` watched in-process while ci-runner's batch, which cannot see
        the laptop's await lock, made the sha's watch item too: two watches, two verdicts. The sha's watch is its ONE
        store item: the first follower claims it and writes its verdict there, a second reads it (no GitHub read)."""
        self.main.append(run(1, self.sha))
        self.conv.append(run(2, self.sha, created=self.pushed_at + 5))
        rc, out = self.watch()
        self.assertEqual(rc, 0, out)
        looks = len(self.listed)
        self.assertGreater(looks, 0)
        rc2, out2 = self.watch()
        self.assertEqual(rc2, 0, out2)
        self.assertEqual(len(self.listed), looks, "the second follower read the first one's verdict, not GitHub")
        self.assertIn("WATCH OK", out2)
        [it] = A.open_store().items("watch")
        self.assertEqual((it["id"], it["state"]), (self.sha, "ok"))
        self.assertEqual([e["to_state"] for e in A.open_store().events() if e["item_type"] == "watch"], ["ok"])

    def test_a_follower_of_a_sha_another_live_follower_watches_waits_for_its_verdict(self):
        A.start_watch(self.r.path, self.sha, self.pushed_at, o_r=MOVIES,
                      follower={"followed_by": "ci-runner:4242", "followed_until": time.time() + 3600})
        st = A.open_store()

        def theirs_decides(_s):  # the other follower's verdict, on the item
            it = st.item("watch", self.sha)
            st.transition("watch", self.sha, "ok", it["version"], data={"verdict": "WATCH OK: Main #7 green"})
        self.clock.on_sleep = theirs_decides
        rc, out = self.watch()
        self.assertEqual(rc, 0, out)
        self.assertEqual(self.listed, [], "never a second watch of GitHub")
        self.assertIn("already watched by ci-runner:4242", out)
        self.assertIn("WATCH OK: Main #7 green", out)

    def test_a_follower_takes_over_a_watch_whose_follower_lapsed(self):
        A.start_watch(self.r.path, self.sha, self.pushed_at, o_r=MOVIES,
                      follower={"followed_by": "ci-runner:4242", "followed_until": time.time() - 1})
        self.main.append(run(1, self.sha))
        self.conv.append(run(2, self.sha, created=self.pushed_at + 5))
        rc, out = self.watch()
        self.assertEqual(rc, 0, out)
        self.assertGreater(len(self.listed), 0)
        self.assertEqual(A.open_store().item("watch", self.sha)["state"], "ok")

    def test_a_follower_the_local_owned_lock_refuses_gives_its_claim_back(self):
        """Review round 8: a follower that claimed the item and then met another local watcher's Owned lock returned
        WATCH UNKNOWN with its claim still on the item: the tick left the sha alone for the claim's 600 s."""
        held = A.Owned("watch", f"{MOVIES}-{self.sha}", ["watch"])
        self.assertTrue(held.acquire())
        self.addCleanup(held.release)
        rc, out = self.watch()
        self.assertEqual(rc, 2, out)
        self.assertIn("already watches", out)
        self.assertFalse(A.open_store().item("watch", self.sha)["data"].get("followed_by"), "the claim was given back")

    def test_a_sha_whose_watch_ended_unknown_is_rewatched_claimed_and_its_verdict_kept(self):
        """Review round 8: the re-watch of an `unknown` item ran unclaimed, so its verdict was never stored and every
        later follower watched GitHub again. It claims the item like any follower and keeps its verdict there."""
        it = A.start_watch(self.r.path, self.sha, self.pushed_at, o_r=MOVIES)
        A.open_store().transition("watch", self.sha, "unknown", it["version"], data={"verdict": "WATCH UNKNOWN: stuck"})
        self.main, self.conv = [run(11, self.sha)], [run(21, self.sha)]
        claimed = []
        saved = A.watch_look

        def look(*a, **kw):  # while it watches, the item is this follower's
            claimed.append(A.open_store().item("watch", self.sha)["data"].get("followed_by"))
            return saved(*a, **kw)
        A.watch_look = look
        self.addCleanup(setattr, A, "watch_look", saved)
        rc, out = self.watch()
        self.assertEqual(rc, 0, out)
        self.assertTrue(claimed and all(claimed), claimed)
        it = A.open_store().item("watch", self.sha)
        self.assertEqual((it["data"].get("outcome"), it["data"].get("followed_by")), ("ok", None))
        self.assertIn("WATCH OK: Main #11 green", it["data"]["verdict"])
        looks = len(self.listed)
        rc2, out2 = self.watch()
        self.assertEqual(rc2, 0, out2)
        self.assertEqual(len(self.listed), looks, "a later follower reads the kept verdict, not GitHub")
        self.assertIn("WATCH OK: Main #11 green", out2)

    def test_other_repos_watch_nothing(self):
        import contextlib, io
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            rc = A.watch_landed(self.r.path, self.sha, self.pushed_at, self.clock, o_r="bitcashier/infra")
        self.assertEqual(rc, 0)
        self.assertEqual(self.listed, [])


class LandWatchTest(Base):
    """land runs the watch after LANDED, outside the landing lock; --no-watch skips it."""

    def setUp(self):
        super().setUp()
        self.saved = A.watch_landed
        self.calls = []

        def fake(repo, sha, pushed_at, clock, o_r=None, close=None, fixes=()):
            # the lease is free while watching: another lander could take it now
            r = A.open_store().get(A.land_lease_name(repo))
            assert not r or not r["holder"] or r["expires_at"] <= time.time(), "the landing lease is held while watching"
            self.calls.append((sha, pushed_at))
            print("WATCH RED: Main #5 test")
            return 3
        A.watch_landed = fake

    def tearDown(self):
        A.watch_landed = self.saved
        super().tearDown()

    def test_land_ends_at_landed_and_watches_detached(self):
        spawned, saved = [], A.start_watch
        A.start_watch = lambda repo, sha, pushed_at, **kw: spawned.append(sha)
        try:
            self.r.write("f1.txt", "mine\n")
            sha = self.r.commit("mine")
            rc, out = self.run_tool("land", cwd=self.r.path)
        finally:
            A.start_watch = saved
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


class WatchItemTest(unittest.TestCase):
    """Step 9: a landed sha is a watch item the tick evaluates once per look; no detached process."""
    def setUp(self):
        import tempfile
        self.root = tempfile.mkdtemp()
        self.saved = {k: os.environ.get(k) for k in ("AGENTCTL_STATE_ROOT", "AGENTCTL_INBOX_DIR")}
        os.environ["AGENTCTL_STATE_ROOT"] = self.root
        os.environ["AGENTCTL_INBOX_DIR"] = os.path.join(self.root, "inbox")
        self.saved_a = A.watch_eval, A.slug, A.main_clone, A.close_worktree, A.red_repo_clone
        A.slug = lambda repo: "pawelkrupinski/movies"
        A.main_clone = lambda repo: repo
        self.here = os.path.join(self.root, "this-hosts-clone")  # this host's clone of the repo (red_repo_clone)
        os.makedirs(self.here)
        A.red_repo_clone = lambda o_r: self.here if o_r == "pawelkrupinski/movies" else None
        self.closed = []
        A.close_worktree = lambda wt: self.closed.append(wt) or f"closed {wt}"

    def tearDown(self):
        A.watch_eval, A.slug, A.main_clone, A.close_worktree, A.red_repo_clone = self.saved_a
        for k, v in self.saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def test_pending_then_ok_closes_the_worktree_and_tells_its_author(self):
        wt = os.path.join(self.root, "wt")
        os.makedirs(wt)
        A.start_watch("/repo", "a" * 40, time.time(), worktree=wt, close=wt, session="s1")
        A.watch_eval = lambda *a: None
        self.assertEqual(A.tick_watches(), [])
        self.assertEqual(A.open_store().item("watch", "a" * 40)["state"], "watching")
        A.watch_eval = lambda *a: ("ok", "WATCH OK: Main #5 green, convergence #6 green (aaaaaaaaaa)", [])
        self.assertEqual(A.tick_watches(now_=time.time() + 120), [f"watch {'a' * 10}: ok"])
        self.assertEqual(self.closed, [wt])
        A.project_events()
        [e] = A.inbox_entries()
        self.assertEqual((e["kind"], e["cwd"], e["exit"]), ("OK", wt, 0))

    def test_a_watch_a_live_follower_holds_is_left_to_it(self):
        A.start_watch("/repo", "c" * 40, time.time(), worktree="/w",
                      follower={"followed_by": "laptop:1", "followed_until": time.time() + 600})
        looked = []
        A.watch_eval = lambda *a: looked.append(1) or ("ok", "WATCH OK", [])
        self.assertEqual(A.tick_watches(), [])
        self.assertEqual(looked, [])
        self.assertEqual(A.tick_watches(now_=time.time() + 4 * 3600), [f"watch {'c' * 10}: unknown"],
                         "a lapsed follower's watch is the tick's again")

    def test_a_lapsed_laptop_followers_watch_is_taken_over_by_another_hosts_tick_in_its_own_clone(self):
        """Review round 7: the item named its repo by the laptop's path; ci-runner's tick, taking over a dead laptop
        follower's watch, looked there, failed every look and ended it UNKNOWN. The repo is its slug, resolved here."""
        laptop = "/Users/somebody/projects/movies-wt-gone"  # no such path on this host
        A.start_watch(laptop, "d" * 40, time.time(), worktree=laptop, close=laptop,
                      follower={"followed_by": "laptop:4242", "followed_until": time.time() - 1})
        looked = []
        A.watch_eval = lambda repo, *a: looked.append(repo) or ("ok", "WATCH OK: Main #5 green (dddddddddd)", [])
        self.assertEqual(A.tick_watches(), [f"watch {'d' * 10}: ok"])
        self.assertEqual(looked, [self.here], "looked at in this host's clone, never the laptop's path")
        self.assertEqual(self.closed, [], "the laptop's worktree is not this host's to close")
        A.start_watch(self.here, "e" * 40, time.time(), worktree=self.here)  # made here: its own path, as it is
        A.watch_eval = lambda repo, *a: looked.append(repo) or None
        A.tick_watches()
        self.assertEqual(looked[-1], self.here)

    def test_a_watch_of_a_repo_this_host_has_no_clone_of_waits_for_its_deadline(self):
        A.WATCH["someone/else"] = A.WATCH["pawelkrupinski/movies"]
        self.addCleanup(A.WATCH.pop, "someone/else", None)
        A.start_watch("/elsewhere", "f" * 40, time.time(), worktree="/w", o_r="someone/else")
        looked = []
        A.watch_eval = lambda *a: looked.append(a) or ("ok", "WATCH OK", [])
        self.assertEqual(A.tick_watches(), [])
        self.assertEqual(looked, [], "no clone here: nothing looked at")
        self.assertEqual(A.open_store().item("watch", "f" * 40)["state"], "watching")

    def test_a_follower_never_renews_a_claim_another_follower_took_over(self):
        """Review round 7: renew() wrote its lease over whoever held the item, so a follower whose claim lapsed (asleep)
        took it back from the one that had taken it over."""
        claim = A.WatchClaim("/repo", "g" * 40, time.time(), "pawelkrupinski/movies", FakeClock())
        A.start_watch("/repo", "g" * 40, time.time(), worktree="/w",
                      follower={"followed_by": "ci-runner:7", "followed_until": time.time() + 600})
        claim.renew()
        self.assertEqual(A.open_store().item("watch", "g" * 40)["data"]["followed_by"], "ci-runner:7")
        A.open_store().update_item("watch", "g" * 40, A.open_store().item("watch", "g" * 40)["version"], claim.lease(time.time() - 900))
        claim.renew()
        self.assertGreater(A.open_store().item("watch", "g" * 40)["data"]["followed_until"], time.time(), "its own: renewed")

    def test_undecided_past_its_deadline_is_unknown_and_escalates(self):
        A.start_watch("/repo", "b" * 40, time.time(), worktree="/w")
        A.watch_eval = lambda *a: None
        A.tick_watches(now_=time.time() + 4 * 3600)
        self.assertEqual(A.open_store().item("watch", "b" * 40)["state"], "unknown")
        self.assertEqual(A.open_store().events()[-1]["severity"], "escalate")


if __name__ == "__main__":
    unittest.main()
