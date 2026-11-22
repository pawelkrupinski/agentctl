#!/usr/bin/env python3
"""Pure-move proof over two lib/ trees, loaded: the same names in the namespace, and every function's code (bytecode,
names, variables, constants, nested code alike, line numbers aside) unchanged.

  python3 devtools/samecode.py <old lib> <new lib>       (lib: the directory holding agentctl/)   exit 0 the same"""
import dis
import os
import sys
import types


def load(lib):
    init = os.path.join(lib, "agentctl", "__init__.py")
    ns = {"__name__": "agentctl", "__file__": init}
    saved = {k: v for k, v in sys.modules.items() if k == "agentctl" or k.startswith("agentctl.")}
    for k in saved:
        del sys.modules[k]
    exec(compile(open(init).read(), init, "exec"), ns)
    for k in [k for k in sys.modules if k == "agentctl" or k.startswith("agentctl.")]:
        del sys.modules[k]
    sys.modules.update(saved)
    return ns


def instructions(code):
    """The code's instructions, opname and argument, with what only the compiler's knowledge of its module changes left
    out: a real module's `import time` makes `time.time()` a plain attribute load and a PUSH_NULL where the namespace
    compiled a method load, and every jump after it moves by one -- which can push a jump offset past 255 or back, so
    an EXTENDED_ARG prefix comes or goes (dis already folds its value into the instruction it prefixes)."""
    out = []
    for i in dis.get_instructions(code):
        if i.opname in ("PUSH_NULL", "EXTENDED_ARG") or i.opname in dis.hasjrel or i.opcode in dis.hasjump:
            continue
        if i.opname == "STORE_NAME" and i.argval == "__firstlineno__":  # a class body's own line: positions aside
            out.pop()
            continue
        op = i.opname.replace("_BORROW", "")  # a borrowed reference or not: the compiler's choice by context alone
        out.append((op, i.argval if not isinstance(i.argval, types.CodeType) else None))
    return out


def constant(x):
    """A constant as compared: a frozenset by its members (its repr follows its iteration order, which insertion and
    hash collisions decide), any other by its repr."""
    return ("frozenset", sorted(map(repr, x))) if isinstance(x, frozenset) else repr(x)


def signature(code):
    return (instructions(code), code.co_names, code.co_varnames,
            tuple(signature(x) if isinstance(x, types.CodeType) else constant(x) for x in code.co_consts
                  if "__firstlineno__" not in code.co_names or x != code.co_firstlineno))


def main(argv):
    old, new = load(argv[0]), load(argv[1])
    if set(old) != set(new):
        print("names differ:", sorted(set(old) ^ set(new))[:20])
        return 1
    diff = [k for k in old if isinstance(old[k], types.FunctionType) and isinstance(new[k], types.FunctionType)
            and signature(old[k].__code__) != signature(new[k].__code__)]
    print("names", len(old), "functions differing:", diff)
    return 1 if diff else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
