# agentctl, part `queueuntil`: land --until-done and durable verdicts.
# Runs inside agentctl's one namespace, after the parts before it in lib/agentctl/__init__.py's PARTS (it uses their names).

# LAND --UNTIL-DONE (the owner, 2026-11-18): agents kept ending badly -- stopping after the rebase without landing,
# handing back partial reports or the same report twice, leaving worktrees behind, and `git branch -D` by hand after a
# landing rebased their commits (never merged by ancestry). This mode owns the whole tail, bounded by --deadline:
# submit (or, called again after an interruption, find its own entry by worktree and head: never queued twice), the
# fast precheck in the foreground, the wait for the verdict, and then
#   NOT LANDED -> the exact reason and the next command (rebase / fix / land again), exit 1;
#   LANDED     -> every local commit's patch proven on origin/main (git cherry, content twins), the worktree clean and
#                 this agent's own (the change's worktree, not the root checkout, no other session's) -> the worktree
#                 removed and its branch deleted (the force delete only after that patch check, close_worktree):
#                 `DONE <sha> (...): ... worktree removed`, exit 0. Not provable -> `LANDED <sha> ... NOT DONE: <why>`,
#                 the worktree kept, exit 3. The caller's own processes in it (its kept sbt server) are stopped first
#                 (cleanup.py free_worktree); still held by another's -> each holder named and the command, exit 4;
#   the deadline -> `NOT DONE: <id> is <state> ...`, the command to resume, exit 2.
# A VERDICT IS DURABLE (2026-11-19: subagents' `--until-done` waits died with a Claude session restart, and they handed
# back "queued, waiting" with no verdict). The change's item holds it, and its exit event reaches the inbox naming the
# worktree and the change (the projector), whether or not a call waited; `land status <entry>` prints it after the
# fact; and a call again from the worktree at that head finds its entry (own_change_item): open -> RESUMING, landed ->
# the cleanup, REJECTED -> that verdict and the next command (before, a rejection decided while nothing waited was
# silently queued again unchanged) -- but only while it STILL HOLDS (verdict_still_holds; 2026-11-21,
# movies-devpanel-oauth: a rejection from 17:02, already printed to its waiter, was replayed at 17:35 as decided "while
# no --until-done waited" though main had moved and fixed the main-side bug behind it): never delivered to a waiter and
# decided on the main of today. Otherwise the change is queued, so checked, again.
UNTIL_DONE_DEADLINE = 4 * 3600  # s (--deadline): a batch at green, a verify round and a push fit well inside it


def parse_duration(text):
    """Seconds of `90`, `90s`, `30m`, `3h`."""
    m = re.fullmatch(r"\s*(\d+(?:\.\d+)?)\s*([smh]?)\s*", str(text))
    if not m:
        raise argparse.ArgumentTypeError(f"not a duration: {text!r} (90, 90s, 30m, 3h)")
    return float(m.group(1)) * {"": 1, "s": 1, "m": 60, "h": 3600}[m.group(2)]


UNTIL_DONE_AGAIN = "~/bin/agentctl land --until-done"


def own_change_item(repo, head, store=None):
    """The change land submitted from worktree `repo` at `head`, as {id, state, data, entered_at}: the open one, else
    the newest decided one. Read from the store's change items and from this host's queue entries (an entry the tick
    has not made an item of yet: the reconciler's own queue)."""
    rp = os.path.realpath(repo)

    def ours(x):
        return x.get("head") == head and os.path.realpath(x.get("worktree") or "") == rp
    mine = [it for it in (store or open_store()).items("change") if ours(it.get("data") or {})]
    known = {it["id"] for it in mine}
    try:
        d = waiting_state(repo)[0]
    except RuntimeError:
        d = None
    for e in waiting_entries(d, open_only=False) if d else []:
        if e["id"] not in known and ours(e) and e.get("status") in ENTRY_STATE:
            mine.append({"id": e["id"], "state": ENTRY_STATE[e["status"]], "data": e, "entered_at": e.get("seq") or 0})
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


def until_done_finish(repo, eid, sha, a, item=None):
    """LANDED: the proof, then the cleanup (LAND --UNTIL-DONE above). Returns the exit code."""
    def kept(why):
        print(f"LANDED {sha} (queued {eid}); NOT DONE: the worktree {repo} is kept: {why}", flush=True)
        return 3
    data = (item or {}).get("data") or {}
    sid = os.environ.get("CLAUDE_CODE_SESSION_ID")
    if data.get("session") and sid and data["session"] != sid:
        return kept(f"not this session's (the change was landed by session {data['session']}); its own agent cleans up")
    try:
        root = os.path.realpath(worktrees(repo)[0]["worktree"])
    except RuntimeError as e:
        return kept(str(e)[:200])
    if os.path.realpath(repo) == root:
        return kept("the root checkout is never removed")
    git(repo, "fetch", "--quiet", remote(), "main", check=False)
    commits, left = cherry_landed(repo, f"refs/remotes/{remote()}/main", "HEAD")
    if commits is None:
        return kept(f"git cherry could not compare it with {remote()}/main")
    if left:
        return kept(f"{len(left)} local commit(s) whose patch is not on {remote()}/main ({', '.join(c[:10] for c in left[:3])})"
                    f": land them with {UNTIL_DONE_AGAIN}")
    if getattr(a, "keep_worktree", False):
        print(f"DONE {sha} (queued {eid}): every local commit is on {remote()}/main; worktree kept (--keep-worktree)",
              flush=True)
        return 0
    held = free_worktree(repo)
    if held:
        print(f"LANDED {sha} (queued {eid}); NOT DONE: the worktree {repo} is kept: still held after "
              f"{int(env_num('AGENTCTL_FREE_WAIT', FREE_WAIT))} s by processes not this session's to stop:", flush=True)
        for pid, cmd in sorted(held.items()):
            print(f"  pid {pid}: {cmd[:200]}", flush=True)
        print(f"next: ask their owner to stop them (or `kill {' '.join(map(str, sorted(held)))}` if they are yours), "
              f"then {UNTIL_DONE_AGAIN} from {repo}: it only cleans up", flush=True)
        return 4
    msg = close_worktree(repo)
    if not msg.startswith("closed worktree"):
        return kept(msg.split(": ", 1)[-1])
    print(f"DONE {sha} (queued {eid}): every local commit's patch is on {remote()}/main; worktree removed ({msg})",
          flush=True)
    return 0


def land_until_done(repo, main_ref, sh, a):
    """`land --until-done` (LAND --UNTIL-DONE above)."""
    t0 = time.time()
    bound = a.deadline if a.deadline is not None else env_num("AGENTCTL_UNTIL_DONE_DEADLINE", UNTIL_DONE_DEADLINE)
    head = git(repo, "rev-parse", "HEAD").strip()
    it = own_change_item(repo, head)
    if it and it["state"] == "landed":
        eid = it["id"]
    elif it and it["state"] == "rejected" and verdict_still_holds(repo, main_ref, it):
        # A VERDICT IS DURABLE (above): decided while no call waited, on this very main, said now
        take_delivery(it)
        why = str((it.get("data") or {}).get("why") or f"{it['id']} rejected")
        print(f"DECIDED {it['id']} ({head[:10]}) while no --until-done waited:", flush=True)
        print(why if why.startswith("NOT LANDED") else f"NOT LANDED: {why}", flush=True)
        print(next_command(repo, why) + f" (to queue {head[:10]} again unchanged: ~/bin/agentctl land)", flush=True)
        return 1
    elif it and not machine_state("change", it["state"]).terminal:
        eid = it["id"]
        print(f"RESUMING {eid} ({head[:10]}, {it['state']}): waiting for its verdict", flush=True)
    else:
        git(repo, "fetch", "--quiet", remote(), "main", check=False)
        commits, left = cherry_landed(repo, main_ref, "HEAD")
        if commits is not None and not left and git_ok(repo, "rev-parse", "--verify", "-q", main_ref):
            # nothing of its own left to land (it reached main another way, or there never was a commit)
            return until_done_finish(repo, "-", git(repo, "rev-parse", main_ref).strip(), a)
        a.until_done = True
        rc = enqueue(repo, main_ref, sh, a)
        eid = getattr(a, "queued_id", None)
        if rc != 0 or not eid:
            print(next_command(repo, ""), flush=True)
            return 1
    state, data, _waited = await_verdict(eid, max(0.0, bound - (time.time() - t0)))
    if state == "landed":
        sha = data.get("landed_sha") or landed_sha_of(eid, data)
        print(f"LANDED {sha} (queued {eid})", flush=True)
        return until_done_finish(repo, eid, sha, a, (it if it and it["id"] == eid else None)
                                 or open_store().item("change", eid))
    if change_decided(state):
        why = str(data.get("why") or f"{eid} {state}")
        print(why if why.startswith("NOT LANDED") else f"NOT LANDED: {why}", flush=True)
        print(next_command(repo, why), flush=True)
        return 1
    print(f"NOT DONE: {eid} is {state} after {fmt_age(time.time() - t0)} (the --deadline); it stays queued -- run "
          f"{UNTIL_DONE_AGAIN} again from {repo}: it resumes this entry", flush=True)
    return 2
