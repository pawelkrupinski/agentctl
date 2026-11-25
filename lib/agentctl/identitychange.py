# agentctl, part `identitychange`: whether a change touches the identity model: its changed paths, the identity-model globs, a claimed pure move
# A real module (lib/agentctl/__init__.py MODULES): its own imports, the standard library's and MODULES' only.

import fnmatch
from typing import Any
from agentctl.statefile import read_text_or
from agentctl.core import git, git_ok, process_cache, remote
from agentctl.repos import precheck_hook
from agentctl.queue import hook_query


def fetch_entry_commits(repo, e):
    """The entry's commits into this clone, fetched from refs/heads/land/<id> when it has not its head."""
    if not git_ok(repo, "cat-file", "-e", f"{e.get('head')}^{{commit}}"):
        git(repo, "fetch", "--quiet", remote(), f"refs/heads/land/{e['id']}", check=False)


def shared_paths(repo: Any, older: Any, younger: Any) -> list[str]:
    """The files both heads change, sorted."""
    return sorted(set(change_paths_of(repo, older) or []) & set(change_paths_of(repo, younger) or []))


def change_paths_of(repo: Any, e: Any, commits: Any = None) -> list[str] | None:
    """The paths the entry's commits (or those of them in `commits`) touch (their commits fetched from
    refs/heads/land/<id> when this clone has none); None when they cannot be read."""
    fetch_entry_commits(repo, e)
    paths: set[str] = set()
    for c in (e.get("commits") or []) if commits is None else commits:
        r = git(repo, "diff-tree", "--no-commit-id", "--name-only", "-r", "--root", "-m", c, check=False)
        if r.returncode != 0:
            return None
        paths |= {p for p in r.stdout.splitlines() if p}
    return sorted(paths)


def pure_move_claimed(repo, commit):
    """Whether the commit's message carries the trailer `Pure-Move: verified` (movies' scripts/pure-move-check claim)."""
    r = git(repo, "log", "-1", "--format=%(trailers:key=Pure-Move,valueonly)", commit, check=False)
    return r.returncode == 0 and [v.strip() for v in r.stdout.splitlines() if v.strip()] == ["verified"]


_identity_known = process_cache({})  # (entry id, head) -> identity_change's verdict


def identity_change(repo, e):
    """Whether the change is an identity change (G5's drift check of a batch holding more than one): G5's identity-model commits, or the hook's
    --identity-change naming a path that can move takes (a hook without it: its --plan selecting the `identity` area). None when it cannot tell (no plan, unreadable commits): checked alone. Kept per
    entry and head in the process (never written to the entry: an executor may be saving it meanwhile)."""
    key = (e["id"], e.get("head"))
    if key in _identity_known:
        return _identity_known[key]
    fetch_entry_commits(repo, e)
    # A VERIFIED PURE MOVE OWES NO IDENTITY AREA (movies' hook, 2026-11-19): a commit saying `Pure-Move: verified` moves
    # no take -- the hook's puremove check proves the move -- so only the commits without the claim make an identity
    # change; the hook selects the identity area by the same rule, so a batch holding the move runs none for it.
    commits = [c for c in e.get("commits") or [] if not pure_move_claimed(repo, c)]
    if not commits:
        _identity_known[key] = False
        return False
    paths = change_paths_of(repo, e, commits)
    hook = precheck_hook(repo)
    # WHAT CAN MOVE TAKES, NOT WHAT RUNS IDENTITY SPECS (2026-11-19): the hook's `--identity-change` names only the
    # take paths (movies: the resolver, its adapters, the title rules, the judgements), while its identity AREA also
    # runs for a common/ change some identity specs read -- which still batches. A hook without it: its --plan's area.
    text = read_text_or(hook or "", "")  # a flag is never asked unless the hook has it: an unknown flag runs the real checks
    flag = "--identity-change" if "--identity-change)" in text else "--plan" if "--plan)" in text else None
    if paths is None or flag is None:
        return None
    plan = hook_query(hook, repo, flag, paths)
    if plan is None:
        return None
    verdict = "identity" in plan or bool(identity_model_commits(repo, commits))
    _identity_known[key] = verdict
    return verdict


# THE BATCH TIP'S DRIFT CHECK (G5, convergence-failures-2026-10-29 §4: E4's 13-commit and E9's 24-commit batches masked
# which identity change broke convergence). A batch holding MORE THAN ONE identity-model commit (IDENTITY_MODEL_GLOBS)
# runs the drift-only pre-land check -- movies' scripts/identity-drift-ci (a drift-only convergence run on ci-runner's
# landing tier; `--against main` for a batch landing on a red gate) -- on its tip after its own check passed and before
# it is pushed. No script at the tip: a logged no-op. NO VERDICT (exit 2): the push goes on (an infra blip never holds
# a batch). DRIFT RED splits the batch: its identity changes are marked (drift_split) and assembled again one per
# batch beside the rest, so the non-identity part lands at once and a lone red identity change -- confirmed by a second
# run -- is rejected with the drift report as its reason.
IDENTITY_MODEL_GLOBS = ("identity/*", "worker/src/main/*/identity/*", "worker/src/main/identity/*",
                        "worker/src/test/scala/scripts/IdentityDrift*")


def identity_model_commits(repo, commits):
    """The commits among `commits` that touch the identity model."""
    out = []
    for c in commits:
        r = git(repo, "diff-tree", "--no-commit-id", "--name-only", "-r", "--root", "-m", c, check=False)
        if any(fnmatch.fnmatch(p, g) for p in (r.stdout if r.returncode == 0 else "").splitlines() if p
               for g in IDENTITY_MODEL_GLOBS):
            out.append(c)
    return out
