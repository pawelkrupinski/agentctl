# agentctl, part `live`: the live sessions: their registry (leased, transcripts found), each one's state and annotation
# A real module (lib/agentctl/__init__.py MODULES): its own imports, the standard library's and MODULES' only.

import glob
import json
import os
import re
import threading
import time
from typing import Any
from agentctl.core import attempt, dispatches_dir, process_cache, reds_dir
from agentctl.statefile import mtime, read_json_file, read_record, tail_bytes
from agentctl.procs import proc_alive
from agentctl.claudefiles import bg_transcript, claude_projects_root


# ---------------------------------------------------------------- live agent output (2026-10-29)
# The owner: "could this page show live output of agents", then "I want full output". Every agent row of the page can
# open a live pane (and /agent/<id> a full-screen one) that streams the agent's Claude transcript (.jsonl) as it grows,
# from its first line: every message, thinking, tool call and tool result, unabridged (only token-, key- and
# password-like strings masked). WHICH transcripts: only those of agents agentctl knows, each under an id the page
# names -- a request names an agent id, never a path:
#   fixer-<run>-<n>    a red fixer (reds/<run>/fixer-<n>.json: its `transcript`, else its `log` when a .jsonl);
#   session-<uuid>     a Claude session of a dispatch record (its session in its worktree's project dir) or of a live
#                      lease's holder (the lease's `log`, as the Agents section reads it);
#   task-<id>          a subagent's transcript, found through $AGENTCTL_LIVE_GLOBS (os.pathsep-separated globs;
#                      default /private/tmp/claude-<uid>/*/*/tasks/*.output, the links Claude Code keeps per subagent).
# And then only a regular .jsonl file owned by this user under ~/.claude/projects ($AGENTCTL_CLAUDE_PROJECTS), after
# symlinks -- a record or lease naming anything else is not streamed.
LIVE_RECENT = 3 * 3600          # s: a session/subagent written within this gets a row of its own on the page
LIVE_MAX_AGE = 7 * 86400        # s: an older record or transcript can no longer be opened
LIVE_ROWS_MAX = 12              # extra rows (sessions, subagents) on the page
LIVE_STALL = 30 * 60            # s without output mid-turn: a subagent (no pid to check) is taken as failed
LIVE_REGISTRY_TTL = 5           # s a registry read is reused
LIVE_ID_RE = re.compile(r"(?:fixer|session|task)-[A-Za-z0-9_-]{1,100}")
_live_leased = process_cache({})               # agent id -> entry, of the lease holders the last Agents read saw (no store read here)
_live_reg = {"at": 0.0, "reg": None}
_live_reg_lock = threading.Lock()
_live_info = process_cache({})                 # transcript -> ((size, mtime), tail info)


def live_globs():
    v = os.environ.get("AGENTCTL_LIVE_GLOBS")
    return [g for g in (v.split(os.pathsep) if v is not None
                        else [f"/private/tmp/claude-{os.getuid()}/*/*/tasks/*.output"]) if g]


def live_transcript_ok(p):
    """The real path of a transcript the page may stream: a regular .jsonl file owned by this user under the Claude
    projects root (after symlinks); else None."""
    if not isinstance(p, str) or not p or "\0" in p or not os.path.isabs(p):
        return None
    try:
        real = os.path.realpath(p)
        st = os.stat(real)
    except (OSError, ValueError):
        return None
    root = os.path.realpath(claude_projects_root()).rstrip(os.sep) + os.sep
    if not real.endswith(".jsonl") or not real.startswith(root) or not os.path.isfile(real) or st.st_uid != os.getuid():
        return None
    return real


def live_slug(wt):
    """Claude Code's project dir name of a working directory: every non-alphanumeric character a '-'."""
    return re.sub(r"[^A-Za-z0-9]", "-", str(wt or ""))


def live_session_transcript(session, wt=None):
    if not session or not re.fullmatch(r"[A-Za-z0-9_-]{1,100}", str(session)):
        return None
    if wt:
        p = os.path.join(claude_projects_root(), live_slug(wt), f"{session}.jsonl")
        if os.path.isfile(p):
            return p
    return bg_transcript(session)


def live_id_part(s):
    return re.sub(r"[^A-Za-z0-9_-]", "_", str(s))[:100]


_live_metas = process_cache({})                # subagent .meta.json -> (mtime, its JSON)


def live_meta(p):
    m = mtime(p)
    hit = _live_metas.get(p)
    if not hit or hit[0] != m:
        hit = _live_metas[p] = (m, read_json_file(p) if m else {})
    return hit[1]


class RegistryRead:
    """One read of the registry: a method per source (fixer records, dispatch records, the leased holders, the subagent
    globs), each adding its agents; a transcript has one id, the first source's."""

    def __init__(self, t: float) -> None:
        self.t = t
        self.reg: dict[str, dict[str, Any]] = {}
        self.seen: set[str] = set()

    def add(self, aid: str, path: Any, **k: Any) -> None:
        real = live_transcript_ok(path)
        if real and real not in self.seen and aid not in self.reg:
            self.seen.add(real)
            self.reg[aid] = {"id": aid, "transcript": real, **k}

    def _fresh(self, p: str) -> bool:
        return self.t - mtime(p) <= LIVE_MAX_AGE

    def fixers(self) -> None:
        for p in glob.glob(reds_dir("*", "fixer*.json")):
            m = re.fullmatch(r"fixer(?:-(\d+))?\.json", os.path.basename(p))
            r = read_record(p) if m and self._fresh(p) else None
            if m and r:
                self._add_fixer(p, m.group(1) or "1", r)

    def _add_fixer(self, p: str, n: str, r: dict[str, Any]) -> None:
        run = live_id_part(os.path.basename(os.path.dirname(p)))
        path = r.get("transcript") or (r.get("log") if str(r.get("log") or "").endswith(".jsonl") else None) \
            or live_session_transcript(r.get("session"), r.get("worktree"))
        self.add(f"fixer-{run}-{n}", path, kind="fixer", label=r.get("label") or f"red fixer #{run}",
                 pid=r.get("pid"), process_start=r.get("process_start"), started=r.get("started"), ended=r.get("ended"),
                 rc=r.get("rc"), verdict=r.get("verdict"), worktree=r.get("worktree"), run=r.get("run"), record=p)

    def _dispatch_records(self) -> list[dict[str, Any]]:
        recs = []
        for p in glob.glob(dispatches_dir("*.json")):
            r = read_record(p) if self._fresh(p) else None
            if r and r.get("session"):
                recs.append(r)
        return recs

    def sessions(self) -> None:
        for r in sorted(self._dispatch_records(), key=lambda r: -float(r.get("epoch") or 0)):  # the newest record of a session names it
            s = str(r["session"])
            self.add(f"session-{live_id_part(s)}", live_session_transcript(s, r.get("worktree")), kind="session",
                     label=f"session {s[:8]}" + (f" · {r.get('agent')}" if r.get("agent") else "")
                     + (f" · {str(r.get('purpose'))[:80]}" if r.get("purpose") else ""),
                     pid=r.get("pid"), process_start=r.get("process_start"), worktree=r.get("worktree"))

    def leased(self) -> None:
        for aid, e in list(_live_leased.items()):
            self.add(aid, e.get("transcript"), **{k: v for k, v in e.items() if k not in ("id", "transcript")})

    def _own_and_fresh(self, p: str) -> bool:
        try:
            return os.lstat(p).st_uid == os.getuid() and self._fresh(p)
        except OSError:
            return False

    def subagents(self) -> None:
        for p in (p for g in live_globs() for p in glob.glob(g)):
            if not self._own_and_fresh(p):
                continue
            stem = os.path.basename(p).rsplit(".", 1)[0]
            real = live_transcript_ok(p)
            meta = live_meta(real[:-len(".jsonl")] + ".meta.json") if real else {}
            self.add(f"task-{live_id_part(stem)}", p, kind="subagent",
                     label=str(meta.get("description") or f"subagent {stem}")[:120], agent_type=meta.get("agentType"),
                     finished=bool(meta.get("finished")))


def live_registry_read(t: float | None = None) -> dict[str, dict[str, Any]]:
    """{agent id: entry} of every agent whose transcript may be streamed, from files only (fixer records, dispatch
    records, the subagent globs) plus the lease holders the last Agents read saw. One transcript has one id: a fixer's
    wins over its session's."""
    read = RegistryRead(time.time() if t is None else t)
    read.fixers()
    read.sessions()
    read.leased()
    read.subagents()
    return read.reg


def live_registry(fresh=False):
    with _live_reg_lock:
        if fresh or _live_reg["reg"] is None or time.time() - _live_reg["at"] >= LIVE_REGISTRY_TTL:
            _live_reg["reg"], _live_reg["at"] = live_registry_read(), time.time()
        return _live_reg["reg"]


def live_lookup(aid):
    """The registry entry of a known agent id, else None (an id that is not one is never looked up)."""
    if not isinstance(aid, str) or not LIVE_ID_RE.fullmatch(aid):
        return None
    e = live_registry().get(aid)
    if e is None:  # a fixer dispatched or a subagent started within the cache's life
        e = live_registry(fresh=True).get(aid)
    if e and not live_transcript_ok(e["transcript"]):
        return None
    return e


LIVE_TALK = ("user", "assistant", "queue-operation")


def live_track(info, rec):
    """Fold one transcript entry into the tail info: the last conversational entry (an assistant's stop_reason or
    None for a user/tool result) and the last timestamp."""
    if not isinstance(rec, dict):
        return info
    if rec.get("timestamp"):
        info["last_ts"] = rec["timestamp"]
    typ = rec.get("type")
    if typ == "assistant":
        m = rec.get("message") or {}
        info["stop"] = m.get("stop_reason") if isinstance(m, dict) else None
        info["api_error"] = bool(rec.get("isApiErrorMessage"))
    elif typ in LIVE_TALK:
        info["stop"], info["api_error"] = None, False
    return info


def live_tail_info(path, max_bytes=256 << 10):
    """{stop, last_ts, api_error, size, mtime} from the transcript's last max_bytes, cached by its size and mtime."""
    try:
        sig = (os.path.getsize(path), mtime(path))
    except (OSError, TypeError):
        return {}
    hit = _live_info.get(path)
    if hit and hit[0] == sig:
        return dict(hit[1])
    info = {"stop": None, "last_ts": None, "api_error": False}
    try:
        lines = tail_bytes(path, max_bytes)[0].decode("utf-8", "replace").splitlines()
    except OSError:
        return {}
    for ln in lines:
        try:
            live_track(info, json.loads(ln))
        except ValueError:
            pass
    info.update(size=sig[0], mtime=sig[1])
    _live_info[path] = (sig, info)
    return dict(info)


def _process_state(alive: Any, stop: Any) -> tuple[str, str] | None:
    """What the process's life says of the agent (alive is None when it cannot be told), None when it says nothing."""
    if alive is False:
        return ("done", "the process ended after its answer") if stop == "end_turn" else ("failed", "the process ended mid-turn")
    if alive:
        return ("waiting on background", "its turn ended; it waits for background work or a message") \
            if stop == "end_turn" else ("working", "its turn is running")
    return None


def live_state(e: dict[str, Any], info: dict[str, Any], t: float | None = None) -> tuple[str, str]:
    """(state, why): working, waiting on background, done or failed -- from the fixer record's end, the process, and
    the transcript's last conversational entry (a turn that ended with end_turn is an answer given)."""
    t = time.time() if t is None else t
    if e.get("ended"):
        rc = e.get("rc")
        return ("done" if rc == 0 else "failed"), (e.get("verdict") or f"ended, rc {rc}")
    stop = info.get("stop")
    if info.get("api_error"):
        return "failed", "the last answer was an API error"
    alive = attempt(lambda: proc_alive(e["pid"], e.get("process_start")), None, catch=(Exception, SystemExit)) if e.get("pid") else None
    process = _process_state(alive, stop)
    if process:
        return process
    if e.get("finished") or stop == "end_turn":
        return "done", "its final answer is given"
    idle = t - float(info.get("mtime") or t)
    if idle > LIVE_STALL:
        return "failed", f"no output for {int(idle // 60)} min, mid-turn"
    return "working", "its turn is running"


def _leased_agents(agents: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """The lease holders whose transcript may be streamed, under the id it will be known by (session-<stem>)."""
    leased = {}
    for a in agents:
        real = live_transcript_ok(a.get("transcript"))
        if real and a.get("leases"):
            aid = f"session-{live_id_part(os.path.basename(real)[:-len('.jsonl')])}"
            leased[aid] = {"transcript": real, "kind": a.get("kind") or "lease holder",
                           "label": a.get("label") or a.get("holder"), "pid": a.get("pid")}
    return leased


def _mark_live(agents: list[dict[str, Any]], reg: dict[str, dict[str, Any]], t: float) -> set[str]:
    """Give each row whose transcript is registered its live id and state; the ids it shows."""
    by_path = {e["transcript"]: e for e in reg.values()}
    shown = set()
    for a in agents:
        real = live_transcript_ok(a.get("transcript"))
        e = by_path.get(real) if real else None
        if e:
            a["live"] = e["id"]
            a["live_state"], a["live_why"] = live_state({**e, "ended": a.get("ended") or e.get("ended")}, live_tail_info(real), t)
            shown.add(e["id"])
    return shown


def _extra_row(e: dict[str, Any], t: float) -> tuple[float, dict[str, Any]] | None:
    """(its mtime, the Agents row) of a registered agent no row shows, when it wrote within LIVE_RECENT."""
    m = mtime(e["transcript"])
    if t - m > LIVE_RECENT:
        return None
    st, why = live_state(e, live_tail_info(e["transcript"]), t)
    return m, {"pid": e.get("pid"), "holder": None, "kind": e["kind"], "label": e.get("label"),
               "worktree": e.get("worktree"), "leases": [], "live": e["id"], "live_state": st,
               "live_why": why, "alive": st in ("working", "waiting on background"),
               "last_progress": m, "transcript": e["transcript"]}


def live_annotate(agents: list[dict[str, Any]], t: float | None = None) -> list[dict[str, Any]]:
    """Give each Agents row whose transcript may be streamed its live id and state (a lease holder's transcript
    becomes known as session-<stem>), then add a row for every other known session or subagent written within
    LIVE_RECENT (newest first, LIVE_ROWS_MAX at most)."""
    t = time.time() if t is None else t
    _live_leased.clear()
    _live_leased.update(_leased_agents(agents))
    reg = live_registry(fresh=True)
    shown = _mark_live(agents, reg, t)
    extra = [row for row in (_extra_row(e, t) for e in reg.values() if e["id"] not in shown and e["kind"] != "fixer") if row]
    extra.sort(key=lambda x: -x[0])
    return agents + [a for _m, a in extra[:LIVE_ROWS_MAX]]
