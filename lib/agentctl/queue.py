# agentctl, part `queue`: the land queue and submission.
# A real module (lib/agentctl/__init__.py MODULES): its own imports, the standard library's and MODULES' only.

import os
import re
import subprocess
import time
from typing import Any, Iterable
from agentctl.procrunner import process_runner
from agentctl.statefile import read_text_or
from agentctl.core import print_error, git, git_ok
from agentctl.gitrepo import commit_known
from agentctl.repos import precheck_hook
from agentctl.land import tool_env


# A PUSH, FETCH OR TUNNEL BLIP IS NO VERDICT ON THE CHANGE (audit #6, 2026-10-28: entry 67 "the push failed (pre-push
# hook or remote)" was a tunnel error in its tail; a fixer GAVE UP on it): such a failure, a stalled push, a lock that
# never came free and main moving under every round re-queue the entry (retry_after, at most QUEUE_INFRA_RETRIES),
# its `attempts` reset (a checker restart after an infra re-queue is no failure of the change).
# TRANSIENT_PUSH: networksignatures.py's PUSH_SIGNATURES (the network's rows and the push's own).


# Main moved between a change's check and its push: FULLY re-checked only when what landed meanwhile can change what the
# full check judged (the owner, 2026-10-27: memo-key -41 passed runs 3341 and 3350, then main moved and it was checked a
# third time). FILE GRANULARITY (the owner, 2026-11-16, "but always recompile and unit test": at module granularity
# nearly every change shared worker or scrapers with what landed, and changes looped 3-4 full rounds of 15-20 min, some
# over two test files in worker/src/test). THE RULE, judged against the base of the change's last FULL check:
#   - what landed changes the build or the hook (RECHECK_ALWAYS, scripts/ci/precommit-share.sh included: it picks what
#     each CI share of the check runs) -- but not the hook's own self-tests (HOOK_SELF_TEST: scripts/hooks/*-test.sh,
#     *-test-lib.sh, *_test.py; the owner, 2026-11-16): no landing check runs one but by its `scripttests` check, for
#     a change selecting it, like any script's test, so they get the verification round below -- or
#   - a path both sides change (added, modified or deleted; a rename counts as both of its paths), or
#   - a path one side deletes (a rename's old path included) whose file name a file the other side changes names
#     (`git grep -F <basename>` over those files at the rebased tip: a script, fixture or test reading it by name; a
#     Scala reference by class name the combined compile below catches)
# -> a new FULL round. Otherwise no full re-check, but ALWAYS the COMBINED-TREE VERIFICATION (VERIFY_MODE): the rebased
# tip checked once more, on the CI tier like any check, with the hook's `--verify-only <full base>..<tip>` -- every test
# config compiled, the lint and fast specs, and the unit tests of every module either side touches -- then pushed with
# the change's own checks skipped at the push (PREPUSH_SKIP: the hook's plan for its paths; the red gate still runs).
# A hook without --verify-only gets the full round, as before the rule. A rebase conflict is NOT LANDED, as ever.
# SELECTION-AWARE (the owner, 2026-11-17: 17 full rounds of ~40 min since 11-16 06:00, nearly all for a hook or registry
# change): when every forcing path that landed only steers WHAT the check selects (SELECTION_PATHS: the hook, its lint
# registry, local-tests.txt), the hook is asked what it selects for the change's paths at the tip the full check passed
# (full_tip) and at the rebased tip: its --plan, --compile-plan, --unit-plan and --lint-plan, and local-tests.txt's
# lines for every area the plan runs (`basic` with sbt). The verify round runs the NEW hook's compile plan (every test
# config), lint plan and unit plan over full_base..tip, a superset of the change's paths, so what those add is run there
# (the added selection, and only it, on top of the verification); a check --plan adds, or an area line local-tests.txt
# adds, it does not run: then the full round. Removals need nothing. An unreadable selection or no full_tip recorded:
# the full round. BUILD-AWARE (the owner, 2026-11-17: 6 full rounds of ~22 min that day for build.sbt / project/
# churn): what landed changes a root *.sbt or project/ -> the same selection test, AND the build as the change's
# modules see it (buildview.py's build_view_change: build.sbt's build-wide statements, the change's and its selection's module
# definitions with everything they name -- settings helpers, configurations, the projects they dependsOn, the
# project/ members -- at full_tip and at the rebased tip) unchanged, and no file of the change naming the changed build
# file (`git grep -F <basename>`: a script or test reading it) -> the verify round, which compiles every test config
# under the new build; anything changed, unreadable, or the change touching the build itself -> the full round.
# SHARE-AWARE (the owner, 2026-11-18, item 6: 1242 min of full rounds in a week for precommit-share.sh or the registry):
# scripts/ci/precommit-share.sh is a SELECTION_PATH too -- it only splits the hook's selection over the CI check's jobs
# -- and every selection read also holds the union of what its shares (SHARES) run for the change: a check one of
# them adds forces the full round, as a check --plan adds does. A file BOTH sides change stays a full round, these too.
# MODULE OVERLAP (the same decision): no shared file but an sbt module both sides touch -> the verify round plus the
# change's AREA tests (verify_areas: a check/verifyareas- branch; module_overlap), where the tree's precommit-share.sh
# runs that mode; the risk of a cross-change break the area tests miss is accepted (Main catches it).
# The other forcing paths (scripts/hooks/* beyond SELECTION_PATHS, hooks-path.sh, ...) stay a full round.
RECHECK_ALWAYS = re.compile(r"^([^/]+\.sbt|project/.*|scripts/hooks/.*|scripts/hooks-path\.sh"
                            r"|scripts/tool-conventions\.sh"
                            r"|scripts/ci/precommit-share\.sh)$")
HOOK_SELF_TEST = re.compile(r"^scripts/hooks/[^/]+(-test\.sh|-test-lib\.sh|_test\.py)$")
VERIFY_MODE = "--verify-only"
SHARE_SCRIPT = "scripts/ci/precommit-share.sh"
SELECTION_PATHS = frozenset({"scripts/hooks/pre-push", "scripts/hooks/lint-registry.tsv", "scripts/hooks/local-tests.txt",
                             SHARE_SCRIPT})
SELECTION_MODES = ("--plan", "--compile-plan", "--unit-plan", "--lint-plan")
# what each share of the CI check runs for the change (precommit-share.sh `checks <share>`, the hook's own PREPUSH_LIST
# under the share's environment), read on a check/<id> ref -- the change's own checks, its range the change since main.
# No `oldrefs` since 2026-11-21: the old-ref compile runs on Main, after landing, and the script refuses the name.
SHARES = ("rest", "identity", "plan")
SHARE_KEY = "shares"
# MODULE OVERLAP (the owner, 2026-11-18): a verify round whose two sides touch one sbt module also runs that change's
# AREA tests -- a check/verifyareas-<full base>-<id> branch, which a precommit-share.sh naming the mode runs with the
# area shares (identity) in --checks-only over <full base>..HEAD; one that does not name it gets the plain verify round
VERIFY_AREAS_MODE = "verifyareas"
VERIFY_RUNS_ADDED = frozenset({"--compile-plan", "--unit-plan", "--lint-plan"})  # the verify round runs what these add


def range_paths(cwd, a, b, deleted=False):
    """The paths a..b changes (both sides of a rename); deleted: only those it deletes (a rename's old path too)."""
    flt = ["--diff-filter=D"] if deleted else []
    return [p for p in git(cwd, "diff", "--name-only", "--no-renames", *flt, a, b).splitlines() if p]


def hook_query(hook, tree, flag, paths):
    """The hook's dry-run answer for paths on stdin (`--plan`, `--compile-plan`), as words; None when unreadable."""
    try:
        r = process_runner().run([hook, flag], cwd=tree, input="".join(f"{p}\n" for p in paths), capture_output=True,
                           text=True, timeout=120, env=tool_env())
    except (OSError, subprocess.TimeoutExpired) as e:
        print_error(f"hook {flag}: unreadable ({str(e)[:160]})")
        return None
    return r.stdout.split() if r.returncode == 0 else None


def verify_supported(hook):
    """Whether the hook (a path) has the combined-tree verification mode."""
    try:
        with open(hook) as f:
            return VERIFY_MODE in f.read()
    except (OSError, TypeError):
        return False


def _readers_naming(tree: str, tip: str, path: str, readers: list[str]) -> tuple[list[str], str | None]:
    """(the readers at tip naming path's file name, why git could not look or None)."""
    r = git(tree, "grep", "-l", "-F", "-e", os.path.basename(path), tip, "--", *readers, check=False)
    if r.returncode not in (0, 1):  # 1: no match; anything else: git could not look -- never "no reader names it"
        return [], f"(git grep failed: {str(r.stderr).strip()[:80] or r.returncode})"
    return sorted(line.split(":", 1)[1] for line in r.stdout.splitlines() if ":" in line), None


def named_reader(tree: str, tip: str, paths: Iterable[str], readers: Iterable[str]) -> tuple[str, str] | None:
    """(path, reader) of a path (a deleted one, a changed build file) whose file name a reader names at tip; None when
    none does; a path git could not look for is named as read (a reason to check again)."""
    readers = [p for p in readers if git_ok(tree, "cat-file", "-e", f"{tip}:{p}")]
    if not paths or not readers:
        return None
    for p in sorted(paths):
        hits, failed = _readers_naming(tree, tip, p, readers)
        if failed or hits:
            return p, failed or hits[0]
    return None


def forces_full_recheck(path):
    """Whether a path landing meanwhile forces a FULL re-check whatever the change touches (THE RULE's first clause)."""
    return bool(RECHECK_ALWAYS.match(path)) and not HOOK_SELF_TEST.match(path)


def hook_selection(tree, paths):
    """What the tree's hook selects for paths (SELECTION-AWARE, above): {mode: words} for each dry-run mode the hook has,
    and local-tests.txt's lines for the areas its plan runs; None when a mode it has cannot be read."""
    out = hook_mode_selections(precheck_hook(tree), tree, paths)
    if out is None:
        return None
    shares = share_selection(tree)
    if shares is False:
        return None
    if shares is not None:
        out[SHARE_KEY] = shares
    out["local-tests"] = local_test_lines(tree, out.get("--plan", []))
    return out


def hook_mode_selections(hook: Any, tree: Any, paths: Any) -> Any:
    """{mode: words} of each dry-run mode the hook has, or None when the hook or a mode it has cannot be read."""
    try:
        with open(hook or "") as f:
            text = f.read()
    except OSError:
        return None
    out = {}
    for mode in SELECTION_MODES:
        if f"{mode})" not in text:  # never asked: an unknown flag would run the hook's real push checks
            continue
        out[mode] = hook_query(hook, tree, mode, paths)
        if out[mode] is None:
            return None
    return out


def local_test_lines(tree: Any, plan: Any) -> Any:
    """local-tests.txt's lines for the areas the hook's plan runs (the basic tests with sbt in it)."""
    areas = {"basic"} if "sbt" in plan else set()
    areas |= set(plan)
    try:
        with open(os.path.join(tree, "scripts", "hooks", "local-tests.txt")) as f:
            return [line.rstrip("\n") for line in f if line.split()[:1] and line.split()[0] in areas]
    except OSError:
        return []


def share_selection(tree):
    """The union of what precommit-share.sh's shares run for the change at the tree's HEAD (sorted words); None when the
    tree has no such script, False when a share cannot be read. Read as a change's own check (GITHUB_REF_NAME check/<x>,
    --checks-only): its range is HEAD since its merge base with origin/main, which in a check tree is the change."""
    script = os.path.join(tree, SHARE_SCRIPT)
    if not os.access(script, os.X_OK):
        return None
    words = set()
    env = {**tool_env(), "GITHUB_REF_NAME": "check/selection"}
    for share in SHARES:
        try:
            r = process_runner().run([script, "checks", share], cwd=tree, capture_output=True, text=True, timeout=120,
                               env=env, stdin=subprocess.DEVNULL)
        except (OSError, subprocess.TimeoutExpired):
            return False
        if r.returncode != 0:
            return False
        words |= set(r.stdout.split())
    return sorted(words)


def sbt_module_of(path):
    """The sbt module a path is in (`worker` for worker/src/...), or None."""
    parts = path.split("/")
    return parts[0] if len(parts) > 2 and parts[1] == "src" else None


def module_overlap(landed_paths, change_paths):
    """The sbt modules both sides touch (MODULE OVERLAP), sorted."""
    landed = {sbt_module_of(p) for p in landed_paths} - {None}
    return sorted(landed & ({sbt_module_of(p) for p in change_paths} - {None}))


def verify_areas_supported(tree):
    """Whether the tree's precommit-share.sh runs the area tests of a check/verifyareas- branch."""
    return f"{VERIFY_AREAS_MODE}-" in read_text_or(os.path.join(tree, SHARE_SCRIPT), "")


def selection_at(tree, rev, tip, paths):
    """hook_selection at rev, read in tree (checked out at tip, and back at tip after); None when rev is unknown."""
    if not rev or not commit_known(tree, rev):
        return None
    if rev == tip:
        return hook_selection(tree, paths)
    git(tree, "checkout", "-q", "--force", "--detach", rev)
    try:
        return hook_selection(tree, paths)
    finally:
        git(tree, "checkout", "-q", "--force", "--detach", tip)


def selection_change(tree, full_tip, tip, paths, new=None):
    """None when the hook selects nothing at tip for paths that the verify round leaves unrun and the full check at
    full_tip did not run (SELECTION-AWARE, above); else what it adds, in words. new: the selection at tip, when read."""
    old, new = selection_at(tree, full_tip, tip, paths), new or selection_at(tree, tip, tip, paths)
    if old is None or new is None:
        return "its selection at the full check cannot be read" if old is None else "its selection cannot be read"
    for key in sorted(set(old) | set(new)):
        added = selection_added(key, old, new)
        if added is not None:
            return selection_added_words(key, added)
    return None


def selection_added_words(key: Any, added: Any) -> str:
    """`its <key> for the change adds a, b, c (+n more)`: the first three of what the selection adds, or a mode."""
    return f"its {key.lstrip('-')} for the change adds {', '.join(added[:3]) or 'a mode'}" \
        + (f" (+{len(added) - 3} more)" if len(added) > 3 else "")


def selection_added(key: Any, old: Any, new: Any) -> list[str] | None:
    """What the selection `new` adds under key to `old`'s (a list: empty for a mode that was not selected at all), or None
    when it adds nothing the verify round leaves unrun."""
    if key in VERIFY_RUNS_ADDED:
        return None
    added = [w for w in (new.get(key) or []) if w not in set(old.get(key) or [])]
    return added if added or (key not in old and key in new) else None


