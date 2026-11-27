"""WHICH RUNS ARE ONE RED'S EPISODE (the runs a `Fixes-Red:` may name) -- characterised over a seeded matrix.

Before RedEpisode, two copies decided it: redseen.episode_names (the gate's: the newest red run's signatures read
through run_sigs, each streak run's by red_units -- cached, re-read within the tick's budget, else its stale record --
and on an error only the newest run, said in a line and gone) and redrecord.red_episode_names (the queue facts': the
red item's job and line against each streak run's signature CACHED under this classifier, nothing else). MATRIX is what
each answered before the consolidation; the test holds the one source to it, but for the disagreements resolved
deliberately (RESOLVED, each with its own test below)."""
import itertools, os, random, sys, tempfile, unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from test_agentctl import A  # noqa: E402
from harness import case_context  # noqa: E402

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
        case_context().runs.main_runs_of("o/r").gate["Main"] = self.runs
        test.addCleanup(case_context().runs.clear)

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
    gate = A.run_names(w.runs[0]) | A.episode_names("o/r", "Main", w.runs[0], ctx=case_context())
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

# THE DISAGREEMENT, resolved (redrecord.py's header): ONE FALLBACK ORDER for a streak run's signatures -- this
# classifier's record, a read again within the budget, the stale record -- so the queue facts count a stale record that
# names the red's job and line, as the gate did (their budget is 0: they never read the forge, so a stale record whose
# LINE moved still counts for the gate only once a tick re-reads it). The facts' episode, by that order:
MATCHES_A = ("cur-A", "stale-A")


def facts_episode(decided, s7, s6):
    return tuple(n for n, state in ((6, s6), (7, s7), (8, decided)) if state in MATCHES_A)


RESOLVED = {k: (v[0], facts_episode(*k[:3])) for k, v in MATRIX.items() if v[1] != facts_episode(*k[:3])}


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

    def test_the_queue_facts_count_a_stale_record_naming_the_red_s_job_and_line(self):
        """Before: the facts counted only a record of THIS classifier; the gate counted the stale one too."""
        w = World(self, "cur-A", "stale-A", "stale-oldline", "0")
        self.assertEqual(numbers(A.red_episode_names(w.item(), w.snap())), (7, 8))

    def test_an_unreadable_episode_is_surfaced_never_silently_no_episode(self):
        """Before: the newest run's failures unreadable, episode_names said one line and returned nothing -- a fix naming
        an older run waited and nothing said why. Now the facts carry why, and a waiting fix's words say it; a later
        read clears it."""
        w = World(self, "raises", "cur-A", "cur-A", "0")
        ep = A.RedEpisode("o/r", "Main", w.runs, [0]).of_run(w.runs[0])
        self.assertEqual(numbers(ep.names), (8,))
        self.assertIn("could not be read", ep.unread)
        st = A.Store(path=os.path.join(self.tmp.name, "store.db"))
        facts = A.queue_facts({"8"}, st)
        self.assertIn("Main", facts["episode_unread"])
        v = A.may_land_on_red(facts, {"fixes": ["7"]}, ctx=case_context())
        self.assertIn("the episode of Main is unread", v.wait)
        w.states[8] = "cur-A"
        A.RedEpisode("o/r", "Main", w.runs, [0]).of_run(w.runs[0])
        self.assertNotIn("Main", A.queue_facts({"8"}, st)["episode_unread"], "read again: no longer unread")

    def test_the_newest_run_unreadable_falls_back_to_its_stale_record(self):
        """THE FALLBACK ORDER for the reference: run_sigs, else the run's stale record, else unread."""
        w = World(self, "raises", "cur-A", "none", "0")
        w.record(8, "stale-A")
        ep = A.RedEpisode("o/r", "Main", w.runs, [0]).of_run(w.runs[0])
        self.assertEqual((numbers(ep.names), ep.unread), ((7, 8), None))


if __name__ == "__main__":
    unittest.main()
