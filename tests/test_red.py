"""agentctl red (the owner, 2026-10-27): "i want gate status to point to agent(s) fixing the red build(s)", then "if a
build is red, an agent must be assigned to it". Claims keyed by failure signature, automatic fixers (worktree commits,
land, waiting, landed fixes), ci-why's `fixing:` lines and summary, RED UNCLAIMED, the dispatch of a headless fixer
(a fake `claude`), its dedupe, caps, escalation, stale-claim takeover and the author-first rule."""
import contextlib, glob, io, json, os, subprocess, sys, tempfile, time, unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from test_agentctl import A, Repo, ENV, sh  # noqa: E402
from test_wait_run import FAKE  # noqa: E402

MOVIES = "pawelkrupinski/movies"
FAKE_CLAUDE = os.path.join(HERE, "fake_claude.py")
FAIL_LINE = "[info] - FooSpec should bar *** FAILED ***"


def run(rid, num, wf, wfile, sha, status="completed", conclusion="success", created="2026-10-27T08:00:00Z"):
    return {"databaseId": rid, "number": num, "status": status, "conclusion": conclusion if status == "completed" else "",
            "headSha": sha, "url": f"http://f/runs/{num}", "displayTitle": "t", "workflowName": wf, "workflowFile": wfile,
            "headBranch": "main", "createdAt": created, "updatedAt": created}


class RedBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        t = os.path.realpath(self.tmp.name)
        for k in ("GIT_AUTHOR_NAME", "GIT_AUTHOR_EMAIL", "GIT_COMMITTER_NAME", "GIT_COMMITTER_EMAIL", "GIT_CONFIG_GLOBAL",
                  "GIT_CONFIG_NOSYSTEM"):
            os.environ[k] = ENV[k]
        self.r = Repo(t)
        self.d = os.path.join(t, "gh")
        os.makedirs(self.d)
        self.settings = os.path.join(t, "settings.json")
        with open(self.settings, "w") as f:
            json.dump({"permissions": {"defaultMode": "auto"}}, f)
        self.env = {"AGENTCTL_SCRATCH": os.path.join(t, "scratch"), "AGENTCTL_FORGEJO_GH": FAKE, "FAKE_GH_DIR": self.d,
                    "GH_TOKEN": "dummy", "AGENTCTL_OWNERS_DIR": os.path.join(t, "owners"),
                    "AGENTCTL_REDS_DIR": os.path.join(t, "reds"), "AGENTCTL_INTERNAL_TREES_DIR": os.path.join(t, "internal"), "AGENTCTL_INBOX_DIR": os.path.join(t, "inbox"),
                    "AGENTCTL_RED_REPO": self.r.path, "AGENTCTL_CLAUDE": FAKE_CLAUDE,
                    "AGENTCTL_CLAUDE_SETTINGS": self.settings, "FAKE_CLAUDE_OUT": os.path.join(t, "claude-out"),
                    "AGENTCTL_BRIEF": os.path.join(t, "brief.md"), "AGENTCTL_BRIEF_SRC": os.path.join(t, "brief-src.md"),
                    "AGENTCTL_WAITING_DIR_UNUSED": "", "FAKE_CLAUDE_SLEEP": "0", "CLAUDE_PID": "",
                    "CLAUDE_CODE_SESSION_ID": "", "AGENTCTL_AGENT_NAME": "", "AGENTCTL_CLAIM_PID": "",
                    "AGENTCTL_RED_FIXER": "", "AGENTCTL_RED_MAX_FIXERS": "", "AGENTCTL_RED_DAILY_CAP": "",
                    "AGENTCTL_RED_MAX_OUTSIDE": "", "AGENTCTL_RED_GATE_MAX_FIXERS": "",
                    "AGENTCTL_NOTIFY": "true", "AGENTCTL_INBOX_ESCALATE_MIN": ""}
        self.saved = {k: os.environ.get(k) for k in self.env}
        os.environ.update(self.env)
        for k, v in self.env.items():
            if v == "":
                os.environ.pop(k, None)
        with open(self.env["AGENTCTL_BRIEF_SRC"], "w") as f:
            f.write("# brief\n")
        self.saved_a = A.FORGEJO_GH, A.WAITING_DIR, A.land_cwds
        A.FORGEJO_GH, A.WAITING_DIR = FAKE, os.path.join(t, "waiting-state")
        A.land_cwds = lambda: {}
        A._gate_lists.clear()
        A._outside_lists.clear()
        self.sha = sh(self.r.path, "git", "rev-parse", "HEAD")
        self.put(f"api-repos_pawelkrupinski_movies_git_commits_{self.sha}.json",
                 {"author": {"name": "Agent Smith", "email": "a@b"}, "message": "resolver: tweak"})
        self.main_red(40, 20)

    def tearDown(self):
        A.FORGEJO_GH, A.WAITING_DIR, A.land_cwds = self.saved_a
        for k, v in self.saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        for p in glob.glob(os.path.join(self.tmp.name, "reds", "*", "fixer*.json")):  # no fixer outlives its test
            rec = json.load(open(p))
            for pid in (rec.get("pid"), rec.get("claude_pid")):
                if pid:
                    try:
                        os.killpg(pid, 15)
                    except OSError:
                        pass
        self.tmp.cleanup()

    def put(self, name, obj):
        with open(os.path.join(self.d, name), "w") as f:
            f.write(obj if isinstance(obj, str) else json.dumps(obj))

    def main_red(self, rid, num, line=FAIL_LINE, job="test (rest)", extra_runs=(), kind="test", older_runs=()):
        steps = [{"name": "Set up job" if kind == "infra" else "Run tests", "conclusion": "failure"}]
        self.put("runs.json", list(extra_runs) + [
            run(rid, num, "Main", "main.yml", self.sha, conclusion="failure")] + list(older_runs) + [
            run(30, 15, "Main", "main.yml", self.sha, created="2026-10-27T06:00:00Z"),
            run(39, 19, "Identity model convergence", "identity-model-convergence.yml", self.sha)])
        self.put(f"run-{rid}.json", {"jobs": [{"name": "build", "status": "completed", "conclusion": "success"},
                                              {"name": job, "status": "completed", "conclusion": "failure", "steps": steps}],
                                     "headSha": self.sha, "url": "u", "number": num, "status": "completed",
                                     "conclusion": "failure", "workflowName": "Main"})
        self.put(f"run-{rid}.log", f"{job}\tRun tests\t2026-10-27T08:00:00Z {line}\n" if kind != "infra" else "")

    def tool(self, *argv, cwd=None):
        out = io.StringIO()
        old = os.getcwd()
        try:
            if cwd:
                os.chdir(cwd)
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
                rc = A.main(list(argv))
        finally:
            os.chdir(old)
        return rc, out.getvalue()

    def why(self, *extra):
        A._gate_lists.clear()
        return self.tool("ci-why", "--repo", "movies", *extra)

    def units(self):
        A._gate_lists.clear()
        rows = [(n, d, A._gate_lists.get(n, []), True) for n, d, _nw, _s in A.gate_runs(MOVIES)]
        rows = [(n, d, A._gate_lists.get(n, []), g) for n, d, _r, g in rows]
        return A.gate_red_units(MOVIES, rows)

    def patrol(self, t, dispatch=True):
        units = self.units()
        return A.red_patrol(MOVIES, self.r.path, units, A.find_fixers(self.r.path, units, now_=t), dispatch=dispatch, now_=t)

    def inbox(self, kind=None):
        es = A.inbox_entries()
        return [e for e in es if kind is None or e.get("kind") == kind]

    def wait_fixer(self, timeout=180, n=1):
        t0 = time.time()
        while time.time() - t0 < timeout:
            done = [e for e in self.inbox() if e.get("kind") in ("FIXER_LANDED", "FIXER_FAILED", "DIAGNOSED")]
            if len(done) >= n:
                return done
            time.sleep(0.2)
        self.fail("no fixer verdict in the inbox")

    def worktree_commit(self, name, msg, base="forgejo/main"):
        wt = self.r.worktree(name, base=base, branch=name)
        self.r.write("fix.txt", f"{name}\n", at=wt)
        return wt, self.r.commit(msg, at=wt)


class FixingShownTest(RedBase):
    def test_nobody_fixing_a_gate_red_says_so(self):
        rc, out = self.why()
        self.assertEqual(rc, 1, out)
        self.assertIn("Main: RED (run 40 #20", out)
        self.assertIn(f"  fixing: {A.DISPATCHING_GAP}", out)
        rc, out = self.why("--summary")
        self.assertEqual(out.strip().splitlines()[-1], f"gate RED (main #20 → {A.DISPATCHING_GAP}; convergence green)")

    def test_a_claim_is_shown_under_the_red_row_and_in_the_summary(self):
        wt = self.r.worktree("repo-wt-fixer", branch="fixer")
        os.environ["AGENTCTL_AGENT_NAME"] = "fixer-agent"
        os.environ["AGENTCTL_CLAIM_PID"] = str(os.getpid())
        try:
            rc, out = self.tool("red", "claim", "20", "--note", "FooSpec's clock", "--repo", "movies", cwd=wt)
        finally:
            os.environ.pop("AGENTCTL_AGENT_NAME")
            os.environ.pop("AGENTCTL_CLAIM_PID")
        self.assertEqual(rc, 0, out)
        self.assertIn("claimed Main #20 [test (rest)] for fixer-agent", out)
        rc, out = self.why()
        self.assertIn(f"  fixing: fixer-agent ({wt}, claimed) -- FooSpec's clock", out)
        self.assertNotIn("NOBODY", out)
        rc, out = self.why("--summary")
        self.assertIn("main #20 → fixer-agent claimed", out)
        rc, out = self.tool("red", "list")
        self.assertIn("Main #20 [test (rest)] fixer-agent", out)
        self.assertIn(": live", out)
        os.environ["AGENTCTL_AGENT_NAME"] = "fixer-agent"
        try:
            rc, out = self.tool("red", "release", "20", cwd=wt)
        finally:
            os.environ.pop("AGENTCTL_AGENT_NAME")
        self.assertEqual(rc, 0, out)
        self.assertIn(f"  fixing: {A.DISPATCHING_GAP}", self.why()[1])

    def test_a_newer_run_failing_the_same_way_keeps_the_claim(self):
        # the claimed run's own first error line: the same as #22's but for numbers and the seed (the same signature)
        self.put("run-40.log", f"test (rest)\tRun tests\t2026-10-27T08:00:00Z {FAIL_LINE} (took 9 ms, seed 0xdeadbeef01)\n")
        os.environ["AGENTCTL_CLAIM_PID"] = str(os.getpid())
        try:
            self.tool("red", "claim", "20", "--repo", "movies", cwd=self.r.path)
        finally:
            os.environ.pop("AGENTCTL_CLAIM_PID")
        # a newer push: Main #22 fails in the same job with the same line (other numbers, another sha)
        self.main_red(44, 22, line="[info] - FooSpec should bar *** FAILED *** (took 812 ms, seed 0x1a2b3c4d5e)",
                      older_runs=[run(40, 20, "Main", "main.yml", self.sha, conclusion="failure")])
        self.put("run-40.log", f"test (rest)\tRun tests\t2026-10-27T08:00:00Z {FAIL_LINE} (took 9 ms, seed 0xdeadbeef01)\n")
        rc, out = self.why()
        self.assertIn("Main: RED (run 44 #22", out)
        self.assertIn("claimed)", out)
        self.assertNotIn("NOBODY", out)

    def test_signatures_ignore_numbers_shas_and_timestamps(self):
        a = A.signature("Main", "test", "2026-10-27T08:00:00.1Z FooSpec failed after 812 ms at abc1234def (/tmp/x1/y)")
        b = A.signature("Main", "test", "2026-10-28T09:11:00.9Z FooSpec failed after 13 ms at 9f8e7d6c5b (/tmp/q/z)")
        self.assertEqual(a, b)
        self.assertNotEqual(a, A.signature("Main", "test", "BarSpec failed"))
        self.assertNotEqual(a, A.signature("Main", "other job", "FooSpec failed after 1 ms at abc1234def"))

    def test_an_unlanded_fixes_red_commit_is_committed_and_its_land_is_landing(self):
        wt, _sha = self.worktree_commit("repo-wt-fix", "fix FooSpec\n\nFixes-Red: 20")
        out = self.why()[1]
        self.assertIn(f"  fixing: repo-wt-fix ({wt}, committed)", out)
        A.land_cwds = lambda: {os.path.realpath(wt): 4242}
        self.assertIn(f"  fixing: repo-wt-fix ({wt}, landing)", self.why()[1])

    def test_a_commit_naming_another_run_fixes_nothing_here(self):
        self.worktree_commit("repo-wt-other", "fix\n\nFixes-Red: 999")
        self.assertIn(f"  fixing: {A.DISPATCHING_GAP}", self.why()[1])

    def test_a_waiting_entry_with_the_fix_is_waiting(self):
        wt = self.r.worktree("repo-wt-wait", branch="wait")
        d, _c, _w = A.waiting_state(self.r.path)
        A.save_entry(d, {"id": "e1", "seq": 1, "worktree": wt, "head": self.sha, "commits": [], "fixes": ["40"],
                         "status": "waiting", "created": A.ts(), "tip": self.sha})
        self.assertIn(f"  fixing: repo-wt-wait ({wt}, waiting)", self.why()[1])

    def test_a_landed_fix_awaits_the_next_run(self):
        self.r.write("fix.txt", "fixed\n")
        fix = self.r.commit("fix FooSpec\n\nFixes-Red: 20")
        self.r.push()
        self.main_red(40, 20, extra_runs=[run(50, 25, "Main", "main.yml", fix, status="in_progress",
                                              created="2026-10-27T09:00:00Z")])
        rc, out = self.why()
        self.assertIn(f"  fixing: fix landed in {fix[:9]}, awaiting #25 (fix FooSpec)", out)
        self.assertIn(f"main #20 → fix {fix[:7]} awaiting #25", self.why("--summary")[1])

    def test_a_landed_fix_whose_run_still_fails_this_way_fixes_nothing(self):
        self.r.write("fix.txt", "fixed\n")
        fix = self.r.commit("fix FooSpec\n\nFixes-Red: 20")
        self.r.push()
        # Main #22 contains the fix and fails the same way
        self.main_red(44, 22, older_runs=[run(40, 20, "Main", "main.yml", self.sha, conclusion="failure")])
        runs = json.load(open(os.path.join(self.d, "runs.json")))
        runs[0]["headSha"] = fix
        self.put("runs.json", runs)
        out = self.why()[1]
        self.assertIn(f"  fixing: {A.DISPATCHING_GAP}", out)
        self.assertIn(f"(fix {fix[:9]} landed, and #22 still fails this way)", out)

    def test_reds_outside_the_gate_show_who_fixes_them_too(self):
        runs = json.load(open(os.path.join(self.d, "runs.json")))
        runs = [r for r in runs if r["workflowName"] != "Main"] + [
            run(60, 30, "Main", "main.yml", self.sha), run(61, 31, "Nightly", "nightly.yml", self.sha, conclusion="failure")]
        self.put("runs.json", runs)
        self.put("run-61.json", {"jobs": [{"name": "stress", "status": "completed", "conclusion": "failure",
                                           "steps": [{"name": "Run", "conclusion": "failure"}]}], "headSha": self.sha,
                                 "number": 31, "status": "completed", "conclusion": "failure", "workflowName": "Nightly"})
        self.put("run-61.log", "stress\tRun\t2026-10-27T08:00:00Z [error] boom\n")
        self.worktree_commit("repo-wt-nightly", "fix nightly\n\nFixes-Red: 31")
        rc, out = self.why()
        self.assertEqual(rc, 0, out)
        self.assertIn("Nightly: RED for", out)
        self.assertIn("    fixing: repo-wt-nightly (", out)


class StaleClaimTest(RedBase):
    def dead_pid(self):
        p = subprocess.Popen(["true"])
        p.wait()
        return p.pid

    def claim(self, pid, minutes_ago, wt=None):
        os.environ["AGENTCTL_CLAIM_PID"] = str(pid)
        os.environ["AGENTCTL_AGENT_NAME"] = "gone-agent"
        try:
            self.tool("red", "claim", "20", "--repo", "movies", cwd=wt or self.r.path)
        finally:
            os.environ.pop("AGENTCTL_CLAIM_PID")
            os.environ.pop("AGENTCTL_AGENT_NAME")
        for c in A.all_claims():
            old = time.time() - minutes_ago * 60
            c["started"] = c["beat"] = A.iso_at(old)
            A.write_json(c["path"], {k: v for k, v in c.items() if k != "path"})

    def test_a_dead_claimant_with_no_heartbeat_for_30_min_is_stale_and_swept(self):
        wt = self.r.worktree("repo-wt-old", branch="old")
        subprocess.run(["git", "-C", wt, "commit", "-q", "--allow-empty", "-m", "old", "--date", "2026-01-01T00:00:00Z"],
                       env=dict(ENV, GIT_COMMITTER_DATE="2026-01-01T00:00:00Z"), check=True)
        gd = sh(wt, "git", "rev-parse", "--absolute-git-dir")
        old = time.time() - 3600
        for f in ("index", "logs/HEAD"):
            if os.path.exists(os.path.join(gd, f)):
                os.utime(os.path.join(gd, f), (old, old))
        self.claim(self.dead_pid(), 45, wt)
        out = self.why()[1]
        self.assertIn(f"  fixing: {A.DISPATCHING_GAP} (the patrol assigns its live author or dispatches a fixer now) (stale claim: gone-agent", out)
        self.assertIn("no commit or activity for", out)
        self.assertIn("STALE", self.tool("red", "list")[1])
        removed = A.sweep_claims()
        self.assertEqual(len(removed), 1, removed)
        self.assertEqual(A.all_claims(), [])

    def test_a_dead_claimant_with_a_fresh_claim_is_not_stale_yet(self):
        self.claim(self.dead_pid(), 5)
        self.assertIn("claimed)", self.why()[1])

    def test_a_stale_claim_is_taken_over_by_a_dispatched_fixer(self):
        wt = self.r.worktree("repo-wt-old2", branch="old2")
        subprocess.run(["git", "-C", wt, "commit", "-q", "--allow-empty", "-m", "old"],
                       env=dict(ENV, GIT_COMMITTER_DATE="2026-01-01T00:00:00Z"), check=True)
        gd = sh(wt, "git", "rev-parse", "--absolute-git-dir")
        old = time.time() - 7200
        for f in ("index", "logs/HEAD"):
            if os.path.exists(os.path.join(gd, f)):
                os.utime(os.path.join(gd, f), (old, old))
        self.claim(self.dead_pid(), 45, wt)
        t = time.time()
        acts = self.patrol(t) + self.patrol(t + 11 * 60)
        self.assertTrue(any("DISPATCHED" in a for a in acts), acts)
        self.wait_fixer()


class PatrolTest(RedBase):
    def test_red_unclaimed_after_10_min_once_and_it_wakes_inbox_wait(self):
        t = time.time()
        self.assertEqual(self.patrol(t, dispatch=False), [])
        self.assertEqual(self.patrol(t + 9 * 60, dispatch=False), [])
        acts = self.patrol(t + 11 * 60, dispatch=False)
        self.assertEqual(len(acts), 1, acts)
        self.assertTrue(acts[0].startswith("RED UNCLAIMED: Main #20 [test (rest)]: unassigned -- nobody fixing it for 11 min"), acts)
        self.assertEqual(self.patrol(t + 20 * 60, dispatch=False), [])  # one per spell
        self.assertEqual(len(self.inbox("RED_UNCLAIMED")), 1)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            rc = A.inbox_wait(A.WAIT_KINDS_DEFAULT, timeout=1)
        self.assertEqual(rc, 0)
        self.assertIn("RED UNCLAIMED: Main #20", out.getvalue())

    def test_a_claimed_red_is_never_unclaimed(self):
        os.environ["AGENTCTL_CLAIM_PID"] = str(os.getpid())
        try:
            self.tool("red", "claim", "20", "--repo", "movies", cwd=self.r.path)
        finally:
            os.environ.pop("AGENTCTL_CLAIM_PID")
        t = time.time()
        self.patrol(t)
        self.assertEqual(self.patrol(t + 30 * 60), [])
        self.assertEqual(self.inbox("RED_UNCLAIMED"), [])
        self.assertEqual(A.fixer_records(), [])

    def test_ci_why_writes_the_unclaimed_note_but_never_dispatches(self):
        os.environ["AGENTCTL_RED_UNCLAIMED_MIN"] = "0"
        try:
            self.why()
        finally:
            os.environ.pop("AGENTCTL_RED_UNCLAIMED_MIN")
        self.assertEqual(len(self.inbox("RED_UNCLAIMED")), 1)
        self.assertEqual(A.fixer_records(), [])

    def test_blocked_on_owner_is_loud_and_counts_as_assigned(self):
        self.main_red(40, 20, kind="infra")  # an owner cause outside the code
        os.environ["AGENTCTL_CLAIM_PID"] = str(os.getpid())
        try:
            rc, out = self.tool("red", "block", "20", "--owner-cause", "infra-apply", "--cmd", "bin/deploy ci-runner --apply",
                                "--repo", "movies", cwd=self.r.path)
        finally:
            os.environ.pop("AGENTCTL_CLAIM_PID")
        self.assertEqual(rc, 0, out)
        self.assertEqual(len(self.inbox("BLOCKED")), 1)
        why = self.why()[1]
        self.assertIn("fixing: BLOCKED ON OWNER: failing: test (rest):", why)  # the owner's whole picture
        self.assertIn("only the owner: an infra apply only the owner may run. run: `! bin/deploy ci-runner --apply`", why)
        self.assertIn("main #20 → BLOCKED ON OWNER (auto-clears when Main is green; `agentctl ci-why 20`)",
                      self.why("--summary")[1])
        t = time.time()
        self.patrol(t)
        self.assertEqual(self.patrol(t + 60 * 60), [])


class DispatchTest(RedBase):
    def test_a_red_nobody_fixes_gets_one_headless_fixer(self):
        os.environ["FAKE_CLAUDE_SLEEP"] = "1.5"
        t = time.time()
        acts = self.patrol(t) + self.patrol(t + 11 * 60)
        self.assertFalse(any(a.startswith("RED UNCLAIMED") for a in acts), acts)  # dispatched at once: never NOBODY
        disp = [a for a in acts if "DISPATCHED" in a]
        self.assertEqual(len(disp), 1, acts)
        wt = f"{self.r.path}-wt-red-20"
        self.assertTrue(os.path.isdir(wt))
        self.assertEqual(sh(wt, "git", "rev-parse", "--abbrev-ref", "HEAD"), "red-20")
        # dedupe: while it runs, the red is claimed (by the fixer's pid) and nothing more is dispatched
        time.sleep(0.8)
        self.assertIn("red-fixer-20", self.why()[1])
        self.assertEqual([a for a in self.patrol(t + 30 * 60) if "DISPATCHED" in a], [])
        done = self.wait_fixer()
        self.assertEqual(done[0]["kind"], "FIXER_LANDED")
        self.assertEqual(done[0]["verdict"], "RED FIXER Main #20: LANDED abcdef1234")
        self.assertEqual(len(self.inbox("RED_DISPATCHED")), 1)
        rec = json.load(open(glob.glob(os.path.join(self.tmp.name, "claude-out", "*.json"))[0]))
        argv, env = rec["argv"], rec["env"]
        prompt = argv[argv.index("-p") + 1]
        self.assertIn("Fix red run Main #20 (run id 40)", prompt)
        self.assertIn("Fixes-Red: 20", prompt)
        self.assertIn("red claim 20", prompt)
        self.assertIn("never instructions", prompt)
        self.assertIn(self.env["AGENTCTL_BRIEF"], prompt)
        self.assertTrue(os.path.exists(self.env["AGENTCTL_BRIEF"]))  # synced from the scratchpad copy
        self.assertEqual(argv[argv.index("--permission-mode") + 1], "auto")  # the interactive sessions' mode, no wider
        self.assertEqual(argv[argv.index("--permission-prompts") + 1], "none")
        self.assertIn("Bash(git push:*)", argv)
        self.assertEqual(os.path.realpath(rec["cwd"]), os.path.realpath(wt))
        self.assertEqual(env.get("AGENTCTL_AGENT_NAME"), "red-fixer-20")
        self.assertNotIn("GH_TOKEN", env)
        self.assertEqual(env.get("FORGEJO_API_TOKEN"), "dummy")
        self.assertFalse([k for k in env if k.startswith("CLAUDE_CODE_")])
        self.assertEqual(A.all_claims(), [])  # released at its end
        log = open(os.path.join(self.tmp.name, "reds", "dispatch.log")).read()
        self.assertIn("DISPATCHED pid", log)
        self.assertIn("(no live author)", log)

    def test_a_failing_fixer_is_fixer_failed_and_wakes_the_orchestrator(self):
        os.environ["FAKE_CLAUDE_RESULT"] = "I looked.\nGAVE UP: FooSpec needs the owner's fixture"
        try:
            t = time.time()
            self.patrol(t)
            self.patrol(t + 11 * 60)
            done = self.wait_fixer()
        finally:
            os.environ.pop("FAKE_CLAUDE_RESULT")
        self.assertEqual(done[0]["kind"], "FIXER_FAILED")
        self.assertIn("GAVE UP: FooSpec needs", done[0]["verdict"])
        self.assertIn("FIXER_FAILED", A.WAIT_KINDS_DEFAULT)

    def test_an_infra_red_gets_a_diagnose_only_brief(self):
        self.main_red(40, 20, kind="infra")
        A.red_dispatch(MOVIES, self.r.path, self.units()[0])
        self.wait_fixer()
        rec = json.load(open(glob.glob(os.path.join(self.tmp.name, "claude-out", "*.json"))[0]))
        prompt = rec["argv"][rec["argv"].index("-p") + 1]
        self.assertIn("DIAGNOSE ONLY", prompt)
        self.assertIn("Make NO code change", prompt)
        self.assertNotIn("Fixes-Red: 20", prompt)

    def test_escalation_after_two_dispatched_fixers(self):
        u = self.units()[0]
        rec = json.load(open(A.sig_path(u["sig"])))
        rec["dispatches"] = [{"at": "x", "run": 20}, {"at": "y", "run": 20}]
        A.write_json(A.sig_path(u["sig"]), rec)
        t = time.time()
        acts = self.patrol(t) + self.patrol(t + 11 * 60)
        # 2026-10-28: the escalation is a named investigation fixer, never "a person must take it" with nobody on it
        self.assertTrue(any(a.startswith("RED ESCALATE: Main #20 [test (rest)] still red after 2") and
                            "investigation assigned" in a for a in acts), acts)
        self.assertEqual(len(self.inbox("RED_ESCALATE")), 1)
        self.assertEqual([r.get("why") for r in A.fixer_records()], ["escalated after 2 fixers: investigation"])
        self.wait_fixer()

    def test_a_claim_made_while_the_patrol_decides_stops_the_dispatch(self):
        t = time.time()
        real, n = A.find_fixers, [0]

        def racing(repo, units, now_=None, o_r=None):
            out = real(repo, units, now_, o_r)
            n[0] += 1
            if n[0] >= 2:  # the patrol's own look is the first; a claim lands before the pre-spawn look
                for i in out.values():
                    i["fixers"] = [{"who": "agent-x", "worktree": "/w/x", "state": "claimed", "note": "", "named": True}]
            return out
        A.find_fixers = racing
        try:
            acts = self.patrol(t + 11 * 60)
        finally:
            A.find_fixers = real
        self.assertFalse(any("RED DISPATCHED" in a or " DISPATCHED pid" in a for a in acts), acts)
        self.assertEqual(A.fixer_records(), [])
        self.assertIn("claimed meanwhile", open(A.reds_dir("dispatch.log")).read())

    def test_a_gate_red_has_its_own_cap_and_no_daily_cap(self):
        """2026-10-27: gate red Main #2146 sat at NOBODY (daily dispatch budget 6 spent): gate reds never are."""
        os.makedirs(os.path.join(self.tmp.name, "reds", "77"))
        A.write_json(os.path.join(self.tmp.name, "reds", "77", "fixer.json"),
                     {"sig": "other", "pid": os.getpid(), "started": A.ts(), "gate": True})
        os.environ["AGENTCTL_RED_GATE_MAX_FIXERS"] = "1"
        os.environ["AGENTCTL_RED_MAX_FIXERS"] = "1"  # the cap of reds outside the gate: not this one's
        try:
            t = time.time()
            acts = self.patrol(t)
        finally:
            os.environ.pop("AGENTCTL_RED_GATE_MAX_FIXERS")
            os.environ.pop("AGENTCTL_RED_MAX_FIXERS")
        self.assertTrue(any("QUEUED (gate fixer cap 1 reached (1 alive))" in a for a in acts), acts)
        self.assertEqual(self.inbox("RED_UNCLAIMED"), [])  # 2026-10-28: never NOBODY -- queued for the next slot
        self.assertEqual([e["sig"] for e in A.fixer_queue(gate=True)], [self.units()[0]["sig"]])
        A.write_json(os.path.join(self.tmp.name, "reds", "77", "fixer.json"),
                     {"sig": "other", "pid": 1 << 22, "started": A.ts(), "gate": False, "ended": A.ts(),
                      "verdict": "LANDED abc1234"})
        os.environ["AGENTCTL_RED_DAILY_CAP"] = "0"
        try:
            acts = self.patrol(t + 12 * 60)
        finally:
            os.environ.pop("AGENTCTL_RED_DAILY_CAP")
        self.assertTrue(any(" DISPATCHED pid" in a for a in acts), acts)  # the daily budget is not a gate red's
        self.wait_fixer()

    def test_an_author_whose_session_is_gone_does_not_delay_the_fixer(self):
        p = subprocess.Popen(["true"])
        p.wait()
        os.environ["CLAUDE_PID"] = str(p.pid)
        try:
            A.inbox_write("watch", "WATCH RED: Main #20 test (rest) (it contains your abc)", [], [], 3)
        finally:
            os.environ.pop("CLAUDE_PID")
        acts = self.patrol(time.time())
        self.assertTrue(any("DISPATCHED" in a and "no live author" in a for a in acts), acts)
        self.wait_fixer()

    def test_dispatch_needs_the_interactive_auto_mode_and_says_what_the_owner_must_set(self):
        with open(self.settings, "w") as f:
            json.dump({"permissions": {"defaultMode": "default"}}, f)
        rc, out = self.tool("red", "dispatch", "20", "--repo", "movies")
        self.assertEqual(rc, 2, out)
        self.assertIn("NOT DISPATCHED: interactive sessions run in permission mode 'default'", out)
        self.assertIn('permissions.defaultMode to "auto"', out)
        t = time.time()
        acts = self.patrol(t) + self.patrol(t + 11 * 60)
        self.assertTrue(any("QUEUED (interactive sessions run in permission mode" in a for a in acts), acts)
        self.assertEqual(A.fixer_records(), [])

    def test_auto_off_stops_the_dispatch(self):
        self.assertIn("automatic dispatch: OFF", self.tool("red", "auto", "off")[1])
        t = time.time()
        acts = self.patrol(t)
        self.assertTrue(any("dispatch is OFF" in a for a in acts), acts)
        self.assertIn("main #20 → queued for a fixer (#1)", self.why("--summary")[1])
        self.assertIn("queued for a fixer (#1, next slot when dispatch can run: dispatch is OFF", self.why()[1])
        self.assertIn("automatic dispatch: ON", self.tool("red", "auto", "on")[1])

    def test_red_dispatch_by_hand_refuses_a_red_being_fixed(self):
        os.environ["AGENTCTL_CLAIM_PID"] = str(os.getpid())
        try:
            self.tool("red", "claim", "20", "--repo", "movies", cwd=self.r.path)
        finally:
            os.environ.pop("AGENTCTL_CLAIM_PID")
        rc, out = self.tool("red", "dispatch", "20", "--repo", "movies")
        self.assertEqual(rc, 1, out)
        self.assertIn("is being fixed", out)


if __name__ == "__main__":
    unittest.main()


BASE_HOOK = """#!/bin/sh
head="${2##*..}"
fail=0
if git ls-tree -r --name-only "$head" | grep -q BROKEN; then
  echo "[info] - BrokenSpec should work *** FAILED *** (took 12 ms)"; echo "! pre-push sbt FAILED (3s)"; fail=1
else echo "pre-push sbt ok (1s)"; fi
if git ls-tree -r --name-only "$head" | grep -q LINT; then echo "[error] lint: bad style"; echo "! pre-push lint FAILED (1s)"; fail=1; fi
exit $fail
"""

# BASE_HOOK's last line, for a hook with a baseline mode: sbt's failure passes when <dir>/<base sha> names the base and
# holds the same failure (the movies hook compares every failure line; this one, the spec's)
BASELINE_MODE = """b="$(git rev-parse "${2%%..*}")"; f="${PREPUSH_BASELINE_DIR:-/nonexistent}/$b"
if [ $fail = 1 ] && ! git ls-tree -r --name-only "$head" | grep -q LINT && [ -r "$f" ] \\
   && [ "$(head -1 "$f")" = "pre-push baseline $b" ] && grep -q 'BrokenSpec should work' "$f"; then
  echo "hook: known red sbt"; exit 0
fi
exit $fail"""


class BaselineTest(RedBase):
    """Item 9: pre-push failing on plain forgejo/main is main's own red: recorded, shown, claimable; a change is told
    which of its failures are its own."""

    def setUp(self):
        super().setUp()
        self.hook = os.path.join(self.tmp.name, "hook.sh")
        with open(self.hook, "w") as f:
            f.write(BASE_HOOK)
        os.chmod(self.hook, 0o755)
        self.r.write("BROKEN", "main's own red\n")
        self.r.commit("main breaks BrokenSpec")
        self.r.push()
        self.put("runs.json", [run(40, 20, "Main", "main.yml", self.sha),
                               run(39, 19, "Identity model convergence", "identity-model-convergence.yml", self.sha)])

    def dry(self, wt, head):
        out = io.StringIO()
        log = os.path.join(self.tmp.name, "land.log")
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
            rc = A.dry_run(wt, self.hook, head, log, "t")
        return rc, out.getvalue()

    def test_parse_the_hooks_failed_checks(self):
        out = ("\x1b[33m! pre-push\x1b[0m [error] x *** FAILED *** 12 ms\n\x1b[33m! pre-push\x1b[0m sbt FAILED (3s)\n"
               "\x1b[36m▸ pre-push\x1b[0m lint ok (1s)\n")
        self.assertEqual(set(A.check_failures(out)), {"sbt"})
        self.assertEqual(A.check_passes(out), {"lint"})

    def test_a_change_failing_only_where_main_fails_is_told_so_and_main_s_red_is_recorded(self):
        wt, head = self.worktree_commit("repo-wt-ok", "an innocent change")
        rc, out = self.dry(wt, head)
        self.assertEqual(rc, 1, out)
        self.assertIn("KNOWN RED: sbt fails the same way on plain forgejo/main", out)
        self.assertIn("NOT LANDED: the pre-push checks failed only where plain forgejo/main fails too (sbt", out)
        self.assertEqual(set(A.local_red()["checks"]), {"sbt"})
        rc, out = self.why()
        self.assertIn("Local checks: RED on plain forgejo/main", out)
        self.assertIn(f"  fixing: {A.DISPATCHING_GAP}", out)
        self.assertIn("brokenspec should work", out)
        os.environ["AGENTCTL_CLAIM_PID"] = str(os.getpid())
        try:
            rc, out = self.tool("red", "claim", "local", "--repo", "movies", cwd=wt)
        finally:
            os.environ.pop("AGENTCTL_CLAIM_PID")
        self.assertEqual(rc, 0, out)
        self.assertIn("claimed Local checks #local [sbt]", out)
        self.assertIn("claimed)", self.why()[1])
        self.assertIn("local checks → ", self.why("--summary")[1])

    def test_a_change_with_a_failure_of_its_own_is_told_which(self):
        self.r.write("LINT", "x\n")
        wt = self.r.worktree("repo-wt-bad", branch="bad")
        self.r.write("LINT", "bad\n", at=wt)
        head = self.r.commit("a lint error", at=wt)
        rc, out = self.dry(wt, head)
        self.assertEqual(rc, 1, out)
        self.assertIn("KNOWN RED (not held against this change): sbt; NEW with this change: lint", out)
        self.assertIn("NOT LANDED: the pre-push checks failed.", out)

    def test_a_hook_with_a_baseline_mode_is_run_again_against_main_s_baseline_and_passes(self):
        with open(self.hook, "w") as f:
            f.write(BASE_HOOK.replace("exit $fail", BASELINE_MODE))
        wt, head = self.worktree_commit("repo-wt-base", "an innocent change")
        rc, out = self.dry(wt, head)
        self.assertIsNone(rc, out)
        self.assertIn("KNOWN RED: sbt fails the same way on plain forgejo/main", out)
        main = A.git(wt, "rev-parse", "forgejo/main").strip()
        with open(os.path.join(A.reds_dir("baseline-out"), main)) as f:
            self.assertEqual(f.readline(), f"pre-push baseline {main}\n")
        self.assertEqual(set(A.local_red()["checks"]), {"sbt"})  # still main's red: the check failed, it was excused

    def test_a_passing_check_clears_main_s_known_red(self):
        wt, head = self.worktree_commit("repo-wt-ok2", "innocent")
        self.dry(wt, head)
        self.assertTrue(A.local_red())
        os.remove(os.path.join(wt, "BROKEN"))
        fix = self.r.commit("fix BrokenSpec\n\nFixes-Red: local", at=wt)
        rc, out = self.dry(wt, fix)
        self.assertIsNone(rc, out)
        self.assertEqual(A.local_red(), {})


class HeartbeatTest(RedBase):
    def test_a_dispatched_fixer_past_its_wall_time_is_ended_and_says_so(self):
        os.environ.update(FAKE_CLAUDE_SLEEP="30", AGENTCTL_RED_FIXER_WALL="1")
        try:
            A.red_dispatch(MOVIES, self.r.path, self.units()[0])
            done = self.wait_fixer(timeout=180)
        finally:
            os.environ.pop("FAKE_CLAUDE_SLEEP")
            os.environ.pop("AGENTCTL_RED_FIXER_WALL")
        self.assertEqual(done[0]["kind"], "FIXER_FAILED")
        self.assertIn("TIMED OUT after 0 min (maximum wall time)", done[0]["verdict"])

    def test_a_live_dispatched_fixer_whose_log_is_quiet_for_30_min_is_stale(self):
        log = os.path.join(self.tmp.name, "quiet.log")
        open(log, "w").close()
        old = time.time() - 45 * 60
        os.utime(log, (old, old))
        c = {"who": "red-fixer-20", "pid": os.getpid(), "started": A.iso_at(old), "dispatched": True, "log": log}
        self.assertIn("quiet for 45 min", A.claim_stale(c))
        c["dispatched"] = False  # an interactive claimant alive is alive
        self.assertIsNone(A.claim_stale(c))


class LifecycleTest(RedBase):
    """Items 8 (failing fixes), 4 (superseded), 3 (flakes), 11 (outside the gate), 12 (an unreadable gate)."""

    def setUp(self):
        super().setUp()
        self.saved_slug = A.slug
        A.slug = lambda repo: MOVIES
        os.environ["AGENTCTL_RED_UNCLAIMED_MIN"] = "0"

    def tearDown(self):
        A.slug = self.saved_slug
        os.environ.pop("AGENTCTL_RED_UNCLAIMED_MIN", None)
        super().tearDown()

    def patrol_now(self, dispatch=True):
        A._gate_lists.clear()
        A._outside_lists.clear()
        return A.patrol_repo(self.r.path, dispatch=dispatch)

    def runs(self, *rs):
        self.put("runs.json", list(rs) + [run(39, 19, "Identity model convergence", "identity-model-convergence.yml", self.sha)])

    def test_a_signature_passing_on_the_next_run_with_no_fix_is_a_flake_kept_open_until_its_fix_lands(self):
        self.patrol_now(dispatch=False)  # Main #20 red: its signature is recorded
        self.runs(run(41, 21, "Main", "main.yml", self.sha), run(40, 20, "Main", "main.yml", self.sha, conclusion="failure"))
        acts = self.patrol_now(dispatch=False)
        self.assertTrue(any(a.startswith("RED FLAKE: Main #20 [test (rest)] passed on the next run #21") for a in acts), acts)
        self.assertEqual(len(self.inbox("RED_FLAKE")), 1)
        self.assertEqual(list(A.flakes().values())[0]["count"], 1)
        out = self.why()[1]
        self.assertIn("Flaky (passed on the next run", out)
        self.assertIn("Main #20 [test (rest)] 1x", out)
        # a flake gets a fixer too (outside the gate's priority), briefed to make it deterministic
        acts = self.patrol_now()
        self.assertTrue(any("DISPATCHED" in a for a in acts), acts)
        self.wait_fixer()
        rec = json.load(open(glob.glob(os.path.join(self.tmp.name, "claude-out", "*.json"))[0]))
        self.assertIn("FLAKE: Make it deterministic", rec["argv"][rec["argv"].index("-p") + 1].replace("it is a FLAKE. M", "FLAKE: M"))
        # its fix lands: closed
        self.r.write("fix.txt", "deterministic\n")
        self.r.commit("FooSpec: a fixed clock\n\nFixes-Red: 20")
        self.r.push()
        acts = self.patrol_now(dispatch=False)
        self.assertTrue(any(a.startswith("RED FLAKE FIXED") for a in acts), acts)
        self.assertNotIn("Flaky", self.why()[1])

    def test_a_red_cleared_by_another_change_tells_its_fixer_to_stop(self):
        wt = self.r.worktree("repo-wt-fixer", branch="fixer")
        os.environ["AGENTCTL_CLAIM_PID"] = str(os.getpid())
        try:
            self.tool("red", "claim", "20", "--repo", "movies", cwd=wt)
        finally:
            os.environ.pop("AGENTCTL_CLAIM_PID")
        self.patrol_now(dispatch=False)
        # a fix by someone else landed (naming #20), and the next run passes: superseded, not a flake
        self.r.write("other.txt", "x\n")
        self.r.commit("the real fix\n\nFixes-Red: 20")
        self.r.push()
        self.runs(run(41, 21, "Main", "main.yml", self.sha), run(40, 20, "Main", "main.yml", self.sha, conclusion="failure"))
        acts = self.patrol_now(dispatch=False)
        self.assertTrue(any(a.startswith("RED CLEARED: Main #20 [test (rest)] no longer fails: fixed by") for a in acts), acts)
        self.assertIn("told to stop", acts[0])
        self.assertTrue(os.path.exists(os.path.join(wt, "RED-STOP.md")))
        self.assertEqual(A.all_claims(), [])
        self.assertEqual(self.inbox("RED_FLAKE"), [])

    def test_a_failed_fix_goes_back_to_its_fixer_once_then_a_fresh_one_then_the_revert(self):
        # the red: one commit (the culprit) between the last green run (#15, head = its parent) and the first red (#20)
        parent = self.sha
        self.r.write("culprit.txt", "breaks FooSpec\n")
        culprit = self.r.commit("the culprit")
        self.r.push()
        self.put(f"api-repos_pawelkrupinski_movies_git_commits_{culprit}.json", {"author": {"name": "x"}, "message": "c"})
        base = [run(40, 20, "Main", "main.yml", culprit, conclusion="failure"), run(30, 15, "Main", "main.yml", parent)]
        self.runs(*base)
        self.put("run-40.json", {**json.load(open(os.path.join(self.d, "run-40.json"))), "headSha": culprit})
        acts = self.patrol_now()  # fixer 1
        self.assertTrue(any("DISPATCHED pid" in a for a in acts), acts)
        self.wait_fixer()
        # fixer 1 "landed" abcdef1234 = a real commit on main naming #20; #22 contains it and fails the same way
        self.r.write("fix.txt", "attempt 1\n")
        fix1 = self.r.commit("fix attempt 1\n\nFixes-Red: 20")
        self.r.push()
        rec_path = glob.glob(os.path.join(self.tmp.name, "reds", "20", "fixer.json"))[0]
        rec = json.load(open(rec_path))
        rec["verdict"] = f"LANDED {fix1[:10]}"
        A.write_json(rec_path, rec)
        self.put("run-44.json", {**json.load(open(os.path.join(self.d, "run-40.json"))), "number": 22, "headSha": fix1})
        self.put("run-44.log", open(os.path.join(self.d, "run-40.log")).read())
        self.runs(run(44, 22, "Main", "main.yml", fix1, conclusion="failure"), *base)
        acts = self.patrol_now()
        self.assertTrue(any("DISPATCHED (resumed)" in a for a in acts), acts)  # back to the same fixer, once
        self.wait_fixer(n=2)
        resumed = [json.load(open(p)) for p in glob.glob(os.path.join(self.tmp.name, "claude-out", "*.json"))]
        argv = [r["argv"] for r in resumed if "--resume" in r["argv"]][0]
        self.assertEqual(argv[argv.index("--resume") + 1], rec["session"])
        self.assertIn(f"YOUR FIX {fix1[:10]} LANDED", argv[argv.index("-p") + 1])
        acts = self.patrol_now()  # it is not resumed twice: a fresh fixer (the 2nd dispatch)
        self.assertTrue(any("DISPATCHED pid" in a and "resumed" not in a for a in acts), acts)
        self.wait_fixer(n=3)
        landed = []
        saved_land, A.spawn_land = A.spawn_land, lambda wt, env, log: landed.append(wt) or 4242
        self.addCleanup(setattr, A, "spawn_land", saved_land)
        acts = self.patrol_now()  # 2 dispatched fixers failed and the culprit is known: the revert, through land
        self.assertTrue(any(a.startswith(f"RED REVERT: Main #22 [test (rest)] still red after 2 dispatched fixers; "
                                         f"its culprit {culprit[:10]} is known") for a in acts), acts)
        wt = f"{self.r.path}-wt-revert-22"
        msg = sh(wt, "git", "log", "-1", "--format=%B")
        self.assertIn(f"This reverts commit {culprit}", msg)
        self.assertIn("Fixes-Red: 22", msg)
        self.assertEqual(landed, [wt])  # landed by `agentctl land`: the normal hook
        self.assertEqual(len(self.inbox("RED_REVERT")), 1)
        self.assertIn(f"repo-wt-revert-22 ({wt}, committed)", self.why()[1])  # the revert is its fixer meanwhile
        self.assertEqual([a for a in self.patrol_now() if "REVERT" in a or "ESCALATE" in a], [])
        sh(self.r.path, "git", "worktree", "remove", "--force", wt)  # the revert went nowhere, and it is still red:
        acts = self.patrol_now()  # RED ESCALATE, never the revert again
        self.assertTrue(any(a.startswith("RED ESCALATE") for a in acts), acts)

    def test_reds_outside_the_gate_get_fixers_under_their_own_cap(self):
        self.runs(run(41, 21, "Main", "main.yml", self.sha), run(61, 31, "Nightly", "nightly.yml", self.sha, conclusion="failure"),
                  run(62, 32, "Weekly", "weekly.yml", self.sha, conclusion="failure"))
        for rid, num, wf in ((61, 31, "Nightly"), (62, 32, "Weekly")):
            self.put(f"run-{rid}.json", {"jobs": [{"name": "stress", "status": "completed", "conclusion": "failure",
                                                   "steps": [{"name": "Run", "conclusion": "failure"}]}],
                                         "headSha": self.sha, "number": num, "status": "completed", "conclusion": "failure",
                                         "workflowName": wf})
            self.put(f"run-{rid}.log", f"stress\tRun\t2026-10-27T08:00:00Z [error] {wf} boom\n")
        os.environ["FAKE_CLAUDE_SLEEP"] = "2"
        try:
            acts = self.patrol_now()
        finally:
            os.environ.pop("FAKE_CLAUDE_SLEEP")
        self.assertEqual(len([a for a in acts if "DISPATCHED pid" in a]), 1, acts)
        self.assertTrue(any("Weekly #32" in a and "QUEUED (outside-the-gate fixer cap 1 reached)" in a for a in acts), acts)
        # never NOBODY (2026-10-28): the capped red waits in the FIFO queue, with its slot named
        self.assertEqual(self.inbox("RED_UNCLAIMED"), [])
        self.assertIn("fixing: queued for a fixer (#1, next slot when", self.why()[1])
        self.wait_fixer()

    def test_an_unreadable_gate_dispatches_nothing_and_says_so_once_with_an_infra_check(self):
        saved = A.gh_json
        A.gh_json = lambda *a: (_ for _ in ()).throw(A.NoToken("agentctl: could not read the movies FORGEJO_API_TOKEN"))
        try:
            A._last_patrol[0] = 0
            os.environ["AGENTCTL_RED_PATROL"] = "1"
            with contextlib.redirect_stderr(io.StringIO()):
                A.maybe_patrol(self.r.path)
                A._last_patrol[0] = 0
                A.maybe_patrol(self.r.path)
        finally:
            A.gh_json = saved
            os.environ["AGENTCTL_RED_PATROL"] = "0"
        un = self.inbox("UNKNOWN")
        self.assertEqual(len(un), 1)
        self.assertIn("UNKNOWN: the red patrol cannot read the gate (could not read the movies FORGEJO_API_TOKEN)", un[0]["verdict"])
        self.assertIn("Infra check: Forgejo API localhost:3000", un[0]["verdict"])
        self.assertEqual(A.fixer_records(), [])
        self.patrol_now(dispatch=False)
        self.assertTrue(os.path.exists(A.recovery_dir("gate-unread.json")))  # a condition on the gate_read probe
        A.ci_state_refresh(MOVIES)  # read again: the same probe passes, the spell ends, its entry acked
        A.recovery_tick(force=True)
        self.assertFalse(os.path.exists(A.recovery_dir("gate-unread.json")))
        self.assertTrue(all(e["read"] for e in A.inbox_entries() if e.get("kind") == "UNKNOWN"))


class StatuslineTest(unittest.TestCase):
    def test_a_stale_cache_says_as_of_and_nobody_stands_out(self):
        home = tempfile.mkdtemp()
        cache = os.path.join(home, ".cache", "agentctl")
        os.makedirs(os.path.join(cache, "statusline.lock"))  # a refresh "running": none is started
        with open(os.path.join(cache, "statusline.txt"), "w") as f:
            f.write("gate RED (main #20 → NOBODY; convergence green) · waiting: 0 · inbox: 1 unread\n")
        old = time.time() - 600
        os.utime(os.path.join(cache, "statusline.txt"), (old, old))
        r = subprocess.run([os.path.join(HERE, "..", "claude-statusline")], input="{}", capture_output=True, text=True,
                           env=dict(os.environ, HOME=home))
        self.assertIn("\x1b[1;31mNOBODY\x1b[0m", r.stdout)
        self.assertIn(" · as of " + time.strftime("%H:%M", time.localtime(old)), r.stdout)


class FixedNotFlakyTest(RedBase):
    """2026-10-27, live: live drift #2051 and capture #2076 were called flakes, but a fix had landed between the red run
    and the green one (03d646e63, `Fixes-Red: 2049`: the same failure in another workflow); and lean vs whole #2050 got
    a fixer although that fix had landed and it only lacked a newer run."""

    def setUp(self):
        super().setUp()
        self.saved_slug = A.slug
        A.slug = lambda repo: MOVIES
        os.environ["AGENTCTL_RED_UNCLAIMED_MIN"] = "0"
        # the same failure, in another workflow's run #99 (id 199): its signatures are known (cached)
        A.save_run_sigs(MOVIES, {"databaseId": 199, "number": 99, "headSha": self.sha}, "Other",
                        [("test (rest)", "test", FAIL_LINE)])

    def tearDown(self):
        A.slug = self.saved_slug
        os.environ.pop("AGENTCTL_RED_UNCLAIMED_MIN", None)
        super().tearDown()

    def patrol_now(self, dispatch=True):
        A._gate_lists.clear()
        A._outside_lists.clear()
        return A.patrol_repo(self.r.path, dispatch=dispatch)

    def land(self, msg, path="fix.txt"):
        self.r.write(path, msg + "\n")
        sha = self.r.commit(msg)
        self.r.push()
        return sha

    def green_after(self, head):
        self.put("runs.json", [run(41, 21, "Main", "main.yml", head), run(40, 20, "Main", "main.yml", self.sha, conclusion="failure"),
                               run(39, 19, "Identity model convergence", "identity-model-convergence.yml", self.sha)])

    def test_a_fix_of_the_same_cause_in_another_workflow_counts_for_this_red(self):
        fix = self.land("CI: the sparse checkout holds the action\n\nFixes-Red: 99")
        out = self.why()[1]
        self.assertIn(f"  fixing: fix landed in {fix[:9]}, no run containing it yet", out)
        self.assertNotIn("NOBODY", out)
        acts = self.patrol_now()
        self.assertEqual([a for a in acts if "DISPATCHED" in a], [], acts)  # fixed: never dispatched
        self.assertEqual(A.fixer_records(), [])

    def assert_fixed_not_flaky(self, fix):
        self.patrol_now(dispatch=False)
        self.green_after(fix)
        acts = self.patrol_now(dispatch=False)
        self.assertEqual(self.inbox("RED_FLAKE"), [], acts)
        self.assertEqual(A.flakes(), {})
        rec = A.all_sig_records()[0]
        self.assertEqual((rec["status"], rec["fixed_by"]), ("cleared", fix))

    def test_a_cross_workflow_fix_between_the_red_and_the_green_run_is_no_flake(self):
        self.patrol_now(dispatch=False)
        fix = self.land("the fix, named for the other workflow's run\n\nFixes-Red: 99")
        self.green_after(fix)
        # landed_fix finds it directly; and fix_between would too
        rec = A.all_sig_records()[0]
        self.assertEqual(A.fix_between(self.r.path, rec, self.sha, fix, A.name_causes(), A.run_workflows())[0], fix)
        self.patrol_now(dispatch=False)
        self.assertEqual(self.inbox("RED_FLAKE"), [])
        self.assertEqual(A.all_sig_records()[0]["status"], "cleared")

    def test_a_fix_aimed_at_the_same_workflow_between_them_is_no_flake(self):
        A.save_run_sigs(MOVIES, {"databaseId": 140, "number": 14, "headSha": self.sha}, "Main",
                        [("preflight", "test", "::error::secret X is unset")])
        self.patrol_now(dispatch=False)
        fix = self.land("Main: the preflight secret\n\nFixes-Red: 14")
        self.green_after(fix)
        self.patrol_now(dispatch=False)
        self.assertEqual(self.inbox("RED_FLAKE"), [])
        self.assertEqual(A.all_sig_records()[0]["fixed_by"], fix)

    def test_a_change_to_ci_s_own_workflows_between_them_is_no_flake(self):
        self.patrol_now(dispatch=False)
        fix = self.land("CI: plan job checkout", path=".github/workflows/main.yml")
        self.assert_fixed_not_flaky(fix)

    def test_an_unrelated_change_between_them_is_still_a_flake(self):
        self.patrol_now(dispatch=False)
        other = self.land("docs: a typo", path="docs/x.md")
        self.green_after(other)
        self.patrol_now(dispatch=False)
        self.assertEqual(len(self.inbox("RED_FLAKE")), 1)

    def test_a_false_flake_is_reclassified_and_its_count_taken_back(self):
        self.patrol_now(dispatch=False)
        other = self.land("docs: a typo", path="docs/x.md")
        self.green_after(other)
        self.patrol_now(dispatch=False)  # called a flake (nothing between could have fixed it, as far as it knew)
        self.assertEqual(sum(e["count"] for e in A.flakes().values()), 1)
        # it turns out run #99's failure is the same cause, and its fix (in the passing run) names #99
        rec = A.all_sig_records()[0]
        fix = sh(self.r.path, "git", "commit", "--allow-empty", "-q", "-m", "x\n\nFixes-Red: 99") or \
            sh(self.r.path, "git", "rev-parse", "HEAD")
        self.r.push()
        rec["passed_in_head"] = fix
        A.write_json(A.sig_path(rec["sig"]), {k: v for k, v in rec.items()})
        acts = self.patrol_now(dispatch=False)
        self.assertTrue(any("RED FLAKE FIXED" in a and "never a flake" in a for a in acts), acts)
        self.assertEqual(A.flakes(), {})


class SiblingTest(FixedNotFlakyTest):
    def test_a_causeless_signature_follows_its_run_mates_and_old_flakes_get_their_heads_backfilled(self):
        # Main #20 fails in `test (rest)` (a cause) and `timings` (an empty log: no cause of its own)
        self.put("run-40.json", {"jobs": [{"name": "test (rest)", "status": "completed", "conclusion": "failure",
                                           "steps": [{"name": "Run tests", "conclusion": "failure"}]},
                                          {"name": "timings", "status": "completed", "conclusion": "failure",
                                           "steps": [{"name": "Run tests", "conclusion": "failure"}]}],
                                 "headSha": self.sha, "url": "u", "number": 20, "status": "completed",
                                 "conclusion": "failure", "workflowName": "Main"})
        self.patrol_now(dispatch=False)
        fix = self.land("the fix\n\nFixes-Red: 99")
        self.green_after(fix)
        self.patrol_now(dispatch=False)
        recs = {r["job"]: r for r in A.all_sig_records()}
        self.assertEqual(recs["test (rest)"]["fixed_by"], fix)
        self.assertEqual(recs["timings"]["fixed_by"], fix)  # not a flake: its run-mate was fixed
        self.assertEqual(self.inbox("RED_FLAKE"), [])
