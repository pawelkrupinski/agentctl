# agentctl, part `lander`: who landed what -- every `agentctl land` remembers its lander, in the store.
# A real module (lib/agentctl/__init__.py MODULES): its own imports, the standard library's and MODULES' only.

import datetime as dt
import glob
import json
import os
import re
import time
from typing import Any
from contextlib import suppress
from agentctl.procrunner import process_runner
from agentctl.core import HOME, branch_of, env_num, err, git, git_ok, process_cache, remote, slug
from agentctl.timefmt import ts
from agentctl.statefile import mtime
from agentctl.repos import RED_PRIMARY
from agentctl.storetoken import no_vault
from agentctl.claudefiles import claude_projects_root
from agentctl.statemachines import ARCHIVE, MACHINES, StaleVersion, StateSpec


# ---------------------------------------------------------------- the lander (the owner, 2026-11-01: "remember which
# agent landed what change"). Every `agentctl land` records who ran it (lander_record): the Claude session
# ($CLAUDE_CODE_SESSION_ID), the subagent when the land ran inside one (its transcript under <session>/subagents/: the
# session id alone names the orchestrator, which spawned it), the transcript, the worktree, the branch, the cwd -- and
# each commit's KEYS (sha, patch-id, subject, author time: a landing rebases, so a commit on main is found again by its
# patch-id or its subject and time, never by its sha alone). A queued change carries it in its change item; a direct
# land (no queue: the other repos) writes a `lander` item. Both live in the reconciler's store for 14 days, across
# restarts and batches (one push carries several changes: each commit maps to its own change). Once a change has
# landed, the tick notes its commits on main (`landed_commits`: main sha -> patch-id). The blame part reads it.
LANDER_TRANSCRIPT_WINDOW = 600   # s: a transcript written this recently that ran `agentctl land` is the lander's
LANDER_LANDED_EVERY = 300        # s between the tick's notes of landed changes' commits on main ($AGENTCTL_LANDER_LANDED_EVERY)
LANDER_KEEP = 14 * 86400         # s a lander record is kept (the store's ARCHIVE)
MACHINES["lander"] = {
    "recorded": StateSpec(LANDER_KEEP, "gone", ("gone",)),
    "gone": StateSpec(ARCHIVE, "gone", (), terminal=True),
}


LAND_CALL = re.compile(r"agentctl land(?![ \t]+(status|hold|release|cancel|--help)\b)(?=[\s;&|)'\"]|$)")


def _tail_lines(path, size=256 * 1024):
    """The transcript's JSON entries in its last `size` bytes (a partial first line dropped)."""
    try:
        with open(path, "rb") as f:
            n = os.path.getsize(path)
            f.seek(max(0, n - size))
            data = f.read().decode(errors="replace")
    except OSError:
        return []
    lines = data.splitlines()[1:] if n > size else data.splitlines()
    out = []
    for line in lines:
        try:
            out.append(json.loads(line))
        except ValueError:
            continue
    return out


def land_calls(path):
    """[(timestamp, command)] of the Bash calls in a transcript's tail that run `agentctl land` (not land status etc.):
    a call, never a mention -- a subagent that only read about land is no lander (2026-11-17)."""
    out = []
    for d in _tail_lines(path):
        msg = d.get("message") if isinstance(d, dict) else None
        content = msg.get("content") if isinstance(msg, dict) else None
        for c in content if isinstance(content, list) else []:
            cmd = (c.get("input") or {}).get("command") if isinstance(c, dict) and c.get("type") == "tool_use" else None
            if isinstance(cmd, str) and LAND_CALL.search(cmd):
                out.append((str(d.get("timestamp") or ""), cmd))
    return out


def shell_args_text(args):
    """A process's `ps` command line as the shell got it: ps's \\012 / \\011 escapes and the eval wrapper's '"'"'
    quoting undone, so a Bash tool call's command reads as a substring of its shell's."""
    return args.replace("'\"'\"'", "'").replace("\\012", "\n").replace("\\011", "\t")


def process_ancestry(stop_pid=None, depth=8):
    """The command lines (shell_args_text) of this process's ancestors up to the Claude process (stop_pid): the
    calling Bash tool call's shell is among them."""
    out, pid = [], os.getppid()
    for _ in range(depth):
        if pid <= 1 or pid == stop_pid:
            break
        r = process_runner().run(["ps", "-ww", "-o", "ppid=,args=", "-p", str(pid)], capture_output=True, text=True)
        parts = r.stdout.strip().split(None, 1)
        if r.returncode != 0 or len(parts) < 2 or not parts[0].isdigit():
            break
        out.append(shell_args_text(parts[1]))
        pid = int(parts[0])
    return out


def lander_transcript(sid, now_=None, worktree=None, ancestry=None):
    """(transcript path, subagent id) of the session `sid`'s agent that runs THIS land: among the session's own
    transcript (<projects>/*/<sid>.jsonl) and its subagents' (<sid>/subagents/agent-<id>.jsonl) written in the last
    LANDER_TRANSCRIPT_WINDOW s, the one whose Bash call runs `agentctl land` (land_calls) -- first the call this
    process runs under (its command in an ancestor's command line: `ancestry`, default process_ancestry()), else a call
    naming the land's worktree, else any land call; the newest call in that tier. A session id alone names the
    orchestrator and every subagent alike, and the newest transcript is often not the lander's (2026-11-17: a
    read-only analysis subagent quoting AGENTCTL.md was recorded). None of them ran land: the session's own
    transcript; (None, None) when there is none."""
    if not sid:
        return None, None
    t = now_ if now_ is not None else time.time()
    root = claude_projects_root()
    main = glob.glob(os.path.join(root, "*", f"{sid}.jsonl"))
    subs = glob.glob(os.path.join(root, "*", sid, "subagents", "agent-*.jsonl"))
    if ancestry is None:
        pid = os.environ.get("CLAUDE_PID") or ""
        ancestry = process_ancestry(int(pid) if pid.isdigit() else None)
    wt = os.path.realpath(worktree) if worktree else None
    best = None
    for p in subs + main:
        if t - mtime(p) > LANDER_TRANSCRIPT_WINDOW:
            continue
        for at, cmd in land_calls(p):
            tier = 2 if any(cmd.strip() and cmd.strip() in a for a in ancestry) else \
                1 if wt and (wt in cmd or (worktree or "") in cmd) else 0
            key = (tier, at, mtime(p))
            if best is None or key > best[0]:
                best = (key, p)
    pick = best[1] if best else (main[0] if main else None)
    if not pick:
        return None, None
    base = os.path.basename(pick)
    agent = base[len("agent-"):-len(".jsonl")] if os.sep + "subagents" + os.sep in pick else None
    return pick, agent


def transcript_cwd(path):
    """The cwd a transcript's session ran in (its first entry that says), or None."""
    try:
        with open(path, errors="replace") as f:
            for i, line in enumerate(f):
                if i > 50:
                    break
                try:
                    cwd = json.loads(line).get("cwd")
                except (ValueError, AttributeError):
                    continue
                if cwd:
                    return cwd
    except (OSError, TypeError):
        pass
    return None


def lander_record(repo):
    """Who runs this `agentctl land`: {session, agent, transcript, session_cwd, worktree, branch, cwd, host, at,
    claude_pid, repo}. Read from this process's own environment -- land runs in the lander's session -- never from a
    daemon's."""
    sid = os.environ.get("CLAUDE_CODE_SESSION_ID") or None
    rec = {"session": sid, "worktree": repo, "branch": branch_of(repo), "cwd": os.getcwd(), "host": os.uname().nodename,
           "at": ts(), "claude_pid": int(os.environ["CLAUDE_PID"]) if (os.environ.get("CLAUDE_PID") or "").isdigit() else None}
    try:
        rec["repo"] = slug(repo)
    except (Exception, SystemExit):
        rec["repo"] = None
    if sid:
        tr, agent = lander_transcript(sid, worktree=repo)
        rec.update(transcript=tr, agent=agent, session_cwd=transcript_cwd(tr) if tr else None)
    fx = os.environ.get("AGENTCTL_RED_FIXER")
    if fx:
        rec["fixer"] = fx
    return rec


def patch_ids(repo, shas):
    """{commit sha: stable patch-id} of the given commits (a merge or an empty commit has none)."""
    if not shas:
        return {}
    show = process_runner().run(["git", "-C", repo, "show", "--no-color", "--format=commit %H", "-p"] + list(shas),
                          capture_output=True, text=True, errors="replace")
    if show.returncode != 0:
        return {}
    r = process_runner().run(["git", "-C", repo, "patch-id", "--stable"], input=show.stdout, capture_output=True, text=True)
    out = {}
    for line in r.stdout.splitlines():
        p = line.split()
        if len(p) == 2:
            out[p[1]] = p[0]
    return out


def commit_keys(repo, base, head):
    """[{sha, patch_id, subject, at}] of base..head, oldest first: what finds each commit again on main after a rebase."""
    rng = [f"{base}..{head}"] if base else ["--max-count=200", head]  # no base: main's newest
    r = git(repo, "log", "--reverse", "--no-merges", "--format=%H%x1f%at%x1f%s", *rng, check=False)
    rows = [l.split("\x1f", 2) for l in r.stdout.splitlines() if l.count("\x1f") == 2] if r.returncode == 0 else []
    pids = patch_ids(repo, [x[0] for x in rows])
    return [{"sha": s, "patch_id": pids.get(s), "subject": subj[:200], "at": int(at) if at.isdigit() else None}
            for s, at, subj in rows]


def lander_fields(e):
    """The lander fields a change item carries from its queue entry (its creation copies them)."""
    return {k: e[k] for k in ("branch", "session", "lander", "commit_keys") if e.get(k)}


def lander_put(store, change_id, lander, keys, landed_sha=None):
    """A direct land's record (no change item carries it): a `lander` item."""
    data = {"change": change_id, "lander": lander, "commit_keys": keys, "repo": lander.get("repo")}
    if landed_sha:
        data["landed_sha"] = landed_sha
        data["landed_commits"] = {k["sha"]: k.get("patch_id") for k in keys}
    return store.put_item("lander", change_id, "recorded", data)


def lander_records(store, o_r=None):
    """[{change, lander, commit_keys, landed_commits, state}]: every recorded landing of the last LANDER_KEEP s -- the
    change items (queued landings) and the lander items (direct ones) -- for repo o_r (None: all)."""
    out = []
    for typ in ("change", "lander"):
        try:
            items = store.items(typ)
        except (Exception, SystemExit) as e:
            err(f"blame: could not read the {typ} items ({str(e)[:120]})")
            continue
        for it in items:
            d = it["data"] or {}
            lander = d.get("lander")
            keys = d.get("commit_keys")
            if not keys and not lander:
                continue
            repo = (lander or {}).get("repo") or d.get("repo") or RED_PRIMARY
            if o_r and repo != o_r:
                continue
            out.append({"change": d.get("change") or it["id"], "lander": lander, "commit_keys": keys or [],
                        "landed_commits": d.get("landed_commits") or {}, "state": it["state"], "type": typ,
                        "worktree": d.get("worktree"), "branch": d.get("branch")})
    return out


def _record_index_keys(rec: Any) -> Any:
    """The index keys one lander record answers to (see lander_index)."""
    keys = [("sha", sha) for sha in rec.get("landed_commits") or {}]
    for k in rec.get("commit_keys") or []:
        if k.get("sha"):
            keys.append(("sha", k["sha"]))
        if k.get("patch_id"):
            keys.append(("pid", k["patch_id"]))
        if k.get("subject") and k.get("at"):
            keys.append(("subj", k["subject"], k["at"]))
    return keys


def lander_index(records):
    """{key: record} by main sha, sha, patch-id and (subject, author time); a landed change outranks any other."""
    idx = {}
    for rec in sorted(records, key=lambda r: r["state"] == "landed"):  # landed last: it wins
        idx.update(dict.fromkeys(_record_index_keys(rec), rec))
    return idx


def lander_of_commit(idx, key):
    """The record that landed main commit `key` ({sha, patch_id, subject, at}), or None."""
    for k in (("sha", key.get("sha")), ("pid", key.get("patch_id")), ("subj", key.get("subject"), key.get("at"))):
        if None not in k and k in idx:
            return idx[k]
    return None


_lander_landed_at = process_cache({})  # repo -> when its landed changes were last noted


def lander_note_landed(store, repo, o_r, t, force=False):
    """Every LANDER_LANDED_EVERY s: each change landed in the last day whose commits on main are not noted yet gets them
    (`landed_commits`: main sha -> patch-id), found by the keys it recorded at submission. Returns what it noted."""
    if not force and t - _lander_landed_at.get(repo, 0) < env_num("AGENTCTL_LANDER_LANDED_EVERY", LANDER_LANDED_EVERY):
        return []
    _lander_landed_at[repo] = t
    todo = [it for it in store.items("change") if it["state"] == "landed" and (it["data"] or {}).get("commit_keys")
            and not it["data"].get("landed_commits") and t - float(it["entered_at"]) < 86400]
    if not todo or not repo:
        return []
    main = f"{remote()}/main"
    base = f"{main}~200" if git_ok(repo, "rev-parse", "-q", "--verify", f"{main}~200") else None
    keys = commit_keys(repo, base, main)
    acts = []
    for it in todo:
        idx = lander_index([{"change": it["id"], "commit_keys": it["data"]["commit_keys"], "state": "landed"}])
        hits = {k["sha"]: k.get("patch_id") for k in keys if lander_of_commit(idx, k)}
        if hits:
            with suppress(StaleVersion):
                store.update_item("change", it["id"], it["version"], {"landed_commits": hits})
                acts.append(f"lander: {it['id']} landed as {', '.join(s[:9] for s in hits)}")
    return acts


def lander_note_direct(store, repo, head, keys):
    """A direct land's record (`agentctl land` with no queue): a `lander` item, best effort -- a store that does not
    answer costs the record, never the landing."""
    try:
        cid = f"direct-{dt.datetime.now().strftime('%Y%m%d-%H%M%S')}-{os.path.basename(repo)}-{head[:10]}"
        with no_vault():
            lander_put(store, cid, lander_record(repo), keys or [], landed_sha=head)
        return cid
    except (Exception, SystemExit) as e:
        err(f"lander: not recorded ({str(e)[:160]})")
        return None


def lander_name(l: dict[str, Any] | None) -> str:
    l = l or {}
    who = f"subagent {l['agent']} of session {str(l.get('session'))[:8]}" if l.get("agent") else \
        f"session {str(l.get('session'))[:8]}" if l.get("session") else "the owner"
    return f"{who} ({str(l.get('worktree') or '?').replace(HOME, '~')})"


def blame_decision(b: dict[str, Any] | None) -> tuple[str | None, str]:
    """THE DECISION RULE: (who, why). who: `lander` -- the culprit and its lander are determined: revert it and revive
    the lander; `owner` -- the culprit is determined and nobody's session landed it (the owner by hand, a land outside
    Claude): revert it and tell the owner; `fixer` -- it cannot be determined: a fresh fixer; None -- still deciding."""
    st = (b or {}).get("state")
    if st == "probing":
        return None, "deciding the culprit"
    if st != "confirmed":
        return "fixer", (b or {}).get("why") or "no culprit"
    g = b["groups"][b["culprit"]]
    if not g.get("change") or not (g.get("lander") or {}).get("session"):
        return "owner", f"{blame_change_name(g)} was landed by hand (no agent session recorded)"
    return "lander", f"{blame_change_name(g)} landed by {lander_name(g['lander'])}"


def blame_change_name(g: dict[str, Any]) -> str:
    return f"change {g['change']}" if g.get("change") else f"commit {g['commits'][0][:10]}"
