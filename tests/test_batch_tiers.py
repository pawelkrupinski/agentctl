"""THE BATCH TIERS (queuebatch.py; the owner, 2026-11-21: "make agentctl batch even more, merge / fix conflicts"). Two
queued changes that overlap are checked as ONE batch whenever one of these assembles them, in order:
1. git merges them cleanly (merge-tree);
2. the overlap is in generated files: rows merged, else the regen command -- and a SNAPSHOT (`merge: regen`) is always
   regenerated over the combined tree when both sides changed it, its output explained by the sides (waiting.py
   SNAPSHOTS ARE REGENERATED, NEVER MERGED);
3. a real source conflict whose hunks are additions on both sides, resolved in the backlog batch's assembly, the
   batch's full landing check its verification (waiting.py A SOURCE CONFLICT IS RESOLVED IN THE BATCH);
4. else the younger waits for the older, as before, and its author is told at once, with the hunks and the fix.
A resolution whose combined tree fails its checks is the culprit: the member is held behind the older, never
rejected for it."""
import json, os, sys, unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from test_agentctl import A, sh  # noqa: E402
import test_backlog_batch, test_generated  # noqa: E402

SNAP = "test/resources/fixtures/08-06-2026/expected-index.html"
# the repo's snapshot regen: renders the page from src/*.txt, sorted (as PageSnapshotSpec renders the corpus); with
# both a.txt and z.txt in the tree it renders a line neither side's own regen had (an interaction of the two)
SNAP_REGEN = """#!/bin/sh
echo ran >> "{log}"
[ -f FAIL_REGEN ] && {{ echo "PageSnapshotSpec wrote no snapshot" >&2; exit 2; }}
mkdir -p $(dirname {snap})
cat $(ls src/*.txt | sort) > {snap}
[ -f INTERACT ] && [ -f src/a.txt ] && [ -f src/z.txt ] && echo "both" >> {snap}
exit 0
"""
LIST = "web/src/test/scala/tools/Swallows.scala"
LIST_TEXT = "object Swallows {\n  val Rows = Map(\n    a -> 1,\n    b -> 2\n  )\n}\n"
# the batch's check fails when the combined list holds both rows (a resolution that compiles and is wrong)
FAIL_BOTH = ('if [ -f FAIL_BOTH ] && grep -q "c -> 3" ' + LIST + ' && grep -q "d -> 4" ' + LIST
             + '; then echo "[info] - Swallows has no stale entry *** FAILED ***"; exit 1; fi\n')


class TiersTest(test_generated.GeneratedQueueTest):
    def setUp(self):
        super().setUp()
        self.r.write("scripts/snap", SNAP_REGEN.format(log=self.regen_log, snap=SNAP))
        os.chmod(os.path.join(self.r.path, "scripts/snap"), 0o755)
        hook = os.path.join(self.r.path, "scripts/hooks/pre-push")
        text = open(hook).read()
        self.r.write("scripts/hooks/pre-push", text.replace("\n", "\n" + FAIL_BOTH, 1))
        os.chmod(hook, 0o755)
        self.r.write("src/m.txt", "m\n")
        self.r.write(SNAP, "m\n")
        self.r.write(LIST, LIST_TEXT)
        self.r.commit("the page snapshot, its regen, a hand-written Scala table")
        self.r.push()
        with open(os.environ["AGENTCTL_RED_REPOS"], "w") as f:
            json.dump({"repos": [{"repo": A.slug(self.r.path), "gate": [["Main", "main.yml"]],
                                  "generated": [{"paths": [test_generated.BASELINE], "regen": "scripts/regen"},
                                                {"paths": [SNAP], "regen": "scripts/snap", "merge": "regen"}]}]}, f)
        for c in A.PROCESS_CACHES:
            c.clear()

    def snap_on_main(self):
        return sh(self.r.root, "git", "--git-dir", self.r.bare, "show", f"main:{SNAP}") + "\n"

    def file_on_main(self, path):
        return sh(self.r.root, "git", "--git-dir", self.r.bare, "show", f"main:{path}") + "\n"

    def held(self, eid):
        return (A.open_store().item("change", eid)["data"] or {}).get("hold") or {}

    # ------------------------------------------------------------------------------------------- tier 2: snapshots
    def test_split_shared_js_replayed_a_snapshot_both_change_is_no_hold(self):
        """2026-11-21 ~07:40Z: split-shared-js `held: its check waits for scrapers-2 (checking)`, both changing the
        page snapshots. A snapshot is generated: no overlap, the younger is checked now beside the older."""
        eo = self.change("scrapers-2", files={"src/a.txt": "a\n", SNAP: "a\nm\n", "identity/src/main/I.scala": "i\n"})
        ey = self.change("split-shared-js", files={"src/z.txt": "z\n", SNAP: "m\nz\n"})
        A.tick_changes(self.r.path, self.d)
        self.assertNotEqual(self.held(ey).get("kind"), "overlap", self.held(ey))
        self.assertIsNone(A.overlap_hold(self.r.path, self.d, self.entry(ey), [self.entry(eo), self.entry(ey)], None,
                                         A.time.time()), "nothing holds it behind scrapers-2")

    def test_a_snapshot_git_merges_cleanly_is_regenerated_all_the_same_and_lands_in_one_check(self):
        ea = self.change("a", files={"src/a.txt": "a\n", SNAP: "a\nm\n"})
        eb = self.change("b", files={"src/z.txt": "z\n", SNAP: "m\nz\n"})
        self.assertIsNone(A.pair_conflict(self.r.path, self.entry(ea), self.entry(eb)), "git alone merges these")
        A.tick_changes(self.r.path, self.d)
        self.drive_as("sync")
        self.assertEqual([self.entry(x)["status"] for x in (ea, eb)], ["landed", "landed"])
        self.assertEqual(len(self.calls_of("--range")), 1, "one tree, one check")
        self.assertEqual(self.regen_runs(), 1, "regenerated over the combined tree, though git merged it")
        self.assertEqual(self.snap_on_main(), "a\nm\nz\n")

    def test_a_snapshot_both_change_in_one_place_is_regenerated_in_the_batch(self):
        ea = self.change("a", files={"src/a.txt": "a\n", SNAP: "a\nm\n"})
        eb = self.change("b", files={"src/b.txt": "b\n", SNAP: "b\nm\n"})
        self.assertTrue(A.pair_conflict(self.r.path, self.entry(ea), self.entry(eb)), "git conflicts here")
        A.tick_changes(self.r.path, self.d)
        self.assertEqual(self.started, [], "both wait for the backlog batch")
        self.drive_as("sync")
        self.assertEqual([self.entry(x)["status"] for x in (ea, eb)], ["landed", "landed"])
        self.assertEqual(len(self.calls_of("--range")), 1, "one check")
        self.assertEqual(self.snap_on_main(), "a\nb\nm\n", "what the combined tree renders, never a line merge")

    def test_a_regenerated_snapshot_holding_a_change_neither_side_made_is_no_resolution(self):
        """A REGENERATED SNAPSHOT HIDES NOTHING: the combined code renders a line neither side's regen had -- an
        interaction nobody committed. Not landed by a regenerator: the member is held behind the older, as before."""
        ea = self.change("a", files={"src/a.txt": "a\n", SNAP: "a\nm\n", "INTERACT": "x\n"})
        eb = self.change("b", files={"src/z.txt": "z\n", SNAP: "m\nz\n"})
        A.tick_changes(self.r.path, self.d)
        self.drive_as("sync")
        self.assertEqual(self.entry(eb)["batch_alone"], "regen")
        self.assertIn(f"the regenerated {SNAP} holds a change neither side made (+both)",
                      open(os.path.join(self.d, "processor.log")).read())
        self.assertNotEqual(self.entry(eb)["status"], "landed")
        A.tick_changes(self.r.path, self.d)
        self.assertEqual(self.held(eb).get("blocker"), ea, "held behind a, as before")

    def test_a_snapshot_regen_failing_on_the_combined_tree_falls_back_to_the_hold_never_a_reject(self):
        ea = self.change("a", files={"src/a.txt": "a\n", SNAP: "a\nm\n", "FAIL_REGEN": "x\n"})
        eb = self.change("b", files={"src/b.txt": "b\n", SNAP: "b\nm\n"})
        A.tick_changes(self.r.path, self.d)
        self.drive_as("sync")
        b = self.entry(eb)
        self.assertEqual((b["batch_alone"], b["status"]), ("regen", "queued"), "left out, never rejected")
        self.assertIn("both sides changed the snapshot `scripts/snap` exited 2: PageSnapshotSpec wrote no snapshot",
                      open(os.path.join(self.d, "processor.log")).read())
        A.tick_changes(self.r.path, self.d)
        self.assertEqual(self.started, [ea], "a is checked alone; b waits behind it")
        self.assertEqual((self.held(eb)["kind"], self.held(eb)["blocker"]), ("overlap", ea))

    def test_the_server_role_never_keeps_a_side_of_a_snapshot(self):
        ea = self.change("a", files={"src/a.txt": "a\n", SNAP: "a\nm\n"})
        eb = self.change("b", files={"src/b.txt": "b\n", SNAP: "b\nm\n"})
        A.tick_changes(self.r.path, self.d)
        self.drive_as("server")
        self.assertEqual(self.regen_runs(), 0)
        self.assertEqual(self.entry(eb)["batch_alone"], "regen")
        self.assertNotIn("provisional", self.entry(eb))
        self.assertEqual(self.entry(ea)["status"], "queued")

    # ------------------------------------------------------------------------------------------- tier 3: source
    def two_appends(self, extra_a=None):
        ea = self.change("a", files={LIST: LIST_TEXT.replace("    b -> 2\n", "    b -> 2,\n    c -> 3\n"),
                                     "web/src/main/A.scala": "a\n", **(extra_a or {})})
        eb = self.change("b", files={LIST: LIST_TEXT.replace("    b -> 2\n", "    b -> 2,\n    d -> 4\n"),
                                     "worker/src/main/B.scala": "b\n"})
        return ea, eb

    def test_worker_rest_2_replayed_both_queued_two_appends_to_a_hand_written_table_land_as_one(self):
        """2026-11-21: worker-rest-2 and review-common both changed RepositoryReadSwallows.scala, a Scala Map of
        multi-line entries (no one-row-a-line table: not registered). Queued together, two entries appended at its end
        conflict in git (each gave the last entry its comma); the batch resolves it, its full check the verification."""
        ea, eb = self.two_appends()
        self.assertTrue(A.pair_conflict(self.r.path, self.entry(ea), self.entry(eb)), "git conflicts here")
        A.tick_changes(self.r.path, self.d)
        self.assertEqual(self.started, [], "both wait for the backlog batch: no hold, nothing told")
        self.assertEqual([x for x in self.inbox() if "CONFLICTS" in json.dumps(x)], [])
        self.drive()
        self.assertEqual([self.entry(x)["status"] for x in (ea, eb)], ["landed", "landed"])
        self.assertEqual(len(self.calls_of("--range")), 1, "one tree, one full check")
        self.assertIn("    b -> 2,\n    c -> 3,\n    d -> 4\n", self.file_on_main(LIST))
        trailer = sh(self.r.root, "git", "--git-dir", self.r.bare, "log", "-1",
                     "--format=%(trailers:key=Auto-Resolved,valueonly)", "main")
        self.assertIn(f"{LIST}: line 4: kept both sides' added lines (1 ours, then 1 theirs, a comma between)", trailer,
                      "the landed commit says what was resolved")
        said = [ev["text"] for ev in A.open_store().item_events("change", eb) if "AUTO-RESOLVED" in (ev.get("text") or "")]
        self.assertTrue(said, "its author is told, while it checks")

    def test_a_resolution_failing_the_batchs_checks_is_the_culprit_and_falls_back_to_the_hold(self):
        ea, eb = self.two_appends({"FAIL_BOTH": "x\n"})
        A.tick_changes(self.r.path, self.d)
        self.drive()
        self.assertEqual(self.entry(ea)["status"], "landed", "the prefix before it lands")
        b = self.entry(eb)
        self.assertNotEqual(b["status"], "landed")
        self.assertEqual(b["batch_alone"], "resolve", "never rejected for the resolution")
        log = open(os.path.join(self.d, "processor.log")).read()
        self.assertIn(f"the batch's AUTO-RESOLUTION of {LIST} is the culprit", log)
        self.assertNotIn(f"NOT LANDED: the checks failed with your change in the batch (entry {eb})", b.get("why") or "")

    def test_a_conflict_no_tier_resolves_holds_and_tells_its_author_the_hunks_and_the_fix(self):
        ea = self.change("a", files={LIST: LIST_TEXT.replace("a -> 1", "a -> 9")})
        eb = self.change("b", files={LIST: LIST_TEXT.replace("a -> 1", "a -> 8")})
        A.tick_changes(self.r.path, self.d)
        self.assertEqual(self.started, [ea], "the younger waits for the older")
        told = [x for x in self.inbox() if f"CONFLICTS {eb}" in json.dumps(x)]
        self.assertEqual(len(told), 1, "told at once")
        text = json.dumps(told[0])
        self.assertIn(f"{LIST} line 3: {ea} has `a -> 9,`, this change `a -> 8,`", text)
        self.assertIn("Fix now: `cd ", text)
        self.assertIn("git rebase a`", text)


class SnapshotRegistryTest(unittest.TestCase):
    def test_a_snapshot_entry_needs_its_regen_command(self):
        ok = A.red_repos_parse({"repos": [{"repo": "o/r", "gate": [["Main", "main.yml"]], "generated": [
            {"paths": [SNAP], "regen": "scripts/snap", "merge": "regen"}]}]})
        self.assertEqual(next(c for c in ok if c["repo"] == "o/r")["generated"][0]["merge"], "regen")
        for bad in ({"paths": [SNAP], "merge": "regen"}, {"paths": [SNAP], "regen": "x", "merge": "lines"}):
            with self.assertRaises(ValueError, msg=bad):
                A.red_repos_parse({"repos": [{"repo": "o/r", "gate": [["Main", "main.yml"]], "generated": [bad]}]})


class HunksTest(unittest.TestCase):
    def test_additions_on_both_sides_resolve_and_a_line_both_changed_does_not(self):
        base = "import a\nimport b\n\nx\n"
        got = A.hunks_resolve(base, base.replace("import b\n", "import b\nimport c\n"),
                              base.replace("import b\n", "import b\nimport d\n"))
        self.assertEqual(got[0], "import a\nimport b\nimport c\nimport d\n\nx\n")
        self.assertIsNone(A.hunks_resolve(base, base.replace("x", "y"), base.replace("x", "z")))
        self.assertIsNone(A.hunks_resolve(base, base.replace("import b\n", ""), base.replace("import b", "import B")),
                          "a deletion against an edit")

    def test_a_snapshot_change_neither_side_made_is_named(self):
        self.assertIsNone(A.snapshot_unexplained("a\nb\nc\n", "a\nB\nc\n", "a\nb\nC\n", "a\nB\nC\n"))
        self.assertEqual(A.snapshot_unexplained("a\nb\nc\n", "a\nB\nc\n", "a\nb\nC\n", "a\nB\nC\nX\n"), "+X")
        self.assertEqual(A.snapshot_unexplained("a\nb\n", "a\nb\nc\n", "a\nb\n", "a\nc\n"), "-b")


for _name in list(test_generated.GeneratedQueueTest.__dict__) + dir(test_backlog_batch.BacklogBatchTest):
    if _name.startswith("test_") and _name not in TiersTest.__dict__:
        setattr(TiersTest, _name, None)

if __name__ == "__main__":
    unittest.main()
