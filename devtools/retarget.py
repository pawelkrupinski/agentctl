#!/usr/bin/env python3
"""Repoint the real modules' imports after a move: every `from agentctl.X import n` where X no longer DEFINES n
imports n from the module that does (the first in MODULES), the import block rewritten in MODULES order.

usage: retarget.py <lib/agentctl>     prints each module it rewrote"""
import ast
import os
import sys

from partlist import defined_names, import_lines, module_list


def retarget(lib):
    modules = module_list(lib)
    defs = {m: defined_names(ast.parse(open(os.path.join(lib, f"{m}.py")).read())) for m in modules}
    owner = {}
    for m in modules:
        for n in defs[m]:
            owner.setdefault(n, m)
    changed_modules = []
    for m in modules:
        path = os.path.join(lib, f"{m}.py")
        src = open(path).read()
        tree = ast.parse(src)
        imports = [n for n in tree.body if isinstance(n, ast.ImportFrom) and (n.module or "").startswith("agentctl.")]
        want, moved = {}, False
        for n in imports:
            source = n.module.split(".", 1)[1]
            for a in n.names:
                target = source if a.name in defs.get(source, ()) or a.name not in owner else owner[a.name]
                moved |= target != source
                want.setdefault(target, set()).add(a.name)
        if not moved:
            continue
        lines = src.split("\n")
        first, last = imports[0].lineno - 1, imports[-1].end_lineno
        if any(n.lineno - 1 < last and n.end_lineno > first and n not in imports for n in tree.body):
            sys.exit(f"{m}: its agentctl imports are not one block")
        block = []
        for source in sorted(want, key=modules.index):
            if source != m:
                block += import_lines(f"agentctl.{source}", sorted(want[source]))
        lines[first:last] = block
        open(path, "w").write("\n".join(lines))
        changed_modules.append(m)
    return changed_modules


if __name__ == "__main__":
    for m in retarget(sys.argv[1]):
        print("retargeted", m)
