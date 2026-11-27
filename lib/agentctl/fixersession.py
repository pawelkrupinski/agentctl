# agentctl, part `fixersession`: a fixer is a background Claude session: launching it, workspace trust, reading its session's state.
# A real module (lib/agentctl/__init__.py MODULES): its own imports, the standard library's and MODULES' only.

from agentctl.procrunner import process_runner
from agentctl.escalationtable import escalation_repos
from agentctl.statefile import mtime, note_failure, read_record, write_json
from agentctl.clock import epoch_now, wall_epoch
from agentctl.fixerknobs import knob_bg_idle_grace
from agentctl.core import HOME, SHORT, _tokens, git, state_root, tilde
from agentctl.repos import escalation_repo_path, red_repo_clone, red_repos
from agentctl.claudefiles import bg_transcript, claude_jobs_root, fixer_verdict
from agentctl.procs import proc_alive
from agentctl.storetoken import no_vault, token, token_ns
from agentctl.inboxentry import inbox_write
import json
import os
import re
import shlex
import subprocess
import time
from typing import Any
from contextlib import suppress


BG_STARTED = re.compile(r"backgrounded\W+([0-9a-f]{6,})")


def bg_row_brief(ent):
    """What `claude agents` says of a session, in one line."""
    if ent is None:
        return "not listed"
    if not ent:
        return "unreadable"
    keys = ("state", "status", "error", "reason", "exitCode", "cwd")
    return ", ".join(f"{k} {str(ent[k])[:160]}" for k in keys if ent.get(k) not in (None, "")) or "no state"


def fixer_label(rec):
    """The session's name in `claude agents`: 'red fixer #2392 · convergence takes-drift-2 (united-states)',
    'orphan · movies-wt-proxy-park'."""
    if rec.get("orphan"):
        return f"orphan · {os.path.basename(rec.get('worktree') or '?')}"
    if rec.get("lander"):  # a revived lander (blame.py)
        return f"lander · {rec['lander'].get('change')} · #{rec.get('run')}"[:80]
    wf = (rec.get("workflow") or "").split()
    return f"red fixer #{rec.get('run')} · {(wf[-1].lower() + ' ') if wf else ''}{rec.get('job') or ''}".strip()[:80]


# WORKSPACE TRUST (2026-10-31, core Main Pipeline #277: 9 fixers in a row ended `claude --bg` exit 1 "Workspace not
# trusted. Run `claude` in ~/bitcashier/core-wt-red-277-9 once and accept the trust prompt"): a background session
# starts only in a workspace the owner trusted. claude keys that trust by the repo's MAIN checkout (a worktree of
# ~/projects/movies is trusted through ~/projects/movies) or a dir between the cwd and its worktree root -- never a
# parent of the repo (~/bitcashier did not count for ~/bitcashier/core). agentctl only READS the trust (granting it is
# the owner's one prompt): an untrusted repo's red is never launched, its offer says so with the owner's one-liner, and
# it dispatches the tick after the owner trusted it.
def claude_config_path() -> str:
    d = os.environ.get("CLAUDE_CONFIG_DIR")
    return os.path.join(d, ".claude.json") if d else os.path.join(HOME, ".claude.json")


def _trust_candidates(p: Any) -> Any:
    """(the main checkout of path p, every directory whose trust covers p: p up to its top, and the main checkout)."""
    r = git(p, "rev-parse", "--path-format=absolute", "--show-toplevel", "--git-common-dir", check=False)
    out = r.stdout.splitlines() if r.returncode == 0 else []
    top = os.path.realpath(out[0]) if out else p
    common = os.path.realpath(out[1]) if len(out) > 1 else ""
    main = os.path.dirname(common) if os.path.basename(common) == ".git" else top
    cands, q = {main}, p
    while True:
        cands.add(q)
        if q == top or q == os.path.dirname(q):
            break
        q = os.path.dirname(q)
    return main, cands


def claude_trust_gap(path: str | None) -> str | None:
    """None when claude trusts `path` (a checkout or worktree) for a background session -- or its config cannot be
    read (no verdict: the launch decides) -- else why not, with the owner's one-liner that trusts it."""
    config = read_record(claude_config_path())
    projects = config.get("projects") if isinstance(config, dict) else None
    if not isinstance(projects, dict) or not path or not os.path.isdir(path):
        return None
    main, cands = _trust_candidates(os.path.realpath(path))
    if any(isinstance(projects.get(c), dict) and projects[c].get("hasTrustDialogAccepted") is True for c in cands):
        return None
    return (f"claude does not trust {tilde(main)} (`claude --bg` refuses an untrusted workspace; a "
            f"worktree is trusted through its main checkout): owner, once: `! cd {shlex.quote(main)} && claude` -- "
            f"accept the trust prompt, then /exit; the red dispatches at the next tick")


# TRUST IS ASKED WHEN A REPO IS REGISTERED, NOT 21 MINUTES INTO A RED (the owner, 2026-11-19: "claude does not trust
# ~/bitcashier/core" reached him as the offer reason of core #1053, and the same line was written to the inbox five
# times on 11-18 as that offer's reason flapped). Accepting the trust prompt needs his terminal -- owner-only
# (`tty-trust`) -- but nothing about it waits for a red: the laptop's sync checks every watched repo's clone
# (red-repos.json; a repo newly registered, or a red family gaining one, is checked at its next pass) and asks ONCE per
# clone, as one owner alert keyed by the clone (`trust:<path>`), retracted by alerts.py's probe the pass the clone is
# trusted. The state file only remembers which clones were asked, so a re-ask follows only a new gap.
TRUST_ASK_PREFIX = "trust:"


def trust_ask_line(path: str, gap: str, waits: str = "every red of that repo") -> str:
    return (f"RED BLOCKED ON OWNER [tty-trust]: WAITING ON YOU: {gap.split(' (', 1)[0]} -- {waits} waits "
            f"on it. Answer: `! cd {shlex.quote(path)} && claude` (accept the trust prompt, then /exit); this clears by "
            "itself once it is trusted")


def trusted_clones() -> dict[str, str]:
    """(clone, what waits on its trust) for every checkout a dispatched session launches in: each watched repo's clone
    (red-repos.json) and each repo the escalation dispatch table sends an agent to (escalationtable.py)."""
    out: dict[str, str] = {}
    for c in red_repos():
        clone = red_repo_clone(c["repo"])
        if clone:
            out.setdefault(clone, f"every red of {c['repo']}")
    for repo in sorted(escalation_repos()):
        clone = escalation_repo_path(repo)
        if clone:
            out[clone] = (out[clone] + " and every escalation fixer there") if clone in out else \
                f"every escalation fixer of {repo}"
    return out


def trust_asks() -> list[str]:
    """The sync's pass: each checkout a session launches in (trusted_clones) checked for claude's workspace trust; an
    untrusted one asked once."""
    path = state_root("trust-asks.json")
    asked = read_record(path) or {}
    out, now_asked = [], {}
    for clone, waits in trusted_clones().items():
        gap = claude_trust_gap(clone)
        if not gap:
            continue
        now_asked[clone] = asked.get(clone) or epoch_now()
        if clone not in asked:
            inbox_write("red", trust_ask_line(clone, gap, waits), [], [], 1, item=TRUST_ASK_PREFIX + os.path.realpath(clone))
            out.append(f"asked the owner to trust {tilde(clone)} ({waits})")
    if now_asked != asked:
        write_json(path, now_asked)
    return out


# WHAT NO DISPATCHED SESSION MAY RUN (a red fixer's, a family owner's, an escalation fixer's): a push past the queue, a
# check bypassed
SESSION_DENIED_TOOLS = ("Bash(git push:*)", "Bash(*--no-verify*)", "Bash(*PREPUSH_SKIP*)")


def bg_start(argv: list[str], cwd: str, env: dict[str, str]) -> tuple[str | None, str | None]:
    """(id, None) once `claude --bg` backgrounded the session, else (None, why)."""
    try:
        r = process_runner().run(argv, cwd=cwd, env=env, stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=120)
    except (OSError, subprocess.TimeoutExpired) as e:
        return None, f"failed ({e})"
    m = BG_STARTED.search(r.stdout + r.stderr)
    # refused: its stderr says why (stdout only when stderr is empty) -- the block's LAUNCH FAILED text (red_crash_launches)
    return (m.group(1), None) if m else (None, f"exit {r.returncode}: {(r.stderr.strip() or r.stdout.strip())[-300:]}")


# CLAUDE RUNS FROM A DIRECTORY THAT EXISTS (2026-11-22 04:27Z, outside-gate owner, wrapper pid 3227): a wrapper's cwd is
# its worktree, and that worktree was deleted and made again mid-turn. claude refuses to start in a deleted directory
# ("The current directory no longer exists"), so every `claude agents` the wrapper ran read nothing ({}: no verdict
# either way) and its session's `done` went unseen until the laptop's orphan reaper -- running elsewhere -- revoked the
# turn. agentctl's own `claude agents` / `claude stop` run from HOME.
def claude_cwd() -> str:
    return HOME if os.path.isdir(HOME) else "/"


def bg_rows(cli: str) -> list[dict[str, Any]] | None:
    """The rows of `claude agents --json --all`, or None when they cannot be read."""
    try:
        r = process_runner().run([cli, "agents", "--json", "--all"], capture_output=True, text=True, timeout=60,
                           stdin=subprocess.DEVNULL, cwd=claude_cwd())
        rows = json.loads(r.stdout) if r.returncode == 0 else None
    except (OSError, subprocess.TimeoutExpired, ValueError):
        rows = None
    return rows if isinstance(rows, list) else None


def bg_session(cli: str, bid: str) -> dict[str, Any] | None:
    """The session's row in `claude agents --json --all` ({id, sessionId, name, state, ...}); None when it is not
    listed; {} when the list cannot be read (no verdict either way)."""
    rows = bg_rows(cli)
    if rows is None:
        return {}
    return next((x for x in rows if x.get("id") == bid or str(x.get("sessionId") or "").startswith(bid)), None)


# A SESSION AT WORK is one `claude agents` lists in a working state. `done` and `stopped` are ended turns; so is
# `blocked` (or `idle`) with no live process behind it -- its turn over, waiting for input that never comes (2026-10-31:
# the main-tests owner's session after a 403, pid gone; its wrapper waited for `done` and kept its reds leased for
# hours). A `blocked` row with a live pid may still be waiting on its own background work: judged by its progress.
BG_ENDED_STATES = ("done", "stopped", "failed", "error")
BG_WAITING_STATES = ("blocked", "idle")


def bg_ended(ent: dict[str, Any] | None) -> bool:
    """The session's row says its turn is over (ent: bg_session's row; {} -- unread -- and None -- not listed -- say
    nothing here)."""
    if not ent:
        return False
    if ent.get("state") in BG_ENDED_STATES:
        return True
    pid = ent.get("pid")
    return ent.get("state") in BG_WAITING_STATES and not proc_alive(pid)


# a backgrounded shell, or a backgrounded subagent (2026-11-25, Alert backtest #28: an owner waiting on its own agent)
BG_TASK_STARTED = re.compile(r'"backgroundTaskId": ?"([\w-]+)"|Command running in background with ID: ([\w-]+)'
                             r'|"status": ?"async_launched", ?"agentId": ?"([\w-]+)"')
BG_TASK_REPORTED = re.compile(r"<task-id>([\w-]+)</task-id>")  # a notification: a user entry or one absorbed mid-turn
BG_TASK_STOPPED = re.compile(r'"(?:task_id|shell_id)": ?"([\w-]+)"')  # TaskStop / KillShell: stopped by the session
_BG_TURN_CACHE: dict[str, tuple[tuple[int, float], dict[str, Any]]] = {}


def user_texts(c: object) -> list[str]:
    """A user entry's text blocks (its content a string, or a list of blocks)."""
    blocks = [{"type": "text", "text": c}] if isinstance(c, str) else c if isinstance(c, list) else []
    return [str(b.get("text")) for b in blocks if isinstance(b, dict) and b.get("type") == "text"]


def user_input(e: dict[str, Any], c: object) -> str | None:
    """A user entry's input: "prompt" (a new turn), "notification" (a task's, new input of the same turn), or None --
    no text, or claude's own `isMeta` note (a system-reminder): no input."""
    if e.get("isMeta") and "<task-notification>" not in str(c):
        return None
    texts = user_texts(c)
    if not texts:
        return None
    return "notification" if all("<task-notification>" in t for t in texts) else "prompt"


def answer_text(c: object) -> str:
    """An assistant entry's text blocks, joined ('' for none)."""
    if not isinstance(c, list):
        return ""
    t = "\n".join(b.get("text") or "" for b in c if isinstance(b, dict) and b.get("type") == "text")
    return t if t.strip() else ""


def turn_entry(e: object) -> tuple[str | None, str]:
    """What one transcript entry is to the turn: ("prompt" | "notification", "") -- input --, ("answer", text),
    ("closed", "") -- claude's `turn_duration` --, or (None, "") for anything else."""
    if not isinstance(e, dict):
        return None, ""
    m = e.get("message")
    c = m.get("content") if isinstance(m, dict) else None
    if e.get("type") == "user":
        return user_input(e, c), ""
    if e.get("type") == "assistant":
        t = answer_text(c)
        return ("answer" if t else None), t
    return ("closed" if e.get("type") == "system" and e.get("subtype") == "turn_duration" else None), ""


def _scan_bg_turn(path: Any) -> Any:
    """bg_turn's reading of the transcript at path (OSError: unreadable)."""
    final, reported, closed = "", set(), False
    started: list[str] = []
    with open(path, errors="replace") as f:
        for line in f:
            try:
                kind, t = turn_entry(json.loads(line))
            except ValueError:
                continue
            if kind in ("prompt", "notification"):  # new input: the turn's answer is what comes after it
                final, closed = "", False
                started = [] if kind == "prompt" else started  # a new turn (a resume is a new process: no older task)
            elif kind == "answer":
                final, closed = t, False
            elif kind == "closed":
                closed = bool(final)
            started += ["".join(g) for g in BG_TASK_STARTED.findall(line)]
            reported.update(BG_TASK_REPORTED.findall(line) + BG_TASK_STOPPED.findall(line))
    return {"final": final, "pending": [x for x in dict.fromkeys(started) if x not in reported], "closed": closed}


def bg_turn(path: str | None) -> dict[str, Any]:
    """What the transcript says of the session's current turn: {"final": its last answer since the latest input (a
    prompt or a task notification), "pending": its background tasks started since its prompt and neither reported
    nor stopped, "closed": claude recorded the turn's end (a `turn_duration` entry) after that answer, with no input
    since}. Cached on the file's size and mtime; empty for an unreadable one."""
    empty = {"final": "", "pending": [], "closed": False}
    try:
        sig = (os.path.getsize(path), os.path.getmtime(path))
    except (OSError, TypeError):
        return empty
    hit = _BG_TURN_CACHE.get(path)
    if hit and hit[0] == sig:
        return hit[1]
    try:
        out = _scan_bg_turn(path)
    except OSError:
        return empty
    _BG_TURN_CACHE[path] = (sig, out)
    return out


def bg_waiting(ent):
    """The row is a turn over with its process alive (claude's spare keeps it): what it waits for is the transcript's."""
    pid = (ent or {}).get("pid")
    return bool(ent) and (ent.get("state") or ent.get("status")) in BG_WAITING_STATES and proc_alive(pid)


# A CLOSED TURN UNDER A `working` ROW (2026-11-21 21:53Z convergence owner session 0f6f7ecd, 22:55Z outside-gate
# 0dedcd39): the session gave its final answer, verdict and all, and claude wrote the turn's `turn_duration` -- but its
# row (and its job, ~/.claude/jobs/<id>/state.json) stayed `working` with the process alive. Only `blocked` / `idle`
# rows had their transcript read, so the wrapper held the turn until RED_STALL (30 min) stopped it, and the orphan
# reaper, seeing that stop, revoked the owner's heartbeat seconds later. A row in any state whose transcript closed the
# turn is judged as a waiting one.
def bg_turn_over(ent, path, now=None):
    """The session's turn is over: its row says so (bg_ended), or it waits with its process alive -- or its transcript
    closed the turn whatever its row says -- and its transcript says the turn ended: a verdict in its final answer, or
    no background task of its own pending for BG_IDLE_GRACE."""
    if bg_ended(ent):
        return True
    if not ent or not (path and os.path.exists(path)):  # no transcript yet: still starting (a resume compacting its
        return False  # history writes none for minutes), judged by its stall budget, never "quiet" since mtime 0
    turn = bg_turn(path)
    if not (bg_waiting(ent) or turn["closed"]):
        return False
    if fixer_verdict(turn["final"]):
        return True
    if turn["pending"]:
        return False
    quiet = (wall_epoch() if now is None else now) - mtime(path)
    return quiet >= knob_bg_idle_grace()


def bg_wait_mark(ent, path, now, every):
    """'' -- or, for a session with its process alive and its own background tasks pending (waiting on them, or
    working beside them), a mark that changes every renewal period: waiting on its own CI run is work, and its lease is
    renewed by it (bounded by the wall time). 2026-11-03: only a `blocked` / `idle` row counted, and an owner whose row
    read `working` while its own measure ran in the background stalled at 30 min."""
    pid = (ent or {}).get("pid")
    if not ent or bg_ended(ent) or not proc_alive(pid):
        return ""
    pending = bg_turn(path)["pending"]
    return f":waiting {','.join(pending)} @{int(now // max(every, 1))}" if pending else ""


def store_reauth():
    """After StoreAuthFailed: forget the store token this process holds (its environment, the cache) and read it again
    -- the processor's socket, never a vault prompt. The new token (also in os.environ), or None when there is no other
    one: the caller then ends, its leases released or revoked with `store auth failed`."""
    old = os.environ.pop("AGENTCTL_STORE_TOKEN", None)
    _tokens.pop(token_ns(SHORT["movies"]), None)
    try:
        with no_vault():
            new = token(SHORT["movies"])
    except (Exception, SystemExit):
        new = None
    if new and new != old:
        os.environ["AGENTCTL_STORE_TOKEN"] = new
        return new
    if old:
        os.environ["AGENTCTL_STORE_TOKEN"] = old
    return None


def bg_stop(cli: str, bid: str | None) -> None:
    if not bid:
        return
    try:
        process_runner().run([cli, "stop", bid], capture_output=True, timeout=60, stdin=subprocess.DEVNULL, cwd=claude_cwd())
    except (OSError, subprocess.TimeoutExpired) as e:
        note_failure("claude stop of the session failed", e)


# A SESSION WITH NO CONVERSATION CANNOT BE RESUMED (2026-11-21, Order independence #99 HARNESS FAULT, issue
# 20261121T191539-21372d): outside-gate's owner turn 58 (session e7d48beb, resumed from 6dcf1a0b during the laptop's
# network trouble) sat `blocked` and was stopped before it wrote one message -- its transcript held only metadata
# rows. The owner recorded it as THE session to resume; claude refused it (`source session e7d48beb-... not found`)
# and the refused launch's own new id became the next turn's resume target: five launches in a row crashed within
# seconds, each resuming the one before, and the breaker called it a harness fault. A session is resumed only when
# its transcript holds a prompt or an answer.
def bg_resumable(session):
    """True when `session`'s transcript holds a conversation (a user or assistant entry): `claude --resume` finds it."""
    path = bg_transcript(session) if session else None
    try:
        with open(path or "", errors="replace") as f:
            for line in f:
                try:
                    e = json.loads(line)
                except ValueError:
                    continue
                if isinstance(e, dict) and e.get("type") in ("user", "assistant") and e.get("message"):
                    return True
    except FileNotFoundError:
        pass
    except OSError as exc:
        note_failure("transcript unreadable", exc)
    return False


def bg_failure(bid):
    """Why claude says a background session failed or is blocked (its job's `detail`, ~/.claude/jobs/<id>/state.json:
    e.g. `source session ... not found`, `Prompt is too long · automatic compaction failed: You've hit your weekly
    limit ...`), '' when it did not or says nothing more than its state: a crash's verdict carries the actual error,
    and a limit named there makes the launch LIMITED (redstep.red_end_outcome)."""
    job = read_record(os.path.join(claude_jobs_root(), str(bid or "")[:8], "state.json"))
    if not isinstance(job, dict) or job.get("state") not in ("failed", "error", "blocked"):
        return ""
    detail = " ".join(str(job.get("detail") or "").split())[:300]
    return "" if detail == job["state"] else detail


def _assistant_text(line: Any) -> Any:
    """The text blocks of one transcript line when it is an assistant message, else ''."""
    try:
        e = json.loads(line)
    except ValueError:
        return ""
    m = e.get("message") if e.get("type") == "assistant" else None
    if isinstance(m, dict) and isinstance(m.get("content"), list):
        return "\n".join(b.get("text") or "" for b in m["content"] if b.get("type") == "text")
    return ""


def bg_final_text(path):
    """The session's last assistant answer (its text blocks), '' when there is none."""
    last = ""
    with suppress(OSError, TypeError):
        with open(path, errors="replace") as f:
            for line in f:
                t = _assistant_text(line)
                if t.strip():
                    last = t
    return last
