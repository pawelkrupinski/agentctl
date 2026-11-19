# agentctl, part `waiting`: waiting.
# Runs inside agentctl's one namespace, after the parts before it in lib/agentctl/__init__.py's PARTS (it uses their names).

# ---------------------------------------------------------------- waiting: production changes held while main is red
# The owner (2026-10-26): "when waiting for main to go green, merge all waiting changes into one waiting branch (always
# use the same name) and merge it when main is green". While the gate is red, `land` puts a production change onto the
# local branch `waiting` (worktree <main clone>-wt-waiting) instead of being refused: rebased onto it, the local checks
# run on the combined tip, `WAITING <sha> (n changes queued on waiting)`. One processor (`waiting run`, detached, locked)
# watches the gate; once main is green it rebuilds `waiting` on origin/main, runs the hook's dry run, pushes the whole
# branch in ONE push, deletes it, and every waiting `land` goes on to its LANDED and WATCH. Tools-only commits and
# Fixes-Red fixes never wait (the hook lets them land while red). Replaces the merge queue.
WAITING_BRANCH = "waiting"
WAITING_DIR = None  # a test's override; else waiting_dir(): under the state root, resolved at each use


def waiting_dir():
    return WAITING_DIR or state_root("waiting")
PRODUCTION_GLOBS = ("*/src/main/*", "conf/*", "*/conf/*", "build.sbt", "project/*", "infra/nix/*", "infra/kubernetes/*",
                    "Dockerfile")
WAITING_AWAIT_TIMEOUT = 24 * 3600
WAITING_HELP = """The processor (one loop of ticks, launchd net.bitcashier.agentctl-waiting) and its queue: every landed change is
an item -- queued, checking, checked, pushing, landed; or waiting while main is red. At green the waiting changes are ONE
batch item: its tip assembled in a check tree (main plus the changes, Fixes-Red first), checked once (pre-push
--fast-only, a fresh sbt server; a stall retried once, then HELD), pushed in one push; a failing batch is bisected and
only the confirmed culprit rejected. No `waiting` branch. Every verdict goes to `agentctl inbox`.

  agentctl land status                          # the changes, the batch, the gate, the processor (hands it the token)
  agentctl land cancel <id>                     # drop a change
  agentctl land hold|release                    # stop / restart taking batches (changes still queue)
  agentctl tick [--once]                        # the processor
  agentctl tick service install|uninstall|status   # it as the launchd agent net.bitcashier.agentctl-waiting
The old names (`waiting status|pause|resume|cancel|run|service`) answer until 2026-11-05, each naming its new one."""


def is_production_path(p):
    """movies' pre-push is_production_path: what reaches a pod or a fleet host (the rest lands while red)."""
    return any(fnmatch.fnmatch(p, g) for g in PRODUCTION_GLOBS)


GATE_UNREAD = "unread"  # gate_reds' member when the gate could not be read: never green, and no Fixes-Red matches it
gate_unread_why = [""]


def gate_reds(repo):
    """The ids (run numbers and run ids) of the gating workflows' red runs on main; empty when green or not gated.
    When the gate cannot be read: {GATE_UNREAD}, which every caller takes as RED -- a production change waits, the
    processor takes no batch (the owner, 2026-10-27: an unread gate is never green; at 08:05Z a launchd processor with
    no token read it as green, and the hook, with no token either, skipped its own gate: a push onto red #2047)."""
    try:
        o_r = slug(repo)
        if o_r not in GATING:
            return set()
        reds = set()
        for name, decided, _newer, _streak in gate_runs(o_r):
            if decided and decided.get("conclusion") != "success":
                reds |= {str(decided.get("number")), str(decided.get("databaseId"))} | episode_names(o_r, name, decided)
        gate_unread_why[0] = ""
        return reds
    except (Exception, SystemExit) as e:  # SystemExit: NoToken
        gate_unread_why[0] = str(e).replace("agentctl: ", "")[:200]
        err(f"could not read the gate ({gate_unread_why[0]}): taken as RED until it is read")
        return {GATE_UNREAD}


# A FIX MATCHES THE RED IT FIXES, NOT ONE RUN NUMBER (2026-11-05). Every push that lands while main is red (tools-only
# commits land freely) starts a new run failing the same way, so a fix naming the run it was written against was
# stranded WAITING once the next run went red: a red owner's fixes named Main #2164, then #2172, while the gate read
# #2173, then #2174; main stayed red ~5.5 h until the fix was re-trailered by hand, twice. Now a `Fixes-Red:` counts
# when it names ANY run of the CURRENT red EPISODE: the workflow's failed runs on main since its last green (the red
# streak) that share a failure signature with its newest red run -- the names red_units links a red item by. A run of
# an older episode (main went green since) never counts. movies' pre-push judges the same rule from gh's run list and
# failed job names (episode_reds there).
def episode_names(o_r, name, decided):
    """The names (numbers and ids) of workflow `name`'s red streak runs on main that fail the way its newest decided red
    run does (red_units' names); empty when its signatures cannot be read -- then only the newest run counts."""
    try:
        sigs = run_sigs(o_r, decided, name)
        units = red_units(o_r, name, decided, _gate_lists.get(name) or [], sigs, True)
    except (Exception, SystemExit) as e:  # SystemExit: NoToken
        err(f"could not read {name} #{decided.get('number')}'s failures ({str(e)[:120]}): only that run counts as its red")
        return set()
    return set().union(*(set(u.get("names") or ()) for u in units))


def waiting_supported(repo):
    """A repo hook, if any, must have --checks-only (its checks without the red gate) to check a waiting tip."""
    hook = precheck_hook(repo)
    if not hook:
        return True
    with open(hook) as f:
        return "--checks-only" in f.read()


def waiting_commits(repo, base, head):
    return git(repo, "rev-list", "--reverse", f"{base}..{head}").split()


def must_wait(repo, commits, reds):
    """A production commit with no Fixes-Red: naming a red run waits; tools-only and declared fixes go (the hook's rule)."""
    for c in commits:
        paths = git(repo, "diff-tree", "--no-commit-id", "--name-only", "-r", "--root", "-m", c).split("\n")
        if not any(p and is_production_path(p) for p in paths):
            continue
        if not (commit_fixes(repo, c) & reds):  # a trailer that does not count (fix_check) is no fix: it waits
            return True
    return False


def waiting_state(repo):
    common = os.path.realpath(git(repo, "rev-parse", "--path-format=absolute", "--git-common-dir").strip())
    d = os.path.join(waiting_dir(), re.sub(r"[^A-Za-z0-9._-]", "_", common.strip("/")))
    os.makedirs(os.path.join(d, "entries"), exist_ok=True)
    clone = os.path.dirname(common) if os.path.basename(common) == ".git" else common
    return d, clone, f"{clone}-wt-waiting"


def waiting_entries(d, open_only=True):
    out = []
    for p in sorted(glob.glob(os.path.join(d, "entries", "*.json"))):
        try:
            with open(p) as f:
                e = json.load(f)
        except (OSError, ValueError):
            continue
        if not open_only or e.get("status") == "waiting":
            out.append(e)
    return sorted(out, key=lambda e: e["seq"])


ENTRY_FINAL = ("landed", "rejected", "superseded", "cancelled", "joined", "migrated")  # migrated: the laptop's, to ci-runner


def save_entry(d, e):
    """The entry, written atomically. A final status on disk is never overwritten by a racing writer's non-final one
    (audit S2: `supersede` raced the checker's save): the disk's status and verdict stand."""
    p = os.path.join(d, "entries", f"{e['id']}.json")
    disk = read_record(p) or {}
    if disk.get("status") in ENTRY_FINAL and e.get("status") not in ENTRY_FINAL:
        e.update({k: disk[k] for k in ("status", "why", "decided", "superseded_by", "landed_sha") if k in disk})
    with open(p + ".tmp", "w") as f:
        json.dump(e, f, indent=1)
    os.replace(p + ".tmp", p)


def wlog(d, msg):
    with open(os.path.join(d, "processor.log"), "a") as f:
        f.write(f"{ts()} {msg}\n")


# NEVER AN EMPTY COMMIT (2026-10-27). An agent queued a commit, queued it again, and both entries ran: the later one's
# commit had already landed through the earlier one, its rebase (a cherry-pick with --keep-redundant-commits) left an
# EMPTY commit, and that was pushed -- four times on movies main (317fba9b3, 1fddd8951, 1d554f051, 693e1be78/82f4d9f55).
# Now every rebuild of an entry drops a commit that is patch-equivalent to its new base, or empty after the pick; an
# entry left with nothing is LANDED (already on main via <commit>); re-queueing supersedes the older entry; and the
# squash guard refuses any push carrying an empty commit unless it is a deliberate one with an `Allow-Empty:` trailer.
ALLOW_EMPTY_TRAILER = "Allow-Empty"


def deliberate_empty(repo, c):
    """A commit made empty on purpose: `git commit --allow-empty` with an `Allow-Empty: <why>` trailer."""
    return bool(git(repo, "log", "-1", f"--format=%(trailers:key={ALLOW_EMPTY_TRAILER},valueonly)", c,
                    check=False).stdout.strip())


def is_empty_commit(repo, c):
    """A non-merge commit whose tree is its parent's (a root commit is never empty here)."""
    parents = git(repo, "rev-list", "--parents", "-n", "1", c).split()[1:]
    if len(parents) != 1:
        return False
    return git(repo, "rev-parse", f"{c}^{{tree}}").strip() == git(repo, "rev-parse", f"{parents[0]}^{{tree}}").strip()


def patch_id(repo, c):
    diff = git(repo, "show", "--format=", "--no-color", "--no-ext-diff", c, check=False).stdout
    if not diff.strip():
        return None
    r = run_cmd(["git", "patch-id", "--stable"], cwd=repo, input=diff)
    return (r.stdout.split() or [None])[0]


def equivalent_on(repo, c, upstream, limit=400):
    """The commit of `upstream` (since its merge-base with c) that carries c's patch, or None (`git cherry`'s test,
    naming the commit), else its content twin (content_twin: same subject, author, date and changed lines)."""
    pid = patch_id(repo, c)
    if not pid:
        return None
    mb = git(repo, "merge-base", c, upstream, check=False).stdout.strip()
    rng = f"{mb}..{upstream}" if mb else upstream
    for u in git(repo, "rev-list", "--no-merges", "-n", str(limit), rng, check=False).stdout.split():
        if patch_id(repo, u) == pid:
            return u
    return content_twin(repo, c, upstream)


def upstream_patch_ids(repo, since_commit, upstream, limit=400):
    """{patch-id: commit} of `upstream`'s non-merge commits since its merge-base with `since_commit`, in one pass."""
    mb = git(repo, "merge-base", since_commit, upstream, check=False).stdout.strip()
    rng = f"{mb}..{upstream}" if mb else upstream
    log = git(repo, "log", "-p", "--no-merges", "--no-color", "--no-ext-diff", "-n", str(limit), "--format=commit %H",
              rng, check=False).stdout
    if not log.strip():
        return {}
    r = run_cmd(["git", "patch-id", "--stable"], cwd=repo, input=log)
    return {pid: c for pid, c in (l.split() for l in r.stdout.splitlines() if len(l.split()) == 2)}


# A DOCS CONFLICT IS NO VERDICT ON A CHANGE (2026-11-04: of the week's 42 conflict rejections, 6 were docs/markdown
# only -- docs/investigations/2026-11-01-flip-unification.md alone rejected five changes, each author appending a
# section at the same end). A pick or rebase whose every conflicted file is prose (UNION_SAFE) is resolved by keeping
# both sides (`git merge-file --union`: the base's lines once, ours then theirs) and goes on; any other conflicted
# file, or a delete/modify conflict, is the author's as before. Generated fixtures (derivation-hash TSVs) are never
# union-merged: both sides' rows would be stale.
UNION_SAFE = ("docs/*", "*.md")


def union_safe(path):
    return any(fnmatch.fnmatch(path, g) for g in UNION_SAFE)


def unmerged(wt):
    return git(wt, "diff", "--name-only", "--diff-filter=U", check=False).stdout.split()


def conflict_stages(wt, files):
    """{file: {1: base, 2: ours, 3: theirs}} of conflicted files, or None when one is added or deleted on one side."""
    stages = {}
    for f in files:
        got = {}
        for n in (1, 2, 3):
            r = git(wt, "show", f":{n}:{f}", check=False)
            if r.returncode != 0:  # added or deleted on one side: no merge of that
                return None
            got[n] = r.stdout
        stages[f] = got
    return stages


def union_resolve(wt, files=None):
    """Resolve wt's conflicted files (or `files` of them) by keeping both sides when every one is UNION_SAFE and has
    all three stages: the files resolved (staged), or None (nothing touched)."""
    files = unmerged(wt) if files is None else files
    if not files or not all(union_safe(f) for f in files):
        return None
    stages = conflict_stages(wt, files)
    if stages is None:
        return None
    import tempfile
    for f, got in stages.items():
        with tempfile.TemporaryDirectory() as td:
            paths = []
            for n in (2, 1, 3):  # merge-file <current> <base> <other>
                q = os.path.join(td, str(n))
                with open(q, "w") as fh:
                    fh.write(got[n])
                paths.append(q)
            r = subprocess.run(["git", "merge-file", "-p", "--union", *paths], capture_output=True, text=True)
            if r.returncode < 0 or r.returncode > 127:
                return None
        with open(os.path.join(wt, f), "w") as fh:
            fh.write(r.stdout)
        git(wt, "add", "--", f)
    return files


# GENERATED FILES ARE NO OVERLAP (2026-11-19 ~06:05Z: six movies changes from parallel refactoring lanes -- agreement,
# f2, datalayer3, resolver2-b1, enrichment2, resolver2 -- sat in single file, each `held: its check waits for <prev>`
# because each edits test/resources/coupling-baseline.tsv, several complexity- and length-baseline.tsv too: every
# coupling or complexity lane landed strictly one at a time, hours of serial checks). Those files are GENERATED (one
# row a class, package or method; `scripts/coupling-report --update`, `scripts/quality-report --update`), and the
# ratchets in the pre-push hook recompute them over the tree they check, so a combined tree's right content is "the
# rows regenerated over it" -- and two changes touching DISJOINT rows do not conflict at all. A repo registers them in
# red-repos.json (`generated`: {paths, regen: its own --update command, timeout}); a registered file is
# - NO OVERLAP: the hold (OVERLAPPING CHANGES ARE CHECKED AS ONE, queue.py), backlog-batch membership, the conflict a
#   queued change is told of, `no longer applies on main` and the push's full-recheck rule all leave it out -- a real
#   source both change (TestWiring.scala) still holds the younger as before;
# - RESOLVED, when a rebase, a batch's assembly or land's own rebase conflicts in it: its rows merged three-way (each
#   row keyed by its columns but the value: a row one side changed takes that side's value; disjoint rows always
#   merge), else -- a row both sides changed differently -- the repo's own regen command run in the tree, which IS
#   the combined tree at that commit (the other files picked), and its files staged into the commit being picked (on
#   the laptop: ci-runner's reconciler has no JVM, its checks run on CI, so there a same-row conflict is MAIN'S ROW,
#   below; the push's own rebase, under the landing lock, never runs one: a new verify round instead). The
#   commit keeps its message and trailers (a `Coupling:`/`Complexity:` rise still carries its own), and the hook's
#   ratchet judges the result as it judges any baseline; a member so resolved that its batch then fails is checked
#   alone (batch_alone), never rejected for it;
# - MAIN'S ROW, PROVISIONALLY, where no regen command runs (2026-11-19 06:58Z: lane F2b's queued change NOT LANDED --
#   "no longer applies on origin/main 2cfa0ee95e" -- because c3922ba26 had re-measured the very baseline rows its own
#   baseline commit changed, and ci-runner's reconciler, the server role, has no JVM: a rebase by hand and a new place
#   at the back of the queue, for a file no agent edits by hand): a row both sides changed keeps the side already in
#   the tree (main's, or the earlier batch members'), every other row merged as above; the files are named
#   (`provisional`) and the checks judge the combined tree -- the ratchet passes a fall not yet recorded and fails a
#   rise, naming the rows, and a failure is reported as the change's own with the regen command to run;
# - a SET table (a key on many lines: coupling-fanout-refs.tsv's `<class><TAB><target>`) is merged line by line;
# - else, the merge and the regen failing (a side not a table, the command's exit, its timeout), the member
#   is left out of the batch and checked alone (batch_alone `regen`, the files and why in the log), held as before
#   behind the change it overlaps. Tests: tests/test_generated.py, tests/test_queue_sim.py GeneratedOverlapTest.
GENERATED_REGEN_TIMEOUT = 1200  # s a generated file's regen command may run before the conflict is the author's ($AGENTCTL_GENERATED_REGEN_TIMEOUT)
_generated_slugs = process_cache({})  # repo path -> its GitHub slug (None: unknown)


def generated_registry(repo):
    """The repo's registered generated files (red-repos.json `generated`, by the repo's GitHub slug): [] when none."""
    key = os.path.realpath(repo or ".")
    if key not in _generated_slugs:
        try:
            _generated_slugs[key] = slug(repo) if repo else None
        except (SystemExit, Exception):  # no remote, a path that is gone: nothing registered
            _generated_slugs[key] = None
    conf = red_repo_conf(_generated_slugs[key]) if _generated_slugs[key] else None
    return (conf or {}).get("generated") or []


def generated_entry(repo, path):
    """The registry entry a path is generated by, or None."""
    return next((g for g in generated_registry(repo) if any(fnmatch.fnmatch(path, p) for p in g["paths"])), None)


def without_generated(repo, paths):
    """paths less the repo's registered generated files (GENERATED FILES ARE NO OVERLAP)."""
    reg = generated_registry(repo)
    return [p for p in paths if not reg or not generated_entry(repo, p)] if paths else paths


def table_parse(text, whole_line=False):
    """(header lines, [(key, line)]) of a generated table: `#` and blank lines head it, every other line a row keyed
    by all its tab-separated columns but the last (whole_line: by the whole line -- a SET table, one fact a line, like
    coupling-fanout-refs.tsv's `<class><TAB><target>`); None when a header line follows a row or a key repeats."""
    head, rows, keys = [], [], set()
    for line in text.splitlines(True):
        bare = line.rstrip("\n")
        if not bare.strip() or bare.startswith("#") or "\t" not in bare:
            if rows:
                return None
            head.append(line)
            continue
        key = bare if whole_line else bare.rsplit("\t", 1)[0]
        if key in keys:
            return None
        keys.add(key)
        rows.append((key, line if line.endswith("\n") else line + "\n"))
    return head, rows


def valued_table(text):
    """Whether a table has a VALUE column: a row whose last column is a number (a baseline's); a set table has none."""
    return any(re.fullmatch(r"-?\d+(\.\d+)?", line.rsplit("\t", 1)[-1].strip())
               for line in text.splitlines() if "\t" in line and not line.startswith("#"))


def three_way(b, o, t, prefer_ours=False):
    """The merged side of one value (b base, o ours, t theirs), or KeyError when both changed it differently (prefer_ours:
    then ours)."""
    if o == t or t == b:
        return o
    if o == b:
        return t
    if prefer_ours:
        return o
    raise KeyError


def rows_merge(base, ours, theirs, prefer_ours=False):
    """A generated table merged row by row (GENERATED FILES ARE NO OVERLAP): ours' order, theirs' added rows after the
    row they follow there; None when a row (or the header) changed on both sides differently (prefer_ours: then ours'
    row), or a side cannot be read as a table. A table whose keys repeat and that has no value column (valued_table)
    is read as a SET table (each line its own row: present or not, which never conflicts)."""
    parsed = [table_parse(x) for x in (base, ours, theirs)]
    if None in parsed and not any(valued_table(x) for x in (base, ours, theirs)):
        parsed = [table_parse(x, whole_line=True) for x in (base, ours, theirs)]
    if None in parsed:
        return None
    (bh, br), (oh, orow), (th, trow) = parsed
    try:
        head = three_way(bh, oh, th, prefer_ours)
        b, o, t = dict(br), dict(orow), dict(trow)
        merged = {k: three_way(b.get(k), o.get(k), t.get(k), prefer_ours) for k in set(b) | set(o) | set(t)}
    except KeyError:
        return None
    order = [k for k, _ in orow if merged.get(k) is not None]
    theirs_order = [k for k, _ in trow]
    for i, k in enumerate(theirs_order):
        if merged.get(k) is None or k in order:
            continue
        before = next((p for p in reversed(theirs_order[:i]) if p in order), None)
        order.insert(order.index(before) + 1 if before else 0, k)
    text = "".join(head) + "".join(merged[k] for k in order)
    return text if ours.endswith("\n") or not ours else text.rstrip("\n")


def regen_here():
    """Whether this host runs a repo's regen commands: not the server role -- ci-runner's reconciler has no JVM (its
    checks run on CI), and a doomed command would hold a tick step for minutes."""
    return host_role() != "server"


def regenerate(wt, files, regenerated=None, provisional=None):
    """Resolve wt's conflicted generated files over the tree as it stands (the combined tree): each merged row by row,
    else its registry entry's regen command run in wt and its files staged -- or, where no regen command runs (the
    server role), merged row by row keeping the tree's (ours: main's) side of a row both sides changed, the file named
    in `provisional` (MAIN'S ROW, PROVISIONALLY). None: resolved (staged, appended to `regenerated`); else why not, in
    words."""
    stages = conflict_stages(wt, files)
    if stages is None:
        return "added or deleted on one side"
    pending = []

    def take(f, text):
        with open(os.path.join(wt, f), "w") as fh:
            fh.write(text)
        git(wt, "add", "--", f)
    for f in files:
        text = rows_merge(stages[f][1], stages[f][2], stages[f][3])
        if text is None:
            pending.append(f)
            continue
        take(f, text)
    for g in generated_registry(wt):  # one run of each command, however many of its files conflict
        mine = [f for f in pending if generated_entry(wt, f) is g]
        if not mine:
            continue
        if not regen_here():
            for f in mine:
                text = rows_merge(stages[f][1], stages[f][2], stages[f][3], prefer_ours=True)
                if text is None:
                    return f"{f} cannot be read as a table, and the server role runs no regen command"
                take(f, text)
                if provisional is not None:
                    provisional.append(f)
            continue
        git(wt, "checkout", "--ours", "--", *mine, check=False)  # no conflict markers in what the command reads
        limit = g.get("timeout") or env_num("AGENTCTL_GENERATED_REGEN_TIMEOUT", GENERATED_REGEN_TIMEOUT)
        cmd = " ".join(shlex.quote(x) for x in g["regen"])
        try:
            r = subprocess.run(g["regen"], cwd=wt, capture_output=True, text=True, timeout=limit, env=tool_env(),
                               stdin=subprocess.DEVNULL)
        except subprocess.TimeoutExpired:
            return f"a row both sides changed, and `{cmd}` ran past {limit:.0f} s"
        except OSError as ex:
            return f"a row both sides changed, and `{cmd}` could not run ({ex.strerror or ex})"
        if r.returncode != 0:
            last = ((r.stderr or r.stdout or "").strip().splitlines() or [""])[-1][:200]
            return f"a row both sides changed, and `{cmd}` exited {r.returncode}" + (f": {last}" if last else "")
        git(wt, "add", "-A", "--", *g["paths"], check=False)
        left = sorted(set(unmerged(wt)) & set(mine))
        if left:
            return f"`{cmd}` left {', '.join(left)} unresolved"
        stray = git(wt, "diff", "--name-only", check=False).stdout.split()  # what else it wrote: never picked along
        if stray:
            git(wt, "checkout", "--", *stray, check=False)
    if regenerated is not None:
        regenerated.extend(f for f in files if f not in regenerated)
    return None


def conflict_resolve(wt, regen=True, notes=None, regenerated=None, provisional=None):
    """Resolve wt's conflicted files when each is UNION_SAFE prose (both sides kept) or a registered generated file
    (regenerate; only with regen): the files resolved (staged), or None (nothing it can resolve; (files, why) appended
    to `notes` when generated files' resolution failed)."""
    files = unmerged(wt)
    if not files:
        return None
    gen = [f for f in files if generated_entry(wt, f)]
    prose = [f for f in files if f not in gen]
    if not all(union_safe(f) for f in prose) or (gen and not regen):
        return None
    if prose and union_resolve(wt, prose) is None:
        return None
    if gen:
        why = regenerate(wt, gen, regenerated, provisional)
        if why:
            if notes is not None:
                notes.append((gen, why))
            return None
    return files


def pick(wt, commits, dropped=None, regen=True, regenerated=None, provisional=None):
    """Cherry-pick commits onto wt's HEAD, one at a time: None, or the conflicting files (the pick aborted, HEAD back
    where it was). A commit already in HEAD's history (patch-equivalent), or empty once picked, is DROPPED, never kept
    as an empty commit: appended to `dropped` as (commit, the commit of HEAD carrying it, or None). A deliberate empty
    commit (an Allow-Empty: trailer) is kept.

    PATCH-EQUIVALENT COMMITS ARE DROPPED BEFORE THE PICK, not only when the pick comes out empty: a change whose patch
    main carries already, but whose lines main has since changed back (a revert), applies cleanly and non-empty --
    and landed the same change a second time (simplification-2026-10-31 item 10: three subjects landed 2-3 times).

    A conflict only in prose or registered generated files is resolved and the pick goes on (conflict_resolve; regen
    False: generated files left conflicting); a generated file's failed resolution is named, with why, in the files."""
    start = git(wt, "rev-parse", "HEAD").strip()
    on_head = upstream_patch_ids(wt, commits[0], start) if commits else {}
    for c in commits:
        before = git(wt, "rev-parse", "HEAD").strip()
        via = on_head.get(patch_id(wt, c) or "")
        if via and not deliberate_empty(wt, c):
            if dropped is not None:
                dropped.append((c, via))
            continue
        r = git(wt, "cherry-pick", "--allow-empty", "--keep-redundant-commits", c, check=False)
        notes = []
        if r.returncode != 0 and conflict_resolve(wt, regen, notes, regenerated, provisional):
            r = git(wt, "-c", "core.editor=true", "cherry-pick", "--continue", check=False)
        if r.returncode != 0:
            files = unmerged(wt)
            git(wt, "cherry-pick", "--abort", check=False)
            via = equivalent_on(wt, c, before) if files else None
            if via:  # its patch is already there, and a later change to the same lines conflicts with it again
                git(wt, "reset", "--hard", "-q", before)
                if dropped is not None:
                    dropped.append((c, via))
                continue
            git(wt, "reset", "--hard", "-q", start)
            noted = {f for gen, _why in notes for f in gen}
            return ([f for f in files if f not in noted] + [f"{', '.join(gen)} (generated: {why})" for gen, why in notes]
                    if files else [r.stderr.strip()[:200]])
        if is_empty_commit(wt, "HEAD") and not deliberate_empty(wt, c):
            git(wt, "reset", "--hard", "-q", before)
            if dropped is not None:
                dropped.append((c, equivalent_on(wt, c, before)))
    return None


def apply_entry(d, wt, e, base, regen=True):
    """Pick the entry's commits onto wt's HEAD (base, or base plus earlier entries): ('ok', None), ('conflict', files),
    ('landed', via) or ('duplicate', via). A commit already on MAIN (base's history) is dropped from the entry for good
    (a partly landed entry keeps only the rest); one only an earlier entry carries is left out of this build alone (that
    entry may still be rejected). Nothing left of it on main: 'landed' -- the caller says LANDED, never pushes empty."""
    first = git(wt, "rev-parse", "HEAD").strip() == base
    dropped, regenerated, provisional = [], [], []
    conflict = pick(wt, e["commits"], dropped, regen, regenerated, provisional)
    if regenerated:  # resolved by regeneration in this build (GENERATED FILES ARE NO OVERLAP): a failure is not its own
        e["regenerated"] = sorted(regenerated)
    else:
        e.pop("regenerated", None)
    if provisional and not conflict:
        e["provisional"] = sorted(set(provisional))
        wlog(d, f"{e['id']}: {', '.join(e['provisional'])}: a row both sides changed and this host runs no regen "
                "command: kept the tree's row (main's), for the checks' ratchet to judge over the combined tree")
    else:
        e.pop("provisional", None)
    if conflict:
        return "conflict", conflict
    if not dropped:
        return "ok", None
    on_main = [(c, v) for c, v in dropped
               if (v and git(wt, "merge-base", "--is-ancestor", v, base, check=False).returncode == 0) or (not v and first)]
    if on_main:
        gone = {c for c, _v in on_main}
        e["commits"] = [c for c in e["commits"] if c not in gone]
        e.setdefault("dropped", []).extend([[c, v] for c, v in on_main])
        save_entry(d, e)
        wlog(d, f"{e['id']}: {len(on_main)} commit(s) already on {remote()}/main, dropped: "
                + ", ".join(f"{c[:10]} (via {v[:10] if v else 'an empty pick'})" for c, v in on_main))
        if not e["commits"]:
            return "landed", next((v for _c, v in reversed(on_main) if v), None)
    if not [c for c in e["commits"] if c not in {x for x, _v in dropped}]:  # all of it is on an earlier entry
        return "duplicate", next((v for _c, v in reversed(dropped) if v), None)
    return "ok", None


def already_landed(d, e, via):
    """Every commit of the entry is on main already (an earlier entry, or someone, landed it): LANDED, nothing pushed."""
    queue_verdict(d, e, "landed", f"LANDED {e['head']} (already on main via {via or 'an equivalent commit'}): "
                                  "nothing left to push", code=0, landed_sha=via, already=True)


def supersede(d, e, by, why):
    """The entry gives way to `by` (a newer entry of the same worktree and branch, or one carrying the same change): its
    checker, if running, is stopped; said in the inbox."""
    line = f"SUPERSEDED {e['id']} by {by}: {why}"
    set_change(d, e, "superseded", event=change_event(e, line, code=0), superseded_by=by, why=why, decided=ts())
    cancel_entry_remote(e, f"its queue entry was superseded by {by}")
    h = read_record(entry_owned(e["id"]).record) or {}
    if lock_held(entry_owned(e["id"]).lockpath) and h.get("pid") and int(h["pid"]) != os.getpid():
        try:
            os.kill(int(h["pid"]), signal.SIGTERM)  # its checker: its CI run is cancelled with it
        except OSError:
            pass
    wlog(d, line)


def tail_of(out, n=25):
    return "\n  ".join([l for l in out.splitlines() if l.strip()][-n:])


def batching(repo):
    """Whether land batches here (every change joins `waiting`): a repo hook with --checks-only (movies)."""
    hook = precheck_hook(repo)
    return bool(hook) and waiting_supported(repo)


def fixes_of(repo, commits):
    """The run ids/numbers the commits' Fixes-Red: trailers name -- those that count as fixes (fix_check)."""
    out = set()
    for c in commits:
        out |= commit_fixes(repo, c)  # only those that count: touching what the red's job reads (fix_check)
    return out


def batch_order(repo, entries, reds):
    """The order a batch is built in: a fix for the current red first, then (main red) what may land while red -- tools
    only, or every commit carrying a Fixes-Red for a red run -- then the rest; by join order within each."""
    facts = gate_facts(reds)

    def rank(e):
        if reds and classify_change(e, facts)["gate_fix"]:  # its KIND: a fix of the gate's red (changekind.py)
            return 0
        if reds and not must_wait(repo, e["commits"], reds):
            return 1
        return 2
    return sorted(entries, key=lambda e: (rank(e), e["seq"]))


def supersede_older_waiting(d, worktree, branch, eid):
    """Waiting again replaces this worktree and branch's older waiting entry: only the newest (eid) lands."""
    for old in waiting_entries(d):
        if old["id"] != eid and same_change_source(old, worktree, branch):
            supersede(d, old, eid, f"{worktree} waits again; only the newest entry lands")


def note_waiting(d, log_path, head, eid):
    """The change's `=== waiting` line in its log, and its verdict line `WAITING <head> (entry <id>, n waiting)` (n:
    the waiting entries, it among them, saved yet or not)."""
    n = len({x["id"] for x in waiting_entries(d)} | {eid})
    with open(log_path, "a") as f:
        f.write(f"=== waiting {ts()} {head} as {eid} ({n} waiting)\n")
    return f"WAITING {head} (entry {eid}, {n} waiting)"


def join(repo, main_ref, sh, log_path, a, head=None, worktree=None, branch=None):
    """The change (its own checks passed) waits for main to turn green: a `waiting` entry (design §14.1: no branch, no
    worktree -- the batch is computed at green, in a check tree). Prints `WAITING <head> (entry <id>, n waiting)`."""
    if head is None:
        rc, head = sync(repo, main_ref, sh, getattr(a, "release_hold", False))
        if rc is not None:
            return rc
    d, _clone, _wt = waiting_state(repo)
    commits = waiting_commits(repo, f"{remote()}/main", head)
    branch = branch_of(repo) if branch is None else branch
    with open(os.path.join(d, "seq.lock"), "a+") as seq_lock:  # a seq of its own, beside a land queueing meanwhile
        fcntl.flock(seq_lock, fcntl.LOCK_EX)
        seq = max([e["seq"] for e in waiting_entries(d, open_only=False)] + [0]) + 1
        eid = f"{dt.datetime.now().strftime('%Y%m%d-%H%M%S')}-{os.path.basename(worktree or repo)}-{seq}"
        supersede_older_waiting(d, worktree or repo, branch, eid)
        e = {"id": eid, "seq": seq, "worktree": worktree or repo, "branch": branch, "head": head, "commits": commits,
             "fixes": sorted(fixes_of(repo, commits)), "status": "waiting", "created": ts(), "tip": head,
             "follow": bool(getattr(a, "follow", False))}
        save_entry(d, e)
    print(note_waiting(d, log_path, head, e["id"]), flush=True)
    a.waiting = e
    spawn_waiting_processor(repo)
    offer_token(repo)
    return 0
