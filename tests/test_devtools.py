"""The migration devtools (devtools/): the definition graph and its cycles, the pure-move tools (movedefs, split_part)
and their proofs (sameast, samecode), and mkmodule turning a part into a real module, each on a small fake lib that
loads through the real lib/agentctl/__init__.py."""
import os, re, shutil, subprocess, sys, tempfile, textwrap, unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from test_agentctl import A  # noqa: E402,F401  (the harness: HOME, PATH, git config)

ROOT = os.path.dirname(HERE)
DEV = os.path.join(ROOT, "devtools")
sys.path.insert(0, DEV)
import defgraph  # noqa: E402

PARTS = {
    "core": """\
        # agentctl, part `core`: the bottom.
        # A real module (lib/agentctl/__init__.py MODULES): its own imports, the standard library's and MODULES' only.

        import os


        def root():
            return os.sep
        """,
    "mid": """\
        # agentctl, part `mid`: the middle.
        # Runs inside agentctl's one namespace, after the parts before it in lib/agentctl/__init__.py's PARTS (it uses their names).

        def helper():
            return root() + "x"


        # a comment block
        def other():
            return helper()
        """,
    "top": """\
        # agentctl, part `top`: the top.
        # Runs inside agentctl's one namespace, after the parts before it in lib/agentctl/__init__.py's PARTS (it uses their names).

        def entry():
            return other()
        """,
}


def quoted(names):
    return ", ".join(f'"{n}"' for n in names)


def fake_lib(parts=PARTS, modules=("core",)):
    """<tmp>/lib/agentctl: the real __init__.py with this PARTS and MODULES, and each part's text."""
    lib = os.path.join(tempfile.mkdtemp(prefix="devtools-"), "lib", "agentctl")
    os.makedirs(lib)
    init = open(os.path.join(ROOT, "lib", "agentctl", "__init__.py")).read()
    init = re.sub(r"MODULES = \([^)]*\)", lambda _m: f"MODULES = ({quoted(modules)},)", init)
    init = re.sub(r"PARTS = \([^)]*\)", lambda _m: f"PARTS = ({quoted(parts)},)", init)
    with open(os.path.join(lib, "__init__.py"), "w") as f:
        f.write(init)
    for name, text in parts.items():
        with open(os.path.join(lib, f"{name}.py"), "w") as f:
            f.write(textwrap.dedent(text))
    return lib


def tool(name, *args):
    return subprocess.run([sys.executable, os.path.join(DEV, f"{name}.py"), *args], capture_output=True, text=True,
                          timeout=60)


def copy(lib):
    other = os.path.join(tempfile.mkdtemp(prefix="devtools-"), "lib", "agentctl")
    shutil.copytree(lib, other)
    return other


def read(lib, part):
    with open(os.path.join(lib, f"{part}.py")) as f:
        return f.read()


class DefinitionGraphTest(unittest.TestCase):
    def test_an_edge_is_a_global_read_and_a_local_of_the_same_name_is_none(self):
        parts = dict(PARTS, top="def entry(helper=None):\n    return other()\n\n\ndef shadow():\n    helper = 1\n    return helper\n")
        defs, graph = defgraph.build(fake_lib(parts))
        self.assertEqual(defs["helper"], "mid")
        self.assertEqual(graph["helper"], ["root"])
        self.assertEqual(graph["entry"], ["other"])
        self.assertEqual(graph["shadow"], [])

    def test_cycles_are_reported_and_fail(self):
        parts = dict(PARTS, mid=PARTS["mid"].replace('return root() + "x"', "return entry()"))
        lib = fake_lib(parts)
        cycles = [sorted(c) for c in defgraph.components(defgraph.build(lib)[1]) if len(c) > 1]
        self.assertEqual(cycles, [["entry", "helper", "other"]])
        r = tool("defgraph", lib, "--cycles")
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertIn("cycle of 3: entry@top, helper@mid, other@mid", r.stdout)
        self.assertEqual(tool("defgraph", fake_lib(), "--cycles").returncode, 0)


class PureMoveTest(unittest.TestCase):
    def test_movedefs_moves_a_definition_with_its_comment_and_the_proofs_agree(self):
        lib = fake_lib()
        before = copy(lib)
        r = tool("movedefs", lib, "top", "other")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertNotIn("def other", read(lib, "mid"))
        self.assertIn("# a comment block\ndef other():", read(lib, "top"))
        self.assertEqual(tool("sameast", before, lib).returncode, 0)
        r = tool("samecode", os.path.dirname(before), os.path.dirname(lib))
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)

    def test_movedefs_before_puts_the_moved_statements_above_the_one_reading_them(self):
        parts = dict(PARTS, mid=PARTS["mid"] + "\n\n        LIMIT, OTHER = 3, 4\n",
                     top=PARTS["top"] + "\n\n        TABLE = {'limit': LIMIT}\n")
        lib = fake_lib(parts)
        self.assertNotEqual(tool("movedefs", lib, "top", "LIMIT", "OTHER", "nonesuch").returncode, 0)
        self.assertIn("LIMIT", read(lib, "mid"), "a refused move writes nothing")
        r = tool("movedefs", lib, "top", "--before", "TABLE", "LIMIT", "OTHER")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("return other()\n\n\nLIMIT, OTHER = 3, 4\n\n\nTABLE = {", read(lib, "top"))
        self.assertNotIn("LIMIT", read(lib, "mid"))

    def test_split_part_cuts_at_a_marker_and_lists_the_piece_in_its_place(self):
        lib = fake_lib()
        before = copy(lib)
        plan = os.path.join(os.path.dirname(lib), "plan.tsv")
        with open(plan, "w") as f:
            f.write("-\tmid\t-\n# a comment block\tmid2\tthe other half\n")
        r = tool("split_part", lib, "mid", plan)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn('"mid", "mid2", "top"', read(lib, "__init__"))
        self.assertTrue(read(lib, "mid2").startswith("# agentctl, part `mid2`: the other half\n"))
        self.assertEqual(tool("sameast", before, lib).returncode, 0)

    def test_the_proofs_catch_a_changed_statement(self):
        lib = fake_lib()
        changed = copy(lib)
        with open(os.path.join(changed, "mid.py"), "w") as f:
            f.write(read(lib, "mid").replace('+ "x"', '+ "y"'))
        self.assertEqual(tool("sameast", lib, changed).returncode, 1)
        r = tool("samecode", os.path.dirname(lib), os.path.dirname(changed))
        self.assertEqual(r.returncode, 1)
        self.assertIn("helper", r.stdout)


class MakeModuleTest(unittest.TestCase):
    def test_a_part_reading_only_modules_becomes_one_with_explicit_imports(self):
        lib = fake_lib()
        r = tool("mkmodule", lib, "mid")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("from agentctl.core import root\n", read(lib, "mid"))
        self.assertIn("MODULES = (\"core\", \"mid\")", read(lib, "__init__"))
        r = subprocess.run([sys.executable, "-c", "import sys; sys.path.insert(0, sys.argv[1]); import agentctl; "
                            "print(agentctl.entry(), sys.modules['agentctl.mid'].helper())", os.path.dirname(lib)],
                           capture_output=True, text=True, timeout=60)
        self.assertEqual(r.stdout.split(), [os.sep + "x"] * 2, r.stderr)

    def test_a_part_reading_a_namespace_name_is_refused(self):
        r = tool("mkmodule", fake_lib(), "top")
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("reads namespace names: [('other', 'mid')]", r.stderr)


if __name__ == "__main__":
    unittest.main()
