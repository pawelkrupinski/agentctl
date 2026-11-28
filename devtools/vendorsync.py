#!/usr/bin/env python3
"""devtools/vendorsync.py [--result FILE] [--wait S]: sync the vendored copies from the movies checkout WITH NO AGENT.

A sync is mechanical (devtools/vendor.py: copy movies' files byte for byte, record their digests), and a correct one
needs no LLM session and no full suite. 2026-11-27: the VENDOR_DRIFT condition stayed open ~3 h; two Sonnet fixers each
committed the right 5-file sync and hit their 60-min wall time without pushing (the full pre-push suite timed out at
load 25-45), and drifted into side fixes. So the laptop's sync loop (lib/agentctl/vendordrift.py) runs THIS first:

  1. a detached worktree of origin/main (no branch, nothing to delete)
  2. `vendor.py sync`; nothing changed -> done
  3. the scoped tests (devtools/vendorscope.py: test_vendored and the tests importing a changed copy), the simulator gate
     when the simulator imports one, the ratchets' selftest and check; a check the new copies refuse is answered by
     `ratchet.py update` when what it records is only FALLS (movies' fall lock, 2a65c3fb37: an unrecorded fall refuses),
     the baselines then committed with the copies
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
from agentctl.statefile import read_text_or, write_json  # noqa: E402

FAILED_EXIT = 3
TAIL = 1200  # characters of a failing step's output kept in the verdict
TEST_JOBS = "4"


def run_command(argv, cwd):
    """(exit status, the output's tail) of a command, unbounded: the caller's bound is the sync loop's."""
    r = ProcessRunner().run(argv, cwd=cwd, capture_output=True, text=True, timeout=None)
    return r.returncode, (r.stdout + r.stderr)[-TAIL:]


class VendorSync:
    """The steps, over `run(argv, cwd) -> (status, output)` and the paths: a test hands a fake runner."""

    def __init__(self, run, root, tree, wait, read_tests=vendorscope.read_tests, movies_root=vendor.movies_root):
        self.run, self.root, self.tree, self.wait, self.read_tests = run, root, tree, wait, read_tests
        self.movies_root = movies_root

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
        if kind != "scoped" or not set(changed) <= set(copies) | {vendor.TABLE}:
            raise Failed(f"the sync changed more than the vendored copies: {changed}")
        self.python("the scoped tests", "tests/run.py", "-j", TEST_JOBS, *tests)
        if sim:
            self.python("the simulator gate", "tests/sim_gate.py")
        tools = self.python("the quality tools", "quality/tools.py").strip().splitlines()[-1]
        self.step("the ratchets", [tools, "quality/ratchet.py", "selftest"])
        status, out = self.run([tools, "quality/ratchet.py", "check"], self.tree)
        if status:
            self.record_falls(tools, f"the ratchets (exit {status}): {out.strip()}")

    def record_falls(self, tools, refused):
        """The check refused: `update` answers it when the baselines it writes only FELL (rows lowered or gone), and the
        check then passes; anything else -- a rise, a new row, a changed limit -- raises Failed for the agent."""
        before = {b: self.baseline_rows(b) for b in vendorscope.BASELINES}
        self.step("the ratchets' update", [tools, "quality/ratchet.py", "update"])
        grown = [f"{b}: {' '.join(row)}" for b in vendorscope.BASELINES
                 for row in only_grown(before[b], self.baseline_rows(b))]
        if grown:
            raise Failed(f"{refused}\n`ratchet.py update` would record not only falls: {'; '.join(grown[:10])}")
        self.step("the ratchets after recording the falls", [tools, "quality/ratchet.py", "check"])

    def baseline_rows(self, rel):
        return baseline_rows(read_text_or(os.path.join(self.tree, rel), ""))

    def unlanded(self):
        """The movies checkout's vendored sources that are not origin/main's (an edit not landed, or a checkout behind
        main): a sync copies the checkout's files, so it must push only what movies has landed."""
        movies = self.movies_root()
        if not movies:
            return []
        sources = [s for _c, s, _d in vendor.read_table(self.tree)]
        self.step("git fetch movies", ["git", "fetch", "-q", "origin", "main"], movies)
        status, out = self.run(["git", "diff", "--name-only", "origin/main", "--", *sources], movies)
        if status:
            raise Failed(f"git diff in the movies checkout (exit {status}): {out.strip()}")
        return out.split()

    def execute(self):
        """('done', why) or raises Failed."""
        self.step("git fetch", ["git", "fetch", "-q", "origin", "main"], self.root)
        self.step("the worktree", ["git", "worktree", "add", "-q", "--detach", self.tree, "origin/main"], self.root)
        try:
            behind = self.unlanded()
            if behind:
                return "waiting: the movies checkout's " + ", ".join(behind) + " is not origin/main's (landed) file"
            self.python("vendor.py sync", "devtools/vendor.py", "sync")
            changed = self.changed()
            if not changed:
                return "nothing to sync"
            self.verify(changed)
            changed = self.changed()  # with the baselines a recorded fall changed
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


def baseline_rows(text):
    """A baseline's `kind<TAB>key<TAB>value` rows as {(kind, key): value}."""
    lines = [x.split("\t") for x in text.splitlines() if x.strip() and not x.startswith("#")]
    return {(x[0], x[1]): x[2] for x in lines if len(x) == 3}


def only_grown(before, after):
    """The rows of `after` that are no fall from `before`: added, raised, or a limit changed (sorted, as (kind, key, value))."""
    def grew(key, value):
        if key not in before:
            return True
        return value != before[key] if key[0] == "limit" else float(value) > float(before[key])
    return sorted((k[0], k[1], v) for k, v in after.items() if grew(k, v))


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
