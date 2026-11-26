"""THE STORE OVER A SLOW OR OLDER STORE (2026-11-26 16:54Z: `status` and `land status` hung past 90 s and nothing
landed for 90+ min). The laptop's uplink gave ~40 KB/s; every sync step, `status` and `land status` read
`items("change")` whole -- 4.1 MB, 1990 changes nearly all decided -- and `items("red")`, 1.8 MB; the tunnel's single
ssh stream queued every other request behind them. Meanwhile ci-runner, pinned to an older rev while agentctl's CI was
red, answered the laptop's new `record_events` with 404 every tick, and the client re-sent its 88 KB page each time.
These tests hold: the open reads cross filtered by the store; a call the store lacks is said once as version skew and
not re-sent; `status` / `land status` fail fast naming the store; WU says when the tunnel listens but nothing answers."""
import contextlib, io, os, socket, sys, time, unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from test_agentctl import A  # noqa: E402
from fake_clock import FakeClock  # noqa: E402
from test_phase2 import Phase2Base  # noqa: E402


class RecordingStore:
    """The store, with every items() call's (type, states) recorded."""

    def __init__(self, st):
        self.st, self.calls = st, []

    def items(self, typ, states=None):
        self.calls.append((typ, None if states is None else sorted(states)))
        return self.st.items(typ, states)

    def __getattr__(self, name):
        return getattr(self.st, name)


class OpenReadsTest(Phase2Base):
    def seed(self):
        self.st.put_item("change", "c-open", "queued", {"worktree": "/w/open", "submitted_at": 1.0})
        for i in range(20):
            self.st.put_item("change", f"c-done-{i}", "landed", {"worktree": f"/w/{i}"})
        self.st.put_item("red", "r-open", "offered", {"key": "k-open", "workflow": "Main"})
        for i in range(10):
            self.st.put_item("red", f"r-done-{i}", "cleared", {"key": f"k-{i}", "workflow": "Main"})

    def assert_open_only(self, rec, typ):
        asked = [s for t, s in rec.calls if t == typ]
        self.assertTrue(asked, f"no {typ} read")
        terminal = {n for n, spec in A.MACHINES[typ].items() if spec.terminal}
        for states in asked:
            self.assertIsNotNone(states, f"a whole read of every {typ}, decided ones included")
            self.assertFalse(terminal & set(states), states)

    def test_the_queue_reads_ask_the_store_for_open_changes_only(self):
        self.seed()
        rec = RecordingStore(A.CurrentStore(lambda clock=None: self.remote))
        self.assertEqual([r["id"] for r in A.change_rows(rec)], ["c-open"])
        self.assert_open_only(rec, "change")
        rec.calls.clear()
        self.assertGreater(A.oldest_change_age(rec, 10.0), 0)
        self.assert_open_only(rec, "change")
        rec.calls.clear()
        A.clear_holds_on(rec, "c-gone")
        self.assert_open_only(rec, "change")

    def test_the_red_reads_ask_the_store_for_open_reds_only(self):
        self.seed()
        rec = RecordingStore(A.CurrentStore(lambda clock=None: self.remote))
        self.assertEqual(sorted(A.red_active(rec)), ["k-open"])
        self.assert_open_only(rec, "red")


class LacksCallTest(Phase2Base):
    def test_an_older_stores_404_is_a_call_it_lacks_not_a_missing_item(self):
        self.assertTrue(A.lacks_call(404, "/v1/store/record_events", {"error": "NotFound", "message": "'record_events'"}))
        self.assertTrue(A.lacks_call(404, "/v1/store/record_events", {"error": "UnknownCall", "message": "record_events"}))
        self.assertFalse(A.lacks_call(404, "/v1/store/item", {"error": "NotFound", "message": "'c-1'"}))
        self.assertFalse(A.lacks_call(404, "/v1/changes/x:cancel", {"error": "NotFound", "message": "'x:cancel'"}))
        self.assertFalse(A.lacks_call(500, "/v1/store/record_events", {"message": "'record_events'"}))

    def older_store(self):
        """This store as one from before record_events: its STORE_RPC without it, its requests counted."""
        saved = A.STORE_RPC
        A.STORE_RPC = tuple(m for m in saved if m != "record_events")
        self.addCleanup(setattr, A, "STORE_RPC", saved)
        seen, handle = [], self.api.handle
        self.api.handle = lambda method, path, *a, **k: (seen.append(path), handle(method, path, *a, **k))[1]
        return seen

    def test_a_call_the_store_lacks_says_version_skew_and_is_not_sent_again_soon(self):
        seen = self.older_store()
        with self.assertRaises(A.StoreLacksCall) as cm:
            self.remote.record_events([])
        self.assertIn("version skew", str(cm.exception))
        self.assertIn(self.url, str(cm.exception))
        self.assertIsInstance(cm.exception, A.StoreUnavailable, "every caller of an unavailable store handles it")
        with self.assertRaises(A.StoreLacksCall):
            A.RemoteStore(self.url, "tok-pawel").record_events([])  # another client of the same process: remembered
        self.assertEqual(seen.count("/v1/store/record_events"), 1, "the refused page is not re-sent every tick")
        self.assertEqual(self.remote.get("role/laptop"), None, "the store's other calls go on")

    def test_the_offline_log_keeps_its_lines_and_says_the_skew_once(self):
        self.sync()
        self.older_store()
        A.note_event("gov-ticket", "t-1", "waiting", "admitted", "governor")
        A.note_event("gov-ticket", "t-1", "admitted", "released", "work-ended")
        out = io.StringIO()
        with contextlib.redirect_stderr(out):
            self.assertEqual(A.drain_event_log(self.remote, refused=A.file_events_refused), 0)
            self.assertEqual(A.drain_event_log(self.remote, refused=A.file_events_refused), 0)
        said = [ln for ln in out.getvalue().splitlines() if "version skew" in ln]
        self.assertEqual(len(said), 1, out.getvalue())
        self.assertEqual(len(A._pending(0)), 2, "both lines kept for a store that has the call")


class FailFastTest(Phase2Base):
    def setUp(self):
        super().setUp()
        self.mute = socket.socket()  # a tunnel that accepts and never answers (the 2026-11-26 forward)
        self.mute.bind(("127.0.0.1", 0))
        self.mute.listen(64)
        self.addCleanup(self.mute.close)
        self.sync()
        os.environ["AGENTCTL_STORE_URL"] = f"http://127.0.0.1:{self.mute.getsockname()[1]}"
        saved = A.STORE_WAITED_TIMEOUT
        A.STORE_WAITED_TIMEOUT = 1
        self.addCleanup(setattr, A, "STORE_WAITED_TIMEOUT", saved)

    def run_main(self, argv):
        out, errs = io.StringIO(), io.StringIO()
        t0 = time.monotonic()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(errs):
            rc = A.main(argv)
        return rc, time.monotonic() - t0, out.getvalue(), errs.getvalue()

    def test_land_status_fails_fast_naming_the_store(self):
        rc, took, _out, errs = self.run_main(["land", "status"])
        self.assertEqual(rc, 3)
        self.assertLess(took, 10, "one bounded call, never retried")
        self.assertIn("STORE UNREACHABLE", errs)
        self.assertIn(os.environ["AGENTCTL_STORE_URL"], errs)

    def test_status_says_the_store_is_unreachable_and_shows_the_snapshot(self):
        rc, took, out, errs = self.run_main(["status"])
        self.assertEqual(rc, 0)
        self.assertLess(took, 10)
        self.assertIn("STORE UNREACHABLE", errs)
        self.assertTrue(out.strip(), "the status from the snapshot all the same")

    def test_only_the_waited_on_commands_are_bounded(self):
        self.assertTrue(A.waited_on(["status"]))
        self.assertTrue(A.waited_on(["land", "status", "x"]))
        self.assertFalse(A.waited_on(["land"]))
        self.assertFalse(A.waited_on(["waiting", "run", "--service"]))


class TunnelUnresponsiveTest(Phase2Base):
    def test_wu_fires_when_the_tunnel_listens_and_the_store_has_not_answered(self):
        self.sync()
        clock = FakeClock(10_000.0)
        A.state_update(store_error={"since": 10_000.0 - 600, "at": 10_000.0, "why": "GET /v1/state: timed out"})
        ok, why = A.inv_wu(clock, None, listening=lambda port: True)
        self.assertIs(ok, False)
        self.assertIn("accepts connections", why)
        self.assertIn("10 min", why)
        A.state_update(store_error={"since": 10_000.0 - 60, "at": 10_000.0, "why": "x"})
        self.assertIs(A.inv_wu(clock, None, listening=lambda port: True)[0], True, "a brief failure is not WU's")
        A.state_update(store_error=None)
        self.assertIs(A.inv_wu(clock, None, listening=lambda port: True)[0], True)
        A.state_update(store_error={"since": 1.0, "at": 10_000.0, "why": "x"})
        self.assertIsNone(A.inv_wu(clock, None, listening=lambda port: False)[0], "a closed port: the tunnel watch's")
        self.assertIn("WU", A.INVARIANTS)


if __name__ == "__main__":
    unittest.main()
