"""THE MOVIES TOOLS agentctl calls are read from movies itself (moviestools.py). 2026-11-22: slots.py's hand-copied
HOOK_CHECKS -- what a local recheck skips, all but the failing checks -- named three checks movies' pre-push no longer
had (oldrefs, page, basic) and missed fifteen it had, so a recheck ran checks it meant to skip."""
import os, sys, tempfile, unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from test_agentctl import A  # noqa: E402

HOOK = """#!/usr/bin/env bash
# PRE-PUSH: `eslint` ... a comment naming skipped words and check_ghost() is no check
skipped() { case " ${PREPUSH_SKIP:-} " in *" $1 "*) return 0 ;; *) return 1 ;; esac; }
check_eslint() { :; }
check_sbt() {
    if skipped coupling; then :; fi
}
check_new_one()  { :; }
for check in $checks; do if skipped "$check"; then continue; fi; done
if skipped redgate; then :; fi
"""


class HookChecksTest(unittest.TestCase):
    def test_the_checks_are_the_hooks_own_functions_and_skip_words(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "pre-push")
            open(p, "w").write(HOOK)
            self.assertEqual(A.hook_checks(p), ["coupling", "eslint", "new_one", "redgate", "sbt"])
            self.assertEqual(A.hook_checks(os.path.join(d, "none")), [])

    def test_no_hand_copied_list_is_left(self):
        self.assertFalse(hasattr(A, "HOOK_CHECKS"), "the hook's checks are read from the hook (hook_checks)")


if __name__ == "__main__":
    unittest.main()
