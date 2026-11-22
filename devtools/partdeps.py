#!/usr/bin/env python3
"""Which namespace parts are nearest to becoming real modules, and what blocks each: the definitions of OTHER
namespace parts its definitions read (devtools/defgraph.py's graph; a module's names block nothing).

  python3 devtools/partdeps.py <lib/agentctl> [--top N] [PART...]

Prints `<blockers> <part> [name@part ...]` for the N parts with the fewest blocking definitions (0: mkmodule can
convert it now); with PARTS, every blocker of each, with the definition reading it."""
import argparse
import sys

import defgraph
from partlist import parts_and_modules


def blockers(lib):
    """{namespace part: {(its definition, the blocking definition)}}, modules left out."""
    parts, modules = parts_and_modules(lib)
    defs, graph = defgraph.build(lib)
    out = {p: set() for p in parts if p not in modules}
    for name, used in graph.items():
        part = defs[name]
        if part in modules:
            continue
        for u in used:
            if defs[u] != part and defs[u] not in modules:
                out[part].add((name, u))
    return out, defs


def main(argv):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("lib")
    ap.add_argument("--top", type=int, default=15)
    ap.add_argument("parts", nargs="*")
    a = ap.parse_args(argv)
    found, defs = blockers(a.lib)
    if a.parts:
        for p in a.parts:
            for name, u in sorted(found.get(p, ())):
                print(f"{p}\t{name}\t{u}@{defs[u]}")
        return 0
    rows = sorted((len({u for _n, u in v}), p, sorted({f"{u}@{defs[u]}" for _n, u in v})) for p, v in found.items())
    for n, p, names in rows[:a.top]:
        print(n, p, " ".join(names[:8]) + (" ..." if len(names) > 8 else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
