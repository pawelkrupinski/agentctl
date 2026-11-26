"""ONE QUEUE, CLOSED BY CONTENT: the 2026-10-30 split-brain shapes.

Six pre-cutover entries in the laptop's local waiting dir and four store items mirrored from them sat "waiting" for
11 h, every one already on main under REBASED shas (landing rebases), the status line said "waiting: 0", and W1
escalated with an id nothing could cancel."""
import json, os, sys, time, unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from test_agentctl import A, Base, sh  # noqa: E402
from fake_clock import FakeClock, on_clock  # noqa: E402


class OneQueueBase(Base):
    def setUp(self):
        super().setUp()
        self.saved_dir = A.WAITING_DIR
        A.WAITING_DIR = os.path.join(self.tmp.name, "waiting-state")
        A._landed_looked.clear()
        self.d = A.waiting_state(self.r.path)[0]
        self.st = A.process_store()

    def tearDown(self):
        A.WAITING_DIR = self.saved_dir
        super().tearDown()

    def change(self, name, text):
        """A worktree with one commit off main: (worktree, head)."""
        wt = self.r.worktree(name, branch=name)
        self.r.write(f"{name}.txt", text, at=wt)
        return wt, self.r.commit(f"{name}: change", at=wt)

    def land_rebased(self, head):
        """The change lands on main under a NEW sha (main moved first, then a rebase): as `land` does."""
        other = self.r.worktree("other", branch="other")
        self.r.write("other.txt", "moved\n", at=other)
        self.r.commit("main moves", at=other)
        self.r.push(at=other)
        sh(other, "git", "cherry-pick", head)
        new = sh(other, "git", "rev-parse", "HEAD")
        self.r.push(at=other)
        self.assertNotEqual(new, head)
        return new

    def entry(self, eid, wt, head, status="waiting", seq=1):
        e = {"id": eid, "seq": seq, "status": status, "worktree": wt, "head": head, "commits": [head],
             "created": A.ts()}
        A.save_entry(self.d, e)
        return e


class LandedByContentTest(OneQueueBase):
    def test_an_entry_whose_commits_landed_under_rebased_shas_closes_as_landed_with_an_event(self):
        wt, head = self.change("proxy-park", "a\n")
        self.entry("20261028-162641-movies-wt-proxy-park-92", wt, head)
        via = self.land_rebased(head)
        did = A.close_landed_changes(self.r.path, A.Clock(), self.st, self.d)
        self.assertEqual(len(did), 1, did)
        e = A.read_record(os.path.join(self.d, "entries", "20261028-162641-movies-wt-proxy-park-92.json"))
        self.assertEqual((e["status"], e["landed_sha"]), ("landed", via))
        it = self.st.item("change", "20261028-162641-movies-wt-proxy-park-92")
        self.assertEqual(it["state"], "landed")
        self.assertTrue(any(ev["kind"] == "change.landed" for ev in self.st.events(0)))

    def test_a_store_item_no_entry_carries_closes_by_its_head(self):
        wt, head = self.change("proxy-park", "b\n")
        self.st.put_item("change", "x-95", "waiting", {"worktree": wt, "head": head})
        via = self.land_rebased(head)
        did = A.close_landed_changes(self.r.path, A.Clock(), store=self.st)  # the laptop's look: no entries of its own
        self.assertEqual(did, ["x-95: its commits are on main (by content): landed"])
        it = self.st.item("change", "x-95")
        self.assertEqual(it["state"], "landed")
        # review round 6: its landed sha is the main commit carrying it, never its pre-rebase head (await_entry watches
        # the item's landed_sha, else its head: a sha no main run contains, watched for 3 h to UNKNOWN)
        self.assertEqual(it["data"].get("landed_sha"), via)

    def test_an_unlanded_change_stays_and_a_head_this_clone_lacks_is_never_guessed(self):
        wt, head = self.change("open", "c\n")
        self.entry("open-1", wt, head, status="queued")
        self.st.put_item("change", "far-1", "waiting", {"head": "1" * 40})
        self.assertEqual(A.close_landed_changes(self.r.path, A.Clock(), self.st, self.d), [])
        self.assertTrue(A.change_state(self.r.path, "1" * 40).unknown)
        self.assertEqual(A.change_state(self.r.path, head).state, A.NOT_LANDED)

    def test_each_change_is_looked_at_once_per_interval(self):
        wt, head = self.change("later", "d\n")
        self.entry("later-1", wt, head)
        t = A.epoch_now()
        self.assertEqual(A.close_landed_changes(self.r.path, FakeClock(t), self.st, self.d), [])
        self.land_rebased(head)
        self.assertEqual(A.close_landed_changes(self.r.path, FakeClock(t + 5), self.st, self.d), [])
        self.assertEqual(len(A.close_landed_changes(self.r.path, FakeClock(t + A.LANDED_LOOK_EVERY), self.st, self.d)), 1)


class FakeRemote:
    """The laptop's view of ci-runner's store: its submissions recorded."""

    def __init__(self, st, refuse=False):
        self.st, self.refuse, self.submitted = st, refuse, []

    def __getattr__(self, name):
        return getattr(self.st, name)

    def submit(self, e):
        if self.refuse:
            raise A.StoreUnavailable("the tunnel is down")
        self.submitted.append(e)
        return {"id": e["id"], "seq": 1, "ahead": 0}


class LocalQueueTest(OneQueueBase):
    def setUp(self):
        super().setUp()
        self.saved_role, self.saved_ref = os.environ.get("AGENTCTL_ROLE"), A.submit_ref
        os.environ["AGENTCTL_ROLE"] = "sync-test"  # the laptop
        self.refs = []
        A.submit_ref = lambda repo, d, e, to_server=False: self.refs.append(e["id"]) or f"refs/heads/land/{e['id']}"

    def tearDown(self):
        os.environ["AGENTCTL_ROLE"] = self.saved_role
        A.submit_ref = self.saved_ref
        super().tearDown()

    def test_the_laptops_local_entries_move_to_ci_runners_queue_or_close_landed(self):
        self.assertEqual(A.host_role(), "sync")
        wa, ha = self.change("a", "a\n")
        wb, hb = self.change("b", "b\n")
        self.entry("q-97", wa, ha, seq=1)
        self.entry("q-106", wb, hb, seq=2)
        self.land_rebased(hb)
        remote = FakeRemote(self.st)
        did = A.migrate_local_queue(self.r.path, remote)
        self.assertEqual(sorted(did), ["q-106: its commits are on main (by content): landed",
                                       "q-97: moved to ci-runner's queue"])
        self.assertEqual(self.refs, ["q-97"])
        self.assertEqual([(e["id"], e["head"]) for e in remote.submitted], [("q-97", ha)])
        es = {e["id"]: e for e in A.waiting_entries(self.d, open_only=False)}
        self.assertEqual((es["q-97"]["status"], es["q-106"]["status"]), ("migrated", "landed"))
        self.assertEqual(A.migrate_local_queue(self.r.path, remote), [], "moved once")

    def test_an_entry_that_cannot_move_is_said_loudly_and_tried_again(self):
        wa, ha = self.change("a", "a\n")
        self.entry("q-110", wa, ha)
        told = []
        saved = A.inbox_write
        A.inbox_write = lambda *a, **k: told.append(a[1])
        try:
            did = A.migrate_local_queue(self.r.path, FakeRemote(self.st, refuse=True))
            A.migrate_local_queue(self.r.path, FakeRemote(self.st, refuse=True))
        finally:
            A.inbox_write = saved
        self.assertIn("LOCAL QUEUE: q-110", did[0])
        self.assertEqual(len(told), 1, "once an hour, not every cycle")
        self.assertEqual(A.waiting_entries(self.d, open_only=False)[0]["status"], "waiting")

    def test_the_server_never_migrates(self):
        os.environ["AGENTCTL_ROLE"] = "server"
        wa, ha = self.change("a", "a\n")
        self.entry("q-1", wa, ha)
        self.assertEqual(A.migrate_local_queue(self.r.path, FakeRemote(self.st)), [])


class StrandedTest(OneQueueBase):
    def test_an_item_no_queue_carries_opens_a_condition_that_its_own_probe_closes(self):
        self.st.put_item("change", "x-101", "waiting", {"worktree": "/w", "head": "2" * 40})
        wa, ha = self.change("a", "a\n")
        self.entry("carried-1", wa, ha)
        A.project_changes(self.d, self.st)
        self.assertEqual(A.stranded_changes(self.r.path, self.d, self.st), ["x-101"])
        [c] = [c for c in A.open_conditions(self.st) if c["key"].startswith("change-stranded")]
        self.assertIn("STRANDED CHANGE x-101", c["text"])
        A.cancel_submitted(A.CurrentStore(A.process_store), "x-101", repo=self.r.path)  # `land cancel` reaches an item no entry carries
        self.assertEqual(self.st.item("change", "x-101")["state"], "cancelled")
        A.recovery_tick(now_=A.epoch_now() + 3600, force=True, store=self.st)
        self.assertEqual([c for c in A.open_conditions(self.st) if c["key"].startswith("change-stranded")], [])


class SupersedeTest(OneQueueBase):
    def test_a_submission_supersedes_the_older_items_of_its_worktree_that_no_entry_carries(self):
        wt, head = self.change("proxy-park", "a\n")
        self.st.put_item("change", "x-95", "waiting", {"worktree": wt, "head": "4" * 40})
        self.st.put_item("change", "y-1", "waiting", {"worktree": "/elsewhere", "head": "5" * 40})
        r = A.submit_change(A.CurrentStore(A.process_store), {"id": "new-1", "worktree": wt, "head": head, "commits": [head]}, repo=self.r.path)
        self.assertEqual(r["id"], "new-1")
        self.assertEqual(self.st.item("change", "x-95")["state"], "superseded")
        self.assertEqual(self.st.item("change", "x-95")["data"]["superseded_by"], "new-1")
        self.assertEqual(self.st.item("change", "y-1")["state"], "waiting")


class OneViewTest(OneQueueBase):
    def test_w1_counts_only_unlanded_work_and_names_it_and_why(self):
        wt, head = self.change("proxy-park", "a\n")
        self.st.put_item("change", "x-92", "waiting", {"worktree": wt, "head": head,
                                                       "submitted_at": A.epoch_now() - 11 * 3600})
        self.st.put_item("change", "x-112", "queued", {"worktree": wt, "head": "3" * 40, "submitted_at": A.epoch_now()})
        saved = os.environ.get("AGENTCTL_MOVIES_REPO")
        os.environ["AGENTCTL_MOVIES_REPO"] = self.r.path
        try:
            ok, detail = A.inv_w1(FakeClock(), A.CurrentStore(A.process_store))
            self.assertFalse(ok)
            self.assertIn("x-92 (waiting: waiting for green, the gate unread (no CI snapshot)) is 11 h old", detail)
            self.land_rebased(head)
            A._landed_looked.clear()
            ok, detail = A.inv_w1(FakeClock(), A.CurrentStore(A.process_store))  # its own landed look closes x-92 before it counts
        finally:
            os.environ["AGENTCTL_MOVIES_REPO"] = saved
        self.assertTrue(ok, detail)
        self.assertEqual(self.st.item("change", "x-92")["state"], "landed")

    def test_the_status_snapshot_counts_the_stores_items_never_a_hosts_entry_files(self):
        self.st.put_item("change", "x-95", "waiting", {"worktree": "/h/movies-wt-proxy-park"})
        self.st.put_item("change", "x-96", "checking", {"worktree": "/h/movies-wt-b"})
        A.write_snapshot(self.d, self.st)
        snap = A.state_read()
        self.assertEqual((snap["waiting"], snap["queued"]), (["/h/movies-wt-proxy-park"], ["/h/movies-wt-b"]))

    def test_one_repository_resolver_names_the_servers_own_clone_on_ci_runner(self):
        saved = os.environ.pop("AGENTCTL_MOVIES_REPO", None)
        try:
            os.environ["AGENTCTL_ROLE"] = "server"
            self.assertEqual(A.movies_repo_path(), os.path.join(A.HOME, "movies"))
            self.assertEqual(A.server_repo(), A.movies_repo_path())
            os.environ["AGENTCTL_ROLE"] = "sync-test"
            self.assertEqual(A.movies_repo_path(), os.path.join(A.HOME, "projects", "movies"))
        finally:
            os.environ["AGENTCTL_ROLE"] = "server"
            if saved is not None:
                os.environ["AGENTCTL_MOVIES_REPO"] = saved



class TreeSlotTest(OneQueueBase):
    def test_a_push_whose_check_tree_another_checker_holds_pushes_from_the_push_tree(self):
        """2026-11-06: three checked changes sat 30 min past their deadline, `busy` every tick: each check tree was held
        by a checker waiting on its remote CI run (up to its 2.4 h max hold), and a push waited for its own tree."""
        trees = []
        saved = A._push_change
        A._push_change = lambda repo, d, e, i, clock, st: trees.append(i) or "landed"
        other = A.tree_slot(self.st, self.d, 1)
        self.assertTrue(other.acquire())
        try:
            e = {"id": "c-1", "tree": 1, "tip": "6" * 40, "status": "checked"}
            self.assertEqual(A.push_change(self.r.path, self.d, e, *on_clock(A.Clock())), "landed")
            self.assertEqual(trees, [A.PUSH_TREE], "never the tree another checker holds")
            push = A.tree_slot(self.st, self.d, A.PUSH_TREE)
            self.assertTrue(push.acquire(), "the push tree's lease is released after the push")
            try:
                self.assertEqual(A.push_change(self.r.path, self.d, e, *on_clock(A.Clock())), "busy", "both held: it waits")
            finally:
                push.release()
            other.release()
            self.assertEqual(A.push_change(self.r.path, self.d, e, *on_clock(A.Clock())), "landed")
            self.assertEqual(trees[-1], 1, "its own tree, free, first: its tip and build are there")
        finally:
            A._push_change = saved
            other.release()

    def test_a_batch_push_never_touches_a_check_tree_another_checker_holds(self):
        other = A.tree_slot(self.st, self.d, 1)
        self.assertTrue(other.acquire())
        try:
            e = {"id": "c-1", "tree": 1, "tip": "6" * 40, "status": "checked"}
            it = {"id": "b-1", "data": {"tip": "6" * 40, "base": "7" * 40, "tree": 1, "members": ["c-1"]}}
            saved = A.batch_members
            A.batch_members = lambda d, it_: [e]
            try:
                self.assertEqual(A.push_batch_item(self.r.path, self.d, self.st, it), "batch b-1: its check tree is busy")
            finally:
                A.batch_members = saved
        finally:
            other.release()



class BatchLeaseTest(OneQueueBase):
    def test_the_batch_executor_holds_its_leases_through_check_and_bisect(self):
        it = self.st.put_item("batch", A.batch_key(self.d) + "#1", "checking",
                              {"tree": 1, "base": "7" * 40, "tip": "6" * 40, "members": []})
        A.check_batch(self.r.path, it["id"], *on_clock(self.clock))
        for name in (f"check/batch-{it['id']}", f"slot/check/{A.batch_key(self.d)}/1"):
            row = self.st.get(name)
            self.assertAlmostEqual(row["max_until"] - row["acquired_at"], A.BATCH_LEASE_MAX, delta=5, msg=name)


if __name__ == "__main__":
    unittest.main()
