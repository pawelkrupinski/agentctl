"""The ratchets (quality/ratchet.py, CLAUDE.md "The ratchets"): a seeded violation of each refuses, a frozen row may
only fall, and a baseline grows only in a commit carrying its trailer. Tool-free: the tool-measured kinds (cognitive,
cyclomatic, mypy-errors) are fed as rows here; the hook's `ratchet.py selftest` seeds them through the real tools."""
import contextlib, io, os, subprocess, sys, tempfile, textwrap, unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from test_agentctl import A  # noqa: E402,F401  (the harness: HOME, PATH, git config)
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "quality"))
import measure, ratchet, typecheck  # noqa: E402


def tree(parts):
    root = tempfile.mkdtemp()
    lib = os.path.join(root, "lib", "agentctl")
    os.makedirs(lib)
    os.makedirs(os.path.join(root, "quality"))
    with open(os.path.join(lib, "__init__.py"), "w") as f:
        f.write(f"PARTS = {tuple(parts)!r}\n")
    for name, text in parts.items():
        with open(os.path.join(lib, f"{name}.py"), "w") as f:
            f.write(textwrap.dedent(text))
    return root


def quiet_check(root, ranges=(), type_rows=()):
    with contextlib.redirect_stdout(io.StringIO()) as out:
        rc = ratchet.check(root, ranges, analysers={}, type_rows=list(type_rows))
    return rc, out.getvalue()


LONG = "def long_one():\n" + "".join(f"    x{i} = {i}\n" for i in range(55)) + "    return x0\n"


class SeededViolationTest(unittest.TestCase):
    def test_a_new_function_past_the_length_limit_refuses(self):
        rc, out = quiet_check(tree({"a": LONG}))
        self.assertEqual(rc, 1)
        self.assertIn("NEW   method-lines long_one = 57", out)

    def test_nested_defs_clones_fan_out_and_cycles_refuse(self):
        nested = "def outer():\n    def a():\n        def b():\n            def c():\n                pass\n"
        clone = "".join(f"    v{i} = alpha({i}, beta) + gamma[{i}]\n" for i in range(8))
        parts = {"a": nested + "\n\ndef c1():\n" + clone + "\n\ndef c2():\n" + clone
                 + "\n\ndef many():\n" + "".join(f"    p{i}()\n" for i in range(10))}
        parts.update({f"p{i}": f"def p{i}():\n    return many()\n" for i in range(10)})
        rc, out = quiet_check(tree(parts))
        self.assertEqual(rc, 1)
        for line in ("NEW   inner-defs outer = 3", "NEW   copies a = 2", "NEW   fan-out a = 10", "NEW   cycle p3 = 1"):
            self.assertIn(line, out)

    def test_a_type_error_row_past_its_baseline_refuses(self):
        rc, out = quiet_check(tree({"a": "x = 1\n"}), type_rows=[("mypy-errors", "a", 3)])
        self.assertEqual(rc, 1)
        self.assertIn("types: NEW   mypy-errors a = 3", out)


class FoundationsTest(unittest.TestCase):
    def test_a_leaf_helper_part_is_no_collaborator(self):
        parts = {"leaf": "def helper():\n    return 1\n",
                 "a": "def many():\n" + "".join(f"    p{i}()\n" for i in range(9)) + "    helper()\n"}
        parts.update({f"p{i}": f"def p{i}():\n    return many()\n" for i in range(9)})
        rc, out = quiet_check(tree(parts))
        self.assertNotIn("fan-out a", out)  # 9 collaborators and the leaf
        self.assertEqual(measure.foundations({"x": set(), "y": {"x"}, "z": {"y", "w"}, "w": {"z"}}), {"x", "y"})


class FrozenRowsOnlyFallTest(unittest.TestCase):
    def test_rise_refuses_fall_passes_and_update_records_the_fall(self):
        root = tree({"a": LONG})
        with contextlib.redirect_stdout(io.StringIO()):
            ratchet.update(root, analysers={}, type_rows=[])
        self.assertEqual(quiet_check(root)[0], 0)
        path = os.path.join(root, "lib", "agentctl", "a.py")
        with open(path, "a") as f:
            f.write("".join(f"x{i} = 0\n" for i in range(3)))
        self.assertEqual(quiet_check(root)[0], 0)  # module-level lines are no function's
        with open(path, "w") as f:
            f.write(LONG.replace("    return x0\n", "    y = 1\n    return x0\n"))
        rc, out = quiet_check(root)
        self.assertEqual(rc, 1)
        self.assertIn("ROSE  method-lines long_one 57 -> 58", out)
        with open(path, "w") as f:
            f.write("def long_one():\n    return 0\n")
        rc, out = quiet_check(root)
        self.assertEqual(rc, 0)
        self.assertIn("fell  method-lines long_one 57 -> 50", out)

    def test_a_moved_function_keeps_its_row(self):
        root = tree({"a": LONG, "b": "y = 1\n"})
        with contextlib.redirect_stdout(io.StringIO()):
            ratchet.update(root, analysers={}, type_rows=[])
        lib = os.path.join(root, "lib", "agentctl")
        open(os.path.join(lib, "a.py"), "w").write("y0 = 1\n")
        open(os.path.join(lib, "b.py"), "w").write(LONG)
        self.assertEqual(quiet_check(root)[0], 0)


class TrailerAttributionTest(unittest.TestCase):
    def git(self, root, *args):
        return subprocess.run(["git", "-C", root, *args], capture_output=True, text=True, check=True).stdout.strip()

    def test_a_grown_baseline_needs_its_kinds_trailer_in_the_commit_growing_it(self):
        root = tree({"a": "def short():\n    return 1\n"})
        with contextlib.redirect_stdout(io.StringIO()):
            ratchet.update(root, analysers={}, type_rows=[])
        self.git(root, "init", "-q")
        self.git(root, "add", "-A")
        self.git(root, "commit", "-qm", "base")
        base = self.git(root, "rev-parse", "HEAD")
        open(os.path.join(root, "lib", "agentctl", "a.py"), "w").write(LONG)
        with contextlib.redirect_stdout(io.StringIO()):
            ratchet.update(root, analysers={}, type_rows=[])
        self.git(root, "commit", "-qam", "grow without a trailer")
        rc, out = quiet_check(root, [f"{base}..HEAD"])
        self.assertEqual(rc, 1)
        self.assertIn("without a `Length: <why>` trailer", out)
        self.git(root, "commit", "-q", "--amend", "-m", "grow\n\nLength: a seeded long function, kept as it is")
        self.assertEqual(quiet_check(root, [f"{base}..HEAD"])[0], 0)


class TypecheckMappingTest(unittest.TestCase):
    def test_errors_map_back_to_their_part_and_line(self):
        root = tree({"a": "x = 1\ny = 2\n", "b": "def f(v):\n    return v\n"})
        with tempfile.TemporaryDirectory() as d:
            starts = typecheck.concatenate(root, os.path.join(d, "ns.py"))
        out = ("ns.py:2: error: one  [x]\nns.py:3: error: Function is missing a type annotation  [no-untyped-def]\n"
               "ns.py:4: note: ignored\n")
        errors = typecheck.parse(out, starts)
        self.assertEqual([(p, line) for p, line, _ in errors], [("a", 2), ("b", 1)])
        self.assertEqual(typecheck.rows(errors), [("mypy-errors", "a", 1), ("mypy-errors", "b", 1)])


class RealModuleImportsTest(unittest.TestCase):
    def test_a_real_modules_sibling_imports_are_blanked_with_their_lines_kept(self):
        root = tree({"a": "x = 1\n", "b": "from agentctl.a import (x,\n    y)\nfrom agentctl.a import x\nv = x\n"})
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "ns.py")
            starts = typecheck.concatenate(root, path)
            text = open(path).read()
        self.assertNotIn("from agentctl", text)
        self.assertEqual(text.splitlines()[starts[1][0] - 1 + 3], "v = x")


class LimitsMatchMoviesTest(unittest.TestCase):
    def test_the_limits_are_the_movies_ratchets(self):
        self.assertEqual(measure.LIMITS, {"cognitive": 10, "cyclomatic": 10, "inner-defs": 2, "method-lines": 50,
                                          "file-lines": 500, "copies": 0, "fan-out": 9, "cycle": 0})
        self.assertEqual(measure.CLONE_TOKENS, 50)


if __name__ == "__main__":
    unittest.main()
