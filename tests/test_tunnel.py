"""Tunnel health: a hung endpoint counts like a refusal -- one kickstart + retry for reads, the processor probe
(2 failures -> kick, 1/min, inbox entry after 2 min unrestored)."""
import os, socket, sys, tempfile, threading, unittest, urllib.error

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
        threading.Thread(target=self.loop, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.s.getsockname()[1]}/api/v1"

    def loop(self):
        while True:
            try:
                self.conns.append(self.s.accept()[0])
            except OSError:
                return

    def close(self):
        self.s.close()
        for c in self.conns:
            c.close()


class TunnelTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.saved = (A.API, A.TUNNEL_PROBE_TIMEOUT, A.launchctl_kick, A.inbox_write, A.TUNNEL_KICK_MIN_GAP,
                      os.environ.get("AGENTCTL_TUNNEL_STATE"))
        os.environ["AGENTCTL_TUNNEL_STATE"] = os.path.join(self.tmp.name, "t.json")
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

    def test_watch_single_blip_and_recovery(self):
        self.assertEqual(A.tunnel_watch(1000, lambda: "refused"), "down")
        self.assertEqual(A.tunnel_watch(1010, lambda: "ok"), "ok")
        self.assertEqual(A.tunnel_watch(1020, lambda: "refused"), "down")  # counter restarted
        self.assertEqual(self.kicks, [])
        A.tunnel_watch(1030, lambda: "refused")
        self.assertEqual(A.tunnel_watch(1040, lambda: "ok"), "restored")
        self.assertEqual(self.inbox, [])


if __name__ == "__main__":
    unittest.main()
