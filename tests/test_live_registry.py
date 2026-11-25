"""THE LIVE AGENTS (live.live_registry_read, live_state, live_annotate), characterised with the file system, the transcript
checks and the process probe faked: which fixer records, dispatch records, leased holders and subagent transcripts make
registry entries (one id per transcript, the fixer's first), each state a live agent can be in, and the rows the Agents
page gains. `run_*` take the module so a copy of the code can be driven the same way."""
import contextlib
import copy
import os
import sys
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from test_agentctl import A  # noqa: E402,F401 (puts lib on the path)

T0 = 1_000_000.0
AGE = 86400.0


class FakeOs:
    """The real os, but for lstat and getuid: a path's owner is the script's."""

    def __init__(self, owners):
        self.owners = owners

    def __getattr__(self, name):
        return getattr(os, name)

    def getuid(self):
        return 1

    def lstat(self, p):
        if p not in self.owners:
            raise OSError("gone")
        return type("St", (), {"st_uid": self.owners[p]})()


def run_registry(module, script):
    """live_registry_read(T0) over the script's files: the registry."""
    files = script.get("files", {})
    ok = set(script.get("ok", []))
    with contextlib.ExitStack() as stack:
        fake_glob = type("G", (), {"glob": staticmethod(lambda pat: list(script.get("globs", {}).get(pat, [])))})
        patches = {
            "glob": fake_glob, "reds_dir": lambda *p: "/R/" + "/".join(p), "dispatches_dir": lambda *p: "/D/" + "/".join(p),
            "mtime": lambda p: T0 - script.get("age", {}).get(p, 0) * AGE, "read_record": lambda p: copy.deepcopy(files.get(p)),
            "live_transcript_ok": lambda p: p if p in ok else None,
            "live_session_transcript": lambda s, wt=None: script.get("sessions", {}).get(s),
            "live_globs": lambda: script.get("patterns", ["/G/*"]), "live_meta": lambda p: script.get("meta", {}).get(p, {}),
            "_live_leased": copy.deepcopy(script.get("leased", {})), "os": FakeOs(script.get("owners", {})),
        }
        for name, fake in patches.items():
            stack.enter_context(mock.patch.object(module, name, fake))
        return module.live_registry_read(T0)


def run_state(module, script):
    with mock.patch.object(module, "proc_alive", lambda pid, start: script["alive"]):
        return module.live_state(script["e"], script.get("info", {}), T0)


def run_annotate(module, script):
    """live_annotate(agents, T0): (the agents after, the leased map)."""
    agents = copy.deepcopy(script["agents"])
    ok = set(script.get("ok", []))
    leased = {}
    with contextlib.ExitStack() as stack:
        patches = {
            "live_transcript_ok": lambda p: p if p in ok else None, "_live_leased": leased,
            "live_registry": lambda fresh=False: copy.deepcopy(script["reg"]),
            "mtime": lambda p: T0 - script.get("age", {}).get(p, 0), "live_tail_info": lambda p: dict(script.get("infos", {}).get(p, {})),
            "proc_alive": lambda pid, start: script.get("alive", {}).get(pid),
        }
        for name, fake in patches.items():
            stack.enter_context(mock.patch.object(module, name, fake))
        return module.live_annotate(agents, T0), dict(leased)


def E(aid, path, kind="session", **more):
    return {"id": aid, "transcript": path, "kind": kind, **more}


FIX = "/R/101/fixer.json"
REGISTRY = {
    "nothing": dict(),
    "a fixer by its transcript": dict(globs={"/R/*/fixer*.json": [FIX]}, files={FIX: {"transcript": "/p/a.jsonl", "pid": 4, "label": "L", "run": 101}},
                                      ok=["/p/a.jsonl"]),
    "a fixer, numbered, its log as the transcript": dict(globs={"/R/*/fixer*.json": ["/R/102/fixer-3.json"]},
                                                         files={"/R/102/fixer-3.json": {"log": "/p/b.jsonl", "run": 102}}, ok=["/p/b.jsonl"]),
    "a fixer, a log that is no transcript, its session": dict(globs={"/R/*/fixer*.json": [FIX]},
                                                              files={FIX: {"log": "/p/x.log", "session": "s1", "worktree": "/w"}},
                                                              sessions={"s1": "/p/s.jsonl"}, ok=["/p/s.jsonl"]),
    "a fixer, not a fixer file name": dict(globs={"/R/*/fixer*.json": ["/R/103/fixerx.json"]}, files={"/R/103/fixerx.json": {"transcript": "/p/a.jsonl"}},
                                           ok=["/p/a.jsonl"]),
    "a fixer, too old": dict(globs={"/R/*/fixer*.json": [FIX]}, files={FIX: {"transcript": "/p/a.jsonl"}}, ok=["/p/a.jsonl"], age={FIX: 8}),
    "a fixer, no record": dict(globs={"/R/*/fixer*.json": [FIX]}, ok=["/p/a.jsonl"]),
    "a fixer, transcript not allowed": dict(globs={"/R/*/fixer*.json": [FIX]}, files={FIX: {"transcript": "/p/a.jsonl"}}),
    "a dispatch session": dict(globs={"/D/*.json": ["/D/1.json"]}, files={"/D/1.json": {"session": "abcdef123456", "agent": "ag", "purpose": "do it", "epoch": 5}},
                               sessions={"abcdef123456": "/p/s.jsonl"}, ok=["/p/s.jsonl"]),
    "a dispatch without a session": dict(globs={"/D/*.json": ["/D/1.json"]}, files={"/D/1.json": {"epoch": 5}}),
    "a dispatch, too old": dict(globs={"/D/*.json": ["/D/1.json"]}, files={"/D/1.json": {"session": "s"}}, age={"/D/1.json": 9},
                                sessions={"s": "/p/s.jsonl"}, ok=["/p/s.jsonl"]),
    "two dispatch records of one session: the newest": dict(globs={"/D/*.json": ["/D/1.json", "/D/2.json"]},
                                                            files={"/D/1.json": {"session": "s", "epoch": 1, "pid": 1},
                                                                   "/D/2.json": {"session": "s", "epoch": 2, "pid": 2}},
                                                            sessions={"s": "/p/s.jsonl"}, ok=["/p/s.jsonl"]),
    "a leased holder": dict(leased={"session-x": {"id": "session-x", "transcript": "/p/l.jsonl", "kind": "lease", "label": "H"}}, ok=["/p/l.jsonl"]),
    "a fixer wins over its session": dict(globs={"/R/*/fixer*.json": [FIX], "/D/*.json": ["/D/1.json"]},
                                          files={FIX: {"transcript": "/p/a.jsonl", "run": 101}, "/D/1.json": {"session": "s", "epoch": 1}},
                                          sessions={"s": "/p/a.jsonl"}, ok=["/p/a.jsonl"]),
    "a subagent": dict(globs={"/G/*": ["/G/agent-1.output"]}, owners={"/G/agent-1.output": 1}, ok=["/G/agent-1.output"],
                       meta={"/G/agent-1.output": {}}),
    "a subagent with a meta": dict(globs={"/G/*": ["/G/a.jsonl"]}, owners={"/G/a.jsonl": 1}, ok=["/G/a.jsonl"],
                                   meta={"/G/a.meta.json": {"description": "D" * 200, "agentType": "T", "finished": True}}),
    "a subagent of another user": dict(globs={"/G/*": ["/G/a.jsonl"]}, owners={"/G/a.jsonl": 2}, ok=["/G/a.jsonl"]),
    "a subagent too old": dict(globs={"/G/*": ["/G/a.jsonl"]}, owners={"/G/a.jsonl": 1}, ok=["/G/a.jsonl"], age={"/G/a.jsonl": 9}),
    "a subagent that cannot be read": dict(globs={"/G/*": ["/G/a.jsonl"]}, ok=["/G/a.jsonl"]),
    "two patterns": dict(patterns=["/G/*", "/H/*"], globs={"/G/*": ["/G/a.jsonl"], "/H/*": ["/H/b.jsonl"]}, owners={"/G/a.jsonl": 1, "/H/b.jsonl": 1},
                         ok=["/G/a.jsonl", "/H/b.jsonl"]),
}
STATES = {
    "ended ok": dict(e={"ended": 1, "rc": 0, "verdict": "V"}), "ended failed": dict(e={"ended": 1, "rc": 2}),
    "ended with a verdict": dict(e={"ended": 1, "rc": 2, "verdict": "no"}),
    "api error": dict(e={"pid": 1}, info={"api_error": True}, alive=True),
    "process gone after an answer": dict(e={"pid": 1}, info={"stop": "end_turn"}, alive=False),
    "process gone mid-turn": dict(e={"pid": 1}, info={"stop": None}, alive=False),
    "alive after an answer": dict(e={"pid": 1}, info={"stop": "end_turn"}, alive=True),
    "alive mid-turn": dict(e={"pid": 1}, info={"stop": "tool_use"}, alive=True),
    "pid unknown, finished": dict(e={"pid": 1, "finished": True}, alive=None),
    "no pid, answered": dict(e={}, info={"stop": "end_turn"}),
    "no pid, quiet long": dict(e={}, info={"mtime": T0 - 3600}),
    "no pid, quiet shortly": dict(e={}, info={"mtime": T0 - 60}),
    "no pid, no mtime": dict(e={}),
    "alive 0": dict(e={"pid": 1}, info={}, alive=0),
}
S1 = {"id": "session-a", "transcript": "/p/a.jsonl", "kind": "session", "label": "A", "pid": 1}
ANNOTATE = {
    "no agents, nothing known": dict(agents=[], reg={}),
    "an agent by its transcript": dict(agents=[{"transcript": "/p/a.jsonl"}], ok=["/p/a.jsonl"], reg={"session-a": S1},
                                       infos={"/p/a.jsonl": {"stop": "end_turn"}}, alive={1: False}),
    "an agent whose transcript is unknown": dict(agents=[{"transcript": "/p/z.jsonl"}], ok=["/p/z.jsonl"], reg={"session-a": S1}),
    "an agent not allowed": dict(agents=[{"transcript": "/p/a.jsonl"}], reg={"session-a": S1}),
    "an ended agent": dict(agents=[{"transcript": "/p/a.jsonl", "ended": True}], ok=["/p/a.jsonl"], reg={"session-a": S1}),
    "a lease holder is learned": dict(agents=[{"transcript": "/p/a.jsonl", "leases": ["l"], "label": "Lab", "holder": "h", "pid": 9}],
                                      ok=["/p/a.jsonl"], reg={}),
    "a lease holder with a kind": dict(agents=[{"transcript": "/p/a.jsonl", "leases": ["l"], "kind": "K", "holder": "h"}], ok=["/p/a.jsonl"], reg={}),
    "an extra recent row": dict(agents=[], reg={"session-a": S1}, age={"/p/a.jsonl": 60}, infos={"/p/a.jsonl": {"stop": "end_turn"}}, alive={1: True}),
    "an extra row, too old": dict(agents=[], reg={"session-a": S1}, age={"/p/a.jsonl": 4 * 3600}),
    "an extra fixer is no row": dict(agents=[], reg={"fixer-1-1": E("fixer-1-1", "/p/f.jsonl", "fixer")}, age={"/p/f.jsonl": 1}),
    "a shown agent is no extra row": dict(agents=[{"transcript": "/p/a.jsonl"}], ok=["/p/a.jsonl"], reg={"session-a": S1}, age={"/p/a.jsonl": 1}),
    "extra rows newest first, capped": dict(agents=[], reg={f"session-{i}": E(f"session-{i}", f"/p/{i}.jsonl", pid=None) for i in range(15)},
                                            age={f"/p/{i}.jsonl": 10 + i for i in range(15)}),
}


def generate_states(rng, n):
    return [dict(e={k: v for k, v in (("ended", rng.choice([None, 1])), ("rc", rng.choice([None, 0, 3])), ("verdict", rng.choice([None, "V"])),
                                      ("pid", rng.choice([None, 1])), ("finished", rng.choice([None, True]))) if v is not None},
                 info={k: v for k, v in (("stop", rng.choice([None, "end_turn", "tool_use"])), ("api_error", rng.choice([None, True])),
                                         ("mtime", rng.choice([None, T0 - 10, T0 - 4000]))) if v is not None},
                 alive=rng.choice([None, True, False, 0])) for _ in range(n)]


def generate_registry(rng, n):
    out = []
    for _ in range(n):
        fixers = [f"/R/{i}/fixer{rng.choice(['', '-2', 'x'])}.json" for i in range(rng.randint(0, 3))]
        disp = [f"/D/{i}.json" for i in range(rng.randint(0, 3))]
        subs = [f"/G/{rng.choice(['a', 'b', 'c'])}{i}.jsonl" for i in range(rng.randint(0, 3))]
        paths = ["/p/1.jsonl", "/p/2.jsonl", "/p/3.jsonl"]
        files = {f: rng.choice([None, {"transcript": rng.choice(paths), "run": 5, "pid": 1}, {"log": rng.choice(paths + ["/x.log"]), "session": "s1"}])
                 for f in fixers}
        files.update({d: rng.choice([None, {"session": rng.choice(["s1", "s2", None]), "epoch": rng.randint(0, 3), "agent": rng.choice([None, "ag"])}])
                      for d in disp})
        out.append(dict(globs={"/R/*/fixer*.json": fixers, "/D/*.json": disp, "/G/*": subs}, files=files,
                        sessions={"s1": rng.choice(paths), "s2": rng.choice(paths)}, ok=rng.sample(paths + subs, rng.randint(0, len(paths + subs))),
                        owners={s: rng.choice([1, 1, 2]) for s in subs}, age={p: rng.choice([0, 0, 7, 9]) for p in fixers + disp + subs},
                        leased=rng.choice([{}, {"session-x": {"id": "session-x", "transcript": rng.choice(paths), "kind": "lease"}}]),
                        meta={s[:s.rfind(".")] + ".meta.json": {"description": "d", "finished": True} for s in subs[:1]}))
    return out


def generate_annotate(rng, n):
    out = []
    for _ in range(n):
        paths = [f"/p/{i}.jsonl" for i in range(5)]
        reg = {f"session-{i}": E(f"session-{i}", paths[i], rng.choice(["session", "fixer", "subagent"]), pid=rng.choice([None, i + 1]), ended=rng.choice([None, True]))
               for i in range(rng.randint(0, 5))}
        agents = [{k: v for k, v in (("transcript", rng.choice(paths)), ("leases", rng.choice([None, ["l"]])), ("ended", rng.choice([None, True])),
                                     ("label", "L"), ("pid", rng.choice([None, 2]))) if v is not None} for _ in range(rng.randint(0, 4))]
        out.append(dict(agents=agents, reg=reg, ok=rng.sample(paths, rng.randint(0, 5)), age={p: rng.choice([10, 5000, 10800, 20000]) for p in paths},
                        infos={p: {"stop": rng.choice([None, "end_turn"]), "mtime": T0 - rng.choice([10, 4000])} for p in paths},
                        alive={i: rng.choice([None, True, False]) for i in range(1, 7)}))
    return out


class LiveRegistryTest(unittest.TestCase):
    def test_every_read_is_what_it_was(self):
        module = __import__("agentctl.live", fromlist=["x"])
        for kind, runner, table in (("registry", run_registry, REGISTRY), ("state", run_state, STATES), ("annotate", run_annotate, ANNOTATE)):
            for name, script in table.items():
                with self.subTest(f"{kind} {name}"):
                    self.assertEqual(runner(module, script), EXPECTED[kind][name], name)


def results():
    module = __import__("agentctl.live", fromlist=["x"])
    return {kind: {n: runner(module, s) for n, s in table.items()}
            for kind, runner, table in (("registry", run_registry, REGISTRY), ("state", run_state, STATES), ("annotate", run_annotate, ANNOTATE))}


EXPECTED = {'registry': {'nothing': {},
              'a fixer by its transcript': {'fixer-101-1': {'id': 'fixer-101-1',
                                                            'transcript': '/p/a.jsonl',
                                                            'kind': 'fixer',
                                                            'label': 'L',
                                                            'pid': 4,
                                                            'process_start': None,
                                                            'started': None,
                                                            'ended': None,
                                                            'rc': None,
                                                            'verdict': None,
                                                            'worktree': None,
                                                            'run': 101,
                                                            'record': '/R/101/fixer.json'}},
              'a fixer, numbered, its log as the transcript': {'fixer-102-3': {'id': 'fixer-102-3',
                                                                               'transcript': '/p/b.jsonl',
                                                                               'kind': 'fixer',
                                                                               'label': 'red fixer #102',
                                                                               'pid': None,
                                                                               'process_start': None,
                                                                               'started': None,
                                                                               'ended': None,
                                                                               'rc': None,
                                                                               'verdict': None,
                                                                               'worktree': None,
                                                                               'run': 102,
                                                                               'record': '/R/102/fixer-3.json'}},
              'a fixer, a log that is no transcript, its session': {'fixer-101-1': {'id': 'fixer-101-1',
                                                                                    'transcript': '/p/s.jsonl',
                                                                                    'kind': 'fixer',
                                                                                    'label': 'red fixer #101',
                                                                                    'pid': None,
                                                                                    'process_start': None,
                                                                                    'started': None,
                                                                                    'ended': None,
                                                                                    'rc': None,
                                                                                    'verdict': None,
                                                                                    'worktree': '/w',
                                                                                    'run': None,
                                                                                    'record': '/R/101/fixer.json'}},
              'a fixer, not a fixer file name': {},
              'a fixer, too old': {},
              'a fixer, no record': {},
              'a fixer, transcript not allowed': {},
              'a dispatch session': {'session-abcdef123456': {'id': 'session-abcdef123456',
                                                              'transcript': '/p/s.jsonl',
                                                              'kind': 'session',
                                                              'label': 'session abcdef12 · ag · do it',
                                                              'pid': None,
                                                              'process_start': None,
                                                              'worktree': None}},
              'a dispatch without a session': {},
              'a dispatch, too old': {},
              'two dispatch records of one session: the newest': {'session-s': {'id': 'session-s',
                                                                                'transcript': '/p/s.jsonl',
                                                                                'kind': 'session',
                                                                                'label': 'session s',
                                                                                'pid': 2,
                                                                                'process_start': None,
                                                                                'worktree': None}},
              'a leased holder': {'session-x': {'id': 'session-x',
                                                'transcript': '/p/l.jsonl',
                                                'kind': 'lease',
                                                'label': 'H'}},
              'a fixer wins over its session': {'fixer-101-1': {'id': 'fixer-101-1',
                                                                'transcript': '/p/a.jsonl',
                                                                'kind': 'fixer',
                                                                'label': 'red fixer #101',
                                                                'pid': None,
                                                                'process_start': None,
                                                                'started': None,
                                                                'ended': None,
                                                                'rc': None,
                                                                'verdict': None,
                                                                'worktree': None,
                                                                'run': 101,
                                                                'record': '/R/101/fixer.json'}},
              'a subagent': {'task-agent-1': {'id': 'task-agent-1',
                                              'transcript': '/G/agent-1.output',
                                              'kind': 'subagent',
                                              'label': 'subagent agent-1',
                                              'agent_type': None,
                                              'finished': False}},
              'a subagent with a meta': {'task-a': {'id': 'task-a',
                                                    'transcript': '/G/a.jsonl',
                                                    'kind': 'subagent',
                                                    'label': 'DDDDDDDDDDDDDDDDDDDDDDDDDDDDDDDDDDDDDDDDDDDDDDDDDDDDDDDDDDDDDDDDDDDDDDDDDDDDDDDDDDDDDDDDDDDDDDDDDDDDDDDDDDDDDDDDDDDDDDDD',
                                                    'agent_type': 'T',
                                                    'finished': True}},
              'a subagent of another user': {},
              'a subagent too old': {},
              'a subagent that cannot be read': {},
              'two patterns': {'task-a': {'id': 'task-a',
                                          'transcript': '/G/a.jsonl',
                                          'kind': 'subagent',
                                          'label': 'subagent a',
                                          'agent_type': None,
                                          'finished': False},
                               'task-b': {'id': 'task-b',
                                          'transcript': '/H/b.jsonl',
                                          'kind': 'subagent',
                                          'label': 'subagent b',
                                          'agent_type': None,
                                          'finished': False}}},
 'state': {'ended ok': ('done', 'V'),
           'ended failed': ('failed', 'ended, rc 2'),
           'ended with a verdict': ('failed', 'no'),
           'api error': ('failed', 'the last answer was an API error'),
           'process gone after an answer': ('done', 'the process ended after its answer'),
           'process gone mid-turn': ('failed', 'the process ended mid-turn'),
           'alive after an answer': ('waiting on background',
                                     'its turn ended; it waits for background work or a message'),
           'alive mid-turn': ('working', 'its turn is running'),
           'pid unknown, finished': ('done', 'its final answer is given'),
           'no pid, answered': ('done', 'its final answer is given'),
           'no pid, quiet long': ('failed', 'no output for 60 min, mid-turn'),
           'no pid, quiet shortly': ('working', 'its turn is running'),
           'no pid, no mtime': ('working', 'its turn is running'),
           'alive 0': ('working', 'its turn is running')},
 'annotate': {'no agents, nothing known': ([], {}),
              'an agent by its transcript': ([{'transcript': '/p/a.jsonl',
                                               'live': 'session-a',
                                               'live_state': 'done',
                                               'live_why': 'the process ended after its answer'}],
                                             {}),
              'an agent whose transcript is unknown': ([{'transcript': '/p/z.jsonl'},
                                                        {'pid': 1,
                                                         'holder': None,
                                                         'kind': 'session',
                                                         'label': 'A',
                                                         'worktree': None,
                                                         'leases': [],
                                                         'live': 'session-a',
                                                         'live_state': 'working',
                                                         'live_why': 'its turn is running',
                                                         'alive': True,
                                                         'last_progress': 1000000.0,
                                                         'transcript': '/p/a.jsonl'}],
                                                       {}),
              'an agent not allowed': ([{'transcript': '/p/a.jsonl'},
                                        {'pid': 1,
                                         'holder': None,
                                         'kind': 'session',
                                         'label': 'A',
                                         'worktree': None,
                                         'leases': [],
                                         'live': 'session-a',
                                         'live_state': 'working',
                                         'live_why': 'its turn is running',
                                         'alive': True,
                                         'last_progress': 1000000.0,
                                         'transcript': '/p/a.jsonl'}],
                                       {}),
              'an ended agent': ([{'transcript': '/p/a.jsonl',
                                   'ended': True,
                                   'live': 'session-a',
                                   'live_state': 'failed',
                                   'live_why': 'ended, rc None'}],
                                 {}),
              'a lease holder is learned': ([{'transcript': '/p/a.jsonl',
                                              'leases': ['l'],
                                              'label': 'Lab',
                                              'holder': 'h',
                                              'pid': 9}],
                                            {'session-a': {'transcript': '/p/a.jsonl',
                                                           'kind': 'lease holder',
                                                           'label': 'Lab',
                                                           'pid': 9}}),
              'a lease holder with a kind': ([{'transcript': '/p/a.jsonl',
                                               'leases': ['l'],
                                               'kind': 'K',
                                               'holder': 'h'}],
                                             {'session-a': {'transcript': '/p/a.jsonl',
                                                            'kind': 'K',
                                                            'label': 'h',
                                                            'pid': None}}),
              'an extra recent row': ([{'pid': 1,
                                        'holder': None,
                                        'kind': 'session',
                                        'label': 'A',
                                        'worktree': None,
                                        'leases': [],
                                        'live': 'session-a',
                                        'live_state': 'waiting on background',
                                        'live_why': 'its turn ended; it waits for background work or a message',
                                        'alive': True,
                                        'last_progress': 999940.0,
                                        'transcript': '/p/a.jsonl'}],
                                      {}),
              'an extra row, too old': ([], {}),
              'an extra fixer is no row': ([], {}),
              'a shown agent is no extra row': ([{'transcript': '/p/a.jsonl',
                                                  'live': 'session-a',
                                                  'live_state': 'working',
                                                  'live_why': 'its turn is running'}],
                                                {}),
              'extra rows newest first, capped': ([{'pid': None,
                                                    'holder': None,
                                                    'kind': 'session',
                                                    'label': None,
                                                    'worktree': None,
                                                    'leases': [],
                                                    'live': 'session-0',
                                                    'live_state': 'working',
                                                    'live_why': 'its turn is running',
                                                    'alive': True,
                                                    'last_progress': 999990.0,
                                                    'transcript': '/p/0.jsonl'},
                                                   {'pid': None,
                                                    'holder': None,
                                                    'kind': 'session',
                                                    'label': None,
                                                    'worktree': None,
                                                    'leases': [],
                                                    'live': 'session-1',
                                                    'live_state': 'working',
                                                    'live_why': 'its turn is running',
                                                    'alive': True,
                                                    'last_progress': 999989.0,
                                                    'transcript': '/p/1.jsonl'},
                                                   {'pid': None,
                                                    'holder': None,
                                                    'kind': 'session',
                                                    'label': None,
                                                    'worktree': None,
                                                    'leases': [],
                                                    'live': 'session-2',
                                                    'live_state': 'working',
                                                    'live_why': 'its turn is running',
                                                    'alive': True,
                                                    'last_progress': 999988.0,
                                                    'transcript': '/p/2.jsonl'},
                                                   {'pid': None,
                                                    'holder': None,
                                                    'kind': 'session',
                                                    'label': None,
                                                    'worktree': None,
                                                    'leases': [],
                                                    'live': 'session-3',
                                                    'live_state': 'working',
                                                    'live_why': 'its turn is running',
                                                    'alive': True,
                                                    'last_progress': 999987.0,
                                                    'transcript': '/p/3.jsonl'},
                                                   {'pid': None,
                                                    'holder': None,
                                                    'kind': 'session',
                                                    'label': None,
                                                    'worktree': None,
                                                    'leases': [],
                                                    'live': 'session-4',
                                                    'live_state': 'working',
                                                    'live_why': 'its turn is running',
                                                    'alive': True,
                                                    'last_progress': 999986.0,
                                                    'transcript': '/p/4.jsonl'},
                                                   {'pid': None,
                                                    'holder': None,
                                                    'kind': 'session',
                                                    'label': None,
                                                    'worktree': None,
                                                    'leases': [],
                                                    'live': 'session-5',
                                                    'live_state': 'working',
                                                    'live_why': 'its turn is running',
                                                    'alive': True,
                                                    'last_progress': 999985.0,
                                                    'transcript': '/p/5.jsonl'},
                                                   {'pid': None,
                                                    'holder': None,
                                                    'kind': 'session',
                                                    'label': None,
                                                    'worktree': None,
                                                    'leases': [],
                                                    'live': 'session-6',
                                                    'live_state': 'working',
                                                    'live_why': 'its turn is running',
                                                    'alive': True,
                                                    'last_progress': 999984.0,
                                                    'transcript': '/p/6.jsonl'},
                                                   {'pid': None,
                                                    'holder': None,
                                                    'kind': 'session',
                                                    'label': None,
                                                    'worktree': None,
                                                    'leases': [],
                                                    'live': 'session-7',
                                                    'live_state': 'working',
                                                    'live_why': 'its turn is running',
                                                    'alive': True,
                                                    'last_progress': 999983.0,
                                                    'transcript': '/p/7.jsonl'},
                                                   {'pid': None,
                                                    'holder': None,
                                                    'kind': 'session',
                                                    'label': None,
                                                    'worktree': None,
                                                    'leases': [],
                                                    'live': 'session-8',
                                                    'live_state': 'working',
                                                    'live_why': 'its turn is running',
                                                    'alive': True,
                                                    'last_progress': 999982.0,
                                                    'transcript': '/p/8.jsonl'},
                                                   {'pid': None,
                                                    'holder': None,
                                                    'kind': 'session',
                                                    'label': None,
                                                    'worktree': None,
                                                    'leases': [],
                                                    'live': 'session-9',
                                                    'live_state': 'working',
                                                    'live_why': 'its turn is running',
                                                    'alive': True,
                                                    'last_progress': 999981.0,
                                                    'transcript': '/p/9.jsonl'},
                                                   {'pid': None,
                                                    'holder': None,
                                                    'kind': 'session',
                                                    'label': None,
                                                    'worktree': None,
                                                    'leases': [],
                                                    'live': 'session-10',
                                                    'live_state': 'working',
                                                    'live_why': 'its turn is running',
                                                    'alive': True,
                                                    'last_progress': 999980.0,
                                                    'transcript': '/p/10.jsonl'},
                                                   {'pid': None,
                                                    'holder': None,
                                                    'kind': 'session',
                                                    'label': None,
                                                    'worktree': None,
                                                    'leases': [],
                                                    'live': 'session-11',
                                                    'live_state': 'working',
                                                    'live_why': 'its turn is running',
                                                    'alive': True,
                                                    'last_progress': 999979.0,
                                                    'transcript': '/p/11.jsonl'}],
                                                  {})}}
