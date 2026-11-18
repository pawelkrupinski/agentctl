"""THE QUEUE SIMULATOR'S SEEDS (tests/queue_sim.py; the owner, 2026-11-18): random arrival sequences -- identity,
non-identity, red fixes, --no-batch, re-lands, supersedes, cancels, outside pushes, the gate going red and green,
breaking changes, rebase conflicts, infra failures -- over the real queue, batch and push code, every invariant checked
after every tick:

  1. one open item per change (worktree + head);
  2. never two pushes of one change (a patch reaches main once);
  3. a red fix is never behind a batch or a non-red change (started and pushed in the tick it may go, in the red-fix
     tree, never a batch member);
  4. a batch reaches landed, bisecting or held within N ticks;
  5. the oldest change lands within a bound when nothing fails (the calm and load profiles);
  6. no change lost (entries and items agree; a LANDED change's patches are on main; every change decided once the
     gate is green and nothing arrives);
  7. the stats: exactly one agentctl.landing record per decided change;
  8. a change that no longer applies because of a change open when it was queued was told at queue time (CONFLICTS,
     or NOT LANDED at its first tick) and never checked meanwhile;
  9. no full recheck for a file overlap with a change that was open when it was queued (checked as one instead).

200 random seeds run in the suite (8 tests of 25, in parallel); $AGENTCTL_TEST_SIM_SEEDS runs more. The fixed seeds
replay real days (victoria-logs-ci `record:agentctl.landing`, tests/fixtures/landing-replay-7d.json) and the bugs the
simulator found; the mutation tests prove it catches a reverted fix."""
import os, shutil, subprocess, sys, tempfile, unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import queue_sim  # noqa: E402

SEEDS = int(os.environ.get("AGENTCTL_TEST_SIM_SEEDS") or 200)
SHARDS = 8
# THE LOAD PROFILE: one check tree, a steady stream of mostly batchable changes, nothing failing. Every change lands
# within LOAD_BOUND ticks only while the backlog batch forms under load (2026-11-18: before PUSHES FIRST, THEN THE
# HOLDS, 40 changes took 40 lone checks and waited up to 55 ticks; with it, at most 27 over 50 seeds, 19 over seeds 0-9;
# with ee7b744 undone, 51-109).
LOAD = {"identity": 0.15, "gaps": [1, 1, 2, 3], "n": 40, "land_bound": 30}


def failures(sims):
    return [v for s in sims for v in s.violations[:3]]


class RandomSeedsTest(unittest.TestCase):
    def run_shard(self, k):
        per = -(-SEEDS // SHARDS)
        sims = [queue_sim.Sim(seed).run() for seed in range(k * per, min(SEEDS, (k + 1) * per))]
        self.assertEqual(failures(sims), [], "replay one with tests/queue_sim.py: Sim(<seed>).run(); "
                                             "$AGENTCTL_TEST_SIM_KEEP=<dir> keeps its processor log")


for _k in range(SHARDS):
    setattr(RandomSeedsTest, f"test_random_seeds_shard_{_k}", lambda self, k=_k: self.run_shard(k))


class ProfileSeedsTest(unittest.TestCase):
    def test_calm_seeds_land_every_change_within_the_bound(self):
        sims = [queue_sim.Sim(seed, calm=True, trees=trees).run() for seed in range(8) for trees in (1, 2, 4)]
        self.assertEqual(failures(sims), [])

    def test_under_load_the_backlog_batch_forms_and_every_change_lands_within_the_bound(self):
        """Seeds 0-3 of the load profile: the fixed seeds of PUSHES FIRST, THEN THE HOLDS (queue.py tick_changes)."""
        sims = [queue_sim.Sim(seed, calm=True, trees=1, profile=LOAD, n_changes=LOAD["n"]).run() for seed in range(4)]
        self.assertEqual(failures(sims), [])
        self.assertGreater(sum(1 for s in sims for t in s.pushes if len(t[2]) > 2), 0, "batches pushed")


# THE CHAOS PROFILE: reds, breaking changes, conflicts, infra failures and outside pushes several times as often, many
# changes sharing files. Its fixed seeds (one check tree): 121, a member superseded while its batch checked was pushed
# with it (A DECIDED MEMBER IS NEVER PUSHED, batch.py); 36 and 52, a change that no longer applied on main waited for a
# tree before it was told (main_conflict, queue.py).
CHAOS = {"red": 0.08, "bad": 0.3, "conflict": 0.3, "infra": 0.08, "outside": 0.1, "shared_path": 0.4}


class ChaosSeedsTest(unittest.TestCase):
    def test_the_fixed_chaos_seeds(self):
        sims = [queue_sim.Sim(seed, trees=1, profile=CHAOS, n_changes=14).run() for seed in (36, 52, 121)]
        self.assertEqual(failures(sims), [])

    def test_chaos_seeds(self):
        """3 seeds in the suite; $AGENTCTL_TEST_SIM_SEEDS=N sweeps N/10 (the larger sweep: an env-flagged run)."""
        n = int(os.environ["AGENTCTL_TEST_SIM_SEEDS"]) // 10 if os.environ.get("AGENTCTL_TEST_SIM_SEEDS") else 3
        sims = [queue_sim.Sim(seed, trees=trees, profile=CHAOS, n_changes=14).run() for seed in range(n)
                for trees in (1, 4)]
        self.assertEqual(failures(sims), [])


class ReplayTest(unittest.TestCase):
    """The real arrivals of each day of 2026-11-11..18 (353 landings), as fixed seeds."""


for _i, _day in enumerate(queue_sim.replay_days()):
    setattr(ReplayTest, f"test_replay_{_day['day']}",
            lambda self, day=_day, i=_i: self.assertEqual(
                failures([queue_sim.Sim(1000 + i, script=queue_sim.replay_script(day)).run()]), []))


class CollisionTest(unittest.TestCase):
    """OVERLAPPING CHANGES (the coordinator, 2026-11-18): two queued changes sharing a FILE are checked as one -- in
    one batch, or the younger after the older, on top of it -- never the younger alone and then again in full; two that
    cannot be assembled together: the younger's author is told at queue time, and it is not checked meanwhile."""

    def test_the_three_collisions_of_2026_11_17_cost_no_check_and_no_full_recheck(self):
        sim = queue_sim.Sim(17, calm=True, script=list(queue_sim.COLLISIONS_2026_11_17))
        sim.run()
        self.assertEqual(failures([sim]), [])
        st = {c.label: c for c in sim.changes}
        self.assertEqual(sorted(st), ["b1", "break-cycles", "catalog", "ci-workflows", "scrapers"])
        for label in ("scrapers", "ci-workflows", "catalog"):
            self.assertTrue(set(st[label].patches) <= sim.patches_of(sim.main), f"{label} landed")
        for label in ("b1", "break-cycles"):
            self.assertFalse(set(st[label].patches) & sim.patches_of(sim.main), f"{label} conflicts: not landed")
            self.assertEqual(sim.checks_of.get(st[label].eid, 0), 0, f"{label}: no check was spent on it")


# THE SIMULATOR CATCHES A REVERTED FIX: agentctl copied, one fix undone, the profile run on the copy must fail an
# invariant. A mutation whose anchor is gone fails here, naming it: update it with the code.
MUTATIONS = {
    "ee7b744 (the batch keeps its age-order place)": (
        'if not fix and batch_seq is not None and batch_seq < e["seq"] and not check_tree_slot_free(repo, d):',
        "if False:  # MUTATION", "load"),
    "pushes first, then the holds": (
        '    for phase in ("push", "check"):\n'
        '        held = backlog_held(repo, d, reds, st, t)\n'
        '        pheld = push_held(repo, d, reds, st, t)\n',
        '    held = backlog_held(repo, d, reds, st, t)\n'
        '    pheld = push_held(repo, d, reds, st, t)\n'
        '    for phase in ("push", "check"):\n', "load"),
    "overlapping changes are checked as one": (
        '    if e.get("backlog_batch"):\n        return None\n    together, first = None, None',
        '    return None  # MUTATION\n    together, first = None, None', "collisions"),
}
RUNNER = """import json, sys
sys.path.insert(0, sys.argv[1])
import queue_sim, test_queue_sim as T
if sys.argv[2] == "collisions":
    bad = queue_sim.Sim(17, calm=True, script=list(queue_sim.COLLISIONS_2026_11_17)).run().violations[:1]
else:
    bad = [v for seed in range(3) for v in queue_sim.Sim(seed, calm=True, trees=1, profile=T.LOAD,
                                                         n_changes=T.LOAD["n"]).run().violations[:1]]
print(json.dumps(bad))
"""


class MutationTest(unittest.TestCase):
    def run_mutation(self, name):
        src = os.path.dirname(HERE)
        anchor, mutant, scenario = MUTATIONS[name]
        if True:
            with tempfile.TemporaryDirectory() as tmp:
                for x in ("agentctl", "lib", "tests"):
                    s = os.path.join(src, x)
                    (shutil.copytree if os.path.isdir(s) else shutil.copy)(
                        s, os.path.join(tmp, x), **({"ignore": shutil.ignore_patterns("__pycache__")} if os.path.isdir(s) else {}))
                q = os.path.join(tmp, "lib", "agentctl", "queue.py")
                with open(q) as f:
                    text = f.read()
                self.assertIn(anchor, text, f"mutation {name}: its anchor is gone from queue.py; update it")
                with open(q, "w") as f:
                    f.write(text.replace(anchor, mutant, 1))
                runner = os.path.join(tmp, "run_mutant.py")
                with open(runner, "w") as f:
                    f.write(RUNNER)
                r = subprocess.run([sys.executable, runner, os.path.join(tmp, "tests"), scenario], capture_output=True,
                                   text=True, timeout=50, env=dict(os.environ))
                self.assertEqual(r.returncode, 0, r.stderr[-2000:])
                self.assertNotEqual(r.stdout.strip().splitlines()[-1], "[]",
                                    f"mutation {name}: no invariant failed -- the simulator no longer catches it")


for _i, _name in enumerate(MUTATIONS):
    setattr(MutationTest, f"test_reverted_fix_{_i}_fails_an_invariant", lambda self, n=_name: self.run_mutation(n))

if __name__ == "__main__":
    unittest.main()
