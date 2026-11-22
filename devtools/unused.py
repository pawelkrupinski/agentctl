#!/usr/bin/env python3
"""List, or with --fix drop, the import names a real module never reads.

usage: unused.py <lib/agentctl> [--fix] <module>...     prints `<module> <line> [names]` per import with dead names
A name counts as read when the module's code loads it, or when its word appears anywhere outside the import itself
(a string annotation, a comment): unused only ever drops what is certainly dead."""
import ast
import os
import re
import sys

from partlist import import_lines


def bound(alias):
    return (alias.asname or alias.name).split(".")[0]


def dead_imports(src):
    """{import node: [its aliases nothing reads]}."""
    tree = ast.parse(src)
    loaded = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)}
    lines = src.split("\n")
    out = {}
    for n in tree.body:
        if not isinstance(n, (ast.Import, ast.ImportFrom)):
            continue
        rest = "\n".join(l for i, l in enumerate(lines) if not n.lineno - 1 <= i < n.end_lineno)
        dead = [a for a in n.names if bound(a) not in loaded and not re.search(rf"\b{re.escape(bound(a))}\b", rest)]
        if dead:
            out[n] = dead
    return out


def spelled(alias):
    return alias.name + (f" as {alias.asname}" if alias.asname else "")


def without(src, dead):
    lines = src.split("\n")
    for n in sorted(dead, key=lambda n: -n.lineno):
        keep = [a for a in n.names if a not in dead[n]]
        if not keep:
            new = []
        elif isinstance(n, ast.ImportFrom):
            new = import_lines(n.module, [spelled(a) for a in keep])
        else:
            new = [f"import {spelled(a)}" for a in keep]
        lines[n.lineno - 1:n.end_lineno] = new
    return "\n".join(lines)


def main(argv):
    lib, fix = argv[0], "--fix" in argv
    for m in [a for a in argv[1:] if a != "--fix"]:
        path = os.path.join(lib, f"{m}.py")
        src = open(path).read()
        dead = dead_imports(src)
        for n, names in dead.items():
            print(m, n.lineno, [bound(a) for a in names])
        if fix and dead:
            open(path, "w").write(without(src, dead))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
