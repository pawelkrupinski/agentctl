"""No published red without an assignee (the owner, 2026-10-27: "why is main red unassigned?" -- for 1-2 min after a red
appeared the status line said `UNASSIGNED (assigning…)`: the snapshot, the patrol and the status line ran on separate
clocks). The cycle that first sees a gate red assigns it (author claim or dispatch) BEFORE the snapshot and the gate line
are published; a gap that remains renders `dispatching a fixer…` in normal weight (bold NOBODY only for a truly unowned
red, with its reason); and a changed gate line touches a marker that makes the status line refresh at its next call."""
import os, subprocess, sys, tempfile, time, unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from test_agentctl import A  # noqa: E402
from test_assign import AssignBase  # noqa: E402
from test_red import MOVIES  # noqa: E402

STATUSLINE = os.path.join(HERE, "..", "claude-statusline")


class AssignFirstTest(AssignBase):
    def setUp(self):
        super().setUp()
        t = self.tmp.name
        for k, v in (("AGENTCTL_GATE_SUMMARY", os.path.join(t, "gate-summary.txt")),
                     ("AGENTCTL_STATUSLINE_STALE", os.path.join(t, "statusline.stale"))):
            os.environ[k] = v
            self.addCleanup(os.environ.pop, k, None)
        self.saved_pub = A.slug, A.route_branch_reds, A.patrol_repo, A.ci_state_write, A.publish_gate_summary
        self.addCleanup(self.restore)
        self.events = []
        real_patrol, real_write, real_pub = A.patrol_repo, A.ci_state_write, A.publish_gate_summary

        def patrol(*a, **k):
            self.events.append("patrol")
            return real_patrol(*a, **k)

        def write(snap):
            self.events.append("snapshot")
            return real_write(snap)

        def pub(o_r, line, epoch=None):
            self.events.append(("publish", line, [c.get("author") for c in A.all_claims()]))
            return real_pub(o_r, line, epoch)
        A.slug, A.route_branch_reds = (lambda r: MOVIES), (lambda *a, **k: [])
        A.patrol_repo, A.ci_state_write, A.publish_gate_summary = patrol, write, pub
        A._seen_gate_reds.clear()
        A._last_gate_publish[0] = 0.0
        A._last_patrol[0] = time.time()  # the 2-min patrol is not due: only the publisher's own look may assign

    def restore(self):
        A.slug, A.route_branch_reds, A.patrol_repo, A.ci_state_write, A.publish_gate_summary = self.saved_pub
        A._gate_lists.clear()

    def test_a_new_gate_red_is_assigned_before_the_snapshot_and_the_gate_line_are_published(self):
        _wt, head = self.author_change("repo-wt-a", pid=os.getpid())
        self.red_at(head)
        A._gate_lists.clear()
        A.maybe_publish_gate(self.r.path)
        names = [e if isinstance(e, str) else e[0] for e in self.events]
        self.assertEqual(names, ["patrol", "snapshot", "publish"], self.events)
        _p, line, authors = self.events[-1]
        self.assertTrue(any(authors), "the author's claim exists when the line is published")
        self.assertIn("main #20 → author repo-wt-a", line)
        self.assertNotIn("UNASSIGNED", line)
        self.assertNotIn(A.DISPATCHING_GAP, line)

    def test_a_red_already_seen_is_not_patrolled_again_by_the_publisher(self):
        _wt, head = self.author_change("repo-wt-a", pid=os.getpid())
        self.red_at(head)
        A._seen_gate_reds.update({40})
        A.maybe_publish_gate(self.r.path)
        self.assertNotIn("patrol", self.events)


class GapRenderingTest(AssignBase):
    def test_the_gap_before_the_patrol_reads_dispatching_a_fixer_never_nobody(self):
        self.main_red(40, 20)
        line = self.why("--summary")[1].strip().splitlines()[-1]
        self.assertEqual(line, f"gate RED (main #20 → {A.DISPATCHING_GAP}; convergence green)")
        self.assertIn(f"fixing: {A.DISPATCHING_GAP}", self.why()[1])

    def test_the_status_line_shows_the_gap_in_normal_weight_and_nobody_in_bold(self):
        home = tempfile.mkdtemp()
        cache = os.path.join(home, ".cache", "agentctl")
        os.makedirs(os.path.join(cache, "statusline.lock"))  # a refresh "running": none is started
        with open(os.path.join(cache, "statusline.txt"), "w") as f:
            f.write(f"gate RED (main #20 → {A.DISPATCHING_GAP}; convergence → NOBODY (skipped: gate fixer cap 0 "
                    "reached)) · waiting: 0 · inbox: 0 unread\n")
        r = subprocess.run([STATUSLINE], input="{}", capture_output=True, text=True, env=dict(os.environ, HOME=home))
        self.assertIn(f"main #20 → {A.DISPATCHING_GAP};", r.stdout, "no escape codes around the gap")
        self.assertIn("\x1b[1;31mNOBODY\x1b[0m (skipped: gate fixer cap 0 reached)", r.stdout)


class StaleMarkerTest(unittest.TestCase):
    def setUp(self):
        self.home = tempfile.mkdtemp()
        self.marker = os.path.join(self.home, "statusline.stale")
        for k, v in (("AGENTCTL_GATE_SUMMARY", os.path.join(self.home, "gate-summary.txt")),
                     ("AGENTCTL_STATUSLINE_STALE", self.marker)):
            os.environ[k] = v
            self.addCleanup(os.environ.pop, k, None)

    def test_the_marker_is_touched_only_when_the_line_changes(self):
        A.publish_gate_summary(MOVIES, "gate RED (main #20 → dispatching a fixer…; convergence green)")
        self.assertTrue(os.path.exists(self.marker))
        os.utime(self.marker, (1, 1))
        A.publish_gate_summary(MOVIES, "gate RED (main #20 → dispatching a fixer…; convergence green)")
        self.assertEqual(os.stat(self.marker).st_mtime, 1, "the same reds and states: no refresh forced")
        A.publish_gate_summary(MOVIES, "gate RED (main #20 → red-20 dispatching; convergence green)")
        self.assertGreater(os.stat(self.marker).st_mtime, 1, "a state changed: the status line refreshes")

    def statusline(self, marker_age):
        cache = os.path.join(self.home, ".cache", "agentctl")
        os.makedirs(cache, exist_ok=True)
        path = os.path.join(cache, "statusline.txt")
        with open(path, "w") as f:
            f.write("gate RED (main #20 → dispatching a fixer…; convergence green) · waiting: 0 · inbox: 0 unread\n")
        now = time.time()
        os.utime(path, (now - 10, now - 10))  # fresh: under the 60 s age
        if marker_age is not None:
            open(self.marker, "a").close()
            os.utime(self.marker, (now - marker_age, now - marker_age))
        env = dict(os.environ, HOME=self.home, AGENTCTL_STATUSLINE_REPO=self.home)
        subprocess.run([STATUSLINE], input="{}", capture_output=True, text=True, env=env)
        t0 = time.time()
        while time.time() - t0 < 15:
            if "gate RED (main #20" not in open(path).read():
                return True  # rewritten by a refresh (here: UNREAD, no agentctl under this HOME)
            if marker_age is None and time.time() - t0 > 2:
                break
            time.sleep(0.2)
        return False

    def test_a_marker_newer_than_the_cache_refreshes_it_at_the_next_call(self):
        self.assertTrue(self.statusline(marker_age=1))

    def test_without_a_newer_marker_a_fresh_cache_is_kept(self):
        self.assertFalse(self.statusline(marker_age=None))
        self.assertFalse(self.statusline(marker_age=100))


if __name__ == "__main__":
    unittest.main()
