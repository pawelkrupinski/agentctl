"""Every long-running agentctl mode is OWNED: one flock per (kind, key) that the kernel frees when the process dies, a
record of owner, pid, start time and code version beside it, the CI runs it started cancelled when it ends (by its signal
handler, or by the next owner after a SIGKILL), a restart when agentctl's file changes on disk, and `orphans` naming
whatever is left over."""
import json, os, shutil, signal, subprocess, sys, tempfile, time, unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from test_agentctl import A, TOOL  # noqa: E402

FAKE = os.path.join(HERE, "fake_forgejo_gh.py")

HOLDER = """
import importlib.machinery, importlib.util, sys, time
loader = importlib.machinery.SourceFileLoader("agentctl", sys.argv[1])
spec = importlib.util.spec_from_loader("agentctl", loader)
A = importlib.util.module_from_spec(spec)
loader.exec_module(A)
with A.Owned("test", "k1", argv=["test"]) as o:
    o.add_run("o/r", 77)
    print("held", flush=True)
    while True:
        time.sleep(0.1)
"""


class OwnedTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.gh = os.path.join(self.tmp.name, "gh")
        os.makedirs(self.gh)
        self.env = {"AGENTCTL_OWNERS_DIR": os.path.join(self.tmp.name, "owners"), "AGENTCTL_FORGEJO_GH": FAKE,
                    "FAKE_GH_DIR": self.gh, "GH_TOKEN": "dummy", "AGENTCTL_SCRATCH": os.path.join(self.tmp.name, "s"),
                    "AGENTCTL_MIN_INTERVAL": "0.2", "AGENTCTL_ATTACH_POLL": "0.2", "AGENTCTL_RESTART_CHECK": "0.2"}
        self.saved = {k: os.environ.get(k) for k in self.env}
        os.environ.update(self.env)
        self.saved_gh, A.FORGEJO_GH = A.FORGEJO_GH, FAKE
        self.procs = []

    def tearDown(self):
        for p in self.procs:
            if p.poll() is None:
                p.kill()
                p.wait()
        A.FORGEJO_GH = self.saved_gh
        for k, v in self.saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        self.tmp.cleanup()

    def cancels(self):
        p = os.path.join(self.gh, "calls.log")
        return [l for l in open(p).read().splitlines() if "run cancel" in l] if os.path.exists(p) else []

    def holder(self, tool=TOOL):
        p = subprocess.Popen([sys.executable, "-c", HOLDER, tool], stdout=subprocess.PIPE, text=True,
                             env=dict(os.environ))
        self.procs.append(p)
        self.assertEqual(p.stdout.readline().strip(), "held")
        return p

    def until(self, cond, timeout=15):
        t = time.time() + timeout
        while time.time() < t:
            if cond():
                return True
            time.sleep(0.05)
        return False

    def test_one_owner_per_kind_and_key(self):
        a = A.Owned("test", "k1", argv=["x"])
        self.assertTrue(a.acquire())
        b = A.Owned("test", "k1", argv=["x"])
        self.assertFalse(b.acquire())
        self.assertEqual(b.holder["pid"], os.getpid())
        self.assertTrue(A.Owned("test", "k2", argv=["x"]).acquire(), "another key is another owner")
        rec = json.load(open(a.record))
        for field in ("kind", "key", "pid", "started", "process_start", "owner", "argv", "code"):
            self.assertIn(field, rec)
        a.release()
        self.assertTrue(b.acquire(), "released: the next one takes it")
        b.release()

    def test_the_kernel_frees_the_lock_and_the_next_owner_cancels_a_killed_ones_runs(self):
        p = self.holder()
        self.assertFalse(A.Owned("test", "k1", argv=["x"]).acquire())
        p.send_signal(signal.SIGKILL)
        p.wait()
        self.assertEqual(self.cancels(), [], "SIGKILL runs no handler")
        o = A.Owned("test", "k1", argv=["x"])
        self.assertTrue(o.acquire())
        self.assertEqual(len(self.cancels()), 1)
        self.assertIn("run cancel 77", self.cancels()[0])
        o.release()

    def test_sigterm_cancels_its_own_runs_and_drops_its_record(self):
        p = self.holder()
        rec = A.Owned("test", "k1", argv=["x"]).record
        p.send_signal(signal.SIGTERM)
        p.wait(timeout=15)
        self.assertEqual(len(self.cancels()), 1)
        self.assertIn("run cancel 77", self.cancels()[0])
        self.assertFalse(os.path.exists(rec))

    def test_a_finished_run_is_not_cancelled(self):
        with A.Owned("test", "k3", argv=["x"]) as o:
            o.add_run("o/r", 5)
            o.drop_run(5)
        self.assertEqual(self.cancels(), [])

    def test_a_held_owner_raises_with_the_holder(self):
        with A.Owned("test", "k4", argv=["x"]):
            with self.assertRaises(A.Held) as e:
                with A.Owned("test", "k4", argv=["x"]):
                    pass
            self.assertEqual(e.exception.holder["pid"], os.getpid())

    def test_restarts_on_the_new_code_when_agentctl_changes_on_disk(self):
        tool = os.path.join(self.tmp.name, "agentctl")
        shutil.copy(TOOL, tool)
        target = os.path.join(self.tmp.name, "target")
        p = subprocess.Popen([sys.executable, tool, "wait", "file", target, "--interval", "1", "--timeout", "60"],
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=dict(os.environ))
        self.procs.append(p)
        self.assertTrue(self.until(lambda: A.owner_records()), "the waiter records itself")
        first = A.owner_records()[0]
        with open(tool, "a") as f:
            f.write("\n# a fix landed\n")
        self.assertTrue(self.until(lambda: any(r["code"] != first["code"] for r in A.owner_records())),
                        "it re-execs on the new file")
        open(target, "w").close()
        out, errs = p.communicate(timeout=30)
        self.assertEqual(p.returncode, 0, errs)
        self.assertIn("agentctl changed on disk", errs)
        self.assertEqual(A.owner_records(), [], "and leaves no record behind")

    def test_a_restart_resumes_the_owners_own_command_not_the_processs(self):
        """land's watch restarts as `agentctl watch ...`, never as `land` again (which found the sha on main, printed
        LANDED a second time and exited 0 with no WATCH verdict, 2026-10-27)."""
        execs = []
        saved = A.os.execv, A.START_CODE, A.sys.argv
        A.os.execv = lambda exe, args: execs.append(args)
        A.START_CODE, A.sys.argv = "an-older-agentctl", ["agentctl", "land"]
        A._last_code_check[0] = 0.0
        try:
            o = A.Owned("watch", "o_r-sha", ["watch", "--sha", "abc"])
            self.assertTrue(o.acquire())
            A.restart_if_changed([o])
        finally:
            A.os.execv, A.START_CODE, A.sys.argv = saved
            A._last_code_check[0] = 0.0
            o.release()
        self.assertEqual(len(execs), 1)
        self.assertEqual(execs[0][2:], ["watch", "--sha", "abc"])

    def test_orphans_names_a_dead_owners_record_and_an_owner_on_old_code(self):
        d = os.environ["AGENTCTL_OWNERS_DIR"]
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, "test-dead.json"), "w") as f:
            json.dump({"kind": "test", "key": "dead", "pid": 999999, "started": "then", "process_start": "x",
                       "owner": "gone-agent", "argv": ["x"], "code": "abc", "runs": [["o/r", 9]]}, f)
        p = self.holder()
        rec = A.Owned("test", "k1", argv=["x"]).record
        r = json.load(open(rec))
        r["code"] = "an-older-agentctl"
        with open(rec, "w") as f:
            json.dump(r, f)
        rc, out = self.tool("orphans")
        self.assertIn("dead", out)
        self.assertIn("process gone", out)
        self.assertIn("runs 9", out)
        self.assertIn(f"pid {p.pid}", out)
        self.assertIn("old code", out)
        rc, out = self.tool("orphans", "--kill")
        self.assertTrue(self.until(lambda: p.poll() is not None))
        self.assertFalse(os.path.exists(os.path.join(d, "test-dead.json")))
        self.assertTrue(any("run cancel 9" in c for c in self.cancels()), "the dead owner's runs are cancelled")

    def tool(self, *argv):
        import contextlib, io
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
            rc = A.main(list(argv))
        return rc, out.getvalue()


if __name__ == "__main__":
    unittest.main()
