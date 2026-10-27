"""Assignment at the moment of red (the owner, 2026-10-27: "why is no one assigned to the red? root cause structural
fix" -- Main #2138 said `fixing: NOBODY` right after it went red). The first patrol assigns a LIVE author (pid alive AND
activity within 15 min) with a claim until HH:MM, else dispatches a fixer at once; an author who does not act within
10 min is dispatched past; every commit of the run since the last green one is attributed, each live author notified,
one fixer at most; infra-shaped reds go to an infra-diagnosis fixer at once; NOBODY only with its reason."""
import glob, json, os, subprocess, sys, time, unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from test_agentctl import A, sh  # noqa: E402
from test_red import RedBase, MOVIES  # noqa: E402


class AssignBase(RedBase):
    def setUp(self):
        super().setUp()
        self.green_sha = self.sha
        self.wts = {}

    def author_change(self, name, pid=None, landed=True):
        """A worktree whose change landed on main through the queue (its entry names the agent's pid)."""
        wt = self.r.worktree(name, branch=name)
        self.r.write(f"{name}.txt", "x\n", at=wt)
        head = self.r.commit(f"change of {name}", at=wt)
        sh(wt, "git", "pull", "-q", "--rebase", "forgejo", "main")
        head = sh(wt, "git", "rev-parse", "HEAD")
        sh(wt, "git", "push", "-q", "forgejo", "HEAD:main")
        sh(self.r.path, "git", "fetch", "-q", "forgejo")
        if landed:
            d, _c, _w = A.waiting_state(self.r.path)
            seq = len(glob.glob(os.path.join(d, "entries", "*.json"))) + 1
            A.save_entry(d, {"id": f"e-{name}", "seq": seq, "worktree": wt, "head": head, "status": "landed",
                             "landed_sha": head, "claude_pid": pid})
        self.wts[name] = wt
        return wt, head

    def dead_pid(self):
        p = subprocess.Popen(["true"])
        p.wait()
        return p.pid

    def red_at(self, sha, kind="test"):
        self.main_red(40, 20, kind=kind)
        runs = json.load(open(os.path.join(self.d, "runs.json")))
        for r in runs:
            if r["databaseId"] == 40:
                r["headSha"] = sha
        self.put("runs.json", runs)
        rec = json.load(open(os.path.join(self.d, "run-40.json")))
        rec["headSha"] = sha
        self.put("run-40.json", rec)

    def age(self, wt, minutes):
        old = time.time() - minutes * 60
        gd = sh(wt, "git", "rev-parse", "--absolute-git-dir")
        for f in ("index", "logs/HEAD", "HEAD", "ORIG_HEAD"):
            p = os.path.join(gd, f)
            if os.path.exists(p):
                os.utime(p, (old, old))
        return old


class AuthorTest(AssignBase):
    def test_a_live_author_is_assigned_at_once_never_nobody_and_a_silent_one_is_dispatched_past_after_10_min(self):
        wt, head = self.author_change("repo-wt-a", pid=os.getpid())
        self.red_at(head)
        t = time.time()
        acts = self.patrol(t)
        self.assertTrue(any(a.startswith("RED AUTHOR: Main #20") and "ASSIGNED to you until" in a for a in acts), acts)
        self.assertEqual(A.fixer_records(), [])
        self.assertEqual(len(self.inbox("RED_AUTHOR")), 1)
        self.assertEqual(os.path.realpath(self.inbox("RED_AUTHOR")[0]["cwd"]), os.path.realpath(wt))
        self.assertIn("RED_AUTHOR", A.WAIT_KINDS_DEFAULT.split(","))
        out = self.why()[1]
        self.assertIn("fixing: author repo-wt-a (", out)
        self.assertIn("-- author, notified, until", out)
        self.assertNotIn("NOBODY", out)
        self.assertIn("main #20 → author repo-wt-a claimed", self.why("--summary")[1])
        self.assertEqual([a for a in self.patrol(t + 5 * 60) if "DISPATCHED" in a], [])  # within its window
        acts = self.patrol(t + 11 * 60)  # no commit, no claim: dispatched past
        self.assertTrue(any("DISPATCHED" in a and "the assigned author did not act" in a for a in acts), acts)
        self.wait_fixer()

    def test_an_author_who_commits_keeps_the_red_past_the_window(self):
        wt, head = self.author_change("repo-wt-a", pid=os.getpid())
        self.red_at(head)
        t = time.time()
        self.patrol(t)
        time.sleep(1.1)
        self.r.write("fix.txt", "fix\n", at=wt)
        self.r.commit("fix it\n\nFixes-Red: 20", at=wt)
        acts = self.patrol(t + 11 * 60)
        self.assertEqual([a for a in acts if "DISPATCHED" in a], [], acts)
        self.assertEqual(A.fixer_records(), [])

    def test_an_author_with_a_live_pid_but_no_activity_for_15_min_is_not_live_a_fixer_goes_at_once(self):
        wt, head = self.author_change("repo-wt-idle", pid=os.getpid())
        self.red_at(head)
        self.age(wt, 30)
        os.environ["AGENTCTL_AUTHOR_ACTIVE_MIN"] = "15"
        t = time.time() + 20 * 60  # the commit itself is 20 min old by then
        acts = self.patrol(t)
        self.assertEqual(self.inbox("RED_AUTHOR"), [])
        self.assertTrue(any("DISPATCHED" in a and "no live author (authors: repo-wt-idle, idle or gone)" in a
                            for a in acts), acts)
        self.wait_fixer()

    def test_a_finished_author_gets_a_fixer_at_once(self):
        gone = self.dead_pid()
        wt, head = self.author_change("repo-wt-gone", pid=gone)
        self.red_at(head)
        acts = self.patrol(time.time())
        self.assertTrue(any("DISPATCHED" in a for a in acts), acts)
        self.assertFalse(any(a.startswith("RED UNCLAIMED") for a in acts), acts)
        self.wait_fixer()

    def test_every_commit_since_the_last_green_run_is_attributed_each_live_author_notified_one_assigned(self):
        wa, ha = self.author_change("repo-wt-a", pid=os.getpid())
        wb, hb = self.author_change("repo-wt-b", pid=os.getpid())
        self.red_at(hb)
        u = self.units()[0]
        self.assertEqual(A.red_commits(self.r.path, u), [hb, ha])
        authors = A.red_authors(self.r.path, u)
        self.assertEqual([os.path.basename(a["worktree"]) for a in authors], ["repo-wt-b", "repo-wt-a"])
        acts = self.patrol(time.time())
        notes = [a for a in acts if a.startswith("RED AUTHOR")]
        self.assertEqual(len(notes), 2, acts)
        self.assertEqual(sum("ASSIGNED to you" in a for a in notes), 1)
        self.assertTrue(any("notified (another author is assigned)" in a for a in notes))
        self.assertEqual(len([c for c in A.all_claims() if c.get("author")]), 1)
        self.assertEqual(A.fixer_records(), [])


class InfraAndNobodyTest(AssignBase):
    def test_an_infra_red_skips_the_authors_and_dispatches_a_diagnosis_at_once(self):
        wt, head = self.author_change("repo-wt-a", pid=os.getpid())
        self.red_at(head, kind="infra")
        acts = self.patrol(time.time())
        self.assertEqual(self.inbox("RED_AUTHOR"), [])
        self.assertTrue(any("DISPATCHED" in a and "infra-shaped: diagnose" in a for a in acts), acts)
        self.wait_fixer()
        rec = json.load(open(glob.glob(os.path.join(self.tmp.name, "claude-out", "*.json"))[0]))
        self.assertIn("DIAGNOSE ONLY", rec["argv"][rec["argv"].index("-p") + 1])

    def test_an_unclear_red_gets_the_diagnosis_brief(self):
        u = dict(self.units()[0], kind="unclear")
        self.assertIn("DIAGNOSE ONLY", A.fixer_prompt(u, "/wt", "/brief"))
        self.assertIn("UNCLEAR", A.fixer_prompt(u, "/wt", "/brief"))

    def test_nobody_only_with_its_reason_the_budget(self):
        self.assertEqual(self.why("--summary")[1].strip().splitlines()[-1],
                         f"gate RED (main #20 → {A.DISPATCHING_GAP}; convergence green)")  # before the patrol: never NOBODY
        os.environ["AGENTCTL_RED_GATE_MAX_FIXERS"] = "0"
        acts = self.patrol(time.time())
        self.assertTrue(any(a.startswith("RED UNCLAIMED: Main #20 [test (rest)]: NOBODY -- gate fixer cap 0 reached")
                            for a in acts), acts)
        self.assertIn("main #20 → NOBODY (skipped: gate fixer cap 0 reached", self.why("--summary")[1])
        self.assertIn("fixing: NOBODY (skipped: gate fixer cap 0 reached", self.why()[1])
        os.environ.pop("AGENTCTL_RED_GATE_MAX_FIXERS")
        self.assertEqual(len(self.inbox("RED_UNCLAIMED")), 1)

    def test_the_processor_patrols_at_once_when_the_snapshot_first_sees_a_gate_red(self):
        A._last_patrol[0] = time.time()
        A._seen_gate_reds.clear()
        saved = A.slug, A.ci_why, A.publish_gate_summary, A.route_branch_reds, A.assign_first
        assigned = []
        A.assign_first = lambda repo, snap: assigned.append(snap)
        A.slug = lambda r: MOVIES
        A.ci_why = lambda *a, **k: (True, "gate RED (x)")
        A.publish_gate_summary = lambda *a, **k: None
        A.route_branch_reds = lambda *a, **k: []
        A._last_gate_publish[0] = 0.0
        try:
            A.maybe_publish_gate(self.r.path)
        finally:
            A.slug, A.ci_why, A.publish_gate_summary, A.route_branch_reds, A.assign_first = saved
        self.assertEqual(len(assigned), 1, "the publisher assigns the new red itself, before it publishes")
        self.assertEqual(A._seen_gate_reds, {40})


if __name__ == "__main__":
    unittest.main()
