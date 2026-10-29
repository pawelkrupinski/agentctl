"""The convergence pins (2026-10-29: the reconciler logged "pinned recording: not re-read (HTTP Error 403: Forbidden)"
every tick -- ci-lander is push-only and may not read releases). The laptop reads them with the owner's token and
writes them into the store; the reconciler copies the store's record and never reads release assets; the last stored
pins stand while the laptop is offline, their age in `status`; local-store mode reads and stores them itself."""
import json, os, sys, time, unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from test_agentctl import A  # noqa: E402
from test_phase2 import Phase2Base  # noqa: E402

MOVIES = "pawelkrupinski/movies"
T = 1_000_000.0  # a clock past PIN_REFRESH from 0 (nothing read yet)


class PinsBase(Phase2Base):
    def setUp(self):
        super().setUp()
        for k in ("AGENTCTL_PINNED_RECORDING", "AGENTCTL_STORE_RETRIES", "AGENTCTL_REDS_DIR"):
            self.addCleanup(os.environ.__setitem__, k, os.environ[k]) if k in os.environ else \
                self.addCleanup(os.environ.pop, k, None)
            os.environ.pop(k, None)
        for name in ("read_pins", "pin_release_tag"):
            self.addCleanup(setattr, A, name, getattr(A, name))
        self.addCleanup(A._pin_tried.__setitem__, 0, 0.0)
        A._pin_tried[0] = 0.0
        os.environ["AGENTCTL_REDS_DIR"] = os.path.join(self.tmp, "state", "reds")  # pinned-recording.json beside it
        self.reads = []
        self.pins = {"pl": "528", "de": "528"}
        A.pin_release_tag = lambda repo: "convergence-fixtures"
        A.read_pins = lambda o_r, tag: self.reads.append((o_r, tag)) or dict(self.pins)

    def laptop(self, url=None):
        """This process as the laptop in remote-store mode (the sync), its own state root."""
        os.environ["AGENTCTL_STATE_ROOT"] = os.path.join(self.tmp, "laptop")
        os.environ["AGENTCTL_REDS_DIR"] = os.path.join(self.tmp, "laptop", "reds")
        os.environ.pop("AGENTCTL_ROLE", None)
        os.environ.update(AGENTCTL_STORE_URL=url or self.url, AGENTCTL_STORE_TOKEN="tok-pawel")

    def server(self):
        """This process as the reconciler on ci-runner: the store's own database, no release reads."""
        os.environ["AGENTCTL_STATE_ROOT"] = os.path.join(self.tmp, "state")
        os.environ["AGENTCTL_REDS_DIR"] = os.path.join(self.tmp, "state", "reds")
        os.environ.pop("AGENTCTL_STORE_URL", None)
        os.environ["AGENTCTL_ROLE"] = "server"

        def forbidden(o_r, tag):
            raise AssertionError("the reconciler read release assets")
        A.read_pins = forbidden

    def stored(self):
        v = self.st.meta_get(A.PIN_META)
        return json.loads(v) if v else None


class SyncWritesTheStoreTest(PinsBase):
    def test_the_sync_reads_the_pins_with_its_token_and_writes_them_into_the_store(self):
        self.laptop()
        self.assertEqual(A.host_role(), "sync")
        rec = A.refresh_pinned_recording(MOVIES, self.repo, now_=T)
        self.assertEqual(self.reads, [(MOVIES, "convergence-fixtures")])
        self.assertTrue(rec["stored"])
        self.assertEqual(self.stored(), {"at": T, "repo": MOVIES, "tag": "convergence-fixtures",
                                         "pins": {"pl": "528", "de": "528"}})
        self.assertEqual(A.pinned_recording(), "528")
        A.refresh_pinned_recording(MOVIES, self.repo, now_=T + A.PIN_REFRESH - 1)
        self.assertEqual(len(self.reads), 1)  # hourly
        self.pins = {"pl": "528", "de": "600"}
        A.refresh_pinned_recording(MOVIES, self.repo, now_=T + A.PIN_REFRESH + 1)
        self.assertEqual(self.stored()["pins"], {"pl": "528", "de": "600"})

    def test_pins_read_while_the_store_is_down_are_stored_at_the_next_call_without_a_new_read(self):
        os.environ["AGENTCTL_STORE_RETRIES"] = "0"
        self.laptop(url="http://127.0.0.1:9")  # nothing listens there
        rec = A.refresh_pinned_recording(MOVIES, self.repo, now_=T)
        self.assertFalse(rec.get("stored"))
        self.assertIsNone(self.stored())
        self.assertEqual(A.pinned_recording(), "528")  # the laptop's own view has them meanwhile
        self.laptop()
        rec = A.refresh_pinned_recording(MOVIES, self.repo, now_=T + 10)
        self.assertTrue(rec["stored"])
        self.assertEqual(self.stored()["at"], T)
        self.assertEqual(len(self.reads), 1)

    def test_a_failed_read_is_retried_after_pin_retry_not_every_tick(self):
        self.laptop()

        def refused(o_r, tag):
            self.reads.append(tag)
            raise RuntimeError("HTTP Error 403: Forbidden")
        A.read_pins = refused
        for t in (T, T + 5, T + A.PIN_RETRY - 1):
            A.refresh_pinned_recording(MOVIES, self.repo, now_=t)
        self.assertEqual(len(self.reads), 1)
        A.refresh_pinned_recording(MOVIES, self.repo, now_=T + A.PIN_RETRY)
        self.assertEqual(len(self.reads), 2)

    def test_the_sync_loop_has_a_bounded_pins_step(self):
        self.laptop()
        step = dict(A.sync_steps(self.repo, self.tmp, MOVIES))["pins"]
        step()
        A._bounded["pins"].join(10)
        self.assertEqual(self.stored()["pins"], {"pl": "528", "de": "528"})


class ReconcilerReadsTheStoreTest(PinsBase):
    def test_the_reconciler_copies_the_stored_pins_and_never_reads_release_assets(self):
        self.server()
        self.assertEqual(A.refresh_pinned_recording(MOVIES, self.repo, force=True), {})  # nothing stored yet: silent
        self.assertEqual(A.pinned_runs(), [])
        self.st.meta_set(A.PIN_META, json.dumps({"at": T, "repo": MOVIES, "tag": "convergence-fixtures",
                                                 "pins": {"pl": "528", "de": "600"}}))
        rec = A.refresh_pinned_recording(MOVIES, self.repo)
        self.assertEqual(rec["pins"], {"pl": "528", "de": "600"})
        self.assertEqual(A.pinned_recording(), "528/600")
        # the laptop offline for two days: the last stored pins stand, and `status` says how old they are
        self.assertEqual(A.refresh_pinned_recording(MOVIES, self.repo, now_=T + 2 * 86400)["pins"],
                         {"pl": "528", "de": "600"})
        line = A.pins_status(now_=T + 2 * 86400)
        self.assertIn("pins: 528/600 (convergence-fixtures), read 2 d ago", line)
        self.assertIn("STALE", line)

    def test_end_to_end_the_laptop_stores_and_the_reconciler_reads(self):
        self.laptop()
        A.refresh_pinned_recording(MOVIES, self.repo, now_=time.time())
        self.server()
        self.assertEqual(A.refresh_pinned_recording(MOVIES, self.repo)["pins"], {"pl": "528", "de": "528"})
        self.assertEqual(A.pinned_recording(), "528")
        self.assertNotIn("STALE", A.pins_status())


class PinsStatusTest(PinsBase):
    def test_status_shows_the_pins_and_their_age_stale_past_a_day(self):
        self.assertIn("pins: unread", A.pins_status({}))
        rec = {"at": T, "tag": "convergence-fixtures", "pins": {"pl": "573", "de": "573"}}
        self.assertEqual(A.pins_status(rec, now_=T + 3 * 3600), "pins: 573 (convergence-fixtures), read 3 h ago")
        self.assertNotIn("STALE", A.pins_status(rec, now_=T + A.PIN_STALE))
        self.assertIn("STALE", A.pins_status(rec, now_=T + A.PIN_STALE + 1))


if __name__ == "__main__":
    unittest.main()
