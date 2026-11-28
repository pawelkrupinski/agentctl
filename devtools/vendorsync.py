#!/usr/bin/env python3
"""devtools/vendorsync.py [--result FILE] [--wait S]: sync the vendored copies from the movies checkout WITH NO AGENT.

A sync is mechanical (devtools/vendor.py: copy movies' files byte for byte, record their digests), and a correct one
needs no LLM session and no full suite. 2026-11-27: the VENDOR_DRIFT condition stayed open ~3 h; two Sonnet fixers each
committed the right 5-file sync and hit their 60-min wall time without pushing (the full pre-push suite timed out at
load 25-45), and drifted into side fixes. So the laptop's sync loop (lib/agentctl/vendordrift.py) runs THIS first:

  1. a detached worktree of origin/main (no branch, nothing to delete)
  2. `vendor.py sync`; nothing changed -> done
  3. the scoped tests (devtools/vendorscope.py: test_vendored and the tests importing a changed copy), the simulator gate
     when the simulator imports one, the ratchets' selftest and check
  4. a commit of exactly the changed copies and VENDORED.tsv, pushed with devtools/selfpush.py (the pre-push hook scopes
     its suite the same way: a vendored-only push runs the scoped tests)
  5. the worktree is removed

Exit 0 and `done` when synced and pushed; exit 3 and `failed: <why>` when a scoped test or ratchet fails -- a real API
change to adapt, which is what the VENDOR_DRIFT agent is for (its brief carries the why). --result FILE gets the
verdict as JSON (state, why, at), written last."""
import argparse
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(ROOT, "lib"))
import vendor  # noqa: E402
import vendorscope  # noqa: E402
from agentctl.clock import epoch_now  # noqa: E402
from agentctl.procrunner import ProcessRunner  # noqa: E402
from agentctl.statefile import write_json  # noqa: E402

FAILED_EXIT = 3
TAIL = 1200  # characters of a failing step's output kept in the verdict
TEST_JOBS = "4"


def run_command(argv, cwd):
    """(exit status, the output's tail) of a command, unbounded: the caller's bound is the sync loop's."""
    r = ProcessRunner().run(argv, cwd=cwd, capture_output=True, text=True, timeout=None)
    return r.returncode, (r.stdout + r.stderr)[-TAIL:]


class VendorSync:
    """The steps, over `run(argv, cwd) -> (status, output)` and the paths: a test hands a fake runner."""

    def __init__(self, run, root, tree, wait, read_tests=vendorscope.read_tests):
        self.run, self.root, self.tree, self.wait, self.read_tests = run, root, tree, wait, read_tests

    def step(self, what, argv, cwd=None):
        """The step's output, or raises Failed(why) with its tail."""
        status, out = self.run(argv, cwd or self.tree)
        if status:
            raise Failed(f"{what} (exit {status}): {out.strip()}")
        return out

    def python(self, what, *args):
        return self.step(what, ["python3", *args])

    def changed(self):
        out = self.step("git status", ["git", "status", "--porcelain", "--untracked-files=no"])
        return [line[3:] for line in out.splitlines() if line.strip()]

    def verify(self, changed):
        """The scoped tests, the simulator gate when reached, the ratchets: raises Failed on the first that fails."""
        copies = [c for c, _s, _d in vendor.read_table(self.tree)]
        kind, sim, tests = vendorscope.scope(changed, copies, self.read_tests(self.tree))
        if kind != "scoped":
            raise Failed(f"the sync changed more than the vendored copies: {changed}")
        self.python("the scoped tests", "tests/run.py", "-j", TEST_JOBS, *tests)
        if sim:
            self.python("the simulator gate", "tests/sim_gate.py")
        tools = self.python("the quality tools", "quality/tools.py").strip().splitlines()[-1]
        self.step("the ratchets", [tools, "quality/ratchet.py", "selftest"])
        self.step("the ratchets", [tools, "quality/ratchet.py", "check"])

    def execute(self):
        """('done', why) or raises Failed."""
        self.step("git fetch", ["git", "fetch", "-q", "origin", "main"], self.root)
        self.step("the worktree", ["git", "worktree", "add", "-q", "--detach", self.tree, "origin/main"], self.root)
        try:
            self.python("vendor.py sync", "devtools/vendor.py", "sync")
            changed = self.changed()
            if not changed:
                return "nothing to sync"
            self.verify(changed)
            self.step("git add", ["git", "add", "--", *changed])
            self.step("git commit", ["git", "commit", "-q", "-m", "Vendor sync: the copies equal the movies checkout's files again\n\n"
                                     "Mechanical (devtools/vendorsync.py): copied byte for byte, the scoped tests, the simulator gate where "
                                     "it imports a copy and the ratchets green."])
            self.python("selfpush", "devtools/selfpush.py", "--wait", str(self.wait))
            return "synced: " + ", ".join(changed)
        finally:
            self.run(["git", "worktree", "remove", "--force", self.tree], self.root)


class Failed(Exception):
    pass


def verdict(sync, now):
    try:
        return {"state": "done", "why": sync.execute(), "at": now}
    except Failed as why:
        return {"state": "failed", "why": str(why), "at": now}


def main(argv):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--result", help="write the verdict (JSON) here")
    ap.add_argument("--wait", type=float, default=1500, help="seconds to wait for the push lock")
    a = ap.parse_args(argv)
    tree = os.path.join(os.path.dirname(ROOT), os.path.basename(ROOT) + "-wt-vendor-sync")
    sync = VendorSync(run_command, ROOT, tree, a.wait)
    sync.run(["git", "worktree", "remove", "--force", tree], ROOT)  # a leftover of a killed run
    result = verdict(sync, epoch_now())
    print(f"vendorsync: {result['state']}: {result['why']}")
    if a.result:
        write_json(a.result, result)
    return 0 if result["state"] == "done" else FAILED_EXIT


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
