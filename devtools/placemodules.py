#!/usr/bin/env python3
"""Put every real module (MODULES) at the front of PARTS, in MODULES' order of first need: a module imports only
modules, so it loads before any namespace part, and a namespace part's module-level code sees all of them.

  python3 devtools/placemodules.py <lib/agentctl>"""
import sys

from partlist import parts_and_modules, place


def main(argv):
    lib = argv[0]
    parts, modules = parts_and_modules(lib)
    place(lib, parts, modules)
    print("modules first:", ", ".join(p for p in parts if p in modules))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
