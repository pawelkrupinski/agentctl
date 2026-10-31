"""The forge (lib/agentctl/forge.py): GitHub, through the real gh and `gh api`, against tests/fake_gh.py (GitHub's CLI
and REST shapes)."""
import contextlib, io, json, os, sys, tempfile, unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from test_agentctl import A  # noqa: E402

FAKE_GH = os.path.join(HERE, "fake_gh.py")
O_R = "pawelkrupinski/movies"


class GithubForgeTest(unittest.TestCase):
    """`serve_*` put one fact in GitHub's own shape."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.d = os.path.join(self.tmp.name, "gh")
        os.makedirs(self.d)
        self.env = {"FAKE_GH_DIR": self.d, "GH_TOKEN": "dummy",
                    "AGENTCTL_SCRATCH": os.path.join(self.tmp.name, "scratch"),
                    "AGENTCTL_DISPATCH_DIR": os.path.join(self.tmp.name, "dispatches"),
                    "AGENTCTL_DISPATCH_FIND": "1", "AGENTCTL_DISPATCH_FIND_POLL": "0"}
        self.saved_env = {k: os.environ.get(k) for k in self.env}
        os.environ.update(self.env)
        self.saved_gh, A.GH_CLI = A.GH_CLI, FAKE_GH

    def tearDown(self):
        A.GH_CLI = self.saved_gh
        for k, v in self.saved_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        self.tmp.cleanup()

    # -- the fakes' data
    def put(self, name, obj):
        with open(os.path.join(self.d, name), "w") as f:
            f.write(obj if isinstance(obj, str) else json.dumps(obj))

    def calls(self):
        p = os.path.join(self.d, "calls.log")
        return [l.split(" ", 1)[1] for l in open(p).read().splitlines()] if os.path.exists(p) else []

    def gh_api(self, path, obj):
        import re
        self.put("api-" + re.sub(r"[/?&=]", "_", path) + ".json", obj)

    def tool(self, *argv):
        out, errb = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(errb):
            rc = A.main(list(argv))
        return rc, out.getvalue(), errb.getvalue()

    # -- the scenarios
    def test_run_list_goes_through_this_forges_cli(self):
        self.put("runs.json", [{"databaseId": 9, "number": 3, "status": "completed", "conclusion": "success",
                                "headBranch": "main", "workflowName": "Main"}])
        runs = A.gh_json(O_R, "run", "list", "--branch", "main", "--limit", "5", "--json", A.RUN_FIELDS)
        self.assertEqual([r["databaseId"] for r in runs], [9])
        self.assertEqual(self.calls(), [f"run list --branch main --limit 5 --json {A.RUN_FIELDS} -R {O_R}"])

    def test_failed_log_and_takes_artifacts(self):
        self.put("run-5.log", "takes (pl)\tRun\t2026-10-30T10:00:00.0Z *** FAILED *** boom\n")
        self.assertIn("boom", A.relevant_lines(A.fetch_failed_log(O_R, 5), 5)["takes (pl)"][0])
        self.gh_api(f"repos/{O_R}/actions/runs/5/artifacts", {"artifacts": [{"name": "identity-takes-pl"}, {"name": "x"}]})
        self.put("art-5-identity-takes-pl.txt", "OK 12 takes")
        self.assertEqual(A.fetch_takes_verdicts(O_R, 5), {"identity-takes-pl": "OK 12 takes"})
        self.assertIn(f"run download 5 -n identity-takes-pl", " ".join(self.calls()))

    def test_cancel(self):
        A.cancel_run(O_R, 77, "superseded")
        self.assertIn(f"run cancel 77 -R {O_R}", self.calls())

    def test_dispatch_run_is_recorded_with_its_run_and_inputs(self):
        self.put("runs.json", [{"databaseId": 41, "number": 7, "status": "queued", "conclusion": "", "headBranch": "measure/x",
                                "workflowName": "conv", "workflowFile": "conv.yml", "createdAt": "2099-01-01T00:00:00Z"}])
        rc, out, _ = self.tool("dispatch", "run", "conv.yml", "--ref", "measure/x", "-f", "runs=3", "--repo", O_R,
                               "--purpose", "t")
        self.assertEqual(rc, 0, out)
        self.assertIn("run 41", out)
        self.assertIn(f"workflow run conv.yml --ref measure/x -f runs=3 -R {O_R}", self.calls())
        self.assertEqual(A.read_record(A.dispatches_dir("inputs", "41.json")), {"runs": "3"})

    def test_measure_branches_read_as_name_sha_time(self):
        self.serve_branches([("main", "a" * 40, "2026-10-30T09:00:00Z"), ("measure/p/1-x", "b" * 40, "2026-10-30T10:00:00Z")])
        got = A.forge_branches(O_R, "measure/")
        self.assertEqual([(n, s, w.isoformat()) for n, s, w in got], [("measure/p/1-x", "b" * 40, "2026-10-30T10:00:00+00:00")])

    def test_actions_runs_read_as_run_records(self):
        self.serve_runs([dict(id=101, number=12, sha="c" * 40, branch="check/7", done=True, ok=False),
                         dict(id=100, number=11, sha="d" * 40, branch="main", done=False, ok=None)])
        runs = A.forge_actions_runs(O_R, workflow="precommit-check.yml")
        self.assertEqual([(r["id"], str(r["run_number"]), r["head_sha"], r["head_branch"], r["status"]) for r in runs],
                         [(101, "12", "c" * 40, "check/7", "failure"), (100, "11", "d" * 40, "main", "running")])
        self.assertEqual(A.run_id_of_number(O_R, 12), 101)

    def test_remote_poll_finds_and_decides_the_check_run(self):
        self.serve_runs([dict(id=101, number=12, sha="c" * 40, branch="check/7", done=True, ok=True)])
        rem = A.remote_poll(O_R, {"tip": "c" * 40, "branch": "check/7"})
        self.assertEqual((rem["run"], rem["status"]), (101, "success"))

    def test_run_url(self):
        url = A.forge_run_url(O_R, 12, {"databaseId": 101})
        self.assertEqual(url, f"https://github.com/{O_R}/actions/runs/101")


    def serve_branches(self, bs):
        self.gh_api(f"repos/{O_R}/branches?per_page=100&page=1", [{"name": n, "commit": {"sha": s}} for n, s, _ in bs])
        for _n, s, t in bs:
            self.gh_api(f"repos/{O_R}/commits/{s}", {"sha": s, "commit": {"committer": {"date": t}}})

    def serve_runs(self, runs):
        rs = [{"id": r["id"], "run_number": r["number"], "head_sha": r["sha"], "head_branch": r["branch"],
               "status": "completed" if r["done"] else "in_progress",
               "conclusion": ("success" if r["ok"] else "failure") if r["done"] else None,
               "html_url": f"https://github.com/{O_R}/actions/runs/{r['id']}",
               "path": ".github/workflows/precommit-check.yml"} for r in runs]
        self.gh_api(f"repos/{O_R}/actions/workflows/precommit-check.yml/runs?per_page=50", {"workflow_runs": rs})
        self.gh_api(f"repos/{O_R}/actions/runs?per_page=100", {"workflow_runs": rs})
        for r in rs:
            self.gh_api(f"repos/{O_R}/actions/runs/{r['id']}", r)

    def test_gh_api_takes_no_repo_flag(self):
        self.gh_api(f"repos/{O_R}/git/commits/abc", {"message": "m"})
        self.assertEqual(A.gh_json(O_R, "api", f"repos/{O_R}/git/commits/abc"), {"message": "m"})
        self.assertEqual(self.calls(), [f"api repos/{O_R}/git/commits/abc"])

    def test_a_missing_branch_is_a_404(self):
        self.assertFalse(A.branch_exists(O_R, "measure/gone"))

    def test_github_status_folds_into_one_word(self):
        f = A.github_run_status
        self.assertEqual([f("queued", None), f("in_progress", None), f("completed", "success"), f("completed", "timed_out"),
                          f("completed", "cancelled"), f("completed", "skipped"), f("completed", "startup_failure")],
                         ["waiting", "running", "success", "failure", "cancelled", "skipped", "failure"])

    def test_branch_delete_deletes_the_ref_after_reading_its_sha(self):
        self.gh_api(f"repos/{O_R}/branches/measure%2Fa%2Fb", {"name": "measure/a/b", "commit": {"sha": "f" * 40}})
        A.delete_remote_branch(None, O_R, "measure/a/b", "f" * 40)
        self.assertIn(f"api -X DELETE -H Accept: application/vnd.github+json repos/{O_R}/git/refs/heads/measure/a/b",
                      self.calls())
        with self.assertRaises(RuntimeError):
            A.delete_remote_branch(None, O_R, "measure/a/b", "0" * 40)

    def test_a_verify_dispatch_posts_the_body_through_gh_api(self):
        A.api(O_R, "POST", "actions/workflows/conv.yml/dispatches", body={"ref": "main", "inputs": {"runs": "2"}})
        calls = self.calls()
        self.assertIn(f"api -X POST -H Accept: application/vnd.github+json repos/{O_R}/actions/workflows/conv.yml/dispatches"
                      " --input -", calls)
        self.assertEqual(json.loads(calls[-1][len("BODY "):]), {"ref": "main", "inputs": {"runs": "2"}})

    def test_inputs_come_from_agentctls_own_dispatch_record(self):
        A.write_json(A.dispatches_dir("inputs", "9.json"), {"runs": 2})
        self.assertEqual(A.run_dispatch_inputs(O_R, 9), {"runs": "2"})
        self.assertEqual(A.run_dispatch_inputs(O_R, 10), {})
        self.assertEqual(self.calls(), [])

    def test_release_pins_download_through_gh(self):
        self.gh_api(f"repos/{O_R}/releases/tags/fixtures", {"assets": [{"name": "hermetic-pl.txt"}, {"name": "x.bin"}]})
        self.put("release-fixtures-hermetic-pl.txt", "4242 recorded\n")
        self.assertEqual(A.read_pins(O_R, "fixtures"), {"pl": "4242"})
        self.assertIn(f"release download fixtures -p hermetic-pl.txt -O - -R {O_R}", self.calls())

    def test_no_token_of_agentctl_s_own_for_gh(self):
        os.environ.pop("GH_TOKEN")
        self.assertEqual(A.token_env(O_R, {}), {})
        self.assertNotIn("GH_TOKEN", A.forge_env(O_R, {}))


if __name__ == "__main__":
    unittest.main()
