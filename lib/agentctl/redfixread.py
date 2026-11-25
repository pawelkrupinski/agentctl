# agentctl, part `redfixread`: a fix's reach: Fixes-Red commits on main, what a red's failing jobs read and whether a fix touches it.
# A real module (lib/agentctl/__init__.py MODULES): its own imports, the standard library's and MODULES' only.

import os
import re
import subprocess
import time
from typing import Any
from contextlib import suppress
from agentctl.procrunner import process_runner
from agentctl.core import attempt, err, git, git_ok, process_cache, reds_dir, remote
from agentctl.statefile import read_record, write_json
from agentctl.forge import gh_json
from agentctl.repos import red_repo_of
from agentctl.workflows import fix_workflow_jobs
from agentctl.trailers import trailer_values
from agentctl.inboxentry import inbox_write
from agentctl.redrecord import red_names, red_what
from agentctl.clones import fetch_main


# -- fixes on their way: Fixes-Red changes in the queue, and Fixes-Red commits landed on main
FIX_OPEN = ("queued", "checking", "checked", "waiting", "joined", "pushing")


# THE FIXES ARE READ FROM MAIN AS IT IS (2026-11-12, Record scrape fixtures #181, red 58f3db9e8a3d): the reconciler's
# own clone fetched main only to check or push a change, so a fix pushed from elsewhere -- 548dfb3c76, `Fixes-Red: 181`,
# landed from the laptop -- was never in it: the red was re-offered after each LANDED and put on the owner. A head of
# main the snapshot names that the clone has not got is fetched once, before the fixes are read.
RED_MAIN_REFETCH = 300
_main_fetched = process_cache({})


def red_main_current(repo, snap):
    """Fetch main into repo when the snapshot's main runs name a head commit it lacks. Returns True when it fetched."""
    if not repo or not snap or not git_ok(repo, "rev-parse", "--verify", "-q", f"refs/remotes/{remote()}/main"):
        return False  # no clone, or one that never fetched main (nothing of main's to keep up to date)
    heads = {r.get("headSha") for g in snap.get("gate") or [] for r in (g.get("runs") or [])[:3]}
    heads |= {v.get("headSha") for v in (snap.get("latest") or {}).values() if isinstance(v, dict)}
    missing = frozenset(h for h in heads if h and not git_ok(repo, "cat-file", "-e", f"{h}^{{commit}}"))
    if not missing:
        return False
    at = _main_fetched.get((repo, missing))
    if at and time.time() - at < RED_MAIN_REFETCH:  # a head main never had (a run of a commit since gone): not every tick
        return False
    _main_fetched.clear()
    _main_fetched[(repo, missing)] = time.time()
    ok, why = fetch_main(repo)
    if not ok:
        err(f"red tick: main moved past the clone and could not be fetched ({why[-160:]}): fixes read from the old main")
    return True


def landed_fix_commits(store, repo):
    """[(sha, commit time, {values}, subject)]: Fixes-Red commits on origin/main in the last 3 days -- but for one whose
    `Red-Unrelated:` trailer says it names the red only to pass the gate (`red_unrelated`): landed, it is no more a fix
    than queued. Identity model convergence #2432 (2026-10-28): four CI speed-ups carrying `Fixes-Red: 2392` beside one
    were each read as the red's fix on its way, its live fixer stopped, then refuted -- "recurring: 4 fixes refuted"."""
    if not repo:
        return []
    fmt = "%H%x09%ct%x09%(trailers:key=Red-Unrelated,valueonly,separator=%x2C)%x09%(trailers:key=Fixes-Red,valueonly,separator=%x2C)%x09%s"
    r = git(repo, "log", f"refs/remotes/{remote()}/main", "--since=3.days", f"--format={fmt}", check=False)
    out = []
    for line in (r.stdout if r.returncode == 0 else "").splitlines():
        sha, ct, unrelated, vals, subj = (line.split("\t", 4) + ["", "", "", "", ""])[:5]
        if trailer_values(vals) and not unrelated.strip():
            counted = fix_check(repo, sha, trailer_values(vals), store)[0]  # a trailer touching nothing the red reads: no fix
            if counted:
                out.append((sha, float(ct or 0), counted, subj))
    return out


def red_unrelated(repo, e):
    """A change landing on a red gate under the owner's speed-up / CI-fix rule names the red only to pass the gate:
    its `Red-Unrelated: <reason>` trailer says it is no fix for that red, so it never takes the red's fixer away."""
    for c in e.get("commits") or []:
        r = git(repo, "log", "-1", "--format=%(trailers:key=Red-Unrelated,valueonly)", c, check=False)
        if r.returncode == 0 and r.stdout.strip():
            return True
    return False


# A FIXES-RED TRAILER MUST TOUCH WHAT THE RED READS (G9, convergence-failures-2026-10-29 §4: d21ac5e81 and 1548eb448,
# unit-test speed-ups, carried `Fixes-Red: 2390;2375;2392` while touching nothing those reds' jobs read). At land time --
# the land / submit path, the queue and the batch, and the tick reading landed fixes -- a commit's `Fixes-Red: <run>`
# that names a known red is checked against the paths the red's failing job reads, derived CONSERVATIVELY from its
# workflow (the job's steps, the reusable workflows and local actions it uses, the repository scripts they name, three
# levels down, and the self-tests a runner among them finds: WHAT A SELF-TEST READS, below) plus the failing test's own sources. A commit counts as that red's fix unless it PLAINLY touches none
# of them: every path it changes is another workflow's file, documentation, the mobile apps (android/, ios/) the job
# never names, or another test spec while the job runs only named specs (`testOnly X` / `runMain X`, no whole test
# task). Unsure -- the red unknown, not a test failure, its workflow or job not found, `local` -- it counts. A trailer
# that does not count leaves the red where it is (no fix-pending, its fixer kept) and the change lands as any other
# change (it waits while the gate is red); its author is told once (`FIX NOT COUNTED`, the inbox). Cached per sha.
FIX_UNREAD_DOC = re.compile(r"(^|/)docs?/|(^|/)(README|CHANGELOG|LICENSE)[^/]*$|\.md$")
FIX_UNREAD_TOP = ("android", "ios")
FIX_WORKFLOW_FILE = re.compile(r"^\.github/workflows/[^/]+\.ya?ml$")
FIX_SPEC_FILE = re.compile(r"/src/(test|it|e2e)/\w+/(.*/)?([A-Z]\w*(Spec|Test|Tests|Suite|IT))\.(scala|java|kt)$")
FIX_BROAD_TEST = re.compile(r"(?<![\w-])(test|testQuick|testUnit\w*|Test/test|it:test|[\w-]+/test|"
                            r"testOnly\s+[^\n\"']*\*)(?![\w-])")
FIX_SBT = re.compile(r"\bsbt\b|sbt-warm|sbtw?\b")
FIX_REPO_PATH = re.compile(r"(?<![\w/.-])(?:\./)?((?:scripts|\.github|tools|bin|(?:android|ios)/scripts)/[\w./-]+)")
# PER RED, PER FAILING STEP (2026-11-15, Order independence #74): 09e482c6a (ios/KinowoUITests only, `Fixes-Red: 74`)
# fixed the run's ios red and was linked to its swift red too -- a value counted for the run when ANY of its reds read
# the commit, and every red of the run took it; the swift job reads ios/ (`swift test --package-path ios`), but its
# failing step was the script self-tests, which read ios/scripts alone. Now (a) a red takes a fix only when ITS failing
# job reads the commit (red_fix_reaches, red_fix_candidates), and (b) a job whose failing steps are known
# (red_failed_steps) reads those steps, plus the steps before them that set the environment up (an action, a setup /
# build / install step, a write to $GITHUB_ENV / $GITHUB_PATH / $GITHUB_OUTPUT) -- never its other legs. android/ or ios/
# is read WHOLE when the text builds it (a project or build file of it, a `--package-path` / `working-directory` /
# `cd` into it, a script file inside it); a directory under it named alone (run-script-tests' `ios/scripts` root) is
# read alone.
FIX_SETUP_STEP = re.compile(r"\$\{?GITHUB_(ENV|PATH|OUTPUT)\b|^\s*-?\s*uses:|\b(build|compile|assemble|install|set ?up|"
                            r"setup|generate|prepare|bootstrap)\b", re.I | re.M)
FIX_TOP_WHOLE = (r"(?:--package-path|--project-dir|working-directory:|\bcd|-C)\s+['\"]?{top}['\"]?(?![\w/.-])"
                 r"|(?<![\w-]){top}/[\w./-]*?(?:\.xcodeproj|\.xcworkspace|Package\.swift|build\.gradle(?:\.kts)?|"
                 r"settings\.gradle(?:\.kts)?|gradlew)(?![\w-])")
FIX_STEP_FAILED = ("failure", "timed_out")
# WHAT A SELF-TEST READS (2026-11-15, Tool self-tests #263: f872ecbe3, a .github/workflows/ change with `Fixes-Red: 263`,
# was FIX NOT COUNTED, though the red's step -- scripts/run-script-tests -- runs download_artifact_layout_test.py, which
# lints every .github/workflows/*.yml through the workflow_steps module it imports). A runner that finds its tests by a
# name glob ('*-test.sh', '*_test.py') reads every test it finds under the roots it names, and each test reads what it
# names, sources or imports (its siblings) -- the same three levels, plus the paths the last level names. What a found
# test reaches is DATA for android/ and ios/ (a test lays out a fake ios/Kinowo.xcodeproj): it adds the paths it names,
# never "builds the app whole", nor sbt / spec names.
FIX_TEST_GLOB = re.compile(r"""['"]\*([\w.-]*test[\w.-]*\.\w+)['"]""", re.I)
FIX_PY_IMPORT = re.compile(r"^\s*(?:from\s+([\w.]+)\s+import\b|import\s+([\w.]+(?:\s*,\s*[\w.]+)*))", re.M)
FIX_SOURCED = re.compile(r"(?:^|[\s;&|(])(?:\.|source)\s+['\"]?(?:\$\{?\w+\}?/)?([\w./-]+\.(?:sh|bash))"
                         r"|shellcheck\s+source=([\w./-]+)", re.M)


def fix_tree_files(repo: str, sha: str) -> list[str]:
    r = git(repo, "ls-tree", "-r", "--name-only", sha, check=False)
    return (r.stdout if r.returncode == 0 else "").splitlines()


def fix_show(repo: str, sha: str, path: str) -> str | None:
    r = git(repo, "show", f"{sha}:{path}", check=False)
    return r.stdout if r.returncode == 0 else None


def fix_show_many(repo: str, sha: str, paths: list[str]) -> dict[str, str]:
    """{path: text} of `paths` at commit sha, in one `git cat-file --batch` (a path missing there: left out)."""
    paths = list(dict.fromkeys(paths))
    if not paths:
        return {}
    try:
        r = process_runner().run(["git", "-C", repo, "cat-file", "--batch"], input="".join(f"{sha}:{p}\n" for p in paths)
                           .encode(), capture_output=True, timeout=120)
    except (OSError, subprocess.TimeoutExpired):
        return {}
    out, buf, i = {}, r.stdout, 0
    for p in paths:
        nl = buf.find(b"\n", i)
        if nl < 0:
            break
        head = buf[i:nl].split()
        i = nl + 1
        if len(head) == 3 and head[1] == b"blob":
            n = int(head[2])
            out[p] = buf[i:i + n].decode("utf-8", "replace")
            i += n + 1
        elif len(head) == 3:
            i += int(head[2]) + 1
    return out


def fix_job_steps(body: str) -> tuple[str, list[tuple[str | None, str]]]:
    """(the job block without its steps, [(step name or None, step text)]) of a job block, by indentation."""
    pre, items, steps_ind, item_ind = [], [], None, None
    for l in body.splitlines():
        if steps_ind is None:
            m = re.match(r"^(\s*)steps:\s*$", l)
            if m:
                steps_ind = len(m.group(1))
            else:
                pre.append(l)
            continue
        ind = len(l) - len(l.lstrip())
        if l.strip() and not l.lstrip().startswith("#") and ind <= steps_ind:  # a job key after its steps
            steps_ind, item_ind = -1, None
            pre.append(l)
            continue
        m = re.match(r"^(\s*)-\s", l)
        if steps_ind >= 0 and m and (item_ind is None or len(m.group(1)) == item_ind):
            item_ind = len(m.group(1))
            items.append([l])
        elif items and steps_ind >= 0:
            items[-1].append(l)
        else:
            pre.append(l)
    out = []
    for ls in items:
        nm = None
        for i, l in enumerate(ls):  # the step's own `name:` (never a `with:` input's)
            m = re.match(r"^\s*(-\s+)?name:\s*['\"]?([^'\"\n]+?)['\"]?\s*$", l)
            if m and (i == 0 and m.group(1) or len(l) - len(l.lstrip()) == item_ind + 2):
                nm = m.group(2).strip()
                break
        out.append((nm, "\n".join(ls)))
    return "\n".join(pre), out


def fix_step_failed(nm: str | None, txt: str, failing: list[str]) -> bool:
    """Step `nm` (its text txt) is one of the failing steps' names (an unnamed `run:` step: GitHub's "Run <command>")."""
    return any(nm == f or (nm is None and f.startswith("Run ") and f[4:].strip() and f[4:].strip() in txt)
               for f in failing)


def fix_step_text(body, failing):
    """The part of job block `body` its failing steps read (PER FAILING STEP, above): the job without its steps, the
    failing steps, and the setup steps before them; the whole block when no step of it is known to have failed."""
    pre, steps = fix_job_steps(body)
    idx = [i for i, (nm, txt) in enumerate(steps) if fix_step_failed(nm, txt, failing)]
    if not idx:
        return body
    keep = [txt for i, (_nm, txt) in enumerate(steps) if i in idx or (i < max(idx) and FIX_SETUP_STEP.search(txt))]
    return "\n".join([pre] + keep)


def red_failed_steps(d: dict[str, Any]) -> list[str]:
    """The steps that failed in red d's jobs on its newest run (by name): its `steps`, else read once through gh and
    kept (reds/runsteps/<run id>.json: a finished run's steps never change); [] when they cannot be read."""
    if d.get("steps"):
        return sorted(map(str, d["steps"]))
    rid = (d.get("last_run") or {}).get("databaseId")
    if not rid:
        return []
    path = reds_dir("runsteps", f"{rid}.json")
    rec = read_record(path)
    if rec is None:
        try:
            jobs = (gh_json(red_repo_of(d), "run", "view", str(rid), "--json", "jobs") or {}).get("jobs") or []
        except (Exception, SystemExit):
            return []
        if not jobs:
            return []
        rec = {str(j.get("name")): [str(s.get("name")) for s in j.get("steps") or []
                                    if s.get("conclusion") in FIX_STEP_FAILED and s.get("name")]
               for j in jobs if j.get("conclusion") in FIX_STEP_FAILED}
        with suppress(OSError):
            os.makedirs(os.path.dirname(path), exist_ok=True)
            write_json(path, rec)
    names = {str(j) for j in d.get("jobs") or [d.get("job")] if j}
    return sorted({s for j, ss in rec.items() if j in names for s in ss or []})


def red_workflow_file(repo: str, sha: str, d: dict[str, Any], files: list[str]) -> tuple[str, str] | None:
    """(path, text) of red d's workflow file in the tree of commit sha (files: that tree's paths), or None."""
    want = str(d.get("workflow") or "")
    for f in files:
        if not FIX_WORKFLOW_FILE.match(f):
            continue
        txt = fix_show(repo, sha, f) or ""
        m = re.search(r"^name:\s*['\"]?([^'\"\n]+)", txt, re.M)
        if (m and m.group(1).strip() == want) or os.path.basename(f) == want:
            return f, txt
    return None


def red_job_names(d: dict[str, Any]) -> list[str]:
    return [str(j) for j in d.get("jobs") or [d.get("job")] if j]


def red_failing_jobs(text: str, d: dict[str, Any]) -> list[str]:
    """The blocks of workflow text's jobs red d failed in (by job key or `name:`); [] when it names no job."""
    heads = {re.split(r" / | \(", n, maxsplit=1)[0].strip().lower() for n in red_job_names(d)}
    return [body for k, (body, nm) in fix_workflow_jobs(text).items()
            if k.lower() in heads or re.sub(r"\$\{\{.*?\}\}", "", nm).strip().lower() in heads] if heads else []


def red_job_reads(repo, sha, d):
    """What red d's failing job reads, from the tree of `sha`: {files, prefixes, text, broad, sbt} -- or None when it
    cannot be told (no workflow file of that name, nothing readable)."""
    files = fix_tree_files(repo, sha)
    wf = red_workflow_file(repo, sha, d, files)
    if not wf:
        return None
    reads, texts = {wf[0]}, []
    picked = red_failing_jobs(wf[1], d)
    failing = red_failed_steps(d) if picked else []
    picked = [fix_step_text(body, failing) for body in picked] if failing else picked
    texts.append("\n".join(picked) if picked else wf[1])  # its job's steps, or (unsure which) the whole workflow
    tree, dirs = set(files), {f.rsplit("/", 1)[0] for f in files if "/" in f}
    seen = set()

    def scan(src, t, data):
        """The paths text t (of file src; data: reached through a self-test a runner found) names, into reads; the
        files to read next, as (path, data)."""
        out = []
        for m in re.finditer(r"uses:\s*\./([\w./-]+)", t):
            p = m.group(1).rstrip("/")
            reads.add(p)
            if p not in seen:
                seen.add(p)
                out.append((p if FIX_WORKFLOW_FILE.match(p) else
                            next((p + a for a in ("/action.yml", "/action.yaml") if p + a in tree), None), data))
        named = [m.group(1).rstrip("/.") for m in FIX_REPO_PATH.finditer(t)]
        base = src.rsplit("/", 1)[0] if src and "/" in src else ""
        for m in FIX_SOURCED.finditer(t):  # a sourced or imported sibling: the file beside its user
            for n in filter(None, (m.group(1), m.group(2))):
                named += [c for c in (n, f"{base}/{n}") if c in tree]
        if src and src.endswith(".py"):
            for m in FIX_PY_IMPORT.finditer(t):
                for n in re.split(r"\s*,\s*", m.group(1) or m.group(2)):
                    c = f"{base}/{n.split()[0].replace('.', '/')}.py" if base else f"{n.split()[0].replace('.', '/')}.py"
                    if c in tree:
                        named.append(c)
        for m in FIX_TEST_GLOB.finditer(t):  # a self-test runner: every test it finds (under the roots it names)
            roots = {w.strip("'\"").removeprefix("./").rstrip("/") for w in re.findall(r"[\w./'\"-]+", t)} & dirs
            for f in files:
                if f.endswith(m.group(1)) and (not roots or any(f.startswith(r + "/") for r in roots)):
                    reads.add(f)
                    if f not in seen:
                        seen.add(f)
                        out.append((f, True))
        for p in named:
            reads.add(p)
            if p in seen or p not in tree or FIX_WORKFLOW_FILE.match(p):
                continue  # another workflow named in a script is its DATA (a lint's input): read, never run here
            seen.add(p)
            out.append((p, data))
        return out

    todo = [(None, texts[0], False)]
    for _depth in range(3):
        want_next = [x for src, t, data in todo for x in scan(src, t, data)]
        bodies = fix_show_many(repo, sha, [p for p, _ in want_next if p])
        todo = [(p, bodies[p], data) for p, data in want_next if bodies.get(p) and len(bodies[p]) < 400_000]
        texts += [t for _p, t, data in todo if not data]  # a found test's reach adds paths alone (above)
    for src, t, data in todo:  # the last level read: the paths it names are read too (never followed further)
        scan(src, t, data)
    text = "\n".join(texts)
    sbt = bool(FIX_SBT.search(text))
    return {"files": reads, "workflow": wf[0], "text": text, "sbt": sbt, "broad": sbt and bool(FIX_BROAD_TEST.search(text)),
            "named": set(re.findall(r"\b[A-Z]\w+\b", text + " " + str(d.get("line") or "") + " " + " ".join(red_job_names(d)))),
            "tops": {top: fix_top_reads(top, text, texts[0], files) for top in FIX_UNREAD_TOP}}


def fix_top_reads(top, text, steps, files):
    """What a job reads of the app directory `top` (android/, ios/), from `text` (its steps and all they name) and
    `steps` (the job's own step text): True for all of it -- it builds it (a project or build file, a `--package-path` /
    `working-directory` / `cd` into it) or one of its steps runs a script file inside it, which may -- else the set of
    the paths under it named (a directory: its subtree; a file a deeper script names: that file, its own text read)."""
    if re.search(FIX_TOP_WHOLE.format(top=re.escape(top)), text):
        return True
    tree = set(files)
    named = re.compile(rf"(?<![\w.]){re.escape(top)}/([\w./-]*[\w-])")
    if any(f"{top}/{m.group(1)}" in tree for m in named.finditer(steps)):
        return True
    return {f"{top}/{m.group(1)}".rstrip("/") for m in named.finditer(text)}


def fix_path_unread(p, rd):
    """True when the job plainly does not read path p."""
    if p in rd["files"] or any(p.startswith(f.rstrip("/") + "/") for f in rd["files"]):
        return False
    if FIX_WORKFLOW_FILE.match(p):
        return True
    if FIX_UNREAD_DOC.search(p) and p not in rd["text"]:
        return True
    top = p.split("/", 1)[0]
    if top in FIX_UNREAD_TOP:
        tr = rd["tops"][top]
        if tr is not True and not any(p == x or p.startswith(x + "/") for x in tr):
            return True
    m = FIX_SPEC_FILE.search(p)
    if m and not rd["broad"] and m.group(3) not in rd["named"]:
        return True
    return False


FIX_RULES = 3  # the reading rules' version (2: per red, per failing step; 3: what a self-test reads): a verdict cached under another is read again


def fix_changed(repo: str, sha: str) -> list[str]:
    r = git(repo, "diff-tree", "--no-commit-id", "--name-only", "-r", "--root", "-m", sha, check=False)
    return [p for p in (r.stdout if r.returncode == 0 else "").splitlines() if p]


def fix_red_verdict(repo, sha, d, changed):
    """{ok, why?}: does commit sha (its paths `changed`) touch what red d's failing job reads? Unsure counts."""
    if d.get("kind") != "test":
        return {"ok": True, "why": "not a test failure"}
    try:
        rd = red_job_reads(repo, sha, d)
    except (Exception, SystemExit):
        rd = None
    if rd is None or not changed or not all(fix_path_unread(p, rd) for p in changed):
        return {"ok": True}
    return {"ok": False, "why": (f"it touches nothing {red_what(d)} reads "
                                 f"({', '.join(changed[:4])}{' ...' if len(changed) > 4 else ''})")}


def fix_cache(sha):
    """(path, the verdicts cached for commit sha under the current FIX_RULES)."""
    path = reds_dir("fixcheck", f"{sha}.json")
    cache = read_record(path) or {}
    return path, {k: v for k, v in cache.items() if isinstance(v, dict) and v.get("rules") == FIX_RULES}


def fix_cache_put(path, cache, fresh):
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        write_json(path, {**cache, **{k: {**v, "rules": FIX_RULES} for k, v in fresh.items()}})
    except OSError:
        pass


def fix_reaches_red(repo, sha, d):
    """Commit sha is a fix THIS red can take: it touches what ITS failing job reads (fix_red_verdict), cached per sha
    and red key. A run's `Fixes-Red:` value counts for the run when any of its reds reads the commit (fix_check); each
    red of the run takes it only when it reads it itself (PER RED, PER FAILING STEP)."""
    if not repo or not sha or not d.get("key"):
        return True
    path, cache = fix_cache(sha)
    k = f"red:{d['key']}"
    if k not in cache:
        if not git_ok(repo, "cat-file", "-e", f"{sha}^{{commit}}"):
            return True  # a commit this clone lacks: unsure counts (and is not cached)
        verdict = fix_red_verdict(repo, sha, d, fix_changed(repo, sha))
        fix_cache_put(path, cache, {k: verdict})  # kept for the next tick; this one's answer is the verdict itself
        return bool(verdict.get("ok"))
    return bool(cache[k].get("ok"))


def fix_check(repo, sha, values, store):
    """(the values that count as fixes, {value: why not} for those that do not): each `Fixes-Red:` value of commit sha
    against the reds it names (see above): a value counts when any red it names reads the commit. Cached per sha; a
    value naming no known red counts (unsure) and is not cached, so a red opened later is still checked."""
    values = {str(v).strip().lstrip("#") for v in values if str(v).strip()}
    if not values or not repo:
        return values, {}
    path, cache = fix_cache(sha)
    ok, bad, todo = set(), {}, []
    for v in sorted(values):
        c = cache.get(v)
        if c is None:
            todo.append(v)
        elif c.get("ok"):
            ok.add(v)
        else:
            bad[v] = c.get("why") or "touches nothing its job reads"
    if not todo:
        return ok, bad
    # SystemExit: NoToken (the laptop's land with no token: the server checks again)
    items = attempt(lambda: [it for it in store.items("red") if it["data"].get("key")], [], catch=(Exception, SystemExit))
    changed = None
    fresh = {}
    for v in todo:
        its = [it for it in items if v in {str(x) for x in red_names(it["data"]) if x is not None}]
        if not its or v == "local":
            ok.add(v)  # unsure: the red unknown (not cached), or local
            if its:
                fresh[v] = {"ok": True, "why": "local"}
            continue
        if changed is None:
            changed = fix_changed(repo, sha)
        verdict = None
        for it in its:  # each red's own verdict kept too: the red takes the fix only on its own (fix_reaches_red)
            rv = fix_red_verdict(repo, sha, it["data"], changed)
            fresh[f"red:{it['data']['key']}"] = rv
            if verdict is None or not verdict["ok"]:
                verdict = rv
        fresh[v] = {"ok": True} if verdict["ok"] else verdict
        if verdict["ok"]:
            ok.add(v)
        else:
            bad[v] = verdict["why"]
    if fresh:
        fix_cache_put(path, cache, fresh)
        for v, c in fresh.items():
            if not v.startswith("red:") and not c.get("ok"):
                fix_not_counted_tell(repo, sha, v, c["why"])
    return ok, bad


def fix_not_counted_tell(repo, sha, v, why):
    """The author is told, once per (sha, value): the trailer does not count, and what that means for the change."""
    line = (f"FIX NOT COUNTED: {sha[:10]}'s `Fixes-Red: {v}`: {why}. The red stays where it is (its fixer keeps it); "
            f"the change lands as an ordinary one (it waits while the gate is red). Name the red only in a commit that "
            f"changes what its failing job runs or reads")
    err(line)
    try:
        cwd = repo if repo and os.path.isdir(repo) else None
        inbox_write("land", line, [line], [], 1, cwd=cwd)
    except Exception:
        pass
