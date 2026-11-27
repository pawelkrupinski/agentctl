"""NO SERVICE LOCATORS (CLAUDE.md, "Design"): collaborators are constructed in the ONE composition root (cli) and passed
through constructors and parameters; no module keeps a collaborator in a global cell that a `wire_*` function fills and
a getter reads. This test finds every `def wire_*` and holds them to FROZEN, the ones still to be removed: a NEW
one fails, and so does a name left in FROZEN after its code is gone (the list only falls). A module-level cell some
other function writes is the design rules' `state` row (quality/design-baseline.tsv), frozen there."""
import ast, os, sys, unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from test_agentctl import A  # noqa: E402,F401  (the harness)

LIB = os.path.join(os.path.dirname(HERE), "lib", "agentctl")
COMPOSITION_ROOT = "cli"

# module.function -> what removes it (the work list; R5b, 2026-11-27: each needs its collaborator threaded from cli through
# the modules that read it today, which reach the landing and queue lanes)
FROZEN = {
    "clock.wire_clock": "281 `epoch_now()` readers take the clock (the clock migration)",
    "procrunner.wire_runner": "72 `process_runner()` readers take the runner",
    "ciruns.wire_ci_runs": "16 red / ci-why / landing modules take the CiRuns reader",
    "inboxentry.wire_inbox_writer": "36 modules that write an inbox entry take the writer",
}


def locators():
    found = {}
    for f in sorted(os.listdir(LIB)):
        if not f.endswith(".py") or f[:-3] == COMPOSITION_ROOT:
            continue
        tree = ast.parse(open(os.path.join(LIB, f)).read())
        for fn in tree.body:
            if isinstance(fn, ast.FunctionDef) and (fn.name.startswith("wire_")):
                found[f"{f[:-3]}.{fn.name}"] = fn.name
    return found


class NoServiceLocators(unittest.TestCase):
    def test_no_new_locator_and_none_left_in_the_list_after_its_removal(self):
        found = set(locators())
        self.assertEqual(sorted(found - set(FROZEN)), [], "a new service locator: pass the collaborator instead")
        self.assertEqual(sorted(set(FROZEN) - found), [], "removed: delete its row from FROZEN")

    def test_only_the_composition_root_calls_a_wire_function(self):
        names = {k.split(".")[1] for k in FROZEN if k.split(".")[1].startswith("wire_")}
        callers = set()
        for f in os.listdir(LIB):
            if f.endswith(".py") and f[:-3] != COMPOSITION_ROOT:
                for n in ast.walk(ast.parse(open(os.path.join(LIB, f)).read())):
                    if isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id in names:
                        callers.add(f"{f[:-3]} calls {n.func.id}")
        self.assertEqual(sorted(callers), [])


if __name__ == "__main__":
    unittest.main()
