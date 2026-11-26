# agentctl, part `superseded`: a worktree superseded by its successor: the marks, the successor's state, closing it, `agentctl supersede`
# A real module (lib/agentctl/__init__.py MODULES): its own imports, the standard library's and MODULES' only.

import argparse
from typing import Any
import os
import time
from agentctl.core import PROTECTED_BRANCHES, attempt, cherry_landed, git, remote, tilde
from agentctl.timefmt import ts
from agentctl.statefile import read_record, write_json
from agentctl.procs import process_cwds
from agentctl.inboxentry import inbox_write
from agentctl.worktrees import worktrees
from agentctl.deletions import log_deletion
from agentctl.conditions import recovery_dir
from agentctl.queueclose import change_landed
from agentctl.statemachines import machine_state


# SUPERSEDED (the owner, 2026-10-31: movies-wt-ci-1/2/3 and -review were carried into ci-5 on 2026-10-30 -- rebased
# with real conflict resolution, so neither patch-id nor the content twin could match them -- and escalated UNLANDED
# ORPHAN until removed by hand). `agentctl supersede <worktree> --by <worktree|change id>` says its work went into
# another change. A superseded worktree gets no orphan action and no escalation (a probe it holds open clears: "superseded
# by"), and closes itself once its successor lands -- a change item of the successor's worktree LANDED since the mark,
# the named change (or the change that superseded it in the queue) LANDED, or the successor's head at the mark on main
# by content: then maintain removes it (clean and idle only; logged; its branch with it). The successor ending unlanded
# -- its worktree gone with nothing landed, or the named change rejected or cancelled -- LAPSES the mark: the orphan
# rules apply again, and the probe that cleared reopens. Each state is re-read every maintain pass (self-recovering).
SUPERSEDE_CHAIN = 10  # change ids followed through `superseded_by` (re-submissions) at most


def superseded_path():
    return recovery_dir("superseded.json")


def superseded_marks():
    """{realpath of the superseded worktree: its mark} (state: open | landed | closed | lapsed)."""
    return read_record(superseded_path()) or {}


def superseded_mark(wt):
    """The live mark of worktree wt (open or landed: its successor carries it), else None."""
    m = superseded_marks().get(os.path.realpath(wt))
    return m if m and m.get("state") in ("open", "landed") else None


def change_chain_end(st, cid):
    """The change item cid, followed through its `superseded_by` (a re-submission replaces it), or None."""
    it = st.item("change", cid)
    for _ in range(SUPERSEDE_CHAIN):
        nxt = (it or {}).get("data", {}).get("superseded_by") if it and it["state"] == "superseded" else None
        if not nxt:
            break
        it = st.item("change", nxt) or it
        if it["id"] != nxt:
            break
    return it


def _change_tree(item: Any) -> Any:
    """The real path of the worktree a change item was submitted from ('' for none)."""
    return os.path.realpath(str(item["data"].get("worktree") or ""))


def successor_state(m: Any, repo: Any, store: Any) -> Any:
    """(open | landed | lapsed, why) of mark m's successor."""
    return SuccessorRead(m, repo, store).state()


class SuccessorRead:
    """Mark m's successor read from the store and the repository: the first stage that finds it landed, lapsed or
    carried decides; else it is open (`by` has not landed yet)."""

    def __init__(self, m: Any, repo: Any, store: Any) -> None:
        self.m, self.repo, self.st = m, repo, store
        self.by, self.at = m["by"], float(m.get("at") or 0)
        self.end = change_chain_end(store, m["change"]) if m.get("change") else None
        self.succ = m.get("worktree")

    def state(self) -> Any:
        for stage in (self._change_landed, self._worktree_landed, self._head_on_main, self._named_change, self._gone):
            found = stage()
            if found:
                return found
        return "open", f"{self.by} has not landed yet"

    def _change_landed(self) -> Any:
        if self.end and self.end["state"] == "landed":
            return "landed", f"its change {self.end['id']} landed"
        return None

    def _worktree_landed(self) -> Any:
        """A change of the successor's worktree landed since the mark."""
        if not self.succ:
            return None
        rs = os.path.realpath(self.succ)
        for it in self.st.items("change", ["landed"]):
            if _change_tree(it) == rs and float(it.get("entered_at") or 0) >= self.at:
                return "landed", f"{os.path.basename(rs)}'s change {it['id']} landed"
        return None

    def _head_on_main(self) -> Any:
        if not self.m.get("head"):
            return None
        done, via = change_landed(self.repo, self.m["head"])
        if done:
            return "landed", f"{self.by}'s head {self.m['head'][:10]} is on main" + (f" ({via[:10]})" if via else "")
        return None

    def _named_change(self) -> Any:
        """A named change decides: open while it (or its re-submission) is."""
        if not self.m.get("change"):
            return None
        end = self.end
        if not end:
            return "lapsed", f"change {self.m['change']} is not known"
        if machine_state("change", end["state"]).terminal:
            return self._ended(end)
        return "open", f"its change {end['id']} is {end['state']}"

    def _ended(self, end: Any) -> Any:
        succ = self.succ
        again = [i for i in (worktree_change_items(succ, self.st)["open"] if succ else [])
                 if _change_tree(i) == os.path.realpath(succ)]
        if again:  # rejected, then landed again from the same worktree: that change carries it now
            return "open", f"its change {end['id']} ended {end['state']}; {again[-1]['id']} carries it now"
        return "lapsed", f"its change {end['id']} ended {end['state']}, unlanded"

    def _gone(self) -> Any:
        if self.succ and not os.path.isdir(self.succ):
            return "lapsed", f"{tilde(self.succ)} is gone with nothing of it landed"
        return None


def close_superseded(repo: Any, wt: Any, why: Any) -> Any:
    """Remove the superseded worktree wt and its branch: never the root checkout, a locked tree, one a process is in or
    one with uncommitted work. Returns (closed, line)."""
    wt = os.path.realpath(wt)
    if not os.path.isdir(wt):
        return True, f"{wt} is already gone"
    trees = worktrees(repo)
    me = next((t for t in trees if os.path.realpath(t.get("worktree") or "") == wt), None)
    kept = _kept_reason(repo, wt, trees, me)
    if kept or me is None:
        return False, kept
    head = git(wt, "rev-parse", "HEAD").strip()
    branch = (me.get("branch") or "").removeprefix("refs/heads/") or None
    r = git(repo, "worktree", "remove", wt, check=False)
    if r.returncode:
        return False, f"kept: git worktree remove refused: {r.stderr.strip()[:160]}"
    log_deletion("worktree", os.path.basename(repo), wt, head, f"superseded: {why}")
    return True, f"closed {tilde(wt)}" + _drop_branch(repo, branch, head, why)


def _kept_reason(repo: Any, wt: Any, trees: Any, me: Any) -> Any:
    """Why worktree wt (`me`, its entry among `trees`) is not closed -- the root checkout, locked, in use, not clean --
    or None."""
    if me is None or wt == os.path.realpath(trees[0]["worktree"]):
        return f"kept: {wt} is not a linked worktree of {repo}"
    if "locked" in me:
        return "kept: locked"
    busy = attempt(lambda: [p for p, c in process_cwds().items() if c == wt or c.startswith(wt + "/")], [],
                   catch=(Exception, SystemExit))
    if busy:
        return f"kept: in use by pid {busy[:3]}"
    dirty = git(wt, "status", "--porcelain", "--untracked-files=all", check=False)
    if dirty.returncode or dirty.stdout.strip():
        return f"kept: the tree is not clean ({len(dirty.stdout.splitlines())} changed/untracked)"
    return None


def _drop_branch(repo: Any, branch: Any, head: Any, why: Any) -> Any:
    """` and branch <name> (<head>)` once the closed worktree's branch is deleted (never a protected one, nor one
    another worktree has out), else ''."""
    if not branch or branch in PROTECTED_BRANCHES or f"refs/heads/{branch}" in {t.get("branch") for t in worktrees(repo)}:
        return ""
    if git(repo, "update-ref", "-d", f"refs/heads/{branch}", head, check=False).returncode != 0:
        return ""
    log_deletion("branch", os.path.basename(repo), branch, head, f"superseded: {why}")
    return f" and branch {branch} ({head[:10]})"


def _supersede_one(repo: Any, store: Any, wt: Any, m: Any, dry_run: Any, did: Any) -> Any:
    """One open or landed mark's pass (see superseded_duty): what it did is appended to `did`; True when the mark changed."""
    name = os.path.basename(wt)
    try:
        state, why = successor_state(m, repo, store)
    except (Exception, SystemExit) as e:  # the store unread: no verdict, the mark stands
        did.append(f"superseded {name}: could not read its successor ({str(e)[:120]})")
        return False
    if dry_run:
        did.append(f"superseded {name} by {m['by']}: {state} ({why})")
        return False
    if state == "lapsed":
        m.update(state="lapsed", why=why, decided=ts())
        did.append(f"superseded {name}: the mark lapsed ({why}); the orphan rules apply again")
        return True
    if state != "landed":
        return False
    changed = m.get("state") != "landed"
    if changed:
        m.update(state="landed", why=why, decided=ts())
    closed, line = close_superseded(repo, wt, f"by {m['by']}, which landed ({why})")
    if closed:
        m.update(state="closed", closed=ts(), why=why)
        did.append(f"superseded {name}: {m['by']} landed ({why}): {line}")
        inbox_write("maintain", f"CLOSED {tilde(wt)}: superseded by {m['by']}, which landed ({why})",
                    [], [line], 0, cwd=wt)
        return True
    if m.get("said") != line:
        m["said"] = line
        did.append(f"superseded {name}: {m['by']} landed ({why}); {line}")
        return True
    return changed


def superseded_duty(repo, store, dry_run=False):
    """One maintain pass over the marks: each successor read again; a landed one's worktree closed, a lapsed mark said
    (the orphan rules take it back). Returns what it did."""
    marks = superseded_marks()
    did: list[str] = []
    changes = [_supersede_one(repo, store, wt, m, dry_run, did) for wt, m in sorted(marks.items())
               if m.get("state") in ("open", "landed")]
    if any(changes):
        write_json(superseded_path(), marks)
    return did


def _successor_worktree(path: Any, by: Any) -> Any:
    top = git(path, "rev-parse", "--show-toplevel", check=False).stdout.strip()
    if not top:
        raise ValueError(f"{by} is no git worktree")
    _c, left = cherry_landed(top, f"refs/remotes/{remote()}/main", "HEAD")
    if not left:
        raise ValueError(f"{os.path.basename(top)} carries nothing that is not on main yet: mark it superseded once "
                         "its successor holds the work (commit it there first)")
    return os.path.basename(top), None, os.path.realpath(top), git(top, "rev-parse", "HEAD").strip()


def _successor_change(by: Any, store: Any) -> Any:
    its = [i for i in store.items("change") if i["id"] == by] or [i for i in store.items("change") if by in i["id"]]
    if len(its) != 1:
        raise ValueError(f"--by {by}: no worktree of that name, and {'no' if not its else len(its)} change ids match it")
    it = its[0]
    wt = it["data"].get("worktree")
    return it["id"], it["id"], os.path.realpath(wt) if wt else None, it["data"].get("head")


def resolve_successor(by, repo, store):
    """(display name, change id or None, successor worktree or None, its head or None) of `--by`: a worktree path (or
    the name of a sibling of repo's worktrees), else a change id (the whole id or a unique part of it)."""
    cands = [os.path.expanduser(by), os.path.join(os.path.dirname(os.path.realpath(repo)), by)]
    path = next((c for c in cands if os.path.isdir(c)), None)
    if path:
        return _successor_worktree(path, by)
    return _successor_change(by, store)


def cmd_supersede(a: argparse.Namespace, store: Any) -> int:
    marks = superseded_marks()
    if not a.worktree:
        return _list_marks(marks, a.all)
    wt = os.path.realpath(os.path.expanduser(a.worktree))
    top = git(wt, "rev-parse", "--show-toplevel", check=False).stdout.strip() if os.path.isdir(wt) else ""
    if not top:
        print(f"supersede: {a.worktree} is no git worktree")
        return 2
    wt = os.path.realpath(top)
    if a.undo:
        m = marks.pop(wt, None)
        write_json(superseded_path(), marks)
        print(f"supersede: {tilde(wt)} " + ("is no longer marked" if m else "was not marked"))
        return 0
    if not a.by:
        print("usage: agentctl supersede <worktree> --by <worktree|change id>   (or --undo)")
        return 2
    return _mark_superseded(marks, wt, a.by, store)


def _list_marks(marks: Any, every: Any) -> Any:
    """Print the live marks (every: the closed and lapsed too)."""
    live = {w: m for w, m in marks.items() if every or m.get("state") in ("open", "landed")}
    for w, m in sorted(live.items()):
        print(f"{tilde(w)}: SUPERSEDED BY {m['by']} ({m.get('state')}{': ' + m['why'] if m.get('why') else ''})")
    if not live:
        print("supersede: no superseded worktree" + ("" if every else " open (--all: the closed and lapsed too)"))
    return 0


def _mark_superseded(marks: Any, wt: Any, by: Any, store: Any) -> Any:
    """Mark worktree wt superseded by `by` (a worktree or a change id): the mark written, or why it was refused (2)."""
    repo = git(wt, "rev-parse", "--path-format=absolute", "--git-common-dir").strip()
    repo = os.path.dirname(repo) if repo.endswith("/.git") else repo
    try:
        name, change, succ, head = resolve_successor(by, wt, store)
    except (Exception, SystemExit) as e:  # SystemExit: NoToken (a change id is read from the store)
        print(f"supersede: {str(e).replace('agentctl: ', '')}")
        return 2
    if succ and os.path.realpath(succ) == wt:
        print("supersede: a worktree cannot supersede itself")
        return 2
    marks[wt] = {"by": name, "change": change, "worktree": succ, "head": head, "at": time.time(), "marked": ts(),
                 "state": "open", "repo": repo}
    write_json(superseded_path(), marks)
    print(f"SUPERSEDED BY {name}: {tilde(wt)} -- no orphan escalation; it closes itself once {name} lands")
    return 0


# AN ORPHAN IS SUBMITTED ONLY WHEN NOTHING ELSE CARRIES IT (ISSUE 20261030T165514-e5f431). At 15:50:59Z maintain
# re-submitted ~/projects/movies-wt-review with no session behind it: its commits were checked only against
# origin/main, while the open change movies-wt-ci-5 already carried them -- a duplicate in the shared queue. Now, before
# the one submit: (a) an open change item whose head carries every one of its commits (cherry_landed, the landed-by-
# content helper, against that head) covers it: no submit, no condition -- that change's verdict reaches its author, and
# once it lands the orphan is gone by the same helper; (b) a worktree whose last change item was CANCELLED is never
# re-submitted: a person cancelled it, so UNLANDED ORPHAN goes to a person at once. Any other orphan still gets its
# one submit (design §11 I9) -- turning every orphan into a condition (option c) would leave real finished work, the
# common case, waiting on the owner.
def worktree_change_items(wt, store):
    """{"open": [the open change items], "last": this worktree's newest change item (any state) or None}."""
    try:
        items = store.items("change")
    except (Exception, SystemExit):
        return {"open": [], "last": None}
    rw = os.path.realpath(wt)
    mine = sorted((i for i in items if os.path.realpath(str((i.get("data") or {}).get("worktree") or "")) == rw),
                  key=lambda i: i["id"])
    return {"open": [i for i in items if not machine_state("change", i["state"]).terminal],
            "last": mine[-1] if mine else None}
