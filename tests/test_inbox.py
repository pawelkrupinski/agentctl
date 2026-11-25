"""Every final verdict of land / watch / wait / waiting await goes to a durable inbox, and land's WAITING line too, so
the orchestrating session reads it whether or not the agent that ran the command is still alive."""
import contextlib, io, json, os, signal, subprocess, sys, tempfile, time, unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from test_agentctl import A, TOOL  # noqa: E402


class InboxTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.inbox = os.path.join(self.tmp.name, "inbox")
        self.saved = {k: os.environ.get(k) for k in ("AGENTCTL_INBOX_DIR", "AGENTCTL_OWNERS_DIR", "AGENTCTL_SCRATCH",
                                                     "CLAUDE_CODE_SESSION_ID")}
        # the entries are this test's own by its session, wherever it runs: `--ack` acks only the caller's, and outside
        # a git worktree (the suite from a non-git cwd) nothing else would name them the caller's
        os.environ.update(AGENTCTL_INBOX_DIR=self.inbox, AGENTCTL_OWNERS_DIR=os.path.join(self.tmp.name, "owners"),
                          AGENTCTL_SCRATCH=os.path.join(self.tmp.name, "s"), CLAUDE_CODE_SESSION_ID="test-inbox-session")

    def tearDown(self):
        for k, v in self.saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        self.tmp.cleanup()

    def entries(self):
        return A.inbox_entries()

    def run_main(self, argv):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = A.main(argv)
        return code, out.getvalue()

    def test_a_wait_writes_its_verdict(self):
        flag = os.path.join(self.tmp.name, "done.flag")
        open(flag, "w").write("ok\n")
        code, out = self.run_main(["wait", "file", flag, "--timeout", "5"])
        self.assertEqual(code, 0)
        [e] = self.entries()
        self.assertEqual(e["command"], "wait")
        self.assertEqual(e["exit"], 0)
        self.assertTrue(e["final"])
        self.assertTrue(e["verdict"].startswith(f"file {flag}: present"), e["verdict"])
        self.assertFalse(e["read"])

    def test_a_refused_land_writes_not_landed(self):
        os.environ["PREPUSH_SKIP"] = "x"
        try:
            code, out = self.run_main(["land"])
        finally:
            del os.environ["PREPUSH_SKIP"]
        self.assertEqual(code, 1)
        [e] = self.entries()
        self.assertEqual((e["command"], e["exit"]), ("land", 1))
        self.assertTrue(e["verdict"].startswith("NOT LANDED: PREPUSH_SKIP"), e["verdict"])

    def test_a_queue_subcommand_of_land_writes_no_entry(self):
        # 2026-11-16: a monitor polling `agentctl land status` wrote ~37 inbox entries in an hour (2,692 over three
        # days), each acked by hand. A queue subcommand answers its caller there and then; only a land's own verdict
        # may outlive the agent that started it
        saved = A.cmd_land
        A.cmd_land = lambda a, clock, store: print("  20261116-185821-movies-x-79a0ca  checking  /w  abc  deadline in 9 min") or 0
        self.addCleanup(setattr, A, "cmd_land", saved)
        for argv in (["land", "status"], ["land", "hold"], ["land", "release"], ["land", "cancel", "e1"]):
            self.assertEqual(self.run_main(argv)[0], 0)
            self.assertEqual(self.entries(), [], argv)
        self.run_main(["land"])
        self.assertEqual([e["command"] for e in self.entries()], ["land"], "a land's own verdict still goes there")

    def test_waiting_is_written_at_once_and_the_final_verdict_later(self):
        def handler(a):
            print("WAITING abc123 (1 change queued on waiting; entry e1)")
            self.assertEqual([e["final"] for e in self.entries()], [False])  # there before the command ends
            print("LANDED abc123")
            print("WATCH OK: Main #5 green, convergence #6 green")
            return 0
        with contextlib.redirect_stdout(io.StringIO()):
            A.with_inbox("land", handler, None)
        waiting, final = self.entries()
        self.assertEqual(waiting["verdict"], "WAITING abc123 (1 change queued on waiting; entry e1)")
        self.assertEqual(final["verdict"], "WATCH OK: Main #5 green, convergence #6 green")
        self.assertEqual(final["verdicts"][-2:], ["LANDED abc123", "WATCH OK: Main #5 green, convergence #6 green"])

    def test_a_land_that_ends_at_waiting_writes_it_once(self):
        def handler(a):
            print("WAITING abc123 (entry e1, 2 in batch)")
            return 0
        with contextlib.redirect_stdout(io.StringIO()):
            A.with_inbox("land", handler, None)
        self.assertEqual([e["verdict"] for e in self.entries()], ["WAITING abc123 (entry e1, 2 in batch)"])

    def test_a_crash_is_a_verdict_too(self):
        def handler(a):
            print("LANDED abc123")
            raise RuntimeError("boom")
        with contextlib.redirect_stdout(io.StringIO()), self.assertRaises(RuntimeError):
            A.with_inbox("watch", handler, None)
        [e] = self.entries()
        self.assertEqual((e["verdict"], e["exit"]), ("crashed: RuntimeError: boom", 1))

    def test_a_signal_ends_with_an_entry(self):
        flag = os.path.join(self.tmp.name, "never.flag")
        p = subprocess.Popen([sys.executable, TOOL, "wait", "file", flag, "--timeout", "60"], env=dict(os.environ),
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        owners, deadline = os.environ["AGENTCTL_OWNERS_DIR"], time.time() + 180
        while time.time() < deadline and not (os.path.isdir(owners) and any(n.endswith(".json") for n in os.listdir(owners))):
            time.sleep(0.05)
        p.send_signal(signal.SIGTERM)
        p.communicate(timeout=180)
        [e] = self.entries()
        self.assertEqual((e["command"], e["verdict"], e["exit"]), ("wait", "ended by signal 15", 143))

    def test_the_composition_root_routes_entries_through_the_alert_model_and_unwired_they_are_plain_files(self):
        seen, saved_write = [], A.alert_write
        A.alert_write = lambda rec, st: seen.append((rec["verdict"], st))
        try:
            A._inbox_writers[0]({"verdict": "routed"})
        finally:
            A.alert_write = saved_write
        self.assertEqual([v for v, _st in seen], ["routed"], "the root's writer is the alert model's")
        self.assertIsNotNone(seen[0][1], "with the process's store")
        saved = list(A._inbox_writers)
        A._inbox_writers[:] = []
        try:
            eid = A.inbox_write("wait", "run o/r#1: success", [], [], 0)
        finally:
            A._inbox_writers[:] = saved
        [e] = self.entries()
        self.assertEqual((e["id"], e["verdict"]), (eid, "run o/r#1: success"))
        self.assertNotIn("audience", e)  # never routed: no alert model ran

    def test_an_entry_is_stamped_by_the_clock_the_composition_root_wired(self):
        from fake_clock import FakeClock
        clock = FakeClock(t=1_700_000_000.0)
        A.wire_collaborators(clock, A.CurrentStore(A.process_store))
        try:
            A.inbox_write("wait", "run o/r#1: success", [], [], 0)
            self.assertNotEqual(A.inbox_write("wait", "run o/r#2: success", [], [], 0),
                                A.inbox_write("wait", "run o/r#3: success", [], [], 0), "a standing clock: one id each")
        finally:
            A.wire_collaborators(A.Clock(), A.CurrentStore(A.process_store))
        e = min(self.entries(), key=lambda x: x["id"])
        self.assertEqual(e["at"], 1_700_000_000.0)
        self.assertTrue(e["id"].startswith("20231114T221320"), e["id"])
        self.assertTrue(e["time"].startswith("2023-11-14"), e["time"])

    def test_inbox_lists_unread_and_acks(self):
        A.inbox_write("land", "WATCH RED: Main #9 test", [], [], 3)
        A.inbox_write("wait", "run o/r#1: success", [], [], 0)
        code, out = self.run_main(["inbox", "--unread"])
        self.assertEqual(code, 0)
        self.assertEqual(len(out.splitlines()), 2)
        self.assertIn("UNREAD land", out)
        self.assertIn("exit 3: WATCH RED: Main #9 test", out)
        self.run_main(["inbox", "--unread", "--ack"])
        _, out = self.run_main(["inbox", "--unread"])
        self.assertIn("inbox: no unread entries", out)
        _, out = self.run_main(["inbox"])
        self.assertEqual(out.count("read  "), 2)

    def test_count_counts_every_entry_never_the_listings_limit(self):
        """The owner, 2026-11-06: "why is there always 30 unread in inbox" -- the status line counted the listing, whose
        default --limit is 30; the real count was 44."""
        for i in range(35):
            A.inbox_write("wait", f"run o/r#{i}: success", [], [], 0)
        code, out = self.run_main(["inbox", "--unread", "--count"])
        self.assertEqual((code, out.strip()), (0, "35"))

    def test_ack_marks_only_the_callers_own_entries_and_all_is_the_owners(self):
        """2026-10-29 19:00: an agent's `inbox --unread --ack` marked every agent's verdicts read."""
        saved = (os.environ.get("CLAUDE_CODE_SESSION_ID"), os.getcwd())
        mine_wt = os.path.join(self.tmp.name, "movies-wt-mine")
        other_wt = os.path.join(self.tmp.name, "movies-wt-mine-2")  # a sibling whose name extends mine
        for wt in (mine_wt, other_wt):
            subprocess.run(["git", "init", "-q", wt], check=True)
        try:
            os.environ["CLAUDE_CODE_SESSION_ID"] = "session-mine"
            own = A.inbox_write("land", "LANDED aaaaaaa", [], [], 0, reply=True)
            os.environ["CLAUDE_CODE_SESSION_ID"] = "session-other"
            in_my_tree = A.inbox_write("land", "WATCH RED: Main #9 test", [], [], 3, cwd=os.path.join(mine_wt, "sub"),
                                       reply=True)
            theirs = A.inbox_write("land", "NOT LANDED: theirs", [], [], 1, cwd=other_wt, reply=True)
            # another item of theirs (one live alert per item: a second NOT LANDED of that worktree would merge into it)
            named = A.inbox_write("land", "WATCH RED: Main #10 named", [], [], 1, cwd=other_wt, reply=True)
            os.environ["CLAUDE_CODE_SESSION_ID"] = "session-mine"
            os.chdir(mine_wt)
            _, out = self.run_main(["inbox", "--unread", "--ack"])
            self.assertIn("acknowledged 2 entries; 2 left unread", out)
            read = {e["id"]: e["read"] for e in self.entries()}
            self.assertEqual(read, {own: True, in_my_tree: True, theirs: False, named: False})
            _, out = self.run_main(["inbox", named[:8], "--ack"])  # a prefix (a date) is no name: theirs stay unread
            self.assertIn("2 left unread", out)
            self.assertFalse({e["id"]: e["read"] for e in self.entries()}[named])
            self.run_main(["inbox", named, "--ack"])  # an entry named by its whole id: acked, whoever's
            self.assertEqual({e["id"]: e["read"] for e in self.entries()}[theirs], False)
            self.assertTrue({e["id"]: e["read"] for e in self.entries()}[named])
            os.chdir(self.tmp.name)  # outside any worktree, another session: nothing of its own
            os.environ["CLAUDE_CODE_SESSION_ID"] = "session-third"
            _, out = self.run_main(["inbox", "--unread", "--ack"])
            self.assertIn("acknowledged 0 entries; 1 left unread", out)
            _, out = self.run_main(["inbox", "--unread", "--ack", "--all"])  # the owner's
            self.assertIn("acknowledged 1 entry", out)
            self.assertTrue(all(e["read"] for e in self.entries()))
        finally:
            os.chdir(saved[1])
            if saved[0] is None:
                os.environ.pop("CLAUDE_CODE_SESSION_ID", None)
            else:
                os.environ["CLAUDE_CODE_SESSION_ID"] = saved[0]

    def test_a_systemexit_message_is_the_verdict(self):
        # 2026-10-27: two detached watches died on the vault's SystemExit and their entries said "(no output)"
        def handler(a):
            raise SystemExit("agentctl: could not read the movies AGENTCTL_STORE_TOKEN from the vault")
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()), \
                self.assertRaises(SystemExit):
            A.with_inbox("watch", handler, None)
        [e] = self.entries()
        self.assertEqual(e["verdict"], "exited: could not read the movies AGENTCTL_STORE_TOKEN from the vault")
        self.assertEqual((e["exit"], e["kind"]), (1, "UNKNOWN"))

    def test_a_bare_exit_1_names_what_it_said_last(self):
        def handler(a):
            A.err("watch: the runs could not be listed")
            return 1
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            A.with_inbox("watch", handler, None)
        [e] = self.entries()
        self.assertEqual(e["verdict"], "exit 1 with no verdict line; it said last: watch: the runs could not be listed")
        self.assertNotIn("(no output)", e["verdict"])

    def test_the_entry_is_written_after_the_cwd_is_removed(self):
        # WATCH OK closes the worktree the detached watch runs in: os.getcwd() raises, and the verdict was lost
        gone = os.path.join(self.tmp.name, "wt")
        os.makedirs(gone)
        here = os.getcwd()

        def handler(a):
            os.chdir(gone)
            os.rmdir(gone)
            print("WATCH OK: Main #5 green, convergence #6 green")
            return 0
        try:
            with contextlib.redirect_stdout(io.StringIO()):
                A.with_inbox("watch", handler, None)
        finally:
            os.chdir(here)
        [e] = self.entries()
        self.assertEqual((e["verdict"], e["kind"]), ("WATCH OK: Main #5 green, convergence #6 green", "OK"))

    def test_kinds(self):
        for verdict, kind in [("WATCH RED: Main #9 test", "RED"), ("WATCH STALLED: no change", "STALLED"),
                              ("WATCH UNKNOWN: x", "UNKNOWN"), ("NOT LANDED: the checks failed", "NOT_LANDED"),
                              ("NOT LANDED: stalled push", "STALLED"), ("FIX FAILED: Main #2 still fails t", "FIX_FAILED"),
                              ("FIX OK: #1 cleared by #2", "FIX_OK"), ("LANDED abc", "LANDED"), ("crashed: X: y", "UNKNOWN"),
                              ("UNKNOWN: the waiting processor cannot read the gate", "UNKNOWN"), ("CHECK CAP 3 -> 2", "CHECK")]:
            self.assertEqual(A.inbox_kind(verdict), kind, verdict)
        self.assertEqual(A.inbox_kind("WAITING abc", final=False), "WAITING")

    def wait(self, *args):
        os.environ["AGENTCTL_INBOX_POLL"] = "0.02"
        try:
            return self.run_main(["inbox", "wait", "--audience", "all", *args])  # the mechanics, whoever's (test_alerts)
        finally:
            del os.environ["AGENTCTL_INBOX_POLL"]

    def test_inbox_wait_prints_an_unread_entry_of_its_kinds(self):
        A.inbox_write("watch", "WATCH OK: Main #5 green", [], [], 0)
        red = A.inbox_write("watch", "WATCH RED: Main #9 test", [], [], 3)
        code, out = self.wait("--timeout", "5")
        self.assertEqual(code, 0)
        self.assertIn(f"{red} UNREAD watch", out)
        self.assertNotIn("WATCH OK", out)
        self.assertFalse(any(e["read"] for e in self.entries()), "wait does not ack: the orchestrator acks once routed")

    def test_inbox_wait_ignores_other_kinds_and_read_entries(self):
        A.inbox_write("watch", "WATCH OK: Main #5 green", [], [], 0)
        eid = A.inbox_write("watch", "WATCH RED: Main #9 test", [], [], 3)
        self.run_main(["inbox", eid, "--ack"])
        code, out = self.wait("--timeout", "0.2")
        self.assertEqual(code, 2, out)
        code, out = self.wait("--timeout", "5", "--kinds", "OK")
        self.assertEqual(code, 0)
        self.assertIn("WATCH OK", out)

    def test_inbox_wait_since_ack_wakes_on_an_old_unread_entry_after_a_newer_ack(self):
        """2026-11-19: an owner entry (issue 55799d's alert) sat unread a day -- `--since-ack` took the NEWEST ack's time
        as a floor, and the orchestrator acks one id at a time, so every later-acked entry hid the older unread one.
        Unread is per entry: an entry not acknowledged wakes the watcher whatever was acked after it."""
        old = A.inbox_write("watch", "WATCH RED: Main #1 old", [], [], 3)
        time.sleep(0.02)
        newer = A.inbox_write("watch", "WATCH RED: Main #2 newer", [], [], 3)
        self.run_main(["inbox", newer, "--ack"])
        code, out = self.wait("--since-ack", "--timeout", "5")
        self.assertEqual(code, 0, out)
        self.assertIn(f"{old} UNREAD watch", out)
        self.assertNotIn(newer, out)

    def test_rearming_the_watcher_prints_the_backlog_at_once(self):
        """An orchestrator re-arming its watcher (after a compaction, a restart) is shown every unread entry there and
        then -- once per arming, the one-shot and the streaming one alike -- never only what arrives later."""
        first = A.inbox_write("watch", "WATCH RED: Main #1 test", [], [], 3)
        second = A.inbox_write("land", "NOT LANDED: the checks failed", [], [], 1)
        for n in range(2):  # armed; a newer entry routed and acked; armed again: the same backlog
            if n:
                time.sleep(0.02)
                self.run_main(["inbox", A.inbox_write("watch", "WATCH RED: Main #3 routed", [], [], 3), "--ack"])
            code, out = self.wait("--since-ack", "--timeout", "5")
            self.assertEqual(code, 0, out)
            self.assertIn(first, out)
            self.assertIn(second, out)
        from unittest import mock

        class Armed(Exception):
            pass
        out = io.StringIO()
        with mock.patch("time.sleep", side_effect=Armed), contextlib.redirect_stdout(out):
            with self.assertRaises(Armed):  # its first pass, before it sleeps: the backlog is out
                A.inbox_wait(A.WAIT_KINDS_DEFAULT, since_ack=True, forever=True, audience="all")
        self.assertIn(first, out.getvalue())
        self.assertIn(second, out.getvalue())

    def test_the_owners_watcher_wakes_on_any_alert_addressed_to_the_owner(self):
        """An issue's weekly escalation is kind OTHER, addressed to the owner by the event projector: the owner's
        default watcher wakes on it (an explicit --kinds still selects by kind alone)."""
        rec = {"id": "20261119T121335006151-ev13287-issue-open", "time": "t", "at": time.time(), "command": "issue",
               "final": True, "exit": 1, "verdict": "AGENTCTL ISSUE 20261104T044122-55799d open 15 d (filed by x): y",
               "kind": "OTHER", "verdicts": [], "tail": [], "item_type": "issue", "item_id": "20261104T044122-55799d",
               "audience": "owner"}
        A.alert_write(rec, A.CurrentStore(A.process_store))
        os.environ["AGENTCTL_INBOX_POLL"] = "0.02"
        try:
            code, out = self.run_main(["inbox", "wait", "--since-ack", "--timeout", "5"])
            self.assertEqual(code, 0, out)
            self.assertIn("AGENTCTL ISSUE 20261104T044122-55799d", out)
            code, out = self.run_main(["inbox", "wait", "--kinds", "RED", "--timeout", "0.2"])
            self.assertEqual(code, 2, out)
        finally:
            del os.environ["AGENTCTL_INBOX_POLL"]

    def test_inbox_wait_wakes_on_a_new_entry(self):
        import threading
        t = threading.Timer(0.3, lambda: A.inbox_write("land", "NOT LANDED: the checks failed", [], [], 1))
        t.start()
        t0 = time.time()
        code, out = self.wait("--timeout", "10")
        t.join()
        self.assertEqual(code, 0)
        self.assertIn("NOT LANDED: the checks failed", out)
        self.assertLess(time.time() - t0, 5)

    def test_other_commands_write_nothing(self):
        self.run_main(["inbox"])
        self.run_main(["state", "demo"])
        self.assertEqual(self.entries(), [])


if __name__ == "__main__":
    unittest.main()
