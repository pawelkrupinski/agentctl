"""GOLDEN CASES (restructure round 4): a function run over thousands of seeded cases with its collaborators faked, every
call and the result digested. A restructure that changes ANY call, its order or its result changes the digest.

  case = Case(seed); case.fake("name", lambda rng, *a, **k: value) ... ; digest(fn, case, args)

The fakes patch the name in every module binding it (tests' view `A`), and are restored afterwards. A fake returns what
its generator draws from the case's own random stream, so the same calls in the same order see the same values."""
import contextlib, hashlib, json, os, random, time
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
        self.A, self.rng, self.log, self.saved, self.attrs = A, random.Random(seed), [], {}, []

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
        if name == "time":  # the code reads the time through the process's clock (clock.epoch_now) and the wall (wall_epoch)
            self.put("epoch_now", value.time)
            self.put("wall_epoch", value.time)

    def patch(self, obj, attr, gen):
        """obj.attr (a function of the standard library: os.path.isdir, glob.glob) replaced by a recorded fake, restored
        afterwards."""
        self.attrs.append((obj, attr, getattr(obj, attr)))
        setattr(obj, attr, self.recorded(f"{getattr(obj, '__name__', type(obj).__name__)}.{attr}", gen))

    def restore(self):
        for name, value in self.saved.items():
            setattr(self.A, name, value)
        for obj, attr, value in reversed(self.attrs):
            setattr(obj, attr, value)


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


@contextlib.contextmanager
def pinned_zone(zone="CET-1"):
    """The digests were recorded on a CET laptop: a local-time line (`time.strftime('%H:%M', localtime(..))`) must read the
    same on a UTC runner. A fixed POSIX zone (UTC+1, no DST, no tzdata), os.environ and the C library restored after."""
    before = os.environ.get("TZ")
    os.environ["TZ"] = zone
    time.tzset()
    try:
        yield
    finally:
        if before is None:
            os.environ.pop("TZ", None)
        else:
            os.environ["TZ"] = before
        time.tzset()


def digest(A, name, build, fn_of, n=1500):
    h = hashlib.sha256()
    with pinned_zone():
        for seed in range(n):
            h.update(outcome(A, seed, build, fn_of).encode())
    return h.hexdigest()


def recorded_digests(path=DIGESTS):
    return json.loads(open(path).read()) if os.path.exists(path) else {}


def check(test, A, name, build, fn_of, n=1500, digests=DIGESTS):
    got = digest(A, name, build, fn_of, n)
    if os.environ.get("R4_RECORD"):
        d = recorded_digests(digests)
        d[name] = got
        with open(digests, "w") as f:
            json.dump(d, f, indent=1, sort_keys=True)
        return
    test.assertEqual(recorded_digests(digests).get(name), got, f"{name}: behaviour moved")
