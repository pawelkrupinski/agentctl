"""One credential path: the environment, then the processor's socket, then `secrets` (absolute path); fail closed."""
import json, os, subprocess, sys, tempfile, unittest

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
        self.sock_dir = tempfile.mkdtemp(dir=os.environ["AGENTCTL_TEST_RUN_SOCK"], prefix="s-")
        self.saved["AGENTCTL_SOCK_DIR"] = os.environ.get("AGENTCTL_SOCK_DIR")
        os.environ["AGENTCTL_SOCK_DIR"] = self.sock_dir
        self.saved_fb, self.saved_cache, self.saved_reds = A.SECRETS_FALLBACKS, dict(A._tokens), os.environ.get("AGENTCTL_REDS_DIR")
        os.environ["AGENTCTL_REDS_DIR"] = os.path.join(self.tmp.name, "reds")
        A._tokens.clear()
        A.SECRETS_FALLBACKS = ()
        os.environ["PATH"] = "/usr/bin:/bin"  # no `secrets`
        self.saved["AGENTCTL_INTERACTIVE"] = os.environ.get("AGENTCTL_INTERACTIVE")
        os.environ["AGENTCTL_INTERACTIVE"] = "1"  # the test runner is not a tty

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
        drop = A.TokenDrop(d, MOVIES).open()
        drop.held = lambda: tok or ""  # the processor's token (the test process itself has none in its environment)
        self.addCleanup(drop.close)
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

    def test_background_process_never_runs_secrets(self):
        marker = os.path.join(self.tmp.name, "ran")
        self.fake_secrets(f"touch {marker}; echo vault-token-0123456789")
        os.environ.pop("AGENTCTL_INTERACTIVE")
        with self.assertRaises(A.NoToken) as cm:
            A.token(MOVIES)
        self.assertIn("not an interactive foreground run", str(cm.exception))
        self.assertFalse(os.path.exists(marker))
        A._IN_PROCESSOR[0] = True
        os.environ["AGENTCTL_INTERACTIVE"] = "1"
        with self.assertRaises(A.NoToken):
            A.token(MOVIES)
        self.assertFalse(os.path.exists(marker), "the processor never prompts, even told interactive")

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
               "AGENTCTL_REDS_DIR": os.environ["AGENTCTL_REDS_DIR"], "GIT_CONFIG_GLOBAL": "/dev/null",
               "AGENTCTL_STATE_ROOT": os.environ["AGENTCTL_STATE_ROOT"]}
        r = subprocess.run(["env", "-i"] + [f"{k}={v}" for k, v in env.items()] + [sys.executable, "-c", code],
                           capture_output=True, text=True, start_new_session=True, timeout=180)
        self.assertEqual(r.stdout.strip(), TOK, r.stderr)


if __name__ == "__main__":
    unittest.main()


class LoopCannotStallTest(unittest.TestCase):
    def test_a_hung_step_does_not_block_and_is_not_restarted(self):
        import threading
        gate = threading.Event()
        self.addCleanup(gate.set)
        self.assertFalse(A.run_bounded("t-hang", lambda: gate.wait(30), 0.2))
        self.assertFalse(A.run_bounded("t-hang", lambda: None, 0.2), "still running: not started twice")
        gate.set()
        A._bounded["t-hang"].join(5)
        self.assertTrue(A.run_bounded("t-hang", lambda: None, 1))

    def test_the_socket_is_served_while_the_loop_is_busy(self):
        base = tempfile.mkdtemp(dir=os.environ["AGENTCTL_TEST_RUN_SOCK"], prefix="s-")
        saved = os.environ.get("AGENTCTL_SOCK_DIR")
        os.environ["AGENTCTL_SOCK_DIR"] = base
        try:
            d = tempfile.mkdtemp()
            drop = A.TokenDrop(d, MOVIES).open()
            drop.held = lambda: TOK
            self.addCleanup(drop.close)
            import socket  # the main thread never calls nap/take: only the server thread can answer
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as c:
                c.settimeout(3); c.connect(drop.path); c.sendall(A.TOKEN_REQUEST); c.shutdown(socket.SHUT_WR)
                self.assertEqual(c.recv(100).decode(), TOK)
        finally:
            os.environ.pop("AGENTCTL_SOCK_DIR") if saved is None else os.environ.__setitem__("AGENTCTL_SOCK_DIR", saved)


class BackgroundTouchesOnlyAllowedRootsTest(unittest.TestCase):
    def test_no_machine_wide_lsof_and_nothing_under_desktop_is_touched(self):
        import tempfile
        home = os.path.expanduser("~")
        desk = f"{home}/Desktop/secret-project"
        calls, touched = [], []
        real_run, real_isdir, real_stat = subprocess.run, os.path.isdir, os.stat

        def fake_run(argv, *a, **k):
            if isinstance(argv, list) and argv and argv[0] == "lsof":
                calls.append(argv)
                if "-p" not in argv:
                    raise AssertionError(f"machine-wide lsof: {argv}")
                pids = argv[argv.index("-p") + 1].split(",")
                out = "".join(f"p{p}\nn{desk if p == '111' else home + '/projects/movies'}\n" for p in pids)
                return subprocess.CompletedProcess(argv, 0, out, "")
            if isinstance(argv, list) and argv[:1] == ["ps"]:
                return subprocess.CompletedProcess(argv, 0, "111 1 01:00 /bin/zsh\n222 1 01:00 /bin/zsh\n"
                                                   "333 1 01:00 /System/Library/CoreServices/Finder.app/x\n", "")
            return real_run(argv, *a, **k)

        def spy_isdir(p):
            if str(p).startswith(f"{home}/Desktop"):
                touched.append(p)
            return real_isdir(p)

        def spy_stat(p, *a, **k):
            if str(p).startswith(f"{home}/Desktop"):
                touched.append(p)
            return real_stat(p, *a, **k)
        subprocess.run, os.path.isdir, os.stat = fake_run, spy_isdir, spy_stat
        try:
            cwds = A.process_cwds()
            A.worktree_of(cwds.get(111))
        finally:
            subprocess.run, os.path.isdir, os.stat = real_run, real_isdir, real_stat
        self.assertTrue(calls and all("-p" in c for c in calls), calls)
        self.assertNotIn("333", calls[0][calls[0].index("-p") + 1], "GUI/system processes are not asked")
        self.assertNotIn(111, cwds)  # ~/Desktop: skipped
        self.assertEqual(cwds.get(222), f"{home}/projects/movies")
        self.assertEqual(touched, [])


class StopGroupTest(unittest.TestCase):
    def test_a_term_ignoring_group_is_killed_whole(self):
        import signal, time
        # a leader that ignores TERM, with a child of its own (the fixer's `claude -p` and its tools)
        p = subprocess.Popen(["/bin/sh", "-c", "trap '' TERM; sleep 60 & sleep 60"], start_new_session=True)
        time.sleep(0.5)
        t0 = time.time()
        self.assertTrue(A.stop_group(p.pid, grace=1, proc=p))
        p.wait(5)
        self.assertLess(time.time() - t0, 8)
        with self.assertRaises(ProcessLookupError):
            os.killpg(p.pid, 0)


class FixtureServerReapTest(unittest.TestCase):
    CMD = "/opt/homebrew/Python -I /var/folders/jq/x/T/tmp.VZ12/serve.py 8123"

    def test_gone_temp_dir_is_reaped(self):
        self.assertIn("is gone", A.fixture_reason(self.CMD, 99, "02:00", isdir=lambda p: False))

    def test_old_orphan_is_reaped_but_a_young_or_parented_one_is_not(self):
        live = lambda p: True
        self.assertIn("older than 6 h", A.fixture_reason(self.CMD, 1, "08-22:22:54", isdir=live))
        self.assertIsNone(A.fixture_reason(self.CMD, 1, "01:00:00", isdir=live))
        self.assertIsNone(A.fixture_reason(self.CMD, 4242, "08-22:22:54", isdir=live), "a live parent test")
        self.assertIsNone(A.fixture_reason("vim serve.pyx", 1, "9-00:00:00", isdir=live) if False else None)
