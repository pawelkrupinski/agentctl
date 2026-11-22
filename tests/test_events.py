"""Events -> the inbox (design §8.2, reconciler phase 1 step 7): the projector writes one inbox entry per event with an
audience or a non-info severity (never twice), the notifier sends one notification per escalation, and the change
path writes the inbox only through its events."""
import ast, os, stat, sys, tempfile, time, unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from test_agentctl import A, TOOL, code_source  # noqa: E402


class EventsTest(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp()
        self.saved = {k: os.environ.get(k) for k in ("AGENTCTL_INBOX_DIR", "AGENTCTL_NOTIFY")}
        os.environ["AGENTCTL_INBOX_DIR"] = os.path.join(self.root, "inbox")
        self.notes = os.path.join(self.root, "notes")
        hook = os.path.join(self.root, "notify.sh")
        with open(hook, "w") as f:
            f.write(f'#!/bin/sh\necho "$1|$2" >> {self.notes}\n')
        os.chmod(hook, os.stat(hook).st_mode | stat.S_IEXEC)
        os.environ["AGENTCTL_NOTIFY"] = hook
        self.st = A.Store(path=os.path.join(self.root, "store.db"))

    def tearDown(self):
        for k, v in self.saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def test_the_projector_writes_each_addressed_event_once_in_todays_format(self):
        e = {"id": "e1", "worktree": "/w/a", "session": "s1"}
        self.st.put_item("change", "e1", "checking")
        self.st.transition("change", "e1", "checked", 1)  # no audience, info: nothing for the inbox
        self.st.transition("change", "e1", "pushing", 2)
        self.st.transition("change", "e1", "landed", 3, event=A.change_event(e, "LANDED abc1234 (queued e1)", code=0))
        self.assertEqual(A.project_events(self.st), 1)
        self.assertEqual(A.project_events(self.st), 0)  # a repeated pass writes nothing twice
        [entry] = A.inbox_entries()
        self.assertEqual((entry["kind"], entry["cwd"], entry["verdict"], entry["exit"]),
                         ("LANDED", "/w/a", "LANDED abc1234 (queued e1)", 0))

    def test_the_notifier_notifies_each_escalation_once(self):
        self.st.put_item("change", "e2", "queued")
        self.st.transition("change", "e2", "queued", 1)  # queued past its deadline: an escalation
        self.assertEqual(A.notify_escalations(self.st), 1)
        self.assertEqual(A.notify_escalations(self.st), 0)
        self.assertEqual(len(open(self.notes).read().splitlines()), 1)
        self.assertTrue(self.st.events()[-1]["notified_at"])

    def test_an_escalation_the_cursor_passed_unmarked_is_still_delivered_and_a_closed_ones_resolved(self):
        """W6, 2026-10-30: RED UNCLAIMED #2560 sat 'not notified' for good -- the notifier read only past its cursor."""
        self.st.put_item("change", "e3", "queued")
        self.st.transition("change", "e3", "queued", 1)  # an escalation, its item still open
        self.st.put_item("change", "e4", "queued")
        self.st.transition("change", "e4", "queued", 1)  # an escalation whose item then closes
        self.st.transition("change", "e4", "cancelled", 2)
        self.st.meta_set("notifier_seq", self.st.events()[-1]["seq"])  # the cursor past both, neither marked
        self.assertEqual(A.notify_escalations(self.st), 1)
        self.assertEqual(len(open(self.notes).read().splitlines()), 1)
        self.assertTrue(all(e["notified_at"] for e in self.st.events() if e["severity"] == "escalate"))
        os.environ["AGENTCTL_W6_MAX"] = "0"
        self.addCleanup(os.environ.pop, "AGENTCTL_W6_MAX", None)
        saved = A.open_store
        A.open_store = lambda *a, **k: self.st
        try:
            self.assertTrue(A.inv_w6()[0])
        finally:
            A.open_store = saved

    def test_a_change_never_leaves_the_queue_without_its_authors_inbox_entry(self):
        """2026-10-30 `…-76336e`: its NOT LANDED entry did not carry the item id (a search by id found nothing), and
        terminal moves written without an audience -- a cancel, a landed-by-content close, a stranded supersede -- reached
        nobody. Every terminal transition now carries an event addressed to the item's worktree, naming the item."""
        for i, to in enumerate(("landed", "rejected", "superseded", "cancelled")):
            self.st.put_item("change", f"c{i}", "queued", {"worktree": "/w/flips", "head": "83af2cb4"})
            self.st.transition("change", f"c{i}", "checking" if to == "rejected" else to, 1)
            if to == "rejected":
                self.st.transition("change", f"c{i}", "rejected", 2,
                                   event={"text": "NOT LANDED: the pre-push checks failed"})  # no audience given
        self.st.put_item("change", "nowt", "queued")  # no worktree known: still an entry (the owner's)
        self.st.transition("change", "nowt", "cancelled", 1)
        self.assertEqual(A.change_exits_unannounced(self.st), [])
        self.assertEqual(A.project_events(self.st), 5)
        by = {e["entry"]: e for e in A.inbox_entries()}
        self.assertEqual(set(by), {"c0", "c1", "c2", "c3", "nowt"})
        self.assertEqual(by["c1"]["cwd"], "/w/flips")
        self.assertEqual((by["c1"]["kind"], by["c1"]["exit"]), ("NOT_LANDED", 1))
        self.assertEqual((by["c0"]["kind"], by["c0"]["exit"]), ("LANDED", 0))
        self.assertTrue(by["c2"]["verdict"].startswith("SUPERSEDED c2"))
        self.assertTrue(by["c3"]["verdict"].startswith("CANCELLED c3"))
        # `agentctl inbox <item id>` finds the entry by the item id
        out = self.run_inbox(["c1"])
        self.assertIn("NOT LANDED", out)
        self.assertNotIn("CANCELLED", out)

    def run_inbox(self, ids):
        import argparse, contextlib, io
        a = argparse.Namespace(ids=ids, unread=False, ack=False, all=False, limit=50, json=False, verbose=False,
                               kinds=None, since_ack=False, timeout=0)
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            A.cmd_inbox(a)
        return buf.getvalue()

    def test_w8_names_a_change_that_left_the_queue_silently(self):
        self.st.put_item("change", "s1", "queued", {"worktree": "/w/a"})
        self.st.transition("change", "s1", "cancelled", 1)
        c = self.st.db()
        c.execute("UPDATE events SET audience='{}', severity='info' WHERE item_id='s1'")  # a writer around transition
        c.close()
        self.assertEqual(A.change_exits_unannounced(self.st), ["s1"])
        saved = A.open_store
        A.open_store = lambda *a, **k: self.st
        try:
            ok, why = A.inv_w8()
            self.assertFalse(ok)
            self.assertIn("s1", why)
            os.environ["AGENTCTL_W8_WINDOW"] = "-60"  # it ages out of the window: the condition clears itself
            self.addCleanup(os.environ.pop, "AGENTCTL_W8_WINDOW", None)
            self.assertTrue(A.inv_w8()[0])
        finally:
            A.open_store = saved

    def test_w8_does_not_name_a_change_that_left_the_queue_while_it_read(self):
        """2026-11-17 10:14:46 `…-ref-scrapers-2a9c4e` (and 2026-11-16 11:32 `…-keeperflake-054603`): W8 read every
        event (~11k, 3 s over the tunnel) and THEN the items; the change was rejected in between, its event (with its
        author's audience) committed after the events read, so the item looked terminal with no event. The next pass
        was ok. The items are read first: an exit the items show has its event committed with it, in the later read."""
        self.st.put_item("change", "r1", "checking", {"worktree": "/w/a"})
        real_events = self.st.events

        def events_then_reject(*a, **k):
            evs = real_events(*a, **k)
            if self.st.item("change", "r1")["state"] == "checking":  # the processor rejects it mid-read
                self.st.transition("change", "r1", "rejected", 1, event={"text": "NOT LANDED: the checks failed"})
            return evs

        self.st.events = events_then_reject
        self.assertEqual(A.change_exits_unannounced(self.st), [])

    def test_a_not_landed_merged_into_the_worktrees_live_alert_is_found_by_its_own_id(self):
        """2026-11-17 `…-ref-scrapers-2a9c4e`: its NOT LANDED merged into its worktree's live land alert, an earlier
        change's (`…-c77e00`) entry, which kept naming that one: `agentctl inbox <its id>` said "no entries"."""
        for i, iid in enumerate(("old-c77e00", "new-2a9c4e")):
            self.st.put_item("change", iid, "checking", {"worktree": "/w/scrapers", "session": "s1"})
            self.st.transition("change", iid, "rejected", 1, event={"text": f"NOT LANDED: check {i} failed"})
        A.project_events(self.st)
        self.assertEqual(len(A.inbox_entries()), 1)  # one live alert for the worktree's landing
        self.assertIn("check 1 failed", self.run_inbox(["new-2a9c4e"]))
        self.assertIn("check 1 failed", self.run_inbox(["old-c77e00"]))  # the earlier id still finds the live alert

    def test_the_projector_keeps_its_cursor_before_an_entry_it_could_not_write(self):
        e = {"id": "e5", "worktree": "/w/a", "session": "s1"}
        self.st.put_item("change", "e5", "pushing")
        self.st.transition("change", "e5", "landed", 1, event=A.change_event(e, "LANDED abc (queued e5)", code=0))
        saved = A.write_json
        A.write_json = lambda *a, **k: (_ for _ in ()).throw(OSError("disk full"))
        try:
            self.assertEqual(A.project_events(self.st), 0)
        finally:
            A.write_json = saved
        self.assertEqual(A.project_events(self.st), 1, "written at the next cycle, never lost")

    def test_the_change_path_writes_the_inbox_only_through_events(self):
        tree = ast.parse(code_source())
        fns = {n.name: n for n in tree.body if isinstance(n, ast.FunctionDef)}
        for name in ("queue_verdict", "queue_join", "supersede", "set_change", "requeue_infra", "push_change",
                     "check_entry", "_check_entry", "tick_changes"):
            calls = [c for c in ast.walk(fns[name]) if isinstance(c, ast.Call) and getattr(c.func, "id", "") == "inbox_write"]
            self.assertEqual(calls, [], f"{name} writes the inbox itself")


class NotifierDeliveryTest(unittest.TestCase):
    """2026-11-03, invariant-W6.9: "152 escalation(s) not notified" -- five reds re-escalated RED UNCLAIMED every 10 min
    for 5 h, each told on its own (10-20 s apiece: a `gh run view` per click URL), the notifier 3 h behind, telling the
    owner "no taker" about reds held for an hour; and an event was marked notified BEFORE its delivery, failed or not."""

    def setUp(self):
        self.root = tempfile.mkdtemp()
        self.saved = {k: os.environ.get(k) for k in ("AGENTCTL_INBOX_DIR", "AGENTCTL_NOTIFY", "AGENTCTL_NOTIFIER", "PATH")}
        os.environ["AGENTCTL_INBOX_DIR"] = os.path.join(self.root, "inbox")
        self.notes = os.path.join(self.root, "notes")
        self.hook = self.script("notify.sh", f'echo "$1|$2" >> {self.notes}\n[ -e {self.root}/down ] && exit 1\nexit 0')
        os.environ["AGENTCTL_NOTIFY"] = self.hook
        self.t = [1_000_000.0]
        self.st = A.Store(path=os.path.join(self.root, "store.db"), clock=lambda: self.t[0])
        self.saved_store = A.open_store
        A.open_store = lambda *a, **k: self.st

    def tearDown(self):
        A.open_store = self.saved_store
        for k, v in self.saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def script(self, name, body):
        p = os.path.join(self.root, name)
        with open(p, "w") as f:
            f.write("#!/bin/sh\n" + body + "\n")
        os.chmod(p, os.stat(p).st_mode | stat.S_IEXEC)
        return p

    def shown(self):
        return open(self.notes).read().splitlines() if os.path.exists(self.notes) else []

    def escalate(self, iid, times=1):
        if not self.st.item("change", iid):
            self.st.put_item("change", iid, "queued")
        for _ in range(times):
            self.st.transition("change", iid, "queued", self.st.item("change", iid)["version"])

    def unmarked(self):
        return [e for e in self.st.events(limit=10 ** 6) if e["severity"] == "escalate" and not e.get("notified_at")]

    def test_notify_falls_back_to_osascript_when_terminal_notifier_is_not_allowed_and_says_whether_it_got_through(self):
        os.environ.pop("AGENTCTL_NOTIFY")
        os.environ["AGENTCTL_NOTIFIER"] = self.script(
            "terminal-notifier", "echo 'Could not request notification permission: Notifications are not allowed for "
            "this application' >&2; exit 3")
        bindir = os.path.join(self.root, "bin")
        os.makedirs(bindir)
        osa = os.path.join(self.root, "osa-rc")
        with open(osa, "w") as f:
            f.write("0")
        with open(os.path.join(bindir, "osascript"), "w") as f:
            f.write(f"#!/bin/sh\necho \"$@\" >> {self.notes}\nexit $(cat {osa})\n")
        os.chmod(os.path.join(bindir, "osascript"), 0o755)
        os.environ["PATH"] = bindir + os.pathsep + os.environ["PATH"]
        saved = A.sys.platform
        A.sys.platform = "darwin"
        try:
            self.assertIs(A.notify("t", "m", url="https://u"), True)
            self.assertIn("display notification", self.shown()[-1])  # the fallback ran
            with open(osa, "w") as f:
                f.write("1")
            self.assertIs(A.notify("t", "m"), False)
        finally:
            A.sys.platform = saved
        self.assertIn("Notifications are not allowed", A.notify_cause())
        self.assertIn("osascript", A.notify_cause())

    def darwin_without_terminal_notifier(self, osascript_body):
        """notify()'s macOS path with no terminal-notifier installed and `osascript_body` as osascript."""
        os.environ.pop("AGENTCTL_NOTIFY")
        bindir = os.path.join(self.root, "bin")
        os.makedirs(bindir, exist_ok=True)
        with open(os.path.join(bindir, "osascript"), "w") as f:
            f.write("#!/bin/sh\n" + osascript_body + "\n")
        os.chmod(os.path.join(bindir, "osascript"), 0o755)
        os.environ["PATH"] = bindir + os.pathsep + os.environ["PATH"]
        saved_platform = A.sys.platform
        A.sys.platform = "darwin"
        self.addCleanup(setattr, A.sys, "platform", saved_platform)
        saved_bin = A.notifier_bin
        A.notifier_bin = lambda: None
        self.addCleanup(setattr, A, "notifier_bin", saved_bin)

    def test_an_osascript_timeout_says_timed_out_and_blames_the_load_not_terminal_notifier(self):
        """2026-11-22 08:04Z: osascript, at load ~150, was killed by its 10 s timeout; WN said only "Command '['osascript',
        '-e', 'display notification ..." (the exception's argv, cut) and told the owner to allow terminal-notifier --
        not installed."""
        self.darwin_without_terminal_notifier("sleep 5")
        os.environ["AGENTCTL_NOTIFY_TIMEOUT"] = "1"
        self.addCleanup(os.environ.pop, "AGENTCTL_NOTIFY_TIMEOUT", None)
        self.escalate("slow")
        self.assertEqual(A.notify_escalations(self.st), 0)
        self.assertEqual(A.notify_cause(), "osascript: timed out after 1 s")
        ok, detail = A.inv_wn()
        self.assertIs(ok, False)
        self.assertIn("(osascript: timed out after 1 s; 1 waiting)", detail)
        self.assertIn("overloaded", detail)
        self.assertNotIn("terminal-notifier", detail)
        self.assertNotIn("Command '", detail)

    def test_a_failing_osascript_keeps_its_exit_code_and_last_stderr_line_and_names_its_own_permission(self):
        self.darwin_without_terminal_notifier("echo starting; echo 'noise' >&2; "
                                              "echo 'execution error: Notifications are not allowed (-1743)' >&2; exit 1")
        self.assertIs(A.notify("t", "m"), False)
        self.assertEqual(A.notify_cause(), "osascript: exit 1: execution error: Notifications are not allowed (-1743)")
        self.assertIn("Script Editor", A.notify_fix())
        self.assertNotIn("terminal-notifier", A.notify_fix())

    def test_each_channel_gets_time_for_an_overloaded_cold_start(self):
        """10 s was the 2026-11-22 osascript's whole cold start under load (XProtect, trustd, then its TCC request)."""
        self.darwin_without_terminal_notifier("exit 0")
        seen, real = [], A.subprocess.run

        def run(argv, **kw):
            seen.append(kw.get("timeout"))
            return real(argv, **kw)
        A.subprocess.run = run
        try:
            self.assertIs(A.notify("t", "m"), True)
        finally:
            A.subprocess.run = real
        self.assertEqual(seen, [30])
        self.assertEqual((A.notify_cause(), A.notify_fix()), ("", ""))

    def test_a_failed_delivery_is_not_recorded_and_raises_ONE_condition_naming_the_owners_fix(self):
        os.environ["AGENTCTL_W6_MAX"] = "0"
        self.addCleanup(os.environ.pop, "AGENTCTL_W6_MAX", None)
        for i in range(5):
            self.escalate(f"d{i}")
        open(os.path.join(self.root, "down"), "w").close()
        self.assertEqual(A.notify_escalations(self.st), 0)
        self.assertEqual(len(self.unmarked()), 5, "nothing reached the owner: nothing is recorded as told")
        ok, detail = A.inv_wn()
        self.assertIs(ok, False)
        self.assertIn("notify.sh: exit 1", detail)
        self.assertIn("fix the notification command ($AGENTCTL_NOTIFY)", detail)
        self.assertIsNone(A.inv_w6()[0], "W6 defers to WN: one condition, not one per waiting escalation")
        os.remove(os.path.join(self.root, "down"))
        self.t[0] += 60
        self.assertEqual(A.notify_escalations(self.st), 1)  # caught up: one summary
        self.assertEqual(self.unmarked(), [])
        self.assertIs(A.inv_wn()[0], True)
        self.assertIs(A.inv_w6()[0], True)

    def test_one_live_notification_per_item_re_notified_on_the_reminder_schedule(self):
        self.escalate("r1", times=3)  # three re-escalations pending at once
        self.assertEqual(A.notify_escalations(self.st), 1)
        self.assertEqual(self.unmarked(), [])
        for _ in range(5):  # re-escalated every 10 min: covered by the live notification, no new one
            self.t[0] += 600
            self.escalate("r1")
            self.assertEqual(A.notify_escalations(self.st), 0)
        self.assertEqual(self.unmarked(), [])
        self.t[0] += A.ASK_REMIND_FIRST
        self.escalate("r1")
        self.assertEqual(A.notify_escalations(self.st), 1, "the first reminder")
        self.assertEqual(len(self.shown()), 2)

    def test_an_escalation_whose_item_moved_on_is_closed_untold(self):
        self.escalate("m1")
        self.st.transition("change", "m1", "checking", self.st.item("change", "m1")["version"])  # not terminal: moved on
        self.assertEqual(A.notify_escalations(self.st), 0)
        self.assertEqual(self.unmarked(), [])
        self.assertEqual(self.shown(), [])

    def test_a_catch_up_after_an_outage_is_one_summary_not_a_flood(self):
        for i in range(A.NOTIFY_BURST + 3):
            self.escalate(f"o{i}")
        self.assertEqual(A.notify_escalations(self.st), 1)
        [line] = self.shown()
        self.assertIn(f"{A.NOTIFY_BURST + 3} escalations", line)
        self.assertEqual(self.unmarked(), [])


if __name__ == "__main__":
    unittest.main()
