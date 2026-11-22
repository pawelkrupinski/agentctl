"""Every long-running agentctl mode is OWNED: one flock per (kind, key) that the kernel frees when the process dies, a
record of owner, pid, start time and code version beside it, the CI runs it started cancelled when it ends (by its signal
handler, or by the next owner after a SIGKILL), a restart when agentctl's file changes on disk, and `orphans` naming
whatever is left over."""
import json, os, shutil, signal, subprocess, sys, tempfile, time, unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from test_agentctl import A, TOOL  # noqa: E402

FAKE = os.path.join(HERE, "fake_gh.py")

HOLDER = f"""
import sys, time
sys.path.insert(0, {HERE!r})
from agentctl_view import load
A = load(sys.argv[1])
with A.Owned("test", "k1", argv=["test"]) as o:
    print("held", flush=True)
    while True:
        time.sleep(0.1)
"""


class OwnedTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.gh = os.path.join(self.tmp.name, "gh")
        os.makedirs(self.gh)
        self.env = {"AGENTCTL_OWNERS_DIR": os.path.join(self.tmp.name, "owners"), "AGENTCTL_GH_CLI": FAKE,
                    "FAKE_GH_DIR": self.gh, "GH_TOKEN": "dummy", "AGENTCTL_SCRATCH": os.path.join(self.tmp.name, "s"),
                    "AGENTCTL_MIN_INTERVAL": "0.2", "AGENTCTL_ATTACH_POLL": "0.2", "AGENTCTL_RESTART_CHECK": "0.2"}
        self.saved = {k: os.environ.get(k) for k in self.env}
        os.environ.update(self.env)
        self.saved_gh, A.GH_CLI = A.GH_CLI, FAKE
        self.procs = []

    def tearDown(self):
        for p in self.procs:
            if p.poll() is None:
                p.kill()
                p.wait()
        A.GH_CLI = self.saved_gh
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

    def until(self, cond, timeout=180):
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

    def test_the_kernel_frees_the_lock_of_a_killed_owner(self):
        p = self.holder()
        self.assertFalse(A.Owned("test", "k1", argv=["x"]).acquire())
        p.send_signal(signal.SIGKILL)
        p.wait()
        o = A.Owned("test", "k1", argv=["x"])
        self.assertTrue(o.acquire())
        o.release()
        self.assertEqual(self.cancels(), [], "an owner cancels no run (d073975)")

    def test_sigterm_drops_its_record(self):
        p = self.holder()
        rec = A.Owned("test", "k1", argv=["x"]).record
        p.send_signal(signal.SIGTERM)
        p.wait(timeout=180)
        self.assertFalse(os.path.exists(rec))

    def test_the_signal_handlers_are_in_place_before_the_record_shows(self):
        """2026-11-22: test_inbox's signal test failed under load 80 (the pre-push of bd46924): its SIGTERM, sent once
        the owner record appeared, came before the handlers that turn it into an inbox entry were installed -- the
        record was written first -- and the process died with no verdict. Anyone who reads the record to signal the
        holder (orphans, cleanup) met the same window."""
        seen = []
        real = A.signal.signal

        def spy(sig, handler):
            seen.append(os.path.exists(rec))
            return real(sig, handler)

        o = A.Owned("test", "k-order", argv=["x"])
        rec = o.record
        A.signal.signal = spy
        try:
            with o:
                pass
        finally:
            A.signal.signal = real
        self.assertTrue(seen)
        self.assertFalse(any(seen[:3]), "a handler was installed after the record showed")

    def test_an_owner_in_a_removed_directory_still_records(self):
        """The owner string is the inbox's (caller_owner): a removed cwd (a watch closing its own worktree) reads as
        what it was, never an OSError out of the record (owned.py used os.getcwd() itself, inbox.py safe_cwd)."""
        here, gone = os.getcwd(), tempfile.mkdtemp()
        saved = os.environ.pop("CLAUDE_CODE_SESSION_ID", None)
        try:
            os.chdir(gone)
            os.rmdir(gone)
            with A.Owned("test", "k-gone", argv=["x"]) as o:
                rec = A.read_record(o.record)
            self.assertEqual(rec["owner"], A.caller_owner())
            self.assertTrue(rec["owner"].endswith("@" + rec["cwd"]))
        finally:
            os.chdir(here)
            if saved is not None:
                os.environ["CLAUDE_CODE_SESSION_ID"] = saved

    def test_a_held_owner_raises_with_the_holder(self):
        with A.Owned("test", "k4", argv=["x"]):
            with self.assertRaises(A.Held) as e:
                with A.Owned("test", "k4", argv=["x"]):
                    pass
            self.assertEqual(e.exception.holder["pid"], os.getpid())

    def test_restarts_on_the_new_code_when_agentctl_changes_on_disk(self):
        tool = os.path.join(self.tmp.name, "agentctl")
        shutil.copy(TOOL, tool)
        shutil.copytree(os.path.join(os.path.dirname(TOOL), "lib"), os.path.join(self.tmp.name, "lib"))  # its package
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
        out, errs = p.communicate(timeout=180)
        self.assertEqual(p.returncode, 0, errs)
        self.assertIn("agentctl changed on disk", errs)
        self.assertEqual(A.owner_records(), [], "and leaves no record behind")

    def test_a_restart_resumes_the_owners_own_command_not_the_processs(self):
        """land's watch restarts as `agentctl watch ...`, never as `land` again (which found the sha on main, printed
        LANDED a second time and exited 0 with no WATCH verdict, 2026-10-27)."""
        execs = []
        saved = A.os.execv, A.START_CODE, A.sys.argv
        A.os.execv = lambda exe, args: execs.append(args)
        gate, A.restart_gate = A.restart_gate, lambda: (True, "")
        self.addCleanup(setattr, A, "restart_gate", gate)
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
                       "owner": "gone-agent", "argv": ["x"], "code": "abc"}, f)
        p = self.holder()
        rec = A.Owned("test", "k1", argv=["x"]).record
        r = json.load(open(rec))
        r["code"] = "an-older-agentctl"
        with open(rec, "w") as f:
            json.dump(r, f)
        rc, out = self.tool("orphans")
        self.assertIn("dead", out)
        self.assertIn("process gone", out)
        self.assertIn(f"pid {p.pid}", out)
        self.assertIn("old code", out)
        rc, out = self.tool("orphans", "--kill")
        self.assertTrue(self.until(lambda: p.poll() is not None))
        self.assertFalse(os.path.exists(os.path.join(d, "test-dead.json")))

    def test_the_safe_reaper_ends_strays_but_never_an_owner_on_old_code_and_logs_each(self):
        d = os.environ["AGENTCTL_OWNERS_DIR"]
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, "test-dead.json"), "w") as f:
            json.dump({"kind": "test", "key": "dead", "pid": 999999, "started": "then", "process_start": "x",
                       "owner": "gone-agent", "argv": ["x"], "code": "abc"}, f)
        p = self.holder()
        rec = A.Owned("test", "k1", argv=["x"]).record
        r = json.load(open(rec))
        r["code"] = "an-older-agentctl"
        with open(rec, "w") as f:
            json.dump(r, f)
        saved, A.REAP_LOG = A.REAP_LOG, os.path.join(self.tmp.name, "reap.log")
        try:
            rc, out = self.tool("orphans", "--kill", "--safe")
        finally:
            log, A.REAP_LOG = A.REAP_LOG, saved
        time.sleep(0.5)
        self.assertIsNone(p.poll(), "a live owner on old code re-execs itself: the reaper leaves it")
        self.assertFalse(os.path.exists(os.path.join(d, "test-dead.json")))
        self.assertIn("removed dead owner test dead pid 999999", open(log).read())

    def tool(self, *argv):
        import contextlib, io
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
            rc = A.main(list(argv))
        return rc, out.getvalue()


if __name__ == "__main__":
    unittest.main()


class CodeRevTest(unittest.TestCase):
    """The status line names the git revision the reconciler runs (2026-11-01: `code d1677f40` named no commit, and a
    running fix looked like one never picked up)."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()

    def tearDown(self):
        self.tmp.cleanup()

    def tool_in(self, root):
        os.makedirs(os.path.join(root, "lib", "agentctl"), exist_ok=True)
        with open(os.path.join(root, "agentctl"), "w") as f:
            f.write("#!/usr/bin/env python3\n")
        with open(os.path.join(root, "lib", "agentctl", "core.py"), "w") as f:
            f.write("x = 1\n")
        return os.path.join(root, "agentctl")

    def test_ci_runners_export_is_named_by_its_rev_through_the_current_symlink(self):
        rev = "4d0fc921c9e6fbc329bba40ba2d3c8ca79b9151f"
        self.tool_in(os.path.join(self.tmp.name, rev))
        os.symlink(rev, os.path.join(self.tmp.name, "current"))
        self.assertEqual(A.code_rev(os.path.join(self.tmp.name, "current", "agentctl")), "4d0fc921c9e6")

    def test_a_checkout_names_its_head_and_dirty_code(self):
        root = os.path.join(self.tmp.name, "bin")
        tool = self.tool_in(root)
        g = lambda *a: subprocess.run(["git", "-C", root, *a], capture_output=True, text=True, check=True).stdout.strip()
        g("init", "-q")
        g("add", "-A")
        g("-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "x")
        head = g("rev-parse", "--short=12", "HEAD")
        self.assertEqual(A.code_rev(tool), head)
        with open(os.path.join(root, "notes.txt"), "w") as f:
            f.write("not code\n")
        self.assertEqual(A.code_rev(tool), head)
        with open(os.path.join(root, "lib", "agentctl", "core.py"), "a") as f:
            f.write("y = 2\n")
        self.assertEqual(A.code_rev(tool), head + "+dirty")
        self.assertEqual(A.code_rev(os.path.join(self.tmp.name, "nowhere", "agentctl")), "")

    def test_the_status_says_the_rev_then_the_hash(self):
        self.assertEqual(A.code_text({"code": "d1677f40615fe544", "rev": "4d0fc921c9e6"}), "4d0fc921c9e6 (code d1677f40)")
        self.assertEqual(A.code_text({"code": "d1677f40615fe544"}), "d1677f40615fe544")
