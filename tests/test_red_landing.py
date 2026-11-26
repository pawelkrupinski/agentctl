"""MAY A CHANGE LAND WHILE THE GATE IS RED? -- characterised over a seeded matrix of queue and red states.

Before redland.py, three copies decided it: waiting.must_wait_why (per commit: every production commit's COUNTING
`Fixes-Red:` must name a run of the gate's red or of an open gate red the store holds), changekind.classify_change's
red_fix / gate_fix (per entry: the names recorded at submission against the queue facts), and queueclose.check_entry's
red-fix tree guard (`bool(e.get("fixes")) and ... ["red_fix"]`); gov.gov_class_of reads the commits' counting fixes
through the classifier. They diverged once (batch #124, 2026-11-25). MATRIX is what every copy answered before the
consolidation, for each (gate, store, change) of the seeded matrix; the test holds the one rule to it, but for the
disagreements resolved deliberately (RESOLVED, each with its own test below)."""
import itertools, os, sys, tempfile, unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from test_agentctl import A  # noqa: E402

GATES = {"green": set(), "red": {"8", "98000"}, "unread": {"unread"}}
STORES = ("none", "gate-item", "outside-item", "both", "unreadable")
# a change: its commits as (production?, the Fixes-Red values that count NOW), and the names its entry recorded
CHANGES = {
    "tools": ([(False, ())], ()),
    "tools-fix8": ([(False, ("8",))], ("8",)),
    "prod": ([(True, ())], ()),
    "prod-fix8": ([(True, ("8",))], ("8",)),
    "prod-fix7": ([(True, ("7",))], ("7",)),
    "prod-fix6": ([(True, ("6",))], ("6",)),
    "prod-fix404": ([(True, ("404",))], ("404",)),
    "mixed": ([(True, ("8",)), (True, ())], ("8",)),
    "mixed7": ([(True, ("7",)), (True, ())], ("7",)),
    "tools-fix-prod": ([(False, ("8",)), (True, ())], ("8",)),
    "stale": ([(True, ())], ("8",)),          # its entry says 8; the commit's trailer no longer counts (fix_check)
    "stale7": ([(True, ())], ("7",)),
}


class Unreadable:
    def items(self, *a, **k):
        raise OSError("store unreadable")


def world(test, store_kind):
    st = A.Store(path=os.path.join(test.tmp.name, f"{store_kind}.db"))
    if store_kind in ("gate-item", "both"):
        st.put_item("red", "k7", "leased", {"key": "k7", "workflow": "Main", "gate": True, "runs": ["7", "8"]})
    if store_kind in ("outside-item", "both"):
        st.put_item("red", "k6", "leased", {"key": "k6", "workflow": "Alert", "gate": False, "red_kind": "outside",
                                            "runs": ["6"]})
    return Unreadable() if store_kind == "unreadable" else st


def fake_commits(test, commits):
    """The git seam: commit c<i> is production or not and carries the counting values given."""
    shas = [f"c{i}" for i in range(len(commits))]
    prod = {s: p for s, (p, _f) in zip(shas, commits)}
    fixes = {s: set(f) for s, (_p, f) in zip(shas, commits)}
    for name, fake in (("touches_production", lambda repo, c: prod[c]),
                       ("commit_fixes", lambda repo, c, store: set(fixes[c]))):
        test.addCleanup(setattr, A, name, getattr(A, name))
        setattr(A, name, fake)
    return shas


def decisions(test, gate, store_kind, change):
    """What each decision answers for one cell: (may land on red, red fix, gate fix, red-fix tree, governor class)."""
    reds = GATES[gate]
    commits, names = CHANGES[change]
    st = world(test, store_kind)
    shas = fake_commits(test, commits)
    entry = {"id": "e", "fixes": list(names), "commits": shas, "status": "queued"}
    facts = A.queue_facts(reds, st)
    k = A.classify_change(entry, facts, 0, "/repo", st)
    may = not reds or A.must_wait_why("/repo", shas, reds, st) is None
    tree = A.may_land_on_red(facts, entry, "/repo", store=st).red_fix  # queueclose.check_entry's guard
    return (may, k["red_fix"], k["gate_fix"], tree, A.gov_class_of("/repo", shas, reds, st))


# what every copy answered before (computed from waiting.must_wait_why, classify_change, the check_entry guard and
# gov_class_of at the parent of the consolidation), cell by cell; "may" is not must_wait_why (the gate red) or True
MATRIX = {
    ('green', 'both', 'mixed'): (True, True, True, True, 'red'),
    ('green', 'both', 'mixed7'): (True, True, True, True, 'red'),
    ('green', 'both', 'prod'): (True, False, False, False, 'check'),
    ('green', 'both', 'prod-fix404'): (True, False, False, False, 'check'),
    ('green', 'both', 'prod-fix6'): (True, True, False, True, 'red'),
    ('green', 'both', 'prod-fix7'): (True, True, True, True, 'red'),
    ('green', 'both', 'prod-fix8'): (True, True, True, True, 'red'),
    ('green', 'both', 'stale'): (True, True, True, True, 'check'),
    ('green', 'both', 'stale7'): (True, True, True, True, 'check'),
    ('green', 'both', 'tools'): (True, False, False, False, 'check'),
    ('green', 'both', 'tools-fix-prod'): (True, True, True, True, 'red'),
    ('green', 'both', 'tools-fix8'): (True, True, True, True, 'red'),
    ('green', 'gate-item', 'mixed'): (True, True, True, True, 'red'),
    ('green', 'gate-item', 'mixed7'): (True, True, True, True, 'red'),
    ('green', 'gate-item', 'prod'): (True, False, False, False, 'check'),
    ('green', 'gate-item', 'prod-fix404'): (True, False, False, False, 'check'),
    ('green', 'gate-item', 'prod-fix6'): (True, False, False, False, 'check'),
    ('green', 'gate-item', 'prod-fix7'): (True, True, True, True, 'red'),
    ('green', 'gate-item', 'prod-fix8'): (True, True, True, True, 'red'),
    ('green', 'gate-item', 'stale'): (True, True, True, True, 'check'),
    ('green', 'gate-item', 'stale7'): (True, True, True, True, 'check'),
    ('green', 'gate-item', 'tools'): (True, False, False, False, 'check'),
    ('green', 'gate-item', 'tools-fix-prod'): (True, True, True, True, 'red'),
    ('green', 'gate-item', 'tools-fix8'): (True, True, True, True, 'red'),
    ('green', 'none', 'mixed'): (True, False, False, False, 'check'),
    ('green', 'none', 'mixed7'): (True, False, False, False, 'check'),
    ('green', 'none', 'prod'): (True, False, False, False, 'check'),
    ('green', 'none', 'prod-fix404'): (True, False, False, False, 'check'),
    ('green', 'none', 'prod-fix6'): (True, False, False, False, 'check'),
    ('green', 'none', 'prod-fix7'): (True, False, False, False, 'check'),
    ('green', 'none', 'prod-fix8'): (True, False, False, False, 'check'),
    ('green', 'none', 'stale'): (True, False, False, False, 'check'),
    ('green', 'none', 'stale7'): (True, False, False, False, 'check'),
    ('green', 'none', 'tools'): (True, False, False, False, 'check'),
    ('green', 'none', 'tools-fix-prod'): (True, False, False, False, 'check'),
    ('green', 'none', 'tools-fix8'): (True, False, False, False, 'check'),
    ('green', 'outside-item', 'mixed'): (True, False, False, False, 'check'),
    ('green', 'outside-item', 'mixed7'): (True, False, False, False, 'check'),
    ('green', 'outside-item', 'prod'): (True, False, False, False, 'check'),
    ('green', 'outside-item', 'prod-fix404'): (True, False, False, False, 'check'),
    ('green', 'outside-item', 'prod-fix6'): (True, True, False, True, 'red'),
    ('green', 'outside-item', 'prod-fix7'): (True, False, False, False, 'check'),
    ('green', 'outside-item', 'prod-fix8'): (True, False, False, False, 'check'),
    ('green', 'outside-item', 'stale'): (True, False, False, False, 'check'),
    ('green', 'outside-item', 'stale7'): (True, False, False, False, 'check'),
    ('green', 'outside-item', 'tools'): (True, False, False, False, 'check'),
    ('green', 'outside-item', 'tools-fix-prod'): (True, False, False, False, 'check'),
    ('green', 'outside-item', 'tools-fix8'): (True, False, False, False, 'check'),
    ('green', 'unreadable', 'mixed'): (True, False, False, False, 'check'),
    ('green', 'unreadable', 'mixed7'): (True, False, False, False, 'check'),
    ('green', 'unreadable', 'prod'): (True, False, False, False, 'check'),
    ('green', 'unreadable', 'prod-fix404'): (True, False, False, False, 'check'),
    ('green', 'unreadable', 'prod-fix6'): (True, False, False, False, 'check'),
    ('green', 'unreadable', 'prod-fix7'): (True, False, False, False, 'check'),
    ('green', 'unreadable', 'prod-fix8'): (True, False, False, False, 'check'),
    ('green', 'unreadable', 'stale'): (True, False, False, False, 'check'),
    ('green', 'unreadable', 'stale7'): (True, False, False, False, 'check'),
    ('green', 'unreadable', 'tools'): (True, False, False, False, 'check'),
    ('green', 'unreadable', 'tools-fix-prod'): (True, False, False, False, 'check'),
    ('green', 'unreadable', 'tools-fix8'): (True, False, False, False, 'check'),
    ('red', 'both', 'mixed'): (False, True, True, True, 'red'),
    ('red', 'both', 'mixed7'): (False, True, True, True, 'red'),
    ('red', 'both', 'prod'): (False, False, False, False, 'check'),
    ('red', 'both', 'prod-fix404'): (False, False, False, False, 'check'),
    ('red', 'both', 'prod-fix6'): (False, True, False, True, 'red'),
    ('red', 'both', 'prod-fix7'): (True, True, True, True, 'red'),
    ('red', 'both', 'prod-fix8'): (True, True, True, True, 'red'),
    ('red', 'both', 'stale'): (False, True, True, True, 'check'),
    ('red', 'both', 'stale7'): (False, True, True, True, 'check'),
    ('red', 'both', 'tools'): (True, False, False, False, 'check'),
    ('red', 'both', 'tools-fix-prod'): (False, True, True, True, 'red'),
    ('red', 'both', 'tools-fix8'): (True, True, True, True, 'red'),
    ('red', 'gate-item', 'mixed'): (False, True, True, True, 'red'),
    ('red', 'gate-item', 'mixed7'): (False, True, True, True, 'red'),
    ('red', 'gate-item', 'prod'): (False, False, False, False, 'check'),
    ('red', 'gate-item', 'prod-fix404'): (False, False, False, False, 'check'),
    ('red', 'gate-item', 'prod-fix6'): (False, False, False, False, 'check'),
    ('red', 'gate-item', 'prod-fix7'): (True, True, True, True, 'red'),
    ('red', 'gate-item', 'prod-fix8'): (True, True, True, True, 'red'),
    ('red', 'gate-item', 'stale'): (False, True, True, True, 'check'),
    ('red', 'gate-item', 'stale7'): (False, True, True, True, 'check'),
    ('red', 'gate-item', 'tools'): (True, False, False, False, 'check'),
    ('red', 'gate-item', 'tools-fix-prod'): (False, True, True, True, 'red'),
    ('red', 'gate-item', 'tools-fix8'): (True, True, True, True, 'red'),
    ('red', 'none', 'mixed'): (False, True, True, True, 'red'),
    ('red', 'none', 'mixed7'): (False, False, False, False, 'check'),
    ('red', 'none', 'prod'): (False, False, False, False, 'check'),
    ('red', 'none', 'prod-fix404'): (False, False, False, False, 'check'),
    ('red', 'none', 'prod-fix6'): (False, False, False, False, 'check'),
    ('red', 'none', 'prod-fix7'): (False, False, False, False, 'check'),
    ('red', 'none', 'prod-fix8'): (True, True, True, True, 'red'),
    ('red', 'none', 'stale'): (False, True, True, True, 'check'),
    ('red', 'none', 'stale7'): (False, False, False, False, 'check'),
    ('red', 'none', 'tools'): (True, False, False, False, 'check'),
    ('red', 'none', 'tools-fix-prod'): (False, True, True, True, 'red'),
    ('red', 'none', 'tools-fix8'): (True, True, True, True, 'red'),
    ('red', 'outside-item', 'mixed'): (False, True, True, True, 'red'),
    ('red', 'outside-item', 'mixed7'): (False, False, False, False, 'check'),
    ('red', 'outside-item', 'prod'): (False, False, False, False, 'check'),
    ('red', 'outside-item', 'prod-fix404'): (False, False, False, False, 'check'),
    ('red', 'outside-item', 'prod-fix6'): (False, True, False, True, 'red'),
    ('red', 'outside-item', 'prod-fix7'): (False, False, False, False, 'check'),
    ('red', 'outside-item', 'prod-fix8'): (True, True, True, True, 'red'),
    ('red', 'outside-item', 'stale'): (False, True, True, True, 'check'),
    ('red', 'outside-item', 'stale7'): (False, False, False, False, 'check'),
    ('red', 'outside-item', 'tools'): (True, False, False, False, 'check'),
    ('red', 'outside-item', 'tools-fix-prod'): (False, True, True, True, 'red'),
    ('red', 'outside-item', 'tools-fix8'): (True, True, True, True, 'red'),
    ('red', 'unreadable', 'mixed'): (False, True, True, True, 'red'),
    ('red', 'unreadable', 'mixed7'): (False, False, False, False, 'check'),
    ('red', 'unreadable', 'prod'): (False, False, False, False, 'check'),
    ('red', 'unreadable', 'prod-fix404'): (False, False, False, False, 'check'),
    ('red', 'unreadable', 'prod-fix6'): (False, False, False, False, 'check'),
    ('red', 'unreadable', 'prod-fix7'): (False, False, False, False, 'check'),
    ('red', 'unreadable', 'prod-fix8'): (True, True, True, True, 'red'),
    ('red', 'unreadable', 'stale'): (False, True, True, True, 'check'),
    ('red', 'unreadable', 'stale7'): (False, False, False, False, 'check'),
    ('red', 'unreadable', 'tools'): (True, False, False, False, 'check'),
    ('red', 'unreadable', 'tools-fix-prod'): (False, True, True, True, 'red'),
    ('red', 'unreadable', 'tools-fix8'): (True, True, True, True, 'red'),
    ('unread', 'both', 'mixed'): (False, True, True, True, 'red'),
    ('unread', 'both', 'mixed7'): (False, True, True, True, 'red'),
    ('unread', 'both', 'prod'): (False, False, False, False, 'check'),
    ('unread', 'both', 'prod-fix404'): (False, False, False, False, 'check'),
    ('unread', 'both', 'prod-fix6'): (False, True, False, True, 'red'),
    ('unread', 'both', 'prod-fix7'): (True, True, True, True, 'red'),
    ('unread', 'both', 'prod-fix8'): (True, True, True, True, 'red'),
    ('unread', 'both', 'stale'): (False, True, True, True, 'check'),
    ('unread', 'both', 'stale7'): (False, True, True, True, 'check'),
    ('unread', 'both', 'tools'): (True, False, False, False, 'check'),
    ('unread', 'both', 'tools-fix-prod'): (False, True, True, True, 'red'),
    ('unread', 'both', 'tools-fix8'): (True, True, True, True, 'red'),
    ('unread', 'gate-item', 'mixed'): (False, True, True, True, 'red'),
    ('unread', 'gate-item', 'mixed7'): (False, True, True, True, 'red'),
    ('unread', 'gate-item', 'prod'): (False, False, False, False, 'check'),
    ('unread', 'gate-item', 'prod-fix404'): (False, False, False, False, 'check'),
    ('unread', 'gate-item', 'prod-fix6'): (False, False, False, False, 'check'),
    ('unread', 'gate-item', 'prod-fix7'): (True, True, True, True, 'red'),
    ('unread', 'gate-item', 'prod-fix8'): (True, True, True, True, 'red'),
    ('unread', 'gate-item', 'stale'): (False, True, True, True, 'check'),
    ('unread', 'gate-item', 'stale7'): (False, True, True, True, 'check'),
    ('unread', 'gate-item', 'tools'): (True, False, False, False, 'check'),
    ('unread', 'gate-item', 'tools-fix-prod'): (False, True, True, True, 'red'),
    ('unread', 'gate-item', 'tools-fix8'): (True, True, True, True, 'red'),
    ('unread', 'none', 'mixed'): (False, False, False, False, 'check'),
    ('unread', 'none', 'mixed7'): (False, False, False, False, 'check'),
    ('unread', 'none', 'prod'): (False, False, False, False, 'check'),
    ('unread', 'none', 'prod-fix404'): (False, False, False, False, 'check'),
    ('unread', 'none', 'prod-fix6'): (False, False, False, False, 'check'),
    ('unread', 'none', 'prod-fix7'): (False, False, False, False, 'check'),
    ('unread', 'none', 'prod-fix8'): (False, False, False, False, 'check'),
    ('unread', 'none', 'stale'): (False, False, False, False, 'check'),
    ('unread', 'none', 'stale7'): (False, False, False, False, 'check'),
    ('unread', 'none', 'tools'): (True, False, False, False, 'check'),
    ('unread', 'none', 'tools-fix-prod'): (False, False, False, False, 'check'),
    ('unread', 'none', 'tools-fix8'): (True, False, False, False, 'check'),
    ('unread', 'outside-item', 'mixed'): (False, False, False, False, 'check'),
    ('unread', 'outside-item', 'mixed7'): (False, False, False, False, 'check'),
    ('unread', 'outside-item', 'prod'): (False, False, False, False, 'check'),
    ('unread', 'outside-item', 'prod-fix404'): (False, False, False, False, 'check'),
    ('unread', 'outside-item', 'prod-fix6'): (False, True, False, True, 'red'),
    ('unread', 'outside-item', 'prod-fix7'): (False, False, False, False, 'check'),
    ('unread', 'outside-item', 'prod-fix8'): (False, False, False, False, 'check'),
    ('unread', 'outside-item', 'stale'): (False, False, False, False, 'check'),
    ('unread', 'outside-item', 'stale7'): (False, False, False, False, 'check'),
    ('unread', 'outside-item', 'tools'): (True, False, False, False, 'check'),
    ('unread', 'outside-item', 'tools-fix-prod'): (False, False, False, False, 'check'),
    ('unread', 'outside-item', 'tools-fix8'): (True, False, False, False, 'check'),
    ('unread', 'unreadable', 'mixed'): (False, False, False, False, 'check'),
    ('unread', 'unreadable', 'mixed7'): (False, False, False, False, 'check'),
    ('unread', 'unreadable', 'prod'): (False, False, False, False, 'check'),
    ('unread', 'unreadable', 'prod-fix404'): (False, False, False, False, 'check'),
    ('unread', 'unreadable', 'prod-fix6'): (False, False, False, False, 'check'),
    ('unread', 'unreadable', 'prod-fix7'): (False, False, False, False, 'check'),
    ('unread', 'unreadable', 'prod-fix8'): (False, False, False, False, 'check'),
    ('unread', 'unreadable', 'stale'): (False, False, False, False, 'check'),
    ('unread', 'unreadable', 'stale7'): (False, False, False, False, 'check'),
    ('unread', 'unreadable', 'tools'): (True, False, False, False, 'check'),
    ('unread', 'unreadable', 'tools-fix-prod'): (False, False, False, False, 'check'),
    ('unread', 'unreadable', 'tools-fix8'): (True, False, False, False, 'check'),
}

# THE DISAGREEMENTS, each resolved (redland.py's header says why; each has its own test below):
#  - NO GATE FIX THAT MAY NOT LAND ON RED: a gate red's fix beside a production commit naming no red ("mixed",
#    "mixed7", "tools-fix-prod") was a gate fix though the rule kept it for green; it is a red fix, not a gate fix.
#  - THE COMMITS' COUNTING TRAILERS, NOT THE NAMES RECORDED AT SUBMISSION ("stale", "stale7": the trailer counts no
#    more): the classifier and the red-fix tree called it a fix, the rule and the governor did not; it is no fix.
WAITS_BESIDE_A_BARE_COMMIT = ("mixed", "mixed7", "tools-fix-prod")
NO_LONGER_COUNTS = ("stale", "stale7")
RESOLVED = {**{k: (v[0], v[1], False, v[3], v[4]) for k, v in MATRIX.items() if k[2] in WAITS_BESIDE_A_BARE_COMMIT},
            **{k: (v[0], False, False, False, v[4]) for k, v in MATRIX.items() if k[2] in NO_LONGER_COUNTS}}


class RedLandingMatrixTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        os.environ["AGENTCTL_STATE_FILE"] = os.path.join(self.tmp.name, "state.json")
        self.addCleanup(os.environ.pop, "AGENTCTL_STATE_FILE", None)

    def test_the_one_rule_answers_as_every_copy_did_but_for_the_resolved_disagreements(self):
        got = {}
        for gate, store_kind, change in itertools.product(GATES, STORES, CHANGES):
            got[(gate, store_kind, change)] = decisions(self, gate, store_kind, change)
        want = {k: RESOLVED.get(k, v) for k, v in MATRIX.items()}
        diff = {k: (want.get(k), v) for k, v in got.items() if want.get(k) != v}
        self.assertEqual(diff, {}, "\n" + "\n".join(f"{k!r}: {v!r}," for k, v in sorted(got.items())))

    def cell(self, gate, store_kind, change):
        return dict(zip(("may", "red_fix", "gate_fix", "tree", "gov"), decisions(self, gate, store_kind, change)))

    def test_a_fix_beside_a_production_commit_naming_no_red_is_a_red_fix_but_no_gate_fix(self):
        """It may not land on the red (its bare commit waits for green), so it is not first in the waiting batch's order
        nor a fix that waits for green; it names the open red, so its check still takes the red-fix tree."""
        for store_kind in ("none", "gate-item"):
            got = self.cell("red", store_kind, "mixed")
            self.assertEqual((got["may"], got["red_fix"], got["gate_fix"]), (False, True, False), store_kind)
        self.assertEqual(self.cell("red", "gate-item", "tools-fix-prod")["gate_fix"], False)
        self.assertEqual(self.cell("red", "gate-item", "prod-fix7")["gate_fix"], True, "a covered fix stays one")

    def test_a_trailer_that_no_longer_counts_makes_no_fix_whatever_the_entry_recorded(self):
        """Its entry still says `fixes: [8]`, but fix_check no longer counts its commit's trailer: no red fix, no red-fix
        tree, the governor's check class -- as the rule and the governor already said."""
        got = self.cell("red", "gate-item", "stale")
        self.assertEqual(got, {"may": False, "red_fix": False, "gate_fix": False, "tree": False, "gov": "check"})

    def test_without_the_commits_an_item_is_judged_by_its_recorded_names(self):
        """An item's data read without git (WR's pass, the governor's class of a lone entry): its names alone."""
        st = world(self, "gate-item")
        v = A.may_land_on_red(A.queue_facts({"8"}, st), {"fixes": ["7"]})
        self.assertEqual((v.red_fix, v.gate_fix, v.wait), (True, True, None))
        v = A.may_land_on_red(A.queue_facts({"8"}, st), {"fixes": ["404"]})
        self.assertEqual((v.red_fix, v.gate_fix), (False, False))
        self.assertIn("no run of the open gate red", v.wait)


if __name__ == "__main__":
    unittest.main()
