"""Every move says why (causes.py): the events' actor / cause / detail columns, the store refusing a move between two
states without a cause, every call site naming one, and THE reject classifier."""
import ast, glob, os, sqlite3, sys, tempfile, unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from test_agentctl import A  # noqa: E402

LIB = os.path.join(os.path.dirname(HERE), "lib", "agentctl")


class StoreCauseTest(unittest.TestCase):
    def setUp(self):
        self.path = os.path.join(tempfile.mkdtemp(), "store.db")
        self.st = A.Store(path=self.path, clock=lambda: 1000.0)
        self.it = self.st.put_item("change", "c1", "queued", {})

    def rows(self):
        return [e for e in self.st.item_events("change", "c1")]

    def test_a_move_without_a_cause_is_refused_and_writes_nothing(self):
        with self.assertRaises(A.MissingCause):
            self.st.transition("change", "c1", "checking", self.it["version"], event={"text": "checking"})
        self.assertEqual(self.st.item("change", "c1")["state"], "queued")
        self.assertEqual([e["kind"] for e in self.rows()], ["change.queued"])

    def test_a_cause_outside_the_enum_is_refused(self):
        with self.assertRaises(A.MissingCause):
            self.st.transition("change", "c1", "checking", self.it["version"], event={"cause": "because"})

    def test_a_move_writes_actor_cause_and_detail(self):
        self.st.transition("change", "c1", "checking", self.it["version"],
                           event={"text": "x", "cause": "check-running", "actor": "reconciler", "detail": "tree 2"})
        ev = self.rows()[-1]
        self.assertEqual((ev["actor"], ev["cause"], ev["detail"]), ("reconciler", "check-running", "tree 2"))

    def test_a_self_move_needs_no_cause(self):
        self.st.transition("change", "c1", "queued", self.it["version"], event={"text": "still held"})
        self.assertIsNone(self.rows()[-1]["cause"])

    def test_an_item_opening_is_an_event_from_no_state(self):
        ev = self.rows()[0]
        self.assertEqual((ev["kind"], ev["from_state"], ev["to_state"], ev["cause"]),
                         ("change.queued", None, "queued", "submitted"))
        self.assertEqual(ev["severity"], "info")
        self.assertEqual(ev["audience"], {})  # no inbox entry
        self.st.put_item("change", "c1", "queued", {})  # an existing item: no second opening
        self.assertEqual(len(self.rows()), 1)

    def test_an_old_store_migrates_in_place_its_rows_kept_null(self):
        old = os.path.join(tempfile.mkdtemp(), "store.db")
        c = sqlite3.connect(old)
        c.execute("CREATE TABLE events (seq INTEGER PRIMARY KEY AUTOINCREMENT, at REAL NOT NULL, kind TEXT NOT NULL, "
                  "item_type TEXT, item_id TEXT, from_state TEXT, to_state TEXT, severity TEXT NOT NULL DEFAULT 'info', "
                  "audience TEXT, text TEXT, refs TEXT, dedupe TEXT UNIQUE, notified_at REAL)")
        c.execute("INSERT INTO events (at, kind, text, dedupe) VALUES (1, 'change.checking', 'checking', 'd1')")
        c.commit()
        c.close()
        st = A.Store(path=old, clock=lambda: 2.0)
        ev = st.events()[0]
        self.assertEqual((ev["text"], ev["actor"], ev["cause"], ev["detail"]), ("checking", None, None, None))

    def test_the_process_actor_is_stamped_on_moves_naming_none(self):
        st = A.CurrentStore(lambda clock=None: self.st, actor="session:s1")
        st.transition("change", "c1", "checking", self.it["version"], event={"cause": "check-running"})
        self.assertEqual(self.rows()[-1]["actor"], "session:s1")

    def test_a_legacy_client_s_move_is_written_unattributed(self):
        args, kw = A.legacy_cause("transition", ["change", "c1", "checking", 1], {"event": {"text": "x"}})
        self.assertEqual(kw["event"]["cause"], "unattributed")
        args, kw = A.legacy_cause("transition", ["change", "c1", "checking", 1, None, {"cause": "batch"}], {})
        self.assertEqual(args[5]["cause"], "batch")


class ProcessActorTest(unittest.TestCase):
    def test_actors(self):
        self.assertEqual(A.process_actor(["tick"], {}), "reconciler")
        self.assertEqual(A.process_actor(["land"], {"AGENTCTL_AGENT_NAME": "red-fixer-9"}), "agent:red-fixer-9")
        self.assertEqual(A.process_actor(["land"], {"CLAUDE_CODE_SESSION_ID": "abc"}), "session:abc")
        self.assertEqual(A.process_actor(["red"], {}, interactive=True), "owner")
        self.assertEqual(A.process_actor(["red"], {}, interactive=False), "cli:red")


class RejectCauseTest(unittest.TestCase):
    """THE reject classifier (2026-11-26: 144 of 495 rejects named no step; infra vs test could not be split)."""

    def test_classes(self):
        cases = [
            ("NOT LANDED: the change no longer applies on origin/main 1234 (main moved)", [], "no-longer-applies", "policy"),
            ("NOT LANDED: its Fixes-Red: names no open red", [], "policy", "policy"),
            ("NOT LANDED: the pre-push checks failed (rest); failing with this change only: sbt",
             ["[error] Failed: Total 3, Failed 1"], "check-failed-test", "test"),
            ("NOT LANDED: the pre-push checks failed (rest); failing with this change only: sbt",
             ["fatal: unable to access 'https://github.com/x': Could not resolve host: github.com"],
             "check-failed-infra", "infra"),
            ("NOT LANDED: the pre-push checks failed ); ", [], "check-failed-test", "test"),
        ]
        for verdict, tail, cause, cls in cases:
            with self.subTest(verdict=verdict):
                self.assertEqual(A.reject_cause(verdict, tail), cause)
                self.assertEqual(A.reject_class(cause), cls)


def _calls(tree):
    for fn in ast.walk(tree):
        if isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
            for node in ast.walk(fn):
                if isinstance(node, ast.Call):
                    yield fn, node


def _name(call):
    f = call.func
    return f.attr if isinstance(f, ast.Attribute) else f.id if isinstance(f, ast.Name) else None


def _self_move(call):
    """The call's target state is the item's own (`it["state"]`, `cur["state"]`): a reminder, which needs no cause."""
    to = call.args[2] if len(call.args) > 2 else None
    return isinstance(to, ast.Subscript) and isinstance(to.slice, ast.Constant) and to.slice.value == "state"


class EveryCallSiteNamesACauseTest(unittest.TestCase):
    """A MISSING CAUSE IS A PROGRAMMING ERROR: every call that moves an item names one -- set_change by its required
    `cause=`, a direct transition in its event (or its function sets one on the event it passes), queue_verdict for a
    status other than landed / rejected (which it classifies itself)."""

    def test_every_call_site(self):
        missing = []
        for path in sorted(glob.glob(os.path.join(LIB, "*.py"))):
            src = open(path).read()
            for fn, call in _calls(ast.parse(src)):
                name, where = _name(call), f"{os.path.basename(path)}:{call.lineno}"
                kws = {k.arg for k in call.keywords}
                seg = ast.get_source_segment(src, call) or ""
                if name == "set_change" and "cause" not in kws:
                    missing.append(where)
                elif name == "transition" and not _self_move(call) and '"cause"' not in seg \
                        and '"cause"' not in (ast.get_source_segment(src, fn) or "") and fn.name != "transition":
                    missing.append(where)
                elif name == "queue_verdict" and "cause" not in kws and len(call.args) > 3 \
                        and not (isinstance(call.args[3], ast.Constant) and call.args[3].value in ("landed", "rejected")):
                    missing.append(where)
        self.assertEqual(missing, [], "moves written without a cause (causes.CAUSES)")

    def test_the_check_sees_a_missing_cause(self):
        src = "def f(st, it):\n    st.transition('change', it['id'], 'checking', it['version'], event={'text': 'x'})\n"
        call = next(c for _, c in _calls(ast.parse(src)))
        self.assertFalse(_self_move(call))
        self.assertNotIn('"cause"', ast.get_source_segment(src, call))


if __name__ == "__main__":
    unittest.main()
