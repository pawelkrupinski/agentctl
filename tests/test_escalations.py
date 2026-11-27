"""THE ESCALATION DISPATCHER (the owner, 2026-11-26: "can agentctl just kick off an agent when sth escalates?"): every
escalation kind is routed by ONE table (escalationtable.py) to an agent or to the owner alone; an agent's escalation is
offered, leased and dispatched on the red dispatcher's offers and leases -- one offer per (kind, subject), one live
fixer per kind, at most ESCALATION_FIXERS in all -- retried with backoff, handed to the owner once its tries are spent,
and closed only when its condition's own probe passes."""
import ast, glob, json, os, subprocess, sys, tempfile, unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from test_agentctl import A  # noqa: E402
from fake_probes import probe, registry  # noqa: E402
ROOT = os.path.dirname(HERE)


class FakeLauncher:
    """The world the dispatcher reads (escalations.py EscalationLauncher): sessions it starts and their states."""

    def __init__(self, repo):
        self.repo, self.starts, self.stopped, self.state = repo, [], [], {}
        self.gap, self.block, self.refuse = None, None, None

    def host(self):
        return "laptop-test"

    def blocker(self):
        return self.block

    def repo_path(self, repo):
        return self.repo

    def trust_gap(self, path):
        return self.gap

    def start(self, route, kind, condition, prompt, repo):
        if self.refuse:
            return None, self.refuse
        sid = f"s{len(self.starts) + 1}"
        self.starts.append({"kind": kind, "condition": condition["id"], "model": route.model, "prompt": prompt,
                            "session": sid})
        self.state[sid] = False
        return {"session": sid, "worktree": f"{repo}-wt-escalation-{len(self.starts)}"}, f"session {sid}"

    def session_over(self, session):
        return self.state.get(session, True)

    def stop(self, session):
        self.stopped.append(session)


class EscalationBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.env = {"AGENTCTL_RECOVERY_DIR": os.path.join(self.tmp.name, "rec"),
                    "AGENTCTL_STATE_ROOT": os.path.join(self.tmp.name, "state"),
                    "AGENTCTL_INBOX_DIR": os.path.join(self.tmp.name, "inbox")}
        self.saved_env = {k: os.environ.get(k) for k in self.env}
        os.environ.update(self.env)
        self.t = [1_800_000_000.0]
        self.st = A.Store(path=os.path.join(self.tmp.name, "store.db"), clock=lambda: self.t[0])
        self.failing = set()  # the condition keys whose probe fails
        self.probe = probe("escprobe", lambda a: (a["key"] not in self.failing, f"probe of {a['key']}"))
        self.saved_dirty = A.mark_gate_dirty
        A.mark_gate_dirty = lambda: None
        self.launcher = FakeLauncher(os.path.join(self.tmp.name, "repo"))

    def tearDown(self):
        A.mark_gate_dirty = self.saved_dirty
        for k, v in self.saved_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        self.tmp.cleanup()

    def open(self, kind, key, text):
        self.failing.add(key)
        return A.open_condition(kind, text, A.run_probe(self.probe, {"key": key}), [], key=key, store=self.st, grace=0)

    def clear(self, key):
        self.failing.discard(key)
        c = next(c for c in A.open_conditions(self.st) if c["key"] == key)
        A.close_condition(c, A.run_probe(self.probe, {"key": key}), self.st, registry(A))

    def run_pass(self, advance=0):
        self.t[0] += advance
        return A.escalation_pass(self.st, self.t[0], self.launcher)

    def live(self):
        return [r for r in self.st.list("escalation/") if r["holder"] and r["expires_at"] > self.t[0]]

    def events(self, cid=None):
        rows = []
        for line in open(A.log_path()).read().splitlines() if os.path.exists(A.log_path()) else []:
            e = json.loads(line)
            if e["item_type"] == "escalation" and (cid is None or e["item_id"].endswith("/" + cid)):
                rows.append(e)
        return rows


class DispatchTableTest(unittest.TestCase):
    def test_every_watchdog_invariant_and_every_condition_kind_the_code_opens_has_a_row(self):
        kinds = {f"INVARIANT:{w}" for w in A.INVARIANTS} | {"INVARIANT:WD"}
        for p in glob.glob(os.path.join(ROOT, "lib", "agentctl", "*.py")):
            for node in ast.walk(ast.parse(open(p).read())):
                if isinstance(node, ast.Call) and getattr(node.func, "id", getattr(node.func, "attr", None)) in (
                        "open_condition", "open_outage") and node.args:
                    arg = node.args[0] if getattr(node.func, "id", None) == "open_condition" else node.args[1]
                    if isinstance(arg, ast.Constant) and arg.value != "INVARIANT":
                        kinds.add(arg.value)
        self.assertEqual(sorted(kinds - set(A.DISPATCH_TABLE)), [], "a kind nobody routed")

    def test_each_row_is_an_agent_with_its_budget_or_the_owners_with_a_category(self):
        for kind, r in A.DISPATCH_TABLE.items():
            if r.mode == A.TO_AGENT:
                self.assertIn(r.repo, ("bin", "movies"), kind)
                self.assertIn(r.model, ("sonnet", "opus"), kind)
                self.assertGreater(r.runtime, 0, kind)
                self.assertGreaterEqual(r.tries, 1, kind)
                self.assertTrue(r.task, kind)
            else:
                self.assertEqual(r.mode, A.TO_OWNER, kind)
                self.assertRegex(r.why, r"^\[[a-z-]+\] ", kind)

    def test_an_unknown_kind_is_the_owners(self):
        self.assertEqual(A.escalation_route({"kind": "SOMETHING-NEW", "text": "x"}).mode, A.TO_OWNER)

    def test_owner_signals_make_any_kind_the_owners(self):
        ours = {"kind": "INVARIANT", "key": "invariant-WP", "text": "INVARIANT WP violated: java pid 4: 30 GB -- ours: "
                "it runs in ~/projects/movies-wt-x"}
        self.assertEqual(A.escalation_kind(ours), "INVARIANT:WP")
        self.assertEqual(A.escalation_route(ours).mode, A.TO_AGENT)
        theirs = dict(ours, text="INVARIANT WP violated: Google Chrome pid 9: 12 GB -- the owner's to look at "
                                 "(agentctl never stops a process it did not start)")
        self.assertEqual(A.escalation_route(theirs).mode, A.TO_OWNER)
        vault = {"kind": "UNKNOWN", "key": "token", "text": "UNKNOWN: token unavailable: run `secrets unlock movies`"}
        self.assertEqual(A.escalation_route(vault).mode, A.TO_OWNER)
        self.assertEqual(A.escalation_route({"kind": "NETWORK", "text": "NETWORK: offline"}).mode, A.TO_OWNER)

    def test_an_opus_row_is_for_an_unknown_root_cause_and_sonnet_is_the_default(self):
        self.assertEqual(A.DISPATCH_TABLE["INVARIANT:W2"].model, "opus")
        self.assertEqual(A.DISPATCH_TABLE["INVARIANT:WT"].model, "sonnet")


class OfferAndDispatchTest(EscalationBase):
    def test_owner_only_kinds_never_offer_lease_or_start(self):
        self.open("INVARIANT", "invariant-WB", "INVARIANT WB violated: uncommitted edits in ~/bin")
        self.open("NETWORK", "route", "NETWORK: offline")
        self.open("NEW-KIND", "nk", "NEW-KIND: something nobody routed")
        for _ in range(20):
            self.run_pass(advance=600)
        self.assertEqual(self.launcher.starts, [])
        self.assertEqual(self.st.offers("escalation"), [])
        self.assertEqual(self.st.list("escalation/"), [])

    def test_nothing_is_offered_before_its_bound(self):
        self.open("INVARIANT", "invariant-W2", "INVARIANT W2 violated: red 12 has no lease")
        self.run_pass(advance=60)
        self.assertEqual(self.launcher.starts, [])
        self.run_pass(advance=A.DISPATCH_AFTER)
        self.assertEqual([s["kind"] for s in self.launcher.starts], ["INVARIANT:W2"])
        self.assertEqual(self.launcher.starts[0]["model"], "opus")

    def test_one_offer_and_one_fixer_per_kind_and_subject(self):
        a = self.open("STALLED", "queue-stall", "STALLED: no change moved for 60 min")
        b = self.open("STALLED", "batch-x", "STALLED: 3 change(s) waiting while the gate is green")
        for _ in range(5):
            self.run_pass(advance=A.DISPATCH_AFTER)
        self.assertEqual(len(self.launcher.starts), 1, self.launcher.starts)  # ONE live fixer of the kind
        self.assertEqual(len(self.live()), 1)
        self.assertEqual(len(self.st.offers("escalation")), 1)  # the other subject's one offer waits
        waiting = self.st.offers("escalation")[0]
        self.assertIn("one STALLED fixer at a time", waiting["reason"])
        self.assertIn(self.launcher.starts[0]["condition"], (a["id"], b["id"]))

    def test_at_most_two_escalation_fixers_in_all(self):
        self.open("INVARIANT", "invariant-W2", "INVARIANT W2 violated: x")
        self.open("INVARIANT", "invariant-WT", "INVARIANT WT violated: y")
        self.open("CLONE", "clone-movies", "CLONE: z")
        for _ in range(3):
            self.run_pass(advance=A.DISPATCH_AFTER)
        self.assertEqual(len(self.launcher.starts), A.ESCALATION_FIXERS)
        self.assertEqual(len(self.live()), 2)
        self.assertIn("capacity 2/2", self.st.offers("escalation")[0]["reason"])

    def test_the_brief_carries_the_text_its_history_and_the_rules(self):
        c = self.open("INVARIANT", "invariant-W2", "INVARIANT W2 violated: red 12 has no lease")
        self.run_pass(advance=A.DISPATCH_AFTER)
        prompt = self.launcher.starts[0]["prompt"]
        for want in ("INVARIANT W2 violated: red 12 has no lease", f"agentctl history {c['id']}", "condition.open",
                     "devtools/selfpush.py", "commit gate", "--no-verify", "CLOSES ONLY WHEN ITS CONDITION"):
            self.assertIn(want, prompt)

    def test_an_untrusted_repo_is_not_dispatchable_and_counts_no_try(self):
        self.launcher.gap = "claude does not trust ~/bin (...): owner, once: `! cd ~/bin && claude`"
        c = self.open("INVARIANT", "invariant-W2", "INVARIANT W2 violated: x")
        for _ in range(30):
            self.run_pass(advance=600)
        self.assertEqual(self.launcher.starts, [])
        off = self.st.offers("escalation")
        self.assertEqual(len(off), 1)
        self.assertTrue(off[0]["reason"].startswith("NOT DISPATCHABLE: claude does not trust ~/bin"))
        rec = self.st.item("condition", c["id"])["data"].get("escalation") or {}
        self.assertEqual(rec.get("tries") or [], [])
        self.assertFalse([e for e in A.inbox_entries() if "ESCALATION GAVE UP" in str(e.get("verdict"))])
        self.launcher.gap = None
        self.run_pass(advance=60)
        self.assertEqual(len(self.launcher.starts), 1)  # trusted: dispatched at the next pass


class ClosureAndRetryTest(EscalationBase):
    def test_the_escalation_closes_when_its_condition_clears(self):
        c = self.open("INVARIANT", "invariant-W6", "INVARIANT W6 violated: 3 escalations not notified")
        self.run_pass(advance=A.DISPATCH_AFTER)
        self.run_pass(advance=60)  # the session works: taken
        self.clear("invariant-W6")
        acts = self.run_pass(advance=60)
        self.assertTrue(any("CLEARED" in a for a in acts), acts)
        self.assertEqual(self.launcher.stopped, ["s1"])
        self.assertEqual(self.live(), [])
        self.assertEqual([(e["from_state"], e["to_state"], e["cause"]) for e in self.events(c["id"])],
                         [(None, "offered", "timeout"), ("offered", "dispatched", "agent"),
                          ("dispatched", "working", "agent"), ("working", "cleared", "probe")])
        took = self.events(c["id"])[2]
        self.assertEqual(took["actor"], "session:s1")
        rep = A.analyze(A.merge(self.events()), self.t[0], 0.0, "escalation")  # `agentctl analyze --type escalation`
        self.assertEqual(rep["to_end_by_kind"]["INVARIANT:W6 -> cleared"]["n"], 1)
        self.assertEqual(rep["to_end_by_kind"]["INVARIANT:W6 -> cleared"]["max"], 120)

    def test_an_agent_that_ends_without_clearing_is_a_failed_try_then_the_owner_is_told_once(self):
        c = self.open("INVARIANT", "invariant-WT", "INVARIANT WT violated: 40 stale roots")
        route = A.DISPATCH_TABLE["INVARIANT:WT"]
        self.run_pass(advance=A.DISPATCH_AFTER)
        for n in range(1, route.tries + 1):
            self.assertEqual(len(self.launcher.starts), n)
            self.launcher.state[f"s{n}"] = True  # the agent says it is done: the condition is still open
            self.run_pass(advance=60)
            self.assertEqual(len(self.launcher.starts), n, "the next try backs off")
            for _ in range(int(A.RETRY_BACKOFF * 2 ** (n - 1) // 600) + 1):
                self.run_pass(advance=600)
        self.assertEqual(len(self.launcher.starts), route.tries)
        for _ in range(10):
            self.run_pass(advance=3600)
        self.assertEqual(len(self.launcher.starts), route.tries, "never past its budget")
        told = [e for e in A.inbox_entries() if "ESCALATION GAVE UP" in str(e.get("verdict"))]
        self.assertEqual(len(told), 1)
        self.assertIn("WAITING ON YOU", told[0]["verdict"])
        self.assertIn("try 1: ENDED", told[0]["verdict"])
        self.assertEqual(told[0].get("audience"), "owner")
        self.assertEqual(self.events(c["id"])[-1]["to_state"], "owner")
        self.assertEqual(self.st.offers("escalation"), [])

    def test_the_backoff_doubles(self):
        self.open("CLONE", "clone-movies", "CLONE: unhealthy")
        self.run_pass(advance=A.DISPATCH_AFTER)
        self.launcher.state["s1"] = True
        self.run_pass(advance=60)
        self.run_pass(advance=A.RETRY_BACKOFF - 120)
        self.assertEqual(len(self.launcher.starts), 1)
        self.run_pass(advance=120)
        self.assertEqual(len(self.launcher.starts), 2)
        self.launcher.state["s2"] = True
        self.run_pass(advance=60)
        self.run_pass(advance=A.RETRY_BACKOFF + 60)
        self.assertEqual(len(self.launcher.starts), 2, "the second backoff is twice the first")
        self.run_pass(advance=A.RETRY_BACKOFF)
        self.assertEqual(len(self.launcher.starts), 3)

    def test_a_session_past_its_wall_time_is_stopped_and_counted(self):
        c = self.open("CLONE", "clone-movies", "CLONE: unhealthy")
        self.run_pass(advance=A.DISPATCH_AFTER)
        for _ in range(int(A.SONNET_RUNTIME // 600)):
            self.run_pass(advance=600)
        self.assertEqual(self.launcher.stopped, ["s1"])
        tries = self.st.item("condition", c["id"])["data"]["escalation"]["tries"]
        self.assertTrue(tries[0]["outcome"].startswith("TIMED OUT"), tries)

    def test_a_start_that_fails_and_a_lapsed_lease_are_failed_tries(self):
        c = self.open("CLONE", "clone-movies", "CLONE: unhealthy")
        self.launcher.refuse = "exit 1: Workspace not trusted"
        self.run_pass(advance=A.DISPATCH_AFTER)
        tries = self.st.item("condition", c["id"])["data"]["escalation"]["tries"]
        self.assertTrue(tries[0]["outcome"].startswith("NOT STARTED: exit 1"), tries)
        self.launcher.refuse = None
        self.run_pass(advance=A.RETRY_BACKOFF + 60)
        self.assertEqual(len(self.launcher.starts), 1)
        self.launcher.state["s1"] = None  # `claude agents` unreadable: no renewal, no verdict
        self.run_pass(advance=A.LEASE_TTL + 60)
        tries = self.st.item("condition", c["id"])["data"]["escalation"]["tries"]
        self.assertTrue(tries[-1]["outcome"].startswith("LAPSED"), tries)

    def test_a_condition_that_clears_while_offered_closes_its_offer(self):
        self.launcher.block = "dispatch is OFF"
        c = self.open("CLONE", "clone-movies", "CLONE: unhealthy")
        self.run_pass(advance=A.DISPATCH_AFTER)
        self.assertEqual(len(self.st.offers("escalation")), 1)
        self.clear("clone-movies")
        self.run_pass(advance=60)
        self.assertEqual(self.st.offers("escalation"), [])
        self.assertEqual(self.events(c["id"])[-1]["to_state"], "cleared")


class OwnerEscalationTest(EscalationBase):
    """The server's escalation of a condition an agent may take (2026-11-27, vendor-drift: dispatched at 20:08:28Z, the
    owner told "ESCALATED: VENDOR_DRIFT open for 10 min" at 20:08:33Z while the agent worked): held while an agent has
    it, and once due it says WHY no agent took it."""

    def server_tick(self, advance=0):
        self.t[0] += advance
        A.recovery_tick(self.st, registry(A, escprobe=self.probe), self.t[0])

    def escalations(self, c):
        return [e["text"] for e in self.st.item_events("condition", c["id"]) if e.get("cause") == "timeout"]

    def test_a_dispatched_agent_holds_the_owners_escalation_until_its_condition_clears(self):
        c = self.open("VENDOR_DRIFT", "vendor-drift", "VENDOR DRIFT: devtools/design_rules/model.py drifted")
        self.run_pass(advance=A.DISPATCH_AFTER)
        self.assertEqual(len(self.launcher.starts), 1)
        self.server_tick(advance=5)  # the server's tick, at its deadline, just after the dispatch
        self.run_pass(advance=60)  # the session works: taken
        for _ in range(4):
            self.server_tick(advance=600)
            self.run_pass()
        self.assertEqual(self.escalations(c), [], "an agent works on it: the owner is not told")
        self.clear("vendor-drift")
        self.run_pass(advance=60)
        self.assertEqual(self.escalations(c), [])

    def test_no_dispatcher_tells_the_owner_after_the_grace_that_it_was_never_offered(self):
        c = self.open("VENDOR_DRIFT", "vendor-drift", "VENDOR DRIFT: x drifted")
        self.server_tick(advance=A.DISPATCH_AFTER + 5)
        self.assertEqual(self.escalations(c), [], "the dispatcher may still take it")
        self.server_tick(advance=A.OWNER_GRACE)
        lines = self.escalations(c)
        self.assertEqual(len(lines), 1)
        self.assertIn("no agent took it: never offered", lines[0])

    def test_an_offer_nobody_could_take_tells_the_owner_its_reason(self):
        self.launcher.gap = "claude does not trust ~/bin"
        c = self.open("VENDOR_DRIFT", "vendor-drift", "VENDOR DRIFT: x drifted")
        self.run_pass(advance=A.DISPATCH_AFTER)
        self.server_tick(advance=5)
        self.assertEqual(self.escalations(c), [])
        self.run_pass(advance=A.OWNER_GRACE)
        self.server_tick()
        self.assertIn("no agent took it: NOT DISPATCHABLE: claude does not trust ~/bin", self.escalations(c)[0])

    def test_a_launch_that_failed_tells_the_owner_at_once_with_its_reason(self):
        self.launcher.refuse = "no `claude` CLI"
        c = self.open("VENDOR_DRIFT", "vendor-drift", "VENDOR DRIFT: x drifted")
        self.run_pass(advance=A.DISPATCH_AFTER)
        self.server_tick(advance=5)
        lines = self.escalations(c)
        self.assertEqual(len(lines), 1)
        self.assertIn("no agent took it: the launch failed: NOT STARTED: no `claude` CLI", lines[0])

    def test_spent_tries_and_owner_kinds_escalate_and_a_backoff_holds(self):
        t = 1_800_000_000.0
        it = {"id": "c1", "data": {"kind": "CLONE", "text": "CLONE: z", "opened_at": t - 3600}}
        spent = {**it, "data": {**it["data"], "escalation": {"tries": [{"outcome": "ENDED: x"}] * 3, "owner_told": t}}}
        self.assertEqual(A.owner_escalation(spent, None, t), (True, "its 3 agent tries did not clear it"))
        backing = {**it, "data": {**it["data"], "escalation": {"tries": [{"outcome": "ENDED: x"}], "state": "offered"}}}
        self.assertFalse(A.owner_escalation(backing, {"reason": "try 2 backs off"}, t)[0])
        capped = {**it, "data": {**it["data"], "escalation": {"state": "offered"}}}
        self.assertEqual(A.owner_escalation(capped, {"reason": "capacity 2/2 escalation fixers"}, t),
                         (True, "no agent took it: capacity 2/2 escalation fixers"))
        owner = {"id": "c2", "data": {"kind": "NETWORK", "text": "NETWORK: offline", "opened_at": t - 3600}}
        self.assertEqual(A.owner_escalation(owner, None, t), (True, ""))


class TrustAskTest(unittest.TestCase):
    def test_the_escalation_repos_are_asked_for_once_like_a_watched_repos_clone(self):
        with tempfile.TemporaryDirectory() as tmp:
            clone = os.path.join(tmp, "bin")
            subprocess.run(["git", "init", "-q", clone], check=True)
            cfg = os.path.join(tmp, "claude")
            os.makedirs(cfg)
            with open(os.path.join(cfg, ".claude.json"), "w") as f:
                json.dump({"projects": {}}, f)
            with open(os.path.join(tmp, "red-repos.json"), "w") as f:
                json.dump({"repos": []}, f)
            env = {"AGENTCTL_BIN_REPO": clone, "CLAUDE_CONFIG_DIR": cfg, "AGENTCTL_STATE_ROOT": os.path.join(tmp, "s"),
                   "AGENTCTL_INBOX_DIR": os.path.join(tmp, "inbox"),
                   "AGENTCTL_RED_REPOS": os.path.join(tmp, "red-repos.json")}
            saved = {k: os.environ.get(k) for k in env}
            os.environ.update(env)
            try:
                self.assertEqual(len(A.trust_asks()), 1)
                self.assertEqual(A.trust_asks(), [])
                asks = [e for e in A.inbox_entries() if "tty-trust" in str(e.get("verdict"))]
                self.assertEqual(len(asks), 1)
                self.assertIn("every escalation fixer of bin", asks[0]["verdict"])
            finally:
                for k, v in saved.items():
                    if v is None:
                        os.environ.pop(k, None)
                    else:
                        os.environ[k] = v


if __name__ == "__main__":
    unittest.main()
