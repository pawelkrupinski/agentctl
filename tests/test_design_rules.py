"""agentctl's design rules (quality/design.py over the movies rule core vendored in devtools/design_rules/): every rule
through the Python extractor -- naming (suffix, abbreviation, idiom, the blocked word permutation), data or
collaborator and the collaborator-only fan-out, the data guard, distance with V2 abstractness, layers, the design lint
(adapters, state, import-time IO, the main guard), the exemption lint -- and the ratchet holding the frozen rows.
DESIGN_RULES_UNDER_TEST names another copy of the checkout (tests/test_design_rules_mutation.py's)."""
import ast, contextlib, io, os, sys, tempfile, unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from test_agentctl import A  # noqa: E402,F401  (the harness: HOME, PATH, git config)

ROOT = os.environ.get("DESIGN_RULES_UNDER_TEST") or os.path.dirname(HERE)
sys.path[:0] = [os.path.join(ROOT, "quality"), os.path.join(ROOT, "devtools", "design_rules")]
import baseline_ratchet  # noqa: E402
import collaborators  # noqa: E402
import design  # noqa: E402
import design_lint  # noqa: E402
import naming  # noqa: E402
import packages  # noqa: E402
import python_facts  # noqa: E402
import ratchet  # noqa: E402
import rules_table  # noqa: E402

RULES = rules_table.parse("\n".join([
    "limit\tfanout\t2\tpy", "limit\tdistance\t0.2\tpy", "limit\tdistance-min-modules\t2\tpy",
    "limit\tdata-methods\t4\tpy", "limit\tdata-decisions\t9\tpy", "limit\tdatabag\t9\tpy", "limit\tdatafanout\t2\tpy",
    "suffix\tHandler", "suffix\tImpl", "abbreviation\tcfg", "abbreviation\targs", "idiom\targs",
    "adapter\tpy\tlib/agentctl/clock.py\tclock", "distance\tpy\tlib/agentctl",
    "layer\tpy\tlib/agentctl\t0\tmodules", "layer\tpy\ttools\t1\ttools"]))


def py(path, source, imports=()):
    return python_facts.module(path, ast.parse(source), frozenset(imports))


def kinds(rows):
    return {(k, key): v for k, key, v in rows}


PURE = "import dataclasses\n@dataclasses.dataclass(frozen=True)\nclass Point:\n    x: int\n\ndef twice(n):\n    return 2 * n\n"
RUNNER = "import subprocess\n\ndef run(argv):\n    return subprocess.run(argv)\n"


class NamingTest(unittest.TestCase):
    def test_a_forbidden_suffix_on_a_type_or_module_is_a_row(self):
        rows = kinds(naming.rows([py("lib/agentctl/cache_handler.py", "class CacheHandler:\n    pass\n")], RULES))
        self.assertIn(("suffix", "lib/agentctl/cache_handler.py:CacheHandler"), rows)
        self.assertIn(("suffix", "lib/agentctl/cache_handler.py:cache_handler"), rows)

    def test_an_abbreviation_is_a_row_and_an_idiom_is_not(self):
        rows = kinds(naming.rows([py("lib/agentctl/x.py", "def read_cfg(args):\n    return 1\n\ndef takes_args():\n    return 2\n")], RULES))
        self.assertIn(("abbreviation", "lib/agentctl/x.py:read_cfg"), rows)
        self.assertNotIn(("abbreviation", "lib/agentctl/x.py:takes_args"), rows)

    def test_two_names_of_one_package_in_another_word_order_clash_across_files(self):
        found = naming.permutations([py("lib/agentctl/prune_sweep.py", "x = 1\n"), py("lib/agentctl/sweep_prune.py", "x = 1\n")])
        self.assertEqual([("py", "lib/agentctl")], list(found))
        self.assertEqual({}, naming.permutations([py("lib/agentctl/prune_sweep.py", "class SweepPrune:\n    pass\n")]))


class CollaboratorTest(unittest.TestCase):
    def test_effects_state_and_interfaces_make_a_collaborator_and_users_follow(self):
        mods = {"a.py": py("a.py", RUNNER), "b.py": py("b.py", PURE, ("a.py",)), "c.py": py("c.py", PURE),
                "d.py": py("d.py", "CACHE = {}\n\ndef put(k):\n    CACHE[k] = 1\n"),
                "e.py": py("e.py", "from typing import Protocol\n\nclass Port(Protocol):\n    pass\n")}
        self.assertEqual({"a.py", "b.py", "d.py", "e.py"}, collaborators.collaborators(mods, RULES))

    def test_fan_out_counts_collaborators_only(self):
        mods = {"user.py": py("user.py", PURE, ("p1.py", "p2.py", "r1.py", "r2.py", "r3.py")),
                **{f"p{i}.py": py(f"p{i}.py", PURE) for i in (1, 2)}, **{f"r{i}.py": py(f"r{i}.py", RUNNER) for i in (1, 2, 3)}}
        found = collaborators.collaborators(mods, RULES)
        self.assertEqual(3, collaborators.fanout(mods["user.py"], found))

    def test_data_holding_logic_is_a_collaborator(self):
        body = "".join(f"    def m{i}(self):\n        a = 1\n        return a\n" for i in range(5))
        logic = py("l.py", f"import dataclasses\n@dataclasses.dataclass\nclass Heavy:\n    x: int\n{body}")
        self.assertEqual({"l.py"}, collaborators.collaborators({"l.py": logic, "p.py": py("p.py", PURE)}, RULES))

    def test_a_data_type_naming_too_many_types_and_a_module_using_too_many_data_modules_are_rows(self):
        fields = "".join(f"    f{i}: T{i}\n" for i in range(10))
        mods = {"w.py": py("w.py", f"import dataclasses\n@dataclasses.dataclass\nclass Wide:\n{fields}")}
        self.assertIn(("databag", "w.py:Wide"), kinds(collaborators.rows(mods, RULES)))
        users = {"u.py": py("u.py", PURE, ("a.py", "b.py", "c.py")), **{f"{n}.py": py(f"{n}.py", PURE) for n in "abc"}}
        self.assertIn(("datafanout", "u.py"), kinds(collaborators.rows(users, RULES)))


class PackageTest(unittest.TestCase):
    def mods(self):
        return {"lib/agentctl/a.py": py("lib/agentctl/a.py", RUNNER), "lib/agentctl/b.py": py("lib/agentctl/b.py", PURE),
                "tools/t.py": py("tools/t.py", PURE, ("lib/agentctl/a.py",))}

    def test_abstractness_counts_collaborators_only(self):
        mods = {"d/a.py": py("d/a.py", "from typing import Protocol\n\nclass Port(Protocol):\n    pass\n"),
                "d/b.py": py("d/b.py", PURE), "d/c.py": py("d/c.py", PURE)}
        self.assertEqual(1.0, packages.abstractness(set(mods), mods, collaborators.collaborators(mods, RULES)))

    def test_an_import_up_the_layers_is_a_row_and_a_down_one_is_not(self):
        mods = self.mods()
        self.assertEqual({}, kinds(packages.layer_rows("py", mods, RULES)))
        mods["lib/agentctl/b.py"] = py("lib/agentctl/b.py", PURE, ("tools/t.py",))
        self.assertIn(("layer", "lib/agentctl->tools"), kinds(packages.layer_rows("py", mods, RULES)))

    def test_a_directory_without_a_layer_row_is_unplaced(self):
        mods = {**self.mods(), "other/o.py": py("other/o.py", PURE, ("lib/agentctl/b.py",))}
        self.assertIn(("unplaced", "other"), kinds(packages.layer_rows("py", mods, RULES)))

    def test_distance_is_measured_in_a_distance_row_directory_of_enough_modules(self):
        mods = {f"lib/agentctl/m{i}.py": py(f"lib/agentctl/m{i}.py", RUNNER) for i in range(3)}
        mods["tools/t.py"] = py("tools/t.py", PURE, ("lib/agentctl/m0.py",))
        collaborating = collaborators.collaborators(mods, RULES)
        self.assertEqual(1.0, kinds(packages.distance_rows("py", mods, collaborating, RULES))[("distance", "lib/agentctl")])


class DesignLintTest(unittest.TestCase):
    def rows(self, path, source):
        return kinds(design_lint.rows([py(path, source)], RULES))

    def test_a_direct_clock_process_or_environment_outside_an_adapter_is_a_row(self):
        src = "import os, time, subprocess\n\ndef f():\n    return time.time(), os.environ, subprocess.run([])\n"
        self.assertEqual(3, self.rows("lib/agentctl/x.py", src)[("effect", "lib/agentctl/x.py")])

    def test_an_adapter_may_touch_its_effect_and_only_that(self):
        self.assertEqual({}, self.rows("lib/agentctl/clock.py", "import time\n\ndef now():\n    return time.time()\n"))
        self.assertIn(("effect", "lib/agentctl/clock.py"), self.rows("lib/agentctl/clock.py", RUNNER))

    def test_a_reference_alone_runs_nothing(self):
        self.assertEqual({}, self.rows("lib/agentctl/x.py", "import subprocess\n\ndef f():\n    return subprocess.CalledProcessError\n"))

    def test_module_state_written_by_a_function_is_a_row_a_shadowing_local_is_not(self):
        self.assertIn(("state", "x.py"), self.rows("x.py", "CACHE = {}\n\ndef put(k):\n    CACHE[k] = 1\n"))
        self.assertEqual({}, self.rows("x.py", "CACHE = {}\n\ndef put(k):\n    CACHE = {}\n    CACHE[k] = 1\n"))

    def test_io_at_import_is_a_row_but_the_main_guard_and_definitions_are_not(self):
        self.assertIn(("import-io", "x.py"), self.rows("x.py", "import os\nHERE = os.listdir('.')\n"))
        self.assertNotIn(("import-io", "x.py"), self.rows("x.py", "import os\n\ndef f():\n    return os.listdir('.')\n\nif __name__ == '__main__':\n    os.listdir('.')\n"))


class ExemptionTest(unittest.TestCase):
    def test_a_row_needs_a_date_and_a_specific_role_led_reason(self):
        text = ("effect\tlib/agentctl/x.py\t2026-11-26\tprocess adapter: runs every git command for the land path and needs the subprocess module\n"
                "effect\tlib/agentctl/y.py\tlast week\tneeded for now\n")
        with tempfile.TemporaryDirectory() as root:
            os.makedirs(os.path.join(root, "quality"))
            with open(os.path.join(root, design.EXEMPTIONS), "w") as f:
                f.write(text)
            problems = design.exemption_problems(root, [])
        self.assertTrue(all("y.py" in p for p in problems), problems)
        self.assertTrue(any("YYYY-MM-DD" in p for p in problems))
        self.assertTrue(any("generic" in p for p in problems))


def seed(root, files):
    for path, source in files.items():
        os.makedirs(os.path.dirname(os.path.join(root, path)), exist_ok=True)
        with open(os.path.join(root, path), "w") as f:
            f.write(source)
    os.makedirs(os.path.join(root, "quality"), exist_ok=True)
    with open(os.path.join(ROOT, design.RULES)) as f, open(os.path.join(root, design.RULES), "w") as out:
        out.write(f.read())


class RatchetTest(unittest.TestCase):
    """The real tree's rows through the engine: frozen rows hold, a new violation is refused."""

    def check(self, root):
        with contextlib.redirect_stdout(io.StringIO()):
            return ratchet.check(root, which=("design",))

    def freeze(self, root):
        with ratchet.in_checkout(root), contextlib.redirect_stdout(io.StringIO()):
            baseline_ratchet.update(ratchet.SPECS["design"], ratchet.current_rows(root, which=("design",))["design"])

    def test_a_new_violation_is_refused_and_a_frozen_one_is_not(self):
        with tempfile.TemporaryDirectory() as root:
            seed(root, {"lib/agentctl/ok.py": PURE, "lib/agentctl/clock.py": "import time\n\ndef now():\n    return time.time()\n"})
            self.freeze(root)
            self.assertEqual(0, self.check(root))
            seed(root, {"lib/agentctl/leaky.py": "import time\n\ndef now():\n    return time.time()\n"})
            self.assertEqual(1, self.check(root))
            self.freeze(root)
            self.assertEqual(0, self.check(root))  # recorded: the frozen row holds

    def test_a_blocked_word_permutation_is_refused_whatever_the_baseline_says(self):
        with tempfile.TemporaryDirectory() as root:
            seed(root, {"lib/agentctl/prune_sweep.py": PURE, "lib/agentctl/sweep_prune.py": PURE})
            self.freeze(root)
            self.assertEqual(1, self.check(root))

    def test_an_import_resolves_to_its_module_and_a_stdlib_namesake_is_no_import(self):
        with tempfile.TemporaryDirectory() as root:
            seed(root, {"lib/agentctl/queue.py": PURE, "lib/agentctl/user.py": "import queue\nfrom agentctl.queue import x\n"})
            mods = design.modules(root)
            self.assertEqual(frozenset({"lib/agentctl/queue.py"}), mods["lib/agentctl/user.py"].imports)
            seed(root, {"lib/agentctl/other.py": "import queue\n"})
            self.assertEqual(frozenset(), design.modules(root)["lib/agentctl/other.py"].imports)

    def test_the_data_guard_reaches_the_rows(self):
        fields = "".join(f"    f{i}: T{i}\n" for i in range(10))
        with tempfile.TemporaryDirectory() as root:
            seed(root, {"lib/agentctl/wide.py": f"import dataclasses\n@dataclasses.dataclass\nclass Wide:\n{fields}"})
            self.assertIn(("databag", "lib/agentctl/wide.py:Wide"), kinds(design.rows(root)))

    def test_an_exemption_row_admits_its_new_row_only(self):
        with tempfile.TemporaryDirectory() as root:
            seed(root, {"lib/agentctl/leaky.py": RUNNER, "lib/agentctl/leaky2.py": RUNNER})
            self.assertIn(("effect", "lib/agentctl/leaky.py"), kinds(design.rows(root)))
            with open(os.path.join(root, design.EXEMPTIONS), "w") as f:
                f.write("effect\tlib/agentctl/leaky.py\t2026-11-26\tgit adapter: runs every git command for the land path and needs subprocess\n")
            found = kinds(design.rows(root))
            self.assertNotIn(("effect", "lib/agentctl/leaky.py"), found)
            self.assertIn(("effect", "lib/agentctl/leaky2.py"), found)

    def test_the_selftests_seeded_tree_has_every_design_kind_and_a_blocked_clash(self):
        with tempfile.TemporaryDirectory() as root:
            ratchet.seed_tree(root)
            kinds_found = {k for k, _ in ratchet.current_rows(root, which=("design",))["design"] if k != "limit"}
            self.assertGreaterEqual(kinds_found, {"suffix", "abbreviation", "effect", "state", "import-io", "fanout"})
            self.assertTrue(design.permutations(root))


if __name__ == "__main__":
    unittest.main()
