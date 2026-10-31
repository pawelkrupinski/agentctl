"""Tunnel health: a hung endpoint counts like a refusal -- one kickstart + retry for reads, the processor probe
(2 failures -> kick, 1/min, inbox entry after 2 min unrestored)."""
import os, socket, sys, tempfile, threading, time, unittest, urllib.error

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from test_agentctl import A  # noqa: E402


class Hang:
    """A listener that accepts and never answers (the incident: alive tunnel, hung connections)."""
    def __init__(self):
        self.s = socket.socket()
        self.s.bind(("127.0.0.1", 0))
        self.s.listen(8)
        self.conns = []
        self.thread = threading.Thread(target=self.loop, daemon=True)
        self.thread.start()
        self.url = f"http://127.0.0.1:{self.s.getsockname()[1]}/api/v1"

    def loop(self):
        while True:
            try:
                self.conns.append(self.s.accept()[0])
            except OSError:
                return

    def close(self):
        # LINUX: close() alone does not stop a listener another thread is blocked in accept() on -- that accept holds
        # the socket, which stays bound and listening, so the port keeps taking connections ('timeout', never
        # 'refused'). shutdown() wakes the accept (EINVAL) and ends the listen; macOS answers it with ENOTCONN, and its
        # close() alone already releases the port.
        try:
            self.s.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        self.s.close()
        self.thread.join(5)
        for c in self.conns:
            c.close()


class TunnelTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.saved = (A.API, A.TUNNEL_PROBE_TIMEOUT, A.launchctl_kick, A.inbox_write, A.TUNNEL_KICK_MIN_GAP,
                      os.environ.get("AGENTCTL_TUNNEL_STATE"))
        saved_has = A.has_tunnel
        A.has_tunnel = lambda: True  # the laptop's: its tunnel is kicked (the store stays this process's own)
        self.addCleanup(setattr, A, "has_tunnel", saved_has)
        os.environ["AGENTCTL_TUNNEL_STATE"] = os.path.join(self.tmp.name, "t.json")
        self.saved_rec = os.environ.get("AGENTCTL_RECOVERY_DIR")  # its own registry: no other test's tunnel condition
        os.environ["AGENTCTL_RECOVERY_DIR"] = os.path.join(self.tmp.name, "rec")
        self.saved_root = os.environ.get("AGENTCTL_STATE_ROOT")  # its own store too: the conditions are items
        os.environ["AGENTCTL_STATE_ROOT"] = os.path.join(self.tmp.name, "state")
        self.addCleanup(lambda: os.environ.__setitem__("AGENTCTL_STATE_ROOT", self.saved_root) if self.saved_root
                        else os.environ.pop("AGENTCTL_STATE_ROOT", None))
        self.addCleanup(lambda: os.environ.__setitem__("AGENTCTL_RECOVERY_DIR", self.saved_rec) if self.saved_rec
                        else os.environ.pop("AGENTCTL_RECOVERY_DIR", None))
        self.kicks, self.inbox = [], []
        A.launchctl_kick = lambda: self.kicks.append(1)
        A.inbox_write = lambda *a, **k: self.inbox.append(a[1])
        A.TUNNEL_PROBE_TIMEOUT = 0.3
        self.hang = Hang()
        A.API = self.hang.url

    def tearDown(self):
        self.hang.close()
        A.API, A.TUNNEL_PROBE_TIMEOUT, A.launchctl_kick, A.inbox_write, A.TUNNEL_KICK_MIN_GAP, st = self.saved
        os.environ.pop("AGENTCTL_TUNNEL_STATE", None) if st is None else os.environ.__setitem__("AGENTCTL_TUNNEL_STATE", st)
        self.tmp.cleanup()

    def test_probe_timeout_and_refused(self):
        self.assertEqual(A.tunnel_probe(), "timeout")
        self.hang.close()
        self.assertEqual(A.tunnel_probe(), "refused")

    def test_on_github_the_probe_asks_the_store_not_forgejo(self):
        # Forgejo was switched off on 2026-10-31: the tunnel carries only the store, so a dead :3000 must not read as
        # a dead tunnel (it would kick and escalate for ever).
        saved = {k: os.environ.get(k) for k in ("AGENTCTL_FORGE", "AGENTCTL_STORE_URL")}
        saved_role = A.host_role
        A.host_role = lambda: "sync"  # the laptop's: the store is across the tunnel
        self.addCleanup(setattr, A, "host_role", saved_role)
        try:
            os.environ["AGENTCTL_FORGE"] = "github"
            os.environ["AGENTCTL_STORE_URL"] = self.hang.url.rsplit("/api/v1", 1)[0]
            A.API = "http://127.0.0.1:1/api/v1"  # Forgejo's address: refused, and not asked
            self.assertEqual(A.tunnel_ports(), [(3014, "agentctl store")])
            self.assertEqual(A.tunnel_probe(), "timeout")
            self.hang.close()
            self.assertEqual(A.tunnel_probe(), "refused")
        finally:
            for k, v in saved.items():
                os.environ.pop(k, None) if v is None else os.environ.__setitem__(k, v)

    def test_timeout_classified_as_down(self):
        self.assertTrue(A.tunnel_down(socket.timeout()))
        self.assertTrue(A.tunnel_down(urllib.error.URLError(socket.timeout("timed out"))))
        self.assertTrue(A.tunnel_down(urllib.error.URLError(ConnectionRefusedError())))
        self.assertTrue(A.tunnel_down(RuntimeError("forgejo-gh: dial tcp: i/o timeout")))
        self.assertFalse(A.tunnel_down(urllib.error.HTTPError("u", 404, "nf", {}, None)))
        self.assertFalse(A.tunnel_down(RuntimeError("HTTP 404 not found")))

    def test_read_on_hung_endpoint_kicks_once_and_retries(self):
        calls = []

        def fn():
            calls.append(1)
            if len(calls) == 1:
                raise socket.timeout("timed out")
            return "ok"
        A.tunnel_probe, saved = (lambda: "ok"), A.tunnel_probe
        try:
            self.assertEqual(A.tunnel_retry(fn), "ok")
        finally:
            A.tunnel_probe = saved
        self.assertEqual((len(calls), len(self.kicks)), (2, 1))

    def test_read_gives_up_after_one_retry(self):
        calls = []

        def fn():
            calls.append(1)
            raise socket.timeout("timed out")
        A.tunnel_probe, saved = (lambda: "ok"), A.tunnel_probe
        try:
            with self.assertRaises(socket.timeout):
                A.tunnel_retry(fn)
        finally:
            A.tunnel_probe = saved
        self.assertEqual((len(calls), len(self.kicks)), (2, 1))

    def test_non_tunnel_error_not_retried(self):
        calls = []

        def fn():
            calls.append(1)
            raise ValueError("x")
        with self.assertRaises(ValueError):
            A.tunnel_retry(fn)
        self.assertEqual((len(calls), self.kicks), (1, []))

    def test_api_against_hung_endpoint(self):
        A.API_TIMEOUT, saved = 0.3, A.API_TIMEOUT
        A.token, st = (lambda o: "t"), A.token
        A.tunnel_probe, sp = (lambda: "timeout"), A.tunnel_probe
        A.time.sleep, ss = (lambda s: None), A.time.sleep
        try:
            with self.assertRaises(Exception):
                A.api("o/r", "GET", "branches")
        finally:
            A.API_TIMEOUT, A.token, A.tunnel_probe, A.time.sleep = saved, st, sp, ss
        self.assertEqual(len(self.kicks), 1)

    def test_watch_kicks_after_two_rate_limited_then_escalates(self):
        down = lambda: "timeout"
        self.assertEqual(A.tunnel_watch(1000, down), "down")
        self.assertEqual(A.tunnel_watch(1010, down), "kicked")
        self.assertEqual(A.tunnel_watch(1030, down), "down")  # < 60 s since the kick
        self.assertEqual(A.tunnel_watch(1075, down), "kicked")
        self.assertEqual(len(self.kicks), 2)
        self.assertEqual(self.inbox, [])
        self.assertEqual(A.tunnel_watch(1135, down), "escalated")  # 125 s after the first kick
        self.assertEqual(A.tunnel_watch(1140, down), "down")  # only once
        self.assertEqual(len(self.inbox), 1)
        self.assertIn("STALLED", self.inbox[0])

    def test_watch_lossy_uplink_is_a_network_note_not_a_kick(self):
        down, lossy, clean = (lambda: "timeout"), (lambda: 0.5), (lambda: 0.0)
        self.assertEqual(A.tunnel_watch(1000, down, uplink=lossy), "network")
        self.assertEqual(A.tunnel_watch(1010, down, uplink=lossy), "network")
        self.assertEqual(A.tunnel_watch(1500, down, uplink=lossy), "network")  # minutes on: still no STALLED
        self.assertEqual(self.kicks, [])
        self.assertEqual(len(self.inbox), 1)  # one NETWORK note per outage
        self.assertIn("NETWORK", self.inbox[0])
        self.assertEqual(A.tunnel_watch(1510, lambda: "ok"), "restored")
        # the uplink is fine again but the tunnel is not: the kick / escalation path as usual
        self.assertEqual(A.tunnel_watch(1520, down, uplink=clean), "down")
        self.assertEqual(A.tunnel_watch(1530, down, uplink=clean), "kicked")

    def test_a_lossy_uplink_shows_the_gate_as_network_not_unread_and_reads_retry_without_a_kick(self):
        # 2026-10-29: ~20% loss to 1.1.1.1 and the jump host kept the forward timing out; restarts only churned it
        down, lossy, clean = (lambda: "timeout"), (lambda: A.UPLINK_LOSS_MAX), (lambda: 0.0)  # AT the bound: lossy
        A.publish_gate_summary("o/r", "gate green · no reds", epoch=A.time.time() - 3600)
        self.assertIn("gate UNREAD", A.status_line())
        self.assertEqual(A.tunnel_watch(A.time.time(), down, uplink=lossy), "network")
        line = A.status_line()
        self.assertIn("gate NETWORK (lossy uplink: 20% loss", line)
        self.assertNotIn("UNREAD", line)
        calls = []

        def read():
            calls.append(1)
            raise RuntimeError("forgejo-gh run list: timed out")
        A.tunnel_down, saved = (lambda e: True), A.tunnel_down
        try:
            with self.assertRaises(RuntimeError):
                A.tunnel_retry(read)
        finally:
            A.tunnel_down = saved
        self.assertEqual((len(calls), self.kicks), (2, []))  # one plain retry, no restart, no 15 s wait
        self.assertEqual(A.tunnel_watch(A.time.time(), lambda: "ok"), "restored")
        self.assertIn("gate UNREAD", A.status_line())  # the NETWORK reading is gone with the outage
        # the uplink fine and the forward dead (05:27-05:50): the end-to-end probe kicks after two failures
        self.assertEqual(A.tunnel_watch(A.time.time(), down, uplink=clean), "down")
        self.assertEqual(A.tunnel_watch(A.time.time() + 10, down, uplink=clean), "kicked")

    def test_network_change_kicks_the_tunnel_and_marks_the_gate(self):
        A._last_route[0] = "unseen"
        home, phone = ("en0", "172.30.154.83"), ("en0", "172.20.10.1")
        saved, A.mark_gate_dirty = A.mark_gate_dirty, (lambda: self.inbox.append("DIRTY"))
        try:
            self.assertEqual(A.network_watch(lambda: home, lambda: self.kicks.append(1)), "")  # first look: no change
            self.assertEqual(A.network_watch(lambda: home, lambda: self.kicks.append(1)), "")
            self.assertEqual(A.network_watch(lambda: None, lambda: self.kicks.append(1)), "offline")
            self.assertEqual(self.kicks, [])
            self.assertEqual(A.network_watch(lambda: phone, lambda: self.kicks.append(1)), "changed")  # back online
            self.assertEqual(A.network_watch(lambda: home, lambda: self.kicks.append(1)), "changed")  # another network
            self.assertEqual(len(self.kicks), 2)
            self.assertEqual(self.inbox, ["DIRTY", "DIRTY"])
        finally:
            A.mark_gate_dirty = saved

    def test_default_route_reads_this_machine(self):
        r = A.default_route()
        self.assertTrue(r is None or (r[0] and r[1]))

    def test_watch_single_blip_and_recovery(self):
        self.assertEqual(A.tunnel_watch(1000, lambda: "refused"), "down")
        self.assertEqual(A.tunnel_watch(1010, lambda: "ok"), "ok")
        self.assertEqual(A.tunnel_watch(1020, lambda: "refused"), "down")  # counter restarted
        self.assertEqual(self.kicks, [])
        A.tunnel_watch(1030, lambda: "refused")
        self.assertEqual(A.tunnel_watch(1040, lambda: "ok"), "restored")
        self.assertEqual(self.inbox, [])



class ServerTunnelTest(unittest.TestCase):
    def test_ci_runner_retries_a_read_once_and_kicks_nothing(self):
        saved = A.launchctl_kick
        kicked, calls = [], []
        A.launchctl_kick = lambda: kicked.append(1)
        try:
            def read():
                calls.append(1)
                if len(calls) == 1:
                    raise TimeoutError("timed out")
                return "ok"
            self.assertFalse(A.has_tunnel())  # the tests' role is the server's
            self.assertEqual(A.tunnel_retry(read), "ok")
        finally:
            A.launchctl_kick = saved
        self.assertEqual((len(calls), kicked), (2, []))



class OutageWithoutStoreTest(unittest.TestCase):
    def test_a_tunnel_outage_moves_the_watch_on_when_the_store_is_behind_that_tunnel(self):
        """tunnel_watch opened its conditions in ci-runner's store, reached through the very tunnel that was down:
        StoreUnavailable raised before the watch saved its state, so it never counted, kicked or escalated."""
        saved = A.open_condition, A.launchctl_kick, os.environ.get("AGENTCTL_TUNNEL_STATE")
        import tempfile
        tmp = tempfile.TemporaryDirectory()
        os.environ["AGENTCTL_TUNNEL_STATE"] = os.path.join(tmp.name, "t.json")

        def down(*a, **k):
            raise A.StoreUnavailable("POST /v1/store/items: Connection refused")
        A.open_condition, A.launchctl_kick = down, lambda: None
        try:
            t = time.time()
            self.assertEqual(A.tunnel_watch(t, probe=lambda: "refused"), "down")
            self.assertEqual(A.tunnel_watch(t + 1, probe=lambda: "refused"), "kicked")
            self.assertEqual(A.tunnel_watch(t + 1 + A.TUNNEL_RESTORE_WAIT, probe=lambda: "refused"), "escalated")
            self.assertTrue(A.read_json_file(A.tunnel_state_path())["escalated"])
        finally:
            A.open_condition, A.launchctl_kick = saved[0], saved[1]
            if saved[2] is None:
                os.environ.pop("AGENTCTL_TUNNEL_STATE")
            else:
                os.environ["AGENTCTL_TUNNEL_STATE"] = saved[2]
            tmp.cleanup()


if __name__ == "__main__":
    unittest.main()
