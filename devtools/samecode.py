#!/usr/bin/env python3
"""Pure-move proof over two lib/ trees, loaded: the same names in the namespace, and every function's code (bytecode,
names, variables, constants, nested code alike, line numbers aside) unchanged.

  python3 devtools/samecode.py <old lib> <new lib>       (lib: the directory holding agentctl/)   exit 0 the same"""
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


def signature(code):
    return (code.co_code, code.co_names, code.co_varnames,
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
