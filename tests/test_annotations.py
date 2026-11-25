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
    returns, class-body variable annotations) read that the module never binds and builtins lack, with their lines."""
    import builtins
    tree = ast.parse(src)
    bound = set(dir(builtins))
    for node in ast.walk(tree):
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            bound |= {(a.asname or a.name).split(".")[0] for a in node.names}
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            bound.add(node.name)
        elif isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store):
            bound.add(node.id)
    if any(isinstance(n, ast.ImportFrom) and n.module == "__future__" and any(a.name == "annotations" for a in n.names)
           for n in tree.body):
        return []
    anns = []

    def scope(body):
        for node in body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                a = node.args
                for arg in a.posonlyargs + a.args + a.kwonlyargs + [a.vararg, a.kwarg]:
                    if arg is not None and arg.annotation is not None:
                        anns.append(arg.annotation)
                if node.returns is not None:
                    anns.append(node.returns)
            elif isinstance(node, ast.ClassDef):
                anns.extend(n.annotation for n in node.body if isinstance(n, ast.AnnAssign))
                scope(node.body)
            elif isinstance(node, (ast.If, ast.Try)):
                scope(node.body + node.orelse + [h for x in getattr(node, "handlers", []) for h in x.body])

    scope(tree.body)
    out = []
    for ann in anns:
        if isinstance(ann, ast.Constant):  # a string annotation is never evaluated at def time
            continue
        for n in ast.walk(ann):
            if isinstance(n, ast.Name) and n.id not in bound:
                out.append((n.id, n.lineno))
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
