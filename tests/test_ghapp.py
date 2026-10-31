"""The reconciler's GitHub identity (lib/agentctl/ghapp.py, W5b): an App's RS256 JWT signed by openssl, the
installation token minted from it (scoped to one repository, cached until shortly before it expires), git's credential
helper, and forge = github's reads and pushes using it -- only with AGENTCTL_IDENTITY=github-app."""
import base64, io, json, os, subprocess, sys, tempfile, threading, unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from test_agentctl import A  # noqa: E402

ENV_KEYS = ("AGENTCTL_IDENTITY", "AGENTCTL_GITHUB_API", "AGENTCTL_GITHUB_LANDER_APP_ID", "AGENTCTL_GITHUB_LANDER_KEY_FILE",
            "AGENTCTL_GITHUB_READER_APP_ID", "AGENTCTL_GITHUB_READER_KEY_FILE", "AGENTCTL_GITHUB_LANDER_REPOS",
            "AGENTCTL_FORGE", "AGENTCTL_HOST", "GH_TOKEN", "GIT_CONFIG_COUNT", "AGENTCTL_GHAPP_GIT")


def b64d(s):
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


class FakeGitHub(BaseHTTPRequestHandler):
    """api.github.com's two App endpoints: GET /repos/<o>/<r>/installation, POST /app/installations/<id>/access_tokens."""
    calls = []

    def log_message(self, *a):
        pass

    def answer(self, code, obj):
        b = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def do_GET(self):
        self.calls.append(("GET", self.path, self.headers.get("Authorization"), None))
        if self.path.endswith("/installation"):
            owner = self.path.split("/")[2]
            return self.answer(200, {"id": 11 if owner == "pawelkrupinski" else 22})
        self.answer(404, {"message": "Not Found"})

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
        self.calls.append(("POST", self.path, self.headers.get("Authorization"), body))
        inst = self.path.split("/")[3]
        self.answer(201, {"token": f"ghs_{inst}_{body['repositories'][0]}_{len(self.calls)}",
                          "expires_at": "2099-01-01T00:00:00Z"})


class GhAppTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.keys = tempfile.TemporaryDirectory()
        cls.key = os.path.join(cls.keys.name, "app.pem")
        cls.pub = os.path.join(cls.keys.name, "app.pub")
        subprocess.run(["openssl", "genrsa", "-out", cls.key, "2048"], check=True, capture_output=True)
        subprocess.run(["openssl", "rsa", "-in", cls.key, "-pubout", "-out", cls.pub], check=True, capture_output=True)

    @classmethod
    def tearDownClass(cls):
        cls.keys.cleanup()

    def setUp(self):
        self.saved = {k: os.environ.get(k) for k in ENV_KEYS}
        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), FakeGitHub)
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        FakeGitHub.calls = []
        A._ghapp_tokens.clear()
        for k in ENV_KEYS:
            os.environ.pop(k, None)
        os.environ.update({"AGENTCTL_IDENTITY": "github-app",
                           "AGENTCTL_GITHUB_API": f"http://127.0.0.1:{self.srv.server_address[1]}",
                           "AGENTCTL_GITHUB_LANDER_APP_ID": "101", "AGENTCTL_GITHUB_LANDER_KEY_FILE": self.key,
                           "AGENTCTL_GITHUB_READER_APP_ID": "202", "AGENTCTL_GITHUB_READER_KEY_FILE": self.key})

    def tearDown(self):
        self.srv.shutdown()
        self.srv.server_close()
        A._ghapp_tokens.clear()
        for k, v in self.saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def test_the_jwt_is_rs256_and_verifies_with_the_apps_public_key(self):
        jwt = A.ghapp_jwt("101", self.key, now_=1_700_000_000)
        head, body, sig = jwt.split(".")
        self.assertEqual(json.loads(b64d(head)), {"alg": "RS256", "typ": "JWT"})
        self.assertEqual(json.loads(b64d(body)), {"iat": 1_700_000_000 - 60, "exp": 1_700_000_000 + 540, "iss": "101"})
        with tempfile.NamedTemporaryFile() as s:
            s.write(b64d(sig))
            s.flush()
            r = subprocess.run(["openssl", "dgst", "-sha256", "-verify", self.pub, "-signature", s.name],
                               input=f"{head}.{body}".encode(), capture_output=True)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)

    def test_a_token_is_minted_per_repo_with_the_right_app_and_reused(self):
        t1 = A.ghapp_token("pawelkrupinski/movies")
        self.assertTrue(t1.startswith("ghs_11_movies_"))
        get, post = FakeGitHub.calls
        self.assertEqual(get[1], "/repos/pawelkrupinski/movies/installation")
        self.assertEqual(json.loads(b64d(get[2].split()[1].split(".")[1]))["iss"], "101")  # the lander's App
        self.assertEqual(post[1], "/app/installations/11/access_tokens")
        self.assertEqual(post[3], {"repositories": ["movies"]})
        self.assertEqual(A.ghapp_token("pawelkrupinski/movies"), t1)  # from memory
        A._ghapp_tokens.clear()
        self.assertEqual(A.ghapp_token("pawelkrupinski/movies"), t1)  # from the 0600 cache file
        self.assertEqual(len(FakeGitHub.calls), 2)
        cache = A._ghapp_cache_path("lander", "pawelkrupinski/movies")
        self.assertEqual(os.stat(cache).st_mode & 0o777, 0o600)
        t2 = A.ghapp_token("bitcashier/core")  # not the lander's: the reader's App
        self.assertTrue(t2.startswith("ghs_22_core_"))
        self.assertEqual(json.loads(b64d(FakeGitHub.calls[2][2].split()[1].split(".")[1]))["iss"], "202")

    def test_a_token_near_its_expiry_is_replaced(self):
        A._ghapp_tokens[("lander", "pawelkrupinski/movies")] = {"token": "old", "expires": 1000.0}
        self.assertEqual(A.ghapp_token("pawelkrupinski/movies", now_=1000 - A.GHAPP_REFRESH - 1), "old")
        self.assertNotEqual(A.ghapp_token("pawelkrupinski/movies", now_=1000 - A.GHAPP_REFRESH + 1), "old")

    def test_an_unconfigured_app_is_no_read_access(self):
        os.environ.pop("AGENTCTL_GITHUB_READER_APP_ID")
        with self.assertRaises(A.NoReadAccess):
            A.ghapp_token("bitcashier/infra")

    def test_the_credential_helper_answers_github_paths_only(self):
        out = io.StringIO()
        A.ghapp_credential(io.StringIO("protocol=https\nhost=github.com\npath=pawelkrupinski/movies.git\n"), out)
        lines = out.getvalue().splitlines()
        self.assertEqual(lines[0], "username=x-access-token")
        self.assertTrue(lines[1].startswith("password=ghs_11_movies_"))
        out = io.StringIO()
        A.ghapp_credential(io.StringIO("protocol=https\nhost=example.com\npath=x/y\n"), out)
        self.assertEqual(out.getvalue(), "")

    def test_git_is_given_the_helper_through_its_environment_once(self):
        env = A.ghapp_git_env({"GIT_CONFIG_COUNT": "1", "GIT_CONFIG_KEY_0": "a.b", "GIT_CONFIG_VALUE_0": "c"})
        self.assertEqual(env["GIT_CONFIG_COUNT"], "4")
        self.assertEqual(env["GIT_CONFIG_KEY_0"], "a.b")
        self.assertEqual(env["GIT_CONFIG_VALUE_1"], "")
        self.assertIn("github-app credential", env["GIT_CONFIG_VALUE_2"])
        self.assertEqual(A.ghapp_git_env(env)["GIT_CONFIG_COUNT"], "4")  # idempotent
        r = subprocess.run(["git", "config", "--get-all", "credential.https://github.com.helper"], capture_output=True,
                           text=True, env={**os.environ, **env})
        self.assertIn("github-app credential", r.stdout)
        os.environ["AGENTCTL_IDENTITY"] = ""
        self.assertNotIn("GIT_CONFIG_COUNT", A.ghapp_git_env({}))

    def test_forge_github_reads_with_the_repos_app_token(self):
        os.environ["AGENTCTL_FORGE"] = "github"
        self.assertTrue(A.forge_env("pawelkrupinski/movies")["GH_TOKEN"].startswith("ghs_11_movies_"))
        self.assertTrue(A.forge_env("bitcashier/infra")["GH_TOKEN"].startswith("ghs_22_infra_"))
        os.environ["AGENTCTL_IDENTITY"] = ""
        os.environ["GH_TOKEN"] = "the owners"
        self.assertEqual(A.forge_env("bitcashier/infra")["GH_TOKEN"], "the owners")  # the laptop: gh's own auth

    def test_red_reads_on_ci_runner_need_the_reader_app_on_github(self):
        os.environ.update(AGENTCTL_FORGE="github", AGENTCTL_HOST="ci-runner")
        self.assertIsNone(A.red_read_token("bitcashier/infra"))
        os.environ.pop("AGENTCTL_GITHUB_READER_KEY_FILE")
        with self.assertRaises(A.NoReadAccess):
            A.red_read_token("bitcashier/infra")


if __name__ == "__main__":
    unittest.main()
