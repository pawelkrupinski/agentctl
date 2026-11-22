"""The config file (docs/simplification-2026-10.md item 7): every knob from the code, a TOML file under the environment,
AGENTCTL.md's reference generated from the code."""
import os, subprocess, sys, tempfile, unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from test_agentctl import A, TOOL  # noqa: E402


def show(env, *extra):
    e = {k: v for k, v in os.environ.items() if k not in ("AGENTCTL_RED_FIXERS", "AGENTCTL_RED_STARVE", "AGENTCTL_CONFIG_SET")}
    e.update(env)
    r = subprocess.run([sys.executable, TOOL, "config", *extra], capture_output=True, text=True, env=e, timeout=60)
    return r.stdout + r.stderr


class ConfigTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.file = os.path.join(self.dir, "agentctl.toml")
        with open(self.file, "w") as f:
            f.write('red_fixers = 3\n[red]\nstarve = 600\n')

    def test_a_default_naming_a_constant_is_evaluated_from_the_source_not_a_namespace(self):
        texts = [("a.py", "A = 3 * 60\nB, C = 2 * A, 'x'\nD = f()\nP = os.path.join(HOME, 'bin')\n")]
        consts = A.config_constants(texts)
        self.assertEqual({k: consts.get(k) for k in "ABCDP"},
                         {"A": 180, "B": 360, "C": "x", "D": None, "P": os.path.join(A.HOME, "bin")})
        self.assertEqual(A.config_default("B", consts), 360)
        self.assertEqual(A.config_default("D", consts), "D")  # not a constant of literals: the expression as written

    def test_the_file_sets_a_knob_the_environment_overrides_it(self):
        out = show({"AGENTCTL_CONFIG": self.file})
        self.assertIn(f"red_fixers                   = 3  ({self.file})", out)
        self.assertIn(f"red_starve                   = 600  ({self.file})", out)  # [red] starve: the same knob
        out = show({"AGENTCTL_CONFIG": self.file, "AGENTCTL_RED_FIXERS": "4"})
        self.assertIn("red_fixers                   = 4  (environment)", out)

    def test_a_restarted_process_rereads_the_file_not_its_inherited_copy(self):
        # what a config file set before a re-exec is config, not the user's environment: the file's value now wins
        out = show({"AGENTCTL_CONFIG": self.file, "AGENTCTL_RED_FIXERS": "9", "AGENTCTL_CONFIG_SET": "AGENTCTL_RED_FIXERS"})
        self.assertIn("red_fixers                   = 3", out)
        with open(self.file, "w") as f:
            f.write("")
        out = show({"AGENTCTL_CONFIG": self.file, "AGENTCTL_RED_FIXERS": "3", "AGENTCTL_CONFIG_SET": "AGENTCTL_RED_FIXERS"})
        self.assertNotIn("red_fixers", out, "removed from the file: gone after the restart")

    def test_every_knob_comes_from_the_code_and_no_identity_or_secret_is_one(self):
        knobs = {k["name"]: k for k in A.config_knobs()}
        self.assertEqual(knobs["AGENTCTL_RED_FIXERS"]["default"], A.RED_FIXERS)
        self.assertIn("live fixers at once", knobs["AGENTCTL_RED_FIXERS"]["doc"])
        for name in ("AGENTCTL_STORE_TOKEN", "AGENTCTL_LEASE_SECRET", "AGENTCTL_AGENT_NAME", "AGENTCTL_ROLE"):
            self.assertNotIn(name, knobs)
        text = A.config_defaults_text()
        self.assertIn("# red_fixers = 5", text)
        import tomllib
        tomllib.loads(text.replace("# ", "", 0))  # the file as printed is valid TOML (every knob commented out)

    def test_agentctl_md_carries_the_generated_reference(self):
        with open(os.path.join(os.path.dirname(TOOL), "AGENTCTL.md")) as f:
            md = f.read()
        self.assertIn(A.config_doc_text(), md, "run `agentctl config doc --write`")


if __name__ == "__main__":
    unittest.main()
