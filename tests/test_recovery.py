"""The recovery registry (the owner, 2026-10-28: "make this self-recover", "the way it got red / blocked i want it to go
green as well", "structural"): every non-green condition is opened by a failing probe and closed by the SAME probe
passing; nothing writes a condition-kind inbox entry or a block around open_condition."""
import ast, json, os, re, sys, tempfile, time, unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from test_agentctl import A, TOOL  # noqa: E402
from test_red import RedBase, MOVIES  # noqa: E402


class Recorder:
    """A probe that fails `fails` times, then passes; records every args it was called with."""
    def __init__(self, fails=1):
        self.calls, self.fails = [], fails

    def __call__(self, a):
        self.calls.append(dict(a))
        return (len(self.calls) > self.fails), f"call {len(self.calls)}"


class RegistryBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.env = {"AGENTCTL_RECOVERY_DIR": os.path.join(self.tmp.name, "rec"),
                    "AGENTCTL_INBOX_DIR": os.path.join(self.tmp.name, "inbox"),
                    "AGENTCTL_STATUSLINE_STALE": os.path.join(self.tmp.name, "stale"),
                    "AGENTCTL_TUNNEL_STATE": os.path.join(self.tmp.name, "tunnel.json")}
        self.saved_env = {k: os.environ.get(k) for k in self.env}
        os.environ.update(self.env)
        self.saved_probes, self.saved_actions = dict(A.PROBES), dict(A.RECOVER_ACTIONS)
        self.saved_dirty = A.mark_gate_dirty
        self.dirty = []
        A.mark_gate_dirty = lambda: self.dirty.append(1)

    def tearDown(self):
        A.PROBES.clear()
        A.PROBES.update(self.saved_probes)
        A.RECOVER_ACTIONS.clear()
        A.RECOVER_ACTIONS.update(self.saved_actions)
        A.mark_gate_dirty = self.saved_dirty
        for k, v in self.saved_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        self.tmp.cleanup()

    def entries(self, kind=None):
        return [e for e in A.inbox_entries() if kind is None or e.get("kind") == kind]


class SameProbeTest(RegistryBase):
    def test_every_probe_kind_opens_and_closes_through_the_same_function(self):
        for kind in sorted(self.saved_probes):
            rec = Recorder()
            A.PROBES[kind] = rec
            args = {"x": kind}
            c = A.open_condition("UNKNOWN", f"UNKNOWN: {kind} is down", A.run_probe(kind, args), [], key=f"k-{kind}")
            self.assertIsNotNone(c, kind)
            self.assertEqual(A.recovery_tick(force=True)[0][:10], "RECOVERED:", kind)
            self.assertEqual(rec.calls, [args, args], kind)  # the opening check and the closing check: one function
            self.assertFalse(os.path.exists(c.get("path") or A.recovery_dir(f"k-{kind}.json")), kind)
        unread = [e for e in self.entries("UNKNOWN") if not e["read"]]
        self.assertEqual(unread, [])  # every condition's entry acked on recovery: nobody acks a solved problem
        self.assertEqual(len(self.entries("RECOVERED")), len(self.saved_probes))
        self.assertTrue(self.dirty)

    def test_a_failing_probe_backs_off_and_a_passing_one_opens_nothing(self):
        A.PROBES["tcp"] = Recorder(fails=3)
        A.open_condition("UNKNOWN", "UNKNOWN: port", A.run_probe("tcp", {"host": "h", "port": 1}), [], key="p")
        t = time.time()
        self.assertEqual(A.recovery_tick(t + 1000), [])
        c = A.open_conditions()[0]
        self.assertEqual(c["fails"], 1)
        self.assertGreater(c["next_check"], t + 1000 + A.PROBE_EVERY["tcp"])
        self.assertEqual(A.recovery_tick(t + 1001), [])  # not due: not probed
        self.assertEqual(len(A.PROBES["tcp"].calls), 2)
        A.PROBES["http"] = Recorder(fails=0)
        self.assertIsNone(A.open_condition("UNKNOWN", "UNKNOWN: x", A.run_probe("http", {"url": "u"}), []))
        self.assertEqual(self.entries("UNKNOWN")[-1]["verdict"], "UNKNOWN: port")

    def test_on_recover_actions_run_once(self):
        ran = []
        A.RECOVER_ACTIONS["kick_tunnel"] = lambda c, a: ran.append(a) or "kicked"
        A.PROBES["route"] = Recorder()
        A.open_condition("NETWORK", "NETWORK: offline", A.run_probe("route"), [("kick_tunnel", {"n": 1})], key="net")
        lines = A.recovery_tick(force=True) + A.recovery_tick(force=True)
        self.assertEqual(ran, [{"n": 1}])
        self.assertEqual(len(lines), 1)
        self.assertIn("kicked", lines[0])


class NoBypassTest(RegistryBase):
    def test_no_condition_without_its_probe(self):
        for bad in (None, {"kind": "tunnel"}, True, "tunnel"):
            with self.assertRaises(A.ConditionBypass):
                A.open_condition("STALLED", "STALLED: x", bad, [])
        with self.assertRaises(TypeError):
            A.open_condition("STALLED", "STALLED: x")  # no default: the probe is required
        with self.assertRaises(A.ConditionBypass):
            A.ProbeResult("tunnel", {}, False, "forged")
        with self.assertRaises(A.ConditionBypass):
            A.run_probe("no-such-probe")
        A.PROBES["tunnel"] = Recorder()
        with self.assertRaises(A.ConditionBypass):
            A.open_condition("STALLED", "STALLED: x", A.run_probe("tunnel"), [("no-such-action", {})])

    def test_a_condition_kind_inbox_entry_is_refused_outside_open_condition(self):
        for v in ("STALLED: tunnel", "UNKNOWN: gate", "NETWORK: uplink", "RED UNCLAIMED: Main #1", "RED BUDGET: x",
                  "RED ESCALATE: x", "RED BLOCKED ON OWNER: x", "NOT LANDED: stalled push"):
            with self.assertRaises(A.ConditionBypass, msg=v):
                A.inbox_write("x", v, [v], [], 1)
        self.assertTrue(A.inbox_write("land", "WATCH UNKNOWN: a command's own verdict", [], [], 2, reply=True))
        self.assertTrue(A.inbox_write("land", "NOT LANDED: the pre-push checks failed", [], [], 1))
        with self.assertRaises(A.ConditionBypass):
            A.red_claim(MOVIES, "1", "", "blocked", "cmd")

    def test_lint_no_condition_written_around_the_registry(self):
        """The source: an inbox_write whose verdict is a condition kind, a blocked red_claim, a ProbeResult or an
        open_condition without a probe argument, anywhere but the registry's own functions, fails this test."""
        with open(TOOL) as f:
            tree = ast.parse(f.read())
        prefix = re.compile(r"^(STALLED:|UNKNOWN:|NETWORK:|RED UNCLAIMED|RED BUDGET|RED ESCALATE|RED BLOCKED ON OWNER|"
                            r"WATCH STALLED|WATCH UNKNOWN|HELD:)")
        allowed = {"open_condition", "inbox_write", "run_probe", "with_inbox"}

        def text_of(node):
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                return node.value
            if isinstance(node, ast.JoinedStr):
                return "".join(v.value if isinstance(v, ast.Constant) else "{}" for v in node.values)
            if isinstance(node, ast.BinOp):
                return text_of(node.left)
            return ""

        def name_of(call):
            f = call.func
            return f.id if isinstance(f, ast.Name) else f.attr if isinstance(f, ast.Attribute) else ""

        bad = []
        for fn in [n for n in tree.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]:
            if fn.name in allowed:
                continue
            assigned = {}  # name -> [(line, text)]: a verdict variable is judged by its nearest assignment before use
            for n in ast.walk(fn):
                if isinstance(n, ast.Assign):
                    for t in n.targets:
                        if isinstance(t, ast.Name):
                            assigned.setdefault(t.id, []).append((n.lineno, text_of(n.value)))
            opens = any(name_of(c) == "open_condition" for c in ast.walk(fn) if isinstance(c, ast.Call))
            for call in [n for n in ast.walk(fn) if isinstance(n, ast.Call)]:
                nm = name_of(call)
                if nm == "inbox_write" and len(call.args) >= 2:
                    v = call.args[1]
                    if isinstance(v, ast.Name):
                        before = [x for x in assigned.get(v.id, []) if x[0] <= call.lineno]
                        texts = [max(before)[1]] if before else []
                    else:
                        texts = [text_of(v)]
                    if any(prefix.match(t) for t in texts):
                        bad.append(f"{fn.name}:{call.lineno} inbox_write of a condition kind")
                if nm == "red_claim" and not opens and any(isinstance(x, ast.Constant) and x.value == "blocked"
                                                           for x in list(call.args) + [k.value for k in call.keywords]):
                    bad.append(f"{fn.name}:{call.lineno} a blocked claim outside open_condition")
                if nm == "ProbeResult" and fn.name not in ("run_probe", "close_condition"):  # a manual close: a reason
                    bad.append(f"{fn.name}:{call.lineno} a ProbeResult not from run_probe")
                if nm == "open_condition" and len(call.args) < 4:
                    bad.append(f"{fn.name}:{call.lineno} open_condition without (kind, text, probe, on_recover)")
        self.assertEqual(bad, [])


class DetectorsTest(RegistryBase):
    def test_tunnel_network_and_stall_open_on_the_tunnel_probe_and_clear_on_it(self):
        A.PROBES["tunnel"] = Recorder(fails=10 ** 6)
        saved = A.uplink_loss, A.launchctl_kick
        A.uplink_loss, A.launchctl_kick = (lambda: 0.9), (lambda: None)
        try:
            self.assertEqual(A.tunnel_watch(), "network")
        finally:
            A.uplink_loss, A.launchctl_kick = saved
        self.assertEqual([c["probe"]["kind"] for c in A.open_conditions()], ["tunnel"])
        self.assertEqual(len(self.entries("NETWORK")), 1)
        A.PROBES["tunnel"].fails = 0
        A.recovery_tick(force=True)
        self.assertEqual(A.open_conditions(), [])
        self.assertTrue(all(e["read"] for e in self.entries("NETWORK")))
        self.assertFalse(A.read_json_file(A.tunnel_state_path()).get("network"))

    def test_network_offline_clears_on_the_route_probe(self):
        A.PROBES["route"] = Recorder()
        A._last_route[0] = ("en0", "10.0.0.1")
        self.assertEqual(A.network_watch(route=lambda: None, kick=lambda: None), "offline")
        self.assertEqual(A.open_conditions()[0]["probe"]["kind"], "route")
        A.recovery_tick(force=True)
        self.assertEqual(A.open_conditions(), [])

    def test_an_infra_not_landed_lands_again_when_its_check_passes(self):
        wt = os.path.join(self.tmp.name, "movies-wt-x")
        os.makedirs(wt)
        relanded = []
        A.RECOVER_ACTIONS["reland"] = lambda c, a: relanded.append(a) or "landing again"
        A.PROBES["git_fetch"] = Recorder()
        A.land_not_landed("NOT LANDED: git fetch forgejo main failed: ssh: connect timed out (tunnel?)", wt)
        c = A.open_conditions()[0]
        self.assertEqual((c["kind"], c["probe"]["kind"]), ("LAND_INFRA", "git_fetch"))
        self.assertEqual(len(self.entries("NOT_LANDED")), 1)
        A.recovery_tick(force=True)
        self.assertEqual(relanded, [{"worktree": wt, "attempt": 1}])
        self.assertTrue(self.entries("NOT_LANDED")[0]["read"])
        # a test failure stays the author's: no condition, no retry
        A.land_not_landed("NOT LANDED: the pre-push checks failed (check tree 2); the change was not pushed", wt)
        self.assertEqual(A.open_conditions(), [])
        # an unknown cause: the author's entry AND one retry, never a second
        A.PROBES["git_fetch"] = Recorder()
        wt2 = os.path.join(self.tmp.name, "movies-wt-y")
        os.makedirs(wt2)
        wt = wt2
        A.land_not_landed("NOT LANDED: something odd", wt)
        self.assertEqual(A.open_conditions()[0]["kind"], "LAND_UNKNOWN")
        A.recovery_tick(force=True)
        A.land_not_landed("NOT LANDED: something odd", wt)  # the same change again: retried once already
        self.assertEqual(A.open_conditions(), [])
        self.assertEqual(len(relanded), 2)

    def test_land_failure_causes(self):
        for v, cause in [("NOT LANDED: git fetch forgejo main failed: x", "infra"),
                         ("NOT LANDED: stalled checks (remote): no output and no CPU for 20 min", "infra"),
                         ("NOT LANDED: its remote run cannot be read (timeout)", "infra"),
                         ("NOT LANDED: rebase onto forgejo/main conflicts in: a.scala", "real"),
                         ("NOT LANDED: the checks failed with your change in the batch (entry 1)", "real"),
                         ("NOT LANDED: the push failed (pre-push hook or remote)", "unknown")]:
            self.assertEqual(A.land_failure_cause(v)[0], cause, v)


class UntilGreenTest(RedBase):
    """`red block --until-green "Decodo probe"`: released by the probe workflow going green, then verified."""
    def setUp(self):
        super().setUp()
        self.main_red(40, 20, kind="infra")  # an outside cause: a code red is never blocked on the owner
        self.rec_dir = os.path.join(self.tmp.name, "rec")
        os.environ["AGENTCTL_RECOVERY_DIR"] = self.rec_dir
        self.saved_actions = dict(A.RECOVER_ACTIONS)
        self.saved_dirty = A.mark_gate_dirty
        A.mark_gate_dirty = lambda: None

    def tearDown(self):
        A.RECOVER_ACTIONS.clear()
        A.RECOVER_ACTIONS.update(self.saved_actions)
        A.mark_gate_dirty = self.saved_dirty
        os.environ.pop("AGENTCTL_RECOVERY_DIR", None)
        super().tearDown()

    def snap(self, conclusion):
        A.ci_state_write({"at": time.time(), "repo": MOVIES, "gate": [], "outside": [],
                          "latest": {"Decodo probe": {"number": 99, "conclusion": conclusion}}})

    def test_the_block_clears_itself_when_the_probe_workflow_goes_green_and_the_red_is_verified(self):
        self.snap("failure")
        os.environ["AGENTCTL_CLAIM_PID"] = str(os.getpid())
        try:
            rc, out = self.tool("red", "block", "20", "--owner-cause", "external-account", "--cmd", "Check the Decodo dashboard", "--until-green",
                                "Decodo probe", "--repo", "movies", cwd=self.r.path)
        finally:
            os.environ.pop("AGENTCTL_CLAIM_PID")
        self.assertEqual(rc, 0, out)
        self.assertIn("auto-clears when Decodo probe is green", out)
        self.assertEqual([c.get("state") for c in A.all_claims()], ["blocked"])
        self.assertIn("main #20 → BLOCKED ON OWNER (auto-clears when Decodo probe is green", self.why("--summary")[1])
        verified = []
        A.RECOVER_ACTIONS["verify"] = lambda c, a: verified.append(a["run"]) or "dispatched"
        self.assertEqual(A.recovery_tick(force=True), [])  # the probe workflow is still red: nothing moves
        self.assertEqual(len(A.all_claims()), 1)
        self.snap("success")
        lines = A.recovery_tick(force=True)
        self.assertEqual(len(lines), 1, lines)
        self.assertEqual(A.all_claims(), [])  # the block released
        self.assertEqual(verified, ["20"])  # the red's own workflow dispatched to verify it
        self.assertTrue(all(e["read"] for e in self.inbox("BLOCKED")))
        self.assertEqual(A.open_conditions(), [])

    def test_a_block_whose_probe_passes_already_is_refused_and_the_default_probe_is_the_red_s_workflow(self):
        self.snap("success")
        rc, out = self.tool("red", "block", "20", "--owner-cause", "external-account", "--cmd", "x", "--until-green", "Decodo probe", "--repo", "movies",
                            cwd=self.r.path)
        self.assertEqual(rc, 1, out)
        self.assertIn("NOT BLOCKED", out)
        os.environ["AGENTCTL_CLAIM_PID"] = str(os.getpid())
        try:
            rc, out = self.tool("red", "block", "20", "--owner-cause", "external-account", "--cmd", "x", "--repo", "movies", cwd=self.r.path)
        finally:
            os.environ.pop("AGENTCTL_CLAIM_PID")
        self.assertEqual(rc, 0, out)
        c = A.open_conditions()[0]
        self.assertEqual(c["probe"]["kind"], "owner_block")  # the outage's check AND the block's justification
        self.assertEqual(c["probe"]["args"]["inner"], {"kind": "workflow_green", "args": {"repo": MOVIES, "workflow": "Main"}})
        self.assertEqual([n for n, _a in c["on_recover"]], ["release_block"])  # its own green IS the verification


if __name__ == "__main__":
    unittest.main()
