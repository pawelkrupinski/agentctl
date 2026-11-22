#!/usr/bin/env python3
"""agentctl's code measures: every row past a limit, as `kind<TAB>key<TAB>value` (quality/ratchet.py holds them).

  python3 quality/measure.py [--root DIR] [--all]     rows past the limits (--all: every row, any value)

Measured: the production code, the entry `agentctl` and lib/agentctl/*.py. The kinds and their limits (the movies
ratchets' limits, CLAUDE.md "Code quality ratchets"):

  cognitive    <qualname>   cognitive complexity (SonarSource's, by complexipy)        10
  cyclomatic   <qualname>   cyclomatic complexity (McCabe, by radon)                   10
  inner-defs   <qualname>   function definitions nested in it (any depth)              2
  method-lines <qualname>   lines from its `def` to its body's end                     50
  file-lines   <part>       the part's lines                                           500
  copies       <a>|<b>...   clone groups of 50+ tokens spanning exactly these parts    0
  fan-out      <part>       the other parts it imports, foundations                      9
                            aside (a part using no part but foundations: a leaf helper)
  cycle        <part>       1: the part is in a reference cycle (a strong component)   0

A part is a module (lib/agentctl/<part>.py, or the entry): its dependencies are the modules it imports (`from
agentctl.<part> import`). A function is keyed by its qualified name alone (`red_owner.step`), so a function moved to
another part keeps its row; a name defined in two parts is keyed `<qualname>@<part>`.
"""
import ast
import hashlib
import io
import os
import sys
import tokenize

LIMITS = {"cognitive": 10, "cyclomatic": 10, "inner-defs": 2, "method-lines": 50, "file-lines": 500, "copies": 0,
          "fan-out": 9, "cycle": 0}
CLONE_TOKENS = 50
HERE = os.path.dirname(os.path.abspath(__file__))


def sources(root):
    """(part name, path) of every production source, the entry first."""
    lib = os.path.join(root, "lib", "agentctl")
    parts = [("agentctl", os.path.join(root, "agentctl"))]
    parts += [(f[:-3], os.path.join(lib, f)) for f in sorted(os.listdir(lib)) if f.endswith(".py")]
    return [(name, path) for name, path in parts if os.path.isfile(path)]


def functions(tree):
    """(qualname, node) of every function, nested ones included, in source order."""
    out = []

    def walk(node, prefix):
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                name = prefix + child.name
                if not isinstance(child, ast.ClassDef):
                    out.append((name, child))
                walk(child, name + ".")
            else:
                walk(child, prefix)

    walk(tree, "")
    return out


def inner_defs(node):
    return sum(isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) for n in ast.walk(node)) - 1


def cognitive(path):
    import complexipy
    return {f.line_start: f.complexity for f in complexipy.file_complexity(path).functions}


def cyclomatic(path):
    from radon.complexity import cc_visit
    out = {}

    def add(blocks):
        for b in blocks:
            if b.letter in ("F", "M"):
                out[b.lineno] = b.complexity
            add(getattr(b, "closures", []))
            add(getattr(b, "methods", []))
            add(getattr(b, "inner_classes", []))

    add(cc_visit(open(path).read()))
    return out


ANALYSERS = {"cognitive": cognitive, "cyclomatic": cyclomatic}  # the measures a pinned tool takes (quality/tools.lock)


def function_rows(parts, analysers):
    """The per-function rows of every part, keyed by qualname (`@part` when two parts define one). A tool that fails
    fails the measure: no row is ever left out for want of a tool."""
    seen, rows = {}, []
    for part, path in parts:
        tree = ast.parse(open(path).read(), path)
        by_line = {kind: analyse(path) for kind, analyse in analysers.items()}
        for qual, node in functions(tree):
            first = min([node.lineno] + [d.lineno for d in node.decorator_list])
            values = {kind: lines.get(node.lineno, lines.get(first, 0)) for kind, lines in by_line.items()}
            values.update({"inner-defs": inner_defs(node), "method-lines": node.end_lineno - node.lineno + 1})
            seen.setdefault(qual, []).append(part)
            rows.append((qual, part, values))
    return [(q if len(seen[q]) == 1 else f"{q}@{p}", v) for q, p, v in rows]


def normalised_tokens(path):
    """(token text, line) of a source without comments, layout tokens and import statements."""
    out, skip_line = [], None
    for tok in tokenize.generate_tokens(io.StringIO(open(path).read()).readline):
        if tok.type in (tokenize.COMMENT, tokenize.NL, tokenize.NEWLINE, tokenize.INDENT, tokenize.DEDENT,
                        tokenize.ENDMARKER):
            continue
        if tok.type == tokenize.NAME and tok.string in ("import", "from") and tok.line.lstrip().startswith(tok.string):
            skip_line = tok.start[0]
        if skip_line == tok.start[0]:
            continue
        if tok.type == tokenize.STRING and tok.string.lstrip("rbuRBUfF").startswith(('"""', "'''")):
            continue  # a docstring is prose, never a clone of code
        out.append((tok.string, tok.start[0]))
    return out


def clone_groups(parts, n=CLONE_TOKENS):
    """Maximal runs of n+ identical tokens found at two or more places: a list of [(part, line)...] per group."""
    toks = {part: normalised_tokens(path) for part, path in parts}
    windows = {}
    for part, ts in toks.items():
        for i in range(len(ts) - n + 1):
            key = hashlib.blake2b("\x00".join(t for t, _ in ts[i:i + n]).encode(), digest_size=12).digest()
            windows.setdefault(key, []).append((part, i))
    shared = {k: v for k, v in windows.items() if len(v) > 1}
    where = {loc: k for k, locs in shared.items() for loc in locs}
    groups = {}
    for key, locs in shared.items():
        # a run starts where the window before it is not shared by the same set of places
        before = {where.get((p, i - 1)) for p, i in locs}
        if len(before) == 1 and None not in before and len(shared[before.pop()]) == len(locs):
            continue
        distinct = []
        for p, i in sorted(locs):
            if not distinct or distinct[-1][0] != p or i - distinct[-1][1] >= n:
                distinct.append((p, i))
        if len(distinct) > 1:
            groups[key] = [(p, toks[p][i][1]) for p, i in distinct]
    return list(groups.values())


def imports(tree):
    """The parts a module imports by `from agentctl.<part> import ...`."""
    return {node.module.split(".", 1)[1] for node in tree.body
            if isinstance(node, ast.ImportFrom) and (node.module or "").startswith("agentctl.")}


def part_graph(parts):
    """{part: {the parts it imports}} (the entry, too: it imports cli)."""
    trees = {part: ast.parse(open(path).read(), path) for part, path in parts}
    return {part: (imports(tree) & set(trees)) - {part} for part, tree in trees.items()}


def foundations(graph):
    """The parts that use no part but other foundations (the fixed point from the leaves): a leaf helper such as the
    state files' reader and writer. Using one is no collaboration (movies' fan-out counts collaborators only), so a
    concept extracted into a part of its own never raises its users' fan-out."""
    found = set()
    while True:
        more = {p for p, uses in graph.items() if p not in found and uses <= found}
        if not more:
            return found
        found |= more


def strong_components(graph):
    index, low, stack, on, out, counter = {}, {}, [], set(), [], [0]

    def visit(v):
        index[v] = low[v] = counter[0]
        counter[0] += 1
        stack.append(v)
        on.add(v)
        for w in graph[v]:
            if w not in index:
                visit(w)
                low[v] = min(low[v], low[w])
            elif w in on:
                low[v] = min(low[v], index[w])
        if low[v] == index[v]:
            comp = set()
            while True:
                w = stack.pop()
                on.discard(w)
                comp.add(w)
                if w == v:
                    break
            out.append(comp)

    sys.setrecursionlimit(10000)
    for v in sorted(graph):
        if v not in index:
            visit(v)
    return out


def measure(root, analysers=None):
    """Every row, any value: [(kind, key, value)]. `analysers` defaults to every tool's (ANALYSERS)."""
    parts = sources(root)
    analysers = ANALYSERS if analysers is None else analysers
    rows = [(kind, qual, v) for qual, values in function_rows(parts, analysers) for kind, v in values.items()]
    rows += [("file-lines", part, sum(1 for _ in open(path))) for part, path in parts]
    copies = {}
    for group in clone_groups(parts):
        key = "|".join(sorted({p for p, _ in group}))
        copies[key] = copies.get(key, 0) + len(group)
    rows += [("copies", key, n) for key, n in copies.items()]
    graph = part_graph(parts)
    found = foundations(graph)
    rows += [("fan-out", part, len(uses - found)) for part, uses in graph.items()]
    for comp in strong_components(graph):
        rows += [("cycle", part, int(len(comp) > 1)) for part in comp]
    return rows


def past_limits(rows):
    return [(k, key, v) for k, key, v in rows if v > LIMITS[k]]


def main(argv):
    root = argv[argv.index("--root") + 1] if "--root" in argv else os.path.dirname(HERE)
    rows = measure(root)
    for kind, key, value in sorted(rows if "--all" in argv else past_limits(rows)):
        print(f"{kind}\t{key}\t{value}")


if __name__ == "__main__":
    main(sys.argv[1:])
