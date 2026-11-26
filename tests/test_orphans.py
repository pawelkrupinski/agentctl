"""Orphaned work (the owner, 2026-10-28: "review and fix landing code ... so things don't get missed or stalled"): a
worktree whose commits are not on main and whose author is gone gets ONE re-land (or a fixer for a real failure), then
UNLANDED ORPHAN, a condition that clears when its commits land or somebody works in it again. Nothing is deleted."""
import contextlib, io, json, os, sys, time, unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from test_agentctl import A, sh  # noqa: E402
from fake_clock import FakeClock  # noqa: E402
from test_red import RedBase  # noqa: E402


class OrphanTest(RedBase):
    def setUp(self):
        super().setUp()
        # the inbox's verdicts and the store's reads on ONE clock (the wall's reading at the start: the test stamps worktree
        # and session times from the wall); the default test clock lags it by design
        self.clock = FakeClock()
        A.wire_collaborators(self.clock, A.CurrentStore(A.process_store, self.clock.time))
        os.environ["AGENTCTL_ORPHAN_IDLE"] = "0"
        self.saved_o = A.process_cwds, A.reland_worktree
        A.process_cwds = lambda: {}
        self.relands = []
        A.reland_worktree = lambda c, a, st: self.relands.append(a["worktree"]) or "landing again"

    def tearDown(self):
        A.process_cwds, A.reland_worktree = self.saved_o
        os.environ.pop("AGENTCTL_ORPHAN_IDLE", None)
        super().tearDown()

    def worktree_commit(self, name, msg, base="origin/main", intent=True):
        """intent: its author ran `agentctl land` there once (LAND INTENT), the case every older test here assumed."""
        wt, sha = super().worktree_commit(name, msg, base)
        if intent:
            self.intend(wt)
        return wt, sha

    def run_tool(self, *argv):
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
            rc = A.main(list(argv))
        return rc, out.getvalue()

    def deleted_log(self):
        p = os.path.join(os.environ["AGENTCTL_SCRATCH"], "agentctl-deleted.log")
        return open(p).read() if os.path.exists(p) else ""

    def test_an_orphan_is_relanded_once_then_escalated_and_clears_when_it_lands(self):
        wt, sha = self.worktree_commit("repo-wt-flipflop", "flipflop: a change nobody landed")
        self.assertEqual(A.orphan_duty(A.CurrentStore(A.process_store), self.r.path), ["orphan repo-wt-flipflop: submit (never landed)"])
        self.assertEqual(self.relands, [wt])
        self.assertEqual(A.orphan_duty(A.CurrentStore(A.process_store), self.r.path), ["orphan repo-wt-flipflop: escalated UNLANDED ORPHAN"])
        self.assertEqual(A.orphan_duty(A.CurrentStore(A.process_store), self.r.path), [])  # once
        self.assertEqual(self.relands, [wt])  # one re-land, never a loop
        [e] = self.inbox("UNLANDED_ORPHAN")
        self.assertIn("repo-wt-flipflop: 1 commit(s) not on origin/main", e["verdict"])
        self.assertEqual([c["kind"] for c in A.open_conditions(A.CurrentStore(A.process_store))], ["UNLANDED_ORPHAN"])  # an item: it escalates by its machine
        self.assertEqual(A.recovery_tick(A.CurrentStore(A.process_store), force=True), [])  # still not on main
        sh(wt, "git", "push", "-q", "origin", "HEAD:main")  # it lands
        sh(self.r.path, "git", "fetch", "-q", "origin")
        self.assertEqual(len(A.recovery_tick(A.CurrentStore(A.process_store), force=True)), 1)
        self.assertTrue(all(x["read"] for x in self.inbox("UNLANDED_ORPHAN")))
        self.assertTrue(os.path.isdir(wt))  # nothing deleted

    def test_a_pass_reads_the_inbox_once_however_many_orphans(self):
        """Review 2026-11-07: every orphan's verdict (its worktree and its throwaway tree) and its land intent read the
        whole inbox again -- 7849 entries, ~2.4 s a read on the laptop -- so a pass over 17 escalated orphans spent
        minutes. A pass reads it once."""
        for i in range(4):
            self.worktree_commit(f"repo-wt-o{i}", f"o{i}: a change nobody landed")
        reads, real = [0], A.inbox_entries

        def counted(*a, **k):
            reads[0] += 1
            return real(*a, **k)
        A.inbox_entries = counted
        try:
            A.orphan_duty(A.CurrentStore(A.process_store), self.r.path)
            first = reads[0]
            reads[0] = 0
            A.orphan_duty(A.CurrentStore(A.process_store), self.r.path)
        finally:
            A.inbox_entries = real
        self.assertTrue(self.relands, "the orphans were submitted")
        self.assertEqual((first, reads[0]), (1, 1), "one inbox read a pass, however many orphans")

    def test_a_real_failure_is_submitted_once_too_and_a_live_worktree_is_no_orphan(self):
        """Step 14: an orphan is submitted once whatever its last verdict (its checks decide; a rejection is its red's to
        rescue): a fixer comes only after that submit is rejected (test_a_pre_push_rejection_gets_one_fixer...)."""
        wt, _sha = self.worktree_commit("repo-wt-prune", "workflow prune")
        A.inbox_write("land", "NOT LANDED: the pre-push checks failed (locally, check tree x-wt-check-1)", [], [], 1, cwd=wt)
        A.process_cwds = lambda: {4242: wt}  # somebody works in it: no orphan
        self.assertEqual(A.orphan_duty(A.CurrentStore(A.process_store), self.r.path), [])
        A.process_cwds = lambda: {}
        self.assertEqual(A.orphan_duty(A.CurrentStore(A.process_store), self.r.path), ["orphan repo-wt-prune: submit (NOT LANDED: the pre-push checks "
                                                      "failed (locally, check tree x-wt-check-1))"])
        self.assertEqual(self.relands, [wt])

    def test_an_orphan_an_open_change_already_carries_is_not_submitted(self):
        """ISSUE 20261030T165514-e5f431: maintain re-submitted movies-wt-review with no session behind it while the open
        change movies-wt-ci-5 already carried its commits -- a duplicate in the queue."""
        wt, sha = self.worktree_commit("repo-wt-review", "review: a fix")
        ci5 = self.r.worktree("repo-wt-ci-5", base="origin/main", branch="ci-5")
        sh(ci5, "git", "cherry-pick", sha)  # the same change, another sha, plus ci-5's own
        self.r.write("ci5.txt", "ci-5\n", at=ci5)
        head = self.r.commit("ci-5: its own", at=ci5)
        st = A.process_store()
        st.put_item("change", "20261030-154005-repo-wt-ci-5-819355", "checking", {"worktree": ci5, "head": head})
        self.assertEqual(sorted(A.orphan_duty(A.CurrentStore(A.process_store), self.r.path)),  # ci-5 too: its own open change carries it
                         [f"orphan {n}: carried by open change 20261030-154005-repo-wt-ci-5-819355: not submitted"
                          for n in ("repo-wt-ci-5", "repo-wt-review")])
        self.assertEqual(A.orphan_duty(A.CurrentStore(A.process_store), self.r.path), [], "said once")
        self.assertEqual(self.relands, [])
        self.assertEqual(A.open_conditions(A.CurrentStore(A.process_store)), [], "nothing for a person to do: ci-5's verdict is its author's")
        it = st.item("change", "20261030-154005-repo-wt-ci-5-819355")
        st.transition("change", it["id"], "rejected", it["version"], event={"cause": "agent"})  # ci-5 did not land: nothing carries it now
        self.assertEqual(sorted(A.orphan_duty(A.CurrentStore(A.process_store), self.r.path)), ["orphan repo-wt-ci-5: submit (never landed)",
                                                              "orphan repo-wt-review: submit (never landed)"])
        self.assertEqual(sorted(self.relands), sorted([wt, ci5]))

    def test_an_escalated_orphan_clears_while_an_open_change_carries_it_and_reopens_when_that_closes_unlanded(self):
        """2026-10-30: orphan-movies-wt-ci-1..3, escalated before 588ffd9, stayed ESCALATED while the open change
        …-ci-5-45426e carried all their commits: the probe ignored the rule orphan_duty already used."""
        wt, sha = self.worktree_commit("repo-wt-ci-1", "ci-1: a fix")
        self.assertEqual(A.orphan_duty(A.CurrentStore(A.process_store), self.r.path), ["orphan repo-wt-ci-1: submit (never landed)"])
        self.assertEqual(A.orphan_duty(A.CurrentStore(A.process_store), self.r.path), ["orphan repo-wt-ci-1: escalated UNLANDED ORPHAN"])
        self.assertEqual(len(A.open_conditions(A.CurrentStore(A.process_store))), 1)
        ci5 = self.r.worktree("repo-wt-ci-5", base="origin/main", branch="ci-5")
        sh(ci5, "git", "cherry-pick", sha)
        self.r.write("ci5.txt", "ci-5\n", at=ci5)
        head = self.r.commit("ci-5: its own", at=ci5)
        st = A.process_store()
        cid = "20261030-175250-repo-wt-ci-5-45426e"
        st.put_item("change", cid, "checking", {"worktree": ci5, "head": head})
        self.assertEqual(len(A.recovery_tick(A.CurrentStore(A.process_store), force=True)), 1)  # the probe: covered by the open change
        self.assertEqual(A.open_conditions(A.CurrentStore(A.process_store)), [])
        self.assertIn(f"orphan repo-wt-ci-1: carried by open change {cid}: not submitted", A.orphan_duty(A.CurrentStore(A.process_store), self.r.path))
        self.assertEqual(A.open_conditions(A.CurrentStore(A.process_store)), [])
        it = st.item("change", cid)
        st.transition("change", cid, "rejected", it["version"], event={"cause": "agent"})  # ci-5 did not land: the orphan reopens
        did = A.orphan_duty(A.CurrentStore(A.process_store), self.r.path)
        self.assertIn("orphan repo-wt-ci-1: escalated UNLANDED ORPHAN", did)
        [c] = A.open_conditions(A.CurrentStore(A.process_store))
        self.assertIn(f"{cid} closed unlanded", c["text"])
        self.assertEqual(self.relands.count(wt), 1, "never re-submitted (ci-5's own tree gets its one submit)")

    def test_an_orphan_whose_last_change_was_cancelled_goes_to_a_person_not_the_queue(self):
        wt, _sha = self.worktree_commit("repo-wt-dropped", "dropped: cancelled on purpose")
        st = A.process_store()
        it = st.put_item("change", "20261030-120000-repo-wt-dropped-1", "queued", {"worktree": wt, "head": _sha})
        st.transition("change", it["id"], "cancelled", it["version"], event={"cause": "agent"})
        self.assertEqual(A.orphan_duty(A.CurrentStore(A.process_store), self.r.path), ["orphan repo-wt-dropped: escalated UNLANDED ORPHAN"])
        self.assertEqual(self.relands, [], "never re-submitted")
        [c] = A.open_conditions(A.CurrentStore(A.process_store))
        self.assertIn("its last change 20261030-120000-repo-wt-dropped-1 was cancelled", c["text"])
        self.assertEqual(A.orphan_duty(A.CurrentStore(A.process_store), self.r.path), [])

    def test_a_superseded_worktree_is_no_orphan_and_closes_itself_when_its_successor_lands(self):
        """2026-10-30: movies-wt-ci-1/2/3 and -review were carried into ci-5 with real conflict resolution (no patch-id,
        no content twin) and escalated UNLANDED ORPHAN until removed by hand. Marked SUPERSEDED BY ci-5: the condition
        clears, nothing is submitted or escalated, and once ci-5 lands the worktree and its branch are closed."""
        wt, sha = self.worktree_commit("repo-wt-ci-1", "ci-1: a fix")
        self.assertEqual(A.orphan_duty(A.CurrentStore(A.process_store), self.r.path), ["orphan repo-wt-ci-1: submit (never landed)"])
        self.assertEqual(A.orphan_duty(A.CurrentStore(A.process_store), self.r.path), ["orphan repo-wt-ci-1: escalated UNLANDED ORPHAN"])
        ci5 = self.r.worktree("repo-wt-ci-5", base="origin/main", branch="ci-5")
        self.r.write("fix.txt", "repo-wt-ci-1, resolved against ci-5\n", at=ci5)  # carried, its lines changed
        self.r.write("ci5.txt", "ci-5\n", at=ci5)
        self.r.commit("ci-5: ci-1's fix and its own", at=ci5)
        rc, out = self.run_tool("supersede", wt, "--by", "repo-wt-ci-5")
        self.assertEqual(rc, 0, out)
        self.assertIn("SUPERSEDED BY repo-wt-ci-5", out)
        self.assertEqual(len(A.recovery_tick(A.CurrentStore(A.process_store), force=True)), 1)  # the probe that raised it clears it
        self.assertEqual(A.open_conditions(A.CurrentStore(A.process_store)), [])
        did = A.orphan_duty(A.CurrentStore(A.process_store), self.r.path)
        self.assertFalse([x for x in did if "repo-wt-ci-1" in x], did)  # no submit, no escalation
        self.assertEqual(self.relands.count(wt), 1)
        rc, out = self.run_tool("supersede")
        self.assertIn("repo-wt-ci-1: SUPERSEDED BY repo-wt-ci-5 (open", out)
        sh(ci5, "git", "push", "-q", "origin", "HEAD:main")  # the successor lands
        sh(self.r.path, "git", "fetch", "-q", "origin")
        did = A.orphan_duty(A.CurrentStore(A.process_store), self.r.path)
        [line] = [x for x in did if "repo-wt-ci-1" in x]
        self.assertIn("superseded repo-wt-ci-1: repo-wt-ci-5 landed", line)
        self.assertIn("and branch repo-wt-ci-1", line)
        self.assertFalse(os.path.exists(wt))
        self.assertEqual(sh(self.r.path, "git", "branch", "--list", "repo-wt-ci-1"), "")
        self.assertIn(f"worktree\trepo\t{os.path.realpath(wt)}\t{sha}\tsuperseded: by repo-wt-ci-5", self.deleted_log())
        self.assertTrue([e for e in self.inbox() if e["verdict"].startswith("CLOSED ") and "repo-wt-ci-1" in e["verdict"]])
        self.assertEqual(A.orphan_duty(A.CurrentStore(A.process_store), self.r.path), [], "closed once")
        rc, out = self.run_tool("supersede", "--all")
        self.assertIn("(closed", out)

    def test_a_mark_by_change_id_follows_resubmissions_and_lapses_when_it_ends_unlanded(self):
        wt, _sha = self.worktree_commit("repo-wt-review", "review: a fix")
        st = A.process_store()
        first, second = "20261030-154005-repo-wt-ci-5-819355", "20261030-175250-repo-wt-ci-5-45426e"
        gone = os.path.join(self.tmp.name, "repo-wt-ci-5")  # the successor's tree is not here (another host's)
        it = st.put_item("change", first, "queued", {"worktree": gone, "head": "a" * 40})
        rc, out = self.run_tool("supersede", wt, "--by", "819355")
        self.assertEqual(rc, 0, out)
        self.assertEqual(A.orphan_duty(A.CurrentStore(A.process_store), self.r.path), [], "carried: no submit")
        st.put_item("change", second, "queued", {"worktree": gone, "head": "b" * 40})
        st.transition("change", first, "superseded", it["version"], data={"superseded_by": second}, event={"cause": "agent"})
        self.assertEqual(A.successor_state(A.superseded_mark(wt), self.r.path, A.CurrentStore(A.process_store))[0], "open", "re-submitted: still on its way")
        it2 = st.item("change", second)
        st.transition("change", second, "rejected", it2["version"], event={"cause": "agent"})
        third = st.put_item("change", "20261030-190000-repo-wt-ci-5-aa11bb", "queued", {"worktree": gone, "head": "c" * 40})
        self.assertEqual(A.successor_state(A.superseded_mark(wt), self.r.path, A.CurrentStore(A.process_store))[0], "open", "landed again from its tree")
        st.transition("change", third["id"], "cancelled", third["version"], event={"cause": "agent"})
        did = A.orphan_duty(A.CurrentStore(A.process_store), self.r.path)
        self.assertIn(f"superseded repo-wt-review: the mark lapsed (its change {second} ended rejected, unlanded); "
                      "the orphan rules apply again", did)
        self.assertIn("orphan repo-wt-review: submit (never landed)", did)
        self.assertIsNone(A.superseded_mark(wt))
        self.assertTrue(os.path.isdir(wt), "a lapsed mark deletes nothing")

    def test_a_successor_that_carries_nothing_yet_is_refused(self):
        wt, _sha = self.worktree_commit("repo-wt-a", "a")
        empty = self.r.worktree("repo-wt-b", base="origin/main", branch="b")
        rc, out = self.run_tool("supersede", wt, "--by", empty)
        self.assertEqual(rc, 2, out)
        self.assertIn("carries nothing that is not on main yet", out)
        rc, out = self.run_tool("supersede", wt, "--by", "no-such-change")
        self.assertEqual(rc, 2, out)
        self.assertEqual(A.superseded_marks(), {})

    # -- PARKED, OWNERSHIP FOLLOWS ACTIVITY, INFRA RETRIES, ONE FIXER (the owner, 2026-11-03: bare-borrow, flip-stage5,
    # order-rows, wfnames and imdbslip escalated UNLANDED ORPHAN over and over)
    def test_a_parked_worktree_is_no_orphan_shows_in_status_and_unpark_brings_it_back(self):
        wt, _sha = self.worktree_commit("repo-wt-bare-borrow", "bare borrow: a design")
        self.assertEqual(A.orphan_duty(A.CurrentStore(A.process_store), self.r.path), ["orphan repo-wt-bare-borrow: submit (never landed)"])
        self.assertEqual(A.orphan_duty(A.CurrentStore(A.process_store), self.r.path), ["orphan repo-wt-bare-borrow: escalated UNLANDED ORPHAN"])
        rc, out = self.run_tool("park", wt, "--owner-decision", "--reason", "owner deciding, see STATE.md")
        self.assertEqual(rc, 0, out)
        self.assertIn("PARKED", out)
        self.assertEqual(len(A.recovery_tick(A.CurrentStore(A.process_store), force=True)), 1)  # the escalation clears: "parked: ..."
        self.assertEqual(A.open_conditions(A.CurrentStore(A.process_store)), [])
        for _ in range(3):
            self.assertEqual([x for x in A.orphan_duty(A.CurrentStore(A.process_store), self.r.path) if "bare-borrow" in x], [])
        self.assertEqual(self.relands, [wt], "never submitted again while parked")
        self.assertEqual(A.parked_lines(), ["parked: 1", "  repo-wt-bare-borrow  until the owner decides: owner "
                                                         "deciding, see STATE.md"])
        self.assertIn("parked: 1", A.render_status(A.status_header(A.epoch_now(), probes=False), A.epoch_now()))
        rc, out = self.run_tool("unpark", wt)
        self.assertIn("UNPARKED", out)
        self.assertEqual(A.orphan_duty(A.CurrentStore(A.process_store), self.r.path), ["orphan repo-wt-bare-borrow: submit (never landed)"])

    def test_parked_until_its_blocker_lands_then_it_is_submitted_again(self):
        wt, _sha = self.worktree_commit("repo-wt-order-rows", "order rows")
        self.assertEqual(A.orphan_duty(A.CurrentStore(A.process_store), self.r.path), ["orphan repo-wt-order-rows: submit (never landed)"])
        self.assertEqual(A.orphan_duty(A.CurrentStore(A.process_store), self.r.path), ["orphan repo-wt-order-rows: escalated UNLANDED ORPHAN"])
        det = self.r.worktree("repo-wt-determinism", base="origin/main", branch="determinism")
        self.r.write("det.txt", "deterministic\n", at=det)
        blocker = self.r.commit("determinism fix", at=det)
        rc, out = self.run_tool("park", wt, "--until-landed", blocker, "--reason", "blocked on the determinism fix")
        self.assertEqual(rc, 0, out)
        A.process_cwds = lambda: {4242: det}  # the blocker's own author is at work
        self.assertEqual([x for x in A.orphan_duty(A.CurrentStore(A.process_store), self.r.path) if "order-rows" in x], [])
        sh(det, "git", "push", "-q", "origin", "HEAD:main")  # the blocker lands
        sh(self.r.path, "git", "fetch", "-q", "origin")
        did = A.orphan_duty(A.CurrentStore(A.process_store), self.r.path)
        self.assertIn(f"parked repo-wt-order-rows: released (its blocker {blocker[:11]} is on main); the orphan rules "
                      "apply again", did)
        self.assertIn("orphan repo-wt-order-rows: submit (never landed)", did, "self-recovering: it gets landed")
        self.assertEqual(self.relands, [wt, wt])
        self.assertIsNone(A.parked_mark(wt))
        self.assertTrue([e for e in self.inbox("PARKED") if e["verdict"].startswith("UNPARKED ")])

    def test_parked_until_a_date_and_the_weekly_digest_of_old_parks(self):
        wt, _sha = self.worktree_commit("repo-wt-flip-stage5", "stage 5")
        later, _s = self.worktree_commit("repo-wt-later", "later")
        rc, out = self.run_tool("park", wt, "--owner-decision", "--reason", "kept for reference")
        self.assertEqual(rc, 0, out)
        rc, out = self.run_tool("park", later, "--until", "2026-01-01", "--reason", "after new year")
        self.assertEqual(rc, 0, out)
        rc, out = self.run_tool("park", later, "--reason", "no condition")
        self.assertEqual(rc, 2, out)
        t = A.epoch_now()
        did = A.park_duty(self.r.path, A.CurrentStore(A.process_store), now_=t)
        self.assertEqual(did, ["parked repo-wt-later: released (its date 2026-01-01 passed); the orphan rules apply again"])
        self.assertEqual(A.park_duty(self.r.path, A.CurrentStore(A.process_store), now_=t + 13 * 86400), [], "younger than 14 days: no digest")
        [line] = A.park_duty(self.r.path, A.CurrentStore(A.process_store), now_=t + 15 * 86400)
        self.assertTrue(line.startswith("PARKED DIGEST: repo-wt-flip-stage5 (15 d, until the owner decides: kept for "
                                        "reference)"), line)
        self.assertEqual(A.park_duty(self.r.path, A.CurrentStore(A.process_store), now_=t + 16 * 86400), [], "one a week")
        self.assertEqual(len(A.park_duty(self.r.path, A.CurrentStore(A.process_store), now_=t + 22.5 * 86400)), 1)
        self.assertEqual(len([e for e in self.inbox("PARKED") if e["verdict"].startswith("PARKED DIGEST")]), 2)
        self.assertNotIn("PARKED", A.WAIT_KINDS_DEFAULT.split(","), "a digest wakes nobody")
        self.assertEqual(A.open_conditions(A.CurrentStore(A.process_store)), [], "never an escalation")

    def session(self, wt, minutes_ago=1, cmd=None):
        d = os.path.join(os.environ["AGENTCTL_CLAUDE_PROJECTS"], "-Users-x-elsewhere")
        os.makedirs(d, exist_ok=True)
        at = A.dt.datetime.fromtimestamp(A.epoch_now() - minutes_ago * 60, A.dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")
        lines = [{"type": "user", "timestamp": at, "message": {"content": "go"}},
                 {"type": "assistant", "timestamp": at, "message": {"content": [
                     {"type": "tool_use", "name": "Bash", "input": {"command": cmd or f"git -C {wt} rebase origin/main"}}]}}]
        p = os.path.join(d, "abcdef123456789.jsonl")
        with open(p, "w") as f:
            f.write("\n".join(json.dumps(x) for x in lines) + "\n")
        os.utime(p, (A.epoch_now() - minutes_ago * 60,) * 2)  # its last write: the session's last sign of life
        return p

    def test_a_live_session_working_in_the_worktree_by_path_owns_it(self):
        """imdbslip: its agent's session ran from another directory, so no process had its cwd there."""
        wt, _sha = self.worktree_commit("repo-wt-imdbslip", "imdb slip")
        self.assertEqual(A.orphan_duty(A.CurrentStore(A.process_store), self.r.path), ["orphan repo-wt-imdbslip: submit (never landed)"])
        self.assertEqual(A.orphan_duty(A.CurrentStore(A.process_store), self.r.path), ["orphan repo-wt-imdbslip: escalated UNLANDED ORPHAN"])
        self.session(wt)
        self.assertEqual(A.session_worktrees([wt]), {os.path.realpath(wt): "session abcdef123456"})
        self.assertEqual(A.session_worktrees([wt + "-2"]), {}, "a sibling's name is no match")
        self.assertEqual(len(A.recovery_tick(A.CurrentStore(A.process_store), force=True)), 1)  # the escalation clears: a live session works in it
        self.assertEqual(A.open_conditions(A.CurrentStore(A.process_store)), [])
        self.assertEqual(A.orphan_candidates(self.r.path, A.CurrentStore(A.process_store)), [])
        self.session(wt, minutes_ago=150)  # the session went quiet: ownership lapses with it
        self.assertEqual(A.session_worktrees([wt]), {})
        self.assertEqual(len(A.orphan_candidates(self.r.path, A.CurrentStore(A.process_store))), 1)

    def test_an_infra_failed_submit_is_retried_when_the_store_answers_and_never_escalated(self):
        """flip-stage5: `crashed: StoreUnavailable` in a tunnel outage was its final verdict, escalated for good."""
        wt, _sha = self.worktree_commit("repo-wt-stage5", "stage 5")
        saved = A.ORPHAN_INFRA_BACKOFF, A.store_answers
        self.addCleanup(lambda: setattr(A, "ORPHAN_INFRA_BACKOFF", saved[0]) or setattr(A, "store_answers", saved[1]))
        A.ORPHAN_INFRA_BACKOFF = (0,)
        self.assertEqual(A.orphan_duty(A.CurrentStore(A.process_store), self.r.path), ["orphan repo-wt-stage5: submit (never landed)"])
        crash = "crashed: StoreUnavailable: POST /v1/store/items: <urlopen error [Errno 61] Connection refused>"
        A.inbox_write("land", crash, [], [], 1, cwd=wt, reply=True)
        A.store_answers = lambda store: False
        did = A.orphan_duty(A.CurrentStore(A.process_store), self.r.path)
        self.assertEqual(did, [f"orphan repo-wt-stage5: its last try failed on infra ({crash[:120]}); retried when the "
                               "store answers"])
        self.assertEqual(A.orphan_duty(A.CurrentStore(A.process_store), self.r.path), [], "said once")
        self.assertEqual(A.open_conditions(A.CurrentStore(A.process_store)), [])
        A.store_answers = lambda store: True  # the store is back
        self.assertEqual(A.orphan_duty(A.CurrentStore(A.process_store), self.r.path), [f"orphan repo-wt-stage5: submit again (its last try failed on "
                                                      f"infra: {crash[:120]})"])
        self.assertEqual(self.relands, [wt, wt])
        self.assertEqual(A.open_conditions(A.CurrentStore(A.process_store)), [], "never a final 'crashed' orphan")
        self.assertTrue(A.orphan_infra("NOT LANDED: its fetch of main failed"))
        self.assertFalse(A.orphan_infra("NOT LANDED: the pre-push checks failed (on ci-runner)"))

    def test_a_pre_push_rejection_gets_one_fixer_then_a_person_and_none_when_parked(self):
        wt, _sha = self.worktree_commit("repo-wt-wfnames", "workflow names")
        parked, _s = self.worktree_commit("repo-wt-parked", "parked")
        self.run_tool("park", parked, "--owner-decision", "--reason", "deciding")
        fixers = []
        saved = A.orphan_dispatch
        self.addCleanup(lambda: setattr(A, "orphan_dispatch", saved))
        A.orphan_dispatch = lambda repo, w, name, verdict: fixers.append((w, verdict)) or (True, "fixer pid 1")
        self.assertEqual(A.orphan_duty(A.CurrentStore(A.process_store), self.r.path), ["orphan repo-wt-wfnames: submit (never landed)"])
        rej = "NOT LANDED: the pre-push checks failed (on ci-runner); the change abc stays in the worktree"
        A.inbox_write("land", rej, [], [], 1, cwd=wt)
        A.inbox_write("land", rej, [], [], 1, cwd=parked)
        did = A.orphan_duty(A.CurrentStore(A.process_store), self.r.path)
        self.assertEqual(did, [f"orphan repo-wt-wfnames: fixer dispatched ({rej[:120]}): fixer pid 1"])
        self.assertEqual(fixers, [(wt, rej)])
        self.assertEqual(A.open_conditions(A.CurrentStore(A.process_store)), [], "a fixer, not an escalation")
        self.r.write("fix2.txt", "the fixer's try\n", at=wt)  # the fixer rebased and committed: a new change key
        self.r.commit("wfnames: the fixer's try", at=wt)
        self.assertEqual(A.orphan_duty(A.CurrentStore(A.process_store), self.r.path), ["orphan repo-wt-wfnames: submit (NOT LANDED: the pre-push checks "
                                                      "failed (on ci-runner); the change abc stays in the worktree)"])
        A.inbox_write("land", rej, [], [], 1, cwd=wt)
        self.assertEqual(A.orphan_duty(A.CurrentStore(A.process_store), self.r.path), ["orphan repo-wt-wfnames: escalated UNLANDED ORPHAN"])
        self.assertEqual(len(fixers), 1, "one fixer per worktree, whatever its rebases do")
        self.assertEqual(A.orphan_duty(A.CurrentStore(A.process_store), self.r.path), [])

    def intend(self, wt):
        """The author ran `agentctl land` there once (its land intent): only such a worktree is ever submitted."""
        A.inbox_write("land", "QUEUED 20261106-000000-x-1 (abc, 1 ahead)", [], [], 0, final=False, cwd=wt)

    def subagent_transcript(self, lines, mtime=None):
        d = os.path.join(os.environ["AGENTCTL_CLAUDE_PROJECTS"], "-Users-x-movies", "d296c9e6-b2a3", "subagents")
        os.makedirs(d, exist_ok=True)
        p = os.path.join(d, "agent-aec18896.jsonl")
        with open(p, "w") as f:
            f.write("\n".join(json.dumps(x) for x in lines) + "\n")
        if mtime is not None:
            os.utime(p, (mtime, mtime))
        return p

    def tool_call(self, minutes_ago, command):
        at = A.dt.datetime.fromtimestamp(A.epoch_now() - minutes_ago * 60, A.dt.timezone.utc).strftime(
            "%Y-%m-%dT%H:%M:%S.000Z")
        return {"type": "assistant", "timestamp": at, "message": {"content": [
            {"type": "tool_use", "name": "Bash", "input": {"command": command}}]}}

    def test_the_es_gate_check_incident_a_live_sessions_check_worktree_is_never_submitted(self):
        """2026-11-06 22:51Z: orphan duty rebased ~/projects/movies-wt/es-gate-check (branch check/es-gate-reenable, a
        gate re-enable the owner holds) and queued it. Its session was alive -- its transcript written minutes before --
        but its last tool call naming the worktree was 104 min old, past the 30 min the ownership rule looked back."""
        wt = self.r.worktree("es-gate-check", branch="check/es-gate-reenable")
        self.r.write("gate.txt", "on\n", at=wt)
        self.r.commit("Gate re-enable", at=wt)
        self.subagent_transcript([self.tool_call(104, f"cd {wt} && git cherry-pick debfb2915e"),
                                  self.tool_call(5, "cd /elsewhere/es-absence && agentctl land")])
        self.assertEqual(A.session_worktrees([wt]), {os.path.realpath(wt): "session agent-aec188"})
        self.assertEqual(A.orphan_duty(A.CurrentStore(A.process_store), self.r.path), [], "a live session's worktree is nobody's orphan")
        self.subagent_transcript([self.tool_call(300, f"cd {wt} && git cherry-pick debfb2915e")],
                                 mtime=A.epoch_now() - 4 * 3600)  # the session ended hours ago
        self.assertEqual(A.session_worktrees([wt]), {})
        self.intend(wt)  # even had it run land once: a check/* branch is not for landing
        self.assertEqual(A.orphan_duty(A.CurrentStore(A.process_store), self.r.path), ["orphan es-gate-check: not submitted (branch "
                                                      "check/es-gate-reenable is not for landing)"])
        self.assertEqual(A.orphan_duty(A.CurrentStore(A.process_store), self.r.path), [], "said once")
        self.assertEqual(self.relands, [])
        self.assertEqual(A.open_conditions(A.CurrentStore(A.process_store)), [])

    def test_branches_named_not_for_landing(self):
        for b in ("check/x", "measure/x", "experiment/x", "probe/x", "wip-x", "wip/x", "prototype/x", "spike/x"):
            self.assertIsNotNone(A.orphan_branch_refusal(b), b)
        for b in ("es-absence", "fix-check-order", "imdb-wipe", "", "main-probe"):
            self.assertIsNone(A.orphan_branch_refusal(b), b)

    def test_a_tool_call_by_home_relative_path_owns_the_worktree(self):
        home = os.path.expanduser("~")
        self.assertEqual(A.tool_call_paths("cd ~/projects/movies-wt/es-gate-check && ls; cat $HOME/x/y \"/a/b c\""),
                         {f"{home}/projects/movies-wt/es-gate-check", f"{home}/x/y", "/a/b"})

    def test_a_worktree_that_never_ran_land_goes_to_a_person_not_the_queue(self):
        wt, _sha = self.worktree_commit("repo-wt-halfdone", "halfdone: its agent died before landing", intent=False)
        did = A.orphan_duty(A.CurrentStore(A.process_store), self.r.path)
        self.assertEqual(did, ["orphan repo-wt-halfdone: escalated UNLANDED ORPHAN"])
        self.assertEqual(self.relands, [], "never submitted: its author never asked to land it")
        [c] = A.open_conditions(A.CurrentStore(A.process_store))
        self.assertIn("never ran `agentctl land`", c["text"])
        self.assertEqual(A.orphan_duty(A.CurrentStore(A.process_store), self.r.path), [])

    def test_a_dry_run_lists_each_orphan_and_neither_submits_nor_records_it(self):
        self.worktree_commit("repo-wt-dry", "dry: nobody landed it")
        did = A.orphan_duty(A.CurrentStore(A.process_store), self.r.path, dry_run=True)
        self.assertEqual(did, ["orphan repo-wt-dry: 1 commit(s); last verdict: never landed"])
        self.assertEqual(self.relands, [])
        self.assertFalse(os.path.exists(A.recovery_dir("orphans.json")))
        self.assertEqual(A.orphan_duty(A.CurrentStore(A.process_store), self.r.path), ["orphan repo-wt-dry: submit (never landed)"])

    def test_an_infra_failed_submit_waits_out_its_backoff_before_it_is_tried_again(self):
        wt, _sha = self.worktree_commit("repo-wt-backoff", "backoff")
        self.assertEqual(A.orphan_duty(A.CurrentStore(A.process_store), self.r.path), ["orphan repo-wt-backoff: submit (never landed)"])
        A.inbox_write("land", "crashed: StoreUnavailable: POST /v1/store/items: timed out", [], [], 1, cwd=wt, reply=True)
        self.assertEqual(A.orphan_duty(A.CurrentStore(A.process_store), self.r.path), [], "inside ORPHAN_INFRA_BACKOFF's first wait")
        self.assertEqual(self.relands, [wt])
        self.assertEqual(A.open_conditions(A.CurrentStore(A.process_store)), [])

    def test_a_submit_the_reland_itself_refused_is_its_verdict(self):
        wt, _sha = self.worktree_commit("repo-wt-refused", "refused")
        A.reland_worktree = lambda c, a, st: self.relands.append(a["worktree"]) or "not landed again: its tree is gone"
        self.assertEqual(A.orphan_duty(A.CurrentStore(A.process_store), self.r.path), ["orphan repo-wt-refused: submit (never landed)"])
        self.assertEqual(self.inbox("ORPHAN_SUBMITTED"), [], "a refused submit queued nothing: no alert")
        self.assertEqual(A.orphan_duty(A.CurrentStore(A.process_store), self.r.path), ["orphan repo-wt-refused: escalated UNLANDED ORPHAN"])
        [c] = A.open_conditions(A.CurrentStore(A.process_store))
        self.assertIn("not landed again: its tree is gone", c["text"])

    def test_a_real_failure_waits_for_a_free_fixer_slot(self):
        wt, _sha = self.worktree_commit("repo-wt-slot", "slot")
        os.environ["AGENTCTL_ORPHAN_FIXERS"] = "0"
        self.addCleanup(os.environ.pop, "AGENTCTL_ORPHAN_FIXERS", None)
        self.assertEqual(A.orphan_duty(A.CurrentStore(A.process_store), self.r.path), ["orphan repo-wt-slot: submit (never landed)"])
        rej = "NOT LANDED: the pre-push checks failed (on ci-runner)"
        A.inbox_write("land", rej, [], [], 1, cwd=wt)
        self.assertEqual(A.orphan_duty(A.CurrentStore(A.process_store), self.r.path),
                         [f"orphan repo-wt-slot: a fixer waits for a free slot ({rej[:80]})"])
        self.assertEqual(A.open_conditions(A.CurrentStore(A.process_store)), [], "waiting for a fixer, not escalated")

    def test_a_held_commit_is_never_submitted(self):
        wt = self.r.worktree("repo-wt-held", branch="held")
        self.r.write("gate.txt", "on\n", at=wt)
        sha = self.r.commit("Gate re-enable\n\nHold: the owner says go after the drift check", at=wt)
        self.intend(wt)
        self.assertEqual(A.orphan_duty(A.CurrentStore(A.process_store), self.r.path), [f"orphan repo-wt-held: not submitted (commit {sha[:10]} carries "
                                                      "Hold: the owner says go after the drift check)"])
        self.assertEqual(self.relands, [])
        self.assertEqual(A.orphan_duty(A.CurrentStore(A.process_store), self.r.path), [])

    def test_a_submit_alerts_the_owner_naming_the_worktree_and_its_commits(self):
        wt, sha = self.worktree_commit("repo-wt-done", "done: finished, landed once, its agent gone")
        self.assertEqual(A.orphan_duty(A.CurrentStore(A.process_store), self.r.path), ["orphan repo-wt-done: submit (never landed)"])
        [e] = self.inbox("ORPHAN_SUBMITTED")
        self.assertIn("repo-wt-done", e["verdict"])
        self.assertIn(f"{sha[:10]} done: finished", e["verdict"])
        self.assertEqual(A.alert_audience(e), "owner")

    def test_a_reland_runs_in_a_throwaway_tree_never_rebasing_the_authors(self):
        wt, sha = self.worktree_commit("repo-wt-author", "author: a change")
        self.r.write("other.txt", "main moves\n")
        self.r.commit("main moves")
        self.r.push()
        A.reland_worktree = self.saved_o[1]
        spawned = []
        saved = A.spawn_detached
        self.addCleanup(lambda: setattr(A, "spawn_detached", saved))
        A.spawn_detached = lambda argv, **kw: spawned.append((argv, kw["cwd"]))
        msg = A.reland_worktree(None, {"worktree": wt, "attempt": 1, "repo": self.r.path}, A.CurrentStore(A.process_store))
        tree = A.reland_tree_path(wt)
        self.assertIn("throwaway tree", msg)
        self.assertEqual([c for _a, c in spawned], [tree])
        self.assertEqual(sh(tree, "git", "rev-parse", "HEAD"), sha)
        self.assertTrue(A.internal_tree(tree))
        self.assertEqual(sh(wt, "git", "rev-parse", "HEAD"), sha, "the author's worktree is never rebased")
        self.assertEqual(sh(wt, "git", "symbolic-ref", "--short", "HEAD"), "repo-wt-author")
        self.assertEqual(os.path.basename(tree), "repo-wt-author", "the change id names the author's worktree")
        self.r.write("fix.txt", "a second try\n", at=wt)
        sha2 = self.r.commit("author: another", at=wt)
        A.reland_worktree(None, {"worktree": wt, "attempt": 2, "repo": self.r.path}, A.CurrentStore(A.process_store))
        self.assertEqual(sh(tree, "git", "rev-parse", "HEAD"), sha2, "the old tree replaced")

    def test_the_change_key_survives_a_rebase(self):
        wt, _sha = self.worktree_commit("repo-wt-x", "x")
        _c, k1 = A.orphan_key(self.r.path, wt)
        self.r.write("other.txt", "main moves\n")
        self.r.commit("main moves")
        self.r.push()
        sh(self.r.path, "git", "fetch", "-q", "origin")
        sh(wt, "git", "rebase", "-q", "origin/main")
        _c, k2 = A.orphan_key(self.r.path, wt)
        self.assertEqual(k1, k2)


if __name__ == "__main__":
    unittest.main()
