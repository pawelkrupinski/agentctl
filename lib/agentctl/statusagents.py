# agentctl, part `statusagents`: the status page's agents: each live session and fixer, its worktree's head and dirt, its logs and last words
# A real module (lib/agentctl/__init__.py MODULES): its own imports, the standard library's and MODULES' only.

from agentctl.clock import epoch_now
import glob
import json
import os
import re
import subprocess
import threading
import time
from contextlib import suppress
from typing import Any
from agentctl.procrunner import process_runner
from agentctl.core import env_num, err, process_cache, reds_dir
from agentctl.timefmt import iso_epoch
from agentctl.statefile import mtime, tail_bytes
from agentctl.procs import proc_alive
from agentctl.fixerrecords import fixer_records
from agentctl.live import live_annotate, live_transcript_ok
from agentctl.redrecord import lease_is_live
from agentctl.leases import lease_meta
from agentctl.livestream import live_mask_text


STATUS_LOG_TAIL = 40            # lines of each log shown in the page


STATUS_LOG_MAX = 2 << 20        # bytes /log serves (the file's tail)


STATUS_AGENT_RECENT = 3 * 3600  # s an ended fixer stays on the page


STATUS_AGENTS_MAX = 16


STATUS_LOG_SUFFIXES = (".log", ".out", ".txt")


def status_strip_lease(r, t):
    """A lease row for the page: never its secret."""
    if not r:
        return None
    m = lease_meta(r)
    return {"name": r.get("name"), "holder": r.get("holder") or None, "live": lease_is_live(r, t), "epoch": r.get("epoch"),
            "acquired_at": r.get("acquired_at"), "renewed_at": r.get("renewed_at"), "expires_at": r.get("expires_at"),
            "max_until": r.get("max_until"), "progress": r.get("progress"), "kind": m.get("kind"), "note": m.get("note"),
            "worktree": m.get("worktree"), "pid": m.get("pid"), "tier": m.get("tier"), "run": m.get("run"),
            "workflow": m.get("workflow"), "log": m.get("log")}


def status_log_body(path, max_bytes=None):
    """(/log's body, the file's size): the file's tail, and when it is short (just rotated), the tail of its newest
    rotated part (<path>.1) before it, marked."""
    max_bytes = max_bytes or STATUS_LOG_MAX
    body, size = tail_bytes(path, max_bytes)
    prev = f"{path}.1"
    if size < max_bytes and os.path.isfile(prev):
        older, _s = tail_bytes(prev, max_bytes - size)
        body = older + f"\n---- rotated: the above is the end of {os.path.basename(prev)} ----\n".encode() + body
    return body, size


def status_tail(path, lines=STATUS_LOG_TAIL, max_bytes=64 << 10):
    """The log's last lines, MASKED (live_mask_text): the page serves them."""
    try:
        data = tail_bytes(path, max_bytes)[0].decode("utf-8", "replace")
    except OSError:
        return None
    return live_mask_text("\n".join(data.splitlines()[-lines:]))


def _assistant_texts(content: Any) -> Any:
    """The text blocks of a transcript message's content (a list of blocks, or one string)."""
    if isinstance(content, list):
        return [c.get("text") for c in content if isinstance(c, dict) and c.get("type") == "text" and c.get("text")]
    return [content] if isinstance(content, str) else []


def status_transcript_last(path, max_bytes=256 << 10):
    """(the last assistant text, its time) of a claude transcript (.jsonl): what the agent last said it is doing."""
    try:
        lines = tail_bytes(path, max_bytes)[0].decode("utf-8", "replace").splitlines()
    except (OSError, TypeError):
        return None, None
    for ln in reversed(lines):
        try:
            rec = json.loads(ln)
        except ValueError:
            continue
        if rec.get("type") != "assistant":
            continue
        texts = _assistant_texts((rec.get("message") or {}).get("content"))
        if texts:
            return live_mask_text(texts[-1][-600:]), rec.get("timestamp")
    return None, None


def status_agent_logs(run):
    """The fixer's logs in its run's reds dir (fixer.log, claude-N.log, ...), newest first, each with its tail."""
    if not re.fullmatch(r"\d+", str(run or "")):  # a lease's meta (any store user's): never a path of its own
        return []
    files = [p for p in glob.glob(reds_dir(str(run), "*")) if p.endswith(STATUS_LOG_SUFFIXES) and os.path.isfile(p)]
    files.sort(key=mtime, reverse=True)
    return [{"path": p, "name": os.path.basename(p), "size": os.path.getsize(p), "mtime": mtime(p), "tail": status_tail(p)}
            for p in files[:4]]


# THE AGENTS' DETAILS ARE BOUNDED PER AGENT AND CACHED PER WORKTREE HEAD (2026-10-29: the section was "no answer within
# 16 s" on every refresh -- the processor runs at launchd's background QoS, where three git calls in each of 10 movies
# worktrees, one after the other, took 117 s; the same pass in a shell took 3 s). Each agent's details are read in a
# thread of their own, all at once; the page waits STATUS_AGENT_BOUND s for each and shows a slower one from its last
# read (marked stale) while the read finishes in the background for the next refresh (one read per worktree at a time).
# The commits and their files are read again only when the worktree's HEAD (read from its files, no git) moved; `git
# status` at most every STATUS_DIRTY_TTL s. Logs and transcripts are read by seeking from their end.
STATUS_AGENT_BOUND = 4      # s the page waits for one agent's details ($AGENTCTL_STATUS_AGENT_BOUND)


STATUS_GIT_TIMEOUT = 120    # s one git call may run (in its background read; never the page's wait)


STATUS_DIRTY_TTL = 60       # s a worktree's `git status` is shown again before it is read again


_status_wt = process_cache({})             # worktree -> {"head", "commits", "recent_files", "dirty", "dirty_at", "error", "at"}


_status_wt_reads = process_cache({})       # worktree -> the thread reading it


_status_said = process_cache({})           # transcript path -> ((size, mtime), (text, at))


_status_wt_lock = threading.Lock()


def _git_dirs(wt: Any) -> Any:
    """(git dir, common git dir) of worktree wt read from its files; None when its `.git` file is no gitdir pointer."""
    gd = os.path.join(wt, ".git")
    if os.path.isfile(gd):
        with open(gd) as f:
            line = f.read().strip()
        if not line.startswith("gitdir:"):
            return None
        gd = os.path.normpath(os.path.join(wt, line[7:].strip()))
    common = gd
    if os.path.isfile(os.path.join(gd, "commondir")):
        with open(os.path.join(gd, "commondir")) as f:
            common = os.path.normpath(os.path.join(gd, f.read().strip()))
    return gd, common


def _ref_sha(gd: Any, common: Any, ref: Any) -> Any:
    """The sha a ref names, from its loose file in the git dir or the common one, else from packed-refs."""
    for base in (gd, common):
        with suppress(OSError):
            with open(os.path.join(base, ref)) as f:
                return f.read().strip() or None
    with open(os.path.join(common, "packed-refs")) as f:
        for ln in f:
            parts = ln.split()
            if len(parts) == 2 and parts[1] == ref:
                return parts[0]
    return None


def status_head(wt):
    """The worktree's HEAD sha from its files (no git, no subprocess); None when it cannot be read that way."""
    try:
        dirs = _git_dirs(wt)
        if dirs is None:
            return None
        gd, common = dirs
        with open(os.path.join(gd, "HEAD")) as f:
            head = f.read().strip()
        if not head.startswith("ref:"):
            return head if re.fullmatch(r"[0-9a-f]{40,64}", head) else None
        return _ref_sha(gd, common, head[4:].strip())
    except OSError:
        return None


def status_git(wt, *args, timeout=None):
    r = process_runner().run(["git", "-C", wt] + list(args), capture_output=True, text=True,
                       timeout=timeout or STATUS_GIT_TIMEOUT, stdin=subprocess.DEVNULL,
                       env=dict(os.environ, GIT_TERMINAL_PROMPT="0", GIT_OPTIONAL_LOCKS="0"))
    return r.stdout if r.returncode == 0 else ""


def _worktree_log(wt: Any) -> Any:
    """([the last 3 commits as {sha, when, subject}], the files they touched, newest first, deduplicated) of a worktree."""
    commits = [dict(zip(("sha", "when", "subject"), ln.split("\x1f", 2)))
               for ln in status_git(wt, "log", "-3", "--format=%h\x1f%cr\x1f%s").splitlines() if ln]
    recent = []
    for ln in status_git(wt, "log", "-3", "--name-only", "--format=").splitlines():
        if ln and ln not in recent:
            recent.append(ln)
    return commits, recent


def status_worktree_read(wt, head):
    """One background read of a worktree into _status_wt: the commits again only when HEAD moved (or is unknown)."""
    c = dict(_status_wt.get(wt) or {})
    try:
        if head is None or c.get("head") != head or "commits" not in c:
            commits, recent = _worktree_log(wt)
            c.update(head=head, commits=commits, recent_files=recent[:20])
        if epoch_now() - float(c.get("dirty_at") or 0) >= STATUS_DIRTY_TTL:
            c["dirty"] = [ln for ln in status_git(wt, "status", "--porcelain").splitlines() if ln][:20]
            c["dirty_at"] = epoch_now()
        c.pop("error", None)
    except (OSError, subprocess.SubprocessError) as e:
        c["error"] = str(e)[:200]
    c["at"] = epoch_now()
    with _status_wt_lock:
        _status_wt[wt] = c


STATUS_GIT_FIELDS = ("commits", "recent_files", "dirty")  # what a worktree's git read shows on the page


def _worktree_fresh(c: Any, head: Any) -> bool:
    """The cached read holds the commits of this HEAD and its status is still young."""
    return bool(c and "commits" in c and head is not None and c.get("head") == head
                and epoch_now() - float(c.get("dirty_at") or 0) < STATUS_DIRTY_TTL)


def _read_and_wait(wt: str, head: Any, bound: float) -> Any:
    """Start the worktree's read (one at a time: a running one is joined) and wait `bound` s for it; the thread."""
    with _status_wt_lock:
        th = _status_wt_reads.get(wt)
        if not (th and th.is_alive()):
            th = threading.Thread(target=status_worktree_read, args=(wt, head), daemon=True, name="status:git")
            _status_wt_reads[wt] = th
            th.start()
    th.join(bound)
    return th


def _still_reading(c: Any, bound: float) -> dict[str, Any]:
    """A read slower than the bound: shown from the last one, `git_stale` saying so."""
    out = {k: (c or {}).get(k) for k in STATUS_GIT_FIELDS}
    out["git_stale"] = (f"git still reading after {bound:.0f} s; shown from the read at "
                        f"{time.strftime('%H:%M:%S', time.localtime(c['at']))}" if c
                        else f"git still reading after {bound:.0f} s; shown at a later refresh")
    return out


def _worktree_view(c: dict[str, Any], head: Any) -> dict[str, Any]:
    """The cached read's fields, with its error and whether HEAD moved since."""
    out = {k: c.get(k) for k in STATUS_GIT_FIELDS}
    if c.get("error"):
        out["git_error"] = c["error"]
    if c.get("head") != head:
        out["git_stale"] = "HEAD moved since the last read"
    return out


def status_worktree(wt, bound):
    """{commits, recent_files, dirty} of a worktree: from the cache when its HEAD has not moved and its status is young;
    else from a read (one per worktree at a time) the caller waits `bound` s for -- a slower read is shown from the last
    one, `git_stale` saying so, and fills the cache for the next refresh."""
    head = status_head(wt)
    c = _status_wt.get(wt)
    if not _worktree_fresh(c, head):
        th = _read_and_wait(wt, head, bound)
        c = _status_wt.get(wt)
        if th.is_alive():
            return _still_reading(c, bound)
    return _worktree_view(c or {}, head)


def status_said(path):
    """status_transcript_last, cached by the transcript's size and mtime."""
    try:
        sig = (os.path.getsize(path), mtime(path))
    except (OSError, TypeError):
        return None, None
    hit = _status_said.get(path)
    if hit and hit[0] == sig:
        return hit[1]
    v = status_transcript_last(path)
    _status_said[path] = (sig, v)
    return v


def status_agent_enrich(a, bound=None):
    b = bound if bound is not None else env_num("AGENTCTL_STATUS_AGENT_BOUND", STATUS_AGENT_BOUND)
    if a.get("alive") is None and a.get("pid"):
        try:
            a["alive"] = proc_alive(a["pid"])
        except (Exception, SystemExit):
            a["alive"] = None
    wt = a.get("worktree")
    if wt and os.path.isdir(wt):
        a.update({k: v for k, v in status_worktree(wt, b).items() if v is not None})
    a["logs"] = status_agent_logs(a.get("run"))
    transcript = live_transcript_ok(a.get("transcript"))  # a lease's meta: only a transcript the page may stream
    if transcript:
        a["said"], a["said_at"] = status_said(transcript)
    return a


def status_enrich_all(agents, bound=None):
    """Every agent's details at once, each in a thread of its own; the wait is one agent's bound (plus a margin), and an
    agent still unread then keeps its row with `detail_error`."""
    b = bound if bound is not None else env_num("AGENTCTL_STATUS_AGENT_BOUND", STATUS_AGENT_BOUND)
    done = set()

    def work(i, a):
        try:
            status_agent_enrich(a, b)
        except (Exception, SystemExit) as e:
            a["detail_error"] = f"{type(e).__name__}: {str(e)[:200]}"
        done.add(i)
    ths = [threading.Thread(target=work, args=(i, a), daemon=True, name="status:agent") for i, a in enumerate(agents)]
    for th in ths:
        th.start()
    end = epoch_now() + b + 2
    for th in ths:
        th.join(max(0.0, end - epoch_now()))
    for i, a in enumerate(agents):
        if i not in done:
            a.setdefault("detail_error", f"details not read within {b + 2:.0f} s")
    return agents


STATUS_RECORD_MAX_AGE = 8 * 3600  # s: an older fixer record (by mtime) is neither alive nor recently ended


def status_fixer_records(t):
    """fixer_records, of the records written in the last STATUS_RECORD_MAX_AGE s only (a fixer's wall time is bounded
    and its record is rewritten when it ends): ~150 old ones are never opened, nor their pids checked."""
    return fixer_records(max_age=STATUS_RECORD_MAX_AGE, now_=t)


def _lease_agents(st: Any, t: Any, agents: dict[str, Any]) -> None:
    """Every live red lease (fixer or claim) joins its holder's agent, keyed by pid (else by holder)."""
    for r in st.list("red/"):
        if not lease_is_live(r, t):
            continue
        ls = status_strip_lease(r, t)
        k = f"pid:{ls['pid']}" if ls.get("pid") else f"holder:{r.get('holder')}"
        a = agents.setdefault(k, {"pid": ls.get("pid"), "holder": r.get("holder"), "kind": ls.get("kind"),
                                  "tier": ls.get("tier"), "workflow": ls.get("workflow"), "run": ls.get("run"),
                                  "worktree": ls.get("worktree"), "transcript": ls.get("log"), "leases": []})
        a["leases"].append(ls)


def _fixer_shown(rec: dict[str, Any], t: Any) -> bool:
    """A fixer record is shown while its fixer is alive or ended within STATUS_AGENT_RECENT."""
    ended = iso_epoch(rec.get("ended"))
    return bool(rec.get("alive")) or bool(ended and t - ended <= STATUS_AGENT_RECENT)


def _merge_fixer(agents: dict[str, Any], rec: dict[str, Any]) -> None:
    """A fixer record joins the agent of its pid (a lease holder's), or is an agent of its own."""
    a = agents.setdefault(f"pid:{rec.get('pid')}", {"pid": rec.get("pid"), "holder": None, "kind": "fixer", "leases": []})
    for f in ("workflow", "run", "worktree", "transcript"):
        a[f] = a.get(f) or rec.get(f)
    a["tier"] = a.get("tier") or str(rec.get("why") or "").replace("tier ", "") or None
    a["alive"] = a.get("alive") or bool(rec.get("alive"))  # fixer_records' own pid check
    a.update({"record": rec.get("path"), "started": rec.get("started"), "ended": rec.get("ended"), "rc": rec.get("rc"),
              "verdict": rec.get("verdict"), "label": rec.get("label"), "key": rec.get("key"),
              "jobs": rec.get("jobs")})


def _settled_agent(a: dict[str, Any]) -> dict[str, Any]:
    """The agent's derived fields: no pid, not alive; its leases' last progress; when it started (its first lease's)."""
    if not a.get("pid"):
        a["alive"] = False
    prog = [x.get("renewed_at") for x in a["leases"] if x.get("renewed_at")]
    a["last_progress"] = max(prog) if prog else None
    acquired = [x["acquired_at"] for x in a["leases"] if x.get("acquired_at")]
    a["started"] = a.get("started") or (min(acquired) if acquired else None)
    return a


def _agent_started(a: dict[str, Any]) -> float:
    s = a.get("started")
    return float(s) if isinstance(s, (int, float)) else iso_epoch(s) or 0.0


def status_agents(st, t, records=None):
    """Every agent holding a red lease (fixer or claim) and every fixer dispatched (alive, or ended within
    STATUS_AGENT_RECENT): pid alive, worktree, tier, lease progress, its worktree's last commits, its logs."""
    agents: dict[str, Any] = {}
    _lease_agents(st, t, agents)
    for rec in records if records is not None else status_fixer_records(t):
        if _fixer_shown(rec, t):
            _merge_fixer(agents, rec)
    out = [_settled_agent(a) for a in agents.values()]
    out.sort(key=lambda a: (not a["leases"], not a.get("alive"), -_agent_started(a)))
    out = status_enrich_all(out[:STATUS_AGENTS_MAX])
    try:
        return live_annotate(out, t)
    except (Exception, SystemExit) as e:  # the live panes are extra: never the section's
        err(f"status page: live ids: {type(e).__name__}: {str(e)[:200]}")
        return out
