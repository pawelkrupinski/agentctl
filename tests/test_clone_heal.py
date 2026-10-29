"""The reconciler's movies clone heals itself (2026-10-29: a clone cut off by a crash loop left a .git with an
incomplete object store that passed for a clone; every landing check's fetch of main failed for hours, re-queued
silently). The clone is atomic (a temp directory, verified, renamed into place), a broken clone is re-cloned and
swapped in under the clone lock, and a change re-queued for the same infra reason opens a condition."""
import os, subprocess, sys, tempfile, unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from test_agentctl import A  # noqa: E402

GIT_ID = ["-c", "user.email=t@t", "-c", "user.name=t"]


def make_source(root):
    """A source repository (the Forgejo stand-in) with main at two commits. Returns its path."""
    src = os.path.join(root, "src")
    subprocess.run(["git", "init", "-q", "-b", "main", src], check=True)
    for i in (1, 2):
        os.makedirs(os.path.join(src, "d"), exist_ok=True)
        with open(os.path.join(src, "d", f"f{i}"), "w") as f:
            f.write(f"{i}\n")
        subprocess.run(["git", "-C", src, "add", "."], check=True)
        subprocess.run(["git", "-C", src, *GIT_ID, "commit", "-qm", f"c{i}"], check=True)
    return src


class AtomicCloneTest(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp()
        self.src = make_source(self.root)
        self.repo = os.path.join(self.root, "var", "movies")
        self.saved_env = dict(os.environ)
        os.environ["AGENTCTL_MOVIES_REMOTE"] = self.src

    def tearDown(self):
        os.environ.clear()
        os.environ.update(self.saved_env)

    def test_a_clone_lands_verified_and_leaves_no_temp_directory(self):
        self.assertEqual(A.ensure_server_clone(self.repo), self.repo)
        self.assertTrue(A.clone_verify(self.repo)[0])
        self.assertEqual(A.clone_tmp_dirs(self.repo), [])

    def test_an_interrupted_clone_never_leaves_a_target_that_looks_complete(self):
        real_run = A.subprocess.run

        def cut_off(cmd, *a, **k):  # the clone writes part of a .git, then the process is cut off
            if cmd[:2] == ["git", "clone"]:
                os.makedirs(os.path.join(cmd[-1], ".git", "objects"), exist_ok=True)
                open(os.path.join(cmd[-1], ".git", "shallow_abc123"), "w").close()
                raise A.subprocess.TimeoutExpired(cmd, 1800)
            return real_run(cmd, *a, **k)
        A.subprocess.run = cut_off
        try:
            with self.assertRaises(SystemExit):
                A.ensure_server_clone(self.repo)
        finally:
            A.subprocess.run = real_run
        self.assertFalse(os.path.exists(self.repo))
        self.assertEqual(A.clone_tmp_dirs(self.repo), [])
        A.ensure_server_clone(self.repo)  # the next start clones afresh
        self.assertTrue(A.clone_verify(self.repo)[0])

    def test_a_clone_that_does_not_verify_is_never_renamed_into_place(self):
        real_run = A.subprocess.run

        def no_refs(cmd, *a, **k):  # git said 0 but the clone has no forgejo/main
            if cmd[:2] == ["git", "clone"]:
                real_run(["git", "init", "-q", cmd[-1]], check=True)
                return A.subprocess.CompletedProcess(cmd, 0, "", "")
            return real_run(cmd, *a, **k)
        A.subprocess.run = no_refs
        try:
            with self.assertRaises(SystemExit) as cm:
                A.ensure_server_clone(self.repo)
        finally:
            A.subprocess.run = real_run
        self.assertIn("does not verify", str(cm.exception))
        self.assertFalse(os.path.exists(self.repo))
        self.assertEqual(A.clone_tmp_dirs(self.repo), [])

    def test_a_killed_clones_temp_directory_is_removed_at_the_next_start(self):
        os.makedirs(self.repo)
        os.rmdir(self.repo)
        stale = os.path.join(os.path.dirname(self.repo), f".movies{A.CLONE_TMP}999999999-1")
        os.makedirs(os.path.join(stale, ".git"))
        A.ensure_server_clone(self.repo)
        self.assertFalse(os.path.exists(stale))
        self.assertTrue(A.clone_verify(self.repo)[0])

    def test_a_missing_blob_of_mains_tree_fails_the_verification(self):
        A.ensure_server_clone(self.repo)
        blob = A.git(self.repo, "rev-parse", f"{A.REMOTE}/main:d/f2").strip()
        os.unlink(os.path.join(self.repo, ".git", "objects", blob[:2], blob[2:]))
        ok, detail = A.clone_verify(self.repo)
        self.assertFalse(ok)
        self.assertIn("missing", detail)


if __name__ == "__main__":
    unittest.main()
