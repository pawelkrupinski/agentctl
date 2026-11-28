# agentctl, part `landedtwin`: is a commit landed: patch-equivalence or a twin on main, one answer
# A real module (lib/agentctl/__init__.py MODULES): its own imports, the standard library's and MODULES' only.

import functools
import os
from agentctl.core import git, run_cmd


# IS IT LANDED: ONE ANSWER (2026-10-30: movies-wt-identity-impact's 67c68416e escalated UNLANDED ORPHAN three days
# after it landed as 2883073d1 -- same subject, author and author date, the same +/- lines, but main had since changed
# the lines AROUND them, so its patch-id (context included) differed and `git cherry` said "+"). A commit is on main
# when main has a patch-equivalent commit (git cherry's "-") OR a TWIN: the same subject, author and author date with
# identical changed lines per file (context and line numbers left out). Every "is it landed" check asks this: the
# orphans, the queue's entries and change items (W1 with them), the fixers' worktrees, the closers.
def file_change_lines(section: list[str]) -> list[str]:
    """The +/- lines of one file's section of a `git show -U0` text: its header keeps its modes and binary-ness (its
    index line is dropped, its blob id kept for a binary change), its hunks their +/- lines."""
    out, in_hunk, blob = [], False, None
    for line in section:
        if in_hunk:
            if line[:1] in ("+", "-", "\\"):
                out.append(line)
        elif line.startswith("@@"):
            in_hunk = True
        elif line.startswith("index "):
            blob = line.split()[1].split("..")[-1]
        elif line.startswith(("new file mode", "deleted file mode", "old mode", "new mode")):
            out.append(line)
        elif line.startswith(("Binary files", "GIT binary patch")):
            out.append(f"{line} {blob}")
    return out


def parse_changed_lines(diff: str) -> dict[str, list[str]]:
    """{file header: [the +/- lines, in order]} of a `git show -U0` text."""
    sections: dict[str, list[str]] = {}
    cur = None
    for line in diff.splitlines():
        if line.startswith("diff --git "):
            cur = sections[line] = []
        elif cur is not None:
            cur.append(line)
    return {header: file_change_lines(section) for header, section in sections.items()}


class CommitUnread(Exception):
    """git could not show the commit: nothing is kept of it."""


@functools.lru_cache(maxsize=4000)
def _changed_lines_of(repo: str, c: str) -> dict[str, list[str]]:
    """changed_lines of commit c, kept per (clone, commit): a commit's content never changes, so the memo is the
    function's own (a pure memo holds no state a caller reaches). An unreadable commit raises, and is not kept."""
    r = run_cmd(["git", "-C", repo, "show", "--format=", "-U0", "--no-color", "--no-ext-diff", "--no-renames",
                 "--diff-merges=first-parent", c], timeout=120, text=False)
    if r.returncode != 0:
        raise CommitUnread(c)
    return parse_changed_lines(r.stdout.decode(errors="replace"))


def changed_lines(repo: str, c: str) -> dict[str, list[str]] | None:
    """{file header: [the +/- lines, in order]} of commit c against its first parent: no context, no line numbers, no
    blob ids of a text change (they name the whole file, context included); a binary change keeps its new blob id."""
    try:
        return _changed_lines_of(repo, c)
    except CommitUnread:
        return None


def _index_main_log(log: str) -> dict[tuple[str, ...], list[str]]:
    """{(author name, email, author date, subject): [sha]} of a `%H%x00%an%x00%ae%x00%at%x00%s` log."""
    idx: dict[tuple[str, ...], list[str]] = {}
    for line in log.splitlines():
        p = line.split("\x00")
        if len(p) == 5:
            idx.setdefault(tuple(p[1:]), []).append(p[0])
    return idx


def _main_index(repo: str, main: str, since: str) -> dict[tuple[str, ...], list[str]]:
    """{(author name, email, author date, subject): [sha]} of main's commits since `since` (kept per main head)."""
    return _main_index_of(os.path.realpath(repo), main, since)


@functools.lru_cache(maxsize=32)
def _main_index_of(repo: str, main: str, since: str) -> dict[tuple[str, ...], list[str]]:
    """The index of the commits between `since` and `main`, a pure function of those commits: its memo is its own."""
    rng = f"{since}..{main}" if since else main
    r = git(repo, "log", "--no-merges", "-n", "20000", "--format=%H%x00%an%x00%ae%x00%at%x00%s", rng, check=False)
    return _index_main_log(r.stdout if r.returncode == 0 else "")


def content_twin(repo: str, c: str, main: str) -> str | None:
    """main's commit (since its merge-base with c) with c's subject, author and author date and identical changed
    lines, or None."""
    meta = git(repo, "log", "-1", "--format=%an%x00%ae%x00%at%x00%s", c, check=False)
    p = meta.stdout.rstrip("\n").split("\x00") if meta.returncode == 0 else []
    head = git(repo, "rev-parse", "--verify", "-q", f"{main}^{{commit}}", check=False).stdout.strip()
    if len(p) != 4 or not head:
        return None
    mb = git(repo, "merge-base", c, head, check=False).stdout.strip()
    mine = inked_lines(changed_lines(repo, c))
    if mine is None:
        return None
    return next((u for u in _main_index(repo, head, mb).get(tuple(p), [])
                 if u != c and inked_lines(changed_lines(repo, u)) == mine), None)


def inked_lines(changed: dict[str, list[str]] | None) -> dict[str, list[str]] | None:
    """changed_lines without its blank +/- lines (issue 20261104T002213-cc3b4a: a blank line moved in a doc hunk on the
    way to main made a landed commit's twin unrecognised, and its worktree got an orphan fixer); None stays None."""
    if changed is None:
        return None
    return {f: [l for l in lines if not (l[:1] in "+-" and not l[1:].strip())] for f, lines in changed.items()}


def cherry_landed(repo: str, main: str, head: str = "HEAD", *limit: str) -> tuple[list[str], list[str]] | tuple[None, None]:
    """(commits, left): head's commits not in main's history (git cherry's list, oldest first) and those of them NOT
    landed -- neither patch-equivalent on main nor with a content twin there. (None, None) when git cannot tell."""
    ch = git(repo, "cherry", main, head, *limit, check=False)
    if ch.returncode != 0:
        return None, None
    commits, left = [], []
    for line in ch.stdout.splitlines():
        if line[:2] not in ("+ ", "- "):
            continue
        sha = line[2:].strip()
        commits.append(sha)
        if line.startswith("+ ") and not content_twin(repo, sha, main):
            left.append(sha)
    return commits, left
