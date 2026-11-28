"""THE PROCESSOR'S LOOP (processor.waiting_run), characterised with the ownership lock, the token drop, the steps, the
bounded step threads, the stall condition and the snapshot faked: a loop that does not start, the setup of each role and
mode, what each round does after its steps (the beats, the round file, the stall note once the failures run on, the
snapshot) and when the loop ends (a stop, a drained --once). `run` takes the module so a copy of the code can be driven
the same way."""
import contextlib
import os
import sys
import threading
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from harness import WiredMachine  # noqa: E402
from test_agentctl import A  # noqa: E402,F401 (puts lib on the path)
from harness import case_context  # noqa: E402


class Clock:
    def time(self):
        return 100.0


def probe_by_kind(recorded):
    """run_probe's fake, recording the check by its kind (conditions.probe_kind): the calls as recorded before the opener
    ran the probe function it imports."""
    from agentctl.conditions import probe_kind
    return lambda check, args=None: recorded(probe_kind(check), args)


def run(module, script):
    """waiting_run("REPO", clock, "ST", once, service) with the script's answers: (its return, the calls it made)."""
    log = []
    naps = [0]
    rounds = iter(script.get("fails", []))

    def rec(name, ret=None):
        def fake(*a, **k):
            k.pop("ctx", None)  # the Context is no call data (tests/golden_cases.py), nor the machine a call reads
            a = tuple(x for x in a if not issubclass(type(x), (A.MachineProbe, WiredMachine)))
            log.append(f"{name} {a} {sorted(k.items())}")
            if isinstance(ret, BaseException):
                raise ret
            return ret(*a, **k) if callable(ret) else ret
        return fake

    class Own:
        def __repr__(self):
            return "OWN"

        def acquire(self):
            log.append("own acquire")
            return script.get("owned", True)

        def release(self):
            log.append("own release")

    class Drop:
        def __init__(self, d, o_r, clock):
            log.append(f"TokenDrop {d} {o_r}")

        def open(self):
            log.append("drop open")
            return self

        def close(self):
            log.append("drop close")

        def nap(self, secs):
            naps[0] += 1
            log.append(f"nap {secs}")
            if naps[0] >= script.get("naps", 3):
                module.TICK_STOP[0] = True

    @contextlib.contextmanager
    def shared_clone(repo):
        log.append(f"clone shared {repo}")
        yield

    def steps(*names):
        def make(*a, ctx=None):
            shared = a[2] if len(a) == 6 and callable(a[2]) else None
            if shared:
                log.append(f"shared step {shared(lambda: 'ran')()}")
            return [("alpha", "A"), ("beta", "B")]
        return make

    first = {"fail": False}

    def run_step(name, step, threads, errors):
        if name == "alpha":
            first["fail"] = next(rounds, False)
        log.append(f"run_step {name}")
        return False if first["fail"] and name == "alpha" else None

    alive = [type("T", (), {"is_alive": lambda self: script.get("busy", False)})()]
    patches = {
        "processor_owned": lambda d, repo: Own(), "waiting_state": lambda repo: ("D", "C", "W"),
        "slug": lambda repo: "SLUG" if not script.get("slug_fails") else (_ for _ in ()).throw(RuntimeError("x")),
        "TokenDrop": Drop, "host_role": lambda: script.get("role", "sync"), "keep_awake": rec("keep_awake"),
        "status_page_start": rec("status_page_start"), "sync_steps": steps(), "server_steps": steps(),
        "clone_shared": shared_clone, "write_beat": rec("write_beat"), "restart_if_changed": rec("restart_if_changed"),
        "run_step": run_step, "sd_notify": rec("sd_notify"), "ROUND_ERRORS_MAX": 2,
        "open_condition": rec("open_condition", script.get("stall")), "run_probe": probe_by_kind(rec("run_probe", "PROBE")),
        "write_json": rec("write_json"), "write_snapshot": rec("write_snapshot", script.get("snapshot")),
        "ChangeRecords": lambda st, d: type("R", (), {"open": lambda self: script.get("queued", []) + script.get("waiting", [])})(),
        "print_error": rec("print_error"), "knob_tick": lambda: 5,
    }
    with contextlib.ExitStack() as stack:
        for name, fake in patches.items():
            stack.enter_context(mock.patch.object(module, name, fake))
        stack.enter_context(mock.patch.dict(case_context().caches.bounded, {"step:x": alive[0], "other": alive[0]} if script.get("running") else {}))
        stack.enter_context(mock.patch.object(module, "TICK_STOP", [bool(script.get("stopped"))]))
        before = list(module._IN_PROCESSOR)
        try:
            got = module.waiting_run("REPO", Clock(), "ST", "REGISTRY", once=script.get("once", False), service=script.get("service", False), ctx=case_context())
        finally:
            flag = module._IN_PROCESSOR[0]
            module._IN_PROCESSOR[:] = before
        return got, flag, log


SCENARIOS = {
    "not the owner": dict(owned=False),
    "stopped before the first tick": dict(stopped=True),
    "sync, once, drained": dict(once=True),
    "sync, once, not even queued entries matter": dict(once=True, queued=["q"], waiting=["w"]),
    "server, once, drained": dict(role="server", once=True),
    "server, once, queued": dict(role="server", once=True, queued=["q"], naps=2),
    "server, once, waiting": dict(role="server", once=True, waiting=["w"], naps=2),
    "server, once, both": dict(role="server", once=True, queued=["q"], waiting=["w"], naps=1),
    "service on a sync host": dict(service=True, naps=1),
    "service on a server host": dict(service=True, role="server", naps=1),
    "slug fails": dict(slug_fails=True, naps=1),
    "a step thread at work": dict(running=True, naps=2),
    "three quiet rounds": dict(naps=3),
    "one failing round": dict(fails=[True, False], naps=2),
    "failures run on, stall noted once": dict(fails=[True, True, True, True], naps=4, role="server"),
    "stall note fails, noted again next round": dict(fails=[True, True, True], naps=3, stall=RuntimeError("down")),
    "stall note exits": dict(fails=[True, True], naps=2, stall=SystemExit("no token")),
    "failed, recovered, failed again": dict(fails=[True, True, False, True, True], naps=5),
    "snapshot fails": dict(role="server", snapshot=RuntimeError("disk"), naps=2),
}


def generate(rng, n):
    return [dict(role=rng.choice(["sync", "server", "other"]), once=rng.choice([False, True]),
                 service=rng.choice([False, True]), owned=rng.choice([True, True, True, False]),
                 stopped=rng.choice([False, False, True]), busy=rng.choice([False, True]), running=rng.choice([False, True]),
                 fails=[rng.choice([True, False]) for _ in range(6)], naps=rng.randint(1, 6),
                 stall=rng.choice([None, None, RuntimeError("s"), SystemExit("s")]),
                 snapshot=rng.choice([None, None, ValueError("v")]), slug_fails=rng.choice([False, False, True]),
                 queued=rng.choice([[], ["q"]]), waiting=rng.choice([[], ["w"]])) for _ in range(n)]


class ProcessorLoopTest(unittest.TestCase):
    def test_every_loop_does_what_it_did(self):
        module = __import__("agentctl.tickloop", fromlist=["x"])
        for name, script in SCENARIOS.items():
            with self.subTest(name):
                self.assertEqual(run(module, script), EXPECTED[name], name)


def results():
    module = __import__("agentctl.tickloop", fromlist=["x"])
    return {n: run(module, s) for n, s in SCENARIOS.items()}


EXPECTED = {'not the owner': (0, False, ['own acquire']),
 'stopped before the first tick': (0,
                                   True,
                                   ['own acquire', 'TokenDrop D SLUG', 'drop open', 'drop close', 'own release']),
 'sync, once, drained': (0,
                         True,
                         ['own acquire',
                          'TokenDrop D SLUG',
                          'drop open',
                          "write_beat ('processor',) []",
                          "restart_if_changed ([OWN], ['waiting', 'run', '--repo', 'REPO', '--once']) []",
                          'run_step alpha',
                          "write_beat ('processor',) []",
                          "sd_notify ('WATCHDOG=1',) []",
                          'run_step beta',
                          "write_beat ('processor',) []",
                          "sd_notify ('WATCHDOG=1',) []",
                          "write_json ('D/round-ok.json', {'at': 100.0}) []",
                          "sd_notify ('WATCHDOG=1',) []",
                          'drop close',
                          'own release']),
 'sync, once, not even queued entries matter': (0,
                                                True,
                                                ['own acquire',
                                                 'TokenDrop D SLUG',
                                                 'drop open',
                                                 "write_beat ('processor',) []",
                                                 "restart_if_changed ([OWN], ['waiting', 'run', '--repo', 'REPO', "
                                                 "'--once']) []",
                                                 'run_step alpha',
                                                 "write_beat ('processor',) []",
                                                 "sd_notify ('WATCHDOG=1',) []",
                                                 'run_step beta',
                                                 "write_beat ('processor',) []",
                                                 "sd_notify ('WATCHDOG=1',) []",
                                                 "write_json ('D/round-ok.json', {'at': 100.0}) []",
                                                 "sd_notify ('WATCHDOG=1',) []",
                                                 'drop close',
                                                 'own release']),
 'server, once, drained': (0,
                           True,
                           ['own acquire',
                            'TokenDrop D SLUG',
                            'drop open',
                            'clone shared REPO',
                            'shared step ran',
                            "write_beat ('processor',) []",
                            "restart_if_changed ([OWN], ['waiting', 'run', '--repo', 'REPO', '--once']) []",
                            'run_step alpha',
                            "write_beat ('processor',) []",
                            "sd_notify ('WATCHDOG=1',) []",
                            'run_step beta',
                            "write_beat ('processor',) []",
                            "sd_notify ('WATCHDOG=1',) []",
                            "write_json ('D/round-ok.json', {'at': 100.0}) []",
                            "write_snapshot ('D', 'ST') []",
                            "sd_notify ('WATCHDOG=1',) []",
                            'drop close',
                            'own release']),
 'server, once, queued': (0,
                          True,
                          ['own acquire',
                           'TokenDrop D SLUG',
                           'drop open',
                           'clone shared REPO',
                           'shared step ran',
                           "write_beat ('processor',) []",
                           "restart_if_changed ([OWN], ['waiting', 'run', '--repo', 'REPO', '--once']) []",
                           'run_step alpha',
                           "write_beat ('processor',) []",
                           "sd_notify ('WATCHDOG=1',) []",
                           'run_step beta',
                           "write_beat ('processor',) []",
                           "sd_notify ('WATCHDOG=1',) []",
                           "write_json ('D/round-ok.json', {'at': 100.0}) []",
                           "write_snapshot ('D', 'ST') []",
                           "sd_notify ('WATCHDOG=1',) []",
                           'nap 5',
                           "write_beat ('processor',) []",
                           "restart_if_changed ([OWN], ['waiting', 'run', '--repo', 'REPO', '--once']) []",
                           'run_step alpha',
                           "write_beat ('processor',) []",
                           "sd_notify ('WATCHDOG=1',) []",
                           'run_step beta',
                           "write_beat ('processor',) []",
                           "sd_notify ('WATCHDOG=1',) []",
                           "write_json ('D/round-ok.json', {'at': 100.0}) []",
                           "write_snapshot ('D', 'ST') []",
                           "sd_notify ('WATCHDOG=1',) []",
                           'nap 5',
                           'drop close',
                           'own release']),
 'server, once, waiting': (0,
                           True,
                           ['own acquire',
                            'TokenDrop D SLUG',
                            'drop open',
                            'clone shared REPO',
                            'shared step ran',
                            "write_beat ('processor',) []",
                            "restart_if_changed ([OWN], ['waiting', 'run', '--repo', 'REPO', '--once']) []",
                            'run_step alpha',
                            "write_beat ('processor',) []",
                            "sd_notify ('WATCHDOG=1',) []",
                            'run_step beta',
                            "write_beat ('processor',) []",
                            "sd_notify ('WATCHDOG=1',) []",
                            "write_json ('D/round-ok.json', {'at': 100.0}) []",
                            "write_snapshot ('D', 'ST') []",
                            "sd_notify ('WATCHDOG=1',) []",
                            'nap 5',
                            "write_beat ('processor',) []",
                            "restart_if_changed ([OWN], ['waiting', 'run', '--repo', 'REPO', '--once']) []",
                            'run_step alpha',
                            "write_beat ('processor',) []",
                            "sd_notify ('WATCHDOG=1',) []",
                            'run_step beta',
                            "write_beat ('processor',) []",
                            "sd_notify ('WATCHDOG=1',) []",
                            "write_json ('D/round-ok.json', {'at': 100.0}) []",
                            "write_snapshot ('D', 'ST') []",
                            "sd_notify ('WATCHDOG=1',) []",
                            'nap 5',
                            'drop close',
                            'own release']),
 'server, once, both': (0,
                        True,
                        ['own acquire',
                         'TokenDrop D SLUG',
                         'drop open',
                         'clone shared REPO',
                         'shared step ran',
                         "write_beat ('processor',) []",
                         "restart_if_changed ([OWN], ['waiting', 'run', '--repo', 'REPO', '--once']) []",
                         'run_step alpha',
                         "write_beat ('processor',) []",
                         "sd_notify ('WATCHDOG=1',) []",
                         'run_step beta',
                         "write_beat ('processor',) []",
                         "sd_notify ('WATCHDOG=1',) []",
                         "write_json ('D/round-ok.json', {'at': 100.0}) []",
                         "write_snapshot ('D', 'ST') []",
                         "sd_notify ('WATCHDOG=1',) []",
                         'nap 5',
                         'drop close',
                         'own release']),
 'service on a sync host': (0,
                            True,
                            ['own acquire',
                             'TokenDrop D SLUG',
                             'drop open',
                             'keep_awake () []',
                             'status_page_start ({},) []',  # the process's served page (Caches.status_page)
                             "write_beat ('processor',) []",
                             "restart_if_changed ([OWN], ['waiting', 'run', '--repo', 'REPO', '--service']) []",
                             'run_step alpha',
                             "write_beat ('processor',) []",
                             "sd_notify ('WATCHDOG=1',) []",
                             'run_step beta',
                             "write_beat ('processor',) []",
                             "sd_notify ('WATCHDOG=1',) []",
                             "write_json ('D/round-ok.json', {'at': 100.0}) []",
                             "sd_notify ('WATCHDOG=1',) []",
                             'nap 5',
                             'drop close',
                             'own release']),
 'service on a server host': (0,
                              True,
                              ['own acquire',
                               'TokenDrop D SLUG',
                               'drop open',
                               'keep_awake () []',
                               'clone shared REPO',
                               'shared step ran',
                               "write_beat ('processor',) []",
                               "restart_if_changed ([OWN], ['waiting', 'run', '--repo', 'REPO', '--service']) []",
                               'run_step alpha',
                               "write_beat ('processor',) []",
                               "sd_notify ('WATCHDOG=1',) []",
                               'run_step beta',
                               "write_beat ('processor',) []",
                               "sd_notify ('WATCHDOG=1',) []",
                               "write_json ('D/round-ok.json', {'at': 100.0}) []",
                               "write_snapshot ('D', 'ST') []",
                               "sd_notify ('WATCHDOG=1',) []",
                               'nap 5',
                               'drop close',
                               'own release']),
 'slug fails': (0,
                True,
                ['own acquire',
                 'TokenDrop D pawelkrupinski/movies',
                 'drop open',
                 "write_beat ('processor',) []",
                 "restart_if_changed ([OWN], ['waiting', 'run', '--repo', 'REPO']) []",
                 'run_step alpha',
                 "write_beat ('processor',) []",
                 "sd_notify ('WATCHDOG=1',) []",
                 'run_step beta',
                 "write_beat ('processor',) []",
                 "sd_notify ('WATCHDOG=1',) []",
                 "write_json ('D/round-ok.json', {'at': 100.0}) []",
                 "sd_notify ('WATCHDOG=1',) []",
                 'nap 5',
                 'drop close',
                 'own release']),
 'a step thread at work': (0,
                           True,
                           ['own acquire',
                            'TokenDrop D SLUG',
                            'drop open',
                            "write_beat ('processor',) []",
                            "restart_if_changed ([OWN], ['waiting', 'run', '--repo', 'REPO']) []",
                            'run_step alpha',
                            "write_beat ('processor',) []",
                            "sd_notify ('WATCHDOG=1',) []",
                            'run_step beta',
                            "write_beat ('processor',) []",
                            "sd_notify ('WATCHDOG=1',) []",
                            "write_json ('D/round-ok.json', {'at': 100.0}) []",
                            "sd_notify ('WATCHDOG=1',) []",
                            'nap 5',
                            "write_beat ('processor',) []",
                            "restart_if_changed ([OWN], ['waiting', 'run', '--repo', 'REPO']) []",
                            'run_step alpha',
                            "write_beat ('processor',) []",
                            "sd_notify ('WATCHDOG=1',) []",
                            'run_step beta',
                            "write_beat ('processor',) []",
                            "sd_notify ('WATCHDOG=1',) []",
                            "write_json ('D/round-ok.json', {'at': 100.0}) []",
                            "sd_notify ('WATCHDOG=1',) []",
                            'nap 5',
                            'drop close',
                            'own release']),
 'three quiet rounds': (0,
                        True,
                        ['own acquire',
                         'TokenDrop D SLUG',
                         'drop open',
                         "write_beat ('processor',) []",
                         "restart_if_changed ([OWN], ['waiting', 'run', '--repo', 'REPO']) []",
                         'run_step alpha',
                         "write_beat ('processor',) []",
                         "sd_notify ('WATCHDOG=1',) []",
                         'run_step beta',
                         "write_beat ('processor',) []",
                         "sd_notify ('WATCHDOG=1',) []",
                         "write_json ('D/round-ok.json', {'at': 100.0}) []",
                         "sd_notify ('WATCHDOG=1',) []",
                         'nap 5',
                         "write_beat ('processor',) []",
                         "restart_if_changed ([OWN], ['waiting', 'run', '--repo', 'REPO']) []",
                         'run_step alpha',
                         "write_beat ('processor',) []",
                         "sd_notify ('WATCHDOG=1',) []",
                         'run_step beta',
                         "write_beat ('processor',) []",
                         "sd_notify ('WATCHDOG=1',) []",
                         "write_json ('D/round-ok.json', {'at': 100.0}) []",
                         "sd_notify ('WATCHDOG=1',) []",
                         'nap 5',
                         "write_beat ('processor',) []",
                         "restart_if_changed ([OWN], ['waiting', 'run', '--repo', 'REPO']) []",
                         'run_step alpha',
                         "write_beat ('processor',) []",
                         "sd_notify ('WATCHDOG=1',) []",
                         'run_step beta',
                         "write_beat ('processor',) []",
                         "sd_notify ('WATCHDOG=1',) []",
                         "write_json ('D/round-ok.json', {'at': 100.0}) []",
                         "sd_notify ('WATCHDOG=1',) []",
                         'nap 5',
                         'drop close',
                         'own release']),
 'one failing round': (0,
                       True,
                       ['own acquire',
                        'TokenDrop D SLUG',
                        'drop open',
                        "write_beat ('processor',) []",
                        "restart_if_changed ([OWN], ['waiting', 'run', '--repo', 'REPO']) []",
                        'run_step alpha',
                        "write_beat ('processor',) []",
                        "sd_notify ('WATCHDOG=1',) []",
                        'run_step beta',
                        "write_beat ('processor',) []",
                        "sd_notify ('WATCHDOG=1',) []",
                        "sd_notify ('WATCHDOG=1',) []",
                        'nap 5',
                        "write_beat ('processor',) []",
                        "restart_if_changed ([OWN], ['waiting', 'run', '--repo', 'REPO']) []",
                        'run_step alpha',
                        "write_beat ('processor',) []",
                        "sd_notify ('WATCHDOG=1',) []",
                        'run_step beta',
                        "write_beat ('processor',) []",
                        "sd_notify ('WATCHDOG=1',) []",
                        "write_json ('D/round-ok.json', {'at': 100.0}) []",
                        "sd_notify ('WATCHDOG=1',) []",
                        'nap 5',
                        'drop close',
                        'own release']),
 'failures run on, stall noted once': (0,
                                       True,
                                       ['own acquire',
                                        'TokenDrop D SLUG',
                                        'drop open',
                                        'clone shared REPO',
                                        'shared step ran',
                                        "write_beat ('processor',) []",
                                        "restart_if_changed ([OWN], ['waiting', 'run', '--repo', 'REPO']) []",
                                        'run_step alpha',
                                        "write_beat ('processor',) []",
                                        "sd_notify ('WATCHDOG=1',) []",
                                        'run_step beta',
                                        "write_beat ('processor',) []",
                                        "sd_notify ('WATCHDOG=1',) []",
                                        "write_snapshot ('D', 'ST') []",
                                        "sd_notify ('WATCHDOG=1',) []",
                                        'nap 5',
                                        "write_beat ('processor',) []",
                                        "restart_if_changed ([OWN], ['waiting', 'run', '--repo', 'REPO']) []",
                                        'run_step alpha',
                                        "write_beat ('processor',) []",
                                        "sd_notify ('WATCHDOG=1',) []",
                                        'run_step beta',
                                        "write_beat ('processor',) []",
                                        "sd_notify ('WATCHDOG=1',) []",
                                        "run_probe ('waiting_ok', {'d': 'D', 'since': 100.0}) []",
                                        'open_condition (\'STALLED\', "STALLED: the waiting processor\'s ticks '
                                        'failed 2 times in a row; see D/processor.log and service.log", \'PROBE\', '
                                        "[]) [('command', 'waiting'), ('key', 'waiting-rounds'), ('store', 'ST')]",
                                        "write_snapshot ('D', 'ST') []",
                                        "sd_notify ('WATCHDOG=1',) []",
                                        'nap 5',
                                        "write_beat ('processor',) []",
                                        "restart_if_changed ([OWN], ['waiting', 'run', '--repo', 'REPO']) []",
                                        'run_step alpha',
                                        "write_beat ('processor',) []",
                                        "sd_notify ('WATCHDOG=1',) []",
                                        'run_step beta',
                                        "write_beat ('processor',) []",
                                        "sd_notify ('WATCHDOG=1',) []",
                                        "write_snapshot ('D', 'ST') []",
                                        "sd_notify ('WATCHDOG=1',) []",
                                        'nap 5',
                                        "write_beat ('processor',) []",
                                        "restart_if_changed ([OWN], ['waiting', 'run', '--repo', 'REPO']) []",
                                        'run_step alpha',
                                        "write_beat ('processor',) []",
                                        "sd_notify ('WATCHDOG=1',) []",
                                        'run_step beta',
                                        "write_beat ('processor',) []",
                                        "sd_notify ('WATCHDOG=1',) []",
                                        "write_snapshot ('D', 'ST') []",
                                        "sd_notify ('WATCHDOG=1',) []",
                                        'nap 5',
                                        'drop close',
                                        'own release']),
 'stall note fails, noted again next round': (0,
                                              True,
                                              ['own acquire',
                                               'TokenDrop D SLUG',
                                               'drop open',
                                               "write_beat ('processor',) []",
                                               "restart_if_changed ([OWN], ['waiting', 'run', '--repo', 'REPO']) []",
                                               'run_step alpha',
                                               "write_beat ('processor',) []",
                                               "sd_notify ('WATCHDOG=1',) []",
                                               'run_step beta',
                                               "write_beat ('processor',) []",
                                               "sd_notify ('WATCHDOG=1',) []",
                                               "sd_notify ('WATCHDOG=1',) []",
                                               'nap 5',
                                               "write_beat ('processor',) []",
                                               "restart_if_changed ([OWN], ['waiting', 'run', '--repo', 'REPO']) []",
                                               'run_step alpha',
                                               "write_beat ('processor',) []",
                                               "sd_notify ('WATCHDOG=1',) []",
                                               'run_step beta',
                                               "write_beat ('processor',) []",
                                               "sd_notify ('WATCHDOG=1',) []",
                                               "run_probe ('waiting_ok', {'d': 'D', 'since': 100.0}) []",
                                               'open_condition (\'STALLED\', "STALLED: the waiting processor\'s '
                                               'ticks failed 2 times in a row; see D/processor.log and service.log", '
                                               "'PROBE', []) [('command', 'waiting'), ('key', 'waiting-rounds'), "
                                               "('store', 'ST')]",
                                               "print_error ('STALLED could not be noted (down); the loop goes on',) []",
                                               "sd_notify ('WATCHDOG=1',) []",
                                               'nap 5',
                                               "write_beat ('processor',) []",
                                               "restart_if_changed ([OWN], ['waiting', 'run', '--repo', 'REPO']) []",
                                               'run_step alpha',
                                               "write_beat ('processor',) []",
                                               "sd_notify ('WATCHDOG=1',) []",
                                               'run_step beta',
                                               "write_beat ('processor',) []",
                                               "sd_notify ('WATCHDOG=1',) []",
                                               "run_probe ('waiting_ok', {'d': 'D', 'since': 100.0}) []",
                                               'open_condition (\'STALLED\', "STALLED: the waiting processor\'s '
                                               'ticks failed 3 times in a row; see D/processor.log and service.log", '
                                               "'PROBE', []) [('command', 'waiting'), ('key', 'waiting-rounds'), "
                                               "('store', 'ST')]",
                                               "print_error ('STALLED could not be noted (down); the loop goes on',) []",
                                               "sd_notify ('WATCHDOG=1',) []",
                                               'nap 5',
                                               'drop close',
                                               'own release']),
 'stall note exits': (0,
                      True,
                      ['own acquire',
                       'TokenDrop D SLUG',
                       'drop open',
                       "write_beat ('processor',) []",
                       "restart_if_changed ([OWN], ['waiting', 'run', '--repo', 'REPO']) []",
                       'run_step alpha',
                       "write_beat ('processor',) []",
                       "sd_notify ('WATCHDOG=1',) []",
                       'run_step beta',
                       "write_beat ('processor',) []",
                       "sd_notify ('WATCHDOG=1',) []",
                       "sd_notify ('WATCHDOG=1',) []",
                       'nap 5',
                       "write_beat ('processor',) []",
                       "restart_if_changed ([OWN], ['waiting', 'run', '--repo', 'REPO']) []",
                       'run_step alpha',
                       "write_beat ('processor',) []",
                       "sd_notify ('WATCHDOG=1',) []",
                       'run_step beta',
                       "write_beat ('processor',) []",
                       "sd_notify ('WATCHDOG=1',) []",
                       "run_probe ('waiting_ok', {'d': 'D', 'since': 100.0}) []",
                       'open_condition (\'STALLED\', "STALLED: the waiting processor\'s ticks failed 2 times in a '
                       'row; see D/processor.log and service.log", \'PROBE\', []) [(\'command\', \'waiting\'), '
                       "('key', 'waiting-rounds'), ('store', 'ST')]",
                       "print_error ('STALLED could not be noted (no token); the loop goes on',) []",
                       "sd_notify ('WATCHDOG=1',) []",
                       'nap 5',
                       'drop close',
                       'own release']),
 'failed, recovered, failed again': (0,
                                     True,
                                     ['own acquire',
                                      'TokenDrop D SLUG',
                                      'drop open',
                                      "write_beat ('processor',) []",
                                      "restart_if_changed ([OWN], ['waiting', 'run', '--repo', 'REPO']) []",
                                      'run_step alpha',
                                      "write_beat ('processor',) []",
                                      "sd_notify ('WATCHDOG=1',) []",
                                      'run_step beta',
                                      "write_beat ('processor',) []",
                                      "sd_notify ('WATCHDOG=1',) []",
                                      "sd_notify ('WATCHDOG=1',) []",
                                      'nap 5',
                                      "write_beat ('processor',) []",
                                      "restart_if_changed ([OWN], ['waiting', 'run', '--repo', 'REPO']) []",
                                      'run_step alpha',
                                      "write_beat ('processor',) []",
                                      "sd_notify ('WATCHDOG=1',) []",
                                      'run_step beta',
                                      "write_beat ('processor',) []",
                                      "sd_notify ('WATCHDOG=1',) []",
                                      "run_probe ('waiting_ok', {'d': 'D', 'since': 100.0}) []",
                                      'open_condition (\'STALLED\', "STALLED: the waiting processor\'s ticks failed '
                                      '2 times in a row; see D/processor.log and service.log", \'PROBE\', []) '
                                      "[('command', 'waiting'), ('key', 'waiting-rounds'), ('store', 'ST')]",
                                      "sd_notify ('WATCHDOG=1',) []",
                                      'nap 5',
                                      "write_beat ('processor',) []",
                                      "restart_if_changed ([OWN], ['waiting', 'run', '--repo', 'REPO']) []",
                                      'run_step alpha',
                                      "write_beat ('processor',) []",
                                      "sd_notify ('WATCHDOG=1',) []",
                                      'run_step beta',
                                      "write_beat ('processor',) []",
                                      "sd_notify ('WATCHDOG=1',) []",
                                      "write_json ('D/round-ok.json', {'at': 100.0}) []",
                                      "sd_notify ('WATCHDOG=1',) []",
                                      'nap 5',
                                      "write_beat ('processor',) []",
                                      "restart_if_changed ([OWN], ['waiting', 'run', '--repo', 'REPO']) []",
                                      'run_step alpha',
                                      "write_beat ('processor',) []",
                                      "sd_notify ('WATCHDOG=1',) []",
                                      'run_step beta',
                                      "write_beat ('processor',) []",
                                      "sd_notify ('WATCHDOG=1',) []",
                                      "sd_notify ('WATCHDOG=1',) []",
                                      'nap 5',
                                      "write_beat ('processor',) []",
                                      "restart_if_changed ([OWN], ['waiting', 'run', '--repo', 'REPO']) []",
                                      'run_step alpha',
                                      "write_beat ('processor',) []",
                                      "sd_notify ('WATCHDOG=1',) []",
                                      'run_step beta',
                                      "write_beat ('processor',) []",
                                      "sd_notify ('WATCHDOG=1',) []",
                                      "run_probe ('waiting_ok', {'d': 'D', 'since': 100.0}) []",
                                      'open_condition (\'STALLED\', "STALLED: the waiting processor\'s ticks failed '
                                      '2 times in a row; see D/processor.log and service.log", \'PROBE\', []) '
                                      "[('command', 'waiting'), ('key', 'waiting-rounds'), ('store', 'ST')]",
                                      "sd_notify ('WATCHDOG=1',) []",
                                      'nap 5',
                                      'drop close',
                                      'own release']),
 'snapshot fails': (0,
                    True,
                    ['own acquire',
                     'TokenDrop D SLUG',
                     'drop open',
                     'clone shared REPO',
                     'shared step ran',
                     "write_beat ('processor',) []",
                     "restart_if_changed ([OWN], ['waiting', 'run', '--repo', 'REPO']) []",
                     'run_step alpha',
                     "write_beat ('processor',) []",
                     "sd_notify ('WATCHDOG=1',) []",
                     'run_step beta',
                     "write_beat ('processor',) []",
                     "sd_notify ('WATCHDOG=1',) []",
                     "write_json ('D/round-ok.json', {'at': 100.0}) []",
                     "write_snapshot ('D', 'ST') []",
                     "print_error ('the snapshot could not be written: disk',) []",
                     "sd_notify ('WATCHDOG=1',) []",
                     'nap 5',
                     "write_beat ('processor',) []",
                     "restart_if_changed ([OWN], ['waiting', 'run', '--repo', 'REPO']) []",
                     'run_step alpha',
                     "write_beat ('processor',) []",
                     "sd_notify ('WATCHDOG=1',) []",
                     'run_step beta',
                     "write_beat ('processor',) []",
                     "sd_notify ('WATCHDOG=1',) []",
                     "write_json ('D/round-ok.json', {'at': 100.0}) []",
                     "write_snapshot ('D', 'ST') []",
                     "print_error ('the snapshot could not be written: disk',) []",
                     "sd_notify ('WATCHDOG=1',) []",
                     'nap 5',
                     'drop close',
                     'own release'])}
