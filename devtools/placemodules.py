#!/usr/bin/env python3
"""Put MODULES in IMPORT ORDER: each module after every module it imports, otherwise in their present order.

  python3 devtools/placemodules.py <lib/agentctl>"""
import sys

from partlist import import_order, module_list, write_modules


def main(argv):
    lib = argv[0]
    order = import_order(lib, module_list(lib))
    write_modules(lib, order)
    print("modules:", ", ".join(order))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
