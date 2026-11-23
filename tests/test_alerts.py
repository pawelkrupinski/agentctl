"""THE ALERT MODEL (the owner, 2026-11-04: 1524 owner-facing alerts in 7 days, ~35 needed the owner; "fix root causes,
make structural fixes"). Every inbox entry goes through ONE router: keyed by an ITEM, addressed to an AUDIENCE (author,
fixer, owner, info); one live alert per item, updated in place; closed by its item's own probe; only the owner's reach
the owner's `inbox wait`, the notifier and the ESCALATED count. Each test here failed before the model."""
import contextlib, io, json, os, stat, sys, tempfile, time, unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from test_agentctl import A  # noqa: E402


class AlertBase(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp()
        self.saved = {k: os.environ.get(k) for k in ("AGENTCTL_INBOX_DIR", "AGENTCTL_NOTIFY", "AGENTCTL_RED_FAMILIES",
                                                     "AGENTCTL_INBOX_POLL", "CLAUDE_CODE_SESSION_ID")}
        self.inbox = os.path.join(self.root, "inbox")
        os.environ["AGENTCTL_INBOX_DIR"] = self.inbox
        self.notes = os.path.join(self.root, "notes")
        hook = os.path.join(self.root, "notify.sh")
        with open(hook, "w") as f:
            f.write(f'#!/bin/sh\necho "$1|$2" >> {self.notes}\n')
        os.chmod(hook, os.stat(hook).st_mode | stat.S_IEXEC)
        os.environ["AGENTCTL_NOTIFY"] = hook
        fams = os.path.join(self.root, "families.json")
        with open(fams, "w") as f:
            json.dump({"families": [{"name": "conv", "owner": True, "workflows": ["Conv"]}]}, f)
        os.environ["AGENTCTL_RED_FAMILIES"] = fams
        self.st = A.Store(path=os.path.join(self.root, "store.db"))
        self.wt = os.path.join(self.root, "movies-wt-a")
        os.makedirs(self.wt)

    def tearDown(self):
        for k, v in self.saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def unread(self, audience=None):
        return [e for e in A.inbox_entries() if not e["read"] and (audience is None or A.alert_audience(e) == audience)]

    def owner_wait(self):
        os.environ["AGENTCTL_INBOX_POLL"] = "0.01"
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = A.main(["inbox", "wait", "--timeout", "0.1"])
        return code, out.getvalue()

    def shown(self):
        return open(self.notes).read().splitlines() if os.path.exists(self.notes) else []

    def red(self, key="r1", workflow="Main", level_to=0):
        it = self.st.put_item("red", key, "offered", {"key": key, "workflow": workflow, "last_run": {"number": 2123},
                                                      "jobs": ["test"], "tier": "fixer"})
        return it

    def unclaimed(self, key="r1"):
        """The reconciler's RED UNCLAIMED: the offered red's self-transition (escalate) with its verdict."""
        it = self.st.item("red", key)
        line = f"RED UNCLAIMED: {A.red_what(it['data'])}: no taker for 10 min (no taker)"
        A.red_move(self.st, it, "offered", line, severity="escalate",
                   refs={"key": key, "verdict": line + " -- `~/bin/agentctl red claim ...`", "command": "red"})
        A.project_events(self.st)
        return self.st.item("red", key)


class RoutingTest(AlertBase):
    def test_not_landed_is_the_authors_and_one_live_alert_per_worktree(self):
        """84 identical `NOT LANDED: the pre-push checks failed` reached the owner in a week: the author's job."""
        for i in range(3):
            self.st.put_item("change", f"c{i}", "checking", {"worktree": self.wt})
            self.st.transition("change", f"c{i}", "rejected", 1, event=A.change_event(
                {"id": f"c{i}", "worktree": self.wt}, f"NOT LANDED: the pre-push checks failed (ci-runner); try {i}"))
        A.project_events(self.st)
        [e] = self.unread()
        self.assertEqual((e["audience"], e["count"], e["kind"]), ("author", 3, "NOT_LANDED"))
        self.assertIn("try 2", e["verdict"], "the live alert carries the newest event")
        self.assertEqual(self.owner_wait()[0], 2, "nothing for the owner")

    def test_watch_red_fan_out_is_one_per_red_per_author_and_never_the_owners(self):
        other = os.path.join(self.root, "movies-wt-b")
        os.makedirs(other)
        for wt in (self.wt, self.wt, self.wt, other):  # every commit in the red's range, two authors
            A.inbox_write("land", "WATCH RED: Main #2711 failed (your commit is in its range)", [], [], 3, cwd=wt)
        es = self.unread()
        self.assertEqual(sorted((os.path.basename(e["cwd"]), e["count"]) for e in es), [("movies-wt-a", 3), ("movies-wt-b", 1)])
        self.assertEqual({e["audience"] for e in es}, {"author"})
        self.assertEqual(self.owner_wait()[0], 2)

    def test_unknown_kinds_are_classified_ended_by_signal_and_usage_are_the_agents_own(self):
        self.assertEqual(A.inbox_kind("ended by signal 15"), "ENDED")
        self.assertEqual(A.inbox_kind("exited: wait run needs numeric run ids (action_run.id, the GITHUB_RUN_ID)"), "USAGE")
        for v in ("ended by signal 15", "exited: wait run needs numeric run ids (x)", "crashed: StoreUnavailable: POST"):
            A.inbox_write("land", v, [], [], 1, cwd=self.wt, reply=True)
        self.assertEqual({A.alert_audience(e) for e in self.unread()}, {"author"})
        self.assertEqual(self.owner_wait()[0], 2)

    def test_fixer_failures_and_dispatches_are_the_fixers(self):
        A.inbox_write("red fixer", "RED FIXER Main #764 [x]: STOPPED: the red's lease was lost", [], [], 1)
        A.inbox_write("red", "RED DISPATCHED: Main Pipeline #764 [x] -> fixer session 'red fixer #764'", [], [], 0)
        self.assertEqual({A.alert_audience(e) for e in self.unread()}, {"fixer"})
        self.assertEqual(self.owner_wait()[0], 2)

    def test_a_decision_is_the_owners_at_once(self):
        A.inbox_write("red", "RED BLOCKED ON OWNER: Conv #728 WAITING ON YOU: baseline the flips? -- `agentctl decide`",
                      [], [], 1)
        code, out = self.owner_wait()
        self.assertEqual(code, 0)
        self.assertIn("WAITING ON YOU", out)


class RedEscalationTest(AlertBase):
    def test_unclaimed_while_a_holder_exists_never_reaches_the_owner(self):
        """11-03: Main #2123 x81 RED UNCLAIMED while its family owner's lease had merely lapsed."""
        self.red()
        self.st.acquire(A.red_lease("r1"), "laptop:fixer:x", 3600, 7200)
        for _ in range(5):
            self.unclaimed()
        [e] = self.unread()
        self.assertEqual((e["audience"], e["count"]), ("fixer", 5))
        A.notify_escalations(self.st)
        self.assertEqual(self.shown(), [], "never notified")
        self.assertEqual(A.escalated_now(self.st), [])

    def test_unclaimed_while_the_family_owner_is_alive_is_the_fixers(self):
        self.red(workflow="Conv")
        self.st.acquire(A.owner_lease("conv"), A.owner_holder("conv"), 3600, 7200)
        for _ in range(3):
            self.unclaimed()
        self.assertEqual({e["audience"] for e in self.unread()}, {"fixer"})
        A.notify_escalations(self.st)
        self.assertEqual(self.shown(), [])

    def test_a_red_nobody_can_take_becomes_the_owners_once_per_level_then_updates_silently(self):
        self.red()
        self.unclaimed()
        [e] = self.unread()
        self.assertEqual(e["audience"], "fixer", "one offer round: the fixers' still")
        self.unclaimed()
        [e] = self.unread()
        self.assertEqual((e["audience"], e["surfaced"]), ("owner", 2), "nobody took it twice: the owner's, surfaced")
        for _ in range(6):
            self.unclaimed()
        [e] = self.unread()
        self.assertEqual((e["count"], e["surfaced"]), (8, 2), "the repeats update it; they surface nothing")
        A.notify_escalations(self.st)
        self.assertEqual(len(self.shown()), 1)
        code, out = self.owner_wait()
        self.assertEqual(code, 0)
        self.assertEqual(out.count("RED UNCLAIMED"), 1)

    def test_escalate_is_the_fixers_a_dispatch_follows(self):
        it = self.red()
        line = "RED ESCALATE: Main #2123 [test] still red after 14 attempts: a deep investigation is offered"
        for _ in range(4):
            it = A.red_move(self.st, it, "offered", line, severity="escalate", refs={"key": "r1", "verdict": line})
        A.project_events(self.st)
        [e] = self.unread()
        self.assertEqual((e["audience"], e["count"]), ("fixer", 4))
        A.notify_escalations(self.st)
        self.assertEqual(self.shown(), [])


class SelfCloseTest(AlertBase):
    def test_an_unclaimed_alert_closes_itself_once_the_red_is_taken(self):
        self.red()
        self.unclaimed()
        self.unclaimed()
        [e] = self.unread("owner")
        it = self.st.item("red", "r1")
        self.st.transition("red", "r1", "leased", it["version"])
        out = A.reconcile_alerts(self.st, force=True)
        self.assertTrue(any("closed red:r1" in x for x in out), out)
        self.assertEqual(self.unread(), [])
        self.assertIn("taken", A.read_record(os.path.join(self.inbox, e["id"] + ".json"))["closed"]["why"])

    def test_a_landed_verdict_closes_its_worktrees_not_landed(self):
        A.inbox_write("land", "NOT LANDED: the pre-push checks failed", [], [], 1, cwd=self.wt)
        A.inbox_write("land", "LANDED abc1234 (queued e9)", [], [], 0, cwd=self.wt)
        self.assertEqual([e["kind"] for e in self.unread()], ["LANDED"])

    def test_a_removed_worktree_closes_its_alerts(self):
        A.inbox_write("land", "NOT LANDED: the pre-push checks failed", [], [], 1, cwd=self.wt)
        os.rmdir(self.wt)
        A.reconcile_alerts(self.st, force=True)
        self.assertEqual(self.unread(), [])

    def test_an_authors_read_alert_is_shown_again_with_each_new_verdict(self):
        a = A.inbox_write("land", "NOT LANDED: one", [], [], 1, cwd=self.wt)
        A.alert_ack(self.inbox, a)
        b = A.inbox_write("land", "NOT LANDED: two", [], [], 1, cwd=self.wt)
        self.assertEqual(a, b)
        [e] = self.unread()
        self.assertEqual((e["verdict"], e["count"]), ("NOT LANDED: two", 2))

    def test_the_owners_read_alert_is_not_re_told_by_repeats(self):
        """Reading an alert does not end it: the item's probe does. The owner read RED UNCLAIMED; 20 repeats later the
        inbox holds the same read entry, updated."""
        self.red()
        self.unclaimed()
        self.unclaimed()
        [e] = self.unread("owner")
        A.alert_ack(self.inbox, e["id"])
        for _ in range(20):
            self.unclaimed()
        self.assertEqual(self.unread(), [])
        self.assertEqual(A.read_record(os.path.join(self.inbox, e["id"] + ".json"))["count"], 22)


class HysteresisTest(AlertBase):
    def test_a_self_healing_invariant_is_the_fixers_until_it_outlives_its_bound(self):
        saved = A.INVARIANTS["W5"]
        A.INVARIANTS["W5"] = lambda clock, st: (False, "the processor has not beaten for 10 min")
        try:
            p = A.run_probe("invariant", {"id": "W5"})
            A.open_condition("INVARIANT", "INVARIANT W5 violated: the processor has not beaten", p, [],
                             key="invariant-W5", command="watchdog", store=self.st)
        finally:
            A.INVARIANTS["W5"] = saved
        [e] = self.unread()
        self.assertEqual(e["audience"], "fixer")
        self.assertEqual(self.owner_wait()[0], 2)
        it = A.condition_item_of(self.st, "invariant-W5")
        self.st.update_item("condition", it["id"], it["version"], {"opened_at": time.time() - 2 * 3600})
        A.reconcile_alerts(self.st, force=True)
        [e] = self.unread()
        self.assertEqual((e["audience"], e["surfaced"]), ("owner", 2), "outlived its bound: the owner's now")
        self.assertEqual(self.owner_wait()[0], 0)

    def test_other_invariants_are_the_owners_at_once(self):
        self.assertEqual(A.alert_condition_audience("INVARIANT", {"verdict": "INVARIANT W6 violated: x"}, time.time(),
                                                    time.time()), "owner")
        self.assertEqual(A.alert_condition_audience("INVARIANT", {"verdict": "INVARIANT WM violated: x"}, time.time(),
                                                    time.time()), "fixer")


class MigrationTest(AlertBase):
    def write_old(self, i, verdict, cwd=None, command="red"):
        """An entry as the code before the model wrote it: no audience, no item, no index."""
        eid = f"20261103T0000{i:02d}000000-1-{command.replace(' ', '-')}"
        rec = {"id": eid, "time": "t", "at": 1000.0 + i, "command": command, "cwd": cwd or self.wt, "final": True,
               "exit": 1, "verdict": verdict, "kind": A.inbox_kind(verdict), "verdicts": [verdict], "tail": []}
        os.makedirs(self.inbox, exist_ok=True)
        A.write_json(os.path.join(self.inbox, eid + ".json"), rec)
        return eid

    def test_the_switch_over_folds_the_backlog_and_floods_nobody(self):
        for i in range(20):
            self.write_old(i, f"RED UNCLAIMED: Main #2123 [test]: no taker for {10 * i} min (no taker)")
        for i in range(20, 25):
            self.write_old(i, "NOT LANDED: the pre-push checks failed (on ci-runner)", command="land")
        self.write_old(30, "RED BLOCKED ON OWNER: Conv #728 WAITING ON YOU: decide")
        code, out = self.owner_wait()
        self.assertEqual(out.count("UNREAD"), 1, out)  # only the decision
        self.assertIn("WAITING ON YOU", out)
        es = self.unread()
        self.assertEqual(sorted((e["kind"], e["count"]) for e in es),
                         [("BLOCKED", 1), ("NOT_LANDED", 5), ("RED_UNCLAIMED", 20)])


class ClearedOwnerAlertTest(AlertBase):
    """The owner, 2026-11-06: "why is there always 30 unread in inbox" -- 44 owner alerts, nearly all of them for things
    that had cleared days before: reds cleared, Forgejo-era orphan worktrees gone, the network back, a watch's commit
    long since decided, a held batch moved on. Each alert's condition is probed on the reconcile pass (bounded, at most
    every ALERT_RECONCILE_EVERY) and acknowledged with a resolution note once it has cleared -- whatever item key an
    older entry was written under."""

    def legacy(self, verdict, item, cwd=None, command="red", at=None):
        """An owner alert as written before its item was typed (a `run:`/`text:`/`orphan:` key no probe knew)."""
        at = time.time() - 7200 if at is None else at
        rec = {"id": f"20261101T000000{len(os.listdir(self.inbox)) if os.path.isdir(self.inbox) else 0:06d}-1-{command}",
               "time": "t", "at": at, "command": command, "cwd": cwd or self.wt, "final": True, "exit": 1,
               "verdict": verdict, "kind": A.inbox_kind(verdict), "verdicts": [verdict], "tail": [],
               "audience": "owner", "item": item}
        return A.alert_write(rec)

    def closed_why(self, eid):
        return (A.read_record(os.path.join(self.inbox, eid + ".json")).get("closed") or {}).get("why")

    def test_a_decision_on_a_red_that_has_since_closed_is_resolved(self):
        self.st.put_item("red", "50dd506d93c9.2", "blocked-external",
                         {"key": "50dd506d93c9", "workflow": "Record scrape fixtures", "last_run": {"number": 172}})
        self.st.put_item("red", "abc.1", "blocked-external",
                         {"key": "abcabcabcabc", "workflow": "Main", "last_run": {"number": 2128}})
        gone = self.legacy("RED BLOCKED ON OWNER: WAITING ON YOU: fix the stuck limit, and treat queue waits on b -- "
                           "Record scrape fixtures #172 [stuck] waits on it. Answer: `! ~/bin/agentctl decide "
                           "50dd506d93c9 \"<your answer>\"`", "run:and treat queue waits on b -- Record scrape fixtures #172")
        live = self.legacy("RED BLOCKED ON OWNER: WAITING ON YOU: accept Hen as known-wrong. -- Main #2128",
                           "run:or accept Hen as known-wrong. -- Main #2128")
        it = self.st.item("red", "50dd506d93c9.2")
        self.st.transition("red", it["id"], "cleared", it["version"])
        A.reconcile_alerts(self.st, force=True)
        self.assertIn("red", self.closed_why(gone) or "")
        self.assertIsNone(self.closed_why(live), "its red is still waiting on the owner")
        self.assertEqual([e["id"] for e in self.unread("owner")], [live])

    def test_an_alert_naming_an_earlier_run_of_a_still_open_red_stays_open(self):
        """Review 2026-11-07: a red stays ONE item while later runs fail the same way (red_attach_obs moves its
        last_run on), so an alert written against #2128 (FIX FAILED, RED DISPATCHED: no `decide <key>`) names a run
        the red still carries in its runs -- it is live, never 'its red is closed'. #214, a prefix of a run the red
        carries (#2140), is not one of them."""
        self.st.put_item("red", "abc.1", "offered", {"key": "abcabcabcabc", "workflow": "Main",
                                                     "last_run": {"number": 2140}, "runs": ["2128", "2140", "9001"]})
        live = self.legacy("FIX FAILED: Main #2128 [test]: #2128 still fails this way (fix 1234567890)",
                           "run:fix failed -- Main #2128")
        gone = self.legacy("FIX FAILED: Main #214 [test]: #214 still fails this way (fix 1234567890)",
                           "run:fix failed -- Main #214")
        A.reconcile_alerts(self.st, force=True)
        self.assertIsNone(self.closed_why(live), "its red is open, at a later run of the same failure")
        self.assertIn("red", self.closed_why(gone) or "", "#214 is no run of the open red")

    def test_a_red_names_the_jobs_failing_in_its_newest_run_not_earlier_runs(self):
        """Issue 20261104T040527-1b4212: convergence #834's turn prompt and RED-INBOX named `leg (germany) / takes
        (germany)` as failing although gh showed only the UK leg failed in #834: red_attach_obs unions the jobs of every
        attached run into the red, so a job that failed in #818 stays named after it passed."""
        self.st.put_item("red", "8fbdbe0d5a86.1", "offered",
                         {"key": "8fbdbe0d5a86", "workflow": "Identity model convergence", "job": "leg (germany) / takes (germany)",
                          "jobs": ["leg (germany) / takes (germany)"], "runs": ["818"],
                          "last_run": {"number": 818, "databaseId": 100}})
        it = self.st.item("red", "8fbdbe0d5a86.1")
        ob = {"names": ["834", "200"], "jobs": ["leg (united-kingdom) / takes (united-kingdom)"],
              "run": {"number": 834, "databaseId": 200}}
        got = A.red_attach_obs(self.st, it, ob)["data"]
        self.assertEqual(got["last_run"]["number"], 834)
        self.assertEqual(got["jobs"], ["leg (united-kingdom) / takes (united-kingdom)"], got["jobs"])

    def test_an_unlanded_orphan_closes_once_its_worktree_is_gone_or_its_commits_are_on_main(self):
        import subprocess
        origin = os.path.join(self.root, "origin.git")
        subprocess.run(["git", "init", "-q", "--bare", "-b", "main", origin], check=True)
        def clone(name):
            wt = os.path.join(self.root, name)
            subprocess.run(["git", "clone", "-q", origin, wt], check=True, capture_output=True)
            subprocess.run(["git", "-C", wt, "checkout", "-q", "-B", "main"], check=True)
            return wt
        base = clone("base")
        subprocess.run(["git", "-C", base, "commit", "-q", "--allow-empty", "-m", "base"], check=True)
        subprocess.run(["git", "-C", base, "push", "-q", "origin", "main"], check=True, capture_output=True)
        landed, unlanded = clone("movies-wt-landed"), clone("movies-wt-unlanded")
        for wt, push in ((landed, True), (unlanded, False)):
            with open(os.path.join(wt, "f"), "w") as f:
                f.write(wt)
            subprocess.run(["git", "-C", wt, "add", "f"], check=True)
            subprocess.run(["git", "-C", wt, "commit", "-q", "-m", f"change {os.path.basename(wt)}"], check=True)
            if push:
                subprocess.run(["git", "-C", wt, "push", "-q", "origin", "main"], check=True, capture_output=True)
        gone_wt = os.path.join(self.root, "movies-wt-gone")
        ids = {wt: self.legacy(f"UNLANDED ORPHAN: {wt}: 1 commit(s) not on forgejo/main, its author gone", f"orphan:{wt}",
                               cwd=wt, command="maintain") for wt in (landed, unlanded, gone_wt)}
        A.reconcile_alerts(self.st, force=True)
        self.assertIn("gone", self.closed_why(ids[gone_wt]) or "")
        self.assertIn("on main", self.closed_why(ids[landed]) or "")
        self.assertIsNone(self.closed_why(ids[unlanded]), "its commit is still not on main: the owner's to decide")

    def test_a_watch_unknown_closes_once_a_later_gate_run_has_decided(self):
        state = os.path.join(self.root, "state.json")
        os.environ["AGENTCTL_STATE_FILE"] = state
        self.addCleanup(os.environ.pop, "AGENTCTL_STATE_FILE", None)
        at = time.time() - 3600
        eid = self.legacy("WATCH UNKNOWN: Main or the convergence still undecided for bc29c46b5e after 3 h: a stuck run",
                          "text:x", command="watch", at=at)
        def gate(created):
            iso = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(created))
            A.write_json(state, {"ci": {"gate": [{"name": n, "decided": {"conclusion": "success", "createdAt": iso}}
                                                 for n in ("Main", "Identity model convergence")]}})
        gate(at - 600)
        A.reconcile_alerts(self.st, force=True)
        self.assertIsNone(self.closed_why(eid), "no gate run decided since: still unknown")
        gate(at + 600)
        A.reconcile_alerts(self.st, force=True)
        self.assertIn("decided", self.closed_why(eid) or "")

    def test_a_network_alert_closes_once_no_network_condition_is_open(self):
        eid = self.legacy("NETWORK: the laptop's uplink loses 40% of packets to 1.1.1.1, so Forgejo timeouts",
                          "text:net", command="tunnel")
        A.reconcile_alerts(self.st, force=True)
        self.assertIn("network", self.closed_why(eid) or "")

    def test_a_held_batch_closes_once_the_batch_moved_on(self):
        self.st.put_item("batch", "b1", "held", {"tip": "1111111111aaaa", "base": "x", "tree": 1})
        held = self.legacy("HELD: the batch check of 1111111111 (2 change(s)) stalled twice on a fresh sbt server",
                           "text:held1", command="batch")
        moved = self.legacy("HELD: the batch check of 7368dcc7f4 (2 change(s)) stalled twice on a fresh sbt server",
                            "text:held2", command="batch")
        A.reconcile_alerts(self.st, force=True)
        self.assertIsNone(self.closed_why(held))
        self.assertIn("batch", self.closed_why(moved) or "")

    def test_a_closed_issues_alert_retires_itself(self):
        """2026-11-19: `AGENTCTL ISSUE 20261104T044122-55799d open 15 d` reached the owner, the issue was closed, and
        its alert stayed unread: no probe knew an `issue:` item. An alert about a store item with a state (an issue, a
        hold, an explorer flip...) closes, acked with why, once that item is in a terminal state or gone."""
        quoted = ("red c494fec65b9d (Main #2128) went RED BLOCKED ON OWNER (inbox ev3602, 'WAITING ON YOU: either "
                  "design and measure...') and 13 min later RED ESCALATE")  # the quote routes it to the owner
        it = A.issue_open(self.st, "A red parked on the owner is offered back: " + quoted, "session 8de717dc")
        open_one = A.issue_open(self.st, "Still open: a fixer read 'WAITING ON YOU: accept Hen as known-wrong' as no "
                                "decision", "session x")
        t = time.time() + 8 * 86400
        for n in range(2):  # the reconciler's weekly escalation, one issue a pass
            self.assertTrue(A.tick_issues(self.st, now_=t + n * 3600))
        A.project_events(self.st)
        alerts = {e.get("item"): e["id"] for e in self.unread("owner")}
        self.assertIn(f"issue:{it['id']}", alerts, alerts)
        A.issue_close(self.st, it["id"], "fixed in abc", "maintainer")
        A.reconcile_alerts(self.st, force=True)
        self.assertIn("issue is closed", self.closed_why(alerts[f"issue:{it['id']}"]) or "")
        self.assertIsNone(self.closed_why(alerts[f"issue:{open_one['id']}"]), "its issue is still open")
        self.assertNotIn(alerts[f"issue:{it['id']}"], [e["id"] for e in self.unread()])

    def test_an_alert_about_a_store_item_gone_from_the_store_retires_itself(self):
        gone = self.legacy("AGENTCTL ISSUE 20261101T000000-abcdef open 9 d (filed by x): y", "issue:20261101T000000-abcdef",
                           command="issue")
        self.st.put_item("hold", "main", "held", {"by": "x"})
        hold = self.st.item("hold", "main")
        held = self.legacy("HOLD main: landing held by x", "hold:main", command="land")
        A.reconcile_alerts(self.st, force=True)
        self.assertIn("gone", self.closed_why(gone) or "")
        self.assertIsNone(self.closed_why(held), "the hold is still held")
        self.st.transition("hold", "main", "released", hold["version"])
        A.reconcile_alerts(self.st, force=True)
        self.assertIn("hold is released", self.closed_why(held) or "")


if __name__ == "__main__":
    unittest.main()
