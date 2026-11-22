#!/usr/bin/env python3
"""Each top-level definition of a module and the module's own top-level names it reads: where a split's seams are.

usage: intra.py <module.py>     prints `<line>\t<name>\t<names it reads, space-separated>`, in source order
A registration into a table (`TABLE["key"] = ...`) is listed under its target as written, as movedefs.py names it."""
import ast
import sys


def bound_names(n):
    """The names a top-level statement binds: a def's or class's name, an assignment's targets."""
    if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
        return [n.name]
    if isinstance(n, (ast.Assign, ast.AnnAssign, ast.AugAssign)):
        ts = n.targets if isinstance(n, ast.Assign) else [n.target]
        return ([x.id for t in ts for x in ast.walk(t) if isinstance(x, ast.Name) and isinstance(x.ctx, ast.Store)]
                + [ast.unparse(t) for t in ts if isinstance(t, ast.Subscript)])
    return []


def reads(src):
    """[(line, name, sorted names of the module's own it reads)] per top-level binding, in source order."""
    tree = ast.parse(src)
    stmts = [(n, bound_names(n)) for n in tree.body]
    own = {x for _n, xs in stmts for x in xs}
    out = []
    for n, xs in stmts:
        used = {x.id for x in ast.walk(n) if isinstance(x, ast.Name) and isinstance(x.ctx, ast.Load) and x.id in own}
        out.extend((n.lineno, x, sorted(used - {x})) for x in xs)
    return out


def main(argv):
    for line, name, used in reads(open(argv[0]).read()):
        print(f"{line}\t{name}\t{' '.join(used)}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
