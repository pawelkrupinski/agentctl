"""THE DESIGN RULES FAIL CLOSED, proved by mutation (the movies repository's design_check_mutation_test.py model): each
mutant is the checkout's rule code -- quality/design.py, quality/ratchet.py, the vendored core in devtools/design_rules/ --
with ONE rule taken out, and tests/test_design_rules.py must FAIL against every one: a rule whose removal no test notices
could silently stop holding. A mutant is applied by exact text; one whose text is not in its file exactly once fails this
test too (the code changed: re-aim the mutant)."""
import os, shutil, subprocess, sys, tempfile, unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from test_agentctl import A  # noqa: E402,F401  (the harness: HOME, PATH, git config)

ROOT = os.path.dirname(HERE)
TEST = os.path.join(HERE, "test_design_rules.py")
CORE = "devtools/design_rules/"
# name -> (file, exact text in it, what replaces it)
MUTANTS = {
    "no suffix rule": (CORE + "naming.py", 'out.append(("suffix", key, 1))', "pass"),
    "no abbreviation rule": (CORE + "naming.py", 'out.append(("abbreviation", key, 1))', "pass"),
    "idioms refused too": (CORE + "rules_table.py", "return set(self.abbreviations) - set(self.idioms)", "return set(self.abbreviations)"),
    "one file's names clash": (CORE + "naming.py", "and len({p for _, p in orders.values()}) > 1", ""),
    "effects make no collaborator": (CORE + "collaborators.py", "return bool(any(m.effects.values()) or ", "return bool("),
    "no data guard": (CORE + "collaborators.py", " or carries_logic(m, rules))", ")"),
    "no closure over users": (CORE + "collaborators.py", "                found.add(user)\n", "                pass\n"),
    "fan-out counts data": (CORE + "collaborators.py", "return len(m.imports & collaborating)", "return len(m.imports)"),
    "no databag rule": (CORE + "collaborators.py", 'out += [("databag",', 'out += [] and [("databag",'),
    "abstractness over every module (V1)": (CORE + "packages.py", "members = inside & collaborating", "members = inside"),
    "no layer rule": (CORE + "packages.py", "if there is not None and there.rank > here.rank:", "if False:"),
    "no unplaced rule": (CORE + "packages.py", 'out.append(("unplaced", source, 1))\n            continue', "continue"),
    "adapters ignored": (CORE + "design_lint.py", "kind not in allowed", "True"),
    "no state rule": (CORE + "design_lint.py", 'out.append(("state", m.path, len(m.state)))', "pass"),
    "no import-io rule": (CORE + "design_lint.py", 'out.append(("import-io", m.path, m.import_io))', "pass"),
    "a reference is an effect": (CORE + "python_facts.py", 'kind and (id(node) in called or kind == "env")', "kind"),
    "the main guard runs at import": (CORE + "python_facts.py", "if isinstance(stmt, DEFINITIONS) or is_main_guard(stmt):", "if isinstance(stmt, DEFINITIONS):"),
    "a local shadows nothing": (CORE + "python_facts.py", "visible = module - (local_names(fn) - declared)", "visible = module"),
    "no reason lint": (CORE + "exemptions.py", 'out += [f"{kind} {key}: {p}" for p in coupling_reasons.reason_problems(name, why)]', "pass"),
    "no design lint in the rows": ("quality/design.py", " + design_lint.rows(mods.values(), rules)", ""),
    "no fan-out rows": ("quality/design.py", 'if (n := collaborators.fanout(m, collaborating)) > rules.limit("fanout")]', "if False]"),
    "no data guard or package rows": ("quality/design.py", "found += collaborators.rows(mods, rules) + packages.rows(\"py\", mods, collaborating, rules)", "found += []"),
    "no exemptions": ("quality/design.py", "return limits + [r for r in found if (r[0], r[1]) not in admitted]", "return limits + found"),
    "permutations allowed": ("quality/design.py", "if (package, frozenset(names)) not in ALLOWED_PERMUTATIONS:", "if False:"),
    "a stdlib namesake is an import": ("quality/design.py", "if os.path.dirname(path) != LIBRARY and sibling in known:", "if sibling in known:"),
    "a permutation does not refuse": ("quality/ratchet.py", "            print(line)\n            ok = False", "            print(line)"),
}


def scratch(root):
    for name in ("quality", "devtools"):
        shutil.copytree(os.path.join(ROOT, name), os.path.join(root, name), ignore=shutil.ignore_patterns("__pycache__"))


def mutate(root, path, old, new):
    full = os.path.join(root, path)
    with open(full) as f:
        text = f.read()
    if text.count(old) != 1:
        raise AssertionError(f"mutant text not in {path} exactly once: {old!r}")
    with open(full, "w") as f:
        f.write(text.replace(old, new))


def run_test(root):
    env = {**os.environ, "DESIGN_RULES_UNDER_TEST": root}
    done = subprocess.run([sys.executable, TEST], env=env, capture_output=True, text=True, timeout=600)
    return done.returncode, done.stdout + done.stderr


class DesignRulesMutationTest(unittest.TestCase):
    def test_the_rules_test_passes_against_the_unmutated_copy(self):
        with tempfile.TemporaryDirectory() as root:
            scratch(root)
            rc, out = run_test(root)
        self.assertEqual(0, rc, out[-1500:])

    def test_the_rules_test_fails_against_every_mutant(self):
        survivors = []
        for name, (path, old, new) in MUTANTS.items():
            with tempfile.TemporaryDirectory() as root:
                scratch(root)
                mutate(root, path, old, new)
                if run_test(root)[0] == 0:
                    survivors.append(name)
        self.assertEqual([], survivors, "mutants tests/test_design_rules.py does not catch")


if __name__ == "__main__":
    unittest.main()
