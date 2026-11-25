"""`agentctl red status`'s text (redstatus.render_red_status), characterised over the status model's shapes: reds with and
without a family and history, reverts unread or unacked, sections unread, never-green workflows (verbose only), a
family with its fixer, an orphaned lease or none, owners (verbose adds their context and rollovers), the dispatcher
on, off, unread, a repo switched off, the laptop's pressure."""
import importlib
import os
import sys
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from test_agentctl import A  # noqa: E402,F401 (puts lib on the path)

RED = {"state": "fixing", "what": "Main", "key": "k1", "text": "failed", "family": "fam", "history": [
    {"at": "t1", "holder": "h", "outcome": "x" * 200}, {"at": "t2", "holder": "h2", "outcome": None}]}
RED2 = {"state": "open", "what": "Conv", "key": "k2", "text": "red", "history": []}
FAMILIES = [{"family": "fam", "open": 2, "holder": "agent-1", "worktree": os.path.expanduser("~/wt"), "at_work": True, "work": "fixing"},
            {"family": "orph", "open": 1, "holder": "agent-2", "at_work": False, "work": "w"},
            {"family": "idle", "open": 1}, {"family": "quiet", "open": 3, "holder": "a3", "at_work": False if False else None}]
OWNERS = [{"family": "fam", "worktree": os.path.expanduser("~/wt"), "alive": True, "turn": 3, "reds": 2, "restarts": 1,
           "context": 5000, "rollovers": [{"at": "r%d" % i, "old": "abcdefghij", "new": None if i == 3 else "klmnopqrst",
                                           "tokens": 100 * i} for i in range(5)]},
          {"family": "gone", "worktree": "/other/wt", "alive": False, "ended": True, "verdict": "done"},
          {"family": "no worktree", "alive": True}]
DISPATCH = {"alive": 1, "cap": 3, "blocker": None, "repos_off": {}, "pressure": None}
MODEL = {"reds": [RED, RED2], "families": FAMILIES, "owners": OWNERS, "dispatcher": DISPATCH}
SCENARIOS = {
    "reds unread": ({"reds": {"error": "timeout"}}, False),
    "nothing open": ({"reds": [], "dispatcher": DISPATCH}, False),
    "everything": (MODEL, False),
    "everything, verbose": (MODEL, True),
    "reverts unacked": ({**MODEL, "reverts": [{"id": 1}, {"id": 2}]}, False),
    "reverts unread": ({**MODEL, "reverts": {"error": "no store"}}, False),
    "reverts empty": ({**MODEL, "reverts": []}, False),
    "families and owners unread": ({**MODEL, "families": {"error": "slow"}, "owners": {"error": "slower"}}, False),
    "owners unread, verbose": ({**MODEL, "owners": {"error": "slower"}}, True),
    "untracked, verbose": ({**MODEL, "untracked": [
        {"repo": "movies", "workflow": "wf", "run": {"number": 4}, "jobs": ["a", "b"], "why": "never green"},
        {"repo": "other/x", "workflow": "wg", "why": "w"}]}, True),
    "untracked, quiet": ({**MODEL, "untracked": [{"repo": "movies", "workflow": "wf", "why": "never green"}]}, False),
    "untracked empty, verbose": ({**MODEL, "untracked": []}, True),
    "dispatcher off": ({**MODEL, "dispatcher": {**DISPATCH, "blocker": "paused", "repos_off": {"a/b": {"at": "t"}, "c/d": {}},
                                               "pressure": "memory 90%"}}, False),
    "dispatcher unread": ({**MODEL, "dispatcher": {"error": "boom"}}, False),
}


def render_all(module):
    with mock.patch.object(module, "revert_line", lambda r, now: f"revert {r['id']} at {now:g}"), \
            mock.patch.object(module, "red_repo_short", lambda repo: f"<{repo}>"), \
            mock.patch.object(module, "RED_PRIMARY", "movies"), \
            mock.patch.object(module, "HOME", os.path.expanduser("~")), \
            mock.patch.object(module.time, "time", lambda: 12345.0):
        return {name: module.render_red_status(m, verbose) for name, (m, verbose) in SCENARIOS.items()}


class RedStatusRenderTest(unittest.TestCase):
    def test_every_scenario_renders_as_it_did(self):
        got = render_all(importlib.import_module("agentctl.redstatus"))
        for name in SCENARIOS:
            with self.subTest(name):
                self.assertEqual(got[name], EXPECTED[name], name)

EXPECTED = {'reds unread': 'reds: unread (timeout)',
 'nothing open': 'reds: none open\n'
                 'dispatcher: ON; fixers alive: 1/3; gate reds never wait (no slot cap, no laptop pressure)',
 'everything': 'reds (2 open):\n'
               '  fixing           Main k1 [fam]: failed\n'
               '  open             Conv k2: red\n'
               'family fam: 2 open red(s), ONE fixer: agent-1 (~/wt): fixing\n'
               'family orph: 1 open red(s), ONE fixer: ORPHANED LEASE (agent-2: w) -> revoked and re-offered at the '
               'next tick\n'
               'family idle: 1 open red(s), ONE fixer: none at work\n'
               'family quiet: 3 open red(s), ONE fixer: a3 (?)\n'
               'owner fam: ALIVE, turn 3, 2 red(s) in hand, 1 restart(s); ~/wt\n'
               'owner gone: not running, turn 0, 0 red(s) in hand, 0 restart(s); /other/wt (ended: done)\n'
               'dispatcher: ON; fixers alive: 1/3; gate reds never wait (no slot cap, no laptop pressure)',
 'everything, verbose': 'reds (2 open):\n'
                        '  fixing           Main k1 [fam]: failed\n'
                        '      t1 h: '
                        'xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx\n'
                        '      t2 h2: None\n'
                        '  open             Conv k2: red\n'
                        'family fam: 2 open red(s), ONE fixer: agent-1 (~/wt): fixing\n'
                        'family orph: 1 open red(s), ONE fixer: ORPHANED LEASE (agent-2: w) -> revoked and '
                        're-offered at the next tick\n'
                        'family idle: 1 open red(s), ONE fixer: none at work\n'
                        'family quiet: 3 open red(s), ONE fixer: a3 (?)\n'
                        'owner fam: ALIVE, turn 3, 2 red(s) in hand, 1 restart(s); ~/wt\n'
                        '      context at its last resume check: 5000 tokens\n'
                        '      rollover r2: abcdefgh -> klmnopqr (200 tokens)\n'
                        '      rollover r3: abcdefgh -> starting (300 tokens)\n'
                        '      rollover r4: abcdefgh -> klmnopqr (400 tokens)\n'
                        'owner gone: not running, turn 0, 0 red(s) in hand, 0 restart(s); /other/wt (ended: done)\n'
                        '      context at its last resume check: unread tokens\n'
                        'dispatcher: ON; fixers alive: 1/3; gate reds never wait (no slot cap, no laptop pressure)',
 'reverts unacked': 'reds (2 open):\n'
                    '  fixing           Main k1 [fam]: failed\n'
                    '  open             Conv k2: red\n'
                    'reverts (2 unacked: `agentctl revert ack <id> --rework|--drop`):\n'
                    'revert 1 at 12345\n'
                    'revert 2 at 12345\n'
                    'family fam: 2 open red(s), ONE fixer: agent-1 (~/wt): fixing\n'
                    'family orph: 1 open red(s), ONE fixer: ORPHANED LEASE (agent-2: w) -> revoked and re-offered at '
                    'the next tick\n'
                    'family idle: 1 open red(s), ONE fixer: none at work\n'
                    'family quiet: 3 open red(s), ONE fixer: a3 (?)\n'
                    'owner fam: ALIVE, turn 3, 2 red(s) in hand, 1 restart(s); ~/wt\n'
                    'owner gone: not running, turn 0, 0 red(s) in hand, 0 restart(s); /other/wt (ended: done)\n'
                    'dispatcher: ON; fixers alive: 1/3; gate reds never wait (no slot cap, no laptop pressure)',
 'reverts unread': 'reds (2 open):\n'
                   '  fixing           Main k1 [fam]: failed\n'
                   '  open             Conv k2: red\n'
                   'reverts: unread (no store)\n'
                   'family fam: 2 open red(s), ONE fixer: agent-1 (~/wt): fixing\n'
                   'family orph: 1 open red(s), ONE fixer: ORPHANED LEASE (agent-2: w) -> revoked and re-offered at '
                   'the next tick\n'
                   'family idle: 1 open red(s), ONE fixer: none at work\n'
                   'family quiet: 3 open red(s), ONE fixer: a3 (?)\n'
                   'owner fam: ALIVE, turn 3, 2 red(s) in hand, 1 restart(s); ~/wt\n'
                   'owner gone: not running, turn 0, 0 red(s) in hand, 0 restart(s); /other/wt (ended: done)\n'
                   'dispatcher: ON; fixers alive: 1/3; gate reds never wait (no slot cap, no laptop pressure)',
 'reverts empty': 'reds (2 open):\n'
                  '  fixing           Main k1 [fam]: failed\n'
                  '  open             Conv k2: red\n'
                  'family fam: 2 open red(s), ONE fixer: agent-1 (~/wt): fixing\n'
                  'family orph: 1 open red(s), ONE fixer: ORPHANED LEASE (agent-2: w) -> revoked and re-offered at '
                  'the next tick\n'
                  'family idle: 1 open red(s), ONE fixer: none at work\n'
                  'family quiet: 3 open red(s), ONE fixer: a3 (?)\n'
                  'owner fam: ALIVE, turn 3, 2 red(s) in hand, 1 restart(s); ~/wt\n'
                  'owner gone: not running, turn 0, 0 red(s) in hand, 0 restart(s); /other/wt (ended: done)\n'
                  'dispatcher: ON; fixers alive: 1/3; gate reds never wait (no slot cap, no laptop pressure)',
 'families and owners unread': 'reds (2 open):\n'
                               '  fixing           Main k1 [fam]: failed\n'
                               '  open             Conv k2: red\n'
                               'families: unread (slow)\n'
                               'owners: unread (slower)\n'
                               'dispatcher: ON; fixers alive: 1/3; gate reds never wait (no slot cap, no laptop '
                               'pressure)',
 'owners unread, verbose': 'reds (2 open):\n'
                           '  fixing           Main k1 [fam]: failed\n'
                           '      t1 h: '
                           'xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx\n'
                           '      t2 h2: None\n'
                           '  open             Conv k2: red\n'
                           'owners: unread (slower)\n'
                           'family fam: 2 open red(s), ONE fixer: agent-1 (~/wt): fixing\n'
                           'family orph: 1 open red(s), ONE fixer: ORPHANED LEASE (agent-2: w) -> revoked and '
                           're-offered at the next tick\n'
                           'family idle: 1 open red(s), ONE fixer: none at work\n'
                           'family quiet: 3 open red(s), ONE fixer: a3 (?)\n'
                           'dispatcher: ON; fixers alive: 1/3; gate reds never wait (no slot cap, no laptop '
                           'pressure)',
 'untracked, verbose': 'reds (2 open):\n'
                       '  fixing           Main k1 [fam]: failed\n'
                       '      t1 h: '
                       'xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx\n'
                       '      t2 h2: None\n'
                       '  open             Conv k2: red\n'
                       'untracked (never green) (2):\n'
                       '  wf #4 [a, b]: never green\n'
                       '  <other/x> wg #None: w\n'
                       'family fam: 2 open red(s), ONE fixer: agent-1 (~/wt): fixing\n'
                       'family orph: 1 open red(s), ONE fixer: ORPHANED LEASE (agent-2: w) -> revoked and re-offered '
                       'at the next tick\n'
                       'family idle: 1 open red(s), ONE fixer: none at work\n'
                       'family quiet: 3 open red(s), ONE fixer: a3 (?)\n'
                       'owner fam: ALIVE, turn 3, 2 red(s) in hand, 1 restart(s); ~/wt\n'
                       '      context at its last resume check: 5000 tokens\n'
                       '      rollover r2: abcdefgh -> klmnopqr (200 tokens)\n'
                       '      rollover r3: abcdefgh -> starting (300 tokens)\n'
                       '      rollover r4: abcdefgh -> klmnopqr (400 tokens)\n'
                       'owner gone: not running, turn 0, 0 red(s) in hand, 0 restart(s); /other/wt (ended: done)\n'
                       '      context at its last resume check: unread tokens\n'
                       'dispatcher: ON; fixers alive: 1/3; gate reds never wait (no slot cap, no laptop pressure)',
 'untracked, quiet': 'reds (2 open):\n'
                     '  fixing           Main k1 [fam]: failed\n'
                     '  open             Conv k2: red\n'
                     'family fam: 2 open red(s), ONE fixer: agent-1 (~/wt): fixing\n'
                     'family orph: 1 open red(s), ONE fixer: ORPHANED LEASE (agent-2: w) -> revoked and re-offered '
                     'at the next tick\n'
                     'family idle: 1 open red(s), ONE fixer: none at work\n'
                     'family quiet: 3 open red(s), ONE fixer: a3 (?)\n'
                     'owner fam: ALIVE, turn 3, 2 red(s) in hand, 1 restart(s); ~/wt\n'
                     'owner gone: not running, turn 0, 0 red(s) in hand, 0 restart(s); /other/wt (ended: done)\n'
                     'dispatcher: ON; fixers alive: 1/3; gate reds never wait (no slot cap, no laptop pressure)',
 'untracked empty, verbose': 'reds (2 open):\n'
                             '  fixing           Main k1 [fam]: failed\n'
                             '      t1 h: '
                             'xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx\n'
                             '      t2 h2: None\n'
                             '  open             Conv k2: red\n'
                             'family fam: 2 open red(s), ONE fixer: agent-1 (~/wt): fixing\n'
                             'family orph: 1 open red(s), ONE fixer: ORPHANED LEASE (agent-2: w) -> revoked and '
                             're-offered at the next tick\n'
                             'family idle: 1 open red(s), ONE fixer: none at work\n'
                             'family quiet: 3 open red(s), ONE fixer: a3 (?)\n'
                             'owner fam: ALIVE, turn 3, 2 red(s) in hand, 1 restart(s); ~/wt\n'
                             '      context at its last resume check: 5000 tokens\n'
                             '      rollover r2: abcdefgh -> klmnopqr (200 tokens)\n'
                             '      rollover r3: abcdefgh -> starting (300 tokens)\n'
                             '      rollover r4: abcdefgh -> klmnopqr (400 tokens)\n'
                             'owner gone: not running, turn 0, 0 red(s) in hand, 0 restart(s); /other/wt (ended: '
                             'done)\n'
                             '      context at its last resume check: unread tokens\n'
                             'dispatcher: ON; fixers alive: 1/3; gate reds never wait (no slot cap, no laptop '
                             'pressure)',
 'dispatcher off': 'reds (2 open):\n'
                   '  fixing           Main k1 [fam]: failed\n'
                   '  open             Conv k2: red\n'
                   'family fam: 2 open red(s), ONE fixer: agent-1 (~/wt): fixing\n'
                   'family orph: 1 open red(s), ONE fixer: ORPHANED LEASE (agent-2: w) -> revoked and re-offered at '
                   'the next tick\n'
                   'family idle: 1 open red(s), ONE fixer: none at work\n'
                   'family quiet: 3 open red(s), ONE fixer: a3 (?)\n'
                   'owner fam: ALIVE, turn 3, 2 red(s) in hand, 1 restart(s); ~/wt\n'
                   'owner gone: not running, turn 0, 0 red(s) in hand, 0 restart(s); /other/wt (ended: done)\n'
                   'dispatcher: OFF -- paused; fixers alive: 1/3; gate reds never wait (no slot cap, no laptop '
                   'pressure)\n'
                   'dispatch off for a/b (owner) since t: its reds recorded, never offered or asked\n'
                   'dispatch off for c/d (owner) since None: its reds recorded, never offered or asked\n'
                   'laptop: memory 90% (holds other work only, never a red fix)',
 'dispatcher unread': 'reds (2 open):\n'
                      '  fixing           Main k1 [fam]: failed\n'
                      '  open             Conv k2: red\n'
                      'family fam: 2 open red(s), ONE fixer: agent-1 (~/wt): fixing\n'
                      'family orph: 1 open red(s), ONE fixer: ORPHANED LEASE (agent-2: w) -> revoked and re-offered '
                      'at the next tick\n'
                      'family idle: 1 open red(s), ONE fixer: none at work\n'
                      'family quiet: 3 open red(s), ONE fixer: a3 (?)\n'
                      'owner fam: ALIVE, turn 3, 2 red(s) in hand, 1 restart(s); ~/wt\n'
                      'owner gone: not running, turn 0, 0 red(s) in hand, 0 restart(s); /other/wt (ended: done)\n'
                      'dispatcher: unread (boom)'}
