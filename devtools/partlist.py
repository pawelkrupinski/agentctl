"""The part list of agentctl's one namespace: PARTS (every part, in load order) and MODULES (the parts that are real
modules), read from lib/agentctl/__init__.py without running its loader. Shared by the devtools."""
import os
import re


def init_path(lib):
    return os.path.join(lib, "__init__.py")


def parts_and_modules(lib):
    ns = {}
    head = open(init_path(lib)).read().split("class Namespace")[0].split("def load_into")[0]
    exec(head, ns)
    return list(ns["PARTS"]), list(ns.get("MODULES", ()))


def part_path(lib, part):
    return os.path.join(lib, f"{part}.py")


LISTS = re.compile(r"MODULES = \([^)]*\)\s*\n(?:NAMESPACE = \([^)]*\)\s*\n)?\s*PARTS = (?:\([^)]*\)|MODULES \+ NAMESPACE)")


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


def write_lists(lib, modules, namespace):
    """__init__.py's part lists: MODULES (the real modules, loaded first), NAMESPACE (the namespace parts, in load
    order) and PARTS = MODULES + NAMESPACE -- each part named once (two long lists of the same names are a clone)."""
    s = open(init_path(lib)).read()
    m = LISTS.search(s)
    if not m:
        raise SystemExit("__init__.py: no MODULES / PARTS lists")
    text = wrapped("MODULES", modules) + "\n\n" + wrapped("NAMESPACE", namespace) + "\n\nPARTS = MODULES + NAMESPACE"
    open(init_path(lib), "w").write(s[:m.start()] + text + s[m.end():])


def place(lib, parts, modules):
    """Write the lists from a PARTS order: the modules first (in that order), then the namespace parts."""
    write_lists(lib, [p for p in parts if p in modules], [p for p in parts if p not in modules])
