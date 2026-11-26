# agentctl, part `redland`: THE RED-LANDING RULE and FIX REPLAY: whether a change may land while the gate is red, and
# whether it is a fix; a fixer's brief's replay trailers, agentctl's own replays, the refusal
# A real module (lib/agentctl/__init__.py MODULES): its own imports, the standard library's and MODULES' only.

import fnmatch
from dataclasses import dataclass
from typing import Any
import os
import re
import subprocess
from typing import Iterator
from agentctl.core import err, git, git_ok, process_cache, remote, state_root
from agentctl.fixchange import fixes_named
from agentctl.redfixes import commit_fixes
from agentctl.procrunner import process_runner
from agentctl.redknobs import knob_brief, knob_fix_replay_timeout
from agentctl.repos import red_repo_conf, red_repo_of
from agentctl.trailers import commit_trailers, trailer_values
from agentctl.redrepos import RED_WORKFLOWS_FILE, red_workflow_policy, red_workflows_on_main
from agentctl.statemachines import is_terminal
from agentctl.redrecord import red_names, red_what
from agentctl.storetoken import no_vault
from agentctl.fixerenv import BRIEF
from agentctl.land import tool_env


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
    tools_only_known_red: bool = False  # TOOLS-ONLY, FAILING ONLY AS MAIN'S GATE FAILS: lands (below)

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
    wait = None if covered else red_wait(facts, names, gnames, bare)
    return RedLanding(frozenset(names), reds, covered, wait, tools_only_known_red(facts, entry, rows))


# THE OWNER'S "LET IT THROUGH" (2026-11-26; policy `known-red-tools`): a TOOLS-ONLY change whose checks failed only
# where main's newest decided gate run fails (the entry's known_red, `gate_only`: every failing key is one the gate's
# run shows; no extra failure, which would have rejected it) does not wait behind that red -- 13 stays and 102 h in the
# history. Production changes are unchanged. Main's own CI re-tests everything after the landing.
def tools_only_known_red(facts: dict[str, Any], entry: dict[str, Any] | None, rows: list[CommitFixes] | None) -> bool:
    """Whether the change's known red is no reason to wait: the gate is red, its commits are read and none touches
    production, and every failing check is one main's gate run fails too."""
    known = (entry or {}).get("known_red")
    return bool(isinstance(known, dict) and known.get("gate_only") and facts.get("gate") and rows is not None
                and not any(prod for _c, prod, _f in rows))


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


def sync_brief():
    """The movies agents' preamble a fixer reads first (~/bin/briefs/movies-agent-preamble.md, $AGENTCTL_BRIEF). The
    fixer's own rules are docs/fixer-brief.md, in its prompt (fixer_brief); nothing is copied from a scratchpad."""
    return knob_brief() or BRIEF


# FIX REPLAY (2026-11-19, Alert backtest #1-#5, 5254bffda9d2): seven fixes landed and were refuted, one per run of the
# backtest -- each fixer landed a guess the nightly then refuted, though the backtest replays cheaply over ci-runner's
# Prometheus snapshot. A workflow whose RED_WORKFLOWS_FILE row names a `replay` command (`{run}`: the red's failing run
# id) holds its fixers to it: a `Fixes-Red:` landing for such a red must carry, in one of its commits, the trailer
# `Fix-Replay: <that command> base=FAIL head=PASS` -- the replay run at origin/main and at the fix. Outside the gate
# that is a WARNING only (policy `fix-replay-outside`: the owner, 2026-11-20, "enforce for gate reds").
#
# GATE REDS (the owner, 2026-11-20: "3 approve and enforce for gate reds"; policy `fix-replay`): a `Fixes-Red:` naming
# a red of the gate (Main, Identity model convergence, Identity takes: the red's `gate`) must carry `Fix-Replay:
# <command>` -- the narrowest command that fails where the red fails -- which AGENTCTL RUNS, at origin/main and at the
# rebased fix, each in a throwaway detached worktree (run_fix_replays, the laptop's land: where the JVM is): it counts
# only when it sees base=FAIL head=PASS, and the proof rides with the submission (`replays`) for the server's check.
# The escape hatch, for a red that genuinely cannot replay (infra, a CI-only race): `Fix-Replay: none — <why>`, a
# SPECIFIC reason (30+ characters, no "n/a", "flaky" or "can't"), in the message's final trailer block (where git reads
# it), listed in the owner's weekly report like the coupling exemptions (movies scripts/ci/fix_replay_exemptions.py,
# the Coupling exemptions workflow).
FIX_REPLAY_TRAILER = re.compile(r"(?i)^Fix-Replay:\s*(.+?)\s+base=(FAIL|PASS)\s+head=(FAIL|PASS)\s*$")


FIX_REPLAY_ANY = re.compile(r"(?i)^Fix-Replay:\s*(.*?)\s*$")


FIX_REPLAY_NONE = re.compile(r"(?i)^none\b\s*(?:—|–|--|-|:)\s*(.*)$")


FIX_REPLAY_NONE_MIN = 30  # characters of a `Fix-Replay: none — <why>` reason


FIX_REPLAY_VAGUE = re.compile(r"(?i)(?<![\w/])n/?a(?![\w/])|flak|can'?t|cannot")


def agentctl_revert_fix_replay(reason):
    """The `Fix-Replay: none — <why>` line agentctl's OWN revert of a culprit carries (blame's revert, the fixer's revert
    fallback; 2026-11-21 Main #2806-#2808: blame's revert carried none, FIX REPLAY refused it, main stayed red ~1 h). A
    revert puts main back to code that passed: there is no fix to replay, and the reason names the reverted change and
    the red, so the owner's weekly list reads it like any other."""
    return f"Fix-Replay: none — {reason}\n"


def red_replay_command(repo, d):
    """The replay command for red data d (its run id in place of `{run}`), or None."""
    cmd = red_workflow_policy(repo, d.get("workflow"))["replay"]
    run = (d.get("last_run") or {}).get("databaseId")
    return cmd.replace("{run}", str(run)) if cmd and run else cmd


class _ReplayTrailers:
    """What the trailers of commits say of FIX REPLAY: the red names of `Fixes-Red:`, the claimed results (`Fix-Replay:
    <cmd> base=X head=Y`), the bare commands agentctl runs, the `none` reasons."""

    def __init__(self) -> None:
        self.named: set[str] = set()
        self.claimed: list[tuple[str, str, str]] = []
        self.bare: list[str] = []
        self.nones: list[str] = []

    def read(self, msg: str) -> None:
        for ln in commit_trailers(msg):  # the final block only: the one git reads, as the weekly list does
            self._line(ln)

    def _line(self, ln: str) -> None:
        k, _, v = ln.partition(":")
        if k.strip().lower() == "fixes-red":
            self.named |= trailer_values(v)
        claimed = FIX_REPLAY_TRAILER.match(ln)
        if claimed:
            self.claimed.append((claimed.group(1).strip(), claimed.group(2).upper(), claimed.group(3).upper()))
            return
        said = FIX_REPLAY_ANY.match(ln)
        if said and said.group(1):
            self._command_or_none(said.group(1))

    def _command_or_none(self, value: str) -> None:
        none = FIX_REPLAY_NONE.match(value)
        if none or value.lower() == "none":
            self.nones.append(none.group(1) if none else "")
        elif value not in self.bare:
            self.bare.append(value)


def fix_replay_trailers(repo, commits):
    """(the names Fixes-Red: gives, [(command, base, head)] of claimed results, [command] agentctl runs, [none
    reasons]) over commits' trailers."""
    found = _ReplayTrailers()
    for c in commits or []:
        found.read(git(repo, "log", "-1", "--format=%B", c, check=False).stdout)
    return found.named, found.claimed, found.bare, found.nones


def fix_replay_none_reasons(repo, commits):
    """The reasons of commits' `Fix-Replay: none — <why>` trailers."""
    return fix_replay_trailers(repo, commits)[3]


def fix_replay_none_lint(reason):
    """Why a `Fix-Replay: none` reason is no reason (too short, or vague), or None."""
    if len(reason or "") < FIX_REPLAY_NONE_MIN:
        return f"{len(reason or '')} characters, under {FIX_REPLAY_NONE_MIN}"
    m = FIX_REPLAY_VAGUE.search(reason)
    return f"it says {m.group(0)!r}: name what cannot replay and why" if m else None


def run_fix_replays(repo, base, head, commits):
    """AGENTCTL'S REPLAY (above): each bare `Fix-Replay: <command>` of a Fixes-Red push, run at base and at head, each in
    a throwaway detached worktree of repo: [{cmd, base, head, base_sha, head_sha}], a side PASS (exit 0), FAIL or
    TIMEOUT. Nothing to run (no Fixes-Red:, no bare command): []."""
    named, _claimed, bare, _nones = fix_replay_trailers(repo, commits)
    if not named or not bare:
        return []
    out = []
    for cmd in bare:
        rec = {"cmd": cmd, "base_sha": base, "head_sha": head}
        for side, sha in (("base", base), ("head", head)):
            rec[side] = fix_replay_once(repo, sha, cmd, side)
        err(f"fix replay: `{cmd}` base={rec['base']} head={rec['head']}")
        out.append(rec)
    return out


def fix_replay_once(repo, sha, cmd, side):
    """`cmd` run (bash) in a throwaway detached worktree of repo at sha: PASS, FAIL or TIMEOUT; its log kept under the
    state root (fix-replay/<sha>-<side>.log)."""
    import shutil, tempfile
    logs = state_root("fix-replay")
    os.makedirs(logs, exist_ok=True)
    wt = tempfile.mkdtemp(prefix=f"replay-{side}-", dir=logs)
    log = os.path.join(logs, f"{sha[:12]}-{side}.log")
    try:
        git(repo, "worktree", "add", "-q", "--detach", wt, sha)
        err(f"fix replay: `{cmd}` at {side} {sha[:10]} (log: {log})")
        with open(log, "w") as f:
            try:
                rc = process_runner().run(["bash", "-c", cmd], cwd=wt, stdin=subprocess.DEVNULL, stdout=f,
                                    stderr=subprocess.STDOUT, env=tool_env(),
                                    timeout=knob_fix_replay_timeout()).returncode
            except subprocess.TimeoutExpired:
                return "TIMEOUT"
        return "PASS" if rc == 0 else "FAIL"
    finally:
        git(repo, "worktree", "remove", "--force", wt, check=False)
        shutil.rmtree(wt, ignore_errors=True)
        git(repo, "worktree", "prune", check=False)


def _none_replay_refusal(what: Any, nones: Any) -> Any:
    """Why the `Fix-Replay: none — <why>` reasons of gate red `what` are refused, or None when each is a reason."""
    bad = [(r, fix_replay_none_lint(r)) for r in nones]
    bad = [(r, why) for r, why in bad if why]
    if not bad:
        return None
    return (f"{what} is a gate red: `Fix-Replay: none — {bad[0][0]}` is no reason ({bad[0][1]}); name, in "
            f"{FIX_REPLAY_NONE_MIN}+ characters, why this red cannot replay (infra, a CI-only race), or give the "
            "command that fails at origin/main and passes at your fix")


def _replay_proved(seen: Any, cmds: Any) -> Any:
    """Whether a recorded run of one of cmds failed at origin/main and passed at the fix."""
    return any((seen.get(c) or {}).get("base") == "FAIL" and (seen.get(c) or {}).get("head") == "PASS" for c in cmds)


def _proof_replay_refusal(what: Any, cmds: Any, proofs: Any) -> Any:
    """Why the replay commands cmds of gate red `what` do not prove the fix (agentctl's recorded runs), or None."""
    seen = {p.get("cmd"): p for p in proofs or [] if p.get("cmd")}
    if _replay_proved(seen, cmds):
        return None
    ran = [c for c in cmds if c in seen]
    if ran:
        p = seen[ran[0]]
        return (f"{what} is a gate red: agentctl ran `{ran[0]}`: base={p.get('base')} head={p.get('head')} -- it must "
                "fail at origin/main and pass at your fix (head=FAIL: the fix is refuted before it lands; base=PASS: "
                "the command does not reach the red)")
    return (f"{what} is a gate red: agentctl has not run `{cmds[0]}` (a bare `Fix-Replay: <command>`, no "
            "base=/head= of your own: `agentctl land` from the laptop runs it at origin/main and at your fix)")


def gate_fix_replay_refusal(d: Any, claimed: Any, bare: Any, nones: Any, proofs: Any) -> Any:
    """Why a Fixes-Red: for gate red d is refused under policy `fix-replay` (above), or None."""
    what = red_what(d)
    if nones:
        return _none_replay_refusal(what, nones)
    cmds = bare + [c for c, _b, _h in claimed if c not in bare]
    if not cmds:
        return (f"{what} is a gate red: its Fixes-Red needs `Fix-Replay: <command>` -- the narrowest command that "
                "fails at origin/main and passes at your fix; agentctl runs it at both before the change counts as the "
                "fix -- or `Fix-Replay: none — <why>` when it genuinely cannot replay (a specific reason, "
                f"{FIX_REPLAY_NONE_MIN}+ characters; the owner reads them weekly)")
    return _proof_replay_refusal(what, cmds, proofs)


def _open_reds(store: Any) -> list[dict[str, Any]] | None:
    """The store's open reds that carry a key, or None when the store cannot be read (an unread store refuses nothing).
    THE LAPTOP'S LAND NEEDS NO VAULT: never `secrets` for this read (no token: no refusal)."""
    try:
        with no_vault():
            return [it for it in store.items("red") if it["data"].get("key") and not is_terminal("red", it["state"])]
    except (Exception, SystemExit):
        return None


def _named_reds(items: list[dict[str, Any]], named: set[str]) -> Iterator[dict[str, Any]]:
    """The data of the reds a Fixes-Red: names, the gate's first (enforced), each read as it is reached."""
    for it in sorted(items, key=lambda it: not it["data"].get("gate")):
        if named & {str(x) for x in red_names(it["data"]) if x is not None}:
            yield it["data"]


def _outside_replay_refusal(repo: Any, d: Any, claimed: Any) -> str | None:
    """The warning for a red outside the gate: its registry missing from main, or its workflow's replay command not
    claimed with base=FAIL head=PASS; None when the workflow names no replay or it is claimed."""
    if not red_workflows_on_main(repo) and (red_repo_conf(red_repo_of(d)) or {}).get("red_workflows"):
        return (f"{RED_WORKFLOWS_FILE} is missing on {remote()}/main, though red-repos.json says "
                f"{red_repo_of(d)} keeps one: no Fixes-Red lands for {red_what(d)} until it is back (its rows "
                "say which reds need a `Fix-Replay:`)")
    cmd = red_replay_command(repo, d)
    if not cmd:
        return None
    stem = red_workflow_policy(repo, d.get("workflow"))["replay"].split("{run}")[0].strip()
    ok = [r for r in claimed if r[0].startswith(stem)]
    if any(r[1:] == ("FAIL", "PASS") for r in ok):
        return None
    said = f" (this push says base={ok[0][1]} head={ok[0][2]})" if ok else ""
    return (f"{red_what(d)} has a replay: run `{cmd}` at origin/main and at your fix, see base=FAIL "
            f"head=PASS, and record it as the trailer `Fix-Replay: {cmd} base=FAIL head=PASS`{said}")


def fix_replay_refusal(repo, commits, store, proofs=None):
    """(policy id, why) when the Fixes-Red: of commits is refused by FIX REPLAY (above) -- `fix-replay` for a gate red,
    enforced; `fix-replay-outside` for a red outside the gate whose workflow names a replay, warn-only -- or None.
    proofs: agentctl's own replays (run_fix_replays; the submission's `replays`). An unread store refuses nothing (the
    red's own verification still judges); a registry missing from main refuses an outside red of a repo that keeps one
    (red-repos.json `red_workflows`: Alert backtest #7's fix landed unreplayed because agentctl enforced five hours
    before the registry reached main)."""
    named, claimed, bare, nones = fix_replay_trailers(repo, commits)
    if not named:
        return None
    items = _open_reds(store)
    if items is None:
        return None
    outside = None
    for d in _named_reds(items, named):
        if d.get("gate"):
            why = gate_fix_replay_refusal(d, claimed, bare, nones, proofs)
            if why:
                return "fix-replay", why
        elif not outside:
            outside = _outside_replay_refusal(repo, d, claimed)
    return ("fix-replay-outside", outside) if outside else None
