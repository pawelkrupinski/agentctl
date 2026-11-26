"""RESTRUCTURE ROUND 14: each function split into named stages keeps its behaviour. The digests (fixtures/
restructure14-digests.json, recorded from the code BEFORE the split with `R4_RECORD=1`) cover thousands of seeded cases
each, collaborators faked (golden_cases.py): every call, its order and the result. Every case is bounded."""
import contextlib, io, os, sys, types, unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from test_agentctl import A  # noqa: E402
from golden_cases import Probe, check as _check  # noqa: E402

DIGESTS = os.path.join(HERE, "fixtures", "restructure14-digests.json")
NOW = 1_795_000_000.0


def check(test, name, build, fn_of, n=3000):
    _check(test, A, name, build, fn_of, n, DIGESTS)


def maybe(rng, value, p=0.5):
    return value if rng.random() < p else None


def raising(rng, p, value, exc=RuntimeError):
    if rng.random() < p:
        raise exc("down")
    return value


def proc(rng, code=0, out=""):
    return types.SimpleNamespace(returncode=code, stdout=out, stderr="")


class Quiet(contextlib.nullcontext):
    def __repr__(self):
        return "<quiet>"


# ------------------------------------------------------------------------------------------------ redauthors
class RedAuthorsTest(unittest.TestCase):
    def build(self, case):
        r = case.rng
        shas = [f"{r.randint(0, 9)}{c}{c}{c}{c}" for c in "abcdefg"][:r.randint(0, 7)]
        case.fake("red_commits", lambda rng, repo, u: list(shas))
        wts = [f"/wt/{i}" for i in range(4)]

        def heads(rng, repo):
            out = {}
            for s in shas:
                if rng.random() < 0.3:
                    out[rng.choice([s, s[:3], s + "x"])] = {"worktree": rng.choice(wts + [None, "/not/allowed"]),
                                                            "session": rng.choice([None, "s1", "s2"])}
            return out
        case.fake("landed_heads", heads)
        case.fake("worktree_holding", lambda rng, repo, c: maybe(rng, rng.choice(wts), 0.5))
        case.fake("allowed_path", lambda rng, p: p != "/not/allowed" and rng.random() < 0.9)
        case.patch(os.path, "isdir", lambda rng, p: rng.random() < 0.9)
        entries = [{"at": r.choice([0, NOW - 10, NOW - 10 ** 5]), "cwd": r.choice(wts + [None])} for _ in range(r.randint(0, 3))]
        case.fake("inbox_entries", lambda rng: raising(rng, 0.1, entries, OSError))
        case.fake("agent_pids_in", lambda rng, ws: {w: rng.sample([11, 12, 13], rng.randint(0, 2)) for w in ws if rng.random() < 0.7})
        case.fake("env_num", lambda rng, n, d: rng.choice([d, 0, 10 ** 9]))
        case.fake("worktree_activity", lambda rng, repo, wt, ents: rng.choice([0.0, NOW - 60, NOW - 900, NOW, NOW - 10 ** 5]))
        case.fake("proc_alive", lambda rng, p: rng.random() < 0.6)
        case.put("wall_epoch", lambda: NOW)
        return ("/repo" if r.random() < 0.95 else "", {"run": {}}, r.choice([None, NOW, NOW + 100]))

    def test_red_authors(self):
        check(self, "red_authors", self.build, lambda A: A.red_authors, 6000)


if __name__ == "__main__":
    unittest.main()
