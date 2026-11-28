#!/usr/bin/env python3
"""devtools/vendor.py: the files agentctl keeps as BYTE-EQUAL copies of the movies repository's (the owner, 2026-11-26:
the same design rules, and agentctl never depends on movies -- so what both run is copied, not imported).

  python3 devtools/vendor.py sync    copy every file from the movies checkout (MOVIES_ROOT, default ~/projects/movies)
                                     over its copy here and record the digests in devtools/design_rules/VENDORED.tsv
  python3 devtools/vendor.py check   every copy still has its recorded digest (a local edit fails); and, where the movies
                                     checkout exists, every copy still equals movies' file (drift fails, naming the file)

VENDORED.tsv: `<copy, relative to the checkout><TAB><movies path><TAB><sha256>`. A change to a copy is made in movies
first (a movies lane), then `sync` here; an agentctl-side improvement is written down for movies, never edited in place.
  python3 devtools/vendor.py drift   only the drift: exit 1 while the movies checkout is ahead of a copy (what the
                                     VENDOR_DRIFT condition's probe asks)
  python3 devtools/vendor.py signature   one digest of the movies files the copies follow: a new one is a new movies
                                     state to sync (a failed automatic sync is retried for it, lib/agentctl/vendordrift.py)
Without the movies checkout `check` reads the digests only: agentctl's CI stays standalone.

THE SHARED ROWS (SHARED_ROWS; 2026-11-28, condition vendor-drift.5): a table agentctl shares only some ROWS of -- the
limits, suffixes, abbreviations and idioms of movies' design-rules table, in quality/design-rules.tsv beside agentctl's
own adapters and layers -- is a vendored artifact on the same path: `drift`, `check`, `signature` and `touched` read its
shared rows, `sync` merges movies' rows in (each new one beside its neighbour, agentctl's own rows and comments kept).
Before, an unconditional test compared them: no sync wrote them, the automatic sync failed on them and every push was
refused."""
import difflib
import hashlib
import os
import pwd
import shutil
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "lib"))  # tool_git, under `python3 -I` too
import tool_git  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TABLE = os.path.join("devtools", "design_rules", "VENDORED.tsv")
DEFAULT_MOVIES = os.path.join(pwd.getpwuid(os.getuid()).pw_dir, "projects", "movies")  # the account's, not a test's $HOME
# (agentctl's table, movies' table, the row kinds the two share)
SHARED_ROWS = (("quality/design-rules.tsv", "test/resources/design-rules.tsv", ("limit", "suffix", "abbreviation", "idiom")),)


def digest(path):
    with open(path, "rb") as f:
        return hashlib.sha256(f.read()).hexdigest()


def read_table(root=ROOT):
    rows = []
    with open(os.path.join(root, TABLE)) as f:
        for line in f:
            if line.strip() and not line.startswith("#"):
                copy, source, recorded = line.rstrip("\n").split("\t")
                rows.append((copy, source, recorded))
    return rows


def movies_root():
    root = os.environ.get("MOVIES_ROOT") or DEFAULT_MOVIES
    return root if os.path.isdir(root) else ""


DRIFTED = "drifted from movies"


def drifted(root=ROOT, movies=None):
    """The copies whose movies file moved ahead (the movies checkout changed a vendored file): `sync` repairs them."""
    return [x for x in problems(root, movies) if DRIFTED in x]


def touched(root=ROOT, base="origin/main"):
    """The vendored copies (and the table) this checkout's commits change against `base`'s merge base, or None when
    git cannot say (no such base): the push's own change to what it vendors."""
    git = tool_git.Git.here(root)
    fork = git.line("merge-base", base, "HEAD")
    if not fork:
        return None
    changed = set(git.lines("diff", "--name-only", fork, "HEAD"))
    return sorted(changed & set(synced_paths(root)))


def synced_paths(root=ROOT):
    """Every file a sync writes: the copies, their table and the shared-rows tables."""
    return [copy for copy, _s, _d in read_table(root)] + [TABLE] + [ours for ours, _t, _k in SHARED_ROWS]


def drift_verdict(root=ROOT, movies=None, base="origin/main"):
    """(blocking, reported) drift. THE STRUCTURAL FIX (2026-11-27: three movies changes to a vendored file each refused
    every agentctl push): drift blocks only a push that touches a copy or the table itself (or one git cannot place
    against `base`); otherwise it is reported, and the VENDOR_DRIFT condition (lib/agentctl/vendordrift.py) has an
    agent sync it."""
    found = drifted(root, movies)
    return (found, []) if found and touched(root, base) != [] else ([], found)


def problems(root=ROOT, movies=None):
    """Why the copies are not as recorded: an edited copy, a missing one, and (with a movies checkout) a drifted one."""
    movies = movies_root() if movies is None else movies
    out = []
    for copy, source, recorded in read_table(root):
        path = os.path.join(root, copy)
        if not os.path.isfile(path):
            out.append(f"{copy}: missing")
            continue
        if digest(path) != recorded:
            out.append(f"{copy}: edited here (digest differs from VENDORED.tsv): change it in movies, then `vendor.py sync`")
        if movies_digest(movies, source) not in ("-", digest(path)):
            out.append(f"{copy}: {DRIFTED}' {source}: `python3 devtools/vendor.py sync`, or write down the "
                       "change for a movies lane")
    return out + shared_problems(root, movies)


def shared_problems(root, movies):
    """A shared-rows table whose rows differ from movies' (with a movies table to compare to)."""
    return [f"{ours}: {DRIFTED}' {theirs} (its shared {', '.join(kinds)} rows): `python3 devtools/vendor.py sync`"
            for ours, theirs, kinds, mine, wanted in shared_tables(root, movies) if shared_rows(mine, kinds) != shared_rows(wanted, kinds)]


def shared_tables(root, movies):
    """(ours, theirs, kinds, our text, movies' text) of each SHARED_ROWS table the movies checkout has."""
    return [(ours, theirs, kinds, read_text_or(os.path.join(root, ours), ""), read_text_or(movies_path(movies, theirs), ""))
            for ours, theirs, kinds in SHARED_ROWS if movies_digest(movies, theirs) != "-"]


def write_text(path, text):
    with open(path, "w") as f:
        f.write(text)


def shared_rows(text, kinds):
    return [line for line in text.splitlines() if line.split("\t")[0] in kinds]


def merge_shared(ours, theirs, kinds):
    """`ours` with its shared rows made movies' (`theirs`'): a changed or dropped row replaced in place, a new one put
    after the shared row before it; every other line of `ours` kept. Equal rows return `ours` itself."""
    lines = ours.splitlines(keepends=True)
    at = [i for i, line in enumerate(lines) if line.split("\t")[0] in kinds]
    mine, wanted = [lines[i].rstrip("\n") for i in at], shared_rows(theirs, kinds)
    if mine == wanted:
        return ours
    if not at:
        return ours + ("" if not ours or ours.endswith("\n") else "\n") + "".join(r + "\n" for r in wanted)
    before, dropped = {}, set()
    for tag, i1, i2, j1, j2 in difflib.SequenceMatcher(a=mine, b=wanted, autojunk=False).get_opcodes():
        if tag != "equal":
            dropped |= set(at[i1:i2])
            slot = at[i1] if i1 < i2 or i1 == 0 else at[i1 - 1] + 1  # a replaced row's place; an insert after its neighbour
            before.setdefault(slot, []).extend(wanted[j1:j2])
    out = []
    for i, line in enumerate(lines + [""]):
        out.extend(r + "\n" for r in before.get(i, []))
        if i not in dropped:
            out.append(line)
    return "".join(out)


def signature(root=ROOT, movies=None):
    """One digest over the movies files the copies follow (`-` for one missing): it changes whenever movies moves a
    vendored file again, so a sync that failed for one state of movies is tried again for the next."""
    movies = movies_root() if movies is None else movies
    sources = [source for _copy, source, _recorded in read_table(root)] + [theirs for _o, theirs, _k in SHARED_ROWS]
    parts = [f"{source}\t{movies_digest(movies, source)}\n" for source in sources]
    return hashlib.sha256("".join(parts).encode()).hexdigest()


def movies_digest(movies, source):
    """The digest of the movies checkout's file `source`, `-` with no checkout or no such file."""
    theirs = movies_path(movies, source)
    return digest(theirs) if os.path.isfile(theirs) else "-"


def movies_path(movies, source):
    """`source` in the movies checkout, "" (no file) without one."""
    return os.path.join(movies, source) if movies else ""


def sync(root=ROOT, movies=None):
    movies = movies or movies_root()
    if not movies:
        print("vendor: no movies checkout (MOVIES_ROOT or ~/projects/movies)", file=sys.stderr)
        return 2
    rows = copy_files(root, movies)
    write_text(os.path.join(root, TABLE), table_header(root) + "".join("\t".join(r) + "\n" for r in rows))
    merged = merged_tables(root, movies)
    write_texts(root, merged)
    print(f"vendor: {len(rows)} copies synced from {movies}" + "".join(f", the shared rows of {ours}" for ours, _t in merged))
    return 0


def copy_files(root, movies):
    """Copy every vendored file from movies over its copy; the table's new rows."""
    rows = []
    for copy, source, _ in read_table(root):
        shutil.copyfile(os.path.join(movies, source), os.path.join(root, copy))
        rows.append((copy, source, digest(os.path.join(root, copy))))
    return rows


def table_header(root):
    return "".join(line for line in read_text_or(os.path.join(root, TABLE), "").splitlines(keepends=True) if line.startswith("#"))


def write_texts(root, texts):
    for rel, text in texts:
        write_text(os.path.join(root, rel), text)


def read_text_or(path, default):
    """The text at path, or default when it cannot be read (agentctl.statefile's own rule: vendor.py stands alone)."""
    try:
        with open(path) as f:
            return f.read()
    except OSError:
        return default


def merged_tables(root, movies):
    """(table, its new text) of each shared-rows table whose rows movies changed."""
    merged = [(ours, mine, merge_shared(mine, wanted, kinds)) for ours, _t, kinds, mine, wanted in shared_tables(root, movies)]
    return [(ours, text) for ours, mine, text in merged if text != mine]


def main(argv):
    if argv[:1] == ["sync"]:
        return sync()
    if argv[:1] == ["check"]:
        found = problems()
        print("\n".join(found) or "vendor: every copy as recorded")
        return 1 if found else 0
    if argv[:1] == ["drift"]:
        found = drifted()
        print("\n".join(found) or "vendor: no copy drifted from movies")
        return 1 if found else 0
    if argv[:1] == ["signature"]:
        print(signature())
        return 0
    print(__doc__)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
