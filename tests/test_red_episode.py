"""WHICH RUNS ARE ONE RED'S EPISODE (the runs a `Fixes-Red:` may name) -- characterised over a seeded matrix.

Before RedEpisode, two copies decided it: redseen.episode_names (the gate's: the newest red run's signatures read
through run_sigs, each streak run's by red_units -- cached, re-read within the tick's budget, else its stale record --
and on an error only the newest run, said in a line and gone) and redepisode.red_episode_names (the queue facts': the
red item's job and line against each streak run's signature CACHED under this classifier, nothing else). MATRIX is what
each answered before the consolidation; the test holds the one source to it, but for the disagreements resolved
deliberately (RESOLVED, each with its own test below)."""
import itertools, os, random, sys, tempfile, unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from test_agentctl import A  # noqa: E402

# a streak run's signature: what its cached record says (classifier current or stale) and what a fresh read finds
SIG_STATES = ("cur-A", "cur-B", "stale-A", "stale-oldline", "none", "broken")
DECIDED_STATES = ("cur-A", "stale-A", "raises")
BUDGETS = ("0", "3")
SEEDS = 40


def cells():
    rng = random.Random(20261126)
    out = []
    for _ in range(SEEDS):
        out.append((rng.choice(DECIDED_STATES), rng.choice(SIG_STATES), rng.choice(SIG_STATES), rng.choice(BUDGETS)))
    return sorted(set(out))


def run(n, ok=False):
    return {"number": n, "databaseId": 1000 + n, "status": "completed", "conclusion": "success" if ok else "failure",
            "headBranch": "main"}


class World:
    """The runs of Main (8, 7, 6 red; 5 green; 4 red) with each red run's signature in its state; run_sigs faked."""

    def __init__(self, test, decided, s7, s6, budget):
        self.states = {8: decided if decided != "raises" else "broken", 7: s7, 6: s6, 4: "cur-A"}
        self.runs = [run(8), run(7), run(6), run(5, ok=True), run(4)]
        for n, state in self.states.items():
            self.record(n, state)
        os.environ["AGENTCTL_STREAK_REREADS"] = budget
        test.addCleanup(os.environ.pop, "AGENTCTL_STREAK_REREADS", None)
        test.addCleanup(setattr, A, "run_sigs", A.run_sigs)
        A.run_sigs = self.run_sigs
        A._gate_lists["Main"] = self.runs
        test.addCleanup(A._gate_lists.pop, "Main", None)

    def record(self, n, state):
        line = {"cur-A": "Failed: A", "cur-B": "Failed: B", "stale-A": "Failed: A", "stale-oldline": "WARN a"}.get(state)
        if line:
            A.write_json(A.reds_dir("runsig", f"{1000 + n}.json"), {
                "classifier": A.classifier_version() if state.startswith("cur") else "old",
                "sigs": [{"job": "Unit tests", "kind": "test", "line": line}]})

    def run_sigs(self, o_r, r, workflow, now_=None):
        state = self.states[r["number"]]
        if state == "broken":
            raise RuntimeError(f"run {r['number']}: log unreadable")
        return [{"job": "Unit tests", "kind": "test", "line": "Failed: B" if state == "cur-B" else "Failed: A"}]

    def item(self):
        return {"workflow": "Main", "gate": True, "job": "Unit tests", "line": "Failed: A", "repo": "o/r"}

    def snap(self):
        return {"gate": [{"name": "Main", "runs": self.runs}]}


def numbers(names):
    return tuple(sorted(int(n) for n in names if int(n) < 1000))


def decisions(test, cell):
    """(the gate's episode -- gate_reds' names of the newest red run: its own and its episode's --, the queue facts'
    episode) of one cell, as run numbers."""
    w = World(test, *cell)
    gate = A.run_names(w.runs[0]) | A.episode_names("o/r", "Main", w.runs[0])
    return (numbers(gate), numbers(A.red_episode_names(w.item(), w.snap())))


# (decided, run 7, run 6, budget): (the gate's episode, the queue facts' episode), before the consolidation
MATRIX = {
    ('cur-A', 'broken', 'cur-A', '3'): ((6, 8), (6, 8)),
    ('cur-A', 'cur-A', 'stale-A', '3'): ((6, 7, 8), (7, 8)),
    ('cur-A', 'cur-B', 'cur-B', '3'): ((8,), (8,)),
    ('cur-A', 'none', 'stale-oldline', '3'): ((6, 7, 8), (8,)),
    ('cur-A', 'stale-A', 'broken', '3'): ((7, 8), (8,)),
    ('cur-A', 'stale-A', 'cur-B', '0'): ((7, 8), (8,)),
    ('cur-A', 'stale-A', 'none', '0'): ((7, 8), (8,)),
    ('cur-A', 'stale-oldline', 'broken', '0'): ((8,), (8,)),
    ('cur-A', 'stale-oldline', 'broken', '3'): ((7, 8), (8,)),
    ('cur-A', 'stale-oldline', 'cur-A', '0'): ((6, 8), (6, 8)),
    ('cur-A', 'stale-oldline', 'stale-A', '0'): ((6, 8), (8,)),
    ('cur-A', 'stale-oldline', 'stale-A', '3'): ((6, 7, 8), (8,)),
    ('raises', 'broken', 'none', '3'): ((8,), ()),
    ('raises', 'broken', 'stale-A', '0'): ((8,), ()),
    ('raises', 'broken', 'stale-oldline', '3'): ((8,), ()),
    ('raises', 'cur-A', 'cur-A', '3'): ((8,), (6, 7)),
    ('raises', 'cur-A', 'none', '0'): ((8,), (7,)),
    ('raises', 'cur-A', 'stale-A', '3'): ((8,), (7,)),
    ('raises', 'cur-B', 'cur-A', '3'): ((8,), (6,)),
    ('raises', 'cur-B', 'none', '0'): ((8,), ()),
    ('raises', 'none', 'cur-A', '3'): ((8,), (6,)),
    ('raises', 'none', 'stale-oldline', '3'): ((8,), ()),
    ('raises', 'stale-A', 'stale-oldline', '0'): ((8,), ()),
    ('stale-A', 'broken', 'cur-A', '3'): ((6, 8), (6,)),
    ('stale-A', 'broken', 'stale-A', '0'): ((6, 8), ()),
    ('stale-A', 'broken', 'stale-A', '3'): ((6, 8), ()),
    ('stale-A', 'cur-B', 'cur-A', '0'): ((6, 8), (6,)),
    ('stale-A', 'cur-B', 'none', '0'): ((8,), ()),
    ('stale-A', 'none', 'stale-A', '3'): ((6, 7, 8), ()),
    ('stale-A', 'stale-A', 'broken', '0'): ((7, 8), ()),
    ('stale-A', 'stale-A', 'cur-A', '3'): ((6, 7, 8), (6,)),
    ('stale-A', 'stale-A', 'cur-B', '0'): ((7, 8), ()),
    ('stale-A', 'stale-A', 'stale-A', '0'): ((6, 7, 8), ()),
    ('stale-A', 'stale-A', 'stale-A', '3'): ((6, 7, 8), ()),
    ('stale-A', 'stale-oldline', 'cur-A', '0'): ((6, 8), (6,)),
}

RESOLVED = {}


class RedEpisodeMatrixTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        os.environ["AGENTCTL_REDS_DIR"] = os.path.join(self.tmp.name, "reds")
        self.addCleanup(os.environ.pop, "AGENTCTL_REDS_DIR", None)

    def test_the_one_source_answers_as_both_copies_did_but_for_the_resolved_disagreements(self):
        got = {}
        for cell in cells():
            for f in os.listdir(A.reds_dir("runsig")) if os.path.isdir(A.reds_dir("runsig")) else ():
                os.remove(A.reds_dir("runsig", f))
            got[cell] = decisions(self, cell)
        want = {k: RESOLVED.get(k, v) for k, v in MATRIX.items()}
        diff = {k: (want.get(k), v) for k, v in got.items() if want.get(k) != v}
        self.assertEqual(diff, {}, "\n" + "\n".join(f"    {k!r}: {v!r}," for k, v in sorted(got.items())))


if __name__ == "__main__":
    unittest.main()
