# agentctl, part `queueuntil`: land --until-done and durable verdicts.
# A real module (lib/agentctl/__init__.py MODULES): its own imports, the standard library's and MODULES' only.

import argparse
import os
import re
from typing import Any
from agentctl.statemachines import change_state_of, machine_state
from agentctl.queueknobs import knob_free_wait
from agentctl.core import attempt, git, remote
from agentctl.waitingdir import ChangeRecords, waiting_state
from agentctl.landstate import landed_state
from agentctl.worktrees import worktrees
from agentctl.cleanup import close_worktree, free_worktree


def parse_duration(text):
    """Seconds of `90`, `90s`, `30m`, `3h`."""
    m = re.fullmatch(r"\s*(\d+(?:\.\d+)?)\s*([smh]?)\s*", str(text))
    if not m:
        raise argparse.ArgumentTypeError(f"not a duration: {text!r} (90, 90s, 30m, 3h)")
    return float(m.group(1)) * {"": 1, "s": 1, "m": 60, "h": 3600}[m.group(2)]


UNTIL_DONE_AGAIN = "~/bin/agentctl land --until-done"


def _unitemed_entries(repo: Any, known: Any, ours: Any, store: Any) -> Any:
    """This host's queue entries that `ours` claims and no change item (`known`: their ids) holds yet, as items: the
    laptop's outbox (on the server every change is an item from its first write, ChangeRecords.save)."""
    d = attempt(lambda: waiting_state(repo)[0], None, catch=(RuntimeError,))
    return [{"id": e["id"], "state": e["status"], "data": e, "entered_at": e.get("seq") or 0}
            for e in (ChangeRecords(store, d).every() if d else [])
            if e["id"] not in known and ours(e) and change_state_of(e.get("status"))]


def own_change_item(repo, head, store):
    """The change land submitted from worktree `repo` at `head`, as {id, state, data, entered_at}: the open one, else
    the newest decided one. Read from the store's change items and from this host's queue entries (an entry the tick
    has not made an item of yet: the reconciler's own queue)."""
    rp = os.path.realpath(repo)

    def ours(x):
        return x.get("head") == head and os.path.realpath(x.get("worktree") or "") == rp
    mine = [it for it in store.items("change") if ours(it.get("data") or {})]
    known = {it["id"] for it in mine}
    mine += _unitemed_entries(repo, known, ours, store)
    open_ = [it for it in mine if not machine_state("change", it["state"]).terminal]
    pool = open_ or mine
    return max(pool, key=lambda it: (it.get("entered_at") or 0, it["id"])) if pool else None


def next_command(repo, why):
    """The command an agent runs next after NOT LANDED `why` (one line)."""
    w = why or ""
    if re.search(r"no longer applies|conflict", w, re.I):
        return (f"next: git -C {repo} fetch origin main && git -C {repo} rebase origin/main (resolve, commit), then "
                f"{UNTIL_DONE_AGAIN}")
    if re.search(r"checks failed|precheck refused|PRECHECK FAIL|failing with", w):
        return f"next: fix what failed above in {repo}, commit, then {UNTIL_DONE_AGAIN}"
    if re.search(r"SUPERSEDED|superseded", w):
        return f"next: {UNTIL_DONE_AGAIN} (it resumes the newer entry of this worktree)"
    return f"next: read the reason above, act on it in {repo}, then {UNTIL_DONE_AGAIN}"


def _report_held_worktree(repo: Any, eid: Any, sha: Any, held: Any) -> Any:
    """The LAND --UNTIL-DONE lines for a worktree still held by processes not this session's: exit code 4."""
    print(f"LANDED {sha} (queued {eid}); NOT DONE: the worktree {repo} is kept: still held after "
          f"{int(knob_free_wait())} s by processes not this session's to stop:", flush=True)
    for pid, cmd in sorted(held.items()):
        print(f"  pid {pid}: {cmd[:200]}", flush=True)
    print(f"next: ask their owner to stop them (or `kill {' '.join(map(str, sorted(held)))}` if they are yours), "
          f"then {UNTIL_DONE_AGAIN} from {repo}: it only cleans up", flush=True)
    return 4


def _left_alone_reason(repo: Any, item: Any) -> Any:
    """Why `land --until-done` must not clean up worktree repo (another session's, the root checkout), or None."""
    data = (item or {}).get("data") or {}
    sid = os.environ.get("CLAUDE_CODE_SESSION_ID")
    if data.get("session") and sid and data["session"] != sid:
        return f"not this session's (the change was landed by session {data['session']}); its own agent cleans up"
    try:
        root = os.path.realpath(worktrees(repo)[0]["worktree"])
    except RuntimeError as e:
        return str(e)[:200]
    return "the root checkout is never removed" if os.path.realpath(repo) == root else None


def until_done_finish(repo: Any, eid: Any, sha: Any, a: Any, item: Any = None) -> Any:
    """LANDED: the proof, then the cleanup (LAND --UNTIL-DONE above). Returns the exit code."""
    def kept(why):
        print(f"LANDED {sha} (queued {eid}); NOT DONE: the worktree {repo} is kept: {why}", flush=True)
        return 3
    why = _left_alone_reason(repo, item)
    if why:
        return kept(why)
    git(repo, "fetch", "--quiet", remote(), "main", check=False)
    state = landed_state(repo, f"refs/remotes/{remote()}/main", "HEAD")
    if state.unknown:  # kept: nothing proves its commits landed
        return kept(f"git cherry could not compare it with {remote()}/main")
    if state.left:
        left = state.left
        return kept(f"{len(left)} local commit(s) whose patch is not on {remote()}/main ({', '.join(c[:10] for c in left[:3])})"
                    f": land them with {UNTIL_DONE_AGAIN}")
    if getattr(a, "keep_worktree", False):
        print(f"DONE {sha} (queued {eid}): every local commit is on {remote()}/main; worktree kept (--keep-worktree)",
              flush=True)
        return 0
    held = free_worktree(repo)
    if held:
        return _report_held_worktree(repo, eid, sha, held)
    msg = close_worktree(repo)
    if not msg.startswith("closed worktree"):
        return kept(msg.split(": ", 1)[-1])
    print(f"DONE {sha} (queued {eid}): every local commit's patch is on {remote()}/main; worktree removed ({msg})",
          flush=True)
    return 0


