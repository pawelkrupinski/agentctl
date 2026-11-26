"""RESTRUCTURE ROUND 10: each function split into named stages keeps its behaviour. The digests (fixtures/
restructure10-digests.json, recorded from the code BEFORE the split with `R4_RECORD=1`) cover thousands of seeded cases
each, collaborators faked (golden_cases.py): every call, its order and the result."""
import builtins, contextlib, io, os, sys, types, unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from test_agentctl import A  # noqa: E402
from golden_cases import Probe, check as _check  # noqa: E402

DIGESTS = os.path.join(HERE, "fixtures", "restructure10-digests.json")
NOW = 1_795_000_000.0


def check(test, name, build, fn_of, n=3000):
    _check(test, A, name, build, fn_of, n, DIGESTS)


def maybe(rng, value, p=0.5):
    return value if rng.random() < p else None


def pick(r, *values):
    return r.choice(values)


# ------------------------------------------------------------------------------------------------ repos
GEN_PATHS = [["a/b.tsv"], ["x", "y/*.json"], [], None, ["/abs"], [""], [1], "str", ["ok", ""]]
GEN_REGEN = [None, None, "make gen", "  ", "", 5, "scripts/regen --flag 'a b'"]


def a_generated(r):
    g = {}
    for key, gen in (("paths", lambda: r.choice(GEN_PATHS)), ("regen", lambda: r.choice(GEN_REGEN)),
                     ("timeout", lambda: r.choice([None, 5, 0, -1, 2.5, "9", True])),
                     ("merge", lambda: r.choice(["rows", "regen", "derived", "bogus", None])),
                     ("block", lambda: r.choice([["# begin", "# end"], ["a"], "x", [1, 2], [], None, ["a", "b", "c"]])),
                     ("order_sensitive", lambda: r.choice([True, False, 1, "yes"])),
                     ("holds", lambda: r.choice([True, []]))):
        if r.random() < (0.9 if key in ("paths",) else 0.35 if key != "holds" else 0.05):
            g[key] = gen()
    return g


class GeneratedParseTest(unittest.TestCase):
    def build(self, case):
        r = case.rng
        raw = r.choice([None, [], [a_generated(r) for _ in range(r.randint(1, 3))]])
        if raw and r.random() < 0.05:
            raw.append(None)
        return ("o/r", raw)

    def test_generated_parse(self):
        check(self, "generated_parse", self.build, lambda A: A.generated_parse, 4000)


def a_repo_entry(r):
    x = {}
    for key, gen in (("repo", lambda: r.choice(["o/n", "pawelkrupinski/movies", "bad", "a/b/c", 5, "x.y/z-w", ""])),
                     ("gate", lambda: r.choice([[["W", "w.yml"]], [["W"]], [["W", ""]], [], None, "s", [["W", "f"], ["X", "g"]], [("W", "f")], [["W", "f", "g"]]])),
                     ("push", lambda: r.choice(["land", "owner-only", "selfpush", "bogus", None, ""])),
                     ("clone", lambda: r.choice(["/home/c", "/x/y", None, ""])),
                     ("red_workflows", lambda: r.choice([True, False, 1, "y"])),
                     ("signed", lambda: r.choice([True, False, 0, None])),
                     ("family", lambda: r.choice(["f", None])), ("note", lambda: "n"),
                     ("generated", lambda: r.choice([None, [a_generated(r)], [a_generated(r), a_generated(r)]]))):
        if key == "repo" or r.random() < 0.6:
            x[key] = gen()
    return x


class RedReposParseTest(unittest.TestCase):
    def build(self, case):
        r = case.rng
        n = r.choice([0, 1, 1, 2, 3])
        entries = [a_repo_entry(r) for _ in range(n)]
        if r.random() < 0.3 and entries:
            entries.append(dict(entries[0]))
        if r.random() < 0.3:
            entries.append({"repo": "pawelkrupinski/movies", **{k: v for k, v in a_repo_entry(r).items() if k != "repo"}})
        return (r.choice([None, {}, {"repos": entries}, {"repos": entries}, {"repos": None}]),)

    def test_red_repos_parse(self):
        check(self, "red_repos_parse", self.build, lambda A: A.red_repos_parse, 6000)


if __name__ == "__main__":
    unittest.main()
