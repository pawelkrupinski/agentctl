"""One credential path: the environment, then the processor's socket, then `secrets` (absolute path); fail closed."""
import json, os, stat, subprocess, sys, tempfile, threading, unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from test_agentctl import A, Base, TOOL  # noqa: E402

MOVIES = "pawelkrupinski/movies"
TOK = "tok-0123456789abcdef"


class TokenResolutionTest(Base):
    def setUp(self):
        super().setUp()
        self.saved = {k: os.environ.get(k) for k in ("GH_TOKEN", "FORGEJO_API_TOKEN", "AGENTCTL_MOVIES_REPO", "AGENTCTL_INBOX_DIR", "PATH")}
        for k in ("GH_TOKEN", "FORGEJO_API_TOKEN"):
            os.environ.pop(k, None)
        os.environ["AGENTCTL_MOVIES_REPO"] = self.r.path
        os.environ["AGENTCTL_INBOX_DIR"] = os.path.join(self.tmp.name, "inbox")
        self.sock_dir = tempfile.mkdtemp(dir="/tmp", prefix="agentctl-sock-")
        self.saved["AGENTCTL_SOCK_DIR"] = os.environ.get("AGENTCTL_SOCK_DIR")
        os.environ["AGENTCTL_SOCK_DIR"] = self.sock_dir
        self.saved_fb, self.saved_cache, self.saved_reds = A.SECRETS_FALLBACKS, dict(A._tokens), os.environ.get("AGENTCTL_REDS_DIR")
        os.environ["AGENTCTL_REDS_DIR"] = os.path.join(self.tmp.name, "reds")
        A._tokens.clear()
        A.SECRETS_FALLBACKS = ()
        os.environ["PATH"] = "/usr/bin:/bin"  # no `secrets`

    def tearDown(self):
        A.SECRETS_FALLBACKS = self.saved_fb
        A._tokens.clear()
        A._tokens.update(self.saved_cache)
        A._IN_PROCESSOR[0] = False
        for k, v in list(self.saved.items()) + [("AGENTCTL_REDS_DIR", self.saved_reds)]:
            os.environ.pop(k, None) if v is None else os.environ.__setitem__(k, v)
        super().tearDown()

    def fake_secrets(self, body):
        p = os.path.join(self.tmp.name, "secrets")
        open(p, "w").write("#!/bin/sh\n" + body + "\n")
        os.chmod(p, 0o755)
        A.SECRETS_FALLBACKS = (p,)  # an absolute path, not on PATH

    def serve(self, tok=TOK):
        d, _c, _w = A.waiting_state(self.r.path)
        if tok:
            os.environ["FORGEJO_API_TOKEN"] = tok
        drop = A.TokenDrop(d, MOVIES).open()
        if tok:
            os.environ.pop("FORGEJO_API_TOKEN")  # the server holds it; the client has no environment
        stop = threading.Event()

        def loop():
            while not stop.is_set():
                if tok:
                    os.environ["FORGEJO_API_TOKEN"] = tok
                drop.nap(0.2)
                os.environ.pop("FORGEJO_API_TOKEN", None)
        t = threading.Thread(target=loop, daemon=True)
        t.start()

        def done():
            stop.set(); t.join(); drop.close()
        self.addCleanup(done)
        return drop

    def test_1_environment_wins(self):
        os.environ["FORGEJO_API_TOKEN"] = "env-token-0123456789"
        self.fake_secrets("echo vault-token-0123456789")
        self.assertEqual(A.token(MOVIES), "env-token-0123456789")

    def test_2_processor_socket(self):
        self.serve()
        self.fake_secrets("exit 1")
        self.assertEqual(A.token(MOVIES), TOK)

    def test_3_secrets_by_absolute_path(self):
        self.fake_secrets("echo vault-token-0123456789")
        self.assertEqual(A.token(MOVIES), "vault-token-0123456789")

    def test_processor_without_token_falls_through_to_secrets(self):
        self.serve(tok=None)
        self.fake_secrets("echo vault-token-0123456789")
        self.assertEqual(A.token(MOVIES), "vault-token-0123456789")

    def test_the_processor_never_asks_its_own_socket(self):
        self.serve()
        A._IN_PROCESSOR[0] = True
        self.assertIsNone(A.token_from_processor(MOVIES))
        A._IN_PROCESSOR[0] = False
        self.assertEqual(A.token_from_processor(MOVIES), TOK)

    def test_no_source_fails_closed_naming_the_cause_with_one_inbox_entry(self):
        for _ in range(2):
            with self.assertRaises(A.NoToken) as cm:
                A.token(MOVIES)
        msg = str(cm.exception)
        self.assertIn("no `secrets` command found", msg)
        self.assertIn("processor holds none", msg)
        inbox = os.environ["AGENTCTL_INBOX_DIR"]
        entries = [json.load(open(os.path.join(inbox, f))) for f in os.listdir(inbox)] if os.path.isdir(inbox) else []
        self.assertEqual(len(entries), 1, entries)
        self.assertIn("run `secrets unlock movies` in any session", entries[0]["verdict"])

    def test_detached_env_i_run_succeeds_through_the_socket(self):
        self.serve()
        code = ("import importlib.machinery as m, importlib.util as u, sys\n"
                f"l = m.SourceFileLoader('agentctl', {TOOL!r}); s = u.spec_from_loader('agentctl', l)\n"
                "A = u.module_from_spec(s); l.exec_module(A)\n"
                f"print(A.token({MOVIES!r}))\n")
        env = {"HOME": os.path.expanduser("~"), "PATH": "/usr/bin:/bin", "AGENTCTL_SOCK_DIR": self.sock_dir,
               "AGENTCTL_MOVIES_REPO": self.r.path, "AGENTCTL_INBOX_DIR": os.environ["AGENTCTL_INBOX_DIR"],
               "AGENTCTL_REDS_DIR": os.environ["AGENTCTL_REDS_DIR"], "GIT_CONFIG_GLOBAL": "/dev/null"}
        r = subprocess.run(["env", "-i"] + [f"{k}={v}" for k, v in env.items()] + [sys.executable, "-c", code],
                           capture_output=True, text=True, start_new_session=True, timeout=30)
        self.assertEqual(r.stdout.strip(), TOK, r.stderr)


if __name__ == "__main__":
    unittest.main()
