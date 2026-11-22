"""The reconciler's movies clone heals itself (2026-10-29: a clone cut off by a crash loop left a .git with an
incomplete object store that passed for a clone; every landing check's fetch of main failed for hours, re-queued
silently). The clone is atomic (a temp directory, verified, renamed into place), a broken clone is re-cloned and
swapped in under the clone lock, and a change re-queued for the same infra reason opens a condition."""
import os, subprocess, sys, tempfile, unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from test_agentctl import A  # noqa: E402

GIT_ID = ["-c", "user.email=t@t", "-c", "user.name=t"]


def make_source(root):
    """A source repository (the remote's stand-in) with main at two commits. Returns its path."""
    src = os.path.join(root, "src")
    subprocess.run(["git", "init", "-q", "-b", "main", src], check=True)
    for i in (1, 2):
        os.makedirs(os.path.join(src, "d"), exist_ok=True)
        with open(os.path.join(src, "d", f"f{i}"), "w") as f:
            f.write(f"{i}\n")
        subprocess.run(["git", "-C", src, "add", "."], check=True)
        subprocess.run(["git", "-C", src, *GIT_ID, "commit", "-qm", f"c{i}"], check=True)
    return src


class AtomicCloneTest(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp()
        self.src = make_source(self.root)
        self.repo = os.path.join(self.root, "var", "movies")
        self.saved_env = dict(os.environ)
        os.environ["AGENTCTL_MOVIES_REMOTE"] = self.src

    def tearDown(self):
        os.environ.clear()
        os.environ.update(self.saved_env)

    def test_a_clone_lands_verified_and_leaves_no_temp_directory(self):
        self.assertEqual(A.ensure_server_clone(self.repo), self.repo)
        self.assertTrue(A.clone_verify(self.repo)[0])
        self.assertEqual(A.clone_tmp_dirs(self.repo), [])

    def test_an_interrupted_clone_never_leaves_a_target_that_looks_complete(self):
        real_run = A.run_clone

        def cut_off(cmd, what):  # the clone writes part of a .git, then the process is cut off
            os.makedirs(os.path.join(cmd[-1], ".git", "objects"), exist_ok=True)
            open(os.path.join(cmd[-1], ".git", "shallow_abc123"), "w").close()
            raise A.subprocess.TimeoutExpired(cmd, 1800)
        A.run_clone = cut_off
        try:
            with self.assertRaises(SystemExit):
                A.ensure_server_clone(self.repo)
        finally:
            A.run_clone = real_run
        self.assertFalse(os.path.exists(self.repo))
        self.assertEqual(A.clone_tmp_dirs(self.repo), [])
        A.ensure_server_clone(self.repo)  # the next start clones afresh
        self.assertTrue(A.clone_verify(self.repo)[0])

    def test_a_clone_that_does_not_verify_is_never_renamed_into_place(self):
        real_run = A.run_clone

        def no_refs(cmd, what):  # git said 0 but the clone has no origin/main
            subprocess.run(["git", "init", "-q", cmd[-1]], check=True)
            return A.subprocess.CompletedProcess(cmd, 0, "", "")
        A.run_clone = no_refs
        try:
            with self.assertRaises(SystemExit) as cm:
                A.ensure_server_clone(self.repo)
        finally:
            A.run_clone = real_run
        self.assertIn("does not verify", str(cm.exception))
        self.assertFalse(os.path.exists(self.repo))
        self.assertEqual(A.clone_tmp_dirs(self.repo), [])

    def test_a_killed_clones_temp_directory_is_removed_at_the_next_start(self):
        os.makedirs(self.repo)
        os.rmdir(self.repo)
        stale = os.path.join(os.path.dirname(self.repo), f".movies{A.CLONE_TMP}999999999-1")
        os.makedirs(os.path.join(stale, ".git"))
        A.ensure_server_clone(self.repo)
        self.assertFalse(os.path.exists(stale))
        self.assertTrue(A.clone_verify(self.repo)[0])

    def test_a_missing_blob_of_mains_tree_fails_the_verification(self):
        A.ensure_server_clone(self.repo)
        blob = A.git(self.repo, "rev-parse", f"{A.remote()}/main:d/f2").strip()
        os.unlink(os.path.join(self.repo, ".git", "objects", blob[:2], blob[2:]))
        ok, detail = A.clone_verify(self.repo)
        self.assertFalse(ok)
        self.assertIn("missing", detail)


def corrupt(repo):
    """The incident's clone: main's commit object gone from the store, a stray shallow_* temp file."""
    sha = A.git(repo, "rev-parse", f"{A.remote()}/main").strip()
    subprocess.run(["git", "-C", repo, "repack", "-q", "-a", "-d"], check=True)  # one pack: then unpack, minus one
    pack = [f for f in os.listdir(os.path.join(repo, ".git", "objects", "pack")) if f.endswith(".pack")][0]
    with open(os.path.join(repo, ".git", "objects", "pack", pack), "rb") as f:
        data = f.read()
    for f in os.listdir(os.path.join(repo, ".git", "objects", "pack")):
        os.unlink(os.path.join(repo, ".git", "objects", "pack", f))
    subprocess.run(["git", "-C", repo, "unpack-objects", "-q"], input=data, check=True)
    os.unlink(os.path.join(repo, ".git", "objects", sha[:2], sha[2:]))
    open(os.path.join(repo, ".git", "shallow_Xy12"), "w").close()
    return sha


class HealTest(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp()
        self.src = make_source(self.root)
        self.repo = os.path.join(self.root, "var", "movies")
        self.saved_env = dict(os.environ)
        os.environ.update(AGENTCTL_MOVIES_REMOTE=self.src, AGENTCTL_MOVIES_REPO=self.repo)
        A._clone_repair_at.clear()
        self.store = A.Store(path=os.path.join(self.root, "store.db"))
        self.saved = A.open_store
        A.open_store = lambda *a, **k: self.store
        A.ensure_server_clone(self.repo)

    def tearDown(self):
        A.open_store = self.saved
        os.environ.clear()
        os.environ.update(self.saved_env)

    def conds(self, state="open"):
        return [it for it in self.store.items("condition", [state]) if it["data"].get("kind") == "CLONE"]

    def test_a_healthy_clone_probes_ok(self):
        r = A.run_probe("clone_health", {"repo": self.repo})
        self.assertTrue(r.ok, r.detail)

    def test_a_corrupt_fetch_marks_the_clone_and_the_tick_repairs_it_keeping_one_broken_copy(self):
        older = f"{self.repo}.broken-20261001-000000"
        os.makedirs(older)
        corrupt(self.repo)
        self.assertFalse(A.run_probe("clone_health", {"repo": self.repo}).ok)
        real_git = A.git

        def failing_fetch(repo, *args, **k):  # what ci-runner's fetch said, all day
            if args[:1] == ("fetch",) and repo == self.repo:
                return A.subprocess.CompletedProcess(args, 128, "", "error: Could not read 409b8bb3c1\nfatal: remote "
                                                     "did not send all necessary objects")
            return real_git(repo, *args, **k)
        A.git = failing_fetch
        try:
            ok, why = A.fetch_main(self.repo)
        finally:
            A.git = real_git
        self.assertFalse(ok)
        self.assertIn("did not send all necessary objects", why)
        self.assertTrue(os.path.exists(A.clone_side(self.repo, "suspect.json")))
        line = A.clone_heal_tick(self.repo)
        self.assertIn("re-cloned and swapped in", line)
        self.assertTrue(A.clone_verify(self.repo)[0])
        self.assertTrue(A.run_probe("clone_health", {"repo": self.repo}).ok)
        broken = [p for p in os.listdir(os.path.dirname(self.repo)) if p.startswith("movies.broken-")]
        self.assertEqual(len(broken), 1)  # the older one went after the swap
        self.assertNotEqual(os.path.join(os.path.dirname(self.repo), broken[0]), older)
        self.assertFalse(os.path.exists(A.clone_side(self.repo, "suspect.json")))
        self.assertEqual(self.conds("open"), [])  # opened with the error, closed by the same probe
        closed = self.conds("closed")
        self.assertEqual(len(closed), 1)
        self.assertIn("CLONE BROKEN", closed[0]["data"]["text"])
        self.assertEqual(A.clone_tmp_dirs(self.repo), [])

    def test_a_fetch_failing_on_the_network_marks_nothing_and_repairs_nothing(self):
        A.git(self.repo, "remote", "set-url", A.remote(), os.path.join(self.root, "gone"))
        ok, why = A.fetch_main(self.repo)
        self.assertFalse(ok)
        self.assertFalse(os.path.exists(A.clone_side(self.repo, "suspect.json")))
        self.assertIsNone(A.run_probe("clone_health", {"repo": self.repo}).ok)  # cannot tell: never a repair

    def test_a_suspect_mark_on_a_healthy_clone_just_goes(self):
        A.write_json(A.clone_side(self.repo, "suspect.json"), {"at": 1, "error": "Could not read 409b8bb3c1"})
        self.assertIn("is fine", A.clone_heal_tick(self.repo))
        self.assertFalse(os.path.exists(A.clone_side(self.repo, "suspect.json")))
        self.assertFalse([p for p in os.listdir(os.path.dirname(self.repo)) if ".broken-" in p])

    def test_at_startup_a_clone_that_does_not_verify_is_repaired(self):
        corrupt(self.repo)
        A.ensure_server_clone(self.repo)
        self.assertTrue(A.clone_verify(self.repo)[0])
        self.assertEqual(len([p for p in os.listdir(os.path.dirname(self.repo)) if ".broken-" in p]), 1)

    def test_the_check_trees_are_relinked_and_keep_their_build(self):
        tree = f"{self.repo}-wt-check-1"
        A.git(self.repo, "worktree", "add", "-q", "--detach", tree, f"{A.remote()}/main")
        os.makedirs(os.path.join(tree, "target"))
        open(os.path.join(tree, "target", "warm"), "w").close()
        corrupt(self.repo)
        A.repair_clone(self.repo, "a test")
        self.assertEqual(A.git(tree, "rev-parse", "HEAD").strip(), A.git(self.repo, "rev-parse", f"{A.remote()}/main").strip())
        A.git(tree, "reset", "-q", "--hard")
        self.assertTrue(os.path.exists(os.path.join(tree, "target", "warm")))
        listed = [os.path.realpath(l.split()[0]) for l in A.git(self.repo, "worktree", "list").splitlines()]
        self.assertIn(os.path.realpath(tree), listed)

    def test_the_swap_waits_for_the_clones_users_and_gives_up_in_bounds(self):
        import threading
        corrupt(self.repo)
        held, release = threading.Event(), threading.Event()

        def user():
            with A.clone_shared(self.repo):
                held.set()
                release.wait(30)
        t = threading.Thread(target=user)
        t.start()
        held.wait(10)
        os.environ["AGENTCTL_CLONE_LOCK_WAIT"] = "0.5"
        self.assertIsNone(A.repair_clone(self.repo, "a test"))  # the user never let go: no swap, next tick
        self.assertFalse(A.clone_verify(self.repo)[0])
        self.assertEqual(A.clone_tmp_dirs(self.repo), [])
        os.environ["AGENTCTL_CLONE_LOCK_WAIT"] = "30"
        threading.Timer(1.0, release.set).start()
        line = A.repair_clone(self.repo, "a test")  # waits for the user, then swaps
        t.join(10)
        self.assertIn("swapped in", line)
        self.assertTrue(A.clone_verify(self.repo)[0])

    def test_a_persons_checkout_is_never_healed(self):
        os.environ.pop("AGENTCTL_MOVIES_REMOTE")
        self.assertFalse(A.heal_enabled(self.repo))
        self.assertFalse(A.note_clone_suspect(self.repo, "Could not read 409b8bb3c1"))


class RequeueLoopTest(unittest.TestCase):
    """2026-10-29: every change re-queued as "infra (its fetch of main failed)" every 5-10 min, for hours, silently."""
    def setUp(self):
        self.d = tempfile.mkdtemp()
        os.makedirs(os.path.join(self.d, "entries"))
        self.st = A.Store(path=os.path.join(self.d, "store.db"))
        self.opened = []
        self.saved = A.open_store, A.wlog, A.open_condition
        A.open_store = lambda *a, **k: self.st
        A.wlog = lambda d_, m: None
        # open_condition's own rule: no condition while its probe passes (nothing is wrong now)
        A.open_condition = lambda kind, text, probe, acts, **kw: None if probe.ok else \
            self.opened.append((kind, text, probe, kw)) or {"key": kw.get("key")}
        self.e = {"id": "e1", "seq": 1, "status": "checking", "worktree": "/w", "head": "a" * 40}
        A.write_json(os.path.join(self.d, "entries", "e1.json"), self.e)

    def tearDown(self):
        A.open_store, A.wlog, A.open_condition = self.saved

    def again(self, why, **kw):
        A.requeue_infra(self.d, self.e, why, **kw)
        self.st.transition("change", "e1", "checking", self.st.item("change", "e1")["version"])
        self.e["status"] = "checking"

    def test_the_third_requeue_for_the_same_reason_opens_a_condition_with_gits_error(self):
        err = "error: Could not read 409b8bb3c1\nfatal: remote did not send all necessary objects"
        for _ in range(3):
            self.assertEqual(self.opened, [])  # the first two: events only
            self.again(f"{A.FETCH_MAIN_FAILED}: fatal: remote did not send all necessary objects",
                       reason=A.FETCH_MAIN_FAILED, detail=err, repo=self.d)
        self.assertEqual(len(self.opened), 1)
        kind, text, probe, kw = self.opened[0]
        self.assertEqual(kind, "STALLED")
        self.assertIn("re-queued 3 times", text)
        self.assertIn("did not send all necessary objects", text)
        self.assertIn("error: Could not read 409b8bb3c1", kw["tail"])
        self.assertEqual(probe.kind, "clone_health")  # the clone's: cleared when it fetches main again
        self.assertTrue(kw["key"].startswith("main-fetch-"))
        self.assertIn("Could not read", "".join(kw["tail"]))
        queued = [x["text"] for x in self.st.events() if x["to_state"] == "queued"]
        self.assertTrue(all("did not send all necessary objects" in t for t in queued), queued)

    def test_a_credential_failure_opens_one_condition_for_the_clone_at_once_never_an_infra_loop(self):
        """2026-11-22: ci-runner's App helper died (ghapp.py run as a script), every check-branch push said `fatal:
        could not read User`, and four changes were re-queued "an infra failure" for hours, each on its own timer."""
        helper = ("push of check/e1 failed: Traceback ...\nModuleNotFoundError: No module named 'agentctl'\n"
                  "fatal: could not read Username for 'https://github.com': terminal prompts disabled")
        self.again("its checks stalled (on ci-runner (no verdict): fatal: could not read Username)", detail=helper,
                   repo=self.d)
        self.assertEqual(len(self.opened), 1, "the first one: no retry brings a credential back")
        kind, text, probe, kw = self.opened[0]
        self.assertEqual(probe.kind, "push_credential")
        self.assertEqual(kw["key"], f"push-credential-{os.path.basename(self.d)}")
        self.assertIn("PUSH CREDENTIAL", text)
        self.assertIn("Not the vault's", text)
        self.assertIn("e1", text)
        self.assertIn("ModuleNotFoundError: No module named 'agentctl'", "".join(kw["tail"]))
        self.assertIn("could not read Username", A.retry_hold(A.read_record(os.path.join(self.d, "entries",
                                                                                         "e1.json")))[1])

    def credential_probe(self, since):
        return A.run_probe("push_credential", {"d": self.d, "since": since, "host": A.host_role()})

    def test_the_credential_condition_clears_on_the_next_push_that_succeeds_never_by_asking_a_helper(self):
        """2026-11-22 review: a probe running `git credential fill` minted a GitHub App token every 60 s while open."""
        saved = A.git, A.run_cmd
        self.addCleanup(lambda: (setattr(A, "git", saved[0]), setattr(A, "run_cmd", saved[1])))
        A.git = A.run_cmd = lambda *a, **k: self.fail(f"the probe ran {a}")
        self.again("its checks stalled (on ci-runner: fatal: could not read Username)", repo=self.d)
        since = self.opened[0][2].args["since"]
        got = self.credential_probe(since)
        self.assertFalse(got.ok, got.detail)
        self.assertIn("1 change(s) wait", got.detail)
        e = A.read_record(os.path.join(self.d, "entries", "e1.json"))
        A.write_json(os.path.join(self.d, "entries", "e1.json"), {**e, "remote": {"pushed_at": since - 5}})
        self.assertFalse(self.credential_probe(since).ok, "a push before the condition opened says nothing")
        A.write_json(os.path.join(self.d, "entries", "e1.json"), {**e, "remote": {"pushed_at": since + 5}})
        got = self.credential_probe(since)
        self.assertTrue(got.ok, got.detail)
        self.assertIn("push succeeded", got.detail)

    def test_the_credential_condition_clears_when_no_change_waits_on_it(self):
        self.again("its checks stalled (on ci-runner: fatal: could not read Username)", repo=self.d)
        since = self.opened[0][2].args["since"]
        e = A.read_record(os.path.join(self.d, "entries", "e1.json"))
        A.write_json(os.path.join(self.d, "entries", "e1.json"), {**e, "status": "rejected"})
        self.assertTrue(self.credential_probe(since).ok)

    def test_the_credential_condition_is_read_only_where_the_push_failed(self):
        self.again("its checks stalled (on ci-runner: fatal: could not read Username)", repo=self.d)
        args = self.opened[0][2].args
        self.assertEqual(args["host"], A.host_role())
        got = A.run_probe("push_credential", {**args, "host": "elsewhere"})
        self.assertIsNone(got.ok, "another host cannot tell")

    def test_a_credential_github_refuses_opens_the_same_one_condition(self):
        self.again("its checks stalled (on ci-runner: fatal: Authentication failed for 'https://github.com/o/r/')",
                   repo=self.d)
        self.assertEqual([p.kind for _k, _t, p, _kw in self.opened], ["push_credential"])

    def test_a_different_reason_starts_the_count_again(self):
        self.again("its checks stalled (sbt)")
        self.again("its check passed its deadline with no live checker")
        self.again("its checks stalled (a hook)")
        self.assertEqual(self.opened, [])
        self.again("its checks stalled (sbt again)")
        self.assertEqual(self.opened, [])
        self.again("its checks stalled (sbt once more)")
        self.assertEqual(len(self.opened), 1)
        self.assertEqual(self.opened[0][2].kind, "infra_requeue")
        self.assertEqual(self.opened[0][3]["key"], "infra-requeue-e1")

    def test_thirty_minutes_on_the_same_reason_open_it_before_the_third(self):
        self.again("its checks stalled (sbt)")
        self.e["infra_reason_since"] -= 31 * 60
        self.again("its checks stalled (sbt)")
        self.assertEqual(len(self.opened), 1)
        self.assertIn("re-queued 2 times", self.opened[0][1])

    def test_the_change_condition_clears_when_the_change_gets_past_it(self):
        for _ in range(3):
            self.again("its checks stalled (sbt)")
        args = self.opened[0][2].args
        self.assertFalse(A.run_probe("infra_requeue", args).ok)  # checking, the same reason: still stuck
        A.write_json(os.path.join(self.d, "entries", "e1.json"), {**self.e, "status": "checked", "infra_reason": None})
        self.assertTrue(A.run_probe("infra_requeue", args).ok)

    def test_a_checks_fetch_of_main_failing_says_why(self):
        saved = A.precheck_hook, A.fetch_main
        A.precheck_hook = lambda repo: None
        A.fetch_main = lambda repo: (False, "error: Could not read 409b8bb3c1\nfatal: remote did not send all "
                                            "necessary objects")
        try:
            self.assertEqual(A._check_entry(self.d, self.d, self.e, 1), 0)
        finally:
            A.precheck_hook, A.fetch_main = saved
        self.assertEqual(self.e["status"], "queued")
        self.assertIn("its fetch of main failed: fatal: remote did not send all necessary objects", self.e["why"])
        self.assertEqual(self.e["infra_reason"], A.FETCH_MAIN_FAILED)


class KeepaliveTest(unittest.TestCase):
    """A ~650 MB clone outlasts TimeoutStartSec=300 and WatchdogSec=120: it keeps systemd told, and is bounded."""
    def setUp(self):
        self.sent = []
        self.saved = A.sd_notify, A.SD_READY[0], dict(os.environ)
        A.sd_notify = lambda m: self.sent.append(m) or True
        os.environ.update(AGENTCTL_CLONE_KEEPALIVE="0.2")

    def tearDown(self):
        A.sd_notify, A.SD_READY[0] = self.saved[0], self.saved[1]
        os.environ.clear()
        os.environ.update(self.saved[2])

    def test_before_ready_a_long_clone_extends_the_start_timeout_with_a_status(self):
        A.SD_READY[0] = False
        r = A.run_clone(["sh", "-c", "sleep 1; echo done; echo note >&2"], "cloning x")
        self.assertEqual((r.returncode, r.stdout.strip(), r.stderr.strip()), (0, "done", "note"))
        self.assertGreaterEqual(len(self.sent), 3)
        self.assertTrue(all(m.startswith("EXTEND_TIMEOUT_USEC=") and "\nSTATUS=cloning x (" in m for m in self.sent))
        self.assertGreater(int(self.sent[0].split("\n")[0].split("=")[1]), 0.2 * 1_000_000)

    def test_after_ready_it_pets_the_watchdog(self):
        A.SD_READY[0] = True
        A.run_clone(["sh", "-c", "sleep 1"], "re-cloning x")
        self.assertGreaterEqual(len(self.sent), 3)
        self.assertTrue(all(m.startswith("WATCHDOG=1\nSTATUS=re-cloning x") for m in self.sent))

    def test_the_clone_is_bounded(self):
        os.environ["AGENTCTL_CLONE_TIMEOUT"] = "1"
        import time
        t0 = time.monotonic()
        with self.assertRaises(A.subprocess.TimeoutExpired):
            A.run_clone(["sleep", "30"], "cloning x")
        self.assertLess(time.monotonic() - t0, 10)


if __name__ == "__main__":
    unittest.main()
