#!/usr/bin/env python3
"""Move top-level definitions between agentctl's modules, as a pure move.

usage: movedefs.py <lib/agentctl> <to-module> [--new "<description>"] [--before <name>] [--from <module>] <name>...
Each named top-level statement (def/class/assignment, with the comment block right above it) is cut from its module and
appended, in source order, to <to-module> (created, and listed in MODULES, with --new), or inserted, in that order,
right before the statement binding <name> there (--before: a module-level statement there reads them). A name two
modules bind is refused unless --from names the one module to cut from; a registration into another module's table
(`TABLE["key"] = ...`) is named by its target as written, `TABLE["key"]`. Then:
mkmodule.py <to-module> (its imports), retarget.py (the importers'), unused.py --fix (the dead ones). Proof: sameast.py
(the same statements) and samecode.py (every function's bytecode unchanged)."""
import ast
import os
import sys

from partlist import module_list, write_modules


def parse_args(argv):
    """(lib, to, new_desc, before, source, names) of the command line."""
    lib, to, args = argv[0], argv[1], argv[2:]
    options = {}
    for flag in ("--new", "--before", "--from"):
        if flag in args:
            i = args.index(flag)
            options[flag] = args[i + 1]
            del args[i:i + 2]
    names = {ast.unparse(ast.parse(x, mode="eval").body) if "[" in x else x for x in args}  # TABLE["k"] as unparsed
    return lib, to, options.get("--new"), options.get("--before"), options.get("--from"), names


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
            nm |= {ast.unparse(t) for t in ts if isinstance(t, ast.Subscript)}  # a registration: TABLE["key"] = ...
        else:
            nm = set()
        out.append((k + 1, n.end_lineno, nm))
        prev_end = n.end_lineno
    return out


def cut_from(parts, to, source):
    """The modules a move may cut from: every one but the target, or only `source`."""
    return [p for p in parts if p != to and (source is None or p == source)]


def read_sources(lib, modules):
    """{module: its text}."""
    return {p: open(os.path.join(lib, f"{p}.py")).read() for p in modules}


def cut_statements(sources, names):
    """(moved [(module, text)], found names, {module: text without them}, {name: modules binding it})."""
    moved, found, writes, binders = [], set(), {}, {}
    for p, src in sources.items():
        lines = src.split("\n")
        cut = [(a, b) for a, b, nm in stmt_spans(src) if nm and nm <= names]
        for a, b, nm in stmt_spans(src):
            if nm & names and not nm <= names:
                sys.exit(f"{p}: a statement binds {sorted(nm)}: name them all")
        if not cut:
            continue
        for a, b in cut:
            moved.append((p, "\n".join(lines[a - 1:b])))
            for x in {x for x0, y0, nm in stmt_spans(src) if (x0, y0) == (a, b) for x in nm}:
                found.add(x)
                binders.setdefault(x, set()).add(p)
        keep = []
        i = 1
        for a, b in cut:
            keep.extend(lines[i - 1:a - 1])
            i = b + 1
        keep.extend(lines[i - 1:])
        text = "\n".join(keep)
        while "\n\n\n\n" in text:
            text = text.replace("\n\n\n\n", "\n\n\n")
        writes[p] = text
    return moved, found, writes, binders


def target_body(existing, to, new_desc, moved, before):
    """The target module's text (`existing`, or the header of a new one) with the moved statements appended, or inserted
    right before `before`'s statement."""
    body = existing if new_desc is None else (
        f"# agentctl, part `{to}`: {new_desc}\n# A real module (lib/agentctl/__init__.py MODULES): its own imports, "
        f"the standard library's and MODULES' only.\n")
    body = body.rstrip("\n")
    block = "".join("\n\n\n" + text.strip("\n") for _p, text in moved)
    if before is None:
        return body + block
    spans = [a for a, _b, nm in stmt_spans(body) if before in nm]
    if len(spans) != 1:
        sys.exit(f"{to}: --before {before}: {len(spans)} statements bind it")
    lines = body.split("\n")
    return "\n".join(lines[:spans[0] - 1]).rstrip("\n") + block.rstrip("\n") + "\n\n\n" + "\n".join(lines[spans[0] - 1:])


def main(argv):
    lib, to, new_desc, before, source, names = parse_args(argv)
    parts = module_list(lib)
    moved, found, writes, binders = cut_statements(read_sources(lib, cut_from(parts, to, source)), names)
    missing = names - found
    if missing:
        sys.exit(f"not found: {sorted(missing)}")  # nothing written yet
    twice = sorted(f"{x} ({', '.join(sorted(ps))})" for x, ps in binders.items() if len(ps) > 1)
    if twice:
        sys.exit(f"bound in more than one module: {'; '.join(twice)}: --from <module> names the one to cut from")
    tpath = os.path.join(lib, f"{to}.py")
    if new_desc is not None and os.path.exists(tpath):  # every check before the first write: a refused move writes nothing
        sys.exit(f"{tpath} exists")
    body = target_body(open(tpath).read() if new_desc is None else "", to, new_desc, moved, before)
    for p, text in writes.items():
        open(os.path.join(lib, f"{p}.py"), "w").write(text)
    if new_desc is not None:
        write_modules(lib, parts + [to])  # mkmodule.py, writing its imports, sorts it into place
    open(tpath, "w").write(body + "\n")
    print(f"moved {len(moved)} statements into {to}")


if __name__ == "__main__":
    main(sys.argv[1:])
