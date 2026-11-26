"""devtools/rank.py (the over-limit functions ranked, shape-tagged, split into disjoint batches) and
devtools/baselinemerge.py (two branches' baseline edits merge to the regenerated rows, never to conflict markers)."""
import os, subprocess, sys, tempfile, textwrap, unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from test_agentctl import A  # noqa: E402,F401  (the harness: HOME, PATH, git config)

ROOT = os.path.dirname(HERE)
DEV = os.path.join(ROOT, "devtools")
sys.path.insert(0, DEV)
import baselinemerge  # noqa: E402
import rank  # noqa: E402

SOURCES = {
    "red_a": '''
def tries(x):
    try:
        return int(x)
    except ValueError:
        return 0

def chains(d):
    a = d.get("a") or 1
    b = d.get("b") or 2
    return a if b else d.get("c") or 3

def route(cmd):
    if cmd == "a":
        return 1
    elif cmd == "b":
        return 2
    elif cmd == "c":
        return 3
    return 0
''',
    "red_b": '''
def fmt(a):
    return f"{a}" + f"{a}" + f"{a}" + f"{a}" + f"{a}" + f"{a}"

def loops(a):
    for i in a:
        pass
    for j in a:
        pass
    while a:
        break

def closure():
    n = 0
    def inc():
        nonlocal n
        n += 1
    return inc

def plain(a):
    return a
''',
    "gov_c": "def solo(a):\n    return a\n",
}
EXPECTED = {"tries": "try-default", "chains": "get-or-chain", "route": "subcommand-ladder", "fmt": "formatter",
            "loops": "multi-loop", "closure": "closure-state", "plain": "other", "solo": "other"}


def fake_root():
    root = tempfile.mkdtemp(prefix="rank-")
    os.makedirs(os.path.join(root, "lib", "agentctl"))
    os.makedirs(os.path.join(root, "quality"))
    for part, text in SOURCES.items():
        with open(os.path.join(root, "lib", "agentctl", part + ".py"), "w") as f:
            f.write(text)
    rows = "".join(f"cognitive\t{name}\t{11 + i}\n" for i, name in enumerate(EXPECTED))
    with open(os.path.join(root, "quality", "quality-baseline.tsv"), "w") as f:
        f.write("# header\n" + rows)
    return root


class RankTest(unittest.TestCase):
    def test_shapes_are_tagged(self):
        root = fake_root()
        got = {f["name"]: f["shape"] for f in rank.ranked(root, rank.baseline_rows(root))}
        self.assertEqual(got, EXPECTED)

    def test_points_over_limit(self):
        root = fake_root()
        got = {f["name"]: f["over"] for f in rank.ranked(root, rank.baseline_rows(root))}
        self.assertEqual(got["tries"], {"cognitive": 1})

    def test_batches_are_disjoint_by_file(self):
        root = fake_root()
        functions = rank.ranked(root, rank.baseline_rows(root))
        for size in (2, 3, 4):
            out = rank.batches(functions, 3, size)
            files = [{f["part"] for f in b} for b in out]
            for i, a in enumerate(files):
                for b in files[i + 1:]:
                    self.assertFalse(a & b, (size, files))
            self.assertTrue(all(len(b) <= size for b in out))

    def test_cli_shape_filter(self):
        root = fake_root()
        out = subprocess.run([sys.executable, os.path.join(DEV, "rank.py"), "--root", root, "--shape", "formatter"],
                             capture_output=True, text=True)
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertEqual([l.split("\t")[1] for l in out.stdout.splitlines()], ["fmt"])


def git(cwd, *args):
    return subprocess.run(["git", "-C", cwd, *args], capture_output=True, text=True)


HEAD = "# THE QUALITY BASELINE\n"


class BaselineMergeTest(unittest.TestCase):
    def repo(self):
        d = tempfile.mkdtemp(prefix="bm-")
        git(d, "init", "-q", "-b", "main")
        os.makedirs(os.path.join(d, "quality"))
        shutil_copy = lambda s, t: open(t, "w").write(open(s).read())
        self.assertEqual(baselinemerge.install(d), 0)
        return d

    def write(self, d, rows):
        with open(os.path.join(d, "quality", "quality-baseline.tsv"), "w") as f:
            f.write(HEAD + "".join(f"{k}\t{key}\t{v}\n" for k, key, v in rows))

    def commit(self, d, msg):
        git(d, "add", "-A")
        self.assertEqual(git(d, "commit", "-qm", msg).returncode, 0)

    def test_driver_installed_by_the_script(self):
        d = self.repo()
        self.assertIn("quality/quality-baseline.tsv merge=baseline-regen", open(os.path.join(d, ".gitattributes")).read())
        self.assertIn("baselinemerge.py driver", git(d, "config", "merge.baseline-regen.driver").stdout)

    def test_conflicting_edits_merge_to_the_regenerated_rows(self):
        d = self.repo()
        self.write(d, [("cognitive", "a", 20), ("cognitive", "b", 30), ("cognitive", "c", 15)])
        self.commit(d, "base")
        git(d, "checkout", "-qb", "side")
        self.write(d, [("cognitive", "a", 12), ("cognitive", "b", 30), ("cognitive", "c", 15), ("cognitive", "n", 11)])
        self.commit(d, "side")
        git(d, "checkout", "-q", "main")
        self.write(d, [("cognitive", "a", 18), ("cognitive", "b", 25), ("cognitive", "c", 15)])
        self.commit(d, "main")
        merged = git(d, "merge", "side", "-m", "merge")
        self.assertEqual(merged.returncode, 0, merged.stdout + merged.stderr)
        text = open(os.path.join(d, "quality", "quality-baseline.tsv")).read()
        self.assertNotIn("<<<<", text)
        self.assertEqual(text, HEAD + "cognitive\ta\t12\ncognitive\tb\t25\ncognitive\tc\t15\ncognitive\tn\t11\n")

    def test_a_row_one_side_removed_stays_removed(self):
        rows = baselinemerge.merge_rows({("k", "x"): 12, ("k", "y"): 12}, {("k", "y"): 12}, {("k", "x"): 11, ("k", "y"): 12})
        self.assertEqual(rows, {("k", "y"): 12})


if __name__ == "__main__":
    unittest.main()
