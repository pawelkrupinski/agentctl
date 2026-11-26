# agentctl, part `park`: a parked worktree: the marks, their release, the digest, `agentctl park` / `unpark`
# A real module (lib/agentctl/__init__.py MODULES): its own imports, the standard library's and MODULES' only.

from agentctl.clock import epoch_now
import argparse
from typing import Any
import datetime as dt
import os
import re
import time
from agentctl.knobs import env_num
from agentctl.core import attempt, git, tilde
from agentctl.timefmt import ts
from agentctl.statefile import read_record, write_json
from agentctl.inboxentry import inbox_write
from agentctl.conditions import recovery_dir
from agentctl.queueclose import change_state
from agentctl.superseded import change_chain_end


# PARKED (the owner, 2026-11-03: "fix root cause and prevent structurally" -- movies-wt-bare-borrow (a design branch the
# owner is still deciding on), -flip-stage5 (an unproven design kept for reference), -order-rows (blocked on another
# change landing first) escalated UNLANDED ORPHAN over and over: an orphan was "author gone + commits not on main", with
# no way to say "this work is parked for a reason"). `agentctl park <worktree> --reason R` with one condition:
#   --until-landed <change id | commit>   re-surfaces once that change LANDED (or its re-submission did) or the commit
#                                        is on main by content;
#   --until <date>                        re-surfaces at that time;
#   --owner-decision                      only `agentctl unpark` (a person) ends it.
# A parked worktree gets no orphan action and no escalation (an open UNLANDED ORPHAN clears: "parked: R"); `agentctl
# status` shows `parked: N` with the reasons. Each maintain pass reads every condition again (self-recovering): met, the
# mark is RELEASED and the orphan rules apply afresh (its orphan records dropped, so it gets its one submit -- it lands).
# A parked worktree older than PARK_DIGEST_DAYS is named in ONE weekly digest line (an inbox entry that wakes nobody).
PARK_DIGEST_DAYS = 14


PARK_DIGEST_EVERY = 7 * 86400


def parked_path():
    return recovery_dir("parked.json")


def parked_marks():
    """{realpath of the parked worktree: its mark} (state: parked | released | unparked)."""
    return read_record(parked_path()) or {}


def parked_mark(wt):
    m = parked_marks().get(os.path.realpath(wt))
    return m if m and m.get("state") == "parked" else None


def park_until_text(m):
    u = m.get("until") or {}
    if u.get("kind") == "landed":
        return f"until {u.get('change') or ''}{' / ' if u.get('change') and u.get('commit') else ''}{(u.get('commit') or '')[:11]} lands"
    if u.get("kind") == "date":
        return f"until {u.get('at')}"
    return "until the owner decides"


def landed_change_why(store: Any, change: Any) -> Any:
    """Why the change a parked mark waits on is met: it (or its re-submission) landed, else None."""
    end = attempt(lambda: change_chain_end(store, change), None, catch=(Exception, SystemExit))
    return f"its blocker {end['id']} landed" if end and end["state"] == "landed" else None


def landed_commit_why(repo: Any, commit: Any) -> Any:
    """Why the commit a parked mark waits on is met: it is on main (by content), else None."""
    state = change_state(repo, commit)
    if not state.landed:  # not landed, or unknown (this clone cannot tell): the mark stands
        return None
    via = state.via
    return f"its blocker {commit[:11]} is on main" + (f" ({via[:10]})" if via and via != commit else "")


def landed_release_why(u: Any, repo: Any, store: Any) -> Any:
    """Why an `until landed` condition u is met now (its change landed, or its commit is on main), else None."""
    return (u.get("change") and landed_change_why(store, u["change"])) or (u.get("commit") and landed_commit_why(repo, u["commit"])) or None


def park_release_why(m, repo, store, t=None):
    """Why mark m's condition is met now, else None (unread: None -- the mark stands)."""
    t = epoch_now() if t is None else t
    u = m.get("until") or {}
    if u.get("kind") == "date":
        return f"its date {u['at']} passed" if t >= float(u.get("epoch") or 0) else None
    return landed_release_why(u, repo, store) if u.get("kind") == "landed" else None


def drop_orphan_records(wt):
    """Forget worktree wt's orphan records: the orphan rules start afresh for it (its one submit)."""
    path = recovery_dir("orphans.json")
    st = read_record(path) or {}
    rw = os.path.realpath(wt)
    keep = {k: v for k, v in st.items() if os.path.realpath(str((v or {}).get("worktree") or "")) != rw}
    if len(keep) != len(st):
        write_json(path, keep)


def release_parked(wt: Any, m: Any, repo: Any, store: Any, t: Any, dry_run: Any) -> Any:
    """The line saying parked mark m is released -- its worktree gone, or its condition met -- else None. Unless dry_run
    the mark is released, its orphan records dropped and the inbox told."""
    name = os.path.basename(wt)
    if not os.path.isdir(wt):
        if not dry_run:
            m.update(state="released", why="its worktree is gone", decided=ts())
        return f"parked {name}: released (its worktree is gone)"
    why = park_release_why(m, m.get("repo") or repo, store, t)
    if not why:
        return None
    if not dry_run:
        m.update(state="released", why=why, decided=ts())
        drop_orphan_records(wt)
        inbox_write("maintain", f"UNPARKED {tilde(wt)}: {why}; the orphan rules apply again "
                                f"(parked: {m['reason']})", [], [], 0, cwd=wt)
    return f"parked {name}: released ({why}); the orphan rules apply again"


def park_digest(old: Any, t: Any, dry_run: Any) -> Any:
    """The weekly digest line of the old parks (once PARK_DIGEST_EVERY has passed since the last), else None. Unless
    dry_run it is told in the inbox and stamped on the marks."""
    last = max([float(m.get("digest_at") or 0) for _w, m in old] or [0])
    if not old or t - last < PARK_DIGEST_EVERY:
        return None
    line = "PARKED DIGEST: " + "; ".join(
        f"{os.path.basename(w)} ({int((t - float(m['at'])) // 86400)} d, {park_until_text(m)}: {m['reason']})" for w, m in old)
    if not dry_run:
        inbox_write("maintain", line, [], [], 0)
        for _w, m in old:
            m["digest_at"] = t
    return line


def parked_long(m: Any, t: Any) -> bool:
    """Whether mark m has stood PARK_DIGEST_DAYS (the weekly digest names it)."""
    return bool(t - float(m.get("at") or t) >= env_num("AGENTCTL_PARK_DIGEST_DAYS", PARK_DIGEST_DAYS) * 86400)


def park_duty(repo, store, dry_run=False, now_=None):
    """One maintain pass over the parks: a met condition releases its mark; the weekly digest of the old ones."""
    t = epoch_now() if now_ is None else now_
    marks = parked_marks()
    did, old = [], []
    for wt, m in sorted(marks.items()):
        if m.get("state") != "parked":
            continue
        line = release_parked(wt, m, repo, store, t, dry_run)
        if line:
            did.append(line)
        elif parked_long(m, t):
            old.append((wt, m))
    digest = park_digest(old, t, dry_run)
    if digest:
        did.append(digest)
    if did and not dry_run:  # every line is a mark released or a digest stamped
        write_json(parked_path(), marks)
    return did


def parked_lines():
    """`agentctl status`'s parked section: `parked: N` and one line per parked worktree with its reason."""
    live = sorted((w, m) for w, m in parked_marks().items() if m.get("state") == "parked")
    return [f"parked: {len(live)}"] + [f"  {os.path.basename(w)}  {park_until_text(m)}: {m['reason']}" for w, m in live]


def matching_changes(x: str, store: Any) -> list[Any]:
    """The change items whose id is x, else those whose id contains it."""
    items: list[Any] = attempt(lambda: store.items("change"), [], catch=(Exception, SystemExit))
    return [i for i in items if i["id"] == x] or [i for i in items if x in i["id"]]


def commit_named(repo: Any, x: str) -> str | None:
    """The sha of the commit of repo that x names, or None."""
    r = git(repo, "rev-parse", "--verify", "-q", f"{x}^{{commit}}", check=False)
    return r.stdout.strip() if r.returncode == 0 else None


def resolve_blocker(x, repo, store):
    """{change, commit} of `--until-landed x`: a change id (whole or a unique part; its head the commit), else a commit
    of repo. Raises ValueError."""
    its = matching_changes(x, store)
    if len(its) == 1:
        return {"change": its[0]["id"], "commit": (its[0].get("data") or {}).get("head")}
    if len(its) > 1:
        raise ValueError(f"--until-landed {x}: {len(its)} change ids match it")
    commit = commit_named(repo, x)
    if commit:
        return {"change": None, "commit": commit}
    if re.fullmatch(r"\d{8}-\d{6}-[\w.-]+", x):  # a change id the store does not hold (yet): named, read each pass
        return {"change": x, "commit": None}
    raise ValueError(f"--until-landed {x}: no change id and no commit of that name")


def park_target(path):
    wt = os.path.realpath(os.path.expanduser(path))
    top = git(wt, "rev-parse", "--show-toplevel", check=False).stdout.strip() if os.path.isdir(wt) else ""
    if not top:
        return None, None
    common = git(top, "rev-parse", "--path-format=absolute", "--git-common-dir").strip()
    return os.path.realpath(top), (os.path.dirname(common) if common.endswith("/.git") else common)


def _park_until(a: Any, repo: Any, store: Any) -> Any:
    """The mark's `until` for the park command's one condition; None (said) when it does not resolve or parse."""
    if a.until_landed:
        try:
            return {"kind": "landed", **resolve_blocker(a.until_landed, repo, store)}
        except ValueError as e:
            print(f"park: {e}")
            return None
    if a.until:
        try:
            when = dt.datetime.fromisoformat(a.until.replace("Z", "+00:00"))
        except ValueError:
            print(f"park: --until {a.until}: not a date (YYYY-MM-DD or an ISO-8601 time)")
            return None
        return {"kind": "date", "at": a.until, "epoch": when.timestamp()}
    return {"kind": "owner"}


def cmd_park(a: argparse.Namespace, store: Any) -> int:
    marks = parked_marks()
    if not a.worktree:
        for line in parked_lines():
            print(line)
        return 0
    wt, repo = park_target(a.worktree)
    if not wt:
        print(f"park: {a.worktree} is no git worktree")
        return 2
    conds = [c for c in (a.until_landed, a.until, a.owner_decision or None) if c]
    if not a.reason or len(conds) != 1:
        print('usage: agentctl park <worktree> --reason "<why>" (--until-landed <change id|commit> | --until <date> | '
              "--owner-decision)")
        return 2
    until = _park_until(a, repo, store)
    if until is None:
        return 2
    marks[wt] = {"reason": a.reason, "until": until, "at": epoch_now(), "parked": ts(), "state": "parked", "repo": repo}
    write_json(parked_path(), marks)
    print(f"PARKED {tilde(wt)} {park_until_text(marks[wt])}: {a.reason} -- no orphan escalation")
    return 0


def cmd_unpark(a):
    marks = parked_marks()
    wt, _repo = park_target(a.worktree)
    wt = wt or os.path.realpath(os.path.expanduser(a.worktree))
    m = marks.get(wt)
    if not m or m.get("state") != "parked":
        print(f"unpark: {tilde(wt)} is not parked")
        return 0
    m.update(state="unparked", decided=ts())
    write_json(parked_path(), marks)
    drop_orphan_records(wt)
    print(f"UNPARKED {tilde(wt)}: the orphan rules apply again")
    return 0
