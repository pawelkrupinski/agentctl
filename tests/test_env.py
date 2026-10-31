"""agentctl env: export lines only, one clear line on a locked vault, never unlock, never print a token."""
import contextlib, http.server, io, json, os, socket, stat, sys, tempfile, threading, unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from test_agentctl import A  # noqa: E402

TOKEN = "SECRETTOKEN123"


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class EnvTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.bin = os.path.join(self.tmp.name, "bin")
        os.makedirs(self.bin)
        self.calls = os.path.join(self.tmp.name, "secrets.calls")
        p = os.path.join(self.bin, "secrets")
        with open(p, "w") as f:
            f.write(f'#!/bin/sh\necho "$*" >> {self.calls}\ncase "$1" in status) echo "${{SECRETS_STATE:-locked}}" ;; get) echo {TOKEN} ;; '
                    'unlock) echo UNLOCK-CALLED ;; esac\n')
        os.chmod(p, os.stat(p).st_mode | stat.S_IEXEC)
        self.saved = {k: os.environ.get(k) for k in ("PATH", "SECRETS_STATE")}
        os.environ["PATH"] = self.bin + ":" + os.environ["PATH"]
        self.saved_ports = A.TUNNEL_PORTS

    def tearDown(self):
        for k, v in self.saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        A.TUNNEL_PORTS = self.saved_ports
        self.tmp.cleanup()

    def tool(self, *argv):
        out, errb = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(errb):
            rc = A.main(list(argv))
        return rc, out.getvalue(), errb.getvalue()

    def secrets_calls(self):
        if not os.path.exists(self.calls):
            return []
        with open(self.calls) as f:
            return f.read().split("\n")

    def test_print_is_only_exports_and_says_locked_once(self):
        os.environ["SECRETS_STATE"] = "locked"
        rc, out, errs = self.tool("env", "--print", "--ns", "movies")
        self.assertEqual(rc, 1)
        self.assertTrue(all(l.startswith("export ") for l in out.splitlines()), out)
        self.assertEqual(errs.strip(), "vault movies locked: ask the user to run `! secrets unlock movies`")
        self.assertFalse(any(c.startswith("unlock") or c.startswith("get") for c in self.secrets_calls()))

    def test_print_unlocked_exit_zero_no_token(self):
        os.environ["SECRETS_STATE"] = "unlocked"
        rc, out, errs = self.tool("env", "--print", "--ns", "bitcashier")
        self.assertEqual(rc, 0, errs)
        self.assertNotIn(TOKEN, out + errs)
        for b in A.SDKMAN_BINS:
            if os.path.isdir(b):
                self.assertIn(b, out)

    def test_check_locked_is_one_line(self):
        os.environ["SECRETS_STATE"] = "locked"
        rc, out, _ = self.tool("env", "--check", "--ns", "movies")
        self.assertEqual(rc, 1)
        self.assertEqual(out.strip().splitlines(), ["vault movies locked: ask the user to run `! secrets unlock movies`"])
        self.assertFalse(any(c.startswith("unlock") for c in self.secrets_calls()))

    def test_check_tunnel_down_and_no_token_read(self):
        os.environ["SECRETS_STATE"] = "unlocked"
        A.TUNNEL_PORTS = [(free_port(), "agentctl store")]
        rc, out, errs = self.tool("env", "--check", "--ns", "movies")
        self.assertEqual(rc, 1, out)
        self.assertIn("agentctl store): REFUSED", out)
        self.assertIn(A.TUNNEL_LABEL, out)
        self.assertIn("github: gh", out)
        self.assertNotIn(TOKEN, out + errs)
        self.assertFalse(any(c.startswith("get") for c in self.secrets_calls()))


class EnvMutationTest(unittest.TestCase):
    def test_locked_test_kills_a_mutant_that_sees_every_vault_unlocked(self):
        real = A.vault_state
        A.vault_state = lambda ns: "unlocked"
        try:
            res = unittest.TextTestRunner(stream=io.StringIO(), verbosity=0).run(EnvTest("test_print_is_only_exports_and_says_locked_once"))
        finally:
            A.vault_state = real
        self.assertTrue(res.failures or res.errors, "mutant survived")


if __name__ == "__main__":
    unittest.main()
