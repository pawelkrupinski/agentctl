"""Movies first (owner, 2026-10-31): with forge = github, `forgejo_owners` keeps a repo-scoped command for those owners'
repos on forge forgejo for its whole process; `store_auth` keeps the store token whatever the command's forge."""
import argparse, os, subprocess, sys, tempfile, unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from test_agentctl import A  # noqa: E402

KEYS = ("AGENTCTL_FORGE", "AGENTCTL_FORGEJO_OWNERS", "AGENTCTL_STORE_AUTH", "AGENTCTL_REMOTE")


class ForgeOwnersTest(unittest.TestCase):
    def setUp(self):
        self.saved = {k: os.environ.get(k) for k in KEYS}
        for k in KEYS:
            os.environ.pop(k, None)
        self.tmp = tempfile.TemporaryDirectory()
        self.core = os.path.join(self.tmp.name, "core")
        self.movies = os.path.join(self.tmp.name, "movies")
        for path, owner, name in ((self.core, "bitcashier", "core"), (self.movies, "pawelkrupinski", "movies")):
            subprocess.run(["git", "init", "-q", path], check=True)
            subprocess.run(["git", "-C", path, "remote", "add", "origin", f"https://github.com/{owner}/{name}.git"], check=True)
            subprocess.run(["git", "-C", path, "remote", "add", "forgejo", f"ssh://forgejo-git/{owner}/{name}.git"], check=True)

    def tearDown(self):
        for k, v in self.saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        self.tmp.cleanup()

    def ns(self, cmd, repo):
        return argparse.Namespace(cmd=cmd, repo=repo)

    def test_a_forgejo_owners_repo_runs_its_command_on_forgejo(self):
        os.environ.update(AGENTCTL_FORGE="github", AGENTCTL_FORGEJO_OWNERS="bitcashier")
        self.assertIsNone(A.forge_for_command(self.ns("land", self.movies)))
        self.assertEqual((A.forge(), A.remote()), ("github", "origin"))
        self.assertEqual(A.forge_for_command(self.ns("land", self.core)), "bitcashier")
        self.assertEqual((A.forge(), A.remote()), ("forgejo", "forgejo"))

    def test_a_short_name_or_slug_is_an_owner_too(self):
        os.environ.update(AGENTCTL_FORGE="github", AGENTCTL_FORGEJO_OWNERS="bitcashier")
        self.assertEqual(A.forge_for_command(self.ns("ci-why", "core")), "bitcashier")
        os.environ["AGENTCTL_FORGE"] = "github"
        self.assertIsNone(A.forge_for_command(self.ns("ci-why", "pawelkrupinski/movies")))

    def test_only_repo_scoped_commands_and_only_on_github(self):
        os.environ.update(AGENTCTL_FORGE="github", AGENTCTL_FORGEJO_OWNERS="bitcashier")
        self.assertIsNone(A.forge_for_command(self.ns("tick", self.core)))
        os.environ["AGENTCTL_FORGE"] = "forgejo"
        self.assertIsNone(A.forge_for_command(self.ns("land", self.core)))
        os.environ.update(AGENTCTL_FORGE="github", AGENTCTL_FORGEJO_OWNERS="")
        self.assertIsNone(A.forge_for_command(self.ns("land", self.core)))
        self.assertEqual(A.forge(), "github")

    def test_store_auth_follows_the_forge_unless_set(self):
        movies = A.SHORT["movies"]
        os.environ["AGENTCTL_FORGE"] = "github"
        self.assertEqual((A.store_auth(), A.token_env_name(movies)), ("store-tokens", "AGENTCTL_STORE_TOKEN"))
        os.environ["AGENTCTL_FORGE"] = "forgejo"
        self.assertEqual((A.store_auth(), A.token_env_name(movies)), ("forgejo", "FORGEJO_API_TOKEN"))
        os.environ["AGENTCTL_STORE_AUTH"] = "store-tokens"  # a bitcashier command switched to forgejo keeps it
        self.assertEqual(A.token_vault_key(movies), ("bitcashier", "AGENTCTL_STORE_TOKEN"))
        self.assertEqual(A.token_env_name(A.SHORT["core"]), "GH_TOKEN")


if __name__ == "__main__":
    unittest.main()
