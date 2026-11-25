# agentctl, part `repos`: the repositories agentctl serves: red-repos.json (each repository's clone, short name, gate workflows and push policy) and the movies and server clones' paths.
# A real module (lib/agentctl/__init__.py MODULES): its own imports, the standard library's and MODULES' only.

import json
import os
import re
import shlex
from typing import Any, Dict, List, Optional
from agentctl.core import HOME, REPOS, err, git_ok, host_role, process_cache, remote, slug
from agentctl.selfcode import TOOL_PATH


def movies_repo_path() -> str:
    """THE movies clone of this host: $AGENTCTL_MOVIES_REPO, else the server's own clone (~/movies) on ci-runner, else
    ~/projects/movies. Every invariant, probe and step resolves the repository here."""
    return os.environ.get("AGENTCTL_MOVIES_REPO") or os.path.join(
        HOME, "movies" if host_role() == "server" else os.path.join("projects", "movies"))


GATED_REPOS = ("pawelkrupinski/movies",)


def gated_repo(o_r):
    return o_r in GATED_REPOS


# THE WATCHED REPOS (the owner, 2026-10-30, ci-failures recommendation 4: infra's Stage NixOS closures was red 29 h and
# core's Main Pipeline 21 h with no fixer, because the red tick read movies only). Which repos the red machine watches
# is config: red-repos.json beside the entry ($AGENTCTL_RED_REPOS names another file), one entry per repo --
# its gate workflows (the newest decided run of each is what opens and clears its reds), its family (every red of the
# repo goes to that ONE fixer; red-families.json says how the family is held) and its push policy: "land" (the fixer
# lands with `agentctl land`) or "owner-only" (an infra push is a deploy: the fixer prepares the fix on a local
# branch and files the owner a one-liner with `agentctl red owner-push`; it never pushes or deploys). The PRIMARY
# repo (movies) keeps everything it had -- its reds' keys, its outside workflows, its local checks, its landing gate
# (its gating checks registry above, which stays movies' alone: watching a repo's reds does not put a red gate in front
# of its landings). The other repos are read through their gate workflows only.
RED_PRIMARY = GATED_REPOS[0]


RED_REPOS_DEFAULT = [{"repo": RED_PRIMARY, "push": "land", "red_workflows": True, "signed": False}]


RED_PUSH_POLICIES = ("land", "owner-only")


_red_repos_cache = process_cache({})


def red_repos_path():
    return os.environ.get("AGENTCTL_RED_REPOS") or os.path.join(os.path.dirname(TOOL_PATH), "red-repos.json")


def red_repos_parse(raw):
    """The watched repos from red-repos.json's parsed JSON: [{repo, gate: [(name, file)], family, push, clone}], the
    primary first (added when the file lacks it). Raises ValueError on a malformed entry: a typo must not quietly
    stop a repo being watched."""
    out = []
    for x in (raw or {}).get("repos") or []:
        repo = x.get("repo")
        if not isinstance(repo, str) or not re.fullmatch(r"[\w.-]+/[\w.-]+", repo):
            raise ValueError(f"a repo entry needs `repo: <owner>/<name>`: {x!r}")
        gate = x.get("gate") or []
        if repo == RED_PRIMARY and not gate:
            pass  # the primary's gate is its gating checks registry (gate_flows), never a list here
        elif not gate or not all(isinstance(g, list) and len(g) == 2 and all(isinstance(v, str) and v for v in g)
                               for g in gate):
            raise ValueError(f"{repo}: `gate` must be [[<workflow name>, <workflow file>], ...]")
        push = x.get("push") or "land"
        if push not in RED_PUSH_POLICIES:
            raise ValueError(f"{repo}: `push` must be one of {', '.join(RED_PUSH_POLICIES)}, not {push!r}")
        clone = x.get("clone")
        keeps = x.get("red_workflows", False)
        if not isinstance(keeps, bool):
            raise ValueError(f"{repo}: `red_workflows` is true or false, not {keeps!r}")
        signed = x.get("signed", True)
        if not isinstance(signed, bool):
            raise ValueError(f"{repo}: `signed` is true or false, not {signed!r}")
        out.append({"repo": repo, "gate": [tuple(g) for g in gate], "family": x.get("family"), "push": push,
                    "clone": os.path.expanduser(clone) if clone else None,
                    "generated": generated_parse(repo, x.get("generated")), "red_workflows": keeps,
                    "signed": signed})
    if len({c["repo"] for c in out}) != len(out):
        raise ValueError("a repo is listed twice")
    if not any(c["repo"] == RED_PRIMARY for c in out):
        out.insert(0, red_repos_parse({"repos": RED_REPOS_DEFAULT})[0])
    return sorted(out, key=lambda c: c["repo"] != RED_PRIMARY)


def generated_parse(repo, raw):
    """A repo's GENERATED FILES (generated.py): [{paths: [glob], regen: [argv] or None, timeout: s}] from its `generated`
    entries (`{"paths": [...], "regen": "<command run in the tree>", "timeout": <s, optional>, "merge": "rows" | "regen"
    | "derived", "block": [<begin line prefix>, <end line prefix>] (optional)}`; no `regen`: a table merged row by row
    only, its rows hand-written -- a row both sides changed is the author's conflict; `merge: regen`: a SNAPSHOT --
    HTML, JSON, a rendered text -- never merged by rows, regenerated over the combined tree whenever both sides changed
    it: generated.py SNAPSHOTS ARE REGENERATED, NEVER MERGED; `merge: derived`: the same, its output needing no
    explaining by the sides, and `block` a file only part of which is generated: generated.py A DERIVED FILE IS THE
    COMBINED TREE'S; `order_sensitive: true`: its files hold a change behind a check in flight, generated.py ONLY A
    VOIDED CHECK HOLDS). Raises ValueError."""
    out = []
    for g in raw or []:
        paths, regen = (g or {}).get("paths"), (g or {}).get("regen")
        if not paths or not all(isinstance(p, str) and p and not p.startswith("/") for p in paths) \
                or (regen is not None and (not isinstance(regen, str) or not regen.strip())):
            raise ValueError(f"{repo}: a `generated` entry needs `paths: [<repo path or glob>, ...]` and, when it has "
                             f"one, `regen: <command>`: {g!r}")
        timeout = g.get("timeout")
        if timeout is not None and not (isinstance(timeout, (int, float)) and timeout > 0):
            raise ValueError(f"{repo}: a `generated` entry's `timeout` is seconds > 0: {g!r}")
        out.append({"paths": list(paths), "regen": shlex.split(regen) if regen is not None else None, "timeout": timeout,
                    **generated_merge(repo, g, regen)})
    return out


def generated_merge(repo: str, g: Dict[str, Any], regen: Optional[str]) -> Dict[str, Any]:
    """A `generated` entry's merge kind and block, as its parsed entry carries them ({} for a table merged by rows).
    Raises ValueError."""
    merge = g.get("merge", "rows")
    if merge not in ("rows", "regen", "derived") or (merge != "rows" and regen is None):
        raise ValueError(f"{repo}: a `generated` entry's `merge` is `rows` (the default), `regen` (a SNAPSHOT: never "
                         f"merged by rows, always regenerated: it needs `regen`) or `derived` (the same, its output the "
                         f"combined tree's): {g!r}")
    block = generated_block(repo, g, merge)
    return {**({"merge": merge} if merge != "rows" else {}), **({"block": block} if block else {}),
            **generated_order(repo, g, merge)}


def generated_order(repo: str, g: Dict[str, Any], merge: str) -> Dict[str, Any]:
    """A `generated` entry's `order_sensitive: true` as its parsed entry carries it ({} when absent): its files hold a
    change behind a check in flight (generated.py ONLY A VOIDED CHECK HOLDS) -- never a DERIVED file's. Raises
    ValueError."""
    if "holds" in g:
        raise ValueError(f"{repo}: a `generated` entry's `holds` is gone (2026-11-25): an entry holds a change behind a "
                         f"check in flight only when it says `order_sensitive: true`: {g!r}")
    order = g.get("order_sensitive", False)
    if not isinstance(order, bool) or (order and merge == "derived"):
        raise ValueError(f"{repo}: a `generated` entry's `order_sensitive` is true or false, and never true on a "
                         f"`derived` entry (regenerated over whatever tree it lands in): {g!r}")
    return {"order_sensitive": True} if order else {}


def generated_block(repo: str, g: Dict[str, Any], merge: str) -> Optional[List[str]]:
    """A `generated` entry's `block` ([begin line prefix, end line prefix]: a file only part of which is generated), or
    None. Raises ValueError."""
    block = g.get("block")
    if block is None:
        return None
    if merge == "rows" or not isinstance(block, list) or len(block) != 2 \
            or not all(isinstance(x, str) and x for x in block):
        raise ValueError(f"{repo}: a `generated` entry's `block` is [<begin line prefix>, <end line prefix>], on a "
                         f"`regen` or `derived` entry: {g!r}")
    return list(block)


def red_repos():
    """The watched repos (red_repos_parse), re-read when the file changes; the primary alone when it is missing or
    unreadable (said once per change)."""
    path = red_repos_path()
    try:
        m = os.path.getmtime(path)
    except OSError:
        m = None
    hit = _red_repos_cache.get(path)
    if hit and hit[0] == m:
        return hit[1]
    repos = red_repos_parse({"repos": RED_REPOS_DEFAULT})
    if m is not None:
        try:
            with open(path) as f:
                repos = red_repos_parse(json.load(f))
        except (OSError, ValueError) as e:
            err(f"red repos: {path} unreadable ({str(e)[:200]}): only {RED_PRIMARY} is watched")
    _red_repos_cache[path] = (m, repos)
    return repos


def red_repo_conf(o_r):
    return next((c for c in red_repos() if c["repo"] == o_r), None)


def red_repo_of(d: Any) -> str:
    """The repo a red (its data, its picture, a fixer record) belongs to: its `repo`, else the primary (every red from
    before 2026-10-30 is movies')."""
    return str((d or {}).get("repo") or RED_PRIMARY)


def red_repo_short(o_r: str) -> str:
    return o_r.split("/", 1)[-1]


def red_repo_clone(o_r):
    """This machine's clone of o_r: its config's `clone` when it is a git checkout, else a known clone (REPOS); None."""
    c = red_repo_conf(o_r) or {}
    p = c.get("clone")
    if p and os.path.isdir(p) and git_ok(p, "rev-parse", "--git-dir"):
        return p
    try:
        return repo_path_for(o_r)
    except (Exception, SystemExit):
        return None


def repo_path_for(o_r):
    # ci-runner's reconciler has none of REPOS (HOME is its state root): its own clone is movies' scan repo, or
    # main_workflow_files reads nothing there -- no workflow outside the newest 200 runs is listed and no callable-only
    # red is ever proven (2026-10-29: Report run timings #38 stayed offered under f8f51e8)
    for p in REPOS + [server_repo()]:
        if os.path.isdir(p) and git_ok(p, "remote", "get-url", remote()):
            try:
                if slug(p) == o_r:
                    return p
            except (SystemExit, RuntimeError):
                pass
    return None


def server_repo() -> Any:
    """The reconciler's own clone on ci-runner (movies_repo_path; `tick` clones it from $AGENTCTL_MOVIES_REMOTE as
    remote()'s name)."""
    return movies_repo_path()


def red_repo(o_r):
    """The clone whose worktrees, landers and waiting entries name the fixers ($AGENTCTL_RED_REPO; '-' for none)."""
    v = os.environ.get("AGENTCTL_RED_REPO")
    if v == "-":
        return None
    return v or repo_path_for(o_r)


def scan_repo_for(o_r, repo=None):
    if os.environ.get("AGENTCTL_RED_REPO"):
        return red_repo(o_r)
    return repo or repo_path_for(o_r)


def repo_signs(o_r: str) -> bool:
    """Whether repo o_r's main keeps commit signatures (red-repos.json `signed`, default true). movies' does not: its
    land rebases every change with NO_SIGN, so a fixer's signature never reaches main."""
    c = red_repo_conf(o_r)
    return True if c is None else bool(c.get("signed", True))


def precheck_hook(repo):
    """The repo's own pre-push hook, when it has one with a dry-run mode (movies: scripts/hooks/pre-push --range)."""
    hook = os.path.join(repo, "scripts", "hooks", "pre-push")
    return hook if os.access(hook, os.X_OK) else None


def repo_of_waiting(d: str) -> str:
    return movies_repo_path()


def waiting_supported(repo):
    """A repo hook, if any, must have --checks-only (its checks without the red gate) to check a waiting tip."""
    hook = precheck_hook(repo)
    if not hook:
        return True
    with open(hook) as f:
        return "--checks-only" in f.read()


def batching(repo):
    """Whether land batches here (every change joins `waiting`): a repo hook with --checks-only (movies)."""
    hook = precheck_hook(repo)
    return bool(hook) and waiting_supported(repo)
