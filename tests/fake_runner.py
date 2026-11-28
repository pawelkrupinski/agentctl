"""FakeRunner: the process runner a test wires in place of the real one (lib/agentctl/procrunner.py: THE PROCESS RUNNER
IS PASSED IN). run / popen: the test's stand-ins for subprocess.run / Popen, each called as (argv, **kw); one left out
runs the real process. It shares ProcessRunner's interface and its real calls, nothing else."""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "lib"))
from agentctl.procrunner import ProcessRunner  # noqa: E402


class FakeRunner(ProcessRunner):
    def __init__(self, run=None, popen=None):
        super().__init__()
        self.fake_run, self.fake_popen = run, popen

    def run(self, argv, **kw):
        return self.fake_run(argv, **kw) if self.fake_run else super().run(argv, **kw)

    def popen(self, argv, **kw):
        return self.fake_popen(argv, **kw) if self.fake_popen else super().popen(argv, **kw)
