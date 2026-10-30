"""Logs rotate by size (2026-10-30: the laptop processor's service.log had grown to 141 MB, never rotated): copy-truncate
in place, the newest part kept as .1, older parts shifted, an O_APPEND writer goes on writing; /log still serves it."""
import os, sys, unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from test_agentctl import A  # noqa: E402
from test_status_page import Base  # noqa: E402


def lines(a, b):
    return "".join(f"line {i:06d} {'x' * 40}\n" for i in range(a, b)).encode()


class LogRotateTest(Base):
    def log(self, rel, data):
        p = os.path.join(self.env["AGENTCTL_STATE_ROOT"], rel)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "wb") as f:
            f.write(data)
        return p

    def test_an_oversized_log_keeps_its_newest_part_and_its_writer(self):
        p = self.log("waiting/x/service.log", lines(0, 3000))  # ~150 KB
        fd = os.open(p, os.O_WRONLY | os.O_APPEND)  # launchd's StandardOutPath: held open, appending
        self.addCleanup(os.close, fd)
        self.assertIsNone(A.rotate_log(p, max_bytes=1 << 20, keep=3))  # under the size: untouched
        said = A.rotate_log(p, max_bytes=10_000, keep=3)
        self.assertIn("kept as .1", said)
        one = open(p + ".1", "rb").read()
        self.assertLessEqual(len(one), 10_000)
        self.assertTrue(one.startswith(b"line "), "from a line start")
        self.assertTrue(one.endswith(lines(2999, 3000)), "the newest line kept")
        self.assertEqual(os.path.getsize(p), 0)
        os.write(fd, b"after the rotation\n")
        self.assertEqual(open(p, "rb").read(), b"after the rotation\n", "the writer goes on at the new end, no hole")

    def test_parts_shift_and_only_keep_are_kept(self):
        p = self.log("gov/gov.log", b"")
        for gen in range(5):
            with open(p, "ab") as f:
                f.write(lines(gen * 1000, gen * 1000 + 300))
            A.rotate_log(p, max_bytes=1000, keep=3)
        parts = sorted(x for x in os.listdir(os.path.dirname(p)) if x.startswith("gov.log"))
        self.assertEqual(parts, ["gov.log", "gov.log.1", "gov.log.2", "gov.log.3"])
        self.assertIn(b"line 004299", open(p + ".1", "rb").read())
        self.assertIn(b"line 003299", open(p + ".2", "rb").read())
        self.assertIn(b"line 002299", open(p + ".3", "rb").read())

    def test_every_agentctl_log_but_a_checks_own_output(self):
        root = self.env["AGENTCTL_STATE_ROOT"]
        want = [self.log("waiting/x/service.log", b"a"), self.log("reds/7/claude-1.log", b"a"), self.log("upkeep.log", b"a")]
        self.log("queue/movies/checks/abc.d/unit/sbt.log", b"a")
        self.log("queue/movies/work/target/x/sbt.log", b"a")
        self.log("notes.txt", b"a")
        got = [p for p in A.agentctl_logs() if p.startswith(os.path.realpath(root))]
        self.assertEqual(sorted(got), sorted(want))
        big = self.log("waiting/x/service.log", lines(0, 1000))
        os.environ["AGENTCTL_LOG_ROTATE_MAX"] = "5000"
        try:
            out = A.rotate_logs()
        finally:
            os.environ.pop("AGENTCTL_LOG_ROTATE_MAX")
        self.assertEqual(len(out), 1, out)
        self.assertTrue(os.path.exists(big + ".1"))
        self.assertTrue(os.path.exists(os.path.join(root, "log-rotate.lock")))

    def test_the_log_viewer_serves_a_rotated_part_and_the_end_of_it_before_a_short_file(self):
        p = self.log("waiting/x/service.log", lines(0, 500))
        A.rotate_log(p, max_bytes=5000, keep=3)
        with open(p, "ab") as f:
            f.write(b"new line\n")
        self.assertEqual(A.status_log_path(p + ".1"), os.path.realpath(p + ".1"))
        body, size = A.status_log_body(p)
        self.assertEqual(size, 9)
        self.assertTrue(body.endswith(b"line 000499 " + b"x" * 40 + b"\n\n---- rotated: the above is the end of "
                                      b"service.log.1 ----\nnew line\n"), body[-200:])


if __name__ == "__main__":
    unittest.main()
