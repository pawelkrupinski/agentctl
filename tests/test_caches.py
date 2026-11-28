"""Every module-level mutable a part keeps between calls is registered (core.process_cache), so Base clears it between
tests: a cache left out leaked one test's state into the next (simplification-2026-10-31 item 10)."""
import glob, os, re, sys, unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from test_agentctl import A, Base  # noqa: E402
import harness  # noqa: E402
from harness import case_context  # noqa: E402

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
        case_context().runs.main_runs_of("leak")  # the next test's setUp clears it


class EveryTestCaseTest(unittest.TestCase):
    """Not only Base's tests: the harness clears the caches before EVERY test (2026-11-17: agentctl CI #44108162628 --
    test_caches' deliberate `_main_runs["leak"] = 1` reached CachedGateTest, a FakeForge test that is no Base, when
    both ran in one worker: ci_state_refresh iterated the 1, "'int' object is not iterable", and the gate was never
    published; the worker split decided when, so it failed now and then and never on the laptop)."""

    def test_the_harness_clears_every_registered_cache_for_any_test(self):
        import harness
        case_context().runs.main_runs_of("leak")
        harness._reset_process_state()
        self.assertEqual([c for c in A.PROCESS_CACHES if len(c)], [])


class OwnedCachesTest(unittest.TestCase):
    """THE MEMOS ARE OWNED (lib/agentctl/caches.py): a context's caches are its own, so a memo filled through one context
    is empty in the next, and a module keeps none of them at module level."""

    def test_a_memo_filled_through_one_context_is_empty_in_another(self):
        first, second = case_context(), harness.context_with(case_context().runs)
        first.caches.trailer_fixes["abc"] = ["run 1"]
        first.caches.never_green_at[("o/r", "wf")] = 1.0
        self.assertEqual((second.caches.trailer_fixes, second.caches.never_green_at), ({}, {}))
        self.assertEqual(first.caches.trailer_fixes, {"abc": ["run 1"]})

    def test_a_skipped_repos_line_is_said_once_per_memo_not_once_per_process(self):
        said, other = {}, {}
        self.assertIsNotNone(A.red_others_skipped(said, "o/r", "HTTP 500", now_=1000))
        self.assertIsNone(A.red_others_skipped(said, "o/r", "HTTP 500", now_=1001))
        self.assertIsNotNone(A.red_others_skipped(other, "o/r", "HTTP 500", now_=1002), "another memo has not said it")

    def test_a_throttle_is_its_contexts_own(self):
        ran, saved = [], A.recovery_tick
        A.recovery_tick = lambda store, registry: ran.append(store)
        try:
            first, second = case_context(), harness.context_with(case_context().runs)
            A.maybe_recovery_tick("one", None, 1000.0, first.caches.ran_at)
            A.maybe_recovery_tick("one again", None, 1001.0, first.caches.ran_at)  # within 5 s: throttled
            A.maybe_recovery_tick("two", None, 1001.0, second.caches.ran_at)  # another context's throttle: not
        finally:
            A.recovery_tick = saved
        self.assertEqual(ran, ["one", "two"])

    def test_the_moved_memos_are_no_module_level_state(self):
        for name in ("_identity_known", "_trailer_fixes", "_link_reads", "_never_green_at", "_others_said",
                     "_last_upkeep", "_last_recovery", "_last_ref_sweep", "_pin_tried", "_last_red_tick",
                     "_last_others_tick", "_last_route", "_batch_said"):
            self.assertFalse(hasattr(A, name), name)



if __name__ == "__main__":
    unittest.main()
