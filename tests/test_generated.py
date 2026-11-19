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


class RegistryTest(unittest.TestCase):
    def test_a_generated_entry_needs_its_paths_and_regen_command(self):
        ok = A.red_repos_parse({"repos": [{"repo": "o/r", "gate": [["Main", "main.yml"]], "generated": [
            {"paths": [BASELINE], "regen": "scripts/coupling-report --update", "timeout": 600}]}]})
        conf = next(c for c in ok if c["repo"] == "o/r")
        self.assertEqual(conf["generated"], [{"paths": [BASELINE], "regen": ["scripts/coupling-report", "--update"],
                                              "timeout": 600}])
        for bad in ({"paths": [BASELINE]}, {"regen": "x"}, {"paths": ["/abs"], "regen": "x"},
                    {"paths": [BASELINE], "regen": "x", "timeout": 0}):
            with self.assertRaises(ValueError, msg=bad):
                A.red_repos_parse({"repos": [{"repo": "o/r", "gate": [["Main", "main.yml"]], "generated": [bad]}]})

    def test_movies_registers_its_four_baselines(self):
        with open(os.path.join(os.path.dirname(HERE), "red-repos.json")) as f:
            movies = next(c for c in A.red_repos_parse(json.load(f)) if c["repo"] == "pawelkrupinski/movies")
        paths = {p: g["regen"] for g in movies["generated"] for p in g["paths"]}
        self.assertEqual(paths, {
            "test/resources/coupling-baseline.tsv": ["scripts/coupling-report", "--update"],
            **{f"test/resources/{n}-baseline.tsv": ["scripts/quality-report", "--update"]
               for n in ("complexity", "length", "duplication")}})


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
        self.r.write("web/src/main/A.scala", "a\n")
        self.r.write("worker/src/fixtures/scala/tools/TestWiring.scala", "".join(f"wire {i}\n" for i in range(10)))
        self.r.commit("the baseline, its regen command, the test wiring")
        self.r.push()
        repos = os.path.join(self.tmp.name, "red-repos.json")
        with open(repos, "w") as f:
            json.dump({"repos": [{"repo": A.slug(self.r.path), "gate": [["Main", "main.yml"]],
                                  "generated": [{"paths": [BASELINE], "regen": "scripts/regen"}]}]}, f)
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
        self.assertIsNone((A.Store().item("change", ey)["data"] or {}).get("hold"))

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

    def test_the_server_role_runs_no_regen_command_and_takes_the_fallback(self):
        """ci-runner's reconciler has no JVM: a same-row conflict there is the member's, never minutes in a tick."""
        ea = self.change("a", {"fanout\tA": 13}, {"web/src/main/A.scala": "a\na2\n"})
        eb = self.change("b", {"fanout\tA": 14}, {"worker/src/main/C.scala": "c\n"})
        A.tick_changes(self.r.path, self.d)
        self.drive_as("server")
        self.assertEqual(self.regen_runs(), 0)
        self.assertEqual(self.entry(eb)["batch_alone"], "regen")
        self.assertIn("the server role runs no regen command", open(os.path.join(self.d, "processor.log")).read())
        self.assertEqual(self.entry(ea)["status"], "queued")

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


for _name in dir(test_backlog_batch.BacklogBatchTest):  # its setUp only, not its tests
    if _name.startswith("test_") and _name not in GeneratedQueueTest.__dict__:
        setattr(GeneratedQueueTest, _name, None)

if __name__ == "__main__":
    unittest.main()
