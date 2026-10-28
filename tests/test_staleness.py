"""Notification and staleness (the owner, 2026-10-28): escalations of every kind reach a person, a claim goes stale by
heartbeat (not by its claimant's pid being alive), and a diagnosis gets its implementer after 30 min whatever else
claims the red."""
import os, stat, sys, time, unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from test_agentctl import A  # noqa: E402
from test_red import RedBase  # noqa: E402


class StaleByHeartbeatTest(unittest.TestCase):
    def test_a_live_claimant_quiet_for_hours_is_stale_a_fresh_one_is_not(self):
        old = A.ts_of(time.time() - 5 * 3600) if hasattr(A, "ts_of") else time.strftime(
            "%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - 5 * 3600))
        c = {"pid": os.getpid(), "process_start": A.process_start(os.getpid()), "started": old, "beat": old}
        self.assertIn("did nothing on it for", A.claim_stale(c))
        c["beat"] = A.ts()
        self.assertIsNone(A.claim_stale(c))
        self.assertIsNone(A.claim_stale(dict(c, beat=old, state="blocked")))  # a block waits on its probe


class EscalateKindsTest(RedBase):
    def test_an_unread_red_escalate_blocked_or_diagnosis_notifies_once(self):
        notes = os.path.join(self.tmp.name, "notes")
        hook = os.path.join(self.tmp.name, "notify.sh")
        with open(hook, "w") as f:
            f.write(f'#!/bin/sh\necho "$1|$2" >> {notes}\n')
        os.chmod(hook, os.stat(hook).st_mode | stat.S_IEXEC)
        os.environ["AGENTCTL_NOTIFY"] = hook
        A._OPENING[0] += 1  # as open_condition writes them
        try:
            A.inbox_write("red", "RED ESCALATE: W #7 [j] still red after 2 dispatched fixers: an investigation", [], [], 1)
            A.inbox_write("red", "RED BLOCKED ON OWNER: W #8 [j]: `x` -- auto-clears when W is green", [], [], 1)
        finally:
            A._OPENING[0] -= 1
        A.inbox_write("red fixer", "RED FIXER W #9: DIAGNOSED: a cause; durable fix: y", [], [], 1)
        A.inbox_write("red fixer", "RED FIXER W #10: GAVE UP: no idea", [], [], 1)
        later = time.time() + 11 * 60
        kinds = sorted(e["kind"] for e in A.escalate_inbox(later))
        self.assertEqual(kinds, ["BLOCKED", "DIAGNOSED", "FIXER_FAILED", "RED_ESCALATE"])
        A.escalate_inbox(later)
        self.assertEqual(len(open(notes).read().splitlines()), 4)  # once each


class DiagnosedFollowupTest(RedBase):
    def test_a_diagnosis_older_than_30_min_gets_its_implementer_despite_a_live_plain_claim(self):
        u = self.units()[0]
        old = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - 40 * 60))
        base = {"worktree": self.r.path, "started": A.ts(), "beat": A.ts(), "run": "20", "runs": ["20", "40"],
                "sigs": [u["sig"]], "jobs": [u["job"]], "workflow": "Main"}
        A.write_json(A.reds_dir("claims", "diag.json"), {**base, "who": "red-fixer-20", "state": "diagnosed",
                                                         "awaiting": "an implementer", "diagnosed_at": old,
                                                         "diagnosis": "the cause"})
        A.write_json(A.reds_dir("claims", "plain.json"), {**base, "who": "orchestrator", "pid": os.getpid(),
                                                          "process_start": A.process_start(os.getpid())})
        called, saved = [], A.diagnosed_followup
        A.diagnosed_followup = lambda o_r, repo, c, t, group=None: called.append(c["who"]) or "RED DISPATCHED x"
        try:
            self.patrol(time.time())
        finally:
            A.diagnosed_followup = saved
        self.assertEqual(called, ["red-fixer-20"])


if __name__ == "__main__":
    unittest.main()
