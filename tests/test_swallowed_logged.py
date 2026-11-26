"""A swallowed error says so (the review of 2026-11-26: ~30 silent `except: pass` sites): each broad handler that used to
pass now tells what it skipped on stderr, and the narrow ones that answered None for an unreadable git do too."""
import contextlib, io, os, subprocess, sys, unittest, unittest.mock

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from test_agentctl import A  # noqa: E402


def refuse(message):
    def raiser(*_a, **_k):
        raise SystemExit(message)
    return raiser


class SwallowedErrorsAreToldTest(unittest.TestCase):
    def patched(self, **fakes):
        saved = {k: getattr(A, k) for k in fakes}
        for k, v in fakes.items():
            setattr(A, k, v)
        self.addCleanup(lambda: [setattr(A, k, v) for k, v in saved.items()])

    def told(self, call):
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            result = call()
        return result, err.getvalue()

    def test_a_repo_root_that_cannot_be_found_is_told_and_the_others_still_count(self):
        self.patched(scan_repo_for=refuse("no clone"), repo_path_for=lambda o_r: "/a/root", movies_repo_path=lambda: "/b/root")
        found, said = self.told(lambda: A.root_checkout("/a/root", "o/r"))
        self.assertTrue(found)
        self.assertIn("red claim: a repo root could not be found (no clone)", said)

    def test_an_unreadable_hook_answer_is_told(self):
        class Broken:
            def run(self, *_a, **_k):
                raise subprocess.TimeoutExpired("hook", 120)
        self.patched(process_runner=lambda: Broken(), tool_env=lambda: {})
        found, said = self.told(lambda: A.hook_query("/h", "/tree", "--plan", ["a"]))
        self.assertIsNone(found)
        self.assertIn("hook --plan: unreadable", said)

    def test_a_repo_whose_slug_cannot_be_read_is_told_and_skipped(self):
        self.patched(REPOS=["/r1"], server_repo=lambda: "/r1", slug=refuse("no remote"), git_ok=lambda *a, **k: True, remote=lambda: "origin")
        with unittest.mock.patch("os.path.isdir", return_value=True):
            found, said = self.told(lambda: A.repo_path_for("o/r"))
        self.assertIsNone(found)
        self.assertIn("repo lookup: no slug for /r1 (no remote)", said)


if __name__ == "__main__":
    unittest.main()
