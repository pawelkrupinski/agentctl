#!/usr/bin/env python3
"""devtools/vendor.py: the files agentctl keeps as BYTE-EQUAL copies of the movies repository's (the owner, 2026-11-26:
the same design rules, and agentctl never depends on movies -- so what both run is copied, not imported).

  python3 devtools/vendor.py sync    copy every file from the movies checkout (MOVIES_ROOT, default ~/projects/movies)
                                     over its copy here and record the digests in devtools/design_rules/VENDORED.tsv
  python3 devtools/vendor.py check   every copy still has its recorded digest (a local edit fails); and, where the movies
                                     checkout exists, every copy still equals movies' file (drift fails, naming the file)

VENDORED.tsv: `<copy, relative to the checkout><TAB><movies path><TAB><sha256>`. A change to a copy is made in movies
first (a movies lane), then `sync` here; an agentctl-side improvement is written down for movies, never edited in place.
Without the movies checkout `check` reads the digests only: agentctl's CI stays standalone."""
import hashlib
import os
import pwd
import shutil
import sys

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
            out.append(f"{copy}: drifted from movies' {source}: `python3 devtools/vendor.py sync`, or write down the "
                       "change for a movies lane")
    return out


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
    print(__doc__)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
