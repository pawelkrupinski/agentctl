"""Tunnel health (the laptop's tunnel carries the agentctl store): a hung endpoint counts like a refusal in the
processor probe (2 failures -> kick, 1/min, inbox entry after 2 min unrestored)."""
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
        self.url = f"http://127.0.0.1:{self.s.getsockname()[1]}"

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
        self.saved = (os.environ.get("AGENTCTL_STORE_URL"), A.TUNNEL_PROBE_TIMEOUT, A.launchctl_kick, A.inbox_write,
                      A.TUNNEL_KICK_MIN_GAP, os.environ.get("AGENTCTL_TUNNEL_STATE"))
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
        os.environ["AGENTCTL_STORE_URL"] = self.hang.url  # the store across the tunnel: it accepts, never answers

    def tearDown(self):
        self.hang.close()
        url, A.TUNNEL_PROBE_TIMEOUT, A.launchctl_kick, A.inbox_write, A.TUNNEL_KICK_MIN_GAP, st = self.saved
        os.environ.pop("AGENTCTL_STORE_URL", None) if url is None else os.environ.__setitem__("AGENTCTL_STORE_URL", url)
        os.environ.pop("AGENTCTL_TUNNEL_STATE", None) if st is None else os.environ.__setitem__("AGENTCTL_TUNNEL_STATE", st)
        self.tmp.cleanup()

    def test_probe_timeout_and_refused(self):
        self.assertEqual(A.tunnel_ports(), [(3014, "agentctl store")])
        saved_role = A.host_role
        A.host_role = lambda: "sync"  # the laptop's: the store is across the tunnel
        self.addCleanup(setattr, A, "host_role", saved_role)
        self.assertEqual(A.tunnel_probe(), "timeout")
        self.hang.close()
        self.assertEqual(A.tunnel_probe(), "refused")

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

    def test_a_lossy_uplink_shows_the_gate_as_network_not_unread(self):
        # 2026-10-29: ~20% loss to 1.1.1.1 and the jump host kept the forward timing out; restarts only churned it
        down, lossy, clean = (lambda: "timeout"), (lambda: A.UPLINK_LOSS_MAX), (lambda: 0.0)  # AT the bound: lossy
        A.publish_gate_summary("o/r", "gate green · no reds", epoch=A.time.time() - 3600)
        self.assertIn("gate UNREAD", A.status_line())
        self.assertEqual(A.tunnel_watch(A.time.time(), down, uplink=lossy), "network")
        line = A.status_line()
        self.assertIn("gate NETWORK (lossy uplink: 20% loss", line)
        self.assertNotIn("UNREAD", line)
        self.assertEqual(self.kicks, [])  # no restart
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
