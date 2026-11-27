"""THE PRE-PUSH HOOK'S CAPABILITY (queuesubmit.land_refs_supported): movies lists what its hook does in
scripts/hooks/pre-push-capabilities (one name a line, `land-refs` among them); a movies revision before that file is
read by the old grep of the hook's text, which stays as the fallback."""
import os
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from test_agentctl import A  # noqa: E402


def sh(cwd, *argv):
    return subprocess.run(argv, cwd=cwd, check=True, capture_output=True, text=True).stdout


class LandRefsCapabilityTest(unittest.TestCase):
    def movies_at_main(self, files):
        """A clone whose origin/main holds `files` ({path: text})."""
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        bare, clone = os.path.join(tmp.name, "o.git"), os.path.join(tmp.name, "clone")
        sh(tmp.name, "git", "init", "-q", "--bare", "-b", "main", bare)
        sh(tmp.name, "git", "init", "-q", "-b", "main", clone)
        sh(clone, "git", "remote", "add", "origin", bare)
        for path, text in {"README": "x\n", **files}.items():
            os.makedirs(os.path.dirname(os.path.join(clone, path)), exist_ok=True)
            with open(os.path.join(clone, path), "w") as f:
                f.write(text)
        sh(clone, "git", "add", "-A")
        sh(clone, "git", "commit", "-qm", "c")
        sh(clone, "git", "push", "-q", "origin", "HEAD:main")
        sh(clone, "git", "fetch", "-q", "origin")
        return clone

    def test_the_capabilities_file_names_land_refs(self):
        clone = self.movies_at_main({"scripts/hooks/pre-push-capabilities": "# what the hook does\nland-refs\n"})
        self.assertTrue(A.land_refs_supported(clone))

    def test_a_capabilities_file_without_it_says_no_whatever_the_hook_text_holds(self):
        clone = self.movies_at_main({"scripts/hooks/pre-push-capabilities": "other\n",
                                     "scripts/hooks/pre-push": "case $ref in refs/heads/land/*) ;; esac\n"})
        self.assertFalse(A.land_refs_supported(clone))

    def test_an_older_revision_is_read_by_its_hooks_text(self):
        self.assertTrue(A.land_refs_supported(self.movies_at_main({"scripts/hooks/pre-push": "refs/heads/land/*) ;;\n"})))
        self.assertFalse(A.land_refs_supported(self.movies_at_main({"scripts/hooks/pre-push": "nothing\n"})))


if __name__ == "__main__":
    unittest.main()
