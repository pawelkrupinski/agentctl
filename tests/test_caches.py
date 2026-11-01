"""Every module-level mutable a part keeps between calls is registered (core.process_cache), so Base clears it between
tests: a cache left out leaked one test's state into the next (simplification-2026-10-31 item 10)."""
import glob, os, re, sys, unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from test_agentctl import A, Base  # noqa: E402

LIB = os.path.join(os.path.dirname(HERE), "lib", "agentctl")
BARE = re.compile(r"^_?[a-z_]+(?:, _?[a-z_]+)* = (?:\{\}|\[\]|set\(\)|dict\(\)|list\(\))", re.M)


def unregistered(text):
    return [m.group(0) for m in BARE.finditer(text)]


class CachesTest(Base):
    def test_every_module_level_cache_is_registered(self):
        found = []
        for f in sorted(glob.glob(os.path.join(LIB, "*.py"))):
            found += [f"{os.path.basename(f)}: {line}" for line in unregistered(open(f).read())]
        self.assertEqual(found, [], "make it with process_cache(...) so the test harness clears it")

    def test_the_rule_sees_a_bare_cache(self):
        self.assertEqual(unregistered("_x = {}\n_y = process_cache({})\nZ = 1\n"), ["_x = {}"])

    def test_a_registered_cache_starts_each_test_empty(self):
        self.assertTrue(A.PROCESS_CACHES)
        self.assertTrue(all(len(c) == 0 for c in A.PROCESS_CACHES))
        A._gate_lists["leak"] = 1  # the next test's setUp clears it


if __name__ == "__main__":
    unittest.main()
