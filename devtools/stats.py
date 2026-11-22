#!/usr/bin/env python3
"""agentctl's complexity stats, START against a commit, both measured by THIS tree's quality/measure.py (one ruler).

  devtools/stats.sh [--json] [--repo DIR] [--start REV] [--python PY] [REV]

START defaults to 27b12c3 (before the simplify lanes, the owner's baseline), REV to origin/main, the repo to this
checkout. Each side is a `git archive` snapshot. Per side: the production lines (the entry and lib/agentctl/*.py),
its files, the parts and real modules (lib/agentctl/__init__.py's PARTS / MODULES; since the namespace went, every
part a module), the functions past ANY function
limit, the rows past each kind's limit, and the mypy errors its types baseline records (quality/types-baseline.tsv:
the types ratchet's sum; mypy itself is not re-run, nor coverage -- each takes minutes, the suite's own run).
--python: the interpreter with the measure's pinned tools (default: what quality/tools.py prints)."""
import ast
import json
import os
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)

KINDS = ("cognitive", "cyclomatic", "method-lines", "inner-defs", "file-lines", "copies", "fan-out", "cycle")
FUNCTION_KINDS = ("cognitive", "cyclomatic", "method-lines", "inner-defs")
START = "27b12c3"


def snapshot(repo, rev, into):
    os.makedirs(into)
    tar = subprocess.run(["git", "-C", repo, "archive", rev], capture_output=True, check=True).stdout
    subprocess.run(["tar", "-x", "-C", into], input=tar, check=True)


def part_lists(lib):
    """(PARTS, MODULES) of a revision's lib/agentctl/__init__.py: an old one's namespace parts and real modules
    (PARTS = MODULES + NAMESPACE), a new one's modules alone (every part one); ([], []) before the package."""
    try:
        tree = ast.parse(open(os.path.join(lib, "__init__.py")).read())
    except (OSError, SyntaxError):
        return [], []
    lists = {}
    for node in tree.body:
        if isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name):
            terms = [node.value.left, node.value.right] if isinstance(node.value, ast.BinOp) else [node.value]
            try:
                lists[node.targets[0].id] = [n for t in terms for n in (
                    lists[t.id] if isinstance(t, ast.Name) else ast.literal_eval(t))]
            except (ValueError, KeyError, TypeError):
                continue
    modules = lists.get("MODULES", [])
    return lists.get("PARTS", modules), modules


def side(repo, rev, py, tmp):
    d = os.path.join(tmp, rev.replace("/", "_"))
    snapshot(repo, rev, d)
    rows = [line.split("\t") for line in subprocess.run(
        [py, os.path.join(ROOT, "quality", "measure.py"), "--root", d], capture_output=True, text=True,
        check=True).stdout.splitlines() if line.count("\t") == 2]
    lib = os.path.join(d, "lib", "agentctl")
    files = sorted(f for f in os.listdir(lib) if f.endswith(".py")) if os.path.isdir(lib) else []
    lines = 0
    for p in [os.path.join(d, "agentctl")] + [os.path.join(lib, f) for f in files]:
        if os.path.isfile(p):
            with open(p) as f:
                lines += sum(1 for _ in f)
    parts, modules = part_lists(lib)
    mypy = None
    tb = os.path.join(d, "quality", "types-baseline.tsv")
    if os.path.exists(tb):
        with open(tb) as f:
            mypy = sum(int(c[2]) for c in (l.rstrip("\n").split("\t") for l in f if not l.startswith("#")) if len(c) == 3)
    sha = subprocess.run(["git", "-C", repo, "log", "-1", "--format=%h %cs", rev], capture_output=True, text=True).stdout.strip()
    return {"rev": rev, "commit": sha, "lines": lines, "files": len(files), "parts": len(parts), "modules": len(modules),
            "functions_over_any": len({k for kind, k, _v in rows if kind in FUNCTION_KINDS}),
            "over": {kind: sum(1 for k, _key, _v in rows if k == kind) for kind in KINDS}, "mypy_errors": mypy}


def text(s, label):
    return (f"{label} {s['rev']}: {s['commit']}\n  lines={s['lines']} files={s['files']} parts={s['parts']} "
            f"modules={s['modules']} fn_over_any={s['functions_over_any']} mypy={s['mypy_errors']}\n  "
            + " ".join(f"{k}={v}" for k, v in s["over"].items()))


def main(argv):
    args, opts = [], {"--repo": ROOT, "--start": START, "--python": None}
    as_json = "--json" in argv
    argv = [a for a in argv if a != "--json"]
    while argv:
        a = argv.pop(0)
        if a in opts:
            opts[a] = argv.pop(0)
        else:
            args.append(a)
    rev = args[0] if args else "origin/main"
    py = opts["--python"] or subprocess.run([sys.executable, os.path.join(ROOT, "quality", "tools.py")],
                                            capture_output=True, text=True, check=True).stdout.strip()
    with tempfile.TemporaryDirectory(prefix="agentctl-stats-") as tmp:
        start, now = side(opts["--repo"], opts["--start"], py, tmp), side(opts["--repo"], rev, py, tmp)
    if as_json:
        print(json.dumps({"start": start, "now": now}, indent=1))
    else:
        print(text(start, "START") + "\n" + text(now, "NOW  "))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
