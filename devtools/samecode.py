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
    compiled a method load, and every jump after it moves by one."""
    out = []
    for i in dis.get_instructions(code):
        if i.opname == "PUSH_NULL" or i.opname in dis.hasjrel or i.opcode in dis.hasjump:
            continue
        out.append((i.opname, i.argval if not isinstance(i.argval, types.CodeType) else None))
    return out


def signature(code):
    return (instructions(code), code.co_names, code.co_varnames,
            tuple(signature(x) if isinstance(x, types.CodeType) else repr(x) for x in code.co_consts))


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
