# agentctl, part `trailers`: commit-message trailers: a message's trailer lines (its final block), a trailer's values, a Fixes-Red: in a message.
# A real module (lib/agentctl/__init__.py MODULES): its own imports, the standard library's and MODULES' only.

import re
from agentctl.core import git


# ONE TRAILER PARSER (changekind.py, 2026-11-19): a `Fixes-Red:` value list -- git's %(trailers:...,valueonly) of one
# or more trailers -- read as its names. Before, four parsers split it four ways (commas only here and in commit_fixes,
# whitespace and commas in entry_fixes, `,;` and whitespace in red_fixes_red_commits, a message regex's first word in
# blame): `Fixes-Red: 6 7` named run "6 7" to the queue and runs 6 and 7 to the red machine.
TRAILER_SPLIT = re.compile(r"[,;\s]+")


def trailer_format(keys: tuple[str, ...], separator: str = "%x09", unfold: bool = False) -> str:
    """git's `--format` fragment reading each of `keys` as its trailers' values (the final block, as git reads it),
    comma-separated within a key and `separator` between keys: the ONE spelling of the `%(trailers...)` read."""
    opts = ",unfold" if unfold else ""
    return separator.join(f"%(trailers:key={k},valueonly{opts},separator=%x2C)" for k in keys)


def commit_trailer_values(repo: str, commit: str, key: str) -> set[str]:
    """The names a commit's `<key>:` trailers give (trailer_values), read by git from its message; none when git cannot
    read the commit. ONE read: every single-commit trailer question goes through it."""
    r = git(repo, "log", "-1", f"--format={trailer_format((key,))}", commit, check=False)
    return trailer_values(r.stdout) if r.returncode == 0 else set()


def trailer_values(s: str | None) -> set[str]:
    """The names of a trailer's values: split on commas, semicolons and whitespace, each without a leading '#'."""
    return {f.strip().lstrip("#") for f in TRAILER_SPLIT.split(s or "") if f.strip().lstrip("#")}


# HELD WORK (2026-11-06 22:51Z: the orphan re-lander queued ~/projects/movies-wt/es-gate-check, a gate re-enable the
# owner was explicitly holding, as stranded work). A `Hold: <why>` trailer says so on the commit itself: land refuses
# it with NOT LANDED unless `--release-hold` (the owner's go), and recovery (orphan duty) never submits it at all.
TRAILER_LINE = re.compile(r"^[A-Za-z][\w-]*:\s")


def paragraph_trailers(para: str) -> list[str]:
    """A paragraph's trailer lines, continuations joined; [] when any line of it is not one (it is prose)."""
    lines: list[str] = []
    for ln in para.splitlines():
        if not ln.strip():
            continue
        if ln[:1].isspace() and lines:  # a continuation of the trailer above
            lines[-1] += " " + ln.strip()
        elif TRAILER_LINE.match(ln):
            lines.append(ln.strip())
        else:
            return []
    return lines


def commit_trailers(msg: str | None) -> list[str]:
    """The trailer lines of a commit message: its FINAL paragraph after the subject, when made only of `Token: value`
    lines (a value may fold onto indented continuation lines, joined here). The block git reads (%(trailers)), and the
    one CLAUDE.md counts: a trailer counts only in the message's final block, beside `Co-Authored-By:` (review
    2026-11-26: fixes_red_in_message took a Fixes-Red: line anywhere, so an earlier paragraph's counted for a blame or
    revert decision but not for the gate). ONE parser: every agentctl reader of a message's trailers goes through it."""
    paras = [p for p in re.split(r"\n\s*\n", (msg or "").strip()) if p.strip()]
    return paragraph_trailers(paras[-1]) if len(paras) > 1 else []


def trailer_values_of(msg: str | None, key: str) -> set[str]:
    """The names a message's `<key>:` trailers give (trailer_values over each; the key matched without case, as git)."""
    vals = [v for k, _, v in (ln.partition(":") for ln in commit_trailers(msg)) if k.strip().lower() == key.lower()]
    return set().union(set(), *(trailer_values(v) for v in vals))


def fixes_red_in_message(msg: str | None) -> set[str]:
    """The names a commit message's `Fixes-Red:` trailers give."""
    return trailer_values_of(msg, "Fixes-Red")


DO_NOT_LAND_BRANCH = re.compile(r"^(measure|experiment)/")


DO_NOT_LAND_SUBJECT = re.compile(r"^\s*(PROTOTYPE|WIP|DO[ _-]?NOT[ _-]?LAND)(?![\w-])", re.I)


DO_NOT_LAND_TRAILER = re.compile(r"^Do-Not-Land:", re.I | re.M)


def do_not_land(repo, commits, branch=""):
    """Why this work must never reach main (throwaway: a measure/* or experiment/* branch, a commit whose subject starts
    PROTOTYPE / WIP / DO NOT LAND, a `Do-Not-Land:` trailer), or None. 2026-11-01: `agentctl land` from a prototype
    worktree (measure/chain-settle-proto, "PROTOTYPE: ... measure only, do not land") queued twice. Every path that queues
    a worktree's commits (land, a re-land, an orphan or blame re-land, a laptop submission) passes through here."""
    if DO_NOT_LAND_BRANCH.match(branch or ""):
        return f"branch {branch} is throwaway work (measure/* and experiment/* never land)"
    for c in commits or []:
        msg = git(repo, "log", "-1", "--format=%B", c, check=False).stdout
        subject = msg.strip().splitlines()[0] if msg.strip() else ""
        if DO_NOT_LAND_SUBJECT.match(subject):
            return f"commit {c[:10]} is marked not for landing (subject: {subject[:80]!r})"
        if DO_NOT_LAND_TRAILER.search(msg):
            return f"commit {c[:10]} carries a Do-Not-Land: trailer"
    return None


HOLD_TRAILER = re.compile(r"^Hold:\s*(\S.*)$", re.I)


def held_commit(repo, commits):
    """`commit <sha> carries Hold: <why>` for the first commit with a Hold: trailer, or None."""
    for c in commits or []:
        msg = git(repo, "log", "-1", "--format=%B", c, check=False).stdout
        for ln in commit_trailers(msg):
            m = HOLD_TRAILER.match(ln)
            if m:
                return f"commit {c[:10]} carries Hold: {m.group(1).strip()}"
    return None
