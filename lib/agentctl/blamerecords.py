# agentctl, part `blamerecords`: blame's records: the reverts withdrawn or refuted, the land refusal they answer, a withdrawn revert's worktree and changes
# A real module (lib/agentctl/__init__.py MODULES): its own imports, the standard library's and MODULES' only.

import os
import re
from typing import Any
from agentctl.statemachines import is_terminal, machine_state
from agentctl.timefmt import ts
from agentctl.core import HOME, PROTECTED_BRANCHES, _tokens, err, git, remote
from agentctl.repos import movies_repo_path
from agentctl.trailers import fixes_red_in_message
from agentctl.procs import process_cwds
from agentctl.inboxentry import inbox_write
from agentctl.deletions import log_deletion
from agentctl.lander import blame_change_key, blame_change_name
from agentctl.redrecord import red_what
from agentctl.red import red_stop_holder
from agentctl.worktrees import worktrees
from agentctl.fixerland import red_dispatch_log
from agentctl.culpritfix import blame_culprit
from agentctl.waiting import cancel_submitted
from agentctl.cleanup import own_ancestry
from agentctl.redopen import red_offer


# -- a withdrawn or refuted revert never lands again (2026-11-01: blame's wrong revert of ...-retained-input for #629 was
# WITHDRAWN -- the red re-decided "pre-existing" -- yet maintain's orphan duty found its leftover worktree
# movies-wt-revert-629, "author gone", and submitted it: re-queued at 22:33Z, landing on red through its Fixes-Red: 629.)
# A blame stops calling a change the culprit in two ways: WITHDRAWN (the decision itself was wrong, before its revert
# landed: blame_withdraw, `agentctl red unblame`) and REFUTED (its revert landed and did not clear the red). And a
# confirmed blame's revert is OBSOLETE once its red is over (terminal: is_terminal("red", ...)) by anything else -- the lander's fix forward
# under the same Fixes-Red (Main #2409, 2026-11-12: the revert sat refused, then escalated as an orphan, while the
# change it would take out was fixed and green on main). Whichever it is,
# its revert -- the commit carrying `Reverts-Change: <change>` and `Fixes-Red: <run>` -- and the worktree blame made for
# it are dead: blame_land_refusal is asked by every path that lands or queues commits (land's sync, the server's
# submission, the queue's checker, a recovery re-land, orphan duty).
REVERTS_CHANGE = re.compile(r"^Reverts-Change:\s*(\S+)", re.M)


def _dead_entry(run: str, why: str, verdict: str, rv: dict[str, Any]) -> dict[str, Any]:
    """One dead revert's record: its red's run, why it is dead, blame's verdict, the revert's worktree and sha."""
    return {"run": run, "why": why, "verdict": verdict, "worktree": rv.get("worktree"), "sha": rv.get("sha")}


class BlameLedger:
    """The reverts the reds' blames leave dead and live, filled one red at a time (`add`): a method per way a blame
    ends a revert -- withdrawn, refuted, obsolete -- and the confirmed one that stands."""

    def __init__(self) -> None:
        self.dead: dict[str, list[dict[str, Any]]] = {}
        self.live: dict[str, Any] = {"runs": {}, "worktrees": set()}

    def bury(self, change: str, entry: dict[str, Any]) -> None:
        self.dead.setdefault(change, []).append(entry)

    def add(self, it: dict[str, Any]) -> None:
        """One red's blame: its withdrawn revert, then its culprit's (refuted, obsolete, or standing)."""
        b = (it.get("data") or {}).get("blame") or {}
        run = str((b.get("run") or {}).get("number") or "")
        w = b.get("withdrawn")
        if isinstance(w, dict):
            self.withdrawn(w, run)
        g = blame_culprit(b)
        if g:
            self.culprit(it, b, blame_change_key(g), run, w)

    def withdrawn(self, w: dict[str, Any], run: str) -> None:
        rv = w.get("revert") or {}
        ch = w.get("change") or rv.get("change") or (rv.get("commits") or [None])[0]
        if ch:
            self.bury(ch, _dead_entry(run, f"withdrawn: {str(w.get('why') or '?')[:160]}", f"NOT THE CULPRIT of #{run}", rv))

    def culprit(self, it: dict[str, Any], b: dict[str, Any], ch: str, run: str, w: Any) -> None:
        rv = b.get("revert") or {}
        if b.get("state") == "refuted":
            self.bury(ch, _dead_entry(run, f"refuted: {str(b.get('why') or '?')[:160]}", f"NOT THE CULPRIT of #{run}", rv))
        elif b.get("state") == "confirmed" and not w:
            if is_terminal("red", it.get("state")):
                self.obsolete(it, ch, run, rv)
            else:
                self.standing(ch, run, rv)

    def obsolete(self, it: dict[str, Any], ch: str, run: str, rv: dict[str, Any]) -> None:
        """A confirmed blame whose red ended without its revert (cleared or verified by anything else)."""
        fixed = ((it.get("data") or {}).get("fix") or {}).get("sha") or ""
        by = f" by {fixed[:10]}" if fixed else ""
        self.bury(ch, _dead_entry(run, f"#{run} is {it['state']}{by} without it", f"OBSOLETE for #{run}", rv))

    def standing(self, ch: str, run: str, rv: dict[str, Any]) -> None:
        """A confirmed blame whose red is open: its revert stands for this run."""
        self.live["runs"].setdefault(ch, set()).add(run)
        if rv.get("worktree"):
            self.live["worktrees"].add(os.path.realpath(rv["worktree"]))


def blame_records(store):
    """(dead, live) from every red's blame. dead: {change: [{run, why, verdict, worktree, sha}]} -- the reverts blame
    withdrew or saw refuted ("not the culprit"), or whose red ended without them ("obsolete"); live: {"runs": {change: {run}}, "worktrees": {path}} -- the confirmed blames whose
    revert stands (a change blamed AGAIN for a newer red is reverted for that red: never refused)."""
    ledger = BlameLedger()
    try:
        items = store.items("red")
    except (Exception, SystemExit):
        return ledger.dead, ledger.live
    for it in items:
        ledger.add(it)
    return ledger.dead, ledger.live


def blame_records_quiet(st):
    """blame_records for `agentctl land` on the laptop: a land never needs a token (it never runs `secrets`), so a
    remote store is read only with a token already in hand; else nothing here -- ci-runner's submission and its checker
    refuse the revert with their own store (submit_change, _check_entry)."""
    if st.needs_token and not (os.environ.get("AGENTCTL_STORE_TOKEN") or _tokens.get("movies")):
        return {}, {"runs": {}, "worktrees": set()}
    return blame_records(st)


def _commit_refusal(c: str, msg: str, dead: dict[str, Any], live: dict[str, Any]) -> str | None:
    """Why commit c (its message `msg`) must never land: it reverts a change blame marked dead, and no confirmed blame of
    a newer red reverts that change again under its own Fixes-Red -- or None."""
    for ch in REVERTS_CHANGE.findall(msg):
        if ch not in dead or fixes_red_in_message(msg) & live["runs"].get(ch, set()):
            continue  # not a dead revert, or blamed again for a newer red: that red's revert
        d = dead[ch][-1]
        return (f"commit {c[:10]} reverts {ch}, which agentctl's blame marked {d['verdict']} "
                f"({d['why']}): its revert never lands again")
    return None


def _worktree_refusal(worktree: str, dead: dict[str, Any], live: dict[str, Any]) -> str | None:
    """Why nothing from `worktree` lands: it is the worktree blame made to revert a change it marked dead, or None."""
    rw = os.path.realpath(worktree)
    if rw in live["worktrees"]:
        return None
    for ch, ds in dead.items():
        for d in ds:
            if d.get("worktree") and os.path.realpath(d["worktree"]) == rw:
                return (f"{worktree.replace(HOME, '~')} is the worktree blame made to revert {ch} for #{d['run']}, "
                        f"marked {d['verdict']} ({d['why']}): nothing from it lands")
    return None


def blame_land_refusal(repo, commits, store, worktree=None, records=None):
    """Why these commits (from `worktree`) must never land, or None: a commit carrying `Reverts-Change: <change>` for a
    change blame withdrew or saw refuted (not the culprit) -- unless a confirmed blame of a newer red reverts that change
    again, under its own Fixes-Red -- or the worktree blame made for such a revert."""
    dead, live = records if records is not None else blame_records(store)
    if not dead:
        return None
    for c in commits or []:
        msg = git(repo, "log", "-1", "--format=%B", c, check=False).stdout if repo else ""
        refused = _commit_refusal(c, msg, dead, live)
        if refused:
            return refused
    return _worktree_refusal(worktree, dead, live) if worktree else None


def blame_only_reverts(repo, wt, change=None):
    """The worktree's commits off <remote>/main, when every one is blame's own revert (`Reverts-Change:`, of `change`
    when given) -- else None: somebody else's work is in it, never removed."""
    main = f"{remote()}/main"
    r = git(wt, "rev-list", f"{main}..HEAD", check=False)
    if r.returncode != 0:
        return None
    shas = r.stdout.split()
    for c in shas:
        found = REVERTS_CHANGE.findall(git(wt, "log", "-1", "--format=%B", c, check=False).stdout)
        if not found or (change and change not in found):
            return None
    return shas


def _revert_worktree_blocker(repo: Any, rw: Any, change: Any) -> Any:
    """Why blame's revert worktree rw must stay (said after `kept: `), else None: it holds other commits, is in use, or is dirty."""
    if blame_only_reverts(repo, rw, change) is None:
        return "it holds commits that are not blame's revert"
    mine = own_ancestry()
    busy = [p for p, c in process_cwds().items() if p not in mine and (c == rw or c.startswith(rw + "/"))]
    if busy:
        return f"in use by pid {busy[:3]}"
    dirty = git(rw, "status", "--porcelain", "--untracked-files=no", check=False)
    return "uncommitted changes" if dirty.returncode or dirty.stdout.strip() else None


def _blame_drop_branch(root: Any, branch: Any, head: Any, why: Any) -> Any:
    """' and branch X' once the revert's branch is deleted (unprotected, ref still at head), else ''."""
    if not branch or branch in PROTECTED_BRANCHES or \
            git(root, "update-ref", "-d", f"refs/heads/{branch}", head, check=False).returncode != 0:
        return ""
    log_deletion("branch", os.path.basename(root), branch, head, f"blame revert never landing: {why}")
    return f" and branch {branch}"


def blame_drop_revert_worktree(repo, wt, change, why, store):
    """Remove the worktree blame made for a revert that will never land, and its branch: only when its commits are all
    blame's reverts (blame_only_reverts), its tree clean and nobody in it; its open queue entries cancelled. Logged in
    agentctl-deleted.log. Returns one line saying what."""
    if not wt or not os.path.isdir(wt):
        return f"revert worktree {str(wt or '?').replace(HOME, '~')} already gone"
    rw = os.path.realpath(wt)
    kept = _revert_worktree_blocker(repo, rw, change)
    if kept:
        return f"revert worktree {rw.replace(HOME, '~')} kept: {kept}"
    cancelled = blame_cancel_changes_of(rw, why, store)
    branch = git(rw, "symbolic-ref", "-q", "--short", "HEAD", check=False).stdout.strip()
    head = git(rw, "rev-parse", "HEAD", check=False).stdout.strip()
    root = os.path.realpath(worktrees(rw)[0]["worktree"])
    r = git(root, "worktree", "remove", "--force", rw, check=False)
    if r.returncode:
        return f"revert worktree {rw.replace(HOME, '~')} kept: git worktree remove refused: {r.stderr.strip()[:160]}"
    log_deletion("worktree", os.path.basename(root), rw, head, f"blame revert never landing: {why}")
    msg = f"removed revert worktree {rw.replace(HOME, '~')}" + _blame_drop_branch(root, branch, head, why)
    return msg + (f"; cancelled {', '.join(cancelled)}" if cancelled else "")


def _cancelled(st: Any, cid: Any, why: Any) -> Any:
    """Cancel queued change cid; False (logged) when that failed."""
    try:
        if hasattr(st, "cancel_change"):
            st.cancel_change(cid)
        else:
            cancel_submitted(st, cid)
        return True
    except (Exception, SystemExit) as e:
        err(f"could not cancel {cid} ({why}): {str(e)[:120]}")
        return False


def blame_cancel_changes_of(wt, why, st):
    """Cancel every open queued change submitted from `wt`; returns their ids."""
    rw, out = os.path.realpath(wt), []
    try:
        items = st.items("change")
    except (Exception, SystemExit):
        return out
    for it in items:
        if machine_state("change", it["state"]).terminal or \
                os.path.realpath(str((it.get("data") or {}).get("worktree") or "")) != rw:
            continue
        if _cancelled(st, it["id"], why):
            out.append(it["id"])
    return out


def _landed_revert(repo: Any, rv: dict[str, Any], ch: str) -> str | None:
    """Why the revert of `ch` cannot be withdrawn (it is already on <remote>/main: its red's next run refutes it), or None."""
    if not (rv.get("ok") and repo and os.path.isdir(repo)):
        return None
    git(repo, "fetch", "--quiet", remote(), "main", check=False)
    on_main = git(repo, "log", f"{remote()}/main", "-n", "400", "--format=%H",
                  f"--grep=^Reverts-Change: {re.escape(ch)}$", check=False).stdout.split()
    if on_main:
        return (f"NOT WITHDRAWN: the revert of {ch} is on {remote()}/main ({on_main[0][:10]}): a landed "
                "revert is refuted by its red's next run, which relands it")
    return None


def _withdrawn_blame(b: dict[str, Any], ch: str, rv: dict[str, Any], why: str) -> dict[str, Any]:
    """The blame once withdrawn: undetermined (the normal fixer flow), the dead revert kept under `withdrawn`."""
    w = {"change": ch, "revert": rv, "revived": b.get("revived"), "why": why, "at": ts()}
    nb = {k: v for k, v in b.items() if k not in ("culprit", "revert", "revived", "parent", "evidence", "reland_due")}
    nb.update(state="undetermined", why=f"withdrawn: {why}", withdrawn=w)
    return nb


def _record_withdrawal(store: Any, it: dict[str, Any], nb: dict[str, Any], line: str) -> None:
    """Write the withdrawn blame to the red: its record only when it is already cleared, else offered to a fixer again."""
    cur = store.item("red", it["id"]) or it
    if machine_state("red", cur["state"]).terminal:  # a red already cleared: only its record changes
        store.update_item("red", cur["id"], cur["version"], {"blame": nb})
    else:
        red_offer(store, cur, line, data={"blame": nb}, quiet=True)


def blame_withdraw(store, it, why, repo=None):
    """WITHDRAW a red's blame: its culprit was wrongly decided and its revert has not landed. The blame becomes
    undetermined (the normal fixer flow) with the dead revert recorded under `withdrawn` (blame_records reads it: that
    revert never lands again), the revived lander's lease is stopped, and the worktree blame made for the revert is
    removed. A revert that already landed is not withdrawn: that is a refutation (FIX FAILED relands it). (ok, line)."""
    d = it["data"]
    b = d.get("blame") or {}
    g = blame_culprit(b)
    if not g or b.get("state") not in ("confirmed",):
        return False, f"NOT WITHDRAWN: {red_what(d)} has no confirmed culprit (blame {b.get('state') or 'none'})"
    rv = b.get("revert") or {}
    ch = blame_change_key(g)
    repo = repo or movies_repo_path()
    landed = _landed_revert(repo, rv, ch)
    if landed:
        return False, landed
    nb = _withdrawn_blame(b, ch, rv, why)
    red_stop_holder(store, d["key"], f"the blame of {ch} was withdrawn: {why}", "blame withdrawn", force=True)
    dropped = blame_drop_revert_worktree(repo, rv.get("worktree"), ch, why, store) if rv.get("worktree") else "no revert worktree"
    line = (f"RED BLAME WITHDRAWN: {red_what(d)}: {blame_change_name(g)} is not the culprit ({why}); its revert never "
            f"lands; {dropped}; the normal fixer flow")
    inbox_write("red", line, [line], [], 0)
    red_dispatch_log(d, line)
    _record_withdrawal(store, it, nb, line)
    return True, line
