# agentctl, part `tools`: env, the tunnel, state, prepush-check.
# A real module (lib/agentctl/__init__.py MODULES): its own imports, the standard library's and MODULES' only.

import fnmatch
import os
import re
import subprocess
import sys
from agentctl.procrunner import process_runner
from agentctl.core import HOME, MAX_FOREIGN, ZERO, env_num, err, git, git_ok, remote, scratch
from agentctl.timefmt import ts
from agentctl.forge import GH_CLI
from agentctl.land import SDKMAN_BINS, tool_env
from agentctl.storetoken import secrets_bin
from agentctl.patches import ALLOW_EMPTY_TRAILER, deliberate_empty, is_empty_commit
from agentctl.tunnel import tunnel_ports
from agentctl.storestate import port_open
from agentctl.netwatch import TUNNEL_LABEL


# ---------------------------------------------------------------- queue (retired)
QUEUE_RETIRED = """NOT QUEUED: the merge queue is RETIRED (2026-10-26). There is no pre-merge CI left to batch: the landing check
is no merge requirement any more. Land every change -- identity changes included -- with `~/bin/agentctl land` (Bash
run_in_background=true): it queues the change at once, and the reconciler checks it (the basic checks plus the identity
area tests your paths select; the page tests run after landing, on Main) and pushes it. While main is red, a production change waits and lands with the batch
once main is green (`agentctl land status`)."""


def cmd_queue_retired(a):
    print(QUEUE_RETIRED)
    return 2


def default_ns():
    return "movies" if os.path.realpath(os.getcwd()).startswith(os.path.realpath(f"{HOME}/projects/movies")) else "bitcashier"


def vault_state(ns):
    """'unlocked', 'locked' or 'unknown: <why>' -- `secrets status` only; never unlock, never read a value."""
    try:
        r = process_runner().run([secrets_bin() or "secrets", "status", ns], capture_output=True, text=True, timeout=10, stdin=subprocess.DEVNULL)
    except FileNotFoundError:
        return "unknown: no `secrets` command"
    except subprocess.TimeoutExpired:
        return "unknown: `secrets status` timed out"
    out = r.stdout.strip()
    return out if out in ("unlocked", "locked") else ("locked" if "locked" in out and "unlocked" not in out else f"unknown: {out or r.stderr.strip()[:80]}")


def locked_line(ns):
    return f"vault {ns} locked: ask the user to run `! secrets unlock {ns}`"


def export_lines():
    lines = []
    bins = [b for b in SDKMAN_BINS if os.path.isdir(b)]
    if bins:
        lines.append(f'export PATH="{":".join(bins)}:$PATH"')
    jh = f"{HOME}/.sdkman/candidates/java/current"
    if os.path.isdir(jh):
        lines.append(f'export JAVA_HOME="{jh}"')
    lines.append(f'export AGENTCTL_ENV=1')
    return lines


# A FIXER HOLDING ITS CREDENTIALS NEEDS NO VAULT (2026-11-22, Tool self-tests #480: the outside-gate owner had its fix
# committed -- unsigned since 83063bb -- and ended BLOCKED ON OWNER [vault] because `env --check` refused on the locked
# movies vault). fixer_env hands a fixer agentctl's credential in its environment (the store token; the repo's own):
# land and every agentctl call use it, so a locked vault is said, never a refusal. A fix that needs a secret of its own
# (`secrets run movies <KEY> -- ...`) still meets the vault there, and asks then.
def vault_not_needed() -> bool:
    """This process is an agentctl fixer that already holds its credentials (fixer_env)."""
    return bool(os.environ.get("AGENTCTL_RED_FIXER") and os.environ.get("AGENTCTL_STORE_TOKEN"))


def env_print(ns: str) -> int:
    """`env --print`: the export lines, then the vault's one line on stderr when it stops this session."""
    print("\n".join(export_lines()))
    st = vault_state(ns)
    if st == "locked" and vault_not_needed():
        print(f"vault {ns} locked, not needed: this fixer's credentials are in its environment (a secret its fix "
              f"needs, `secrets run {ns} ...`, asks then)", file=sys.stderr)
        return 0
    if st != "unlocked":
        print(locked_line(ns) if st == "locked" else f"vault {ns} {st}", file=sys.stderr)
        return 1
    return 0


def cmd_env(a):
    ns = a.ns or default_ns()
    if not a.check:
        return env_print(ns)
    env = tool_env()
    problems, report = [], []

    def which(cmd):
        for d in env["PATH"].split(":"):
            p = os.path.join(d, cmd)
            if os.path.isfile(p) and os.access(p, os.X_OK):
                return p
        return None

    sbt = which("sbt")
    (report if sbt else problems).append(f"sbt: {sbt or 'NOT FOUND (install sdkman sbt)'}")
    java = os.path.join(env["JAVA_HOME"], "bin", "java") if env.get("JAVA_HOME") else which("java")
    jv = process_runner().run([java, "-version"], capture_output=True, text=True, timeout=30) if java else None
    if jv and jv.returncode == 0:
        report.append(f"java: {(jv.stderr or jv.stdout).splitlines()[0]}")
    else:
        problems.append(f"java: no java that runs ({java or 'none on PATH'})")
    st = vault_state(ns)
    if st == "locked" and not vault_not_needed():
        print(locked_line(ns))
        return 1
    (report if st in ("unlocked", "locked") else problems).append(
        f"vault {ns}: {st}" + (", not needed (this fixer's credentials are in its environment)" if st == "locked" else ""))
    tunnel_ok = True
    for port, what in tunnel_ports():
        ok = port_open(port)
        tunnel_ok &= ok
        (report if ok else problems).append(f"tunnel localhost:{port} ({what}): {'open' if ok else 'REFUSED'}")
    if not tunnel_ok:
        problems.append(f"  fix: launchctl kickstart -k gui/$(id -u)/{TUNNEL_LABEL}")
    gh = (GH_CLI if os.path.isabs(GH_CLI) and os.access(GH_CLI, os.X_OK) else None) or which(GH_CLI)  # gh's own auth
    (report if gh else problems).append(f"github: gh {gh or 'MISSING (' + GH_CLI + ')'}")
    for l in report:
        print(f"ok   {l}")
    for l in problems:
        print(f"FAIL {l}")
    return 1 if problems else 0


# ---------------------------------------------------------------- state
TEMPLATE = """# State: {topic}
updated: {ts}
repo:
branch:
worktree:
commits:
run ids:
waiting on:
next step:
notes:
"""


def cmd_state(a):
    path = os.path.join(scratch(), f"state-{a.topic}.md")
    if not os.path.exists(path):
        with open(path, "w") as f:
            f.write(TEMPLATE.format(topic=a.topic, ts=ts()))
        err(f"created {path}")
    text = open(path).read()
    changed = False
    for spec, append in [(s, False) for s in a.set or []] + [(s, True) for s in a.add or []]:
        field, _, value = spec.partition("=")
        field = field.strip()
        pat = re.compile(rf"^{re.escape(field)}:[ \t]*(.*)$", re.M)
        m = pat.search(text)
        if m:
            old = m.group(1).strip()
            new = f"{old}, {value}" if append and old else value
            text = text[: m.start()] + f"{field}: {new}" + text[m.end():]
        else:
            text = text.rstrip("\n") + f"\n{field}: {value}\n"
        changed = True
    if changed:
        text = re.sub(r"^updated:.*$", f"updated: {ts()}", text, count=1, flags=re.M)
        with open(path, "w") as f:
            f.write(text)
    print(f"# {path}")
    print(text, end="")
    return 0


# ---------------------------------------------------------------- prepush-check
def changed_files(repo, a, b):
    """[(status, path)] for a..b (renames split into delete + add)."""
    out = git(repo, "diff", "--name-status", "--no-renames", "-z", a, b)
    parts = out.split("\0")
    res = []
    i = 0
    while i + 1 < len(parts):
        res.append((parts[i], parts[i + 1]))
        i += 2
    return res


def renamed_away(repo, a, b):
    """Old paths that a..b MOVES (git rename detection, default 50% similarity, so a move with a small edit counts): a
    moved file is not a deletion of main's work (2026-11-07/08: a 50-file module extraction and a renamed slice
    directory were refused as 'undoing' the commits that created the old paths). A deletion with no matching add is
    no rename and still counts."""
    parts = git(repo, "diff", "-M", "--name-status", "-z", a, b).split("\0")
    old, i = set(), 0
    while i < len(parts) and parts[i]:
        if parts[i][0] == "R":
            old.add(parts[i + 1])
            i += 3
        else:
            i += 2
    return old


def blobs(repo, specs):
    """Map 'rev:path' -> blob sha or None, in one cat-file call."""
    if not specs:
        return {}
    r = git(repo, "cat-file", "--batch-check=%(objectname)", input="\n".join(specs) + "\n")
    lines = r.splitlines()
    return {s: (None if l.endswith("missing") else l.strip()) for s, l in zip(specs, lines)}


def reverted_commits(repo, mb, head):
    shas = set()
    for body in git(repo, "log", "--format=%B%x00", f"{mb}..{head}").split("\0"):
        shas.update(re.findall(r"This reverts commit ([0-9a-f]{7,40})", body))
    files = set()
    for s in shas:
        r = git(repo, "show", "--name-only", "--format=", s, check=False)
        if r.returncode == 0:
            files.update(l for l in r.stdout.splitlines() if l)
    files.update(declared_retirements(repo, mb, head))
    return files


def declared_retirements(repo, mb, head):
    """Paths named by a `Retires: <path>` trailer (final trailer block) of a commit in mb..head AND absent at head:
    a declared deletion is the author's call, not a squash undoing main. Undeclared or still-present files don't count."""
    paths = set()
    for body in git(repo, "log", "--format=%(trailers:key=Retires,valueonly,unfold)%x00", f"{mb}..{head}").split("\0"):
        paths.update(l.strip() for l in body.splitlines() if l.strip())
    return {p for p in paths if git(repo, "cat-file", "-e", f"{head}:{p}", check=False).returncode != 0}


def foreign_reverts(repo, base, head, files, depth=8, since="45.days"):
    """Files whose content at head equals an OLDER version from main's history -- i.e. the push would undo
    someone's change on main (absence counts: deleting a file main recently added)."""
    hits = []
    head_specs = [f"{head}:{p}" for _, p in files]
    head_blob = blobs(repo, head_specs)
    for (_, p), hs in zip(files, head_specs):
        hb = head_blob[hs]
        commits = git(repo, "log", f"-n{depth}", f"--since={since}", "--format=%H", base, "--", p).split()
        if not commits:
            continue
        olds = blobs(repo, [f"{c}^:{p}" for c in commits])
        cur = blobs(repo, [f"{base}:{p}"])[f"{base}:{p}"]
        for c in commits:
            ob = olds[f"{c}^:{p}"]
            if ob == hb and ob != cur:
                hits.append((p, c[:10]))
                break
    return hits


SUBSTANTIVE_LINE = re.compile(r"\w{3}")
IDENTIFIER = re.compile(r"[A-Za-z_]\w{2,}")


def diff_lines(repo, a, b, path):
    """(removed, added): the substantive lines (stripped, with a word of 3+ characters) a..b takes out of and puts into
    path; `}` and blank lines say nothing about whose content it is."""
    removed, added = set(), set()
    for ln in git(repo, "diff", "--no-renames", "-U0", a, b, "--", path).splitlines():
        if ln.startswith(("---", "+++")) or ln[:1] not in ("+", "-"):
            continue
        body = ln[1:].strip()
        if SUBSTANTIVE_LINE.search(body):
            (removed if ln[0] == "-" else added).add(body)
    return removed, added


def identifiers(lines):
    return {t for ln in lines for t in IDENTIFIER.findall(ln)}


def coherent_edit(repo, mb, head, path, commit, files, hits):
    """True when head's return of `path` to its content before `commit` is part of the push's own coherent edit, not an
    undo of `commit`: the identifiers the push takes out of `path` it also takes out of another file `commit` touched,
    where it removes lines `commit` added WITHOUT restoring what `commit` replaced there (the replacement is new content)
    -- the definition those lines referred to changed in this same push. 2026-11-19, movies 467e9026: splitting
    WebReadModel dropped its `threadPools`/`sleep` constructor parameters, so four controller specs dropped the arguments
    73aad048 had added and were byte-for-byte their pre-73aad048 selves; the push also rewrote WebReadModel.scala (whose
    `threadPools` lines 73aad048 added), which is no revert. A squash onto a moved main or a conflict resolved to an old
    side restores `commit`'s replaced lines wherever it touches them (or touches no other file of it), so it still counts."""
    removed, added = diff_lines(repo, mb, head, path)
    gone = identifiers(removed) - identifiers(added)
    if not gone:
        return False
    flagged = {p for p, _ in hits}
    pushed = {p for _, p in files}
    theirs = git(repo, "diff-tree", "--no-commit-id", "--no-renames", "--name-only", "-r", commit, check=False).stdout.split()
    for q in theirs:
        if q == path or q in flagged or q not in pushed:
            continue
        commit_removed, commit_added = diff_lines(repo, f"{commit}^", commit, q)
        push_removed, push_added = diff_lines(repo, mb, head, q)
        undone = push_removed & commit_added
        if not undone or not identifiers(undone) & gone:
            continue
        restored = push_added & commit_removed
        if push_added - commit_removed and 2 * len(restored) < max(len(commit_removed), 1):
            return True
    return False


def allowed(path, globs):
    return any(fnmatch.fnmatch(path, g) or path.startswith(g.rstrip("/") + "/") for g in globs)


def check_range(repo, base, head, allow, max_foreign):
    problems = []
    mb = git(repo, "merge-base", base, head).strip()
    files = changed_files(repo, mb, head)
    stat = git(repo, "diff", "--shortstat", mb, head).strip()
    print(f"prepush-check: {base}...{head[:12]}: {stat or 'no changes'}")
    if allow:
        outside = [p for _, p in files if not allowed(p, allow)]
        if outside:
            problems.append(f"{len(outside)} file(s) outside the allowed set {allow}: " + ", ".join(outside[:20]))
    empty = [c for c in git(repo, "rev-list", "--no-merges", f"{mb}..{head}").split()
             if is_empty_commit(repo, c) and not deliberate_empty(repo, c)]
    if empty:  # never an empty commit on main (four landed as re-queued duplicates, 2026-10-27)
        problems.append(f"{len(empty)} EMPTY commit(s) (no change: already on main?): "
                        + ", ".join(f"{c[:10]} {git(repo, 'log', '-1', '--format=%s', c).strip()[:60]}" for c in empty[:10])
                        + f". Drop them (git rebase {base}); a deliberate empty commit needs an "
                          f"`{ALLOW_EMPTY_TRAILER}: <why>` trailer")
    exempt = reverted_commits(repo, mb, head) | renamed_away(repo, mb, head)
    reverts = [(p, c) for p, c in foreign_reverts(repo, base, head, files) if p not in exempt]
    reverts = [(p, c) for p, c in reverts if not coherent_edit(repo, mb, head, p, c, files, reverts)]
    if allow:  # files in the allowed set are the agent's own: reverting them is its call
        reverts = [(p, c) for p, c in reverts if not allowed(p, allow)]
    if len(reverts) > max_foreign:
        gone = [p for p, _c in reverts if git(repo, "cat-file", "-e", f"{head}:{p}", check=False).returncode != 0]
        problems.append(
            f"{len(reverts)} file(s) would be reverted/deleted back to an older main version (max {max_foreign}) -- "
            f"a squash onto a moved main? `git rebase {remote()}/main` and re-check. "
            + ", ".join(f"{p} (undoes {c})" for p, c in reverts[:20])
            + (f". AN INTENDED DELETION (a lane removing its own scaffolding) is declared, never forced: one "
               f"`Retires: <path>` trailer per file in the commit's final trailer block (here: "
               + "; ".join(f"Retires: {p}" for p in gone[:10]) + (" ..." if len(gone) > 10 else "") + ")"
               if gone else "")
            + ". AGENTCTL_MAX_FOREIGN raises the cap for every file and is a bypass: name it in your report"
        )
    return problems


def cmd_prepush(a):
    repo = os.getcwd()
    allow = (a.allow or []) + os.environ.get("AGENTCTL_ALLOW", "").split()
    max_foreign = a.max_foreign if a.max_foreign is not None else int(env_num("AGENTCTL_MAX_FOREIGN", MAX_FOREIGN))
    problems = []
    if a.hook:
        remote = a.hook[0]
        base_ref = f"refs/remotes/{remote}/main"
        for line in sys.stdin.read().splitlines():
            parts = line.split()
            if len(parts) != 4:
                continue
            _, lsha, rref, rsha = parts
            if lsha == ZERO or not rref.startswith("refs/heads/"):
                continue  # deletes and tags: nothing to check
            base = base_ref if git_ok(repo, "rev-parse", "--verify", "-q", base_ref) else rsha
            if base == ZERO:
                continue
            problems += check_range(repo, base, lsha, allow, max_foreign)
    else:
        problems = check_range(repo, a.base, git(repo, "rev-parse", a.head).strip(), allow, max_foreign)
    for p in problems:
        print(f"prepush-check: REFUSED: {p}", file=sys.stderr)
    if problems:
        return 1
    print("prepush-check: ok")
    return 0
