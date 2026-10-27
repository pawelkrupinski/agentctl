"""THE GOVERNOR (the owner, 2026-10-27: "one governor for laptop memory and CPU"): work is admitted by its expected memory
against the budget, live free memory and pressure, in priority order (red > check > agent > measure); an idle kept sbt
server is evicted first when something needs room; a shell holds its ticket on an inherited descriptor."""
import contextlib, io, json, os, subprocess, sys, tempfile, time, unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from test_agentctl import A  # noqa: E402

KEYS = ("AGENTCTL_GOV_DIR", "AGENTCTL_GOV_FREE_PROBE", "AGENTCTL_GOV_PRESSURE_PROBE", "AGENTCTL_GOV_LOAD_PROBE",
        "AGENTCTL_GOV_POLL", "AGENTCTL_GOV_BUDGET_GB", "AGENTCTL_GOV_RESERVE_GB", "AGENTCTL_GOV_EVICT_SETTLE",
        "KINOWO_SBT_WARM_STATE", "AGENTCTL_CHECKS_DIR", "AGENTCTL_MAX_CHECKS")


class GovernorTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.d = self.tmp.name
        self.saved = {k: os.environ.get(k) for k in KEYS}
        self.free = os.path.join(self.d, "free")
        self.set_free(30)
        os.environ.update(AGENTCTL_GOV_DIR=os.path.join(self.d, "gov"), AGENTCTL_GOV_FREE_PROBE=f"cat {self.free}",
                          AGENTCTL_GOV_PRESSURE_PROBE="echo 20", AGENTCTL_GOV_LOAD_PROBE="echo 1", AGENTCTL_GOV_POLL="0.1",
                          AGENTCTL_GOV_BUDGET_GB="20", AGENTCTL_GOV_RESERVE_GB="3", AGENTCTL_GOV_EVICT_SETTLE="0",
                          KINOWO_SBT_WARM_STATE=os.path.join(self.d, "sbt-warm"),
                          AGENTCTL_CHECKS_DIR=os.path.join(self.d, "checks"), AGENTCTL_MAX_CHECKS="2")
        self.procs, self.tickets = [], []

    def tearDown(self):
        for t in self.tickets:
            t.release()
        for p in self.procs:
            if p.poll() is None:
                p.kill()
                p.wait()
        for k, v in self.saved.items():
            os.environ.pop(k, None) if v is None else os.environ.__setitem__(k, v)
        self.tmp.cleanup()

    def set_free(self, gb):
        with open(self.free, "w") as f:
            f.write(f"{gb}\n")

    def admit(self, cls, mem=None, **kw):
        with contextlib.redirect_stderr(io.StringIO()):
            t = A.gov_admit(cls, mem, f"test {cls}", wait=kw.pop("wait", False), **kw)
        if t:
            self.tickets.append(t)
        return t

    def aged(self, t, seconds):
        rec = A.read_record(t.path[:-5] + ".json")
        rec["at"] -= seconds
        A.write_json(t.path[:-5] + ".json", rec)

    def test_admitted_by_expected_memory_against_free_memory_less_the_reserve(self):
        self.set_free(5)
        self.assertIsNone(self.admit("agent", 5), "5 GB free less a 3 GB reserve is 2: no room for the 2.5 it needs now")
        self.set_free(6)
        self.assertIsNotNone(self.admit("agent", 5))

    def test_what_new_tickets_will_still_grow_into_is_counted(self):
        self.set_free(14)
        first = self.admit("agent", 5)
        self.assertIsNotNone(first)
        self.set_free(10)
        self.assertIsNone(self.admit("agent", 5), "10 free - 3 reserve - 5 still growing = 2: no room for 2.5")
        self.aged(first, A.GOV_RAMP + 1)
        self.assertIsNotNone(self.admit("agent", 5), "grown: its memory is in the free figure already")

    def test_the_budget_caps_what_is_admitted_whatever_is_free(self):
        for _ in range(3):
            t = self.admit("agent", 5)
            self.assertIsNotNone(t)
            self.aged(t, A.GOV_RAMP + 1)
        self.assertIsNone(self.admit("agent", 6), "15 + 6 GB over the 20 GB budget")
        self.assertIsNotNone(self.admit("agent", 5))

    def test_memory_pressure_stops_all_but_red_until_90(self):
        os.environ["AGENTCTL_GOV_PRESSURE_PROBE"] = "echo 80"
        self.assertIsNone(self.admit("agent"))
        self.assertIsNone(self.admit("check"))
        self.assertIsNotNone(self.admit("red"))

    def test_cpu_load_holds_agents_and_measurements_not_checks(self):
        cores = os.cpu_count()
        os.environ["AGENTCTL_GOV_LOAD_PROBE"] = f"echo {int(cores * 1.2)}"
        self.assertIsNone(self.admit("measure"))
        self.assertIsNotNone(self.admit("agent"))
        os.environ["AGENTCTL_GOV_LOAD_PROBE"] = f"echo {cores * 3}"
        self.assertIsNone(self.admit("agent"))
        self.assertIsNotNone(self.admit("check"))

    def waiter(self, cls, at):
        p = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
        self.procs.append(p)
        A.write_json(os.path.join(A.gov_dir(), f"w-{at:.6f}-{p.pid}.json"),
                     {"class": cls, "mem": 5, "label": f"queued {cls}", "pid": p.pid, "at": at})
        return p

    def test_priority_order_red_check_agent_measure_first_come_within_a_class(self):
        self.waiter("agent", time.time() - 10)
        self.assertIsNotNone(self.admit("red"), "red goes before a waiting agent")
        self.assertIsNotNone(self.admit("check"), "a check too")
        self.assertIsNone(self.admit("agent"), "an agent that came later waits behind the earlier one")
        self.assertIsNone(self.admit("measure"))

    def test_a_dead_waiter_holds_nobody_up(self):
        p = self.waiter("red", time.time() - 10)
        p.kill()
        p.wait()
        self.assertIsNotNone(self.admit("agent"))

    def server(self, name, idle, main_clone=False):
        """A kept server in sbt-warm's registry: its checkout (with an sbt-warm whose --stop ends it and frees memory)."""
        root = os.path.join(self.d, name)
        os.makedirs(os.path.join(root, "scripts"))
        if main_clone:
            os.makedirs(os.path.join(root, ".git"))
        p = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"])
        self.procs.append(p)
        stub = os.path.join(root, "scripts", "sbt-warm")
        with open(stub, "w") as f:
            f.write(f"#!/bin/sh\necho \"$*\" >> {self.d}/stops-{name}\nkill {p.pid}\necho 30 > {self.free}\n")
        os.chmod(stub, 0o755)
        state = os.environ["KINOWO_SBT_WARM_STATE"]
        os.makedirs(state, exist_ok=True)
        prefix = os.path.join(state, name)
        for ext, body in ((".root", root), (".pid", str(p.pid)), (".used", "")):
            with open(prefix + ext, "w") as f:
                f.write(body + "\n")
        os.utime(prefix + ".used", (time.time() - idle, time.time() - idle))
        return root, p

    def test_an_idle_server_is_evicted_when_the_head_waiter_needs_room(self):
        self.set_free(2)
        recent, _ = self.server("recent", idle=10)
        old, _ = self.server("old", idle=900)
        self.server("older-root", idle=3000, main_clone=True)
        t = self.admit("check", 5, wait=True)
        self.assertIsNotNone(t)
        self.assertTrue(os.path.exists(f"{self.d}/stops-old"), "the idle server unused longest was stopped")
        self.assertEqual(open(f"{self.d}/stops-old").read().strip(), "--stop --root")
        self.assertFalse(os.path.exists(f"{self.d}/stops-recent"), "one used a minute ago is not idle")
        self.assertFalse(os.path.exists(f"{self.d}/stops-older-root"), "never the root checkout's server")
        self.assertIn(f"evicted the idle sbt server of {old}", open(os.path.join(A.gov_dir(), "gov.log")).read())

    def test_a_server_a_call_is_running_in_is_never_evicted(self):
        self.set_free(2)
        root, _ = self.server("busy", idle=900)
        busy = os.path.join(os.environ["KINOWO_SBT_WARM_STATE"], "busy.busy")
        ready = os.path.join(self.d, "ready")
        p = subprocess.Popen([sys.executable, "-c", (
            "import fcntl, sys, time\nf = open(sys.argv[1], 'a+'); fcntl.flock(f, fcntl.LOCK_SH)\n"
            "open(sys.argv[2], 'w').close(); time.sleep(60)"), busy, ready])
        self.procs.append(p)
        while not os.path.exists(ready):
            time.sleep(0.05)
        self.assertIsNone(self.admit("check", 5))
        self.assertFalse(os.path.exists(f"{self.d}/stops-busy"))

    def test_a_shell_holds_its_ticket_on_descriptor_7(self):
        ticket = os.path.join(A.gov_dir(), "t-shell.lock")
        flag = os.path.join(self.d, "admitted")
        p = subprocess.Popen(["bash", "-c", f'exec 7>>"{ticket}"; "{sys.executable}" "{A.__file__}" gov admit --fd 7 '
                                            f'--ticket "{ticket}" --class agent --label shell --pid $$ --try && '
                                            f'touch "{flag}"; sleep 3'])
        self.procs.append(p)
        deadline = time.time() + 10
        while not os.path.exists(flag) and time.time() < deadline:
            time.sleep(0.05)
        self.assertTrue(os.path.exists(flag), "admitted")
        self.assertTrue(A.lock_held(ticket), "the shell holds the ticket after agentctl exits")
        self.assertEqual([t["label"] for t in A.gov_tickets()], ["shell"])
        p.wait()
        self.assertFalse(A.lock_held(ticket), "freed when the shell ends")

    def test_status_names_tickets_and_evictable_servers(self):
        self.admit("check", 1, kind="check")
        self.server("idle1", idle=900)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            A.main(["gov", "status"])
        self.assertIn("1 admitted, 1.0/20 GB committed", out.getvalue())
        self.assertIn("evictable: idle1", out.getvalue())


if __name__ == "__main__":
    unittest.main()
