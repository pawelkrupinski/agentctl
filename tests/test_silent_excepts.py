"""No NEW bare or silent `except` (the review of 2026-11-26: ~85 `except OSError: pass`): a handler whose whole body is
`pass` / `continue` hides a failure, so it either tells it (`note_failure`, `err`), re-raises, or is the one narrow
case the code expects -- and that is a row of tests/silent-excepts-allowlist.tsv with its reason, or one of the
exception types below that only ever mean "already so". A bare `except:` is never allowed."""
import ast
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.join(HERE, "..")
ALLOWLIST = os.path.join(HERE, "silent-excepts-allowlist.tsv")
SCANNED = ("lib/agentctl", "devtools", "quality", "agentctl")
# a silent handler of only these needs no row: each says the thing is already so, never that something broke
ALREADY_SO = {
    "FileNotFoundError": "the file is already gone",
    "ProcessLookupError": "the process is already gone",
    "StaleVersion": "another writer moved the item first: the transition is theirs",
    "TerminalState": "the item already ended: nothing to move",
    "IllegalTransition": "the item is no longer where the move starts: nothing to do",
}


def _names(node):
    t = node.type
    if t is None:
        return ("<bare>",)
    return tuple(ast.unparse(e) for e in (t.elts if isinstance(t, ast.Tuple) else [t]))


def _silent(handler):
    return all(isinstance(b, (ast.Pass, ast.Continue)) or (isinstance(b, ast.Expr) and isinstance(b.value, ast.Constant))
               for b in handler.body)


def silent_excepts(root=ROOT):
    """[(file, enclosing def, exception types)] of every bare or silent handler under the scanned places."""
    found = []
    paths = []
    for rel in SCANNED:
        p = os.path.join(root, rel)
        if os.path.isfile(p):
            paths.append((rel, p))
        elif os.path.isdir(p):
            paths += [(f"{rel}/{n}", os.path.join(p, n)) for n in sorted(os.listdir(p)) if n.endswith(".py")]
    for rel, p in paths:
        tree = ast.parse(open(p).read())
        stack = []

        def walk(node):
            scope = isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
            if scope:
                stack.append(node.name)
            if isinstance(node, ast.ExceptHandler) and (node.type is None or _silent(node)):
                found.append((rel, ".".join(stack) or "<module>", ", ".join(_names(node))))
            for c in ast.iter_child_nodes(node):
                walk(c)
            if scope:
                stack.pop()
        walk(tree)
    return found


def allowlisted(path=ALLOWLIST):
    rows = set()
    for line in open(path).read().split("\n"):
        if line and not line.startswith("#"):
            f, scope, types, why = line.split("\t")
            assert len(why) >= 20, f"{f} {scope}: the reason must say why the swallow is intended"
            rows.add((f, scope, types))
    return rows


def unexplained(found, rows):
    return [x for x in found if x not in rows and (x[2] == "<bare>" or not set(x[2].split(", ")) <= set(ALREADY_SO))]


class SilentExceptsTest(unittest.TestCase):
    def test_no_new_bare_or_silent_except(self):
        bad = unexplained(silent_excepts(), allowlisted())
        self.assertEqual(bad, [], "tell it (statefile.note_failure), narrow it, re-raise it, or add a row with its reason "
                                  "to tests/silent-excepts-allowlist.tsv")

    def test_every_allowlist_row_still_matches_a_handler(self):
        stale = allowlisted() - set(silent_excepts())
        self.assertEqual(sorted(stale), [], "a row whose handler is gone or no longer silent: strike it")

    def test_the_lint_sees_a_silent_and_a_bare_handler_and_lets_a_told_one_pass(self):
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            os.makedirs(os.path.join(d, "devtools"))
            open(os.path.join(d, "devtools", "x.py"), "w").write(
                "def a():\n    try:\n        f()\n    except OSError:\n        pass\n"
                "def b():\n    try:\n        f()\n    except:\n        raise\n"
                "def c():\n    try:\n        f()\n    except OSError as e:\n        note_failure('x', e)\n"
                "def d():\n    try:\n        f()\n    except FileNotFoundError:\n        pass\n")
            found = silent_excepts(d)
        self.assertEqual(found, [("devtools/x.py", "a", "OSError"), ("devtools/x.py", "b", "<bare>"),
                                 ("devtools/x.py", "d", "FileNotFoundError")])
        self.assertEqual(unexplained(found, set()), [("devtools/x.py", "a", "OSError"), ("devtools/x.py", "b", "<bare>")])


if __name__ == "__main__":
    unittest.main()
