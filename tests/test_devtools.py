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
import partlist  # noqa: E402
import partdeps  # noqa: E402

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


def fake_lib(parts=PARTS, modules=("core",)):
    """<tmp>/lib/agentctl: the real __init__.py with these parts (modules first) and each part's text."""
    lib = os.path.join(tempfile.mkdtemp(prefix="devtools-"), "lib", "agentctl")
    os.makedirs(lib)
    shutil.copy(os.path.join(ROOT, "lib", "agentctl", "__init__.py"), lib)
    partlist.write_lists(lib, [p for p in parts if p in modules], [p for p in parts if p not in modules])
    for name, text in parts.items():
        with open(os.path.join(lib, f"{name}.py"), "w") as f:
            f.write(textwrap.dedent(text))
    return lib


def lists(lib):
    return partlist.parts_and_modules(lib)


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


class PartDepsTest(unittest.TestCase):
    def test_a_part_is_blocked_by_the_namespace_definitions_it_reads_and_never_by_a_module(self):
        found, defs = partdeps.blockers(fake_lib())
        self.assertEqual(found, {"mid": set(), "top": {("entry", "other")}})  # mid reads core's root: a module
        r = tool("partdeps", fake_lib(), "--top", "5")
        self.assertEqual(r.stdout.splitlines(), ["0 mid ", "1 top other@mid"], r.stderr)


class SuiteAtTest(unittest.TestCase):
    def test_the_suite_runs_on_a_snapshot_of_the_revision_not_the_worktree(self):
        repo = tempfile.mkdtemp(prefix="devtools-")
        os.makedirs(os.path.join(repo, "tests"))
        with open(os.path.join(repo, "tests", "run.py"), "w") as f:
            f.write("print(open('marker').read())\nraise SystemExit(3)\n")
        with open(os.path.join(repo, "marker"), "w") as f:
            f.write("committed")
        for args in (["init", "-q"], ["add", "-A"], ["commit", "-qm", "x"]):
            subprocess.run(["git", "-C", repo, *args], check=True, capture_output=True)
        with open(os.path.join(repo, "marker"), "w") as f:
            f.write("edited after")
        out = tempfile.mkdtemp(prefix="devtools-")
        r = subprocess.run(["bash", os.path.join(DEV, "suite-at.sh"), repo, "HEAD", out], capture_output=True,
                           text=True, timeout=60, env=dict(os.environ, SUITE_CMD=f"{sys.executable} tests/run.py"))
        self.assertEqual(r.returncode, 3, r.stderr)
        with open(os.path.join(out, "suite.log")) as f:
            self.assertEqual(f.read().split(), ["committed", "exit", "3"])


    def test_coverage_reaches_the_processes_the_suite_starts(self):
        venv = coverage_venv()
        if not venv:
            self.skipTest("no coverage venv (~/.cache/agentctl-quality/venv)")
        repo = tempfile.mkdtemp(prefix="devtools-")
        for d in ("tests", os.path.join("lib", "agentctl")):
            os.makedirs(os.path.join(repo, d))
        files = {"lib/agentctl/__init__.py": "",
                 "lib/agentctl/m.py": "def f(a):\n    if a:\n        return 1\n    return 2\n\nf(1)\n",
                 "tests/run.py": "import subprocess, sys\n"
                                 "subprocess.run([sys.executable, '-c', 'import sys; sys.path.insert(0, \"lib\"); "
                                 "import agentctl.m'], check=True)\n"}
        for path, text in files.items():
            with open(os.path.join(repo, path), "w") as f:
                f.write(text)
        for args in (["init", "-q"], ["add", "-A"], ["commit", "-qm", "x"]):
            subprocess.run(["git", "-C", repo, *args], check=True, capture_output=True)
        out = tempfile.mkdtemp(prefix="devtools-")
        r = subprocess.run(["bash", os.path.join(DEV, "suite-at.sh"), repo, "HEAD", out, "cov"], capture_output=True,
                           text=True, timeout=120, env=dict(os.environ, COVERAGE_VENV=venv))
        with open(os.path.join(out, "suite.log")) as f:
            log = f.read()
        self.assertEqual(r.returncode, 0, log)
        self.assertNotIn("No data to report", log)
        self.assertRegex(log, r"TOTAL\s+\d+\s+\d+\s+\d+\s+\d+\s+(?!0\.0%)[\d.]+%")


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
        self.assertNotEqual(tool("movedefs", lib, "new", "--new", "x", "--after", "nonesuch", "LIMIT", "OTHER").returncode, 0)
        self.assertIn("LIMIT", read(lib, "mid"), "nor does one refused for its new part's place")
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
        self.assertEqual(lists(lib)[0], ["core", "mid", "mid2", "top"])
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
        self.assertEqual(lists(lib), (["core", "mid", "top"], ["core", "mid"]))
        r = subprocess.run([sys.executable, "-c", "import sys; sys.path.insert(0, sys.argv[1]); import agentctl; "
                            "print(agentctl.entry(), sys.modules['agentctl.mid'].helper())", os.path.dirname(lib)],
                           capture_output=True, text=True, timeout=60)
        self.assertEqual(r.stdout.split(), [os.sep + "x"] * 2, r.stderr)

    def test_code_moved_into_a_module_gets_the_imports_it_lacks(self):
        lib = fake_lib(dict(PARTS, mid="import re\n\n\ndef helper():\n    return root(), re, json.dumps(1)\n"))
        self.assertEqual(tool("mkmodule", lib, "mid").returncode, 0)
        r = tool("mkmodule", lib, "mid")  # again: nothing lacking, nothing added
        self.assertEqual(r.returncode, 0, r.stderr)
        text = read(lib, "mid")
        self.assertEqual(text.count("from agentctl.core import root"), 1)
        self.assertEqual(text.count("import json"), 1)

    def test_a_listed_module_without_imports_yet_gets_them_all(self):
        lib = fake_lib(modules=("core", "mid"))  # listed, but its text is still a namespace part's (a rebase took theirs)
        r = tool("mkmodule", lib, "mid")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("from agentctl.core import root\n", read(lib, "mid"))

    def test_placemodules_puts_the_modules_first(self):
        lib = fake_lib(dict([("top", PARTS["top"]), ("mid", PARTS["mid"]), ("core", PARTS["core"])]))
        self.assertEqual(tool("placemodules", lib).returncode, 0)
        self.assertEqual(lists(lib)[0], ["core", "top", "mid"])
        self.assertIn("PARTS = MODULES + NAMESPACE", read(lib, "__init__"))

    def test_a_module_level_comprehension_variable_is_no_namespace_name(self):
        lib = fake_lib(dict(PARTS, mid="TABLE = {p: p for p in (1, 2)}\n"))
        r = tool("mkmodule", lib, "mid")
        self.assertEqual(r.returncode, 0, r.stderr)

    def test_a_name_another_part_imports_from_the_standard_library_is_imported_from_there(self):
        low = "# agentctl, part `low`: a module.\n# A real module.\n\nfrom typing import Optional\n\nX: Optional[int] = 1\n"
        parts = {"core": PARTS["core"], "low": low, "mid": "def helper() -> Optional[str]:\n    return root()\n",
                 "top": PARTS["top"]}
        lib = fake_lib(parts, modules=("core", "low"))
        r = tool("mkmodule", lib, "mid")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("from typing import Optional\n", read(lib, "mid"))
        self.assertNotIn("from agentctl.core import Optional", read(lib, "mid"))

    def test_a_part_reading_a_namespace_name_is_refused(self):
        r = tool("mkmodule", fake_lib(), "top")
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("reads namespace names: [('other', 'mid')]", r.stderr)



def tools_python():
    """The quality tools' interpreter under the REAL home (the harness's HOME is a fake one), or None."""
    import glob, pwd
    hits = sorted(glob.glob(os.path.join(pwd.getpwuid(os.getuid()).pw_dir, ".cache", "agentctl-quality",
                                         "tools-*", "bin", "python")))
    return hits[-1] if hits else None


def coverage_venv():
    """The coverage venv's bin/ under the REAL home (suite-at.sh's cov mode), or None."""
    import pwd
    d = os.path.join(pwd.getpwuid(os.getuid()).pw_dir, ".cache", "agentctl-quality", "venv", "bin")
    return d if os.path.exists(os.path.join(d, "coverage")) else None


class SameCodeTest(unittest.TestCase):
    def test_a_jump_offset_past_255_leaves_no_extended_arg_in_the_signature(self):
        import samecode
        ns = {}
        exec("def f(a):\n if a:\n" + "".join(f"  x{i} = a.b{i}\n" for i in range(300)) + " return 1\n", ns)
        self.assertNotIn("EXTENDED_ARG", [op for op, _ in samecode.instructions(ns["f"].__code__)])


class StatsTest(unittest.TestCase):
    def test_start_and_now_are_measured_from_their_snapshots_with_one_ruler(self):
        py = tools_python()
        if not py:
            self.skipTest("the quality tools are not installed (hooks/pre-push installs them)")
        lib = fake_lib()
        repo = os.path.dirname(os.path.dirname(lib))
        os.makedirs(os.path.join(repo, "quality"))
        with open(os.path.join(repo, "quality", "types-baseline.tsv"), "w") as f:
            f.write("# kind\tkey\tvalue\nmypy-errors\tmid\t3\nmypy-errors\ttop\t4\n")
        git = lambda *a: subprocess.run(["git", "-C", repo, *a], capture_output=True, text=True, check=True).stdout
        git("init", "-q")
        git("add", "-A")
        git("commit", "-qm", "start")
        start = git("rev-parse", "HEAD").strip()
        with open(os.path.join(lib, "top.py"), "a") as f:  # a function past method-lines (50)
            f.write("\n\ndef long():\n" + "".join(f"    x{i} = {i}\n" for i in range(60)) + "    return x0\n")
        git("commit", "-qam", "now")
        r = subprocess.run([sys.executable, os.path.join(DEV, "stats.py"), "--json", "--repo", repo, "--start", start,
                            "--python", py, "HEAD"], capture_output=True, text=True, timeout=120)
        self.assertEqual(r.returncode, 0, r.stderr)
        out = __import__("json").loads(r.stdout)
        self.assertEqual((out["start"]["over"]["method-lines"], out["now"]["over"]["method-lines"]), (0, 1))
        self.assertEqual((out["start"]["functions_over_any"], out["now"]["functions_over_any"]), (0, 1))
        self.assertEqual((out["now"]["parts"], out["now"]["modules"], out["now"]["files"]), (3, 1, 4))
        self.assertEqual(out["now"]["mypy_errors"], 7)
        self.assertGreater(out["now"]["lines"], out["start"]["lines"])

if __name__ == "__main__":
    unittest.main()
