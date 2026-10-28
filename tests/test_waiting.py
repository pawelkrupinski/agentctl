"""agentctl land (the owner, 2026-10-27): every change runs its OWN checks first, in parallel within the check slots;
then, main green, it pushes; main red, a production change joins ONE `waiting` branch by cherry-pick and land returns at
WAITING. At green the processor rebuilds `waiting` on main, runs ONE fast check of the batch (compile + fast tests),
pushes it in one push, bisects a failing batch by prefix, and sends every verdict to the inbox."""
import json, os, stat, sys, time, unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from test_agentctl import A, Base, StallSamples, sh  # noqa: E402

REPO_HOOK = """#!/bin/sh
# supports --checks-only (the checks without the red gate) and --fast-only (compile and the fast tests)
echo "$*" >> "{calls}"
if git ls-files | grep -q BROKEN; then echo "[error] BROKEN is in the tree"; exit 1; fi
if git ls-files | grep -q PartA && git ls-files | grep -q PartB; then echo "[error] PartA and PartB disagree"; exit 1; fi
exit 0
"""


class WaitingTest(Base):
    def setUp(self):
        super().setUp()
        t = self.tmp.name
        self.saved = (A.WAITING_DIR, A.gate_reds, A.spawn_waiting_processor, A.await_entry,
                      A.time.sleep, A.watch_landed)
        A.WAITING_DIR = os.path.join(t, "waiting-state")
        self.reds = {"7"}
        A.gate_reds = lambda repo: set(self.reds)
        A.spawn_waiting_processor = lambda repo: None
        self.watches = []
        self.saved_spawn_watch = A.spawn_watch
        A.spawn_watch = lambda repo, e, pushed_at: self.watches.append(e["tip"])
        self.saved_spawn_watch_of = A.spawn_watch_of
        A.spawn_watch_of = lambda repo, sha, pushed_at, close=None, fixes=(): self.watches.append(sha)
        os.environ["AGENTCTL_CHECKS_DIR"] = os.path.join(t, "checks")
        self.saved_inbox = os.environ.get("AGENTCTL_INBOX_DIR")
        os.environ["AGENTCTL_INBOX_DIR"] = os.path.join(t, "inbox")
        self.awaited = []
        A.await_entry = lambda repo, eid, close=None: (self.awaited.append(eid), (9, None, None))[1]
        A.time.sleep = lambda s: None
        A.watch_landed = lambda repo, sha, pushed_at, o_r=None, close=None, fixes=(): 0
        os.environ.pop("PREPUSH_SKIP", None)
        self.calls, self.pushes = os.path.join(t, "hook-calls"), os.path.join(t, "pushes")
        hooks = os.path.join(t, "hooks")
        os.makedirs(hooks)
        with open(os.path.join(hooks, "pre-push"), "w") as f:
            f.write(f'#!/bin/sh\ncat > /dev/null\necho push >> "{self.pushes}"\n')
        os.chmod(os.path.join(hooks, "pre-push"), 0o755)
        sh(self.r.path, "git", "config", "core.hooksPath", hooks)
        self.r.write("scripts/hooks/pre-push", REPO_HOOK.format(calls=self.calls))
        os.chmod(os.path.join(self.r.path, "scripts/hooks/pre-push"), 0o755)
        self.r.write("web/src/main/Shared.scala", "line 1\n")
        self.r.commit("the repo's hook and a shared source")
        self.r.push()
        for p in (self.pushes, self.calls):
            if os.path.exists(p):
                os.remove(p)

    def tearDown(self):
        (A.WAITING_DIR, A.gate_reds, A.spawn_waiting_processor, A.await_entry, A.time.sleep,
         A.watch_landed) = self.saved
        A.spawn_watch = self.saved_spawn_watch
        A.spawn_watch_of = self.saved_spawn_watch_of
        os.environ.pop("AGENTCTL_CHECKS_DIR", None)
        if self.saved_inbox is None:
            os.environ.pop("AGENTCTL_INBOX_DIR", None)
        else:
            os.environ["AGENTCTL_INBOX_DIR"] = self.saved_inbox
        for k in ("AGENTCTL_LAND_STALL_MIN",):
            os.environ.pop(k, None)
        super().tearDown()

    def agent(self, name, path, text, msg=None):
        wt = self.r.worktree(f"wt-{name}", branch=name)
        self.r.write(path, text, at=wt)
        return wt, self.r.commit(msg or f"{name}: {path}", at=wt)

    def land(self, wt, *extra):
        return self.run_tool("land", *extra, cwd=wt)

    def remote_log(self):
        return sh(self.r.root, "git", "--git-dir", self.r.bare, "log", "--format=%s", "main").splitlines()

    def waiting_log(self):
        return sh(self.r.path, "git", "log", "--format=%s", "forgejo/main..waiting").splitlines()

    def pushes_count(self):
        return len(open(self.pushes).read().split()) if os.path.exists(self.pushes) else 0

    def inbox(self):
        A.project_events()  # as the processor's loop does each cycle (step 7: the events are the inbox's source)
        return A.inbox_entries()

    def entries(self):
        d, _c, _w = A.waiting_state(self.r.path)
        return {e["id"].rsplit("-", 1)[-1]: e for e in A.waiting_entries(d, open_only=False)}

    def calls_of(self, mode):
        return [l for l in open(self.calls).read().splitlines() if l.startswith(mode)] if os.path.exists(self.calls) else []

    def test_green_main_lands_directly_after_its_own_checks(self):
        self.reds = set()
        wt, sha = self.agent("a", "web/src/main/A.scala", "a\n")
        rc, out = self.land(wt)
        self.assertEqual(rc, 0, out)
        self.assertIn(f"LANDED {sha}", out)
        self.assertEqual(len(self.calls_of("--range")), 1)
        self.assertEqual(self.watches, [sha], "the watch is detached")
        self.assertFalse(self.entries())

    def test_red_main_own_checks_first_then_join_by_cherry_pick_and_one_fast_check_and_push_at_green(self):
        wt1, s1 = self.agent("a", "web/src/main/A.scala", "a\n")
        wt2, s2 = self.agent("b", "worker/src/main/B.scala", "b\n")
        rc, out = self.land(wt1)
        self.assertEqual(rc, 0, out)
        self.assertRegex(out, r"WAITING [0-9a-f]{40} \(entry \S+-1, 1 in batch\)")
        rc, out = self.land(wt2)
        self.assertRegex(out, r"WAITING [0-9a-f]{40} \(entry \S+-2, 2 in batch\)")
        self.assertEqual(len(self.calls_of("--checks-only")), 2, "each change's own checks ran first, without the gate")
        self.assertEqual(self.pushes_count(), 0)
        A.waiting_run(self.r.path, once=True)  # still red: nothing moves
        self.assertEqual(self.calls_of("--fast-only"), [])
        self.reds = set()
        A.waiting_run(self.r.path, once=True)
        self.assertEqual(len(self.calls_of("--fast-only")), 1, "ONE fast check of the rebuilt batch")
        self.assertEqual(self.calls_of("--range"), [], "no full check of the batch: each passed its own")
        self.assertEqual(self.pushes_count(), 1, "ONE push")
        self.assertEqual(self.remote_log()[:2], ["b: worker/src/main/B.scala", "a: web/src/main/A.scala"])
        e = self.entries()
        self.assertEqual([e["1"]["status"], e["2"]["status"]], ["landed", "landed"])
        self.assertEqual(sorted(x["cwd"] for x in self.inbox() if x["verdict"].startswith("LANDED")), sorted([wt1, wt2]))
        self.assertEqual(len(self.watches), 2)

    def test_a_failing_own_check_never_joins(self):
        wt, _ = self.agent("b", "web/src/main/BROKEN.scala", "b\n")
        rc, out = self.land(wt)
        self.assertEqual(rc, 1, out)
        self.assertIn("NOT LANDED: the pre-push checks failed", out)
        self.assertFalse(self.entries())

    def test_tools_only_and_a_fix_land_directly_while_red(self):
        wt, sha = self.agent("t", "scripts/x.sh", "echo\n")
        rc, out = self.land(wt)
        self.assertIn(f"LANDED {sha}", out)
        wf, fsha = self.agent("f", "web/src/main/Fix.scala", "fix\n", msg="f: fix\n\nFixes-Red: 7")
        rc, out = self.land(wf)
        self.assertIn(f"LANDED {fsha}", out)
        self.assertFalse(self.entries())

    def test_fixes_red_entries_go_first_in_the_batch(self):
        wa, _ = self.agent("a", "web/src/main/A.scala", "a\n")
        self.land(wa)
        d, _c, wt = A.waiting_state(self.r.path)
        e = {"id": "x-9", "seq": 9, "worktree": wa, "head": "", "commits": [], "fixes": ["7"], "status": "waiting"}
        self.assertEqual([x["id"] for x in A.batch_order(self.r.path, A.waiting_entries(d) + [e], {"7"})][0], "x-9")

    def test_a_conflict_at_join_is_the_only_rejection_there(self):
        wt1, _ = self.agent("a", "web/src/main/Shared.scala", "line 1 by a\n")
        wt2, _ = self.agent("b", "web/src/main/Shared.scala", "line 1 by b\n")
        self.land(wt1)
        rc, out = self.land(wt2)
        self.assertEqual(rc, 1, out)
        self.assertIn("NOT LANDED: the change conflicts with the changes already on `waiting`", out)
        self.assertEqual(self.waiting_log(), ["a: web/src/main/Shared.scala"])

    def test_a_join_rebuilds_waiting_on_the_current_main(self):
        wt1, _ = self.agent("a", "web/src/main/A.scala", "a\n")
        self.land(wt1)
        self.r.write("web/src/main/Shared.scala", "line 1 on main\n")
        self.r.commit("main changed the shared line")
        self.r.push()
        wt2 = self.r.worktree("wt-b", branch="b")
        self.r.write("web/src/main/Shared.scala", "line 1 by b\n", at=wt2)
        self.r.commit("b: Shared.scala", at=wt2)
        rc, out = self.land(wt2)
        self.assertNotIn("conflicts", out)
        self.assertEqual(sh(self.r.path, "git", "merge-base", "waiting", "forgejo/main"),
                         sh(self.r.path, "git", "rev-parse", "forgejo/main"))

    def test_a_failing_batch_rejects_only_the_culprit_by_prefix_and_lands_the_rest(self):
        wts = [self.agent("a", "web/src/main/PartA.scala", "a\n")[0], self.agent("b", "web/src/main/PartB.scala", "b\n")[0],
               self.agent("c", "web/src/main/C.scala", "c\n")[0]]
        for wt in wts:
            rc, out = self.land(wt)
            self.assertEqual(rc, 0, out)  # each passes its own checks
        self.reds = set()
        A.waiting_run(self.r.path, once=True)
        e = self.entries()
        self.assertEqual([e["1"]["status"], e["2"]["status"], e["3"]["status"]], ["landed", "rejected", "waiting"])
        self.assertIn("PartA and PartB disagree", e["2"]["why"])
        self.assertEqual(self.remote_log()[0], "a: web/src/main/PartA.scala", "the passing prefix lands at once")
        bad = [x for x in self.inbox() if x["verdict"].startswith("NOT LANDED")]
        self.assertEqual([x["cwd"] for x in bad], [wts[1]])
        A.waiting_run(self.r.path, once=True)
        self.assertEqual(self.remote_log()[0], "c: web/src/main/C.scala", "the rest lands with the next batch")

    def test_a_join_during_the_check_waits_for_the_next_batch(self):
        wa, _ = self.agent("a", "web/src/main/A.scala", "a\n")
        wb, _ = self.agent("b", "web/src/main/B.scala", "b\n")
        self.land(wa)
        self.reds = set()
        check, joined = A.batch_check, []

        def joining(repo, d, tree, base, tip, cls="check"):
            if not joined:
                self.reds = {"7"}  # red again for a moment: b joins while a's batch is being checked
                joined.append(self.land(wb))
                self.reds = set()
            return check(repo, d, tree, base, tip, cls)
        A.batch_check = joining
        try:
            A.waiting_run(self.r.path, once=True)
        finally:
            A.batch_check = check
        self.assertEqual(joined[0][0], 0, joined[0][1])
        self.assertEqual(self.remote_log()[0], "a: web/src/main/A.scala")
        self.assertEqual(self.entries()["2"]["status"], "waiting")
        A.waiting_run(self.r.path, once=True)
        self.assertEqual(self.remote_log()[0], "b: web/src/main/B.scala")

    def hung_sbt(self, hook_body):
        """The repo's hook calls scripts/sbt-warm, a fake whose command hangs like the sbtn of 2026-10-27; --stop logs."""
        self.stops, hung = os.path.join(self.tmp.name, "sbt-stops"), os.path.join(self.tmp.name, "sbt-hung")
        # hung: the stall clock runs only while a hung sbt is there (a --stop ends it), never on the machine's load
        self.r.write("scripts/sbt-warm", f'#!/bin/sh\n[ "$1" = --stop ] && {{ echo stop >> "{self.stops}"; rm -f "{hung}"; '
                                         f'exit 0; }}\ntouch "{hung}"\nsleep 60\n')
        StallSamples(hung=hung).install(self)
        os.chmod(os.path.join(self.r.path, "scripts/sbt-warm"), 0o755)
        self.r.write("scripts/hooks/pre-push", REPO_HOOK.format(calls=self.calls).replace("exit 0\n", hook_body + "\nexit 0\n"))
        os.chmod(os.path.join(self.r.path, "scripts/hooks/pre-push"), 0o755)
        self.r.commit("a hanging sbt")
        self.r.push()
        os.remove(self.pushes)

    def test_a_hung_sbt_in_the_batch_check_is_retried_on_a_fresh_server_then_held(self):
        self.hung_sbt('case "$1" in --fast-only) scripts/sbt-warm compile ;; esac')
        wa, _ = self.agent("a", "web/src/main/A.scala", "a\n")
        self.land(wa)
        self.reds = set()
        os.environ["AGENTCTL_LAND_STALL_MIN"] = "0.02"  # 1.2 s
        A.waiting_run(self.r.path, once=True)
        self.assertEqual(len(self.calls_of("--fast-only")), 2, "a stalled check is retried once")
        self.assertEqual(len(open(self.stops).read().split()), 4, "on a FRESH server: stopped before and after each")
        self.assertEqual(self.entries()["1"]["status"], "waiting", "held, not rejected")
        self.assertEqual(self.pushes_count(), 0)
        self.assertEqual(len([x for x in self.inbox() if x["verdict"].startswith("HELD")]), 1)
        d, _c, w = A.waiting_state(self.r.path)
        self.assertGreater(A.batch_state(d)["held_until"], time.time())
        self.assertEqual(A.waiting_round(self.r.path, d, w), "held")
        self.run_tool("waiting", "resume", cwd=self.r.path)
        self.assertFalse(A.batch_state(d).get("held_until"))

    def test_the_hooks_stall_signal_counts_as_a_stall(self):
        self.hung_sbt('case "$1" in --fast-only) echo "pre-push STALLED: sbt (no progress for 1200 s)"; exit 75 ;; esac')
        wa, _ = self.agent("a", "web/src/main/A.scala", "a\n")
        self.land(wa)
        self.reds = set()
        A.waiting_run(self.r.path, once=True)
        self.assertEqual(len(self.calls_of("--fast-only")), 2)
        self.assertTrue([x for x in self.inbox() if x["verdict"].startswith("HELD")])

    def test_a_batch_lands_with_no_land_process_alive_and_its_watch_verdict_reaches_the_inbox(self):
        A.spawn_watch = self.saved_spawn_watch  # the real, detached watch
        wa, _ = self.agent("a", "web/src/main/A.scala", "a\n")
        rc, out = self.land(wa)  # the agent's turn ends here: no land process waits on the entry
        self.assertEqual(rc, 0)
        self.reds = set()
        A.waiting_run(self.r.path, once=True, service=True)  # the service processor
        sha = self.entries()["1"]["landed_sha"]
        deadline = time.time() + 180
        while time.time() < deadline and not [x for x in self.inbox() if x["command"] == "watch"]:
            time.sleep(0.2)
        verdicts = {x["command"]: x for x in self.inbox() if x.get("final", True)}
        self.assertTrue(verdicts["land"]["verdict"].startswith(f"LANDED {sha}"))
        self.assertIn(sha, verdicts["watch"]["argv"])
        self.assertEqual((verdicts["watch"]["exit"], verdicts["watch"]["cwd"]), (0, wa))

    def test_the_service_processor_stays_cheap_while_waiting_is_empty(self):
        calls = []
        saved = A.waiting_round
        A.waiting_round = lambda *a: calls.append(a) or "empty"
        try:
            self.assertEqual(A.waiting_run(self.r.path, once=True, service=True), 0)
        finally:
            A.waiting_round = saved
        self.assertEqual(calls, [], "an empty `waiting` is a directory listing: no git, no gate, no lock")

    def test_land_follow_still_waits_and_watches(self):
        A.await_entry = lambda repo, eid, close=None: (print("LANDED abc (with …)"), (0, "abc", 1.0))[1]
        watched = []
        A.watch_landed = lambda repo, sha, pushed_at, o_r=None, close=None, fixes=(): (watched.append(sha), 3)[1]
        wt, _ = self.agent("a", "web/src/main/A.scala", "a\n")
        rc, out = self.land(wt, "--follow")
        self.assertEqual(rc, 3, out)
        self.assertEqual(watched, ["abc"])

    def test_status_pause_and_cancel(self):
        wt1, _ = self.agent("a", "web/src/main/A.scala", "a\n")
        self.land(wt1)
        self.run_tool("waiting", "pause", cwd=self.r.path)
        d, _c, w = A.waiting_state(self.r.path)
        self.assertEqual(A.waiting_round(self.r.path, d, w), "paused")
        rc, out = self.run_tool("waiting", "status", cwd=self.r.path)
        self.assertIn("PAUSED", out)
        self.run_tool("waiting", "resume", cwd=self.r.path)
        rc, out = self.run_tool("waiting", "cancel", "-1", cwd=self.r.path)
        self.assertEqual(rc, 0, out)
        self.assertEqual(self.entries()["1"]["status"], "cancelled")

    def test_an_unread_gate_holds_the_batch_and_says_so_once(self):
        # 08:05Z 10-27: the processor could not read the gate (no token under launchd), took it for green, and pushed
        wt1, _ = self.agent("a", "web/src/main/A.scala", "a\n")
        self.land(wt1)
        self.reds = {A.GATE_UNREAD}
        A.gate_unread_why[0] = "could not read the movies FORGEJO_API_TOKEN from the vault"
        A.waiting_run(self.r.path, once=True)
        A.waiting_run(self.r.path, once=True)
        self.assertEqual(self.pushes_count(), 0, "an unread gate is never green")
        self.assertEqual(self.calls_of("--fast-only"), [])
        notes = [e for e in self.inbox() if e["verdict"].startswith("UNKNOWN: the waiting processor cannot read the gate")]
        self.assertEqual(len(notes), 1, "said once per unread spell")
        self.assertEqual(notes[0]["kind"], "UNKNOWN")
        rc, out = self.run_tool("waiting", "status", cwd=self.r.path)
        self.assertIn("gate: UNREAD, taken as red", out)
        self.assertNotIn("green", out)
        self.reds = set()
        A.waiting_run(self.r.path, once=True)
        self.assertEqual(self.pushes_count(), 1, "read green again: the batch lands")

    def test_land_with_an_unread_gate_joins_waiting(self):
        self.reds = {A.GATE_UNREAD}
        wt, _ = self.agent("a", "web/src/main/A.scala", "a\n")
        rc, out = self.land(wt)
        self.assertEqual(rc, 0, out)
        self.assertIn("WAITING", out)
        self.assertEqual(self.pushes_count(), 0)

    def test_the_token_is_handed_to_a_listening_processor(self):
        import tempfile
        sockdir = tempfile.mkdtemp(dir="/tmp", prefix="agentctl-sock-")
        saved = {k: os.environ.get(k) for k in ("AGENTCTL_SOCK_DIR", "FORGEJO_API_TOKEN", "GH_TOKEN")}
        os.environ["AGENTCTL_SOCK_DIR"] = sockdir
        os.environ.pop("GH_TOKEN", None)
        os.environ.pop("FORGEJO_API_TOKEN", None)
        saved_slug, saved_token = A.slug, A.token
        A.slug = lambda repo: "pawelkrupinski/movies"
        try:
            d, _c, _w = A.waiting_state(self.r.path)
            drop = A.TokenDrop(d, "pawelkrupinski/movies").open()
            self.assertEqual(oct(os.stat(drop.path).st_mode & 0o777), "0o600")
            A.token = lambda o_r: "tok-0123456789abcdef"
            self.assertTrue(A.offer_token(self.r.path))
            drop.nap(0.5)
            self.assertEqual(os.environ.get("FORGEJO_API_TOKEN"), "tok-0123456789abcdef")
            drop.close()
            self.assertFalse(os.path.exists(drop.path))
            self.assertFalse(A.offer_token(self.r.path), "nobody listens: nothing handed over")
        finally:
            A.slug, A.token = saved_slug, saved_token
            for k, v in saved.items():
                os.environ.pop(k, None) if v is None else os.environ.__setitem__(k, v)

    def test_a_detached_child_is_handed_the_token_in_its_environment(self):
        saved = A.token
        A.token = lambda o_r: "tok-abcdef0123456789"
        try:
            env = A.token_env("pawelkrupinski/movies", {})
            self.assertEqual(env, {"FORGEJO_API_TOKEN": "tok-abcdef0123456789"})

            def none(o_r):
                raise A.NoToken("locked")
            A.token = none
            self.assertEqual(A.token_env("pawelkrupinski/movies", {"X": "1"}), {"X": "1"})
        finally:
            A.token = saved

    def test_service_install_status_uninstall_with_a_fake_launchctl(self):
        t = self.tmp.name
        log = os.path.join(t, "launchctl.log")
        fake = os.path.join(t, "launchctl")
        with open(fake, "w") as f:
            f.write(f'#!/bin/sh\necho "$*" >> {log}\n[ "$1" = print ] && {{ echo "    state = running"; echo "    pid = 4242"; }}\nexit 0\n')
        os.chmod(fake, 0o755)
        os.environ.update(AGENTCTL_LAUNCHCTL=fake, AGENTCTL_LAUNCH_AGENTS=os.path.join(t, "LaunchAgents"))
        try:
            rc, out = self.run_tool("waiting", "service", "install", cwd=self.r.path)
            self.assertEqual(rc, 0, out)
            plist = open(os.path.join(t, "LaunchAgents", "net.bitcashier.agentctl-waiting.plist")).read()
            for needle in ("<key>KeepAlive</key>", "<key>RunAtLoad</key>", "<string>--service</string>", self.r.path,
                           "service.log"):
                self.assertIn(needle, plist)
            self.assertIn("bootstrap gui/", open(log).read())
            rc, out = self.run_tool("waiting", "service", "status", cwd=self.r.path)
            self.assertIn("running, pid 4242", out)
            rc, out = self.run_tool("waiting", "service", "uninstall", cwd=self.r.path)
            self.assertFalse(os.path.exists(os.path.join(t, "LaunchAgents", "net.bitcashier.agentctl-waiting.plist")))
            self.assertIn("bootout gui/", open(log).read())
        finally:
            for k in ("AGENTCTL_LAUNCHCTL", "AGENTCTL_LAUNCH_AGENTS"):
                os.environ.pop(k, None)

    def test_the_queue_points_at_waiting(self):
        rc, out = self.run_tool("queue", "add", cwd=self.r.path)
        self.assertEqual(rc, 2)
        self.assertIn("waiting", out)


if __name__ == "__main__":
    unittest.main()
