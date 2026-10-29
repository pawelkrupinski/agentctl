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

    def test_the_change_path_writes_the_inbox_only_through_events(self):
        tree = ast.parse(code_source())
        fns = {n.name: n for n in tree.body if isinstance(n, ast.FunctionDef)}
        for name in ("queue_verdict", "queue_join", "supersede", "set_change", "requeue_infra", "push_change",
                     "check_entry", "_check_entry", "tick_changes"):
            calls = [c for c in ast.walk(fns[name]) if isinstance(c, ast.Call) and getattr(c.func, "id", "") == "inbox_write"]
            self.assertEqual(calls, [], f"{name} writes the inbox itself")


if __name__ == "__main__":
    unittest.main()
