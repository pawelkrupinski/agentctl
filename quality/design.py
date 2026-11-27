#!/usr/bin/env python3
"""agentctl's DESIGN RULES (CLAUDE.md, "Design"): the movies repository's general rule core -- vendored byte-equal in
devtools/design_rules/ (devtools/vendor.py) -- over agentctl's Python modules, held to quality/design-baseline.tsv by the
ratchet engine every ratchet uses (devtools/design_rules/baseline_ratchet.py).

  Naming:    suffix, abbreviation <file>:<name>    no Impl / Manager / Helper / Util / Processor names, no abbreviations
  Design:    effect, state, import-io <file>       no direct clock, subprocess or os.environ outside an `adapter` row's
                                                   module; no module-level mutable state; no IO at import
             mixed <file>:<function>               a function performing an effect AND deciding (cyclomatic at the
                                                   `mixed-cyclomatic` limit or past it): its value the cyclomatic
  Coupling:  fanout <file>                         the COLLABORATOR modules it imports (a module touching the world, holding
                                                   state or an interface, or using one), at most 9
             distance, unplaced, dircycle <dir>, layer <from>-><to>, databag, datafanout
  A `Coupling: split of <X> into <A>[, <B>]` commit may raise an importer's fan-out by the pieces it now imports, where
  the vendored splits.py proves its use of X moved into them (split_check: both sides measured from the commit's trees).
  An exemption (quality/design-exemptions.tsv: kind, key, added date, a SPECIFIC why) admits one NEW row; its row added
  carries the kind's trailer too. Two names of one package with the same words in another order BLOCK the push.

  design.py rows [--root DIR]        the rows past a rule, `kind<TAB>key<TAB>value`
  design.py permutations [--root DIR] the blocked name clashes
The baseline's check and update are quality/ratchet.py's."""
import ast
import dataclasses
import os
import subprocess
import sys
from collections.abc import Callable

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "devtools", "design_rules"))
import coupling_reasons  # noqa: E402
import collaborators  # noqa: E402
import design_lint  # noqa: E402
import exemptions  # noqa: E402
import naming  # noqa: E402
import packages  # noqa: E402
import python_facts  # noqa: E402
import rules_table  # noqa: E402
import splits  # noqa: E402
import trailers  # noqa: E402

RULES = "quality/design-rules.tsv"
EXEMPTIONS = "quality/design-exemptions.tsv"
BASELINE = "quality/design-baseline.tsv"
TRAILERS = {"Naming": ("suffix", "abbreviation"), "Design": ("effect", "state", "import-io", "mixed"),
            "Coupling": ("fanout", "distance", "layer", "unplaced", "dircycle", "databag", "datafanout")}
ZERO_LIMITS = ("suffix", "abbreviation", "effect", "state", "import-io", "mixed", "layer", "unplaced", "dircycle")
LIBRARY = "lib/agentctl"
MEASURED_DIRECTORIES = ("quality", "devtools")  # beside the entry and lib/agentctl; devtools/design_rules is vendored
ALLOWED_PERMUTATIONS: dict[tuple[str, frozenset[str]], str] = {}  # (package, names): a clash being renamed -- its owner and why


def rules_of(root):
    with open(os.path.join(root, RULES)) as f:
        return rules_table.parse(f.read())


def is_measured(path: str) -> bool:
    """Whether a checkout path is a measured module: the entry, lib/agentctl, and the tools of quality/ and devtools/
    (self-tests, the vendored core and the tests are left out)."""
    directory, _, name = path.rpartition("/")
    return path == "agentctl" or (directory in (LIBRARY, *MEASURED_DIRECTORIES) and name.endswith(".py")
                                  and not name.endswith("_test.py"))


def sources(root):
    """The measured modules' paths in the checkout at `root`."""
    out = [p for p in ("agentctl",) if os.path.isfile(os.path.join(root, p))]
    for directory in (LIBRARY, *MEASURED_DIRECTORIES):
        full = os.path.join(root, directory)
        if os.path.isdir(full):
            out += [p for f in sorted(os.listdir(full)) if is_measured(p := f"{directory}/{f}")]
    return out


def imported(tree):
    """(dotted module, names) of every import statement."""
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                yield alias.name, ()
        elif isinstance(node, ast.ImportFrom) and node.module and not node.level:
            yield node.module, tuple(a.name for a in node.names)


def edges_of(path, tree, known):
    """The measured modules `path` imports: `agentctl.<part>` is lib/agentctl/<part>.py, a bare name its own directory's
    module (never another directory's: a module named like the standard library's, `queue`, is no import of it)."""
    out = set()
    for module, names in imported(tree):
        head, _, rest = module.partition(".")
        if head == "agentctl":
            out.update(t for t in (f"{LIBRARY}/{rest.split('.')[0]}.py" if rest else "",
                                   *(f"{LIBRARY}/{n}.py" for n in names if not rest)) if t in known)
        else:
            sibling = os.path.join(os.path.dirname(path), head + ".py")
            if os.path.dirname(path) != LIBRARY and sibling in known:
                out.add(sibling)
    out.discard(path)
    return out


def modules(root, added=frozenset()):
    """{path: the language-agnostic Module} of every measured source of the checkout at `root`."""
    texts = {}
    for path in sources(root):
        with open(os.path.join(root, path)) as f:
            texts[path] = f.read()
    return modules_of(texts, added)


def modules_of(texts, added=frozenset()):
    """{path: Module} of the measured sources `texts` ({path: source})."""
    trees = {path: ast.parse(text, path) for path, text in texts.items()}
    known = set(trees)
    out = {}
    for path, tree in trees.items():
        module = python_facts.module(path, tree, frozenset(edges_of(path, tree, known)))
        out[path] = dataclasses.replace(module, new=path in added)
    return out


def rows(root, added=frozenset()):
    """[(kind, key, value)] of every row past a rule, the limits first (the baseline records them)."""
    rules = rules_of(root)
    mods = modules(root, added)
    collaborating = collaborators.collaborators(mods, rules)
    found = naming.rows(mods.values(), rules) + design_lint.rows(mods.values(), rules)
    found += [("fanout", p, n) for p, m in sorted(mods.items())
              if (n := collaborators.fanout(m, collaborating)) > rules.limit("fanout")]
    found += collaborators.rows(mods, rules) + packages.rows("py", mods, collaborating, rules)
    admitted = exemptions_keys(root)
    limits = [("limit", kind, 0) for kind in ZERO_LIMITS]
    limits += [("limit", kind, rules.limit(kind)) for kind in ("fanout", "distance", "databag", "datafanout")]
    return limits + [r for r in found if (r[0], r[1]) not in admitted]


def git_out(root: str, *args: str) -> str:
    return subprocess.run(["git", "-C", root, *args], capture_output=True, text=True, check=True).stdout


def revision_texts(root: str, rev: str) -> dict[str, str]:
    """{path: source} of the measured modules at `rev` of the checkout at `root`."""
    listed = git_out(root, "ls-tree", "-r", "--name-only", rev).splitlines()
    return {path: git_out(root, "show", f"{rev}:{path}") for path in listed if is_measured(path)}


def measured_side(texts: dict[str, str], rules: rules_table.Rules) -> tuple[splits.Modules, set[str], dict[str, int]]:
    """One side of a split commit: its modules, the collaborators among them and each module's fan-out."""
    mods = modules_of(texts)
    collaborating = collaborators.collaborators(mods, rules)
    return mods, collaborating, {p: collaborators.fanout(m, collaborating) for p, m in mods.items()}


Grown = tuple[str, str, float | None, float]  # baseline_ratchet.Grown: kind, key, its row before (None: none), after


def split_check(root: str) -> Callable[[str, str, list[Grown]], list[str]]:
    """The split credit (the vendored splits.py) over both sides of a `Coupling: split of` commit, each measured from
    its tree: an importer's fan-out rise is admitted only where its use of X moved into the pieces."""
    def check(sha: str, value: str, grown: list[Grown]) -> list[str]:
        rules = rules_of(root)
        base, head = (measured_side(revision_texts(root, rev), rules) for rev in (f"{sha}^", sha))
        return splits.refusals(value, grown, (base[0], head[0]), (base[1], head[1]), (base[2], head[2]))
    return check


def exemptions_read(root):
    path = os.path.join(root, EXEMPTIONS)
    if not os.path.isfile(path):
        return {}
    with open(path) as f:
        return exemptions.parse(f.read())


def exemptions_keys(root):
    return set(exemptions_read(root))


def permutations(root):
    """The refused name clashes: `<dir>: name (path), ...` of every one not in ALLOWED_PERMUTATIONS."""
    out = []
    for (_, package), names in naming.permutations(modules(root).values()).items():
        if (package, frozenset(names)) not in ALLOWED_PERMUTATIONS:
            out.append(f"{package}: " + ", ".join(f"{n} ({p})" for n, p in sorted(names.items())))
    return out


def exemption_problems(root, ranges):
    """Why the exemption table is refused: a row without a date or a specific why, or a row added without its trailer."""
    out = exemptions.row_problems(exemptions_read(root))
    for rng in ranges:
        base = rng.split("..")[0]
        for trailer, kinds in TRAILERS.items():
            rose = exemptions.added_rows(kinds)
            if rose(trailers.text_at(base, EXEMPTIONS), trailers.text_at(rng.split("..")[1] or "HEAD", EXEMPTIONS)):
                out += [f"{u} (no `{trailer}:` trailer)" for u in trailers.unjustified_rises(rng, EXEMPTIONS, trailer, rose)]
    return out


def added_files(root, ranges):
    out = set()
    for rng in ranges:
        done = subprocess.run(["git", "-C", root, "diff", "--name-only", "--diff-filter=A", "--no-renames", rng],
                              capture_output=True, text=True)
        out.update(done.stdout.split())
    return out


def action_warnings(root, ranges):
    mods = modules(root, added_files(root, ranges))
    return naming.action_names(m for m in mods.values() if m.new)


def main(argv):
    root = os.path.abspath(argv[argv.index("--root") + 1]) if "--root" in argv else os.path.dirname(HERE)
    if argv[:1] == ["rows"]:
        for kind, key, value in rows(root):
            print(f"{kind}\t{key}\t{value}")
        return 0
    if argv[:1] == ["permutations"]:
        print("\n".join(permutations(root)))
        return 0
    print(__doc__)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
