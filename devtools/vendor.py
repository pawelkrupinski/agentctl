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
Without the movies checkout `check` reads the digests only: agentctl's CI stays standalone."""
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
    return sorted(changed & ({copy for copy, _s, _d in read_table(root)} | {TABLE}))


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
        theirs = os.path.join(movies, source) if movies else ""
        if theirs and os.path.isfile(theirs) and digest(theirs) != digest(path):
            out.append(f"{copy}: {DRIFTED}' {source}: `python3 devtools/vendor.py sync`, or write down the "
                       "change for a movies lane")
    return out


def signature(root=ROOT, movies=None):
    """One digest over the movies files the copies follow (`-` for one missing): it changes whenever movies moves a
    vendored file again, so a sync that failed for one state of movies is tried again for the next."""
    movies = movies_root() if movies is None else movies
    parts = []
    for _copy, source, _recorded in read_table(root):
        theirs = os.path.join(movies, source) if movies else ""
        parts.append(f"{source}\t{digest(theirs) if theirs and os.path.isfile(theirs) else '-'}\n")
    return hashlib.sha256("".join(parts).encode()).hexdigest()


def sync(root=ROOT, movies=None):
    movies = movies or movies_root()
    if not movies:
        print("vendor: no movies checkout (MOVIES_ROOT or ~/projects/movies)", file=sys.stderr)
        return 2
    rows = []
    for copy, source, _ in read_table(root):
        shutil.copyfile(os.path.join(movies, source), os.path.join(root, copy))
        rows.append((copy, source, digest(os.path.join(root, copy))))
    with open(os.path.join(root, TABLE)) as f:
        header = "".join(line for line in f if line.startswith("#"))
    with open(os.path.join(root, TABLE), "w") as f:
        f.write(header + "".join("\t".join(r) + "\n" for r in rows))
    print(f"vendor: {len(rows)} copies synced from {movies}")
    return 0


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
