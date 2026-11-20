"""THE SIMULATOR GATE's own rules (tests/sim_gate.py): a rise past a kind's baseline, a changed definition without a new
version, a baseline loosened at the same version, and one commit changing an invariant row beside lib/agentctl/ each
refuse; a fall is a note. The fixed scenarios themselves run in the pre-push hook and tests.yml's sim-gate job."""
import os, subprocess, sys, tempfile, unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import queue_sim  # noqa: E402
import sim_gate  # noqa: E402


def rows(**over):
    fps = sim_gate.fingerprints()
    out = {k: {"version": 1, "fingerprint": fps.get(k, "-"), "baseline": 0, "what": ""} for k in queue_sim.INVARIANT_KINDS}
    for k, v in over.items():
        out[k] = {**out[k], **v}
    return out


def results(**violating):
    """A measured run where kind k is violated by violating[k] scenarios."""
    out = {f"s{i}": {"kinds": {}, "lines": [], "latency": 1.0} for i in range(10)}
    for k, n in violating.items():
        for i in range(n):
            out[f"s{i}"]["kinds"][k] = 1
            out[f"s{i}"]["lines"] = [f"[{k}] boom"]
    return out


class GateTest(unittest.TestCase):
    def test_every_kind_the_simulator_raises_is_registered_and_fingerprinted(self):
        fps = sim_gate.fingerprints()
        self.assertEqual(set(fps) - set(queue_sim.INVARIANT_KINDS), set(), "a violate() kind not in INVARIANT_KINDS")
        self.assertEqual(set(queue_sim.INVARIANT_KINDS) - set(fps), set(), "a kind nothing raises")
        reg = sim_gate.read_registry(open(sim_gate.REGISTRY).read())
        self.assertEqual(set(reg), set(queue_sim.INVARIANT_KINDS))
        stale = {k for k in reg if reg[k]["fingerprint"] != fps[k]}
        self.assertEqual(stale, set(), "a changed invariant definition: `tests/sim_gate.py --update --bump <kind>`, "
                                       "in a commit of its own")

    def test_a_rise_past_the_baseline_refuses_and_a_fall_is_a_note(self):
        fails, _ = sim_gate.check(results(I9=2), rows(I9={"baseline": 1}))
        self.assertTrue(any(f.startswith("I9") and "2 scenario(s)" in f for f in fails), fails)
        fails, notes = sim_gate.check(results(I9=1), rows(I9={"baseline": 3}))
        self.assertEqual(fails, [])
        self.assertTrue(any("I9" in n for n in notes))

    def test_a_changed_definition_needs_a_new_version(self):
        fails, _ = sim_gate.check(results(), rows(I4={"fingerprint": "000000000000"}))
        self.assertTrue(any(f.startswith("I4: its definition changed") for f in fails), fails)

    def test_a_baseline_loosened_at_the_same_version_refuses_against_the_base(self):
        base = rows(I9={"baseline": 1})
        fails, _ = sim_gate.check(results(I9=2), rows(I9={"baseline": 2}), base)
        self.assertTrue(any("rose from 1 to 2 at the same version" in f for f in fails), fails)
        fails, _ = sim_gate.check(results(I9=2), rows(I9={"baseline": 2, "version": 2}), base)
        self.assertFalse(any("rose from" in f for f in fails), fails)

    def test_the_fingerprint_follows_the_invariants_source(self):
        src = open(os.path.join(HERE, "queue_sim.py")).read()
        before = sim_gate.fingerprints(src)
        after = sim_gate.fingerprints(src.replace('self.profile.get("batch_bound", 40)',
                                                  'self.profile.get("batch_bound", 80)'))
        self.assertNotEqual(before["I4"], after["I4"], "loosening the batch bound is a new definition of I4")
        self.assertEqual(before["I9"], after["I9"], "a kind raised elsewhere keeps its fingerprint")


class CommitRuleTest(unittest.TestCase):
    """An invariant's definition changes in a commit of its own (2026-11-20: the batching work loosened invariants 4
    and 9 in the commit that needed them looser)."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="agentctl-simgate-")
        g = lambda *a: subprocess.run(["git", *a], cwd=self.tmp, check=True, capture_output=True, text=True)
        self.g = g
        g("init", "-q", "-b", "main")
        os.makedirs(os.path.join(self.tmp, "tests"))
        os.makedirs(os.path.join(self.tmp, "lib", "agentctl"))
        self.write("tests/sim-invariants.tsv", "I9\t1\taaa\t1\tx\n")
        self.write("lib/agentctl/queue.py", "a = 1\n")
        g("add", "-A")
        g("commit", "-qm", "base")
        self.base = g("rev-parse", "HEAD").stdout.strip()

    def write(self, rel, text):
        with open(os.path.join(self.tmp, rel), "w") as f:
            f.write(text)

    def commit(self, msg):
        self.g("add", "-A")
        self.g("commit", "-qm", msg)

    def rule(self):
        old = sim_gate.ROOT
        sim_gate.ROOT = self.tmp
        try:
            return sim_gate.commit_rule(f"{self.base}..HEAD")
        finally:
            sim_gate.ROOT = old

    def test_a_loosened_row_beside_the_code_it_measures_refuses(self):
        self.write("tests/sim-invariants.tsv", "I9\t1\taaa\t2\tx\n")
        self.write("lib/agentctl/queue.py", "a = 2\n")
        self.commit("both")
        self.assertEqual([k for _s, k in self.rule()], [["I9"]])

    def test_a_row_bumped_in_a_commit_of_its_own_passes(self):
        self.write("tests/sim-invariants.tsv", "I9\t2\tbbb\t2\tx\n")
        self.commit("the invariant")
        self.write("lib/agentctl/queue.py", "a = 2\n")
        self.commit("the code")
        self.assertEqual(self.rule(), [])

    def test_a_baseline_falling_beside_the_code_passes(self):
        self.write("tests/sim-invariants.tsv", "I9\t1\taaa\t0\tx\n")
        self.write("lib/agentctl/queue.py", "a = 2\n")
        self.commit("a fix, its fall recorded")
        self.assertEqual(self.rule(), [])


if __name__ == "__main__":
    unittest.main()
