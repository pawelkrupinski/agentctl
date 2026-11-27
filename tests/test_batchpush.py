"""A GREEN BATCH'S PUSH (batchpush.push_batch_item), characterised with git, the landing lock, the push and the store's
verdicts faked: each reason the batch is not pushed (a busy tree or lease, main moved, a member decided, the tree gone,
an unreadable plan, a red gate, a race, a stall, a failure), the environment each depth pushes with, and the verdicts and
watches after a landing. `run_push` takes the module so a copy of the code can be driven the same way."""
import contextlib
import copy
import importlib
import os
import sys
import tempfile
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from test_agentctl import A  # noqa: E402,F401 (puts lib on the path)

TIP, BASE = "t" * 40, "b" * 40


def run_push(module, script):
    """push_batch_item(repo, d, st, it) with the script's answers: (its return, the log, the checks.log text)."""
    log = []
    with tempfile.TemporaryDirectory() as tmp, contextlib.ExitStack() as stack:
        d, clone = os.path.join(tmp, "d"), os.path.join(tmp, "clone")
        os.makedirs(d)
        if script.get("tree", True):
            os.makedirs(f"{clone}-wt-check-7")
        worktrees = [os.path.join(tmp, f"wt-{k}") for k in range(2)]
        os.makedirs(worktrees[0])
        members = copy.deepcopy(script.get("members", [
            {"id": "a", "tip": "ta", "worktree": worktrees[0], "status": "checked", "session": "s1"},
            {"id": "b", "tip": "tb", "worktree": worktrees[1], "status": "other"}]))

        def shown(x):
            return str(x).replace(tmp, "TMP")

        def rec(name, ret=None):
            def fake(*a, **k):
                log.append(shown(f"{name} {a} {sorted((x, v) for x, v in k.items() if x != 'cause')}"))  # causes: test_causes
                return ret(*a, **k) if callable(ret) else ret
            return fake

        class Slot:
            def acquire(self):
                log.append(f"slot acquire {script.get('slot', True)}")
                return script.get("slot", True)

            def release(self):
                log.append("slot release")

        @contextlib.contextmanager
        def lock(repo, st):
            log.append(f"lock {script.get('lock', True)}")
            yield script.get("lock", True)
            log.append("unlock")

        def git(repo, *args, check=True):
            log.append(shown(f"git {args}"))
            if args[0] == "rev-parse":
                return script.get("main", BASE) + "\n"

        def run_watched(argv, cwd, env, path, name, **bounds):
            log.append(shown(f"run_watched {argv} {cwd} {sorted(env.items())} {path} {name} {sorted(bounds.items())}"))
            return script.get("push", (0, "pushed"))

        class Owned:
            def __init__(self, kind, key):
                self.key = key

            def live(self):
                return script.get("held", False) and "e-b" not in self.key

        class Store:
            def __repr__(self):
                return "ST"

            def clock(self):
                return 4242.0

            def item(self, kind, ident):
                return {"id": ident}

        patches = {
            "batch_members": lambda d_, it: members, "tree_slot": lambda st, d_, i: Slot(),
            "waiting_state": lambda repo: (d, clone, "w"), "land_lock_if_free": lock, "git": git,
            "git_ok": lambda *a: script.get("tree_commit", True), "remote": lambda: "origin",
            "lock_note": rec("lock_note"), "tool_env": lambda: {"T": "1"}, "precheck_hook": lambda tree: "HOOK",
            "range_paths": rec("range_paths", "paths"), "hook_query": rec("hook_query", lambda *a: script.get("skip")),
            "set_change": rec("set_change", lambda st, d_, e, to, event=None, cause=None: None),
            "push_argv": lambda remote, tip: ["push", remote, tip], "push_bounds": lambda: {"limit": 9},
            "run_watched": run_watched, "ts": lambda: "TS", "landed_state": lambda repo, main, tip: A.LandState(  # logged as `landed (repo, tip, main)`: the check's old spelling
                A.LANDED if rec("landed", script.get("landed", True))(repo, tip, main) else A.NOT_LANDED),
            "mark_gate_dirty": rec("mark_gate_dirty"), "queue_verdict": rec("queue_verdict"), "wlog": rec("wlog"),
            "Owned": Owned,
            "start_watch": rec("start_watch"), "batch_fail": rec("batch_fail", lambda st, d_, it, why, n=0: None),
            "batch_move": rec("batch_move", lambda st, it, to, data=None, text=None, severity=None, cause=None: {"id": it["id"]}),
        }
        for name, fake in patches.items():
            stack.enter_context(mock.patch.object(module, name, fake))
        it = {"id": "b1", "state": "pushing", "data": {"tip": TIP, "base": BASE, "tree": "7", "members": script.get("listed", ["a", "b"]),
                                                       **script.get("data", {})}}
        got = module.push_batch_item("REPO", d, Store(), it)
        checks = os.path.join(d, "checks.log")
        return shown(got), log, shown(open(checks).read()) if os.path.exists(checks) else None


SCENARIOS = {
    "no members": dict(members=[]),
    "tree busy": dict(slot=False),
    "lease busy": dict(lock=False),
    "main moved": dict(main="c" * 40),
    "a member left": dict(listed=["a", "b", "z"]),
    "two members left": dict(listed=["a", "b", "y", "z"]),
    "tree directory gone": dict(tree=False),
    "tree without the tip": dict(tree_commit=False),
    "landed, default env": dict(),
    "landed, held member watches elsewhere": dict(held=True),
    "landed, full with a run": dict(data={"depth": "full", "runs": {TIP: 99}}),
    "landed, full without a run": dict(data={"depth": "full"}),
    "landed, verify": dict(data={"depth": "verify"}, skip=["a", "b"]),
    "verify, plan unreadable": dict(data={"depth": "verify"}, skip=None),
    "landed, kept": dict(data={"kept": True}, skip=["x"]),
    "kept, plan unreadable": dict(data={"kept": True}, skip=None),
    "full and kept: full wins": dict(data={"depth": "full", "kept": True}),
    "another depth marks no member pushing": dict(data={"depth": "backlog"}),
    "red gate": dict(landed=False, push=(1, "main is red: only a fix lands")),
    "raced": dict(landed=False, push=(1, "! [rejected] (fetch first)")),
    "gate text after a stall": dict(landed=False, push=(None, "main is red: only a fix")),
    "a stall with a race's text": dict(landed=False, push=(None, "fetch first")),
    "stalled": dict(landed=False, push=(None, "")),
    "failed": dict(landed=False, push=(1, "l1\nl2\nl3\nl4\n" + "z" * 300)),
    "failed, no output": dict(landed=False, push=(2, None)),
}


class BatchPushTest(unittest.TestCase):
    def test_every_scenario_does_what_it_did(self):
        module = importlib.import_module("agentctl.batchpush")
        for name, script in SCENARIOS.items():
            with self.subTest(name):
                self.assertEqual(run_push(module, script), EXPECTED[name], name)


EXPECTED = {'no members': ('batch b1: nothing to push',
                ["batch_move (ST, {'id': 'b1', 'state': 'pushing', 'data': {'tip': "
                 "'tttttttttttttttttttttttttttttttttttttttt', 'base': 'bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb', "
                 "'tree': '7', 'members': ['a', 'b']}}, 'assembling') [('text', 'batch b1: its changes are gone')]"],
                None),
 'tree busy': ('batch b1: its check tree is busy', ['slot acquire False'], None),
 'lease busy': ('batch b1: the landing lease is busy',
                ['slot acquire True', 'lock False', 'unlock', 'slot release'],
                None),
 'main moved': ('batch b1: main moved',
                ['slot acquire True',
                 'lock True',
                 "git ('fetch', '--quiet', 'origin', 'main')",
                 "git ('rev-parse', 'origin/main')",
                 "batch_move (ST, {'id': 'b1', 'state': 'pushing', 'data': {'tip': "
                 "'tttttttttttttttttttttttttttttttttttttttt', 'base': 'bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb', "
                 "'tree': '7', 'members': ['a', 'b']}}, 'assembling') [('text', 'batch b1: main moved during its "
                 "check: assembled again')]",
                 'unlock',
                 'slot release'],
                None),
 'a member left': ('batch b1: 1 member(s) gone',
                   ['slot acquire True',
                    'lock True',
                    "git ('fetch', '--quiet', 'origin', 'main')",
                    "git ('rev-parse', 'origin/main')",
                    "batch_move (ST, {'id': 'b1', 'state': 'pushing', 'data': {'tip': "
                    "'tttttttttttttttttttttttttttttttttttttttt', 'base': 'bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb', "
                    "'tree': '7', 'members': ['a', 'b', 'z']}}, 'assembling') [('text', 'batch b1: z left it "
                    "(decided) during its check: assembled again without')]",
                    'unlock',
                    'slot release'],
                   None),
 'two members left': ('batch b1: 2 member(s) gone',
                      ['slot acquire True',
                       'lock True',
                       "git ('fetch', '--quiet', 'origin', 'main')",
                       "git ('rev-parse', 'origin/main')",
                       "batch_move (ST, {'id': 'b1', 'state': 'pushing', 'data': {'tip': "
                       "'tttttttttttttttttttttttttttttttttttttttt', 'base': "
                       "'bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb', 'tree': '7', 'members': ['a', 'b', 'y', 'z']}}, "
                       "'assembling') [('text', 'batch b1: y, z left it (decided) during its check: assembled again "
                       "without')]",
                       'unlock',
                       'slot release'],
                      None),
 'tree directory gone': ('batch b1: tree gone',
                         ['slot acquire True',
                          'lock True',
                          "git ('fetch', '--quiet', 'origin', 'main')",
                          "git ('rev-parse', 'origin/main')",
                          "lock_note ('push of the batch tttttttttt',) []",
                          "batch_move (ST, {'id': 'b1', 'state': 'pushing', 'data': {'tip': "
                          "'tttttttttttttttttttttttttttttttttttttttt', 'base': "
                          "'bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb', 'tree': '7', 'members': ['a', 'b']}}, "
                          "'assembling') [('text', 'batch b1: its tree is gone: assembled again')]",
                          'unlock',
                          'slot release'],
                         None),
 'tree without the tip': ('batch b1: tree gone',
                          ['slot acquire True',
                           'lock True',
                           "git ('fetch', '--quiet', 'origin', 'main')",
                           "git ('rev-parse', 'origin/main')",
                           "lock_note ('push of the batch tttttttttt',) []",
                           "batch_move (ST, {'id': 'b1', 'state': 'pushing', 'data': {'tip': "
                           "'tttttttttttttttttttttttttttttttttttttttt', 'base': "
                           "'bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb', 'tree': '7', 'members': ['a', 'b']}}, "
                           "'assembling') [('text', 'batch b1: its tree is gone: assembled again')]",
                           'unlock',
                           'slot release'],
                          None),
 'landed, default env': ('batch b1: landed tttttttttt',
                         ['slot acquire True',
                          'lock True',
                          "git ('fetch', '--quiet', 'origin', 'main')",
                          "git ('rev-parse', 'origin/main')",
                          "lock_note ('push of the batch tttttttttt',) []",
                          "git ('checkout', '-q', '--force', '--detach', 'tttttttttttttttttttttttttttttttttttttttt')",
                          "run_watched ['push', 'origin', 'tttttttttttttttttttttttttttttttttttttttt'] "
                          "TMP/clone-wt-check-7 [('PREPUSH_REUSE_FAST', '1'), ('T', '1')] TMP/d/push-tttttttttt push "
                          "[('limit', 9)]",
                          "git ('fetch', '--quiet', 'origin', 'main')",
                          "landed ('REPO', 'tttttttttttttttttttttttttttttttttttttttt', 'refs/remotes/origin/main') "
                          '[]',
                          'unlock',
                          'mark_gate_dirty () []',
                          "queue_verdict (ST, 'TMP/d', {'id': 'a', 'tip': 'ta', 'worktree': 'TMP/wt-0', 'status': "
                          "'checked', 'session': 's1'}, 'landed', 'LANDED ta (batch b1: 2 change(s) in one push)') "
                          "[('batch', 2), ('batch_id', 'b1'), ('code', 0), ('landed_sha', 'ta'), ('pushed_at', "
                          '4242.0)]',
                          "start_watch (ST, 'REPO', 'ta', 4242.0) [('close', 'TMP/wt-0'), ('session', 's1'), "
                          "('worktree', 'TMP/wt-0')]",
                          "queue_verdict (ST, 'TMP/d', {'id': 'b', 'tip': 'tb', 'worktree': 'TMP/wt-1', 'status': "
                          "'other'}, 'landed', 'LANDED tb (batch b1: 2 change(s) in one push)') [('batch', 2), "
                          "('batch_id', 'b1'), ('code', 0), ('landed_sha', 'tb'), ('pushed_at', 4242.0)]",
                          "start_watch (ST, 'REPO', 'tb', 4242.0) [('close', None), ('session', None), ('worktree', "
                          "'TMP/wt-1')]",
                          "batch_move (ST, {'id': 'b1'}, 'landed') [('text', 'LANDED "
                          "tttttttttttttttttttttttttttttttttttttttt (2 change(s) in one push)')]",
                          "wlog ('TMP/d', 'LANDED tttttttttttttttttttttttttttttttttttttttt (2 change(s) in one "
                          "push)') []",
                          'slot release'],
                         '=== push TS tttttttttt rc 0\npushed\n'),
 'landed, held member watches elsewhere': ('batch b1: landed tttttttttt',
                                           ['slot acquire True',
                                            'lock True',
                                            "git ('fetch', '--quiet', 'origin', 'main')",
                                            "git ('rev-parse', 'origin/main')",
                                            "lock_note ('push of the batch tttttttttt',) []",
                                            "git ('checkout', '-q', '--force', '--detach', "
                                            "'tttttttttttttttttttttttttttttttttttttttt')",
                                            "run_watched ['push', 'origin', "
                                            "'tttttttttttttttttttttttttttttttttttttttt'] TMP/clone-wt-check-7 "
                                            "[('PREPUSH_REUSE_FAST', '1'), ('T', '1')] TMP/d/push-tttttttttt push "
                                            "[('limit', 9)]",
                                            "git ('fetch', '--quiet', 'origin', 'main')",
                                            "landed ('REPO', 'tttttttttttttttttttttttttttttttttttttttt', "
                                            "'refs/remotes/origin/main') []",
                                            'unlock',
                                            'mark_gate_dirty () []',
                                            "queue_verdict (ST, 'TMP/d', {'id': 'a', 'tip': 'ta', 'worktree': "
                                            "'TMP/wt-0', 'status': 'checked', 'session': 's1'}, 'landed', 'LANDED ta "
                                            "(batch b1: 2 change(s) in one push)') [('batch', 2), ('batch_id', "
                                            "'b1'), ('code', 0), ('landed_sha', 'ta'), ('pushed_at', 4242.0)]",
                                            "queue_verdict (ST, 'TMP/d', {'id': 'b', 'tip': 'tb', 'worktree': "
                                            "'TMP/wt-1', 'status': 'other'}, 'landed', 'LANDED tb (batch b1: 2 "
                                            "change(s) in one push)') [('batch', 2), ('batch_id', 'b1'), ('code', "
                                            "0), ('landed_sha', 'tb'), ('pushed_at', 4242.0)]",
                                            "batch_move (ST, {'id': 'b1'}, 'landed') [('text', 'LANDED "
                                            "tttttttttttttttttttttttttttttttttttttttt (2 change(s) in one push)')]",
                                            "wlog ('TMP/d', 'LANDED tttttttttttttttttttttttttttttttttttttttt (2 "
                                            "change(s) in one push)') []",
                                            'slot release'],
                                           '=== push TS tttttttttt rc 0\npushed\n'),
 'landed, full with a run': ('batch b1: landed tttttttttt',
                             ['slot acquire True',
                              'lock True',
                              "git ('fetch', '--quiet', 'origin', 'main')",
                              "git ('rev-parse', 'origin/main')",
                              "lock_note ('push of the batch tttttttttt',) []",
                              "git ('checkout', '-q', '--force', '--detach', "
                              "'tttttttttttttttttttttttttttttttttttttttt')",
                              "set_change (ST, 'TMP/d', {'id': 'a', 'tip': 'ta', 'worktree': 'TMP/wt-0', 'status': "
                              "'checked', 'session': 's1'}, 'pushing') [('event', {'text': 'a: pushing in batch b1 "
                              "(tttttttttt)'})]",
                              "run_watched ['push', 'origin', 'tttttttttttttttttttttttttttttttttttttttt'] "
                              "TMP/clone-wt-check-7 [('PREPUSH_REMOTE_RUN', '99'), ('T', '1')] TMP/d/push-tttttttttt "
                              "push [('limit', 9)]",
                              "git ('fetch', '--quiet', 'origin', 'main')",
                              "landed ('REPO', 'tttttttttttttttttttttttttttttttttttttttt', "
                              "'refs/remotes/origin/main') []",
                              'unlock',
                              'mark_gate_dirty () []',
                              "queue_verdict (ST, 'TMP/d', {'id': 'a', 'tip': 'ta', 'worktree': 'TMP/wt-0', "
                              "'status': 'checked', 'session': 's1'}, 'landed', 'LANDED ta (batch b1: 2 change(s) in "
                              "one push)') [('batch', 2), ('batch_id', 'b1'), ('code', 0), ('landed_sha', 'ta'), "
                              "('pushed_at', 4242.0)]",
                              "start_watch (ST, 'REPO', 'ta', 4242.0) [('close', 'TMP/wt-0'), ('session', 's1'), "
                              "('worktree', 'TMP/wt-0')]",
                              "queue_verdict (ST, 'TMP/d', {'id': 'b', 'tip': 'tb', 'worktree': 'TMP/wt-1', "
                              "'status': 'other'}, 'landed', 'LANDED tb (batch b1: 2 change(s) in one push)') "
                              "[('batch', 2), ('batch_id', 'b1'), ('code', 0), ('landed_sha', 'tb'), ('pushed_at', "
                              '4242.0)]',
                              "start_watch (ST, 'REPO', 'tb', 4242.0) [('close', None), ('session', None), "
                              "('worktree', 'TMP/wt-1')]",
                              "batch_move (ST, {'id': 'b1'}, 'landed') [('text', 'LANDED "
                              "tttttttttttttttttttttttttttttttttttttttt (2 change(s) in one push)')]",
                              "wlog ('TMP/d', 'LANDED tttttttttttttttttttttttttttttttttttttttt (2 change(s) in one "
                              "push)') []",
                              'slot release'],
                             '=== push TS tttttttttt rc 0\npushed\n'),
 'landed, full without a run': ('batch b1: landed tttttttttt',
                                ['slot acquire True',
                                 'lock True',
                                 "git ('fetch', '--quiet', 'origin', 'main')",
                                 "git ('rev-parse', 'origin/main')",
                                 "lock_note ('push of the batch tttttttttt',) []",
                                 "git ('checkout', '-q', '--force', '--detach', "
                                 "'tttttttttttttttttttttttttttttttttttttttt')",
                                 "set_change (ST, 'TMP/d', {'id': 'a', 'tip': 'ta', 'worktree': 'TMP/wt-0', "
                                 "'status': 'checked', 'session': 's1'}, 'pushing') [('event', {'text': 'a: pushing "
                                 "in batch b1 (tttttttttt)'})]",
                                 "run_watched ['push', 'origin', 'tttttttttttttttttttttttttttttttttttttttt'] "
                                 "TMP/clone-wt-check-7 [('T', '1')] TMP/d/push-tttttttttt push [('limit', 9)]",
                                 "git ('fetch', '--quiet', 'origin', 'main')",
                                 "landed ('REPO', 'tttttttttttttttttttttttttttttttttttttttt', "
                                 "'refs/remotes/origin/main') []",
                                 'unlock',
                                 'mark_gate_dirty () []',
                                 "queue_verdict (ST, 'TMP/d', {'id': 'a', 'tip': 'ta', 'worktree': 'TMP/wt-0', "
                                 "'status': 'checked', 'session': 's1'}, 'landed', 'LANDED ta (batch b1: 2 change(s) "
                                 "in one push)') [('batch', 2), ('batch_id', 'b1'), ('code', 0), ('landed_sha', "
                                 "'ta'), ('pushed_at', 4242.0)]",
                                 "start_watch (ST, 'REPO', 'ta', 4242.0) [('close', 'TMP/wt-0'), ('session', 's1'), "
                                 "('worktree', 'TMP/wt-0')]",
                                 "queue_verdict (ST, 'TMP/d', {'id': 'b', 'tip': 'tb', 'worktree': 'TMP/wt-1', "
                                 "'status': 'other'}, 'landed', 'LANDED tb (batch b1: 2 change(s) in one push)') "
                                 "[('batch', 2), ('batch_id', 'b1'), ('code', 0), ('landed_sha', 'tb'), "
                                 "('pushed_at', 4242.0)]",
                                 "start_watch (ST, 'REPO', 'tb', 4242.0) [('close', None), ('session', None), "
                                 "('worktree', 'TMP/wt-1')]",
                                 "batch_move (ST, {'id': 'b1'}, 'landed') [('text', 'LANDED "
                                 "tttttttttttttttttttttttttttttttttttttttt (2 change(s) in one push)')]",
                                 "wlog ('TMP/d', 'LANDED tttttttttttttttttttttttttttttttttttttttt (2 change(s) in "
                                 "one push)') []",
                                 'slot release'],
                                '=== push TS tttttttttt rc 0\npushed\n'),
 'landed, verify': ('batch b1: landed tttttttttt',
                    ['slot acquire True',
                     'lock True',
                     "git ('fetch', '--quiet', 'origin', 'main')",
                     "git ('rev-parse', 'origin/main')",
                     "lock_note ('push of the batch tttttttttt',) []",
                     "git ('checkout', '-q', '--force', '--detach', 'tttttttttttttttttttttttttttttttttttttttt')",
                     "range_paths ('TMP/clone-wt-check-7', 'bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb', "
                     "'tttttttttttttttttttttttttttttttttttttttt') []",
                     "hook_query ('HOOK', 'TMP/clone-wt-check-7', '--plan', 'paths') []",
                     "set_change (ST, 'TMP/d', {'id': 'a', 'tip': 'ta', 'worktree': 'TMP/wt-0', 'status': 'checked', "
                     "'session': 's1'}, 'pushing') [('event', {'text': 'a: pushing in batch b1 (tttttttttt)'})]",
                     "run_watched ['push', 'origin', 'tttttttttttttttttttttttttttttttttttttttt'] "
                     "TMP/clone-wt-check-7 [('PREPUSH_SKIP', 'a b'), ('T', '1')] TMP/d/push-tttttttttt push "
                     "[('limit', 9)]",
                     "git ('fetch', '--quiet', 'origin', 'main')",
                     "landed ('REPO', 'tttttttttttttttttttttttttttttttttttttttt', 'refs/remotes/origin/main') []",
                     'unlock',
                     'mark_gate_dirty () []',
                     "queue_verdict (ST, 'TMP/d', {'id': 'a', 'tip': 'ta', 'worktree': 'TMP/wt-0', 'status': "
                     "'checked', 'session': 's1'}, 'landed', 'LANDED ta (push batch b1: 2 change(s) in one push)') "
                     "[('batch', 2), ('batch_id', 'b1'), ('code', 0), ('landed_sha', 'ta'), ('pushed_at', 4242.0)]",
                     "start_watch (ST, 'REPO', 'ta', 4242.0) [('close', 'TMP/wt-0'), ('session', 's1'), ('worktree', "
                     "'TMP/wt-0')]",
                     "queue_verdict (ST, 'TMP/d', {'id': 'b', 'tip': 'tb', 'worktree': 'TMP/wt-1', 'status': "
                     "'other'}, 'landed', 'LANDED tb (push batch b1: 2 change(s) in one push)') [('batch', 2), "
                     "('batch_id', 'b1'), ('code', 0), ('landed_sha', 'tb'), ('pushed_at', 4242.0)]",
                     "start_watch (ST, 'REPO', 'tb', 4242.0) [('close', None), ('session', None), ('worktree', "
                     "'TMP/wt-1')]",
                     "batch_move (ST, {'id': 'b1'}, 'landed') [('text', 'LANDED "
                     "tttttttttttttttttttttttttttttttttttttttt (2 change(s) in one push)')]",
                     "wlog ('TMP/d', 'LANDED tttttttttttttttttttttttttttttttttttttttt (2 change(s) in one push)') []",
                     'slot release'],
                    '=== push TS tttttttttt rc 0\npushed\n'),
 'verify, plan unreadable': ('batch b1: its plan is unreadable',
                             ['slot acquire True',
                              'lock True',
                              "git ('fetch', '--quiet', 'origin', 'main')",
                              "git ('rev-parse', 'origin/main')",
                              "lock_note ('push of the batch tttttttttt',) []",
                              "git ('checkout', '-q', '--force', '--detach', "
                              "'tttttttttttttttttttttttttttttttttttttttt')",
                              "range_paths ('TMP/clone-wt-check-7', 'bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb', "
                              "'tttttttttttttttttttttttttttttttttttttttt') []",
                              "hook_query ('HOOK', 'TMP/clone-wt-check-7', '--plan', 'paths') []",
                              "batch_fail (ST, 'TMP/d', {'id': 'b1', 'state': 'pushing', 'data': {'tip': "
                              "'tttttttttttttttttttttttttttttttttttttttt', 'base': "
                              "'bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb', 'tree': '7', 'members': ['a', 'b'], "
                              '\'depth\': \'verify\'}}, "the hook\'s plan for its push could not be read", 2) []',
                              'unlock',
                              'slot release'],
                             None),
 'landed, kept': ('batch b1: landed tttttttttt',
                  ['slot acquire True',
                   'lock True',
                   "git ('fetch', '--quiet', 'origin', 'main')",
                   "git ('rev-parse', 'origin/main')",
                   "lock_note ('push of the batch tttttttttt',) []",
                   "git ('checkout', '-q', '--force', '--detach', 'tttttttttttttttttttttttttttttttttttttttt')",
                   "range_paths ('TMP/clone-wt-check-7', 'bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb', "
                   "'tttttttttttttttttttttttttttttttttttttttt') []",
                   "hook_query ('HOOK', 'TMP/clone-wt-check-7', '--plan', 'paths') []",
                   "run_watched ['push', 'origin', 'tttttttttttttttttttttttttttttttttttttttt'] TMP/clone-wt-check-7 "
                   "[('PREPUSH_SKIP', 'x'), ('T', '1')] TMP/d/push-tttttttttt push [('limit', 9)]",
                   "git ('fetch', '--quiet', 'origin', 'main')",
                   "landed ('REPO', 'tttttttttttttttttttttttttttttttttttttttt', 'refs/remotes/origin/main') []",
                   'unlock',
                   'mark_gate_dirty () []',
                   "queue_verdict (ST, 'TMP/d', {'id': 'a', 'tip': 'ta', 'worktree': 'TMP/wt-0', 'status': "
                   "'checked', 'session': 's1'}, 'landed', 'LANDED ta (batch b1: 2 change(s) in one push)') "
                   "[('batch', 2), ('batch_id', 'b1'), ('code', 0), ('landed_sha', 'ta'), ('pushed_at', 4242.0)]",
                   "start_watch (ST, 'REPO', 'ta', 4242.0) [('close', 'TMP/wt-0'), ('session', 's1'), ('worktree', "
                   "'TMP/wt-0')]",
                   "queue_verdict (ST, 'TMP/d', {'id': 'b', 'tip': 'tb', 'worktree': 'TMP/wt-1', 'status': 'other'}, "
                   "'landed', 'LANDED tb (batch b1: 2 change(s) in one push)') [('batch', 2), ('batch_id', 'b1'), "
                   "('code', 0), ('landed_sha', 'tb'), ('pushed_at', 4242.0)]",
                   "start_watch (ST, 'REPO', 'tb', 4242.0) [('close', None), ('session', None), ('worktree', "
                   "'TMP/wt-1')]",
                   "batch_move (ST, {'id': 'b1'}, 'landed') [('text', 'LANDED "
                   "tttttttttttttttttttttttttttttttttttttttt (2 change(s) in one push)')]",
                   "wlog ('TMP/d', 'LANDED tttttttttttttttttttttttttttttttttttttttt (2 change(s) in one push)') []",
                   'slot release'],
                  '=== push TS tttttttttt rc 0\npushed\n'),
 'kept, plan unreadable': ('batch b1: its plan is unreadable',
                           ['slot acquire True',
                            'lock True',
                            "git ('fetch', '--quiet', 'origin', 'main')",
                            "git ('rev-parse', 'origin/main')",
                            "lock_note ('push of the batch tttttttttt',) []",
                            "git ('checkout', '-q', '--force', '--detach', "
                            "'tttttttttttttttttttttttttttttttttttttttt')",
                            "range_paths ('TMP/clone-wt-check-7', 'bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb', "
                            "'tttttttttttttttttttttttttttttttttttttttt') []",
                            "hook_query ('HOOK', 'TMP/clone-wt-check-7', '--plan', 'paths') []",
                            "batch_move (ST, {'id': 'b1', 'state': 'pushing', 'data': {'tip': "
                            "'tttttttttttttttttttttttttttttttttttttttt', 'base': "
                            "'bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb', 'tree': '7', 'members': ['a', 'b'], 'kept': "
                            'True}}, \'assembling\', {\'kept\': None, \'prechecked\': None}) [(\'text\', "batch b1: '
                            'the hook\'s plan for its push could not be read: checked again")]',
                            'unlock',
                            'slot release'],
                           None),
 'full and kept: full wins': ('batch b1: landed tttttttttt',
                              ['slot acquire True',
                               'lock True',
                               "git ('fetch', '--quiet', 'origin', 'main')",
                               "git ('rev-parse', 'origin/main')",
                               "lock_note ('push of the batch tttttttttt',) []",
                               "git ('checkout', '-q', '--force', '--detach', "
                               "'tttttttttttttttttttttttttttttttttttttttt')",
                               "set_change (ST, 'TMP/d', {'id': 'a', 'tip': 'ta', 'worktree': 'TMP/wt-0', 'status': "
                               "'checked', 'session': 's1'}, 'pushing') [('event', {'text': 'a: pushing in batch b1 "
                               "(tttttttttt)'})]",
                               "run_watched ['push', 'origin', 'tttttttttttttttttttttttttttttttttttttttt'] "
                               "TMP/clone-wt-check-7 [('T', '1')] TMP/d/push-tttttttttt push [('limit', 9)]",
                               "git ('fetch', '--quiet', 'origin', 'main')",
                               "landed ('REPO', 'tttttttttttttttttttttttttttttttttttttttt', "
                               "'refs/remotes/origin/main') []",
                               'unlock',
                               'mark_gate_dirty () []',
                               "queue_verdict (ST, 'TMP/d', {'id': 'a', 'tip': 'ta', 'worktree': 'TMP/wt-0', "
                               "'status': 'checked', 'session': 's1'}, 'landed', 'LANDED ta (batch b1: 2 change(s) "
                               "in one push)') [('batch', 2), ('batch_id', 'b1'), ('code', 0), ('landed_sha', 'ta'), "
                               "('pushed_at', 4242.0)]",
                               "start_watch (ST, 'REPO', 'ta', 4242.0) [('close', 'TMP/wt-0'), ('session', 's1'), "
                               "('worktree', 'TMP/wt-0')]",
                               "queue_verdict (ST, 'TMP/d', {'id': 'b', 'tip': 'tb', 'worktree': 'TMP/wt-1', "
                               "'status': 'other'}, 'landed', 'LANDED tb (batch b1: 2 change(s) in one push)') "
                               "[('batch', 2), ('batch_id', 'b1'), ('code', 0), ('landed_sha', 'tb'), ('pushed_at', "
                               '4242.0)]',
                               "start_watch (ST, 'REPO', 'tb', 4242.0) [('close', None), ('session', None), "
                               "('worktree', 'TMP/wt-1')]",
                               "batch_move (ST, {'id': 'b1'}, 'landed') [('text', 'LANDED "
                               "tttttttttttttttttttttttttttttttttttttttt (2 change(s) in one push)')]",
                               "wlog ('TMP/d', 'LANDED tttttttttttttttttttttttttttttttttttttttt (2 change(s) in one "
                               "push)') []",
                               'slot release'],
                              '=== push TS tttttttttt rc 0\npushed\n'),
 'another depth marks no member pushing': ('batch b1: landed tttttttttt',
                                           ['slot acquire True',
                                            'lock True',
                                            "git ('fetch', '--quiet', 'origin', 'main')",
                                            "git ('rev-parse', 'origin/main')",
                                            "lock_note ('push of the batch tttttttttt',) []",
                                            "git ('checkout', '-q', '--force', '--detach', "
                                            "'tttttttttttttttttttttttttttttttttttttttt')",
                                            "run_watched ['push', 'origin', "
                                            "'tttttttttttttttttttttttttttttttttttttttt'] TMP/clone-wt-check-7 "
                                            "[('PREPUSH_REUSE_FAST', '1'), ('T', '1')] TMP/d/push-tttttttttt push "
                                            "[('limit', 9)]",
                                            "git ('fetch', '--quiet', 'origin', 'main')",
                                            "landed ('REPO', 'tttttttttttttttttttttttttttttttttttttttt', "
                                            "'refs/remotes/origin/main') []",
                                            'unlock',
                                            'mark_gate_dirty () []',
                                            "queue_verdict (ST, 'TMP/d', {'id': 'a', 'tip': 'ta', 'worktree': "
                                            "'TMP/wt-0', 'status': 'checked', 'session': 's1'}, 'landed', 'LANDED ta "
                                            "(batch b1: 2 change(s) in one push)') [('batch', 2), ('batch_id', "
                                            "'b1'), ('code', 0), ('landed_sha', 'ta'), ('pushed_at', 4242.0)]",
                                            "start_watch (ST, 'REPO', 'ta', 4242.0) [('close', 'TMP/wt-0'), "
                                            "('session', 's1'), ('worktree', 'TMP/wt-0')]",
                                            "queue_verdict (ST, 'TMP/d', {'id': 'b', 'tip': 'tb', 'worktree': "
                                            "'TMP/wt-1', 'status': 'other'}, 'landed', 'LANDED tb (batch b1: 2 "
                                            "change(s) in one push)') [('batch', 2), ('batch_id', 'b1'), ('code', "
                                            "0), ('landed_sha', 'tb'), ('pushed_at', 4242.0)]",
                                            "start_watch (ST, 'REPO', 'tb', 4242.0) [('close', None), ('session', "
                                            "None), ('worktree', 'TMP/wt-1')]",
                                            "batch_move (ST, {'id': 'b1'}, 'landed') [('text', 'LANDED "
                                            "tttttttttttttttttttttttttttttttttttttttt (2 change(s) in one push)')]",
                                            "wlog ('TMP/d', 'LANDED tttttttttttttttttttttttttttttttttttttttt (2 "
                                            "change(s) in one push)') []",
                                            'slot release'],
                                           '=== push TS tttttttttt rc 0\npushed\n'),
 'red gate': ('batch b1: red',
              ['slot acquire True',
               'lock True',
               "git ('fetch', '--quiet', 'origin', 'main')",
               "git ('rev-parse', 'origin/main')",
               "lock_note ('push of the batch tttttttttt',) []",
               "git ('checkout', '-q', '--force', '--detach', 'tttttttttttttttttttttttttttttttttttttttt')",
               "run_watched ['push', 'origin', 'tttttttttttttttttttttttttttttttttttttttt'] TMP/clone-wt-check-7 "
               "[('PREPUSH_REUSE_FAST', '1'), ('T', '1')] TMP/d/push-tttttttttt push [('limit', 9)]",
               "git ('fetch', '--quiet', 'origin', 'main')",
               "landed ('REPO', 'tttttttttttttttttttttttttttttttttttttttt', 'refs/remotes/origin/main') []",
               "batch_move (ST, {'id': 'b1', 'state': 'pushing', 'data': {'tip': "
               "'tttttttttttttttttttttttttttttttttttttttt', 'base': 'bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb', "
               "'tree': '7', 'members': ['a', 'b']}}, 'assembling') [('text', 'batch b1: the gate went red before "
               "the push')]",
               'unlock',
               'slot release'],
              '=== push TS tttttttttt rc 1\nmain is red: only a fix lands\n'),
 'raced': ('batch b1: raced',
           ['slot acquire True',
            'lock True',
            "git ('fetch', '--quiet', 'origin', 'main')",
            "git ('rev-parse', 'origin/main')",
            "lock_note ('push of the batch tttttttttt',) []",
            "git ('checkout', '-q', '--force', '--detach', 'tttttttttttttttttttttttttttttttttttttttt')",
            "run_watched ['push', 'origin', 'tttttttttttttttttttttttttttttttttttttttt'] TMP/clone-wt-check-7 "
            "[('PREPUSH_REUSE_FAST', '1'), ('T', '1')] TMP/d/push-tttttttttt push [('limit', 9)]",
            "git ('fetch', '--quiet', 'origin', 'main')",
            "landed ('REPO', 'tttttttttttttttttttttttttttttttttttttttt', 'refs/remotes/origin/main') []",
            "batch_move (ST, {'id': 'b1', 'state': 'pushing', 'data': {'tip': "
            "'tttttttttttttttttttttttttttttttttttttttt', 'base': 'bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb', 'tree': "
            "'7', 'members': ['a', 'b']}}, 'assembling') [('text', 'batch b1: raced: assembled again')]",
            'unlock',
            'slot release'],
           '=== push TS tttttttttt rc 1\n! [rejected] (fetch first)\n'),
 'gate text after a stall': ('batch b1: its push stalled',
                             ['slot acquire True',
                              'lock True',
                              "git ('fetch', '--quiet', 'origin', 'main')",
                              "git ('rev-parse', 'origin/main')",
                              "lock_note ('push of the batch tttttttttt',) []",
                              "git ('checkout', '-q', '--force', '--detach', "
                              "'tttttttttttttttttttttttttttttttttttttttt')",
                              "run_watched ['push', 'origin', 'tttttttttttttttttttttttttttttttttttttttt'] "
                              "TMP/clone-wt-check-7 [('PREPUSH_REUSE_FAST', '1'), ('T', '1')] TMP/d/push-tttttttttt "
                              "push [('limit', 9)]",
                              "git ('fetch', '--quiet', 'origin', 'main')",
                              "landed ('REPO', 'tttttttttttttttttttttttttttttttttttttttt', "
                              "'refs/remotes/origin/main') []",
                              "batch_fail (ST, 'TMP/d', {'id': 'b1', 'state': 'pushing', 'data': {'tip': "
                              "'tttttttttttttttttttttttttttttttttttttttt', 'base': "
                              "'bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb', 'tree': '7', 'members': ['a', 'b']}}, "
                              "'its push stalled', 2) []",
                              'unlock',
                              'slot release'],
                             '=== push TS tttttttttt rc None\nmain is red: only a fix\n'),
 "a stall with a race's text": ('batch b1: its push stalled',
                                ['slot acquire True',
                                 'lock True',
                                 "git ('fetch', '--quiet', 'origin', 'main')",
                                 "git ('rev-parse', 'origin/main')",
                                 "lock_note ('push of the batch tttttttttt',) []",
                                 "git ('checkout', '-q', '--force', '--detach', "
                                 "'tttttttttttttttttttttttttttttttttttttttt')",
                                 "run_watched ['push', 'origin', 'tttttttttttttttttttttttttttttttttttttttt'] "
                                 "TMP/clone-wt-check-7 [('PREPUSH_REUSE_FAST', '1'), ('T', '1')] "
                                 "TMP/d/push-tttttttttt push [('limit', 9)]",
                                 "git ('fetch', '--quiet', 'origin', 'main')",
                                 "landed ('REPO', 'tttttttttttttttttttttttttttttttttttttttt', "
                                 "'refs/remotes/origin/main') []",
                                 "batch_fail (ST, 'TMP/d', {'id': 'b1', 'state': 'pushing', 'data': {'tip': "
                                 "'tttttttttttttttttttttttttttttttttttttttt', 'base': "
                                 "'bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb', 'tree': '7', 'members': ['a', 'b']}}, "
                                 "'its push stalled', 2) []",
                                 'unlock',
                                 'slot release'],
                                '=== push TS tttttttttt rc None\nfetch first\n'),
 'stalled': ('batch b1: its push stalled',
             ['slot acquire True',
              'lock True',
              "git ('fetch', '--quiet', 'origin', 'main')",
              "git ('rev-parse', 'origin/main')",
              "lock_note ('push of the batch tttttttttt',) []",
              "git ('checkout', '-q', '--force', '--detach', 'tttttttttttttttttttttttttttttttttttttttt')",
              "run_watched ['push', 'origin', 'tttttttttttttttttttttttttttttttttttttttt'] TMP/clone-wt-check-7 "
              "[('PREPUSH_REUSE_FAST', '1'), ('T', '1')] TMP/d/push-tttttttttt push [('limit', 9)]",
              "git ('fetch', '--quiet', 'origin', 'main')",
              "landed ('REPO', 'tttttttttttttttttttttttttttttttttttttttt', 'refs/remotes/origin/main') []",
              "batch_fail (ST, 'TMP/d', {'id': 'b1', 'state': 'pushing', 'data': {'tip': "
              "'tttttttttttttttttttttttttttttttttttttttt', 'base': 'bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb', "
              "'tree': '7', 'members': ['a', 'b']}}, 'its push stalled', 2) []",
              'unlock',
              'slot release'],
             '=== push TS tttttttttt rc None\n\n'),
 'failed': ('batch b1: its push failed: l3\n'
            '  l4\n'
            '  '
            'zzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzz',
            ['slot acquire True',
             'lock True',
             "git ('fetch', '--quiet', 'origin', 'main')",
             "git ('rev-parse', 'origin/main')",
             "lock_note ('push of the batch tttttttttt',) []",
             "git ('checkout', '-q', '--force', '--detach', 'tttttttttttttttttttttttttttttttttttttttt')",
             "run_watched ['push', 'origin', 'tttttttttttttttttttttttttttttttttttttttt'] TMP/clone-wt-check-7 "
             "[('PREPUSH_REUSE_FAST', '1'), ('T', '1')] TMP/d/push-tttttttttt push [('limit', 9)]",
             "git ('fetch', '--quiet', 'origin', 'main')",
             "landed ('REPO', 'tttttttttttttttttttttttttttttttttttttttt', 'refs/remotes/origin/main') []",
             "batch_fail (ST, 'TMP/d', {'id': 'b1', 'state': 'pushing', 'data': {'tip': "
             "'tttttttttttttttttttttttttttttttttttttttt', 'base': 'bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb', "
             "'tree': '7', 'members': ['a', 'b']}}, 'its push failed: l3\\n  l4\\n  "
             "zzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzz', "
             '2) []',
             'unlock',
             'slot release'],
            '=== push TS tttttttttt rc 1\n'
            'l1\n'
            'l2\n'
            'l3\n'
            'l4\n'
            'zzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzz\n'),
 'failed, no output': ('batch b1: its push failed: ',
                       ['slot acquire True',
                        'lock True',
                        "git ('fetch', '--quiet', 'origin', 'main')",
                        "git ('rev-parse', 'origin/main')",
                        "lock_note ('push of the batch tttttttttt',) []",
                        "git ('checkout', '-q', '--force', '--detach', 'tttttttttttttttttttttttttttttttttttttttt')",
                        "run_watched ['push', 'origin', 'tttttttttttttttttttttttttttttttttttttttt'] "
                        "TMP/clone-wt-check-7 [('PREPUSH_REUSE_FAST', '1'), ('T', '1')] TMP/d/push-tttttttttt push "
                        "[('limit', 9)]",
                        "git ('fetch', '--quiet', 'origin', 'main')",
                        "landed ('REPO', 'tttttttttttttttttttttttttttttttttttttttt', 'refs/remotes/origin/main') []",
                        "batch_fail (ST, 'TMP/d', {'id': 'b1', 'state': 'pushing', 'data': {'tip': "
                        "'tttttttttttttttttttttttttttttttttttttttt', 'base': "
                        "'bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb', 'tree': '7', 'members': ['a', 'b']}}, 'its push "
                        "failed: ', 2) []",
                        'unlock',
                        'slot release'],
                       '=== push TS tttttttttt rc 2\nNone\n')}
