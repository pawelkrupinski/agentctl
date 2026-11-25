"""GOLDEN CASES (restructure round 4): a function run over thousands of seeded cases with its collaborators faked, every
call and the result digested. A restructure that changes ANY call, its order or its result changes the digest.

  case = Case(seed); case.fake("name", lambda rng, *a, **k: value) ... ; digest(fn, case, args)

The fakes patch the name in every module binding it (tests' view `A`), and are restored afterwards. A fake returns what
its generator draws from the case's own random stream, so the same calls in the same order see the same values."""
import hashlib, json, os, random
from unittest import mock

DIGESTS = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures", "restructure4-digests.json")


def stable(x):
    """repr with sets sorted, so a hash seed never moves a digest."""
    if isinstance(x, (set, frozenset)):
        return "{" + ", ".join(sorted(stable(i) for i in x)) + "}"
    if isinstance(x, dict):
        return "{" + ", ".join(f"{stable(k)}: {stable(v)}" for k, v in x.items()) + "}"
    if isinstance(x, (list, tuple)):
        return "(" + ", ".join(stable(i) for i in x) + ")"
    return repr(x)


class Probe:
    """An object whose every method is a recorded fake (a store, a clock): `Probe(case, "st", put_item=gen)`."""

    def __init__(self, case, name, **gens):
        self._case, self._name, self._gens = case, name, gens

    def __getattr__(self, attr):
        if attr.startswith("__"):
            raise AttributeError(attr)
        return self._case.recorded(f"{self._name}.{attr}", self._gens.get(attr, lambda rng, *a, **k: None))

    def __repr__(self):
        return f"<{self._name}>"


class Case:
    def __init__(self, A, seed):
        self.A, self.rng, self.log, self.saved = A, random.Random(seed), [], {}

    def recorded(self, name, gen):
        def fake(*a, **k):
            try:
                value = gen(self.rng, *a, **k)
            except Exception as e:
                self.log.append([name, stable(a), stable(k), "raised " + type(e).__name__])
                raise
            self.log.append([name, stable(a), stable(k), stable(value)])
            return value
        return fake

    def fake(self, name, gen):
        self.saved.setdefault(name, getattr(self.A, name))
        setattr(self.A, name, self.recorded(name, gen))

    def put(self, name, value):
        """A plain replacement (a module, a constant) of the name, restored afterwards."""
        self.saved.setdefault(name, getattr(self.A, name))
        setattr(self.A, name, value)

    def restore(self):
        for name, value in self.saved.items():
            setattr(self.A, name, value)


def outcome(A, seed, build, fn_of):
    """One case: build(case) fakes and returns the args; fn_of(A) the function under test. -> the digest line."""
    case = Case(A, seed)
    try:
        args = build(case)
        try:
            result = ["ok", stable(fn_of(A)(*args))]
        except Exception as e:  # an exception IS an outcome
            result = ["raised", type(e).__name__, str(e)[:80]]
        return json.dumps([case.log, result], sort_keys=True)
    finally:
        case.restore()


def digest(A, name, build, fn_of, n=1500):
    h = hashlib.sha256()
    for seed in range(n):
        h.update(outcome(A, seed, build, fn_of).encode())
    return h.hexdigest()


def recorded_digests():
    return json.loads(open(DIGESTS).read()) if os.path.exists(DIGESTS) else {}


def check(test, A, name, build, fn_of, n=1500):
    got = digest(A, name, build, fn_of, n)
    if os.environ.get("R4_RECORD"):
        d = recorded_digests()
        d[name] = got
        with open(DIGESTS, "w") as f:
            json.dump(d, f, indent=1, sort_keys=True)
        return
    test.assertEqual(recorded_digests().get(name), got, f"{name}: behaviour moved")
