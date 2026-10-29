"""Cache expiry (Agent B item 3): each maintain pass removes the state root's per-land logs, apply logs, sbt dumps,
baseline caches, idle red run dirs and gone trees' tags after 7 days -- and nothing else, never a protected folder."""
import argparse, io, contextlib, json, os, sys, tempfile, time, unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from test_agentctl import A  # noqa: E402

DAY = 86400


class CacheExpiryTest(unittest.TestCase):
    def setUp(self):
        self.root = os.path.realpath(tempfile.mkdtemp(prefix="state-"))
        self.now = time.time()

    def put(self, rel, age_days, text="x"):
        p = os.path.join(self.root, rel)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "w") as f:
            f.write(text)
        t = self.now - age_days * DAY
        os.utime(p, (t, t))
        return p

    def age(self, rel, age_days):
        t = self.now - age_days * DAY
        os.utime(os.path.join(self.root, rel), (t, t))

    def expire(self, **kw):
        return {os.path.relpath(p, self.root) for p, _why in A.expire_cache(self.root, 7, now=self.now, **kw)}

    def test_old_land_logs_dumps_and_baselines_go_new_ones_and_other_files_stay(self):
        old = {self.put(r, 8) for r in ("agentctl-land-movies-wt-x-123.log", "agentctl-land-movies-wt-x-123.log.checks.out",
                                        "phase2-apply-20261020-062013.log", "sbt-dumps/1-20261020.txt",
                                        "reds/baseline/abc.json", "reds/baseline-out/main")}
        keep = {self.put(r, a) for r, a in (("agentctl-land-movies-wt-y-9.log", 2), ("watches.log", 30),
                                             ("checks/runs.jsonl", 30), ("inbox/2026-wait.json", 30),
                                             ("maintain.json", 30), ("waiting/r/entries/e.json", 30))}
        self.assertEqual(self.expire(), {os.path.relpath(p, self.root) for p in old})
        self.assertFalse(any(os.path.exists(p) for p in old))
        self.assertTrue(all(os.path.exists(p) for p in keep))

    def test_dry_run_lists_and_removes_nothing(self):
        p = self.put("agentctl-land-a-1.log", 8)
        self.assertEqual(self.expire(dry_run=True), {"agentctl-land-a-1.log"})
        self.assertTrue(os.path.exists(p))

    def test_a_red_run_dir_goes_only_when_all_of_it_is_old_and_no_fixer_runs(self):
        self.put("reds/2050/fixer.json", 9, json.dumps({"run": 2050, "ended": "2026-10-20T00:00:00Z"}))
        self.put("reds/2050/claude-1.log", 9)
        self.age("reds/2050", 9)
        self.put("reds/2051/fixer.json", 9, json.dumps({"run": 2051}))  # still running: kept, however old
        self.age("reds/2051", 9)
        self.put("reds/2052/fixer.json", 9, json.dumps({"run": 2052, "ended": "x"}))
        self.put("reds/2052/claude-2.log", 1)  # touched yesterday: kept
        self.put("reds/sessions/s.json", 9)  # not a run dir
        self.assertEqual(self.expire(), {"reds/2050"})
        self.assertTrue(os.path.isdir(os.path.join(self.root, "reds", "2051")))
        self.assertTrue(os.path.exists(os.path.join(self.root, "reds", "sessions", "s.json")))

    def test_an_internal_tree_tag_goes_only_when_its_tree_is_gone(self):
        live = tempfile.mkdtemp()
        self.put("internal-trees/a.json", 9, json.dumps({"path": "/tmp/agentctl-gone-tree-xyz"}))
        self.put("internal-trees/b.json", 9, json.dumps({"path": live}))
        self.put("internal-trees/c.json", 9, json.dumps({"path": os.path.join(A.HOME, "Desktop", "t")}))
        self.assertEqual(self.expire(), {"internal-trees/a.json"})

    def test_a_symlink_is_removed_never_followed(self):
        target = tempfile.mkdtemp()
        with open(os.path.join(target, "keep"), "w") as f:
            f.write("x")
        link = os.path.join(self.root, "agentctl-land-link.log")
        os.symlink(target, link)
        t = self.now - 9 * DAY
        os.utime(link, (t, t), follow_symlinks=False)
        os.makedirs(os.path.join(self.root, "reds"))
        os.symlink(target, os.path.join(self.root, "reds", "2060"))
        self.expire()
        self.assertTrue(os.path.exists(os.path.join(target, "keep")))

    def test_a_root_in_a_protected_folder_is_never_touched(self):
        desk = os.path.join(A.HOME, "Desktop", "agentctl-state")
        self.assertEqual(A.expire_cache(desk, 7, now=self.now), [])

    def test_maintain_runs_it(self):
        p = self.put("agentctl-land-z-1.log", 8)
        saved = {k: getattr(A, k) for k in ("cmd_orphans", "orphan_duty", "write_beat", "cmd_sweep")}
        saved_root = os.environ.get("AGENTCTL_STATE_ROOT")
        os.environ["AGENTCTL_STATE_ROOT"] = self.root
        A.cmd_orphans = lambda a: 0
        A.orphan_duty = lambda dry_run=False: []
        A.write_beat = lambda name: None
        A.cmd_sweep = lambda a: 0
        try:
            out = io.StringIO()
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
                A.cmd_maintain(argparse.Namespace(dry_run=False, sweep=False))
        finally:
            for k, v in saved.items():
                setattr(A, k, v)
            os.environ["AGENTCTL_STATE_ROOT"] = saved_root
        self.assertIn("removed 1 cache entry older than 7 days", out.getvalue())
        self.assertFalse(os.path.exists(p))


if __name__ == "__main__":
    unittest.main()
