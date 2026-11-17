"""One credential path: the environment, then the processor's socket, then `secrets` (absolute path); fail closed."""
import json, os, subprocess, sys, tempfile, time, unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from test_agentctl import A, Base, TOOL  # noqa: E402

MOVIES = "pawelkrupinski/movies"
TOK = "tok-0123456789abcdef"


class TokenResolutionTest(Base):
    def setUp(self):
        super().setUp()
        self.saved = {k: os.environ.get(k) for k in ("GH_TOKEN", "AGENTCTL_STORE_TOKEN", "AGENTCTL_MOVIES_REPO", "AGENTCTL_INBOX_DIR", "PATH")}
        for k in ("GH_TOKEN", "AGENTCTL_STORE_TOKEN"):
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
        self.doCleanups()  # the served drop closes first: a hand-off it took must land before the environment is restored
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
        os.environ["AGENTCTL_STORE_TOKEN"] = "env-token-0123456789"
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

    def test_an_agents_own_land_reads_the_vault_itself_and_background_code_never_does(self):
        """2026-10-30: agents' `land` failed NoToken (no TTY in a Claude Code command, and the processor held none), so
        they wrapped it in `secrets run movies AGENTCTL_STORE_TOKEN --`. An agent's own command, in its session, may run
        `secrets`; anything agentctl started in the background (spawn_detached: AGENTCTL_DETACHED) never does."""
        marker = os.path.join(self.tmp.name, "ran")
        self.fake_secrets(f"touch {marker}; echo vault-token-0123456789")
        self.saved.update({k: os.environ.get(k) for k in ("CLAUDECODE", "CLAUDE_CODE_SESSION_ID", "AGENTCTL_DETACHED")})
        os.environ.pop("AGENTCTL_INTERACTIVE")
        os.environ.update(CLAUDECODE="1", CLAUDE_CODE_SESSION_ID="7aaaf20a-ffd4-4bdd-bc99-07b7f259075e",
                          AGENTCTL_DETACHED="1")
        with self.assertRaises(A.NoToken):
            A.token(MOVIES)
        self.assertFalse(os.path.exists(marker), "a detached agentctl process never runs secrets")
        os.environ.pop("AGENTCTL_DETACHED")
        self.assertEqual(A.token(MOVIES), "vault-token-0123456789")
        self.assertTrue(os.path.exists(marker))
        A._tokens.clear()
        A._IN_PROCESSOR[0] = True
        with self.assertRaises(A.NoToken):
            A.token(MOVIES)  # the processor: never, in a Claude session or not

    def test_spawn_detached_marks_its_child_as_background(self):
        out = os.path.join(self.tmp.name, "env.txt")
        with open(out, "w") as f:
            p = A.spawn_detached([sys.executable, "-c", "import os; print(os.environ.get('AGENTCTL_DETACHED'))"],
                                 stdout=f, stdin=subprocess.DEVNULL, env={"PATH": "/usr/bin:/bin"})
            p.wait(30)
        self.assertEqual(open(out).read().strip(), "1")

    def test_a_submission_with_no_token_goes_to_the_outbox_for_the_sync(self):
        class NoTokStore:
            def submit(self, e):
                raise A.NoToken("agentctl: could not read the movies AGENTCTL_STORE_TOKEN (test)")
        r = A.submit_remote(NoTokStore(), {"id": "20261030-x-abc"})
        self.assertIn("AGENTCTL_STORE_TOKEN", r["outbox"])
        self.assertTrue(os.path.exists(os.path.join(A.sync_outbox_dir(), "20261030-x-abc.json")))

    def test_processor_without_token_falls_through_to_secrets(self):
        self.serve(tok=None)
        self.fake_secrets("echo vault-token-0123456789")
        self.assertEqual(A.token(MOVIES), "vault-token-0123456789")

    def test_a_vault_read_re_seeds_a_tokenless_processor(self):
        """2026-11-15: a restarted processor held no token 16:58-18:36 while agents' own commands read the vault
        (ci-why, inbox in their sessions) -- only `land` / `land status` handed it over. Any vault read now does."""
        self.serve(tok=None)
        self.fake_secrets("echo vault-token-0123456789")
        self.assertEqual(A.token(MOVIES), "vault-token-0123456789")
        deadline = time.time() + 5
        while os.environ.get("AGENTCTL_STORE_TOKEN") != "vault-token-0123456789" and time.time() < deadline:
            time.sleep(0.05)
        self.assertEqual(os.environ.get("AGENTCTL_STORE_TOKEN"), "vault-token-0123456789", "handed to the processor")

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
        self.assertIn("run `secrets unlock bitcashier` in any session", entries[0]["verdict"])

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
            # the main thread never calls nap/take: only the server thread can answer
            self.assertEqual(A.token_request(drop.path), TOK)
        finally:
            os.environ.pop("AGENTCTL_SOCK_DIR") if saved is None else os.environ.__setitem__("AGENTCTL_SOCK_DIR", saved)


class TokenRequestRaceTest(unittest.TestCase):
    def test_the_answer_is_read_even_when_the_processor_closed_before_the_half_close(self):
        # the race the socket test lost under load: the server answers and closes before the client's shutdown()
        import socket, threading
        path = os.path.join(tempfile.mkdtemp(dir=os.environ["AGENTCTL_TEST_RUN_SOCK"], prefix="s-"), "t.sock")
        srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        srv.bind(path)
        srv.listen(1)
        real = socket.socket.shutdown

        def late_shutdown(sock, how):  # the client is descheduled: the server has answered and closed by now
            done.wait(5)
            time.sleep(0.2)
            return real(sock, how)
        done = threading.Event()

        def serve():
            conn, _ = srv.accept()
            conn.recv(4096)
            conn.sendall(TOK.encode())
            conn.close()
            done.set()
        threading.Thread(target=serve, daemon=True).start()
        socket.socket.shutdown = late_shutdown
        try:
            self.assertEqual(A.token_request(path), TOK)
        finally:
            socket.socket.shutdown = real
            srv.close()


class TokenNoteRecursionTest(unittest.TestCase):
    def test_noting_a_missing_token_on_a_remote_store_does_not_recurse(self):
        # the sync's store is remote: noting the missing token calls the store, which reads the token, which is missing
        calls, saved = [], A.open_condition

        def needs_token(*a, **k):
            calls.append(1)
            A.token_unavailable_note("movies", "again")
            raise SystemExit("no token")
        A.open_condition = needs_token
        try:
            A.token_unavailable_note("movies", "none")
        finally:
            A.open_condition = saved
        self.assertEqual(calls, [1])
        self.assertFalse(A._noting_token[0])


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
        self.addCleanup(setattr, A, "PROC_ROOT", A.PROC_ROOT)
        A.PROC_ROOT = os.path.join(tempfile.gettempdir(), "no-proc-here")  # the lsof route (macOS), on Linux too
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


class TokenSaidOnTransitionTest(unittest.TestCase):
    """2026-10-30: ~842,000 log lines of the missing token, and a hand-over line from every interactive command."""

    def setUp(self):
        import io
        self.saved = A.token_unavailable_note, A.interactive_foreground, sys.stderr
        self.notes, self.err = [], io.StringIO()
        A.token_unavailable_note = lambda ns, why: self.notes.append(why)
        A.interactive_foreground = lambda: False
        sys.stderr = self.err
        A._token_missing.clear()

    def tearDown(self):
        A.token_unavailable_note, A.interactive_foreground, sys.stderr = self.saved
        A._token_missing.clear()

    def lines(self):
        return [l for l in self.err.getvalue().splitlines() if l]

    def test_missing_is_said_once_then_a_summary_then_back_once(self):
        for i in range(50):
            A.token_missing("movies", "locked", now_=1000 + i)
        self.assertEqual(len(self.lines()), 1, self.lines())
        self.assertIn("the movies token is missing (locked)", self.lines()[0])
        self.assertEqual(self.notes, ["locked"])  # the inbox condition: on the transition, not per read
        A.token_missing("movies", "locked", now_=1000 + 600)
        self.assertEqual(len(self.lines()), 2)
        self.assertIn("still missing: 51 read(s) failed", self.lines()[1])
        A.token_missing("movies", "locked", now_=1000 + 700)
        self.assertEqual(len(self.lines()), 2)
        A.token_back("movies")
        A.token_back("movies")
        self.assertEqual(len(self.lines()), 3)
        self.assertIn("is back (52 read(s) failed", self.lines()[2])
        A.token_missing("movies", "locked again", now_=5000)  # a new transition: said again
        self.assertIn("is missing (locked again)", self.lines()[3])

    def test_a_step_without_token_is_quiet(self):
        def step():
            raise A.NoToken("agentctl: could not read the movies AGENTCTL_STORE_TOKEN (test)")
        self.assertFalse(A.run_step("t-notoken", step))
        self.assertEqual(self.lines(), [])
        self.assertFalse(A.run_step("t-other", lambda: 1 / 0))
        self.assertEqual(len(self.lines()), 1)
        self.assertIn("t-other failed", self.lines()[0])

    def test_the_token_is_handed_over_only_when_the_processor_lacks_it(self):
        base = tempfile.mkdtemp(dir=os.environ["AGENTCTL_TEST_RUN_SOCK"], prefix="s-")
        saved = {k: os.environ.get(k) for k in ("AGENTCTL_SOCK_DIR", "AGENTCTL_STORE_TOKEN", "GH_TOKEN")}
        saved_ws, saved_tok, saved_slug = A.waiting_state, A.token, A.slug
        os.environ["AGENTCTL_SOCK_DIR"] = base
        os.environ.pop("GH_TOKEN", None)
        os.environ.pop("AGENTCTL_STORE_TOKEN", None)
        d = tempfile.mkdtemp()
        A.waiting_state = lambda repo: (d, None, None)
        A.slug = lambda repo: MOVIES
        A.token = lambda o_r: TOK
        try:
            drop = A.TokenDrop(d, MOVIES).open()
            self.addCleanup(drop.close)
            self.assertTrue(A.offer_token("/x"))  # it holds none: handed over
            deadline = time.time() + 5
            while os.environ.get("AGENTCTL_STORE_TOKEN") != TOK and time.time() < deadline:
                time.sleep(0.05)
            self.assertEqual(os.environ.get("AGENTCTL_STORE_TOKEN"), TOK)
            for _ in range(5):
                self.assertFalse(A.offer_token("/x"), "it holds this token: nothing to hand over")
            handed = [l for l in self.lines() if "handed over" in l]
            self.assertEqual(len(handed), 1, self.lines())
        finally:
            A.waiting_state, A.token, A.slug = saved_ws, saved_tok, saved_slug
            for k, v in saved.items():
                os.environ.pop(k, None) if v is None else os.environ.__setitem__(k, v)
