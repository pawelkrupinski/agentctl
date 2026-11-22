# agentctl, part `conflicts`: a change replayed onto main: its commits picked, each conflict resolved (union-safe files, generated files, source hunks) or left to its author
# A real module (lib/agentctl/__init__.py MODULES): its own imports, the standard library's and MODULES' only.

import fnmatch
import os
import subprocess
from agentctl.core import NO_SIGN, git
from agentctl.worktrees import WorktreeGuard
from agentctl.patches import deliberate_empty, equivalent_on, is_empty_commit, patch_id, upstream_patch_ids
from agentctl.generated import (
    conflict_stages, generated_entry, regen_here, regenerate, snapshots_both_changed, unmerged)


# A DOCS CONFLICT IS NO VERDICT ON A CHANGE (2026-11-04: of the week's 42 conflict rejections, 6 were docs/markdown
# only -- docs/investigations/2026-11-01-flip-unification.md alone rejected five changes, each author appending a
# section at the same end). A pick or rebase whose every conflicted file is prose (UNION_SAFE) is resolved by keeping
# both sides (`git merge-file --union`: the base's lines once, ours then theirs) and goes on; any other conflicted
# file, or a delete/modify conflict, is the author's as before. Generated fixtures (derivation-hash TSVs) are never
# union-merged: both sides' rows would be stale.
UNION_SAFE = ("docs/*", "*.md")


def union_safe(path):
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
    import tempfile
    for f, got in stages.items():
        with tempfile.TemporaryDirectory() as td:
            paths = []
            for n in (2, 1, 3):  # merge-file <current> <base> <other>
                q = os.path.join(td, str(n))
                with open(q, "w") as fh:
                    fh.write(got[n])
                paths.append(q)
            r = subprocess.run(["git", "merge-file", "-p", "--union", *paths], capture_output=True, text=True)
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
def hunks_resolve(base, ours, theirs):
    """(merged text, [what each hunk took]) of a source conflict whose every conflicting hunk resolves (A SOURCE
    CONFLICT IS RESOLVED ...), or None."""
    import tempfile
    texts = [x if x.endswith("\n") or not x else x + "\n" for x in (ours, base, theirs)]
    with tempfile.TemporaryDirectory() as td:
        paths = []
        for n, x in zip(("ours", "base", "theirs"), texts):
            q = os.path.join(td, n)
            with open(q, "w") as fh:
                fh.write(x)
            paths.append(q)
        r = subprocess.run(["git", "merge-file", "-p", "--diff3", "-L", "ours", "-L", "base", "-L", "theirs", *paths],
                           capture_output=True, text=True)
    if r.returncode < 0 or r.returncode > 127:
        return None
    out, how, region, part, lineno = [], [], None, None, 0
    for line in r.stdout.splitlines(True):
        bare = line.rstrip("\n")
        if region is None:
            if bare == "<<<<<<< ours":
                region, part = {"o": [], "b": [], "t": []}, "o"
                continue
            if bare in ("||||||| base", "=======", ">>>>>>> theirs"):
                return None
            out.append(line)
            lineno += 1
            continue
        if bare == "||||||| base" and part == "o":
            part = "b"
        elif bare == "=======" and part == "b":
            part = "t"
        elif bare == ">>>>>>> theirs" and part == "t":
            took = hunk_take(region["o"], region["b"], region["t"])
            if took is None:
                return None
            lines, what = took
            how.append(f"line {lineno + 1}: {what}")
            out.extend(lines)
            lineno += len(lines)
            region = None
        elif bare.startswith(("<<<<<<< ", "||||||| ", "=======", ">>>>>>> ")):
            return None  # a marker out of place: a file holding marker-like lines of its own
        else:
            region[part].append(line)
    if region is not None:
        return None
    text = "".join(out)
    return (text if ours.endswith("\n") or not ours else text.rstrip("\n")), how


def first_hunk(base, ours, theirs):
    """(line number, ours' first line, theirs' first line) of the first conflicting hunk of a three-way merge (diff3),
    or None when it merges cleanly or git cannot tell."""
    import tempfile
    with tempfile.TemporaryDirectory() as td:
        paths = []
        for n, x in zip(("ours", "base", "theirs"), (ours, base, theirs)):
            q = os.path.join(td, n)
            with open(q, "w") as fh:
                fh.write(x if x.endswith("\n") or not x else x + "\n")
            paths.append(q)
        r = subprocess.run(["git", "merge-file", "-p", "--diff3", "-L", "ours", "-L", "base", "-L", "theirs", *paths],
                           capture_output=True, text=True)
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


def hunk_take(o, b, t):
    """(lines, what) for one diff3 hunk (ours, base, theirs lines) when it resolves, else None."""
    n = 0
    while n < min(len(o), len(t)) and o[n] == t[n]:
        n += 1
    m = 0
    while m < min(len(o), len(t)) - n and o[-1 - m] == t[-1 - m]:
        m += 1
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


def conflict_resolve(wt, regen=True, notes=None, regenerated=None, provisional=None, resolved=None):
    """Resolve wt's conflicted files when each is UNION_SAFE prose (both sides kept), a registered generated file
    (regenerate; only with regen) or -- with `resolved` a list, a backlog batch's assembly -- a source file whose
    conflicting hunks are additions on both sides (source_resolve): the files resolved (staged), or None (nothing it
    can resolve; (files, why) appended to `notes` when generated files' resolution failed)."""
    files = unmerged(wt)
    if not files:
        return None
    gen = [f for f in files if generated_entry(wt, f)]
    prose = [f for f in files if f not in gen and union_safe(f)]
    source = [f for f in files if f not in gen and f not in prose]
    if (source and resolved is None) or (gen and not regen):
        return None
    if source and not source_resolve(wt, source, resolved):
        return None
    if prose and union_resolve(wt, prose) is None:
        return None
    if gen:
        why = regenerate(wt, gen, regenerated, provisional)
        if why:
            if notes is not None:
                notes.append((gen, why))
            return None
    return files


def pick(wt, commits, dropped=None, regen=True, regenerated=None, provisional=None, resolved=None):
    """pick_commits under WorktreeGuard (EVERY EXIT PUTS THE WORKTREE BACK, slots.py): no cherry-pick is left in
    progress in the tree, whatever ends it."""
    with WorktreeGuard(wt):
        return pick_commits(wt, commits, dropped, regen, regenerated, provisional, resolved)


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
    start = git(wt, "rev-parse", "HEAD").strip()
    on_head = upstream_patch_ids(wt, commits[0], start) if commits else {}
    for c in commits:
        before = git(wt, "rev-parse", "HEAD").strip()
        via = on_head.get(patch_id(wt, c) or "")
        if via and not deliberate_empty(wt, c):
            if dropped is not None:
                dropped.append((c, via))
            continue
        r = git(wt, *NO_SIGN, "cherry-pick", "--allow-empty", "--keep-redundant-commits", c, check=False)
        notes, mine = [], [] if resolved is not None else None
        if r.returncode != 0 and conflict_resolve(wt, regen, notes, regenerated, provisional, mine):
            r = git(wt, *NO_SIGN, "-c", "core.editor=true", "cherry-pick", "--continue", check=False)
        if r.returncode == 0 and regen and regen_here():  # elsewhere git's clean merge stands, for the checks to judge
            sides = snapshots_both_changed(wt, c, before)
            why = regenerate(wt, sorted(sides), regenerated, provisional, sides=sides) if sides else None
            if why:
                git(wt, "reset", "--hard", "-q", start)
                return [f"{', '.join(sorted(sides))} (generated: {why})"]
            if sides and git(wt, "diff", "--cached", "--quiet", check=False).returncode != 0:
                git(wt, *NO_SIGN, "commit", "-q", "--amend", "--no-edit", "--allow-empty", check=False)
        if r.returncode == 0 and mine:
            trailers = [x for m in mine for x in ("--trailer", f"Auto-Resolved: {m['file']}: {'; '.join(m['hunks'])}")]
            git(wt, *NO_SIGN, "commit", "-q", "--amend", "--no-edit", "--allow-empty", *trailers, check=False)
            resolved.extend({"commit": c, **m} for m in mine)
        if r.returncode != 0:
            files = unmerged(wt)
            git(wt, "cherry-pick", "--abort", check=False)
            via = equivalent_on(wt, c, before) if files else None
            if via:  # its patch is already there, and a later change to the same lines conflicts with it again
                git(wt, "reset", "--hard", "-q", before)
                if dropped is not None:
                    dropped.append((c, via))
                continue
            git(wt, "reset", "--hard", "-q", start)
            noted = {f for gen, _why in notes for f in gen}
            return ([f for f in files if f not in noted] + [f"{', '.join(gen)} (generated: {why})" for gen, why in notes]
                    if files else [r.stderr.strip()[:200]])
        if is_empty_commit(wt, "HEAD") and not deliberate_empty(wt, c):
            git(wt, "reset", "--hard", "-q", before)
            if dropped is not None:
                dropped.append((c, equivalent_on(wt, c, before)))
    return None


# A BOUNCE NAMES WHAT CAUSED IT (2026-11-19: lane F2b's `no longer applies on origin/main 2cfa0ee95e` named neither
# file nor commit; its agent rebased blind). Only a real conflict reaches the agent (generated files are resolved in
# the check tree, waiting.py GENERATED FILES ARE NO OVERLAP), and its verdict names each conflicting file with the
# commit of main that last changed it since the change's base; the files are kept on the entry (`conflict_files`), so
# the change queued again from the same source takes its old place (server.py queue_change, A RESOLVED CONFLICT
# KEEPS ITS PLACE).
def conflict_paths(conflicts):
    """The file paths of a pick's conflict list (a generated file's entry carries its resolution's why)."""
    return [c.split(" (generated: ", 1)[0] for c in conflicts or []]


def conflict_cause(repo, since, main, conflicts, n=3):
    """`; it conflicts in F (changed on main by <sha> <subject>), ...` for the conflict list, each file's newest
    commit in since..main; '' when none is known."""
    out = []
    for c in (conflicts or [])[:n]:
        path = c.split(" (generated: ", 1)[0]
        line = git(repo, "log", "-1", "--format=%h %s", "--abbrev=10", f"{since}..{main}", "--", path,
                   check=False).stdout.strip() if since and main else ""
        out.append(f"{c} (changed on main by {line[:90]})" if line else c)
    more = len(conflicts or []) - n
    return f"; it conflicts in {', '.join(out)}" + (f" (+{more} more)" if more > 0 else "") if out else ""
