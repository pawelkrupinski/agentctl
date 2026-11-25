"""ONLY A VOIDED CHECK HOLDS (generated.py; 2026-11-25, the owner: "fix structurally / for the future").
`agentctl status` had py-scalafix `held: its check waits for py-isolated (checking): both change docs/tools.md`: a
change checked alone (batch_alone) was judged strictly, its derived page an overlap. And a generated file held a
change behind a check in flight by default, unless its entry opted out (`holds: false`). Now a queued change waits for
an older one only where a check of it now is thrown away -- their patches do not merge, a source both change, or a
generated file whose registry entry is declared `order_sensitive: true` -- and a derived file never holds."""
import json, os, sys, unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from test_agentctl import A, sh  # noqa: E402
from fake_clock import on_clock  # noqa: E402
import test_backlog_batch, test_derived, test_generated  # noqa: E402
from test_derived import CATALOGUE, CLASSES, tool  # noqa: E402

SOURCE = "scripts/ci/ci_timings.py"
SOURCE_TEXT = "".join(f"line {i}\n" for i in range(1, 11))
CLASSES_TEXT = "# path\tclass\n" + "".join(f"tools/t{i}.sh\tKEEP\n" for i in range(10))


class ConflictHoldTest(test_derived.DerivedTest):
    def setUp(self):
        super().setUp()
        self.r.write(SOURCE, SOURCE_TEXT)
        self.r.write(CLASSES, CLASSES_TEXT)
        self.r.commit("a shared script, the migration table's rows")
        self.r.push()
        conf = json.load(open(os.environ["AGENTCTL_RED_REPOS"]))
        conf["repos"][0]["generated"].append({"paths": [CLASSES]})  # a table no entry declares order-sensitive
        with open(os.environ["AGENTCTL_RED_REPOS"], "w") as f:
            json.dump(conf, f)
        for c in A.PROCESS_CACHES:
            c.clear()

    def lane_of(self, name, tool_name, classes=(), line=None, alone=False, row=None):
        """A shell-to-Python lane: a tool added (docs/tools.md regenerated), rows appended to the migration table,
        one line of the shared script rewritten ((index, text)); alone: landed with --no-batch."""
        wt = self.r.worktree(f"wt-{name}", branch=name)
        self.r.write(f"tools/{tool_name}.sh", tool(f"the {tool_name} tool"), at=wt)
        if row is not None:  # one existing row of the migration table re-classed
            with open(os.path.join(wt, CLASSES)) as f:
                rows = f.read().splitlines(True)
            rows[row + 1] = rows[row + 1].replace("KEEP", "MIGRATE")
            self.r.write(CLASSES, "".join(rows), at=wt)
        if classes:
            with open(os.path.join(wt, CLASSES)) as f:
                self.r.write(CLASSES, f.read() + "".join(f"{r}\n" for r in classes), at=wt)
        if line:
            lines = SOURCE_TEXT.splitlines(True)
            lines[line[0]] = line[1] + "\n"
            self.r.write(SOURCE, "".join(lines), at=wt)
        sh(wt, "scripts/catalogue")
        self.r.commit(f"{name}: its change", at=wt)
        if alone:
            rc, out = self.land(wt, "--no-batch")
            self.assertEqual(rc, 0, out)
            return out.split("QUEUED ", 1)[1].split()[0]
        return self.queued(wt)[0]

    def test_lanes_sharing_only_generated_files_with_a_check_in_flight_are_one_batch_one_check_one_push(self):
        """py-isolated in flight; py-reuse sharing the catalogue and the migration table with it; ci-timings-bar
        sharing the catalogue with py-reuse and editing the shared script apart. Before: py-reuse held behind the check
        in flight for the table, ci-timings-bar checked alone beside it. Now neither waits for py-isolated: the two
        queued lanes are one backlog batch -- one check, one push -- and main's catalogue is the generator's."""
        ei = self.lane_of("isolated", "i", classes=["tools/i.sh\tMIGRATE"], alone=True)
        self.in_flight(ei)
        er = self.lane_of("reuse", "r", classes=["tools/r.sh\tKEEP"], line=(0, "reuse's line 1"))
        eb = self.lane_of("bar", "b", line=(9, "bar's line 10"))
        A.tick_changes(self.r.path, self.d, *on_clock(A.Clock()))
        self.assertEqual([x for x in self.started if x != ei], [], "the two queued lanes wait for their batch")
        for x in (er, eb):
            self.assertNotEqual(self.hold_of(x).get("kind"), "overlap", self.hold_of(x))
        A.check_entry(self.r.path, ei, *on_clock(self.clock))
        A.tick_changes(self.r.path, self.d, *on_clock(A.Clock()))
        self.assertEqual(self.entry(ei)["status"], "landed")
        calls, pushes = len(self.calls_of("--range")), len(self.pushed())
        self.drive()
        self.assertTrue(self.landed(er, eb), [self.entry(x) for x in (er, eb)])
        self.assertEqual(len(self.calls_of("--range")) - calls, 1, "one batch, one check")
        self.assertEqual(len(self.pushed()) - pushes, 1, "one push")
        self.assertIn("## Tools (4)", self.on_main(CATALOGUE))
        self.assertEqual(self.on_main(CATALOGUE), self.generated_over_main(CATALOGUE, "scripts/catalogue"))
        self.assertEqual(self.on_main(SOURCE).splitlines()[::9], ["reuse's line 1", "bar's line 10"])
        self.assertEqual(sorted(x for x in self.on_main(CLASSES).splitlines()[1:] if not x.startswith("tools/t")),
                         ["tools/i.sh\tMIGRATE", "tools/r.sh\tKEEP"])

    def test_a_change_checked_alone_sharing_only_the_catalogue_with_a_check_in_flight_is_checked_now(self):
        """py-scalafix (2026-11-25, `level 149`): left out of a batch (batch_alone `regen`), it was judged strictly --
        every file it shares an overlap -- and held behind py-isolated's, then py-mirror-service's check for
        docs/tools.md and migration-classes.tsv. A derived file is no overlap even then, nor a generated file no entry
        declares order-sensitive whose rows merge: combined over whatever tree it lands in."""
        ei = self.lane_of("isolated", "i", alone=True, row=1)
        self.in_flight(ei)
        es = self.lane_of("scalafix", "s", row=8)
        e = self.entry(es)
        e["batch_alone"] = "regen"
        A.save_entry(self.d, e)
        A.tick_changes(self.r.path, self.d, *on_clock(A.Clock()))
        self.assertIn(es, self.started, f"checked now, beside the older: {self.hold_of(es)}")
        self.check_and_push(ei, es)
        self.assertEqual([x.split("\t")[0] for x in self.on_main(CLASSES).splitlines() if "MIGRATE" in x],
                         ["tools/t1.sh", "tools/t8.sh"], "both rows re-classed on main")

    def test_a_real_conflict_still_holds_naming_the_source_never_the_derived_page(self):
        """Two lanes rewriting one line of the shared script differently cannot be assembled: the younger waits, and
        its hold names the script alone -- even checked alone (strict), never the catalogue both regenerated."""
        ei = self.lane_of("isolated", "i", line=(4, "isolated's line 5"), alone=True)
        self.in_flight(ei)
        ey = self.lane_of("younger", "y", line=(4, "younger's line 5"))
        e = self.entry(ey)
        e["batch_alone"] = "regen"
        A.save_entry(self.d, e)
        A.tick_changes(self.r.path, self.d, *on_clock(A.Clock()))
        self.assertNotIn(ey, self.started, "never checked on a tree it cannot land on")
        self.assertEqual((self.hold_of(ey).get("blocker"), self.hold_of(ey).get("files")), (ei, [SOURCE]))

    def test_a_source_both_edit_apart_still_waits_for_the_check_in_flight(self):
        """A merge-clean overlap on a SOURCE: checked now, the younger's push would owe THE RULE's full recheck once
        the older lands (simulator invariant I9) -- a check thrown away. It waits, naming the source alone."""
        ei = self.lane_of("isolated", "i", line=(0, "isolated's line 1"), alone=True)
        self.in_flight(ei)
        ey = self.lane_of("younger", "y", line=(9, "younger's line 10"))
        A.tick_changes(self.r.path, self.d, *on_clock(A.Clock()))
        self.assertNotIn(ey, self.started)
        self.assertEqual((self.hold_of(ey).get("blocker"), self.hold_of(ey).get("files")), (ei, [SOURCE]))


for _name in list(test_derived.DerivedTest.__dict__) + list(test_generated.GeneratedQueueTest.__dict__) \
        + dir(test_backlog_batch.BacklogBatchTest):
    if _name.startswith("test_") and _name not in ConflictHoldTest.__dict__:
        setattr(ConflictHoldTest, _name, None)

if __name__ == "__main__":
    unittest.main()
