"""The red tests' shared base (a movies-like repo, a fake Forgejo and a fake `claude`), and the signature's
normalization. The red machine itself (items, leases, offers, the dispatcher) is tested in test_reds.py."""
import contextlib, glob, io, json, os, sys, tempfile, time, unittest

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
        self.env = {"AGENTCTL_STATE_ROOT": os.path.join(t, "state"), "AGENTCTL_SCRATCH": os.path.join(t, "scratch"), "AGENTCTL_FORGEJO_GH": FAKE, "FAKE_GH_DIR": self.d,
                    "GH_TOKEN": "dummy", "AGENTCTL_OWNERS_DIR": os.path.join(t, "owners"),
                    "AGENTCTL_REDS_DIR": os.path.join(t, "reds"), "AGENTCTL_INTERNAL_TREES_DIR": os.path.join(t, "internal"), "AGENTCTL_INBOX_DIR": os.path.join(t, "inbox"),
                    "AGENTCTL_RED_REPO": self.r.path, "AGENTCTL_CLAUDE": FAKE_CLAUDE,
                    "AGENTCTL_CLAUDE_SETTINGS": self.settings, "FAKE_CLAUDE_OUT": os.path.join(t, "claude-out"),
                    "AGENTCTL_CLAUDE_PROJECTS": os.path.join(t, "claude-projects"), "AGENTCTL_BG_POLL": "0.2",
                    "AGENTCTL_BRIEF": os.path.join(t, "brief.md"), "AGENTCTL_BRIEF_SRC": os.path.join(t, "brief-src.md"),
                    "AGENTCTL_WAITING_DIR_UNUSED": "", "FAKE_CLAUDE_SLEEP": "0", "CLAUDE_PID": "",
                    "CLAUDE_CODE_SESSION_ID": "", "AGENTCTL_AGENT_NAME": "", "AGENTCTL_CLAIM_PID": "",
                    # a dispatched fixer running these tests: its own record is never a test's lease to note on
                    "AGENTCTL_RED_FIXER": "", "AGENTCTL_FIXER_RECORD": "", "AGENTCTL_RED_MAX_FIXERS": "", "AGENTCTL_RED_DAILY_CAP": "",
                    "AGENTCTL_RED_MAX_OUTSIDE": "", "AGENTCTL_RED_GATE_MAX_FIXERS": "",
                    "AGENTCTL_NOTIFY": "true", "AGENTCTL_INBOX_ESCALATE_MIN": "",
                    "AGENTCTL_LAPTOP_PRESSURE": "-"}
        self.env["AGENTCTL_STATE_ROOT"] = os.path.join(t, "state-root")  # its own store: no other test's items or leases
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
        A._flow_runs.clear()  # a workflow an earlier test listed on its own (audit #7) has no runs in this one
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
        for p in glob.glob(os.path.join(self.tmp.name, "claude-out", "agents", "*.json")):  # nor any fake session
            try:
                pid = json.load(open(p)).get("pid")
                if pid:
                    os.killpg(pid, 15)
            except (OSError, ValueError):
                pass
        for p in glob.glob(os.path.join(self.tmp.name, "reds", "*", "fixer*.json")):  # no fixer outlives its test
            rec = json.load(open(p))
            for pid in (rec.get("pid"), rec.get("claude_pid")):
                if pid:
                    try:
                        os.killpg(pid, 15)
                    except OSError:
                        pass
            t0 = time.time()  # gone before its directory is: a stopped wrapper still writes its verdict
            while rec.get("pid") and A.pid_alive(rec["pid"]) and time.time() - t0 < 15:
                try:  # our own child: a zombie answers kill(0) until reaped (it cost every such test 15 s)
                    os.waitpid(rec["pid"], os.WNOHANG)
                except ChildProcessError:
                    pass
                time.sleep(0.1)
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


class SignatureTest(RedBase):
    def test_signatures_ignore_numbers_shas_and_timestamps(self):
        a = A.signature("Main", "test", "2026-10-27T08:00:00.1Z FooSpec failed after 812 ms at abc1234def (/tmp/x1/y)")
        b = A.signature("Main", "test", "2026-10-28T09:11:00.9Z FooSpec failed after 13 ms at 9f8e7d6c5b (/tmp/q/z)")
        self.assertEqual(a, b)
        self.assertNotEqual(a, A.signature("Main", "test", "BarSpec failed"))
        a = A.red_key("Main", "test", "2026-10-27T08:00:00.1Z FooSpec failed after 812 ms at abc1234def (/tmp/x1/y)")
        self.assertEqual(a, A.red_key("Main", "other", "2026-10-28T09:11:00.9Z FooSpec failed after 13 ms at 9f8e7d6c5b"
                                                       " (/tmp/q/z)"))


if __name__ == "__main__":
    unittest.main()
