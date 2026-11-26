"""Reds as items held by leases (design §5.3, §6, §7.2; reconciler phase 1 step 10). The owner's rules for reds, each
made structural and tested here:
  - every red always has a live lease holder or an open offer: NOBODY is impossible;
  - a code red is never blocked on the owner;
  - one shared external cause is one item holding its attached reds (they do not escalate one by one);
  - a red is identified by its failing test or step, not its job name;
  - a refuted fix drops its lease;
  - escalation counts one per open cause;
  - every state self-clears through the same observation that opened it.
Plus the audit items: #7 (every workflow's newest decided run), #8 (stuck undecided runs are reds), #15 (the author
from the change item, never an inherited environment)."""
import json, os, signal, subprocess, sys, time, types, unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from test_agentctl import A  # noqa: E402
from fake_clock import FakeClock, RealTimeClock  # noqa: E402
from agentctl.clock import Clock  # noqa: E402
from test_red import RedBase, run, FAIL_LINE, MOVIES  # noqa: E402

OTHER_LINE = "[info] - BarSpec should baz *** FAILED ***"


class DroppingStore:
    """A store as RemoteStore meets ci-runner's through the tunnel: the n-th calls of a method named (1-based, per
    method) raise StoreUnavailable, a reset connection; every other call answers. `calls`: (method, answer) in order,
    a renew's own answer, 'dropped' for a dropped one."""

    def __init__(self, st, **drops):
        self.st, self.drops, self.counts, self.calls = st, drops, {}, []

    def __getattr__(self, m):
        f = getattr(self.st, m)

        def call(*a, **k):
            self.counts[m] = self.counts.get(m, 0) + 1
            if self.counts[m] in self.drops.get(m, ()):
                self.calls.append((m, "dropped"))
                raise A.StoreUnavailable(f"POST /v1/store/{m}: [Errno 54] Connection reset by peer")
            r = f(*a, **k)
            self.calls.append((m, r if m == "renew" else "ok"))
            return r
        return call


class TickClock(Clock):
    """The time of the test's own ticks (RedsBase.t): what a command run in-process stamps its leases with. The default
    test clock starts AGENTCTL_TEST_CLOCK_LAG s behind the wall, so a 10-minute block lease it stamped read as long
    expired to a tick at the wall's time from a lag of 600 s (OwnerTest, 2026-11-25)."""

    def __init__(self, t):
        self.t = t

    def time(self):
        return self.t[0]

    def monotonic(self):
        return self.t[0]


class RedsBase(RedBase):
    def setUp(self):
        super().setUp()
        self.saved_root = os.environ.get("AGENTCTL_STATE_ROOT")
        os.environ["AGENTCTL_STATE_ROOT"] = os.path.join(self.tmp.name, "state")
        os.environ["AGENTCTL_RECOVERY_DIR"] = os.path.join(self.tmp.name, "recovery")
        os.environ["AGENTCTL_RUNNERS_PAGE_CACHE"] = os.path.join(self.tmp.name, "runners-page.json")
        A.wire_clock(RealTimeClock())  # these tests make real git commits and run a real fixer: the wall's stamps
        self.t = [A.epoch_now()]
        self.st = A.process_store(clock=lambda: self.t[0])
        A._blocker_cache.clear()

    def tearDown(self):
        if self.saved_root is None:
            os.environ.pop("AGENTCTL_STATE_ROOT", None)
        else:
            os.environ["AGENTCTL_STATE_ROOT"] = self.saved_root
        os.environ.pop("AGENTCTL_RECOVERY_DIR", None)
        os.environ.pop("AGENTCTL_RUNNERS_PAGE_CACHE", None)
        super().tearDown()

    def snap(self):
        A._gate_lists.clear()
        A._outside_lists.clear()
        return A.ci_state_refresh(MOVIES)  # written: a workflow_green probe reads it

    def tool(self, *argv, cwd=None, clock=None):
        return super().tool(*argv, cwd=cwd, clock=clock or TickClock(self.t))

    def tick(self, dispatch=False, advance=0, snap=True):
        self.t[0] += advance
        return A.red_tick(self.st, MOVIES, self.r.path, self.snap() if snap else None, now_=self.t[0], dispatch=dispatch,
                          sync=True)  # this process plays both hosts: the server's tick and the sync's claims

    def reds(self):
        return {it["data"]["key"]: it for it in self.st.items("red")}

    def only(self):
        its = [it for it in self.st.items("red") if it["state"] not in A.RED_TERMINAL]
        self.assertEqual(len(its), 1, [(i["id"], i["state"]) for i in its])
        return its[0]

    def unheld(self):
        return A.red_unheld(self.st, self.t[0])

    def green(self, rid, num):
        self.put("runs.json", [run(rid, num, "Main", "main.yml", self.sha)] + json.load(open(os.path.join(self.d, "runs.json"))))


class IdentityTest(RedsBase):
    def test_jobs_failing_at_the_same_step_with_no_error_line_are_one_red(self):
        self.put("run-40.json", {"jobs": [{"name": f"leg ({c})", "status": "completed", "conclusion": "failure",
                                           "steps": [{"name": "Set up job", "conclusion": "failure"}]} for c in ("uk", "de")],
                                 "headSha": self.sha, "url": "u", "number": 20, "status": "completed",
                                 "conclusion": "failure", "workflowName": "Main"})
        self.put("run-40.log", "")
        self.tick()
        self.assertEqual(sorted(self.only()["data"]["jobs"]), ["leg (de)", "leg (uk)"])


    def test_a_red_is_its_failing_test_not_its_job_name(self):
        # two matrix jobs failing with the SAME test: one red; the same job failing with another test: another red
        self.put("run-40.json", {"jobs": [{"name": "test (uk)", "status": "completed", "conclusion": "failure",
                                           "steps": [{"name": "Run tests", "conclusion": "failure"}]},
                                          {"name": "test (de)", "status": "completed", "conclusion": "failure",
                                           "steps": [{"name": "Run tests", "conclusion": "failure"}]}],
                                 "headSha": self.sha, "url": "u", "number": 20, "status": "completed",
                                 "conclusion": "failure", "workflowName": "Main"})
        self.put("run-40.log", f"test (uk)\tRun tests\t2026-10-27T08:00:00Z {FAIL_LINE} (took 9 ms)\n"
                               f"test (de)\tRun tests\t2026-10-27T08:00:00Z {FAIL_LINE} (took 13 ms)\n")
        self.tick()
        it = self.only()
        self.assertEqual(sorted(it["data"]["jobs"]), ["test (de)", "test (uk)"])
        self.assertEqual(A.red_key("Main", "test (uk)", FAIL_LINE), A.red_key("Main", "test (de)", FAIL_LINE + ""))
        self.assertNotEqual(A.red_key("Main", "test (uk)", FAIL_LINE), A.red_key("Main", "test (uk)", OTHER_LINE))

    def test_a_newer_run_failing_the_same_way_attaches_to_the_same_red(self):
        self.main_red(40, 20, line=FAIL_LINE + " (took 9 ms, seed 0xdeadbeef01)")
        self.tick()
        first = self.only()
        self.main_red(44, 22, line=FAIL_LINE + " (took 812 ms, seed 0x1a2b3c4d5e)",
                      older_runs=[run(40, 20, "Main", "main.yml", self.sha, conclusion="failure")])
        self.tick()
        it = self.only()
        self.assertEqual(it["id"], first["id"])
        self.assertEqual(it["data"]["last_run"]["number"], 22)
        self.assertTrue({"20", "22"} <= set(it["data"]["runs"]))


class NeverNobodyTest(RedsBase):
    def test_every_red_is_offered_at_once_and_an_untaken_offer_escalates_every_cycle_naming_why(self):
        os.makedirs(A.reds_dir(), exist_ok=True)
        open(A.reds_dir("DISPATCH_OFF"), "w").close()
        self.tick(dispatch=True)
        it = self.only()
        self.assertEqual(it["state"], "offered")
        self.assertEqual(self.unheld(), [])  # held or offered: never NOBODY
        off = self.st.offer_of(f"red:{it['id']}")
        self.assertIn("dispatch is OFF", off["reason"])
        self.assertEqual(it["deadline"] - it["entered_at"], A.RED_OFFER["gate"])
        for cycle in (1, 2):
            self.tick(dispatch=True, advance=A.RED_OFFER["gate"] + 1)
            it = self.only()
            self.assertEqual((it["state"], it["level"]), ("offered", cycle))
            self.assertEqual(self.unheld(), [])
        esc = [e for e in self.st.events() if e["severity"] == "escalate"]
        self.assertEqual(len(esc), 2)
        self.assertIn("dispatch is OFF", esc[-1]["text"])
        A.project_events(self.st)
        self.assertTrue(any("RED UNCLAIMED" in str(e.get("verdict")) for e in self.inbox()))
        self.assertNotIn("NOBODY", self.why()[1])

    def test_at_capacity_an_outside_red_waits_saying_so(self):
        offers = self.st.offers  # an outside red (a gate red never waits for a slot: the next test)
        self.st.offers = lambda kind=None: [dict(o, picture=dict(o["picture"], gate=False)) for o in offers(kind)]
        os.environ["AGENTCTL_RED_FIXERS"] = "0"
        try:
            self.tick(dispatch=True)
        finally:
            os.environ.pop("AGENTCTL_RED_FIXERS")
        it = self.only()
        self.assertEqual(it["state"], "offered")
        self.assertIn("capacity 0/0", self.st.offer_of(f"red:{it['id']}")["reason"])
        self.assertEqual(self.unheld(), [])
        rc, out = self.why()
        self.assertIn("fixing: offered", out)

    def test_at_capacity_a_gate_red_gets_its_fixer_anyway(self):
        """Nothing stops a gate fix, ever (the owner, 2026-10-31): the cap holds no gate red's fixer."""
        os.environ["AGENTCTL_RED_FIXERS"] = "0"
        try:
            self.tick(dispatch=True)
        finally:
            os.environ.pop("AGENTCTL_RED_FIXERS")
        it = self.only()
        self.assertIsNone(self.st.offer_of(f"red:{it['id']}"), "taken: no offer waits")
        self.assertEqual(A.lease_meta(self.st.get(A.red_lease(it["data"]["key"])))["kind"], "fixer")

    def test_the_tick_repairs_a_missing_offer(self):
        self.tick()
        it = self.only()
        self.st.close_offer(f"red:{it['id']}", "lost by a bug")
        self.assertEqual(len(self.unheld()), 1)  # W2 would see it
        self.tick()
        self.assertEqual(self.unheld(), [])


class ClaimTest(RedsBase):
    def test_a_claim_names_a_worktree_never_the_root_checkout_and_lapses_without_activity(self):
        self.tick()
        rc, out = self.tool("red", "claim", "20", "--repo", "movies", cwd=self.r.path)
        self.assertEqual(rc, 2, out)  # the root checkout counts for nobody (audit #3)
        wt = self.r.worktree("repo-wt-fixer", branch="fixer")
        os.environ["AGENTCTL_AGENT_NAME"] = "fixer-agent"
        try:
            rc, out = self.tool("red", "claim", "20", "--note", "FooSpec's clock", "--repo", "movies", cwd=wt,
                                clock=FakeClock(self.t[0]))  # the lease is stamped on the ticks' clock
        finally:
            os.environ.pop("AGENTCTL_AGENT_NAME")
        self.assertEqual(rc, 0, out)
        it = self.only()
        row = self.st.get(A.red_lease(it["data"]["key"]))
        self.assertTrue(row["holder"].startswith("laptop:claim:fixer-agent@"))
        self.t[0] = A.epoch_now()
        self.tick()
        self.assertEqual(self.only()["state"], "leased")
        self.assertIn("fixer-agent", self.why()[1])
        # activity in its worktree renews it; none lets it lapse at its TTL and the red is offered again
        self.r.write("x.txt", "work\n", at=wt)
        self.tick(advance=A.RED_CLAIM_TTL - 60)
        self.assertEqual(self.only()["state"], "leased")
        self.tick(advance=A.RED_CLAIM_TTL + 1)
        it = self.only()
        self.assertEqual(it["state"], "offered")
        self.assertEqual(it["data"]["attempts"], 1)
        self.assertEqual(self.unheld(), [])

    def test_release_drops_the_lease_and_the_red_is_offered_again_not_counted(self):
        self.tick()
        wt = self.r.worktree("repo-wt-f2", branch="f2")
        os.environ["AGENTCTL_AGENT_NAME"] = "agent-2"
        try:
            self.assertEqual(self.tool("red", "claim", "20", "--repo", "movies", cwd=wt)[0], 0)
            self.tick()
            rc, out = self.tool("red", "release", "20", "--repo", "movies", cwd=wt,
                                clock=FakeClock(self.t[0]))  # the lease is stamped on the ticks' clock
        finally:
            os.environ.pop("AGENTCTL_AGENT_NAME")
        self.assertEqual(rc, 0, out)
        self.tick()
        it = self.only()
        self.assertEqual((it["state"], it["data"]["attempts"]), ("offered", 0))



class FixTest(RedsBase):
    def fix_commit(self, num, name="repo-wt-fix"):
        wt = self.r.worktree(name, branch=name)
        self.r.write("fix.txt", f"{name}\n", at=wt)
        sha = self.r.commit(f"fix FooSpec\n\nFixes-Red: {num}", at=wt)
        self.r.push(at=wt, ref=f"{sha}:main")
        return wt, sha

    def test_a_fix_naming_its_red_by_the_api_run_id_links_it(self):
        # issue 20261109T064005-9bc34e: `Fixes-Red: <API run id>` (red #20 here is run id 40) links like a run number
        self.tick()
        _wt, sha = self.fix_commit(40)
        self.tick()
        it = self.only()
        self.assertEqual((it["state"], (it["data"].get("fix") or {}).get("sha")), ("fix-pending", sha))

    def test_a_landed_fix_is_verified_by_the_next_run_containing_it(self):
        self.tick()
        wt, sha = self.fix_commit(20)
        self.tick()
        it = self.only()
        self.assertEqual((it["state"], it["data"]["fix"]["sha"]), ("fix-pending", sha))
        self.assertEqual(self.unheld(), [])
        self.put("runs.json", [run(50, 23, "Main", "main.yml", sha)] + json.load(open(os.path.join(self.d, "runs.json"))))
        self.tick()
        self.assertEqual([i["state"] for i in self.st.items("red")], ["verified"])
        self.assertTrue(any(str(e.get("verdict")).startswith("FIX OK") for e in self.inbox()))

    def test_a_fix_pushed_to_main_from_another_clone_links_its_red(self):
        # 2026-11-12, Record scrape fixtures #181 (red 58f3db9e8a3d): its fix 548dfb3c76 (`Fixes-Red: 181`) was pushed
        # from the laptop. The reconciler's own clone fetched main only to check or push a change, so the red facts never
        # saw the commit: the red was re-offered after every LANDED, then put on the owner as WAITING ON YOU. A main the
        # snapshot shows past the clone is fetched before the fixes are read.
        self.tick()
        other = os.path.join(self.tmp.name, "laptop")
        subprocess.run(["git", "clone", "-q", self.r.bare, other], check=True)
        self.r.write("fix.txt", "from the laptop\n", at=other)
        sha = self.r.commit("fix FooSpec\n\nFixes-Red: 20", at=other)
        subprocess.run(["git", "-C", other, "push", "-q", "origin", "HEAD:main"], check=True)  # never the clone's fetch
        self.put("runs.json", [run(50, 23, "Main", "main.yml", sha, status="in_progress", created=A.iso_at(self.t[0]))]
                 + json.load(open(os.path.join(self.d, "runs.json"))))
        self.tick()
        it = self.only()
        self.assertEqual((it["state"], (it["data"].get("fix") or {}).get("sha")), ("fix-pending", sha))

    def test_a_fix_linked_to_a_red_waiting_on_the_owner_lifts_the_block(self):
        # 58f3db9e8a3d: put on the owner ("WAITING ON YOU: hold the red as fixed by 548dfb3c76 ...") only because agentctl
        # had not linked the fix. A linked fix is the red's answer: it waits for the run that proves it, the owner's
        # question withdrawn and its block released -- never a block a later re-offer falls back into.
        self.tick()
        it = self.only()
        A.red_decision_block(self.st, it, {}, "hold the red as fixed by its landed fix; rule: x", self.t[0],
                             category="policy-no-rule")
        it = self.only()
        self.assertEqual(it["state"], "blocked-external")
        _wt, sha = self.fix_commit(20)
        self.tick(advance=60)
        it = self.only()
        self.assertEqual((it["state"], it["data"]["fix"]["sha"]), ("fix-pending", sha))
        self.assertEqual((it["data"].get("cause"), it["data"].get("block"), it["data"].get("ask")), (None, None, None))
        self.assertIsNone(A.red_block_of(it, self.st))
        self.assertEqual(A.red_owner_reminders(self.st, self.t[0] + 86400), [])

    def test_a_red_named_by_its_runs_run_name_is_verified_by_its_workflows_newer_runs(self):
        # 2026-11-12, b059f05c70fa: opened as "Identity takes (convergence run 43013487979)" #40 -- a run-name read as its
        # workflow's name -- no newer run was ever "of" that workflow: its landed fix waited 6 h unverified, twice, while
        # Identity takes #56 on main, containing the fix, was green. It takes its workflow's name from its run's file.
        self.r.write(".github/workflows/identity-takes.yml", "name: Identity takes\nrun-name: Identity takes (x)\n"
                     "on:\n  workflow_dispatch:\n")
        self.r.commit("the workflow")
        self.r.push()
        _wt, sha = self.fix_commit(40)
        takes = lambda rid, num, conclusion, head: dict(run(rid, num, "Identity takes", "identity-takes.yml", head,
                                                            conclusion=conclusion),
                                                        runName=f"Identity takes (convergence run {rid + 1000})")
        self.put("runs.json", [takes(61, 56, "success", sha), run(41, 21, "Main", "main.yml", sha),
                               run(39, 19, "Identity model convergence", "identity-model-convergence.yml", sha)])
        self.put("api-repos_pawelkrupinski_movies_actions_runs_60.json",
                 {"id": 60, "run_number": 40, "path": ".github/workflows/identity-takes.yml", "status": "completed",
                  "conclusion": "failure", "head_sha": self.sha, "head_branch": "main"})
        it = self.st.put_item("red", "k1", "fix-pending", {
            "key": "k1", "workflow": "Identity takes (convergence run 1060)", "jobs": ["leg (us) / takes-drift-1 (us)"],
            "kind": "unclear", "red_kind": "outside", "gate": False, "runs": ["40", "60"],
            "last_run": {"number": 40, "databaseId": 60, "headSha": self.sha},
            "opened_at": self.t[0], "fix": {"entry": None, "status": "landed", "sha": sha, "at": self.t[0]}})
        self.tick(advance=60)
        it = self.st.item("red", "k1")
        self.assertEqual((it["state"], it["data"]["workflow"]), ("verified", "Identity takes"), it["data"].get("history"))

    def test_a_landed_fix_no_run_can_ever_check_is_offered_at_once_not_after_hours(self):
        # its workflow has no file on main (b059f05c70fa's "no workflow file named 'Identity takes (convergence run
        # 43013487979)'"): no run will ever prove the fix, so it is not a fix on its way -- a fixer gets the red now.
        _wt, sha = self.fix_commit(70)
        self.st.put_item("red", "k2", "fix-pending", {
            "key": "k2", "workflow": "Gone workflow", "jobs": ["x"], "kind": "test", "red_kind": "outside", "gate": False,
            "runs": ["70", "77"], "last_run": {"number": 70, "databaseId": 77, "headSha": self.sha},
            "opened_at": self.t[0], "fix": {"entry": None, "status": "landed", "sha": sha,
                                            "at": self.t[0] - A.RED_FIX_RUN_START - 1}})
        self.tick(advance=1)
        it = self.st.item("red", "k2")
        self.assertEqual((it["state"], it["data"].get("unverified")), ("offered", [sha]))
        self.assertIn("no workflow file", it["data"]["history"][-1]["outcome"])

    def test_a_set_aside_fixs_rebased_copy_never_takes_the_red_back(self):
        # b059f05c70fa: its fix 19482f5c08 set aside unverified at 00:30, the red went back to fix-pending 23 s later on
        # b6d3d8235 -- the same patch before its rebase, the landed tip of a change item -- for another 6 h, no fixer.
        self.tick()
        wt, sha = self.fix_commit(20)
        self.tick()
        self.assertEqual(self.only()["state"], "fix-pending")
        self.tick(advance=A.RED_FIX_GIVEUP)
        self.assertEqual((self.only()["state"], self.only()["data"]["unverified"]), ("offered", [sha]))
        cw = self.r.worktree("repo-wt-copy", base=f"{sha}~1", branch="copy")
        self.r.write("other.txt", "moves the base\n", at=cw)
        self.r.commit("an unrelated commit under the copy", at=cw)
        subprocess.run(["git", "-C", cw, "cherry-pick", sha], check=True, capture_output=True)
        copy = subprocess.run(["git", "-C", cw, "rev-parse", "HEAD"], capture_output=True, text=True).stdout.strip()
        self.assertNotEqual(copy, sha)
        d = A.waiting_state(self.r.path)[0]
        e, _ = A.queue_change(A.CurrentStore(A.process_store), d, {"worktree": cw, "branch": "copy", "head": copy, "commits": [copy]})
        e["status"] = "waiting"
        A.save_entry(d, e)
        ch = self.st.put_item("change", e["id"], "queued", {"worktree": cw, "head": copy, "submitted_at": self.t[0]})
        self.st.transition("change", e["id"], "landed", ch["version"], data={"tip": copy})
        self.tick(advance=60)
        self.assertEqual(self.only()["state"], "offered")

    def test_a_queued_fix_holds_its_red_until_it_lands_and_verification_runs_from_the_landing(self):
        # 2026-10-29: #2493's red showed "offered" (a fixer re-dispatched "to check why the 2406 fix did not clear it")
        # while its fix 16a683a55 sat queued behind the red gate since 00:21Z, untested; the import had lost run 2406
        # from the red's names. The link comes from the change's Fixes-Red trailer and any spell of the red's key.
        self.tick()
        it = self.only()
        key = it["data"]["key"]
        self.st.put_item("red", f"{key}.0", "cleared", {"key": key, "runs": ["19"]})  # an earlier spell saw run 19
        wt = self.r.worktree("repo-wt-queued", branch="repo-wt-queued")
        self.r.write("fix.txt", "queued\n", at=wt)
        sha = self.r.commit("fix FooSpec\n\nFixes-Red: 19", at=wt)
        d = A.waiting_state(self.r.path)[0]
        e, _ = A.queue_change(A.CurrentStore(A.process_store), d, {"worktree": wt, "branch": "repo-wt-queued", "head": sha, "commits": [sha]})  # no `fixes`
        e["status"] = "waiting"
        A.save_entry(d, e)
        self.tick()
        it = self.only()
        self.assertEqual((it["state"], it["data"]["fix"]["entry"]), ("fix-pending", e["id"]))
        self.assertEqual(self.st.offers("fixer"), [])
        # the gate is red on this very red (run 20; the fix names run 19 of the same key): it lands on red
        self.assertEqual(A.red_view(self.st, it, self.t[0])[1], "landing on red (Fixes-Red), waiting")
        self.tick(advance=3 * A.RED_FIX_GIVEUP)  # hours in the queue behind the red gate: still its holder
        self.assertEqual(self.only()["state"], "fix-pending")
        self.assertEqual(self.unheld(), [])
        self.assertEqual(A.red_enforce(self.st, A.red_fix_facts(self.r.path, self.st), self.t[0]), [])
        e = json.load(open(os.path.join(d, "entries", f"{e['id']}.json")))
        self.r.push(at=wt, ref=f"{sha}:main")  # landed: on main (a landed sha main lacks is no fix)
        e.update(status="landed", landed_sha=sha, landed_at=A.iso_at(self.t[0]))
        A.save_entry(d, e)
        self.tick(advance=60)
        it = self.only()
        self.assertEqual((it["state"], (it["data"]["fix"] or {}).get("status")), ("fix-pending", "landed"))  # verifying from now
        self.tick(advance=A.RED_FIX_GIVEUP - 120)
        self.assertEqual(self.only()["state"], "fix-pending")
        self.tick(advance=240)  # RED_FIX_GIVEUP after it LANDED, no run proving it: set aside, offered
        self.assertEqual(self.only()["state"], "offered")

    def test_a_cancelled_fix_follows_its_worktree_to_the_change_that_landed_it(self):
        # 2026-10-31, Main #2877: "fix 20261031-073114-movies-wt-flakes-9f6bc4 waiting to land (joined)" for hours: the
        # change was CANCELLED in the store (its entry still read `waiting`) and the same worktree re-queued as
        # ...-ed6118, which LANDED d677b1bb6. The red follows the worktree: verified by the run containing the landing.
        self.tick()
        wt = self.r.worktree("repo-wt-flakes", branch="repo-wt-flakes")
        self.r.write("fix.txt", "flakes\n", at=wt)
        sha = self.r.commit("fix FooSpec\n\nFixes-Red: 20", at=wt)
        d = A.waiting_state(self.r.path)[0]
        e, _ = A.queue_change(A.CurrentStore(A.process_store), d, {"worktree": wt, "branch": "repo-wt-flakes", "head": sha, "commits": [sha]})
        e["status"] = "waiting"
        A.save_entry(d, e)
        self.tick()
        self.assertEqual(self.only()["data"]["fix"]["entry"], e["id"])
        ch = self.st.put_item("change", e["id"], "queued", {"worktree": wt, "head": sha, "submitted_at": self.t[0]})
        self.st.transition("change", e["id"], "cancelled", ch["version"])  # the entry file keeps `waiting` (stale)
        self.tick(advance=60)
        self.assertEqual(self.only()["state"], "fix-pending")  # nothing landed yet: no re-offer while it is re-queued
        nx = self.st.put_item("change", "zz-requeued", "queued", {"worktree": wt, "head": sha,
                                                                  "submitted_at": self.t[0] + 1})  # re-queued, new id
        self.tick(advance=A.RED_FIX_REQUEUE_GRACE + 60)  # past the grace: it waits on the re-queued change
        it = self.only()
        self.assertEqual((it["state"], it["data"]["fix"]["entry"]), ("fix-pending", "zz-requeued"))
        self.r.push(at=wt, ref=f"{sha}:main")  # ...which lands
        self.st.transition("change", "zz-requeued", "landed", nx["version"], data={"tip": sha})
        self.tick(advance=60)
        it = self.only()
        self.assertEqual((it["state"], it["data"]["fix"]["entry"], it["data"]["fix"]["status"], it["data"]["fix"]["sha"]),
                         ("fix-pending", "zz-requeued", "landed", sha))
        self.tick(advance=60)  # the stale `waiting` entry never snaps it back
        self.assertEqual(self.only()["data"]["fix"]["entry"], "zz-requeued")
        self.put("runs.json", [run(50, 23, "Main", "main.yml", sha)] + json.load(open(os.path.join(self.d, "runs.json"))))
        self.tick()
        self.assertEqual([i["state"] for i in self.st.items("red")], ["verified"])

    def test_a_cancelled_fix_with_no_successor_is_offered_again(self):
        self.tick()
        wt = self.r.worktree("repo-wt-gone", branch="repo-wt-gone")
        self.r.write("fix.txt", "gone\n", at=wt)
        sha = self.r.commit("fix FooSpec\n\nFixes-Red: 20", at=wt)
        d = A.waiting_state(self.r.path)[0]
        e, _ = A.queue_change(A.CurrentStore(A.process_store), d, {"worktree": wt, "branch": "repo-wt-gone", "head": sha, "commits": [sha]})
        e["status"] = "waiting"
        A.save_entry(d, e)
        self.tick()
        ch = self.st.put_item("change", e["id"], "queued", {"worktree": wt, "head": sha, "submitted_at": self.t[0]})
        self.st.transition("change", e["id"], "cancelled", ch["version"])
        self.tick(advance=A.RED_FIX_GIVEUP + 60)  # never re-queued, never landed
        self.assertEqual(self.only()["state"], "offered")

    def queued_fix_pending(self, name):
        """A fix-pending red whose fix is a queued change: (its entry, the entry's file)."""
        self.tick()
        wt = self.r.worktree(f"repo-wt-{name}", branch=f"repo-wt-{name}")
        self.r.write("fix.txt", f"{name}\n", at=wt)
        sha = self.r.commit("fix FooSpec\n\nFixes-Red: 20", at=wt)
        d = A.waiting_state(self.r.path)[0]
        e, _ = A.queue_change(A.CurrentStore(A.process_store), d, {"worktree": wt, "branch": f"repo-wt-{name}", "head": sha, "commits": [sha]})
        A.save_entry(d, e)
        self.tick()
        self.assertEqual((self.only()["state"], self.only()["data"]["fix"]["entry"]), ("fix-pending", e["id"]))
        return e, os.path.join(d, "entries", f"{e['id']}.json"), wt, sha

    def test_a_fix_the_store_holds_checking_is_on_its_way_though_the_queue_files_lack_it(self):
        # 2026-11-22, Identity takes #349: the queue's entry files live on ci-runner alone; a change the store holds
        # queued or checking is the red's fix on its way whatever the facts' entries read -- never "no fix on its way".
        e, path, wt, sha = self.queued_fix_pending("store-only")
        self.st.put_item("change", e["id"], "checking", {"worktree": wt, "head": sha, "submitted_at": self.t[0]})
        os.remove(path)  # the facts' queue (the entry files) no longer has it
        self.t[0] += 3 * A.RED_FIX_GIVEUP
        facts = A.red_fix_facts(self.r.path, self.st)
        self.assertEqual(facts["entries"], [])
        it = self.only()
        self.assertEqual(A.red_fix_held(it, self.t[0], facts, self.st), f"fix {e['id']} checking")
        self.assertEqual(A.red_enforce(self.st, facts, self.t[0]), [])
        self.assertEqual(self.only()["state"], "fix-pending")

    def test_an_unread_queue_is_never_read_as_nothing_queued(self):
        e, _path, _wt, _sha = self.queued_fix_pending("unread")
        real = A.waiting_state

        def unreadable(repo):
            raise OSError("[Errno 5] Input/output error: entries")
        A.waiting_state = unreadable
        try:
            facts = A.red_fix_facts(self.r.path, self.st)
        finally:
            A.waiting_state = real
        self.assertIn("Input/output error", facts["queue_unread"])
        self.t[0] += 3 * A.RED_FIX_GIVEUP
        self.assertIn(f"fix {e['id']} ", A.red_fix_held(self.only(), self.t[0], facts, self.st) or "")
        self.assertEqual(A.red_enforce(self.st, facts, self.t[0]), [])

    def test_a_rejected_fix_past_the_requeue_grace_is_offered_again_naming_its_rejection(self):
        # 2026-11-22, Identity takes #349: its fix ...-779be4 was rejected at 04:28:56 (the landing check's itrest row);
        # at 04:44 the invariant said "no fix on its way for 23 min in fix-pending" -- read as a queued fix overlooked.
        e, _path, wt, sha = self.queued_fix_pending("rejected")
        ch = self.st.put_item("change", e["id"], "checking", {"worktree": wt, "head": sha, "submitted_at": self.t[0]})
        self.st.transition("change", e["id"], "rejected", ch["version"],
                           data={"why": "NOT LANDED: the pre-push checks failed: deploy.LauncherJvmFlagsSpec"})
        A.save_entry(A.waiting_state(self.r.path)[0], {**e, "status": "rejected"})
        self.tick(advance=60)
        self.assertEqual(self.only()["state"], "fix-pending")  # within the grace: its worktree may queue it again
        self.t[0] += A.RED_FIX_REQUEUE_GRACE  # the invariant meets it first (the red's own step raced the fixer's move)
        acts = A.red_enforce(self.st, A.red_fix_facts(self.r.path, self.st), self.t[0])
        self.assertEqual(len(acts), 1, acts)
        it = self.only()
        self.assertEqual(it["state"], "offered")
        outcome = it["data"]["history"][-1]["outcome"]
        self.assertIn(f"its fix {e['id']} was rejected", outcome)
        self.assertIn("deploy.LauncherJvmFlagsSpec", outcome)

    def test_a_landed_fix_no_run_ever_checks_is_set_aside_and_the_red_offered_again(self):
        self.tick()
        _wt, sha = self.fix_commit(20)
        self.tick()
        self.assertEqual(self.only()["state"], "fix-pending")
        self.tick(advance=A.RED_FIX_GIVEUP)  # no Main run containing it, ever
        it = self.only()
        self.assertEqual((it["state"], it["data"]["unverified"]), ("offered", [sha]))
        self.tick(advance=60)
        self.assertEqual(self.only()["state"], "offered")  # the set-aside fix does not snap it back to fix-pending
        self.assertEqual(self.unheld(), [])

    def test_a_refuted_fix_drops_its_lease_and_the_red_is_offered_again_with_the_failed_fix(self):
        self.tick()
        hw = self.r.worktree("repo-wt-holder", branch="holder")
        _wt, sha = self.fix_commit(20)
        self.tick()
        it = self.only()
        self.assertEqual(it["state"], "fix-pending")
        # someone holds the red meanwhile (a fixer still on it)
        self.st.acquire(A.red_lease(it["data"]["key"]), "laptop:claim:h@" + hw, 1800, 3600, meta={"kind": "claim",
                                                                                                     "worktree": hw})
        # Main #23 contains the fix and still fails the same way
        self.main_red(51, 23, older_runs=[run(40, 20, "Main", "main.yml", self.sha, conclusion="failure")])
        rj = json.load(open(os.path.join(self.d, "run-51.json")))
        rj["headSha"] = sha
        self.put("run-51.json", rj)
        runs = json.load(open(os.path.join(self.d, "runs.json")))
        runs[0]["headSha"] = sha
        self.put("runs.json", runs)
        self.tick()
        it = self.only()
        self.assertEqual(it["state"], "offered")
        self.assertEqual(it["data"]["refuted"], [sha])
        self.assertEqual(it["data"]["attempts"], 1)
        self.assertFalse(A.lease_is_live(self.st.get(A.red_lease(it["data"]["key"])), self.t[0]))  # its lease dropped
        self.assertTrue(os.path.exists(os.path.join(hw, "RED-STOP.md")))
        self.assertTrue(any(str(e.get("verdict")).startswith("FIX FAILED") for e in self.inbox()))


    def refute_with_main_23(self, sha):
        """Main #23 contains sha and still fails the same way."""
        self.main_red(51, 23, older_runs=[run(40, 20, "Main", "main.yml", self.sha, conclusion="failure")])
        rj = json.load(open(os.path.join(self.d, "run-51.json")))
        rj["headSha"] = sha
        self.put("run-51.json", rj)
        runs = json.load(open(os.path.join(self.d, "runs.json")))
        runs[0]["headSha"] = sha
        self.put("runs.json", runs)

    def landed_fix_whose_entry_reads_open(self):
        """A fix landed through the queue whose entry FILE still says `waiting` (stale) while the store's change says
        landed -- what red_fix_of reads as on its way and red_fix_fresh turns into the landed sha."""
        wt = self.r.worktree("repo-wt-stale", branch="repo-wt-stale")
        self.r.write("fix.txt", "stale\n", at=wt)
        sha = self.r.commit("fix FooSpec\n\nFixes-Red: 20", at=wt)
        d = A.waiting_state(self.r.path)[0]
        e, _ = A.queue_change(A.CurrentStore(A.process_store), d, {"worktree": wt, "branch": "repo-wt-stale", "head": sha, "commits": [sha]})
        e["status"] = "waiting"
        A.save_entry(d, e)
        ch = self.st.put_item("change", e["id"], "queued", {"worktree": wt, "head": sha, "submitted_at": self.t[0]})
        self.r.push(at=wt, ref=f"{sha}:main")
        self.st.transition("change", e["id"], "landed", ch["version"], data={"tip": sha})
        return sha

    def test_a_refuted_fix_whose_entry_still_reads_open_never_takes_the_red_back(self):
        # 2026-11-11, Verify scrape recording #23: a red flipped offered -> fix-pending -> offered every ~50 s, 593
        # times ("recurring: 593 fixes refuted"), each round re-adopting the same refuted 547709c1e2 through its queue
        # entry, so no fixer was ever offered it; it then sat in fix-pending on that fix.
        self.tick()
        sha = self.landed_fix_whose_entry_reads_open()
        self.tick()
        self.assertEqual((self.only()["state"], self.only()["data"]["fix"]["sha"]), ("fix-pending", sha))
        self.refute_with_main_23(sha)
        self.tick()
        self.assertEqual((self.only()["state"], self.only()["data"]["refuted"]), ("offered", [sha]))
        self.tick(advance=60)
        it = self.only()
        self.assertEqual((it["state"], it["data"]["refuted"]), ("offered", [sha]))  # never snapped back to fix-pending

    def test_a_stale_open_entry_of_a_refuted_fix_never_hides_the_next_fixes_red_trailer(self):
        # 2026-11-13, Verify scrape recording #26 (784badc4406e.4): its fix 8d06b1a5ab (`Fixes-Red: 26`) landed on main,
        # but the queue entry of the refuted fix before it (b6d3d8235, its file still `waiting`, naming #23 -- a run of
        # the same red key) was the one red_fix_of picked; set aside, it hid every landed trailer, so the red was
        # re-offered and its fixer put "hold it as fixed by 8d06b1a5ab" on the owner.
        self.tick()
        stale = self.landed_fix_whose_entry_reads_open()
        self.tick()
        self.refute_with_main_23(stale)
        self.tick()
        self.assertEqual((self.only()["state"], self.only()["data"]["refuted"]), ("offered", [stale]))
        _wt, sha = self.fix_commit(23, name="repo-wt-next")
        self.tick(advance=60)
        it = self.only()
        self.assertEqual((it["state"], (it["data"].get("fix") or {}).get("sha")), ("fix-pending", sha))

    def stale_entry_landed_as(self, tip, name="repo-wt-phantom", head=None):
        """A queue entry naming run 20 whose FILE still reads open, its store change LANDED with `tip` -- the tip of the
        change's own check tree, which the landing may have re-cut (Order independence #78, 2026-11-16)."""
        d = A.waiting_state(self.r.path)[0]
        e, _ = A.queue_change(A.CurrentStore(A.process_store), d, {"worktree": name, "branch": name, "head": head or tip, "commits": [head or tip],
                                  "fixes": ["20"]})
        e["status"] = "joined"
        A.save_entry(d, e)
        ch = self.st.put_item("change", e["id"], "queued", {"worktree": name, "head": head or tip,
                                                            "submitted_at": self.t[0]})
        self.st.transition("change", e["id"], "landed", ch["version"], data={"tip": tip})
        return e

    def unlanded_commit(self, name="repo-wt-phantom"):
        wt = self.r.worktree(name, base="origin/main", branch=name)
        self.r.write("fix.txt", "the phantom fix\n", at=wt)
        return wt, self.r.commit("fix FooSpec (its entry names the red; no trailer, so main's trailers never link it)", at=wt)

    def test_a_landed_changes_tip_that_never_reached_main_is_no_fix(self):
        # 2026-11-16, Order independence #78 (b3f51c5166b0.2): "fix landed in b6d3d8235" -- a change item's tip, its
        # check tree's pre-rebase commit, on no main; the red sat in fix-pending, its fixer gone, on a sha no run holds.
        self.tick()
        _wt, phantom = self.unlanded_commit()
        self.stale_entry_landed_as(phantom)
        self.tick(advance=60)
        it = self.only()
        self.assertEqual(it["state"], "offered", it["data"].get("fix"))

    def test_a_landed_changes_re_cut_tip_follows_its_content_to_main(self):
        self.tick()
        _wt, phantom = self.unlanded_commit()
        lw = self.r.worktree("repo-wt-landed", base="origin/main", branch="repo-wt-landed")
        self.r.write("other.txt", "main moved meanwhile\n", at=lw)
        self.r.commit("an unrelated commit under the landed copy", at=lw)
        subprocess.run(["git", "-C", lw, "cherry-pick", phantom], check=True, capture_output=True)
        landed = subprocess.run(["git", "-C", lw, "rev-parse", "HEAD"], capture_output=True, text=True).stdout.strip()
        self.r.push(at=lw, ref=f"{landed}:main")
        self.stale_entry_landed_as(phantom)
        self.tick(advance=60)
        it = self.only()
        self.assertEqual((it["state"], it["data"]["fix"]["sha"]), ("fix-pending", landed))

    def test_a_change_that_landed_before_the_red_opened_is_no_fix_for_it(self):
        # b6d3d8235's change landed on 2026-11-11 (as 19482f5c08, Verify scrape recording #23's fix, its entry also
        # naming "40"); Order independence #78, a run containing it, opened this spell on 2026-11-16 and took it.
        wt = self.r.worktree("repo-wt-old", base="origin/main", branch="repo-wt-old")
        self.r.write("fix.txt", "old\n", at=wt)
        sha = self.r.commit("fix FooSpec (linked by its entry alone)", at=wt)
        self.r.push(at=wt, ref=f"{sha}:main")
        self.t[0] -= 2 * 86400
        self.stale_entry_landed_as(sha, name="repo-wt-old")
        self.t[0] += 2 * 86400
        self.tick()
        self.tick(advance=60)
        self.assertEqual(self.only()["state"], "offered", self.only()["data"].get("fix"))

    def test_a_fix_pending_on_a_sha_main_never_got_is_offered_again(self):
        self.tick()
        it = self.only()
        _wt, phantom = self.unlanded_commit()
        self.st.transition("red", it["id"], "fix-pending", it["version"],
                           data={"fix": {"entry": None, "status": "landed", "sha": phantom, "at": self.t[0]}})
        self.tick(advance=60)
        it = self.only()
        self.assertEqual((it["state"], it["data"].get("fix")), ("offered", None))
        self.assertIn("not on main", it["data"]["history"][-1]["outcome"])

    def test_a_fix_pending_on_a_re_cut_sha_follows_it_to_its_landed_copy(self):
        self.tick()
        it = self.only()
        _wt, phantom = self.unlanded_commit()
        lw = self.r.worktree("repo-wt-landed", base="origin/main", branch="repo-wt-landed")
        self.r.write("other.txt", "main moved meanwhile\n", at=lw)
        self.r.commit("an unrelated commit under the landed copy", at=lw)
        subprocess.run(["git", "-C", lw, "cherry-pick", phantom], check=True, capture_output=True)
        landed = subprocess.run(["git", "-C", lw, "rev-parse", "HEAD"], capture_output=True, text=True).stdout.strip()
        self.r.push(at=lw, ref=f"{landed}:main")
        e = self.stale_entry_landed_as(phantom)
        self.st.transition("red", it["id"], "fix-pending", it["version"],
                           data={"fix": {"entry": e["id"], "status": "landed", "sha": phantom, "at": self.t[0]}})
        self.tick(advance=60)
        it = self.only()
        self.assertEqual((it["state"], it["data"]["fix"]["sha"]), ("fix-pending", landed))

    def test_a_fix_pending_on_a_fix_that_landed_before_it_opened_is_offered_again(self):
        # b3f51c5166b0.2 as it stood: fix-pending since 2026-11-16 on a fix that landed on 2026-11-11.
        self.tick()
        it = self.only()
        wt = self.r.worktree("repo-wt-old", base="origin/main", branch="repo-wt-old")
        self.r.write("fix.txt", "old\n", at=wt)
        sha = self.r.commit("fix FooSpec (linked by its entry alone)", at=wt)
        self.r.push(at=wt, ref=f"{sha}:main")
        self.st.transition("red", it["id"], "fix-pending", it["version"], data={"fix": {
            "entry": None, "status": "landed", "sha": sha, "at": float(it["data"]["opened_at"]) - 5 * 86400}})
        self.tick(advance=60)
        it = self.only()
        self.assertEqual((it["state"], it["data"].get("fix")), ("offered", None))
        self.assertIn("landed before", it["data"]["history"][-1]["outcome"])

    def waits_on(self, sha, other="other", other_state="leased", other_data=None):
        """The red fix-pending on red `other`'s commit sha (e948278d144a on e1be6bd9fd46's 715ab5a1a)."""
        self.tick()
        self.st.put_item("red", other, other_state, {"key": other, "workflow": "Main", "runs": ["2521"],
                                                     **(other_data or {})})
        it = self.only() if other_state in A.RED_TERMINAL else next(
            i for i in self.st.items("red") if i["id"] != other and i["state"] not in A.RED_TERMINAL)
        self.st.transition("red", it["id"], "fix-pending", it["version"], data={"fix": {
            "entry": None, "status": "waits-on", "sha": sha, "red": other, "at": self.t[0]}})
        return it["id"]

    def test_a_red_waiting_on_another_reds_fix_waits_while_it_is_not_on_main(self):
        _wt, sha = self.unlanded_commit()
        iid = self.waits_on(sha)
        self.tick(advance=A.RED_FIX_GIVEUP + 60)
        it = self.st.item("red", iid)
        self.assertEqual((it["state"], it["data"]["fix"]["status"]), ("fix-pending", "waits-on"))
        self.assertNotIn(iid, [i["id"] for i in self.unheld()])

    def test_a_red_waiting_on_another_reds_fix_verifies_it_once_it_lands(self):
        wt, sha = self.unlanded_commit()
        iid = self.waits_on(sha)
        lw = self.r.worktree("repo-wt-landed", base="origin/main", branch="repo-wt-landed")
        self.r.write("other.txt", "main moved meanwhile\n", at=lw)
        self.r.commit("an unrelated commit under the landed copy", at=lw)
        subprocess.run(["git", "-C", lw, "cherry-pick", sha], check=True, capture_output=True)
        landed = subprocess.run(["git", "-C", lw, "rev-parse", "HEAD"], capture_output=True, text=True).stdout.strip()
        self.r.push(at=lw, ref=f"{landed}:main")
        self.tick(advance=60)
        it = self.st.item("red", iid)
        self.assertEqual((it["state"], it["data"]["fix"]["status"], it["data"]["fix"]["sha"]),
                         ("fix-pending", "landed", landed))

    def test_a_red_waiting_on_a_fix_its_red_set_aside_is_offered_again(self):
        _wt, sha = self.unlanded_commit()
        iid = self.waits_on(sha, other_state="offered", other_data={"refuted": [sha]})
        self.tick(advance=60)
        it = self.st.item("red", iid)
        self.assertEqual((it["state"], it["data"].get("fix")), ("offered", None))
        self.assertIn("set aside", it["data"]["history"][-1]["outcome"])

    def test_a_red_waiting_on_a_red_closed_without_the_fix_is_offered_again(self):
        _wt, sha = self.unlanded_commit()
        iid = self.waits_on(sha, other_state="cleared")
        self.tick(advance=60)
        it = self.st.item("red", iid)
        self.assertEqual((it["state"], it["data"].get("fix")), ("offered", None))
        self.assertIn("closed", it["data"]["history"][-1]["outcome"])

    def test_two_reds_waiting_on_each_others_fix_are_offered_again(self):
        # A waits on B's commit while B waits on A's: neither can land first, and each one's holder is "the other red"
        # (red_fix_held), so W2 never fires -- a state with no exit unless the reconciler sees the cycle.
        _wt, sha = self.unlanded_commit()
        self.tick()
        mine = self.only()
        self.st.put_item("red", "other", "fix-pending", {"key": "other", "workflow": "Main", "runs": ["2521"], "fix": {
            "entry": None, "status": "waits-on", "sha": sha, "red": mine["data"]["key"], "at": self.t[0]}})
        self.st.transition("red", mine["id"], "fix-pending", mine["version"], data={"fix": {
            "entry": None, "status": "waits-on", "sha": sha, "red": "other", "at": self.t[0]}})
        self.tick(advance=60)
        it = self.st.item("red", mine["id"])
        self.assertEqual((it["state"], it["data"].get("fix")), ("offered", None))
        self.assertIn("waits on", it["data"]["history"][-1]["outcome"])
        self.assertIn("cycle", it["data"]["history"][-1]["outcome"])
        other = self.st.item("red", "other")
        self.assertFalse([h for h in other["data"].get("history") or [] if "cycle" in str(h.get("outcome"))],
                         "ONE side of the cycle is offered again for it: the other then waits on that side's fix")

    def test_a_waits_on_verdict_that_would_close_a_wait_cycle_is_no_wait(self):
        self.tick()
        mine = self.only()["data"]["key"]
        self.st.put_item("red", "other", "fix-pending", {"key": "other", "workflow": "Main", "runs": ["2521"], "fix": {
            "entry": None, "status": "waits-on", "sha": "a" * 40, "red": mine, "at": self.t[0]}})
        self.assertIsNone(A.red_waits_on_of("WAITS ON bbbbbbbbbb (red other): its fix", mine, self.st))
        self.st.put_item("red", "third", "leased", {"key": "third", "workflow": "Main", "runs": ["2522"]})
        self.assertEqual(A.red_waits_on_of("WAITS ON bbbbbbbbbb (red third): its fix", mine, self.st),
                         {"sha": "bbbbbbbbbb", "red": "third"})

    def test_refuted_fixes_are_counted_once_each(self):
        # 784badc4406e.3 stored the same two shas 593 times: "recurring: 593 fixes refuted", and a fixer brief that
        # would have listed all 593 as the landed fixes it outlived.
        self.tick()
        it = self.only()
        a, b = "a" * 40, "b" * 40
        self.st.update_item("red", it["id"], it["version"], {"refuted": [a, b, a, b, a, a]})
        text, _short = A.red_view_state(self.st, self.only())
        self.assertNotIn("recurring", text)  # 2 distinct refuted fixes, under RED_RECURRING
        self.assertEqual(A.red_refuted({"refuted": [a, b, a, b, a, a, "c" * 40]}), [a, b, "c" * 40])

    def test_a_red_pending_on_a_fix_it_already_refuted_is_offered_again(self):
        # The state 784badc4406e.3 was left in: fix-pending on a fix its own `refuted` lists, waiting hours for a run
        # to verify what a run had already refuted.
        self.tick()
        sha = self.landed_fix_whose_entry_reads_open()
        self.tick()
        it = self.only()
        self.assertEqual(it["state"], "fix-pending")
        self.st.update_item("red", it["id"], it["version"], {"refuted": [sha]})
        self.tick(advance=60)
        self.assertEqual(self.only()["state"], "offered")

    def test_an_outside_reds_landed_fix_gets_its_listed_workflow_dispatched_to_verify_it(self):
        # a scheduled workflow is dispatched to verify only when the repo lists it (A VERIFY DISPATCH IS PER WORKFLOW;
        # unlisted, it waits for its slot: test_verify_wait.py)
        self.put("runs.json", [run(40, 20, "Main", "main.yml", self.sha), run(39, 19, "Identity model convergence",
                                                                             "identity-model-convergence.yml", self.sha),
                               run(70, 2104, "Identity capture", "identity-capture.yml", self.sha, conclusion="failure")])
        self.put("run-70.json", {"jobs": [{"name": "capture (uk)", "status": "completed", "conclusion": "failure",
                                           "steps": [{"name": "Run", "conclusion": "failure"}]}], "headSha": self.sha,
                                 "url": "u", "number": 2104, "status": "completed", "conclusion": "failure",
                                 "workflowName": "Identity capture"})
        self.put("run-70.log", "capture (uk)\tRun\t2026-10-27T08:00:00Z [info] - CaptureSpec *** FAILED ***\n")
        self.r.write(".github/workflows/identity-capture.yml",
                     "name: Identity capture\non:\n  schedule:\n    - cron: '0 3 * * 1'\n  workflow_dispatch:\n")
        self.r.write(A.RED_WORKFLOWS_FILE, "Identity capture\tyes\t-\tcheap: dispatched to verify a fix\n")
        self.r.commit("the workflow")
        self.r.push()
        self.tick()
        self.fix_commit(2104)
        self.tick()
        it = self.only()
        self.assertEqual(it["state"], "fix-pending")
        calls, saved = [], (A.api, A.record_dispatch)
        A.api = lambda o_r, method, path, params=None, body=None: (  # reads stay real: the run lists are REST reads
            saved[0](o_r, method, path, params, body) if method == "GET" else calls.append((method, path)))
        A.record_dispatch = lambda **kw: None
        try:  # past the window from the fix's own time (its commit's wall clock, seconds after self.t's start)
            self.tick(advance=float(it["data"]["fix"]["at"]) + A.RED_FIX_RUN_START + 1 - self.t[0])
        finally:
            A.api, A.record_dispatch = saved
        self.assertIn(("POST", "actions/workflows/identity-capture.yml/dispatches"), calls)
        self.assertTrue(self.only()["data"]["fix"]["verify_dispatched"])

    def outside_capture_red(self):
        """Identity capture #2104 red on main (an outside red: no gate run ever proves its fix)."""
        self.put("runs.json", [run(40, 20, "Main", "main.yml", self.sha), run(39, 19, "Identity model convergence",
                                                                             "identity-model-convergence.yml", self.sha),
                               run(70, 2104, "Identity capture", "identity-capture.yml", self.sha, conclusion="failure")])
        self.put("run-70.json", {"jobs": [{"name": "capture (uk)", "status": "completed", "conclusion": "failure",
                                           "steps": [{"name": "Run", "conclusion": "failure"}]}], "headSha": self.sha,
                                 "url": "u", "number": 2104, "status": "completed", "conclusion": "failure",
                                 "workflowName": "Identity capture"})
        self.put("run-70.log", "capture (uk)\tRun\t2026-10-27T08:00:00Z [info] - CaptureSpec *** FAILED ***\n")
        self.r.write(".github/workflows/identity-capture.yml",
                     "name: Identity capture\non:\n  schedule:\n    - cron: '0 3 * * 1'\n  workflow_dispatch:\n")
        self.r.commit("the workflow")
        self.r.push()
        self.tick()
        return self.only()

    def test_a_fixers_own_landed_fix_holds_its_red_in_fix_pending_until_its_workflows_next_run(self):
        # 2026-11-13, Verify scrape recording #26 (784badc4406e.4): its fixer ended `LANDED 8d06b1a5ab`, and the red was
        # offered again (the fix not linked), its next fixer asked the owner to "hold the red as fixed by 8d06b1a5ab
        # until the next nightly run proves it" (WAITING ON YOU). A fixer's own LANDED is the red's fix on its way: the
        # red waits in fix-pending, no owner item, for as long as its workflow has not run since; the next run decides.
        # ON A TUESDAY, whatever day the suite runs: the day of ticks below must not reach the weekly Monday 03:00Z
        # slot's deadline, past which a missed verify is re-offered by design (a9443a6) -- run on a Sunday, the test
        # crossed it and failed (2026-11-22, refusing every push).
        while time.gmtime(self.t[0]).tm_wday != 1:
            self.t[0] += 86400
        it = self.outside_capture_red()
        key = it["data"]["key"]
        wt = self.r.worktree("repo-wt-capture", branch="repo-wt-capture")
        self.r.write("capture.txt", "fixed\n", at=wt)
        sha = self.r.commit("fix CaptureSpec", at=wt)  # no trailer: only the fixer's verdict names it
        self.r.push(at=wt, ref=f"{sha}:main")
        lease = self.st.acquire(A.red_lease(key), "laptop:claim:fixer@" + wt, 1800, 3600,
                                meta={"kind": "claim", "worktree": wt})
        self.tick(advance=5)
        self.assertEqual(self.only()["state"], "leased")
        self.st.release(A.red_lease(key), lease["epoch"], lease["secret"], outcome=f"LANDED {sha}")
        self.tick(advance=5)
        it = self.only()
        self.assertEqual((it["state"], (it["data"].get("fix") or {}).get("sha")), ("fix-pending", sha))
        seq = max([e["seq"] for e in self.st.events()] or [0])
        calls, saved = [], (A.api, A.record_dispatch)
        A.api = lambda o_r, method, path, params=None, body=None: (
            saved[0](o_r, method, path, params, body) if method == "GET" else calls.append((method, path)))
        A.record_dispatch = lambda **kw: None
        try:  # a nightly's day with no run of its workflow: never past RED_FIX_GIVEUP to a fixer, never to the owner
            for _ in range(8):
                self.tick(advance=A.RED_FIX_GIVEUP // 2)
        finally:
            A.api, A.record_dispatch = saved
        it = self.only()
        self.assertEqual((it["state"], it["data"]["fix"]["sha"]), ("fix-pending", sha))
        later = [e for e in self.st.events(after=seq) if e.get("item_id") == it["id"]]
        self.assertEqual([e for e in later if e.get("severity") == "escalate"], [])
        self.assertIsNone(A.red_block_of(it, self.st))
        # its workflow's next run contains the fix and fails the same way: back to a fixer (refuted), not the owner
        runs = json.load(open(os.path.join(self.d, "runs.json")))
        nxt = run(71, 2105, "Identity capture", "identity-capture.yml", sha, conclusion="failure",
                  created=A.iso_at(self.t[0]))
        self.put("runs.json", [nxt] + runs)
        rj = json.load(open(os.path.join(self.d, "run-70.json")))
        self.put("run-71.json", {**rj, "headSha": sha, "number": 2105})
        self.put("run-71.log", open(os.path.join(self.d, "run-70.log")).read())
        self.tick(advance=60)
        it = self.only()
        self.assertEqual((it["state"], it["data"]["refuted"]), ("offered", [sha]))

    def test_a_verify_dispatch_takes_the_failing_runs_inputs_from_its_title_when_agentctl_did_not_start_it(self):
        # 2026-11-11, Verify scrape recording #23: dispatched by Record scrape fixtures (no agentctl record of its
        # inputs; GitHub serves none), the verifying dispatch went out without its required `recording` and GitHub
        # refused it (HTTP 422) -- no run could verify the red. The workflow's run-name carries `name=value` inputs.
        self.r.write(".github/workflows/verify-scrape-recording.yml",
                     "name: Verify scrape recording\nrun-name: Verify scrape recording recording=${{ inputs.recording }} "
                     "countries=${{ inputs.countries }}\non:\n  workflow_dispatch:\n    inputs:\n      recording:\n"
                     "        required: true\n        type: string\n      countries:\n        required: false\n"
                     "        type: string\n")
        self.r.commit("the workflow")
        self.r.push()
        self.tick()
        self.put("run-70.json", {"displayTitle": "Verify scrape recording recording=4242 countries=pl,de unrelated=x",
                                 "number": 23, "status": "completed", "conclusion": "failure", "jobs": []})
        posted, saved = [], (A.api, A.record_dispatch)
        A.api = lambda o_r, method, path, params=None, body=None: posted.append((path, body))
        A.record_dispatch = lambda **kw: None
        try:
            line = A.red_verify_dispatch("pawelkrupinski/movies", self.r.path, "Verify scrape recording", "f" * 40, 70)
        finally:
            A.api, A.record_dispatch = saved
        self.assertEqual(posted, [("actions/workflows/verify-scrape-recording.yml/dispatches",
                                   {"ref": "main", "inputs": {"recording": "4242", "countries": "pl,de"}})], line)


    def test_a_change_landing_on_red_under_the_ci_fix_rule_is_no_fix_for_the_red(self):
        self.tick()
        wt = self.r.worktree("repo-wt-ci", branch="ci")
        self.r.write("ci.txt", "x\n", at=wt)
        c = self.r.commit("CI kick fix\n\nFixes-Red: 20\nRed-Unrelated: CI kick fix, unrelated to the identity red", at=wt)
        d, _c, _w = A.waiting_state(self.r.path)
        os.makedirs(os.path.join(d, "entries"), exist_ok=True)
        A.write_json(os.path.join(d, "entries", "e9.json"), {"id": "e9", "seq": 9, "status": "checking", "fixes": ["20"],
                                                             "commits": [c], "worktree": wt})
        self.tick()
        self.assertEqual(self.only()["state"], "offered")  # not fix-pending: the red keeps its own fixers

    def test_a_change_landed_on_red_under_the_speed_up_rule_is_no_fix_for_the_red(self):
        # Identity model convergence #2432 (2026-10-28): four CI speed-ups landed on red carrying `Fixes-Red: 2392` beside
        # `Red-Unrelated:`; each was read off main as the red's fix on its way, its live fixer was stopped, and the next
        # run counted it a refuted fix -- the red went "recurring: 4 fixes refuted" with none of them a fix.
        self.tick()
        wt = self.r.worktree("repo-wt-speedup", branch="speedup")
        self.r.write("speedup.txt", "x\n", at=wt)
        sha = self.r.commit("CI speed-up\n\nFixes-Red: 20\nRed-Unrelated: a CI speed-up, landing on red; no fix for this red",
                            at=wt)
        self.r.push(at=wt, ref=f"{sha}:main")
        self.tick()
        self.assertEqual(self.only()["state"], "offered")  # not fix-pending: the red keeps its own fixers
        self.assertIsNone(self.only()["data"].get("fix"))

    def test_a_commit_naming_the_red_only_to_pass_the_gate_fixed_nothing_between_red_and_green(self):
        rec = {"runs": ["20"], "job": "takes-drift-1 (germany)", "line": "identity drift (de)", "workflow": "x.yml"}
        red = A.git(self.r.path, "rev-parse", "HEAD").strip()
        self.r.write("speedup.txt", "x\n")
        unrelated = self.r.commit("CI speed-up\n\nFixes-Red: 20\nRed-Unrelated: a CI speed-up, landing on red")
        self.assertEqual(A.fix_between(self.r.path, rec, red, unrelated, {}, {}), (None, True))  # passing by chance: a flake
        self.r.write("fix.txt", "x\n")
        fix = self.r.commit("fix it\n\nFixes-Red: 20")
        self.assertEqual(A.fix_between(self.r.path, rec, red, fix, {}, {}), (fix, True))


class ClearTest(RedsBase):
    def test_the_observation_that_opened_a_red_clears_it(self):
        self.tick()
        self.assertEqual(self.only()["state"], "offered")
        self.tick()  # the same red run: stays
        self.assertEqual(self.only()["state"], "offered")
        self.green(41, 21)  # a newer decided run without the failure: the same observation, negative
        self.tick()
        states = [i["state"] for i in self.st.items("red") if i["data"].get("red_kind") != "flake"]
        self.assertEqual(states, ["cleared"])
        self.assertIsNone(self.st.offer_of(f"red:{self.st.items('red')[0]['id']}"))

    def test_an_unread_gate_never_clears_or_opens_anything(self):
        self.tick()
        self.green(41, 21)
        self.tick(snap=False)
        self.assertEqual(self.only()["state"], "offered")


class OwnerTest(RedsBase):
    def test_a_code_red_is_never_blocked_on_the_owner(self):
        self.tick()
        rc, out = self.tool("red", "block", "20", "--owner-cause", "deploy", "--cmd", "deploy it", "--repo", "movies",
                            cwd=self.r.path)
        self.assertEqual(rc, 2, out)
        self.assertIn("TEST FAILURE", out)
        self.assertEqual(self.only()["state"], "offered")
        # and the ladder has no owner rung: every attempt count ends at a code tier, the deepest escalating
        tiers = {A.red_tier({"kind": "test", "attempts": n}) for n in range(0, 9)}
        self.assertEqual(tiers, {"fresh", "deep"})
        self.assertNotIn("owner", {t for _n, _c, t in A.RED_LADDER})

    def test_one_external_cause_holds_its_reds_and_only_the_cause_escalates(self):
        # two outside reds of the same workflow (an outage's own probe), held by ONE cause condition
        self.put("runs.json", [run(60, 30, "Decodo probe", "decodo.yml", self.sha, conclusion="failure"),
                               run(61, 31, "Decodo probe", "decodo.yml", self.sha, conclusion="success",
                                   created="2026-10-27T06:00:00Z")] + json.load(open(os.path.join(self.d, "runs.json"))))
        self.put("run-60.json", {"jobs": [{"name": "probe", "status": "completed", "conclusion": "failure",
                                           "steps": [{"name": "Set up job", "conclusion": "failure"}]},
                                          {"name": "probe2", "status": "completed", "conclusion": "failure",
                                           "steps": [{"name": "Set up runner", "conclusion": "failure"}]}],
                                 "headSha": self.sha, "url": "u", "number": 30, "status": "completed",
                                 "conclusion": "failure", "workflowName": "Decodo probe"})
        self.put("run-60.log", "")
        self.tick()
        rc, out = self.tool("red", "block", "30", "--cause", "Decodo", "--owner-cause", "external-account", "--cmd",
                            "check Decodo", "--until-green", "Decodo probe", "--repo", "movies", cwd=self.r.path)
        self.assertEqual(rc, 0, out)
        self.tick()
        self.tick()
        decodo = [i for i in self.st.items("red") if i["data"]["workflow"] == "Decodo probe"]
        self.assertEqual(len(decodo), 1)  # its line-less infra jobs: one red
        self.assertEqual({i["state"] for i in decodo}, {"blocked-external"})
        self.assertEqual(self.unheld(), [])
        for _ in range(4):  # past the offer deadlines; the tick renews the block meanwhile (its probe still fails)
            self.tick(advance=9 * 60)
        self.assertEqual({self.st.item("red", i["id"])["state"] for i in decodo}, {"blocked-external"})
        esc = [e for e in self.st.events() if e["severity"] == "escalate" and e["item_id"] in {i["id"] for i in decodo}]
        # the reds do not escalate one by one: the owner is told ONCE for the cause (ask.py), never per tick or red
        self.assertEqual([e["kind"] for e in esc], ["red.ask"])
        self.assertIn("WAITING ON YOU", esc[0]["refs"]["verdict"])
        lease = self.st.get("block/cause/Decodo")
        self.assertTrue(A.lease_is_live(lease, self.t[0]))  # it survives every tick while its probe fails
        lease = self.st.get("block/cause/Decodo")
        self.assertEqual(lease["holder"], "owner:external-account")


    def test_an_owner_command_that_deletes_is_handed_over_only_scoped_and_guarded(self):
        # issue 20261110T084827-38f01b: `red block` for core #923 handed the owner `sudo rm -rf
        # /run/github-runner/bitcashier-gh-builder-{1,4,6,7,8,12}/core/core/docker`; the owner ran it 8 h later while
        # builders 4, 6 and 12 ran Main Pipeline #939, whose docker/ vanished under it: red #939
        self.main_red(40, 20, kind="infra")
        self.tick()
        unsafe = {
            "ssh ci-runner 'cd /run && sudo rm -rf /run/github-runner/gh-builder-{1,4,6}/core/core/docker'": "unscoped",
            "sudo rm -rf /run/github-runner/gh-builder-4/core/core/*": "unscoped",
            "ssh ci-runner 'pgrep -x dockerd && sudo rm -rf /run/github-runner/gh-builder-4/core/core/docker'":
                "Runner.Worker",
            "rm -rf /srv/cache/stale-dir": "unguarded",
            "test -d ~ && rm -rf ~": "unscoped",
            "docker system prune -af": "unscoped",
        }
        for cmd, why in unsafe.items():
            rc, out = self.tool("red", "block", "20", "--owner-cause", "infra-apply", "--cmd", cmd, "--until-http",
                                "http://127.0.0.1:9/x", "--repo", "movies", cwd=self.r.path)
            self.assertEqual(rc, 2, f"{cmd}: {out}")
            self.assertIn("NOT BLOCKED", out)
            self.assertIn(why, out, cmd)
            self.assertEqual(self.st.list("block/"), [], cmd)
        guarded = ("ssh ci-runner 'for s in 1 4 6; do pgrep -f \"gh-builder-$s/.*Runner.Worker\" >/dev/null || "
                   "sudo rm -rf \"/run/github-runner/gh-builder-$s/core/core/docker\"; done'")
        self.assertIsNone(A.owner_cmd_unsafe(guarded))
        self.assertIsNone(A.owner_cmd_unsafe("secrets unlock movies"))
        rc, out = self.tool("red", "block", "20", "--owner-cause", "infra-apply", "--cmd", guarded, "--until-http",
                            "http://127.0.0.1:9/x", "--repo", "movies", cwd=self.r.path)
        self.assertEqual(rc, 0, out)
        # a block raised before the guard, carrying an unguarded deletion, lapses at the next tick: never asked
        lease = self.st.list("block/")[0]
        self.st.set_meta(lease["name"], lease["epoch"], lease["secret"],
                         {"cmd": "sudo rm -rf /run/github-runner/gh-builder-{1,4}/core/core/docker"})
        self.tick()
        self.assertEqual([r for r in self.st.list("block/") if A.lease_is_live(r, self.t[0])], [])
        self.assertIn("unscoped", A.lease_meta(self.st.list("block/")[0])["outcome"])

    def test_a_live_fixer_outranks_a_block_whose_justification_then_lapses(self):
        self.main_red(40, 20, kind="infra")
        self.tick()
        rc, out = self.tool("red", "block", "20", "--owner-cause", "deploy", "--cmd", "deploy it", "--until-http",
                            "http://127.0.0.1:9/x", "--repo", "movies", cwd=self.r.path)
        self.assertEqual(rc, 0, out)
        self.tick()
        it = self.only()
        self.assertEqual(it["state"], "blocked-external")
        wt = self.r.worktree("repo-wt-fx", branch="fx")
        self.assertEqual(self.tool("red", "claim", "20", "--repo", "movies", cwd=wt)[0], 0)
        self.t[0] = A.epoch_now()
        self.tick()
        self.tick()
        self.assertEqual(self.only()["state"], "leased")
        self.assertEqual([r for r in self.st.list("block/") if A.lease_is_live(r, self.t[0])], [])
        self.assertIn("INVALID: a live fixer", A.lease_meta(self.st.list("block/")[0])["outcome"])

    def held_by_a_fixer(self, session, run="20"):
        """The open red, leased to a dispatched fixer the way red_fixer holds it, and that fixer's record."""
        self.main_red(40, 20, kind="infra")
        self.tick()
        it = self.only()
        name = red = A.red_lease(it["data"]["key"])
        took = self.st.acquire(name, f"laptop:fixer:red-{it['data']['key']}-1", A.RED_FIXER_TTL, A.RED_FIXER_MAX,
                               progress="start", meta={"kind": "fixer"})
        os.makedirs(A.reds_dir(run), exist_ok=True)
        path = A.reds_dir(run, "fixer.json")
        A.write_json(path, {"run": int(run), "lease": red, "epoch": took["epoch"], "session": session,
                            "started": "2026-10-29T00:00:00Z"})
        return it, path

    def test_a_dispatched_fixer_blocks_its_own_red_on_the_owner(self):
        # 2026-10-29, #2426: its fixer's `red block` answered "a live fixer is on #2426" -- itself -- so the red went
        # back to the dispatcher instead of the owner, and the next fixer was told its fix "did not fix it"
        it, path = self.held_by_a_fixer("s-mine")
        os.environ.update(CLAUDE_CODE_SESSION_ID="s-mine", AGENTCTL_FIXER_RECORD=path)
        rc, out = self.tool("red", "block", "20", "--owner-cause", "credentials", "--cmd", "set the key",
                            "--until-http", "http://127.0.0.1:9/x", "--repo", "movies", cwd=self.r.path)
        self.assertEqual(rc, 0, out)
        self.assertIn("RED BLOCKED ON OWNER", out)
        lease = self.st.get(A.red_lease(it["data"]["key"]))
        self.assertFalse(A.lease_is_live(lease, A.epoch_now()))  # handed over: its wrapper ends with the block's verdict
        self.assertTrue(A.lease_meta(lease)["outcome"].startswith("BLOCKED ON OWNER"))
        self.t[0] = A.epoch_now()
        self.tick()
        self.assertEqual(self.only()["state"], "blocked-external")

    def test_a_fixer_hands_its_lease_to_a_block_already_up_for_its_red(self):
        # #2426 again: a block lease for the red was already live beside the fixer's lease, and the second `red block`
        # answered "block/<key> is held by owner:credentials" without handing the fixer's lease over
        it, path = self.held_by_a_fixer("s-mine")
        lease = self.st.get(A.red_lease(it["data"]["key"]))
        self.st.acquire(A.block_lease(it["data"]["key"]), "owner:credentials", A.BLOCK_TTL, A.BLOCK_MAX, progress="p",
                        meta={"kind": "block", "owner_cause": "credentials", "cmd": "set the key",
                              "keys": ["the-runs-other-red"], "text": "BLOCKED ON OWNER: set the key"})
        os.environ.update(CLAUDE_CODE_SESSION_ID="s-mine", AGENTCTL_FIXER_RECORD=path)
        rc, out = self.tool("red", "block", "20", "--owner-cause", "credentials", "--cmd", "set the key",
                            "--until-http", "http://127.0.0.1:9/x", "--repo", "movies", cwd=self.r.path)
        self.assertEqual(rc, 0, out)
        self.assertIn("already blocked", out)
        self.assertIn(it["data"]["key"], A.lease_meta(self.st.get(A.block_lease(it["data"]["key"])))["keys"])
        self.assertFalse(A.lease_is_live(self.st.get(A.red_lease(it["data"]["key"])), A.epoch_now()), lease)

    def test_a_fixer_blocks_its_own_red_named_by_its_key(self):
        # issue 20261119T030011-293b9a: `red block 0c8eea91b712` failed looking the key up as a run, and a run whose
        # units no longer read (a green rerun) answered "names no red on main", while `red claim` found the red
        it, path = self.held_by_a_fixer("s-mine")
        os.environ.update(CLAUDE_CODE_SESSION_ID="s-mine", AGENTCTL_FIXER_RECORD=path)
        rc, out = self.tool("red", "block", it["data"]["key"], "--owner-cause", "credentials", "--cmd", "set the key",
                            "--until-http", "http://127.0.0.1:9/x", "--repo", "movies", cwd=self.r.path)
        self.assertEqual(rc, 0, out)
        self.assertIn("RED BLOCKED ON OWNER", out)

    def test_another_fixers_lease_still_refuses_a_block(self):
        _it, path = self.held_by_a_fixer("s-other")
        os.environ.update(CLAUDE_CODE_SESSION_ID="s-mine", AGENTCTL_FIXER_RECORD=path)  # a spare's leaked environment
        rc, out = self.tool("red", "block", "20", "--owner-cause", "credentials", "--cmd", "set the key",
                            "--until-http", "http://127.0.0.1:9/x", "--repo", "movies", cwd=self.r.path)
        self.assertEqual(rc, 1, out)
        self.assertIn("a live fixer is on #20", out)

    def test_a_family_owner_blocks_a_red_handed_to_it_on_the_owner(self):
        # issues 20261106T210149-680984, 20261119T015532-983695: the convergence family owner (the lease holder,
        # `laptop:owner:convergence`) ran `red block N --owner-cause deploy`: "NOT BLOCKED: a live fixer is on #N:
        # laptop:owner:convergence" -- the live fixer IS the caller. red_block only counts a lease its own record names.
        self.main_red(40, 20, kind="infra")
        self.tick()
        it = self.only()
        took = self.st.acquire(A.red_lease(it["data"]["key"]), "laptop:owner:convergence", A.OWNER_TTL, A.OWNER_MAX,
                               progress="start", meta={"kind": "owned", "family": "convergence"})
        path = A.owner_record_path("convergence")
        os.makedirs(os.path.dirname(path), exist_ok=True)
        A.write_json(path, {"lease": A.owner_lease("convergence"), "family": "convergence", "session": "s-owner",
                            "started": "2026-10-29T00:00:00Z"})
        A.bind_fixer_session("s-owner", path)  # the owner wrapper binds its session (owner.py: bind_fixer_session)
        os.environ.update(CLAUDE_CODE_SESSION_ID="s-owner", AGENTCTL_FIXER_RECORD=path)
        self.assertEqual(A.own_fixer_record_path(), path)
        rc, out = self.tool("red", "block", "20", "--owner-cause", "deploy", "--cmd", "deploy it",
                            "--until-http", "http://127.0.0.1:9/x", "--repo", "movies", cwd=self.r.path)
        self.assertEqual(rc, 0, out)
        self.assertIn("RED BLOCKED ON OWNER", out)

    def test_a_block_by_run_number_is_the_reds_workflow_not_another_workflows_run_of_that_number(self):
        # issue 20261107T072345-7bca40: `red block 14` for Verify scrape recording #14 answered "NOT BLOCKED: Report run
        # timings is green already (#16)": resolve_run took the first workflow having a run numbered 14, and red_block
        # kept that name (`name or known[0]...workflow`) although the known red says which workflow it is.
        it, path = self.held_by_a_fixer("s-mine")
        os.environ.update(CLAUDE_CODE_SESSION_ID="s-mine", AGENTCTL_FIXER_RECORD=path)
        other = run(77, 20, "Report run timings", "timings.yml", self.sha)
        A._gate_lists.clear(), A._outside_lists.clear()
        self.addCleanup(lambda: (A._gate_lists.clear(), A._outside_lists.clear()))
        A._outside_lists["Report run timings"] = [other]
        rc, out = self.tool("red", "block", "20", "--owner-cause", "credentials", "--cmd", "set the key",
                            "--repo", "movies", cwd=self.r.path)
        self.assertNotIn("Report run timings", out)
        self.assertEqual(rc, 0, out)

    def test_a_resumed_fixers_first_claim_binds_its_record_though_the_record_still_names_the_old_session(self):
        # issue 20261119T025305-21375d (core #1053, fixer-3/-4): a `resume` dispatch starts its record with the PREVIOUS
        # fixer's session (red_dispatch: "session": resume["session"]); `claude --bg --resume` runs a NEW session (new id,
        # its bg id the id's first 8 characters) and the wrapper writes that id into the record only when its first poll
        # sees it. The fixer's own first command `red claim N --fixer <record>` runs before: "no live fixer record of
        # this session (7d0ab9e2)", and the fixer's red work went unclaimed.
        it, path = self.held_by_a_fixer("0e110e5f-old-session")
        rec = A.read_record(path)
        A.write_json(path, dict(rec, resume=True, bg_id="7d0ab9e2"))  # the new bg session's id, not yet in `session`
        os.environ.update(CLAUDE_CODE_SESSION_ID="7d0ab9e2-8e4f-443a-9fd7-45609d696377")
        os.environ.pop("AGENTCTL_FIXER_RECORD", None)
        rc, out = self.tool("red", "claim", "20", "--fixer", path, "--repo", "movies", cwd=self.r.path)
        self.assertNotIn("NOT CLAIMED", out)
        self.assertEqual(rc, 0, out)

    def test_a_locked_vault_block_needs_no_owner_cause_and_waits_on_the_vault(self):
        # issue 20261119T015532-983695: "no owner-cause for a locked vault" -- a `secrets unlock <ns>` block joins
        # the vault's ONE wait (red_vault_block), lifted by the owner's `decide vault-<ns> here`
        self.tick()
        rc, out = self.tool("red", "block", "20", "--cmd", "secrets unlock movies", "--repo", "movies",
                            cwd=self.r.path)
        self.assertEqual(rc, 0, out)
        self.assertIn("waits on the movies vault", out)
        self.assertEqual(A.lease_meta(self.st.get(A.block_lease(cause="vault-movies")))["vault"], "movies")
        self.tick()
        self.assertEqual(self.only()["state"], "blocked-external")

    def test_a_fixer_never_notes_on_a_record_of_another_session(self):
        # a `claude --bg` spare spawned for #2432's fixer, reused for #2426's: its AGENTCTL_FIXER_RECORD is #2432's
        _it, theirs = self.held_by_a_fixer("s-other", run="32")
        os.makedirs(A.reds_dir("20"), exist_ok=True)
        mine = A.reds_dir("20", "fixer-2.json")
        A.write_json(mine, {"run": 20, "lease": "red/x", "session": "s-mine", "started": "2026-10-29T00:01:00Z"})
        os.environ.update(CLAUDE_CODE_SESSION_ID="s-mine", AGENTCTL_FIXER_RECORD=theirs)
        self.assertEqual(A.own_fixer_record_path(), mine)
        rc, out = self.tool("red", "claim", "20", "--note", "on #20", "--repo", "movies", cwd=self.r.path)
        self.assertEqual(rc, 0, out)
        self.assertEqual(A.read_record(mine)["note"], "on #20")
        self.assertIsNone(A.read_record(theirs).get("note"))

    def test_escalation_counts_one_per_open_red_and_none_for_reds_a_cause_holds(self):
        os.makedirs(A.reds_dir(), exist_ok=True)
        open(A.reds_dir("DISPATCH_OFF"), "w").close()
        for _ in range(3):  # three cycles of no taker: three escalate events, ONE escalated red
            self.tick(dispatch=True, advance=A.RED_OFFER["gate"] + 1)
        es = A.escalated_now(self.st)
        self.assertEqual(len(es), 1)
        self.assertTrue(A.escalation_line(es).startswith("ESCALATED: 1 (Main #20"))
        A.project_events(self.st)  # the projector writes the escalations into the inbox; the notifier tells
        self.assertTrue(any(str(e.get("verdict")).startswith("RED UNCLAIMED") for e in A.inbox_entries()))
        # one live notification per item (2026-11-03, W6 152): three re-escalations of one red pending at once are ONE
        self.assertEqual(A.notify_escalations(self.st), 1)
        self.assertEqual(A.notify_escalations(self.st), 0)  # once
        wt = self.r.worktree("repo-wt-taker", branch="taker")
        self.assertEqual(self.tool("red", "claim", "20", "--repo", "movies", cwd=wt)[0], 0)
        self.t[0] = A.epoch_now()
        self.tick()
        self.assertEqual(A.escalated_now(self.st), [])  # held now: the escalation is gone by itself

    def test_red_unclaimed_is_never_told_once_the_red_has_a_holder_and_a_short_lapse_is_no_new_unclaimed(self):
        """2026-11-03: the owner got "RED UNCLAIMED: Main #2123 ... no taker" (and core #699) an hour after the reds were
        leased -- the notifier, hours behind, told every queued escalation. One escalated before its lease and still
        pending when the notifier runs is closed untold; a lapse re-leased within the offer's grace is no UNCLAIMED."""
        os.makedirs(A.reds_dir(), exist_ok=True)
        open(A.reds_dir("DISPATCH_OFF"), "w").close()
        for _ in range(3):
            self.tick(dispatch=True, advance=A.RED_OFFER["gate"] + 1)
        self.assertTrue(any(str(e.get("text")).startswith("RED UNCLAIMED") and not e.get("notified_at")
                            for e in self.st.events(limit=10 ** 6)))
        wt = self.r.worktree("repo-wt-holder", branch="holder")
        self.assertEqual(self.tool("red", "claim", "20", "--repo", "movies", cwd=wt)[0], 0)
        self.t[0] = A.epoch_now()
        self.tick()
        self.assertEqual(self.only()["state"], "leased")
        shown = []
        saved = A.notify
        A.notify = lambda title, text, **kw: shown.append(text) or True
        try:
            A.notify_escalations(self.st)
        finally:
            A.notify = saved
        self.assertEqual([x for x in shown if "UNCLAIMED" in x], [], shown)
        self.assertFalse([e for e in self.st.events(limit=10 ** 6) if e["severity"] == "escalate" and not e.get("notified_at")])
        n = len(self.st.events(limit=10 ** 6))
        self.assertEqual(self.tool("red", "release", "20", "--repo", "movies", cwd=wt)[0], 0)
        self.t[0] = A.epoch_now()
        self.tick(dispatch=True)
        self.tick(dispatch=True, advance=A.RED_OFFER["gate"] - 120)  # lapsed, inside the offer's grace
        self.assertEqual(self.tool("red", "claim", "20", "--repo", "movies", cwd=wt)[0], 0)
        self.t[0] = A.epoch_now() + A.RED_OFFER["gate"] - 60
        self.tick(dispatch=True, advance=0)
        self.tick(dispatch=True, advance=A.RED_OFFER["gate"] + 1)
        self.assertFalse([e for e in self.st.events(limit=10 ** 6)[n:] if "UNCLAIMED" in str(e.get("text"))])


class LadderTest(RedsBase):
    def test_the_ladder_author_then_fresh_resume_revert_deep(self):
        d = {"kind": "test", "attempts": 0, "author": {"worktree": "/w"}}
        self.assertEqual(A.red_tier(d), "author")
        self.assertEqual(A.red_tier({**d, "author_tried": True}), "fresh")
        self.assertEqual(A.red_tier({"kind": "test", "attempts": 1, "history": [{"session": "s", "worktree": "/w"}]}),
                         "resume")
        self.assertEqual(A.red_tier({"kind": "test", "attempts": 2}, culprit="abc"), "revert")
        self.assertEqual(A.red_tier({"kind": "test", "attempts": 2, "reverted": {"ok": True}}, culprit="abc"), "fresh")
        self.assertEqual(A.red_tier({"kind": "test", "attempts": 5}), "deep")
        self.assertEqual(A.red_tier({"kind": "infra", "attempts": 0}), "infra")


class AuthorTest(RedsBase):
    def test_the_author_comes_from_the_change_item_never_the_inherited_environment(self):
        wt = self.r.worktree("repo-wt-author", branch="author")
        d, _c, _w = A.waiting_state(self.r.path)
        os.makedirs(os.path.join(d, "entries"), exist_ok=True)
        A.write_json(os.path.join(d, "entries", "e1.json"), {"id": "e1", "seq": 1, "status": "landed", "landed_sha": self.sha,
                                                             "worktree": wt, "session": "sess-author", "claude_pid": 1})
        os.environ["CLAUDE_PID"] = str(os.getpid())  # an inherited pid names nobody
        try:
            heads = A.landed_heads(self.r.path)
        finally:
            os.environ.pop("CLAUDE_PID")
        self.assertEqual(heads[self.sha], {"worktree": wt, "session": "sess-author"})


class DispatchTest(RedsBase):
    def test_the_dispatcher_takes_the_offer_its_fixer_holds_the_lease_and_its_verdict_reoffers(self):
        os.environ["FAKE_CLAUDE_RESULT"] = "GAVE UP: cannot reproduce"
        try:
            self.tick(dispatch=True)
            it = self.only()
            row = self.st.get(A.red_lease(it["data"]["key"]))
            self.assertTrue(row["holder"].startswith("laptop:fixer:"))
            self.assertIsNone(self.st.offer_of(f"red:{it['id']}"))  # take closed it
            self.assertEqual(self.unheld(), [])
            t0 = time.time()
            while A.lease_is_live(self.st.get(A.red_lease(it["data"]["key"])), time.time()) and time.time() - t0 < 120:
                time.sleep(0.2)
        finally:
            os.environ.pop("FAKE_CLAUDE_RESULT")
        self.t[0] = A.epoch_now()
        self.tick()
        it = self.only()
        self.assertEqual(it["state"], "offered")
        self.assertEqual(it["data"]["attempts"], 1)
        self.assertIn("GAVE UP", it["data"]["history"][-1]["outcome"])
        self.assertEqual(it["data"]["tier"], "resume")  # the same fixer's session, its worktree


    def test_one_run_one_fixer_its_other_reds_taken_with_it(self):
        self.put("run-40.json", {"jobs": [{"name": "a", "status": "completed", "conclusion": "failure",
                                           "steps": [{"name": "Run tests", "conclusion": "failure"}]},
                                          {"name": "b", "status": "completed", "conclusion": "failure",
                                           "steps": [{"name": "Run tests", "conclusion": "failure"}]}],
                                 "headSha": self.sha, "url": "u", "number": 20, "status": "completed",
                                 "conclusion": "failure", "workflowName": "Main"})
        self.put("run-40.log", f"a\tRun tests\t2026-10-27T08:00:00Z {FAIL_LINE}\nb\tRun tests\t2026-10-27T08:00:00Z {OTHER_LINE}\n")
        os.environ["FAKE_CLAUDE_SLEEP"] = "30"
        try:
            self.tick(dispatch=True)
        finally:
            os.environ["FAKE_CLAUDE_SLEEP"] = "0"
        its = [i for i in self.st.items("red")]
        self.assertEqual(len(its), 2)
        holders = {self.st.get(A.red_lease(i["data"]["key"]))["holder"] for i in its}
        self.assertEqual(len(holders), 1, holders)  # both reds under ONE fixer
        self.assertEqual(len(A.fixer_records()), 1)
        self.assertEqual(self.unheld(), [])


    def test_five_ticks_on_one_red_dispatch_exactly_one_fixer(self):
        os.environ["FAKE_CLAUDE_SLEEP"] = "60"
        try:
            for _ in range(5):
                self.tick(dispatch=True, advance=20)
        finally:
            os.environ["FAKE_CLAUDE_SLEEP"] = "0"
        self.assertEqual(len(A.fixer_records()), 1)
        self.assertEqual(self.only()["state"], "leased")

    def test_a_runs_infra_failures_without_error_lines_are_one_red_and_one_fixer(self):
        self.put("run-40.json", {"jobs": [{"name": f"leg ({c})", "status": "completed", "conclusion": "failure",
                                           "steps": [{"name": f"Run leg {c}", "conclusion": "failure"}]}
                                          for c in ("uk", "de", "pl", "es")] + [
                                     {"name": "timings", "status": "completed", "conclusion": "failure",
                                      "steps": [{"name": "Report", "conclusion": "failure"}]}],
                                 "headSha": self.sha, "url": "u", "number": 20, "status": "completed",
                                 "conclusion": "failure", "workflowName": "Main"})
        self.put("run-40.log", "")
        os.environ["FAKE_CLAUDE_SLEEP"] = "60"
        try:
            for _ in range(3):
                self.tick(dispatch=True, advance=20)
        finally:
            os.environ["FAKE_CLAUDE_SLEEP"] = "0"
        it = self.only()
        self.assertEqual(len(it["data"]["jobs"]), 5)
        self.assertEqual(len(A.fixer_records()), 1)

    def test_a_test_red_is_never_cleared_by_its_own_run_reread_only_by_a_newer_decided_main_run(self):
        self.tick()
        old = self.only()
        self.put("run-40.log", f"test (rest)\tRun tests\t2026-10-27T08:00:00Z {OTHER_LINE}\n")
        os.unlink(A.reds_dir("runsig", "40.json"))
        self.tick()  # the same run read again differently: no newer decided run on main, so nothing clears
        self.assertEqual(self.st.item("red", old["id"])["state"], "offered")

    def test_clearing_one_red_of_a_run_never_stops_the_fixer_holding_its_others(self):
        self.tick()
        it = self.only()
        wt = self.r.worktree("repo-wt-h", branch="h")
        self.st.acquire(A.red_lease(it["data"]["key"]), "laptop:fixer:x", 900, 3600, meta={"kind": "fixer", "worktree": wt})
        self.st.acquire("red/otherkey", "laptop:fixer:x", 900, 3600, meta={"kind": "fixer", "worktree": wt})
        self.assertIsNone(A.red_stop_holder(self.st, it["data"]["key"], "x", "red cleared"))
        self.assertFalse(os.path.exists(os.path.join(wt, "RED-STOP.md")))
        self.assertTrue(A.lease_is_live(self.st.get("red/otherkey"), self.t[0]))


class OldRedTest(RedsBase):
    def outside(self, wf_text):
        saved = os.environ.get("AGENTCTL_RED_OLD_RUN")
        os.environ["AGENTCTL_RED_OLD_RUN"] = str(2 * 86400)
        self.addCleanup(os.environ.__setitem__, "AGENTCTL_RED_OLD_RUN", saved or "1e12")
        self.put("runs.json", [run(40, 20, "Main", "main.yml", self.sha), run(39, 19, "Identity model convergence",
                                                                             "identity-model-convergence.yml", self.sha),
                               run(90, 38, "Report run timings", "report-timings.yml", self.sha, conclusion="failure",
                                   created="2026-10-19T21:10:26Z")])
        self.put("run-90.json", {"jobs": [{"name": "report", "status": "completed", "conclusion": "failure",
                                           "steps": [{"name": "Report", "conclusion": "failure"}]}], "headSha": self.sha,
                                 "url": "u", "number": 38, "status": "completed", "conclusion": "failure",
                                 "workflowName": "Report run timings"})
        self.put("run-90.log", "report\tReport\t2026-10-19T21:10:26Z [error] boom\n")
        if wf_text:
            self.r.write(".github/workflows/report-timings.yml", wf_text)
            self.r.commit("wf")
            self.r.push()

    def test_an_old_red_of_a_workflow_that_cannot_run_is_offered_never_retired(self):
        # "Report run timings" #38 (2026-10-29): an old red no fresh run could check was retired, re-opened by the next
        # tick's same observation and retired again 811 times, never offered, "offering" in ci-why for 9 days
        self.outside("name: Report run timings\non:\n  schedule:\n    - cron: \"0 3 * * *\"\n")
        for _ in range(4):
            self.tick(advance=60)
        self.assertEqual(len(self.st.items("red")), 1)  # one spell, no churn
        it = self.only()
        self.assertEqual(it["state"], "offered")
        self.assertTrue(it["data"]["old_checked"])
        self.assertIn("workflow_dispatch", it["data"]["old_note"])
        self.assertIsNotNone(self.st.offer_of(f"red:{it['id']}"))  # the dispatcher takes it
        self.assertEqual(self.unheld(), [])

    def made_callable_only(self, caller_runs, other_callers=(), message="callable only, called by Nightly"):
        # "Report run timings" #38 (2026-10-19..29): a `workflow_call.secrets` block Forgejo 15 rejected, fixed that
        # night by 32d1805de, red on main for 10 days: a callable-only workflow never runs on its own to clear it
        self.outside("name: Report run timings\non:\n  schedule:\n    - cron: \"0 3 * * *\"\n")
        self.tick()
        it = self.only()
        self.r.write(".github/workflows/report-timings.yml", "name: Report run timings\non:\n  workflow_call: {}\n")
        self.r.write(".github/workflows/nightly.yml", "name: Nightly\non:\n  schedule:\n    - cron: \"0 1 * * *\"\n"
                     "jobs:\n  timings:\n    uses: ./.github/workflows/report-timings.yml\n")
        self.head = self.r.commit(message)
        self.r.push()
        self.caller_runs(caller_runs, other_callers)
        for _ in range(4):
            self.tick(advance=60)
        return it

    def caller_runs(self, caller_runs, other_callers=()):
        runs, head = json.load(open(os.path.join(self.d, "runs.json"))), self.head
        self.put("runs.json", [run(100 + i, 50 + i, "Nightly", "nightly.yml", head if after else self.sha, conclusion=c)
                               for i, (after, c) in enumerate(caller_runs)] +
                 [run(200 + i, 70 + i, n, f, head, conclusion=c) for i, (n, f, c) in enumerate(other_callers)] + runs)

    def test_a_callable_only_workflows_red_is_verified_once_its_callers_pass_with_its_changed_file(self):
        it = self.made_callable_only([(True, "success")])
        reds = self.st.items("red")
        self.assertEqual([(i["id"], i["state"]) for i in reds], [(it["id"], "verified")])  # one spell, never re-opened
        self.assertEqual(reds[0]["data"]["settled_run"], 90)
        self.assertTrue(any("its callers' runs since passed: Nightly #50" in str(e.get("text")) for e in self.st.events()))
        self.assertIsNone(self.st.offer_of(f"red:{it['id']}"))
        self.assertEqual(self.unheld(), [])

    def test_a_callable_only_workflows_red_is_verified_by_a_green_caller_whatever_its_other_callers_do(self):
        # Report run timings has 36 callers on main and some are red at any hour for their own reasons (2026-10-29:
        # Archive recording #823, Verify scrape recording #2493): with "none failed", #38 stayed red under f8f51e8
        self.r.write(".github/workflows/weekly.yml", "name: Weekly\non:\n  schedule:\n    - cron: \"0 2 * * 1\"\n"
                     "jobs:\n  timings:\n    uses: ./.github/workflows/report-timings.yml\n")
        it = self.made_callable_only([(True, "success")], other_callers=[("Weekly", "weekly.yml", "failure")])
        self.assertEqual([(i["id"], i["state"]) for i in self.st.items("red") if i["data"]["workflow"] ==
                          "Report run timings"], [(it["id"], "verified")])
        self.assertTrue(any("its callers' runs since passed: Nightly #50" in str(e.get("text")) for e in self.st.events()))

    def test_a_callable_only_workflows_landed_fix_is_verified_by_its_callers_runs_not_left_pending(self):
        # its fixer's own `Fixes-Red: 38` parks the red in fix-pending, where no dispatch can verify a workflow_call-only
        # workflow: unless its callers' runs are read there, it waits RED_FIX_GIVEUP and goes back to a fixer
        it = self.made_callable_only([], message="callable only, called by Nightly\n\nFixes-Red: 38")
        self.assertEqual(self.only()["state"], "fix-pending")
        self.caller_runs([(True, "success")])
        self.tick(advance=60)
        self.assertEqual([(i["id"], i["state"]) for i in self.st.items("red") if i["data"]["workflow"] ==
                          "Report run timings"], [(it["id"], "verified")])
        self.assertTrue(any("its callers' runs since passed: Nightly #50" in str(e.get("text")) for e in self.st.events()))

    def test_a_settled_callable_only_run_reads_verified_and_leaves_the_status_line(self):
        # Report run timings #38 (2026-10-29): verified at 16:17Z by its callers' runs, yet still its newest run on main,
        # so ci-why and the status line read it "offering" -- "offered at the next tick", which never comes
        self.made_callable_only([(True, "success")])
        snap = self.snap()
        self.assertEqual([o["name"] for o in snap["outside"]], ["Report run timings"])  # still observed, never re-opened
        summary = A.ci_why(MOVIES, summary=True, snap=snap, repo=self.r.path, store=A.process_store())[1]
        self.assertNotIn("Report run timings", summary)
        full = A.ci_why(MOVIES, snap=snap, repo=self.r.path, store=A.process_store())[1]
        self.assertNotIn("offered at the next tick", full)
        self.assertIn("fixing: verified: Report run timings runs only when called", full)

    def test_a_callable_only_workflows_red_stays_offered_without_a_passing_caller_run_since_its_change(self):
        # the trigger alone never closes a red (5cf3f69: 811 re-opened retirements); no caller run since: no proof
        it = self.made_callable_only([(False, "success")])
        self.assertEqual((self.only()["id"], self.only()["state"]), (it["id"], "offered"))
        self.assertEqual(self.unheld(), [])

    def test_a_callable_only_workflows_red_stays_offered_while_a_caller_run_since_its_change_fails(self):
        self.made_callable_only([(True, "failure")])
        mine = [i for i in self.st.items("red") if i["data"]["workflow"] == "Report run timings"]
        self.assertEqual([i["state"] for i in mine], ["offered"])

    def test_an_old_red_whose_fresh_run_is_refused_is_offered_not_left_verifying(self):
        # Archive recording #823 (2026-10-29): the verify dispatch was refused (HTTP 400, a required input) and the red
        # sat in fix-pending "fix None verify" for 19 h with no fixer
        self.outside("name: Report run timings\non:\n  workflow_dispatch:\n    inputs:\n      runs: {required: true}\n")
        saved = (A.api, A.record_dispatch)

        def refuse(o_r, method, path, params=None, body=None):  # the dispatch refused; reads stay real
            if method == "GET":
                return saved[0](o_r, method, path, params, body)
            raise RuntimeError("HTTP Error 400: Bad Request")
        A.api, A.record_dispatch = refuse, (lambda **kw: None)
        try:
            self.tick()
            self.tick(advance=60)
        finally:
            A.api, A.record_dispatch = saved
        it = self.only()
        self.assertEqual(it["state"], "offered")
        self.assertIn("HTTP Error 400", it["data"]["old_note"])
        self.assertEqual(self.unheld(), [])

    def stuck_verify(self, verify):
        self.outside("name: Report run timings\non:\n  schedule:\n    - cron: \"0 3 * * *\"\n")
        self.tick()
        it = self.only()
        A.red_move(self.st, it, "fix-pending", "as recorded live", data={
            "old_checked": True, "fix": {"status": "verify", "at": self.t[0], "entry": None, "sha": "",
                                         "verify_dispatched": A.ts(), "verify": verify}})
        return self.only()

    def test_a_fix_pending_whose_verify_was_refused_is_offered_again_at_the_next_tick(self):
        it = self.stuck_verify("FIX UNVERIFIED: could not dispatch Archive recording (HTTP Error 400: Bad Request)")
        self.assertEqual([i["id"] for i in self.unheld()], [it["id"]])  # the invariant sees it
        self.tick(advance=60)
        it = self.only()
        self.assertEqual((it["state"], it["data"]["fix"]), ("offered", None))
        self.assertTrue(any(e["severity"] == "escalate" and "UNVERIFIED" in str(e.get("text")) for e in self.st.events()))
        self.assertIn("UNVERIFIED", it["data"]["history"][-1]["outcome"])
        self.assertEqual(self.unheld(), [])

    def test_a_fix_pending_whose_verify_run_never_comes_is_offered_again_after_the_give_up_window(self):
        self.stuck_verify("dispatched Report run timings on main to verify ")
        self.tick(advance=60)
        self.assertEqual(self.only()["state"], "fix-pending")  # within its window: the verify run may still come
        self.tick(advance=A.RED_FIX_GIVEUP)
        it = self.only()
        self.assertEqual((it["state"], it["data"]["fix"]), ("offered", None))
        self.assertEqual(self.unheld(), [])

    def test_the_invariant_reoffers_any_fix_pending_with_no_fix(self):
        it = self.stuck_verify("dispatched")
        A.red_move(self.st, it, "fix-pending", "fix None", data={"fix": None})
        self.assertEqual(len(self.unheld()), 1)
        acts = A.red_enforce(self.st, {"entries": [], "landed": []}, self.t[0])
        self.assertTrue(acts)
        self.assertEqual(self.only()["state"], "offered")
        self.assertEqual(self.unheld(), [])

    def test_an_old_red_of_a_dispatchable_workflow_is_verified_by_a_fresh_run(self):
        self.outside("name: Report run timings\non:\n  workflow_dispatch:\n")
        calls, saved = [], (A.api, A.record_dispatch)
        A.api = lambda o_r, method, path, params=None, body=None: (  # reads stay real: the run lists are REST reads
            saved[0](o_r, method, path, params, body) if method == "GET" else calls.append((method, path)))
        A.record_dispatch = lambda **kw: None
        try:
            self.tick(dispatch=True)
        finally:
            A.api, A.record_dispatch = saved
        it = self.only()
        self.assertEqual((it["state"], it["data"]["fix"]["status"]), ("fix-pending", "verify"))
        self.assertIn(("POST", "actions/workflows/report-timings.yml/dispatches"), calls)


    def test_an_old_reds_fresh_run_repeats_the_failing_runs_inputs(self):
        # Archive recording #823 (2026-10-29): dispatched with no inputs, its required `runs` input was refused (HTTP 400)
        self.outside("name: Report run timings\non:\n  workflow_dispatch:\n    inputs:\n      runs: {required: true}\n")
        calls, saved = [], (A.api, A.record_dispatch)
        A.write_json(A.dispatches_dir("inputs", "90.json"), {"runs": "528 573"})  # `dispatch run` recorded them

        def api(o_r, method, path, params=None, body=None):
            if method == "GET":  # reads stay real: the run lists are REST reads
                return saved[0](o_r, method, path, params, body)
            calls.append((method, path, body))
            return None
        A.api, A.record_dispatch = api, (lambda **kw: None)
        try:
            self.tick(dispatch=True)
        finally:
            A.api, A.record_dispatch = saved
        it = self.only()
        self.assertEqual((it["state"], it["data"]["fix"]["status"]), ("fix-pending", "verify"))
        self.assertEqual(it["data"]["last_run"]["databaseId"], 90)
        self.assertIn(("POST", "actions/workflows/report-timings.yml/dispatches",
                       {"ref": "main", "inputs": {"runs": "528 573"}}), calls)
        self.assertIn('with {"runs": "528 573"}', it["data"]["fix"]["verify"])

    def test_a_refused_verify_dispatch_names_the_forges_reason(self):
        import email.message, io as _io, urllib.error
        e = urllib.error.HTTPError("http://f/x", 400, "Bad Request", email.message.Message(),
                                   _io.BytesIO(b'{"message":"Require value for input: runs"}'))
        self.assertEqual(A.http_error_text(e), 'HTTP Error 400: Bad Request: {"message":"Require value for input: runs"}')


class AttachTest(RedsBase):
    def test_watch_red_attaches_by_run_number_with_its_author(self):
        its = A.red_attach(self.st, "Main", 20, author={"session": "s1", "worktree": "/w"})
        self.assertEqual(len(its), 1)
        self.assertEqual(self.st.item("red", its[0]["id"])["data"]["author"]["session"], "s1")


class UnkickedTest(RedsBase):
    def test_a_relevant_main_push_with_no_convergence_run_is_a_red_until_one_runs(self):
        self.green(41, 21)  # Main green; convergence #19 (on self.sha) is its newest main run
        self.r.write(".github/convergence-paths.txt", "worker/**\n")
        self.r.write(".github/actions/changed-paths/matches.sh",
                     "#!/usr/bin/env bash\ngrep -q '^worker/' && echo true || echo false\n")
        self.r.commit("the gate's files")
        self.r.write("worker/x.scala", "x\n")
        head = self.r.commit("a pipeline change")
        self.r.push()
        os.environ["AGENTCTL_UNKICKED_AFTER"] = "0"
        try:
            self.tick()
            its = [i for i in self.st.items("red") if i["data"]["job"] == "kick"]
            self.assertEqual([i["state"] for i in its], ["offered"])
            self.assertIn(head[:10], its[0]["data"]["line"])
            runs = json.load(open(os.path.join(self.d, "runs.json")))
            later = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(A.epoch_now() + 60))
            self.put("runs.json", [run(99, 30, "Identity model convergence", "identity-model-convergence.yml", head,
                                       status="in_progress", created=later)] + runs)
            self.tick()
        finally:
            os.environ.pop("AGENTCTL_UNKICKED_AFTER")
        self.assertEqual([i["state"] for i in self.st.items("red") if i["data"]["job"] == "kick"], ["cleared"])


    def test_a_relevant_main_push_with_a_convergence_run_but_no_identity_takes_run_is_a_red(self):
        """2026-11-08: convergence's preflight dispatches Identity takes on the same ref: a run of convergence with none
        of Identity takes since the push is its own unkicked red."""
        takes = ("Identity takes", "identity-takes.yml")
        self.put("runs.json", json.load(open(os.path.join(self.d, "runs.json"))) + [run(38, 18, *takes, self.sha)])
        self.green(41, 21)
        self.r.write(".github/convergence-paths.txt", "worker/**\n")
        self.r.write(".github/actions/changed-paths/matches.sh",
                     "#!/usr/bin/env bash\ngrep -q '^worker/' && echo true || echo false\n")
        self.r.commit("the gate's files")
        self.r.write("worker/x.scala", "x\n")
        head = self.r.commit("a pipeline change")
        self.r.push()
        later = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(A.epoch_now() + 60))
        runs = json.load(open(os.path.join(self.d, "runs.json")))
        self.put("runs.json", [run(99, 30, "Identity model convergence", "identity-model-convergence.yml", head,
                                   status="in_progress", created=later)] + runs)
        os.environ["AGENTCTL_UNKICKED_AFTER"] = "0"
        try:
            self.tick()
            its = [i for i in self.st.items("red") if i["data"]["job"] == "kick"]
            self.assertEqual([(i["state"], i["data"]["workflow"]) for i in its], [("offered", "Identity takes")])
            self.assertIn("convergence's preflight did not dispatch it", its[0]["data"]["line"])
            self.put("runs.json", [run(98, 29, *takes, head, status="in_progress", created=later)]
                     + json.load(open(os.path.join(self.d, "runs.json"))))
            self.tick()
        finally:
            os.environ.pop("AGENTCTL_UNKICKED_AFTER")
        self.assertEqual([i["state"] for i in self.st.items("red") if i["data"]["job"] == "kick"], ["cleared"])


class SiblingTest(RedsBase):
    def test_a_red_appearing_in_a_run_its_fixer_holds_is_leased_to_that_fixer_and_ends_with_it(self):
        self.tick()
        a = self.only()
        took = self.st.acquire(A.red_lease(a["data"]["key"]), "laptop:fixer:red-a-1", 900, 3600,
                               meta={"kind": "fixer", "gate": True, "workflow": "Main", "run": 20})
        self.tick()
        # a newer read of the same run shows a second failure: the run's fixer holds it at once
        self.put("run-40.json", {"jobs": [{"name": "test (rest)", "status": "completed", "conclusion": "failure",
                                           "steps": [{"name": "Run tests", "conclusion": "failure"}]},
                                          {"name": "b", "status": "completed", "conclusion": "failure",
                                           "steps": [{"name": "Run tests", "conclusion": "failure"}]}],
                                 "headSha": self.sha, "url": "u", "number": 20, "status": "completed",
                                 "conclusion": "failure", "workflowName": "Main"})
        self.put("run-40.log", f"test (rest)\tRun tests\t2026-10-27T08:00:00Z {FAIL_LINE}\n"
                               f"b\tRun tests\t2026-10-27T08:00:00Z {OTHER_LINE}\n")
        os.unlink(A.reds_dir("runsig", "40.json"))
        for _ in range(3):
            self.tick(dispatch=True)
        b = [i for i in self.st.items("red") if i["data"]["line"] == OTHER_LINE][0]
        self.assertEqual(b["state"], "leased")
        self.assertEqual(self.st.get(A.red_lease(b["data"]["key"]))["holder"], "laptop:fixer:red-a-1")
        self.assertEqual(A.escalated_now(self.st), [])
        self.assertNotIn("offered", A.red_view(self.st, b)[0])
        self.st.release(A.red_lease(a["data"]["key"]), took["epoch"], took["secret"], outcome="GAVE UP: x")
        self.tick()
        self.tick()
        b = self.st.item("red", b["id"])
        self.assertEqual(b["state"], "offered")  # its fixer ended: offered again with it


class BackgroundSessionTest(RedsBase):
    """The owner (2026-10-29): "I want fixers to be visible in ui": a fixer is a `claude --bg -n <label>` session."""

    def sessions(self):
        r = subprocess.run([os.environ["AGENTCTL_CLAUDE"], "agents", "--json", "--all"], capture_output=True, text=True)
        return json.loads(r.stdout)

    def wait(self, cond, limit=30):
        t0 = time.time()
        while not cond() and time.time() - t0 < limit:
            time.sleep(0.1)
        return cond()

    def test_a_fixer_is_a_named_session_renewed_by_its_transcript_and_stopped_when_its_lease_is_revoked(self):
        os.environ.update(FAKE_CLAUDE_SLEEP="60", AGENTCTL_RED_RENEW="0.3")
        try:
            self.tick(dispatch=True)
            it = self.only()
            name = A.red_lease(it["data"]["key"])
            rec = lambda: (A.fixer_records() or [{}])[0]
            self.assertTrue(self.wait(lambda: rec().get("transcript")))
            [s] = self.sessions()
            self.assertEqual((s["name"], s["state"]), ("red fixer #20 · main test (rest)", "working"))
            # the wrapper sends the lease its meta before the record names the session (the test just below)
            self.assertTrue(self.wait(lambda: A.lease_meta(self.st.get(name)).get("session") == s["sessionId"]))  # a resume goes back to it
            argv = json.load(open(os.path.join(os.environ["FAKE_CLAUDE_OUT"], f"{s['id']}.json")))["argv"]
            self.assertNotIn("-p", argv)
            self.assertIn("Bash(git push:*)", argv)
            before = self.st.get(name)["progress"]
            with open(rec()["transcript"], "a") as f:  # the session works: its transcript grows
                f.write(json.dumps({"type": "assistant", "message": {"content": [{"type": "text", "text": "x"}]}}) + "\n")
            self.assertTrue(self.wait(lambda: self.st.get(name)["progress"] != before))
            self.st.revoke(name, "RED CLEARED")
            self.assertTrue(self.wait(lambda: rec().get("ended")))
            [s] = self.sessions()
            self.assertEqual((s["state"], s.get("stopped")), ("done", True))  # `claude stop`, never a kill
            self.assertTrue(rec()["verdict"].startswith("STOPPED"), rec()["verdict"])
        finally:
            os.environ.update(FAKE_CLAUDE_SLEEP="0")
            os.environ.pop("AGENTCTL_RED_RENEW", None)

    def test_the_record_never_names_the_session_before_its_lease_carries_it(self):
        # 2026-11-25 run 45702726773: a reader that saw the record's session found the lease's meta session None, the
        # meta going out at the wrapper's next step. The wrapper sends the meta first: at the moment the lease takes it,
        # the record on disk does not name the session yet; and once the record does, the lease carries it.
        path = os.path.join(self.tmp.name, "fixer.json")
        A.write_json(path, {"run": 20, "workflow": "Main", "worktree": self.tmp.name, "log": "l", "kind": "test"})
        seen = []

        class Spy:
            def set_meta(spy, name, epoch, secret, meta):
                seen.append((A.read_record(path).get("session"), meta.get("session")))

        w = A.RedFixerWrapper(types.SimpleNamespace(record=path), Spy())
        w.held, w.meta, w.bid = [types.SimpleNamespace(triple=lambda: ("n", 1, "s"))], {}, "b"
        saved = A.bg_transcript
        A.bg_transcript = lambda sid: "/t.jsonl"
        try:
            w.adopt_transcript({"sessionId": "sess-1"})
        finally:
            A.bg_transcript = saved
        self.assertEqual(seen, [(None, "sess-1")])  # the lease took it while the record still named nothing
        self.assertEqual((A.read_record(path)["session"], w.meta_due), ("sess-1", False))

    def test_the_verdict_is_the_sessions_final_answer_and_the_session_is_left_listed(self):
        os.environ["FAKE_CLAUDE_RESULT"] = "GAVE UP: cannot reproduce"
        try:
            self.tick(dispatch=True)
            self.assertTrue(self.wait(lambda: (A.fixer_records() or [{}])[0].get("ended")))
        finally:
            os.environ.pop("FAKE_CLAUDE_RESULT")
        self.assertEqual(A.fixer_records()[0]["verdict"], "GAVE UP: cannot reproduce")
        self.assertEqual([s["state"] for s in self.sessions()], ["done"])

    def test_a_store_that_drops_requests_never_ends_the_wrapper_nor_strands_its_lease(self):
        # 2026-10-29, #2517: the wrapper died on `POST /v1/store/get: [Errno 54] Connection reset by peer` (ci-runner's
        # store, through the tunnel) a minute after it started its session: nothing renewed the lease, it lapsed, and a
        # second fixer was dispatched onto the red the first was still working on
        self.tick()
        it = self.only()
        name = A.red_lease(it["data"]["key"])
        took = self.st.acquire(name, f"laptop:fixer:red-{it['data']['key']}-1", A.RED_FIXER_TTL, A.RED_FIXER_MAX,
                               progress="start", meta={"kind": "fixer"})
        os.makedirs(A.reds_dir("20"), exist_ok=True)
        path = A.reds_dir("20", "fixer.json")
        A.write_json(path, {"run": 20, "run_id": 40, "workflow": "Main", "job": "test (rest)", "kind": "test",
                            "lease": name, "epoch": took["epoch"], "worktree": self.r.worktree("repo-wt-red-20"),
                            "started": A.ts(), "log": A.reds_dir("20", "claude-1.log")})
        # the 2nd set_meta: the session's id, sent when `claude agents` first lists it
        flaky = DroppingStore(self.st, get={2, 3, 4}, set_meta={2}, renew={1}, release={1})
        saved = {sg: signal.getsignal(sg) for sg in (signal.SIGTERM, signal.SIGHUP, signal.SIGINT)}
        os.environ.update(FAKE_CLAUDE_SLEEP="10", AGENTCTL_RED_RENEW="0.3", AGENTCTL_LEASE_SECRET=took["secret"])
        try:
            rc = A.red_fixer(types.SimpleNamespace(record=path, repo=self.r.path), flaky)
        finally:
            for sg, h in saved.items():
                signal.signal(sg, h)
            os.environ.update(FAKE_CLAUDE_SLEEP="0")
            os.environ.pop("AGENTCTL_RED_RENEW", None)
            os.environ.pop("AGENTCTL_LEASE_SECRET", None)
        rec = json.load(open(path))
        self.assertEqual((rc, rec["verdict"]), (0, "LANDED abcdef1234"))
        last_drop = max(i for i, (m, r) in enumerate(flaky.calls) if r == "dropped" and m != "release")
        self.assertIn(("renew", "ok"), flaky.calls[last_drop + 1:], flaky.calls)  # renewed once the store answered
        lease = self.st.get(name)
        self.assertFalse(A.lease_is_live(lease, A.epoch_now()))  # released with its verdict: the release was asked again
        self.assertEqual(A.lease_meta(lease)["outcome"], "LANDED abcdef1234")
        self.assertEqual(A.lease_meta(lease)["session"], rec["session"])  # sent again: a resume goes back to it

    def test_labels(self):
        self.assertEqual(A.fixer_label({"run": 2392, "workflow": "Identity model convergence",
                                        "job": "takes-drift-2 (united-states)"}),
                         "red fixer #2392 · convergence takes-drift-2 (united-states)")
        self.assertEqual(A.fixer_label({"orphan": True, "worktree": "/x/movies-wt-proxy-park"}),
                         "orphan · movies-wt-proxy-park")


class QuickExitTest(RedsBase):
    def test_a_fixer_that_never_gets_going_is_no_attempt_and_its_red_backs_off(self):
        os.environ["FAKE_CLAUDE_RESULT"] = ""  # no verdict line at all: `exit 0 with no LANDED / GAVE UP line`
        try:
            self.tick(dispatch=True)
            it = self.only()
            t0 = time.time()
            while A.lease_is_live(self.st.get(A.red_lease(it["data"]["key"])), time.time()) and time.time() - t0 < 60:
                time.sleep(0.2)
            self.t[0] = A.epoch_now()
            self.tick(dispatch=True)
            self.tick(dispatch=True)
        finally:
            os.environ.pop("FAKE_CLAUDE_RESULT")
        it = self.only()
        self.assertEqual((it["state"], it["data"]["attempts"]), ("offered", 0))
        self.assertEqual(len(A.fixer_records()), 1)  # no instant re-dispatch
        self.assertIn("never got going", self.st.offer_of(f"red:{it['id']}")["reason"])


    def test_a_fixer_that_meets_a_spent_usage_limit_waits_for_its_reset_and_says_so(self):
        """2026-11-23..24: three launches in a row ended on `You've hit your weekly limit` and were held as a harness
        fault; the limit is an outside wait -- no attempt, no crash, and the offer names it until it resets."""
        os.environ["FAKE_CLAUDE_RESULT"] = "You've hit your weekly limit · resets 2am (UTC)"
        try:
            self.tick(dispatch=True)
            it = self.only()
            t0 = time.time()
            while A.lease_is_live(self.st.get(A.red_lease(it["data"]["key"])), time.time()) and time.time() - t0 < 60:
                time.sleep(0.2)
            self.t[0] = A.epoch_now()
            self.tick(dispatch=True)
            self.tick(dispatch=True)
        finally:
            os.environ.pop("FAKE_CLAUDE_RESULT")
        it = self.only()
        self.assertEqual((it["state"], it["data"]["attempts"]), ("offered", 0))
        self.assertTrue(it["data"]["history"][-1]["outcome"].startswith("LIMITED: "), it["data"]["history"][-1])
        self.assertEqual(len(A.fixer_records()), 1)
        self.assertGreater(it["data"]["dispatch_after"], self.t[0])
        self.assertIn("usage limit is spent", self.st.offer_of(f"red:{it['id']}")["reason"])

class WorkspaceTrustTest(RedsBase):
    """2026-10-31, core #277: 9 fixers in a row ended `claude --bg` exit 1 "Workspace not trusted" (a core worktree; claude
    trusts a worktree through its repo's MAIN checkout, and ~/bitcashier/core never was). No launch is burnt on an
    untrusted repo: the red's offer says why with the owner's one-liner, and it dispatches the tick after trust."""

    def config(self, trusted):
        path = os.path.join(os.environ["HOME"], ".claude.json")
        root = os.path.realpath(self.r.path)
        with open(path, "w") as f:
            json.dump({"projects": {root: {"hasTrustDialogAccepted": trusted},
                                    os.path.dirname(root): {"hasTrustDialogAccepted": True}}}, f)  # a parent: no trust
        return path

    def test_an_untrusted_repo_is_never_launched_and_its_offer_names_the_fix(self):
        path = self.config(False)
        try:
            self.tick(dispatch=True)
            it = self.only()
            self.assertEqual(A.fixer_records(), [])  # nothing launched, no worktree
            reason = self.st.offer_of(f"red:{it['id']}")["reason"]
            self.assertIn("does not trust", reason)
            self.assertIn(f"cd {os.path.realpath(self.r.path)}", reason.replace("~", os.environ["HOME"]))
            self.assertEqual(int(it["data"].get("start_failures") or 0), 0)  # no backoff: it is not a launch
            self.config(True)
            self.tick(dispatch=True)
            self.assertEqual(len(A.fixer_records()), 1)  # trusted: dispatched at once
        finally:
            os.remove(path)

    def test_an_unreadable_config_decides_nothing(self):
        self.assertIsNone(A.claude_trust_gap(self.r.path))  # no ~/.claude.json in the test HOME


class LaunchErrorTest(RedsBase):
    def test_a_backed_off_red_shows_its_last_launch_error(self):
        os.environ["FAKE_CLAUDE_BG_FAIL"] = "Workspace not trusted. Run `claude` in /x once and accept the trust prompt"
        try:
            self.tick(dispatch=True)
            it = self.only()
            t0 = time.time()
            while A.lease_is_live(self.st.get(A.red_lease(it["data"]["key"])), time.time()) and time.time() - t0 < 60:
                time.sleep(0.2)
            self.t[0] = A.epoch_now()
            self.tick(dispatch=True)
            self.tick(dispatch=True)
        finally:
            os.environ.pop("FAKE_CLAUDE_BG_FAIL")
        it = self.only()
        reason = self.st.offer_of(f"red:{it['id']}")["reason"]
        self.assertIn("never got going", reason)
        self.assertIn("Workspace not trusted", reason)  # the actual launch error, not just "never got going"


class UnclaimedFixerTest(RedsBase):
    def test_a_session_that_never_starts_its_transcript_is_stopped_with_its_launch_state(self):
        os.environ.update(FAKE_CLAUDE_NO_SESSION="1", AGENTCTL_FIXER_CLAIM_WITHIN="2", AGENTCTL_BG_POLL="0.2")
        try:
            self.tick(dispatch=True)
            it = self.only()
            t0 = time.time()
            while A.lease_is_live(self.st.get(A.red_lease(it["data"]["key"])), time.time()) and time.time() - t0 < 60:
                time.sleep(0.2)
        finally:
            for k in ("FAKE_CLAUDE_NO_SESSION", "AGENTCTL_FIXER_CLAIM_WITHIN", "AGENTCTL_BG_POLL"):
                os.environ.pop(k)
        rec = A.fixer_records()[0]
        self.assertTrue(rec["verdict"].startswith("NOT STARTED: "), rec["verdict"])
        self.assertIn("never claimed", rec["verdict"])
        self.assertIn("state", rec["verdict"])  # what `claude agents` says about it


class AuditTest(RedsBase):
    def test_7_a_workflow_outside_the_newest_200_runs_is_still_read(self):
        saved = A.main_workflow_files, A.gh_json
        real = A.gh_json

        def gh(o_r, *args):
            if "--workflow" in args and "nightly.yml" in args:
                return [run(70, 5, "Nightly", "nightly.yml", self.sha, conclusion="failure")]
            return real(o_r, *args)
        A.main_workflow_files = lambda o_r: {"Nightly": "nightly.yml"}
        A.gh_json = gh
        A._flow_runs.clear()
        try:
            out = A.outside_gate(MOVIES)
        finally:
            A.main_workflow_files, A.gh_json = saved
        self.assertIn("Nightly", [n for n, *_ in out])

    def test_7_a_workflows_triggers_are_read_from_its_on_key(self):
        for txt, want in (("on: workflow_call\n", {"workflow_call"}), ("on: [push, workflow_call]\n", {"push", "workflow_call"}),
                          ("on: {push: {branches: [main]}, workflow_call: {}}\n", {"push", "workflow_call"}),
                          ("on:\n    push:\n        branches: [main]\n    workflow_call:\njobs: {}\n", {"push", "workflow_call"}),
                          ("name: x\non:\n  workflow_call:\n    inputs:\n      lane: {}\npermissions: {}\n", {"workflow_call"}),
                          ("\"on\":\n  - workflow_dispatch\n  - workflow_call\n", {"workflow_dispatch", "workflow_call"})):
            self.assertEqual(A.workflow_triggers(txt), want, txt)

    def test_7_the_servers_own_clone_is_the_scan_repo_where_no_laptop_clone_exists(self):
        # ci-runner (2026-10-29): HOME is /var/lib/agentctl, so no REPOS path exists; without its own clone
        # main_workflow_files read nothing and audit #7 listed no workflow outside the newest 200 runs
        clone = os.path.join(self.tmp.name, "server-movies")
        subprocess.run(["git", "init", "-q", clone], check=True)
        subprocess.run(["git", "-C", clone, "remote", "add", "origin",
                        "git@github.com:pawelkrupinski/movies.git"], check=True)
        saved = A.REPOS, os.environ.pop("AGENTCTL_RED_REPO", None), os.environ.get("AGENTCTL_MOVIES_REPO")
        A.REPOS = [os.path.join(self.tmp.name, "no-such-clone")]
        os.environ["AGENTCTL_MOVIES_REPO"] = clone
        try:
            self.assertEqual(A.scan_repo_for(MOVIES), clone)
        finally:
            A.REPOS = saved[0]
            os.environ["AGENTCTL_RED_REPO"] = saved[1] or self.r.path
            if saved[2] is None:
                os.environ.pop("AGENTCTL_MOVIES_REPO", None)
            else:
                os.environ["AGENTCTL_MOVIES_REPO"] = saved[2]

    def test_8_a_run_stuck_past_three_times_its_p90_is_a_red_until_it_decides(self):
        old = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(A.epoch_now() - 5 * 3600))
        runs = json.load(open(os.path.join(self.d, "runs.json")))
        self.put("runs.json", [run(80, 30, "Identity model convergence", "identity-model-convergence.yml", self.sha,
                                   status="in_progress", created=old)] + runs)
        self.tick()
        stuck = [i for i in self.st.items("red") if i["data"]["red_kind"] == "stuck-run"]
        self.assertEqual(len(stuck), 1)
        self.assertEqual(stuck[0]["data"]["kind"], "infra")
        self.put("runs.json", runs)
        self.tick()
        stuck = [i for i in self.st.items("red") if i["data"]["red_kind"] == "stuck-run"]
        self.assertEqual([i["state"] for i in stuck], ["cleared"])

    def stuck_after(self, jobs, busy):
        """The stuck-run reds after a tick with a bisect run created 5 h ago on the `measure` label (4 slots, `busy` of
        them running) whose jobs are `jobs`."""
        old = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(A.epoch_now() - 5 * 3600))
        runs = json.load(open(os.path.join(self.d, "runs.json")))
        self.put("runs.json", [run(81, 31, "Convergence bisect", "convergence-bisect.yml", self.sha, status="queued",
                                   created=old)] + runs)
        self.put("run-81.json", {"jobs": jobs})
        page = {"ok": A.epoch_now(), "limits": {"runners": {"runner-measure": {"capacity": 4, "state": "running",
                                                                              "labels": '["measure"]'}}},
                "running": [{"runner": "runner-measure", "run": "30"} for _ in range(busy)],
                "waiting_jobs": [{"label": '["measure"]', "repo": MOVIES, "run": "29", "secs": "18500"},
                                 {"label": '["measure"]', "repo": MOVIES, "run": "31", "secs": "18000"}]}
        with open(os.environ["AGENTCTL_RUNNERS_PAGE_CACHE"], "w") as f:
            json.dump(page, f)
        self.tick()
        return [i for i in self.st.items("red") if i["data"]["red_kind"] == "stuck-run"]

    def test_8_a_run_queued_behind_every_busy_runner_of_its_label_is_queued_not_stuck(self):
        # Convergence bisect #2385 (run 3568): an hour waiting for a `measure` slot was called a stuck infra red
        self.assertEqual(self.stuck_after([{"name": "preflight", "status": "queued", "startedAt": None}], busy=4), [])
        self.assertEqual([s.get("queued") for s in A.state_read()["ci"]["stuck"]],
                         ["queued: measure full (4/4 busy, 1 ahead, oldest 5 h)"])

    def test_8_a_run_never_started_while_its_label_has_a_free_slot_is_stuck(self):
        self.assertEqual(len(self.stuck_after([{"name": "preflight", "status": "queued", "startedAt": None}], busy=3)), 1)

    def test_8_a_run_is_as_old_as_its_first_jobs_start_not_its_creation(self):
        now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(A.epoch_now() - 60))
        self.assertEqual(self.stuck_after([{"name": "preflight", "status": "in_progress", "startedAt": now}], busy=4), [])



class StuckLimitTest(unittest.TestCase):
    """The owner, 2026-11-06 (`decide 971723aec893`: "fix the stuck limit as proposed"): six false stuck reds in a week.
    Record scrape fixtures #165/#172 (daily, 32-65 min a run) and Identity drift explorer #1 (its first run, waiting on
    the Identity model convergence run it dispatched) were judged against the 30-minute floor: the newest-200 run list
    held none of their finished runs. A workflow's limit now comes from its own last 30 finished runs; with fewer than
    STUCK_MIN_HISTORY, from its jobs' timeout-minutes; a run waiting on a run it dispatched is judged by that child;
    a run whose unfinished jobs all wait for busy runners is queued, not stuck."""
    NOW = 2_000_000_000.0

    def iso(self, ago):
        return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(self.NOW - ago))

    def ci_run(self, n, ago, dur=None, event="schedule", workflow=None):
        r = {"number": n, "databaseId": 1000 + n, "createdAt": self.iso(ago), "event": event, "workflowName": workflow}
        if dur is None:
            r["status"] = "in_progress"
        else:
            r.update(status="completed", conclusion="success", updatedAt=self.iso(ago - dur))
        return r

    def jobs(self, *specs):
        """specs: (status, started seconds ago or None)."""
        return lambda r: [{"name": f"j{i}", "status": st, "startedAt": self.iso(ago) if ago is not None else None}
                          for i, (st, ago) in enumerate(specs)]

    def stuck(self, lists, jobs_of, history=None, timeouts=None, queue=None):
        calls = []
        def history_of(name):
            calls.append(name)
            return (history or {}).get(name, [])
        out = A.stuck_runs(lists, now_=self.NOW, o_r=MOVIES, jobs_of=jobs_of, queue=queue or {},
                           history_of=history_of, timeout_of=lambda name: (timeouts or {}).get(name))
        return out, calls

    def test_record_scrape_fixtures_172_is_judged_by_its_own_finished_runs(self):
        """#172 at 32 min (the alarm) and at 2 h: its own last runs took 32-65 min, so its limit is ~3 h."""
        name = "Record scrape fixtures"
        own = [self.ci_run(165 + i, 86400 * (7 - i), dur=(32 + 5 * i) * 60) for i in range(7)]
        for age in (32 * 60, 2 * 3600):
            out, calls = self.stuck({name: [self.ci_run(172, age)]}, self.jobs(("in_progress", age)), {name: own})
            self.assertEqual(out, [], f"at {age // 60} min")
            self.assertEqual(calls, [name], "its own history read, once")
        out, _ = self.stuck({name: [self.ci_run(172, 4 * 3600)]}, self.jobs(("in_progress", 4 * 3600)), {name: own})
        self.assertEqual([s["run"]["number"] for s in out], [172], "past 3 x its p90 it is stuck")
        self.assertAlmostEqual(out[0]["limit"], 3 * 62 * 60, delta=120)

    def test_a_workflow_with_a_long_history_in_the_list_reads_nothing_more(self):
        name = "Main"
        runs = [self.ci_run(10 + i, 3600 * (i + 2), dur=600) for i in range(6)]
        out, calls = self.stuck({name: [self.ci_run(99, 3600)] + runs}, self.jobs(("in_progress", 3600)))
        self.assertEqual(([s["run"]["number"] for s in out], calls), ([99], []))

    def test_identity_drift_explorer_1_a_first_run_takes_its_jobs_timeout_minutes(self):
        name = "Identity drift explorer"
        out, _ = self.stuck({name: [self.ci_run(1, 75 * 60)]}, self.jobs(("in_progress", 75 * 60)),
                            timeouts={name: 305 * 60})
        self.assertEqual(out, [])
        out, _ = self.stuck({name: [self.ci_run(1, 6 * 3600)]}, self.jobs(("in_progress", 6 * 3600)),
                            timeouts={name: 305 * 60})
        self.assertEqual([s["limit"] for s in out], [305 * 60])

    def test_a_parent_waiting_on_the_run_it_dispatched_is_judged_by_that_child(self):
        """Identity drift explorer #1 started Identity model convergence #909 and waited on it: #909 healthy, #1 is."""
        conv = "Identity model convergence"
        hist = [self.ci_run(800 + i, 3600 * (i + 10), dur=3600, workflow=conv) for i in range(6)]
        lists = {"Identity drift explorer": [self.ci_run(1, 3 * 3600)],
                 conv: [self.ci_run(909, 2 * 3600, event="workflow_dispatch", workflow=conv)] + hist}
        jobs = self.jobs(("in_progress", 2.9 * 3600))
        out, _ = self.stuck(lists, jobs)  # no history, no timeout: the floor -- but its child decides
        self.assertEqual([s.get("waits_on") for s in out], ["Identity model convergence #909"])
        self.assertEqual(A.stuck_obs({"stuck": out}), {}, "no red: the child is judged on its own")
        lists[conv][0] = self.ci_run(909, 2 * 3600, dur=600, event="workflow_dispatch", workflow=conv)
        out, _ = self.stuck(lists, jobs)
        self.assertEqual([(s["run"]["number"], s.get("waits_on")) for s in out], [(1, None)],
                         "its child done, the parent past its limit is stuck")

    def test_a_run_whose_unfinished_jobs_all_wait_for_busy_runners_is_queued_not_stuck(self):
        name = "Convergence bisect"
        page = {"ok": self.NOW, "limits": {"runners": {"rm": {"capacity": 1, "state": "running",
                                                              "labels": '["measure"]'}}},
                "running": [{"runner": "rm", "run": "30"}],
                "waiting_jobs": [{"label": '["measure"]', "repo": MOVIES, "run": "5", "secs": "9000"}]}
        jobs = lambda r: [{"name": "preflight", "status": "completed", "startedAt": self.iso(3 * 3600)},
                          {"name": "bisect", "status": "queued", "startedAt": None}]
        out, _ = self.stuck({name: [self.ci_run(5, 3 * 3600)]}, jobs, queue=page)
        self.assertEqual(len(out), 1)
        self.assertTrue(out[0].get("queued"), out)
        self.assertEqual(A.stuck_obs({"stuck": out}), {})

    def test_the_jobs_timeout_minutes_are_read_from_the_workflow_file(self):
        txt = ("name: Identity drift explorer\non:\n    workflow_dispatch:\njobs:\n    explore:\n"
               "        runs-on: ubuntu\n        timeout-minutes: 300\n        steps:\n"
               "            - name: x\n              timeout-minutes: 999\n              run: y\n"
               "    timings:\n        needs: [explore]\n        uses: ./.github/workflows/report-timings.yml\n"
               "    report:\n        runs-on: ubuntu\n        timeout-minutes: 5\n        steps: []\n")
        self.assertEqual(A.workflow_timeout_bound(txt), 305 * 60)
        self.assertEqual(A.workflow_timeout_bound("jobs:\n  a:\n    runs-on: x\n"), 360 * 60,
                         "no job sets one: GitHub's own 360-minute default")

    def test_a_timeout_minutes_with_a_trailing_comment_is_honoured(self):
        txt = "jobs:\n  a:\n    runs-on: x\n    timeout-minutes: 30  # the real bound\n    steps: []\n"
        self.assertEqual(A.workflow_timeout_bound(txt), 30 * 60, "not GitHub's 360-minute default")

    def test_a_job_without_its_own_timeout_counts_githubs_default_beside_the_others(self):
        """Review 2026-11-07: one job setting timeout-minutes made every other job count 0, so a workflow of a 30-min
        job and a job with none was bounded at 30 min (a false stuck red past it). A job with none runs up to 360."""
        txt = ("jobs:\n  a:\n    runs-on: x\n    timeout-minutes: 30\n  b:\n    runs-on: x\n    steps: []\n"
               "  c:\n    uses: ./.github/workflows/r.yml\n")
        self.assertEqual(A.workflow_timeout_bound(txt), (30 + 360) * 60,
                         "b: GitHub's default; c (a reusable call, which takes no timeout-minutes): its own jobs'")

    def test_no_op_runs_in_the_list_pull_no_workflows_limit_down(self):
        """Issue 20261101T101427-e15fe1 (Order independence #28): the list held #27, a 31 s run whose real jobs were all
        skipped, and runs of 40-95 min only in the workflow's own history. 5+ such no-op runs in the list also skip the
        history read, so p90 = 31 s fell to the 30 min floor and a healthy 40 min run was a false stuck red."""
        name = "Order independence"
        noops = [self.ci_run(20 + i, 3600 * (i + 3), dur=31) for i in range(6)]
        full = [self.ci_run(10 + i, 86400 * (i + 1), dur=(40 + 10 * i) * 60) for i in range(5)]
        out, _ = self.stuck({name: [self.ci_run(28, 40 * 60)] + noops}, self.jobs(("in_progress", 40 * 60)), {name: full})
        self.assertEqual(out, [], "a healthy 40 min run of a workflow whose full runs take 40-80 min")

    def test_runs_that_failed_fast_are_no_sample_of_how_long_a_workflow_takes(self):
        """Issue 20261108T205920-0e4015 (Identity takes #3): the workflow's only finished runs failed in ~3 min. With
        fewer than 5 the jobs' timeout-minutes bound it (kept); five fast failures still set a 30 min limit."""
        name = "Identity takes"
        failed = [dict(self.ci_run(i, 3600 * (i + 3), dur=180), conclusion="failure") for i in range(1, 6)]
        out, _ = self.stuck({name: [self.ci_run(6, 40 * 60)] + failed}, self.jobs(("completed", 40 * 60), ("in_progress", 3 * 60)),
                            timeouts={name: 120 * 60})
        self.assertEqual(out, [], "a 40 min run whose last job started 3 min ago, within the jobs' 120 min bound")

    def test_a_scheduled_bot_not_started_yet_waits_its_turn_behind_checks(self):
        """Issue 20261108T135045-06bcab (Scala Steward #145): a daily run queued 1 h behind a CPU-saturated runner while
        slots were free (the scheduler holds it: checks first) was a stuck red. Not started, scheduled: 6 h; started:
        its workflow's own limit."""
        name = "Scala Steward"
        runs = [self.ci_run(140 + i, 86400 * (i + 1), dur=600) for i in range(6)]
        out, _ = self.stuck({name: [self.ci_run(145, 2 * 3600)] + runs}, self.jobs(("queued", None)))
        self.assertEqual(out, [], "not started, its schedule's run waits its turn")
        out, _ = self.stuck({name: [self.ci_run(146, 2 * 3600, event="workflow_dispatch")] + runs},
                            self.jobs(("queued", None)))
        self.assertEqual(len(out), 1, "a dispatched run is judged as before")

    def test_the_stuck_line_says_how_many_runs_its_limit_came_from(self):
        """Issue 20261101T101427-e15fe1, last sentence: print the sample count beside the limit."""
        name = "Main"
        runs = [self.ci_run(10 + i, 3600 * (i + 2), dur=600) for i in range(6)]
        out, _ = self.stuck({name: [self.ci_run(99, 3 * 3600)] + runs}, self.jobs(("in_progress", 3 * 3600)))
        line = A.stuck_obs({"stuck": out})
        [ob] = line.values()
        self.assertRegex(ob["line"], r"from \d+ runs", ob["line"])


class GateOwnRedLabelTest(unittest.TestCase):
    """2026-10-29 19:20: convergence #2550's fix (Fixes-Red: 2527, the same red key) read "blocked by gate convergence
    #2550" -- blocked by the very red it fixes. A fix of the gate's own red lands on red; any other waits on the gate."""
    CONV = "Identity model convergence"

    def setUp(self):
        self.saved = A.state_read
        A.state_read = lambda: {"ci": {"gate": [
            {"name": "Main", "decided": {"number": 900, "databaseId": 9900, "conclusion": "success"}},
            {"name": self.CONV, "decided": {"number": 2550, "databaseId": 12550, "conclusion": "failure"}}]}}

    def tearDown(self):
        A.state_read = self.saved

    def item(self, workflow, runs, status):
        return {"id": "k", "state": "fix-pending", "deadline": 0, "entered_at": 0, "level": 0,
                "data": {"key": "k", "workflow": workflow, "runs": runs,
                         "fix": {"entry": "20261029-1920-movies-wt-conv-guards-be7bb1", "status": status}}}

    def test_a_fix_of_the_gates_own_red_lands_on_red_whichever_run_it_names(self):
        for status in ("checking", "queued"):
            text, short = A.red_view(None, self.item(self.CONV, ["2527", "12527", "2550", "12550"], status), 0)
            self.assertEqual(short, f"landing on red (Fixes-Red), {status}")
            self.assertIn("landing on red (Fixes-Red)", text)
            self.assertNotIn("blocked by gate", text + short)

    def test_a_fix_of_another_red_is_blocked_by_the_gate(self):
        text, short = A.red_view(None, self.item(self.CONV, ["2400"], "queued"), 0)  # an older red, not the gate's
        self.assertEqual(short, "fix waiting to land (blocked by gate convergence #2550)")
        text, short = A.red_view(None, self.item("Main", ["2550"], "queued"), 0)  # same number, another workflow
        self.assertIn("blocked by gate convergence #2550", short)

if __name__ == "__main__":
    unittest.main()


class SetAsideByContentTest(unittest.TestCase):
    """Item 3 (simplification-2026-10-31): a refuted fix rebased onto a moved main is another sha with the same patch --
    it is set aside like the original, by content; a different change naming the same red still counts."""

    def red(self, **d):
        return {"data": {"key": "k1", "runs": ["2473"], "opened_at": 1000.0, **d}}

    def test_a_rebased_copy_of_a_refuted_fix_is_no_fix(self):
        facts = {"landed": [("b" * 40, 5000.0, {"2473"}, "fix")], "patches": {"a" * 40: "p1", "b" * 40: "p1"}}
        self.assertIsNone(A.red_fix_of(self.red(refuted=["a" * 40]), facts))
        self.assertIsNone(A.red_fix_of(self.red(unverified=["a" * 40]), facts))

    def test_a_fix_an_earlier_spell_of_the_red_refuted_is_no_fix_of_the_next(self):
        # 2026-11-13, 784badc4406e.4 (Verify scrape recording #26): with the new code live it went fix-pending on
        # 547709c1e2 -- the fix spell .3 had refuted on 11-11 -- through its queue entry (naming #42442828541, a run of
        # the key; its file's `updated` fresh), not on 8d06b1a5ab, landed with `Fixes-Red: 26`. A fix any spell of the
        # key refuted or set aside is aside for every later spell, and a landed entry is as old as its landing.
        old, new = "a" * 40, "c" * 40
        facts = {"entries": [{"id": "e129", "status": "landed", "landed_sha": old, "fixes": ["999"],
                              "created": "1970-01-01T00:10:00Z", "updated": "1970-01-01T02:00:00Z"}],
                 "landed": [(new, 9000.0, {"2473"}, "fix")], "key_runs": {"k1": ["999", "2473"]},
                 "key_aside": {"k1": [old]}}
        self.assertEqual(A.red_fix_of(self.red(opened_at=10000.0), facts)["sha"], new)
        facts["key_aside"] = {}  # unrefuted, yet landed (its commit's time) long before this spell opened
        self.assertEqual(A.red_fix_of(self.red(opened_at=10000.0), {**facts, "landed_at": {old: 600.0}})["sha"], new)

    def test_another_change_naming_the_red_is_still_its_fix(self):
        facts = {"landed": [("c" * 40, 5000.0, {"2473"}, "fix")], "patches": {"a" * 40: "p1", "c" * 40: "p2"}}
        self.assertEqual(A.red_fix_of(self.red(refuted=["a" * 40]), facts)["sha"], "c" * 40)

    def test_a_patch_that_cannot_be_read_falls_back_to_the_sha(self):
        facts = {"landed": [("a" * 40, 5000.0, {"2473"}, "fix")], "patches": {}}
        self.assertIsNone(A.red_fix_of(self.red(refuted=["a" * 9]), facts))
