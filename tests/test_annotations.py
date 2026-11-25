"""EVERY ANNOTATION NAMES WHAT ITS MODULE BINDS (2026-11-23 05:36Z): processorlink's `await_entry(..., store: Any, ...)`
with no `from typing import Any` imported fine on the laptop's Python 3.15 (annotations evaluated lazily, PEP 649) and
raised NameError at import on ci-runner's 3.13 (evaluated at `def`): the store and reconciler never came up, auto-pin
marked 328e158 and 1d742c3 bad, and ci-runner stayed on 68f0e4e for two days with every later fix unshipped. Each
module's functions' and classes' annotations are evaluated here as an eager interpreter evaluates them."""
import ast
import os
import unittest

LIB = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "lib", "agentctl")


def unbound_annotation_names(src):
    """The names a module's eagerly-evaluated annotations (module-level and class-level defs, their parameters and
    returns, variable annotations) read BEFORE the module binds them (2026-11-25: ciwhy's `list[Run]` sat above
    `Run = dict[...]`; a name under `if TYPE_CHECKING:` is no runtime binding), builtins aside, with their lines."""
    import builtins
    tree = ast.parse(src)
    if any(isinstance(n, ast.ImportFrom) and n.module == "__future__" and any(a.name == "annotations" for a in n.names)
           for n in tree.body):
        return []
    out = []

    def check(ann, bound):
        if ann is None or isinstance(ann, ast.Constant):  # a string annotation is never evaluated at def time
            return
        out.extend((n.id, n.lineno) for n in ast.walk(ann)
                   if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load) and n.id not in bound)

    def bind(node, bound):
        for n in ast.walk(node):
            if isinstance(n, (ast.Import, ast.ImportFrom)):
                bound |= {(a.asname or a.name).split(".")[0] for a in n.names}
            elif isinstance(n, ast.Name) and isinstance(n.ctx, ast.Store):
                bound.add(n.id)

    def is_type_checking(test):
        return (isinstance(test, ast.Name) and test.id == "TYPE_CHECKING") or \
            (isinstance(test, ast.Attribute) and test.attr == "TYPE_CHECKING")

    def run(body, bound):
        for node in body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                a = node.args
                for arg in a.posonlyargs + a.args + a.kwonlyargs + [a.vararg, a.kwarg]:
                    if arg is not None:
                        check(arg.annotation, bound)
                check(node.returns, bound)
                bound.add(node.name)
            elif isinstance(node, ast.ClassDef):
                run(node.body, set(bound))
                bound.add(node.name)
            elif isinstance(node, ast.AnnAssign):
                check(node.annotation, bound)
                bind(node, bound)
            elif isinstance(node, ast.If):
                if not is_type_checking(node.test):
                    run(node.body, bound)
                run(node.orelse, bound)
            elif isinstance(node, ast.Try):
                run(node.body + node.orelse + node.finalbody, bound)
                for h in node.handlers:
                    run(h.body, bound)
            else:
                bind(node, bound)

    run(tree.body, set(dir(builtins)))
    return out


class AnnotationsTest(unittest.TestCase):
    def test_every_eagerly_evaluated_annotation_names_a_bound_name(self):
        bad = []
        for f in sorted(os.listdir(LIB)):
            if f.endswith(".py"):
                with open(os.path.join(LIB, f)) as fh:
                    bad += [f"{f}:{line}: {name}" for name, line in unbound_annotation_names(fh.read())]
        self.assertEqual(bad, [], "an annotation an eager Python (ci-runner's 3.13) raises NameError on at import")

    def test_the_reader_sees_the_2026_11_23_shape(self):
        src = "import os\nfrom typing import Optional\ndef f(store: Any, x: 'Later') -> Optional[int]:\n    pass\n"
        self.assertEqual(unbound_annotation_names(src), [("Any", 3)])

    def test_the_reader_sees_a_name_bound_below_its_use_and_one_bound_only_for_the_type_checker(self):
        late = "def f(r: list[Run]) -> None:\n    pass\nRun = dict[str, int]\n"
        self.assertEqual(unbound_annotation_names(late), [("Run", 1)])
        typing_only = "from typing import TYPE_CHECKING\nif TYPE_CHECKING:\n    from x import T\ndef f(r: T) -> None:\n    pass\n"
        self.assertEqual(unbound_annotation_names(typing_only), [("T", 4)])
        fine = "Run = dict[str, int]\nclass C:\n    def f(self, r: list[Run]) -> None:\n        pass\n"
        self.assertEqual(unbound_annotation_names(fine), [])


SERVER_PYTHON = "3.13"   # ci-runner's interpreter (`ssh ci-runner python3 --version`, 2026-11-25: 3.13.17)


class ServerPythonInCiTest(unittest.TestCase):
    """THE AUTO-PIN READS tests.yml (2026-11-25, the owner): a static reader catches one shape of a 3.13-only failure;
    the suite on ci-runner's own Python catches them all. Every shard runs under SERVER_PYTHON as well as the newest."""

    def test_every_suite_shard_runs_on_ci_runners_python(self):
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        with open(os.path.join(root, ".github", "workflows", "tests.yml")) as f:
            wf = f.read()
        suite = wf.split("\n  suite:", 1)[1].split("\n  sim-gate:", 1)[0]
        import re
        versions = re.search(r"python:\s*\[([^\]]*)\]", suite)
        self.assertIsNotNone(versions, "the suite job's matrix names no python versions")
        self.assertIn(f'"{SERVER_PYTHON}"', versions.group(1))
        self.assertIn("python-version: ${{ matrix.python }}", suite)
