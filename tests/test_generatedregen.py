"""RESOLVING CONFLICTED GENERATED FILES (generated.regenerate and rows_merge), characterised with git, the registry and
the regen command faked, over real files in a temporary tree: tables merged row by row, a snapshot or derived file
regenerated and its output explained, a block's hand-written lines merged, the server role keeping main's row, and
every refusal in words. `run_regenerate` takes the modules the code lives in, so a copy can be driven the same way."""
import contextlib
import importlib
import os
import sys
import tempfile
import types
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from test_agentctl import A  # noqa: E402,F401 (puts lib on the path)

BASE = "# h\na\t1\nb\t2\nc\t3\n"
OURS = "# h\na\t1\nb\t20\nc\t3\n"
THEIRS = "# h\na\t10\nb\t2\nc\t3\nd\t4\n"
CLASH = "# h\na\t1\nb\t99\nc\t3\n"
SNAP_B, SNAP_O, SNAP_T = "x\ny\n", "x\ny\nours\n", "x\ntheirs\ny\n"


def sides(base, ours, theirs):
    return {1: base, 2: ours, 3: theirs}


TABLE = {"paths": ["t/*.tsv"], "regen": ["regen-t"]}
SNAPSHOT = {"paths": ["s/*.html"], "regen": ["regen-s"], "merge": "regen"}
DERIVED = {"paths": ["d/*.md"], "regen": ["regen-d"], "merge": "derived"}
BLOCKED = {"paths": ["b/*.txt"], "regen": ["regen-b"], "merge": "derived", "block": ["# begin", "# end"]}
HAND = {"paths": ["h/*.tsv"]}


def run_regenerate(modules, script):
    """regenerate(wt, files, regenerated, provisional, sides) with the script's answers: (its return, the log, what it
    appended to `regenerated` and `provisional`, the files it wrote)."""
    modules = modules if isinstance(modules, list) else [modules]
    log = []
    with tempfile.TemporaryDirectory() as wt, contextlib.ExitStack() as stack:
        def shown(x):
            return str(x).replace(wt, "WT")

        class Done:
            def __init__(self, out="", rc=0):
                self.stdout, self.returncode = out, rc

        def git(w, *args, check=True):
            log.append(shown(f"git {args}"))
            if args[0] == "diff":
                return Done(script.get("stray", ""))
            return Done(f"shown:{args[1]}")

        def run_regen(w, g):
            log.append(shown(f"run_regen {g['regen']}"))
            for f, text in script.get("writes", {}).get(g["regen"][0], {}).items():
                os.makedirs(os.path.dirname(os.path.join(wt, f)), exist_ok=True)
                with open(os.path.join(wt, f), "w") as fh:
                    fh.write(text)
            return script.get("fails", {}).get(g["regen"][0])

        fakes = {
            "git": git, "run_regen": run_regen,
            "generated_registry": lambda w: script["registry"],
            "conflict_stages": lambda w, files: script["stages"],
            "regen_runs": lambda g: script.get("runs", True),
            "unmerged": lambda w: script.get("unmerged", []),
            "tracked": lambda w, globs: script.get("tracked", []),
        }
        for module in modules:
            for name, fake in fakes.items():
                if hasattr(module, name):
                    stack.enter_context(mock.patch.object(module, name, fake))
        for f, text in script.get("exists", {}).items():
            os.makedirs(os.path.dirname(os.path.join(wt, f)), exist_ok=True)
            with open(os.path.join(wt, f), "w") as fh:
                fh.write(text)
        for f in script["files"]:
            os.makedirs(os.path.dirname(os.path.join(wt, f)), exist_ok=True)
        regenerated, provisional = [], []
        entry = next(m for m in modules if hasattr(m, "regenerate"))
        got = entry.regenerate(wt, script["files"], None if script.get("no_lists") else regenerated,
                               None if script.get("no_lists") else provisional, sides=script.get("sides"))
        written = {}
        for f in script["files"] + list(script.get("exists", {})):
            path = os.path.join(wt, f)
            if os.path.exists(path):
                with open(path) as fh:
                    written[f] = fh.read()
    return shown(got), log, regenerated, provisional, written


SCENARIOS = {
    "server role, a repeated key": dict(registry=[TABLE], stages={"t/a.tsv": sides("a\t1\na\t2\n", "a\t1\na\t2\nb\t1\n", "a\t3\n")},
                                        files=["t/a.tsv"], runs=False),
    "stages unreadable": dict(registry=[TABLE], stages=None, files=["t/a.tsv"]),
    "rows merged": dict(registry=[TABLE], stages={"t/a.tsv": sides(BASE, OURS, THEIRS)}, files=["t/a.tsv"]),
    "rows merged, no lists": dict(registry=[TABLE], stages={"t/a.tsv": sides(BASE, OURS, THEIRS)}, files=["t/a.tsv"],
                                  no_lists=True),
    "row clash regenerated": dict(registry=[TABLE], stages={"t/a.tsv": sides(BASE, OURS, CLASH)}, files=["t/a.tsv"],
                                  writes={"regen-t": {"t/a.tsv": "regen\n"}}),
    "row clash, regen fails": dict(registry=[TABLE], stages={"t/a.tsv": sides(BASE, OURS, CLASH)}, files=["t/a.tsv"],
                                   fails={"regen-t": "`regen-t` exited 1"}),
    "row clash, stray files and unmerged": dict(registry=[TABLE], stages={"t/a.tsv": sides(BASE, OURS, CLASH)},
                                                files=["t/a.tsv"], unmerged=["t/a.tsv"], stray="t/z.tsv\n"),
    "row clash, stray files": dict(registry=[TABLE], stages={"t/a.tsv": sides(BASE, OURS, CLASH)}, files=["t/a.tsv"],
                                   stray="t/z.tsv\n"),
    "row clash, hand-written table": dict(registry=[HAND], stages={"h/a.tsv": sides(BASE, OURS, CLASH)}, files=["h/a.tsv"]),
    "row clash on the server role": dict(registry=[TABLE], stages={"t/a.tsv": sides(BASE, OURS, CLASH)}, files=["t/a.tsv"],
                                         runs=False),
    "server role, not a table": dict(registry=[TABLE], stages={"t/a.tsv": sides("x", "y", "z")}, files=["t/a.tsv"],
                                     runs=False),
    "server role, no lists": dict(registry=[TABLE], stages={"t/a.tsv": sides(BASE, OURS, CLASH)}, files=["t/a.tsv"],
                                  runs=False, no_lists=True),
    "server role, a snapshot": dict(registry=[SNAPSHOT], stages={"s/a.html": sides(SNAP_B, SNAP_O, SNAP_T)},
                                    files=["s/a.html"], runs=False),
    "snapshot explained": dict(registry=[SNAPSHOT], stages={"s/a.html": sides(SNAP_B, SNAP_O, SNAP_T)}, files=["s/a.html"],
                               writes={"regen-s": {"s/a.html": "x\ntheirs\ny\nours\n"}}, tracked=["s/b.html"]),
    "snapshot explained, from clean sides": dict(registry=[SNAPSHOT], stages={}, files=["s/a.html"],
                                                 sides={"s/a.html": sides(SNAP_B, SNAP_O, SNAP_T)},
                                                 writes={"regen-s": {"s/a.html": "x\ntheirs\ny\nours\n"}}),
    "snapshot adds a line neither side made": dict(registry=[SNAPSHOT], stages={"s/a.html": sides(SNAP_B, SNAP_O, SNAP_T)},
                                                   files=["s/a.html"],
                                                   writes={"regen-s": {"s/a.html": "x\ntheirs\ny\nours\nodd\n"}}),
    "snapshot drops a line neither side dropped": dict(registry=[SNAPSHOT],
                                                       stages={"s/a.html": sides(SNAP_B, SNAP_O, SNAP_T)}, files=["s/a.html"],
                                                       writes={"regen-s": {"s/a.html": "x\ntheirs\nours\n"}}),
    "snapshot not written": dict(registry=[SNAPSHOT], stages={"s/a.html": sides(SNAP_B, SNAP_O, SNAP_T)}, files=["s/a.html"]),
    "snapshot moved another": dict(registry=[SNAPSHOT], stages={"s/a.html": sides(SNAP_B, SNAP_O, SNAP_T)}, files=["s/a.html"],
                                   writes={"regen-s": {"s/a.html": "x\ntheirs\ny\nours\n", "s/b.html": "moved\n"}},
                                   tracked=["s/b.html"], exists={"s/b.html": "was\n"}),
    "snapshot's regen fails": dict(registry=[SNAPSHOT], stages={"s/a.html": sides(SNAP_B, SNAP_O, SNAP_T)}, files=["s/a.html"],
                                   fails={"regen-s": "`regen-s` ran past 5 s"}),
    "derived regenerated": dict(registry=[DERIVED], stages={"d/a.md": sides("1\n", "2\n", "3\n")}, files=["d/a.md"],
                                writes={"regen-d": {"d/a.md": "9\n"}}),
    "derived, regen fails": dict(registry=[DERIVED], stages={"d/a.md": sides("1\n", "2\n", "3\n")}, files=["d/a.md"],
                                 fails={"regen-d": "boom"}),
    "block merged": dict(registry=[BLOCKED], stages={"b/a.txt": sides("h1\n# begin\nB\n# end\nt\n", "h1\n# begin\nO\n# end\nt\n",
                                                                       "h0\n# begin\nT\n# end\nt\n")},
                         files=["b/a.txt"], writes={"regen-b": {"b/a.txt": "done\n"}}),
    "block's hand-written lines clash": dict(registry=[BLOCKED], stages={"b/a.txt": sides(
        "h\n# begin\nB\n# end\n", "o\n# begin\nO\n# end\n", "t\n# begin\nT\n# end\n")}, files=["b/a.txt"]),
    "two files, two commands": dict(registry=[TABLE, DERIVED], stages={
        "t/a.tsv": sides(BASE, OURS, CLASH), "d/a.md": sides("1\n", "2\n", "3\n"), "t/m.tsv": sides(BASE, OURS, THEIRS)},
        files=["t/a.tsv", "d/a.md", "t/m.tsv"], writes={"regen-t": {"t/a.tsv": "r\n"}, "regen-d": {"d/a.md": "9\n"}}),
}


SET_B, SET_O, SET_T = "x\ty\nq\tr\n", "x\ty\nq\tr\nn\tm\n", "x\ty\n"
ROWS = {
    "each side's own rows": (BASE, OURS, THEIRS, False),
    "a row both changed": (BASE, OURS, CLASH, False),
    "a row both changed, ours kept": (BASE, OURS, CLASH, True),
    "theirs' added row follows the row it followed": ("a\t1\nc\t3\n", "a\t1\nc\t3\n", "a\t1\nb\t2\nc\t3\nd\t4\n", False),
    "theirs' added row first": ("a\t1\n", "a\t1\n", "z\t0\na\t1\n", False),
    "ours deletes, theirs keeps": (BASE, "# h\na\t1\nc\t3\n", BASE, False),
    "both delete one row": (BASE, "# h\na\t1\nc\t3\n", "# h\na\t1\nc\t3\n", False),
    "a set table": (SET_B, SET_O, SET_T, False),
    "a repeated key in a valued table": ("a\t1\na\t2\n", "a\t1\na\t2\n", "a\t1\na\t2\n", False),
    "a header changed by both": ("# h\na\t1\n", "# o\na\t1\n", "# t\na\t1\n", False),
    "a header changed by both, ours kept": ("# h\na\t1\n", "# o\na\t1\n", "# t\na\t1\n", True),
    "a header after a row": ("a\t1\n# h\nb\t2\n", "a\t1\n", "a\t1\n", False),
    "no trailing newline in ours": ("a\t1\n", "a\t2", "a\t1\nb\t3\n", False),
    "empty ours": ("", "", "a\t1\n", False),
}


class GeneratedRegenTest(unittest.TestCase):
    def test_rows_merge_does_what_it_did(self):
        module = importlib.import_module("agentctl.generated")
        for name, (base, ours, theirs, prefer) in ROWS.items():
            with self.subTest(name):
                self.assertEqual(module.rows_merge(base, ours, theirs, prefer), EXPECTED_ROWS[name], name)

    def test_every_scenario_does_what_it_did(self):
        modules = [importlib.import_module(f"agentctl.{m}") for m in MODULES]
        for name, script in SCENARIOS.items():
            with self.subTest(name):
                self.assertEqual(run_regenerate(modules, script), EXPECTED[name], name)


MODULES = ["generated", "generatedregen"]
EXPECTED = {'server role, a repeated key': ('t/a.tsv cannot be read as a table, and the server role runs no regen command',
                                 [],
                                 [],
                                 [],
                                 {}),
 'stages unreadable': ('added or deleted on one side', [], [], [], {}),
 'rows merged': ('None',
                 ["git ('add', '--', 't/a.tsv')"],
                 ['t/a.tsv'],
                 [],
                 {'t/a.tsv': '# h\na\t10\nb\t20\nc\t3\nd\t4\n'}),
 'rows merged, no lists': ('None',
                           ["git ('add', '--', 't/a.tsv')"],
                           [],
                           [],
                           {'t/a.tsv': '# h\na\t10\nb\t20\nc\t3\nd\t4\n'}),
 'row clash regenerated': ('None',
                           ["git ('checkout', '--ours', '--', 't/a.tsv')",
                            "run_regen ['regen-t']",
                            "git ('add', '-A', '--', 't/*.tsv')",
                            "git ('diff', '--name-only')"],
                           ['t/a.tsv'],
                           [],
                           {'t/a.tsv': 'regen\n'}),
 'row clash, regen fails': ('a row both sides changed, and `regen-t` exited 1',
                            ["git ('checkout', '--ours', '--', 't/a.tsv')", "run_regen ['regen-t']"],
                            [],
                            [],
                            {}),
 'row clash, stray files and unmerged': ('`regen-t` left t/a.tsv unresolved',
                                         ["git ('checkout', '--ours', '--', 't/a.tsv')",
                                          "run_regen ['regen-t']",
                                          "git ('add', '-A', '--', 't/*.tsv')"],
                                         [],
                                         [],
                                         {}),
 'row clash, stray files': ('None',
                            ["git ('checkout', '--ours', '--', 't/a.tsv')",
                             "run_regen ['regen-t']",
                             "git ('add', '-A', '--', 't/*.tsv')",
                             "git ('diff', '--name-only')",
                             "git ('checkout', '--', 't/z.tsv')"],
                            ['t/a.tsv'],
                            [],
                            {}),
 'row clash, hand-written table': ('a row both sides changed in h/a.tsv, which no regen command decides',
                                   [],
                                   [],
                                   [],
                                   {}),
 'row clash on the server role': ('None',
                                  ["git ('add', '--', 't/a.tsv')"],
                                  ['t/a.tsv'],
                                  ['t/a.tsv'],
                                  {'t/a.tsv': '# h\na\t1\nb\t20\nc\t3\n'}),
 'server role, not a table': ('None', ["git ('add', '--', 't/a.tsv')"], ['t/a.tsv'], ['t/a.tsv'], {'t/a.tsv': 'y'}),
 'server role, no lists': ('None', ["git ('add', '--', 't/a.tsv')"], [], [], {'t/a.tsv': '# h\na\t1\nb\t20\nc\t3\n'}),
 'server role, a snapshot': ('s/a.html: a snapshot both sides changed, and the server role runs no regen command',
                             [],
                             [],
                             [],
                             {}),
 'snapshot explained': ('None',
                        ["git ('checkout', '--ours', '--', 's/a.html')",
                         "git ('show', ':s/b.html')",
                         "run_regen ['regen-s']",
                         "git ('add', '-A', '--', 's/*.html')",
                         "git ('diff', '--name-only')"],
                        ['s/a.html'],
                        [],
                        {'s/a.html': 'x\ntheirs\ny\nours\n'}),
 'snapshot explained, from clean sides': ('None',
                                          ["run_regen ['regen-s']",
                                           "git ('add', '-A', '--', 's/*.html')",
                                           "git ('diff', '--name-only')"],
                                          ['s/a.html'],
                                          [],
                                          {'s/a.html': 'x\ntheirs\ny\nours\n'}),
 'snapshot adds a line neither side made': ('the regenerated s/a.html holds a change neither side made (+odd): an '
                                            'interaction its authors regenerate and commit',
                                            ["git ('checkout', '--ours', '--', 's/a.html')", "run_regen ['regen-s']"],
                                            [],
                                            [],
                                            {'s/a.html': 'x\ntheirs\ny\nours\nodd\n'}),
 'snapshot drops a line neither side dropped': ('the regenerated s/a.html holds a change neither side made (-y): an '
                                                'interaction its authors regenerate and commit',
                                                ["git ('checkout', '--ours', '--', 's/a.html')",
                                                 "run_regen ['regen-s']"],
                                                [],
                                                [],
                                                {'s/a.html': 'x\ntheirs\nours\n'}),
 'snapshot not written': ('`regen-s` did not write s/a.html',
                          ["git ('checkout', '--ours', '--', 's/a.html')", "run_regen ['regen-s']"],
                          [],
                          [],
                          {}),
 'snapshot moved another': ('the regen also moved s/b.html, which only one side changed: an interaction its authors '
                            'regenerate and commit',
                            ["git ('checkout', '--ours', '--', 's/a.html')",
                             "git ('show', ':s/b.html')",
                             "run_regen ['regen-s']"],
                            [],
                            [],
                            {'s/a.html': 'x\ntheirs\ny\nours\n', 's/b.html': 'moved\n'}),
 "snapshot's regen fails": ('both sides changed the snapshot `regen-s` ran past 5 s',
                            ["git ('checkout', '--ours', '--', 's/a.html')", "run_regen ['regen-s']"],
                            [],
                            [],
                            {}),
 'derived regenerated': ('None',
                         ["git ('checkout', '--ours', '--', 'd/a.md')",
                          "run_regen ['regen-d']",
                          "git ('add', '-A', '--', 'd/*.md')",
                          "git ('diff', '--name-only')"],
                         ['d/a.md'],
                         [],
                         {'d/a.md': '9\n'}),
 'derived, regen fails': ('both sides changed the derived file boom',
                          ["git ('checkout', '--ours', '--', 'd/a.md')", "run_regen ['regen-d']"],
                          [],
                          [],
                          {}),
 'block merged': ('None',
                  ["git ('add', '--', 'b/a.txt')",
                   "run_regen ['regen-b']",
                   "git ('add', '-A', '--', 'b/*.txt')",
                   "git ('diff', '--name-only')"],
                  ['b/a.txt'],
                  [],
                  {'b/a.txt': 'done\n'}),
 "block's hand-written lines clash": ('b/a.txt: both sides changed its hand-written lines, outside the generated '
                                      'block, in one place',
                                      [],
                                      [],
                                      [],
                                      {}),
 'two files, two commands': ('None',
                             ["git ('add', '--', 't/m.tsv')",
                              "git ('checkout', '--ours', '--', 't/a.tsv')",
                              "run_regen ['regen-t']",
                              "git ('add', '-A', '--', 't/*.tsv')",
                              "git ('diff', '--name-only')",
                              "git ('checkout', '--ours', '--', 'd/a.md')",
                              "run_regen ['regen-d']",
                              "git ('add', '-A', '--', 'd/*.md')",
                              "git ('diff', '--name-only')"],
                             ['t/a.tsv', 'd/a.md', 't/m.tsv'],
                             [],
                             {'t/a.tsv': 'r\n', 'd/a.md': '9\n', 't/m.tsv': '# h\na\t10\nb\t20\nc\t3\nd\t4\n'})}
EXPECTED_ROWS = {"each side's own rows": '# h\na\t10\nb\t20\nc\t3\nd\t4\n',
 'a row both changed': None,
 'a row both changed, ours kept': '# h\na\t1\nb\t20\nc\t3\n',
 "theirs' added row follows the row it followed": 'a\t1\nb\t2\nc\t3\nd\t4\n',
 "theirs' added row first": 'z\t0\na\t1\n',
 'ours deletes, theirs keeps': '# h\na\t1\nc\t3\n',
 'both delete one row': '# h\na\t1\nc\t3\n',
 'a set table': 'x\ty\nn\tm\n',
 'a repeated key in a valued table': None,
 'a header changed by both': None,
 'a header changed by both, ours kept': '# o\na\t1\n',
 'a header after a row': None,
 'no trailing newline in ours': 'a\t2\nb\t3',
 'empty ours': 'a\t1\n'}
