"""agentctl's code, in parts (docs/simplification-2026-10.md, item 3).

ONE NAMESPACE. ~/bin/agentctl (the entry) runs each part below, in this order, inside its own module namespace: the
parts are the old single file cut at its section headers, so module-level code runs exactly as before, every name is
`agentctl.<name>`, and a test that patches `A.<name>` patches it for every part. A part is therefore not imported on its
own; it reads the names of the parts before it. Moving code between parts never changes behaviour as long as the
order of definitions is kept.

`import agentctl` with ~/bin/lib on sys.path loads the same namespace as a package (load_into(globals())).

REAL MODULES, BOTTOM UP (2026-11-22, the owner: "make the parts real modules"). A part listed in MODULES is a normal
module, `agentctl.<part>`, with explicit imports: of the standard library, and only of other MODULES (never a name of
a part still in the namespace -- so the modules form the bottom of the order). It is imported in its PARTS place and
its names are copied into the namespace, so the parts above it read them as before. A test's `A.<name> = x` reaches the
modules too (Namespace: an assignment on the namespace is made in every module binding that name), so patching keeps
its one-namespace meaning while the parts move over one by one.
"""
import importlib as _importlib
import os as _os
import sys as _sys
import types as _types

MODULES = ("core", "timefmt", "statefile", "moviestools", "forge", "ghapp", "classify", "store", "procs", "selfcode", "repos", "buildview", "policy", "notifyurl", "config")

PARTS = ("core", "timefmt", "statefile", "moviestools", "forge", "ghapp", "classify", "store", "procs", "selfcode",
         "repos", "buildview", "policy", "notifyurl", "config", "storetoken", "wait", "owned", "ciwhy", "branches",
         "red", "redseen", "redfixread", "redfixes", "redmove", "redretired", "reduntracked", "redstep", "redcrash",
         "redpending", "redverify", "reddispatch", "redreplay", "fixer", "fixerland", "fixersession", "fixerlife",
         "redclaim", "redview", "redearly", "redtick", "redblock", "redvault", "ask", "owner", "land", "cpu", "gov", "slots",
         "landwatch", "waiting", "queue", "queuerecheck", "queuesubmit", "queueuntil", "queueslots", "queueclose",
         "queuediff", "queuepush", "queuebatch", "remote", "batch", "changekind", "tools", "cleanup", "maint",
         "lander", "blame", "explorer", "reverts", "server", "upkeep", "watchdog", "inbox", "alerts", "recovery",
         "projector", "status", "live", "stats", "cli")


class Namespace(_types.ModuleType):
    """The namespace module: an assignment to one of its names is made in every real module binding that name too."""

    def __setattr__(self, name, value):
        super().__setattr__(name, value)
        if isinstance(value, _types.ModuleType) and value.__name__ == f"agentctl.{name}":
            return  # the import system binding a submodule on its package: no patch (policy.py defines `policy`)
        for mod in modules_loaded():
            if name in mod.__dict__:
                setattr(mod, name, value)

    def __delattr__(self, name):
        super().__delattr__(name)
        for mod in modules_loaded():
            mod.__dict__.pop(name, None)


def modules_loaded():
    return [m for m in (_sys.modules.get(f"agentctl.{p}") for p in MODULES) if m is not None]


def _package(ns, lib):
    """sys.modules["agentctl"]: the module whose namespace ns is (as a Namespace), else a bare package; either way with
    lib as its __path__, so `agentctl.<part>` imports from lib."""
    mod = next((m for m in list(_sys.modules.values()) if getattr(m, "__dict__", None) is ns), None)
    if mod is not None and type(mod) is _types.ModuleType:
        mod.__class__ = Namespace
    if mod is not None:
        _sys.modules["agentctl"] = mod
    pkg = _sys.modules.setdefault("agentctl", _types.ModuleType("agentctl"))
    pkg.__path__ = [lib]
    return pkg


def load_into(ns):
    lib = _os.path.dirname(_os.path.abspath(__file__)) if "__file__" in globals() else None
    lib = lib or _os.path.join(_os.path.dirname(_os.path.realpath(ns["__file__"])), "lib", "agentctl")
    _package(ns, lib)
    for part in PARTS:
        if part in MODULES:
            mod = _importlib.import_module(f"agentctl.{part}")
            ns.update({k: v for k, v in vars(mod).items() if not k.startswith("__")})
            continue
        path = _os.path.join(lib, f"{part}.py")
        with open(path) as f:
            exec(compile(f.read(), path, "exec"), ns)


if globals().get("__name__") == "agentctl":  # imported as a package: this module is the namespace
    load_into(globals())
