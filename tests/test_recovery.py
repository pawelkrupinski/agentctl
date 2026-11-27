"""The recovery registry (the owner, 2026-10-28: "make this self-recover", "the way it got red / blocked i want it to go
green as well", "structural"): every non-green condition is opened by a failing probe and closed by the SAME probe
passing; nothing writes a condition-kind inbox entry or a block around open_condition."""
import ast, json, os, re, subprocess, sys, tempfile, time, unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from test_agentctl import A, TOOL, code_source  # noqa: E402
from harness import case_context  # noqa: E402
from test_red import RedBase, MOVIES  # noqa: E402
from test_reds import TickClock  # noqa: E402
from fake_probes import probe, registry  # noqa: E402


class Recorder:
    """A probe that fails `fails` times, then passes; records every args it was called with."""
    def __init__(self, fails=1):
        self.calls, self.fails = [], fails

    def __call__(self, a, **_bound):  # _bound: the store or clock cli binds into the check of its kind
        self.calls.append(dict(a))
        return (len(self.calls) > self.fails), f"call {len(self.calls)}"


class RegistryBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.env = {"AGENTCTL_RECOVERY_DIR": os.path.join(self.tmp.name, "rec"),
                    "AGENTCTL_STATE_ROOT": os.path.join(self.tmp.name, "state"),
                    "AGENTCTL_INBOX_DIR": os.path.join(self.tmp.name, "inbox"),
                    "AGENTCTL_STATUSLINE_STALE": os.path.join(self.tmp.name, "stale"),
                    "AGENTCTL_TUNNEL_STATE": os.path.join(self.tmp.name, "tunnel.json")}
        self.saved_env = {k: os.environ.get(k) for k in self.env}
        os.environ.update(self.env)
        self.saved_dirty = A.mark_gate_dirty
        self.dirty = []
        A.mark_gate_dirty = lambda: self.dirty.append(1)

    def tearDown(self):
        A.mark_gate_dirty = self.saved_dirty
        for k, v in self.saved_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        self.tmp.cleanup()

    def entries(self, kind=None):
        return [e for e in A.inbox_entries() if kind is None or e.get("kind") == kind]

    def fake(self, kind, fn):
        """fn in place of probe_<kind> (every module binding it), until the test ends."""
        saved = getattr(A, f"probe_{kind}")
        setattr(A, f"probe_{kind}", probe(kind, fn))
        self.addCleanup(setattr, A, f"probe_{kind}", saved)
        return fn

    def tick(self, *a, **k):
        """recovery_tick with cli's registry, built now (on the test's fakes)."""
        return A.recovery_tick(A.CurrentStore(A.process_store) if not a else a[0], registry(A), *a[1:], **k)


class WiredRegistryTest(unittest.TestCase):
    def test_the_root_builds_one_registry_of_every_probe_and_action_and_no_module_keeps_one(self):
        reg = registry(A)
        self.assertEqual(set(reg.checks), set(A.PROBE_EVERY))  # every probe has its recheck interval, and only those
        self.assertEqual(set(reg.actions), set(A.RECOVER_ACTION_NAMES))
        for kind, check in reg.checks.items():  # the check of a kind is its probe function (a partial binding the store)
            self.assertIs(getattr(check, "func", check), getattr(A, f"probe_{kind}"), kind)
        mod = sys.modules["agentctl.conditions"]
        self.assertFalse(hasattr(mod, "PROBES") or hasattr(mod, "RECOVER_ACTIONS"), "no registry cell in the machine")

    def test_a_registry_missing_a_kind_or_holding_a_check_of_another_name_is_refused(self):
        with self.assertRaises(A.ConditionBypass):
            A.ConditionRegistry([A.probe_tcp], dict(registry(A).actions))
        with self.assertRaises(A.ConditionBypass):
            A.run_probe(lambda a: (True, "not a probe"), {})

    def test_the_invariant_probe_checks_with_mains_clock_and_store(self):
        clock, store, seen = A.Clock(), object(), []
        A.INVARIANTS["WX"] = lambda c, st: seen.append((c, st)) or (True, "held")
        try:
            self.assertTrue(A.run_probe(registry(A, clock, store).check("invariant"), {"id": "WX"}).ok)
        finally:
            A.INVARIANTS.pop("WX")
        self.assertEqual(seen, [(clock, store)], "the watchdog's invariants read the clock and store cli handed")


class SameProbeTest(RegistryBase):
    def test_every_probe_kind_opens_and_closes_through_the_same_function(self):
        for kind in sorted(A.PROBE_EVERY):
            rec = self.fake(kind, Recorder())
            args = {"x": kind}
            c = A.open_condition("UNKNOWN", f"UNKNOWN: {kind} is down", A.run_probe(rec, args), [], key=f"k-{kind}", store=A.CurrentStore(A.process_store))
            self.assertIsNotNone(c, kind)
            self.assertEqual(self.tick(force=True)[0][:10], "RECOVERED:", kind)
            self.assertEqual(rec.calls, [args, args], kind)  # the opening check and the closing check: one function
            self.assertFalse(os.path.exists(c.get("path") or A.recovery_dir(f"k-{kind}.json")), kind)
        unread = [e for e in self.entries("UNKNOWN") if not e["read"]]
        self.assertEqual(unread, [])  # every condition's entry acked on recovery: nobody acks a solved problem
        self.assertEqual(len(self.entries("RECOVERED")), len(A.PROBE_EVERY))
        self.assertTrue(self.dirty)

    def test_a_failing_probe_backs_off_and_a_passing_one_opens_nothing(self):
        tcp = self.fake("tcp", Recorder(fails=3))
        A.open_condition("UNKNOWN", "UNKNOWN: port", A.run_probe(tcp, {"host": "h", "port": 1}), [], key="p", store=A.CurrentStore(A.process_store))
        t = A.epoch_now()
        self.assertEqual(self.tick(A.CurrentStore(A.process_store), t + 1000), [])
        c = A.open_conditions(A.CurrentStore(A.process_store))[0]
        self.assertEqual(c["fails"], 1)
        self.assertGreater(c["next_check"], t + 1000 + A.PROBE_EVERY["tcp"])
        self.assertEqual(self.tick(A.CurrentStore(A.process_store), t + 1001), [])  # not due: not probed
        self.assertEqual(len(tcp.calls), 2)
        http = self.fake("http", Recorder(fails=0))
        self.assertIsNone(A.open_condition("UNKNOWN", "UNKNOWN: x", A.run_probe(http, {"url": "u"}), [], store=A.CurrentStore(A.process_store)))
        self.assertEqual(self.entries("UNKNOWN")[-1]["verdict"], "UNKNOWN: port")

    def test_on_recover_actions_run_once(self):
        ran = []
        reg = registry(A, route=probe("route", Recorder()))
        reg.actions["tunnel_reset"] = lambda c, a, st: ran.append(a) or "kicked"
        A.open_condition("NETWORK", "NETWORK: offline", A.run_probe(reg.checks["route"]), [("tunnel_reset", {"n": 1})], key="net", store=A.CurrentStore(A.process_store))
        lines = (A.recovery_tick(A.CurrentStore(A.process_store), reg, force=True)
                 + A.recovery_tick(A.CurrentStore(A.process_store), reg, force=True))
        self.assertEqual(ran, [{"n": 1}])
        self.assertEqual(len(lines), 1)
        self.assertIn("kicked", lines[0])


class BlipTest(RegistryBase):
    """A BLIP OPENS NOTHING (the owner, 2026-11-17: 18 of 19 UNKNOWN spells in a week closed by themselves in ~70 s,
    each an inbox entry and a notification): an UNKNOWN opens only when seen failing again past its grace."""

    def setUp(self):
        super().setUp()
        os.environ["AGENTCTL_UNKNOWN_GRACE"] = "300"
        self.addCleanup(os.environ.__setitem__, "AGENTCTL_UNKNOWN_GRACE", "0")
        self.token = self.fake("token", Recorder(fails=99))
        self.t = [1_794_900_000.0]
        self.st = A.Store(clock=lambda: self.t[0])  # open_condition's time is its store's clock

    def sighting(self, after):
        self.t[0] += after
        return A.open_condition("UNKNOWN", "UNKNOWN: token unavailable", A.run_probe(self.token, {"ns": "movies"}), [],
                                key="token-movies", store=self.st)

    def test_a_failure_that_clears_inside_the_grace_opens_nothing(self):
        self.assertIsNone(self.sighting(0))
        self.assertIsNone(self.sighting(70))
        self.assertEqual((A.open_conditions(A.CurrentStore(A.process_store)), self.entries("UNKNOWN")), ([], []))

    def test_a_failure_seen_again_past_the_grace_opens_once(self):
        self.assertIsNone(self.sighting(0))
        self.assertIsNone(self.sighting(200))
        self.assertIsNotNone(self.sighting(150))
        self.assertIsNotNone(self.sighting(60))  # open: refreshed, no second entry
        self.assertEqual(len(self.entries("UNKNOWN")), 1)

    def test_blips_far_apart_never_add_up(self):
        self.assertIsNone(self.sighting(0))
        self.assertIsNone(self.sighting(A.CONDITION_PENDING_LAPSE + 1))  # the last sighting lapsed: a new run
        self.assertIsNone(self.sighting(100))
        self.assertEqual(A.open_conditions(A.CurrentStore(A.process_store)), [])

    def test_other_kinds_and_a_zero_grace_open_at_once(self):
        self.assertIsNotNone(A.open_condition("STALLED", "STALLED: x", A.run_probe(self.token, {}), [], key="s", store=A.CurrentStore(A.process_store)))
        self.assertIsNotNone(A.open_condition("UNKNOWN", "UNKNOWN: y", A.run_probe(self.token, {}), [], key="u", grace=0, store=A.CurrentStore(A.process_store)))


class HostTest(RegistryBase):
    """A condition is probed on the host role that opened it; escalations are the server's alone."""

    def role(self, r):
        saved = os.environ.get("AGENTCTL_ROLE")
        os.environ["AGENTCTL_ROLE"] = r
        self.addCleanup(lambda: os.environ.__setitem__("AGENTCTL_ROLE", saved) if saved is not None
                        else os.environ.pop("AGENTCTL_ROLE", None))

    def test_the_laptops_condition_is_probed_on_the_laptop_never_on_ci_runner(self):
        st = A.process_store()  # one store, both hosts (the laptop's is ci-runner's through the API)
        rec = self.fake("tcp", Recorder(fails=1))
        self.role("sync-test")
        c = A.open_condition("WATCHDOG", "W5: the processor is down", A.run_probe(rec, {}), [], key="w5",
                             store=st)
        self.assertEqual(c["host"], "sync")
        self.role("server")
        t = A.epoch_now() + 3 * 3600
        self.assertEqual(self.tick(st, t, force=True), [])
        self.assertEqual(len(rec.calls), 1, "ci-runner never runs the laptop's probe")
        self.assertEqual(st.item("condition", "w5")["level"], 1, "the server escalates it")
        self.role("sync-test")
        self.assertEqual(self.tick(st, t + 1, force=True)[0][:10], "RECOVERED:")
        self.assertEqual(len(rec.calls), 2)

    def test_a_token_condition_the_server_opened_is_cleared_by_the_laptops_probe(self):
        st = A.process_store()
        rec = self.fake("token", Recorder(fails=1))
        self.role("server")
        c = A.open_condition("UNKNOWN", "UNKNOWN: token unavailable", A.run_probe(rec, {"ns": "movies"}), [],
                             key="token-movies", store=st)
        self.assertEqual(c["host"], "server")
        self.role("sync-test")  # only the laptop has the processor's socket
        self.assertEqual(self.tick(st, A.epoch_now() + 1, force=True)[0][:10], "RECOVERED:")


class ConditionItemTest(RegistryBase):
    """Step 12: a condition is an item of the "condition" machine -- it escalates (an event the notifier delivers) 10 min
    after opening and every 2 h after, and only its own probe passing closes it. (An owner-only kind: an agent's kind
    waits for its agent first, tests/test_escalations.py OwnerEscalationTest.)"""
    def test_open_escalate_by_deadline_close_by_its_probe_and_old_files_migrate(self):
        ok = [False]
        tunnel = self.fake("tunnel", lambda a: (ok[0], "down" if not ok[0] else "up"))
        A.open_condition("NETWORK", "NETWORK: x", A.run_probe(tunnel), [], key="k1", store=A.CurrentStore(A.process_store))
        st = A.process_store()
        it = st.item("condition", "k1")
        self.assertEqual((it["state"], it["level"]), ("open", 0))
        self.assertEqual(self.tick(now_=A.epoch_now() + 11 * 60), [])
        it = st.item("condition", "k1")
        self.assertEqual(it["level"], 1)
        self.assertEqual([e["severity"] for e in st.events() if e["item_id"] == "k1"][-1], "escalate")
        self.assertGreater(it["deadline"], time.time() + 3600)  # the next escalation in 2 h
        ok[0] = True
        self.assertEqual(len(self.tick(force=True)), 1)
        self.assertEqual(st.item("condition", "k1")["state"], "closed")
        self.assertEqual(A.open_conditions(A.CurrentStore(A.process_store)), [])
        os.makedirs(A.recovery_dir(), exist_ok=True)  # a condition file from before step 12: an item now
        A.write_json(A.recovery_dir("old.json"), {"key": "old", "kind": "NETWORK", "text": "NETWORK: x",
                                                  "probe": {"kind": "tunnel", "args": {}}, "next_check": 1e12})
        self.tick()
        self.assertFalse(os.path.exists(A.recovery_dir("old.json")))
        self.assertEqual([c["key"] for c in A.open_conditions(A.CurrentStore(A.process_store))], ["old"])  # an item now, its probe due at its own time


class NoBypassTest(RegistryBase):
    def test_no_condition_without_its_probe(self):
        for bad in (None, {"kind": "tunnel"}, True, "tunnel"):
            with self.assertRaises(A.ConditionBypass):
                A.open_condition("STALLED", "STALLED: x", bad, [], store=A.CurrentStore(A.process_store))
        with self.assertRaises(TypeError):
            A.open_condition("STALLED", "STALLED: x", store=A.CurrentStore(A.process_store))  # no default: the probe is required
        with self.assertRaises(A.ConditionBypass):
            A.ProbeResult("tunnel", {}, False, "forged")
        with self.assertRaises(A.ConditionBypass):
            registry(A).check("no-such-probe")
        tunnel = self.fake("tunnel", Recorder())
        with self.assertRaises(A.ConditionBypass):
            A.open_condition("STALLED", "STALLED: x", A.run_probe(tunnel), [("no-such-action", {})], store=A.CurrentStore(A.process_store))

    def test_a_condition_kind_inbox_entry_is_refused_outside_open_condition(self):
        for v in ("STALLED: tunnel", "UNKNOWN: gate", "NETWORK: uplink", "NOT LANDED: stalled push"):
            with self.assertRaises(A.ConditionBypass, msg=v):
                A.inbox_write("x", v, [v], [], 1)
        self.assertTrue(A.inbox_write("land", "WATCH UNKNOWN: a command's own verdict", [], [], 2, reply=True))
        self.assertTrue(A.inbox_write("land", "NOT LANDED: the pre-push checks failed", [], [], 1))

    def test_the_guard_stands_in_front_of_the_writer_it_wraps(self):
        """The rule lives in conditions, not in the entry builder: the wrapped writer never sees a bypass."""
        seen = []
        write = A.condition_guarded(lambda rec: seen.append(rec["verdict"]) or rec["id"])
        entry = {"id": "e1", "final": True, "reply": False, "kind": "STALLED", "verdict": "STALLED: x"}
        with self.assertRaises(A.ConditionBypass):
            write(entry)
        self.assertEqual(seen, [])
        A._OPENING[0] += 1
        try:
            self.assertEqual(write(entry), "e1")
        finally:
            A._OPENING[0] -= 1
        self.assertEqual(write({**entry, "reply": True}), "e1")
        self.assertEqual(write({**entry, "kind": "LANDED"}), "e1")
        self.assertEqual(seen, ["STALLED: x"] * 3)

    def test_lint_no_condition_written_around_the_registry(self):
        """The source: an inbox_write whose verdict is a condition kind, a blocked red_claim, a ProbeResult or an
        open_condition without a probe argument, anywhere but the registry's own functions, fails this test."""
        tree = ast.parse(code_source())
        prefix = re.compile(r"^(STALLED:|UNKNOWN:|NETWORK:|"
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
                if nm == "ProbeResult" and fn.name not in ("run_probe", "close_condition", "_close_result"):  # a manual close: a reason
                    bad.append(f"{fn.name}:{call.lineno} a ProbeResult not from run_probe")
                if nm == "open_condition" and len(call.args) < 4:
                    bad.append(f"{fn.name}:{call.lineno} open_condition without (kind, text, probe, on_recover)")
        self.assertEqual(bad, [])


class DetectorsTest(RegistryBase):
    def test_the_tunnel_kick_runs_the_launchctl_the_tests_name(self):
        """Review 2026-11-26 (D I5): launchctl_kick ran `launchctl` itself, past $AGENTCTL_LAUNCHCTL, so a test of the
        tunnel watch kickstarted the laptop's real tunnel. It goes through launchd.launchctl now."""
        ran, saved = [], (A.run_cmd, A.has_tunnel, os.environ.get("AGENTCTL_LAUNCHCTL"))
        A.run_cmd = lambda argv, **kw: ran.append(list(argv)) or subprocess.CompletedProcess(argv, 0, "", "")
        A.has_tunnel = lambda: True
        os.environ["AGENTCTL_LAUNCHCTL"] = "/fake/launchctl"
        try:
            A.launchctl_kick()
        finally:
            A.run_cmd, A.has_tunnel = saved[:2]
            os.environ.pop("AGENTCTL_LAUNCHCTL") if saved[2] is None else os.environ.__setitem__("AGENTCTL_LAUNCHCTL", saved[2])
        self.assertEqual([a[:3] for a in ran], [["/fake/launchctl", "kickstart", "-k"]])

    def test_tunnel_network_and_stall_open_on_the_tunnel_probe_and_clear_on_it(self):
        tunnel = self.fake("tunnel", Recorder(fails=10 ** 6))
        saved = A.uplink_loss, A.launchctl_kick
        A.uplink_loss, A.launchctl_kick = (lambda: 0.9), (lambda: None)
        try:
            self.assertEqual(A.tunnel_watch(A.CurrentStore(A.process_store)), "network")
        finally:
            A.uplink_loss, A.launchctl_kick = saved
        self.assertEqual([c["probe"]["kind"] for c in A.open_conditions(A.CurrentStore(A.process_store))], ["tunnel"])
        self.assertEqual(len(self.entries("NETWORK")), 1)
        tunnel.fails = 0
        self.tick(force=True)
        self.assertEqual(A.open_conditions(A.CurrentStore(A.process_store)), [])
        self.assertTrue(all(e["read"] for e in self.entries("NETWORK")))
        self.assertFalse(A.read_json_file(A.tunnel_state_path()).get("network"))

    def test_network_offline_clears_on_the_route_probe(self):
        self.fake("route", Recorder())
        A._last_route[0] = ("en0", "10.0.0.1")
        self.assertEqual(A.network_watch(A.CurrentStore(A.process_store), route=lambda: None, kick=lambda: None), "offline")
        self.assertEqual(A.open_conditions(A.CurrentStore(A.process_store))[0]["probe"]["kind"], "route")
        self.tick(force=True)
        self.assertEqual(A.open_conditions(A.CurrentStore(A.process_store)), [])


class UntilGreenTest(RedBase):
    """`red block --until-green "Decodo probe"` (step 11): a block LEASE the tick renews while the probe fails; the probe
    workflow going green lapses it, and the red is verified by its next run."""
    def setUp(self):
        super().setUp()
        self.main_red(40, 20, kind="infra")  # an outside cause: a code red is never blocked on the owner
        saved = os.environ.get("AGENTCTL_STATE_ROOT")
        os.environ["AGENTCTL_STATE_ROOT"] = os.path.join(self.tmp.name, "state")
        self.addCleanup(lambda: os.environ.__setitem__("AGENTCTL_STATE_ROOT", saved) if saved is not None
                        else os.environ.pop("AGENTCTL_STATE_ROOT", None))
        self.t = [A.epoch_now()]  # the test's one time: the command stamps its lease with it, the tick reads it
        self.st = A.process_store(clock=lambda: self.t[0])

    def tool(self, *argv, cwd=None, clock=None):
        return super().tool(*argv, cwd=cwd, clock=clock or TickClock(self.t))

    def snap(self, conclusion):
        case_context().runs.clear()
        s = A.ci_state_refresh(MOVIES, write=False, ctx=case_context())
        s["latest"]["Decodo probe"] = {"number": 99, "conclusion": conclusion}
        A.ci_state_write(s)
        return s

    def tick(self, snap):
        return A.red_tick(self.st, MOVIES, self.r.path, snap, now_=self.t[0], dispatch=False, registry=registry(A), ctx=case_context())

    def test_the_block_lapses_when_the_probe_workflow_goes_green_and_the_red_is_verified(self):
        self.tick(self.snap("failure"))
        rc, out = self.tool("red", "block", "20", "--owner-cause", "external-account", "--cmd", "Check the Decodo dashboard",
                            "--until-green", "Decodo probe", "--repo", "movies", cwd=self.r.path)
        self.assertEqual(rc, 0, out)
        self.assertIn("auto-clears when Decodo probe is green", out)
        lease = [r for r in self.st.list("block/") if r["holder"]]
        self.assertEqual([r["holder"] for r in lease], ["owner:external-account"])
        self.tick(self.snap("failure"))
        it = [i for i in self.st.items("red")][0]
        self.assertEqual(it["state"], "blocked-external")
        # the status line names the question itself (ask.py, 2026-11-03), never a bare BLOCKED
        self.assertIn("main #20 → WAITING ON YOU: run `! Check the Decodo dashboard`", self.why("--summary")[1])
        meta = json.loads(lease[0]["meta"])
        self.st.set_meta(lease[0]["name"], lease[0]["epoch"], lease[0]["secret"], {"probed_at": 0})
        self.tick(self.snap("success"))  # the probe passes: the lease lapses
        self.assertFalse(A.lease_is_live(self.st.get(lease[0]["name"]), self.t[0]), meta)
        self.tick(self.snap("success"))
        it = self.st.item("red", it["id"])
        self.assertEqual((it["state"], it["data"]["fix"]["status"]), ("fix-pending", "verify"))

    def test_a_block_whose_probe_passes_already_is_refused_and_a_test_failure_never_blocks(self):
        self.tick(self.snap("success"))
        rc, out = self.tool("red", "block", "20", "--owner-cause", "external-account", "--cmd", "x", "--until-green",
                            "Decodo probe", "--repo", "movies", cwd=self.r.path)
        self.assertEqual(rc, 1, out)
        self.assertIn("NOT BLOCKED", out)
        os.unlink(A.reds_dir("runsig", "40.json"))
        self.main_red(40, 20)  # a test failure
        rc, out = self.tool("red", "block", "20", "--owner-cause", "external-account", "--cmd", "x", "--repo", "movies",
                            cwd=self.r.path)
        self.assertEqual(rc, 2, out)
        self.assertIn("TEST FAILURE", out)


if __name__ == "__main__":
    unittest.main()
