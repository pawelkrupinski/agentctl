# agentctl, part `culpritfix`: a confirmed culprit's remedy: its revert landed as the gate's fix, its re-land, its lander revived on its change
# A real module (lib/agentctl/__init__.py MODULES): its own imports, the standard library's and MODULES' only.

import os
import subprocess
import sys
from typing import Any
from agentctl.procrunner import process_runner
from agentctl.selfcode import TOOL_PATH
from agentctl.timefmt import ts
from agentctl.core import NO_SIGN, git, remote
from agentctl.claudefiles import bg_transcript
from agentctl.allowedpaths import allowed_path
from agentctl.paths import reds_dir, tilde
from agentctl.inboxentry import inbox_write
from agentctl.redrepos import red_owner_only
from agentctl.worktreelife import WorktreeGuard, fresh_worktree
from agentctl.fixerenv import fixer_env
from agentctl.lander import blame_change_key, blame_change_name, blame_decision, lander_name
from agentctl.redrecord import red_what
from agentctl.land import spawn_land
from agentctl.fixerland import dispatch_log, red_dispatch
from agentctl.redland import agentctl_revert_fix_replay


# -- 3. the revert: the culprit's commits undone, landed as a gate fix
def blame_culprit(b):
    """The culprit's group (confirmed, or refuted since), or None."""
    b = b or {}
    return b["groups"][b["culprit"]] if b.get("culprit") is not None and b.get("groups") else None


def blame_revert(o_r, repo, u, b):
    """ONE `git revert` of the culprit's commits (newest first, one commit) with `Reverts-Change:` and `Fixes-Red:`,
    landed by a detached `agentctl land` (a gate fix: it lands while main is red, through the normal hook). Returns
    {ok, msg, worktree, at}; a revert that does not apply is aborted (ok False)."""
    g = blame_culprit(b)
    num = u["run"].get("number")
    rec = {"at": ts(), "commits": g["commits"], "change": g.get("change")}
    if red_owner_only(u.get("repo") or o_r):
        return {**rec, "ok": False, "msg": f"no revert in {u.get('repo') or o_r}: its pushes are the owner's"}
    wt, why = fresh_worktree(repo, "revert", num)
    if not wt:
        return {**rec, "ok": False, "msg": why}
    with WorktreeGuard(wt):  # EVERY EXIT PUTS THE WORKTREE BACK (slots.py) -- the land starts after it, never inside
        failed = blame_revert_commit(wt, u, b, g, rec, num)
    if failed:
        return failed
    pid = spawn_land(wt, fixer_env(o_r, num), reds_dir(f"revert-{num}.log"))
    return {**rec, "ok": True, "worktree": wt, "sha": git(wt, "rev-parse", "HEAD").strip(),
            "msg": f"landing a revert of {blame_change_name(g)} from {wt} (pid {pid})"}


def blame_revert_commit(wt, u, b, g, rec, num):
    """blame_revert's revert and commit in its fresh worktree wt: None when committed, else the failed record."""
    r = git(wt, *NO_SIGN, "revert", "--no-commit", *reversed(g["commits"]), check=False)
    if r.returncode != 0:
        git(wt, "revert", "--abort", check=False)
        git(wt, "reset", "-q", "--hard", f"{remote()}/main", check=False)
        return {**rec, "ok": False, "worktree": wt, "conflict": True,
                "msg": f"the revert of {blame_change_name(g)} does not apply ({(r.stderr or r.stdout).strip()[:160]})"}
    name = blame_change_key(g)
    msg = (f"Revert {blame_change_name(g)}: {str(g.get('subject') or '')[:60]}\n\n"
           f"agentctl blame: {u['workflow']} #{num} turned red with it; its parent {str(b.get('parent'))[:10]} passes the "
           f"same check, its end fails ({str(b.get('evidence') or '')[:300]}).\nReverted: "
           f"{', '.join(c[:10] for c in g['commits'])}. Its lander, {lander_name(g.get('lander'))}, re-applies it with "
           f"its fix.\n\n" + "".join(f"This reverts commit {c}.\n" for c in reversed(g["commits"]))  # prepush-check's
           + f"\nReverts-Change: {name}\nFixes-Red: {num}\n"
           + agentctl_revert_fix_replay(f"agentctl blame's revert of {blame_change_name(g)}: {u['workflow']} #{num}'s "
                                        f"parent {str(b.get('parent'))[:10]} passed the same check, so the revert "
                                        "restores passing code; nothing new to replay")
           + "Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>\n")
    c = git(wt, *NO_SIGN, "commit", "-q", "-m", msg, check=False)
    if c.returncode != 0:
        return {**rec, "ok": False, "worktree": wt, "msg": f"git commit of the revert failed ({c.stderr.strip()[:160]})"}
    return None


def blame_reland(o_r, repo, u, b):
    """The revert did not clear the red, so the culprit was wrong: its revert reverted (`Relands-Change:`), landed by
    `agentctl land` (no Fixes-Red: a production change, it lands at the next green). {ok, msg, worktree, at}."""
    revert = b.get("revert") or {}
    rv = revert.get("landed") or revert.get("sha")
    num = u["run"].get("number")
    g = blame_culprit(b) or {}
    if not rv:
        return {"ok": False, "at": ts(), "msg": "no landed revert to undo"}
    wt, why = fresh_worktree(repo, "reland", num)
    if not wt:
        return {"ok": False, "at": ts(), "msg": why}
    r = git(wt, *NO_SIGN, "revert", "--no-edit", rv, check=False)
    if r.returncode != 0:
        git(wt, "revert", "--abort", check=False)
        return {"ok": False, "at": ts(), "worktree": wt, "msg": f"the revert {rv[:10]} cannot be undone ({(r.stderr or r.stdout).strip()[:160]})"}
    body = git(wt, "log", "-1", "--format=%B").strip()
    git(wt, *NO_SIGN, "commit", "--amend", "-q", "-m", f"{body}\n\nagentctl blame: reverting {rv[:10]} did not clear {u['workflow']} "
        f"#{num}: {blame_change_name(g)} was not its culprit, so it is relanded.\n\nRelands-Change: "
        f"{g.get('change') or (g.get('commits') or ['?'])[0]}\nCo-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>\n")
    pid = spawn_land(wt, fixer_env(o_r, num), reds_dir(f"reland-{num}.log"))
    return {"ok": True, "at": ts(), "worktree": wt, "msg": f"relanding {blame_change_name(g)} (reverting {rv[:10]}) from {wt} (pid {pid})"}


def _refuted_note(g: Any, shas: Any, b: Any) -> Any:
    """The fixer's note when the revert of the blamed change did not clear the red."""
    return (f" THE BLAME WAS WRONG: {blame_change_name(g)} ({shas}) was reverted and that did not clear this red, so "
            f"it is being relanded ({(b.get('reland') or {}).get('msg') or 'due'}). Look elsewhere for the cause.")


def _offending_note(g: Any, shas: Any, b: Any, rv: Any) -> Any:
    """The fixer's note naming the offending change: reverted (rv ok) or not."""
    offending = (f" THE OFFENDING CHANGE is {blame_change_name(g)} ({shas}: `git show {shas}`), landed from "
                 f"{tilde(str(g.get('worktree') or '?'))}; its parent passes the same check, its end fails")
    if rv.get("ok"):
        return (f"{offending} ({str(b.get('evidence') or '')[:200]}). It was REVERTED ({rv.get('msg')}): your job is to "
                f"make it land again, fixed -- in your worktree, after `git fetch {remote()} && git rebase "
                f"{remote()}/main`, `git revert <the revert's sha>` re-applies it; add the fix and land it "
                f"(`Fixes-Red:` not needed: the revert fixed the red). Its lander could not be revived: "
                f"{b.get('revive_failed') or 'none recorded'}.")
    return (f"{offending}. Its revert could not be landed ({rv.get('msg') or 'not tried'}): fix it forward "
            f"(`Fixes-Red:`).")


def blame_note(b):
    """What a fresh fixer is told of a red's blame (its change's diff and worktree), or ''."""
    b = b or {}
    g = blame_culprit(b)
    if not g:
        return ""
    shas = " ".join(c[:10] for c in g["commits"])
    if b.get("state") == "refuted":
        return _refuted_note(g, shas, b)
    return _offending_note(g, shas, b, b.get("revert") or {})


def _lander_text(g: Any) -> Any:
    """Who landed the blamed change: its lander's name, or the owner by hand when no session did."""
    return lander_name(g.get("lander")) if (g.get("lander") or {}).get("session") else "the owner (by hand)"


def blame_status(d):
    """The red's blame for the status line and `red status`: (text, short) or None."""
    b = d.get("blame") or {}
    g = blame_culprit(b)
    if not g:
        return None
    rv = b.get("revert") or {}
    who = _lander_text(g)
    name, short = blame_change_name(g), g.get("change") or g["commits"][0][:9]
    if b.get("state") == "refuted":
        return f"blame refuted: the revert of {name} did not clear it; relanded", f"revert of {short} refuted"
    if rv.get("ok"):
        rev = "; lander revived" if b.get("revived") else ""
        return f"REVERTED {name} ({rv.get('msg')}), landed by {who}{rev}", f"reverted {short}{rev}"
    if rv:
        return f"revert of {name} failed: {rv.get('msg')}", "revert failed"
    return f"culprit: {name}, landed by {who}", f"culprit {short}"


def _note_lander_tried(store: Any, it_id: Any, b: Any) -> Any:
    """The red's item carries the blame record and that its lander was tried."""
    it = store.item("red", it_id)
    if it:
        store.update_item("red", it_id, it["version"], {"blame": b, "lander_tried": True})


def _revert_line(pic: Any, g: Any, who: Any, rv: Any, u: Any) -> Any:
    line = (f"RED REVERT: {red_what(pic)}: {blame_change_name(g)} ({', '.join(c[:10] for c in g['commits'][:5])}), "
            f"landed by {lander_name(g.get('lander')) if who == 'lander' else 'the owner, by hand'}: {rv['msg']}")
    if who == "owner" and rv.get("ok"):
        line += (" -- OWNER: it was yours (no agent session landed it); re-apply it with a fix once you know why "
                 f"(`git revert <the revert>`), see `~/bin/agentctl ci-why {u['run'].get('databaseId')}`")
    return line


def _announce_revert(store: Any, off: Any, took: Any, u: Any, line: Any, quiet: Any) -> Any:
    inbox_write("red", line, [line], [], 0 if quiet else 1)
    dispatch_log(u, line)
    store.note(off["lease"], took["epoch"], took["secret"], line[:300])


def lander_spawn(store, o_r, repo, off, took, u):
    """Tier `lander` (the decision rule's determined culprit), under the taken lease: the culprit reverted, then its
    lander revived; an owner's change (no session): reverted and the owner told. Whatever cannot be done falls back to
    a fresh fixer under the same lease, briefed with the change (never a loop). (ok, message)."""
    pic = off["picture"]
    b = dict(pic.get("blame") or {})
    num = u["run"].get("number")
    it_id = off["item"].split(":", 1)[1]
    g = blame_culprit(b)
    who, why = blame_decision(b)
    rv = b.get("revert") or blame_revert(o_r, repo, u, b)
    b["revert"] = rv
    line = _revert_line(pic, g, who, rv, u)
    _announce_revert(store, off, took, u, line, bool(rv.get("ok")) and who != "owner")
    _note_lander_tried(store, it_id, b)
    if who == "owner" and rv.get("ok"):
        return True, line  # nobody to revive: the lease lapses by its TTL, the revert's Fixes-Red holds the red meanwhile
    if who == "lander" and (rv.get("ok") or rv.get("conflict")):  # a revert that conflicts: it fixes forward instead
        return lander_revive(store, o_r, repo, off, took, u, b)
    b["revive_failed"] = "its revert did not land" if not rv.get("ok") else "an owner's change"
    return lander_fallback(store, o_r, repo, off, took, u, b, f"revert failed ({rv['msg'][:120]})")


def lander_fallback(store, o_r, repo, off, took, u, b, why):
    """A FRESH FIXER, under the same lease: the decision rule's only other way -- the culprit or its lander could not be
    acted on. Briefed with the change (blame_note)."""
    _note_lander_tried(store, off["item"].split(":", 1)[1], b)
    u = dict(u, blame_note=blame_note(b))
    return red_dispatch(o_r, repo, u, why=f"tier lander: {why} -> a fresh fixer", lease=off["lease"], took=took)


# -- 4. the lander revived
def lander_ci_why(rid, limit=25):
    """The failing step and its log lines, as `agentctl ci-why <run id>` says them (bounded; '' when it cannot)."""
    try:
        r = process_runner().run([sys.executable, TOOL_PATH, "ci-why", str(rid)], capture_output=True, text=True, timeout=90,
                           stdin=subprocess.DEVNULL, env={**os.environ, "AGENTCTL_DETACHED": "1"})
        return "\n".join((r.stdout or "").strip().splitlines()[-limit:])
    except (OSError, subprocess.TimeoutExpired):
        return ""


class LanderRevival:
    """THE LANDER REVIVED, one stage a method: its session and transcript, a worktree to work in, the brief, the
    launch (red_dispatch), the red's record and the inbox line. A stage that cannot go on hands the red to a fresh
    fixer (lander_fallback), saying why."""

    def __init__(self, store: Any, o_r: Any, repo: Any, off: Any, took: Any, u: Any, b: Any) -> None:
        self.store, self.o_r, self.repo, self.off, self.took, self.u, self.b = store, o_r, repo, off, took, u, b
        self.g = blame_culprit(b)
        self.lander = self.g.get("lander") or {}

    def fall_back(self, failed: Any, why: str) -> Any:
        return lander_fallback(self.store, self.o_r, self.repo, self.off, self.took, self.u,
                               dict(self.b, revive_failed=failed), why)

    def transcript(self, sid: Any) -> Any:
        """The session's transcript: the lander's recorded one when it is there, else (not a subagent's) the background
        session's; None when there is none."""
        l = self.lander
        if l.get("transcript") and os.path.exists(l["transcript"]):
            return l["transcript"]
        return None if l.get("agent") else bg_transcript(sid) if sid else None

    def worktree(self) -> tuple[Any, Any, Any]:
        """(worktree, the one it moved from, why none): the lander's own when it is there and allowed, else a fresh one."""
        wt = self.lander.get("worktree")
        if wt and os.path.isdir(wt) and allowed_path(wt):
            return wt, None, None
        fresh, why = fresh_worktree(self.repo, "lander", self.u["run"].get("number"))
        return fresh, wt, why

    def brief(self, sid: Any, tr: Any, wt: Any, moved: Any) -> dict[str, Any]:
        l, g, b = self.lander, self.g, self.b
        rv = b.get("revert") or {}
        rid = self.u["run"].get("databaseId")
        return {"mode": "transcript" if l.get("agent") else "resume", "session": sid, "agent": l.get("agent"),
                "transcript": tr, "change": blame_change_key(g), "shas": [c[:10] for c in g["commits"]],
                "subject": g.get("subject"), "revert": rv.get("msg"), "reverted": bool(rv.get("ok")), "worktree": wt,
                "moved_from": moved, "evidence": b.get("evidence"), "ci_why": lander_ci_why(rid) if rid else ""}

    def launch_cwd(self, wt: Any) -> Any:
        """Where the session is resumed: its own cwd, when it is there (a subagent has none), else the worktree."""
        l = self.lander
        return l.get("session_cwd") if not l.get("agent") and l.get("session_cwd") and os.path.isdir(l["session_cwd"]) else wt

    def dispatch(self, brief: dict[str, Any], wt: Any, cwd: Any) -> tuple[bool, str]:
        resume = {"worktree": wt, "note": None, "launch_cwd": cwd}
        if not self.lander.get("agent"):
            resume["session"] = brief["session"]
        return red_dispatch(self.o_r, self.repo, dict(self.u, lander=brief, blame_note=None),
                            why=f"tier lander: {lander_name(self.lander)} revived", resume=resume,
                            lease=self.off["lease"], took=self.took)

    def record(self, brief: dict[str, Any], wt: Any) -> None:
        it = self.store.item("red", self.off["item"].split(":", 1)[1])
        if it:
            self.store.update_item("red", it["id"], it["version"], {"blame": dict(
                self.b, revived={"at": ts(), "how": brief["mode"], "worktree": wt})})

    def announce(self, brief: dict[str, Any], wt: Any, cwd: Any) -> str:
        """The inbox line saying the lander was revived (written under the worktree), returned as the verdict."""
        reverted = "was reverted" if brief["reverted"] else "could not be reverted (it fixes forward)"
        line = (f"LANDER REVIVED: {red_what(self.u)}: {lander_name(self.lander)} -- its change {brief['change']} {reverted} -- "
                f"{'resumed' if brief['mode'] == 'resume' else 'continued from its transcript'} in {tilde(wt)} "
                "to fix it and land it again (`claude agents`)")
        inbox_write("red", line, [line], [], 0, cwd=wt)
        return line

    def revive(self) -> Any:
        sid = self.lander.get("session")
        tr = self.transcript(sid)
        if not sid:
            return self.fall_back("no session recorded", "no lander session")
        if not tr:
            why = f"its transcript is gone ({self.lander.get('transcript') or 'session ' + sid[:8]})"
            return self.fall_back(why, why)
        wt, moved, why = self.worktree()
        if not wt:
            return self.fall_back(why, why)
        brief = self.brief(sid, tr, wt, moved)
        cwd = self.launch_cwd(wt)
        ok, msg = self.dispatch(brief, wt, cwd)
        if not ok:
            return self.fall_back(msg, f"revival failed ({msg[:120]})")
        self.record(brief, wt)
        return True, self.announce(brief, wt, cwd)


def lander_revive(store, o_r, repo, off, took, u, b):
    """THE LANDER REVIVED, under the red's lease (tier `lander`): its own Claude session resumed in its worktree
    (`claude --bg --resume <session>`, launched in the session's own cwd); a SUBAGENT -- which has no session of its own
    to resume -- as a session in its worktree that reads its transcript first. Told the red run, the failing step and
    lines (ci-why), that its change was reverted, and to fix it, rebase, re-apply it with the fix and land again. No
    session, its transcript gone, no worktree to work in, or the launch refused: a fresh fixer (lander_fallback)."""
    return LanderRevival(store, o_r, repo, off, took, u, b).revive()
