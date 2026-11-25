# agentctl, part `culpritfix`: a confirmed culprit's remedy: its revert landed as the gate's fix, its re-land, its lander revived on its change
# A real module (lib/agentctl/__init__.py MODULES): its own imports, the standard library's and MODULES' only.

import os
import subprocess
import sys
from agentctl.procrunner import process_runner
from agentctl.core import HOME, NO_SIGN, git, reds_dir, remote
from agentctl.timefmt import ts
from agentctl.selfcode import TOOL_PATH
from agentctl.redrecord import red_what
from agentctl.claudefiles import bg_transcript
from agentctl.lander import blame_change_name, blame_decision, lander_name
from agentctl.allowedpaths import allowed_path
from agentctl.inboxentry import inbox_write
from agentctl.land import spawn_land
from agentctl.worktrees import WorktreeGuard, fresh_worktree
from agentctl.fixerenv import fixer_env
from agentctl.redreplay import agentctl_revert_fix_replay
from agentctl.redrepos import red_owner_only
from agentctl.fixerland import dispatch_log, red_dispatch


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
    name = g.get("change") or g["commits"][0]
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


def blame_note(b):
    """What a fresh fixer is told of a red's blame (its change's diff and worktree), or ''."""
    b = b or {}
    g = blame_culprit(b)
    if not g:
        return ""
    rv = b.get("revert") or {}
    shas = " ".join(c[:10] for c in g["commits"])
    if b.get("state") == "refuted":
        return (f" THE BLAME WAS WRONG: {blame_change_name(g)} ({shas}) was reverted and that did not clear this red, so "
                f"it is being relanded ({(b.get('reland') or {}).get('msg') or 'due'}). Look elsewhere for the cause.")
    if rv.get("ok"):
        return (f" THE OFFENDING CHANGE is {blame_change_name(g)} ({shas}: `git show {shas}`), landed from "
                f"{str(g.get('worktree') or '?').replace(HOME, '~')}; its parent passes the same check, its end fails "
                f"({str(b.get('evidence') or '')[:200]}). It was REVERTED ({rv.get('msg')}): your job is to make it land "
                f"again, fixed -- in your worktree, after `git fetch {remote()} && git rebase {remote()}/main`, "
                f"`git revert <the revert's sha>` re-applies it; add the fix and land it (`Fixes-Red:` not needed: the "
                f"revert fixed the red). Its lander could not be revived: {b.get('revive_failed') or 'none recorded'}.")
    return (f" THE OFFENDING CHANGE is {blame_change_name(g)} ({shas}: `git show {shas}`), landed from "
            f"{str(g.get('worktree') or '?').replace(HOME, '~')}; its parent passes the same check, its end fails. Its "
            f"revert could not be landed ({rv.get('msg') or 'not tried'}): fix it forward (`Fixes-Red:`).")


def blame_status(d):
    """The red's blame for the status line and `red status`: (text, short) or None."""
    b = d.get("blame") or {}
    g = blame_culprit(b)
    if not g:
        return None
    rv = b.get("revert") or {}
    who = lander_name(g.get("lander")) if (g.get("lander") or {}).get("session") else "the owner (by hand)"
    if b.get("state") == "refuted":
        return (f"blame refuted: the revert of {blame_change_name(g)} did not clear it; relanded",
                f"revert of {g.get('change') or g['commits'][0][:9]} refuted")
    if rv.get("ok"):
        rev = "; lander revived" if b.get("revived") else ""
        return (f"REVERTED {blame_change_name(g)} ({rv.get('msg')}), landed by {who}{rev}",
                f"reverted {g.get('change') or g['commits'][0][:9]}{rev}")
    if rv:
        return f"revert of {blame_change_name(g)} failed: {rv.get('msg')}", "revert failed"
    return f"culprit: {blame_change_name(g)}, landed by {who}", f"culprit {g.get('change') or g['commits'][0][:9]}"


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
    line = (f"RED REVERT: {red_what(pic)}: {blame_change_name(g)} ({', '.join(c[:10] for c in g['commits'][:5])}), "
            f"landed by {lander_name(g.get('lander')) if who == 'lander' else 'the owner, by hand'}: {rv['msg']}")
    if who == "owner" and rv.get("ok"):
        line += (" -- OWNER: it was yours (no agent session landed it); re-apply it with a fix once you know why "
                 f"(`git revert <the revert>`), see `~/bin/agentctl ci-why {u['run'].get('databaseId')}`")
    inbox_write("red", line, [line], [], 0 if rv.get("ok") and who != "owner" else 1)
    dispatch_log(u, line)
    store.note(off["lease"], took["epoch"], took["secret"], line[:300])
    it = store.item("red", it_id)
    if it:
        store.update_item("red", it_id, it["version"], {"blame": b, "lander_tried": True})
    if who == "owner" and rv.get("ok"):
        return True, line  # nobody to revive: the lease lapses by its TTL, the revert's Fixes-Red holds the red meanwhile
    if who == "lander" and (rv.get("ok") or rv.get("conflict")):  # a revert that conflicts: it fixes forward instead
        return lander_revive(store, o_r, repo, off, took, u, b)
    b["revive_failed"] = "its revert did not land" if not rv.get("ok") else "an owner's change"
    return lander_fallback(store, o_r, repo, off, took, u, b, f"revert failed ({rv['msg'][:120]})")


def lander_fallback(store, o_r, repo, off, took, u, b, why):
    """A FRESH FIXER, under the same lease: the decision rule's only other way -- the culprit or its lander could not be
    acted on. Briefed with the change (blame_note)."""
    it_id = off["item"].split(":", 1)[1]
    it = store.item("red", it_id)
    if it:
        store.update_item("red", it_id, it["version"], {"blame": b, "lander_tried": True})
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


def lander_revive(store, o_r, repo, off, took, u, b):
    """THE LANDER REVIVED, under the red's lease (tier `lander`): its own Claude session resumed in its worktree
    (`claude --bg --resume <session>`, launched in the session's own cwd); a SUBAGENT -- which has no session of its own
    to resume -- as a session in its worktree that reads its transcript first. Told the red run, the failing step and
    lines (ci-why), that its change was reverted, and to fix it, rebase, re-apply it with the fix and land again. No
    session, its transcript gone, no worktree to work in, or the launch refused: a fresh fixer (lander_fallback)."""
    g = blame_culprit(b)
    l = g.get("lander") or {}
    num, rid = u["run"].get("number"), u["run"].get("databaseId")
    sid = l.get("session")
    tr = l.get("transcript") if l.get("transcript") and os.path.exists(l["transcript"]) else \
        (None if l.get("agent") else bg_transcript(sid) if sid else None)
    if not sid:
        return lander_fallback(store, o_r, repo, off, took, u, dict(b, revive_failed="no session recorded"),
                               "no lander session")
    if not tr:
        why = f"its transcript is gone ({l.get('transcript') or 'session ' + sid[:8]})"
        return lander_fallback(store, o_r, repo, off, took, u, dict(b, revive_failed=why), why)
    wt, moved = l.get("worktree"), None
    if not wt or not os.path.isdir(wt) or not allowed_path(wt):
        moved = wt
        wt, why = fresh_worktree(repo, "lander", num)
        if not wt:
            return lander_fallback(store, o_r, repo, off, took, u, dict(b, revive_failed=why), why)
    rv = b.get("revert") or {}
    name = g.get("change") or g["commits"][0]
    brief = {"mode": "transcript" if l.get("agent") else "resume", "session": sid, "agent": l.get("agent"),
             "transcript": tr, "change": name, "shas": [c[:10] for c in g["commits"]], "subject": g.get("subject"),
             "revert": rv.get("msg"), "reverted": bool(rv.get("ok")), "worktree": wt, "moved_from": moved,
             "evidence": b.get("evidence"),
             "ci_why": lander_ci_why(rid) if rid else ""}
    cwd = l.get("session_cwd") if not l.get("agent") and l.get("session_cwd") and os.path.isdir(l["session_cwd"]) else wt
    resume = {"worktree": wt, "note": None, "launch_cwd": cwd}
    if not l.get("agent"):
        resume["session"] = sid
    ok, msg = red_dispatch(o_r, repo, dict(u, lander=brief, blame_note=None),
                           why=f"tier lander: {lander_name(l)} revived", resume=resume, lease=off["lease"], took=took)
    if not ok:
        return lander_fallback(store, o_r, repo, off, took, u, dict(b, revive_failed=msg), f"revival failed ({msg[:120]})")
    it = store.item("red", off["item"].split(":", 1)[1])
    if it:
        store.update_item("red", it["id"], it["version"], {"blame": dict(b, revived={"at": ts(), "how": brief["mode"],
                                                                                    "worktree": wt})})
    line = (f"LANDER REVIVED: {red_what(u)}: {lander_name(l)} -- its change {name} "
            f"{'was reverted' if rv.get('ok') else 'could not be reverted (it fixes forward)'} -- "
            f"{'resumed' if brief['mode'] == 'resume' else 'continued from its transcript'} in {wt.replace(HOME, '~')} "
            "to fix it and land it again (`claude agents`)")
    inbox_write("red", line, [line], [], 0, cwd=wt)
    return True, line
