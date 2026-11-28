# agentctl, part `vendordrift`: VENDOR DRIFT as a condition: the movies checkout moved ahead of agentctl's byte-equal vendored copies (devtools/vendor.py), routed by the escalation table to an agent that syncs them
# A real module (lib/agentctl/__init__.py MODULES): its own imports, the standard library's and MODULES' only.
#
# 2026-11-27: three times in one day a deliberate movies change to a vendored file (scripts/design-rules/*,
# baseline_ratchet, trailers, ps_table, atomic) broke tests/test_vendored.py and refused EVERY agentctl push until someone
# synced it. The structural fix, in two halves: (a) the test blocks only a push that itself touches a copy or
# VENDORED.tsv (vendor.py drift_verdict), drift elsewhere is reported; (b) THIS: the laptop's sync loop asks
# `devtools/vendor.py drift` (at most every CHECK_EVERY s) and, while the movies checkout is ahead, holds ONE VENDOR_DRIFT
# condition (key `vendor-drift`). The escalation table (escalationtable.py) routes it to a Sonnet agent in ~/bin whose
# task is the sync; its probe -- the same drift check -- closes it once the synced copies are on ~/bin's checkout.
# Opened and closed by the existing condition, offer and lease machinery; it writes no inbox entry (the agent is the answer).
#
# 2026-11-28: NO AGENT FOR A MECHANICAL SYNC. The condition stayed open ~3 h: two fixers each committed the right 5-file
# sync and hit their wall time without pushing (the full pre-push suite timed out at load 25-45). Now the step first runs
# devtools/vendorsync.py, detached (copy, scoped tests, selfpush: a vendored-only push runs a scoped suite), and opens the
# condition only when that FAILED -- a real API change to adapt -- so the agent's brief carries why it failed.
#
# 2026-11-28, condition vendor-drift.3: the one sync of a drift failed because movies landed a newer vendored file while it
# waited for the push lock (the hook's drift check read it), and none ran again for 3 h: agents were dispatched for a
# mechanical sync. A failure belongs to the MOVIES STATE it synced (`vendor.py signature`, kept in `.started`): when
# movies moves a vendored file again -- the race's newer file, or the movies fix the failure asked for -- it is retried.

import os
import subprocess
from typing import Any, Callable

from agentctl.core import print_error, pid_alive, run_cmd, spawn_detached
from agentctl.repos import bin_repo
from agentctl.statefile import read_json_file, unlink_file, write_json
from agentctl.conditions import open_condition, run_probe
from agentctl.escalationtable import VENDOR_DRIFT_KIND

CHECK_EVERY = 300        # s between the opening look's runs of the drift check (the probe closing it runs on PROBE_EVERY)
VENDOR_DRIFT_KEY = "vendor-drift"
MARK = "vendor_drift:checked"
AUTO_BOUND = 45 * 60     # s the detached sync may take (copy, scoped tests, the push lock) before it counts as failed
DONE_GRACE = 15 * 60     # s after a pushed sync for ~/bin's checkout to take it before "drift persists" is a failure


def probe_vendor_drift(a: dict[str, Any]) -> tuple[bool | None, str]:
    """No vendored copy has drifted from the movies checkout (`devtools/vendor.py drift` in the agentctl checkout a["root"]):
    True when none has, False naming the drifted files, None when the check itself cannot run."""
    root = a["root"]
    r = run_cmd(["python3", os.path.join(root, "devtools", "vendor.py"), "drift"], timeout=60, cwd=root)
    if r.returncode not in (0, 1):
        return None, f"vendor.py drift: exit {r.returncode}: {r.stderr.strip()[-120:]}"
    return r.returncode == 0, "; ".join(x.strip() for x in r.stdout.splitlines() if x.strip())[:280]


def movies_signature(root: str) -> str:
    """`devtools/vendor.py signature` of the agentctl checkout `root`: one digest of the movies files its copies follow,
    "" when it cannot say."""
    r = run_cmd(["python3", os.path.join(root, "devtools", "vendor.py"), "signature"], timeout=60, cwd=root)
    return r.stdout.strip() if r.returncode == 0 else ""


class AutoSync:
    """The deterministic sync's detached process: `<path>.started` ({at, pid, signature}) says one was launched for that
    movies state, `<path>` is the verdict it writes last ({state: done|failed, why, at}). spawn(argv) returns the process
    (its pid is kept to tell a dead one); signature() is the movies state now (movies_signature)."""

    def __init__(self, root: str, result_path: str, clock: Callable[[], float],
                 spawn: Callable[[list[str]], Any] = lambda argv: spawn_detached(argv, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True),
                 alive: Callable[[int], bool] = lambda pid: pid_alive(pid),
                 signature: Callable[[str], str] = movies_signature) -> None:
        self.root, self.path, self.clock, self.spawn, self.alive = root, result_path, clock, spawn, alive
        self.signature = signature

    def state(self) -> tuple[str, str]:
        """idle (none run since the last drift ended, or the one that failed synced another movies state), running, done
        or failed, with its why."""
        started, verdict = read_json_file(self.path + ".started"), read_json_file(self.path)
        if not started:
            return "idle", ""
        if verdict.get("state") and verdict.get("at", 0) >= started.get("at", 0):
            state, why = self.judged(verdict)
        else:
            state, why = self.running(started)
        if state == "failed" and started.get("signature") != self.signature(self.root):
            return "idle", ""  # movies moved a vendored file since that sync started: this state gets its own
        return state, why

    def judged(self, verdict: dict[str, Any]) -> tuple[str, str]:
        """The script's verdict; a pushed one whose drift still shows after DONE_GRACE is a failure (the push did not close it)."""
        state, why = str(verdict["state"]), str(verdict.get("why") or "")
        if state == "done" and self.clock() - float(verdict.get("at") or 0) > DONE_GRACE:
            return "failed", f"the automatic sync pushed ({why}) but the drift persists"
        return state, why

    def running(self, started: dict[str, Any]) -> tuple[str, str]:
        """Still running; failed past AUTO_BOUND or when its process died with no verdict."""
        if self.clock() - float(started.get("at") or 0) > AUTO_BOUND:
            return "failed", f"the automatic sync did not finish within {AUTO_BOUND // 60} min"
        if not self.alive(int(started.get("pid") or 0)):
            return "failed", "the automatic sync process ended without a verdict (devtools/vendorsync.py missing or crashed)"
        return "running", ""

    def start(self) -> None:
        proc = self.spawn(["python3", os.path.join(self.root, "devtools", "vendorsync.py"), "--result", self.path])
        write_json(self.path + ".started", {"at": self.clock(), "pid": getattr(proc, "pid", 0), "signature": self.signature(self.root)})

    def reset(self) -> None:
        for path in (self.path, self.path + ".started"):
            unlink_file(path)


def vendor_drift_auto(root: str, result_path: str, clock: Callable[[], float]) -> AutoSync:
    """The sync loop's AutoSync: it runs devtools/vendorsync.py of the agentctl checkout ($AGENTCTL_BIN_REPO's, else `root`)."""
    return AutoSync(bin_repo() or root, result_path, clock)


def vendor_drift_step(store: Any, check: Callable[[dict[str, Any]], tuple[bool | None, str]], clock: Callable[[], float], root: str,
                      auto: AutoSync) -> None:
    """The sync loop's look, at most every CHECK_EVERY s: while a vendored copy trails the movies checkout it first runs the
    mechanical sync (`auto`, once per drift), and only when that FAILED is the VENDOR_DRIFT condition open (once; refreshed
    while it stays so), its line saying why. check: the registry's vendor_drift probe; root: agentctl's checkout; auto: the mechanical sync."""
    now = clock()
    if now - float(store.meta_get(MARK) or 0) < CHECK_EVERY:
        return
    store.meta_set(MARK, str(now))
    probe = run_probe(check, {"root": root})
    if probe.ok is not False:
        if probe.ok:
            auto.reset()  # no drift: the next one gets its own automatic sync
        return
    state, why = auto.state()
    if state == "idle":
        auto.start()
    if state != "failed":
        return
    text = (f"VENDOR DRIFT: the movies checkout changed a file agentctl vendors, so tests/test_vendored.py fails for the next "
            f"push that touches one: {probe.detail}. The automatic sync (devtools/vendorsync.py) failed, which is a real "
            f"change to adapt: {why}")
    c = open_condition(VENDOR_DRIFT_KIND, text, probe, [], key=VENDOR_DRIFT_KEY, inbox=False, store=store, grace=0)
    if c:
        print_error(text)
