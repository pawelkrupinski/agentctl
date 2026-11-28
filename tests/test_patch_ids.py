"""ONE PATCH-ID READ (agentctl review 2, A D5): `onmain.patch_ids` is the one `git patch-id --stable` over commits -- a
batch in one git call, a merge or an empty commit having none -- and `onmain.patch_id` one commit of it (the lander's
records, the revert's key, the content twin's walk and the red machine's fixes all read patches through it)."""
import os, sys, unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from test_agentctl import A, sh  # noqa: E402
from test_red import RedBase  # noqa: E402


class PatchIdsTest(RedBase):
    def commits(self):
        self.r.write("f.txt", "one\n")
        first = self.r.commit("f.txt")
        self.r.write("f.txt", "one\ntwo\n")
        second = self.r.commit("f.txt")
        sh(self.r.path, "git", "commit", "-q", "--allow-empty", "-m", "an empty commit")
        empty = sh(self.r.path, "git", "rev-parse", "HEAD")
        return first, second, empty

    def test_a_batch_names_each_commits_patch_and_none_for_an_empty_commit(self):
        first, second, empty = self.commits()
        ids = A.patch_ids(self.r.path, [first, second, empty])
        self.assertEqual(sorted(ids), sorted([first, second]), "an empty commit has no patch")
        self.assertNotEqual(ids[first], ids[second])
        self.assertEqual(A.patch_ids(self.r.path, [first, "0" * 40, second]), {k: ids[k] for k in (first, second)},
                         "a sha git cannot show does not spoil the batch")

    def test_one_commit_is_the_batch_of_one(self):
        first, second, empty = self.commits()
        ids = A.patch_ids(self.r.path, [first, second])
        self.assertEqual(A.patch_id(self.r.path, second), ids[second])
        self.assertIsNone(A.patch_id(self.r.path, empty))
        self.assertEqual(A.patch_ids(self.r.path, []), {})

    def test_the_walk_for_a_commits_twin_on_upstream_reads_the_patches_in_one_git_call(self):
        """equivalent_on read one patch per upstream commit (up to 400 `git patch-id` processes per question)."""
        first, _second, _empty = self.commits()
        main = sh(self.r.path, "git", "rev-parse", "--abbrev-ref", "HEAD")
        sh(self.r.path, "git", "checkout", "-q", "-b", "side", first)
        self.r.write("g.txt", "side\n")
        mine = self.r.commit("g.txt")
        sh(self.r.path, "git", "checkout", "-q", main)
        sh(self.r.path, "git", "cherry-pick", mine)
        twin = sh(self.r.path, "git", "rev-parse", "HEAD")
        for i in range(3):  # newer commits, each read before the twin is found
            self.r.write(f"h{i}.txt", "other\n")
            self.r.commit(f"h{i}.txt")
        calls = []
        real = A.run_cmd
        A.run_cmd = lambda argv, *a, **k: (calls.append(argv), real(argv, *a, **k))[1]
        self.addCleanup(setattr, A, "run_cmd", real)
        self.assertEqual(A.equivalent_on(self.r.path, mine, main), twin)
        self.assertEqual([c for c in calls if "patch-id" in c].__len__(), 2, "the commit's, then upstream's all at once")


if __name__ == "__main__":
    unittest.main()
