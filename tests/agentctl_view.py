"""THE TESTS' VIEW OF AGENTCTL: one object, `A`, over every module of the package (lib/agentctl MODULES).

agentctl's modules import each other explicitly; nothing in production shares a namespace. The tests read and patch
agentctl as one, as they did when it was one file: `A.<name>` is the name of whichever module defines it, and
`A.<name> = fake` (or mock.patch.object(A, ...)) replaces it in EVERY module binding it -- the module defining it
and each one importing it -- so a fake reaches the code under test wherever it reads the name from. `del A.<name>`
drops it from them all. Each module's names are copied onto the package once, when the view loads (a later module's
value of a name winning: they are the same object but for the MACHINES registry, extended in place, and equal aliases).

The clock is passed in from the composition root (lib/agentctl/clock.py: a test hands a tests/fake_clock.py FakeClock
to main, land, the checkers, the ticks, the watch and the processor's loop, or a store on a fake clock). Until the
process runner and the store are passed in too, this view is how a test fakes the rest (`A.subprocess.run = ...`,
tests/harness.py's proxy; `A.gh_json = ...`); it is the tests' only, never imported by agentctl itself.

  from agentctl_view import load; A = load(<path of an agentctl entry>)     (a child process's way in, too)
"""
import importlib
import os
import sys
import types


class View(types.ModuleType):
    """The package, as the tests see it: an assignment to one of its names is made in every module binding it."""

    def __setattr__(self, name, value):
        super().__setattr__(name, value)
        if isinstance(value, types.ModuleType) and value.__name__ == f"agentctl.{name}":
            return  # the import system binding a submodule on its package: no patch (policy.py defines `policy`)
        for mod in modules_loaded(self):
            if name in mod.__dict__:
                setattr(mod, name, value)

    def __delattr__(self, name):
        super().__delattr__(name)
        for mod in modules_loaded(self):
            mod.__dict__.pop(name, None)


def modules_loaded(pkg):
    return [m for m in (sys.modules.get(f"agentctl.{p}") for p in pkg.__dict__.get("MODULES", ())) if m is not None]


def load(tool):
    """The package next to the entry `tool` (<tree>/agentctl -> <tree>/lib/agentctl), every module imported (cli, the
    composition root, wires them as the entry does), as a View."""
    lib = os.path.join(os.path.dirname(os.path.realpath(tool)), "lib")
    if lib not in sys.path:
        sys.path.insert(0, lib)
    pkg = importlib.import_module("agentctl")
    for m in pkg.MODULES:
        mod = importlib.import_module(f"agentctl.{m}")
        pkg.__dict__.update({k: v for k, v in vars(mod).items() if not k.startswith("__")})
    pkg.__class__ = View
    return pkg
