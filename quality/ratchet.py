#!/usr/bin/env python3
"""agentctl's ratchets (CLAUDE.md, "The ratchets"): the code's measures may not grow past their checked-in baselines.

  quality/ratchet.py check [--range A..B ...]   measure, hold every row to its baseline, and judge each range's
                                                commits that grew a baseline (exit 1 on any refusal)
  quality/ratchet.py update                     rewrite the baselines from the measure (falls recorded; a rise is
                                                then committed with its trailer)
  quality/ratchet.py selftest                   seeds a violation of every measure into a scratch tree and checks the
                                                real tools refuse it (the hook runs it first: a tool that measures
                                                nothing must never pass)

Run it with the tools' python (quality/tools.py prints it; hooks/pre-push installs it). The ratchets and their
trailers -- the movies model (~/projects/movies/scripts/ci/quality_ratchet.py):

  quality     quality/quality-baseline.tsv   cognitive, cyclomatic, inner-defs   Complexity:
                                             method-lines, file-lines            Length:
                                             copies                              Duplication:
                                             fan-out, cycle                      Coupling:
  types       quality/types-baseline.tsv     mypy-errors (mypy --strict)         Types:

A row is `kind<TAB>key<TAB>value`; `#` lines are comments. A measured row past its kind's limit (quality/measure.py
LIMITS; mypy-errors 0) must have a baseline row at or above it: a key without one is NEW, a value above it ROSE, and
both refuse. A row below its baseline, or gone, is a fall: said, and passes (`update` records it). The baseline may
grow only in a commit carrying the kind's trailer in its final trailer block (THE ATTRIBUTION RULE: each commit of a
range that grows a baseline is judged by its own step, parent to itself; one commit's trailer never excuses another's).
"""
import os
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import measure  # noqa: E402
import typecheck  # noqa: E402

TRAILERS = {"cognitive": "Complexity", "cyclomatic": "Complexity", "inner-defs": "Complexity",
            "method-lines": "Length", "file-lines": "Length", "copies": "Duplication", "fan-out": "Coupling",
            "cycle": "Coupling", "mypy-errors": "Types"}
BASELINES = {"quality": "quality/quality-baseline.tsv", "types": "quality/types-baseline.tsv"}
HEADERS = {
    "quality": "# THE QUALITY BASELINE (quality/ratchet.py update writes it; hooks/pre-push holds every push to it).\n"
               "# Every function, part and part set past a limit (quality/measure.py), frozen at its value. A row may only\n"
               "# fall (update records it); a row added or raised lands only in a commit carrying the kind's trailer:\n"
               "# Complexity: (cognitive, cyclomatic, inner-defs), Length: (method-lines, file-lines), Duplication:\n"
               "# (copies), Coupling: (fan-out, cycle). CLAUDE.md, \"The ratchets\".\n",
    "types": "# THE TYPES BASELINE (quality/ratchet.py update writes it; hooks/pre-push holds every push to it).\n"
             "# mypy --strict errors per part (quality/typecheck.py), frozen. A row may only fall; a rise lands only in a\n"
             "# commit carrying `Types: <why>`. CLAUDE.md, \"The ratchets\".\n",
}
HINTS = {"Complexity": "split the function at a real seam", "Length": "split the function or the part",
         "Duplication": "extract the copied block into one function named after its concept",
         "Coupling": "use fewer other parts' names (move the code to the part that owns them)",
         "Types": "annotate it (mypy --strict: python3 quality/typecheck.py --errors lists each error)"}


def limit(kind):
    return measure.LIMITS.get(kind, 0)


def current_rows(root, which=("quality", "types"), analysers=None, type_rows=None):
    """{ratchet: {(kind, key): value}} of every row past its limit."""
    out = {}
    if "quality" in which:
        out["quality"] = {(k, key): v for k, key, v in measure.past_limits(measure.measure(root, analysers))}
    if "types" in which:
        if type_rows is None:
            type_rows = typecheck.rows(typecheck.mypy_errors(root))
        out["types"] = {(k, key): v for k, key, v in type_rows if v > limit(k)}
    return out


def parse(text):
    rows = {}
    for line in text.splitlines():
        if line.strip() and not line.startswith("#"):
            kind, key, value = line.split("\t")
            rows[(kind, key)] = int(value)
    return rows


def read_baseline(root, ratchet):
    path = os.path.join(root, BASELINES[ratchet])
    return parse(open(path).read()) if os.path.exists(path) else {}


def compare(baseline, current):
    """(refusals, falls): lines naming each NEW / ROSE row, and each fall."""
    refusals, falls = [], []
    for (kind, key), value in sorted(current.items()):
        frozen = baseline.get((kind, key))
        trailer = TRAILERS[kind]
        if frozen is None:
            refusals.append(f"NEW   {kind} {key} = {value} (limit {limit(kind)}): {HINTS[trailer]}, or record it "
                            f"(update + a `{trailer}: <why>` trailer)")
        elif value > frozen:
            refusals.append(f"ROSE  {kind} {key} {frozen} -> {value}: {HINTS[trailer]}, or record it "
                            f"(update + a `{trailer}: <why>` trailer)")
    for (kind, key), frozen in sorted(baseline.items()):
        value = current.get((kind, key), limit(kind))
        if value < frozen:
            falls.append(f"fell  {kind} {key} {frozen} -> {value}")
    return refusals, falls


def git(root, *args):
    return subprocess.run(["git", "-C", root, *args], capture_output=True, text=True, timeout=60)


def grown_kinds(before, after):
    return {kind for (kind, key), v in after.items() if v > before.get((kind, key), limit(kind))}


def unjustified(root, rng, ratchet):
    """Each commit of `rng` whose own step grows the ratchet's baseline without the trailer its grown kinds need."""
    path = BASELINES[ratchet]
    commits = git(root, "rev-list", "--reverse", rng, "--", path).stdout.split()
    out = []
    for sha in commits:
        before = parse(git(root, "show", f"{sha}^:{path}").stdout)
        after = parse(git(root, "show", f"{sha}:{path}").stdout)
        for trailer in sorted({TRAILERS[k] for k in grown_kinds(before, after)}):
            given = git(root, "log", "-1", f"--format=%(trailers:key={trailer},valueonly)", sha).stdout.strip()
            if not given:
                out.append(f"{sha[:10]} grows {path} without a `{trailer}: <why>` trailer in its final trailer block")
    return out


def check(root, ranges=(), **measured):
    current = current_rows(root, **measured)
    refused = []
    for ratchet, rows in current.items():
        refusals, falls = compare(read_baseline(root, ratchet), rows)
        refused += [f"{ratchet}: {line}" for line in refusals]
        for line in falls:
            print(f"{ratchet}: {line} (record it: quality/ratchet.py update)")
        for rng in ranges:
            refused += [f"{ratchet}: {line}" for line in unjustified(root, rng, ratchet)]
    for line in refused:
        print(line)
    total = {r: len(rows) for r, rows in current.items()}
    print(f"ratchets: {'REFUSED' if refused else 'OK'} ({', '.join(f'{r} {n} rows' for r, n in total.items())})")
    return 1 if refused else 0


def write_baseline(root, ratchet, rows):
    lines = [f"{kind}\t{key}\t{value}" for (kind, key), value in sorted(rows.items())]
    with open(os.path.join(root, BASELINES[ratchet]), "w") as f:
        f.write(HEADERS[ratchet] + "\n".join(lines) + "\n")


def update(root, **measured):
    for ratchet, rows in current_rows(root, **measured).items():
        write_baseline(root, ratchet, rows)
        print(f"{ratchet}: {len(rows)} rows -> {BASELINES[ratchet]}")
    return 0


SEEDED = '''
def seeded_long_and_complex(a, b, c):
    """seeded: every complexity and length measure past its limit"""
    def one():
        def two():
            def three():
                return 1
            return three
        return two
    total = 0
''' + "".join(f'''    if a > {i} and b < {i} or c == {i}:
        for x in range({i}):
            if x % 2 and (a or b):
                total += x
''' for i in range(14)) + '''    return total + undefined_name_seeded(total)
'''
CLONE = "".join(f"    v{i} = alpha({i}, beta, gamma) + delta[{i}]\n" for i in range(12))


def selftest():
    """Seeds every kind's violation into a scratch tree; the real measure must report each, and check must refuse."""
    with tempfile.TemporaryDirectory() as tmp:
        lib = os.path.join(tmp, "lib", "agentctl")
        os.makedirs(lib)
        os.makedirs(os.path.join(tmp, "quality"))
        names = [f"p{i}" for i in range(11)]
        with open(os.path.join(lib, "__init__.py"), "w") as f:
            f.write(f"PARTS = {tuple(['seed'] + names)!r}\n")
        uses = "\n".join(f"    {n}_name()" for n in names)
        with open(os.path.join(lib, "seed.py"), "w") as f:
            f.write(SEEDED + "\n\ndef cloned_a(beta, gamma, delta):\n" + CLONE + "\n\ndef cloned_b(beta, gamma, delta):\n"
                    + CLONE + f"\n\ndef uses_many():\n{uses}\n" + "\n" * 480)
        for n in names:
            with open(os.path.join(lib, f"{n}.py"), "w") as f:
                f.write(f"def {n}_name():\n    return uses_many()\n")
        rows = current_rows(tmp)
        kinds = {kind for ratchet in rows.values() for kind, _ in ratchet}
        missing = sorted(set(TRAILERS) - kinds)
        if missing:
            print(f"selftest: FAILED -- the measure missed the seeded {', '.join(missing)}")
            return 1
        import contextlib
        import io
        with contextlib.redirect_stdout(io.StringIO()):
            refused = check(tmp)
        if refused != 1:
            print("selftest: FAILED -- check passed a seeded violation")
            return 1
        print(f"selftest: OK (every kind refused: {', '.join(sorted(kinds))})")
        return 0


def main(argv):
    root = os.path.dirname(HERE)
    if argv[:1] == ["check"]:
        return check(root, [argv[i + 1] for i, a in enumerate(argv) if a == "--range"])
    if argv[:1] == ["update"]:
        return update(root)
    if argv[:1] == ["selftest"]:
        return selftest()
    print(__doc__)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
