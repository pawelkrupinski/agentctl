#!/usr/bin/env python3
"""Planning the namespace's last moves, on devtools/defgraph.py's graph (modules left out: their names block nothing).

  python3 devtools/moveplan.py <lib/agentctl> order          the part order with fewest upward reads, and those
  python3 devtools/moveplan.py <lib/agentctl> plan [--mode M] [--tsv OUT]   the moves leaving no upward read
  python3 devtools/moveplan.py <lib/agentctl> stuck PART     PART's definitions reading (through PART) another part
  python3 devtools/moveplan.py <lib/agentctl> who NAME...    a definition's part, what it reads and who reads it

ORDER: Eades' greedy feedback-arc-set heuristic on the part graph, each edge weighted by its definition edges: a part
reading no remaining part goes low, one no remaining part reads goes high, else the part most read and least reading
goes low. PLAN, mode `up`: every definition rises to the highest part its closure reads; mode `hybrid` (the default)
first lowers each leaf (a definition reading no namespace definition) to its lowest reader, then rises. A plan row is
`<name>\\t<from part>\\t<to part>`, for movedefs."""
import argparse
import sys
from dataclasses import dataclass

import defgraph
from partlist import parts_and_modules


@dataclass
class Graph:
    defs: dict      # name -> part
    deps: dict      # namespace name -> the namespace names it reads (itself left out)
    readers: dict   # namespace name -> the namespace names reading it
    parts: list     # the namespace parts, in load order


def load(lib):
    parts, modules = parts_and_modules(lib)
    defs, graph = defgraph.build(lib)
    ns = [n for n in defs if defs[n] not in modules]
    nsset = set(ns)
    deps = {n: [u for u in graph.get(n, []) if u in nsset and u != n] for n in ns}
    readers = {n: [] for n in ns}
    for n in ns:
        for u in deps[n]:
            readers[u].append(n)
    return Graph(defs, deps, readers, [p for p in parts if p not in modules])


def part_weights(g):
    """{(a, b): definition edges}: part a reads part b that many times (b must sit below a)."""
    w = {}
    for n, us in g.deps.items():
        for u in us:
            a, b = g.defs[n], g.defs[u]
            if a != b:
                w[(a, b)] = w.get((a, b), 0) + 1
    return w


def part_order(g):
    w = part_weights(g)
    rem, low, high = set(g.parts), [], []

    def reads(p):
        return sum(v for (a, b), v in w.items() if a == p and b in rem)

    def read_by(p):
        return sum(v for (a, b), v in w.items() if b == p and a in rem)

    while rem:
        moved = True
        while moved:
            moved = False
            for p in sorted(rem):
                if reads(p) == 0:
                    low.append(p)
                elif read_by(p) == 0:
                    high.insert(0, p)
                else:
                    continue
                rem.discard(p)
                moved = True
        if rem:
            p = max(sorted(rem), key=lambda p: read_by(p) - reads(p))
            low.append(p)
            rem.discard(p)
    return low + high


def upward(g, order):
    """[(definition edges, a, b)] for every part a reading a part b above it in order, most first."""
    idx = {p: i for i, p in enumerate(order)}
    return sorted(((v, a, b) for (a, b), v in part_weights(g).items() if idx[b] > idx[a]), reverse=True)


def topological(g):
    """The namespace definitions, each after every definition it reads (a cycle is cut where the walk meets it)."""
    out, seen = [], set()
    for root in g.deps:
        if root in seen:
            continue
        seen.add(root)
        stack = [(root, iter(g.deps[root]))]
        while stack:
            n, it = stack[-1]
            u = next((u for u in it if u not in seen), None)
            if u is None:
                stack.pop()
                out.append(n)
            else:
                seen.add(u)
                stack.append((u, iter(g.deps[u])))
    return out


def move_plan(g, order, mode="hybrid"):
    """{name: (from part, to part)} for every definition the plan moves."""
    idx = {p: i for i, p in enumerate(order)}
    pos = {n: idx[g.defs[n]] for n in g.deps}
    if mode == "hybrid":
        for n in g.deps:
            if not g.deps[n] and g.readers[n]:
                pos[n] = min([pos[n]] + [pos[r] for r in g.readers[n]])
    for n in topological(g):
        pos[n] = max([pos[n]] + [pos[u] for u in g.deps[n]])
    return {n: (g.defs[n], order[pos[n]]) for n in g.deps if order[pos[n]] != g.defs[n]}


def stuck(g, part):
    """PART's definitions that read another namespace part, directly or through PART's own definitions."""
    mine = [n for n in g.deps if g.defs[n] == part]
    out = {n for n in mine if any(g.defs[u] != part for u in g.deps[n])}
    grew = True
    while grew:
        more = {n for n in mine if n not in out and any(u in out for u in g.deps[n])}
        out |= more
        grew = bool(more)
    return out


def other_readers(g, name):
    return sorted({g.defs[r] for r in g.readers.get(name, ())} - {g.defs[name]})


def main(argv):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("lib")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("order")
    p = sub.add_parser("plan")
    p.add_argument("--mode", choices=("hybrid", "up"), default="hybrid")
    p.add_argument("--tsv")
    sub.add_parser("stuck").add_argument("part")
    sub.add_parser("who").add_argument("names", nargs="+")
    a = ap.parse_args(argv)
    g = load(a.lib)
    if a.cmd == "stuck":
        s = stuck(g, a.part)
        print(sum(1 for n in g.deps if g.defs[n] == a.part), "defs;", len(s), "stuck:", " ".join(sorted(s)))
    elif a.cmd == "who":
        for n in a.names:
            reads = sorted(f"{u}@{g.defs[u]}" for u in g.deps.get(n, ()) if g.defs[u] != g.defs[n])
            print(f"{n}@{g.defs[n]}\n  reads: {' '.join(reads)}\n  used by: {' '.join(other_readers(g, n))}")
    else:
        order = part_order(g)
        if a.cmd == "order":
            up = upward(g, order)
            print("order:", " ".join(order))
            print("upward def-edges:", sum(v for v, _, _ in up), "in", len(up), "part pairs")
            for v, x, y in up:
                print(f"  {v:3} {x} -> {y}")
        else:
            plan = move_plan(g, order, a.mode)
            print("moves", len(plan))
            by = {}
            for n, ft in plan.items():
                by.setdefault(ft, []).append(n)
            for (x, y), names in sorted(by.items(), key=lambda kv: (-len(kv[1]), kv[0])):
                print(f"  {x} -> {y}: {len(names)} {' '.join(sorted(names))}")
            if a.tsv:
                with open(a.tsv, "w") as f:
                    f.writelines(f"{n}\t{x}\t{y}\n" for n, (x, y) in plan.items())
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
