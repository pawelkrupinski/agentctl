"""THE GOVERNOR (the owner, 2026-10-27: "one governor for laptop memory and CPU"): work is admitted by its expected memory
against the budget, live free memory and pressure, in priority order (red > check > agent > measure); an idle kept sbt
server is evicted first when something needs room; a shell holds its ticket on an inherited descriptor."""
import contextlib, io, json, os, subprocess, sys, tempfile, threading, time, unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from test_agentctl import A  # noqa: E402

KEYS = ("AGENTCTL_GOV_CPU_FACTOR", "AGENTCTL_RUNNERS_PAGE_CACHE", "AGENTCTL_GOV_DIR", "AGENTCTL_GOV_FREE_PROBE", "AGENTCTL_GOV_PRESSURE_PROBE", "AGENTCTL_GOV_LOAD_PROBE",
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

    def test_a_red_fix_is_admitted_whatever_the_memory_budget_or_pressure(self):
        """Nothing stops a gate fix, ever (the owner, 2026-10-31): no budget, free memory or pressure wait for red."""
        os.environ.update(AGENTCTL_GOV_PRESSURE_PROBE="echo 99", AGENTCTL_GOV_BUDGET_GB="1")
        self.set_free(0)
        self.assertIsNone(self.admit("check"))
        self.assertIsNotNone(self.admit("red", 5))

    def test_laptop_pressure_holds_other_work_with_its_reason(self):
        os.environ["AGENTCTL_LAPTOP_PRESSURE"] = "kernel memory pressure warn (level 2)"
        try:
            ok, why, _ = A.gov_fits("agent", 1, [], A.gov_machine())
            self.assertEqual((ok, why), (False, "laptop pressure: kernel memory pressure warn (level 2)"))
            self.assertTrue(A.gov_fits("red", 1, [], A.gov_machine())[0])
        finally:
            os.environ["AGENTCTL_LAPTOP_PRESSURE"] = "-"

    def test_outside_load_alone_never_starves_the_queue_one_job_at_a_time(self):
        """2026-11-19 15:35Z: 0 cores charged and a load of ~60 (other agents' git storms, Spotlight, the waiters' own
        polling) held every sbt-warm waiter on the agent load factor for 10+ min. With nothing charged and nothing just
        admitted, the head goes; the next waits while that one ramps up (its load not yet visible)."""
        os.environ["AGENTCTL_GOV_LOAD_PROBE"] = f"echo {A.machine_cores() * 3}"
        self.assertIsNotNone(self.admit("agent", 1, job="keeper"), "nothing charged, nothing ramping: it goes")
        self.assertIsNone(self.admit("agent", 1, job="keeper"), "the next waits while the first ramps up")
        self.aged(self.tickets[0], A.GOV_RAMP + 1)
        self.assertIsNotNone(self.admit("agent", 1, job="keeper"), "a keeper ramped up and charging nothing: the next")

    def test_cpu_load_holds_measurements_then_agents_then_checks_never_red(self):
        cores = A.machine_cores()  # the harness's fixed machine, as the governor reads it
        self.admit("red", 1, job="testOnly")  # something charged: the class load factors hold
        os.environ["AGENTCTL_GOV_LOAD_PROBE"] = f"echo {int(cores * 1.2)}"
        self.assertIsNone(self.admit("measure"))
        self.assertIsNotNone(self.admit("agent"))
        os.environ["AGENTCTL_GOV_LOAD_PROBE"] = f"echo {cores * 3}"
        self.assertIsNone(self.admit("agent"))
        self.assertIsNotNone(self.admit("check"))
        os.environ["AGENTCTL_GOV_LOAD_PROBE"] = f"echo {cores * 5}"
        self.assertIsNone(self.admit("check"), "a check waits past 4x the cores")
        self.assertIsNotNone(self.admit("red"), "a red fix never waits on CPU")

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

    def test_a_waiter_behind_the_head_blocks_cheaply_and_wakes_when_the_head_goes(self):
        """2026-11-19 15:35Z: ~13 sbt-warm waiters each re-read every waiter's liveness (two `ps` per waiter) every 2 s --
        ~170 short processes a second, a load of ~60 on 18 cores that held them all on the load factor. A waiter behind
        the head runs no process while it waits (a stat of the change mark, backing off), and goes as soon as the head
        leaves."""
        head = self.waiter("red", time.time() - 10)
        spawned = []
        saved = A.run_cmd

        def counting(args, *a, **kw):
            spawned.append(args)
            return saved(args, *a, **kw)
        A.run_cmd = counting
        got = []
        th = threading.Thread(target=lambda: got.append(self.admit("agent", 1, wait=True)))
        try:
            th.start()
            time.sleep(1.5)  # 15 polls at 0.1 s
            waiting = len(spawned)
            head.kill()
            head.wait()
            A.gov_bump()  # the head's own process removes its record and marks the change
            th.join(10)
        finally:
            A.run_cmd = saved
        self.assertLessEqual(waiting, 6, f"a waiter behind the head kept spawning: {spawned[:8]}")
        self.assertTrue(got and got[0] is not None, "admitted once the head went")

    def test_a_dead_waiter_holds_nobody_up(self):
        p = self.waiter("red", time.time() - 10)
        p.kill()
        p.wait()
        self.assertIsNotNone(self.admit("agent"))

    def test_a_waiter_whose_pid_was_reused_holds_nobody_up(self):
        """Waiter liveness was the pid alone: a killed waiter's reused pid sat at the head until GovTimeout (2 h)."""
        A.write_json(os.path.join(A.gov_dir(), "w-1.000000-1.json"),
                     {"class": "red", "mem": 1, "label": "gone", "pid": os.getpid(), "at": time.time() - 10,
                      "start": "Thu Jan  1 00:00:00 1970"})  # this pid, but not the process that wrote it
        self.assertIsNotNone(self.admit("agent"))

    def test_the_ticket_exists_before_the_waiter_record_goes(self):
        seen = []
        real = A.os.unlink

        def unlink(p):
            if os.path.basename(p).startswith("w-"):
                seen.append(len(A.gov_tickets()))
            return real(p)
        A.os.unlink = unlink
        try:
            self.assertIsNotNone(self.admit("agent"))
        finally:
            A.os.unlink = real
        self.assertEqual(seen, [1], "the next head never sees neither")

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
                                            f'touch "{flag}"; while [ ! -e "{flag}.release" ]; do sleep 0.05; done'])
        self.procs.append(p)
        deadline = time.time() + 180
        while not os.path.exists(flag) and time.time() < deadline:
            time.sleep(0.05)
        self.assertTrue(os.path.exists(flag), "admitted")
        self.assertTrue(A.lock_held(ticket), "the shell holds the ticket after agentctl exits")
        self.assertEqual([t["label"] for t in A.gov_tickets()], ["shell"])
        open(f"{flag}.release", "w").close()  # the shell ends when the test says, not after a wall-clock sleep
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

    # ---------------------------------------------------------------- cores (the owner, 2026-11-18)
    def test_cores_are_charged_and_hold_work_past_the_machine_cores(self):
        """8 JVMs on 40 of 77 GB drove 18 cores to a load of 100: memory alone over-admits CPU-bound work."""
        cores = A.machine_cores()  # 12, the harness's machine
        for _ in range(cores // 4):
            self.assertIsNotNone(self.admit("agent", 1, job="testUnit"))
        ok, why, on_mem = A.gov_fits("agent", 1, A.gov_tickets(), A.gov_machine(), cores=4)
        self.assertEqual((ok, on_mem), (False, False))
        self.assertIn(f"cores: {cores} charged", why)
        self.assertIsNone(self.admit("agent", 1, job="compile"))
        self.assertIsNone(self.admit("check", 1, kind="check"), "a check is charged its hook's cores too")
        self.assertIsNotNone(self.admit("red", 1, job="testUnit"), "a red fix never waits on cores")

    def test_the_load_counts_when_it_is_above_the_charged_cores(self):
        """What holds no ticket (a browser, Xcode) is in the load; a burst not yet in the 1-min load is in the charge."""
        self.assertIsNotNone(self.admit("agent", 1, job="testUnit"))
        os.environ["AGENTCTL_GOV_LOAD_PROBE"] = "echo 10"
        self.assertIsNone(self.admit("agent", 1, job="testUnit"), "max(4 charged, load 10) + 4 > 12")
        os.environ["AGENTCTL_GOV_LOAD_PROBE"] = "echo 6"
        self.assertIsNotNone(self.admit("agent", 1, job="testUnit"), "max(4, 6) + 4 <= 12")
        os.environ["AGENTCTL_GOV_CPU_FACTOR"] = "2"
        os.environ["AGENTCTL_GOV_LOAD_PROBE"] = "echo 1"
        self.assertIsNotNone(self.admit("agent", 1, job="testUnit"), "the factor scales the cap: 8 + 4 <= 24")

    def test_a_ticket_of_no_cores_never_waits_on_cores(self):
        """A kept server's keeper holds its memory; each call through it takes a cores ticket of its own."""
        for _ in range(3):
            self.admit("agent", 1, job="testUnit")
        self.assertIsNotNone(self.admit("agent", 1, job="keeper"))
        self.assertIsNotNone(self.admit("agent", 1), "no job named (a jvm-gate.sh from before cores): memory only")
        self.assertEqual(A.gov_charged(A.gov_tickets()), 12.0)

    def test_with_nothing_charged_only_the_class_load_factor_holds(self):
        """Outside load alone never starves the queue: the first job goes at what the class factor allows."""
        os.environ["AGENTCTL_GOV_LOAD_PROBE"] = f"echo {int(A.machine_cores() * 1.2)}"
        self.assertIsNotNone(self.admit("agent", 1, job="testUnit"))
        self.assertIsNone(self.admit("agent", 1, job="testUnit"), "now charged: max(4, 14) + 4 > 12")

    def usage(self, job, cores, load1=1.0, n=3, wall=100):
        for _ in range(n):
            A.gov_record_usage(job, wall, cores * wall, load1)

    def test_a_jobs_cores_come_from_its_measured_uncontended_runs(self):
        self.assertEqual(A.gov_job_cores("testUnit"), 4.0, "the default until measured")
        self.usage("testUnit", 6.2, n=2)
        self.assertEqual(A.gov_job_cores("testUnit"), 4.0, "two runs are not enough")
        self.usage("testUnit", 1.0, load1=40, n=5)
        self.assertEqual(A.gov_job_cores("testUnit"), 4.0, "a starved run (load over the cores) says nothing")
        self.usage("testUnit", 6.2, n=1)
        self.assertEqual(A.gov_job_cores("testUnit"), 6.0, "p75 of the uncontended runs, to the half core")
        self.assertIsNone(A.gov_record_usage("testUnit", 5, 50), "a 5 s call is not recorded")
        self.assertEqual(A.gov_job_cores("keeper"), 0.0)

    def test_usage_is_recorded_from_the_command_line(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            A.main(["gov", "usage", "--job", "compile", "--wall", "100", "--cpu", "250", "--load1", "2"])
        self.assertIn("compile: 2.5 cores", out.getvalue())
        self.assertEqual([r["cores"] for r in A.gov_usage()], [2.5])

    def test_the_admission_records_cores_what_it_waited_on_and_its_end(self):
        for _ in range(3):
            self.admit("agent", 1, job="testUnit")
        import threading
        threading.Timer(0.5, self.tickets[0].release).start()
        t = self.admit("agent", 1, job="testUnit", wait=True)
        self.assertIsNotNone(t)
        rows = [json.loads(l) for l in open(os.path.join(A.gov_dir(), "admits.jsonl"))]
        mine = [r for r in rows if r.get("id") == t.rec["id"]][0]
        self.assertEqual((mine["cores"], mine["job"]), (4.0, "testUnit"))
        if mine["waited"] >= 1:
            self.assertIn("cores", mine["waited_on"], "the hold it waited on, not the None of the pass that fit")
        self.assertIn({"end", "id"}, [set(r) for r in rows], "a released ticket's end is recorded")

    def test_waited_on_names_the_hold_after_a_long_wait(self):
        for _ in range(3):
            self.admit("agent", 1, job="testUnit")
        import threading
        threading.Timer(1.3, self.tickets[0].release).start()
        t = self.admit("agent", 1, job="testUnit", wait=True)
        self.assertGreaterEqual(t.rec["waited"], 1)
        self.assertIn("cores: 12 charged", t.rec["waited_on"])

    def lane(self, waiting=()):
        page = os.path.join(self.d, "runners.json")
        A.write_json(page, {"ok": time.time(), "waiting_jobs": [{"label": json.dumps([l]), "repo": "o/r", "run": 1}
                                                                for l in waiting], "running": [], "limits": {}})
        os.environ["AGENTCTL_RUNNERS_PAGE_CACHE"] = page

    def test_a_job_the_governor_would_hold_goes_to_a_free_ci_lane(self):
        self.lane()
        self.assertEqual(A.gov_route("testUnit")[0], "local", "admitted now: runs here")
        for _ in range(3):
            self.admit("agent", 1, job="testUnit")
        where, why = A.gov_route("testUnit")
        self.assertEqual(where, "ci")
        self.assertIn("cores", why)
        self.assertEqual(A.gov_route("testUnit", cls="red")[0], "local", "a red fix is never held")
        self.lane(waiting=["movies-measure"])
        where, why = A.gov_route("testUnit")
        self.assertEqual(where, "local")
        self.assertIn("1 job(s) already wait on movies-measure", why)
        os.environ["AGENTCTL_RUNNERS_PAGE_CACHE"] = os.path.join(self.d, "missing.json")
        self.assertEqual(A.gov_route("testUnit")[0], "local", "an unread lane is no free lane")

    def test_a_waiter_ahead_routes_a_job_to_a_free_lane_too(self):
        self.lane()
        self.waiter("agent", time.time() - 10)
        self.assertEqual(A.gov_route("testUnit")[0], "ci")
        self.assertEqual(A.gov_route("testUnit", cls="check")[0], "local", "a check goes before a waiting agent")

    def test_route_from_the_command_line_exits_0_for_ci(self):
        self.lane()
        for _ in range(3):
            self.admit("agent", 1, job="testUnit")
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            rc = A.main(["gov", "route", "--job", "testUnit"])
        self.assertEqual(rc, 0)
        self.assertTrue(out.getvalue().startswith("ci: "))

    def test_the_replay_keeps_a_burst_under_the_cap(self):
        """05:46Z on 2026-11-18: six waiters admitted within 7 s the moment the lagging load dipped."""
        t0 = 1_000_000.0
        rows = [{"class": "agent", "label": "identity-what-if", "kind": "job", "at": t0 + i, "waited": 600, "id": f"t{i}"}
                for i in range(6)]
        rows.append({"end": t0 + 300, "id": "t0"})
        res = A.gov_replay(rows, 12, t0 + 10, hold=1800)
        self.assertEqual(len(res["old"]), 6)
        self.assertEqual(res["old_peak"], 24.0)
        self.assertEqual(len(res["new"]), 3, "12 cores hold three 4-core what-ifs")
        self.assertEqual(len(res["waiting"]), 2, "the first ended after 300 s (its end record), so a fourth went in")
        self.assertLessEqual(res["new_peak"], 12.0)

    def test_status_shows_the_charged_cores(self):
        self.admit("agent", 1, job="testUnit")
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            A.main(["gov", "status"])
        self.assertIn("4/12 cores charged", out.getvalue())


if __name__ == "__main__":
    unittest.main()
