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
  9. no full recheck for a file overlap with a change that was open when it was queued (checked as one instead);
  10. no change held behind another for generated files alone (GENERATED FILES ARE NO OVERLAP, waiting.py);
  11. no unverified combined tree reaches main: every tree the queue pushes is one a check or a batch check passed
      (the owner's R3, 2026-11-19: the invariant every landing-latency cut is held to).

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
CHAOS = {"red": 0.08, "bad": 0.3, "conflict": 0.3, "infra": 0.08, "outside": 0.1, "shared_path": 0.4, "outside_red": 0}


class ChaosSeedsTest(unittest.TestCase):
    def test_the_fixed_chaos_seeds(self):
        sims = [queue_sim.Sim(seed, trees=1, profile=CHAOS, n_changes=14).run() for seed in (36, 52, 121)]
        self.assertEqual(failures(sims), [])

    def test_chaos_seeds(self):
        """3 seeds in the suite; $AGENTCTL_TEST_SIM_SEEDS=N sweeps N/10 (the larger sweep: an env-flagged run)."""
        n = int(os.environ["AGENTCTL_TEST_SIM_SEEDS"]) // 10 if os.environ.get("AGENTCTL_TEST_SIM_SEEDS") else 3
        sims = [queue_sim.Sim(seed, trees=trees, profile={**CHAOS, "outside_red": 0.08}, n_changes=14).run()
                for seed in range(n) for trees in (1, 4)]
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


class GeneratedOverlapTest(unittest.TestCase):
    """GENERATED FILES ARE NO OVERLAP (waiting.py; 2026-11-19 ~06:05Z): six changes, each editing coupling-baseline.tsv,
    each held behind the one before it -- hours of serial checks. Replayed (queue_sim.BASELINES_2026_11_19): no hold for
    the baselines; the real source two of them share (TestWiring.scala, f2 and enrichment2) still holds. Measured over
    seeds 0-19, 4 check trees: before, mean 8.2 ticks from queued to landed, max 18 (one at a time, whatever the trees);
    after, mean 5.3, max 11."""

    def run_seeds(self, seeds, script=None, trees=4):
        sims = [queue_sim.Sim(seed, calm=True, trees=trees, script=list(script or queue_sim.BASELINES_2026_11_19)).run()
                for seed in seeds]
        self.assertEqual(failures(sims), [])
        return sims

    def test_the_baseline_queue_of_2026_11_19_holds_only_on_test_wiring(self):
        for sim in self.run_seeds(range(4)):
            label = {c.eid: c.label for c in sim.changes}
            held = {(label[eid], label.get(b), f) for eid, hs in sim.holds.items() for _t, k, b, f in hs if k == "overlap"}
            self.assertEqual(held, {("enrichment2", "f2", (queue_sim.TEST_WIRING,))}, f"seed {sim.seed}")
            self.assertEqual(len(queue_sim.landing_waits(sim)), 6, "every change landed")

    def test_the_six_changes_land_sooner(self):
        import statistics
        waits = [w for sim in self.run_seeds(range(20)) for w in queue_sim.landing_waits(sim).values()]
        self.assertLessEqual(max(waits), 12, "before: 18 ticks, the six one at a time")
        self.assertLess(statistics.mean(waits), 6.5, "before: 8.2 ticks")

    def test_a_conflict_in_generated_rows_is_resolved_in_the_batch(self):
        """Adjacent rows of coupling-baseline.tsv: git conflicts, the rows merge (or regenerate): one batch, both land."""
        script = [(0, {"label": "a", "worktree": "a", "paths": ["web/src/main/A.scala", queue_sim.COUPLING]}),
                  (0, {"label": "b", "worktree": "b", "paths": ["worker/src/main/B.scala", queue_sim.COUPLING],
                       "gen_conflicts_with": ["a"]})]
        for sim in self.run_seeds(range(3), script):
            self.assertEqual(sorted(queue_sim.landing_waits(sim)), ["a", "b"])
            self.assertTrue(any(len(p[2]) >= 2 for p in sim.pushes), "pushed as one batch")
            self.assertEqual([h for hs in sim.holds.values() for h in hs if h[1] == "overlap"], [], "nothing held")

    def test_a_member_whose_generated_files_cannot_be_resolved_is_checked_alone_and_held_as_before(self):
        script = [(0, {"label": "a", "worktree": "a", "paths": ["web/src/main/A.scala", queue_sim.COUPLING]}),
                  (0, {"label": "b", "worktree": "b", "paths": ["worker/src/main/B.scala", queue_sim.COUPLING],
                       "gen_conflicts_with": ["a"], "regen_fails": True})]
        for sim in self.run_seeds(range(3), script):
            self.assertIn("a", queue_sim.landing_waits(sim))
            self.assertNotIn("b", queue_sim.landing_waits(sim), "it no longer applies once a landed: NOT LANDED")
            label = {c.eid: c.label for c in sim.changes}
            held = {(label[eid], label.get(b)) for eid, hs in sim.holds.items() for _t, k, b, _f in hs if k == "overlap"}
            self.assertEqual(held, {("b", "a")}, "left out of the batch, it waits for a as before")


class Timeline08010cTest(unittest.TestCase):
    """THE 08010c TIMELINE (queue_sim.TIMELINE_08010C; 2026-11-19): a fix of a red OUTSIDE the gate, checked while two
    ordinary changes owed combined-tree verifications and made a push batch, sat ~2 h. Replayed over 8 seeds: it is
    classified a red fix (changekind.py), never held behind a batch, a push batch or a verification, lands within
    FIX_08010C_BOUND ticks of arriving, and the change overlapping it waits for it -- not the other way round."""

    def test_the_fix_of_a_red_outside_the_gate_goes_first(self):
        for seed in range(8):
            sim = queue_sim.Sim(seed, calm=True, script=list(queue_sim.TIMELINE_08010C)).run()
            self.assertEqual(failures([sim]), [], f"seed {seed}")
            waits = queue_sim.landing_waits(sim)
            self.assertLessEqual(waits["fix"], queue_sim.FIX_08010C_BOUND, f"seed {seed}: {waits}")
            label = {c.eid: c.label for c in sim.changes}
            fix = next(c for c in sim.changes if c.label == "fix")
            kinds = {h[1] for h in sim.holds.get(fix.eid, [])}
            self.assertEqual(kinds & {"batch", "push-batch", "verify", "backlog-batch", "older-batch"}, set(),
                             f"seed {seed}: the fix was held by {kinds}")
            replay = {(k, label.get(b)) for _t, k, b, _f in sim.holds.get(
                next(c for c in sim.changes if c.label == "replay").eid, []) if k == "overlap"}
            self.assertEqual(replay, {("overlap", "fix")}, f"seed {seed}: fix-replay waits for the fix it overlaps")
            st = queue_sim.A.Store()
            self.assertEqual([i for i in st.items("issue") if "[change-unheld" in i["data"]["text"]], [],
                             f"seed {seed}: no change was past its deadline unheld")

    def test_the_priority_table_in_agentctl_md_is_the_code_s(self):
        """(d) THE PRIORITY ORDER IS DOCUMENTED FROM THE CODE: AGENTCTL.md carries changekind.py's table verbatim."""
        with open(os.path.join(os.path.dirname(HERE), "AGENTCTL.md")) as f:
            doc = f.read()
        self.assertIn(queue_sim.A.priority_table_markdown(), doc,
                      "AGENTCTL.md's priority table drifted from changekind.py PRIORITY: paste "
                      "`python3 -c 'import sys; sys.path.insert(0, \"lib\"); import agentctl as A; "
                      "print(A.priority_table_markdown())'` over it")


# THE SIMULATOR CATCHES A REVERTED FIX: agentctl copied, one fix undone, the profile run on the copy must fail an
# invariant. A mutation whose anchor is gone fails here, naming it: update it with the code.
MUTATIONS = {
    "ee7b744 (the batch keeps its age-order place)": (
        'if not fix and batch_seq is not None and batch_seq < e["seq"] and not check_tree_slot_free(repo, d):',
        "if False:  # MUTATION", "load"),
    "pushes first, then the holds": (
        '    for phase in ("push", "check"):\n'
        '        held = backlog_held(repo, d, reds, st, t, facts)\n'
        '        pheld = push_held(repo, d, reds, st, t, facts)\n',
        '    held = backlog_held(repo, d, reds, st, t, facts)\n'
        '    pheld = push_held(repo, d, reds, st, t, facts)\n'
        '    for phase in ("push", "check"):\n', "load"),
    # (a) ONE CLASSIFIER: a fix of a red outside the gate counted as ordinary again (c74b64f's bug, now in one place)
    "changekind.py: a fix of any open red is a red fix": (
        "        if n in gate or n in open_:", "        if n in gate:  # MUTATION", "08010c"),
    # (d) the priority order dropped: queue order alone
    "the priority order": (
        "    todo = sorted(entries, key=lambda e: kind_order(kinds, e))",
        '    todo = sorted(entries, key=lambda e: e["seq"])  # MUTATION', "redfix"),
    # (b)/(c) a checked change's push held behind a batch without saying so (c74b64f's other half)
    "a held push records its hold": (
        '        note_hold(st, e, "batch", f"its push waits for batch {first[\'id\']} ({first[\'state\']}), which it is not in", t,\n'
        '                  batch=first["id"])\n', "", "random"),
    "generated files are no overlap": (
        "        shared = shared if strict else without_generated(repo, shared)\n",
        "", "baselines"),
    # (11) the combined tree pushed unverified: main moved with no file overlap, and no verify round ran
    "a moved main's combined tree is verified": (
        "            if why is not None or tree_hook:  # a new round, not an attempt",
        "            if why is not None:  # MUTATION", "random"),
    "overlapping changes are checked as one": (
        '    if e.get("backlog_batch"):\n        return None\n    together, first = None, None',
        '    return None  # MUTATION\n    together, first = None, None', "collisions"),
}
RUNNER = """import json, sys
sys.path.insert(0, sys.argv[1])
import queue_sim, test_queue_sim as T
if sys.argv[2] == "08010c":
    bad = [v for seed in range(4) for v in queue_sim.Sim(seed, calm=True, script=list(queue_sim.TIMELINE_08010C)).run().violations[:1]]
elif sys.argv[2] == "redfix":
    bad = [v for seed in range(4) for v in queue_sim.Sim(seed, calm=True, trees=2, script=list(T.REDFIX_BEHIND_ORDINARY)).run().violations[:1]]
elif sys.argv[2] == "random":
    bad = [v for seed in range(8) for v in queue_sim.Sim(seed).run().violations[:1]]
elif sys.argv[2] == "collisions":
    bad = queue_sim.Sim(17, calm=True, script=list(queue_sim.COLLISIONS_2026_11_17)).run().violations[:1]
elif sys.argv[2] == "baselines":
    bad = queue_sim.Sim(0, calm=True, script=list(queue_sim.BASELINES_2026_11_19)).run().violations[:1]
else:
    bad = [v for seed in range(3) for v in queue_sim.Sim(seed, calm=True, trees=1, profile=T.LOAD,
                                                         n_changes=T.LOAD["n"]).run().violations[:1]]
print(json.dumps(bad))
"""


# THE RED FIX BESIDE AN ORDINARY CHANGE: two check trees, one busy; an ordinary change and a fix of a red outside the gate
# arrive in one tick, the ordinary one first -- the fix starts first (the priority order's mutation scenario).
REDFIX_BEHIND_ORDINARY = [
    (0, {"label": "a", "worktree": "a", "paths": ["web/src/main/A.scala"], "kind": "nobatch"}),
    (1, {"kind": "open-outside-red"}),
    (1, {"label": "c", "worktree": "c", "paths": ["common/src/main/C.scala"], "kind": "nobatch"}),
    (1, {"label": "fix", "worktree": "fix", "kind": "outside-fix", "paths": ["scripts/fix.sh"]}),
]


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
                q = os.path.join(tmp, "lib", "agentctl", "changekind.py" if name.startswith("changekind.py") else "queue.py")
                with open(q) as f:
                    text = f.read()
                self.assertIn(anchor, text, f"mutation {name}: its anchor is gone from {os.path.basename(q)}; update it")
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
