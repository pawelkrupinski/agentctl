"""agentctl queue: batches land with one landing check, a conflicting entry is rejected alone, a red batch bisects to
its culprit, a red gate holds production entries, entries added while the processor works land in the next batch."""
import json, os, stat, subprocess, sys, time, unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from test_agentctl import A, TOOL, Base, sh  # noqa: E402

HOOK = "#!/bin/sh\nunset GIT_DIR GIT_INDEX_FILE GIT_WORK_TREE\ncat > /dev/null\n"
# The stand-in identity-landing-check, printing the real one's verdict shape. BLOCKED when the checked tree holds:
#   RED     always, failing the spec the file names (deterministic);
#   FLAKY   only on the first check of any tree holding it (red, then green);
#   SHIFTY  on every check, FlakySpecA the first time, FlakySpecB after (the signature moves).
# Else LANDABLE. Every call is logged; $CHECK_HOOK (a shell script) runs once, during the first check.
CHECK = """#!/bin/sh
sha="$1"
echo "$sha" >> "$CHECK_CALLS"
echo "${FORGEJO_API_TOKEN:-<none>}" > "$CHECK_CALLS.token"
# what the real check prints when it dispatches: identity-ci-run's line in the check log, identity-measure's in its out dir
echo "identity-ci-run: pushed measure/q/ci-1" >&2
mkdir -p "${IDENTITY_LANDING_OUT:-/nonexistent}" 2>/dev/null && echo "identity-measure: pushed measure/q/me-1" > "$IDENTITY_LANDING_OUT/measure.log"
echo $$ > "$CHECK_CALLS.pid"
if [ -f "$CHECK_CALLS.noverdict" ]; then echo "NO VERDICT: could not check unit -- dispatch failed"; exit 2; fi
if [ -n "$CHECK_HOOK" ] && [ ! -f "$CHECK_HOOK.done" ]; then touch "$CHECK_HOOK.done"; sh "$CHECK_HOOK"; fi
if [ -f "$CHECK_CALLS.sleep" ]; then rm -f "$CHECK_CALLS.sleep"; sleep 30; fi  # the first check only: a long one
blocked() {
  echo "identity-landing-check: $1 *** FAILED ***" >&2
  echo "identity landing check: $sha"; echo; echo BLOCKED; echo "  - $1: some test"; echo "    hint: read its failure in the log"
  exit 1
}
count() { n=$(cat "$CHECK_CALLS.$1" 2>/dev/null || echo 0); echo $((n + 1)) > "$CHECK_CALLS.$1"; echo "$n"; }
if git cat-file -e "$sha:RED" 2>/dev/null; then blocked "$(git show "$sha:RED")"; fi
if git cat-file -e "$sha:FLAKY" 2>/dev/null && [ "$(count flaky)" = 0 ]; then blocked FlakySpec; fi
if git cat-file -e "$sha:SHIFTY" 2>/dev/null; then [ "$(count shifty)" = 0 ] && blocked FlakySpecA; blocked FlakySpecB; fi
echo LANDABLE
"""
ENV_KEYS = ("AGENTCTL_QUEUE_DIR", "AGENTCTL_QUEUE_NO_SPAWN", "AGENTCTL_LANDING_CHECK", "AGENTCTL_LANDING_SLOTS", "CHECK_CALLS",
            "CHECK_HOOK", "AGENTCTL_QUEUE_POLL", "AGENTCTL_FORGEJO_GH", "FAKE_GH_DIR", "GH_TOKEN", "PREPUSH_SKIP",
            "PATH", "FORGEJO_API_TOKEN", "AGENTCTL_FORGEJO_API", "FAKE_SECRETS_LOCKED",
            "AGENTCTL_QUEUE_CHECK_POLL")
# A stand-in `secrets`: `get <ns> FORGEJO_API_TOKEN` prints tok-secret, or fails like a locked vault.
SECRETS = """#!/bin/sh
[ -f "$FAKE_SECRETS_LOCKED" ] && { echo "vault $2 locked" >&2; exit 1; }
[ "$1" = get ] && { echo tok-secret; exit 0; }
[ "$1" = status ] && { echo unlocked; exit 0; }
exit 2
"""


class QueueTest(Base):
    def setUp(self):
        super().setUp()
        self.saved_env = {k: os.environ.get(k) for k in ENV_KEYS}
        t = self.tmp.name
        self.qd = os.path.join(t, "queue")
        self.calls = os.path.join(t, "check-calls")
        self.pushes = os.path.join(t, "pushes")
        hooks = os.path.join(t, "hooks")
        os.makedirs(hooks)
        sh(self.r.path, "git", "config", "core.hooksPath", hooks)
        self.hook_path = os.path.join(hooks, "pre-push")
        self.hook(f'echo push >> {self.pushes}\nexit 0')
        check = os.path.join(t, "landing-check")
        self.write_exec(check, CHECK)
        os.environ.update(AGENTCTL_QUEUE_DIR=self.qd, AGENTCTL_QUEUE_NO_SPAWN="1", AGENTCTL_LANDING_CHECK=check,
                          AGENTCTL_LANDING_SLOTS=f"{t}/slot-1 {t}/slot-2", CHECK_CALLS=self.calls)
        bindir = os.path.join(t, "bin")
        os.makedirs(bindir)
        self.write_exec(os.path.join(bindir, "secrets"), SECRETS)
        self.locked = os.path.join(t, "vault-locked")
        os.environ.update(PATH=f"{bindir}:{os.environ['PATH']}", FAKE_SECRETS_LOCKED=self.locked,
                          AGENTCTL_FORGEJO_API="http://127.0.0.1:9/api/v1")  # nothing listens: the token is not judged
        for k in ("CHECK_HOOK", "PREPUSH_SKIP", "FORGEJO_API_TOKEN"):
            os.environ.pop(k, None)
        self.saved_api, self.saved_gh = A.API, A.FORGEJO_GH
        self.gh = os.path.join(t, "fakegh")
        os.makedirs(self.gh)
        self.gh_runs([])
        os.environ.update(FAKE_GH_DIR=self.gh, GH_TOKEN="x", AGENTCTL_FORGEJO_GH=os.path.join(HERE, "fake_forgejo_gh.py"))
        A.FORGEJO_GH = os.environ["AGENTCTL_FORGEJO_GH"]
        A.API = os.environ["AGENTCTL_FORGEJO_API"]
        self.saved_why = A.ci_why
        A.ci_why = lambda o_r, tail=6, run_id=None, repo=None: (False, "ci-why: main is green for the gate")
        self.sleep = A.time.sleep
        A.time.sleep = lambda s: None

    def tearDown(self):
        A.ci_why, A.time.sleep, A.API, A.FORGEJO_GH = self.saved_why, self.sleep, self.saved_api, self.saved_gh
        for k, v in self.saved_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        super().tearDown()

    @staticmethod
    def write_exec(path, text):
        with open(path, "w") as f:
            f.write(text)
        os.chmod(path, os.stat(path).st_mode | stat.S_IEXEC)

    def hook(self, body):
        self.write_exec(self.hook_path, HOOK + body + "\n")

    # -- helpers
    def gh_runs(self, runs):
        with open(os.path.join(self.gh, "runs.json"), "w") as f:
            json.dump(runs, f)

    def gh_calls(self):
        p = os.path.join(self.gh, "calls.log")
        return [l.split(" ", 1)[1] for l in open(p).read().splitlines()] if os.path.exists(p) else []

    def dispatched_runs(self):
        """Run 501 still going on the check's ci branch; on its measure branch 502 done with every job done, and 503
        marked failed while 3 of its rows still wait for runner slots."""
        self.gh_runs([{"databaseId": 501, "status": "in_progress", "headBranch": "measure/q/ci-1", "workflowName": "Identity run"},
                      {"databaseId": 502, "status": "completed", "conclusion": "success", "headBranch": "measure/q/me-1",
                       "workflowName": "Identity model convergence"},
                      {"databaseId": 503, "status": "completed", "conclusion": "failure", "headBranch": "measure/q/me-1",
                       "workflowName": "Identity model convergence"}])
        for rid, jobs in ((502, ["completed", "completed"]), (503, ["completed", "queued", "queued", "in_progress"])):
            with open(os.path.join(self.gh, f"run-{rid}.json"), "w") as f:
                json.dump({"status": "completed", "jobs": [{"name": f"j{i}", "status": st} for i, st in enumerate(jobs)]}, f)

    def check_paths_on_main(self, precheck=None):
        """main gains the repo-owned lists: the convergence paths and (optionally) the queue's pre-check."""
        self.r.write(".github/convergence-paths.txt", "# paths\nidentity/src/main/**\ncommon/src/main/**\nbuild.sbt\n")
        if precheck is not None:
            self.r.write("scripts/queue-precheck-specs.txt", precheck)
        self.r.write("snap.txt", "the checked-in snapshot\n")
        self.r.commit("repo-owned lists")
        self.r.push()
        if os.path.exists(self.pushes):
            os.unlink(self.pushes)

    def agent(self, name, files, msg=None):
        """A worktree on its own branch with one commit writing `files` ({path: text})."""
        wt = self.r.worktree(f"wt-{name}", branch=name)
        for p, text in files.items():
            self.r.write(p, text, at=wt)
        return wt, self.r.commit(msg or name, at=wt)

    def add(self, wt, *extra):
        rc, out = self.run_tool("queue", "add", *extra, cwd=wt)
        self.assertEqual(rc, 0, out)
        return out.split("QUEUED ", 1)[1].split(":", 1)[0]

    def process(self, *extra):
        return self.run_tool("queue", "run", "--once", "--repo", self.r.path, *extra)

    def wait(self, eid):
        return self.run_tool("queue", "wait", eid, "--timeout", "5", cwd=self.r.path)

    def entry(self, eid):
        return A.read_json(os.path.join(self.qd, "entries", eid + ".json"))

    def remote_log(self):
        return sh(self.r.root, "git", "--git-dir", self.r.bare, "log", "--format=%s", "main").splitlines()

    def check_calls(self):
        return open(self.calls).read().split() if os.path.exists(self.calls) else []

    def push_count(self):
        return len(open(self.pushes).read().split()) if os.path.exists(self.pushes) else 0

    # -- tests
    def test_batch_lands_with_one_landing_check_and_one_push(self):
        w1, _ = self.agent("a1", {"identity/src/main/scala/A.scala": "a\n"})
        w2, _ = self.agent("a2", {"scripts/tool.sh": "echo\n"})
        w3, _ = self.agent("a3", {"worker/src/main/scala/services/identity/B.scala": "b\n"})
        ids = [self.add(w) for w in (w1, w2, w3)]
        self.assertTrue(self.entry(ids[0])["identity"])
        self.assertFalse(self.entry(ids[1])["identity"])
        rc, out = self.process()
        self.assertEqual(rc, 0, out)
        self.assertEqual(self.remote_log()[:3], ["a3", "a2", "a1"])
        self.assertEqual(len(self.check_calls()), 1, out)
        self.assertEqual(self.push_count(), 1)
        tip = sh(self.r.root, "git", "--git-dir", self.r.bare, "rev-parse", "main")
        self.assertEqual(self.check_calls(), [tip])
        for eid in ids:
            rc, out = self.wait(eid)
            self.assertEqual(rc, 0, out)
            self.assertRegex(out.strip().splitlines()[-1], r"^LANDED [0-9a-f]{40}$")
        self.assertEqual(self.wait(ids[2])[1].split()[-1], tip)

    def test_tools_only_batch_needs_no_landing_check(self):
        w1, _ = self.agent("t1", {"scripts/x.sh": "x\n"})
        eid = self.add(w1)
        rc, out = self.process()
        self.assertEqual(rc, 0, out)
        self.assertEqual(self.check_calls(), [])
        self.assertEqual(self.entry(eid)["state"], "landed")

    def test_conflicting_entry_is_rejected_alone(self):
        w1, _ = self.agent("c1", {"f1.txt": "one\n"})
        w2, _ = self.agent("c2", {"f1.txt": "two\n"})
        w3, _ = self.agent("c3", {"f3.txt": "three\n"})
        ids = [self.add(w) for w in (w1, w2, w3)]
        rc, out = self.process()
        self.assertEqual(rc, 0, out)
        self.assertEqual(self.remote_log()[:2], ["c3", "c1"])
        self.assertNotIn("c2", self.remote_log())
        rc, out = self.wait(ids[1])
        self.assertEqual(rc, 1, out)
        self.assertIn("REJECTED conflicts with an earlier entry of the batch", out)
        self.assertIn("f1.txt", out)
        self.assertIn("rebases onto forgejo/main alone", out)
        self.assertEqual(self.entry(ids[0])["state"], "landed")
        self.assertEqual(self.entry(ids[2])["state"], "landed")
        self.assertEqual(self.push_count(), 1)

    def test_red_batch_bisects_to_the_culprit_and_lands_the_rest(self):
        ws = [self.agent(f"b{i}", {f"identity/B{i}.scala": f"{i}\n", **({"RED": "MatchedTakesLabelSpec\n"} if i == 3 else {})}) for i in range(1, 5)]
        ids = [self.add(w) for w, _ in ws]
        rc, out = self.process()
        self.assertEqual(rc, 0, out)
        log = self.remote_log()
        self.assertEqual(log[:3], ["b4", "b2", "b1"])
        self.assertNotIn("b3", log)
        # the full batch (red), bare main (green), the first half (green), the first three (red), the first three
        # again (red, same signature: deterministic), then b4 alone on the new main
        calls = self.check_calls()
        self.assertEqual(len(calls), 6, out)
        self.assertEqual(calls[3], calls[4])
        self.assertEqual(self.entry(ids[2])["state"], "rejected")
        rc, out = self.wait(ids[2])
        self.assertEqual(rc, 1, out)
        self.assertIn("REJECTED the identity landing check is BLOCKED with it, twice (MatchedTakesLabelSpec)", out)
        self.assertIn("MatchedTakesLabelSpec", out)
        for i in (0, 1, 3):
            self.assertEqual(self.entry(ids[i])["state"], "landed", ids[i])

    def main_sha(self):
        return sh(self.r.root, "git", "--git-dir", self.r.bare, "rev-parse", "main")

    def hold(self):
        return A.read_json(os.path.join(self.qd, "hold.json"))

    def assert_nothing_moved(self, ids, main):
        self.assertEqual(self.main_sha(), main)
        for eid in ids:
            self.assertEqual(self.entry(eid)["state"], "queued", eid)

    def test_red_then_green_boundary_is_a_flake_held_until_resume(self):
        w1, _ = self.agent("f1", {"identity/F1.scala": "1\n"})
        w2, _ = self.agent("f2", {"identity/F2.scala": "2\n", "FLAKY": "x\n"})
        ids = [self.add(w) for w in (w1, w2)]
        main = self.main_sha()
        rc, out = self.process()
        self.assertEqual(rc, 0, out)
        self.assert_nothing_moved(ids, main)  # f1 is green, but nothing lands on a flake's evidence
        hold = self.hold()
        self.assertEqual(hold["kind"], "flaky")
        self.assertIn("FLAKY FlakySpec (batch ", hold["what"])
        self.assertIn("FlakySpec", open(os.path.join(self.qd, "flaky.log")).read())
        self.assertIn("FLAKY FlakySpec", self.run_tool("queue", "status", cwd=self.r.path)[1])
        rc, out = self.wait(ids[0])
        self.assertEqual(rc, 3, out)
        self.assertIn("HELD flaky FlakySpec", out)
        n = len(self.check_calls())
        self.process()  # held: no check, nothing lands, until resume
        self.assertEqual(len(self.check_calls()), n)
        self.assert_nothing_moved(ids, main)
        rc, out = self.run_tool("queue", "resume", cwd=self.r.path)
        self.assertEqual(rc, 0, out)
        self.assertIsNone(self.hold())
        rc, out = self.process()
        self.assertEqual([self.entry(i)["state"] for i in ids], ["landed", "landed"], out)

    def test_a_different_signature_on_the_recheck_is_a_flake(self):
        w, _ = self.agent("sh1", {"identity/S.scala": "s\n", "SHIFTY": "x\n"})
        eid = self.add(w)
        main = self.main_sha()
        self.process()
        self.assert_nothing_moved([eid], main)
        hold = self.hold()
        self.assertEqual(hold["kind"], "flaky")
        self.assertIn("FlakySpecA", hold["what"])
        self.assertIn("different signature (FlakySpecB)", hold["detail"])

    def test_red_base_holds_main_red_and_resumes_on_a_new_green_main(self):
        self.r.write("RED", "BaseSpec\n")
        self.r.commit("main breaks")
        self.r.push()
        red_main = self.main_sha()
        w, _ = self.agent("mr", {"identity/M.scala": "m\n"})
        eid = self.add(w)
        self.process()
        self.assert_nothing_moved([eid], red_main)
        hold = self.hold()
        self.assertEqual(hold["kind"], "main-red")
        self.assertEqual(hold["what"], f"MAIN RED BaseSpec at {red_main[:10]}")
        n = len(self.check_calls())
        self.process()  # same main: still held, no check spent
        self.assertEqual(len(self.check_calls()), n)
        self.assertEqual(self.hold()["kind"], "main-red")
        sh(self.r.path, "git", "rm", "-q", "RED")
        self.r.commit("main fixed")
        self.r.push()
        rc, out = self.process()  # main moved and the gate is green: resumes by itself
        self.assertIsNone(self.hold(), out)
        self.assertEqual(self.entry(eid)["state"], "landed", out)

    def test_base_verdict_is_paid_once_per_main_sha(self):
        w1, _ = self.agent("x1", {"identity/X1.scala": "1\n", "RED": "XSpec\n"})
        id1 = self.add(w1)
        main = self.main_sha()
        self.process()
        self.assertEqual(self.entry(id1)["state"], "rejected")
        w2, _ = self.agent("x2", {"identity/X2.scala": "2\n", "RED": "YSpec\n"})
        id2 = self.add(w2)
        self.process()
        self.assertEqual(self.entry(id2)["state"], "rejected")
        self.assertEqual(self.check_calls().count(main), 1)
        self.assertEqual(len(self.check_calls()), 5)  # x1, main, x1 again; x2, x2 again

    # -- the processor's token
    def test_queue_add_hands_the_processor_its_token_in_a_private_file(self):
        w, _ = self.agent("tk", {"identity/T.scala": "t\n"})
        rc, out = self.run_tool("queue", "add", cwd=w)
        self.assertEqual(rc, 0, out)
        self.assertNotIn("tok-secret", out)
        tok = os.path.join(self.qd, "token")
        self.assertEqual(stat.S_IMODE(os.stat(tok).st_mode), 0o600)
        self.assertEqual(open(tok).read().strip(), "tok-secret")
        open(self.locked, "w").close()  # the processor's own session cannot read the vault
        rc, out = self.process()
        self.assertNotIn("tok-secret", out)
        self.assertEqual(open(self.calls + ".token").read().strip(), "tok-secret")
        self.assertEqual(self.entry(self.add_id(w))["state"], "landed")

    def add_id(self, wt):
        return A.find_entry(self.qd, None, wt)["id"]

    def test_no_token_holds_before_any_check_until_resume(self):
        open(self.locked, "w").close()
        w, _ = self.agent("nt", {"identity/N.scala": "n\n"})
        rc, out = self.run_tool("queue", "add", cwd=w)
        self.assertEqual(rc, 0, out)
        self.assertIn("secrets unlock", out)
        eid = self.add_id(w)
        self.process()
        self.assertEqual(self.check_calls(), [])
        self.assertEqual(self.entry(eid)["state"], "queued")
        self.assertEqual(self.hold()["kind"], "no-token")
        rc, out = self.wait(eid)
        self.assertEqual(rc, 3, out)
        self.assertIn("HELD no-token: run `secrets unlock", out)
        self.assertIn("agentctl queue resume", out)
        os.unlink(self.locked)
        rc, out = self.run_tool("queue", "resume", cwd=w)  # resume refreshes the token from this session
        self.assertEqual(rc, 0, out)
        self.process()
        self.assertEqual(self.entry(eid)["state"], "landed")

    def test_a_token_forgejo_rejects_holds_as_no_token(self):
        import http.server, threading
        class H(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(401)
                self.end_headers()
            def log_message(self, *a):
                pass
        srv = http.server.HTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        A.API = f"http://127.0.0.1:{srv.server_port}/api/v1"
        try:
            w, _ = self.agent("bt", {"identity/B.scala": "b\n"})
            eid = self.add(w)
            self.process()
        finally:
            srv.shutdown()
        self.assertEqual(self.check_calls(), [])
        self.assertEqual(self.hold()["kind"], "no-token")
        self.assertEqual(self.entry(eid)["state"], "queued")

    # -- no verdict
    def test_no_verdict_holds_and_retries_but_never_rejects(self):
        open(self.calls + ".noverdict", "w").close()
        w, _ = self.agent("nv", {"identity/V.scala": "v\n"})
        eid = self.add(w)
        self.process()
        hold = self.hold()
        self.assertEqual(hold["kind"], "no-verdict")
        self.assertFalse(hold["needs_resume"])
        self.assertIn("HELD", self.run_tool("queue", "status", cwd=self.r.path)[1])
        for _ in range(A.QUEUE_CHECK_ERRORS + 2):  # retried with backoff, up to the bound; never rejected
            self.process()
            self.assertEqual(self.entry(eid)["state"], "queued")
        self.assertEqual(len(self.check_calls()), A.QUEUE_CHECK_ERRORS)
        self.assertTrue(self.hold()["needs_resume"])
        rc, out = self.wait(eid)
        self.assertEqual(rc, 3, out)
        self.assertIn("HELD no-verdict", out)
        os.unlink(self.calls + ".noverdict")
        self.run_tool("queue", "resume", cwd=self.r.path)
        self.process()
        self.assertEqual(self.entry(eid)["state"], "landed")
        self.assertIsNone(self.hold())

    def test_requeue_restores_a_rejected_entry_in_its_place_without_duplicating(self):
        w1, _ = self.agent("q1", {"f1.txt": "q1\n"})
        w2, _ = self.agent("q2", {"f2.txt": "q2\n"})
        id1, id2 = self.add(w1), self.add(w2)
        for eid in (id1, id2):
            self.run_tool("queue", "cancel", eid, cwd=self.r.path)
        id1b = self.add(w1)  # q1 re-added, same head
        rc, out = self.run_tool("queue", "requeue", id1, cwd=self.r.path)
        self.assertEqual(rc, 0, out)
        self.assertEqual(self.entry(id1)["state"], "rejected")  # not duplicated: the open one takes its place
        self.assertEqual(self.entry(id1b)["added_at"], self.entry(id1)["added_at"])
        rc, out = self.run_tool("queue", "requeue", id2, cwd=self.r.path)
        self.assertEqual(rc, 0, out)
        self.assertEqual(self.entry(id2)["state"], "queued")
        order = [e["id"] for e in A.read_entries(self.qd) if e["state"] == "queued"]
        self.assertEqual(order, [id1b, id2])
        self.process()
        self.assertEqual(self.remote_log()[:2], ["q2", "q1"])

    def test_requeue_last_goes_behind_the_open_entries(self):
        w1, _ = self.agent("l1", {"f1.txt": "l1\n"})
        w2, _ = self.agent("l2", {"f2.txt": "l2\n"})
        id1 = self.add(w1)
        self.run_tool("queue", "cancel", id1, cwd=self.r.path)
        id2 = self.add(w2)
        rc, out = self.run_tool("queue", "requeue", id1, "--last", cwd=self.r.path)
        self.assertEqual(rc, 0, out)
        self.assertEqual([e["id"] for e in A.read_entries(self.qd) if e["state"] == "queued"], [id2, id1])

    # -- the check's CI runs are never abandoned
    def test_a_finished_check_cancels_its_runs_still_going_and_deletes_its_branches(self):
        self.dispatched_runs()
        w, _ = self.agent("cr", {"identity/C.scala": "c\n"})
        eid = self.add(w)
        self.process()
        self.assertEqual(self.entry(eid)["state"], "landed")
        calls = self.gh_calls()
        self.assertIn("run cancel 501 -R", " ".join(calls))
        self.assertIn("run cancel 503 -R", " ".join(calls))  # failed, but its queued rows would hold runner slots
        self.assertNotIn("run cancel 502", " ".join(calls))
        for b in ("measure%2Fq%2Fci-1", "measure%2Fq%2Fme-1"):
            self.assertTrue(any(c.startswith("api -X DELETE") and c.rstrip().endswith(f"branches/{b}") for c in calls), (b, calls))
        self.assertFalse(os.path.exists(os.path.join(self.qd, "active-check.json")))

    def test_no_check_starts_when_the_gate_turns_red_first(self):
        answers = iter([False])  # green when the batch is formed, red when its check would start
        A.ci_why = lambda *a, **k: (next(answers, True), "Main: RED (run 9 #5, failure)")
        w, _ = self.agent("gr", {"identity/app/src/main/G.scala": "g\n"})
        eid = self.add(w)
        self.assertTrue(self.entry(eid)["production"])
        self.process()
        self.assertEqual(self.check_calls(), [])
        self.assertEqual(self.entry(eid)["state"], "queued")

    def test_a_processor_that_died_mid_check_has_its_runs_cancelled_at_the_next_start(self):
        self.dispatched_runs()
        os.makedirs(self.qd, exist_ok=True)
        log = os.path.join(self.tmp.name, "dead-check.log")
        with open(log, "w") as f:
            f.write("identity-ci-run: pushed measure/q/ci-1\n")
        A.write_json(os.path.join(self.qd, "active-check.json"), {"log": log, "out": log + ".d", "sha": "f" * 40})
        self.process()
        self.assertIn("run cancel 501 -R", " ".join(self.gh_calls()))
        self.assertFalse(os.path.exists(os.path.join(self.qd, "active-check.json")))

    def test_sigterm_mid_check_kills_the_check_and_cancels_its_runs(self):
        import signal
        self.dispatched_runs()
        open(self.calls + ".sleep", "w").close()
        A.time.sleep = self.sleep  # (time is one module: the setUp stub silences the test's own waits too)
        w, _ = self.agent("st", {"identity/S.scala": "s\n"})
        eid = self.add(w)
        p = subprocess.Popen([sys.executable, TOOL, "queue", "run", "--once", "--repo", self.r.path], stdout=subprocess.PIPE,
                             stderr=subprocess.STDOUT, text=True)
        for _ in range(300):
            if os.path.exists(self.calls + ".pid") and os.path.exists(os.path.join(self.qd, "active-check.json")):
                break
            time.sleep(0.05)
        time.sleep(0.3)
        if not os.path.exists(self.calls + ".pid"):
            p.kill()
            self.fail("the check never started:\n" + p.communicate()[0])
        check_pid = int(open(self.calls + ".pid").read())
        p.send_signal(signal.SIGTERM)
        out, _ = p.communicate(timeout=60)
        self.assertIn("run cancel 501 -R", " ".join(self.gh_calls()), out)
        for _ in range(50):
            if not A.pid_alive(check_pid):
                break
            time.sleep(0.1)
        self.assertFalse(A.pid_alive(check_pid), "the check outlived the processor")
        self.assertEqual(self.entry(eid)["state"], "queued", out)

    # -- the owner's pause and cancel
    def during_first_check(self, *argv):
        """Run `agentctl <argv>` (as the owner, from the main checkout) while the first landing check is under way."""
        hook = os.path.join(self.tmp.name, "owner.sh")
        with open(hook, "w") as f:
            f.write(f"cd {self.r.path} && {sys.executable} {TOOL} {' '.join(argv)} > {hook}.out 2>&1\n")
        os.environ["CHECK_HOOK"] = hook
        os.environ["AGENTCTL_QUEUE_CHECK_POLL"] = "0.1"
        open(self.calls + ".sleep", "w").close()  # that first check runs long, so the processor must interrupt it

    def test_hold_pauses_the_queue_and_waiters_neither_spawn_nor_give_up(self):
        w, _ = self.agent("ph", {"identity/P.scala": "p\n"})
        eid = self.add(w)
        rc, out = self.run_tool("queue", "hold", "--reason", "swapping processors", cwd=self.r.path)
        self.assertEqual(rc, 0, out)
        self.assertIn("PAUSED", self.run_tool("queue", "status", cwd=self.r.path)[1])
        self.assertIn("swapping processors", self.run_tool("queue", "status", cwd=self.r.path)[1])
        self.process()
        self.assertEqual(self.check_calls(), [])
        self.assertEqual(self.entry(eid)["state"], "queued")
        os.environ.pop("AGENTCTL_QUEUE_NO_SPAWN")
        os.environ["AGENTCTL_QUEUE_POLL"] = "0.05"
        A.time.sleep = self.sleep
        rc, out = self.run_tool("queue", "wait", eid, "--timeout", "1", cwd=w)
        self.assertEqual(rc, 2, out)  # still waiting when it timed out: a pause is not a HELD verdict
        self.assertFalse(A.processor_running(self.qd)[0])
        self.assertFalse(os.path.exists(os.path.join(self.qd, "processor.log")), "the waiter started a processor")
        os.environ["AGENTCTL_QUEUE_NO_SPAWN"] = "1"
        A.time.sleep = lambda s: None
        rc, out = self.run_tool("queue", "resume", cwd=self.r.path)
        self.assertEqual(rc, 0, out)
        self.process()
        self.assertEqual(self.entry(eid)["state"], "landed")

    def test_hold_during_a_check_stops_it_and_cancels_its_runs(self):
        self.dispatched_runs()
        w, _ = self.agent("hc", {"identity/H.scala": "h\n"})
        eid = self.add(w)
        self.during_first_check("queue", "hold", "--reason", "now")
        t = time.monotonic()
        self.process()
        self.assertLess(time.monotonic() - t, 20, "the check was waited out, not stopped")
        self.assertEqual(os.listdir(os.path.join(self.qd, "verdicts")), [])  # no verdict taken from a stopped check
        self.assertEqual(self.entry(eid)["state"], "queued")
        self.assertIn("run cancel 501 -R", " ".join(self.gh_calls()))
        self.assertEqual(self.hold()["kind"], "manual")
        self.assertEqual(self.push_count(), 0)

    def test_cancel_drops_an_entry_from_the_batch_under_check_and_the_rest_lands(self):
        self.dispatched_runs()
        w1, _ = self.agent("k1", {"identity/K1.scala": "1\n"})
        w2, _ = self.agent("k2", {"identity/K2.scala": "2\n"})
        id1, id2 = self.add(w1), self.add(w2)
        self.during_first_check("queue", "cancel", id2)
        t = time.monotonic()
        rc, out = self.process()
        self.assertLess(time.monotonic() - t, 20, "the check was waited out, not stopped")
        self.assertIn("CANCEL", open(os.path.join(self.tmp.name, "owner.sh.out")).read())
        rej = self.entry(id2)
        self.assertEqual(rej["state"], "rejected", out)
        self.assertIn("cancelled by its owner", rej["why"])
        self.assertEqual(self.entry(id1)["state"], "landed", out)
        self.assertIn("run cancel 501 -R", " ".join(self.gh_calls()))
        self.assertEqual(len(self.check_calls()), 2)  # the interrupted batch, then k1 alone
        self.assertEqual(self.remote_log()[0], "k1")
        self.assertNotIn("k2", self.remote_log())

    # -- the local pre-check
    PRECHECK = ("# cheap and deterministic: before any CI\n"
                "sh test ! -f STALE || { echo regenerated > snap.txt; echo 'ReadModelSnapshotSpec *** FAILED *** snapshot stale'; exit 1; }\n")

    def test_a_failing_precheck_is_bisected_locally_and_only_the_culprit_rejected(self):
        self.check_paths_on_main(self.PRECHECK)
        ws = [self.agent(f"pc{i}", {f"identity/P{i}.scala": f"{i}\n", **({"STALE": "x\n"} if i == 2 else {})}) for i in (1, 2, 3)]
        ids = [self.add(w) for w, _ in ws]
        rc, out = self.process()
        self.assertEqual(self.entry(ids[1])["state"], "rejected", out)
        why = self.entry(ids[1])["why"]
        self.assertIn("pre-check", why)
        self.assertIn("ReadModelSnapshotSpec *** FAILED ***", why)
        self.assertIn("+regenerated", why)  # the snapshot's diff, for its owner to review
        self.assertEqual([self.entry(i)["state"] for i in (ids[0], ids[2])], ["landed", "landed"], out)
        self.assertEqual(len(self.check_calls()), 1)  # CI ran once, for the batch without the culprit
        self.assertEqual(sh(self.r.root, "git", "--git-dir", self.r.bare, "show", "main:snap.txt"), "the checked-in snapshot")

    def test_a_main_red_precheck_blames_no_entry(self):
        self.check_paths_on_main(self.PRECHECK)
        self.r.write("STALE", "x\n")
        self.r.commit("main breaks the pre-check")
        self.r.push()
        w, _ = self.agent("mp", {"identity/M.scala": "m\n"})
        eid = self.add(w)
        self.process()
        self.assertEqual(self.entry(eid)["state"], "queued")
        self.assertEqual(self.hold()["kind"], "main-red")
        self.assertIn("pre-check", self.hold()["what"])
        self.assertEqual(self.check_calls(), [])

    # -- main moving under a long check
    def move_main_during_check(self, files):
        other = os.path.join(self.tmp.name, "other")
        sh(self.r.root, "git", "clone", "-q", self.r.bare, other)
        mover = os.path.join(self.tmp.name, "mover.sh")
        writes = " && ".join(f"mkdir -p $(dirname {p}) && echo moved > {p}" for p in files)
        with open(mover, "w") as f:
            f.write(f"cd {other} && {writes} && git add -A && git -c core.hooksPath=/dev/null commit -q -m mover && "
                    f"git -c core.hooksPath=/dev/null push -q origin HEAD:main\n")
        os.environ["CHECK_HOOK"] = mover

    def test_main_moved_by_paths_the_check_does_not_cover_keeps_the_verdict(self):
        self.check_paths_on_main(self.PRECHECK)
        w, _ = self.agent("kv", {"identity/K.scala": "k\n"})
        eid = self.add(w)
        self.move_main_during_check(["scripts/tool.sh", "docs/x.md"])
        rc, out = self.process()
        self.assertEqual(self.entry(eid)["state"], "landed", out)
        self.assertEqual(len(self.check_calls()), 1, out)
        self.assertEqual(self.remote_log()[:2], ["kv", "mover"])

    def test_main_moved_by_a_covered_path_rechecks(self):
        self.check_paths_on_main(self.PRECHECK)
        w, _ = self.agent("rc", {"identity/R.scala": "r\n"})
        eid = self.add(w)
        self.move_main_during_check(["common/src/main/scala/X.scala"])
        rc, out = self.process()
        self.assertEqual(self.entry(eid)["state"], "landed", out)
        self.assertEqual(len(self.check_calls()), 2, out)

    def test_main_moved_but_the_precheck_fails_on_the_new_tip_rechecks_nothing_and_rebuilds(self):
        self.check_paths_on_main(self.PRECHECK)
        w, _ = self.agent("pf", {"identity/F.scala": "f\n"})
        eid = self.add(w)
        self.move_main_during_check(["STALE"])  # a tools-only path, yet the combination fails the pre-check
        rc, out = self.process()
        self.assertNotIn(self.entry(eid)["state"], ("landed",), out)
        self.assertNotIn("pf", self.remote_log())

    def test_single_red_entry_is_rejected_without_bisecting(self):
        w, _ = self.agent("r1", {"identity/R.scala": "r\n", "RED": "SomeSpec\n"})
        eid = self.add(w)
        self.process()
        self.assertEqual(self.entry(eid)["state"], "rejected")
        self.assertEqual(len(self.check_calls()), 3)  # the entry, bare main, the entry again
        self.assertEqual(self.push_count(), 0)

    def test_red_gate_holds_production_entries_and_lands_tools_only(self):
        A.ci_why = lambda *a, **k: (True, "ci-why x: main is RED for the gate\nMain: RED (run 9 #5, failure)")
        wp, _ = self.agent("prod", {"app/src/main/scala/P.scala": "p\n"})
        wt, _ = self.agent("tools", {"scripts/t.sh": "t\n"})
        pid, tid = self.add(wp), self.add(wt)
        rc, out = self.process()
        self.assertEqual(rc, 0, out)
        self.assertEqual(self.entry(tid)["state"], "landed")
        held = self.entry(pid)
        self.assertEqual(held["state"], "queued")
        self.assertIn("held: main is red for the gate (Main: RED (run 9 #5, failure))", held["note"])
        self.assertNotIn("prod", self.remote_log())
        rc, out = self.run_tool("queue", "status", cwd=self.r.path)
        self.assertIn("held: main is red", out)
        A.ci_why = lambda *a, **k: (False, "green")
        rc, out = self.process()
        self.assertEqual(self.entry(pid)["state"], "landed", out)
        self.assertEqual(self.remote_log()[0], "prod")

    def test_fixes_red_entry_goes_while_the_gate_is_red(self):
        A.ci_why = lambda *a, **k: (True, "Main: RED (run 9 #5, failure)")
        w, _ = self.agent("fix", {"app/src/main/scala/F.scala": "f\n"}, msg="fix it\n\nFixes-Red: 5")
        eid = self.add(w)
        self.process()
        self.assertEqual(self.entry(eid)["state"], "landed")

    def test_entry_added_while_processing_lands_in_the_next_batch(self):
        w1, _ = self.agent("p1", {"identity/P1.scala": "1\n"})
        w2, _ = self.agent("p2", {"identity/P2.scala": "2\n"})
        id1 = self.add(w1)
        add2 = os.path.join(self.tmp.name, "add2.sh")
        with open(add2, "w") as f:  # another agent queues while the first batch's landing check runs
            f.write(f"cd {w2} && {sys.executable} {TOOL} queue add > {self.tmp.name}/add2.out 2>&1\n")
        os.environ["CHECK_HOOK"] = add2
        rc, out = self.process()
        self.assertEqual(rc, 0, out)
        added = open(os.path.join(self.tmp.name, "add2.out")).read()
        self.assertIn("QUEUED", added)
        id2 = added.split("QUEUED ", 1)[1].split(":", 1)[0]
        self.assertEqual(self.entry(id1)["state"], "landed")
        self.assertEqual(self.entry(id2)["state"], "landed")
        self.assertEqual(self.remote_log()[:2], ["p2", "p1"])
        self.assertEqual(len(self.check_calls()), 2)  # two batches: p1 alone, then p2 on the new main
        self.assertEqual(self.push_count(), 2)

    def test_main_moving_under_the_batch_rebuilds_and_rechecks(self):
        w1, _ = self.agent("m1", {"identity/M.scala": "m\n"})
        eid = self.add(w1)
        other = os.path.join(self.tmp.name, "other")
        sh(self.r.root, "git", "clone", "-q", self.r.bare, other)
        mover = os.path.join(self.tmp.name, "mover.sh")
        with open(mover, "w") as f:
            f.write(f"cd {other} && git -c core.hooksPath=/dev/null commit -q --allow-empty -m mover && "
                    f"git -c core.hooksPath=/dev/null push -q origin HEAD:main\n")
        os.environ["CHECK_HOOK"] = mover
        rc, out = self.process()
        self.assertEqual(self.entry(eid)["state"], "landed", out)
        self.assertEqual(self.remote_log()[:2], ["m1", "mover"])
        self.assertEqual(len(self.check_calls()), 2, out)
        self.assertIn("moved", out)

    def test_failed_hook_on_a_batch_retries_each_entry_alone(self):
        self.hook(f'echo push >> {self.pushes}\ngit ls-tree -r --name-only "$(git rev-parse HEAD)" | grep -qx BAD && '
                  '{ echo "sbt FAILED: BadSpec" >&2; exit 1; }\nexit 0')
        w1, _ = self.agent("h1", {"scripts/ok.sh": "ok\n"})
        w2, _ = self.agent("h2", {"BAD": "bad\n"})
        id1, id2 = self.add(w1), self.add(w2)
        rc, out = self.process()
        self.assertEqual(self.entry(id1)["state"], "landed", out)
        rej = self.entry(id2)
        self.assertEqual(rej["state"], "rejected", out)
        self.assertIn("BadSpec", rej["why"])
        self.assertEqual(self.remote_log()[0], "h1")

    def test_add_refuses_a_second_open_entry_and_prepush_skip(self):
        w1, _ = self.agent("d1", {"f2.txt": "d\n"})
        eid = self.add(w1)
        rc, out = self.run_tool("queue", "add", cwd=w1)
        self.assertEqual(rc, 1)
        self.assertIn(f"already has entry {eid}", out)
        rc, out = self.run_tool("queue", "cancel", eid, cwd=w1)
        self.assertEqual(rc, 0, out)
        os.environ["PREPUSH_SKIP"] = "sbt"
        rc, out = self.run_tool("queue", "add", cwd=w1)
        self.assertEqual(rc, 1)
        self.assertIn("PREPUSH_SKIP", out)

    def test_queue_never_skips_the_hook(self):
        src = open(TOOL).read()
        q = src[src.index("# ---------------------------------------------------------------- queue"):
                src.index("# ---------------------------------------------------------------- env\nTUNNEL_PORTS")]
        self.assertIn("push_argv(REMOTE, tip)", q)
        for bad in ('"--no-verify"', '"--force"', "PREPUSH_SKIP="):
            self.assertFalse(bad in q, bad)

    def test_add_spawns_a_detached_processor_and_wait_reports_landed(self):
        os.environ.update(AGENTCTL_QUEUE_POLL="0.2")  # the fake gh from setUp: no workflows on main, green for the gate
        os.environ.pop("AGENTCTL_QUEUE_NO_SPAWN")
        A.time.sleep = self.sleep
        w1, _ = self.agent("s1", {"identity/S.scala": "s\n"})
        eid = self.add(w1)
        pid = None
        for _ in range(100):
            pid = (A.read_json(os.path.join(self.qd, "processor.json")) or {}).get("pid")
            if pid:
                break
            time.sleep(0.05)
        with self.assertRaises(ChildProcessError):  # fully detached: never our child, so never our zombie
            os.waitpid(pid, os.WNOHANG)
        rc, out = self.run_tool("queue", "wait", eid, "--timeout", "60", cwd=w1)
        log = open(os.path.join(self.qd, "processor.log")).read()
        self.assertEqual(rc, 0, out + log)
        self.assertIn("LANDED", out)
        self.assertEqual(self.remote_log()[0], "s1")
        for _ in range(100):  # let the processor exit before the temp dir goes
            if not A.processor_running(self.qd)[0]:
                break
            time.sleep(0.1)


class QueueLivenessTest(Base):
    def test_a_zombie_pid_is_not_a_running_processor(self):
        qd = os.path.join(self.tmp.name, "q")
        os.makedirs(qd)
        z = subprocess.Popen(["true"])
        try:
            for _ in range(100):  # exited, not reaped: a zombie
                if subprocess.run(["ps", "-o", "stat=", "-p", str(z.pid)], capture_output=True, text=True).stdout.strip().startswith("Z"):
                    break
                time.sleep(0.02)
            A.write_json(os.path.join(qd, "processor.json"), {"pid": z.pid, "phase": "holding"})
            self.assertFalse(A.processor_running(qd)[0])
        finally:
            z.wait()

    def test_the_processor_lock_holder_is_running(self):
        qd = os.path.join(self.tmp.name, "q")
        os.makedirs(qd)
        A.write_json(os.path.join(qd, "processor.json"), {"pid": os.getpid(), "phase": "holding"})
        lock = A.Flock(os.path.join(qd, "processor.lock"))
        self.assertTrue(lock.acquire(block=False))
        try:
            self.assertTrue(A.processor_running(qd)[0])
        finally:
            lock.release()
        self.assertFalse(A.processor_running(qd)[0])  # a live pid without the lock is no processor


class QueuePathsTest(unittest.TestCase):
    def test_identity_paths(self):
        for p in ("identity/src/main/scala/services/identity/RelayStage.scala", "worker/src/main/scala/services/identity/X.scala",
                  "common/src/main/resources/title-rules.tsv", "common/src/main/scala/services/titlerules/R.scala",
                  "test/resources/fixtures/identity-judgements.tsv", "common/src/main/scala/services/readmodel/ReadModelProjection.scala",
                  "worker/src/main/scala/services/resolution/ResolutionCache.scala"):
            self.assertTrue(A.needs_identity_check(p), p)
        for p in ("scripts/identity-measure", "docs/identity.md", "web/src/main/scala/Page.scala", "README.md"):
            self.assertFalse(A.needs_identity_check(p), p)

    def test_production_paths(self):
        for p in ("web/src/main/scala/A.scala", "build.sbt", "project/plugins.sbt", "conf/app.conf", "infra/nix/x.nix", "Dockerfile"):
            self.assertTrue(A.is_production_path(p), p)
        for p in ("scripts/x.sh", "web/src/test/scala/A.scala", "test/resources/fixtures/a.tsv", ".github/workflows/m.yml"):
            self.assertFalse(A.is_production_path(p), p)


if __name__ == "__main__":
    unittest.main()
