#!/usr/bin/env python3
"""Write a module's explicit imports: the standard library's and the other modules' names it reads (after movedefs.py
moved code into it). A module with imports gets only the lines it now lacks; one with none yet (movedefs --new) gets
them after its header comments. Refuses when it reads a name no module defines. MODULES is re-sorted by import.

usage: mkmodule.py <lib/agentctl> <module>"""
import ast
import builtins
import os
import re
import symtable
import sys

from partlist import import_lines, module_list, place


def top_defs(tree):
    out = set()
    for n in tree.body:
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            out.add(n.name)
        elif isinstance(n, (ast.Assign, ast.AnnAssign, ast.AugAssign)):
            ts = n.targets if isinstance(n, ast.Assign) else [n.target]
            out |= {x.id for t in ts for x in ast.walk(t) if isinstance(x, ast.Name) and isinstance(x.ctx, ast.Store)}
        elif isinstance(n, (ast.Import, ast.ImportFrom)):
            out |= {(a.asname or a.name).split(".")[0] for a in n.names}
        elif isinstance(n, (ast.For, ast.With)):
            out |= {x.id for x in ast.walk(n) if isinstance(x, ast.Name) and isinstance(x.ctx, ast.Store)}
    return out


def reads(src, path):
    def walk(t):
        # at module level: a name read and never bound there (a module-level comprehension's variable is bound)
        out = {s.get_name() for s in t.get_symbols() if s.is_referenced() and (
            not (s.is_assigned() or s.is_imported()) if t.get_type() == "module" else s.is_global())}
        for c in t.get_children():
            out |= walk(c)
        return out
    return walk(symtable.symtable(src, path, "exec"))


def read_trees(lib, modules):
    """{module: its syntax tree}."""
    return {p: ast.parse(open(os.path.join(lib, f"{p}.py")).read()) for p in modules}


def owners(trees, part):
    """{name: the first other module defining it}."""
    owner = {}
    for p, t in trees.items():
        if p != part:
            for n in top_defs(t) - {"__doc__"}:
                owner.setdefault(n, p)
    return owner


def core_imports_of(core_tree):
    """{name: how core.py imports it}."""
    core_imports = {}
    for n in core_tree.body:
        if isinstance(n, ast.Import):
            for a in n.names:
                core_imports[(a.asname or a.name).split(".")[0]] = (a.name, a.asname)
        elif isinstance(n, ast.ImportFrom) and n.module and not n.module.startswith("agentctl"):
            for a in n.names:
                core_imports[a.asname or a.name] = ("from", n.module, a.name, a.asname)
    return core_imports


def std_from_of(trees):
    """{name: (module, original name)} for every part's `from <non-agentctl module> import`."""
    std_from = {}
    for t in trees.values():
        for n in t.body:
            if isinstance(n, ast.ImportFrom) and n.module and not n.module.startswith("agentctl") and not n.level:
                for a in n.names:
                    std_from.setdefault(a.asname or a.name, (n.module, a.name))
    return std_from


def import_plan(need, src, core_imports, std_from, owner):
    """(standard imports, {module: names} from outside agentctl, {part: names} from the parts, names no module defines)."""
    stdlib, from_std, from_mod, bad = [], {}, {}, []
    for name in sorted(need):
        if name in core_imports:
            spec = core_imports[name]
            if spec[0] == "from":
                from_std.setdefault(spec[1], []).append(spec[2] if not spec[3] else f"{spec[2]} as {spec[3]}")
            elif spec[0].startswith("urllib."):  # each urllib submodule the part names
                stdlib += [f"import urllib.{u}" for u in sorted(set(re.findall(r"\burllib\.(error|parse|request)\b", src)))]
            else:
                stdlib.append(f"import {spec[0]} as {spec[1]}" if spec[1] else f"import {spec[0]}")
        elif name in std_from:  # a name some part imports from outside agentctl (typing's Optional): from there
            mod_, orig = std_from[name]
            from_std.setdefault(mod_, []).append(orig if orig == name else f"{orig} as {name}")
        elif owner.get(name) is None and name in sys.stdlib_module_names:  # a standard module core does not import (ast, operator)
            stdlib.append(f"import {name}")
        elif owner.get(name) is None:
            bad.append(name)
        else:
            from_mod.setdefault(owner[name], []).append(name)
    return stdlib, from_std, from_mod, bad


def merge_into_imports(src_lines, tree, lines):
    """The source with `lines` (the standard library's above the first module import, the modules' after the last import)."""
    imports = [n for n in tree.body if isinstance(n, (ast.Import, ast.ImportFrom))]
    ours = [n for n in imports if isinstance(n, ast.ImportFrom) and (n.module or "").startswith("agentctl")]
    last = max(n.end_lineno for n in imports)
    first_ours = ours[0].lineno - 1 if ours else last
    std = [x for x in lines if not x.startswith(("from agentctl", "    "))]
    mods = lines[len(std):]
    return src_lines[:first_ours] + std + src_lines[first_ours:last] + mods + src_lines[last:]


def after_header(src_lines, lines):
    """The source with `lines` inserted after its header comments (a module with no imports yet)."""
    i = 0
    while i < len(src_lines) and src_lines[i].startswith("#"):
        i += 1
    body = src_lines[i:]
    while body and not body[0].strip():
        body.pop(0)
    return src_lines[:i] + [""] + lines + ["", ""] + body


def main(lib, part):
    modules = module_list(lib)
    path = os.path.join(lib, f"{part}.py")
    src = open(path).read()
    tree = ast.parse(src)
    own = top_defs(tree)
    need = {n for n in reads(src, path) - own - set(dir(builtins)) if not n.startswith("__")}  # dunders: the compiler's
    trees = read_trees(lib, modules)
    stdlib, from_std, from_mod, bad = import_plan(need, src, core_imports_of(trees["core"]), std_from_of(trees),
                                                  owners(trees, part))
    if bad:
        sys.exit(f"{part}: reads names no module defines: {bad}")
    lines = sorted(set(stdlib)) + [f"from {m} import {', '.join(sorted(v))}" for m, v in sorted(from_std.items())]
    for m, v in sorted(from_mod.items(), key=lambda kv: modules.index(kv[0])):
        lines += import_lines(f"agentctl.{m}", sorted(v))
    src_lines = src.split("\n")
    if any(isinstance(n, (ast.Import, ast.ImportFrom)) for n in tree.body):  # its own imports: only those it lacks, after them
        if lines:
            open(path, "w").write("\n".join(merge_into_imports(src_lines, tree, lines)))
            place(lib, modules)
        print(part, "+", lines)
        return
    open(path, "w").write("\n".join(after_header(src_lines, lines)))
    place(lib, modules)
    print(part, "->", lines)


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2])
