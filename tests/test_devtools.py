"""The restructuring's devtools (devtools/): the pure-move tools between modules (movedefs) and their proofs (sameast,
samecode), the import tools (mkmodule, retarget, unused, placemodules), split.sh and prove.sh (a split by spec, its
proofs), suite-at and stats, each on a small fake lib of real modules under the real lib/agentctl/__init__.py."""
import os, re, shutil, subprocess, sys, tempfile, textwrap, unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from test_agentctl import A  # noqa: E402,F401  (the harness: HOME, PATH, git config)

ROOT = os.path.dirname(HERE)
DEV = os.path.join(ROOT, "devtools")
sys.path.insert(0, DEV)
import partlist  # noqa: E402

HEADER = "# agentctl, part `{0}`: the {0}.\n# A real module (lib/agentctl/__init__.py MODULES): its own imports, the standard library's and MODULES' only.\n\n"
PARTS = {
    "core": HEADER.format("core") + """\
import os


def root():
    return os.sep
""",
    "mid": HEADER.format("mid") + """\
from agentctl.core import root


def helper():
    return root() + "x"


# a comment block
def other():
    return helper()
""",
    "top": HEADER.format("top") + """\
from agentctl.mid import other


def entry():
    return other()
""",
}


def fake_lib(parts=PARTS):
    """<tmp>/lib/agentctl: the real __init__.py listing these modules, in this order, and each one's text."""
    lib = os.path.join(tempfile.mkdtemp(prefix="devtools-"), "lib", "agentctl")
    os.makedirs(lib)
    shutil.copy(os.path.join(ROOT, "lib", "agentctl", "__init__.py"), lib)
    partlist.write_modules(lib, list(parts))
    for name, text in parts.items():
        with open(os.path.join(lib, f"{name}.py"), "w") as f:
            f.write(textwrap.dedent(text))
    return lib


def modules(lib):
    return partlist.module_list(lib)


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
    def test_movedefs_moves_a_definition_with_its_comment_and_the_import_tools_and_proofs_follow(self):
        lib = fake_lib()
        before = copy(lib)
        r = tool("movedefs", lib, "top", "other")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertNotIn("def other", read(lib, "mid"))
        self.assertIn("# a comment block\ndef other():", read(lib, "top"))
        self.assertEqual(tool("retarget", lib).stdout, "retargeted top\n")  # top defines other now
        self.assertEqual(tool("mkmodule", lib, "top").returncode, 0)  # it reads mid's helper
        self.assertIn("from agentctl.mid import helper\n", read(lib, "top"))
        self.assertNotIn("import other", read(lib, "top"))
        self.assertEqual(tool("sameast", before, lib).returncode, 0)
        r = tool("samecode", os.path.dirname(before), os.path.dirname(lib))
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)

    def test_movedefs_before_puts_the_moved_statements_above_the_one_reading_them(self):
        parts = dict(PARTS, mid=PARTS["mid"] + "\n\nLIMIT, OTHER = 3, 4\n", top=PARTS["top"] + "\n\nTABLE = {'limit': LIMIT}\n")
        lib = fake_lib(parts)
        self.assertNotEqual(tool("movedefs", lib, "top", "LIMIT", "OTHER", "nonesuch").returncode, 0)
        self.assertIn("LIMIT", read(lib, "mid"), "a refused move writes nothing")
        self.assertNotEqual(tool("movedefs", lib, "top", "--new", "x", "LIMIT", "OTHER").returncode, 0)
        self.assertIn("LIMIT", read(lib, "mid"), "nor does one refused for its new module existing")
        r = tool("movedefs", lib, "top", "--before", "TABLE", "LIMIT", "OTHER")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("return other()\n\n\nLIMIT, OTHER = 3, 4\n\n\nTABLE = {", read(lib, "top"))
        self.assertNotIn("LIMIT", read(lib, "mid"))

    def test_movedefs_from_moves_only_that_modules_statement_and_a_registration_by_its_key(self):
        parts = dict(PARTS, core=PARTS["core"] + "\n\nTABLE = {}\n",
                     mid=PARTS["mid"] + "\n\nTABLE = {'x': 1}\nTABLE['y'] = 2\n")
        lib = fake_lib(parts)
        self.assertNotEqual(tool("movedefs", lib, "top", "TABLE").returncode, 0, "two modules bind it: say which")
        self.assertIn("TABLE = {}", read(lib, "core"), "a refused move writes nothing")
        r = tool("movedefs", lib, "top", "--from", "mid", 'TABLE["y"]')  # either quote
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("TABLE = {}", read(lib, "core"))
        self.assertIn("TABLE = {'x': 1}", read(lib, "mid"))
        self.assertNotIn("TABLE['y']", read(lib, "mid"))
        self.assertIn("\n\n\nTABLE['y'] = 2\n", read(lib, "top"))

    def test_a_new_module_is_listed_and_mkmodule_gives_it_its_imports_in_import_order(self):
        lib = fake_lib()
        before = copy(lib)
        r = tool("movedefs", lib, "low", "--new", "the low helpers", "helper")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertTrue(read(lib, "low").startswith("# agentctl, part `low`: the low helpers\n# A real module"))
        r = tool("mkmodule", lib, "low")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("\nfrom agentctl.core import root\n", read(lib, "low"))
        tool("mkmodule", lib, "mid")
        self.assertEqual(modules(lib), ["core", "low", "mid", "top"])  # mid imports low now: low sorts before it
        self.assertEqual(tool("sameast", before, lib).returncode, 0)
        r = tool("samecode", os.path.dirname(before), os.path.dirname(lib))
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)

    def test_the_proofs_catch_a_changed_statement(self):
        lib = fake_lib()
        changed = copy(lib)
        with open(os.path.join(changed, "mid.py"), "w") as f:
            f.write(read(lib, "mid").replace('+ "x"', '+ "y"'))
        self.assertEqual(tool("sameast", lib, changed).returncode, 1)
        r = tool("samecode", os.path.dirname(lib), os.path.dirname(changed))
        self.assertEqual(r.returncode, 1)
        self.assertIn("helper", r.stdout)


def committed(lib):
    """lib's repository (<tmp>, holding lib/agentctl) with everything committed: prove.sh's revision."""
    repo = os.path.dirname(os.path.dirname(lib))
    for cmd in (["init", "-q"], ["add", "-A"], ["commit", "-qm", "base"]):
        subprocess.run(["git", "-C", repo, *cmd], check=True, capture_output=True, timeout=60)
    return repo


def script(name, *args):
    return subprocess.run(["bash", os.path.join(DEV, name), *args], capture_output=True, text=True, timeout=120)


class SplitTest(unittest.TestCase):
    def test_a_spec_splits_a_module_into_a_new_and_an_existing_one_and_prove_holds_it_a_pure_move(self):
        lib = fake_lib()
        repo = committed(lib)
        spec = os.path.join(repo, "spec")
        with open(spec, "w") as f:
            f.write("low|the low helpers|helper\ntop|-|other\n")
        r = script("split.sh", lib, "mid", spec)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("low: ", r.stdout)
        self.assertNotIn("def helper", read(lib, "mid"))
        self.assertIn("from agentctl.core import root\n", read(lib, "low"))
        self.assertIn("from agentctl.low import helper\n", read(lib, "top"))
        self.assertNotIn("from agentctl.mid import", read(lib, "top"))  # retargeted, the dead import dropped
        self.assertEqual(modules(lib), ["core", "mid", "low", "top"])  # top imports low now: after it
        r = script("prove.sh", repo, "--no-ratchet")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)

    def test_prove_names_a_changed_statement_and_a_module_that_does_not_import(self):
        lib = fake_lib()
        repo = committed(lib)
        with open(os.path.join(lib, "mid.py"), "w") as f:
            f.write(read(lib, "mid").replace('+ "x"', '+ "y"') + "\nfrom agentctl.nonesuch import gone\n")
        r = script("prove.sh", repo, "HEAD", "--no-ratchet")
        self.assertEqual(r.returncode, 1)
        for failed in ("SAMEAST FAILED", "SAMECODE FAILED", "IMPORT FAILED"):
            self.assertIn(failed, r.stdout)


class IntraTest(unittest.TestCase):
    def test_each_definition_lists_the_modules_own_names_it_reads(self):
        lib = fake_lib(dict(PARTS, mid=PARTS["mid"] + "\n\nLIMIT = 3\nTABLE = {}\nTABLE['y'] = LIMIT\n\n\n"
                                                      "def last():\n    os = helper()\n    return os, LIMIT\n"))
        r = tool("intra", os.path.join(lib, "mid.py"))
        self.assertEqual(r.returncode, 0, r.stderr)
        rows = [line.split("\t") for line in r.stdout.splitlines()]
        self.assertEqual([(name, used) for _line, name, used in rows],
                         [("helper", ""), ("other", "helper"), ("LIMIT", ""), ("TABLE", ""),
                          ("TABLE['y']", "LIMIT TABLE"), ("last", "LIMIT helper")])
        self.assertEqual(int(rows[0][0]), read(lib, "mid").split("\n").index("def helper():") + 1)


class ImportToolsTest(unittest.TestCase):
    def test_retarget_imports_a_moved_name_from_the_module_now_defining_it(self):
        parts = {"core": "import os\n\n\ndef root():\n    return os.sep\n",
                 "base": "def helper():\n    return 1\n",
                 "top": "from agentctl.core import helper, root\n\n\ndef entry():\n    return helper() + root()\n"}
        lib = fake_lib(parts)
        r = tool("retarget", lib)
        self.assertEqual(r.stdout, "retargeted top\n", r.stderr)
        self.assertTrue(read(lib, "top").startswith("from agentctl.core import root\nfrom agentctl.base import helper\n"))

    def test_a_registration_into_another_modules_table_does_not_define_it(self):
        parts = {"core": "", "low": "from agentctl.core import TABLE\n\n\nTABLE['low'] = 1\n",  # TABLE went to base
                 "base": "TABLE = {'base': 1}\n",
                 "top": "from agentctl.core import TABLE\n\n\ndef entry():\n    return TABLE\n"}
        lib = fake_lib(parts)
        r = tool("retarget", lib)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("from agentctl.base import TABLE\n", read(lib, "top"))
        self.assertIn("from agentctl.base import TABLE\n", read(lib, "low"))

    def test_retarget_keeps_another_import_among_the_block_above_it(self):
        parts = {"core": "def root():\n    return 1\n", "base": "def helper():\n    return 1\n",
                 "top": "from agentctl.core import helper\nfrom typing import Any\nfrom agentctl.core import root\n"}
        lib = fake_lib(parts)
        r = tool("retarget", lib)
        self.assertEqual(r.stdout, "retargeted top\n", r.stderr)
        self.assertEqual("from typing import Any\nfrom agentctl.core import root\nfrom agentctl.base import helper\n",
                         read(lib, "top"))

    def test_retarget_refuses_an_import_block_holding_another_statement(self):
        parts = {"core": "def root():\n    return 1\n", "base": "def helper():\n    return 1\n",
                 "top": "from agentctl.core import helper\nVALUE = 1\nfrom agentctl.core import root\n"}
        lib = fake_lib(parts)
        r = tool("retarget", lib)
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("VALUE = 1", read(lib, "top"))

    def test_unused_drops_only_the_names_nothing_reads(self):
        parts = dict(PARTS, core="import os, re\nfrom agentctl.mid import helper, other  # x\n\n\n"
                                 "def root():\n    return os.sep  # re-reading is not re\n")
        lib = fake_lib(parts)
        r = tool("unused", lib, "core")
        self.assertEqual(r.stdout, "core 2 ['helper', 'other']\n", r.stderr)
        tool("unused", lib, "--fix", "core")
        self.assertTrue(read(lib, "core").startswith("import os, re\n\n\ndef root"), read(lib, "core"))


class MakeModuleTest(unittest.TestCase):
    def test_code_moved_into_a_module_gets_the_imports_it_lacks(self):
        lib = fake_lib(dict(PARTS, mid=PARTS["mid"] + "\n\ndef more():\n    return root(), re, json.dumps(1)\n"))
        r = tool("mkmodule", lib, "mid")
        self.assertEqual(r.returncode, 0, r.stderr)
        r = tool("mkmodule", lib, "mid")  # again: nothing lacking, nothing added
        self.assertEqual(r.returncode, 0, r.stderr)
        text = read(lib, "mid")
        self.assertEqual(text.count("from agentctl.core import root"), 1)
        self.assertEqual((text.count("import json"), text.count("import re")), (1, 1))
        imports = [line for line in text.split("\n") if line.startswith(("import ", "from "))]
        self.assertEqual(imports, ["import json", "import re", "from agentctl.core import root"],
                         "the standard library's imports above the modules' ones")
        r = subprocess.run([sys.executable, "-c", "import sys; sys.path.insert(0, sys.argv[1]); import agentctl.top as t; "
                            "print(t.entry())", os.path.dirname(lib)], capture_output=True, text=True, timeout=60)
        self.assertEqual(r.stdout.split(), [os.sep + "x"], r.stderr)

    def test_placemodules_puts_each_module_after_the_modules_it_imports(self):
        parts = {"top": "from agentctl.mid import helper\n", "mid": "from agentctl.core import root\n\n\nhelper = root\n",
                 "core": "def root():\n    return 1\n", "free": "X = 1\n"}
        lib = fake_lib(parts)
        r = tool("placemodules", lib)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(modules(lib), ["core", "mid", "top", "free"])  # free imports nothing: it keeps its place
        with open(os.path.join(lib, "core.py"), "w") as f:
            f.write("from agentctl.top import helper\n")
        r = tool("placemodules", lib)
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("an import cycle among: core, mid, top", r.stderr)

    def test_a_module_level_comprehension_variable_is_no_name_to_import(self):
        lib = fake_lib(dict(PARTS, mid="import os\n\nTABLE = {p: p for p in (1, 2)}\n"))
        r = tool("mkmodule", lib, "mid")
        self.assertEqual(r.returncode, 0, r.stderr)

    def test_a_name_another_module_imports_from_the_standard_library_is_imported_from_there(self):
        low = "# agentctl, part `low`: a module.\n# A real module.\n\nfrom typing import Optional\n\nX: Optional[int] = 1\n"
        parts = {"core": PARTS["core"], "low": low, "mid": "import os\n\n\ndef helper() -> Optional[str]:\n    return os.sep\n",
                 "top": PARTS["top"]}
        lib = fake_lib(parts)
        r = tool("mkmodule", lib, "mid")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("from typing import Optional\n", read(lib, "mid"))
        self.assertNotIn("from agentctl.core import Optional", read(lib, "mid"))

    def test_a_module_reading_a_name_no_module_defines_is_refused(self):
        r = tool("mkmodule", fake_lib(dict(PARTS, top=PARTS["top"] + "\n\ndef more():\n    return nonesuch()\n")), "top")
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("reads names no module defines: ['nonesuch']", r.stderr)


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

    def test_a_borrowed_load_reads_as_the_plain_load(self):
        """The compiler borrows a local's reference (LOAD_FAST_BORROW, 3.14+) or not by its context alone: a move
        that adds a module-level import turned canon_job's `re` loads into plain LOAD_FAST."""
        import samecode
        ns = {}
        exec("def f(a):\n    return a + 1\n", ns)
        ops = [op for op, _ in samecode.instructions(ns["f"].__code__)]
        self.assertIn("LOAD_FAST", ops)
        self.assertFalse([op for op in ops if op.endswith("_BORROW")], ops)

    def test_a_frozen_set_constant_reads_the_same_in_any_iteration_order(self):
        """A set display of constants compiles to a frozenset constant, whose repr follows its iteration order --
        insertion and hash collisions, not its members (samecode once called redblock's unmoved owner_cmd_unsafe
        changed)."""
        import samecode
        ns = {}
        exec("def f(a):\n    return {1, 9, 'x'} & a\n", ns)
        code = ns["f"].__code__
        const = next(c for c in code.co_consts if isinstance(c, frozenset))
        other = code.replace(co_consts=tuple(frozenset(sorted(c, key=repr, reverse=True)) if c is const else c
                                             for c in code.co_consts))
        self.assertNotEqual(repr(const), repr(next(c for c in other.co_consts if isinstance(c, frozenset))))
        self.assertEqual(samecode.signature(code), samecode.signature(other))


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
        self.assertEqual((out["now"]["parts"], out["now"]["modules"], out["now"]["files"]), (3, 3, 4))
        self.assertEqual(out["now"]["mypy_errors"], 7)
        self.assertGreater(out["now"]["lines"], out["start"]["lines"])


class SelfPushTest(unittest.TestCase):
    """devtools/selfpush.py: one lock, fetch, rebase, push; the pre-push hook (a counting fake) runs once per push."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="selfpush-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
        self.env.update(AGENTCTL_PUSH_LOCK=os.path.join(self.tmp, "lock"), GIT_CONFIG_GLOBAL=os.devnull,
                        GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@t", GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@t")
        self.log = os.path.join(self.tmp, "hook.log")
        hooks = os.path.join(self.tmp, "hooks")
        os.makedirs(hooks)
        with open(os.path.join(hooks, "pre-push"), "w") as f:  # the suite's stand-in: marks its start and end
            f.write(f"#!/bin/sh\necho start >> {self.log}\nsleep 1\necho end >> {self.log}\n")
        os.chmod(os.path.join(hooks, "pre-push"), 0o755)
        self.origin = os.path.join(self.tmp, "origin.git")
        self.git(self.tmp, "init", "-q", "--bare", "-b", "main", self.origin)
        seed = self.clone("seed")
        self.commit(seed, "base.txt", "base")
        self.git(seed, "push", "-q", "origin", "HEAD:main")
        self.hooks = hooks

    def git(self, cwd, *args):
        return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=True, env=self.env).stdout

    def clone(self, name):
        path = os.path.join(self.tmp, name)
        self.git(self.tmp, "clone", "-q", self.origin, path)
        return path

    def commit(self, repo, name, text):
        with open(os.path.join(repo, name), "w") as f:
            f.write(text)
        self.git(repo, "add", name)
        self.git(repo, "commit", "-qm", name)

    def start(self, repo):
        self.git(repo, "config", "core.hooksPath", self.hooks)
        return subprocess.Popen([sys.executable, os.path.join(DEV, "selfpush.py")], cwd=repo, env=self.env,
                                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)

    def test_two_concurrent_pushes_serialise_and_each_runs_the_suite_once(self):
        a, b = self.clone("a"), self.clone("b")
        self.commit(a, "a.txt", "a")
        self.commit(b, "b.txt", "b")
        procs = [self.start(a), self.start(b)]
        outs = [p.communicate(timeout=60)[0] for p in procs]
        self.assertEqual([p.returncode for p in procs], [0, 0], outs)
        with open(self.log) as f:
            self.assertEqual(f.read().split(), ["start", "end", "start", "end"])  # never overlapping, once each
        files = self.git(self.origin, "ls-tree", "--name-only", "main").split()
        self.assertEqual(sorted(files), ["a.txt", "b.txt", "base.txt"])

    def test_a_rebase_conflict_aborts_cleanly_and_releases_the_lock(self):
        a, other = self.clone("a"), self.clone("other")
        self.commit(other, "base.txt", "theirs")
        self.git(other, "push", "-q", "origin", "HEAD:main")
        self.commit(a, "base.txt", "ours")
        p = self.start(a)
        out = p.communicate(timeout=60)[0]
        self.assertEqual(p.returncode, 1, out)
        self.assertIn("NOT PUSHED", out)
        self.assertFalse(os.path.exists(os.path.join(a, ".git", "rebase-merge")), out)
        self.assertFalse(os.path.exists(self.log), "the suite must not run on a conflicted tree")
        import fcntl
        with open(self.env["AGENTCTL_PUSH_LOCK"], "a+") as f:
            fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)  # raises when still held

    def test_a_dirty_tree_is_reported_as_dirty_not_as_a_conflict(self):
        a, other = self.clone("a"), self.clone("other")
        self.commit(other, "o.txt", "o")
        self.git(other, "push", "-q", "origin", "HEAD:main")
        self.commit(a, "a.txt", "a")
        with open(os.path.join(a, "base.txt"), "w") as f:
            f.write("edited, uncommitted")
        p = self.start(a)
        out = p.communicate(timeout=60)[0]
        self.assertEqual(p.returncode, 1, out)
        self.assertIn("uncommitted", out)
        self.assertNotIn("conflict", out)
        self.assertNotIn("no rebase in progress", out)  # no failing --abort

    def test_the_wait_line_is_printed_once_per_interval(self):
        import importlib.util, io, fcntl, contextlib
        spec = importlib.util.spec_from_file_location("selfpush", os.path.join(DEV, "selfpush.py"))
        sp = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(sp)
        sp.LOCK, sp.REPORT_EVERY = os.path.join(self.tmp, "held"), 1
        with open(sp.LOCK, "a+") as holder:
            fcntl.flock(holder, fcntl.LOCK_EX)
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                self.assertIsNone(sp.take_lock(3.5))
        stamps = [int(l.split("(")[1].split(" s")[0]) for l in buf.getvalue().splitlines()]
        self.assertEqual(len(stamps), len(set(stamps)), stamps)  # never repeated
        self.assertGreaterEqual(len(stamps), 3)

    def test_a_direct_push_hook_prints_the_hint(self):
        r = subprocess.run(["sh", os.path.join(ROOT, "hooks", "pre-push")], input="x " + "0" * 40 + " y " + "0" * 40 + "\n",
                           capture_output=True, text=True, env=self.env, cwd=self.tmp)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("selfpush.py", r.stderr)


if __name__ == "__main__":
    unittest.main()
