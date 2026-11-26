# agentctl, part `queuerecheck`: what a recheck runs: out of the build's reach, minimal rechecks, per-file lints, one side compiled against the other.
# A real module (lib/agentctl/__init__.py MODULES): its own imports, the standard library's and MODULES' only.

import os
import subprocess
from typing import Any
from agentctl.procrunner import process_runner
from agentctl.core import git, process_cache
from agentctl.repos import batching, precheck_hook
from agentctl.buildview import BUILD_VIEW_PATHS, build_view_change, read_build
from agentctl.waitingdir import waiting_entries
from agentctl.queue import (
    SELECTION_PATHS, forces_full_recheck, hook_query, named_reader, range_paths, selection_at, selection_change)
from agentctl.generated import without_generated
from agentctl.queueknobs import knob_queue


# OUT OF THE BUILD'S REACH (the owner, 2026-11-21: "narrow the 27 'build' rechecks (about 24 min each)"; 26 of them in
# 11-19..21 were BUILD-AWARE's view moving, every view holding e2e, worker and repochecks -- the `basic` lines every sbt
# check runs -- 12 of those 26 changes to scripts, docs, workflows, infra or the mobile apps, 5 of them touching no file
# the build compiles, reads or names). A change none of whose paths the build reaches -- none under an sbt project's directory, the build
# itself (a root *.sbt, project/), the shared resources (test/, data/, conf/), and none the build's own files name --
# (a root Markdown file -- CLAUDE.md -- is no input of the build) runs under the new build only the checks that read its files, which are the lint and script self-tests its paths
# select; the verify round runs exactly those, over the combined tree, under the new build (with every test config
# compiled). Every other check -- the basic lines, the area tests -- reads none of its files, and main's own landing
# check ran them under the new build: the verify round, not the full one. A file the change has that names the build
# file still forces the full round (named_reader, queue.py).
BUILD_SHARED_DIRS = ("test", "data", "conf")


def _in_build(p: Any, dirs: Any) -> Any:
    """Whether path p is the build's, or compiled by it: a root file but Markdown, a project directory's, a source."""
    return bool(("/" not in p and not p.endswith(".md")) or p.split("/", 1)[0] in dirs or p.split("/")[1:2] == ["src"]
                or p.endswith((".scala", ".java")))


def _build_files(tree: Any, tip: Any) -> Any:
    """The files at tip the sbt build reads: project/ (but target directories) and the root .sbt files."""
    return [f for f in git(tree, "ls-tree", "-r", "--name-only", tip, "--", "project", check=False).stdout.split()
            if "/target/" not in f"/{f}"] + [f for f in git(tree, "ls-tree", "--name-only", tip, check=False).stdout.split()
                                             if f.endswith(".sbt")]


def out_of_build_reach(tree, tip, paths):
    """Whether no path of the change is one the sbt build compiles or reads at tip (OUT OF THE BUILD'S REACH); False when
    the build cannot be read."""
    b = read_build(tree, tip)
    if b is None or not paths:
        return False
    dirs = (set(b["projects"]) - {"."}) | set(BUILD_SHARED_DIRS) | {"project"}
    if any(_in_build(p, dirs) for p in paths):
        return False  # a root file but Markdown (build.sbt, the Dockerfile ...) is the build's or read by it; source too
    files = _build_files(tree, tip)
    if not files:
        return False
    names = [a for p in sorted({os.path.basename(p) for p in paths}) for a in ("-e", p)]
    r = git(tree, "grep", "-l", "-F", *names, tip, "--", *files, check=False)
    return r.returncode == 1  # 0: the build names one of them (it may read it); else unreadable


def _build_view_moves(tree: Any, tip: Any, full_tip: Any, change_paths: Any, builds: Any, new: Any) -> Any:
    """How the build file's change moves the check the change passed, when its selection does not: a script, workflow or
    test reading the build file by name (production code names it in comments only), else the build's view moving -- unless
    the change is out of the build's reach."""
    named = named_reader(tree, tip, builds, [p for p in change_paths if "/src/main/" not in p])
    if named:
        return f"{named[1]} names {named[0]}"
    return None if out_of_build_reach(tree, tip, change_paths) else build_view_change(tree, full_tip, tip, change_paths, new)


def _build_file_reason(tree: Any, tip: Any, full_tip: Any, change_paths: Any, build: Any) -> str | None:
    """Why the build files main changed (`build`) force the change to be checked fully again -- they move what the change's
    paths select, or what the build file's view of them is -- or None."""
    builds = [p for p in build if BUILD_VIEW_PATHS.match(p)]
    new = selection_at(tree, tip, tip, change_paths) if builds else None
    moves = selection_change(tree, full_tip, tip, change_paths, new=new)
    if not moves and builds:
        moves = _build_view_moves(tree, tip, full_tip, change_paths, builds, new)
    return f"they change {(builds or build)[0]}, and {moves}" if moves else None


def _both_touch(tree: Any, landed_paths: Any, change_paths: Any) -> str | None:
    """The files both sides changed (a regenerated one aside: the ratchet judges it), in words, or None."""
    same = without_generated(tree, sorted(set(landed_paths) & set(change_paths)))
    if not same:
        return None
    return f"both touch {same[0]}" + (f" (+{len(same) - 1} more)" if len(same) > 1 else "")


def _deleted_read(tree: Any, base: Any, moved: Any, tip: Any, landed_paths: Any, change_paths: Any) -> str | None:
    """A file one side deletes that the other side's files still name, in words, or None."""
    sides = ((range_paths(tree, base, moved, deleted=True), change_paths, "they delete"),
             (range_paths(tree, moved, tip, deleted=True), landed_paths, "it deletes"))
    for deleted, readers, whose in sides:
        hit = named_reader(tree, tip, deleted, readers)
        if hit:
            return f"{whose} {hit[0]}, which {hit[1]} names"
    return None


def recheck_reason(tree, base, moved, tip, change_paths, full_tip=None):
    """(commits main moved by, why the change must be FULLY checked again or None) -- THE RULE (queue.py), of what landed in
    base..moved against the change's paths (moved..tip, the rebased change; tree checked out at tip). full_tip: the tip
    the full check passed, for SELECTION-AWARE."""
    n = len(git(tree, "rev-list", f"{base}..{moved}", check=False).stdout.split())
    landed_paths = range_paths(tree, base, moved)
    build = sorted(p for p in landed_paths if forces_full_recheck(p))
    other = [p for p in build if p not in SELECTION_PATHS and not BUILD_VIEW_PATHS.match(p)]
    if other:
        return n, f"they change {other[0]}"
    why = _build_file_reason(tree, tip, full_tip, change_paths, build) if build else None
    return n, why or _both_touch(tree, landed_paths, change_paths) or _deleted_read(tree, base, moved, tip, landed_paths, change_paths)


# MINIMAL RECHECKS (the owner, 2026-11-20: "batch those as well ... so that rechecks are kept to a minimum"; that
# morning fix-red-2717 sat through a full re-check, then a verify round, after main moved by 12 commits). THE RULE (queue.py)
# decides between a FULL round and the combined tree's verify round; before this, a change main moved under always paid
# at least the verify round. Now a third answer: its VERDICT IS KEPT -- rebased and pushed, no round -- when no check the
# change's paths select reads a path main changed since its last check, by the hook's own path selection, read at the
# rebased tip: its compile plan (each test config compiles its module and every module upstream of it, so the two
# sides' plans meet where one side's code is compiled against the other's), its whole-tree ratchets (`--coupling-plan`,
# `--quality-plan`: any path either selects reads the whole production tree), its plan's checks (an area such as
# `identity`, `scripttests`, `drift`, ... -- but not the checks that read only the files they are handed,
# PER_FILE_CHECKS, whose shared file THE RULE already sees, nor `sbt`, which the plans above spell out), and its lint
# plan's path-selected specs (the standing `*` rows, which every push runs, read no path in particular). Two sides
# selecting one such check -> the verify round, as before, the check named (`read_overlap`); the rebase changing the
# change's own diff (its patch id: a merged generated row, a three-way pick) -> the verify round too
# (`own_diff_changed`). A hook without `--plan` and `--compile-plan` cannot say what its checks read: the verify round.
# The accepted risk is a standing lint or a basic line reading both sides (Main catches it), the risk the module-overlap
# rule takes. The push then skips the plan the change's checks passed (PREPUSH_SKIP) and the hook judges the red gate.
# Every decision -- a full round, a verify round, a kept verdict -- and why is kept on the change (`rechecks`) and
# printed by `land status <id>`.
READ_MODES = ("--compile-plan", "--coupling-plan", "--quality-plan", "--plan", "--lint-plan",
              "--script-test-plan")  # cheapest first
# PER TEST (2026-11-20): a hook answering --script-test-plan names the script self-tests a path selects; its `scripttests`
# plan word -- shared by any two script changes, 33 of 131 rechecks in 3 days -- is then read per test instead
SCRIPT_TESTS_CHECK, SCRIPT_TEST_MODE = "scripttests", "--script-test-plan"
WHOLE_TREE_MODES = ("--coupling-plan", "--quality-plan")
# PER-FILE LINTS (movies, 2026-11-21, landing-speed proposal 1): a hook answering --per-file-lints names, of the lint
# specs some paths select, those judging each file alone that the paths select by what the spec reads -- never by its
# own code. Both sides naming one is no overlap of its lint plan: the two meet in its verdict only through a file both
# change, which THE RULE rechecks in full, or through its code, which then selects it not "alone".
PER_FILE_MODE = "--per-file-lints"
PER_FILE_CHECKS = frozenset({"shellcheck", "actionlint", "eslint"})


def hook_text_modes(hook, flags=(PER_FILE_MODE,)):
    """The flags the hook (a path) answers, of flags: a flag is never asked unless the hook has it."""
    try:
        with open(hook or "") as f:
            text = f.read()
    except OSError:
        return []
    return [m for m in flags if f"{m})" in text]


def hook_modes(hook):
    """The dry-run modes of READ_MODES the hook (a path) answers."""
    return hook_text_modes(hook, READ_MODES)


def compiled_path(path):
    """Whether sbt compiles path or builds with it: a source file, or the build (a root *.sbt, project/)."""
    return path.endswith((".scala", ".java", ".sbt")) or path.startswith("project/")


SOURCE_CONFIGS = {"test": "Test", "it": "It", "page": "Page"}  # <module>/src/<dir>/ -> the config compiling it


def _config_word(parts: Any) -> str:
    """`<module>/<Config>/compile` of a path's split parts: its source dir's config, else the module's Test config."""
    config = SOURCE_CONFIGS.get(parts[2], "Test") if len(parts) > 3 and parts[1] == "src" else ""
    return f"{parts[0]}/{config}/compile"


def own_configs(plan, paths):
    """The configs of a compile plan (`<module>/<Config>/compile` words) standing for the paths' OWN code, as compiled:
    `<module>/src/test|it|page/` its config; `src/main/` (and fixtures) its module's Test config, which compiles main
    with it. A compiled path under no module of the plan (the build, project/) is every config of it; a config the plan
    does not name, every config of its module."""
    out = set()
    for p in paths:
        if not compiled_path(p):
            continue
        parts = p.split("/")
        module = {w for w in plan if w.split("/", 1)[0] == parts[0]} if len(parts) > 1 else set()
        if not module:
            return set(plan)
        word = _config_word(parts)
        out |= {word} if word in module else module
    return out


# ONE SIDE COMPILED AGAINST THE OTHER (2026-11-21: in 3 days, kept_verdicts 0 of every landing; 24 of the verify rounds
# run after main moved outside the change's files named the compile plan, most of them `e2e/Test/compile`): a compile
# plan names the configs compiling against the paths -- a module's own and those of every module downstream of it -- so
# ANY two code changes meet in e2e, which depends on everything, though neither side's code is compiled against the
# other's there (worker and web are independent; e2e's own sources are unchanged). The two sides' compiles interact only
# where one side's plan holds the config compiling the other side's OWN code (own_configs): common's plan holds
# web/Test/compile, which compiles a web change; worker's plan holds none of web's; a repochecks spec's plan holds
# worker/It/compile (worker's integration specs compile against repochecks' specs), which a worker/src/main change is
# not compiled in alone (worker/Test/compile stands for it, and the spec's plan does not hold it).
def compile_meets(hook, tree, mine, theirs):
    """The configs where one side's code is compiled against the other's, as words; None when a plan is unreadable."""
    a, b = hook_query(hook, tree, "--compile-plan", mine), hook_query(hook, tree, "--compile-plan", theirs)
    if a is None or b is None:
        return None
    a, b = set(a), set(b)
    return (a & own_configs(b, theirs)) | (b & own_configs(a, mine))


def check_reads(hook, tree, mode, paths, standing, per_file):
    """The checks of one dry-run mode that paths select, as words (MINIMAL RECHECKS); None when unreadable."""
    out = hook_query(hook, tree, mode, paths)
    if out is None:
        return None
    if mode in WHOLE_TREE_MODES:
        return {"the whole production tree"} if out else set()
    if mode == "--plan":
        return set(out) - per_file
    if mode == "--lint-plan":
        return set(out) - standing
    return set(out)


def _reads_main(mode: str, both: Any) -> str | None:
    """`both`, the checks both sides select by mode, in words (at most three named), or None when there are none."""
    both = sorted(both)
    if not both:
        return None
    return (f"its {mode.lstrip('-')} reads what main changed: {', '.join(both[:3])}"
            + (f" (+{len(both) - 3} more)" if len(both) > 3 else ""))


class OverlapRead:
    """What the two sides' checks read, mode by mode (read_overlap's rounds): a method per kind of read -- the hook's own
    plans (`read_plans`), the compile plan, and the checks of any other mode."""

    def __init__(self, hook: Any, tree: Any, modes: Any, change_paths: Any, landed_paths: Any) -> None:
        self.hook, self.tree, self.modes = hook, tree, modes
        self.change_paths, self.landed_paths = change_paths, landed_paths
        self.standing: set[str] = set()
        self.per_file: frozenset[str] = frozenset()
        self.per_file_mode = False

    def read_plans(self) -> str | None:
        """The standing lint rows, the checks that judge each file alone, and whether the hook names per-file lints; why
        the lint plan cannot be read, or None."""
        modes = self.modes
        if "--lint-plan" in modes:
            standing = hook_query(self.hook, self.tree, "--lint-plan", [])
            if standing is None:
                return "its lint plan cannot be read"
            self.standing = set(standing)
        # a hook that cannot say what its whole-tree ratchets read: its `sbt` check is one check both sides may share
        self.per_file = PER_FILE_CHECKS | ({"sbt"} if all(m in modes for m in WHOLE_TREE_MODES) else set()) \
            | ({SCRIPT_TESTS_CHECK} if SCRIPT_TEST_MODE in modes else set())
        self.per_file_mode = PER_FILE_MODE in hook_text_modes(self.hook)
        return None

    def overlap(self, mode: str) -> str | None:
        """Why the change's checks of this mode must run again, or None."""
        return self.compile_overlap() if mode == "--compile-plan" else self.check_overlap(mode)

    def compile_overlap(self) -> str | None:
        both = compile_meets(self.hook, self.tree, self.change_paths, self.landed_paths)
        if both is None:
            return "its compile-plan cannot be read"
        return _reads_main("--compile-plan", both)

    def check_overlap(self, mode: str) -> str | None:
        mine = check_reads(self.hook, self.tree, mode, self.change_paths, self.standing, self.per_file)
        theirs = check_reads(self.hook, self.tree, mode, self.landed_paths, self.standing, self.per_file) if mine else set()
        if mine is None or theirs is None:
            return f"its {mode.lstrip('-')} cannot be read"
        both = mine & theirs
        if mode == "--lint-plan" and both and self.per_file_mode:
            alone = [hook_query(self.hook, self.tree, PER_FILE_MODE, ps) for ps in (self.change_paths, self.landed_paths)]
            if None in alone:
                return f"its {PER_FILE_MODE.lstrip('-')} cannot be read"
            both -= set(alone[0]) & set(alone[1])  # PER-FILE LINTS: judged alone, a spec meets both sides only by a shared file or its code
        return _reads_main(mode, both)


def read_overlap(tree, landed_paths, change_paths):
    """Why the change's checks must run again over what main changed meanwhile (landed_paths): a check both sides'
    paths select, in words -- or None when none is (its verdict is kept). MINIMAL RECHECKS, above; tree is checked out
    at the rebased tip."""
    hook = precheck_hook(tree)
    modes = hook_modes(hook)
    if "--plan" not in modes or "--compile-plan" not in modes:
        return "the hook has no plan saying what its checks read"
    if not landed_paths or not change_paths:
        return None
    reading = OverlapRead(hook, tree, modes, change_paths, landed_paths)
    unreadable = reading.read_plans()
    if unreadable:
        return unreadable
    for mode in modes:
        why = reading.overlap(mode)
        if why:
            return why
    return None


def own_diff_changed(tree, base, tip, moved, new_head):
    """Whether rebasing the change (base..tip) onto moved (moved..new_head) changed its own diff: their patch ids
    differ (MINIMAL RECHECKS: a generated row merged, a three-way pick). True when either cannot be read."""
    def pid(a, b):
        diff = git(tree, "diff", "--no-renames", "--binary", a, b, check=False)
        if diff.returncode != 0:
            return None
        try:
            r = process_runner().run(["git", "patch-id", "--stable"], cwd=tree, input=diff.stdout, capture_output=True,
                               text=True, timeout=60)
        except (OSError, subprocess.TimeoutExpired):
            return None
        return (r.stdout.split() or [""])[0] if r.returncode == 0 else None
    old, new = pid(base, tip), pid(moved, new_head)
    return old is None or new is None or old != new


RECHECK_LOG_MAX = 12  # the rounds and kept verdicts a change keeps (`land status <id>` prints them)


def recheck_logged(e, kind, why, main, at):
    """e's recheck log with one more decision: a `full` round, a `verify` round or a `kept` verdict, why, on which
    main, at `at`."""
    return (list(e.get("rechecks") or [])[-(RECHECK_LOG_MAX - 1):]
            + [{"at": int(at), "kind": kind, "main": (main or "")[:10], "why": str(why)[:300]}])


_kept_known = process_cache({})  # (entry id, tip, checked base, main) -> verdict_kept's answer


def verdict_kept(repo, e, main):
    """Whether checked change e would keep its verdict on main (MINIMAL RECHECKS) as the repo reads it now -- no
    shared file, no forcing path, no check reading what landed -- so it pushes alone rather than waiting for a push
    batch's verify round. Its push decides again, at its rebased tip (_push_change)."""
    base, tip = e.get("checked_base"), e.get("tip")
    if not base or not tip or not main or base == main:
        return False
    key = (e["id"], tip, base, main)
    if key not in _kept_known:
        landed, mine = range_paths(repo, base, main), range_paths(repo, base, tip)
        _kept_known[key] = bool(landed) and not any(forces_full_recheck(p) for p in landed) \
            and not without_generated(repo, sorted(set(landed) & set(mine))) \
            and read_overlap(repo, landed, mine) is None
    return _kept_known[key]


def same_change_source(e, worktree, branch):
    """Whether entry e came from this worktree and branch (an entry from before branches were recorded: the worktree)."""
    if os.path.realpath(e.get("worktree") or "") != os.path.realpath(worktree or ""):
        return False
    return not e.get("branch") or not branch or e["branch"] == branch


def queued_entries(d: str) -> list[dict[str, Any]]:
    return [e for e in waiting_entries(d, open_only=False) if e.get("status") in ("queued", "checking", "checked", "pushing")]


def queue_mode(repo, a):
    """Whether land queues: a repo whose hook can check a change apart (movies), unless --follow / --no-watch
    (scripts) or AGENTCTL_QUEUE=0. A dispatched red fixer queues too (RED FIX LANDS THROUGH THE QUEUE, below)."""
    if getattr(a, "follow", False) or getattr(a, "no_watch", False):
        return False
    return knob_queue() != "0" and batching(repo)
