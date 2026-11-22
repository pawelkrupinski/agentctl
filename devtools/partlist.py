"""agentctl's module list: MODULES (every module, in import order), read from lib/agentctl/__init__.py without
importing it, and written back. Shared by the devtools."""
import ast
import os
import re


def init_path(lib):
    return os.path.join(lib, "__init__.py")


def module_list(lib):
    ns = {}
    exec(open(init_path(lib)).read(), ns)
    return list(ns["MODULES"])


def part_path(lib, part):
    return os.path.join(lib, f"{part}.py")


LIST = re.compile(r"MODULES = \([^)]*\)")


def wrapped(name, names):
    """`NAME = ("a", "b", ...)`, wrapped under 118 columns."""
    lines, cur = [], ""
    for word in (f'"{n}",' for n in names):
        if cur and len(cur) + len(word) + len(name) + 5 > 118:
            lines.append(cur.rstrip())
            cur = ""
        cur += word + " "
    lines.append(cur.rstrip().rstrip(","))
    return f"{name} = (" + ("\n" + " " * (len(name) + 4)).join(lines) + ("," if len(names) == 1 else "") + ")"


def write_modules(lib, names):
    """__init__.py's MODULES, in this order."""
    s = open(init_path(lib)).read()
    m = LIST.search(s)
    if not m:
        raise SystemExit("__init__.py: no MODULES list")
    open(init_path(lib), "w").write(s[:m.start()] + wrapped("MODULES", names) + s[m.end():])


def module_imports(lib, module):
    """The modules a module imports at its top level (`from agentctl.<m> import ...`)."""
    tree = ast.parse(open(part_path(lib, module)).read())
    return {n.module.split(".")[1] for n in tree.body
            if isinstance(n, ast.ImportFrom) and (n.module or "").startswith("agentctl.")}


def import_order(lib, modules):
    """The modules, each after every module it imports, otherwise in their given order (Kahn's, the earliest ready
    first); refuses an import cycle, naming its modules."""
    deps = {m: module_imports(lib, m) & set(modules) - {m} for m in modules}
    out, done = [], set()
    while len(out) < len(modules):
        ready = next((m for m in modules if m not in done and deps[m] <= done), None)
        if ready is None:
            raise SystemExit(f"an import cycle among: {', '.join(m for m in modules if m not in done)}")
        out.append(ready)
        done.add(ready)
    return out


def place(lib, names):
    """Write MODULES: these modules, in import order."""
    write_modules(lib, import_order(lib, list(names)))


def import_lines(module, names):
    """`from <module> import a, b`, parenthesised and wrapped under 118 columns when longer: the one shape every devtool
    writes an import in."""
    text = f"from {module} import {', '.join(names)}"
    if len(text) <= 118:
        return [text]
    body, cur = [], "    "
    for x in names:
        if len(cur) + len(x) + 2 > 116:
            body.append(cur.rstrip())
            cur = "    "
        cur += x + ", "
    body.append(cur.rstrip().rstrip(","))
    return [f"from {module} import ("] + body[:-1] + [body[-1] + ")"]


def defined_names(tree):
    """The names a module's top level DEFINES (def, class, assignment targets), its imports aside."""
    out = set()
    for n in tree.body:
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            out.add(n.name)
        elif isinstance(n, (ast.Assign, ast.AnnAssign)):
            for t in n.targets if isinstance(n, ast.Assign) else [n.target]:
                # TABLE["k"] = ... registers into TABLE (a Load of it): it defines nothing
                out |= {x.id for x in ast.walk(t) if isinstance(x, ast.Name) and isinstance(x.ctx, ast.Store)}
    return out
