#!/usr/bin/env python3
"""A git merge driver for the ratchets' baselines (quality/quality-baseline.tsv, quality/types-baseline.tsv, quality/design-baseline.tsv): two
branches that each froze or lowered different rows merge to the file the ratchet's `update` would write, never to
conflict markers.

  python3 devtools/baselinemerge.py install            write .gitattributes lines + this clone's git config
  python3 devtools/baselinemerge.py driver %O %A %B    the driver git runs: merges into %A (exit 0, never a conflict)

WHY NOT RE-RUN `update`: git runs a driver file by file, before the rest of the merged tree exists, so a measure then
would read half-merged sources. The driver merges the ROWS instead (a baseline is a map (kind, key) -> value): a row
only one side changed takes that side's value; a row both changed takes the lower one, an absent row (it fell to its
limit) counting lowest. That is `update`'s result whenever the two branches' edits are independent, and when they are
not the ratchet's own check (`quality/ratchet.py check`, in pre-push) names the row to re-record with `update`.
The header is ours (the file's own text, written by `update`). The rows are written sorted, as `update` writes them.
"""
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "devtools", "design_rules"))
import baseline_ratchet  # noqa: E402

DRIVER = "baseline-regen"
FILES = ("quality/quality-baseline.tsv", "quality/types-baseline.tsv", "quality/design-baseline.tsv")


def merge_rows(base, ours, theirs):
    """The merged {(kind, key): value}: a side that left a row as in base yields to the other; both changed: the lower."""
    out = {}
    for row in set(base) | set(ours) | set(theirs):
        b, o, t = base.get(row), ours.get(row), theirs.get(row)
        value = t if o == b else o if t == b or o == t else min(o or 0, t or 0) or None
        if value is not None:
            out[row] = value
    return out


def header(text):
    lines = []
    for line in text.splitlines(keepends=True):
        if not line.startswith("#"):
            break
        lines.append(line)
    return "".join(lines)


def drive(base_path, ours_path, theirs_path):
    """Merge the three baseline files into ours_path."""
    texts = [open(p).read() for p in (base_path, ours_path, theirs_path)]
    rows = merge_rows(*(baseline_ratchet.parse(t) for t in texts))
    lines = [f"{kind}\t{key}\t{baseline_ratchet.show(value)}" for (kind, key), value in sorted(rows.items())]
    with open(ours_path, "w") as f:
        f.write(header(texts[1]) + "\n".join(lines) + "\n")
    return 0


def script(root):
    """This script as the clone's MAIN worktree has it (the config is shared by every worktree, and a worktree is
    removed after landing), else this file."""
    common = subprocess.run(["git", "-C", root, "rev-parse", "--path-format=absolute", "--git-common-dir"],
                            capture_output=True, text=True).stdout.strip()
    main = os.path.join(os.path.dirname(common), "devtools", "baselinemerge.py") if common else ""
    return main if os.path.exists(main) else os.path.join(HERE, "baselinemerge.py")


def install(root=ROOT):
    """The attribute lines (committed) and the driver's command (this clone's git config, which worktrees share)."""
    path = os.path.join(root, ".gitattributes")
    have = open(path).read() if os.path.exists(path) else ""
    for f in FILES:
        line = f"{f} merge={DRIVER}\n"
        if line not in have:
            have += line
    with open(path, "w") as out:
        out.write(have)
    command = f"python3 {script(root)} driver %O %A %B"
    for key, value in ((f"merge.{DRIVER}.name", "regenerate a ratchet baseline"), (f"merge.{DRIVER}.driver", command)):
        subprocess.run(["git", "-C", root, "config", key, value], check=True)
    print(f"installed: {path} and merge.{DRIVER}.driver")
    return 0


def main(argv):
    if argv[:1] == ["install"]:
        return install()
    if argv[:1] == ["driver"] and len(argv) == 4:
        return drive(*argv[1:])
    print(__doc__)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
