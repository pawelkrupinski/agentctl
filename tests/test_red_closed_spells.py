"""A red's closed spells (`key`, `key.2`, ...) are read in ONE store listing, never one round trip a spell (review 2026-11-26,
B 19: up to 199 `store.item` reads, each an HTTP call to the store on ci-runner)."""
import os, sys, unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from test_agentctl import A  # noqa: E402


class CountingStore:
    def __init__(self, items):
        self.rows = {it["id"]: it for it in items}
        self.reads = 0

    def items(self, typ, states=None):
        self.reads += 1
        return list(self.rows.values())

    def item(self, typ, iid):
        self.reads += 1
        return self.rows.get(iid)


def spell(iid, state, key="k1"):
    return {"id": iid, "state": state, "data": {"key": key}}


class ClosedSpellsTest(unittest.TestCase):
    def test_the_newest_closed_spell_is_read_in_one_store_read(self):
        store = CountingStore([spell("k1", "cleared"), spell("k1.2", "cleared"), spell("k1.3", "cleared")])
        self.assertEqual(A.red_closed_of(store, "k1")["id"], "k1.3")
        self.assertEqual(store.reads, 1)

    def test_a_spell_id_names_the_same_chain(self):
        store = CountingStore([spell("k1", "cleared"), spell("k1.2", "cleared")])
        self.assertEqual(A.red_closed_of(store, "k1.2")["id"], "k1.2")

    def test_a_gap_ends_the_chain_and_an_open_newest_spell_is_no_closed_one(self):
        self.assertEqual(A.red_closed_of(CountingStore([spell("k1", "cleared"), spell("k1.3", "cleared")]), "k1")["id"], "k1")
        self.assertIsNone(A.red_closed_of(CountingStore([spell("k1", "cleared"), spell("k1.2", "offered")]), "k1"))
        self.assertIsNone(A.red_closed_of(CountingStore([]), "k1"))


if __name__ == "__main__":
    unittest.main()
