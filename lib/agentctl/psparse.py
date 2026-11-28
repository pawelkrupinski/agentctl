# agentctl, module `psparse`: THE PS PARSER: one reading of `ps -o <cols>=` output and of ps's times.
# A real module (lib/agentctl/__init__.py MODULES): its own imports, the standard library's only.
#
# THE THREE CPU DEFINITIONS (distinct on purpose; every reader says which it means):
#   cpu_seconds (ps `time`)  cumulative CPU a process has USED, in seconds: a land step's progress (land.group_cpu_seconds),
#                            a JVM's work over time (jvmreap, maint.ps_cpu_tree). Parsed by tosec.
#   pcpu (ps `pcpu`, `%cpu`) the share of one core a process uses NOW (decayed over its life): the governor's busy cores
#                            (pressure.ps_busy_cores), procwatch's samples, jvmreap's "busy now" look.
#   cpu ticks (cpu.cpu_ticks) the whole machine's busy and total scheduler ticks, from the kernel, not ps: the load
#                            judged without a process list (cpu.py).

from dataclasses import dataclass
import re
from typing import Any, Callable


def tosec(t: Any) -> float:
    """ps's [[dd-]hh:]mm:ss[.frac] (etime) or unbounded mm:ss (cputime) -> seconds. THE CPU-TIME PARSER; 0.0 when garbled."""
    dd = 0
    t = str(t)
    if "-" in t:
        d_, t = t.split("-", 1)
        dd = int(d_ or 0)
    s = 0.0
    for part in t.split(":"):
        try:
            s = s * 60 + float(part)
        except ValueError:
            return 0.0
    return s + dd * 86400


@dataclass(frozen=True)
class PsRow:
    """One process of a `ps` listing: the columns asked for are set, the rest keep their empty defaults."""
    pid: int = 0
    ppid: int = 0
    pgid: int = 0
    rss: int = 0  # KB
    pcpu: float = 0.0
    cpu_seconds: float = 0.0
    etime: str = ""
    tty: str = ""
    cputime: str = ""  # ps's own text of the CPU time (`cpu_seconds` is the number)
    stat: str = ""
    comm: str = ""
    args: str = ""  # `command` and `args` alike


# ps column -> (PsRow field, converter). A converter raising ValueError skips the row.
COLUMNS: dict[str, tuple[str, Callable[[str], Any]]] = {
    "pid": ("pid", int), "ppid": ("ppid", int), "pgid": ("pgid", int), "rss": ("rss", int), "pcpu": ("pcpu", float),
    "%cpu": ("pcpu", float), "time": ("cpu_seconds", tosec), "etime": ("etime", str), "tty": ("tty", str), "cputime": ("cputime", str), "stat": ("stat", str),
    "comm": ("comm", str), "command": ("args", str), "args": ("args", str)}
FREE_TEXT = ("comm", "command", "args")  # a last column that may hold spaces
SPACE_FREE = re.compile(r"\s+")


def parse_ps(text: str, cols: tuple[str, ...]) -> list[PsRow]:
    """The rows of `ps -o col=,col=...` output (`cols` in order). A last column of free text (comm, command, args) keeps
    its spaces; a missing last `stat` reads "". A row with too few or too many fields, or a field its column cannot read
    (a pid that is no number), is skipped -- never an error: ps's output races the processes it lists."""
    n, free = len(cols), cols[-1] in FREE_TEXT
    rows = []
    for line in text.splitlines():
        parts = line.split(None, n - 1) if free else line.split()
        if cols[-1] == "stat" and len(parts) == n - 1:
            parts.append("")
        if len(parts) != n:
            continue
        try:
            rows.append(PsRow(**{COLUMNS[c][0]: COLUMNS[c][1](v.strip()) for c, v in zip(cols, parts)}))
        except ValueError:
            continue
    return rows
