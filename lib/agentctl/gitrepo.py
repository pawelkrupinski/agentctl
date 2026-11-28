# agentctl, part `gitrepo`: a clone's commit graph as questions -- THE LEAF over git for "is this commit here" and "does
# this commit contain that one" (commit_known, is_ancestor: core's git_ok, which every test's fake answers). GitRepo is the interface, named and typed as movies'
# scripts/lib/tool_git GitRepo (the subset agentctl asks: resolve, is_ancestor) so the two could be vendored as one;
# SubprocessGitRepo asks git through the runner it is given (one parameter per dependency), InMemoryGitRepo holds commits
# and branches in memory and answers alike: a decision taking a GitRepo needs no temporary repository
# (tests/test_gitrepo.py holds both to one contract). Split off core (2026-11-27).
# A real module (lib/agentctl/__init__.py MODULES): its own imports, the standard library's and MODULES' only.

import hashlib
import re
import subprocess
from typing import Any, Optional, Protocol
from agentctl.procrunner import ProcessRunner
from agentctl.core import git, git_ok


def is_ancestor(repo: str, ancestor: str | None, descendant: str | None) -> bool:
    """Whether `descendant` (a commit or ref) contains `ancestor`: git's own test, False when either is unknown here."""
    return git_ok(repo, "merge-base", "--is-ancestor", str(ancestor), str(descendant))


def commit_known(repo: str, rev: str | None) -> bool:
    """Whether `rev` names a commit this clone holds (ONE existence check: no fetch)."""
    return git_ok(repo, "cat-file", "-e", f"{rev}^{{commit}}")


def commit_files(repo: str, sha: str) -> list[str] | None:
    """The paths commit `sha` changes (a merge's and a root commit's too: `-m --root`), None when this clone cannot read it
    (a commit it lacks: never raises). ONE reader of a commit's files."""
    r = git(repo, "diff-tree", "--no-commit-id", "--name-only", "-r", "--root", "-m", sha, check=False)
    return [p for p in r.stdout.splitlines() if p] if r.returncode == 0 else None


def commit_message(repo: str, sha: str) -> str:
    """Commit `sha`'s whole message ('' when this clone cannot read it): ONE reader of a commit's message."""
    return str(git(repo, "log", "-1", "--format=%B", sha, check=False).stdout)


class GitRepo(Protocol):
    """The commit-graph questions a decision asks of a clone (movies' tool_git.GitRepo, its subset)."""

    def resolve(self, rev: str) -> Optional[str]:
        """The commit `rev` names (a sha, a unique prefix, a branch, HEAD, with `^`, `~N`, `^{commit}`), else None."""
        ...

    def is_ancestor(self, a: str, b: str) -> bool:
        """Whether `b` contains `a` (a commit contains itself); False when either is unknown."""
        ...


class SubprocessGitRepo:
    """The clone at `path`, asked through git by `runner` (the root's ProcessRunner, or a test's fake)."""

    def __init__(self, path: str, runner: ProcessRunner) -> None:
        self.path = path
        self.runner = runner

    def run(self, *args: str) -> "Optional[subprocess.CompletedProcess[Any]]":
        """git -C path args, bounded: None past the bound."""
        try:
            return self.runner.run(["git", "-C", self.path, *args], capture_output=True, text=True, timeout=120)
        except subprocess.TimeoutExpired:
            return None

    def resolve(self, rev: str) -> Optional[str]:
        done = self.run("rev-parse", "--verify", "-q", f"{rev}^{{commit}}")
        return (str(done.stdout).strip() if done else "") or None  # a failing `--verify -q` prints nothing

    def is_ancestor(self, a: str, b: str) -> bool:
        done = self.run("merge-base", "--is-ancestor", a, b)
        return done is not None and done.returncode == 0


REV_SUFFIX = re.compile(r"(\^\{commit\}|\^\d*|~\d*)$")


class InMemoryGitRepo:
    """A commit graph in memory: `commit(*parents)` makes one on the checked-out branch (its head the parent when none
    is named), `branch(name, at)` names one. The boundary only: what git would answer, read from the graph."""

    def __init__(self) -> None:
        self.parents: dict[str, tuple[str, ...]] = {}
        self.branches: dict[str, Optional[str]] = {"main": None}
        self.current = "main"

    def commit(self, *parents: str) -> str:
        head = self.branches[self.current]
        held = tuple(parents) if parents else ((head,) if head else ())
        sha = hashlib.sha1(repr((held, len(self.parents))).encode()).hexdigest()
        self.parents[sha] = held
        self.branches[self.current] = sha
        return sha

    def branch(self, name: str, at: str = "HEAD") -> None:
        self.branches[name] = self.resolve(at)

    def checkout(self, name: str) -> None:
        self.current = name

    def resolve(self, rev: str) -> Optional[str]:
        suffix = REV_SUFFIX.search(rev)
        if suffix and suffix.group(0) != rev:
            base = self.resolve(rev[:suffix.start()])
            return self.step(base, suffix.group(0)) if base else None
        if rev == "HEAD":
            return self.branches[self.current]
        if rev in self.branches:
            return self.branches[rev]
        found = [sha for sha in self.parents if len(rev) >= 4 and sha.startswith(rev)]
        return found[0] if len(found) == 1 else None

    def step(self, sha: str, suffix: str) -> Optional[str]:
        """The commit `suffix` (`^{commit}`, `^N`, `~N`) leads to from `sha`."""
        if suffix == "^{commit}":
            return sha
        count = int(suffix[1:] or 1)
        return self.nth_parent(sha, count) if suffix[0] == "^" else self.first_parents_back(sha, count)

    def nth_parent(self, sha: str, n: int) -> Optional[str]:
        """`sha^n`: its n-th parent (`^0` the commit itself)."""
        parents = self.parents[sha]
        return sha if n == 0 else parents[n - 1] if n <= len(parents) else None

    def first_parents_back(self, sha: str, n: int) -> Optional[str]:
        """`sha~n`: the first parent, n times."""
        at: Optional[str] = sha
        for _ in range(n):
            first = self.parents[at][:1] if at else ()
            at = first[0] if first else None
        return at

    def reachable(self, sha: str) -> set[str]:
        seen: set[str] = set()
        todo = [sha]
        while todo:
            at = todo.pop()
            if at not in seen:
                seen.add(at)
                todo.extend(self.parents[at])
        return seen

    def is_ancestor(self, a: str, b: str) -> bool:
        sha_a, sha_b = self.resolve(a), self.resolve(b)
        return bool(sha_a and sha_b and sha_a in self.reachable(sha_b))
