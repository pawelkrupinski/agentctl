"""ONE EPISODE, ONE NOTIFICATION (watchdog.py; 2026-11-23: W1 told the same queued change's violation every few
minutes, its condition closing and re-opening as the hold was read present and absent)."""
import os, sys, unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from test_agentctl import A  # noqa: E402

W1 = ("unlanded change 20261123-013033-movies-what-if-py-edef9e (queued: nothing holds it, yet it has not started) "
      "is {} old")


class EpisodeTest(unittest.TestCase):
    def test_the_same_change_is_told_once_per_episode_another_at_once(self):
        st, t = {}, 1_000_000.0
        self.assertTrue(A.new_episode(st, "W1", W1.format("3 h"), t))
        self.assertFalse(A.new_episode(st, "W1", W1.format("3 h 3 min"), t + 180), "the same change, minutes later")
        self.assertFalse(A.new_episode(st, "W1", W1.format("4 h"), t + 3600))
        other = W1.replace("what-if-py-edef9e", "slice-py-e3ac58").format("3 h")
        self.assertTrue(A.new_episode(st, "W1", other, t + 3700), "another change is another episode")
        self.assertTrue(A.new_episode(st, "W1", W1.format("9 h"), t + 3700 + A.WATCHDOG_RETELL), "told again later")

    def test_a_detail_naming_no_change_is_keyed_by_its_words(self):
        self.assertEqual(A.episode_subject("W5: processor silent 12 min"), A.episode_subject("W5: processor silent 15 min"))


if __name__ == "__main__":
    unittest.main()
