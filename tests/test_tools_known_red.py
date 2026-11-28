"""A TOOLS-ONLY CHANGE THAT FAILS ONLY AS MAIN'S GATE FAILS LANDS (the owner, 2026-11-26: "let it through").

The history: 13 stays and 102 h on `waiting` for tools-only changes whose checks failed only where main's newest decided
gate run fails (queuecheck.judge_check_failure's known_red). The rule is redland.may_land_on_red's, read by
queuediff.wait_or_push: tools-only commits, and every failing check one main's gate run fails too (`gate_only`); an
extra failure is the change's own (rejected), a failure only another queued change showed waits as before, and a
production change still waits unless it fixes the red. Main's own CI re-tests everything after the landing."""
import os, sys, unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from test_agentctl import A, put_change  # noqa: E402
from harness import case_context  # noqa: E402
from fake_clock import on_clock  # noqa: E402
import test_queue  # noqa: E402

KNOWN = {"keys": ["Unit tests"], "where": ["Main #7 on main"], "base": None, "gate_only": True}


class ToolsKnownRedTest(test_queue.QueueTest):
    def known_red_change(self, path, known):
        self.reds = {"7"}
        d, _c, _w = A.waiting_state(self.r.path)
        wt, _ = self.agent("c", path, "a\n", msg="a change")
        eid, _ = self.queued(wt)
        A.check_entry(self.r.path, eid, *on_clock(self.clock), ctx=case_context())
        e = A.ChangeRecords(A.process_store(), d).get(eid)
        e["known_red"] = dict(known, base=e.get("checked_base"))
        put_change(A.CurrentStore(A.process_store), d, e)
        A.push_change(self.r.path, d, e, *on_clock(A.Clock()), ctx=case_context())
        return A.ChangeRecords(A.process_store(), d).get(eid), d

    def test_a_tools_only_change_failing_only_as_main_s_gate_fails_lands(self):
        e, _d = self.known_red_change("scripts/tool.sh", KNOWN)
        self.assertEqual(e["status"], "landed")

    def test_the_move_names_its_cause(self):
        e, _d = self.known_red_change("scripts/tool.sh", KNOWN)
        causes = [ev["cause"] for ev in A.process_store().item_events("change", e["id"])]
        self.assertIn("tools-only-known-red", causes)

    def test_a_tools_only_change_with_a_failure_main_s_gate_does_not_show_waits(self):
        e, _d = self.known_red_change("scripts/tool.sh", dict(KNOWN, gate_only=False))
        self.assertEqual(e["status"], "waiting")

    def test_a_production_change_failing_only_as_main_fails_still_waits(self):
        e, _d = self.known_red_change("web/src/main/A.scala", KNOWN)
        self.assertEqual(e["status"], "waiting")

    def test_the_rule_reads_the_commits_never_a_flag_alone(self):
        facts = A.gate_facts({"7"})
        entry = {"id": "e", "known_red": KNOWN}
        for prod, lands in ((False, True), (True, False)):
            shas = ["c0"]
            self.addCleanup(setattr, A, "touches_production", A.touches_production)
            self.addCleanup(setattr, A, "commit_fixes", A.commit_fixes)
            A.touches_production = lambda repo, c, known, p=prod: p
            A.commit_fixes = lambda repo, c, store, ctx=None: set()
            v = A.may_land_on_red(facts, dict(entry, commits=shas), "/repo", store=None, ctx=case_context())
            self.assertEqual(v.tools_only_known_red, lands, f"production={prod}")
        v = A.may_land_on_red(A.gate_facts(set()), dict(entry, commits=["c0"]), "/repo", store=None, ctx=case_context())
        self.assertFalse(v.tools_only_known_red, "a green gate: nothing to land past")

    def test_the_judge_marks_a_known_red_gate_only_when_every_key_is_main_s_gate_run(self):
        saved = A.judge_failure
        self.addCleanup(setattr, A, "judge_failure", saved)
        for where, gate_only in (({"T": "Main #7 on main"}, True),
                                 ({"T": "Main #7 on main", "U": "change c-9 on the same main"}, False)):
            A.judge_failure = lambda *a, w=where, **k: ("base", w)
            e = {"id": "x", "head": "h", "worktree": "w"}
            kr = A.judge_check_failure(None, self.r.path, self.tmp.name, e, self.r.path, "b", "", "tree", 0.0, ctx=case_context())[2]
            self.assertEqual(kr["gate_only"], gate_only)


if __name__ == "__main__":
    unittest.main()
