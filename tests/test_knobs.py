"""THE KNOBS (configfile): each has ONE typed accessor with its default and doc beside it; the code reads the accessor,
never the environment. Per knob: its default, the environment overriding it, a config-file value, and a malformed
value. The lint: lib/agentctl/ reads an AGENTCTL_* name from os.environ (or through env_num / env_text) only in
configfile, except the sites of tests/knob-env-allowlist.tsv -- each with its reason -- which only ever shrink
(`python3 tests/test_knobs.py --write` rewrites the file from the code: a fall is recorded, a new row needs a reason).
HOME, PATH and the other names that are not AGENTCTL_* are the process's own and are not examined."""
import ast, contextlib, glob, os, re, sys, unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from test_agentctl import A, TOOL  # noqa: E402

LIB = os.path.join(os.path.dirname(TOOL), "lib", "agentctl")
ALLOWLIST = os.path.join(HERE, "knob-env-allowlist.tsv")
READERS = {"env_num", "env_text"}
NOT_YET = "batch 2: not yet behind a configfile accessor"


def _name(node):
    """The AGENTCTL_* name a node spells (a string, or an f-string's literal start), else None."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str) and node.value.startswith("AGENTCTL_"):
        return node.value
    if isinstance(node, ast.JoinedStr) and node.values and isinstance(node.values[0], ast.Constant):
        v = str(node.values[0].value)
        return v + "*" if v.startswith("AGENTCTL_") else None
    return None


def _is_environ(node):
    return ast.unparse(node) == "os.environ"


def env_reads(src):
    """The AGENTCTL_* names a source reads from the environment: os.environ.get / [] / `in`, os.getenv, env_num, env_text."""
    out = set()
    for n in ast.walk(ast.parse(src)):
        name = None
        if isinstance(n, ast.Call) and n.args:
            f = ast.unparse(n.func)
            if f in ("os.environ.get", "os.getenv") or f in READERS:
                name = _name(n.args[0])
        elif isinstance(n, ast.Subscript) and _is_environ(n.value) and isinstance(n.ctx, ast.Load):
            name = _name(n.slice)
        elif isinstance(n, ast.Compare) and len(n.ops) == 1 and isinstance(n.ops[0], (ast.In, ast.NotIn)) \
                and _is_environ(n.comparators[0]):
            name = _name(n.left)
        if name:
            out.add(name)
    return out


def code_reads():
    reads = set()
    for p in sorted(glob.glob(os.path.join(LIB, "*.py"))):
        mod = os.path.basename(p)[:-3]
        if mod == "configfile":
            continue
        with open(p) as f:
            reads |= {(mod, n) for n in env_reads(f.read())}
    return reads


def allowlist():
    rows = {}
    with open(ALLOWLIST) as f:
        for line in f:
            if line.strip() and not line.startswith("#"):
                mod, name, reason = line.rstrip("\n").split("\t")
                rows[(mod, name)] = reason
    return rows


def write_allowlist():
    old = allowlist() if os.path.exists(ALLOWLIST) else {}
    lines = ["# module\tAGENTCTL_ name\treason: the reads of the environment outside configfile (tests/test_knobs.py). Only falls."]
    for mod, name in sorted(code_reads()):
        lines.append(f"{mod}\t{name}\t{old.get((mod, name), NOT_YET)}")
    with open(ALLOWLIST, "w") as f:
        f.write("\n".join(lines) + "\n")


@contextlib.contextmanager
def env(**kv):
    saved = {k: os.environ.get(k) for k in kv}
    try:
        for k, v in kv.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        yield
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


with open(os.path.join(LIB, "configfile.py")) as _f:
    _SRC = _f.read()
NUMERIC = re.findall(r'def (knob_\w+)\(\) -> float:\n    return env_num\("(AGENTCTL_\w+)", (\w+)\)', _SRC)
TEXT = re.findall(r'def (knob_\w+)\(\) -> str:\n    return env_text\("(AGENTCTL_\w+)"(?:, ([^)\n]+))?\)(?:  #.*)?\n', _SRC)
SKIP_TEXT = {"AGENTCTL_STORE_TOKEN"}  # a token variable: the harness refuses any test touching one


class KnobTest(unittest.TestCase):
    def test_every_numeric_knob_has_its_default_its_env_override_and_its_file_value(self):
        self.assertGreater(len(NUMERIC), 35)
        for fn, name, const in NUMERIC:
            acc = getattr(A, fn)
            with env(**{name: None}):
                self.assertEqual(acc(), getattr(A, const), name)
            with env(**{name: "7"}):
                self.assertEqual(acc(), 7.0, name)
            with env(**{name: "not a number"}):
                self.assertEqual(acc(), getattr(A, const), name)  # malformed: the default, as env_num always did
            with env(**{name: None, "AGENTCTL_CONFIG_SET": None}):
                try:
                    A.config_apply({name: (9, "agentctl.toml")})
                    self.assertEqual(acc(), 9.0, name)
                finally:
                    os.environ.pop(name, None)
                    A.CONFIG_SOURCE.pop(name, None)
            with env(**{name: "5"}):
                A.config_apply({name: (9, "agentctl.toml")})
                self.assertEqual(acc(), 5.0, name + ": the environment wins over the file")

    def test_every_text_knob_has_its_default_its_env_override_and_its_file_value(self):
        self.assertGreater(len(TEXT), 20)
        for fn, name, default in TEXT:
            if name in SKIP_TEXT:
                continue
            acc = getattr(A, fn)
            want = ast.literal_eval(default) if default and default[0] in "\"'" else (
                getattr(A, default) if default else "")
            with env(**{name: None}):
                self.assertEqual(acc(), want, name)
            with env(**{name: ""}):
                self.assertEqual(acc(), want, name + ": empty is unset")
            with env(**{name: "/x/y"}):
                self.assertEqual(acc(), "/x/y", name)
            with env(**{name: None, "AGENTCTL_CONFIG_SET": None}):
                try:
                    A.config_apply({name: ("from-file", "agentctl.toml")})
                    self.assertEqual(acc(), "from-file", name)
                finally:
                    os.environ.pop(name, None)
                    A.CONFIG_SOURCE.pop(name, None)

    def test_the_raw_and_structured_accessors(self):
        with env(AGENTCTL_CAFFEINATE=None):
            self.assertEqual(A.knob_caffeinate(), "/usr/bin/caffeinate")
        with env(AGENTCTL_CAFFEINATE=""):
            self.assertEqual(A.knob_caffeinate(), "", "set empty: none, not the default")
        with env(AGENTCTL_LAPTOP_PRESSURE=None):
            self.assertIsNone(A.knob_laptop_pressure())
        with env(AGENTCTL_LAPTOP_PRESSURE="-"):
            self.assertEqual(A.knob_laptop_pressure(), "-")
        with env(AGENTCTL_INTERACTIVE="1"):
            self.assertTrue(A.knob_interactive())
        with env(AGENTCTL_INTERACTIVE="yes"):
            self.assertFalse(A.knob_interactive())
        with env(AGENTCTL_GITHUB_READER_APP_ID=" 12 ", AGENTCTL_GITHUB_READER_KEY_FILE=None):
            self.assertEqual(A.knob_github_app("reader"), ("12", ""))
        with env(AGENTCTL_REMOTE=" up "):
            self.assertEqual(A.knob_remote(), "up")
        with env(AGENTCTL_REMOTE=None):
            self.assertEqual(A.knob_remote(), "origin")

    def test_one_accessor_serves_every_reader_of_a_knob(self):
        # the knobs that were read in several modules: a patched default reaches all of them through the one accessor
        for knob in ("RED_STALL", "RED_ORPHAN_GRACE", "BG_POLL", "RED_FIXER_WALL", "STEP_BOUND", "GOV_POLL", "MAX_FOREIGN"):
            self.assertTrue(hasattr(A, "knob_" + knob.lower()), knob)
        with env(AGENTCTL_RED_STALL="123"):
            self.assertEqual(A.knob_red_stall(), 123.0)

    def test_the_environment_is_read_outside_configfile_only_where_the_allowlist_says(self):
        reads, allowed = code_reads(), allowlist()
        self.assertEqual(sorted(reads - set(allowed)), [], "a new read of the environment: add an accessor in configfile")
        self.assertEqual(sorted(set(allowed) - reads), [], "a site gone: strike its row (python3 tests/test_knobs.py --write)")
        for key, reason in allowed.items():
            self.assertGreater(len(reason), 10, key)

    def test_the_lint_sees_every_way_of_reading_a_name(self):
        src = ('import os\na = os.environ.get("AGENTCTL_A")\nb = os.environ["AGENTCTL_B"]\nc = "AGENTCTL_C" in os.environ\n'
               'd = env_num("AGENTCTL_D", 1)\ne = os.getenv("AGENTCTL_E")\nf = os.environ.get(f"AGENTCTL_{x}_F")\n'
               'os.environ["AGENTCTL_G"] = "1"\nh = os.environ.get("HOME")\ni = env_text("AGENTCTL_I")\n')
        self.assertEqual(env_reads(src), {"AGENTCTL_A", "AGENTCTL_B", "AGENTCTL_C", "AGENTCTL_D", "AGENTCTL_E",
                                          "AGENTCTL_*", "AGENTCTL_I"})

    def test_the_generated_config_reference_lists_the_accessors_knobs(self):
        names = {k["name"] for k in A.config_knobs()}
        for _fn, name, _c in NUMERIC:
            self.assertIn(name, names)


if __name__ == "__main__":
    if "--write" in sys.argv:
        write_allowlist()
    else:
        unittest.main()
