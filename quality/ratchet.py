#!/usr/bin/env python3
"""agentctl's ratchets (CLAUDE.md, "The ratchets"): the code's measures may not grow past their checked-in baselines.

  quality/ratchet.py check [--range A..B ...]   measure, hold every row to its baseline, and judge each range's
                                                commits that grew a baseline (exit 1 on any refusal)
  quality/ratchet.py update                     rewrite the baselines from the measure (falls recorded; a rise is
                                                then committed with its trailer)
  quality/ratchet.py selftest                   seeds a violation of every measure into a scratch tree and checks the
                                                real tools refuse it (the hook runs it first: a tool that measures
                                                nothing must never pass)

Run it with the tools' python (quality/tools.py prints it; hooks/pre-push installs it). The engine is the movies
repository's baseline ratchet (devtools/design_rules/baseline_ratchet.py, vendored byte-equal): a baseline holds a
`limit<TAB>kind<TAB>N` row per kind and a `kind<TAB>key<TAB>value` row per measured row past it; a NEW row past a
limit or a RISING one refuses the push, and so does a fall the baseline does not record (THE FALL LOCK: `update`
records it, no trailer needed); the baseline may grow only in a commit carrying the kind's trailer in its final trailer block (THE ATTRIBUTION RULE, devtools/design_rules/trailers.py:
each commit of a range is judged by its own step; one commit's trailer never excuses another's).

  quality     quality/quality-baseline.tsv   cognitive, cyclomatic, inner-defs   Complexity:
                                             method-lines, file-lines            Length:
                                             copies                              Duplication:
                                             cycle                               Coupling:
  types       quality/types-baseline.tsv     mypy-errors (mypy --strict)         Typing:
  design      quality/design-baseline.tsv    suffix, abbreviation                Naming:    (quality/design.py: the
                                             effect, state, import-io            Design:     movies design rules,
                                             fanout, distance, layer, ...        Coupling:   vendored)
"""
import contextlib
import dataclasses
import io
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(1, os.path.join(os.path.dirname(HERE), "devtools", "design_rules"))
import baseline_ratchet  # noqa: E402
import design  # noqa: E402
import measure  # noqa: E402
import typecheck  # noqa: E402

HEADER = ("# THE {title} BASELINE (quality/ratchet.py update writes it; hooks/pre-push holds every push to it).\n"
          "# {what}, frozen at its value. A row may only fall (update records it); a row added or raised\n"
          "# lands only in a commit carrying the kind's trailer ({trailers}). CLAUDE.md, \"The ratchets\".\n")


def spec(title, what, name, trailer_kinds, hint):
    trailers = ", ".join(f"`{t}:`" for t in trailer_kinds)
    return baseline_ratchet.Spec(
        title=title, tool=name, baseline=f"quality/{name}-baseline.tsv",
        header=HEADER.format(title=title, what=what, trailers=trailers).rstrip("\n"), hint=hint,
        update_command="quality/ratchet.py update", held_rows="rows past a limit", trailers=trailer_kinds,
        held=lambda _: True)


SPECS = {
    "quality": spec("QUALITY", "Every function, part and part set past a limit (quality/measure.py)", "quality",
                    {"Complexity": ("cognitive", "cyclomatic", "inner-defs"), "Length": ("method-lines", "file-lines"),
                     "Duplication": ("copies",), "Coupling": ("cycle",)},
                    "split the function at a real seam; extract a copied block into one function named after its concept"),
    "types": spec("TYPES", "mypy --strict errors per part (quality/typecheck.py)", "types", {"Typing": ("mypy-errors",)},
                  "annotate it (mypy --strict: python3 quality/typecheck.py --errors lists each error)"),
    "design": spec("DESIGN", "Every module, name and directory past a design rule (quality/design.py)", "design",
                   design.TRAILERS,
                   "rename it, inject the clock / runner / settings, move the state into an object the root passes on, "
                   "cut the dependency -- or record it (update + the kind's trailer, or a quality/design-exemptions.tsv row)"),
}


def current_rows(root, which=("quality", "types", "design"), analysers=None, type_rows=None):
    """{ratchet: {(kind, key): value}}: every kind's limit row, and every row past its limit."""
    out = {}
    if "quality" in which:
        past = [(k, key, v) for k, key, v in measure.past_limits(measure.measure(root, analysers))]
        out["quality"] = {("limit", k): measure.LIMITS[k] for k in SPECS["quality"].kinds}
        out["quality"].update({(k, key): v for k, key, v in past})
    if "types" in which:
        if type_rows is None:
            type_rows = typecheck.rows(typecheck.mypy_errors(root))
        out["types"] = {("limit", "mypy-errors"): 0}
        out["types"].update({(k, key): v for k, key, v in type_rows if v > 0})
    if "design" in which:
        out["design"] = {(k, key): v for k, key, v in design.rows(root)}
    return out


def refused_by_design(root, ranges):
    """The design ratchet's own refusals beside the baseline's: a blocked name clash, a refused exemption row."""
    out = [f"NAMING REFUSED: two names of one package with the same words in another order -- rename one after what "
           f"makes it distinct: {clash}" for clash in design.permutations(root)]
    out += [f"DESIGN EXEMPTION REFUSED: {p}" for p in design.exemption_problems(root, ranges)]
    for warning in design.action_warnings(root, ranges):
        print(f"naming WARNING: a new type named for an action with at most one public def: {warning} -- fold it into "
              "the class whose data it reads, or name the one thing it is")
    return out


@contextlib.contextmanager
def in_checkout(root):
    """The engine reads the baselines and the history from the checkout it is run in."""
    here = os.getcwd()
    os.chdir(root)
    try:
        yield
    finally:
        os.chdir(here)


def spec_of(root, ratchet):
    """The ratchet's spec, the design one with the split credit judging a `Coupling: split of` commit's importers."""
    spec = SPECS[ratchet]
    return dataclasses.replace(spec, split_check=design.split_check(root)) if ratchet == "design" else spec


def check(root, ranges=(), **measured):
    current = current_rows(root, **measured)
    ok = True
    for ratchet, rows in current.items():
        if not os.path.isfile(os.path.join(root, SPECS[ratchet].baseline)):
            print(f"{ratchet}: REFUSED -- no {SPECS[ratchet].baseline}: quality/ratchet.py update writes it")
            ok = False
            continue
        with in_checkout(root):
            ok = baseline_ratchet.check(spec_of(root, ratchet), rows, list(ranges)) and ok
    if "design" in current:
        for line in refused_by_design(root, ranges):
            print(line)
            ok = False
    print(f"ratchets: {'OK' if ok else 'REFUSED'} ({', '.join(f'{r} {len(rows)} rows' for r, rows in current.items())})")
    return 0 if ok else 1


def update(root, **measured):
    for ratchet, rows in current_rows(root, **measured).items():
        with in_checkout(root):
            baseline_ratchet.update(SPECS[ratchet], rows)
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
SEEDED_DESIGN = {
    "thing_manager": "class ThingManager:\n    pass\n",  # suffix
    "cfg_reader": "def cfg_value():\n    return 1\n",  # abbreviation
    "sweep_prune": "def sweep():\n    return 1\n",  # a clash with
    "prune_sweep": "def prune():\n    return 1\n",  # ... this one
    "direct_clock": "import time\n\n\ndef now():\n    return time.time()\n",  # effect
    "stateful": "CACHE = {}\n\n\ndef remember(key):\n    CACHE[key] = 1\n",  # state
    "loads_io": "import os\n\nHERE = os.listdir('.')\n",  # import-io
    "runs_each": "import subprocess\n\n\ndef run_each(items):\n    for item in items:\n        if item:\n            subprocess.run([item])\n",  # mixed
}


def seed_tree(tmp):
    lib = os.path.join(tmp, "lib", "agentctl")
    os.makedirs(lib)
    os.makedirs(os.path.join(tmp, "quality"))
    with open(os.path.join(HERE, "design-rules.tsv")) as f, open(os.path.join(tmp, design.RULES), "w") as out:
        out.write(f.read())
    names = [f"p{i}" for i in range(11)]
    with open(os.path.join(lib, "__init__.py"), "w") as f:
        f.write(f"MODULES = {tuple(['seed'] + names)!r}\n")
    uses = "\n".join(f"    {n}_name()" for n in names)
    with open(os.path.join(lib, "seed.py"), "w") as f:
        f.write("".join(f"from agentctl.{n} import {n}_name\n" for n in names) + "\n\n" + SEEDED + "\n\ndef cloned_a(beta, gamma, delta):\n" + CLONE + "\n\ndef cloned_b(beta, gamma, delta):\n"
                + CLONE + f"\n\ndef uses_many():\n{uses}\n" + "\n" * 480)
    for n in names:  # eleven collaborators (each reads the clock) for the fan-out, in a cycle with seed
        with open(os.path.join(lib, f"{n}.py"), "w") as f:
            f.write(f"import time\nfrom agentctl.seed import uses_many\n\n\ndef {n}_name():\n    time.sleep(0)\n    return uses_many()\n")
    for name, source in SEEDED_DESIGN.items():
        with open(os.path.join(lib, f"{name}.py"), "w") as f:
            f.write(source)


def selftest():
    """Seeds every kind's violation into a scratch tree; the real measure must report each, and check must refuse."""
    with tempfile.TemporaryDirectory() as tmp:
        seed_tree(tmp)
        rows = current_rows(tmp)
        kinds = {kind for ratchet in rows.values() for kind, _ in ratchet if kind != "limit"}
        wanted = {k for s in SPECS.values() for k in s.kinds} - {"unplaced", "layer", "dircycle", "distance", "databag", "datafanout"}
        missing = sorted(wanted - kinds)
        if missing:
            print(f"selftest: FAILED -- the measure missed the seeded {', '.join(missing)}")
            return 1
        for ratchet in rows:  # an empty baseline (the limits alone): every seeded row is new
            with in_checkout(tmp), contextlib.redirect_stdout(io.StringIO()):
                baseline_ratchet.update(SPECS[ratchet], {k: v for k, v in rows[ratchet].items() if k[0] == "limit"})
        with contextlib.redirect_stdout(io.StringIO()):
            refused = check(tmp)
        if refused != 1 or not design.permutations(tmp):
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
