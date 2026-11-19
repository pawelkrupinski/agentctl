"""ONE MACHINE HOLDS THE LAPTOP ROLE (2026-11-18 23:41Z, core Main Pipeline #1053, red 0c8eea91b712): a second Mac's
sync loop dispatched a fixer against the shared store while this one held the laptop's leases; each judged the other's
pids as gone. The role is a lease in the store: one machine holds it, the other is standby and does no laptop duty."""
import os, sys, tempfile, unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from test_agentctl import A  # noqa: E402

PRO2 = {"AGENTCTL_HOSTNAME": "Pawes-MacBook-Pro-2", "AGENTCTL_MACHINE_ID": "8C1E-PRO-2"}
MBP = {"AGENTCTL_HOSTNAME": "pawel-mbp", "AGENTCTL_MACHINE_ID": "41A0-MBP"}


class LaptopRoleTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.t = [1795045000.0]
        self.st = A.Store(path=os.path.join(self.tmp.name, "store.db"), clock=lambda: self.t[0])
        keys = ("AGENTCTL_STATE_ROOT", "AGENTCTL_INBOX_DIR", "AGENTCTL_HOSTNAME", "AGENTCTL_MACHINE_ID")
        saved = {k: os.environ.get(k) for k in keys}
        self.addCleanup(lambda: [os.environ.pop(k, None) if v is None else os.environ.__setitem__(k, v)
                                 for k, v in saved.items()])
        self.told = []
        saved_inbox = A.inbox_write
        A.inbox_write = lambda cmd, line, *a, **k: self.told.append((os.environ["AGENTCTL_HOSTNAME"], line))
        self.addCleanup(setattr, A, "inbox_write", saved_inbox)

    def on(self, machine):
        """Act as that machine: its own identity and its own state root (the record of its lease's secret)."""
        os.environ.update(machine)
        os.environ["AGENTCTL_STATE_ROOT"] = os.path.join(self.tmp.name, machine["AGENTCTL_HOSTNAME"])
        os.environ["AGENTCTL_INBOX_DIR"] = os.path.join(self.tmp.name, machine["AGENTCTL_HOSTNAME"], "inbox")

    def step(self, machine):
        self.on(machine)
        return A.laptop_role_step(self.st)

    def test_both_start_one_holds_the_other_is_standby_and_told_once(self):
        self.assertTrue(self.step(PRO2))
        self.assertFalse(self.step(MBP))
        self.t[0] += 60
        self.assertTrue(self.step(PRO2), "the holder renews it pass after pass")
        self.assertFalse(self.step(MBP))
        self.assertEqual([h for h, _ in self.told], ["pawel-mbp"], "the standby machine's owner is told ONCE")
        self.assertIn("laptop role held by Pawes-MacBook-Pro-2 since", self.told[0][1])
        self.assertIn("this machine (pawel-mbp) is standby", self.told[0][1])
        row = self.st.get(A.LAPTOP_ROLE_LEASE)
        self.assertEqual(row["holder"], "role:Pawes-MacBook-Pro-2:8C1E-PRO-2")
        self.on(PRO2)
        self.assertIsNone(A.laptop_role_standby(self.st))
        self.on(MBP)
        self.assertIn("Pawes-MacBook-Pro-2", A.laptop_role_standby(self.st))

    def test_a_restarted_holder_keeps_its_role(self):
        self.step(PRO2)
        os.remove(os.path.join(self.tmp.name, "Pawes-MacBook-Pro-2", "laptop-role.json"))  # its secret is lost
        self.t[0] += 30
        self.assertTrue(self.step(PRO2))
        self.assertFalse(self.step(MBP))

    def test_take_hands_the_role_over(self):
        self.step(PRO2)
        self.step(MBP)
        self.on(MBP)
        held, _ = A.laptop_role(self.st, take=True)
        self.assertTrue(held)
        self.assertFalse(self.step(PRO2), "the old holder is standby now")
        self.assertTrue(self.step(MBP))
        self.assertEqual([h for h, _ in self.told], ["pawel-mbp", "Pawes-MacBook-Pro-2"])

    def test_a_silent_holder_is_taken_over_past_the_ttl(self):
        self.step(PRO2)
        self.t[0] += A.LAPTOP_ROLE_TTL - 30
        self.assertFalse(self.step(MBP))
        self.t[0] += 60  # Pro-2 asleep or gone: no renewal for LAPTOP_ROLE_TTL
        self.assertTrue(self.step(MBP))
        self.assertFalse(self.step(PRO2))

    def test_a_silent_laptop_contact_does_not_pause_the_role(self):
        self.step(PRO2)
        self.st.contact("laptop")  # the laptop's leases' clocks pause when it goes silent; the role's never does
        self.t[0] += A.LAPTOP_ROLE_TTL + 3600
        self.assertTrue(self.step(MBP))

    def test_tonights_shape_a_standby_machine_dispatches_no_fixer_and_runs_no_duty(self):
        # 23:41Z: pawel-mbp's sync loop dispatched fixer red-0c8eea91b712-1 while Pro-2 ran the laptop's duties
        self.step(PRO2)
        self.step(MBP)
        self.on(MBP)
        u = {"workflow": "Main Pipeline", "run": {"number": 1053, "databaseId": 1}, "job": "build-and-test",
             "key": "0c8eea91b712", "repo": "pawelkrupinski/core"}
        saved = A.Store
        A.Store = lambda *a, **k: self.st
        self.addCleanup(setattr, A, "Store", saved)
        ok, msg = A.red_dispatch("pawelkrupinski/core", None, u, store=self.st)
        self.assertFalse(ok)
        self.assertIn("NOT DISPATCHED: laptop role held by Pawes-MacBook-Pro-2", msg)
        ran = []
        for name in ("notify_escalations", "reconcile_alerts", "red_dispatcher", "red_renew_holders", "red_reap_orphans",
                     "refresh_pinned_recording", "explorer_tick"):
            saved_fn = getattr(A, name)
            setattr(A, name, lambda *a, _n=name, **k: ran.append(_n) or [])
            self.addCleanup(setattr, A, name, saved_fn)
        steps = dict(A.sync_steps(None, None, "pawelkrupinski/movies"))
        for name in ("laptop role", "notifier", "alerts", "claims", "dispatcher", "pins", "drift explorer"):
            steps[name]()
        self.assertEqual(ran, [], "standby: none of the laptop's duties")
        self.on(PRO2)
        for name in ("laptop role", "notifier", "alerts", "claims", "dispatcher"):
            steps[name]()
        self.assertEqual(ran, ["notify_escalations", "reconcile_alerts", "red_renew_holders", "red_reap_orphans",
                               "red_dispatcher"])

    def test_the_store_refuses_a_laptop_lease_to_a_client_that_is_not_the_role_holder(self):
        # pawel-mbp's agentctl predates the role: it names no role, and its fixer's lease is refused at the store
        body = {"args": ["red/0c8eea91b712", "laptop:fixer:red-0c8eea91b712-1", 900, 10800], "kwargs": {}}
        self.assertIsNone(A.laptop_role_refusal(self.st, "/v1/store/acquire", body, None), "no role held: no refusal")
        self.step(PRO2)
        refused = A.laptop_role_refusal(self.st, "/v1/store/acquire", body, None)
        self.assertIn("STANDBY: laptop:fixer:red-0c8eea91b712-1 refused: laptop role held by Pawes-MacBook-Pro-2", refused)
        self.assertIsNotNone(A.laptop_role_refusal(self.st, "/v1/store/take", {"args": [7, "laptop:owner:core"]},
                                                   "role:pawel-mbp:41A0-MBP"))
        self.assertIsNone(A.laptop_role_refusal(self.st, "/v1/store/acquire", body, "role:Pawes-MacBook-Pro-2:8C1E-PRO-2"))
        self.assertIsNone(A.laptop_role_refusal(self.st, "/v1/store/acquire",
                                                {"args": ["role/laptop", "role:pawel-mbp:41A0-MBP", 600, 1]}, None))
        self.assertIsNone(A.laptop_role_refusal(self.st, "/v1/store/renew", body, None), "renewals go on")

    def test_a_store_with_no_role_lease_refuses_nothing(self):
        self.on(MBP)
        self.assertIsNone(A.laptop_role_standby(self.st))


if __name__ == "__main__":
    unittest.main()
