#!/usr/bin/env python3
"""Put every real module (MODULES) at the front of PARTS, in MODULES' order of first need: a module imports only
modules, so it loads before any namespace part, and a namespace part's module-level code sees all of them.

  python3 devtools/placemodules.py <lib/agentctl>"""
import re
import sys

from partlist import init_path, parts_and_modules


def main(argv):
    lib = argv[0]
    parts, modules = parts_and_modules(lib)
    order = [p for p in parts if p in modules] + [p for p in parts if p not in modules]
    s = open(init_path(lib)).read()
    m = re.search(r"PARTS = \(([^)]*)\)", s)
    body = ", ".join(f'"{p}"' for p in order)
    lines, cur = [], ""
    for word in body.split(" "):
        if len(cur) + len(word) + 1 > 110:
            lines.append(cur.rstrip())
            cur = ""
        cur += word + " "
    lines.append(cur.rstrip())
    s = s[:m.start()] + "PARTS = (" + "\n         ".join(lines) + ")" + s[m.end():]
    open(init_path(lib), "w").write(s)
    print("modules first:", ", ".join(p for p in order if p in modules))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
