"""ONE STORE CALL SURFACE (review 2026-11-26, C 1.9): storecalls.STORE_CALLS is the one spelling of the store's calls.
These tests hold it to Store's own methods and parameters, the client's methods and retry safety to it, the server's
routes to it, and both directions of version skew: an old client's calls served by a new store, a new client's call
refused by an old store as one it lacks (never a missing item)."""
import os, sys, unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from test_agentctl import A  # noqa: E402
from test_phase2 import Phase2Base  # noqa: E402


def params(fn):
    code = fn.__code__
    return " ".join(code.co_varnames[1:code.co_argcount])


class TableTest(unittest.TestCase):
    def test_every_call_is_a_store_method_with_its_parameters(self):
        for c in A.STORE_CALLS:
            self.assertTrue(callable(getattr(A.Store, c.name, None)), c.name)
            self.assertEqual(params(getattr(A.Store, c.name)), c.args, c.name)
            self.assertIn(c.kind, (A.READ, A.IDEMPOTENT, A.WRITE), c.name)
            self.assertGreaterEqual(c.since, 1)
        self.assertEqual(len({c.name for c in A.STORE_CALLS}), len(A.STORE_CALLS), "a name twice")

    def test_every_public_store_method_is_a_call_or_the_servers_own(self):
        public = {m for m in dir(A.Store) if not m.startswith("_") and callable(getattr(A.Store, m))}
        self.assertEqual(public - set(A.SERVER_ONLY), {c.name for c in A.STORE_CALLS},
                         "a new Store method is a row of STORE_CALLS or named SERVER_ONLY")

    def test_the_client_has_each_call_from_the_table_and_its_retry_safety(self):
        for c in A.STORE_CALLS:
            self.assertTrue(callable(getattr(A.RemoteStore, c.name, None)), c.name)
            self.assertIn(c.args.replace(" ", ", "), getattr(A.RemoteStore, c.name).__doc__)
        self.assertTrue(A.served_calls()["events"].retry_safe)
        self.assertTrue(A.served_calls()["renew"].retry_safe)
        self.assertFalse(A.served_calls()["transition"].retry_safe, "a transition sent twice may move twice")

    def test_an_older_version_serves_only_the_calls_it_had(self):
        newest = A.served_calls()
        self.assertEqual(set(newest), {c.name for c in A.STORE_CALLS})
        first = A.served_calls(1)
        self.assertTrue(set(first) <= set(newest))
        self.assertTrue(all(c.since == 1 for c in first.values()))


class SkewTest(Phase2Base):
    def test_a_new_store_serves_every_call_an_old_client_sends(self):
        """An old client: the wire format of every version-1 call, sent as it always was."""
        self.st.put_item("change", "c1", "queued", {})
        for name in A.served_calls(1):
            code, _ctype, body = self.api.handle("POST", f"/v1/store/{name}", {"Authorization": "token tok-pawel"},
                                                 b'{"args": [], "kwargs": {}}')
            self.assertNotIn(b"UnknownCall", body, name)
        self.assertEqual(self.remote.item("change", "c1")["state"], "queued")

    def test_an_old_store_refuses_a_newer_call_as_one_it_lacks(self):
        self.api.calls = {n: c for n, c in A.served_calls().items() if n != "derived"}  # a store from before it
        with self.assertRaises(A.StoreLacksCall):
            self.remote.derived()
        self.assertEqual(self.remote.items("change"), [], "its other calls go on")

    def test_the_server_serves_exactly_the_table(self):
        code, _ctype, body = self.api.handle("POST", "/v1/store/db", {"Authorization": "token tok-pawel"}, b"{}")
        self.assertEqual(code, 404)
        self.assertIn(b"UnknownCall", body, "a server-only method is no call")


if __name__ == "__main__":
    unittest.main()
