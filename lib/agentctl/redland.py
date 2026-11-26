# agentctl, part `redland`: THE RED-LANDING RULE -- whether a change may land while the gate is red, and whether it is
# a fix of an open red (any red, or a gate red), decided in ONE place.
# A real module (lib/agentctl/__init__.py MODULES): its own imports, the standard library's and MODULES' only.

import fnmatch
from dataclasses import dataclass
from typing import Any
from agentctl.core import git, git_ok, process_cache, remote
from agentctl.fixchange import fixes_named
from agentctl.redfixes import commit_fixes


# ONE RULE (2026-11-26; batch #124's deadlock of 2026-11-25 was two of its copies disagreeing). Three copies decided it:
# waiting.must_wait_why (per commit: every production commit's COUNTING `Fixes-Red:` names a run of the gate's red or
# of an open gate red the store holds), changekind.classify_change's red_fix / gate_fix (per entry: the names recorded
# at submission) and queueclose.check_entry's red-fix tree guard; gov.gov_class_of read the commits' counting fixes
# through the classifier. Characterised over a seeded matrix (tests/test_red_landing.py), they disagreed in two ways,
# resolved here:
#  - A CHANGE THAT MAY NOT LAND ON RED IS NO GATE FIX. One fix commit beside a production commit naming no red (or a
#    tools-only fix commit beside one) was a `gate_fix` -- first in the batch order, and, waiting, WR's "a gate red's
#    fix waits for green" -- though the rule kept it for green. A gate fix is now a change whose every production
#    commit names the gate red: what the hook lets land on it. Naming an open red, it is still a red fix (its own tree,
#    checked first), as an outside red's fix is.
#  - THE COMMITS' COUNTING TRAILERS ARE THE TRUTH, NOT THE NAMES RECORDED AT SUBMISSION. A trailer fix_check stopped
#    counting since (a red opened whose job does not read the commit) left the entry `fixes`: the classifier called it
#    a fix while the rule and the governor did not. Where the commits can be read (a repo and the entry's commits), the
#    verdict reads their counting values; an item's data read without git is judged by its recorded names.
PRODUCTION_GLOBS = ("*/src/main/*", "conf/*", "*/conf/*", "build.sbt", "project/*", "infra/nix/*", "infra/kubernetes/*",
                    "Dockerfile")


def is_production_path(p: str) -> bool:
    """movies' pre-push is_production_path: what reaches a pod or a fleet host (the rest lands while red)."""
    return any(fnmatch.fnmatch(p, g) for g in PRODUCTION_GLOBS)


_production: dict[str, bool] = process_cache({})  # sha -> whether it touches a production path (a commit never changes)


def fetch_entry_commits(repo: str, e: dict[str, Any], want: str | None = None) -> None:
    """The entry's commits into this clone, fetched from refs/heads/land/<id> when it has not `want` (its head)."""
    if not git_ok(repo, "cat-file", "-e", f"{want or e.get('head')}^{{commit}}"):
        git(repo, "fetch", "--quiet", remote(), f"refs/heads/land/{e['id']}", check=False)


def touches_production(repo: str, c: str) -> bool | None:
    """Whether commit c touches a production path; None when this clone cannot read it (never raises: 2026-11-26
    16:47Z-18:5xZ every change tick and batch tick on ci-runner died on `git diff-tree <sha>: bad object` for one
    change's commit its clone lacked, and nothing landed)."""
    if c not in _production:
        r = git(repo, "diff-tree", "--no-commit-id", "--name-only", "-r", "--root", "-m", c, check=False)
        if r.returncode != 0:
            return None
        _production[c] = any(p and is_production_path(p) for p in r.stdout.split("\n"))
    return bool(_production[c])


def red_kind_of(d: dict[str, Any]) -> str:
    """A red item's kind as a change's classifier names it: `gate` (it holds every production push), else its own
    red_kind (`outside`, `flake`, `stuck-run`), `outside` when it records none."""
    return "gate" if d.get("gate") else str(d.get("red_kind") or "outside")


def named_reds(names: set[str], gate: set[str], open_: dict[str, list[Any]]) -> list[dict[str, Any]]:
    """[{name, red, kind}] of the reds a change names: the gate's current names and every open red's (queue_facts)."""
    reds = []
    for n in sorted(names):
        if n in gate or n in open_:
            rid, kind = (open_.get(n) or [None, None])[:2]
            reds.append({"name": n, "red": rid, "kind": "gate" if n in gate else kind})
    return reds


def gate_red_names(facts: dict[str, Any]) -> set[str]:
    """Every name a gate red answers to: the gate's current runs and each open gate red's names (its item's, its episode's)."""
    return set(facts.get("gate") or ()) | {n for n, (_rid, kind) in (facts.get("open") or {}).items() if kind == "gate"}


@dataclass(frozen=True)
class RedLanding:
    """The verdict: the reds a change names (named_reds), whether every production commit names a gate red
    (`covered`), and why it must wait while the gate is red (`wait`, None: it may land on the red, or the gate is green)."""
    names: frozenset[str]
    reds: tuple[dict[str, Any], ...]
    covered: bool
    wait: str | None

    @property
    def red_fix(self) -> bool:
        """It names an open red: checked in the red-fix tree, first (the PRIORITY class red-fix)."""
        return bool(self.reds)

    @property
    def names_gate_red(self) -> bool:
        """It names a gate red, whether or not it may land on it (a fix waiting beside a production commit naming none)."""
        return any(r["kind"] == "gate" for r in self.reds)

    @property
    def gate_fix(self) -> bool:
        """It fixes a gate red and may land on it: every production commit names one."""
        return self.covered and self.names_gate_red


CommitFixes = tuple[str, bool, set[str]]  # (sha, touches production, the Fixes-Red values that count now)


def commit_rows(entry: dict[str, Any] | None, repo: str | None, commits: list[str] | None,
                store: Any) -> list[CommitFixes] | None:
    """The change's commits as the rule reads them (`commits`, else the entry's when repo is given and it lists any),
    or None: unread (an item's data, an entry that lost its commits)."""
    if commits is None and repo and entry and entry.get("commits"):
        commits = list(entry["commits"])
    if not repo or commits is None:
        return None
    rows = []
    for c in commits:
        prod = entry_commit_production(repo, entry, c)
        if prod is None:
            return None  # unread: the change is judged by its recorded names, never a dead tick
        rows.append((c, prod, set(commit_fixes(repo, c, store))))
    return rows


def entry_commit_production(repo: str, entry: dict[str, Any] | None, c: str) -> bool | None:
    """touches_production of the entry's commit c, fetched from its land ref first when the clone lacks it."""
    prod = touches_production(repo, c)
    if prod is None and entry and entry.get("id"):
        fetch_entry_commits(repo, entry, c)
        prod = touches_production(repo, c)
    return prod


def may_land_on_red(facts: dict[str, Any], entry: dict[str, Any] | None = None, repo: str | None = None,
                    commits: list[str] | None = None, store: Any = None) -> RedLanding:
    """THE RULE, for a change under facts (changekind.queue_facts: the gate's reds and the store's open reds). Its
    commits read (commit_rows), each production commit must carry a counting `Fixes-Red:` naming a gate red (the hook's
    rule; tools-only commits land freely). Unread, the change is judged by its entry's recorded names (fixes_named)."""
    rows = commit_rows(entry, repo, commits, store)
    names = set().union(*(f for _c, _p, f in rows)) if rows is not None else fixes_named(entry)
    reds = tuple(named_reds(names, set(facts.get("gate") or ()), facts.get("open") or {}))
    gnames = gate_red_names(facts)
    bare = first_bare(rows, gnames)
    covered = bare is None and (rows is not None or any(r["kind"] == "gate" for r in reds))
    return RedLanding(frozenset(names), reds, covered, None if covered else red_wait(facts, names, gnames, bare))


def first_bare(rows: list[CommitFixes] | None, gnames: set[str]) -> tuple[str, set[str]] | None:
    """The first production commit whose counting fixes name no gate red, with them; None when there is none."""
    return next(((c, f) for c, p, f in rows or () if p and not f & gnames), None)


def red_wait(facts: dict[str, Any], names: set[str], gnames: set[str], bare: tuple[str, set[str]] | None) -> str | None:
    """Why a change not covered waits while the gate is red (None when it is green): its first bare production
    commit (wait_words), else (unread commits) that its names name no run of the gate red."""
    gate = set(facts.get("gate") or ())
    if not gate:
        return None
    why = wait_words(bare[0], bare[1], gate, gnames - gate) if bare \
        else f"it names {', '.join(sorted(names)) or 'no red'}, no run of the open gate red"
    unread = facts.get("episode_unread") or {}
    return why + "".join(f"; the episode of {w} is unread ({u}): only its newest run counts" for w, u in sorted(unread.items()))


def wait_words(c: str, fixes: set[str], reds: set[str], open_names: set[str]) -> str:
    """Why commit c waits, in words (WR names it when a fix that cannot move says why: `Fixes-Red:` missing, not
    counting, or naming no run of the open gate red)."""
    if not fixes:
        return f"{c[:10]} is a production commit whose `Fixes-Red:` is missing or does not count (fix_check: it touches nothing the red's job reads)"
    return (f"{c[:10]} is a production commit whose `Fixes-Red:` names {', '.join(sorted(fixes))}, no run of the open gate red "
            f"(the gate's runs {', '.join(sorted(reds)[:4])}; the red items' {', '.join(sorted(open_names)[:6]) or 'none'})")
