#!/usr/bin/env python3
"""Turn a part into a real module: write its explicit imports (stdlib + the MODULES it reads), mark it, list it.

usage: mkmodule.py <lib/agentctl> <part>
Refuses when the part reads a name only a namespace part (not a MODULE) defines. On a part that is a module already
(code was moved into it), adds only the import lines it now lacks."""
import ast
import builtins
import os
import re
import symtable
import sys

from partlist import parts_and_modules, place

lib, part = sys.argv[1], sys.argv[2]
init = os.path.join(lib, "__init__.py")
parts, modules = parts_and_modules(lib)


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


path = os.path.join(lib, f"{part}.py")
src = open(path).read()
tree = ast.parse(src)
own = top_defs(tree)
need = {n for n in reads(src, path) - own - set(dir(builtins)) if not n.startswith("__")}  # dunders: the compiler's
owner = {}
for p in parts:
    if p == part:
        continue
    t = ast.parse(open(os.path.join(lib, f"{p}.py")).read())
    for n in top_defs(t) - {"__doc__"}:
        owner.setdefault(n, p)
core_imports = {}
for n in ast.parse(open(os.path.join(lib, "core.py")).read()).body:
    if isinstance(n, ast.Import):
        for a in n.names:
            core_imports[(a.asname or a.name).split(".")[0]] = (a.name, a.asname)
    elif isinstance(n, ast.ImportFrom) and n.module and not n.module.startswith("agentctl"):
        for a in n.names:
            core_imports[a.asname or a.name] = ("from", n.module, a.name, a.asname)
std_from = {}  # {name: (module, original name)} for every part's `from <non-agentctl module> import`
for p in parts:
    for n in ast.parse(open(os.path.join(lib, f"{p}.py")).read()).body:
        if isinstance(n, ast.ImportFrom) and n.module and not n.module.startswith("agentctl") and not n.level:
            for a in n.names:
                std_from.setdefault(a.asname or a.name, (n.module, a.name))
stdlib, from_std, from_mod, bad = [], {}, {}, []
for name in sorted(need):
    if name in core_imports:
        spec = core_imports[name]
        if spec[0] == "from":
            from_std.setdefault(spec[1], []).append(spec[2] if not spec[3] else f"{spec[2]} as {spec[3]}")
        else:
            if spec[0].startswith("urllib."):  # each urllib submodule the part names
                stdlib += [f"import urllib.{u}" for u in sorted(set(re.findall(r"\burllib\.(error|parse|request)\b", src)))]
            else:
                stdlib.append(f"import {spec[0]} as {spec[1]}" if spec[1] else f"import {spec[0]}")
        continue
    if name in std_from:  # a name some part imports from outside agentctl (typing's Optional): from there
        mod_, orig = std_from[name]
        from_std.setdefault(mod_, []).append(orig if orig == name else f"{orig} as {name}")
        continue
    p = owner.get(name)
    if p is None and name in sys.stdlib_module_names:  # a standard module core does not import (ast, operator)
        stdlib.append(f"import {name}")
        continue
    if p in modules:
        from_mod.setdefault(p, []).append(name)
    else:
        bad.append((name, p))
if bad:
    sys.exit(f"{part}: reads namespace names: {bad}")
lines = sorted(set(stdlib)) + [f"from {m} import {', '.join(sorted(v))}" for m, v in sorted(from_std.items())]
for m, v in sorted(from_mod.items(), key=lambda kv: parts.index(kv[0])):
    text = f"from agentctl.{m} import {', '.join(sorted(v))}"
    if len(text) > 118:
        body, cur = [], "    "
        for x in sorted(v):
            if len(cur) + len(x) + 2 > 116:
                body.append(cur.rstrip()); cur = "    "
            cur += x + ", "
        body.append(cur.rstrip().rstrip(","))
        text = f"from agentctl.{m} import (\n" + "\n".join(body) + ")"
    lines.append(text)
src_lines = src.split("\n")
if part in modules and any(isinstance(n, (ast.Import, ast.ImportFrom)) for n in tree.body):  # already a module (code
    # moved into it): add only the imports it now lacks, after its own
    if lines:
        last = max(n.end_lineno for n in tree.body if isinstance(n, (ast.Import, ast.ImportFrom)))
        open(path, "w").write("\n".join(src_lines[:last] + lines + src_lines[last:]))
    print(part, "+", lines)
    sys.exit(0)
# insert the imports after the header comments
i = 0
while i < len(src_lines) and src_lines[i].startswith("#"):
    i += 1
if src_lines[1:2] and "Runs inside agentctl's one namespace" in src_lines[1]:
    src_lines[1] = "# A real module (lib/agentctl/__init__.py MODULES): its own imports, the standard library's and MODULES' only."
body = src_lines[i:]
while body and not body[0].strip():
    body.pop(0)
out = src_lines[:i] + [""] + lines + ["", ""] + body
open(path, "w").write("\n".join(out))
place(lib, parts, set(modules) | {part})
print(part, "->", lines)
