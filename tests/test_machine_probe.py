"""THE MACHINE PROBE'S ONE CONTRACT (lib/agentctl/machine.py): the same assertions over HostMachine (this host: its
load, cores, memory, ps, lsof, sysctl and disk) and FixedMachine (the tests' fixed machine), so a reader taking a
MachineProbe -- the governor, the watchdog's invariants, the pressure reading -- is unit-tested on the fake and means the
same on the laptop. The derived readings (load1, run_queue, ram_gb, free_gb, sample) are the base class's, shared by
both; each implementation answers only the raw reads."""
import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from test_agentctl import A  # noqa: E402,F401 (the harness: HOME, PATH; puts lib on the path)
from harness import fixed_machine  # noqa: E402
from agentctl import machine  # noqa: E402


class Contract:
    """The assertions both implementations meet; `probe()` is the one under test."""

    def probe(self) -> machine.MachineProbe:
        raise NotImplementedError

    def test_the_load_averages_are_a_pair_or_unreadable_and_load1_is_the_first(self):
        p = self.probe()
        queues = p.load_averages()
        if queues is None:
            self.assertRaises(OSError, p.load1)
            self.assertIsNone(p.run_queue())
        else:
            self.assertEqual(len(queues), 2)
            self.assertTrue(all(isinstance(q, float) and q >= 0 for q in queues))
            self.assertEqual(p.load1(), queues[0])
            self.assertEqual(p.run_queue(), queues[0])

    def test_cores_are_a_positive_count(self):
        self.assertIsInstance(self.probe().cores(), int)
        self.assertGreaterEqual(self.probe().cores(), 1)

    def test_memory_has_every_field_and_ram_is_known(self):
        p = self.probe()
        self.assertEqual(set(p.memory()), set(machine.MEMORY_FIELDS))
        self.assertGreater(p.ram_gb(), 0)
        free = p.free_gb()
        self.assertTrue(free is None or free >= 0)
        self.assertLessEqual(set(p.pressure_memory()), {"free_pct", "level", "swap_mb"})

    def test_a_sample_is_load_cpu_pressure_and_swap(self):
        load1, cpu, _pressure, _swap = self.probe().sample()
        self.assertGreaterEqual(load1, 0)
        self.assertTrue(0 <= cpu <= 100)

    def test_the_process_table_holds_the_pids_asked_for_alone(self):
        p = self.probe()
        rows = p.process_rows(("pid", "ppid", "pcpu", "args"))
        self.assertTrue(all(isinstance(r, A.PsRow) for r in rows))
        mine = p.process_rows(("pid", "ppid", "pcpu", "args"), pids=(os.getpid(),))
        self.assertEqual([r.pid for r in mine], [os.getpid()])

    def test_cwds_file_table_open_files_disk_and_scratch_roots(self):
        p = self.probe()
        self.assertTrue(all(isinstance(k, int) and isinstance(v, str) for k, v in p.process_cwds().items()))
        table = p.file_table()
        self.assertTrue(table is None or (len(table) == 2 and table[1] > 0))
        self.assertIsInstance(p.open_files(), str)
        self.assertGreaterEqual(p.disk_free_gb(), 0)
        tmp, projects = p.scratch_roots()
        self.assertTrue(isinstance(tmp, str) and isinstance(projects, str))

    def test_the_jvm_scans_reads_are_the_cwds_kept_roots_and_listeners_of_the_pids_asked(self):
        p = self.probe()
        self.assertTrue(all(isinstance(k, int) and isinstance(v, str) for k, v in p.cwds_of([os.getpid()]).items()))
        self.assertEqual(p.cwds_of([]), {})
        self.assertTrue(all(isinstance(d, str) for d in p.kept_roots([os.getpid()])))
        self.assertTrue(all(isinstance(x, int) for x in p.listening_pids(["9"])))


class HostMachineTest(Contract, unittest.TestCase):
    def probe(self):
        return machine.HostMachine()

    def test_linux_reads_proc_meminfo(self):
        with tempfile.NamedTemporaryFile("w", suffix="meminfo", delete=False) as f:
            f.write("MemTotal:       33554432 kB\nMemFree:  1000 kB\nMemAvailable:    8388608 kB\n"
                    "SwapTotal:       2097152 kB\nSwapFree:        1048576 kB\n")
        try:
            p = machine.HostMachine(meminfo=f.name)
            m = p.memory()
            self.assertEqual((m["ram_gb"], m["free_gb"], m["pressure"]), (32.0, 8.0, 75))
            self.assertEqual((m["swap_used_mb"], m["swap_total_mb"]), (1024.0, 2048.0))
            self.assertEqual(p.ram_gb(), 32.0)
            self.assertEqual(p.pressure_memory(), {"free_pct": 25, "swap_mb": 1024.0})
        finally:
            os.unlink(f.name)

    def test_macos_readings_parse_sysctl_and_vm_stat(self):
        m = machine.macos_memory("38654705664\n", "Mach Virtual Memory Statistics: (page size of 16384 bytes)\n"
                                 "Pages free:  65536.\nPages inactive: 65536.\nPages speculative: 0.\n", "40\n",
                                 "total = 2048.00M  used = 512.50M  free = 1535.50M")
        self.assertEqual(m, {"ram_gb": 36.0, "free_gb": 2.0, "pressure": 60, "swap_total_mb": 2048.0,
                             "swap_used_mb": 512.5})
        self.assertEqual(machine.sysctl_pressure("52\n2\nvm.swapusage: total = 1024.00M  used = 311.5M  free = 1.00M"),
                         {"free_pct": 52, "level": 2, "swap_mb": 311.5})
        self.assertEqual(machine.sysctl_pressure("x\n1"), {})


class FixedMachineTest(Contract, unittest.TestCase):
    def probe(self):
        return fixed_machine(ps=f"{os.getpid()} 1 0.5 python3 tests\n4242 1 12.0 java -jar sbt-launch.jar\n",
                             ps_cols=["pid", "ppid", "pcpu", "args"], cwds={str(os.getpid()): "/tmp"},
                             kept_roots=["/tmp"], listeners=[4242],
                             file_table=[1000, 100000], open_files="p1\ncpython3\nf3\n")

    def test_ram_unread_is_the_page_count_not_a_fixed_36_gb(self):
        """Review 2026-11-26 (D I3): ram_gb fell back to a hard-coded 36 GB when its probes read nothing; the fallback is
        the shared rule's, the page count each machine answers."""
        self.assertEqual(machine.FixedMachine({"physical_ram_gb": 64}).ram_gb(), 64.0)
        self.assertEqual(machine.FixedMachine({"ram_gb": 18, "physical_ram_gb": 64}).ram_gb(), 18.0)

    def test_busy_cores_are_its_load_unless_named_and_a_load5_its_load1(self):
        self.assertEqual(machine.FixedMachine({"load1": 3.0}).tick_busy_cores(), 3.0)
        self.assertEqual(machine.FixedMachine({"load1": 3.0, "busy": 7.5}).tick_busy_cores(), 7.5)
        self.assertIsNone(machine.FixedMachine({}).tick_busy_cores())
        self.assertEqual(machine.FixedMachine({"load1": 3.0}).load_averages(), (3.0, 3.0))
        self.assertEqual(machine.FixedMachine({"load1": 3.0, "load5": 9.0}).load_averages(), (3.0, 9.0))

    def test_a_file_machine_is_read_again_at_every_reading(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "machine.json")
            with open(path, "w") as f:
                json.dump({"free_gb": 30}, f)
            p = machine.machine_probe("@" + path)
            self.assertEqual(p.free_gb(), 30.0)
            with open(path, "w") as f:
                json.dump({"free_gb": 0}, f)
            self.assertEqual(p.free_gb(), 0.0, "another process changed the machine")


class MachineProbeOfTest(unittest.TestCase):
    """The composition root's choice (cli.main: machine_probe(knob_machine()))."""

    def test_json_is_a_fixed_machine_and_anything_else_this_host(self):
        self.assertIsInstance(machine.machine_probe('{"cores": 4}'), machine.FixedMachine)
        self.assertEqual(machine.machine_probe('{"cores": 4}').cores(), 4)
        for raw in ("", "not json", "[1, 2]"):
            self.assertIsInstance(machine.machine_probe(raw), machine.HostMachine, raw)


if __name__ == "__main__":
    unittest.main()
