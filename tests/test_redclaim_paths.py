"""THE INTERACTIVE RED CLAIM AND RELEASE (redclaim.red_claim, red_release), characterised with the lease store, the fixer's
record, the worktree and the red lookups faked: a claim noted on a fixer's own lease, a claim of another red from a fixer,
the refusals (not a worktree, no red), a fresh, renewed and held lease, and a release of a live lease, someone else's, an
owner block, or none. `run_claim` / `run_release` take the module so a copy of the code can be driven the same way."""
import contextlib
import copy
import importlib
import os
import sys
import tempfile
import time
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from test_agentctl import A  # noqa: E402,F401 (puts lib on the path)
from harness import case_context  # noqa: E402


def _harness(module, script, log, tmp, stack):
    def shown(x):
        return str(x).replace(tmp, "TMP")

    def rec(name, ret=None):
        def fake(*a, **k):
            k.pop("ctx", None)  # the Context is no call data (tests/golden_cases.py)
            log.append(shown(f"{name} {a} {sorted(k.items())}"))
            return ret(*a, **k) if callable(ret) else ret
        return fake

    rows = copy.deepcopy(script.get("rows", {}))
    for row in rows.values():
        if row.get("holder") == "HOLD":
            row["holder"] = f"laptop:claim:me@{tmp}/wt"

    class Store:
        def __repr__(self):
            return "ST"

        def clock(self):
            return 500.0

        def get(self, name):
            log.append(f"get {name}")
            return rows.get(name)

        renew = rec("renew", lambda *a, **k: script.get("renewed", "ok"))
        acquire = rec("acquire", lambda *a, **k: script.get("acquired", {}))
        close_offer = rec("close_offer")
        set_meta = rec("set_meta")
        revoke = rec("revoke")

    patches = {
        "own_fixer_record_path": lambda: script.get("own"), "safe_cwd": lambda: os.path.join(tmp, "here"),
        "read_record": lambda p: copy.deepcopy(script.get("rec")), "write_json": rec("write_json"),
        "red_targets": rec("red_targets", lambda *a, **k: copy.deepcopy(script.get("items", []))),
        "lease_is_live": lambda row, now: bool(row and row.get("live")), "red_lease": lambda key: f"lease-{key}",
        "red_what": lambda d: f"what-{d['key']}", "red_block_of": lambda it, st: script.get("blocks", {}).get(it["data"]["key"]),
        "lease_meta": lambda row: (row or {}).get("meta", {}), "worktree_of": lambda p: os.path.join(tmp, "wt") if script.get("worktree_of") else None,
        "claimant": lambda here=None: script.get("who", "me"), "root_checkout": lambda wt, o_r: script.get("root", False),
        "red_wt_mark": lambda wt: "MARK", "red_noted": rec("red_noted"),
    }
    for name, fake in patches.items():
        stack.enter_context(mock.patch.object(module, name, fake))
    stack.enter_context(mock.patch.object(importlib.import_module("agentctl.paths"), "HOME", tmp))  # paths.tilde's home
    stack.enter_context(mock.patch.dict(os.environ, {"CLAUDE_CODE_SESSION_ID": "SESSION", "TZ": "UTC"}))
    time.tzset()
    clock = type("Clock", (), {"time": staticmethod(lambda: 1000.0), "strftime": staticmethod(time.strftime),
                               "localtime": staticmethod(time.localtime)})
    stack.enter_context(mock.patch.object(module, "time", clock))
    stack.enter_context(mock.patch.object(module, "epoch_now", clock.time, create=True))
    return Store(), shown


def run_claim(module, script):
    """red_claim(store, "R", target, note, wt) with the script's answers: ((rc, lines), the calls it made)."""
    log = []
    with tempfile.TemporaryDirectory() as tmp0, contextlib.ExitStack() as stack:
        tmp = os.path.realpath(tmp0)
        os.makedirs(os.path.join(tmp, "here"))
        os.makedirs(os.path.join(tmp, "wt"))
        store, shown = _harness(module, script, log, tmp, stack)
        wt = {"tmp": os.path.join(tmp, "wt"), "gone": os.path.join(tmp, "gone"), None: None}[script.get("wt")]
        got = module.red_claim(store, "R", script.get("target", "#7"), script.get("note", ""), wt, ctx=case_context())
        return shown(got), log


def run_release(module, script):
    log = []
    with tempfile.TemporaryDirectory() as tmp0, contextlib.ExitStack() as stack:
        tmp = os.path.realpath(tmp0)
        store, shown = _harness(module, script, log, tmp, stack)
        return shown(module.red_release(store, "R", script.get("target", "7"), script.get("any", False), ctx=case_context())), log


def item(key, ident=None, gate=False):
    return {"id": ident or key, "data": {"key": key, "gate": gate}}


LIVE_ME = {"live": True, "holder": "me-holder", "name": "n", "epoch": 3, "secret": "s", "meta": {"who": "me"}}
HOLD = "HOLD"
FIXER = dict(own="/own.json", rec={"run": 7, "lease": "reds/7", "worktree": "WTREC", "note": "was"})
CLAIMS = {
    "outside a fixer, claimed": dict(items=[item("k1")], wt="tmp"),
    "outside a fixer, with a note and a gate red": dict(items=[item("k1", gate=True)], wt="tmp", note="n1"),
    "outside a fixer, two reds": dict(items=[item("k1"), item("k2")], wt="tmp"),
    "outside a fixer, worktree by the cwd": dict(items=[item("k1")], worktree_of=True),
    "outside a fixer, root checkout": dict(items=[item("k1")], wt="tmp", root=True),
    "outside a fixer, no such directory": dict(items=[item("k1")], wt="gone"),
    "outside a fixer, no red": dict(items=[], wt="tmp"),
    "outside a fixer, renewed": dict(items=[item("k1")], wt="tmp", rows={"lease-k1": {**LIVE_ME, "holder": "HOLD"}}),
    "renewed with a note": dict(items=[item("k1")], wt="tmp", note="x", rows={"lease-k1": {**LIVE_ME, "holder": "HOLD"}}),
    "live under another holder, held": dict(items=[item("k1")], wt="tmp", rows={"lease-k1": LIVE_ME},
                                            acquired={"held": {"holder": "other", "expires_at": 0}}),
    "one held, one claimed": dict(items=[item("k1"), item("k2")], wt="tmp", acquired={"held": {"holder": "o", "expires_at": 0}}),
    "fixer, its own red by run": dict(**FIXER, target="#7", note="N"),
    "fixer, its own red by key, no note": dict(**FIXER, target="7"),
    "fixer, its own red, no note anywhere": dict(own="/own.json", rec={"run": 7, "lease": "reds/7"}, target="7"),
    "fixer, no record": dict(own="/own.json", rec=None, target="7", items=[], wt="tmp"),
    "fixer, names no red": dict(**FIXER, target="9", items=[], note="n"),
    "fixer, reds all its own": dict(**FIXER, target="9", items=[item("k1"), item("k2")],
                                    rows={"lease-k1": {**LIVE_ME, "holder": "H"}, "lease-k2": {**LIVE_ME, "name": "reds/7", "holder": "G"}},
                                    primary={"live": True, "holder": "H"}),
    "fixer, another red claimed": dict(**FIXER, target="9", items=[item("k1")], wt="tmp", note="more"),
    "fixer, one own and one other": dict(**FIXER, target="9", items=[item("k1"), item("k2")], wt="tmp",
                                         rows={"lease-k1": {**LIVE_ME, "holder": "H"}}, primary={"live": True, "holder": "H"}),
}
CLAIMS["fixer, reds all its own"]["rows"]["reds/7"] = {"live": True, "holder": "H", "name": "reds/7"}
CLAIMS["fixer, one own and one other"]["rows"]["reds/7"] = {"live": True, "holder": "H", "name": "reds/7"}
CLAIMS["fixer, another red claimed"]["rows"] = {"reds/7": {"live": False, "holder": "H", "name": "reds/7"}}
CLAIMS["fixer, another red claimed"]["rec"] = {"run": 7, "lease": "reds/7", "worktree": os.path.join("/", "nowhere")}

RELEASES = {
    "released": dict(items=[item("k1")], rows={"lease-k1": {**LIVE_ME, "holder": "H"}}),
    "no live lease": dict(items=[item("k1")], rows={"lease-k1": {"live": False}}),
    "no red": dict(items=[]),
    "held by another": dict(items=[item("k1")], rows={"lease-k1": {**LIVE_ME, "holder": "H", "meta": {"who": "you"}}}),
    "any releases another's": dict(items=[item("k1")], any=True, rows={"lease-k1": {**LIVE_ME, "holder": "H", "meta": {"who": "you"}}}),
    "two, one mine": dict(items=[item("k1"), item("k2")], rows={"lease-k1": {**LIVE_ME, "holder": "H"},
                                                                "lease-k2": {**LIVE_ME, "holder": "G", "meta": {"who": "you"}}}),
    "block of the harness, lifted": dict(items=[item("k1")], any=True, rows={"lease-k1": {"live": False}, "blk": {"epoch": 1, "secret": "z"}},
                                         blocks={"k1": {"name": "blk", "cause": "crash loop", "meta": {"crash_loop": True, "keys": ["k1"]}}}),
    "block of a decision shared with others": dict(items=[item("k1")], any=True, rows={"lease-k1": {"live": False}, "blk": {"epoch": 1, "secret": "z"}},
                                                   blocks={"k1": {"name": "blk", "cause": "owner", "meta": {"decision": "d", "keys": ["k1", "k2"]}}}),
    "block without a reason is no lift": dict(items=[item("k1")], any=True, rows={"lease-k1": {"live": False}},
                                              blocks={"k1": {"name": "blk", "cause": "x", "meta": {}}}),
    "block only read with --any": dict(items=[item("k1")], rows={"lease-k1": {"live": False}},
                                       blocks={"k1": {"name": "blk", "cause": "x", "meta": {"crash_loop": True}}}),
    "a live lease is no block lift": dict(items=[item("k1")], any=True, rows={"lease-k1": {**LIVE_ME, "holder": "H"}},
                                          blocks={"k1": {"name": "blk", "cause": "x", "meta": {"crash_loop": True}}}),
    "lift then release": dict(items=[item("k1"), item("k2")], any=True, rows={"lease-k1": {"live": False}, "blk": {"epoch": 1, "secret": "z"},
                                                                              "lease-k2": {**LIVE_ME, "holder": "H"}},
                              blocks={"k1": {"name": "blk", "cause": "c", "meta": {"decision": "d"}}}),
}


def gen_claims(rng, n):
    out = []
    for _ in range(n):
        keys = [f"k{i}" for i in range(rng.randint(0, 3))]
        rows = {}
        for k in keys:
            if rng.random() < .6:
                rows[f"lease-{k}"] = {**LIVE_ME, "holder": rng.choice(["H", "HOLD", "G"]), "live": rng.random() < .8,
                                      "name": rng.choice(["n", "reds/7"])}
        rows["reds/7"] = {"live": rng.random() < .7, "holder": "H", "name": "reds/7"}
        s = dict(items=[item(k, gate=rng.random() < .3) for k in keys], rows=rows, wt=rng.choice(["tmp", "tmp", "gone", None]),
                 note=rng.choice(["", "n"]), target=rng.choice(["#7", "7", "9", "run-7"]), root=rng.random() < .1,
                 acquired=rng.choice([{}, {}, {"held": {"holder": "o", "expires_at": 0}}]), who=rng.choice(["me", "you"]),
                 worktree_of=rng.choice([None, True]), primary={"live": True, "holder": "H"})
        if rng.random() < .5:
            s.update(own="/own.json", rec=rng.choice([None, {"run": 7, "lease": "reds/7"}, {"run": 3, "lease": "reds/7", "worktree": "/nope"},
                                                      {"run": 3, "note": "old"}]))
        out.append(s)
    return out


def gen_releases(rng, n):
    out = []
    for _ in range(n):
        keys = [f"k{i}" for i in range(rng.randint(0, 3))]
        rows, blocks = {"blk": {"epoch": 1, "secret": "z"}}, {}
        for k in keys:
            rows[f"lease-{k}"] = {**LIVE_ME, "holder": "H", "live": rng.random() < .6, "meta": {"who": rng.choice(["me", "you"])}}
            if rng.random() < .5:
                blocks[k] = {"name": "blk", "cause": "c", "meta": rng.choice([{}, {"crash_loop": 1}, {"decision": "d", "keys": ["k0", "k1", "k2"]},
                                                                           {"decision": "d", "keys": [k]}])}
        out.append(dict(items=[item(k) for k in keys], rows=rows, blocks=blocks, any=rng.random() < .5, who=rng.choice(["me", "you"])))
    return out


class RedClaimTest(unittest.TestCase):
    def test_every_claim_and_release_does_what_it_did(self):
        module = __import__("agentctl.redclaim", fromlist=["x"])
        for name, script in CLAIMS.items():
            with self.subTest(name):
                self.assertEqual(run_claim(module, script), EXPECTED["claim"][name], name)
        for name, script in RELEASES.items():
            with self.subTest(name):
                self.assertEqual(run_release(module, script), EXPECTED["release"][name], name)


def results():
    module = __import__("agentctl.redclaim", fromlist=["x"])
    return {"claim": {n: run_claim(module, s) for n, s in CLAIMS.items()},
            "release": {n: run_release(module, s) for n, s in RELEASES.items()}}


EXPECTED = {'claim': {'outside a fixer, claimed': ("(0, ['claimed what-k1 for me (~/wt) as lease-k1; it lapses after 30 min "
                                        'without activity in your worktree; release: ~/bin/agentctl red release '
                                        "k1'])",
                                        ["red_targets (ST, 'R', '#7') []",
                                         'get lease-k1',
                                         "acquire (ST, 'lease-k1', 'laptop:claim:me@TMP/wt', 1800, 28800) [('item', "
                                         "'red:k1'), ('meta', {'kind': 'claim', 'worktree': 'TMP/wt', 'who': 'me', "
                                         "'note': '', 'session': 'SESSION', 'gate': False}), ('progress', 'MARK')]",
                                         "close_offer (ST, 'red:k1', 'taken') []"]),
           'outside a fixer, with a note and a gate red': ("(0, ['claimed what-k1 for me (~/wt) as lease-k1: n1; it "
                                                           'lapses after 30 min without activity in your worktree; '
                                                           "release: ~/bin/agentctl red release k1'])",
                                                           ["red_targets (ST, 'R', '#7') []",
                                                            'get lease-k1',
                                                            "acquire (ST, 'lease-k1', 'laptop:claim:me@TMP/wt', "
                                                            "1800, 28800) [('item', 'red:k1'), ('meta', {'kind': "
                                                            "'claim', 'worktree': 'TMP/wt', 'who': 'me', 'note': "
                                                            "'n1', 'session': 'SESSION', 'gate': True}), "
                                                            "('progress', 'MARK')]",
                                                            "close_offer (ST, 'red:k1', 'taken') []"]),
           'outside a fixer, two reds': ("(0, ['claimed what-k1 for me (~/wt) as lease-k1; it lapses after 30 min "
                                         'without activity in your worktree; release: ~/bin/agentctl red release '
                                         "k1', 'claimed what-k2 for me (~/wt) as lease-k2; it lapses after 30 min "
                                         'without activity in your worktree; release: ~/bin/agentctl red release '
                                         "k2'])",
                                         ["red_targets (ST, 'R', '#7') []",
                                          'get lease-k1',
                                          "acquire (ST, 'lease-k1', 'laptop:claim:me@TMP/wt', 1800, 28800) [('item', "
                                          "'red:k1'), ('meta', {'kind': 'claim', 'worktree': 'TMP/wt', 'who': 'me', "
                                          "'note': '', 'session': 'SESSION', 'gate': False}), ('progress', 'MARK')]",
                                          "close_offer (ST, 'red:k1', 'taken') []",
                                          'get lease-k2',
                                          "acquire (ST, 'lease-k2', 'laptop:claim:me@TMP/wt', 1800, 28800) [('item', "
                                          "'red:k2'), ('meta', {'kind': 'claim', 'worktree': 'TMP/wt', 'who': 'me', "
                                          "'note': '', 'session': 'SESSION', 'gate': False}), ('progress', 'MARK')]",
                                          "close_offer (ST, 'red:k2', 'taken') []"]),
           'outside a fixer, worktree by the cwd': ("(0, ['claimed what-k1 for me (~/wt) as lease-k1; it lapses "
                                                    'after 30 min without activity in your worktree; release: '
                                                    "~/bin/agentctl red release k1'])",
                                                    ["red_targets (ST, 'R', '#7') []",
                                                     'get lease-k1',
                                                     "acquire (ST, 'lease-k1', 'laptop:claim:me@TMP/wt', 1800, "
                                                     "28800) [('item', 'red:k1'), ('meta', {'kind': 'claim', "
                                                     "'worktree': 'TMP/wt', 'who': 'me', 'note': '', 'session': "
                                                     "'SESSION', 'gate': False}), ('progress', 'MARK')]",
                                                     "close_offer (ST, 'red:k1', 'taken') []"]),
           'outside a fixer, root checkout': ('(2, ["NOT CLAIMED: ~/wt is not a worktree of your own (a claim is '
                                              "renewed by its worktree's activity; the root checkout counts for "
                                              'nobody): `--worktree <your worktree>`"])',
                                              []),
           'outside a fixer, no such directory': ('(2, ["NOT CLAIMED: ~/gone is not a worktree of your own (a claim '
                                                  "is renewed by its worktree's activity; the root checkout counts "
                                                  'for nobody): `--worktree <your worktree>`"])',
                                                  []),
           'outside a fixer, no red': ("(1, ['NOT CLAIMED: ##7 names no red on main'])",
                                       ["red_targets (ST, 'R', '#7') []"]),
           'outside a fixer, renewed': ("(0, ['renewed what-k1 (lease-k1)'])",
                                        ["red_targets (ST, 'R', '#7') []",
                                         'get lease-k1',
                                         "renew (ST, 'lease-k1', 3, 's', 'beat 1000.0', 1800) [('note', None)]"]),
           'renewed with a note': ("(0, ['renewed what-k1 (lease-k1)'])",
                                   ["red_targets (ST, 'R', '#7') []",
                                    'get lease-k1',
                                    "renew (ST, 'lease-k1', 3, 's', 'beat 1000.0', 1800) [('note', 'x')]"]),
           'live under another holder, held': ("(1, ['NOT CLAIMED: what-k1 is held by other until 00:00'])",
                                               ["red_targets (ST, 'R', '#7') []",
                                                'get lease-k1',
                                                "acquire (ST, 'lease-k1', 'laptop:claim:me@TMP/wt', 1800, 28800) "
                                                "[('item', 'red:k1'), ('meta', {'kind': 'claim', 'worktree': "
                                                "'TMP/wt', 'who': 'me', 'note': '', 'session': 'SESSION', 'gate': "
                                                "False}), ('progress', 'MARK')]"]),
           'one held, one claimed': ("(1, ['NOT CLAIMED: what-k1 is held by o until 00:00', 'NOT CLAIMED: what-k2 is "
                                     "held by o until 00:00'])",
                                     ["red_targets (ST, 'R', '#7') []",
                                      'get lease-k1',
                                      "acquire (ST, 'lease-k1', 'laptop:claim:me@TMP/wt', 1800, 28800) [('item', "
                                      "'red:k1'), ('meta', {'kind': 'claim', 'worktree': 'TMP/wt', 'who': 'me', "
                                      "'note': '', 'session': 'SESSION', 'gate': False}), ('progress', 'MARK')]",
                                      'get lease-k2',
                                      "acquire (ST, 'lease-k2', 'laptop:claim:me@TMP/wt', 1800, 28800) [('item', "
                                      "'red:k2'), ('meta', {'kind': 'claim', 'worktree': 'TMP/wt', 'who': 'me', "
                                      "'note': '', 'session': 'SESSION', 'gate': False}), ('progress', 'MARK')]"]),
           'fixer, its own red by run': ("(0, ['noted on your lease reds/7: N'])",
                                         ["write_json ('/own.json', {'run': 7, 'lease': 'reds/7', 'worktree': "
                                          "'WTREC', 'note': 'N'}) []"]),
           'fixer, its own red by key, no note': ("(0, ['noted on your lease reds/7: was'])",
                                                  ["write_json ('/own.json', {'run': 7, 'lease': 'reds/7', "
                                                   "'worktree': 'WTREC', 'note': 'was'}) []"]),
           'fixer, its own red, no note anywhere': ("(0, ['noted on your lease reds/7: working'])",
                                                    ["write_json ('/own.json', {'run': 7, 'lease': 'reds/7', 'note': "
                                                     "'working'}) []"]),
           'fixer, no record': ("(0, ['noted on your lease None: working'])",
                                ["red_targets (ST, 'R', '7') []",
                                 "write_json ('/own.json', {'note': 'working'}) []",
                                 "red_noted (ST, [], 'working') []"]),
           'fixer, names no red': ("(0, ['noted on your lease reds/7: n'])",
                                   ["red_targets (ST, 'R', '9') []",
                                    'get reds/7',
                                    "write_json ('/own.json', {'run': 7, 'lease': 'reds/7', 'worktree': 'WTREC', "
                                    "'note': 'n'}) []",
                                    "red_noted (ST, [], 'n') []"]),
           'fixer, reds all its own': ("(0, ['noted on your lease reds/7: was'])",
                                       ["red_targets (ST, 'R', '9') []",
                                        'get reds/7',
                                        'get lease-k1',
                                        'get lease-k2',
                                        "write_json ('/own.json', {'run': 7, 'lease': 'reds/7', 'worktree': 'WTREC', "
                                        "'note': 'was'}) []",
                                        "red_noted (ST, [{'id': 'k1', 'data': {'key': 'k1', 'gate': False}}, {'id': "
                                        "'k2', 'data': {'key': 'k2', 'gate': False}}], 'was') []"]),
           'fixer, another red claimed': ("(0, ['claimed what-k1 for me (~/wt) as lease-k1: more; it lapses after 30 "
                                          'min without activity in your worktree; release: ~/bin/agentctl red '
                                          "release k1'])",
                                          ["red_targets (ST, 'R', '9') []",
                                           'get reds/7',
                                           'get lease-k1',
                                           'get lease-k1',
                                           'get lease-k1',
                                           "acquire (ST, 'lease-k1', 'laptop:claim:me@TMP/wt', 1800, 28800) "
                                           "[('item', 'red:k1'), ('meta', {'kind': 'claim', 'worktree': 'TMP/wt', "
                                           "'who': 'me', 'note': 'more', 'session': 'SESSION', 'gate': False, "
                                           "'fixer': '/own.json'}), ('progress', 'MARK')]",
                                           "close_offer (ST, 'red:k1', 'taken') []"]),
           'fixer, one own and one other': ("(0, ['claimed what-k2 for me (~/wt) as lease-k2; it lapses after 30 min "
                                            'without activity in your worktree; release: ~/bin/agentctl red release '
                                            "k2'])",
                                            ["red_targets (ST, 'R', '9') []",
                                             'get reds/7',
                                             'get lease-k1',
                                             'get lease-k2',
                                             'get lease-k1',
                                             'get lease-k2',
                                             'get lease-k2',
                                             "acquire (ST, 'lease-k2', 'laptop:claim:me@TMP/wt', 1800, 28800) "
                                             "[('item', 'red:k2'), ('meta', {'kind': 'claim', 'worktree': 'TMP/wt', "
                                             "'who': 'me', 'note': '', 'session': 'SESSION', 'gate': False, 'fixer': "
                                             "'/own.json'}), ('progress', 'MARK')]",
                                             "close_offer (ST, 'red:k2', 'taken') []"])},
 'release': {'released': ("(0, ['released what-k1 (H)'])",
                          ["red_targets (ST, 'R', '7') [('create', False)]",
                           'get lease-k1',
                           "revoke (ST, 'lease-k1', 'RELEASED by me') []"]),
             'no live lease': ("(1, ['red: no live lease on #7'])",
                               ["red_targets (ST, 'R', '7') [('create', False)]", 'get lease-k1']),
             'no red': ("(1, ['red: no live lease on #7'])", ["red_targets (ST, 'R', '7') [('create', False)]"]),
             'held by another': ('(1, ["what-k1 is held by H, not me (--any releases anyone\'s)"])',
                                 ["red_targets (ST, 'R', '7') [('create', False)]", 'get lease-k1']),
             "any releases another's": ("(0, ['released what-k1 (H)'])",
                                        ["red_targets (ST, 'R', '7') [('create', False)]",
                                         'get lease-k1',
                                         "revoke (ST, 'lease-k1', 'RELEASED by me') []"]),
             'two, one mine': ('(0, [\'released what-k1 (H)\', "what-k2 is held by G, not me (--any releases '
                               'anyone\'s)"])',
                               ["red_targets (ST, 'R', '7') [('create', False)]",
                                'get lease-k1',
                                "revoke (ST, 'lease-k1', 'RELEASED by me') []",
                                'get lease-k2']),
             'block of the harness, lifted': ("(0, ['lifted the owner block of what-k1 (crash loop): offered again "
                                              "at the next tick'])",
                                              ["red_targets (ST, 'R', '7') [('create', False)]",
                                               'get lease-k1',
                                               'get blk',
                                               "revoke (ST, 'blk', 'RELEASED by me: offered again') []"]),
             'block of a decision shared with others': ("(0, ['lifted the owner block of what-k1 (owner): offered "
                                                        "again at the next tick'])",
                                                        ["red_targets (ST, 'R', '7') [('create', False)]",
                                                         'get lease-k1',
                                                         'get blk',
                                                         "set_meta (ST, 'blk', 1, 'z', {'keys': ['k2']}) []"]),
             'block without a reason is no lift': ("(1, ['red: no live lease on #7'])",
                                                   ["red_targets (ST, 'R', '7') [('create', False)]",
                                                    'get lease-k1']),
             'block only read with --any': ("(1, ['red: no live lease on #7'])",
                                            ["red_targets (ST, 'R', '7') [('create', False)]", 'get lease-k1']),
             'a live lease is no block lift': ("(0, ['released what-k1 (H)'])",
                                               ["red_targets (ST, 'R', '7') [('create', False)]",
                                                'get lease-k1',
                                                "revoke (ST, 'lease-k1', 'RELEASED by me') []"]),
             'lift then release': ("(0, ['lifted the owner block of what-k1 (c): offered again at the next tick', "
                                   "'released what-k2 (H)'])",
                                   ["red_targets (ST, 'R', '7') [('create', False)]",
                                    'get lease-k1',
                                    'get blk',
                                    "revoke (ST, 'blk', 'RELEASED by me: offered again') []",
                                    'get lease-k2',
                                    "revoke (ST, 'lease-k2', 'RELEASED by me') []"])}}
