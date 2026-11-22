#!/usr/bin/env python3
"""Pure-move proof over two lib/agentctl trees: the same multiset of top-level statements (by AST, positions aside)
across all their parts. A real module's own import lines are left out (they state what the namespace gave it).

  python3 devtools/sameast.py <old lib/agentctl> <new lib/agentctl>       exit 0 the same, 1 not (the first few shown)"""
import ast
import collections
import os
import sys

from partlist import part_path, parts_and_modules


def statements(lib):
    out = collections.Counter()
    for p in parts_and_modules(lib)[0]:
        for n in ast.parse(open(part_path(lib, p)).read()).body:
            if not isinstance(n, (ast.Import, ast.ImportFrom)):
                out[ast.dump(n)] += 1
    return out


def main(argv):
    a, b = statements(argv[0]), statements(argv[1])
    gone, new = a - b, b - a
    print("statements", sum(a.values()), "gone", sum(gone.values()), "new", sum(new.values()))
    for k in list(gone)[:3]:
        print("GONE", k[:200])
    for k in list(new)[:3]:
        print("NEW", k[:200])
    return 1 if gone or new else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
