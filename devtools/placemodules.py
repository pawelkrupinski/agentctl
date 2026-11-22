#!/usr/bin/env python3
"""Put the real modules (MODULES) in IMPORT ORDER, each after every module it imports (otherwise in their present
order), and at the front of PARTS: a module loads before every part reading it.

  python3 devtools/placemodules.py <lib/agentctl>"""
import sys

from partlist import import_order, parts_and_modules, place


def main(argv):
    lib = argv[0]
    parts, modules = parts_and_modules(lib)
    order = import_order(lib, modules)
    place(lib, order + [p for p in parts if p not in modules], set(modules))
    print("modules first:", ", ".join(order))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
