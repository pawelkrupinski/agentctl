"""GENERATED FILES ARE NO OVERLAP (waiting.py; 2026-11-19 ~06:05Z: six movies changes, each editing
test/resources/coupling-baseline.tsv, each held behind the one queued before it -- every coupling or complexity lane
landed one at a time). A repo's registered generated files (red-repos.json `generated`: {paths, regen}) never hold a
queued change behind another, never keep two changes out of one backlog batch, and a conflict in one -- a rebase, a
batch's assembly -- is resolved: its rows merged three-way, else the repo's own regen command run over the combined
tree; the commit keeps its message and trailers. A failed resolution leaves the member out (batch_alone `regen`),
held as before. A real source both change still holds."""
import json, os, sys, time, unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from test_agentctl import A, sh  # noqa: E402
import test_backlog_batch  # noqa: E402

BASELINE = "test/resources/coupling-baseline.tsv"
REFS = "test/resources/coupling-fanout-refs.tsv"  # a SET table: `<class><TAB><target>` a line, a class on many lines
REFS_LINES = "A\tmodels.X\nA\tmodels.Y\nA\ttools.Z\nB\tmodels.X\n"
HEADER = "# THE COUPLING BASELINE\n# kind<TAB>key<TAB>value\n"


def table(rows):
    return HEADER + "".join(f"{k}\t{v}\n" for k, v in rows)


ROWS = [("limit\tfanout", 9), ("ce\tmodels", 3), ("ce\tservices", 7), ("ce\ttools", 2), ("fanout\tA", 12),
        ("fanout\tB", 11)]


class RowsMergeTest(unittest.TestCase):
    def test_disjoint_rows_merge_even_when_adjacent(self):
        base = table(ROWS)
        ours = base.replace("ce\tservices\t7", "ce\tservices\t6")
        theirs = base.replace("ce\ttools\t2", "ce\ttools\t1").replace("fanout\tB\t11\n", "")
        self.assertEqual(A.rows_merge(base, ours, theirs),
                         base.replace("ce\tservices\t7", "ce\tservices\t6").replace("ce\ttools\t2", "ce\ttools\t1")
                         .replace("fanout\tB\t11\n", ""))

    def test_a_row_added_on_their_side_follows_the_row_it_follows_there(self):
        base = table(ROWS)
        theirs = base.replace("ce\tmodels\t3\n", "ce\tmodels\t3\nce\tsettings\t4\n")
        ours = base + "fanout\tC\t10\n"
        self.assertEqual(A.rows_merge(base, ours, theirs), theirs + "fanout\tC\t10\n")

    def test_one_row_both_sides_changed_differently_is_no_merge(self):
        base = table(ROWS)
        self.assertIsNone(A.rows_merge(base, base.replace("fanout\tA\t12", "fanout\tA\t13"),
                                       base.replace("fanout\tA\t12", "fanout\tA\t10")))
        same = base.replace("fanout\tA\t12", "fanout\tA\t10")
        self.assertEqual(A.rows_merge(base, same, same), same, "the same value on both sides is no conflict")

    def test_a_header_both_sides_changed_or_a_repeated_key_is_no_merge(self):
        base = table(ROWS)
        self.assertIsNone(A.rows_merge(base, "# ours\n" + base, "# theirs\n" + base))
        self.assertIsNone(A.rows_merge(base, base + "ce\tmodels\t4\n", base))

    def test_a_set_table_merges_line_by_line(self):
        """coupling-fanout-refs.tsv (2026-11-19, lane F2b's bounce): a class on many lines, no value column -- each
        line is its own row, present or not; a line one side removed is gone, one either side added is kept."""
        ours = REFS_LINES.replace("A\tmodels.Y\n", "")  # main: A no longer uses Y
        theirs = REFS_LINES.replace("A\tmodels.Y\n", "A\tmodels.Y\nA\tmodels.W\n").replace("B\tmodels.X\n", "")
        self.assertEqual(A.rows_merge(REFS_LINES, ours, theirs), "A\tmodels.X\nA\tmodels.W\nA\ttools.Z\n")

    def test_prefer_ours_takes_our_value_for_a_row_both_sides_changed(self):
        """The server role runs no regen command: a row both sides changed differently provisionally keeps OUR side's
        (main's) value -- the checks' ratchet then measures the combined tree -- while every other row merges."""
        base = table(ROWS)
        ours = base.replace("fanout\tA\t12", "fanout\tA\t15")
        theirs = base.replace("fanout\tA\t12", "fanout\tA\t10").replace("ce\ttools\t2", "ce\ttools\t1")
        self.assertIsNone(A.rows_merge(base, ours, theirs))
        self.assertEqual(A.rows_merge(base, ours, theirs, prefer_ours=True),
                         base.replace("fanout\tA\t12", "fanout\tA\t15").replace("ce\ttools\t2", "ce\ttools\t1"))


class RegistryTest(unittest.TestCase):
    def test_a_generated_entry_needs_its_paths_and_regen_command(self):
        ok = A.red_repos_parse({"repos": [{"repo": "o/r", "gate": [["Main", "main.yml"]], "generated": [
            {"paths": [BASELINE], "regen": "scripts/coupling-report --update", "timeout": 600}]}]})
        conf = next(c for c in ok if c["repo"] == "o/r")
        self.assertEqual(conf["generated"], [{"paths": [BASELINE], "regen": ["scripts/coupling-report", "--update"],
                                              "timeout": 600}])
        rows_only = A.red_repos_parse({"repos": [{"repo": "o/r", "gate": [["Main", "main.yml"]], "generated": [
            {"paths": [BASELINE]}]}]})
        self.assertEqual(next(c for c in rows_only if c["repo"] == "o/r")["generated"],
                         [{"paths": [BASELINE], "regen": None, "timeout": None}], "a hand-written table: rows only")
        for bad in ({"paths": [BASELINE], "regen": ""}, {"regen": "x"}, {"paths": ["/abs"], "regen": "x"},
                    {"paths": [BASELINE], "regen": "x", "timeout": 0}):
            with self.assertRaises(ValueError, msg=bad):
                A.red_repos_parse({"repos": [{"repo": "o/r", "gate": [["Main", "main.yml"]], "generated": [bad]}]})

    def test_movies_registers_its_baselines_and_the_fanout_refs(self):
        with open(os.path.join(os.path.dirname(HERE), "red-repos.json")) as f:
            movies = next(c for c in A.red_repos_parse(json.load(f)) if c["repo"] == "pawelkrupinski/movies")
        paths = {p: g["regen"] for g in movies["generated"] for p in g["paths"]}
        self.assertEqual(paths, {
            "test/resources/coupling-baseline.tsv": ["scripts/coupling-report", "--update"],
            "test/resources/coupling-fanout-refs.tsv": ["scripts/coupling-report", "--update"],
            **{f"test/resources/{n}-baseline.tsv": ["scripts/quality-report", "--update"]
               for n in ("complexity", "length", "duplication")},
            # BATCHED CHECKS (2026-11-20): the hand-written tables, merged row by row with no regen command
            **{p: None for p in ("test/resources/coupling-exemptions.tsv", "test/resources/coupling-layers.tsv",
                                 "test/resources/fixtures/identity-judgements.tsv",
                                 "test/resources/fixtures/identity-unmatched/expected-matches.tsv")}})


# the repo's regen command: writes the baseline the way the repo's own tool would, from the combined tree (its row
# `fanout\tA` counts the lines of web/src/main/A.scala), and logs that it ran
REGEN = """#!/bin/sh
echo ran >> "{log}"
[ -f FAIL_REGEN ] && {{ echo "sbt couplingReport failed" >&2; exit 2; }}
n=$(wc -l < web/src/main/A.scala | tr -d ' ')
sed "s/^fanout\tA\t.*/fanout\tA\t$n/" {path} > {path}.tmp && mv {path}.tmp {path}
"""


class GeneratedQueueTest(test_backlog_batch.BacklogBatchTest):
    def setUp(self):
        super().setUp()
        self.regen_log = os.path.join(self.tmp.name, "regen-ran")
        self.r.write("scripts/regen", REGEN.format(log=self.regen_log, path=BASELINE))
        os.chmod(os.path.join(self.r.path, "scripts/regen"), 0o755)
        self.r.write(BASELINE, table(ROWS))
        self.r.write(REFS, REFS_LINES)
        self.r.write("web/src/main/A.scala", "a\n")
        self.r.write("worker/src/fixtures/scala/tools/TestWiring.scala", "".join(f"wire {i}\n" for i in range(10)))
        self.r.commit("the baseline, its regen command, the test wiring")
        self.r.push()
        repos = os.path.join(self.tmp.name, "red-repos.json")
        with open(repos, "w") as f:
            json.dump({"repos": [{"repo": A.slug(self.r.path), "gate": [["Main", "main.yml"]],
                                  "generated": [{"paths": [BASELINE, REFS], "regen": "scripts/regen"}]}]}, f)
        self.saved_repos = os.environ.get("AGENTCTL_RED_REPOS")
        os.environ["AGENTCTL_RED_REPOS"] = repos
        for c in A.PROCESS_CACHES:
            c.clear()

    def tearDown(self):
        if self.saved_repos is None:
            os.environ.pop("AGENTCTL_RED_REPOS", None)
        else:
            os.environ["AGENTCTL_RED_REPOS"] = self.saved_repos
        super().tearDown()

    def change(self, name, rows=None, files=(), msg=None):
        """A change of its own worktree: the baseline's rows edited ({key: value}), and files written."""
        wt = self.r.worktree(f"wt-{name}", branch=name)
        if rows:
            with open(os.path.join(wt, BASELINE)) as f:
                text = f.read()
            for k, v in rows.items():
                text = "".join(f"{k}\t{v}\n" if line.rsplit("\t", 1)[0] == k else line for line in text.splitlines(True))
            self.r.write(BASELINE, text, at=wt)
        for path, text in dict(files).items():
            self.r.write(path, text, at=wt)
        self.r.commit(msg or f"{name}: its change", at=wt)
        return self.queued(wt)[0]

    def drive_as(self, role):
        """The batch driven to rest as on `role`'s host (regen_here: the laptop's sync role runs regen commands, the
        server's -- the tests' default -- none)."""
        saved = A.regen_here
        A.regen_here = lambda: role == "sync"
        try:
            return self.drive()
        finally:
            A.regen_here = saved

    def regen_runs(self):
        return len(open(self.regen_log).read().split()) if os.path.exists(self.regen_log) else 0

    def baseline_on_main(self):
        return sh(self.r.root, "git", "--git-dir", self.r.bare, "show", f"main:{BASELINE}") + "\n"

    # ------------------------------------------------------------------------------------------- the overlap hold
    def test_a_change_sharing_only_the_baseline_with_one_in_flight_is_not_held(self):
        eo = self.change("older", {"ce\tservices": 6}, {"identity/src/main/I.scala": "i\n"})  # identity: checked alone
        ey = self.change("younger", {"ce\ttools": 1}, {"identity/src/main/J.scala": "j\n"})
        did = A.tick_changes(self.r.path, self.d)
        self.assertEqual(self.started, [eo, ey], f"both checked at once: {did}")
        self.assertEqual([x for x in self.inbox() if "CONFLICTS" in json.dumps(x)], [], "no conflict to tell")
        hold = (A.Store().item("change", ey)["data"] or {}).get("hold") or {}
        self.assertEqual(hold.get("kind"), "starting", "not held: its executor is starting (`land status` says so)")

    def test_a_real_source_both_change_still_holds(self):
        eo = self.change("older", {"ce\tservices": 6}, {"identity/src/main/I.scala": "i\n",
                                                        "worker/src/fixtures/scala/tools/TestWiring.scala": "o\n"})
        ey = self.change("younger", {"ce\ttools": 1}, {"identity/src/main/J.scala": "j\n",
                                                       "worker/src/fixtures/scala/tools/TestWiring.scala": "y\n"})
        A.tick_changes(self.r.path, self.d)
        self.assertEqual(self.started, [eo], "the younger waits for the older")
        hold = A.Store().item("change", ey)["data"]["hold"]
        self.assertEqual((hold["kind"], hold["blocker"], hold["files"]),
                         ("overlap", eo, ["worker/src/fixtures/scala/tools/TestWiring.scala"]),
                         "held for the source alone, never the baseline")

    # ------------------------------------------------------------------------------------------- the batch
    def test_adjacent_rows_conflict_in_git_and_merge_in_the_backlog_batch(self):
        ea = self.change("a", {"ce\tservices": 6}, {"web/src/main/B.scala": "b\n"},
                         msg="a: cut a dependency\n\nCoupling: the seam belongs here")
        eb = self.change("b", {"ce\ttools": 1}, {"worker/src/main/C.scala": "c\n"})
        self.assertTrue(A.pair_conflict(self.r.path, self.entry(ea), self.entry(eb)), "git alone conflicts here")
        A.tick_changes(self.r.path, self.d)
        self.assertEqual(self.started, [], "both wait for the backlog batch")
        self.drive()
        self.assertEqual([self.entry(x)["status"] for x in (ea, eb)], ["landed", "landed"])
        self.assertEqual(len(self.calls_of("--range")), 1, "checked as one tree")
        self.assertIn("ce\tservices\t6\nce\ttools\t1\n", self.baseline_on_main(), "both rows, merged")
        self.assertEqual(self.regen_runs(), 0, "disjoint rows need no regen command")
        log = sh(self.r.root, "git", "--git-dir", self.r.bare, "log", "--format=%s|%(trailers:key=Coupling,valueonly)",
                 "-2", "main").splitlines()
        self.assertEqual(sorted(log), ["a: cut a dependency|the seam belongs here", "b: its change|"],
                         "each member's own commit, its trailer kept")

    def test_one_row_both_change_is_regenerated_over_the_combined_tree(self):
        ea = self.change("a", {"fanout\tA": 13}, {"web/src/main/A.scala": "a\na2\n"})
        eb = self.change("b", {"fanout\tA": 14}, {"worker/src/main/C.scala": "c\n"})
        A.tick_changes(self.r.path, self.d)
        self.drive_as("sync")
        self.assertEqual([self.entry(x)["status"] for x in (ea, eb)], ["landed", "landed"])
        self.assertEqual(self.regen_runs(), 1, "the repo's regen command, once")
        self.assertIn("fanout\tA\t2\n", self.baseline_on_main(), "the value measured over the combined tree")

    def test_a_failed_regeneration_leaves_the_member_out_and_holds_it_as_before(self):
        ea = self.change("a", {"fanout\tA": 13}, {"web/src/main/A.scala": "a\na2\n", "FAIL_REGEN": "x\n"})
        eb = self.change("b", {"fanout\tA": 14}, {"worker/src/main/C.scala": "c\n"})
        A.tick_changes(self.r.path, self.d)
        self.drive_as("sync")
        b = self.entry(eb)
        self.assertEqual(b["batch_alone"], "regen")
        log = open(os.path.join(self.d, "processor.log")).read()
        self.assertIn("generated: a row both sides changed, and `scripts/regen` exited 2: sbt couplingReport failed",
                      log)
        self.assertEqual(self.entry(ea)["status"], "queued", "the batch is gone: a is checked alone")
        A.tick_changes(self.r.path, self.d)
        self.assertEqual(self.started, [ea], "b waits behind a, as before")
        self.assertEqual(A.Store().item("change", eb)["data"]["hold"]["files"], [BASELINE])

    def test_the_server_role_runs_no_regen_command_and_keeps_mains_row_for_the_checks_to_judge(self):
        """ci-runner's reconciler has no JVM: a same-row conflict there takes the side already in the tree, never
        minutes in a tick, and the batch's checks (the ratchet over the combined tree) judge it."""
        ea = self.change("a", {"fanout\tA": 13}, {"web/src/main/A.scala": "a\na2\n"})
        eb = self.change("b", {"fanout\tA": 14}, {"worker/src/main/C.scala": "c\n"})
        A.tick_changes(self.r.path, self.d)
        self.drive_as("server")
        self.assertEqual(self.regen_runs(), 0)
        self.assertEqual([self.entry(x)["status"] for x in (ea, eb)], ["landed", "landed"])
        self.assertIn("fanout\tA\t13\n", self.baseline_on_main(), "a's row, already in the tree b was picked onto")
        self.assertIn("kept the tree's row", open(os.path.join(self.d, "processor.log")).read())

    # ------------------------------------------------------------------------------------------- the push's rebase
    def test_main_moving_by_a_generated_conflict_is_a_new_round_never_a_rejection(self):
        ey = self.change("younger", {"ce\ttools": 1}, {"identity/src/main/J.scala": "j\n"})
        A.check_entry(self.r.path, ey)
        self.assertEqual(self.entry(ey)["status"], "checked")
        other = self.r.worktree("wt-other", branch="other")  # lands meanwhile, on the next row
        with open(os.path.join(other, BASELINE)) as f:
            text = f.read()
        self.r.write(BASELINE, text.replace("ce\tservices\t7", "ce\tservices\t5"), at=other)
        self.r.commit("other: a row next to it", at=other)
        self.r.push(at=other)
        A.tick_changes(self.r.path, self.d)
        e = self.entry(ey)
        self.assertEqual((e["status"], e.get("verify")), ("queued", True), "a new round, not NOT LANDED")
        A.check_entry(self.r.path, ey)
        A.tick_changes(self.r.path, self.d)
        self.assertEqual(self.entry(ey)["status"], "landed")
        self.assertIn("ce\tservices\t5\nce\ttools\t1\n", self.baseline_on_main())


    # ------------------------------------------------------------------------------------------- main moved since QUEUED
    def test_f2b_replayed_main_remeasured_the_baseline_and_its_refs_and_the_change_still_applies(self):
        """2026-11-19 06:58Z, lane F2b (20261119-065741-movies-phase3-f2b-5c34a7): queued on 7db1859b8, main moved to
        2cfa0ee95 by c3922ba26, which re-measured coupling-baseline.tsv (the rows F2b's own baseline commit changed,
        differently) and coupling-fanout-refs.tsv (lines next to F2b's). Its third commit's pick conflicted in both;
        the refs file was not registered and its rows repeat a class, the baseline's same rows needed a regen the
        server role cannot run: NOT LANDED, a rebase by hand, a new place at the back of the queue. Now both are
        resolved in the check tree and the change is checked where it stood."""
        wt = self.r.worktree("wt-f2b", branch="f2b")
        self.r.write("web/src/main/D.scala", "d\n", at=wt)
        self.r.commit("f2b: the debug page becomes its own controller", at=wt)
        with open(os.path.join(wt, BASELINE)) as f:
            text = f.read()
        self.r.write(BASELINE, text.replace("fanout\tA\t12", "fanout\tA\t10"), at=wt)
        self.r.write(REFS, REFS_LINES.replace("A\tmodels.Y\n", ""), at=wt)
        self.r.commit("f2b: record the falls", at=wt)
        eid = self.queued(wt)[0]
        main = self.r.worktree("wt-remeasure", branch="remeasure")  # c3922ba26: the measurement changes under it
        self.r.write(BASELINE, text.replace("fanout\tA\t12", "fanout\tA\t15"), at=main)
        self.r.write(REFS, REFS_LINES.replace("A\tmodels.Y\n", "A\tmodels.Y\nA\tmodels.V\n"), at=main)
        self.r.commit("Coupling: a file's imports are charged to the classes using them\n\nCoupling: a measurement fix",
                      at=main)
        self.r.push(at=main)
        A.check_entry(self.r.path, eid)
        e = self.entry(eid)
        self.assertEqual(e["status"], "checked", e.get("why"))
        tip_file = lambda path: sh(self.r.path, "git", "show", f"{e['tip']}:{path}") + "\n"
        self.assertIn("fanout\tA\t15\n", tip_file(BASELINE), "main's re-measured row, for the ratchet to judge")
        self.assertEqual(tip_file(REFS), "A\tmodels.X\nA\tmodels.V\nA\ttools.Z\nB\tmodels.X\n", "both sides' lines")
        A.tick_changes(self.r.path, self.d)
        self.assertEqual(self.entry(eid)["status"], "landed")

    def test_a_change_failing_its_checks_over_mains_rows_is_told_to_regenerate(self):
        wt = self.r.worktree("wt-rise", branch="rise")
        with open(os.path.join(wt, BASELINE)) as f:
            text = f.read()
        self.r.write(BASELINE, text.replace("fanout\tA\t12", "fanout\tA\t13"), at=wt)
        self.r.write("web/src/main/BROKEN.scala", "the ratchet measures a rise\n", at=wt)
        self.r.commit("rise: a dependency\n\nCoupling: needed", at=wt)
        eid = self.queued(wt)[0]
        main = self.r.worktree("wt-remeasure", branch="remeasure")
        self.r.write(BASELINE, text.replace("fanout\tA\t12", "fanout\tA\t11"), at=main)
        self.r.commit("main: a fall", at=main)
        self.r.push(at=main)
        A.check_entry(self.r.path, eid)
        e = self.entry(eid)
        # A ROW ONLY A REGEN DECIDES IS NO BOUNCE: held in its place, its agent told ONE command
        self.assertEqual(e["status"], "queued")
        self.assertEqual(e["regen_hold"]["cmd"], f"cd {wt} && ~/bin/agentctl land")
        self.assertIn(f"REGEN NEEDED {eid}: its {BASELINE} kept origin/main's rows where both sides changed them", e["why"])
        self.assertIn(f"it keeps its place in the queue: run `cd {wt} && ~/bin/agentctl land`", e["why"])
        self.assertIn("runs `scripts/regen` on this machine", e["why"])
        A.tick_changes(self.r.path, self.d)  # held: not checked again, not rejected
        self.assertEqual(self.entry(eid)["status"], "queued")
        later = self.change("later", files={"web/src/main/L.scala": "l\n"})
        self.r.write("web/src/main/BROKEN.scala", "", at=wt)  # its agent ran the command: rebased, regenerated
        self.r.commit("rise: regenerated", at=wt)
        again = self.queued(wt)[0]
        self.assertEqual(self.entry(again)["seq"], self.entry(eid)["seq"], "the held change's place")
        self.assertLess(self.entry(again)["seq"], self.entry(later)["seq"])
        self.assertEqual(self.entry(again)["queued_since"], A.entry_queued_since(self.entry(eid)), "and its queue age")
        self.assertEqual(self.entry(eid)["status"], "superseded")

    def test_a_regen_hold_nobody_answers_is_rejected_after_its_bound(self):
        wt = self.r.worktree("wt-rise", branch="rise")
        with open(os.path.join(wt, BASELINE)) as f:
            text = f.read()
        self.r.write(BASELINE, text.replace("fanout\tA\t12", "fanout\tA\t13"), at=wt)
        self.r.write("web/src/main/BROKEN.scala", "the ratchet measures a rise\n", at=wt)
        self.r.commit("rise: a dependency\n\nCoupling: needed", at=wt)
        eid = self.queued(wt)[0]
        main = self.r.worktree("wt-remeasure", branch="remeasure")
        self.r.write(BASELINE, text.replace("fanout\tA\t12", "fanout\tA\t11"), at=main)
        self.r.commit("main: a fall", at=main)
        self.r.push(at=main)
        A.check_entry(self.r.path, eid)
        self.assertEqual(self.entry(eid)["status"], "queued")
        A.tick_changes(self.r.path, self.d, now_=time.time() + A.CONFLICT_PLACE_KEPT + 60)
        e = self.entry(eid)
        self.assertEqual(e["status"], "rejected")
        self.assertIn(f"waited 24 h for `cd {wt} && ~/bin/agentctl land`", e["why"])

    def test_a_stale_change_main_moved_under_is_rebased_in_the_check_tree_and_goes_on(self):
        """The 3-way rebase of a non-generated change: main changed the same file in another hunk since the change
        queued -- it is picked onto the new main in its check tree and lands, never bounced to its agent."""
        wiring = "worker/src/fixtures/scala/tools/TestWiring.scala"
        lines = [f"wire {i}\n" for i in range(10)]
        wt = self.r.worktree("wt-mine", branch="mine")
        self.r.write(wiring, "".join(lines[:9] + ["wire 9 mine\n"]), at=wt)
        self.r.commit("mine: rewire the last", at=wt)
        eid = self.queued(wt)[0]
        other = self.r.worktree("wt-other", branch="other")
        self.r.write(wiring, "".join(["wire 0 theirs\n"] + lines[1:]), at=other)
        self.r.commit("other: rewire the first", at=other)
        self.r.push(at=other)
        A.check_entry(self.r.path, eid)
        A.tick_changes(self.r.path, self.d)
        self.assertEqual(self.entry(eid)["status"], "landed", self.entry(eid).get("why"))
        on_main = sh(self.r.root, "git", "--git-dir", self.r.bare, "show", f"main:{wiring}")
        self.assertIn("wire 0 theirs", on_main)
        self.assertIn("wire 9 mine", on_main)

    def test_a_place_kept_never_reuses_an_id_number(self):
        """Queue simulator, replay 2026-11-17 seed 1006: the entry that took a bounced entry's place carried its old
        seq, so the next submission drew the number just used -- one id for two submissions in the same second, and
        the second answered with the first entry (lost)."""
        d = os.path.join(self.tmp.name, "ids")
        os.makedirs(os.path.join(d, "entries"))
        now = A.ts()
        A.save_entry(d, {"id": "a-1", "seq": 1, "worktree": "/w", "branch": "b", "status": "rejected",
                         "conflict_files": ["x"], "decided": now, "created": now})
        A.save_entry(d, {"id": "o-2", "seq": 2, "worktree": "/o", "branch": "o", "status": "queued", "created": now})
        first, _ = A.queue_change(d, {"worktree": "/w", "branch": "b", "head": "1" * 40})
        second, _ = A.queue_change(d, {"worktree": "/x", "branch": "x", "head": "2" * 40})
        self.assertEqual((first["seq"], first["drawn"]), (1, 3), "its old place, a new number")
        self.assertEqual(second["drawn"], 4)
        self.assertNotEqual(first["id"].rsplit("-", 1)[1], second["id"].rsplit("-", 1)[1])

    def test_the_change_item_carries_its_queue_age(self):
        e = {"id": "x", "created": "2026-11-19T06:57:41Z", "worktree": "/w", "head": "h"}
        self.assertEqual(A.change_item_fields(e)["submitted_at"], A.parse_time("2026-11-19T06:57:41Z").timestamp())
        self.assertEqual(A.change_item_fields({**e, "queued_since": 5.0})["submitted_at"], 5.0)

    def test_a_real_conflict_bounces_naming_its_files_and_the_commit_of_main_that_caused_it(self):
        wiring = "worker/src/fixtures/scala/tools/TestWiring.scala"
        wt = self.r.worktree("wt-mine", branch="mine")
        self.r.write(wiring, "mine\n", at=wt)
        self.r.commit("mine: rewire", at=wt)
        eid = self.queued(wt)[0]
        other = self.r.worktree("wt-other", branch="other")
        self.r.write(wiring, "theirs\n", at=other)
        culprit = self.r.commit("other: rewire differently", at=other)
        self.r.push(at=other)
        A.check_entry(self.r.path, eid)
        e = self.entry(eid)
        self.assertEqual(e["status"], "rejected")
        self.assertIn(f"it conflicts in {wiring} (changed on main by {culprit[:10]} other: rewire differently)", e["why"])
        self.assertEqual(e["conflict_files"], [wiring])

    def test_the_change_requeued_after_resolving_a_conflict_keeps_its_place(self):
        """Only a real conflict reaches the agent, and resolving it costs no queue place: the change queued again from
        the same worktree and branch takes the rejected entry's place (its seq), ahead of what queued meanwhile."""
        wiring = "worker/src/fixtures/scala/tools/TestWiring.scala"
        wt = self.r.worktree("wt-mine", branch="mine")
        self.r.write(wiring, "mine\n", at=wt)
        self.r.commit("mine: rewire", at=wt)
        first = self.queued(wt)[0]
        other = self.r.worktree("wt-other", branch="other")
        self.r.write(wiring, "theirs\n", at=other)
        self.r.commit("other: rewire differently", at=other)
        self.r.push(at=other)
        A.check_entry(self.r.path, first)
        self.assertEqual(self.entry(first)["status"], "rejected")
        later = self.change("later", files={"web/src/main/L.scala": "l\n"})
        sh(wt, "git", "fetch", "-q", "origin")
        sh(wt, "git", "rebase", "-q", "-X", "theirs", "origin/main")  # the agent resolves it
        again = self.queued(wt)[0]
        self.assertEqual(self.entry(again)["seq"], self.entry(first)["seq"], "its old place")
        self.assertLess(self.entry(again)["seq"], self.entry(later)["seq"])
        self.assertEqual(self.entry(again)["requeued_from"], first)


class HandWrittenTableTest(GeneratedQueueTest):
    """BATCHED CHECKS (the owner, 2026-11-20: "generated baselines are not conflicts"): a hand-written table merged row
    by row -- movies' coupling-exemptions.tsv, coupling-layers.tsv, the judgements -- registered with no regen command.
    Two changes adding or editing disjoint rows are batched, never held; a row both change stays the author's
    conflict, and never takes one side's row provisionally (no ratchet would judge a lost exemption)."""

    def setUp(self):
        super().setUp()
        with open(os.environ["AGENTCTL_RED_REPOS"], "w") as f:
            json.dump({"repos": [{"repo": A.slug(self.r.path), "gate": [["Main", "main.yml"]],
                                  "generated": [{"paths": [BASELINE]}]}]}, f)
        for c in A.PROCESS_CACHES:
            c.clear()

    def test_disjoint_rows_are_no_hold_and_merge_in_the_batch(self):
        ea = self.change("a", {"ce\tservices": 6}, {"web/src/main/B.scala": "b\n"})
        eb = self.change("b", {"ce\ttools": 1}, {"worker/src/main/C.scala": "c\n"})
        A.tick_changes(self.r.path, self.d)
        self.assertEqual(self.started, [], "both wait for the backlog batch: no overlap hold")
        self.drive_as("server")
        self.assertEqual([self.entry(x)["status"] for x in (ea, eb)], ["landed", "landed"])
        self.assertIn("ce\tservices\t6\nce\ttools\t1\n", self.baseline_on_main(), "both rows")
        self.assertEqual(self.regen_runs(), 0)

    def test_a_row_both_change_is_never_kept_provisionally(self):
        ea = self.change("a", {"fanout\tA": 13}, {"web/src/main/A.scala": "a\na2\n"})
        eb = self.change("b", {"fanout\tA": 14}, {"worker/src/main/C.scala": "c\n"})
        A.tick_changes(self.r.path, self.d)
        self.drive_as("server")
        self.assertEqual(self.regen_runs(), 0)
        self.assertEqual(self.entry(eb)["batch_alone"], "regen", "b falls out of the stack, its why in the log")
        self.assertIn("a row both sides changed in test/resources/coupling-baseline.tsv, which no regen command decides",
                      open(os.path.join(self.d, "processor.log")).read())
        self.assertNotEqual(self.entry(eb)["status"], "landed", "never landed on a's row")


for _cls in (GeneratedQueueTest, HandWrittenTableTest):
    for _name in dir(test_backlog_batch.BacklogBatchTest):  # its setUp only, not its tests
        if _name.startswith("test_") and _name not in _cls.__dict__ and _name not in GeneratedQueueTest.__dict__:
            setattr(_cls, _name, None)
for _name in list(GeneratedQueueTest.__dict__):  # nor the generated tests (a regen command registered) twice
    if _name.startswith("test_") and _name not in HandWrittenTableTest.__dict__:
        setattr(HandWrittenTableTest, _name, None)

if __name__ == "__main__":
    unittest.main()
