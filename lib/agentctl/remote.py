# agentctl, part `remote`: remote checks, the token hand-off, watches.
# Runs inside agentctl's one namespace, after the parts before it in lib/agentctl/__init__.py's PARTS (it uses their names).

# ---------------------------------------------------------------- remote checks: ci-runner's check tier
# The owner (2026-10-27, then decision 1): a queued change's pre-commit checks run on ci-runner's check tier (first only
# under the laptop's pressure; since the laptop's tick went, always: the reconciler is ci-runner's). The checked tip is pushed to `check/<entry id>` (the hook checks nothing
# for a check/ branch: it is a run's ref, never landed), whose push runs .github/workflows/precommit-check.yml: the same
# hook, `--checks-only <merge-base with main>..HEAD`, on the check tier's runner. agentctl follows that run (owned: the run
# is cancelled if the checker dies), reads the verdict -- the same (ok, output) shape as a local check, the failed jobs'
# relevant lines as output -- and deletes the branch. The push to main then carries PREPUSH_REMOTE_RUN=<run id>: the hook
# takes that run's pass for its checks only after GitHub confirms it (the workflow, success, the pushed commit itself).
REMOTE_WORKFLOW = "precommit-check.yml"
REMOTE_TIMEOUT = 2 * 3600
REMOTE_START = 15 * 60  # no run for the pushed branch by then: no verdict (checked locally instead)


def remote_checks_supported(tree):
    return os.path.exists(os.path.join(tree, ".github", "workflows", REMOTE_WORKFLOW))


class Terminated(SystemExit):
    """A signal's exit (Owned's handler): never swallowed by a wait loop that rides out a token or tunnel outage."""


def remote_matches(e, base, head, mode):
    """The entry's recorded remote check, when it is the check of exactly this tip on this base in this mode."""
    rem = e.get("remote") or {}
    if rem.get("tip") == head and rem.get("base") == base and rem.get("mode") == mode and rem.get("head") == e.get("head") \
            and rem.get("status") not in ("cancelled", "skipped", "abandoned"):
        return rem
    return None


# THE VERDICT IS THE CHECK JOBS' (the owner, 2026-11-07, "do the structural fix"): batch #20's 13 changes sat 27+ min
# past their run's last check job, behind a `timings / report` job QUEUED for a light runner of a saturated ci-runner,
# because the verdict waited for the RUN to complete. The workflow declares its verdict's jobs itself -- a workflow-level
# `VERDICT_JOBS: <glob>, <glob>, ...` of job names (fnmatch), read at the very commit the run checks -- and the verdict
# is in once every job it names has concluded: a job outside it (a report, a publish) still queued or running holds
# nothing. Any job that concluded in failure fails the check at once, named or not. A workflow declaring none is decided
# by the run's own conclusion, as before. The hook's remote pass (movies' scripts/hooks/pre-push) reads the same line.
VERDICT_JOBS_LINE = re.compile(r"^[ \t]+VERDICT_JOBS:[ \t]*['\"]?([^'\"\n#]*)", re.M)
JOB_FAILED = ("failure", "timed_out", "startup_failure", "action_required")


# A JOB GITHUB HAS NOT CREATED YET IS NOT IN THE RUN'S JOB LIST (measured 2026-11-08: precommit-check run 42463101933's
# `check (page)`, which needs every other check, was created 1 s after `check (rest)` concluded; Main's publish-* jobs
# are absent from a running Main's list). Judged by the listed jobs alone, every named job had concluded in that second
# and the verdict was a success without the page checks. So the workflow's own jobs say which verdict jobs must exist:
# each job of its `jobs:` block whose name (its `name:`, else its key) a VERDICT_JOBS glob names must be listed (a
# `${{ ... }}` in the name matches any text, a reusable workflow's jobs are `<name> / <inner>`) before a success counts.
JOB_KEY_LINE = re.compile(r"^( +)([A-Za-z_][\w-]*):[ \t]*(?:#.*)?$")
JOB_NAME_LINE = re.compile(r"^( +)name:[ \t]*(.*?)[ \t]*$")
EXPRESSION = re.compile(r"\$\{\{.*?\}\}")


def workflow_job_names(text):
    """The name templates of a workflow's jobs, as its text declares them (a job without `name:` is named by its key)."""
    names, lines = [], text.splitlines()
    try:
        i = next(n for n, l in enumerate(lines) if re.match(r"^jobs:[ \t]*(?:#.*)?$", l)) + 1
    except StopIteration:
        return []
    job_indent = job = props_indent = None
    for line in lines[i:]:
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        indent = len(line) - len(line.lstrip(" "))
        if indent == 0:
            break
        key = JOB_KEY_LINE.match(line)
        if job_indent is None and key:
            job_indent = indent
        if indent == job_indent and key:
            if job:
                names.append(job["name"])
            job, props_indent = {"name": key.group(2)}, None
            continue
        if job is None or indent <= job_indent:
            continue
        if props_indent is None:
            props_indent = indent
        named = JOB_NAME_LINE.match(line)
        if indent == props_indent and named:
            value = named.group(2)
            quoted = re.match(r"""^(['"])(.*?)\1""", value)
            job["name"] = quoted.group(2) if quoted else value.split(" #", 1)[0].rstrip()
    if job:
        names.append(job["name"])
    return names


def job_name_glob(template):
    """The fnmatch pattern of the listed names a job name template stands for (`${{ ... }}` matches anything)."""
    parts = EXPRESSION.split(template)
    return "*".join(re.sub(r"([*?\[])", r"[\1]", p) for p in parts)


def verdict_jobs(o_r, sha):
    """(the verdict's job-name globs, the name templates of the workflow's jobs they name), as REMOTE_WORKFLOW declares
    them at commit `sha`; ([], []) when it declares none, or when no job of its `jobs:` block can be read (then the run's
    own conclusion decides: an expectation that cannot be read never lets a success in early)."""
    import base64
    r = api(o_r, "GET", f"contents/.github/workflows/{REMOTE_WORKFLOW}", {"ref": sha}) or {}
    text = base64.b64decode(r.get("content") or "").decode(errors="replace")
    m = VERDICT_JOBS_LINE.search(text)
    globs = [g.strip() for g in m.group(1).split(",") if g.strip()] if m else []
    expect = [t for t in workflow_job_names(text) if any(fnmatch.fnmatchcase(t, g) for g in globs)]
    return (globs, expect) if expect else ([], [])


def jobs_verdict(jobs, globs, expect=()):
    """(verdict, failed jobs, jobs still going outside the verdict) of a run from its jobs (GitHub's REST shape): verdict
    `failure` once any job concluded in failure, `success` once at least one job matches `globs`, every one that does
    has concluded otherwise and every template of `expect` (the workflow's verdict jobs) is listed, else None
    (undecided)."""
    failed = [j for j in jobs if j.get("status") == "completed" and j.get("conclusion") in JOB_FAILED]
    named = [j for j in jobs if any(fnmatch.fnmatchcase(j.get("name") or "", g) for g in globs)]
    going = [j for j in jobs if j.get("status") != "completed" and j not in named]
    if failed:
        return "failure", failed, going
    listed = [j.get("name") or "" for j in jobs]
    created = all(any(n == t or fnmatch.fnmatchcase(n, job_name_glob(t)) or fnmatch.fnmatchcase(n, job_name_glob(t) + " / *")
                      for n in listed) for t in expect)
    if named and created and all(j.get("status") == "completed"
                                 and j.get("conclusion") in ("success", "skipped", "neutral") for j in named):
        return "success", [], going
    return None, [], going


def remote_poll(o_r, rem):
    """STATELESS: the remote check's state from the forge alone -- the run (found by its check/ branch and tip when its id
    is not known yet), its status, and for a decided run the verdict: decided by its verdict jobs while it still runs
    (VERDICT_JOBS above), else by its conclusion. Returns the updated record (a copy); raises when the forge cannot be
    read."""
    rem = dict(rem)
    if rem.get("run") is None:
        runs = forge_actions_runs(o_r, workflow=REMOTE_WORKFLOW, limit=50)
        hit = next((x for x in runs if x.get("head_sha") == rem["tip"]
                    and x.get("head_branch") in (rem["branch"], f"refs/heads/{rem['branch']}")), None)
        if not hit:
            return rem
        rem["run"] = hit["id"]
    run = forge_actions_run(o_r, rem["run"])
    status = run.get("status")
    rem["run_status"] = status
    name = f"{REMOTE_WORKFLOW} run {rem['run']} (#{run.get('run_number')})"
    if rem.get("status"):
        return rem
    if status in ("running", "waiting"):
        if rem.get("verdict_jobs") is None or rem.get("verdict_expect") is None:
            rem["verdict_jobs"], rem["verdict_expect"] = verdict_jobs(o_r, rem["tip"])
        if not rem["verdict_jobs"]:
            return rem
        jobs = (api(o_r, "GET", f"actions/runs/{rem['run']}/jobs", {"per_page": 100}) or {}).get("jobs") or []
        verdict, failed, going = jobs_verdict(jobs, rem["verdict_jobs"], rem["verdict_expect"])
        if verdict is None:
            return rem
        rem["status"], rem["decided_at"] = verdict, time.time()
        rest = f"; not waited for: {', '.join(j.get('name', '?') for j in going)}" if going else ""
        if verdict == "success":
            rem["out"] = f"{name}: success (its verdict jobs: {', '.join(rem['verdict_jobs'])}{rest})"
        else:
            out = [f"{name} failed: {run.get('html_url')}"]
            for j in failed:
                out += [f"-- {j.get('name', '?')}"] + job_log_lines(o_r, rem["run"], {**j, "databaseId": j.get("id")}, 25)
            rem["out"] = "\n".join(out)
        return rem
    if status in ("success", "failure", "cancelled", "skipped"):
        rem["status"], rem["decided_at"] = status, time.time()
        if status == "success":
            rem["out"] = f"{name}: success"
        elif status == "failure":
            out = [f"{name} failed: {run.get('html_url')}"]
            try:
                for job, ls in relevant_lines(fetch_failed_log(o_r, rem["run"]), 25).items():
                    out += [f"-- {job}"] + ls
            except Exception as ex:
                out.append(f"(its failed log could not be read: {str(ex)[:120]})")
            rem["out"] = "\n".join(out)
        else:
            rem["out"] = f"{REMOTE_WORKFLOW} run {rem['run']} {status}"
    return rem


def save_remote(d, e, rem):
    """The remote check's record in the entry, saved at once (a replacement checker adopts it); `remote_run` mirrors the
    run for the push's hook and `waiting status`."""
    e["remote"] = rem
    if rem.get("run") is not None:
        e["remote_run"] = rem["run"]
    else:
        e.pop("remote_run", None)
    if d and e.get("seq") is not None:
        save_entry(d, e)


def delete_check_branch(o_r, branch):
    try:
        forge_delete_branch(o_r, branch)
    except Exception as ex:
        err(f"could not delete {branch} ({str(ex)[:120]}): `agentctl sweep` lists it")


def remote_mode_prefix(e, base, mode):
    """The check branch's mode prefix (remote_check): `fast-`, `verify-<full base sha>-` or none."""
    if mode == "--fast-only":
        return "fast-"
    if mode == VERIFY_MODE:
        return f"verify-{e.get('full_base') or base}-"
    return ""


def remote_check(repo, e, base, head, mode, own=None):
    """(ok, output) of the change's checks on ci-runner; ok None when no verdict came (the caller checks locally).
    The run belongs to the ENTRY, not to this process: its record (`remote` in the entry) is saved the moment the branch is
    pushed and again when the run is found, so a checker that dies or is replaced mid-wait strands nothing -- the next one
    ADOPTS the run (no new push, no new run) and the processor re-derives its state from GitHub every cycle. Its time
    bounds count from the push, not from this process's start."""
    o_r = slug(repo)
    try:
        d = waiting_state(repo)[0]
    except (Exception, SystemExit):
        d = None
    rem = remote_matches(e, base, head, mode)
    if rem and d:
        wlog(d, f"{e['id']}: adopting its remote check (run {rem.get('run') or 'not found yet'}, pushed "
                f"{int((time.time() - rem['pushed_at']) // 60)} min ago)")
    if not rem:
        # the branch NAMES the hook's mode for precommit-check.yml: check/fast-<id> runs `--fast-only` (the batch's
        # combined check, as on the laptop), check/verify-<full base sha>-<id> `--verify-only <full base>..HEAD` (a
        # change's combined tree, queue.py's THE RULE), any other check/<id> `--checks-only`
        branch = f"check/{remote_mode_prefix(e, base, mode)}{e['id']}"
        r = git(repo, "push", "--quiet", "--force", remote(), f"{head}:refs/heads/{branch}", check=False)
        if r.returncode != 0:
            return None, f"push of {branch} failed: {r.stderr.strip()[:300]}"
        rem = {"branch": branch, "head": e.get("head"), "base": base, "tip": head, "mode": mode, "run": None,
               "pushed_at": time.time(), "status": None}
        save_remote(d, e, rem)
        try:
            record_dispatch(branch=branch, workflow=REMOTE_WORKFLOW, worktree=e.get("worktree"), sha=head,
                            agent="land queue", pid=os.getpid(), purpose=f"pre-commit check of queue entry {e['id']}")
        except (Exception, SystemExit) as ex:
            err(f"remote check of {e['id']}: owner not recorded ({str(ex)[:120]})")
    delays = backoff(15, 120)
    while True:
        try:
            new = remote_poll(o_r, rem)
            if new.get("run") is not None and rem.get("run") is None:
                try:
                    save_dispatch({"branch": rem["branch"], "run_id": new["run"]})
                except OSError:
                    pass
            if new != rem:
                rem = new
                save_remote(d, e, rem)
        except Terminated:
            raise
        except (Exception, SystemExit) as ex:  # GitHub down for a while: keep reading until the bound
            err(f"remote check of {e['id']}: {str(ex)[:160]}")
        try:  # the CI run is the check's work: its status (and, while it runs, the minute) is the progress mark
            lock_beat(f"remote:{rem.get('run')}:{rem.get('status') or rem.get('run_status') or 'pending'}:"
                      f"{int(time.time() // 60)}")
        except LeaseLost as ex:
            err(f"remote check of {e['id']}: {ex}; a new checker adopts its run")
            return None, str(ex)
        if rem.get("status"):
            rem["collected_at"] = time.time()
            save_remote(d, e, rem)
            delete_check_branch(o_r, rem["branch"])
            if rem["status"] in ("success", "failure"):
                return rem["status"] == "success", rem["out"]
            return None, rem["out"]
        age = time.time() - rem["pushed_at"]
        if rem.get("run") is None and age > REMOTE_START:
            rem.update(status="abandoned", collected_at=time.time())
            save_remote(d, e, rem)
            delete_check_branch(o_r, rem["branch"])
            return None, f"no {REMOTE_WORKFLOW} run started for {rem['branch']} in {REMOTE_START // 60} min"
        if age > REMOTE_TIMEOUT:
            cancel_run(o_r, rem["run"], "the remote check timed out")
            rem.update(status="abandoned", collected_at=time.time())
            save_remote(d, e, rem)
            delete_check_branch(o_r, rem["branch"])
            return None, f"{REMOTE_WORKFLOW} run {rem['run']} undecided after {REMOTE_TIMEOUT // 3600} h"
        time.sleep(next(delays))


def cancel_entry_remote(e, why):
    """An entry that ends (superseded, cancelled) takes its undecided remote run with it: the run is the entry's."""
    rem = e.get("remote") or {}
    if rem.get("run") is not None and not rem.get("status"):
        try:
            cancel_run(slug(e["worktree"]) if os.path.isdir(e.get("worktree") or "") else SHORT["movies"], rem["run"], why)
        except (Exception, SystemExit) as ex:
            err(f"could not cancel run {rem['run']} of {e['id']}: {str(ex)[:120]}")


def processor_owned(d, repo=None):
    """The processor's ownership: one per waiting state directory (one per repository)."""
    return Owned("waiting", os.path.basename(d.rstrip("/")) or "repo", ["waiting", "run", "--repo", repo or ""])


def waiting_processor_alive(d):
    return lock_held(processor_owned(d).lockpath)


def spawn_waiting_processor(repo):
    d, clone, _w = waiting_state(repo)
    if waiting_processor_alive(d):
        return
    repo = clone if os.path.isdir(clone) else repo  # the clone, not a joiner's worktree: that one closes after WATCH OK
    with open(os.path.join(d, "processor.log"), "a") as logf:
        spawn_detached([sys.executable, TOOL_PATH, "waiting", "run", "--repo", repo],
                         stdin=subprocess.DEVNULL, stdout=logf, stderr=logf, start_new_session=True, cwd=repo,
                         env=detached_env(repo))


# THE TOKEN HAND-OFF. The processor as a launchd service, or detached once its land exits, gets no answer from the vault
# (an unlock belongs to the session that made it). So it listens on a unix socket (0600, in a 0700 directory) and a
# process that can read the vault -- every `land` that joins `waiting`, `waiting status` / `resume`, any command whose
# token() read the vault (seed_processor), and the watchdog's kick (kick_processor) -- hands it the token. It lives only in the processor's memory and environment (a re-exec keeps it; the push's hook and the watches it
# starts inherit it), never on disk. Until it has one, the gate is unread: taken as red, said once in the inbox.
def token_sock_path(d):
    import hashlib
    base = os.environ.get("AGENTCTL_SOCK_DIR") or f"/tmp/agentctl-{os.getuid()}"  # short: a socket path is <= 104 bytes
    return os.path.join(base, hashlib.sha1(os.path.abspath(d).encode()).hexdigest()[:12] + ".sock")


TOKEN_SHAPE = re.compile(r"[\w.~+/=-]{8,512}")
TOKEN_REQUEST = b"?token\n"  # a client asking the processor for its token (not shaped like one)


class TokenDrop:
    """The processor's end of the hand-off."""

    def __init__(self, d, o_r):
        self.path, self.o_r, self.sock, self.thread = token_sock_path(d), o_r, None, None

    def open(self):
        import socket
        try:
            os.makedirs(os.path.dirname(self.path), mode=0o700, exist_ok=True)
            os.chmod(os.path.dirname(self.path), 0o700)
            if os.path.exists(self.path):
                os.unlink(self.path)
            s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            old = os.umask(0o177)
            try:
                s.bind(self.path)
            finally:
                os.umask(old)
            s.listen(4)
            self.sock = s
            import threading  # served on a thread of its own: no step of the main loop can leave a client waiting
            self.thread = threading.Thread(target=self.serve, name="token-socket", daemon=True)
            self.thread.start()
        except OSError as e:
            err(f"no token hand-off socket ({e}): the processor reads the vault itself")
        return self

    def held(self):
        return os.environ.get(token_env_name(self.o_r), "")

    def serve(self):
        import select
        while self.sock:
            try:
                ready, _, _ = select.select([self.sock], [], [], 1)
                if ready and self.sock:
                    self.take()
            except (OSError, ValueError):
                return

    def nap(self, secs):
        """Sleep secs (the socket is served by its own thread)."""
        end = time.time() + secs
        while True:
            left = end - time.time()
            if left <= 0:
                return
            time.sleep(min(left, 10))

    def take(self):
        try:
            conn, _ = self.sock.accept()
        except OSError:
            return
        with conn:
            conn.settimeout(5)
            try:
                tok = conn.recv(4096).decode().strip()
                if tok == TOKEN_REQUEST.decode().strip():  # a same-uid process (the socket is 0600) asks for the token
                    conn.sendall(self.held().encode())
                    return
            except (OSError, UnicodeDecodeError):
                return
            key = token_env_name(self.o_r)
            if TOKEN_SHAPE.fullmatch(tok) and os.environ.get(key) != tok:  # said once per real change, never per offer
                os.environ[key] = tok
                err("the store token was handed over: the gate is read again")
                token_back(token_ns(self.o_r))

    def close(self):
        """Stop serving, and wait for a hand-off already taken to finish: once close returns, no token lands in this
        process's environment (2026-11-16: a test's drop wrote $AGENTCTL_STORE_TOKEN after its tearDown had restored
        the environment, and test_forge, next in the same worker, saw that token)."""
        if self.sock:
            sock, self.sock = self.sock, None
            sock.close()
            try:
                os.unlink(self.path)
            except OSError:
                pass
        import threading  # the part's own import, as in open()
        thread, self.thread = self.thread, None
        if thread is not None and thread is not threading.current_thread():
            thread.join(5)


def offer_token(repo):
    """Hand this process's store token to the repo's waiting processor when it listens (see THE TOKEN HAND-OFF) AND
    lacks it: asked first (one socket round trip), so an interactive command hands over only on a real transition
    (2026-10-30: every `land` / `waiting status` handed it over, 175 "handed over" lines in 400). Never fatal; True when
    handed over."""
    try:
        d, _c, _w = waiting_state(repo)
        path = token_sock_path(d)
        if not os.path.exists(path):
            return False
        return bool(hand_token(path, token(slug(repo))))
    except (Exception, SystemExit):
        return False


def hand_token(path, tok):
    """Ask the processor at socket `path` for the token it holds and hand it `tok` when it holds another or none: True
    when handed over, False when it already held it, None when nothing answered."""
    import socket
    try:
        if token_request(path) == tok:
            return False
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
            s.settimeout(3)
            s.connect(path)
            s.sendall(tok.encode())
        return True
    except OSError:
        return None


def await_entry(repo, eid, close=None):
    """Block until the entry lands or is rejected: (rc, landed sha, pushed_at). Restarts a dead processor. OWNED (one
    awaiter per entry; on agentctl's new code it re-execs as `agentctl waiting await <entry>`, which then watches)."""
    own = Owned("await", eid, ["waiting", "await", eid, "--repo", repo] + (["--close", close] if close else []))
    if not own.acquire():
        h = own.holder or {}
        print(f"NOT LANDED (still waiting): entry {eid} is already awaited by pid {h.get('pid')} ({h.get('owner')})")
        return 2, None, None
    try:
        return _await_entry(repo, eid, own)
    finally:
        own.release()


def _await_entry(repo, eid, own):
    """The change's STORE ITEM decides (a RemoteStore on the laptop): an entry's JSON is only on the host whose queue
    runs it -- ci-runner's for every change since ONE QUEUE (2026-11-16: `waiting await` on the laptop crashed with
    FileNotFoundError on one). A joined change's item follows its waiting entry (project_changes)."""
    t0, delays = time.time(), backoff(15, 60)
    timeout = env_num("AGENTCTL_WAITING_TIMEOUT", WAITING_AWAIT_TIMEOUT)
    state, said = "not in the store yet", None
    while True:
        try:
            it = Store().item("change", eid)
            if not it:
                state = "not in the store yet"
        except StoreUnavailable as ex:  # a blip: asked again at the next poll, bounded by the timeout below
            it, state = None, f"unknown (the store did not answer: {str(ex)[:80]})"
        if it:
            state, data = it["state"], it.get("data") or {}
            if state == "landed":
                sha = data.get("landed_sha") or data.get("tip") or data.get("head")
                print(f"LANDED {sha} (with the {data.get('batch', 1)} change(s) of `{WAITING_BRANCH}`, once main was "
                      "green)", flush=True)
                return 0, sha, data.get("pushed_at") or it.get("entered_at")
            if state == "superseded":  # its newer entry lands the change: exit 0, as its event says (supersede)
                why = str(data.get("why") or "")
                print(why if why.startswith("SUPERSEDED") else
                      f"SUPERSEDED by {data.get('superseded_by') or 'another entry'}: {why or 'superseded'}", flush=True)
                return 0, None, None
            if machine_state("change", state).terminal:  # rejected, cancelled: never hangs a follow
                why = str(data.get("why") or state)
                print(why if why.startswith("NOT LANDED") else f"NOT LANDED: {why}", flush=True)
                return 1, None, None
        if state != said:  # what it waits on, said once each time it changes (a store outage is never silent)
            err(f"awaiting {eid}: it is {state}")
            said = state
        if time.time() - t0 > timeout:
            print(f"NOT LANDED (still waiting): entry {eid} is {state} after {int(timeout // 3600)} h; "
                  "`agentctl land status`")
            return 2, None, None
        spawn_waiting_processor(repo)
        restart_if_changed([own])
        time.sleep(next(delays))


def assemble(repo, d, wt, entries, base):
    """The batch's tip in check tree wt = base + each entry's commits, in order; a conflicting entry is rejected alone,
    one already on main LANDED, a duplicate superseded. Returns the kept entries (each with its tip in the batch)."""
    git(wt, "reset", "--hard", "-q", base)
    git(wt, "checkout", "-q", "--force", "--detach", base)
    kept = []
    for e in entries:
        how, x = apply_entry(d, wt, e, base)
        if how == "conflict":
            queue_verdict(d, e, "rejected", f"NOT LANDED: main moved while it waited and the change no longer applies "
                                            f"(conflicts in: {', '.join(x)}): rebase onto {remote()}/main and land again")
            continue
        if how == "landed":  # all of it reached main meanwhile: LANDED, and nothing of it is pushed again
            already_landed(d, e, x)
            continue
        if how == "duplicate":  # an earlier entry carries the very same change: that one lands it
            owner = next((k for k in kept if x and git(wt, "merge-base", "--is-ancestor", x, k["tip"],
                                                        check=False).returncode == 0), None)
            supersede(d, e, owner["id"] if owner else "an earlier entry",
                      "the same change already waits" + (f" as {owner['id']}" if owner else ""))
            continue
        e["tip"] = git(wt, "rev-parse", "HEAD").strip()  # its tip IN THE BATCH: bisect and the push read it from disk
        save_entry(d, e)
        kept.append(e)
    return kept


def sbt_stop(tree):
    """A FRESH sbt server for the batch check: stop the tree's server and keeper (scripts/sbt-warm --stop), if any."""
    script = os.path.join(tree, "scripts", "sbt-warm")
    if os.access(script, os.X_OK):
        try:
            subprocess.run([script, "--stop"], cwd=tree, capture_output=True, text=True, timeout=180, env=tool_env())
        except subprocess.TimeoutExpired:
            err(f"sbt-warm --stop in {tree} took over 3 min")


STALL_SIGNAL = re.compile(r"(^|\s)(pre-push |sbt-warm )?STALLED:", re.M)


def batch_mode(hook):
    """The combined check's hook mode: `--fast-only` (compile of every test config, the fast tests, the touched files'
    lints and the checks a touched merge=union file selects: each change was checked in full alone, on an older main,
    and the rebase writes a union-merged file no member's check saw) when the hook has it, else the full `--range` dry run."""
    try:
        with open(hook) as f:
            return "--fast-only" if "--fast-only" in f.read() else "--range"
    except OSError:
        return "--range"


def batch_check(repo, d, tree, base, tip, cls="check"):
    """The hook's dry run over base..tip in `tree` (the basic checks plus every touched area's), on a fresh sbt server,
    under the stall abort. (ok, output, stalled): a stall -- land's 20 min, or the hook's exit 75 / `STALLED:` -- is
    retried once on a fresh server; stalled twice, `stalled` is True."""
    hook = precheck_hook(tree) or precheck_hook(repo)
    if not hook:
        return True, "", False
    if host_role() == "server" and remote_checks_supported(tree):  # ci-runner: the CI check tier (owner decision 1)
        ok, out = remote_check(repo, {"id": f"batch-{tip[:10]}", "head": tip}, base, tip, batch_mode(hook))
        return (ok, out, False) if ok is not None else (False, out, True)
    out = ""
    for attempt in (1, 2):
        git(tree, "checkout", "-q", "--force", "--detach", tip)
        sbt_stop(tree)
        wlog(d, f"checking {base[:10]}..{tip[:10]} (attempt {attempt}, fresh sbt server)")
        with check_slot(repo, f"waiting batch {tip[:10]}", cls) as slot:
            rc, out = run_watched([hook, batch_mode(hook), f"{base}..{tip}"], tree, slot.env(tool_env()),
                                  os.path.join(d, f"check-{tip[:10]}"), f"batch-check-{attempt}", sampler=slot.sample)
            slot.done(rc, out)
        sbt_stop(tree)
        with open(os.path.join(d, "checks.log"), "a") as f:
            f.write(f"=== {ts()} {base[:10]}..{tip[:10]} attempt {attempt} rc {rc}\n{out}\n")
        if rc is None or rc == 75 or STALL_SIGNAL.search(out or ""):
            wlog(d, f"the check of {tip[:10]} STALLED (attempt {attempt})")
            continue
        return rc == 0, out, False
    return False, out, True


def main_clone(path):
    """The main clone a worktree belongs to (its git common dir's checkout): what a DETACHED process works from, since
    the worktree it was started for may be removed under it (2026-10-27: a watch crashed with `git rev-parse
    --show-toplevel in ~/projects/movies-wt-gate-closed: cannot change to ...` after its agent removed the worktree at
    LANDED). A path that is gone resolves by name: <clone>-wt-<name> -> <clone>."""
    if path and os.path.isdir(path):
        r = git(path, "rev-parse", "--path-format=absolute", "--git-common-dir", check=False)
        if r.returncode == 0:
            common = os.path.realpath(r.stdout.strip())
            clone = os.path.dirname(common) if os.path.basename(common) == ".git" else common
            if os.path.isdir(clone):
                return clone
        return git(path, "rev-parse", "--show-toplevel").strip()
    m = re.match(r"^(.*?)-wt-[^/]+/?$", path or "")
    if m and os.path.isdir(m.group(1)):
        return m.group(1)
    raise RuntimeError(f"{path}: gone, and no main clone found for it")


# WATCHES FROM ITEMS (design §5.1 `landed`, phase 1 step 9): a landed sha is a `watch` item; each tick evaluates it ONCE
# (no detached process, no loop): its Main run and convergence run (pick_main_run / pick_convergence_run), WATCH OK
# -> ok (the worktree closed when proven merged), WATCH RED -> red (its author told; the red machine attaches it), still
# undecided at its 3 h deadline -> unknown, an escalation (a stuck run). The verdicts reach the author's inbox as
# events. `land --follow` keeps its in-process watch (the agent waits on purpose).
WATCH_EVERY = 60


def start_watch(repo, sha, pushed_at, worktree=None, close=None, session=None):
    """The landed sha's watch item (idempotent)."""
    o_r = slug(repo)
    if not WATCH.get(o_r):
        return None
    return Store().put_item("watch", sha, "watching", {"repo": main_clone(repo), "o_r": o_r, "pushed_at": pushed_at or time.time(),
                                                       "worktree": worktree or repo, "close": close, "session": session,
                                                       "next_at": 0})


def watch_event(it, verdict, severity="info", code=0):
    dd = it["data"]
    return {"text": verdict.splitlines()[0], "severity": severity,
            "audience": {"worktree": dd.get("worktree"), "session": dd.get("session")},
            "refs": {"verdict": verdict.splitlines()[0], "tail": verdict.splitlines()[1:25], "exit": code,
                     "command": "watch", "sha": it["id"], "close": dd.get("close")}}


def tick_watches(now_=None):
    """One evaluation of each due watch item. Returns what it decided."""
    t = now_ if now_ is not None else time.time()
    st = Store()
    did = []
    for it in st.items("watch", states=("watching",)):
        dd = it["data"]
        if t > it["deadline"]:
            st.transition("watch", it["id"], "unknown", it["version"],
                          event=watch_event(it, f"WATCH UNKNOWN: Main or the convergence still undecided for {it['id'][:10]} "
                                                f"after {int(WATCH_TIMEOUT // 3600)} h: a stuck run (`agentctl ci-why`)",
                                            "escalate", 2))
            did.append(f"watch {it['id'][:10]}: unknown")
            continue
        if float(dd.get("next_at") or 0) > t:
            continue
        flows = WATCH.get(dd["o_r"]) or {}
        try:
            verdict = watch_eval(dd["repo"], it["id"], dd["pushed_at"], dd["o_r"], flows, t)
        except (Exception, SystemExit) as e:  # unreadable now: tried again at the next look, the deadline bounds it
            err(f"watch {it['id'][:10]}: could not read the runs ({str(e)[:120]})")
            verdict = None
        if verdict is None:
            st.update_item("watch", it["id"], it["version"], {"next_at": t + env_num("AGENTCTL_WATCH_POLL", WATCH_EVERY)})
            continue
        state, line, red_runs = verdict
        st.transition("watch", it["id"], state, it["version"], event=watch_event(it, line, code=0 if state == "ok" else 3))
        if state == "ok" and dd.get("close") and os.path.isdir(dd["close"]):
            err(close_worktree(dd["close"]))
        for wf, n in red_runs:  # the red machine takes it, its author offered first
            try:
                red_attach(st, wf, n, author={"session": dd.get("session"), "worktree": dd.get("worktree")})
            except (Exception, SystemExit) as e:
                err(f"watch {it['id'][:10]}: could not attach {wf} #{n} ({str(e)[:120]})")
        did.append(f"watch {it['id'][:10]}: {state}")
    return did


def watch_look(repo, sha, o_r, flows, done, pushed_at, since, now_, on_run=None):
    """ONE look at the runs that contain sha (the tick's watch item and `land --follow`'s own watch alike): each
    undecided flow's run is picked; a completed (not cancelled) one goes into `done` as (run, green?), a convergence
    that has not started CONV_START s after `since` as (None, None). on_run(kind, run or None) hears each pick.
    Returns {kind: (run id, status, updatedAt)} of the runs followed (the stall judge's state)."""
    conv_start = env_num("AGENTCTL_WATCH_CONV_START", WATCH_CONV_START)
    seen = {}
    for kind, (name, wfile) in flows.items():
        if kind in done:
            continue
        runs = main_runs(o_r, wfile)
        run = pick_main_run(repo, sha, runs) if kind == "main" else pick_convergence_run(repo, sha, runs, pushed_at, o_r)
        if run is None:
            if kind != "main" and now_ - float(since) >= conv_start:
                done[kind] = (None, None)
                if on_run:
                    on_run(kind, None)
            continue
        seen[kind] = (run.get("databaseId"), run.get("status"), run.get("updatedAt"))
        if run.get("status") == "completed" and not is_cancelled(run):
            done[kind] = (run, run.get("conclusion") == "success")
        if on_run:
            on_run(kind, run)
    return seen


def watch_verdict(o_r, sha, flows, done):
    """('red', lines, [(workflow, number)]) when a decided run is red, ('ok', line, []) when every flow is decided
    green (or the convergence never started), else None."""
    reds = [(k, r) for k, (r, ok) in done.items() if ok is False]
    if reds:
        lines = []
        for kind, r in reds:
            try:
                failed = failed_jobs(o_r, r["databaseId"])
            except (Exception, SystemExit) as e:  # the verdict stands without the job names
                err(f"could not read the jobs of run {r['databaseId']} ({str(e)[:120]})")
                failed = []
            lines.append(f"WATCH RED: {flows[kind][0]} #{r.get('number')} {', '.join(failed) or r.get('conclusion')} "
                         f"(it contains your {sha[:10]}: if your change broke it, fix forward now with `Fixes-Red: "
                         f"{r.get('number')}`)")
        return "red", "\n".join(lines), [(flows[k][0], r.get("number")) for k, r in reds]
    if len(done) == len(flows):
        parts = [f"{flows[k][0].split()[0] if k == 'main' else k} " +
                 (f"#{r.get('number')} green" if r else "none started") for k, (r, ok) in done.items()]
        return "ok", "WATCH OK: " + ", ".join(sorted(parts, key=lambda x: not x.startswith("Main"))), []
    return None


def watch_eval(repo, sha, pushed_at, o_r, flows, now_):
    """The tick's look at a watch item: ('ok', line, []), ('red', lines, reds) or None (undecided)."""
    done = {}
    watch_look(repo, sha, o_r, flows, done, pushed_at, pushed_at, now_)
    v = watch_verdict(o_r, sha, flows, done)
    return (v[0], v[1] + f" ({sha[:10]})", v[2]) if v and v[0] == "ok" else v


def bisect(repo, d, batch, base, bwt):
    """The batch failed: the first entry whose prefix fails (binary search over the prefixes' tips, in the batch's tree
    bwt). (index, output) of the culprit; (None, output) when the whole batch passes on its own again (flaky or infra)."""
    lo, hi, out_hi = 0, len(batch) - 1, ""
    ok, out, stalled = batch_check(repo, d, bwt, base, batch[hi]["tip"])
    if ok or stalled:
        return None, out
    out_hi = out
    while lo < hi:
        mid = (lo + hi) // 2
        ok, out, stalled = batch_check(repo, d, bwt, base, batch[mid]["tip"])
        if stalled:
            return None, out
        if ok:
            lo = mid + 1
        else:
            hi, out_hi = mid, out
    # one flaky failure rejects nobody (audit #10): the culprit's prefix is checked once more before it is rejected
    ok, out, stalled = batch_check(repo, d, bwt, base, batch[lo]["tip"])
    if ok or stalled:
        wlog(d, f"bisect: the prefix through {batch[lo]['id']} passed when checked again: flaky, nobody rejected")
        return None, out
    return lo, out_hi


def unread_note(d, reds, entries):
    """The gate unread while changes wait: once per unread spell, an UNKNOWN inbox entry (they would wait unseen)."""
    if GATE_UNREAD not in reds or condition_item_of(Store(), "gate-unread-waiting"):
        return
    wlog(d, f"the gate is unread ({gate_unread_why[0]}): taken as red")
    open_condition("UNKNOWN", f"UNKNOWN: the waiting processor cannot read the gate ({gate_unread_why[0]}); {len(entries)} "
                              "change(s) wait, taken as red. Hand it the token: `~/bin/agentctl land status` from a "
                              "session with the vault unlocked",
                   run_probe("gate_read", {"since": time.time()}), [], key="gate-unread-waiting", command="waiting",
                   exit_code=2, cwd=entries[0].get("worktree") if entries else None)
