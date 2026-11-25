# agentctl, part `generatedregen`: the regeneration of conflicted generated files: a table merged row by row, else its regen command run over the combined tree and its output checked (a snapshot explained by the sides, a block's hand-written lines merged)
# A real module (lib/agentctl/__init__.py MODULES): its own imports, the standard library's and MODULES' only.

import os
import shlex
import subprocess
import sys
from typing import Any, Callable, Dict, List, Optional
from agentctl.procrunner import process_runner
from agentctl.core import env_num, git
from agentctl.land import tool_env
from agentctl.generated import (
    GENERATED_REGEN_TIMEOUT, block_merge, conflict_stages, explained, generated_entry, generated_registry,
    regen_only, regen_runs, rows_merge, tracked, unmerged)


# SNAPSHOTS ARE REGENERATED, NEVER MERGED (2026-11-21 ~07:40Z: split-shared-js `held: its check waits for scrapers-2`,
# both changing movies' page snapshots expected-{index,warszawa-index,wroclaw-index}.html -- files PageSnapshotSpec
# writes, which no agent edits by hand). A snapshot -- rendered HTML, a JSON read model, a whole-corpus text -- has no
# rows: a three-way merge of its lines, git's or ours, is a guess at what the combined code renders. Registered with
# `merge: regen`, it is no overlap (like every generated file), and whenever BOTH sides changed it -- a conflict, or a
# clean textual merge of two edits -- its regen command runs over the combined tree and its output is what the batch
# checks. A REGENERATED SNAPSHOT HIDES NOTHING: its output must be EXPLAINED by the two sides -- every line it adds
# added by one of them, every line it drops dropped by one of them (snapshot_unexplained) -- or the resolution fails
# and the member is held as before: an interaction neither side committed (a line only the combined code renders) is
# never landed by a regenerator; its author regenerates and commits it. Where no regen command runs (the server role)
# a snapshot is never kept provisionally: held. Then the batch's checks run over the combined tree as over any other
# (movies: FilmScheduleEndToEnd asserts expected-schedules.txt and the read model; PageSnapshotSpec runs on Main).
def snapshot_unexplained(base, ours, theirs, regenerated):
    """The first line of a regenerated snapshot neither side explains (added by neither, or dropped by neither), as
    `+line` / `-line`; None when each of its changes against the base is one of the sides' own."""
    from collections import Counter
    b, o, t, r = (Counter(x.splitlines()) for x in (base, ours, theirs, regenerated))
    added, removed = r - b, b - r
    may_add, may_remove = (o - b) | (t - b), (b - o) | (b - t)
    for line, n in added.items():
        if n > may_add.get(line, 0):
            return "+" + line[:160]
    for line, n in removed.items():
        if n > may_remove.get(line, 0):
            return "-" + line[:160]
    return None


def regen_env():
    """tool_env with the interpreter agentctl runs under last on PATH: a derived file's `python3 <script>` runs on a host
    whose PATH has no python3 of its own (ci-runner's reconciler: python3 by its store path; REGENERATED WHERE THE
    COMBINE HAPPENS)."""
    env = tool_env()
    here = os.path.dirname(sys.executable)
    if here and here not in env.get("PATH", "").split(":"):
        env["PATH"] = ":".join(x for x in (env.get("PATH", ""), here) if x)
    return env


def run_regen(wt, g):
    """Run registry entry g's regen command in wt: None, or why it failed, in words."""
    limit = g.get("timeout") or env_num("AGENTCTL_GENERATED_REGEN_TIMEOUT", GENERATED_REGEN_TIMEOUT)
    cmd = " ".join(shlex.quote(x) for x in g["regen"])
    try:
        r = process_runner().run(g["regen"], cwd=wt, capture_output=True, text=True, timeout=limit, env=regen_env(),
                           stdin=subprocess.DEVNULL)
    except subprocess.TimeoutExpired:
        return f"`{cmd}` ran past {limit:.0f} s"
    except OSError as ex:
        return f"`{cmd}` could not run ({ex.strerror or ex})"
    return None if r.returncode == 0 else regen_exit_text(cmd, r)


def regen_exit_text(cmd: str, r: Any) -> str:
    """A regen command's non-zero exit, in words: its code and the last line of its stderr (else stdout)."""
    last = ((r.stderr or r.stdout or "").strip().splitlines() or [""])[-1][:200]
    return f"`{cmd}` exited {r.returncode}" + (f": {last}" if last else "")


class Regeneration:
    """One resolution of wt's conflicted generated files over the tree as it stands (the combined tree): `files` each
    merged row by row, else its registry entry's regen command run in wt and its files staged -- or, where no regen
    command runs (the server role), merged row by row keeping the tree's (ours: main's) side of a row both sides changed,
    the file named in `provisional` (MAIN'S ROW, PROVISIONALLY). `pending` holds the files no row merge settled."""

    def __init__(self, wt: str, files: List[str], regenerated: Optional[List[str]], provisional: Optional[List[str]],
                 stages: Dict[str, Dict[int, str]], sides: Any):
        self.wt, self.files, self.regenerated, self.provisional = wt, files, regenerated, provisional
        self.stages, self.sides = stages, sides
        self.pending: List[str] = []

    def take(self, f: str, text: str) -> None:
        with open(os.path.join(self.wt, f), "w") as fh:
            fh.write(text)
        git(self.wt, "add", "--", f)

    def merge_rows(self) -> None:
        for f in self.files:
            text = None if regen_only(generated_entry(self.wt, f)) else rows_merge(*(self.stages[f][n] for n in (1, 2, 3)))
            if text is None:
                self.pending.append(f)
                continue
            self.take(f, text)

    def run(self) -> Optional[str]:
        self.merge_rows()
        for g in generated_registry(self.wt):  # one run of each command, however many of its files conflict
            mine = [f for f in self.pending if generated_entry(self.wt, f) is g]
            why = self.resolve(g, mine) if mine else None
            if why:
                return why
        if self.regenerated is not None:
            self.regenerated.extend(f for f in self.files if f not in self.regenerated)
        return None

    def resolve(self, g: Dict[str, Any], mine: List[str]) -> Optional[str]:
        if not g.get("regen"):  # a hand-written table merged row by row only: never a side's row kept provisionally
            return f"a row both sides changed in {', '.join(mine)}, which no regen command decides"
        if not regen_runs(g):
            return self.keep_ours(g, mine)
        return self.regenerate_entry(g, mine)

    def keep_ours(self, g: Dict[str, Any], mine: List[str]) -> Optional[str]:
        """No regen command runs here: a snapshot is held, a table keeps main's side of the rows both changed."""
        if regen_only(g):
            return f"{', '.join(mine)}: a snapshot both sides changed, and the server role runs no regen command"
        for f in mine:
            text = rows_merge(*(self.stages[f][n] for n in (1, 2, 3)), prefer_ours=True)
            if text is None:
                return f"{f} cannot be read as a table, and the server role runs no regen command"
            self.take(f, text)
            if self.provisional is not None:
                self.provisional.append(f)
        return None

    def regenerate_entry(self, g: Dict[str, Any], mine: List[str]) -> Optional[str]:
        """Run entry g's regen command over the tree and check what it wrote: None, or why it is no resolution."""
        wt = self.wt
        if self.sides is None:  # no conflict markers in what the command reads
            why = regen_base(wt, g, mine, self.stages, self.take)
            if why:
                return why
        before = {p: git(wt, "show", f":{p}", check=False).stdout for p in tracked(wt, g["paths"])
                  if p not in mine} if explained(g) else {}
        why = run_regen(wt, g)
        if why:
            return regen_failure(g, why)
        why = snapshot_hidden(wt, g, mine, self.stages, before) if explained(g) else None
        if why:
            return why
        return self.stage_output(g, mine)

    def stage_output(self, g: Dict[str, Any], mine: List[str]) -> Optional[str]:
        """Stage what the regen command wrote for entry g, and leave out whatever else it touched."""
        wt = self.wt
        git(wt, "add", "-A", "--", *g["paths"], check=False)
        left = sorted(set(unmerged(wt)) & set(mine))
        if left:
            return f"`{' '.join(g['regen'])}` left {', '.join(left)} unresolved"
        stray = git(wt, "diff", "--name-only", check=False).stdout.split()  # what else it wrote: never picked along
        if stray:
            git(wt, "checkout", "--", *stray, check=False)
        return None


def regen_failure(g: Dict[str, Any], why: str) -> str:
    """A regen command's failure `why`, led by which kind of file it was run for."""
    return ("both sides changed the snapshot " if explained(g) else "both sides changed the derived file "
            if regen_only(g) else "a row both sides changed, and ") + why


def regenerate(wt, files, regenerated=None, provisional=None, sides=None):
    """Resolve wt's conflicted generated files over the tree as it stands (the combined tree): each merged row by row,
    else its registry entry's regen command run in wt and its files staged -- or, where no regen command runs (the
    server role), merged row by row keeping the tree's (ours: main's) side of a row both sides changed, the file named
    in `provisional` (MAIN'S ROW, PROVISIONALLY). A SNAPSHOT (`merge: regen`) is never merged by rows: regenerated, its
    output explained by the sides (SNAPSHOTS ARE REGENERATED, NEVER MERGED), or not resolved. `sides`: {file: {1: base,
    2: ours, 3: theirs}} of files both sides changed that git merged cleanly (a snapshot's, regenerated all the same);
    else the conflict's stages. None: resolved (staged, appended to `regenerated`); else why not, in words."""
    stages = sides if sides is not None else conflict_stages(wt, files)
    if stages is None:
        return "added or deleted on one side"
    return Regeneration(wt, files, regenerated, provisional, stages, sides).run()


def snapshot_hidden(wt: str, g: Dict[str, Any], mine: List[str], stages: Dict[str, Dict[int, str]],
                    before: Dict[str, str]) -> Optional[str]:
    """Why a snapshot regenerated in wt is no resolution -- a file not written, a change neither side made, another
    snapshot of the command moved (A REGENERATED SNAPSHOT HIDES NOTHING) --, or None."""
    for f in mine:
        path = os.path.join(wt, f)
        if not os.path.exists(path):
            return f"`{' '.join(g['regen'])}` did not write {f}"
        with open(path) as fh:
            odd = snapshot_unexplained(stages[f][1], stages[f][2], stages[f][3], fh.read())
        if odd:
            return (f"the regenerated {f} holds a change neither side made ({odd}): an interaction its "
                    "authors regenerate and commit")
    for p, text in before.items():
        path = os.path.join(wt, p)
        if os.path.exists(path) and open(path).read() != text:
            return (f"the regen also moved {p}, which only one side changed: an interaction its authors "
                    "regenerate and commit")
    return None


def regen_base(wt: str, g: Dict[str, Any], mine: List[str], stages: Dict[str, Dict[int, str]],
               take: Callable[[str, str], None]) -> Optional[str]:
    """Put wt's conflicted files of entry g in a state its regen command reads: ours' side, or -- a file with a
    generated `block` -- its hand-written lines merged (A DERIVED FILE IS THE COMBINED TREE'S). None, or why not."""
    if not g.get("block"):
        git(wt, "checkout", "--ours", "--", *mine, check=False)
        return None
    for f in mine:
        text = block_merge(stages[f][1], stages[f][2], stages[f][3], g["block"])
        if text is None:
            return f"{f}: both sides changed its hand-written lines, outside the generated block, in one place"
        take(f, text)
    return None
