"""THE GIT LEAF'S ONE CONTRACT (lib/agentctl/gitrepo.py; movies' scripts/lib/tool_git_contract_test.py model): the same
assertions over SubprocessGitRepo (a real temporary repository) and InMemoryGitRepo (the graph in memory), so a
decision taking a GitRepo is unit-tested on the fake and means the same on a clone. The module functions core's
callers kept (git_ok, is_ancestor, commit_known) are held to the same graph on the real side."""
import os
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from test_agentctl import A  # noqa: E402,F401 (the harness: HOME, PATH, git config; puts lib on the path)
from agentctl import gitrepo  # noqa: E402

UNKNOWN = "0" * 40


class RealGraph:
    """InMemoryGitRepo's building calls (commit, branch, checkout) made on a real repository, read through
    SubprocessGitRepo."""

    def __init__(self, path: str) -> None:
        self.path = path
        self.git("init", "-q", "-b", "main")
        self.empty_tree = self.git("hash-object", "-t", "tree", "-w", "/dev/null")
        self.repo = gitrepo.SubprocessGitRepo(path, A.ProcessRunner())
        self.made = 0

    def git(self, *args: str) -> str:
        return subprocess.run(["git", "-C", self.path, "-c", "commit.gpgsign=false", *args], check=True,
                              capture_output=True, text=True).stdout.strip()

    def commit(self, *parents: str) -> str:
        head = self.repo.resolve("HEAD")
        held = parents or ((head,) if head else ())
        self.made += 1
        sha = self.git("commit-tree", self.empty_tree, *(f for p in held for f in ("-p", p)), "-m", f"c{self.made}")
        self.git("update-ref", "HEAD", sha)
        return sha

    def branch(self, name: str, at: str = "HEAD") -> None:
        self.git("branch", "-f", name, at)

    def checkout(self, name: str) -> None:
        self.git("symbolic-ref", "HEAD", f"refs/heads/{name}")


class Contract:
    """The assertions both implementations meet; `graph()` gives (builder, its GitRepo)."""

    def graph(self):
        raise NotImplementedError

    def test_an_empty_repository_resolves_nothing(self):
        _built, repo = self.graph()
        for rev in ("HEAD", "main", UNKNOWN, "nope"):
            self.assertIsNone(repo.resolve(rev), rev)

    def built(self):
        """a - b - m on main, c on side off a, m merging b and c."""
        g, repo = self.graph()
        a = g.commit()
        b = g.commit()
        g.branch("side", a)
        g.checkout("side")
        c = g.commit()
        g.checkout("main")
        m = g.commit(b, c)
        return repo, a, b, c, m

    def test_names_and_steps_resolve_as_git_reads_them(self):
        repo, a, b, c, m = self.built()
        cases = {a: a, a[:10]: a, "main": m, "HEAD": m, "side": c, "main^": b, "main^1": b, "main^2": c, "main^3": None,
                 "main~2": a, "main~3": None, "main^0": m, "main^{commit}": m, f"{b}^{{commit}}": b, "nope": None,
                 UNKNOWN: None}
        for rev, want in cases.items():
            self.assertEqual(want, repo.resolve(rev), rev)

    def test_a_commit_contains_itself_and_what_its_parents_reach(self):
        repo, a, b, c, m = self.built()
        for x, y in ((a, m), (c, m), (b, m), (a, a), (a, "side"), ("side", "main")):
            self.assertTrue(repo.is_ancestor(x, y), (x, y))
        for x, y in ((m, a), (b, c), (c, b), ("main", "side"), (UNKNOWN, m), (a, UNKNOWN), ("nope", "main")):
            self.assertFalse(repo.is_ancestor(x, y), (x, y))


class SubprocessGitRepoTest(Contract, unittest.TestCase):
    def graph(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        g = RealGraph(tmp.name)
        return g, g.repo

    def test_the_module_functions_ask_the_same_clone(self):
        repo, a, b, c, m = self.built()
        path = repo.path
        self.assertTrue(gitrepo.commit_known(path, a))
        self.assertFalse(gitrepo.commit_known(path, UNKNOWN))
        self.assertFalse(gitrepo.commit_known(path, None))
        self.assertTrue(gitrepo.is_ancestor(path, c, m))
        self.assertFalse(gitrepo.is_ancestor(path, m, c))
        self.assertFalse(gitrepo.is_ancestor(path, None, m))
        self.assertTrue(gitrepo.git_ok(path, "rev-parse", "--verify", "-q", "side"))
        self.assertFalse(gitrepo.git_ok(path, "rev-parse", "--verify", "-q", "nope"))


class InMemoryGitRepoTest(Contract, unittest.TestCase):
    def graph(self):
        repo = gitrepo.InMemoryGitRepo()
        return repo, repo


if __name__ == "__main__":
    unittest.main()
