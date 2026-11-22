#!/usr/bin/env python3
"""A definition's closure among the NAMESPACE parts: every namespace definition it reaches through the definition
graph (defgraph.py), a real module's ending the walk -- what must move with it, or be cut, before its part can be one.

usage: closure.py <lib/agentctl> <name>...     prints `<name> <size> {part: names}` (a count past 6 names)"""
import sys

import defgraph
from partlist import parts_and_modules


def closure(defs, graph, modules, root):
    seen, todo = set(), [root]
    while todo:
        n = todo.pop()
        if n in seen or defs.get(n) in modules:
            continue
        seen.add(n)
        todo += graph.get(n, [])
    return seen


def main(argv):
    lib = argv[0]
    parts, modules = parts_and_modules(lib)
    defs, graph = defgraph.build(lib)
    for root in argv[1:]:
        if root not in defs:
            print(f"{root}: not defined", file=sys.stderr)
            return 1
        by = {}
        for n in closure(defs, graph, set(modules), root):
            by.setdefault(defs[n], []).append(n)
        shown = {p: (len(v) if len(v) > 6 else sorted(v)) for p, v in sorted(by.items(), key=lambda kv: parts.index(kv[0]))}
        print(root, sum(map(len, by.values())), shown)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
