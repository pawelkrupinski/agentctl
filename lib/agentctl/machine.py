# agentctl, part `machine`: THE MACHINE PROBE: every read of the machine agentctl runs on (its load, cores, memory, busy
# cores, process table, open files, file table and free disk) through one seam, MachineProbe: HostMachine reads this
# host, FixedMachine is the fixed machine a test pins. The composition root (cli.main) picks one (machine_probe) and
# passes it down as a parameter; nothing below it asks "am I in a test?".
# A real module (lib/agentctl/__init__.py MODULES): its own imports, the standard library's and MODULES' only.

import ctypes
import ctypes.util
import json
import os
import re
import shutil
import sys
from abc import ABC, abstractmethod
from typing import Any, Mapping, Optional, Sequence
from agentctl.statefile import read_text_or
from agentctl.psparse import PsRow, parse_ps
from agentctl.cpu import machine_busy_cores
from agentctl.core import HOME, run_cmd
from agentctl.govknobs import knob_disk_path, knob_machine
from agentctl.procs import lsof_cwds, process_cwds, ps_rows

MEMORY_FIELDS = ("ram_gb", "free_gb", "pressure", "swap_used_mb", "swap_total_mb")


class MachineProbe(ABC):
    """The machine: its RAW READS (abstract, one per source: the only place the two implementations differ) and what
    the readers derive from them (concrete, shared by both: a test's machine answers through the same rules)."""

    @abstractmethod
    def load_averages(self) -> Optional[tuple[float, float]]:
        """The 1- and 5-min load averages (the run queue), or None where unreadable."""

    @abstractmethod
    def cores(self) -> int:
        """The CPU count."""

    @abstractmethod
    def memory(self) -> dict[str, Any]:
        """{ram_gb, free_gb, pressure (% of RAM not available), swap_used_mb, swap_total_mb}, each None when unreadable."""

    @abstractmethod
    def pressure_memory(self) -> dict[str, Any]:
        """The kernel's own memory reading: {free_pct, level, swap_mb}, those it has (one cheap read: laptop_pressure's)."""

    @abstractmethod
    def physical_ram_gb(self) -> float:
        """GB of RAM by the C library's page count: ram_gb's fallback when memory() cannot say."""

    @abstractmethod
    def tick_busy_cores(self) -> Optional[float]:
        """The cores the whole machine is busy on by its own CPU ticks (cpu.py), or None where unreadable."""

    @abstractmethod
    def process_rows(self, cols: tuple[str, ...], pids: tuple[int, ...] = (), wide: bool = False) -> list[PsRow]:
        """THE PROCESS TABLE: every process (or the `pids` asked for) with the ps `cols`; no rows when unreadable."""

    @abstractmethod
    def footprint_kb(self, pid: int) -> Optional[int]:
        """The KB of the process's phys_footprint (what Activity Monitor and memory pressure charge the host; `footprint`),
        or None where unreadable. ps's RSS overcounts a Virtualization.framework guest about 1.8x; this is bounded by its
        guest memory."""

    @abstractmethod
    def process_cwds(self) -> dict[int, str]:
        """{pid: cwd} of the processes worth asking (procs.cwd_candidates), allowed paths only."""

    @abstractmethod
    def cwds_of(self, pids: Sequence[int]) -> dict[int, str]:
        """{pid: cwd} of the `pids` asked for, allowed paths only (pid-scoped: never a machine-wide lsof)."""

    @abstractmethod
    def kept_roots(self, pids: Sequence[int]) -> set[str]:
        """The checkouts the `sbt-warm --keep` processes `pids` keep a server in."""

    @abstractmethod
    def listening_pids(self, ports: Sequence[str]) -> set[int]:
        """The pids listening on any of the TCP `ports`."""

    @abstractmethod
    def file_table(self) -> Optional[tuple[int, int]]:
        """(open files, the most the kernel allows) system-wide, or None where it cannot say."""

    @abstractmethod
    def open_files(self) -> str:
        """`lsof -n -F pc`: every open descriptor with its process ("" when unreadable)."""

    @abstractmethod
    def disk_free_gb(self, path: Optional[str] = None) -> float:
        """GB free on `path` ($AGENTCTL_DISK_PATH, else HOME)."""

    @abstractmethod
    def scratch_roots(self) -> tuple[str, str]:
        """(the temp root, the projects directory) the watchdog's WT counts in."""

    # ------------------------------------------------------------------ derived: the same rules for both
    def load1(self) -> float:
        """The 1-min load average; OSError where unreadable."""
        queues = self.load_averages()
        if queues is None:
            raise OSError("the load average is unreadable")
        return queues[0]

    def run_queue(self) -> Optional[float]:
        """The 1-min load average: the run queue, beside the busy cores (a saturated CPU is pressure only when it is
        oversubscribed too). None where unreadable."""
        queues = self.load_averages()
        return queues[0] if queues else None

    def ram_gb(self) -> float:
        """GB of RAM: memory()'s, else the C library's page count (review 2026-11-26: a hard-coded 36 GB, a silently
        wrong budget on any other machine)."""
        return float(self.memory()["ram_gb"] or self.physical_ram_gb())

    def free_gb(self) -> Optional[float]:
        """GB of RAM a new heap can have now (free + inactive + speculative pages); None when unreadable."""
        free = self.memory()["free_gb"]
        return None if free is None else float(free)

    def sample(self) -> tuple[float, float, Any, Any]:
        """(load1, cpu %, memory pressure %, swap used MB), cheaply; None where unreadable."""
        load1 = self.load1()
        cpu = min(100.0, 100.0 * load1 / self.cores())
        m = self.memory()
        return load1, cpu, m["pressure"], m["swap_used_mb"]


RUSAGE_INFO_V2 = 2
PHYS_FOOTPRINT_OFFSET = 72  # rusage_info_v2: ri_uuid[16], then ri_phys_footprint is the 8th u64


def process_footprint_kb(pid: int) -> Optional[int]:
    """`pid`'s ri_phys_footprint in KB by proc_pid_rusage (libproc, macOS), None where it cannot be read."""
    path = ctypes.util.find_library("proc")
    if not path:
        return None
    buf = ctypes.create_string_buffer(512)
    if ctypes.CDLL(path).proc_pid_rusage(pid, RUSAGE_INFO_V2, buf) != 0:
        return None
    return int.from_bytes(buf.raw[PHYS_FOOTPRINT_OFFSET:PHYS_FOOTPRINT_OFFSET + 8], "little") // 1024


def disk_path(path: Optional[str]) -> str:
    """The path a free-disk reading is of: the one named, else $AGENTCTL_DISK_PATH, else HOME."""
    return path or knob_disk_path() or HOME


def read_meminfo(path: str = "/proc/meminfo") -> dict[str, int]:
    """{field: kB} of Linux's /proc/meminfo ({} elsewhere)."""
    return {m.group(1): int(m.group(2)) for m in re.finditer(r"^(\w+):\s+(\d+)", read_text_or(path, ""), re.M)}


def meminfo_memory(mi: Mapping[str, int]) -> dict[str, Any]:
    """memory()'s fields from a Linux /proc/meminfo dict (MemAvailable), those it has."""
    out: dict[str, Any] = dict.fromkeys(MEMORY_FIELDS)
    out["ram_gb"] = mi["MemTotal"] / 2 ** 20
    if "MemAvailable" in mi:
        out["free_gb"] = mi["MemAvailable"] / 2 ** 20
        out["pressure"] = round(100 * (1 - mi["MemAvailable"] / mi["MemTotal"]))
    if "SwapTotal" in mi and "SwapFree" in mi:
        out["swap_total_mb"] = mi["SwapTotal"] / 1024
        out["swap_used_mb"] = (mi["SwapTotal"] - mi["SwapFree"]) / 1024
    return out


def macos_memory(memsize: str, vm_stat: str, level: str, swapusage: str) -> dict[str, Any]:
    """memory()'s fields from macOS's sysctl hw.memsize, vm_stat, kern.memorystatus_level (memory_pressure's free %)
    and vm.swapusage, each None when unreadable."""
    out: dict[str, Any] = dict.fromkeys(MEMORY_FIELDS)
    if memsize.strip().isdigit():
        out["ram_gb"] = int(memsize.strip()) / 2 ** 30
    page = re.search(r"page size of (\d+) bytes", vm_stat)
    if page:
        pages = sum(int(m.group(2)) for m in re.finditer(r"Pages (free|inactive|speculative):\s+(\d+)", vm_stat))
        out["free_gb"] = pages * int(page.group(1)) / 2 ** 30
    if level.strip().isdigit():
        out["pressure"] = 100 - int(level.strip())
    tot, used = re.search(r"total = ([\d.]+)M", swapusage), re.search(r"used = ([\d.]+)M", swapusage)
    if tot and used:
        out["swap_total_mb"], out["swap_used_mb"] = float(tot.group(1)), float(used.group(1))
    return out


def meminfo_pressure(mi: Mapping[str, int]) -> dict[str, Any]:
    """pressure_memory()'s fields (free_pct, swap_mb) a Linux /proc/meminfo dict gives, those it has."""
    got: dict[str, Any] = {}
    if "MemAvailable" in mi:
        got["free_pct"] = round(100 * mi["MemAvailable"] / mi["MemTotal"])
    if "SwapTotal" in mi and "SwapFree" in mi:
        got["swap_mb"] = (mi["SwapTotal"] - mi["SwapFree"]) / 1024
    return got


def sysctl_pressure(text: str) -> dict[str, Any]:
    """pressure_memory()'s fields (free_pct, level, swap_mb) macOS's one sysctl call gives (kern.memorystatus_level,
    kern.memorystatus_vm_pressure_level, vm.swapusage), those it has."""
    lines = text.splitlines()
    got: dict[str, Any] = {}
    if len(lines) >= 2 and lines[0].strip().isdigit() and lines[1].strip().isdigit():
        got["free_pct"], got["level"] = int(lines[0]), int(lines[1])
    used = re.search(r"used = ([\d.]+)M", "\n".join(lines))
    if used:
        got["swap_mb"] = float(used.group(1))
    return got


class HostMachine(MachineProbe):
    """This host: macOS's sysctl and vm_stat, Linux's /proc (ci-runner, where a check may fall back to running locally),
    ps and lsof. `meminfo` is the /proc/meminfo path (another file stands for it in a test of the Linux reading)."""

    def __init__(self, meminfo: str = "/proc/meminfo") -> None:
        self.meminfo = meminfo

    def load_averages(self) -> Optional[tuple[float, float]]:
        try:
            la = os.getloadavg()
        except OSError:
            return None
        return la[0], la[1]

    def cores(self) -> int:
        return os.cpu_count() or 1

    def memory(self) -> dict[str, Any]:
        mi = read_meminfo(self.meminfo)
        if mi.get("MemTotal"):
            return meminfo_memory(mi)
        return macos_memory(self.sysctl("hw.memsize"), str(run_cmd(["vm_stat"], timeout=10).stdout),
                            self.sysctl("kern.memorystatus_level"), self.sysctl("vm.swapusage"))

    @staticmethod
    def sysctl(key: str) -> str:
        return str(run_cmd(["sysctl", "-n", key], timeout=10).stdout)

    def pressure_memory(self) -> dict[str, Any]:
        mi = read_meminfo(self.meminfo)
        if mi.get("MemTotal"):
            return meminfo_pressure(mi)
        if sys.platform != "darwin":
            return {}
        return sysctl_pressure(str(run_cmd(["sysctl", "-n", "kern.memorystatus_level",
                                            "kern.memorystatus_vm_pressure_level", "vm.swapusage"], timeout=10).stdout))

    def physical_ram_gb(self) -> float:
        return float(os.sysconf("SC_PHYS_PAGES") * os.sysconf("SC_PAGE_SIZE") / 2 ** 30)

    def tick_busy_cores(self) -> Optional[float]:
        return machine_busy_cores(self.cores())

    def process_rows(self, cols: tuple[str, ...], pids: tuple[int, ...] = (), wide: bool = False) -> list[PsRow]:
        return ps_rows(cols, pids, wide)

    def footprint_kb(self, pid: int) -> Optional[int]:
        return process_footprint_kb(pid)

    def process_cwds(self) -> dict[int, str]:
        return process_cwds()

    def cwds_of(self, pids: Sequence[int]) -> dict[int, str]:
        return lsof_cwds(pids) if pids else {}

    def kept_roots(self, pids: Sequence[int]) -> set[str]:
        return set(lsof_cwds(pids).values()) if pids else set()

    def listening_pids(self, ports: Sequence[str]) -> set[int]:
        found: set[int] = set()
        for port in ports:
            r = run_cmd(["lsof", "-nP", f"-iTCP:{port}", "-sTCP:LISTEN", "-t"])
            found |= {int(x) for x in r.stdout.split() if x.isdigit()}
        return found

    def file_table(self) -> Optional[tuple[int, int]]:
        try:
            r = run_cmd(["sysctl", "-n", "kern.num_files", "kern.maxfiles"], timeout=10)
            num, most = (int(x) for x in r.stdout.split())
        except (OSError, ValueError):
            return None
        return (num, most) if r.returncode == 0 and most > 0 else None

    def open_files(self) -> str:
        try:
            return str(run_cmd(["lsof", "-n", "-F", "pc"], timeout=60).stdout)
        except OSError:
            return ""

    def disk_free_gb(self, path: Optional[str] = None) -> float:
        return shutil.disk_usage(disk_path(path)).free / 1e9

    def scratch_roots(self) -> tuple[str, str]:
        return "/tmp", os.path.join(HOME, "projects")


class FixedMachine(MachineProbe):
    """A FIXED MACHINE: the tests' one seam (tests/harness.py pins an idle 12-core 36 GB machine, so no test depends on
    the runner it runs on -- 2026-10-31: GitHub's 4-core hosted runner, with its own load and RAM, failed a dozen
    governor, check-cap and land tests that pass on the laptop and on ci-runner). Its fields: ram_gb, free_gb, pressure,
    swap_used_mb, swap_total_mb, load1, load5 (default load1), cores (default 12), busy (default load1; null with
    no load1: unreadable, read from its ps rows), free_pct, level, disk_free_gb (default 500, whatever the path), physical_ram_gb (default 36), ps (rows
    of the columns ps_cols names), footprints ({pid: phys_footprint KB}), cwds ({pid: cwd}), kept_roots ([checkout]), listeners ([pid]), file_table ([open, most]), open_files (lsof's text), tmp_root,
    projects. `path`: a JSON file of them, read again at every reading (a test's other process changes it)."""

    PS_COLS = ("pid", "ppid", "rss", "pcpu", "comm")

    def __init__(self, fields: Optional[dict[str, Any]] = None, path: Optional[str] = None) -> None:
        self._fields, self.path = dict(fields or {}), path

    def fields(self) -> dict[str, Any]:
        if self.path is None:
            return self._fields
        try:
            got = json.loads(read_text_or(self.path, "{}"))
        except ValueError:
            return {}
        return got if isinstance(got, dict) else {}

    def update(self, **fields: Any) -> "FixedMachine":
        """The fields changed in place (a test's machine as it changes); returns itself."""
        self._fields.update(fields)
        return self

    def load_averages(self) -> Optional[tuple[float, float]]:
        f = self.fields()
        if f.get("load1") is None:
            return None
        return float(f["load1"]), float(f.get("load5", f["load1"]))

    def cores(self) -> int:
        return int(self.fields().get("cores") or 12)

    def memory(self) -> dict[str, Any]:
        f = self.fields()
        return {k: f.get(k) for k in MEMORY_FIELDS}

    def pressure_memory(self) -> dict[str, Any]:
        f = self.fields()
        return {"free_pct": f.get("free_pct"), "level": f.get("level"), "swap_mb": f.get("swap_used_mb")}

    def physical_ram_gb(self) -> float:
        return float(self.fields().get("physical_ram_gb") or 36)

    def tick_busy_cores(self) -> Optional[float]:
        f = self.fields()
        busy = f.get("busy", f.get("load1"))
        return None if busy is None else float(busy)

    def process_rows(self, cols: tuple[str, ...], pids: tuple[int, ...] = (), wide: bool = False) -> list[PsRow]:
        f = self.fields()
        rows = parse_ps(str(f.get("ps") or ""), tuple(f.get("ps_cols") or self.PS_COLS))
        return [r for r in rows if r.pid in pids] if pids else rows

    def footprint_kb(self, pid: int) -> Optional[int]:
        kb = (self.fields().get("footprints") or {}).get(str(pid))
        return None if kb is None else int(kb)

    def process_cwds(self) -> dict[int, str]:
        return {int(pid): str(cwd) for pid, cwd in (self.fields().get("cwds") or {}).items()}

    def cwds_of(self, pids: Sequence[int]) -> dict[int, str]:
        return self.process_cwds() if pids else {}  # the fixture's table, whichever pids are asked

    def kept_roots(self, pids: Sequence[int]) -> set[str]:
        return {str(root) for root in self.fields().get("kept_roots") or ()}

    def listening_pids(self, ports: Sequence[str]) -> set[int]:
        return {int(pid) for pid in self.fields().get("listeners") or ()}

    def file_table(self) -> Optional[tuple[int, int]]:
        table: Sequence[int] = self.fields().get("file_table") or ()
        return (int(table[0]), int(table[1])) if len(table) == 2 else None

    def open_files(self) -> str:
        return str(self.fields().get("open_files") or "")

    def disk_free_gb(self, path: Optional[str] = None) -> float:
        """Its disk_free_gb (default 500) on whatever path."""
        return float(self.fields().get("disk_free_gb", 500))

    def scratch_roots(self) -> tuple[str, str]:
        f = self.fields()
        return f.get("tmp_root") or os.path.join(HOME, ".no-tmp"), f.get("projects") or os.path.join(HOME, ".no-projects")


def process_machine() -> MachineProbe:
    """The process's machine: machine_probe of $AGENTCTL_MACHINE (govknobs.knob_machine)."""
    return machine_probe(knob_machine())


def machine_probe(raw: str) -> MachineProbe:
    """THE PROCESS'S MACHINE (cli.main's, from $AGENTCTL_MACHINE: govknobs.knob_machine): a fixed machine -- its JSON,
    or `@<file>` of it, read at every reading -- else this host. A malformed value is this host."""
    if raw.startswith("@"):
        return FixedMachine(path=raw[1:])
    try:
        fields = json.loads(raw) if raw else None
    except ValueError:
        fields = None
    return FixedMachine(fields) if isinstance(fields, dict) else HostMachine()
