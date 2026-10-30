"""Events -> the inbox (design §8.2, reconciler phase 1 step 7): the projector writes one inbox entry per event with an
audience or a non-info severity (never twice), the notifier sends one notification per escalation, and the change
path writes the inbox only through its events."""
import ast, os, stat, sys, tempfile, time, unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from test_agentctl import A, TOOL, code_source  # noqa: E402


class EventsTest(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp()
        self.saved = {k: os.environ.get(k) for k in ("AGENTCTL_INBOX_DIR", "AGENTCTL_NOTIFY")}
        os.environ["AGENTCTL_INBOX_DIR"] = os.path.join(self.root, "inbox")
        self.notes = os.path.join(self.root, "notes")
        hook = os.path.join(self.root, "notify.sh")
        with open(hook, "w") as f:
            f.write(f'#!/bin/sh\necho "$1|$2" >> {self.notes}\n')
        os.chmod(hook, os.stat(hook).st_mode | stat.S_IEXEC)
        os.environ["AGENTCTL_NOTIFY"] = hook
        self.st = A.Store(path=os.path.join(self.root, "store.db"))

    def tearDown(self):
        for k, v in self.saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def test_the_projector_writes_each_addressed_event_once_in_todays_format(self):
        e = {"id": "e1", "worktree": "/w/a", "session": "s1"}
        self.st.put_item("change", "e1", "checking")
        self.st.transition("change", "e1", "checked", 1)  # no audience, info: nothing for the inbox
        self.st.transition("change", "e1", "pushing", 2)
        self.st.transition("change", "e1", "landed", 3, event=A.change_event(e, "LANDED abc1234 (queued e1)", code=0))
        self.assertEqual(A.project_events(self.st), 1)
        self.assertEqual(A.project_events(self.st), 0)  # a repeated pass writes nothing twice
        [entry] = A.inbox_entries()
        self.assertEqual((entry["kind"], entry["cwd"], entry["verdict"], entry["exit"]),
                         ("LANDED", "/w/a", "LANDED abc1234 (queued e1)", 0))

    def test_the_notifier_notifies_each_escalation_once(self):
        self.st.put_item("change", "e2", "queued")
        self.st.transition("change", "e2", "queued", 1)  # queued past its deadline: an escalation
        self.assertEqual(A.notify_escalations(self.st), 1)
        self.assertEqual(A.notify_escalations(self.st), 0)
        self.assertEqual(len(open(self.notes).read().splitlines()), 1)
        self.assertTrue(self.st.events()[-1]["notified_at"])

    def test_an_escalation_the_cursor_passed_unmarked_is_still_delivered_and_a_closed_ones_resolved(self):
        """W6, 2026-10-30: RED UNCLAIMED #2560 sat 'not notified' for good -- the notifier read only past its cursor."""
        self.st.put_item("change", "e3", "queued")
        self.st.transition("change", "e3", "queued", 1)  # an escalation, its item still open
        self.st.put_item("change", "e4", "queued")
        self.st.transition("change", "e4", "queued", 1)  # an escalation whose item then closes
        self.st.transition("change", "e4", "cancelled", 2)
        self.st.meta_set("notifier_seq", self.st.events()[-1]["seq"])  # the cursor past both, neither marked
        self.assertEqual(A.notify_escalations(self.st), 1)
        self.assertEqual(len(open(self.notes).read().splitlines()), 1)
        self.assertTrue(all(e["notified_at"] for e in self.st.events() if e["severity"] == "escalate"))
        os.environ["AGENTCTL_W6_MAX"] = "0"
        self.addCleanup(os.environ.pop, "AGENTCTL_W6_MAX", None)
        saved = A.Store
        A.Store = lambda *a, **k: self.st
        try:
            self.assertTrue(A.inv_w6()[0])
        finally:
            A.Store = saved

    def test_a_change_never_leaves_the_queue_without_its_authors_inbox_entry(self):
        """2026-10-30 `…-76336e`: its NOT LANDED entry did not carry the item id (a search by id found nothing), and
        terminal moves written without an audience -- a cancel, a landed-by-content close, a stranded supersede -- reached
        nobody. Every terminal transition now carries an event addressed to the item's worktree, naming the item."""
        for i, to in enumerate(("landed", "rejected", "superseded", "cancelled")):
            self.st.put_item("change", f"c{i}", "queued", {"worktree": "/w/flips", "head": "83af2cb4"})
            self.st.transition("change", f"c{i}", "checking" if to == "rejected" else to, 1)
            if to == "rejected":
                self.st.transition("change", f"c{i}", "rejected", 2,
                                   event={"text": "NOT LANDED: the pre-push checks failed"})  # no audience given
        self.st.put_item("change", "nowt", "queued")  # no worktree known: still an entry (the owner's)
        self.st.transition("change", "nowt", "cancelled", 1)
        self.assertEqual(A.change_exits_unannounced(self.st), [])
        self.assertEqual(A.project_events(self.st), 5)
        by = {e["entry"]: e for e in A.inbox_entries()}
        self.assertEqual(set(by), {"c0", "c1", "c2", "c3", "nowt"})
        self.assertEqual(by["c1"]["cwd"], "/w/flips")
        self.assertEqual((by["c1"]["kind"], by["c1"]["exit"]), ("NOT_LANDED", 1))
        self.assertEqual((by["c0"]["kind"], by["c0"]["exit"]), ("LANDED", 0))
        self.assertTrue(by["c2"]["verdict"].startswith("SUPERSEDED c2"))
        self.assertTrue(by["c3"]["verdict"].startswith("CANCELLED c3"))
        # `agentctl inbox <item id>` finds the entry by the item id
        out = self.run_inbox(["c1"])
        self.assertIn("NOT LANDED", out)
        self.assertNotIn("CANCELLED", out)

    def run_inbox(self, ids):
        import argparse, contextlib, io
        a = argparse.Namespace(ids=ids, unread=False, ack=False, all=False, limit=50, json=False, verbose=False,
                               kinds=None, since_ack=False, timeout=0)
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            A.cmd_inbox(a)
        return buf.getvalue()

    def test_w8_names_a_change_that_left_the_queue_silently(self):
        self.st.put_item("change", "s1", "queued", {"worktree": "/w/a"})
        self.st.transition("change", "s1", "cancelled", 1)
        c = self.st.db()
        c.execute("UPDATE events SET audience='{}', severity='info' WHERE item_id='s1'")  # a writer around transition
        c.close()
        self.assertEqual(A.change_exits_unannounced(self.st), ["s1"])
        saved = A.Store
        A.Store = lambda *a, **k: self.st
        try:
            ok, why = A.inv_w8()
            self.assertFalse(ok)
            self.assertIn("s1", why)
            os.environ["AGENTCTL_W8_WINDOW"] = "-60"  # it ages out of the window: the condition clears itself
            self.addCleanup(os.environ.pop, "AGENTCTL_W8_WINDOW", None)
            self.assertTrue(A.inv_w8()[0])
        finally:
            A.Store = saved

    def test_the_projector_keeps_its_cursor_before_an_entry_it_could_not_write(self):
        e = {"id": "e5", "worktree": "/w/a", "session": "s1"}
        self.st.put_item("change", "e5", "pushing")
        self.st.transition("change", "e5", "landed", 1, event=A.change_event(e, "LANDED abc (queued e5)", code=0))
        saved = A.write_json
        A.write_json = lambda *a, **k: (_ for _ in ()).throw(OSError("disk full"))
        try:
            self.assertEqual(A.project_events(self.st), 0)
        finally:
            A.write_json = saved
        self.assertEqual(A.project_events(self.st), 1, "written at the next cycle, never lost")

    def test_the_change_path_writes_the_inbox_only_through_events(self):
        tree = ast.parse(code_source())
        fns = {n.name: n for n in tree.body if isinstance(n, ast.FunctionDef)}
        for name in ("queue_verdict", "queue_join", "supersede", "set_change", "requeue_infra", "push_change",
                     "check_entry", "_check_entry", "tick_changes"):
            calls = [c for c in ast.walk(fns[name]) if isinstance(c, ast.Call) and getattr(c.func, "id", "") == "inbox_write"]
            self.assertEqual(calls, [], f"{name} writes the inbox itself")


if __name__ == "__main__":
    unittest.main()
