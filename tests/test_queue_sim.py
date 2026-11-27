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


def MUTANT_BOUND():
    """A mutant scenario's bound: 10 s inside the per-test timeout, which the pre-push hook grows with the laptop's load
    (a fixed 50 s refused a push at load 290, 2026-11-22: mutation 1 takes ~40 s on an idle laptop)."""
    return max(50, int(os.environ.get("AGENTCTL_TEST_TIMEOUT") or 60) - 10)

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
        """Seeds 0-3 of the load profile: the fixed seeds of PUSHES FIRST, THEN THE HOLDS (queuepush.py tick_changes)."""
        sims = [queue_sim.Sim(seed, calm=True, trees=1, profile=LOAD, n_changes=LOAD["n"]).run() for seed in range(4)]
        self.assertEqual(failures(sims), [])
        self.assertGreater(sum(1 for s in sims for t in s.pushes if len(t[2]) > 2), 0, "batches pushed")


# THE CHAOS PROFILE: reds, breaking changes, conflicts, infra failures and outside pushes several times as often, many
# changes sharing files. Its fixed seeds (one check tree): 121, a member superseded while its batch checked was pushed
# with it (A DECIDED MEMBER IS NEVER PUSHED, batch.py); 36 and 52, a change that no longer applied on main waited for a
# tree before it was told (main_conflict, overlapcheck.py).
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

    def test_the_baseline_queue_of_2026_11_19_holds_only_on_test_wiring_or_behind_a_check_in_flight(self):
        """Behind a QUEUED change, only the TestWiring.scala overlap holds; a generated overlap waits only for a check
        already in flight (A GENERATED OVERLAP WAITS FOR A CHECK IN FLIGHT, overlapcheck.py; invariant I10 holds the rest)."""
        for sim in self.run_seeds(range(4)):
            label = {c.eid: c.label for c in sim.changes}
            held = {(label[eid], label.get(b), f) for eid, hs in sim.holds.items() for _t, k, b, f in hs if k == "overlap"}
            source = {h for h in held if not set(h[2]) <= set(queue_sim.GENERATED)}
            self.assertLessEqual(source, {("enrichment2", "f2", (queue_sim.TEST_WIRING,))}, f"seed {sim.seed}")
            self.assertEqual(len(queue_sim.landing_waits(sim)), 6, "every change landed")

    def test_the_six_changes_land_sooner(self):
        import statistics
        sims = self.run_seeds(range(20))
        waits = [w for sim in sims for w in queue_sim.landing_waits(sim).values()]
        self.assertLessEqual(max(waits), 12, "before: 18 ticks, the six one at a time")
        self.assertLess(statistics.mean(waits), 6.5, "before: 8.2 ticks")
        checks = sum(sum(sim.checks_of.values()) + sim.batch_checks for sim in sims)
        self.assertLessEqual(checks, 50, "A GENERATED OVERLAP WAITS FOR A CHECK IN FLIGHT: 148 checks unheld, 40 held")

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


class BatchTiersSimTest(unittest.TestCase):
    """THE BATCH TIERS (overlapcheck.py; the owner, 2026-11-21): the two holds of 2026-11-21 replayed over the real queue code
    (queue_sim.HOLDS_2026_11_21), before (`before_tiers`: snapshots unregistered, no source resolution) and after."""

    def run_seeds(self, script, seeds=range(4), **profile):
        sims = [queue_sim.Sim(seed, calm=True, script=list(script), profile=profile).run() for seed in seeds]
        self.assertEqual(failures(sims), [])
        return sims

    def holds(self, sim):
        label = {c.eid: c.label for c in sim.changes}
        return {(label[eid], label.get(b)) for eid, hs in sim.holds.items() for _t, k, b, _f in hs if k == "overlap"}

    def test_todays_two_holds_queued_together_are_one_tree_one_check(self):
        for before, after in zip(self.run_seeds(queue_sim.HOLDS_2026_11_21, before_tiers=True),
                                 self.run_seeds(queue_sim.HOLDS_2026_11_21)):
            self.assertIn(("worker-rest-2", "review-common"), self.holds(before), "before: held")
            self.assertEqual(self.holds(after), set(), f"seed {after.seed}: nothing held")
            self.assertEqual(sorted(queue_sim.landing_waits(after)),
                             ["review-common", "scrapers-2", "split-shared-js", "worker-rest-2"], "every change landed")
            self.assertEqual(sum(after.checks_of.values()), 0, "no change checked alone")
            self.assertEqual(after.batch_checks, 1, "ONE check of the four as one tree")
            self.assertEqual(len([p for p in after.pushes if p[3] == "queue"]), 1, "one push")
            self.assertEqual(sorted(queue_sim.landing_waits(before)), ["review-common", "scrapers-2"],
                             "before: the younger two held, then NOT LANDED once the older landed (a rebase each)")

    def test_as_observed_the_younger_two_wait_for_the_check_in_flight_and_meet_in_the_next_batch(self):
        """The younger two queued while the older two checked in a batch (as on 2026-11-21): A GENERATED OVERLAP
        WAITS FOR A CHECK IN FLIGHT (overlapcheck.py) -- checked beside it, split-shared-js's check was voided by the older
        landing (100 checks over 20 seeds, against 40 held, the median the same) -- and the two meet in one batch."""
        for before, sim in zip(self.run_seeds(queue_sim.HOLDS_2026_11_21_IN_FLIGHT, before_tiers=True),
                               self.run_seeds(queue_sim.HOLDS_2026_11_21_IN_FLIGHT)):
            held = {h[0] for h in self.holds(sim)}
            self.assertEqual(held, {"split-shared-js", "worker-rest-2"}, f"seed {sim.seed}")
            self.assertEqual(len(queue_sim.landing_waits(sim)), 4, "every change landed")
            self.assertEqual(sum(sim.checks_of.values()), 0, "none checked alone: the younger two as one batch")
            self.assertEqual(sim.batch_checks, 2)
            self.assertLessEqual(sum(sim.checks_of.values()) + sim.batch_checks,
                                 sum(before.checks_of.values()) + before.batch_checks)


    def test_a_resolved_conflict_failing_the_batch_falls_back_to_the_hold(self):
        script = [(0, {"label": "a", "worktree": "a", "paths": ["common/src/main/A.scala", queue_sim.SWALLOWS]}),
                  (0, {"label": "b", "worktree": "b", "paths": ["worker/src/main/B.scala", queue_sim.SWALLOWS],
                       "resolvable_with": ["a"], "resolve_bad": True})]
        for sim in self.run_seeds(script):
            self.assertIn("a", queue_sim.landing_waits(sim))
            self.assertNotIn("b", queue_sim.landing_waits(sim), "never landed on the failed resolution")
            b = next(c for c in sim.changes if c.label == "b")
            self.assertEqual(sim.checks_of.get(b.eid, 0), 0, "never checked alone on a main it no longer applies on")


class MinimalRecheckSimTest(unittest.TestCase):
    """MINIMAL RECHECKS (queuerecheck.py; the owner, 2026-11-20): over the real queue code, a change main moves under is
    checked again only when its checks read what main changed -- by the simulated hook's own plans (queue_sim
    reads_of) -- and INVARIANT 11 still holds: every tree pushed is one a check passed, or holds one whose extra
    patches no check of it reads."""

    def run_timeline(self, push_paths):
        out = []
        for seed in range(6):
            sim = queue_sim.Sim(seed, calm=True, script=[
                (0, {"label": "a", "worktree": "a", "paths": ["web/src/main/A.scala"]}),
                (1, {"kind": "push", "paths": push_paths})]).run()
            self.assertEqual(failures([sim]), [], f"seed {seed}")
            a = next(c for c in sim.changes if c.label == "a")
            self.assertTrue(set(a.patches) <= sim.patches_of(sim.main), f"seed {seed}: a landed")
            out.append((sim.checks_of.get(a.eid, 0), sim.kept_pushes))
        return out

    def test_a_move_outside_its_selected_paths_keeps_the_verdict(self):
        for checks, kept in self.run_timeline(["docs/moved.md", "android/Moved.kt"]):
            self.assertEqual((checks, kept), (1, 1), "one check, its verdict kept over the move")

    def test_a_script_moving_under_a_scala_change_keeps_its_verdict(self):
        """movies' lint registry names what each spec opens (queue_sim lint_table; 2026-11-21): a script selects the
        specs reading scripts, a production source the source scans, and no spec reads both. Until then every `@repo`
        row was selected by both, and the change was verified again."""
        for checks, kept in self.run_timeline(["scripts/tool.sh"]):
            self.assertEqual((checks, kept), (1, 1), "one check, its verdict kept over the script's move")

    def test_a_workflow_moving_under_a_script_change_shares_the_workflow_wiring_specs(self):
        """...but a spec reading workflows and the scripts they run (WorkflowScriptsRunnableSpec) reads both sides."""
        out = []
        for seed in range(6):
            sim = queue_sim.Sim(seed, calm=True, script=[
                (0, {"label": "a", "worktree": "a", "paths": ["scripts/tool.sh"]}),
                (1, {"kind": "push", "paths": [".github/workflows/x.yml"]})]).run()
            self.assertEqual(failures([sim]), [], f"seed {seed}")
            a = next(c for c in sim.changes if c.label == "a")
            out.append((sim.checks_of.get(a.eid, 0), sim.kept_pushes))
        for checks, kept in out:
            self.assertEqual((checks, kept), (2, 0), "its check, then the verify round: the wiring specs read both")

    def test_a_move_inside_its_selected_paths_forces_a_recheck(self):
        for checks, kept in self.run_timeline(["common/src/main/Upstream.scala"]):
            self.assertEqual((checks, kept), (2, 0), "its check, then the verify round: web compiles common")


class RedEpisodeTest(unittest.TestCase):
    """SEVERAL RUNS OF ONE RED (queue_sim.REDFIX_*; 2026-11-25, batch #124): a fix naming an older run of the open gate red
    lands on the red like any fix of it; one naming a run its red item only learns of later is let go when it does (IW:
    never waits for green past N ticks once the store names it); a fix of ANOTHER failure waits for the green."""

    def sims(self, script):
        return [queue_sim.Sim(seed, calm=True, trees=2, script=list(script)).run() for seed in range(4)]

    def test_a_fix_naming_an_older_run_of_the_open_red_lands_on_the_red(self):
        for sim in self.sims(queue_sim.REDFIX_OLDER_RUN):
            self.assertEqual(failures([sim]), [])
            self.assertLessEqual(queue_sim.landing_waits(sim)["fix"], queue_sim.FIX_08010C_BOUND)

    def test_a_fix_is_let_go_once_the_red_names_its_run(self):
        for sim in self.sims(queue_sim.REDFIX_HEALS):
            self.assertEqual(failures([sim]), [])
            fix = next(c for c in sim.changes if c.label == "fix")
            self.assertLessEqual(sim.done_at[fix.cid], 8 + 10, "the red learned the run at tick 8: the fix goes at once")

    def test_a_fix_beside_a_bare_production_commit_waits_for_green_and_holds_nothing(self):
        """THE RED-LANDING RULE (redland.py): it may not land on the red, so it lands only once the gate is green; the
        tools-only change lands on the red meanwhile (IR: nothing bare is pushed onto the red)."""
        for sim in self.sims(queue_sim.REDFIX_BESIDE_BARE):
            self.assertEqual(failures([sim]), [])
            by = {c.label: c for c in sim.changes}
            self.assertTrue(sim.green_ticks, "the gate went green")
            self.assertGreaterEqual(sim.done_at[by["fix"].cid], sim.green_ticks[0], "it waited for green")
            self.assertLess(sim.done_at[by["tools"].cid], sim.green_ticks[0], "the tools-only change landed on the red")

    def test_a_fix_naming_a_run_only_a_stale_signature_puts_in_the_episode_lands_on_the_red(self):
        """ONE RedEpisode (redrecord.py): the queue facts count the stale record as the gate does (IW)."""
        for sim in self.sims(queue_sim.REDFIX_STALE_EPISODE):
            self.assertEqual(failures([sim]), [])
            self.assertLessEqual(queue_sim.landing_waits(sim)["fix"], queue_sim.FIX_08010C_BOUND)

    def test_the_fix_of_another_failure_waits_for_green(self):
        for sim in self.sims(queue_sim.REDFIX_OTHER_FAILURE):
            self.assertEqual(failures([sim]), [])
            fix = next(c for c in sim.changes if c.label == "fix")
            self.assertGreater(sim.done_at[fix.cid], 20, "it names a run no item holds: it waits while the gate is red")


class TwoHoldersTest(unittest.TestCase):
    """TWO LAND HOLDERS AT ONCE (queue_sim.TWO_HOLDERS_*; tests #289 / #292): at every interleaving of the store's seams
    -- above all the other extending before the maker gives its hold its expiry -- exactly one hold, both holders
    return it, neither raises, both moves are said (IH), and the queue lands its changes once the short holds expire."""

    def test_every_interleaving_makes_one_hold_both_report(self):
        for script in (queue_sim.TWO_HOLDERS_APART, queue_sim.TWO_HOLDERS_EXTEND_FIRST, queue_sim.TWO_HOLDERS_MOVE_BETWEEN,
                       queue_sim.TWO_HOLDERS_EXPIRE_BETWEEN):
            for seed in range(2):
                sim = queue_sim.Sim(seed, calm=True, trees=2, script=list(script)).run()
                self.assertEqual(failures([sim]), [], script[0][1]["order"])
                self.assertEqual(set(queue_sim.landing_waits(sim)), {"a", "late"})


class CrashLoopsTest(unittest.TestCase):
    """THE FIXER CRASH LOOP (queue_sim.CRASH_LOOPS, 2026-11-26): launches dying at launch of the vault, of one other cause,
    or of a different cause each time -- IL (a red whose launches all died of one cause is blocked, never offered to
    another fixer) holds, and catches the breaker with its cause undone."""

    def run_all(self):
        return [queue_sim.Sim(seed, calm=True, trees=2, script=list(queue_sim.CRASH_LOOPS)).run(stop=False) for seed in range(2)]

    def test_il_holds_and_the_change_lands(self):
        sims = self.run_all()
        self.assertEqual(failures(sims), [])
        for sim in sims:
            self.assertEqual(set(queue_sim.landing_waits(sim)), {"a"})

    def test_a_loop_of_one_cause_offered_again_breaks_il(self):
        saved = queue_sim.A.crash_cause
        queue_sim.A.crash_cause = lambda streak: None
        self.addCleanup(setattr, queue_sim.A, "crash_cause", saved)
        self.assertIn("IL", {k for sim in self.run_all() for k in sim.kinds_seen})


class KnownRedToolsTest(unittest.TestCase):
    """queue_sim.KNOWN_RED_TOOLS (the owner's "let it through", 2026-11-26): tools-only changes failing only in the open
    gate red's own test land on it (IK), the production one waits and never lands on it unnamed (IR); IK catches the
    rule with its tools-only exception undone."""

    def run_all(self):
        return [queue_sim.Sim(seed, calm=True, trees=2, script=list(queue_sim.KNOWN_RED_TOOLS)).run(stop=False) for seed in range(2)]

    def test_ik_and_ir_hold_and_every_change_lands(self):
        sims = self.run_all()
        self.assertEqual(failures(sims), [])
        for sim in sims:
            self.assertEqual(set(queue_sim.landing_waits(sim)), {"tools", "prod", "docs", "late"})

    def test_the_exception_undone_breaks_ik(self):
        saved = queue_sim.A.tools_only_known_red
        queue_sim.A.tools_only_known_red = lambda *a, **k: False
        self.addCleanup(setattr, queue_sim.A, "tools_only_known_red", saved)
        self.assertIn("IK", {k for sim in self.run_all() for k in sim.kinds_seen})


class EscalationsTest(unittest.TestCase):
    """THE ESCALATION DISPATCHER (queue_sim.ESCALATIONS, 2026-11-26) beside the queue: agent kinds whose fixer clears,
    fails then clears, never clears, or hangs past its wall time, two subjects of one kind, more agent kinds than the
    cap, owner-only kinds, and the repo untrusted for a while -- IE (one fixer per kind, the cap), IO (an owner-only
    kind never dispatches) and IG (every dispatched escalation ends cleared or the owner's within its budget) hold,
    and each catches the dispatcher with its rule undone."""

    def run_all(self):
        return [queue_sim.Sim(seed, calm=True, trees=2, script=list(script)).run(stop=False)
                for script in (queue_sim.ESCALATIONS, queue_sim.ESCALATIONS_UNTRUSTED) for seed in range(2)]

    def test_the_invariants_hold_and_the_changes_land(self):
        sims = self.run_all()
        self.assertEqual(failures(sims), [])
        for sim in sims:
            self.assertEqual(set(queue_sim.landing_waits(sim)), {"a", "b"})
            self.assertTrue(sim.escalations.sessions)

    def mutant(self, name, value):
        saved = getattr(queue_sim.A, name)
        setattr(queue_sim.A, name, value)
        self.addCleanup(setattr, queue_sim.A, name, saved)

    def test_two_fixers_of_a_kind_or_past_the_cap_break_ie(self):
        self.mutant("fixer_live", lambda lease, t: False)
        self.assertIn("IE", {k for sim in self.run_all() for k in sim.kinds_seen})

    def test_an_owner_only_kind_dispatched_breaks_io(self):
        self.mutant("escalation_route", lambda condition: queue_sim.A.agent_route("anything"))
        self.assertIn("IO", {k for sim in self.run_all() for k in sim.kinds_seen})

    def test_tries_never_handed_to_the_owner_break_ig(self):
        self.mutant("give_to_owner", lambda *a: None)
        self.assertIn("IG", {k for sim in self.run_all() for k in sim.kinds_seen})



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
            st = queue_sim.A.process_store()
            self.assertEqual([i for i in st.items("issue") if "[change-unheld" in i["data"]["text"]], [],
                             f"seed {seed}: no change was past its deadline unheld")

    def test_the_priority_table_in_agentctl_md_is_the_code_s(self):
        """(d) THE PRIORITY ORDER IS DOCUMENTED FROM THE CODE: AGENTCTL.md carries changekind.py's table verbatim."""
        with open(os.path.join(os.path.dirname(HERE), "AGENTCTL.md")) as f:
            doc = f.read()
        self.assertIn(queue_sim.A.priority_table_markdown(), doc,
                      "AGENTCTL.md's priority table drifted from changekind.py PRIORITY: paste "
                      "`python3 -c 'import sys; sys.path.insert(0, \"lib\"); from agentctl.changekind import priority_table_markdown as p; "
                      "print(p())'` over it")


# THE SIMULATOR CATCHES A REVERTED FIX: agentctl copied, one fix undone, the profile run on the copy must fail an
# invariant. A mutation whose anchor is gone fails here, naming it: update it with the code.
MUTATIONS = {
    "ee7b744 (the batch keeps its age-order place)": (
        'if not fix and self.batch_seq is not None and self.batch_seq < e["seq"] \\\n'
        '                and not check_tree_slot_free(st, self.repo, self.d) \\\n'
        '                and policy_blocks("older-batch", st, e["id"], "the last free check tree kept for the older batch"):',
        "if False:  # MUTATION", "loadlone"),
    "pushes first, then the holds": (
        '    for phase in ("push", "check"):\n'
        '        held = backlog_held(repo, d, reds, st, t, facts, ctx=ctx)\n'
        '        pheld = push_held(repo, d, reds, st, t, facts, ctx=ctx)\n',
        '    held = backlog_held(repo, d, reds, st, t, facts, ctx=ctx)\n'
        '    pheld = push_held(repo, d, reds, st, t, facts, ctx=ctx)\n'
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
        '                  batch=first["id"])\n', "", "chaoshold"),
    "generated files are no overlap": (
        "        shared = held_files(repo, shared, strict)\n",
        "", "nobatchgen"),
    # (11) the combined tree pushed unverified: main moved with no file overlap, and no verify round ran
    "a moved main's combined tree is verified": (
        "        if why is not None or tree_hook:  # a new round, not an attempt",
        "        if why is not None:  # MUTATION", "random"),
    # (11) MINIMAL RECHECKS undone: a verdict kept though a check of the change reads what main changed
    "a kept verdict needs its checks to read nothing main changed": (
        "        verify_why = read_overlap(", "        verify_why = None and read_overlap(", "random"),
    "overlapping changes are checked as one": (
        '    if e.get("backlog_batch"):\n        return None\n    return OverlapScan(',
        '    return None  # MUTATION\n    return OverlapScan(', "collisions"),
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
elif sys.argv[2] == "nobatchgen":  # a lone (--no-batch) change and a younger one sharing only a baseline, both queued
    script = [(0, {"label": "first", "worktree": "first", "kind": "nobatch", "paths": ["docs/first.md"]}),
              (0, {"label": "lone", "worktree": "lone", "kind": "nobatch", "paths": ["web/src/main/A.scala", queue_sim.COUPLING]}),
              (0, {"label": "b", "worktree": "b", "paths": ["worker/src/main/B.scala", queue_sim.COUPLING]})]
    bad = [v for seed in range(3) for v in queue_sim.Sim(seed, calm=True, trees=1, script=script).run().violations[:1]]
elif sys.argv[2] == "baselines":
    bad = queue_sim.Sim(0, calm=True, script=list(queue_sim.BASELINES_2026_11_19)).run().violations[:1]
elif sys.argv[2] == "chaoshold":  # chaos seeds where a checked change's push waits for a batch it is not in (7, 8
    # since 2026-11-20: 3 and 4 no longer reach it once a red fix waiting at green is released from the waiting batch;
    # 0 and 2 since 2026-11-21, the lint plan modelled as movies' registry is: fewer verdicts kept, other batches; 7 and 8
    # again once the registry names what each spec opens: more verdicts kept, the batches of before)
    bad = [v for seed in (7, 8) for v in queue_sim.Sim(seed, trees=4, profile={**T.CHAOS, "outside_red": 0.08},
                                                       n_changes=14).run().violations[:1]]
elif sys.argv[2] == "loadlone":  # the load with many lone (--no-batch) changes: since every other change batches
    # (2026-11-20), only they compete with a backlog batch for its tree
    p = dict(T.LOAD, nobatch=0.4)
    bad = [v for seed in range(3) for v in queue_sim.Sim(seed, calm=True, trees=1, profile=p,
                                                         n_changes=T.LOAD["n"]).run().violations[:1]]
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
                # the part holding the anchor, wherever it moved (one part, exactly)
                lib = os.path.join(tmp, "lib", "agentctl")
                holders = [os.path.join(lib, f) for f in sorted(os.listdir(lib))
                           if f.endswith(".py") and anchor in open(os.path.join(lib, f)).read()]
                self.assertEqual(len(holders), 1, f"mutation {name}: its anchor is in {len(holders)} parts; update it")
                q = holders[0]
                with open(q) as f:
                    text = f.read()
                with open(q, "w") as f:
                    f.write(text.replace(anchor, mutant, 1))
                runner = os.path.join(tmp, "run_mutant.py")
                with open(runner, "w") as f:
                    f.write(RUNNER)
                r = subprocess.run([sys.executable, runner, os.path.join(tmp, "tests"), scenario], capture_output=True,
                                   text=True, timeout=MUTANT_BOUND(), env=dict(os.environ))
                self.assertEqual(r.returncode, 0, r.stderr[-2000:])
                self.assertNotEqual(r.stdout.strip().splitlines()[-1], "[]",
                                    f"mutation {name}: no invariant failed -- the simulator no longer catches it")


for _i, _name in enumerate(MUTATIONS):
    setattr(MutationTest, f"test_reverted_fix_{_i}_fails_an_invariant", lambda self, n=_name: self.run_mutation(n))

if __name__ == "__main__":
    unittest.main()
