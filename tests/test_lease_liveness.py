"""LEASE LIVENESS AND KINDS, SAID ONCE: `lease_is_live(row, t)` (a holder, and an expiry past t) is the one reading;
`lease_live(name, store)` is it on the store's clock with the meta decoded; `LeaseKinds` names which kinds of a `red/`
lease count as what (a running session, a joined red, a family holder, an owner's)."""
import os, sys, unittest
from types import SimpleNamespace as NS

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from test_agentctl import A  # noqa: E402


def row(holder="h", expires=100.0, **meta):
    import json
    return {"name": "red/k", "holder": holder, "expires_at": expires, "epoch": 1, "max_until": 200.0, "meta": json.dumps(meta)}


class LeaseLivenessTest(unittest.TestCase):
    def test_a_row_is_live_with_a_holder_and_an_expiry_strictly_after_now(self):
        self.assertTrue(A.lease_is_live(row(), 99.0))
        self.assertFalse(A.lease_is_live(row(), 100.0))  # the expiry instant is already free
        self.assertFalse(A.lease_is_live(row(holder=""), 50.0))  # released: no holder
        self.assertFalse(A.lease_is_live(None, 50.0))
        self.assertFalse(A.lease_is_live({"expires_at": 100.0}, 50.0))  # no holder key: not live, never a KeyError

    def test_lease_live_reads_the_store_clock_and_decodes_the_meta(self):
        st = NS(get=lambda name: row(note="n") if name == "x" else None, clock=lambda: 99.0)
        got = A.lease_live("x", st)
        self.assertEqual(got["meta"], {"note": "n"})
        self.assertIsNone(A.lease_live("y", st))
        st.clock = lambda: 100.0
        self.assertIsNone(A.lease_live("x", st))

    def test_the_listing_marks_the_same_rows_live(self):
        self.assertIn("LIVE", A._lease_line(row(), 99.0))
        self.assertIn("free", A._lease_line(row(), 100.0))
        self.assertTrue(A._lease_recent(row(), 99.0) and A._lease_recent(row(), 100.0 + A.LEASE_LIST_RECENT))
        self.assertFalse(A._lease_recent(row(), 101.0 + A.LEASE_LIST_RECENT))


class LeaseKindsTest(unittest.TestCase):
    def test_which_kinds_count_as_what(self):
        table = {  # kind: (a running session, a joined red, a family holder, an owner's red, a claim)
            "fixer": (True, False, True, False, False), "sibling": (True, True, False, False, False),
            "owned": (True, False, False, True, False), "claim": (False, True, False, False, True),
            None: (False, False, False, False, False), "owner": (False, False, False, False, False)}
        for kind, want in table.items():
            r = row(**({"kind": kind} if kind else {}))
            got = (A.LeaseKinds.is_session(r), A.LeaseKinds.is_joined(r), A.LeaseKinds.holds_family(r),
                   A.LeaseKinds.is_owned(r), A.LeaseKinds.is_claim(r))
            self.assertEqual(got, want, kind)

    def test_a_claim_made_inside_a_dispatched_fixer_holds_its_family(self):
        self.assertTrue(A.LeaseKinds.holds_family(row(kind="claim", fixer=True)))
        self.assertFalse(A.LeaseKinds.holds_family(row(kind="claim")))  # an interactive claim takes only its red

    def test_the_fixers_alive_are_one_row_per_holder_of_the_session_kinds(self):
        rows = [row(holder="a", kind="fixer"), row(holder="a", kind="sibling"), row(holder="b", kind="claim"),
                row(holder="c", kind="owned"), row(holder="d", kind="fixer", expires=5.0)]
        st = NS(list=lambda prefix: rows)
        self.assertEqual([r["holder"] for r in A.red_fixers_alive(st, 50.0)], ["a", "c"])


if __name__ == "__main__":
    unittest.main()
