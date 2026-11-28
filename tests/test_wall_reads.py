"""The wall-read ratchet: lib/agentctl reads real time only through the process's clock (clock.epoch_now, the Clock a
command is handed), or on a line that says why it must not: `# wall: <why>`.

2026-11-26, the fifth flake of one family (test_queue / test_waiting, the leaked wired clock, test_backlog_batch,
OwnerTest, test_recovery's UntilGreenTest): a test stamped on its clock while production code read the wall."""
import os
import re
import unittest

LIB = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "lib", "agentctl")
WALL = re.compile(r"\btime\.(time|monotonic|monotonic_ns|time_ns|sleep|clock_gettime)\b|datetime\.(now|utcnow)\b|"
                  r"\bdate\.today\b|\bClock\(\)")
MARK = "# wall:"
# the clock itself, and the composition root where the real Clock is made
CLOCK_MODULE, COMPOSITION_ROOT = "clock.py", ("cli.py", "cliprocess.py")  # cli and the module holding its process wiring


def wall_reads(root=LIB):
    found = []
    for name in sorted(os.listdir(root)):
        if not name.endswith(".py"):
            continue
        before = ""
        for n, line in enumerate(open(os.path.join(root, name)).read().split("\n"), 1):
            marked, before = MARK in line or before.lstrip().startswith(MARK), line
            code = line.split("#")[0] if line.lstrip().startswith("#") else re.sub(r'""".*|#.*', "", line)
            m = WALL.search(code)
            if not m or marked or name == CLOCK_MODULE or (name in COMPOSITION_ROOT and m.group(0) == "Clock()"):
                continue
            found.append(f"{name}:{n}: {line.strip()}")
    return found


class WallReadsTest(unittest.TestCase):
    def test_no_unmarked_wall_read(self):
        self.assertEqual(wall_reads(), [], "read the process's clock (clock.epoch_now) or mark the line `# wall: <why>`")

    def test_the_lint_sees_a_read_and_a_marker(self):
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            open(os.path.join(d, "x.py"), "w").write("import time\na = time.time()\nb = time.sleep(1)  # wall: real wait\n"
                                                      "c = dt.datetime.now()\n")
            self.assertEqual([f.split(": ")[0] for f in wall_reads(d)], ["x.py:2", "x.py:4"])


if __name__ == "__main__":
    unittest.main()
