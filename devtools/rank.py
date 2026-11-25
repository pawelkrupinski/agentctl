#!/usr/bin/env python3
"""Every over-limit function of the quality baseline, ranked by 30-day churn x cognitive complexity.

  python3 devtools/rank.py [--shape S] [--limit N] [--batches N --size K] [--root DIR] [--live]

One line per function: file:line, the function, each measure with its points over the limit, its SHAPE, its file's
churn (commits touching the file in 30 days). SHAPE is a heuristic reading of the function's syntax, the first that
fits: try-default (a try/except whose handler returns or assigns a default), get-or-chain (3+ `.get(..) or` / ternary
defaults), subcommand-ladder (an if/elif chain of 3+ branches comparing to string literals), formatter (6+ f-strings),
multi-loop (3+ loops), closure-state (a nested def, or nonlocal), other.
--batches N --size K  splits the top functions into N batches of at most K, DISJOINT by module prefix (red_owner and
red_store share the prefix `red`): a prefix's functions all go to one batch, so two batches never share a file.
--live  measure the tree now (quality/measure.py in the tools venv) instead of reading the baseline.
"""
import argparse
import ast
import os
import subprocess
import sys
from collections import Counter

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "quality"))
import measure  # noqa: E402

FUNCTION_KINDS = ("cognitive", "cyclomatic", "inner-defs", "method-lines")
SHAPES = ("try-default", "get-or-chain", "subcommand-ladder", "formatter", "multi-loop", "closure-state", "other")


def baseline_rows(root):
    path = os.path.join(root, "quality", "quality-baseline.tsv")
    rows = [line.split("\t") for line in open(path).read().splitlines() if line.strip() and not line.startswith("#")]
    return [(kind, key, int(value)) for kind, key, value in rows]


def live_rows(root):
    tools = subprocess.run([sys.executable, os.path.join(root, "quality", "tools.py")], capture_output=True,
                           text=True, check=True).stdout.strip()
    out = subprocess.run([tools, os.path.join(root, "quality", "measure.py"), "--root", root], capture_output=True,
                         text=True, check=True).stdout
    return [(k, key, int(v)) for k, key, v in (line.split("\t") for line in out.splitlines() if line.count("\t") == 2)]


def definitions(root):
    """{key: (part, line, node)}: every function by its qualname, `<qualname>@<part>` when two parts define it."""
    found, count = [], Counter()
    for part, path in measure.sources(root):
        for qualname, node in measure.functions(ast.parse(open(path).read())):
            found.append((part, qualname, node))
            count[qualname] += 1
    return {(f"{q}@{p}" if count[q] > 1 else q): (p, n.lineno, n) for p, q, n in found}


def is_default(stmt):
    return isinstance(stmt, (ast.Return, ast.Assign, ast.AnnAssign, ast.Pass, ast.Continue))


def string_compare(test):
    if isinstance(test, ast.Compare):
        return any(isinstance(c, ast.Constant) and isinstance(c.value, str) for c in [test.left, *test.comparators]) \
            or any(isinstance(c, (ast.Tuple, ast.List, ast.Set)) and c.elts and all(
                isinstance(e, ast.Constant) and isinstance(e.value, str) for e in c.elts) for c in test.comparators)
    return isinstance(test, ast.BoolOp) and any(string_compare(v) for v in test.values)


def ladder_length(node):
    n = 0
    while isinstance(node, ast.If) and string_compare(node.test):
        n += 1
        node = node.orelse[0] if len(node.orelse) == 1 and isinstance(node.orelse[0], ast.If) else None
    return n


def has_get_or(node):
    return isinstance(node, ast.BoolOp) and isinstance(node.op, ast.Or) and any(
        isinstance(v, ast.Call) and isinstance(v.func, ast.Attribute) and v.func.attr == "get" for v in node.values)


def shape(fn):
    """The heuristic SHAPE tag of a function node."""
    nodes = list(ast.walk(fn))
    if any(isinstance(n, ast.Try) and any(len(h.body) == 1 and is_default(h.body[0]) for h in n.handlers)
           for n in nodes):
        return "try-default"
    if sum(has_get_or(n) or isinstance(n, ast.IfExp) for n in nodes) >= 3:
        return "get-or-chain"
    if any(ladder_length(n) >= 3 for n in nodes):
        return "subcommand-ladder"
    if sum(isinstance(n, ast.JoinedStr) for n in nodes) >= 6:
        return "formatter"
    if sum(isinstance(n, (ast.For, ast.While, ast.AsyncFor)) for n in nodes) >= 3:
        return "multi-loop"
    if any(isinstance(n, ast.Nonlocal) for n in nodes) or any(
            isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n is not fn for n in nodes):
        return "closure-state"
    return "other"


def churn(root, days=30):
    """{path relative to root: commits touching it in the last `days` days}."""
    out = subprocess.run(["git", "-C", root, "log", f"--since={days}.days.ago", "--name-only", "--format="],
                         capture_output=True, text=True).stdout
    return Counter(line for line in out.splitlines() if line)


def part_path(part):
    return "agentctl" if part == "agentctl" else f"lib/agentctl/{part}.py"


def ranked(root, rows):
    """Ranked dicts: part, line, name, over {kind: points}, shape, churn, score."""
    defs, commits = definitions(root), churn(root)
    over = {}
    for kind, key, value in rows:
        if kind in FUNCTION_KINDS and key in defs:
            over.setdefault(key, {})[kind] = value - measure.LIMITS[kind]
    out = []
    for key, points in over.items():
        part, line, node = defs[key]
        c = commits[part_path(part)]
        out.append({"part": part, "line": line, "name": key, "over": points, "shape": shape(node), "churn": c,
                    "score": c * (points.get("cognitive", 0) + measure.LIMITS["cognitive"] if "cognitive" in points else 1)})
    return sorted(out, key=lambda f: (-f["score"], f["part"], f["line"]))


def prefix(part):
    return part.split("_")[0]


def batches(functions, count, size):
    """`count` disjoint batches of up to `size` functions: a prefix's functions all go to one batch."""
    groups = {}
    for f in functions:
        groups.setdefault(prefix(f["part"]), []).append(f)
    out = [[] for _ in range(count)]
    for group in sorted(groups.values(), key=lambda g: -sum(f["score"] for f in g)):
        room = max(out, key=lambda b: size - len(b))
        room.extend(group[:size - len(room)])
    return out


def render(f):
    over = " ".join(f"{k}+{v}" for k, v in sorted(f["over"].items()))
    return f"{part_path(f['part'])}:{f['line']}\t{f['name']}\t{over}\t{f['shape']}\tchurn30={f['churn']}"


def main(argv):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--root", default=ROOT)
    ap.add_argument("--shape", choices=SHAPES)
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--batches", type=int)
    ap.add_argument("--size", type=int, default=10)
    ap.add_argument("--live", action="store_true")
    ap.add_argument("--counts", action="store_true", help="print the shape counts only")
    args = ap.parse_args(argv)
    functions = ranked(args.root, live_rows(args.root) if args.live else baseline_rows(args.root))
    if args.counts:
        for tag, n in Counter(f["shape"] for f in functions).most_common():
            print(f"{tag}\t{n}")
        return 0
    if args.shape:
        functions = [f for f in functions if f["shape"] == args.shape]
    if args.limit:
        functions = functions[:args.limit]
    if args.batches:
        for i, batch in enumerate(batches(functions, args.batches, args.size), 1):
            print(f"# batch {i}: {len(batch)} functions, files {sorted({part_path(f['part']) for f in batch})}")
            print("\n".join(render(f) for f in batch))
        return 0
    print("\n".join(render(f) for f in functions))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
