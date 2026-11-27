"""NO BARE `Any` TO LOWER THE MYPY COUNT (the owner, 2026-11-26). Round 16 (01e78e1) annotated ~1,000 parameters and returns
of unannotated functions as plain `Any`, which cut `mypy --strict`'s errors from ~4,400 to 3,835 without checking a line:
`Any` says nothing a missing annotation did not, and hides the function's untyped calls from the measure. They were
removed (a real type only where the default or the returns say it: `bool`, `int`, `str`, `None`). Each module round 16
touched keeps the bare-`Any` annotations it had before it -- the count does not rise; a signature gets its real type or
none."""
import ast, os, sys, unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from test_agentctl import A  # noqa: E402,F401  (the harness: HOME, PATH, git config)

LIB = os.path.join(os.path.dirname(HERE), "lib", "agentctl")

# the bare-`Any` parameters and returns of each module round 16 touched, as they were before it
BEFORE_ROUND_16 = {
    "batchrun": 4, "buildview": 6, "gating": 11, "issues": 6, "land": 32, "laptoprole": 5, "live": 4, "localrecheck": 2,
    "maint": 61, "orphanreap": 35, "eventprojection": 6, "landedchange": 23, "changerows": 1, "changelap": 53, "checkround": 11, "queueslots": 10, "recordingpins": 0, "redclaim": 23,
    "redhold": 25, "checkrunjobs": 4, "remotestore": 0, "statuspage": 9, "syncsteps": 10, "waiting": 19,
}
# a module folded into one of them brings its own (R2's pure moves, 2026-11-26): its count before the fold
FOLDED_IN = {"redclaim": {"redownerpush": 17}}


def bare_any(path):
    """How many parameters and returns of a module are annotated with plain `Any`."""
    n = 0
    for fn in ast.walk(ast.parse(open(path).read())):
        if isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
            a = fn.args
            args = [*a.posonlyargs, *a.args, *a.kwonlyargs, *([a.vararg] if a.vararg else []), *([a.kwarg] if a.kwarg else [])]
            notes = [x.annotation for x in args] + [fn.returns]
            n += sum(isinstance(x, ast.Name) and x.id == "Any" for x in notes)
    return n


class NoGamedAnyTest(unittest.TestCase):
    def test_round_16_modules_gain_no_bare_any(self):
        before = {m: was + sum(FOLDED_IN.get(m, {}).values()) for m, was in BEFORE_ROUND_16.items()}
        rose = {m: (bare_any(os.path.join(LIB, m + ".py")), was) for m, was in before.items()
                if bare_any(os.path.join(LIB, m + ".py")) > was}
        self.assertEqual({}, rose, "a bare `Any` annotation added to lower the mypy count: give the real type, or none")


if __name__ == "__main__":
    unittest.main()
