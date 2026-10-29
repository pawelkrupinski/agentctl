"""agentctl's code, in parts (docs/simplification-2026-10.md, item 3).

ONE NAMESPACE. ~/bin/agentctl (the entry) runs each part below, in this order, inside its own module namespace: the
parts are the old single file cut at its section headers, so module-level code runs exactly as before, every name is
`agentctl.<name>`, and a test that patches `A.<name>` patches it for every part. A part is therefore not imported on its
own; it reads the names of the parts before it. Moving code between parts never changes behaviour as long as the
order of definitions is kept.

`import agentctl` with ~/bin/lib on sys.path loads the same namespace as a package (load_into(globals())).
"""
import os as _os

PARTS = ("inbox", "recovery", "status", "live", "cli")


def load_into(ns):
    lib = _os.path.dirname(_os.path.abspath(__file__)) if "__file__" in globals() else None
    lib = lib or _os.path.join(_os.path.dirname(_os.path.realpath(ns["__file__"])), "lib", "agentctl")
    for part in PARTS:
        path = _os.path.join(lib, f"{part}.py")
        with open(path) as f:
            exec(compile(f.read(), path, "exec"), ns)


if globals().get("__name__") == "agentctl":  # imported as a package: this module is the namespace
    load_into(globals())
