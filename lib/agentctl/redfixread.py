# agentctl, part `redfixread`: a fix's reach: Fixes-Red commits on main, what a red's failing jobs read and whether a fix touches it.
# A real module (lib/agentctl/__init__.py MODULES): its own imports, the standard library's and MODULES' only.

from agentctl.clock import epoch_now
import os
import re
import subprocess
import time
from typing import Any
from contextlib import suppress
from agentctl.procrunner import process_runner
from agentctl.core import err, git, git_ok, process_cache, reds_dir, remote
from agentctl.statefile import read_record, write_json
from agentctl.repos import red_repo_of
from agentctl.trailers import commit_trailer_values
from agentctl.workflows import fix_workflow_jobs
from agentctl.clones import fetch_main
from agentctl.ciruns import ci_runs
from agentctl.statemachines import open_states


# -- fixes on their way: Fixes-Red changes in the queue, and Fixes-Red commits landed on main
FIX_OPEN = tuple(open_states("change"))  # an entry of any other status (an old `joined`) is decided


# THE FIXES ARE READ FROM MAIN AS IT IS (2026-11-12, Record scrape fixtures #181, red 58f3db9e8a3d): the reconciler's
# own clone fetched main only to check or push a change, so a fix pushed from elsewhere -- 548dfb3c76, `Fixes-Red: 181`,
# landed from the laptop -- was never in it: the red was re-offered after each LANDED and put on the owner. A head of
# main the snapshot names that the clone has not got is fetched once, before the fixes are read.
RED_MAIN_REFETCH = 300
_main_fetched = process_cache({})


def _snapshot_heads(snap: Any) -> Any:
    """The head shas of the snapshot's gate runs (the newest three each) and of its latest runs."""
    heads = {r.get("headSha") for g in snap.get("gate") or [] for r in (g.get("runs") or [])[:3]}
    return heads | {v.get("headSha") for v in (snap.get("latest") or {}).values() if isinstance(v, dict)}


def _missing_heads(repo: Any, snap: Any) -> Any:
    """The head commits of the snapshot's main runs that the clone lacks."""
    heads = _snapshot_heads(snap)
    return frozenset(h for h in heads if h and not git_ok(repo, "cat-file", "-e", f"{h}^{{commit}}"))


def red_main_current(repo, snap):
    """Fetch main into repo when the snapshot's main runs name a head commit it lacks. Returns True when it fetched."""
    if not repo or not snap or not git_ok(repo, "rev-parse", "--verify", "-q", f"refs/remotes/{remote()}/main"):
        return False  # no clone, or one that never fetched main (nothing of main's to keep up to date)
    missing = _missing_heads(repo, snap)
    if not missing:
        return False
    at = _main_fetched.get((repo, missing))
    if at and epoch_now() - at < RED_MAIN_REFETCH:  # a head main never had (a run of a commit since gone): not every tick
        return False
    _main_fetched.clear()
    _main_fetched[(repo, missing)] = epoch_now()
    ok, why = fetch_main(repo)
    if not ok:
        err(f"red tick: main moved past the clone and could not be fetched ({why[-160:]}): fixes read from the old main")
    return True


def red_unrelated(repo, e):
    """A change landing on a red gate under the owner's speed-up / CI-fix rule names the red only to pass the gate:
    its `Red-Unrelated: <reason>` trailer says it is no fix for that red, so it never takes the red's fixer away."""
    for c in e.get("commits") or []:
        if commit_trailer_values(repo, c, "Red-Unrelated"):
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


class JobSteps:
    """A job block read line by line: the lines before its `steps:` (and after them), and each step's own lines."""

    def __init__(self) -> None:
        self.pre: list[str] = []
        self.items: list[list[str]] = []
        self.steps_ind: Any = None
        self.item_ind: Any = None

    def add(self, l: str) -> None:
        if self.steps_ind is not None:
            self.in_steps(l)
            return
        m = re.match(r"^(\s*)steps:\s*$", l)
        if m:
            self.steps_ind = len(m.group(1))
        else:
            self.pre.append(l)

    def in_steps(self, l: str) -> None:
        ind = len(l) - len(l.lstrip())
        if l.strip() and not l.lstrip().startswith("#") and ind <= self.steps_ind:  # a job key after its steps
            self.steps_ind, self.item_ind = -1, None
            self.pre.append(l)
            return
        m = re.match(r"^(\s*)-\s", l)
        if self.steps_ind >= 0 and m and (self.item_ind is None or len(m.group(1)) == self.item_ind):
            self.item_ind = len(m.group(1))
            self.items.append([l])
        elif self.items and self.steps_ind >= 0:
            self.items[-1].append(l)
        else:
            self.pre.append(l)


def _step_name(ls: list[str], item_ind: Any) -> str | None:
    """The step's own `name:` (never a `with:` input's), or None."""
    for i, l in enumerate(ls):
        m = re.match(r"^\s*(-\s+)?name:\s*['\"]?([^'\"\n]+?)['\"]?\s*$", l)
        if m and (i == 0 and m.group(1) or len(l) - len(l.lstrip()) == item_ind + 2):
            return m.group(2).strip()
    return None


def fix_job_steps(body: str) -> tuple[str, list[tuple[str | None, str]]]:
    """(the job block without its steps, [(step name or None, step text)]) of a job block, by indentation."""
    job = JobSteps()
    for l in body.splitlines():
        job.add(l)
    return "\n".join(job.pre), [(_step_name(ls, job.item_ind), "\n".join(ls)) for ls in job.items]


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


def _failed_step_names(job: dict[str, Any]) -> list[str]:
    return [str(s.get("name")) for s in job.get("steps") or [] if s.get("conclusion") in FIX_STEP_FAILED and s.get("name")]


def _run_step_failures(d: dict[str, Any], rid: Any) -> dict[str, list[str]] | None:
    """{job name: its failed steps' names} of run rid, read through gh; None when they cannot be read or the run lists
    no job."""
    try:
        jobs = ci_runs().run_jobs(red_repo_of(d), rid)
    except (Exception, SystemExit):
        return None
    if not jobs:
        return None
    return {str(j.get("name")): _failed_step_names(j) for j in jobs if j.get("conclusion") in FIX_STEP_FAILED}


def _run_step_record(d: dict[str, Any], rid: Any) -> dict[str, list[str]] | None:
    """The failed steps of run rid by job: kept (reds/runsteps/<run id>.json: a finished run's steps never change), else
    read once through gh and kept; None when they cannot be read."""
    path = reds_dir("runsteps", f"{rid}.json")
    rec: Any = read_record(path)
    if rec is None:
        rec = _run_step_failures(d, rid)
        if rec is not None:
            with suppress(OSError):
                os.makedirs(os.path.dirname(path), exist_ok=True)
                write_json(path, rec)
    return rec  # type: ignore[no-any-return]


def red_failed_steps(d: dict[str, Any]) -> list[str]:
    """The steps that failed in red d's jobs on its newest run (by name): its `steps`, else the run's record
    (_run_step_record); [] when they cannot be read."""
    if d.get("steps"):
        return sorted(map(str, d["steps"]))
    rid = (d.get("last_run") or {}).get("databaseId")
    rec = _run_step_record(d, rid) if rid else None
    if rec is None:
        return []
    names = set(red_job_names(d))
    return sorted({s for j in names for s in rec.get(j) or []})


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


class JobReads:
    """The paths a job's steps read, followed through the actions, scripts and tests they name (three levels deep):
    `reads`, the files the walk reached, each file read once (`seen`)."""

    def __init__(self, repo: Any, sha: Any, files: Any) -> None:
        self.repo, self.sha, self.files = repo, sha, files
        self.tree, self.dirs = set(files), {f.rsplit("/", 1)[0] for f in files if "/" in f}
        self.reads: set[Any] = set()
        self.seen: set[Any] = set()

    def walk(self, first: Any) -> Any:
        """The texts read, `first` (the job's steps) and every file found within three levels; the last level's names
        are read too (never followed further)."""
        texts = [first]
        todo: list[Any] = [(None, first, False)]
        for _depth in range(3):
            todo = self.read_next([x for src, t, data in todo for x in self.scan(src, t, data)])
            texts += [t for _p, t, data in todo if not data]  # a found test's reach adds paths alone (above)
        for src, t, data in todo:
            self.scan(src, t, data)
        return texts

    def read_next(self, want_next: Any) -> Any:
        """(path, text, data) of each file wanted that the commit holds, unless it is huge."""
        bodies = fix_show_many(self.repo, self.sha, [p for p, _ in want_next if p])
        return [(p, bodies[p], data) for p, data in want_next if bodies.get(p) and len(bodies[p]) < 400_000]

    def scan(self, src: Any, t: Any, data: Any) -> Any:
        """The paths text t (of file src; data: reached through a self-test a runner found) names, into reads; the
        files to read next, as (path, data)."""
        out = self.actions(t, data)
        named = self.named_paths(src, t)
        out += self.found_tests(t)
        for p in named:
            self.reads.add(p)
            if p in self.seen or p not in self.tree or FIX_WORKFLOW_FILE.match(p):
                continue  # another workflow named in a script is its DATA (a lint's input): read, never run here
            self.seen.add(p)
            out.append((p, data))
        return out

    def actions(self, t: Any, data: Any) -> Any:
        """The local actions and workflows `uses:` names: (their file, data) to read next."""
        out = []
        for m in re.finditer(r"uses:\s*\./([\w./-]+)", t):
            p = m.group(1).rstrip("/")
            self.reads.add(p)
            if p not in self.seen:
                self.seen.add(p)
                out.append((p if FIX_WORKFLOW_FILE.match(p) else
                            next((p + a for a in ("/action.yml", "/action.yaml") if p + a in self.tree), None), data))
        return out

    def named_paths(self, src: Any, t: Any) -> Any:
        """The repository paths text t names, the files it sources and (of a .py file) imports among them."""
        named = [m.group(1).rstrip("/.") for m in FIX_REPO_PATH.finditer(t)]
        base = src.rsplit("/", 1)[0] if src and "/" in src else ""
        for m in FIX_SOURCED.finditer(t):  # a sourced or imported sibling: the file beside its user
            for n in filter(None, (m.group(1), m.group(2))):
                named += [c for c in (n, f"{base}/{n}") if c in self.tree]
        if src and src.endswith(".py"):
            named += self.py_imports(t, base)
        return named

    def py_imports(self, t: Any, base: Any) -> Any:
        """The sibling modules a Python text imports that the tree holds."""
        out = []
        for m in FIX_PY_IMPORT.finditer(t):
            for n in re.split(r"\s*,\s*", m.group(1) or m.group(2)):
                c = f"{base}/{n.split()[0].replace('.', '/')}.py" if base else f"{n.split()[0].replace('.', '/')}.py"
                if c in self.tree:
                    out.append(c)
        return out

    def found_tests(self, t: Any) -> Any:
        """A self-test runner: every test it finds (under the roots it names), read as data."""
        out = []
        for m in FIX_TEST_GLOB.finditer(t):
            roots = {w.strip("'\"").removeprefix("./").rstrip("/") for w in re.findall(r"[\w./'\"-]+", t)} & self.dirs
            for f in self.files:
                if f.endswith(m.group(1)) and (not roots or any(f.startswith(r + "/") for r in roots)):
                    out += self.found(f)
        return out

    def found(self, f: Any) -> Any:
        """A test the runner finds, read; (it, data) to read next the first time."""
        self.reads.add(f)
        if f in self.seen:
            return []
        self.seen.add(f)
        return [(f, True)]


def _job_steps_text(workflow_text: Any, d: Any) -> Any:
    """The text of red d's failing job's failing steps; (unsure which) the whole workflow."""
    picked = red_failing_jobs(workflow_text, d)
    failing = red_failed_steps(d) if picked else []
    picked = [fix_step_text(body, failing) for body in picked] if failing else picked
    return "\n".join(picked) if picked else workflow_text


def red_job_reads(repo, sha, d):
    """What red d's failing job reads, from the tree of `sha`: {files, prefixes, text, broad, sbt} -- or None when it
    cannot be told (no workflow file of that name, nothing readable)."""
    files = fix_tree_files(repo, sha)
    wf = red_workflow_file(repo, sha, d, files)
    if not wf:
        return None
    steps = _job_steps_text(wf[1], d)  # its job's steps, or (unsure which) the whole workflow
    job = JobReads(repo, sha, files)
    job.reads.add(wf[0])
    texts = job.walk(steps)
    text = "\n".join(texts)
    sbt = bool(FIX_SBT.search(text))
    return {"files": job.reads, "workflow": wf[0], "text": text, "sbt": sbt, "broad": sbt and bool(FIX_BROAD_TEST.search(text)),
            "named": set(re.findall(r"\b[A-Z]\w+\b", text + " " + str(d.get("line") or "") + " " + " ".join(red_job_names(d)))),
            "tops": {top: fix_top_reads(top, text, steps, files) for top in FIX_UNREAD_TOP}}


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


