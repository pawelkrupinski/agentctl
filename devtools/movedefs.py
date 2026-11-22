#!/usr/bin/env python3
"""Move top-level definitions between parts of agentctl's one namespace, as a pure move.

usage: movedefs.py <lib/agentctl> <to-part> [--new "<description>" --after <part>] [--before <name>] <name>...
Each named top-level statement (def/class/assignment, with the comment block right above it) is cut from its part and
appended, in source order, to <to-part> (created after --after when new), or inserted, in that order, right before the
statement binding <name> there (--before: a module-level statement there reads them). Proof: every function's bytecode in the
loaded namespace is unchanged (run samecode.py after)."""
import ast
import os
import sys

from partlist import parts_and_modules, place

lib = sys.argv[1]
to = sys.argv[2]
args = sys.argv[3:]
new_desc, after, before = None, None, None
if "--new" in args:
    i = args.index("--new"); new_desc = args[i + 1]; del args[i:i + 2]
if "--before" in args:
    i = args.index("--before"); before = args[i + 1]; del args[i:i + 2]
if "--after" in args:
    i = args.index("--after"); after = args[i + 1]; del args[i:i + 2]
names = set(args)
init = os.path.join(lib, "__init__.py")
parts = parts_and_modules(lib)[0]


def stmt_spans(src):
    """[(first line incl. its comment block, last line, names)] of each top-level statement (1-based)."""
    tree = ast.parse(src)
    lines = src.split("\n")
    out = []
    prev_end = 0
    for n in tree.body:
        first = min([n.lineno] + [d.lineno for d in getattr(n, "decorator_list", [])])
        # the comment block directly above (no blank line between) belongs to the statement
        k = first - 1
        while k - 1 >= prev_end and k - 1 >= 0 and lines[k - 1].startswith("#"):
            k -= 1
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            nm = {n.name}
        elif isinstance(n, (ast.Assign, ast.AnnAssign, ast.AugAssign)):
            ts = n.targets if isinstance(n, ast.Assign) else [n.target]
            nm = {x.id for t in ts for x in ast.walk(t) if isinstance(x, ast.Name) and isinstance(x.ctx, ast.Store)}
        else:
            nm = set()
        out.append((k + 1, n.end_lineno, nm))
        prev_end = n.end_lineno
    return out


moved, found, writes = [], set(), {}
for p in parts:
    if p == to:
        continue
    path = os.path.join(lib, f"{p}.py")
    src = open(path).read()
    lines = src.split("\n")
    cut = [(a, b) for a, b, nm in stmt_spans(src) if nm and nm <= names]
    for a, b, nm in stmt_spans(src):
        if nm & names and not nm <= names:
            sys.exit(f"{p}: a statement binds {sorted(nm)}: name them all")
    if not cut:
        continue
    for a, b in cut:
        moved.append((p, "\n".join(lines[a - 1:b])))
        found |= {x for x0, y0, nm in stmt_spans(src) if (x0, y0) == (a, b) for x in nm}
    keep = []
    i = 1
    for a, b in cut:
        keep.extend(lines[i - 1:a - 1])
        i = b + 1
    keep.extend(lines[i - 1:])
    text = "\n".join(keep)
    while "\n\n\n\n" in text:
        text = text.replace("\n\n\n\n", "\n\n\n")
    writes[path] = text
missing = names - found
if missing:
    sys.exit(f"not found: {sorted(missing)}")  # nothing written yet
tpath = os.path.join(lib, f"{to}.py")
placing = False
if new_desc is not None:  # every check before the first write: a refused move writes nothing
    if os.path.exists(tpath):
        sys.exit(f"{tpath} exists")
    all_parts, modules = parts_and_modules(lib)
    if after not in all_parts:
        sys.exit(f"--after {after}: not in PARTS")
    new_order = all_parts[:all_parts.index(after) + 1] + [to] + all_parts[all_parts.index(after) + 1:]
    placing = True
    body = (f"# agentctl, part `{to}`: {new_desc}\n# Runs inside agentctl's one namespace, after the parts before it in "
            f"lib/agentctl/__init__.py's PARTS (it uses their names).\n")
else:
    body = open(tpath).read()
body = body.rstrip("\n")
block = "".join("\n\n\n" + text.strip("\n") for _p, text in moved)
if before is None:
    body += block
else:
    spans = [a for a, _b, nm in stmt_spans(body) if before in nm]
    if len(spans) != 1:
        sys.exit(f"{to}: --before {before}: {len(spans)} statements bind it")
    lines = body.split("\n")
    body = "\n".join(lines[:spans[0] - 1]).rstrip("\n") + block.rstrip("\n") + "\n\n\n" + "\n".join(lines[spans[0] - 1:])
for path, text in writes.items():
    open(path, "w").write(text)
if placing:
    place(lib, new_order, modules)
open(tpath, "w").write(body + "\n")
print(f"moved {len(moved)} statements into {to}")
