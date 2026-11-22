# agentctl, module `selfcode`: agentctl's own code -- the entry's path (TOOL_PATH), its files, and their version (a hash of them all).
# A real module (lib/agentctl/__init__.py MODULES): its own imports, the standard library's and MODULES' only.

import os


# The entry, `agentctl`, beside lib/: this file is lib/agentctl/selfcode.py (the entry and the package both resolve
# through realpath, so a symlinked ~/bin finds the same tree).
TOOL_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.realpath(__file__)))), "agentctl")


def code_files(path: str = TOOL_PATH) -> list[tuple[str, str]]:
    """agentctl's code: the entry script and every part of its package beside it (lib/agentctl/*.py), as
    (relative path, absolute path), in a fixed order."""
    root = os.path.dirname(path)
    lib = os.path.join(root, "lib", "agentctl")
    parts = sorted(f for f in os.listdir(lib) if f.endswith(".py")) if os.path.isdir(lib) else []
    return [(os.path.basename(path), path)] + [(f"lib/agentctl/{f}", os.path.join(lib, f)) for f in parts]


def code_version(path: str = TOOL_PATH) -> str:
    """A hash of ALL of agentctl's code (code_files): a change to any part is new code."""
    import hashlib
    h = hashlib.sha256()
    try:
        for rel, p in code_files(path):
            with open(p, "rb") as f:
                h.update(rel.encode() + b"\0" + f.read() + b"\0")
    except OSError:
        return ""
    return h.hexdigest()[:16]
