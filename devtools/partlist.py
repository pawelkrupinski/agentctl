"""The part list of agentctl's one namespace: PARTS (every part, in load order) and MODULES (the parts that are real
modules), read from lib/agentctl/__init__.py without running its loader. Shared by the devtools."""
import os


def init_path(lib):
    return os.path.join(lib, "__init__.py")


def parts_and_modules(lib):
    ns = {}
    head = open(init_path(lib)).read().split("class Namespace")[0].split("def load_into")[0]
    exec(head, ns)
    return list(ns["PARTS"]), list(ns.get("MODULES", ()))


def part_path(lib, part):
    return os.path.join(lib, f"{part}.py")
