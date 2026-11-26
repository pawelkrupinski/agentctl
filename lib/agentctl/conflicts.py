# agentctl, part `conflicts`: a change replayed onto main: its commits picked, each conflict resolved (union-safe files, generated files, source hunks) or left to its author
# A real module (lib/agentctl/__init__.py MODULES): its own imports, the standard library's and MODULES' only.

import fnmatch
import os
import subprocess
from typing import Any, Optional
from agentctl.core import NO_SIGN, git
from agentctl.worktrees import WorktreeGuard
from agentctl.patches import deliberate_empty, equivalent_on, is_empty_commit, patch_id, upstream_patch_ids
from agentctl.generated import conflict_stages, generated_entry, merge_file, snapshots_both_changed, unmerged
from agentctl.generatedregen import regenerate


# A DOCS CONFLICT IS NO VERDICT ON A CHANGE (2026-11-04: of the week's 42 conflict rejections, 6 were docs/markdown
# only -- docs/investigations/2026-11-01-flip-unification.md alone rejected five changes, each author appending a
# section at the same end). A pick or rebase whose every conflicted file is prose (UNION_SAFE) is resolved by keeping
# both sides (`git merge-file --union`: the base's lines once, ours then theirs) and goes on; any other conflicted
# file, or a delete/modify conflict, is the author's as before. Generated fixtures (derivation-hash TSVs) are never
# union-merged: both sides' rows would be stale.
UNION_SAFE = ("docs/*", "*.md")


def union_safe(path: str) -> bool:
    return any(fnmatch.fnmatch(path, g) for g in UNION_SAFE)


def union_resolve(wt, files=None):
    """Resolve wt's conflicted files (or `files` of them) by keeping both sides when every one is UNION_SAFE and has
    all three stages: the files resolved (staged), or None (nothing touched)."""
    files = unmerged(wt) if files is None else files
    if not files or not all(union_safe(f) for f in files):
        return None
    stages = conflict_stages(wt, files)
    if stages is None:
        return None
    for f, got in stages.items():
        r = merge_file(got[2], got[1], got[3], "--union")
        if r.returncode < 0 or r.returncode > 127:
            return None
        with open(os.path.join(wt, f), "w") as fh:
            fh.write(r.stdout)
        git(wt, "add", "--", f)
    return files


# A SOURCE CONFLICT IS RESOLVED IN THE BATCH WHEN BOTH SIDES ONLY ADD (the owner, 2026-11-21: "make agentctl batch even
# more, merge / fix conflicts"). Tier 3 of THE BATCH TIERS (queuebatch.py): a real source two queued changes conflict in --
# both appended a test case, both added an import, both added a row to a hand-written Scala table -- is resolved on
# the combined tree, hunk by hunk over `git merge-file --diff3`: a hunk is taken when, past the lines both sides wrote
# the same (their common prefix and suffix, which must account for every base line of the hunk), what is left on each
# side is pure addition -- kept, ours first, then theirs; a side holding the other's lines takes that side. Anything
# else (a line both changed differently, a deletion against an edit) is no resolution. Only in a BACKLOG batch's
# assembly, whose check is the full landing check of exactly the tree that lands: that check is the verification.
# What was resolved is recorded -- an `Auto-Resolved:` trailer on the picked commit, `autoresolved` on the entry (`land
# status` shows it) -- and a member so resolved that its batch then fails is checked alone and held behind the change
# it conflicts with, as before (batch.py: the bisect names the resolution). Never in a lone change's rebase, a push's
# rebase or a verify round.
def merge_diff3(base: str, ours: str, theirs: str) -> "subprocess.CompletedProcess[str]":
    """`git merge-file --diff3` of three texts, each ending in a newline, the sides labelled ours, base and theirs."""
    texts = [x if x.endswith("\n") or not x else x + "\n" for x in (ours, base, theirs)]
    return merge_file(*texts, "--diff3", "-L", "ours", "-L", "base", "-L", "theirs")


class DiffHunks:
    """A diff3 merge read line by line: the text outside the conflicts, and each conflicting hunk taken (hunk_take)."""

    def __init__(self) -> None:
        self.out: list[str] = []
        self.how: list[str] = []
        self.region: Any = None
        self.part: Any = None
        self.lineno = 0

    def add(self, line: str) -> bool:
        """False when the merge cannot be resolved (a marker out of place, a hunk nothing takes)."""
        bare = line.rstrip("\n")
        return self.outside(line, bare) if self.region is None else self.inside(line, bare)

    def outside(self, line: str, bare: str) -> bool:
        if bare == "<<<<<<< ours":
            self.region, self.part = {"o": [], "b": [], "t": []}, "o"
            return True
        if bare in ("||||||| base", "=======", ">>>>>>> theirs"):
            return False
        self.out.append(line)
        self.lineno += 1
        return True

    def inside(self, line: str, bare: str) -> bool:
        if bare == "||||||| base" and self.part == "o":
            self.part = "b"
        elif bare == "=======" and self.part == "b":
            self.part = "t"
        elif bare == ">>>>>>> theirs" and self.part == "t":
            return self.close()
        elif bare.startswith(("<<<<<<< ", "||||||| ", "=======", ">>>>>>> ")):
            return False  # a marker out of place: a file holding marker-like lines of its own
        else:
            self.region[self.part].append(line)
        return True

    def close(self) -> bool:
        took = hunk_take(self.region["o"], self.region["b"], self.region["t"])
        if took is None:
            return False
        lines, what = took
        self.how.append(f"line {self.lineno + 1}: {what}")
        self.out.extend(lines)
        self.lineno += len(lines)
        self.region = None
        return True


def hunks_resolve(base, ours, theirs):
    """(merged text, [what each hunk took]) of a source conflict whose every conflicting hunk resolves (A SOURCE
    CONFLICT IS RESOLVED ...), or None."""
    r = merge_diff3(base, ours, theirs)
    if r.returncode < 0 or r.returncode > 127:
        return None
    walk = DiffHunks()
    if not all(walk.add(line) for line in r.stdout.splitlines(True)) or walk.region is not None:
        return None
    text = "".join(walk.out)
    return (text if ours.endswith("\n") or not ours else text.rstrip("\n")), walk.how


def conflict_file_versions(repo: Any, older: Any, younger: Any, f: str) -> list[Any]:
    """`git show` of file f at the merge base, the older and the younger head; [] when any is missing or git fails."""
    try:
        mb = git(repo, "merge-base", older["head"], younger["head"], check=False).stdout.strip()
        got = [git(repo, "show", f"{x}:{f}", check=False) for x in (mb, older["head"], younger["head"])] if mb else []
    except Exception:  # noqa: BLE001 -- a hint, never a reason the author is not told
        return []
    return [] if any(r.returncode != 0 for r in got) else got


def conflict_hunks(repo: Any, older: Any, younger: Any, files: Any, limit: int = 3) -> list[str]:
    """The first conflicting hunk of each of the files (at most `limit`), in words -- `F line N: <older's line> vs
    <this change's line>` -- for the author told CONFLICTS (THE BATCH TIERS, tier 4); [] when git cannot tell."""
    out = []
    for f in [x for x in files if " (" not in x][:limit]:
        got = conflict_file_versions(repo, older, younger, f)
        if not got:
            continue
        hunk = first_hunk(*(r.stdout for r in got))
        if hunk:
            n, theirs, mine = hunk
            out.append(f"{f} line {n}: {older['id']} has `{theirs[:80]}`, this change `{mine[:80]}`")
    return out


def first_hunk(base: str, ours: str, theirs: str) -> tuple[int, str, str] | None:
    """(line number, ours' first line, theirs' first line) of the first conflicting hunk of a three-way merge (diff3),
    or None when it merges cleanly or git cannot tell."""
    r = merge_diff3(base, ours, theirs)
    lines = r.stdout.splitlines()
    if r.returncode <= 0 or "<<<<<<< ours" not in lines:
        return None
    at = lines.index("<<<<<<< ours")
    mid = lines.index("||||||| base", at)
    sep = lines.index("=======", mid)
    end = lines.index(">>>>>>> theirs", sep)
    o = [x for x in lines[at + 1:mid] if x.strip()]
    t = [x for x in lines[sep + 1:end] if x.strip()]
    return at + 1, (o[0].strip() if o else "(nothing)"), (t[0].strip() if t else "(nothing)")


def _common_edges(o: Any, t: Any) -> Any:
    """(leading, trailing) line counts two sides of a hunk share."""
    n = 0
    while n < min(len(o), len(t)) and o[n] == t[n]:
        n += 1
    m = 0
    while m < min(len(o), len(t)) - n and o[-1 - m] == t[-1 - m]:
        m += 1
    return n, m


def hunk_take(o, b, t):
    """(lines, what) for one diff3 hunk (ours, base, theirs lines) when it resolves, else None."""
    n, m = _common_edges(o, t)
    if n + m < len(b):  # a base line one side changed otherwise than the other: a real conflict
        return None
    mo, mt = o[n:len(o) - m], t[n:len(t) - m]
    if not mo or not mt:
        return o if mo else t, f"took the side holding the other's lines ({len(o)} lines)"
    sep = ""
    if b and n and o[n - 1].rstrip("\n") == b[-1].rstrip("\n") + "," and not mo[-1].rstrip("\n").endswith(","):
        mo = mo[:-1] + [mo[-1].rstrip("\n") + ",\n"]  # both appended to a list whose last item each gave a comma
        sep = ", a comma between"
    return (o[:n] + mo + mt + o[len(o) - m:],
            f"kept both sides' added lines ({len(mo)} ours, then {len(mt)} theirs{sep})")


def source_resolve(wt, files, resolved):
    """Resolve wt's conflicted SOURCE files hunk by hunk (A SOURCE CONFLICT IS RESOLVED ...): True when every one
    resolved (written, staged, each appended to `resolved` as {file, hunks}); False and nothing written otherwise."""
    stages = conflict_stages(wt, files)
    if stages is None:
        return False
    done = {}
    for f in files:
        got = hunks_resolve(stages[f][1], stages[f][2], stages[f][3])
        if got is None:
            return False
        done[f] = got
    for f, (text, how) in done.items():
        with open(os.path.join(wt, f), "w") as fh:
            fh.write(text)
        git(wt, "add", "--", f)
        resolved.append({"file": f, "hunks": how})
    return True


def _conflict_kinds(wt: Any, files: list[str]) -> tuple[list[str], list[str], list[str]]:
    """(generated, union-safe prose, source) files of the conflicted ones."""
    gen = [f for f in files if generated_entry(wt, f)]
    prose = [f for f in files if f not in gen and union_safe(f)]
    source = [f for f in files if f not in gen and f not in prose]
    return gen, prose, source


def _regenerated(wt: Any, gen: list[str], regenerated: Any, provisional: Any, notes: Any) -> bool:
    """The generated files regenerated; False (and (files, why) noted) when that failed."""
    why = regenerate(wt, gen, regenerated, provisional)
    if why and notes is not None:
        notes.append((gen, why))
    return not why


def conflict_resolve(wt, regen=True, notes=None, regenerated=None, provisional=None, resolved=None):
    """Resolve wt's conflicted files when each is UNION_SAFE prose (both sides kept), a registered generated file
    (regenerate; only with regen) or -- with `resolved` a list, a backlog batch's assembly -- a source file whose
    conflicting hunks are additions on both sides (source_resolve): the files resolved (staged), or None (nothing it
    can resolve; (files, why) appended to `notes` when generated files' resolution failed)."""
    files = unmerged(wt)
    if not files:
        return None
    gen, prose, source = _conflict_kinds(wt, files)
    if (source and resolved is None) or (gen and not regen):
        return None
    if source and not source_resolve(wt, source, resolved):
        return None
    if prose and union_resolve(wt, prose) is None:
        return None
    if gen and not _regenerated(wt, gen, regenerated, provisional, notes):
        return None
    return files


def pick(wt, commits, dropped=None, regen=True, regenerated=None, provisional=None, resolved=None):
    """pick_commits under WorktreeGuard (EVERY EXIT PUTS THE WORKTREE BACK, slots.py): no cherry-pick is left in
    progress in the tree, whatever ends it."""
    with WorktreeGuard(wt):
        return pick_commits(wt, commits, dropped, regen, regenerated, provisional, resolved)


class Pick:
    """pick_commits' walk over the commits: one at a time, a method per outcome of a pick."""

    def __init__(self, wt: Any, regen: Any, dropped: Any, regenerated: Any, provisional: Any, resolved: Any) -> None:
        self.wt, self.regen, self.dropped, self.regenerated, self.provisional = wt, regen, dropped, regenerated, provisional
        self.resolved = resolved
        self.start = git(wt, "rev-parse", "HEAD").strip()
        self.on_head: Any = {}

    def run(self, commits: Any) -> Any:
        """None, or the conflicting files (HEAD back where it started)."""
        if commits:
            self.on_head = upstream_patch_ids(self.wt, commits[0], self.start)
        for c in commits:
            failed = self.one(c)
            if failed is not None:
                return failed
        return None

    def drop(self, c: Any, via: Any) -> None:
        if self.dropped is not None:
            self.dropped.append((c, via))

    def one(self, c: Any) -> Any:
        """The conflicting files of picking commit c, None when it was picked or dropped."""
        wt = self.wt
        before = git(wt, "rev-parse", "HEAD").strip()
        via = self.on_head.get(patch_id(wt, c) or "")
        if via and not deliberate_empty(wt, c):
            self.drop(c, via)
            return None
        notes: list[Any] = []
        mine: Any = [] if self.resolved is not None else None
        r = self.cherry_pick(c, notes, mine)
        if r.returncode != 0:
            return self.conflicted(c, before, r, notes)
        failed = self.snapshots(c, before) if self.regen else None  # only what a command here regenerates (regen_runs)
        if failed:
            return failed
        if mine:
            self.annotate(c, mine)
        self.drop_if_empty(c, before)
        return None

    def cherry_pick(self, c: Any, notes: Any, mine: Any) -> Any:
        """The cherry-pick of c, its conflicts resolved where they can be (conflict_resolve) and the pick continued."""
        wt = self.wt
        r = git(wt, *NO_SIGN, "cherry-pick", "--allow-empty", "--keep-redundant-commits", c, check=False)
        if r.returncode != 0 and conflict_resolve(wt, self.regen, notes, self.regenerated, self.provisional, mine):
            r = git(wt, *NO_SIGN, "-c", "core.editor=true", "cherry-pick", "--continue", check=False)
        return r

    def drop_if_empty(self, c: Any, before: Any) -> None:
        """A commit empty once picked (its change already there) dropped, HEAD back, unless it was meant empty."""
        wt = self.wt
        if is_empty_commit(wt, "HEAD") and not deliberate_empty(wt, c):
            git(wt, "reset", "--hard", "-q", before)
            if self.dropped is not None:
                self.dropped.append((c, equivalent_on(wt, c, before)))

    def snapshots(self, c: Any, before: Any) -> Any:
        """A SNAPSHOT both sides changed, regenerated though git merged it cleanly (SNAPSHOTS ARE REGENERATED, NEVER
        MERGED), the commit amended with it; the failure to name, with HEAD back, when it cannot be."""
        wt = self.wt
        sides = snapshots_both_changed(wt, c, before)
        why = regenerate(wt, sorted(sides), self.regenerated, self.provisional, sides=sides) if sides else None
        if why:
            git(wt, "reset", "--hard", "-q", self.start)
            return [f"{', '.join(sorted(sides))} (generated: {why})"]
        if sides and git(wt, "diff", "--cached", "--quiet", check=False).returncode != 0:
            git(wt, *NO_SIGN, "commit", "-q", "--amend", "--no-edit", "--allow-empty", check=False)
        return None

    def annotate(self, c: Any, mine: Any) -> None:
        """The commit's source conflicts resolved: named by an `Auto-Resolved:` trailer, recorded in `resolved`."""
        trailers = [x for m in mine for x in ("--trailer", f"Auto-Resolved: {m['file']}: {'; '.join(m['hunks'])}")]
        git(self.wt, *NO_SIGN, "commit", "-q", "--amend", "--no-edit", "--allow-empty", *trailers, check=False)
        self.resolved.extend({"commit": c, **m} for m in mine)

    def conflicted(self, c: Any, before: Any, r: Any, notes: Any) -> Any:
        """A pick that failed: None when its patch is already there (dropped), else the conflicting files, HEAD back."""
        wt = self.wt
        files = unmerged(wt)
        git(wt, "cherry-pick", "--abort", check=False)
        via = equivalent_on(wt, c, before) if files else None
        if via:  # its patch is already there, and a later change to the same lines conflicts with it again
            git(wt, "reset", "--hard", "-q", before)
            self.drop(c, via)
            return None
        git(wt, "reset", "--hard", "-q", self.start)
        noted = {f for gen, _why in notes for f in gen}
        return ([f for f in files if f not in noted] + [f"{', '.join(gen)} (generated: {why})" for gen, why in notes]
                if files else [r.stderr.strip()[:200]])


def pick_commits(wt, commits, dropped=None, regen=True, regenerated=None, provisional=None, resolved=None):
    """Cherry-pick commits onto wt's HEAD, one at a time: None, or the conflicting files (the pick aborted, HEAD back
    where it was). A commit already in HEAD's history (patch-equivalent), or empty once picked, is DROPPED, never kept
    as an empty commit: appended to `dropped` as (commit, the commit of HEAD carrying it, or None). A deliberate empty
    commit (an Allow-Empty: trailer) is kept.

    PATCH-EQUIVALENT COMMITS ARE DROPPED BEFORE THE PICK, not only when the pick comes out empty: a change whose patch
    main carries already, but whose lines main has since changed back (a revert), applies cleanly and non-empty --
    and landed the same change a second time (simplification-2026-10-31 item 10: three subjects landed 2-3 times).

    A conflict only in prose or registered generated files is resolved and the pick goes on (conflict_resolve; regen
    False: generated files left conflicting); a generated file's failed resolution is named, with why, in the files.
    With `resolved` a list (a backlog batch's assembly), a source conflict of additions on both sides is resolved too,
    each appended as {commit, file, hunks} and named on the picked commit by an `Auto-Resolved:` trailer. With regen,
    a SNAPSHOT both sides changed is regenerated even when git merged it cleanly (SNAPSHOTS ARE REGENERATED, NEVER
    MERGED), the commit amended with it."""
    return Pick(wt, regen, dropped, regenerated, provisional, resolved).run(commits)


# A BOUNCE NAMES WHAT CAUSED IT (2026-11-19: lane F2b's `no longer applies on origin/main 2cfa0ee95e` named neither
# file nor commit; its agent rebased blind). Only a real conflict reaches the agent (generated files are resolved in
# the check tree, waiting.py GENERATED FILES ARE NO OVERLAP), and its verdict names each conflicting file with the
# commit of main that last changed it since the change's base; the files are kept on the entry (`conflict_files`), so
# the change queued again from the same source takes its old place (server.py queue_change, A RESOLVED CONFLICT
# KEEPS ITS PLACE).
def conflict_paths(conflicts):
    """The file paths of a pick's conflict list (a generated file's entry carries its resolution's why)."""
    return [c.split(" (generated: ", 1)[0] for c in conflicts or []]


def _main_change(repo: Any, since: Any, main: Any, path: Any) -> Any:
    """`<sha> <subject>` of path's newest commit in since..main ('' when unknown)."""
    if not (since and main):
        return ""
    return git(repo, "log", "-1", "--format=%h %s", "--abbrev=10", f"{since}..{main}", "--", path,
               check=False).stdout.strip()


def conflict_cause(repo, since, main, conflicts, n=3):
    """`; it conflicts in F (changed on main by <sha> <subject>), ...` for the conflict list, each file's newest
    commit in since..main; '' when none is known."""
    out = []
    for c in (conflicts or [])[:n]:
        path = c.split(" (generated: ", 1)[0]
        line = _main_change(repo, since, main, path)
        out.append(f"{c} (changed on main by {line[:90]})" if line else c)
    more = len(conflicts or []) - n
    return f"; it conflicts in {', '.join(out)}" + (f" (+{more} more)" if more > 0 else "") if out else ""


# A RESOLVED CONFLICT KEEPS ITS PLACE (2026-11-19: lane F2b, bounced for a conflict main had made, rebased and queued
# again at the back, behind every change queued while it was out): a change queued from the same worktree and branch
# within CONFLICT_PLACE_KEPT s of its last entry's rejection for a conflict (`conflict_files`: no longer applies on
# main) takes that entry's seq -- the queue's age order -- instead of a new one. The rejected entry is final, so no two
# live entries share a seq. Tests: tests/test_generated.py.
CONFLICT_PLACE_KEPT = 24 * 3600


# A CHANGE RE-APPLIED ONTO MAIN IS JUDGED BY ITS OWN COMMITS (2026-11-23: dispatch-py and title-trace-py, stacked on
# kick-convergence's branch, were refused `no longer applies` the moment kick landed: their heads and main's merged
# with an add/add conflict in kick_convergence.py -- kick's commits, on main under new shas -- while their own commits,
# picked onto main as the check's rebase picks them, conflicted in docs/tools.md alone, which it regenerates). A
# merge of the two heads sees every commit since their merge base; the rebase drops the change's commits main carries
# (patch-equivalent, pick_commits) and resolves what conflict_resolve resolves. So the pre-check does as the rebase:
# the change's commits main lacks, each merged onto the last (git merge-tree, no worktree), and only a conflict in a
# file the rebase leaves to its author (source: no registered generated file, no prose) refuses.
def own_commits_conflict(repo: str, onto: str, commits: list[str]) -> Optional[list[str]]:
    """The files `commits` (oldest first) conflict in when picked onto `onto` as pick_commits picks them -- each one
    main carries already (patch-equivalent) dropped, a generated or prose file's conflict left to the rebase's
    resolution -- or None: they apply. None too when git cannot tell (an old git, a commit missing): the rebase says."""
    on_main = upstream_patch_ids(repo, commits[0], onto) if commits else {}
    tree: Optional[str] = onto
    for c in [c for c in commits if not on_main.get(patch_id(repo, c) or "") or deliberate_empty(repo, c)]:
        tree, left = tree_pick(repo, tree or onto, c)
        if tree is None:
            return left or None
    return None


def tree_pick(repo: str, tree: str, c: str) -> tuple[Optional[str], list[str]]:
    """Commit c merged onto `tree` as a cherry-pick merges it (git merge-tree over c's parent, no worktree): (the
    tree it makes, []), (None, the files only its author resolves -- source, neither generated nor prose), or (None,
    []) when git cannot tell."""
    r = git(repo, "merge-tree", "--write-tree", "--name-only", "--no-messages", f"--merge-base={c}^", tree, c,
            check=False)
    if r.returncode not in (0, 1):
        return None, []
    lines = r.stdout.splitlines()
    left = [f for f in lines[1:] if f.strip() and not generated_entry(repo, f) and not union_safe(f)]
    return (None, left) if r.returncode == 1 and left else (lines[0].strip(), [])
