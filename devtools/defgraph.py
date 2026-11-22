#!/usr/bin/env python3
"""The scope-aware definition graph of agentctl's parts: one node per top-level definition (def, class, assignment),
an edge to every other top-level definition whose name its code reads as a GLOBAL (symtable, so a local or a parameter
of the same name is no edge). Decorators, default values and base classes are read at module scope, so they count.

  python3 devtools/defgraph.py <lib/agentctl> [--json OUT] [--cycles]

--json writes {"defs": {name: part}, "graph": {name: [names]}}; --cycles prints every strongly connected component of
more than one definition (a definition cycle), largest first, with each member's part. Exit 1 when there is one."""
import argparse
import ast
import json
import symtable
import sys

from partlist import part_path, parts_and_modules


def globals_of(table):
    """Names a scope (and every scope nested in it) reads as globals."""
    out = {s.get_name() for s in table.get_symbols() if s.is_referenced() and s.is_global()}
    for child in table.get_children():
        out |= globals_of(child)
    return out


def names_in(node):
    return {y.id for y in ast.walk(node) if isinstance(y, ast.Name)}


def definition_reads(node, children):
    """(names it binds, names it reads) for a top-level statement, or None for any other statement."""
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
        t = children.get(node.name)
        used = globals_of(t) if t else set()
        module_scope = list(node.decorator_list)
        if isinstance(node, ast.ClassDef):
            module_scope += node.bases
        else:
            module_scope += node.args.defaults + [d for d in node.args.kw_defaults if d is not None]
        for x in module_scope:
            used |= names_in(x)
        return [node.name], used
    if isinstance(node, (ast.Assign, ast.AnnAssign, ast.AugAssign)):
        targets = node.targets if isinstance(node, ast.Assign) else [node.target]
        names = [x.id for t in targets for x in ast.walk(t) if isinstance(x, ast.Name)]
        used = names_in(node.value) if node.value is not None else set()
        for inner in ast.walk(node.value) if node.value is not None else []:  # a lambda's or comprehension's own names
            if isinstance(inner, ast.Lambda):
                used -= {a.arg for a in inner.args.args}
            if isinstance(inner, (ast.ListComp, ast.SetComp, ast.DictComp, ast.GeneratorExp)):
                for g in inner.generators:
                    used -= names_in(g.target)
        return names, used
    return None


def build(lib):
    defs, graph = {}, {}
    for p in parts_and_modules(lib)[0]:
        path = part_path(lib, p)
        src = open(path).read()
        children = {c.get_name(): c for c in symtable.symtable(src, path, "exec").get_children()}
        for node in ast.parse(src).body:
            got = definition_reads(node, children)
            if got is None:
                continue
            names, used = got
            for nm in names:
                defs.setdefault(nm, p)
                graph.setdefault(nm, set()).update(used)
    return defs, {k: sorted(u for u in v if u in defs and u != k) for k, v in graph.items()}


def components(graph):
    """Tarjan's strongly connected components, iteratively (the graph is deep)."""
    index, low, on, stack, out, counter = {}, {}, set(), [], [], [0]
    for root in graph:
        if root in index:
            continue
        work = [(root, iter(graph[root]))]
        index[root] = low[root] = counter[0]; counter[0] += 1; stack.append(root); on.add(root)
        while work:
            v, it = work[-1]
            w = next(it, None)
            if w is None:
                work.pop()
                if work:
                    low[work[-1][0]] = min(low[work[-1][0]], low[v])
                if low[v] == index[v]:
                    comp = []
                    while True:
                        x = stack.pop(); on.discard(x); comp.append(x)
                        if x == v:
                            break
                    out.append(comp)
            elif w not in index:
                index[w] = low[w] = counter[0]; counter[0] += 1; stack.append(w); on.add(w)
                work.append((w, iter(graph.get(w, ()))))
            elif w in on:
                low[v] = min(low[v], index[w])
    return out


def main(argv):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("lib")
    ap.add_argument("--json")
    ap.add_argument("--cycles", action="store_true")
    a = ap.parse_args(argv)
    defs, graph = build(a.lib)
    if a.json:
        with open(a.json, "w") as f:
            json.dump({"defs": defs, "graph": graph}, f)
    print("definitions", len(defs), "edges", sum(len(v) for v in graph.values()))
    if not a.cycles:
        return 0
    cycles = sorted((c for c in components(graph) if len(c) > 1), key=len, reverse=True)
    for c in cycles:
        print(f"cycle of {len(c)}: " + ", ".join(f"{n}@{defs[n]}" for n in sorted(c)))
    return 1 if cycles else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
