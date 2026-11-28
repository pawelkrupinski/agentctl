"""NO SERVICE LOCATORS (CLAUDE.md, "Design"): collaborators are constructed in the ONE composition root (cli) and passed
through constructors and parameters; no module keeps a collaborator in a global cell that a `wire_*` function fills and
a getter reads. This test finds every `def wire_*` and holds them to FROZEN, the ones still to be removed: a NEW
one fails, and so does a name left in FROZEN after its code is gone (the list only falls). Each one left has its
AMBIENT READERS -- the modules and functions calling its getter instead of taking the collaborator (a `ctx.clock`,
`ctx.runner`, `ctx.inbox`, or a parameter) -- held to READER_CEILINGS, which only fall: a new ambient reader fails, and
so does a ceiling left above its count (lower it). A module-level cell some
other function writes is the design rules' `state` row (quality/design-baseline.tsv), frozen there."""
import ast, os, sys, unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from test_agentctl import A  # noqa: E402,F401  (the harness)

LIB = os.path.join(os.path.dirname(HERE), "lib", "agentctl")
# the composition root is the entry `cli` and the modules it splits its wiring into (the fan-out split, 2026-11-28)
COMPOSITION_ROOT = ("cli", "cliprocess", "cliwiring")

# module.function -> what removes it (the work list; R5b, 2026-11-27: each needs its collaborator threaded from cli through
# the modules that read it today, which reach the landing and queue lanes)
FROZEN = {
    "clock.wire_clock": "281 `epoch_now()` readers take the clock (the clock migration)",
    "procrunner.wire_runner": "72 `process_runner()` readers take the runner",
    "inboxentry.wire_inbox_writer": "36 modules that write an inbox entry take the writer",
}

# locator -> (its module, the getters that read its cell)
GETTERS = {
    "clock.wire_clock": ("clock", ("epoch_now",)),
    "procrunner.wire_runner": ("procrunner", ("process_runner",)),
    "inboxentry.wire_inbox_writer": ("inboxentry", ("inbox_write", "write_inbox_entry")),
}

# locator -> (modules, functions) reading it ambiently: frozen ceilings, which only fall (2026-11-27: the Context fold,
# docs/design/contexts.md -- every reader holding a `ctx` reads its field instead)
READER_CEILINGS = {
    "clock.wire_clock": (86, 125),
    "procrunner.wire_runner": (39, 54),
    "inboxentry.wire_inbox_writer": (31, 39),
}


def ambient_readers(sources, module, getters):
    """{(module, top-level function or `<module>`)} of every reference to one of `getters` in `sources` ({module:
    source}), outside the getters' own `module` and the composition root; the imports are no read."""
    found = set()
    for name, source in sorted(sources.items()):
        if name in (module, *COMPOSITION_ROOT):
            continue
        for top in ast.parse(source).body:
            if isinstance(top, (ast.Import, ast.ImportFrom)):
                continue
            where = top.name if isinstance(top, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) else "<module>"
            if any(isinstance(n, ast.Name) and n.id in getters for n in ast.walk(top)):
                found.add((name, where))
    return found


def library_sources():
    return {f[:-3]: open(os.path.join(LIB, f)).read() for f in os.listdir(LIB) if f.endswith(".py")}


def reader_counts(sources):
    """{locator: (modules, functions)} reading it ambiently."""
    out = {}
    for locator, (module, getters) in GETTERS.items():
        readers = ambient_readers(sources, module, getters)
        out[locator] = (len({m for m, _ in readers}), len(readers))
    return out


def locators():
    found = {}
    for f in sorted(os.listdir(LIB)):
        if not f.endswith(".py") or f[:-3] in COMPOSITION_ROOT:
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
            if f.endswith(".py") and f[:-3] not in COMPOSITION_ROOT:
                for n in ast.walk(ast.parse(open(os.path.join(LIB, f)).read())):
                    if isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id in names:
                        callers.add(f"{f[:-3]} calls {n.func.id}")
        self.assertEqual(sorted(callers), [])

    def test_the_ambient_readers_of_each_locator_only_fall(self):
        self.assertEqual(sorted(GETTERS), sorted(FROZEN), "every locator left has its getters and a ceiling")
        got = reader_counts(library_sources())
        for locator, (modules, functions) in READER_CEILINGS.items():
            now = got[locator]
            self.assertLessEqual(now[0], modules, f"{locator}: a new module reads it ambiently: take the collaborator (ctx)")
            self.assertLessEqual(now[1], functions, f"{locator}: a new function reads it ambiently: take the collaborator (ctx)")
            self.assertEqual(now, (modules, functions), f"{locator}: its readers fell to {now}: lower READER_CEILINGS")

    def test_a_new_ambient_reader_is_counted(self):
        sources = library_sources()
        before = reader_counts(sources)
        sources["newreader"] = ("from agentctl.clock import epoch_now\nfrom agentctl.inboxentry import inbox_write\n"
                                "from agentctl.procrunner import process_runner\n\n\ndef reads():\n"
                                "    return epoch_now(), process_runner(), inbox_write\n")
        after = reader_counts(sources)
        for locator, (modules, functions) in before.items():
            self.assertEqual(after[locator], (modules + 1, functions + 1), locator)


if __name__ == "__main__":
    unittest.main()
