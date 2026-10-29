"""One agentctl editor (Agent B item 4): `agentctl issue` files a defect in the store and on the status page, the
red fixer is told to use it, and ~/bin's pre-commit hook refuses a fixer's commit."""
import json, os, subprocess, sys, tempfile, unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from test_agentctl import A, Base, TOOL  # noqa: E402

HOOK = os.path.join(HERE, "..", "hooks", "pre-commit")
FIXER_ENV = ("CLAUDE_CODE_SESSION_ID", "AGENTCTL_RED_FIXER", "AGENTCTL_FIXER_RECORD", "AGENTCTL_AGENT_NAME")


class NoFixerBase(Base):
    def setUp(self):
        super().setUp()
        self.saved_env = {k: os.environ.pop(k, None) for k in FIXER_ENV}

    def tearDown(self):
        for k, v in self.saved_env.items():
            os.environ.pop(k, None) if v is None else os.environ.__setitem__(k, v)
        super().tearDown()


class IssueTest(NoFixerBase):
    def test_an_issue_is_stored_listed_on_the_status_page_and_closed(self):
        rc, out = self.run_tool("issue", "land drops the --stash flag", cwd=self.r.path)
        self.assertEqual(rc, 0, out)
        self.assertIn("ISSUE ", out)
        [it] = A.Store().items("issue")
        self.assertEqual((it["state"], it["data"]["text"]), ("open", "land drops the --stash flag"))
        self.assertEqual(os.path.realpath(it["data"]["cwd"]), os.path.realpath(self.r.path))
        data = A.status_data(store=A.Store(), bound=10)
        self.assertEqual([i["text"] for i in data["issues"]], ["land drops the --stash flag"])
        self.assertIn('sec("agentctl issues"', A.STATUS_PAGE_HTML)
        rc, out = self.run_tool("issue", "--list")
        self.assertIn("land drops the --stash flag", out)
        rc, out = self.run_tool("issue", "--close", it["id"], "--note", "fixed in abc")
        self.assertEqual(rc, 0, out)
        self.assertEqual(A.Store().item("issue", it["id"])["state"], "closed")
        self.assertEqual(A.status_data(store=A.Store(), bound=10)["issues"], [])
        rc, out = self.run_tool("issue", "--list")
        self.assertIn("no open agentctl issues", out)

    def test_an_open_issue_escalates_weekly_never_times_out_silently(self):
        self.assertEqual(A.deadline_walk("issue", "open"), (False, True))

    def test_a_store_without_the_issue_machine_says_not_filed(self):
        saved = A.MACHINES.pop("issue")
        try:
            rc, out = self.run_tool("issue", "x")
        finally:
            A.MACHINES["issue"] = saved
        self.assertEqual(rc, 2)
        self.assertIn("NOT FILED", out)


class AmFixerTest(NoFixerBase):
    def am(self, **env):
        e = {k: v for k, v in os.environ.items() if k not in FIXER_ENV}
        e.update(env)
        return subprocess.run([sys.executable, TOOL, "red", "am-fixer"], capture_output=True, text=True, env=e,
                              timeout=60)

    def test_anybody_but_a_fixer_is_not_one(self):
        self.assertEqual(self.am().returncode, 1)
        self.assertEqual(self.am(CLAUDE_CODE_SESSION_ID="s-1").returncode, 1)
        # a session never takes a fixer's identity from an inherited environment (74e2ec5)
        self.assertEqual(self.am(CLAUDE_CODE_SESSION_ID="s-1", AGENTCTL_RED_FIXER="1",
                                 AGENTCTL_AGENT_NAME="red-fixer-1").returncode, 1)

    def test_a_fixer_by_its_session_record_or_by_its_environment_without_a_session(self):
        rec = A.reds_dir("2400", "fixer.json")
        os.makedirs(os.path.dirname(rec), exist_ok=True)
        A.write_json(rec, {"run": 2400, "session": "s-2", "started": A.ts()})
        r = self.am(CLAUDE_CODE_SESSION_ID="s-2")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("2400", r.stdout)
        self.assertEqual(self.am(AGENTCTL_RED_FIXER="1").returncode, 0)


class PreCommitHookTest(unittest.TestCase):
    def setUp(self):
        self.repo = tempfile.mkdtemp()
        subprocess.run(["git", "init", "-q", self.repo], check=True)
        subprocess.run(["git", "-C", self.repo, "config", "core.hooksPath", os.path.dirname(os.path.abspath(HOOK))],
                       check=True)

    def commit(self, tool, **env):
        e = {k: v for k, v in os.environ.items() if k not in FIXER_ENV}
        e.update(env, AGENTCTL_HOOK_TOOL=tool)
        return subprocess.run(["git", "-C", self.repo, "commit", "-q", "--allow-empty", "-m", "x"], capture_output=True,
                              text=True, env=e, timeout=60)

    def fake_tool(self, rc):
        p = os.path.join(self.repo, f"tool-{rc}")
        with open(p, "w") as f:
            f.write(f"#!/bin/sh\necho red-fixer-9\nexit {rc}\n")
        os.chmod(p, 0o755)
        return p

    def test_a_fixer_is_refused_and_told_to_file_an_issue(self):
        r = self.commit(TOOL, AGENTCTL_RED_FIXER="1")
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("agentctl issue", r.stderr)

    def test_anybody_else_commits(self):
        self.assertEqual(self.commit(TOOL).returncode, 0)

    def test_a_broken_agentctl_never_blocks_the_commit_that_fixes_it(self):
        r = self.commit(self.fake_tool(3))
        self.assertEqual(r.returncode, 0)
        self.assertIn("could not tell", r.stderr)
        self.assertEqual(self.commit(self.fake_tool(0)).returncode, 1)


if __name__ == "__main__":
    unittest.main()
