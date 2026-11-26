"""THE MAINTAIN PASS (maintain.cmd_maintain) AND THE CACHE EXPIRY (maintain.expire_cache), characterised with the sweeps, the
reap, the notifier and the state file faked and the expiry run over a real temporary state root: each section's text
and calls, a section that fails (the pass goes on), the daily sweep's three readings, and each kind of cache entry the
expiry keeps or removes. `run` and `run_expire` take the module so a copy of the code can be driven the same way."""
import argparse
import contextlib
import io
import json
import os
import sys
import tempfile
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from test_agentctl import A  # noqa: E402,F401 (puts lib on the path)

NOW, DAY = 10_000_000.0, 86400


def run(module, script):
    """cmd_maintain(args, store) with the script's answers: (its rc, what it printed, the calls it made)."""
    log = []

    def rec(name, ret=None):
        def fake(*a, **k):
            log.append(f"{name} {a} {sorted(k.items())}")
            if isinstance(ret, BaseException):
                raise ret
            return ret(*a, **k) if callable(ret) else ret
        return fake

    with tempfile.TemporaryDirectory() as agents, contextlib.ExitStack() as stack:
        if script.get("plist"):
            open(os.path.join(agents, "net.bitcashier.agentctl-watchdog.plist"), "w").close()
        patches = {
            "write_beat": rec("write_beat"), "beat_age": lambda name: script.get("watchdog"),
            "launch_agents_dir": lambda: agents, "notify": rec("notify"), "cmd_orphans": rec("cmd_orphans"),
            "rotate_logs": rec("rotate_logs", script.get("rotated", [])),
            "spotlight_exclude": rec("spotlight_exclude", script.get("marked", [])),
            "ios_simulator_sweep": rec("ios_simulator_sweep", script.get("ios")),
            "state_root": lambda name: "/S/" + name, "read_record": lambda p: script.get("state"),
            "write_json": rec("write_json"), "ts": lambda: "TS", "epoch_now": lambda: __import__("datetime").datetime(2026, 11, 25, script.get("hour", 10)).timestamp(),
            "cmd_sweep": rec("cmd_sweep", script.get("sweep_rc", 0)),
            "orphan_duty": rec("orphan_duty", script.get("orphans", [])),
            "expire_cache": rec("expire_cache", script.get("expired", [])),
        }
        for name, fake in patches.items():
            stack.enter_context(mock.patch.object(module, name, fake))
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            rc = module.cmd_maintain(argparse.Namespace(dry_run=script.get("dry", False), sweep=script.get("force", False)), "ST")
        return rc, out.getvalue(), log


SCENARIOS = {
    "quiet pass": dict(),
    "dry run": dict(dry=True),
    "watchdog never ran": dict(plist=True),
    "watchdog stale": dict(plist=True, watchdog=900.0),
    "watchdog fresh": dict(plist=True, watchdog=60.0),
    "no watchdog service": dict(watchdog=None),
    "logs rotated": dict(rotated=["a.log 3 MB", "b.log 5 MB"]),
    "trees marked": dict(marked=["t1", "t2"]),
    "spotlight fails": dict(marked=RuntimeError("no mdutil")),
    "spotlight exits": dict(marked=SystemExit("stop")),
    "ios fails": dict(ios=OSError("no xcrun")),
    "sweep before the hour": dict(hour=8),
    "sweep at the hour": dict(hour=9),
    "sweep done today": dict(hour=12, state={"sweep": "2026-11-25", "sweep_at": "earlier"}),
    "sweep done yesterday": dict(hour=12, state={"sweep": "2026-11-24", "x": 1}, sweep_rc=3),
    "sweep forced": dict(hour=3, force=True, state={"sweep": "2026-11-25", "sweep_at": "earlier"}),
    "sweep dry": dict(hour=12, dry=True),
    "orphans listed": dict(orphans=["  one", "  two"]),
    "orphans fail": dict(orphans=Exception("store down")),
    "expiry one": dict(expired=[("p", "w")]),
    "expiry several": dict(expired=[("p", "w"), ("q", "w")]),
    "expiry fails": dict(expired=SystemExit("no")),
    "expiry dry": dict(dry=True, expired=[("p", "w")]),
}


def generate(rng, n):
    keys = [("dry", [False, True]), ("force", [False, True]), ("hour", [3, 8, 9, 15]), ("plist", [False, True]),
            ("watchdog", [None, 10.0, 301.0]), ("rotated", [[], ["x"]]),
            ("marked", [[], ["t"], RuntimeError("m"), SystemExit("m")]), ("ios", [None, OSError("i"), SystemExit(2)]),
            ("state", [None, {"sweep": "2026-11-25", "sweep_at": "a"}, {"sweep": "old"}]),
            ("orphans", [[], ["  o"], ValueError("o")]), ("expired", [[], [("p", "w")], [("p", "w")] * 2, KeyError("e")]),
            ("sweep_rc", [0, 1])]
    return [{k: rng.choice(v) for k, v in keys} for _ in range(n)]


def _age(path, days):
    os.utime(path, (NOW - days * DAY, NOW - days * DAY), follow_symlinks=False)


def _write(path, days, text="x"):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        f.write(text)
    _age(path, days)


def build_root(tmp, spec):
    """The state root of the spec's entries: ([(kind, name, days, extra)]) as real files, directories and tags."""
    root = os.path.join(tmp, "state")
    os.makedirs(root)
    tree = os.path.join(tmp, "tree-here")
    os.makedirs(tree)
    for kind, name, days, extra in spec:
        p = os.path.join(root, name)
        if kind == "file":
            _write(p, days)
        elif kind == "dir":
            os.makedirs(p, exist_ok=True)
            _age(p, days)
        elif kind == "reddir":
            os.makedirs(p)
            for rel, d in extra.get("files", [("a.log", days)]):
                _write(os.path.join(p, rel), d)
            for fixer, ended in extra.get("fixers", []):
                _write(os.path.join(p, fixer), days, json.dumps({"ended": ended} if ended is not None else {"run": 1}))
            _age(p, days)
        elif kind == "link":
            os.makedirs(os.path.dirname(p), exist_ok=True)
            os.symlink(extra["to"].replace("TREE", tree), p)
            _age(p, days)
        elif kind == "tag":
            body = extra.get("body")
            _write(p, days, json.dumps({"path": {"here": tree, "protected": "/x/NOPE/tree"}.get(body, os.path.join(tmp, "tree-gone"))})
                   if body in ("here", "gone", "protected") else (body or ""))
    return root


def run_expire(module, spec, days=None, dry=False, refuse=None):
    """expire_cache over the spec's state root: ([(path, why)] relative to it, what is left, sorted)."""
    with tempfile.TemporaryDirectory() as tmp, mock.patch.object(module, "allowed_path", lambda p: "NOPE" not in p):
        root = build_root(tmp, spec)
        got = module.expire_cache(root=refuse or root, days=days, now=NOW, dry_run=dry)
        real = os.path.realpath(root)
        left = sorted(os.path.relpath(os.path.join(dp, n), root) for dp, dns, fns in os.walk(root) for n in dns + fns)
        return sorted((os.path.relpath(p, real), why) for p, why in got), left


def expire_case(module, case):
    spec, kw = case
    return run_expire(module, spec, **kw)


EXPIRY = {
    "empty root": ([], {}),
    "old and young log": ([("file", "agentctl-land-1.log", 9, {}), ("file", "agentctl-land-2.log", 1, {}),
                           ("file", "phase2-apply-3.log", 30, {}), ("file", "other.log", 30, {})], {}),
    "a directory by a pattern stays": ([("dir", "agentctl-land-old", 30, {})], {}),
    "sbt dumps": ([("file", "sbt-dumps/a", 10, {}), ("file", "sbt-dumps/b", 2, {})], {}),
    "baseline caches": ([("file", "reds/baseline/x.json", 10, {}), ("file", "reds/baseline/x.txt", 10, {}),
                         ("file", "reds/baseline-out/y", 10, {})], {}),
    "red dir all old": ([("reddir", "reds/101", 12, {})], {}),
    "red dir with a young file": ([("reddir", "reds/102", 12, {"files": [("a", 12), ("b", 1)]})], {}),
    "red dir with a running fixer": ([("reddir", "reds/103", 12, {"fixers": [("fixer.json", None)]})], {}),
    "red dir with an ended fixer": ([("reddir", "reds/104", 12, {"fixers": [("fixer-2.json", 5)]})], {}),
    "red dir, not a number": ([("reddir", "reds/old", 12, {})], {}),
    "red dir, a link": ([("link", "reds/105", 12, {"to": "TREE"})], {}),
    "tag of a gone tree": ([("tag", "internal-trees/a.json", 10, {"body": "gone"})], {}),
    "a file exactly at the horizon stays": ([("file", "agentctl-land-1.log", 7, {})], {}),
    "red dir named like a number and more": ([("reddir", "reds/1x", 12, {})], {}),
    "tag of a protected tree": ([("tag", "internal-trees/f.json", 10, {"body": "protected"})], {}),
    "tag of a live tree": ([("tag", "internal-trees/b.json", 10, {"body": "here"})], {}),
    "tag without a path": ([("tag", "internal-trees/c.json", 10, {"body": "{}"})], {}),
    "tag unreadable": ([("tag", "internal-trees/d.json", 10, {"body": ""})], {}),
    "young tag": ([("tag", "internal-trees/e.json", 2, {"body": "gone"})], {}),
    "a longer horizon": ([("file", "agentctl-land-1.log", 9, {}), ("file", "agentctl-land-2.log", 40, {})], {"days": 30}),
    "dry run": ([("file", "agentctl-land-1.log", 9, {}), ("reddir", "reds/106", 12, {})], {"dry": True}),
    "root outside the roots": ([("file", "agentctl-land-1.log", 9, {})], {"refuse": "/x/NOPE/y"}),
}


def generate_expiry(rng, n):
    names = ["agentctl-land-%d", "phase2-apply-%d.log", "sbt-dumps/%d", "reds/baseline/%d.json", "reds/baseline-out/%d",
             "keep-%d"]
    out = []
    for _ in range(n):
        spec = []
        for i in range(rng.randint(0, 7)):
            age = rng.choice([1, 6, 7, 8, 20])
            kind = rng.choice(["file", "file", "reddir", "reddir", "tag", "link"])
            if kind == "file":
                spec.append(("file", rng.choice(names) % i, age, {}))
            elif kind == "reddir":
                spec.append(("reddir", "reds/%s" % rng.choice(["%d" % (100 + i), "x%d" % i, "1x%d" % i]), age,
                             {"files": [("a", age), ("b", rng.choice([1, age]))],
                              "fixers": rng.choice([[], [("fixer.json", None)], [("fixer-2.json", 3)]])}))
            elif kind == "tag":
                spec.append(("tag", "internal-trees/%d.json" % i, age, {"body": rng.choice(["here", "gone", "{}", "", "[]", "protected"])}))
            else:
                spec.append(("link", "reds/%d" % (200 + i), age, {"to": "TREE"}))
        out.append((spec, rng.choice([{}, {}, {"days": 3}, {"dry": True}])))
    return out


class MaintainTest(unittest.TestCase):
    def test_every_pass_does_what_it_did(self):
        module = __import__("agentctl.maintain", fromlist=["x"])
        for name, script in SCENARIOS.items():
            with self.subTest(name):
                self.assertEqual(run(module, script), EXPECTED["pass"][name], name)

    def test_every_expiry_does_what_it_did(self):
        module = __import__("agentctl.maintain", fromlist=["x"])
        for name, (spec, kw) in EXPIRY.items():
            with self.subTest(name):
                self.assertEqual(run_expire(module, spec, **kw), EXPECTED["expiry"][name], name)


def results():
    module = __import__("agentctl.maintain", fromlist=["x"])
    return {"pass": {n: run(module, s) for n, s in SCENARIOS.items()},
            "expiry": {n: run_expire(module, spec, **kw) for n, (spec, kw) in EXPIRY.items()}}


EXPECTED = {'pass': {'quiet pass': (0,
                         '== agentctl maintain TS\n'
                         '== reap\n'
                         '== logs\n'
                         '  none over the size\n'
                         '== spotlight\n'
                         '  every worktree marked\n'
                         '== ios simulators\n'
                         '== sweep (daily)\n'
                         '== orphaned work\n'
                         '    none\n'
                         '== cache expiry\n'
                         '  removed 0 cache entries older than 7 days\n',
                         ["write_beat ('maintain',) []",
                          'cmd_orphans (Namespace(kill=True, safe=True),) []',
                          'rotate_logs () []',
                          'spotlight_exclude () []',
                          "ios_simulator_sweep () [('dry_run', False)]",
                          "cmd_sweep (Namespace(repo=None, idle_days=3, dry_run=False, no_report=False), 'ST') []",
                          "write_json ('/S/maintain.json', {'sweep': '2026-11-25', 'sweep_rc': 0, 'sweep_at': 'TS'}) "
                          '[]',
                          "orphan_duty ('ST',) [('dry_run', False)]",
                          "expire_cache () [('dry_run', False)]"]),
          'dry run': (0,
                      '== agentctl maintain TS (DRY RUN)\n'
                      '== reap\n'
                      '== logs\n'
                      '  (dry run)\n'
                      '== spotlight\n'
                      '  every worktree marked\n'
                      '== ios simulators\n'
                      '== sweep (daily)\n'
                      '== orphaned work\n'
                      '    none\n'
                      '== cache expiry\n'
                      '  would remove 0 cache entries older than 7 days\n',
                      ['cmd_orphans (Namespace(kill=False, safe=True),) []',
                       "ios_simulator_sweep () [('dry_run', True)]",
                       "cmd_sweep (Namespace(repo=None, idle_days=3, dry_run=True, no_report=True), 'ST') []",
                       "orphan_duty ('ST',) [('dry_run', True)]",
                       "expire_cache () [('dry_run', True)]"]),
          'watchdog never ran': (0,
                                 '== agentctl maintain TS\n'
                                 '== the watchdog last ran never: notified\n'
                                 '== reap\n'
                                 '== logs\n'
                                 '  none over the size\n'
                                 '== spotlight\n'
                                 '  every worktree marked\n'
                                 '== ios simulators\n'
                                 '== sweep (daily)\n'
                                 '== orphaned work\n'
                                 '    none\n'
                                 '== cache expiry\n'
                                 '  removed 0 cache entries older than 7 days\n',
                                 ["write_beat ('maintain',) []",
                                  "notify ('agentctl: the watchdog is not running', 'last ran never') []",
                                  'cmd_orphans (Namespace(kill=True, safe=True),) []',
                                  'rotate_logs () []',
                                  'spotlight_exclude () []',
                                  "ios_simulator_sweep () [('dry_run', False)]",
                                  'cmd_sweep (Namespace(repo=None, idle_days=3, dry_run=False, no_report=False), '
                                  "'ST') []",
                                  "write_json ('/S/maintain.json', {'sweep': '2026-11-25', 'sweep_rc': 0, "
                                  "'sweep_at': 'TS'}) []",
                                  "orphan_duty ('ST',) [('dry_run', False)]",
                                  "expire_cache () [('dry_run', False)]"]),
          'watchdog stale': (0,
                             '== agentctl maintain TS\n'
                             '== the watchdog last ran 15 min ago: notified\n'
                             '== reap\n'
                             '== logs\n'
                             '  none over the size\n'
                             '== spotlight\n'
                             '  every worktree marked\n'
                             '== ios simulators\n'
                             '== sweep (daily)\n'
                             '== orphaned work\n'
                             '    none\n'
                             '== cache expiry\n'
                             '  removed 0 cache entries older than 7 days\n',
                             ["write_beat ('maintain',) []",
                              "notify ('agentctl: the watchdog is not running', 'last ran 15 min ago') []",
                              'cmd_orphans (Namespace(kill=True, safe=True),) []',
                              'rotate_logs () []',
                              'spotlight_exclude () []',
                              "ios_simulator_sweep () [('dry_run', False)]",
                              "cmd_sweep (Namespace(repo=None, idle_days=3, dry_run=False, no_report=False), 'ST') "
                              '[]',
                              "write_json ('/S/maintain.json', {'sweep': '2026-11-25', 'sweep_rc': 0, 'sweep_at': "
                              "'TS'}) []",
                              "orphan_duty ('ST',) [('dry_run', False)]",
                              "expire_cache () [('dry_run', False)]"]),
          'watchdog fresh': (0,
                             '== agentctl maintain TS\n'
                             '== reap\n'
                             '== logs\n'
                             '  none over the size\n'
                             '== spotlight\n'
                             '  every worktree marked\n'
                             '== ios simulators\n'
                             '== sweep (daily)\n'
                             '== orphaned work\n'
                             '    none\n'
                             '== cache expiry\n'
                             '  removed 0 cache entries older than 7 days\n',
                             ["write_beat ('maintain',) []",
                              'cmd_orphans (Namespace(kill=True, safe=True),) []',
                              'rotate_logs () []',
                              'spotlight_exclude () []',
                              "ios_simulator_sweep () [('dry_run', False)]",
                              "cmd_sweep (Namespace(repo=None, idle_days=3, dry_run=False, no_report=False), 'ST') "
                              '[]',
                              "write_json ('/S/maintain.json', {'sweep': '2026-11-25', 'sweep_rc': 0, 'sweep_at': "
                              "'TS'}) []",
                              "orphan_duty ('ST',) [('dry_run', False)]",
                              "expire_cache () [('dry_run', False)]"]),
          'no watchdog service': (0,
                                  '== agentctl maintain TS\n'
                                  '== reap\n'
                                  '== logs\n'
                                  '  none over the size\n'
                                  '== spotlight\n'
                                  '  every worktree marked\n'
                                  '== ios simulators\n'
                                  '== sweep (daily)\n'
                                  '== orphaned work\n'
                                  '    none\n'
                                  '== cache expiry\n'
                                  '  removed 0 cache entries older than 7 days\n',
                                  ["write_beat ('maintain',) []",
                                   'cmd_orphans (Namespace(kill=True, safe=True),) []',
                                   'rotate_logs () []',
                                   'spotlight_exclude () []',
                                   "ios_simulator_sweep () [('dry_run', False)]",
                                   'cmd_sweep (Namespace(repo=None, idle_days=3, dry_run=False, no_report=False), '
                                   "'ST') []",
                                   "write_json ('/S/maintain.json', {'sweep': '2026-11-25', 'sweep_rc': 0, "
                                   "'sweep_at': 'TS'}) []",
                                   "orphan_duty ('ST',) [('dry_run', False)]",
                                   "expire_cache () [('dry_run', False)]"]),
          'logs rotated': (0,
                           '== agentctl maintain TS\n'
                           '== reap\n'
                           '== logs\n'
                           '  a.log 3 MB\n'
                           '  b.log 5 MB\n'
                           '== spotlight\n'
                           '  every worktree marked\n'
                           '== ios simulators\n'
                           '== sweep (daily)\n'
                           '== orphaned work\n'
                           '    none\n'
                           '== cache expiry\n'
                           '  removed 0 cache entries older than 7 days\n',
                           ["write_beat ('maintain',) []",
                            'cmd_orphans (Namespace(kill=True, safe=True),) []',
                            'rotate_logs () []',
                            'spotlight_exclude () []',
                            "ios_simulator_sweep () [('dry_run', False)]",
                            "cmd_sweep (Namespace(repo=None, idle_days=3, dry_run=False, no_report=False), 'ST') []",
                            "write_json ('/S/maintain.json', {'sweep': '2026-11-25', 'sweep_rc': 0, 'sweep_at': "
                            "'TS'}) []",
                            "orphan_duty ('ST',) [('dry_run', False)]",
                            "expire_cache () [('dry_run', False)]"]),
          'trees marked': (0,
                           '== agentctl maintain TS\n'
                           '== reap\n'
                           '== logs\n'
                           '  none over the size\n'
                           '== spotlight\n'
                           '  marked 2 tree(s) never to index\n'
                           '== ios simulators\n'
                           '== sweep (daily)\n'
                           '== orphaned work\n'
                           '    none\n'
                           '== cache expiry\n'
                           '  removed 0 cache entries older than 7 days\n',
                           ["write_beat ('maintain',) []",
                            'cmd_orphans (Namespace(kill=True, safe=True),) []',
                            'rotate_logs () []',
                            'spotlight_exclude () []',
                            "ios_simulator_sweep () [('dry_run', False)]",
                            "cmd_sweep (Namespace(repo=None, idle_days=3, dry_run=False, no_report=False), 'ST') []",
                            "write_json ('/S/maintain.json', {'sweep': '2026-11-25', 'sweep_rc': 0, 'sweep_at': "
                            "'TS'}) []",
                            "orphan_duty ('ST',) [('dry_run', False)]",
                            "expire_cache () [('dry_run', False)]"]),
          'spotlight fails': (0,
                              '== agentctl maintain TS\n'
                              '== reap\n'
                              '== logs\n'
                              '  none over the size\n'
                              '== spotlight\n'
                              '  could not mark (no mdutil)\n'
                              '== ios simulators\n'
                              '== sweep (daily)\n'
                              '== orphaned work\n'
                              '    none\n'
                              '== cache expiry\n'
                              '  removed 0 cache entries older than 7 days\n',
                              ["write_beat ('maintain',) []",
                               'cmd_orphans (Namespace(kill=True, safe=True),) []',
                               'rotate_logs () []',
                               'spotlight_exclude () []',
                               "ios_simulator_sweep () [('dry_run', False)]",
                               "cmd_sweep (Namespace(repo=None, idle_days=3, dry_run=False, no_report=False), 'ST') "
                               '[]',
                               "write_json ('/S/maintain.json', {'sweep': '2026-11-25', 'sweep_rc': 0, 'sweep_at': "
                               "'TS'}) []",
                               "orphan_duty ('ST',) [('dry_run', False)]",
                               "expire_cache () [('dry_run', False)]"]),
          'spotlight exits': (0,
                              '== agentctl maintain TS\n'
                              '== reap\n'
                              '== logs\n'
                              '  none over the size\n'
                              '== spotlight\n'
                              '  could not mark (stop)\n'
                              '== ios simulators\n'
                              '== sweep (daily)\n'
                              '== orphaned work\n'
                              '    none\n'
                              '== cache expiry\n'
                              '  removed 0 cache entries older than 7 days\n',
                              ["write_beat ('maintain',) []",
                               'cmd_orphans (Namespace(kill=True, safe=True),) []',
                               'rotate_logs () []',
                               'spotlight_exclude () []',
                               "ios_simulator_sweep () [('dry_run', False)]",
                               "cmd_sweep (Namespace(repo=None, idle_days=3, dry_run=False, no_report=False), 'ST') "
                               '[]',
                               "write_json ('/S/maintain.json', {'sweep': '2026-11-25', 'sweep_rc': 0, 'sweep_at': "
                               "'TS'}) []",
                               "orphan_duty ('ST',) [('dry_run', False)]",
                               "expire_cache () [('dry_run', False)]"]),
          'ios fails': (0,
                        '== agentctl maintain TS\n'
                        '== reap\n'
                        '== logs\n'
                        '  none over the size\n'
                        '== spotlight\n'
                        '  every worktree marked\n'
                        '== ios simulators\n'
                        '  could not sweep (no xcrun)\n'
                        '== sweep (daily)\n'
                        '== orphaned work\n'
                        '    none\n'
                        '== cache expiry\n'
                        '  removed 0 cache entries older than 7 days\n',
                        ["write_beat ('maintain',) []",
                         'cmd_orphans (Namespace(kill=True, safe=True),) []',
                         'rotate_logs () []',
                         'spotlight_exclude () []',
                         "ios_simulator_sweep () [('dry_run', False)]",
                         "cmd_sweep (Namespace(repo=None, idle_days=3, dry_run=False, no_report=False), 'ST') []",
                         "write_json ('/S/maintain.json', {'sweep': '2026-11-25', 'sweep_rc': 0, 'sweep_at': 'TS'}) "
                         '[]',
                         "orphan_duty ('ST',) [('dry_run', False)]",
                         "expire_cache () [('dry_run', False)]"]),
          'sweep before the hour': (0,
                                    '== agentctl maintain TS\n'
                                    '== reap\n'
                                    '== logs\n'
                                    '  none over the size\n'
                                    '== spotlight\n'
                                    '  every worktree marked\n'
                                    '== ios simulators\n'
                                    '== sweep: from 09:00\n'
                                    '== orphaned work\n'
                                    '    none\n'
                                    '== cache expiry\n'
                                    '  removed 0 cache entries older than 7 days\n',
                                    ["write_beat ('maintain',) []",
                                     'cmd_orphans (Namespace(kill=True, safe=True),) []',
                                     'rotate_logs () []',
                                     'spotlight_exclude () []',
                                     "ios_simulator_sweep () [('dry_run', False)]",
                                     "orphan_duty ('ST',) [('dry_run', False)]",
                                     "expire_cache () [('dry_run', False)]"]),
          'sweep at the hour': (0,
                                '== agentctl maintain TS\n'
                                '== reap\n'
                                '== logs\n'
                                '  none over the size\n'
                                '== spotlight\n'
                                '  every worktree marked\n'
                                '== ios simulators\n'
                                '== sweep (daily)\n'
                                '== orphaned work\n'
                                '    none\n'
                                '== cache expiry\n'
                                '  removed 0 cache entries older than 7 days\n',
                                ["write_beat ('maintain',) []",
                                 'cmd_orphans (Namespace(kill=True, safe=True),) []',
                                 'rotate_logs () []',
                                 'spotlight_exclude () []',
                                 "ios_simulator_sweep () [('dry_run', False)]",
                                 'cmd_sweep (Namespace(repo=None, idle_days=3, dry_run=False, no_report=False), '
                                 "'ST') []",
                                 "write_json ('/S/maintain.json', {'sweep': '2026-11-25', 'sweep_rc': 0, 'sweep_at': "
                                 "'TS'}) []",
                                 "orphan_duty ('ST',) [('dry_run', False)]",
                                 "expire_cache () [('dry_run', False)]"]),
          'sweep done today': (0,
                               '== agentctl maintain TS\n'
                               '== reap\n'
                               '== logs\n'
                               '  none over the size\n'
                               '== spotlight\n'
                               '  every worktree marked\n'
                               '== ios simulators\n'
                               '== sweep: done today (earlier)\n'
                               '== orphaned work\n'
                               '    none\n'
                               '== cache expiry\n'
                               '  removed 0 cache entries older than 7 days\n',
                               ["write_beat ('maintain',) []",
                                'cmd_orphans (Namespace(kill=True, safe=True),) []',
                                'rotate_logs () []',
                                'spotlight_exclude () []',
                                "ios_simulator_sweep () [('dry_run', False)]",
                                "orphan_duty ('ST',) [('dry_run', False)]",
                                "expire_cache () [('dry_run', False)]"]),
          'sweep done yesterday': (0,
                                   '== agentctl maintain TS\n'
                                   '== reap\n'
                                   '== logs\n'
                                   '  none over the size\n'
                                   '== spotlight\n'
                                   '  every worktree marked\n'
                                   '== ios simulators\n'
                                   '== sweep (daily)\n'
                                   '== orphaned work\n'
                                   '    none\n'
                                   '== cache expiry\n'
                                   '  removed 0 cache entries older than 7 days\n',
                                   ["write_beat ('maintain',) []",
                                    'cmd_orphans (Namespace(kill=True, safe=True),) []',
                                    'rotate_logs () []',
                                    'spotlight_exclude () []',
                                    "ios_simulator_sweep () [('dry_run', False)]",
                                    'cmd_sweep (Namespace(repo=None, idle_days=3, dry_run=False, no_report=False), '
                                    "'ST') []",
                                    "write_json ('/S/maintain.json', {'sweep': '2026-11-25', 'x': 1, 'sweep_rc': 3, "
                                    "'sweep_at': 'TS'}) []",
                                    "orphan_duty ('ST',) [('dry_run', False)]",
                                    "expire_cache () [('dry_run', False)]"]),
          'sweep forced': (0,
                           '== agentctl maintain TS\n'
                           '== reap\n'
                           '== logs\n'
                           '  none over the size\n'
                           '== spotlight\n'
                           '  every worktree marked\n'
                           '== ios simulators\n'
                           '== sweep (daily)\n'
                           '== orphaned work\n'
                           '    none\n'
                           '== cache expiry\n'
                           '  removed 0 cache entries older than 7 days\n',
                           ["write_beat ('maintain',) []",
                            'cmd_orphans (Namespace(kill=True, safe=True),) []',
                            'rotate_logs () []',
                            'spotlight_exclude () []',
                            "ios_simulator_sweep () [('dry_run', False)]",
                            "cmd_sweep (Namespace(repo=None, idle_days=3, dry_run=False, no_report=False), 'ST') []",
                            "write_json ('/S/maintain.json', {'sweep': '2026-11-25', 'sweep_at': 'TS', 'sweep_rc': "
                            '0}) []',
                            "orphan_duty ('ST',) [('dry_run', False)]",
                            "expire_cache () [('dry_run', False)]"]),
          'sweep dry': (0,
                        '== agentctl maintain TS (DRY RUN)\n'
                        '== reap\n'
                        '== logs\n'
                        '  (dry run)\n'
                        '== spotlight\n'
                        '  every worktree marked\n'
                        '== ios simulators\n'
                        '== sweep (daily)\n'
                        '== orphaned work\n'
                        '    none\n'
                        '== cache expiry\n'
                        '  would remove 0 cache entries older than 7 days\n',
                        ['cmd_orphans (Namespace(kill=False, safe=True),) []',
                         "ios_simulator_sweep () [('dry_run', True)]",
                         "cmd_sweep (Namespace(repo=None, idle_days=3, dry_run=True, no_report=True), 'ST') []",
                         "orphan_duty ('ST',) [('dry_run', True)]",
                         "expire_cache () [('dry_run', True)]"]),
          'orphans listed': (0,
                             '== agentctl maintain TS\n'
                             '== reap\n'
                             '== logs\n'
                             '  none over the size\n'
                             '== spotlight\n'
                             '  every worktree marked\n'
                             '== ios simulators\n'
                             '== sweep (daily)\n'
                             '== orphaned work\n'
                             '    one\n'
                             '    two\n'
                             '== cache expiry\n'
                             '  removed 0 cache entries older than 7 days\n',
                             ["write_beat ('maintain',) []",
                              'cmd_orphans (Namespace(kill=True, safe=True),) []',
                              'rotate_logs () []',
                              'spotlight_exclude () []',
                              "ios_simulator_sweep () [('dry_run', False)]",
                              "cmd_sweep (Namespace(repo=None, idle_days=3, dry_run=False, no_report=False), 'ST') "
                              '[]',
                              "write_json ('/S/maintain.json', {'sweep': '2026-11-25', 'sweep_rc': 0, 'sweep_at': "
                              "'TS'}) []",
                              "orphan_duty ('ST',) [('dry_run', False)]",
                              "expire_cache () [('dry_run', False)]"]),
          'orphans fail': (0,
                           '== agentctl maintain TS\n'
                           '== reap\n'
                           '== logs\n'
                           '  none over the size\n'
                           '== spotlight\n'
                           '  every worktree marked\n'
                           '== ios simulators\n'
                           '== sweep (daily)\n'
                           '== orphaned work\n'
                           '  could not look (store down)\n'
                           '== cache expiry\n'
                           '  removed 0 cache entries older than 7 days\n',
                           ["write_beat ('maintain',) []",
                            'cmd_orphans (Namespace(kill=True, safe=True),) []',
                            'rotate_logs () []',
                            'spotlight_exclude () []',
                            "ios_simulator_sweep () [('dry_run', False)]",
                            "cmd_sweep (Namespace(repo=None, idle_days=3, dry_run=False, no_report=False), 'ST') []",
                            "write_json ('/S/maintain.json', {'sweep': '2026-11-25', 'sweep_rc': 0, 'sweep_at': "
                            "'TS'}) []",
                            "orphan_duty ('ST',) [('dry_run', False)]",
                            "expire_cache () [('dry_run', False)]"]),
          'expiry one': (0,
                         '== agentctl maintain TS\n'
                         '== reap\n'
                         '== logs\n'
                         '  none over the size\n'
                         '== spotlight\n'
                         '  every worktree marked\n'
                         '== ios simulators\n'
                         '== sweep (daily)\n'
                         '== orphaned work\n'
                         '    none\n'
                         '== cache expiry\n'
                         '  removed 1 cache entry older than 7 days\n',
                         ["write_beat ('maintain',) []",
                          'cmd_orphans (Namespace(kill=True, safe=True),) []',
                          'rotate_logs () []',
                          'spotlight_exclude () []',
                          "ios_simulator_sweep () [('dry_run', False)]",
                          "cmd_sweep (Namespace(repo=None, idle_days=3, dry_run=False, no_report=False), 'ST') []",
                          "write_json ('/S/maintain.json', {'sweep': '2026-11-25', 'sweep_rc': 0, 'sweep_at': 'TS'}) "
                          '[]',
                          "orphan_duty ('ST',) [('dry_run', False)]",
                          "expire_cache () [('dry_run', False)]"]),
          'expiry several': (0,
                             '== agentctl maintain TS\n'
                             '== reap\n'
                             '== logs\n'
                             '  none over the size\n'
                             '== spotlight\n'
                             '  every worktree marked\n'
                             '== ios simulators\n'
                             '== sweep (daily)\n'
                             '== orphaned work\n'
                             '    none\n'
                             '== cache expiry\n'
                             '  removed 2 cache entries older than 7 days\n',
                             ["write_beat ('maintain',) []",
                              'cmd_orphans (Namespace(kill=True, safe=True),) []',
                              'rotate_logs () []',
                              'spotlight_exclude () []',
                              "ios_simulator_sweep () [('dry_run', False)]",
                              "cmd_sweep (Namespace(repo=None, idle_days=3, dry_run=False, no_report=False), 'ST') "
                              '[]',
                              "write_json ('/S/maintain.json', {'sweep': '2026-11-25', 'sweep_rc': 0, 'sweep_at': "
                              "'TS'}) []",
                              "orphan_duty ('ST',) [('dry_run', False)]",
                              "expire_cache () [('dry_run', False)]"]),
          'expiry fails': (0,
                           '== agentctl maintain TS\n'
                           '== reap\n'
                           '== logs\n'
                           '  none over the size\n'
                           '== spotlight\n'
                           '  every worktree marked\n'
                           '== ios simulators\n'
                           '== sweep (daily)\n'
                           '== orphaned work\n'
                           '    none\n'
                           '== cache expiry\n'
                           '  could not expire (no)\n',
                           ["write_beat ('maintain',) []",
                            'cmd_orphans (Namespace(kill=True, safe=True),) []',
                            'rotate_logs () []',
                            'spotlight_exclude () []',
                            "ios_simulator_sweep () [('dry_run', False)]",
                            "cmd_sweep (Namespace(repo=None, idle_days=3, dry_run=False, no_report=False), 'ST') []",
                            "write_json ('/S/maintain.json', {'sweep': '2026-11-25', 'sweep_rc': 0, 'sweep_at': "
                            "'TS'}) []",
                            "orphan_duty ('ST',) [('dry_run', False)]",
                            "expire_cache () [('dry_run', False)]"]),
          'expiry dry': (0,
                         '== agentctl maintain TS (DRY RUN)\n'
                         '== reap\n'
                         '== logs\n'
                         '  (dry run)\n'
                         '== spotlight\n'
                         '  every worktree marked\n'
                         '== ios simulators\n'
                         '== sweep (daily)\n'
                         '== orphaned work\n'
                         '    none\n'
                         '== cache expiry\n'
                         '  would remove 1 cache entry older than 7 days\n',
                         ['cmd_orphans (Namespace(kill=False, safe=True),) []',
                          "ios_simulator_sweep () [('dry_run', True)]",
                          "cmd_sweep (Namespace(repo=None, idle_days=3, dry_run=True, no_report=True), 'ST') []",
                          "orphan_duty ('ST',) [('dry_run', True)]",
                          "expire_cache () [('dry_run', True)]"])},
 'expiry': {'empty root': ([], []),
            'old and young log': ([('agentctl-land-1.log', 'older than 7 days'),
                                   ('phase2-apply-3.log', 'older than 7 days')],
                                  ['agentctl-land-2.log', 'other.log']),
            'a directory by a pattern stays': ([], ['agentctl-land-old']),
            'sbt dumps': ([('sbt-dumps/a', 'older than 7 days')], ['sbt-dumps', 'sbt-dumps/b']),
            'baseline caches': ([('reds/baseline-out/y', 'older than 7 days'),
                                 ('reds/baseline/x.json', 'older than 7 days')],
                                ['reds', 'reds/baseline', 'reds/baseline-out', 'reds/baseline/x.txt']),
            'red dir all old': ([('reds/101', 'red run dir, untouched for 7 days, no fixer running')], ['reds']),
            'red dir with a young file': ([], ['reds', 'reds/102', 'reds/102/a', 'reds/102/b']),
            'red dir with a running fixer': ([], ['reds', 'reds/103', 'reds/103/a.log', 'reds/103/fixer.json']),
            'red dir with an ended fixer': ([('reds/104', 'red run dir, untouched for 7 days, no fixer running')],
                                            ['reds']),
            'red dir, not a number': ([], ['reds', 'reds/old', 'reds/old/a.log']),
            'red dir, a link': ([], ['reds', 'reds/105']),
            'tag of a gone tree': ([('internal-trees/a.json', 'internal-tree tag of a gone worktree')],
                                   ['internal-trees']),
            'a file exactly at the horizon stays': ([], ['agentctl-land-1.log']),
            'red dir named like a number and more': ([], ['reds', 'reds/1x', 'reds/1x/a.log']),
            'tag of a protected tree': ([], ['internal-trees', 'internal-trees/f.json']),
            'tag of a live tree': ([], ['internal-trees', 'internal-trees/b.json']),
            'tag without a path': ([('internal-trees/c.json', 'internal-tree tag of a gone worktree')],
                                   ['internal-trees']),
            'tag unreadable': ([('internal-trees/d.json', 'internal-tree tag of a gone worktree')],
                               ['internal-trees']),
            'young tag': ([], ['internal-trees', 'internal-trees/e.json']),
            'a longer horizon': ([('agentctl-land-2.log', 'older than 30 days')], ['agentctl-land-1.log']),
            'dry run': ([('agentctl-land-1.log', 'older than 7 days'),
                         ('reds/106', 'red run dir, untouched for 7 days, no fixer running')],
                        ['agentctl-land-1.log', 'reds', 'reds/106', 'reds/106/a.log']),
            'root outside the roots': ([], ['agentctl-land-1.log'])}}
