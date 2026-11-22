# agentctl, part `trailers`: commit-message trailers: a message's trailer lines (its final block), a trailer's values, a Fixes-Red: in a message.
# A real module (lib/agentctl/__init__.py MODULES): its own imports, the standard library's and MODULES' only.

import re


# ONE TRAILER PARSER (changekind.py, 2026-11-19): a `Fixes-Red:` value list -- git's %(trailers:...,valueonly) of one
# or more trailers -- read as its names. Before, four parsers split it four ways (commas only here and in commit_fixes,
# whitespace and commas in entry_fixes, `,;` and whitespace in red_fixes_red_commits, a message regex's first word in
# blame): `Fixes-Red: 6 7` named run "6 7" to the queue and runs 6 and 7 to the red machine.
TRAILER_SPLIT = re.compile(r"[,;\s]+")


def trailer_values(s: str | None) -> set[str]:
    """The names of a trailer's values: split on commas, semicolons and whitespace, each without a leading '#'."""
    return {f.strip().lstrip("#") for f in TRAILER_SPLIT.split(s or "") if f.strip().lstrip("#")}


def fixes_red_in_message(msg: str | None) -> set[str]:
    """The names a commit message's `Fixes-Red:` trailer lines give (trailer_values over each line's value)."""
    return set().union(set(), *(trailer_values(m) for m in re.findall(r"^Fixes-Red:[ \t]*(.*)$", msg or "", re.M)))


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
    """The trailer lines of a commit message: every paragraph after the subject made only of `Token: value` lines (a
    value may fold onto indented continuation lines, joined here). Not only the last paragraph (review 2026-11-07): a
    `Hold:` block written above the attribution block, or a folded Hold: value, is a trailer to whoever wrote it, and
    reading it as prose let held work land."""
    paras = [p for p in re.split(r"\n\s*\n", (msg or "").strip()) if p.strip()]
    out = []
    for para in paras[1:]:
        out += paragraph_trailers(para)
    return out
