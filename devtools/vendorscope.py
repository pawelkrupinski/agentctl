#!/usr/bin/env python3
"""devtools/vendorscope.py RANGE: which tests a push must run, decided from its own diff (hooks/pre-push reads it).

A push that changes ONLY vendored copies (devtools/design_rules/*, devtools/lib/*) and their table VENDORED.tsv is a
mechanical sync (devtools/vendorsync.py): the copies are byte-equal to movies' files, so what can break is what imports
them. Its scope is `test_vendored` and the tests that import a changed module, plus the simulator gate only when the
simulator imports one (2026-11-28: a full pre-push suite at load 25-45 timed out twice on a correct 5-file sync and the
condition stayed open 3 h). Any other push is `full`: the whole suite, e2e and gate.

Prints `full`, or `scoped sim=<0|1> <test module>...` (the patterns tests/run.py takes)."""
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(ROOT, "lib"))
import vendor  # noqa: E402
import tool_git  # noqa: E402  (devtools/lib: vendor.py put it on the path)
from agentctl.statefile import read_text_or  # noqa: E402

ALWAYS = ("test_vendored", "test_vendor_drift", "test_design_rules", "test_design_rules_mutation", "test_quality_ratchet")
SIMULATORS = ("queue_sim.py", "sim_gate.py")


def vendored_only(changed, copies):
    """A non-empty change of vendored copies and the table alone."""
    return bool(changed) and set(changed) <= set(copies) | {vendor.TABLE}


def imports_any(source, stems):
    """The source imports one of the modules (`import x`, `from x import`) or names the vendored package."""
    pattern = r"^\s*(?:import|from)\s+(?:" + "|".join(re.escape(s) for s in stems) + r")\b|\bdesign_rules\b"
    return bool(re.search(pattern, source, re.M))


def scope(changed, copies, sources):
    """('full', ...) or ('scoped', simulator_reached, test modules): sources maps a tests/ file name to its text."""
    if not vendored_only(changed, copies):
        return ("full", False, [])
    stems = sorted({os.path.splitext(os.path.basename(c))[0] for c in changed if c.endswith(".py")})
    tests = {os.path.splitext(n)[0] for n, text in sources.items() if n.startswith("test_") and imports_any(text, stems)}
    sim = any(imports_any(sources.get(n, ""), stems) for n in SIMULATORS)
    return ("scoped", sim, sorted(tests | set(ALWAYS)))


def read_tests(root):
    """The text of each tracked tests/*.py by file name."""
    names = tool_git.Git.here(root).lines("ls-files", "tests/*.py")
    return {os.path.basename(n): read_text_or(os.path.join(root, n), "") for n in names}


def changed_files(root, rng):
    return tool_git.Git.here(root).lines("diff", "--name-only", rng)


def main(argv):
    rng = argv[0] if argv else ""
    if not rng or rng.startswith("0000000"):
        print("full")
        return 0
    copies = [c for c, _s, _d in vendor.read_table(ROOT)]
    kind, sim, tests = scope(changed_files(ROOT, rng), copies, read_tests(ROOT))
    print("full" if kind == "full" else f"scoped sim={int(sim)} " + " ".join(tests))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
