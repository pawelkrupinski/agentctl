# agentctl, part `fixer`: the red fixers (dispatch, sessions, the fixer wrapper).
# Runs inside agentctl's one namespace, after the parts before it in lib/agentctl/__init__.py's PARTS (it uses their names).

# THE FIXER BRIEF (the owner, 2026-10-30): the rules every dispatched fixer and family owner gets -- fast reproducer
# first, mechanism-only identity fixes, no allow-list or baseline edits, land as you go, `agentctl issue` for defects,
# bounded commands -- live in ONE tracked file, docs/fixer-brief.md beside the entry, and go into the prompt word for
# word. The code adds only what is particular to the red (its run, job, line, lease) and to the moment (the nudge time).
FIXER_BRIEF = os.path.join(os.path.dirname(TOOL_PATH), "docs", "fixer-brief.md")


REPO_SECTION = re.compile(r"^### REPO (\S+)\s*$", re.M)


def fixer_brief_for(text, o_r):
    """The brief for a red of o_r: everything but the `### REPO <owner/name>` sections of OTHER repos (each section
    runs to the next `#`/`##`/`### REPO` heading)."""
    out, pos = [], 0
    for m in REPO_SECTION.finditer(text):
        out.append(text[pos:m.start()])
        nxt = re.compile(r"^(#{1,2} |### REPO )", re.M).search(text, m.end())
        end = nxt.start() if nxt else len(text)
        if m.group(1) == o_r:
            out.append(text[m.start():end])
        pos = end
    out.append(text[pos:])
    return re.sub(r"\n{3,}", "\n\n", "".join(out)).strip()


def fixer_brief(o_r=None):
    """(path, text) of the fixer brief: $AGENTCTL_BRIEF_SRC when it names a readable file (an override, for trying a
    wording out), else the tracked docs/fixer-brief.md -- with only o_r's per-repo section (default the primary). Never
    raises: a missing brief is said in the prompt."""
    for p in (os.environ.get("AGENTCTL_BRIEF_SRC", BRIEF_SRC), FIXER_BRIEF):
        if p:
            try:
                with open(p) as f:
                    return p, fixer_brief_for(f.read().strip(), o_r or RED_PRIMARY)
            except OSError:
                pass
    return FIXER_BRIEF, (f"(THE FIXER BRIEF IS MISSING at {FIXER_BRIEF}: file it with `~/bin/agentctl issue`, then go on "
                         "under ~/bin/AGENTCTL.md's rules.)")


# OWNER-ONLY REPOS (red-repos.json `"push": "owner-only"`; infra: a push to main is a deploy). Their fixers never land,
# push or deploy: the prompt says so (red_owner_rules, after every task text that says "land it"), the session denies
# the commands (OWNER_ONLY_DENIED), `agentctl land` refuses inside such a fixer, and `agentctl red owner-push` files the
# prepared branch with the owner as a one-liner.
OWNER_ONLY_DENIED = ("Bash(*agentctl land*)", "Bash(*bin/deploy*)", "Bash(*nixos-rebuild*)",
                     "Bash(*switch-to-configuration*)", "Bash(*git*push*)")


def red_owner_rules(o_r, num):
    return (f" OWNER-ONLY REPO ({o_r}): this overrides every `land` above. NEVER `agentctl land`, never push, never "
            "deploy: a push to its main IS a deploy, and it is the owner's. Prepare the fix on your worktree's branch "
            f"(rebased on {remote()}/main, one commit per step, a `Fixes-Red: {num}` trailer, the repo's own checks run), "
            f"then `~/bin/agentctl red owner-push {num} --note \"<what it fixes; which units a deploy restarts>\"` from "
            "your worktree, and end with `BLOCKED ON OWNER: <the one-liner it printed>`.")


def fixer_prompt(u, wt, brief, resume_note=None, owner=None):
    """The fixer's prompt for red u; owner: a family owner's turn ({family, notebook, reds}), whose leases its wrapper
    holds (no claim) and whose session lives on across reds."""
    num, rid = u["run"].get("number"), u["run"].get("databaseId")
    # WHO YOU ARE comes in the prompt, never the environment: a `claude --bg` session inherits the claude daemon's
    # environment -- whichever fixer's wrapper spawned it (2026-10-29: every background fixer was 'red-fixer-2473')
    fx = f" --fixer {u['record']}" if u.get("record") else ""
    first = "" if owner else f"FIRST run `~/bin/agentctl red claim {num}{fx} --note \"<one line: what you are doing>\"`. "
    who = (owner_intro(owner) if owner else
           "agentctl revived you (tier lander, blame.py), a background session the owner sees in `claude agents`. "
           if u.get("lander") else
           "You are an unattended red-build fixer dispatched by agentctl (nobody else was fixing this red), a background "
           "session the owner sees in `claude agents`. ")
    o_r = red_repo_of(u)
    bpath, btext = fixer_brief(o_r)
    if o_r != RED_PRIMARY:  # the movies preamble is movies'; the repo's own CLAUDE.md is the first read here
        brief = f"{o_r}'s CLAUDE.md in your worktree"
    common = (who + (f"YOUR RED IS IN {o_r} (its per-repo rules are in the brief below). " if o_r != RED_PRIMARY else "")
              + f"Read {brief} and ~/bin/AGENTCTL.md first. Your worktree is {wt} (branch off {remote()}/main, already "
              "created): work only there. " + first + "Commits unlanded for "
              f"{FIXER_UNLANDED_NUDGE // 60} min get a RED-NUDGE.md in your worktree (read it, land, delete it), then an "
              f"escalation to the owner.\n\n=== THE FIXER BRIEF ({bpath.replace(HOME, '~')}) ===\n{btext}\n"
              "=== END OF THE FIXER BRIEF ===\n\n")
    replay = red_replay_command(red_repo_clone(o_r), u) if not owner else None
    if replay:  # FIX REPLAY (redreplay.py): its land is refused without the replay's base=FAIL head=PASS
        common += (f"REPLAY BEFORE LANDING: this red's check replays cheaply: `{replay}`. Run it at {remote()}/main "
                   "(base) and at your fix (head) and land only when it says base=FAIL head=PASS, with the trailer "
                   f"`Fix-Replay: {replay} base=FAIL head=PASS` beside Fixes-Red. ")
    if u.get("gate") and not owner:  # FIX REPLAY FOR GATE REDS (redreplay.py; the owner, 2026-11-20): enforced
        common += ("REPLAY YOUR GATE FIX: beside Fixes-Red, add the trailer `Fix-Replay: <command>` -- the narrowest "
                   "command that fails where this red fails (a `testOnly`, a script's self-test) -- and agentctl's "
                   f"land runs it at {remote()}/main and at your fix: the change counts as the fix only on base=FAIL "
                   "head=PASS. A red that genuinely cannot replay (infra, a CI-only race): `Fix-Replay: none — <why>`, "
                   f"a specific reason of {FIX_REPLAY_NONE_MIN}+ characters (no n/a, flaky or can't), which the owner "
                   "reads weekly. land refuses a gate fix with neither. ")
    if len(red_refuted(u)) >= RED_RECURRING:
        common += (f"RECURRING: this failure outlived {len(red_refuted(u))} landed fixes ({', '.join(x[:10] for x in red_refuted(u))}): "
                   "each treated a symptom. Read them (`git show`), find the ROOT CAUSE they all missed, and fix that; "
                   "another symptom fix is not a fix. ")
    if u.get("orphan"):  # an unlanded change whose author ended (orphan_duty)
        common = common.replace(first, "").replace(f"(branch off {remote()}/main, already created)", "(the ended author's own)")
        task = (f"This worktree holds commits that are NOT on {remote()}/main, and the agent that wrote them has ended. Its last "
                f"verdict: {str(u.get('line') or 'none')[:400]!r}. Make it land: `git fetch {remote()} && git rebase "
                f"{remote()}/main` (never squash), fix what the checks reject (read the failing checks' output; never loosen a "
                "test, never skip a check), then `~/bin/agentctl land`. If the change is wrong or "
                "already on main in another form, say so. End at `LANDED <sha>`, or `GAVE UP: <why>`.")
    elif u.get("lander"):  # THE LANDER REVIVED (blame.py): its change reverted, it fixes and re-lands it
        common = common.replace(f"(branch off {remote()}/main, already created)", "(yours)")
        task = lander_prompt(u, wt)
    elif u.get("diagnosis"):  # a diagnose-only fixer found the cause: this one makes the durable fix
        task = (f"Red run {u['workflow']} #{num} (run id {rid}), job `{u['job']}`, was DIAGNOSED by a diagnose-only fixer: "
                f"{u['diagnosis'][:600]!r}. The diagnosis is not the end, and nor is a 'one-off' verdict: make the DURABLE "
                "fix. Remove the cause (code, CI config, the test harness) if it is in the repo. If it really lies outside "
                "the code (a crashed browser, a runner, a transient), land instead the means to diagnose it next time: keep "
                "the failing process's stderr and exit status in the job log or its artifacts (e.g. Chrome's stderr, which "
                "CdpDriver discards), a retry-free guard that names the cause. `~/bin/agentctl ci-why " + str(rid) + "` "
                "explains the run. Never rerun to green, never loosen a test. Commit with a `Fixes-Red: " + str(num) + "` "
                "trailer and land it with `~/bin/agentctl land`. End at `LANDED <sha>`; if you cannot, "
                "`GAVE UP: <why>`; if the fix needs the owner's decision (a design change the red rules do not let you "
                "make, in an owner-only category: the brief's ASK THE OWNER ONLY WHAT NO AGENT CAN DO), `DIAGNOSED: <cause>; decide[<category>]: <the decision>` (the red waits on the owner with it); if it needs the owner, `~/bin/agentctl red block " + str(num) + " --owner-cause <kind> --cmd \"<command>\"` (only for a cause outside the code that only the owner can act on: never a code red) "
                "and end `BLOCKED ON OWNER: <command>`.")
    elif u["workflow"] == LOCAL_FLOW:
        task = (f"The pre-push check `{u['job']}` FAILS ON PLAIN {remote()}/main {str(u['run'].get('headSha'))[:10]} (no "
                f"change needed to see it; first failure line: {u.get('line', '')[:200]!r}). scripts/hooks/pre-push "
                "documents each check; reproduce it in your worktree, root-cause and fix it (never loosen the check), "
                "commit with a `Fixes-Red: local` trailer and land with `~/bin/agentctl land`. End "
                "at `LANDED <sha>`, or `GAVE UP: <why>`.")
    elif u["kind"] in ("infra", "unclear"):  # the group's primary: no test failure in the run at all
        task = (f"Red run {u['workflow']} #{num} (run id {rid}) failed in job `{u['job']}`, and ci-why calls it "
                f"{'INFRA-SHAPED' if u['kind'] == 'infra' else 'UNCLEAR (no FAILED/error line: runner lost? log truncated?)'}. "
                f"DIAGNOSE ONLY: `~/bin/agentctl ci-why {rid}`, the CI runners' health (~/bin/ci-runners-page, the "
                "ci-runner host's systemd units over ssh, read-only), disk, docker, the tunnel. Make NO code change and "
                "do not rerun anything to get green. If it needs a deploy, a restart or any owner action, run "
                f"`~/bin/agentctl red block {num} --owner-cause <kind> --cmd \"<the exact command the owner must run>\"` and end with a line "
                "`BLOCKED ON OWNER: <command>`; otherwise end with `DIAGNOSED: <cause>; durable fix: <what to change, or "
                "what to log so the next occurrence names its cause>` -- an implementer is dispatched with it at once.")
    else:
        task = (f"Fix red run {u['workflow']} #{num} (run id {rid}), failing in "
                + (f"jobs {', '.join(u['jobs'])} (first: `{u['job']}`)" if len(u.get("jobs") or []) > 1 else f"job `{u['job']}`")
                + (f" with: {u['line'][:200]!r}" if u.get("line") else "") +
                f". `~/bin/agentctl ci-why {rid}` explains it. Root-cause it (never rerun to green, never loosen a test "
                "or a budget to pass), commit the fix with a `Fixes-Red: " + str(num) + "` trailer, and land it with "
                "`~/bin/agentctl land`. End at `LANDED <sha>`. If you cannot fix it, end with a line "
                "`GAVE UP: <why>`; if it needs the owner, `~/bin/agentctl red block " + str(num) + " --owner-cause <kind> --cmd \"<command>\"` (only for a cause outside the code that only the owner can act on: never a code red) "
                "and end `BLOCKED ON OWNER: <command>`.")
    if u.get("decision"):  # THE OWNER ANSWERED this red's question (`agentctl decide`): act on that answer
        dec = u["decision"]
        task = (f"THE OWNER DECIDED this red: asked {str(dec.get('question'))[:400]!r}, the owner answered "
                f"{str(dec.get('answer'))[:600]!r}. Act on exactly that answer (it settles what the fixers could not "
                "decide) and land the result. If acting on it fails (the land is NOT LANDED, a check rejects it, it "
                "cannot be done), end `NOT LANDED: <why>` or `GAVE UP: <why>`: the owner is told at once. ") + task
    task += WAITS_ON_BRIEF + LAND_BRIEF
    if red_owner_only(o_r):  # every task above says "land it": not in an owner-only repo
        task += red_owner_rules(o_r, num)
    if u.get("old_note"):
        task += (f" This red's newest run is old and {u['old_note']}: decide whether it still fails (fix it), or why it "
                 "cannot recur (land the change that makes that so, e.g. its workflow fixed or removed), and say which.")
    pw = u.get("prior_work") or {}
    if pw.get("commits"):
        task += (f" A PREVIOUS FIXER of this red left commits not on {remote()}/main in {pw['worktree']} "
                 f"({', '.join(pw['commits'])}): read them first (`git -C {pw['worktree']} log -p {remote()}/main..HEAD`). If "
                 "they are (part of) the fix, cherry-pick them into your worktree (never squash; keep their messages, add "
                 f"the `Fixes-Red: {num}` trailer) and land them; never leave them behind unexamined.")
    if u.get("predicts"):
        task += (f" This red is on branch {u['predicts']}, not main yet: a run of commits that have since landed on main, "
                 "with main's inputs, so main will fail the same way next. Fix it on main before it does.")
    if is_regression(u.get("line")):  # THE ORDER VERDICT (classify.py): the control pass failed in default order too
        task += (" REGRESSION, NOT AN ORDER DEPENDENCE: its control pass ran the failing tests again in DEFAULT order at "
                 "the same commit and they failed there too, so the order they ran in is not the cause and no ordering "
                 "fix is one. Go after the COMMIT THAT BROKE THEM: the range from this workflow's newest green run on "
                 f"{remote()}/main to this run's head {str(u['run'].get('headSha') or '')[:10]} (`git log --oneline "
                 "<green head>..<red head> -- <the tests' and their code's paths>`); reproduce the failure with a plain "
                 "`testOnly` of the named tests, then fix that change forward (or revert it) -- and say which commit it "
                 "was in the commit message.")
    elif str(u.get("line") or "").startswith("ORDER DEPENDENCE (or a flake):"):
        task += (" ORDER DEPENDENCE (or a flake): its control pass ran the failing tests in DEFAULT order at the same "
                 "commit and they PASSED: find the state one test leaves for another (a singleton, a file, the clock, a "
                 "cache) with the replay the line names, and make each test set up its own.")
    if u.get("flake"):
        task += (" This red PASSED on the next run: it is a FLAKE. Make it deterministic (find the race, the clock, "
                 "the ordering); do not just retry it.")
    if u.get("blame_note"):  # the red's blame (blame.py): the offending change, its revert, what to do with it
        task += u["blame_note"]
    return common + task + (f" {resume_note}" if resume_note else "")


FIXER_IDENTITY_ENV = ("AGENTCTL_AGENT_NAME", "AGENTCTL_RED_FIXER", "AGENTCTL_FIXER_RECORD", "AGENTCTL_CLAIM_PID",
                      "AGENTCTL_LEASE_SECRET", "AGENTCTL_LEASE_SECRETS")


def fixer_env(o_r, num, wrapper_pid=None):
    """A clean environment for a dispatched fixer: no parent Claude session, no secret but the credential agentctl
    itself reads, never PREPUSH_SKIP."""
    env = {k: v for k, v in os.environ.items()
           if not k.startswith(("CLAUDE_CODE_", "CLAUDECODE")) and k not in ("CLAUDE_PID", "PREPUSH_SKIP", "AI_AGENT")
           and not SECRET_ENV.search(k)}
    try:
        tok = token(o_r)
        env[token_env_name(o_r)] = tok
    except (Exception, SystemExit):
        pass
    # EVERY FIXER TALKS TO THE STORE (`red claim | beat | release`, `land`) with the STORE TOKEN, whatever its repo: the
    # filter above drops it with every other secret, and a non-movies repo's credential is gh's own (none here)
    if "AGENTCTL_STORE_TOKEN" not in env:
        try:
            with no_vault():
                env["AGENTCTL_STORE_TOKEN"] = token(SHORT["movies"])
        except (Exception, SystemExit):
            pass
    env.update(AGENTCTL_AGENT_NAME=f"red-fixer-{num}", AGENTCTL_RED_FIXER=str(num))
    # launchd gives the processor neither USER nor LOGNAME, and without them `claude` finds no login in the keychain
    # ("Not logged in", 2026-10-27); nor ~/.local/bin on PATH
    import pwd
    me = pwd.getpwuid(os.getuid()).pw_name
    env.setdefault("USER", me)
    env.setdefault("LOGNAME", me)
    path = env.get("PATH", "/usr/bin:/bin")
    env["PATH"] = ":".join(dict.fromkeys([os.path.join(HOME, ".local", "bin"), "/opt/homebrew/bin"] + path.split(":")))
    # SIGNING THROUGH THE SECRETS AGENT (issue 20261116T224456-3b788f): launchd hands its jobs its own SSH_AUTH_SOCK, an
    # empty agent, while ~/.zshrc points commit.gpgsign (gpg.format ssh) at ~/.ssh/secrets-agent.sock; a fixer's or a
    # revert's commit then failed "No private key found". Where that socket is, every spawned session signs through it.
    # The socket is a symlink to the secrets daemon's own (issue 20261119T155416-99cfd9, core red 1071: the daemon's end
    # was not there when the fixer started, so os.path.exists said no and the session kept launchd's empty agent for
    # good): the configured route counts, dangling or not -- once the daemon is up the same session signs.
    agent = os.path.join(HOME, ".ssh", "secrets-agent.sock")
    if os.path.lexists(agent):
        env["SSH_AUTH_SOCK"] = agent
    # NO SIGNATURE WHERE MAIN KEEPS NONE (2026-11-22: #348/#349's revert and Order independence #105's fix, tested, sat
    # BLOCKED ON OWNER on the vault for 91 and 43 min, each owner turn a new `claude` process asking its own Touch ID).
    # movies' land rebases every change with NO_SIGN, so the signature those waits were for never reached main: a
    # fixer of a repo whose main is unsigned (red-repos.json `signed: false`) commits unsigned, git's own env config.
    if not repo_signs(o_r):
        n = int(env.get("GIT_CONFIG_COUNT") or 0)
        env.update({"GIT_CONFIG_COUNT": str(n + 2), f"GIT_CONFIG_KEY_{n}": "commit.gpgsign",
                    f"GIT_CONFIG_VALUE_{n}": "false", f"GIT_CONFIG_KEY_{n + 1}": "tag.gpgsign",
                    f"GIT_CONFIG_VALUE_{n + 1}": "false"})
    if wrapper_pid:
        env["AGENTCTL_CLAIM_PID"] = str(wrapper_pid)
    return env


WAITS_ON_BRIEF = (" If this red's fix is ANOTHER red's commit that is not on main yet, land nothing and end "
                  "`WAITS ON <sha> (red <its key>): <why>`: agentctl waits for that commit (or its rebased copy) to reach "
                  "main and verifies this red against the next run containing it -- never a NOT LANDED for such a hold.")
# HOW A FIXER LANDS (2026-11-16, Main #2515: the fixer wrapped `agentctl land` in `timeout -s KILL` twice, then ran
# scripts/hooks/pre-push itself; neither verdict came within its 180-min turn).
LAND_BRIEF = (" `~/bin/agentctl land` QUEUES your fix like every change (red fixes go first, checked on the CI tier), runs "
              "the 45-s fast precheck in the foreground -- a trailer or rule it refuses is your NOT LANDED at once: fix "
              "it and land again -- then waits for the queue's verdict itself, bounded, and prints LANDED <sha> or NOT "
              "LANDED: <why>. Run it with run_in_background=true and act on its last line. Never wrap it in `timeout` "
              "(a kill leaves your change queued without you), and never run scripts/hooks/pre-push yourself: the "
              "queue's checks are the landing checks.")
FIXER_VERDICT = re.compile(r"^\W*(LANDED [0-9a-f]{7,40}|WAITS ON [0-9a-f]{7,40}.*|NOT LANDED\b.*|GAVE UP:.*|"
                           r"BLOCKED ON OWNER:.*|DIAGNOSED:.*)", re.M)


def fixer_verdict(text):
    """The fixer's verdict: the last verdict line of its final answer (stream-json's `result`), else of its output."""
    final = None
    for line in text.splitlines():
        if line.startswith("{"):
            try:
                o = json.loads(line)
            except ValueError:
                continue
            if o.get("type") == "result":
                final = o.get("result") or ""
    for t in ([final] if final else []) + [text]:
        found = FIXER_VERDICT.findall(t)
        if found:
            return found[-1].strip()
    return None


# THE agentctl REPOSITORY (~/bin): a fixer never edits it (`agentctl issue`); watchdog WB checks its checkout stays clean.
# $AGENTCTL_BIN_REPO ('-': none, the tests').
def bin_repo():
    r = os.environ.get("AGENTCTL_BIN_REPO") or os.path.dirname(TOOL_PATH)
    return None if r == "-" or not os.path.isdir(os.path.join(r, ".git")) else r


def red_dispatch(o_r, repo, u, why="by hand", resume=None, effort=None, lease=None, took=None, extra=(), store=None):
    """Start a headless fixer for red u under its taken lease: a fresh worktree <clone>-wt-red-<run> off origin/main
    (resume: the same worktree and session), then a detached `agentctl red fixer` that runs `claude -p` there, renews
    the lease by its progress, and releases it with its verdict. (ok, message)."""
    standby = None if on_ci_runner() else laptop_role_standby(store)
    if standby:  # ONE MACHINE HOLDS THE LAPTOP ROLE (server.py): a standby machine starts no fixer
        return False, f"NOT DISPATCHED: {standby}"
    num = u["run"].get("number")
    o_r = u.get("repo") or o_r
    d = reds_dir(red_repo_tag(o_r, num))
    os.makedirs(d, exist_ok=True)
    k = 1 + len(glob.glob(os.path.join(d, "fixer*.json")))
    rec_path = os.path.join(d, "fixer.json" if k == 1 else f"fixer-{k}.json")
    cont = None if resume else previous_fixer_tree(u)
    if resume and resume.get("worktree") and os.path.isdir(resume["worktree"]):
        wt = resume["worktree"]
    elif cont:  # FIXER CONTINUITY: the previous fixer's worktree and its unlanded commits, never a fresh start
        wt = cont["worktree"]
        resume = {"note": cont["note"]}
    else:
        _d, clone, _w = waiting_state(repo)
        wt, branch, n = f"{clone}-wt-red-{num}", f"red-{num}", 1
        while os.path.exists(wt) or git_ok(repo, "rev-parse", "--verify", "-q", f"refs/heads/{branch}"):
            n += 1
            wt, branch = f"{clone}-wt-red-{num}-{n}", f"red-{num}-{n}"
        git(repo, "worktree", "prune", check=False)
        git(repo, "fetch", "--quiet", remote(), "main", check=False)
        r = git(repo, "worktree", "add", "-q", "-b", branch, wt, f"{remote()}/main", check=False)
        if r.returncode != 0:
            return False, dispatch_log(u, f"NOT DISPATCHED: git worktree add failed ({r.stderr.strip()[:160]})")
    rec = {"key": u.get("key"), "lease": lease, "epoch": (took or {}).get("epoch"),
           "jobs": u.get("jobs") or [u.get("job")],
           "run": num, "run_id": u["run"].get("databaseId"), "workflow": u["workflow"], "job": u.get("job"),
           "kind": u.get("kind"), "line": u.get("line"), "flake": bool(u.get("flake")), "worktree": wt, "started": ts(),
           "why": why, "log": os.path.join(d, f"claude-{k}.log"), "session": None,
           # A RESUME IS A NEW SESSION (issues 20261119T025305-21375d, 20261103T232907-ca63b2): `claude --bg --resume
           # <old>` reports its own id, so the record names the session it resumes apart; `session` is the new one's,
           # written by the wrapper's first poll or bound by the fixer's own first `red claim --fixer`
           "resumed_from": resume.get("session") if resume else None,
           "resume": bool(resume and resume.get("session")), "resume_note": resume.get("note") if resume else None,
           "continued_from": (cont or {}).get("worktree"), "diagnosis": u.get("diagnosis"),
           "effort": effort, "refuted": u.get("refuted") or [], "predicts": u.get("predicts"), "orphan": u.get("orphan"),
           "extra_leases": [{"name": x["name"], "epoch": x["epoch"]} for x in extra], "prior_work": u.get("prior_work"),
           "blame_note": u.get("blame_note"), "lander": u.get("lander"), "decision": u.get("decision"),
           "launch_cwd": (resume or {}).get("launch_cwd")}
    if red_is_other(o_r):
        rec["repo"] = o_r
    write_json(rec_path, rec)
    env = fixer_env(o_r, num)
    if took:
        env["AGENTCTL_LEASE_SECRET"] = took["secret"]
    if extra:
        env["AGENTCTL_LEASE_SECRETS"] = json.dumps({x["name"]: x["secret"] for x in extra})
    with open(os.path.join(d, "fixer.log"), "a") as logf:
        p = spawn_detached([sys.executable, TOOL_PATH, "red", "fixer", "--repo", repo, "--record", rec_path],
                           stdin=subprocess.DEVNULL, stdout=logf, stderr=logf, start_new_session=True, cwd=wt, env=env)
    rec.update(pid=p.pid, process_start=process_start(p.pid))
    write_json(rec_path, rec)
    if store is not None and lease and took and "epoch" in took:
        # its wrapper, named in the lease from the start: one that dies before its first word is judged at once
        # (red_reap_orphans), its stderr (fixer.log) the why -- never a silent 15-min lapse
        try:
            store.set_meta(lease, took["epoch"], took["secret"],
                           {"pid": p.pid, "host": socket_host(), "worktree": wt,
                            "wrapper_log": os.path.join(d, "fixer.log")})
        except Exception as e:  # the wrapper sends its own meta at its first poll
            err(f"red dispatch: the lease's meta was not written ({str(e)[:160]})")
    line = dispatch_log(u, f"DISPATCHED{' (resumed)' if rec['resume'] else ' (continuing)' if cont else ''} pid {p.pid} "
                           f"in {wt} ({why}); log {rec['log']}")
    inbox_write("red", f"RED DISPATCHED: {u['workflow']} #{num} [{u.get('job')}] -> fixer session "
                       f"'{fixer_label({**rec, 'run': num})}' (`claude agents`) in {wt.replace(HOME, '~')} ({why})", [], [], 0, cwd=wt)
    return True, line


def unlanded_commits(wt):
    """[(sha, author time)] of the worktree's commits not on origin/main (cherry_landed: a rebased copy on main counts as
    landed), oldest first; [] when it is not a worktree."""
    if not wt or not os.path.isdir(wt):
        return []
    shas = cherry_landed(wt, f"refs/remotes/{remote()}/main", "HEAD")[1] or []
    out = []
    for sha in shas:
        r = git(wt, "log", "-1", "--format=%at", sha, check=False)
        out.append((sha, int(r.stdout.strip() or 0) if r.returncode == 0 and r.stdout.strip().isdigit() else 0))
    return out


def fixers_of(key, run, fam=None):
    """The fixer records of a red: by its key (any run of it), by its run, or by its family (red-families.json)."""
    return [r for r in fixer_records() if key in fixer_keys(r) or (r.get("workflow"), str(r.get("run"))) == run
            or (fam and red_family(r) == fam)]


def previous_fixer_tree(u):
    """FIXER CONTINUITY (the owner, 2026-10-29: fixer -8 of #2473's red started fresh in movies-wt-red-2517 while -7's
    five commits sat unlanded in movies-wt-red-2473-5): a new fixer of the same red -- its key, any run of it -- goes on
    in the previous fixer's worktree when that holds commits not on origin/main (the most recently committed one),
    and is told to cherry-pick any other's. {worktree, note} or None. Read on the laptop at dispatch: the worktrees are
    here, never on ci-runner."""
    run = (u.get("workflow"), str((u.get("run") or {}).get("number")))
    trees = [r.get("worktree") for r in sorted(fixers_of(u.get("key"), run, red_family(u)),
                                                key=lambda r: str(r.get("started") or ""), reverse=True)]
    trees += [h.get("worktree") for h in u.get("history") or []] + [(u.get("prior_work") or {}).get("worktree")]
    found = {}
    for wt in trees:
        if wt and wt not in found and os.path.isdir(wt) and allowed_path(wt) and not os.path.exists(os.path.join(wt, "RED-STOP.md")):
            left = unlanded_commits(wt)
            if left:
                found[wt] = left
    if not found:
        return None
    wt = max(found, key=lambda w: max(at for _s, at in found[w]))
    others = {w: c for w, c in found.items() if w != wt}
    num = (u.get("run") or {}).get("number")
    note = (f"CONTINUE, NEVER START OVER: {wt} is the previous fixer's worktree of this red, with {len(found[wt])} "
            f"commit(s) not on {remote()}/main ({', '.join(s[:10] for s, _ in found[wt][:10])}). Read them first (`git log -p "
            f"{remote()}/main..HEAD`), `git fetch {remote()} && git rebase {remote()}/main` (never squash), carry the fix on from "
            f"there (add `Fixes-Red: {num}` to what you commit) and land each verified step with `~/bin/agentctl land`.")
    if others:
        note += " Other earlier fixers of it left unlanded commits too: " + "; ".join(
            f"{w} ({', '.join(s[:10] for s, _ in c[:10])})" for w, c in others.items()) + \
            " -- cherry-pick what belongs to the fix (never squash), say why you leave the rest."
    return {"worktree": wt, "note": note}


# LAND AS YOU GO (the owner, 2026-10-29: fixer -7 of #2473 sat on five unlanded commits for hours, and the import that
# took its lease away left them stranded). The laptop watches every working fixer's worktree: commits not on
# origin/main for FIXER_UNLANDED_NUDGE (by author time: a rebase keeps it) get a RED-NUDGE.md there, which its prompt
# tells it to act on; still unlanded at FIXER_UNLANDED_ESCALATE, one RED ESCALATE inbox entry. Both clear themselves
# once the commits are on main (RECOVERED after an escalation).
FIXER_UNLANDED_NUDGE = 45 * 60
FIXER_UNLANDED_ESCALATE = 90 * 60
FIXER_LAND_WATCH_EVERY = 60
_last_land_watch = [0.0]


def fixers_land_watch(now_=None, force=False):
    t = now_ if now_ is not None else time.time()
    if not force and t - _last_land_watch[0] < env_num("AGENTCTL_FIXER_LAND_WATCH", FIXER_LAND_WATCH_EVERY):
        return []
    _last_land_watch[0] = t
    out = []
    for r in fixer_records():
        wt = r.get("worktree")
        if not r.get("alive") or not wt or not os.path.isdir(wt):
            continue
        if red_owner_only(red_repo_of(r)):  # its commits are never landed by it: filed with `red owner-push`
            continue
        sp, nudge = r["path"] + ".land.json", os.path.join(wt, "RED-NUDGE.md")
        st = read_record(sp) or {}
        left = unlanded_commits(wt)
        label = fixer_label(r)
        if not left:
            if st:
                for x in (sp, nudge):
                    try:
                        os.unlink(x)
                    except OSError:
                        pass
                if st.get("escalated_at"):
                    inbox_write("red fixer", f"RECOVERED: {label}: its commits are on main ({wt.replace(HOME, '~')})",
                                [], [], 0, cwd=wt)
                out.append(f"{label}: everything landed")
            continue
        oldest = min((at for _s, at in left if at), default=t)
        mins = int((t - oldest) // 60)
        shas = ", ".join(s_[:10] for s_, _ in left[:8])
        if t - oldest >= env_num("AGENTCTL_FIXER_UNLANDED_NUDGE", FIXER_UNLANDED_NUDGE) and not st.get("nudged_at"):
            with open(nudge, "w") as f:
                f.write(f"LAND NOW (agentctl, {ts()}): {len(left)} commit(s) here are not on {remote()}/main, the oldest "
                        f"{mins} min old ({shas}). Land each verified step with `~/bin/agentctl land` (Fixes-Red lands on "
                        f"red); if one is not ready, say why in `agentctl red claim {r.get('run')} --note`. Delete this "
                        "file once they are queued.\n")
            st["nudged_at"] = t
            out.append(f"{label}: nudged ({len(left)} commit(s) unlanded for {mins} min)")
        elif st.get("nudged_at") and not st.get("escalated_at") and \
                t - oldest >= env_num("AGENTCTL_FIXER_UNLANDED_ESCALATE", FIXER_UNLANDED_ESCALATE):
            line = (f"RED ESCALATE: {label}: {len(left)} commit(s) unlanded for {mins} min in {wt.replace(HOME, '~')} "
                    f"({shas}), nudged at {time.strftime('%H:%M', time.localtime(st['nudged_at']))}: land them or stop it")
            inbox_write("red fixer", line, [line], [], 1, cwd=wt)
            st["escalated_at"] = t
            out.append(line)
        write_json(sp, st)
    return out


def dispatch_log(u, what):
    """One line to reds/dispatch.log; returns it."""
    line = f"{ts()} {u['workflow']} #{u['run'].get('number')} [{u.get('job')}] red {u.get('key')}: {what}"
    os.makedirs(reds_dir(), exist_ok=True)
    with open(reds_dir("dispatch.log"), "a") as f:
        f.write(line + "\n")
    return line


def fixer_mark(rec, wt):
    """A dispatched fixer's (or an owner's) progress mark: its transcript's bytes, its worktree's HEAD and `git status`,
    and the session's other real activity (session_activity: its sub-agents' transcripts, its commands' output, its job
    directory's files)."""
    import hashlib
    size = os.path.getsize(rec["log"]) if os.path.exists(rec["log"]) else 0
    head = git(wt, "rev-parse", "HEAD", check=False).stdout.strip()[:12] if os.path.isdir(wt) else ""
    st = git(wt, "status", "--porcelain", check=False).stdout if os.path.isdir(wt) else ""
    return f"{size}:{head}:{hashlib.sha1(st.encode()).hexdigest()[:10]}" + session_activity(rec["log"], rec.get("bg_id"))


# REAL ACTIVITY IS PROGRESS (2026-11-03, convergence red 8fbdbe0d5a86: ~8 h through several owners, "lease expired" every
# 15-30 min and "STALLED: no progress (transcript, worktree) for 30 min"). The mark read the session's own transcript
# and the worktree only, and a session at work is often silent in both: a sub-agent it delegated to writes ITS
# transcript (<session>/subagents/*.jsonl), a long command writes its output (claude's tmp <project>/<session>/tasks/),
# a bisect or a measure collector writes under $CLAUDE_JOB_DIR (~/.claude/jobs/<id>/tmp) -- 09:20 to 09:51 that day
# the owner was waiting on its own CI measure, 22:28 on, a local bisect wrote bisect.log every few seconds while the
# owner's red lease counted down to "lapses without progress". Each of them now moves the mark.
SESSION_ACTIVITY_FILES = 2000  # files looked at per session at most (a job dir full of logs stays cheap)


def claude_tmp_root():
    """claude's per-user tmp root (<root>/<project dir>/<session>/tasks/<task>.output), $AGENTCTL_CLAUDE_TMP."""
    env = os.environ.get("AGENTCTL_CLAUDE_TMP")
    if env:
        return env
    for d in (f"/private/tmp/claude-{os.getuid()}", f"/tmp/claude-{os.getuid()}"):
        if os.path.isdir(d):
            return d
    return f"/tmp/claude-{os.getuid()}"


def claude_jobs_root():
    return os.environ.get("AGENTCTL_CLAUDE_JOBS") or os.path.join(HOME, ".claude", "jobs")


def session_activity_dirs(transcript, bid=None):
    """The directories a session writes to besides its transcript (see REAL ACTIVITY IS PROGRESS)."""
    if not transcript or not str(transcript).endswith(".jsonl"):
        return []
    base = str(transcript)[:-len(".jsonl")]
    session, project = os.path.basename(base), os.path.basename(os.path.dirname(base))
    out = [os.path.join(base, "subagents"), os.path.join(claude_tmp_root(), project, session, "tasks")]
    jid = str(bid or session[:8])
    if re.fullmatch(r"[0-9a-f]{6,}", jid):
        out.append(os.path.join(claude_jobs_root(), jid, "tmp"))
    return out


def session_activity(transcript, bid=None):
    """':act <bytes>@<newest mtime>' over session_activity_dirs, '' when there is none: a mark part that moves while
    the session's sub-agents, commands or job files grow."""
    total, newest, n = 0, 0.0, 0
    for d in session_activity_dirs(transcript, bid):
        for root, _dirs, files in os.walk(d):
            for fn in files:
                n += 1
                if n > SESSION_ACTIVITY_FILES:
                    break
                try:
                    s = os.stat(os.path.join(root, fn))
                except OSError:
                    continue
                total += s.st_size
                newest = max(newest, s.st_mtime)
    return f":act {total}@{int(newest)}" if newest else ""


def fixer_meta_sent(st, leases, meta):
    """Merge a fixer's meta (worktree, session, pid, log: a resume finds its session by it) into its leases, the store
    keeping only its holder's: True, or False when the store did not answer and the wrapper must send it again."""
    try:
        for n_, e_, s_ in leases:
            st.set_meta(n_, e_, s_, meta)
        return True
    except StoreUnavailable as e:
        print(f"{ts()} the store did not answer ({e}): the leases' meta is sent again", flush=True)
        return False


# A FIXER IS A BACKGROUND CLAUDE SESSION (the owner, 2026-10-29: "I want fixers to be visible in ui"): `claude --bg -n
# <label>` starts it, so `claude agents` lists it by its label and the owner can `claude attach | logs | stop` it. The
# wrapper tracks it by the id --bg prints, never a child pid: `claude agents --json --all` says working / done, its
# transcript (~/.claude/projects/*/<session>.jsonl) is its progress and holds its final answer, `claude stop` ends it.
BG_POLL = 5  # seconds between the wrapper's looks at its session ($AGENTCTL_BG_POLL)
BG_STARTED = re.compile(r"backgrounded\W+([0-9a-f]{6,})")
FIXER_CLAIM_WITHIN = 600  # s a started session has to start its transcript / run `red claim` ($AGENTCTL_FIXER_CLAIM_WITHIN)


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
def claude_config_path():
    d = os.environ.get("CLAUDE_CONFIG_DIR")
    return os.path.join(d, ".claude.json") if d else os.path.join(HOME, ".claude.json")


def claude_trust_gap(path):
    """None when claude trusts `path` (a checkout or worktree) for a background session -- or its config cannot be
    read (no verdict: the launch decides) -- else why not, with the owner's one-liner that trusts it."""
    try:
        with open(claude_config_path()) as f:
            projects = (json.load(f) or {}).get("projects")
    except (OSError, ValueError, AttributeError):
        return None
    if not isinstance(projects, dict) or not path or not os.path.isdir(path):
        return None
    p = os.path.realpath(path)
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
    if any(isinstance(projects.get(c), dict) and projects[c].get("hasTrustDialogAccepted") is True for c in cands):
        return None
    return (f"claude does not trust {main.replace(HOME, '~')} (`claude --bg` refuses an untrusted workspace; a "
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


def trust_ask_line(path, gap):
    return (f"RED BLOCKED ON OWNER [tty-trust]: WAITING ON YOU: {gap.split(' (', 1)[0]} -- every red of that repo waits "
            f"on it. Answer: `! cd {shlex.quote(path)} && claude` (accept the trust prompt, then /exit); this clears by "
            "itself once it is trusted")


def trust_asks():
    """The sync's pass: each watched repo's clone checked for claude's workspace trust; an untrusted one asked once."""
    path = state_root("trust-asks.json")
    asked = read_record(path) or {}
    out, now_asked = [], {}
    for c in red_repos():
        clone = red_repo_clone(c["repo"])
        gap = claude_trust_gap(clone) if clone else None
        if not gap:
            continue
        now_asked[clone] = asked.get(clone) or time.time()
        if clone not in asked:
            inbox_write("red", trust_ask_line(clone, gap), [], [], 1, item=TRUST_ASK_PREFIX + os.path.realpath(clone))
            out.append(f"asked the owner to trust {clone.replace(HOME, '~')} ({c['repo']})")
    if now_asked != asked:
        write_json(path, now_asked)
    return out


def bg_start(argv, cwd, env):
    """(id, None) once `claude --bg` backgrounded the session, else (None, why)."""
    try:
        r = subprocess.run(argv, cwd=cwd, env=env, stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=120)
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
def claude_cwd():
    return HOME if os.path.isdir(HOME) else "/"


def bg_rows(cli):
    """The rows of `claude agents --json --all`, or None when they cannot be read."""
    try:
        r = subprocess.run([cli, "agents", "--json", "--all"], capture_output=True, text=True, timeout=60,
                           stdin=subprocess.DEVNULL, cwd=claude_cwd())
        rows = json.loads(r.stdout) if r.returncode == 0 else None
    except (OSError, subprocess.TimeoutExpired, ValueError):
        rows = None
    return rows if isinstance(rows, list) else None


def bg_session(cli, bid):
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
RED_STALL = 30 * 60   # a turn with no progress (transcript, worktree) this long is stalled: stopped ($AGENTCTL_RED_STALL)


def bg_ended(ent):
    """The session's row says its turn is over (ent: bg_session's row; {} -- unread -- and None -- not listed -- say
    nothing here)."""
    if not ent:
        return False
    if ent.get("state") in BG_ENDED_STATES:
        return True
    pid = ent.get("pid")
    return ent.get("state") in BG_WAITING_STATES and not (pid and pid_alive(int(pid)))


# A FINISHED TURN WHOSE PROCESS LIVES ON (2026-11-01, convergence #579 / Verify #1 `fixer-crash-loop`): claude now keeps
# a background session's process (`claude bg-spare`) after its turn, its row `blocked` / `idle` with a live pid. bg_ended
# read that as "maybe waiting on its own background work" and the wrapper waited for progress that never came: the
# owner's turns ended with GAVE UP / DIAGNOSED, the wrapper never read it, each red's lease lapsed OWNER_TTL later as
# "lease expired", and three such lapses blocked the red on the owner as a crash loop. The transcript decides now
# (bg_turn): a waiting session whose turn's final answer carries a verdict has ended its turn; one with no background
# task of its own pending, quiet for BG_IDLE_GRACE, has ended it with no verdict; one waiting on its own background task
# (`agentctl wait`, backgrounded before it ended its turn, as agents are told) is at work, and each beat of that wait is
# progress (bg_wait_mark), so its lease never lapses under it.
BG_IDLE_GRACE = 60   # s a waiting session with no pending task of its own and no verdict stays quiet: its turn ended
BG_TASK_STARTED = re.compile(r'"backgroundTaskId": ?"([\w-]+)"|Command running in background with ID: ([\w-]+)')
BG_TASK_REPORTED = re.compile(r"<task-id>([\w-]+)</task-id>")  # a notification: a user entry or one absorbed mid-turn
BG_TASK_STOPPED = re.compile(r'"(?:task_id|shell_id)": ?"([\w-]+)"')  # TaskStop / KillShell: stopped by the session
_BG_TURN_CACHE = {}


def bg_turn(path):
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
    final, started, reported, closed = "", [], set(), False
    try:
        with open(path, errors="replace") as f:
            for line in f:
                try:
                    e = json.loads(line)
                except ValueError:
                    continue
                m = e.get("message") if isinstance(e, dict) else None
                c = m.get("content") if isinstance(m, dict) else None
                if isinstance(e, dict) and e.get("type") == "user":
                    blocks = [{"type": "text", "text": c}] if isinstance(c, str) else c if isinstance(c, list) else []
                    for b in blocks:
                        if isinstance(b, dict) and b.get("type") == "text":
                            final, closed = "", False  # new input: the turn's answer is what comes after it
                            if "<task-notification>" not in str(b.get("text")):  # a prompt: a new turn (a resume
                                started = []                                      # is a new process: no older task)
                elif isinstance(e, dict) and e.get("type") == "assistant" and isinstance(c, list):
                    t = "\n".join(b.get("text") or "" for b in c if isinstance(b, dict) and b.get("type") == "text")
                    if t.strip():
                        final, closed = t, False
                elif isinstance(e, dict) and e.get("type") == "system" and e.get("subtype") == "turn_duration":
                    closed = bool(final)
                started += [x or y for x, y in BG_TASK_STARTED.findall(line)]
                reported.update(BG_TASK_REPORTED.findall(line) + BG_TASK_STOPPED.findall(line))
    except OSError:
        return empty
    out = {"final": final, "pending": [x for x in dict.fromkeys(started) if x not in reported], "closed": closed}
    _BG_TURN_CACHE[path] = (sig, out)
    return out


def bg_waiting(ent):
    """The row is a turn over with its process alive (claude's spare keeps it): what it waits for is the transcript's."""
    pid = (ent or {}).get("pid")
    return bool(ent) and (ent.get("state") or ent.get("status")) in BG_WAITING_STATES and bool(pid) and pid_alive(int(pid))


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
    if not ent:
        return False
    turn = bg_turn(path)
    if not (bg_waiting(ent) or turn["closed"]):
        return False
    if fixer_verdict(turn["final"]):
        return True
    if turn["pending"]:
        return False
    quiet = (now or time.time()) - mtime(path)
    return quiet >= env_num("AGENTCTL_BG_IDLE_GRACE", BG_IDLE_GRACE)


def bg_wait_mark(ent, path, now, every):
    """'' -- or, for a session with its process alive and its own background tasks pending (waiting on them, or
    working beside them), a mark that changes every renewal period: waiting on its own CI run is work, and its lease is
    renewed by it (bounded by the wall time). 2026-11-03: only a `blocked` / `idle` row counted, and an owner whose row
    read `working` while its own measure ran in the background stalled at 30 min."""
    pid = (ent or {}).get("pid")
    if not ent or bg_ended(ent) or not (pid and pid_alive(int(pid))):
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


def bg_stop(cli, bid):
    try:
        subprocess.run([cli, "stop", bid], capture_output=True, timeout=60, stdin=subprocess.DEVNULL, cwd=claude_cwd())
    except (OSError, subprocess.TimeoutExpired):
        pass


def bg_transcript(session):
    root = os.environ.get("AGENTCTL_CLAUDE_PROJECTS") or os.path.join(HOME, ".claude", "projects")
    hits = glob.glob(os.path.join(root, "*", f"{session}.jsonl"))
    return hits[0] if hits else None


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
    except OSError:
        pass
    return False


def bg_failure(bid):
    """Why claude says a background session failed (its job's `detail`, ~/.claude/jobs/<id>/state.json: e.g. `source
    session ... not found`), '' when it did not fail or says nothing: a crash's verdict carries the actual error."""
    try:
        with open(os.path.join(claude_jobs_root(), str(bid or "")[:8], "state.json")) as f:
            job = json.load(f)
    except (OSError, ValueError):
        return ""
    if not isinstance(job, dict) or job.get("state") not in ("failed", "error"):
        return ""
    return " ".join(str(job.get("detail") or "").split())[:300]


def bg_final_text(path):
    """The session's last assistant answer (its text blocks), '' when there is none."""
    last = ""
    try:
        with open(path, errors="replace") as f:
            for line in f:
                try:
                    e = json.loads(line)
                except ValueError:
                    continue
                m = e.get("message") if e.get("type") == "assistant" else None
                if isinstance(m, dict) and isinstance(m.get("content"), list):
                    t = "\n".join(b.get("text") or "" for b in m["content"] if b.get("type") == "text")
                    if t.strip():
                        last = t
    except (OSError, TypeError):
        pass
    return last


# ONE FIXER LIFECYCLE (2026-11-04): a dispatched fixer's wrapper (red_fixer) and a family owner's (owner.red_owner) keep
# their leases and judge their session's progress with the SAME code below, so the two can never drift again. On
# 2026-11-03 the owners learnt to renew by activity and to take a merely lapsed heartbeat again (aaf0082..057ba94); the
# red fixers did not, and on 2026-11-04 13 of them ended "STOPPED: the red's lease was lost" -- each a RED FIXER failure
# in the owner's inbox and a fresh dispatch. Most were no failure at all: their red had CLEARED (the reconciler revokes
# the lease with `red cleared`) while they worked. A lost lease is now read for what it is (lease_loss):
#   - LAPSED (its clock ran out -- the wrapper or its machine silent, the store out of reach -- and nobody took,
#     revoked or released it): the living wrapper takes it again (lease_retake) and goes on;
#   - DONE (revoked because the red needs no fixer any more: cleared, verified, retired, a fix submitted): the work is
#     over, the wrapper ends quietly -- no failure, no attempt;
#   - LOST (re-taken by another, released, revoked otherwise, past its max hold): the wrapper stops, as before.
LEASE_DONE = re.compile(r"(red (cleared|verified|retired)|fix submitted)\b")
LEASE_RETAKE_DROP = ("outcome", "notes", "released_at", "revoked", "paused")


def lease_loss(row, epoch, holder, t):
    """('lapsed', None), ('done', outcome) or ('lost', why) for a lease held at `epoch` by `holder` that reads lost."""
    m = lease_meta(row)
    if not row or row.get("epoch") != epoch:
        return "lost", "taken by another holder"
    out = str(m.get("outcome") or "")
    if m.get("revoked") and LEASE_DONE.match(out):
        return "done", out
    if out or m.get("revoked") or (row.get("holder") and row["holder"] != holder):
        return "lost", out or "taken by another holder"
    if t >= float(row.get("max_until") or 0):
        return "lost", "past its max hold"
    return "lapsed", None


def lease_retake(st, name, epoch, holder, ttl, progress):
    """A LAPSED lease taken again by its living holder: the same item, meta and max hold. {'epoch', 'secret'}, or None
    when it is no longer to be had (somebody took, revoked or released it meanwhile)."""
    row = st.get(name)
    now = st.clock()
    if lease_loss(row, epoch, holder, now)[0] != "lapsed":
        return None
    meta = {k: v for k, v in lease_meta(row).items() if k not in LEASE_RETAKE_DROP}
    got = st.acquire(name, holder, ttl, max(60.0, float(row["max_until"]) - now), progress=progress,
                     item=row.get("item"), meta=meta)
    return None if "held" in got else got


class HeldLease:
    """One lease a wrapper holds (ONE FIXER LIFECYCLE): renew() with each beat, check() at a poll. Each answers None
    while it is held -- a lapsed one taken again on the way (`retaken` counts them) -- else (kind, why): 'done' or
    'lost' (lease_loss). StoreUnavailable / StoreAuthFailed reach the caller (asked again at the next poll)."""

    def __init__(self, name, epoch, secret, holder, ttl):
        self.name, self.epoch, self.secret, self.holder, self.ttl, self.retaken = name, epoch, secret, holder, ttl, 0

    def triple(self):
        return self.name, self.epoch, self.secret

    def check(self, st, row=None, progress="retaken"):
        row = st.get(self.name) if row is None else row
        now = st.clock()
        if self.holder is None and row and row.get("epoch") == self.epoch:  # its holder learnt from its own epoch
            self.holder = row.get("holder") or None
        if row and row.get("epoch") == self.epoch and row.get("holder") == self.holder and lease_is_live(row, now):
            return None
        kind, why = lease_loss(row, self.epoch, self.holder, now)
        if kind == "lapsed":
            got = lease_retake(st, self.name, self.epoch, self.holder, self.ttl, progress)
            if got:
                self.epoch, self.secret, self.retaken = got["epoch"], got["secret"], self.retaken + 1
                print(f"{ts()} {self.name} had lapsed (asleep or out of the store's reach): taken again", flush=True)
                return None
            kind, why = "lost", "taken by another holder after it lapsed"
        return kind, why

    def renew(self, st, progress, note=None):
        if st.renew(self.name, self.epoch, self.secret, progress, self.ttl, note=note) != "lost":
            return None
        return self.check(st, progress=progress)


class TurnProgress:
    """THE WRAPPER JUDGES PROGRESS (RED_STALL) of a turn, for both kinds: the mark (fixer_mark + bg_wait_mark) moving is
    progress; a turn whose mark stood still past `stall` is stalled. The leases renew with each beat of a turn not
    stalled (2026-11-03: renewed only on a new mark, a 15 min lease lapsed inside the 30 min stall budget)."""

    def __init__(self, stall, now):
        self.stall, self.mark, self.at = stall, None, now

    def judge(self, log, bid, wt, ent, transcript, now, every):
        """(mark, moved, stalled) at this beat."""
        mark = fixer_mark({"log": log or "", "bg_id": bid}, wt) + bg_wait_mark(ent, transcript, now, every)
        if mark != self.mark:
            self.mark, self.at = mark, now
            return mark, True, False
        return mark, False, now - self.at > self.stall


def lease_stop_line(kind, why, what="red's"):
    """The wrapper's stop reason for a lease no longer its own (HeldLease)."""
    if kind == "done":
        return f"ENDED: {why} (its lease was taken back: nothing left to fix)"
    if str(why).startswith("BLOCKED ON OWNER"):
        return str(why)
    return f"STOPPED: the {what} lease was lost ({why})"


def fixer_quiet(verdict):
    """A verdict the reconciler acts on itself (red_lease_ended), never a RED FIXER failure in the owner's inbox: the red
    ended under the fixer (ENDED), it is already fixed (verified by its probe), or it needs the owner's decision (one
    WAITING ON YOU, from the reconciler)."""
    v = str(verdict or "")
    return v.startswith("ENDED: ") or bool(GAVE_UP_FIXED.match(v) or GAVE_UP_DECIDE.match(v))


class CurrentStore:
    """Store() resolved again at every call, so a long-running process follows `agentctl store use URL` (2026-10-29
    cutover: the gate fixer's wrapper, started on the laptop's then store, renewed its leases there for hours while
    ci-runner's copies expired and its reds were offered again)."""

    def __getattr__(self, name):
        return getattr(Store(), name)


def red_fixer(a):
    """The dispatched fixer's wrapper (detached): start its background claude session under the red's lease, renew the
    lease by its progress every RED_RENEW_EVERY s, `claude stop` it when the lease is lost (RED-STOP.md), release the
    lease with the verdict from the session's final answer."""
    rec = read_record(a.record)
    keep_awake()  # the laptop must not idle-sleep past the lease's TTL while the fixer holds it (host_slept)
    num, wt = rec["run"], rec["worktree"]
    session = rec.get("session")  # known once `claude agents` lists the session (--bg picks its id)
    me = os.getpid()
    who = f"red-fixer-{num}"
    st = CurrentStore()
    name, epoch = rec.get("lease"), rec.get("epoch")
    secret = os.environ.pop("AGENTCTL_LEASE_SECRET", None)
    extra = [(x["name"], x["epoch"], json.loads(os.environ.get("AGENTCTL_LEASE_SECRETS") or "{}").get(x["name"]))
             for x in rec.get("extra_leases") or []]
    os.environ.pop("AGENTCTL_LEASE_SECRETS", None)
    if not name:  # an orphan's fixer (no offer): it takes a lease of its own
        name = red_lease(f"orphan-{os.path.basename(wt)}")
        took = st.acquire(name, f"laptop:fixer:{who}", RED_FIXER_TTL, RED_FIXER_MAX, progress="start",
                          meta={"kind": "fixer", "worktree": wt, "gate": False})
        if "held" in took:
            rec.update(pid=me, ended=ts(), rc=0, verdict=f"DUPLICATE: {took['held']['holder']} is on it")
            write_json(a.record, rec)
            print(f"RED FIXER {rec['workflow']} #{num}: DUPLICATE")
            return 0
        epoch, secret = took["epoch"], took["secret"]
    # ONE FIXER LIFECYCLE: its leases kept as an owner's are (HeldLease: a lapse taken again, a red that ended is no
    # failure); the holder is the dispatcher's, read from the lease's own epoch
    own = HeldLease(name, epoch, secret, rec.get("holder"), RED_FIXER_TTL)
    held_ = [own] + [HeldLease(n_, e_, s_, rec.get("holder"), RED_FIXER_TTL) for n_, e_, s_ in extra]

    def leases():
        return [h.triple() for h in held_]
    meta, meta_due = {"worktree": wt, "session": session, "pid": me, "log": rec["log"], "host": socket_host(),
                      "wrapper_log": os.path.join(os.path.dirname(os.path.abspath(a.record)), "fixer.log")}, False
    try:
        row = st.get(name)
        if row and row["epoch"] == epoch and row["secret"] == secret:
            own.holder = own.holder or row.get("holder") or None
            for h in held_:
                h.holder = h.holder or own.holder
            rec["holder"] = own.holder
            st.note(name, epoch, secret, f"dispatched: {rec.get('why')}")
            meta_due = not fixer_meta_sent(st, leases(), meta)
    except StoreUnavailable as e:  # its lease unread: the meta is sent at the polls below
        print(f"{ts()} the store did not answer ({e}): the leases' meta is sent again", flush=True)
        meta_due = True
    rec.update(pid=me, process_start=process_start(me), session=session, lease=name, epoch=epoch)
    write_json(a.record, rec)
    u = {"workflow": rec["workflow"], "job": rec.get("job"), "jobs": rec.get("jobs"), "kind": rec.get("kind") or "test",
         "line": rec.get("line"), "flake": rec.get("flake"), "diagnosis": rec.get("diagnosis"), "orphan": rec.get("orphan"),
         "refuted": rec.get("refuted"), "predicts": rec.get("predicts"), "prior_work": rec.get("prior_work"),
         "run": {"number": num, "databaseId": rec.get("run_id")}, "repo": rec.get("repo"),
         "blame_note": rec.get("blame_note"), "lander": rec.get("lander"), "decision": rec.get("decision")}
    u["record"] = a.record
    prompt = fixer_prompt(u, wt, sync_brief(), rec.get("resume_note"))
    cli = claude_cli()
    label = fixer_label(rec)
    argv = [cli, "--bg", prompt, "--permission-mode", interactive_mode(), "--permission-prompts", "none", "-n", label,
            "--disallowedTools", "Bash(git push:*)", "Bash(*--no-verify*)", "Bash(*PREPUSH_SKIP*)"]
    if red_owner_only(red_repo_of(rec)):  # an owner-only repo's fixer: no landing, no push, no deploy (red_owner_rules)
        argv += list(OWNER_ONLY_DENIED)
    if rec.get("resume") and rec.get("resumed_from"):
        argv += ["--resume", rec["resumed_from"]]
    if rec.get("effort"):  # a deep investigation
        argv += ["--effort", rec["effort"]]
    # NO FIXER IDENTITY IN THE SESSION'S ENVIRONMENT: `claude --bg` may start the claude daemon with it, and every
    # later background session -- other fixers', the owner's -- inherits the daemon's (2026-10-29: the daemon spawned by
    # #2473 fixer-5's wrapper named them all 'red-fixer-2473'). The prompt names the record; the session binds to it.
    env = {k: v for k, v in os.environ.items() if k not in FIXER_IDENTITY_ENV and k != "AGENTCTL_DETACHED"}  # an agent
    if rec.get("session"):
        bind_fixer_session(rec["session"], a.record)
    wall = env_num("AGENTCTL_RED_FIXER_WALL", RED_FIXER_WALL)
    every = env_num("AGENTCTL_RED_RENEW", RED_RENEW_EVERY)
    poll = env_num("AGENTCTL_BG_POLL", BG_POLL)
    t0, verdict, last_renew = time.time(), None, time.time()
    launch = rec.get("launch_cwd") if rec.get("launch_cwd") and os.path.isdir(rec["launch_cwd"]) else wt
    bid, why = bg_start(argv, launch, env)  # a revived lander: in its session's own cwd, where `--resume` finds it
    if not bid:
        verdict = f"NOT STARTED: `claude --bg` {why}"
        if rec.get("lander"):  # said, never looped: the red's next dispatch is a fresh fixer (red_lease_ended)
            line = f"LAUNCH FAILED: {why[:400]} (reviving the lander of {rec['lander'].get('change')}, {rec['workflow']} #{num})"
            inbox_write("red fixer", line, [line], [], 1, cwd=wt)
    else:
        rec.update(bg_id=bid, label=label)
        write_json(a.record, rec)

        def cancelled(signum, _frame):  # the wrapper is stopped: its session goes with it
            bg_stop(cli, bid)
            for n_, e_, s_ in leases():
                try:
                    st.release(n_, e_, s_, outcome="STOPPED: the fixer's wrapper was stopped")
                except StoreUnavailable:  # it lapses by its TTL instead
                    pass
            os._exit(143)
        for sg in (signal.SIGTERM, signal.SIGHUP, signal.SIGINT):
            signal.signal(sg, cancelled)
        missing, unclaimed_told = 0, False
        stall = env_num("AGENTCTL_RED_STALL", RED_STALL)
        prog = TurnProgress(stall, time.time())
        claim_within = env_num("AGENTCTL_FIXER_CLAIM_WITHIN", FIXER_CLAIM_WITHIN)
        try:
            while verdict is None:
                time.sleep(poll)
                ent = bg_session(cli, bid)
                if ent and ent.get("sessionId") and not rec.get("transcript"):
                    rec.update(session=ent["sessionId"], transcript=bg_transcript(ent["sessionId"]))
                    write_json(a.record, rec)
                    bind_fixer_session(rec["session"], a.record)
                    if rec["transcript"]:
                        rec["log"] = rec["transcript"]
                    meta.update(session=rec["session"], log=rec["log"], bg_id=bid)
                    meta_due = not fixer_meta_sent(st, leases(), meta)
                    write_json(a.record, rec)
                missing = missing + 1 if ent is None else 0
                if bg_turn_over(ent, rec.get("transcript")) or missing >= 3:  # finished its turn (done, stopped,
                    # blocked; or waiting with its verdict said, bg_turn_over), or gone from the list
                    break
                stop = None
                late = time.time() - t0 > claim_within
                if late and not rec.get("transcript") and not (read_record(a.record) or {}).get("note"):
                    # NEVER CLAIMED (2026-10-31): a session that in claim_within neither started its transcript nor ran
                    # its first `red claim` never got going -- ended with what `claude agents` says of it, never a
                    # silent "never got going"
                    stop = (f"NOT STARTED: session {bid} never claimed in {fmt_age(claim_within)} (`claude agents`: "
                            f"{bg_row_brief(ent)})")
                elif late and not unclaimed_told and not (read_record(a.record) or {}).get("note"):
                    unclaimed_told = True  # it works but never ran `red claim`: said once, on its lease and the inbox
                    tail = " | ".join(l.strip() for l in bg_final_text(rec.get("transcript")).splitlines()[-3:] if l.strip())
                    line = (f"RED FIXER {rec['workflow']} #{num}: session {bid} has not claimed in "
                            f"{fmt_age(claim_within)} (`claude agents`: {bg_row_brief(ent)}); its last words: {tail[:300]}")
                    inbox_write("red fixer", line, [line], [], 1, cwd=wt)
                    try:
                        st.note(*own.triple(), line[:300])
                    except StoreUnavailable:
                        pass
                if stop:
                    pass
                elif time.time() - t0 > wall:
                    stop = f"TIMED OUT after {int(wall // 60)} min (maximum wall time)"
                elif os.path.exists(os.path.join(wt, "RED-STOP.md")):
                    stop = "STOPPED: " + open(os.path.join(wt, "RED-STOP.md")).readline().strip()
                else:
                    # A store that does not answer decides nothing: it is asked again at the next poll, and a lease that
                    # lapsed meanwhile reads as lost once it answers. 2026-10-29, #2517: `POST /v1/store/get: Connection
                    # reset by peer` (ci-runner's store, through the tunnel) ended the wrapper a minute in; nothing renewed
                    # its lease, and a second fixer was dispatched onto the red its session was still working on.
                    try:
                        if meta_due:
                            meta_due = not fixer_meta_sent(st, leases(), meta)
                        gone_ = own.check(st)  # revoked (the red cleared, a release, its own block): stop at once;
                        if gone_:              # lapsed (asleep, the store out of reach): taken again, going on
                            stop = lease_stop_line(*gone_)
                        elif time.time() - last_renew >= every:
                            note = (read_record(a.record) or {}).get("note")
                            mark, _moved, stalled = prog.judge(rec["log"], rec.get("bg_id"), wt, ent,
                                                               rec.get("transcript"), time.time(), every)
                            if stalled:  # PROOF OF WORK: a living wrapper is not one
                                stop = f"STALLED: no progress (transcript, worktree) for {fmt_age(stall)}"
                            beat = f"{mark} beat {int(time.time())}"
                            for h in held_[1:] if not stop else ():  # the run's other reds: renewed with it
                                h.renew(st, beat, note=note)
                            gone_ = None if stop else own.renew(st, beat, note=note)
                            if gone_:
                                stop = lease_stop_line(*gone_)
                            last_renew = time.time()
                        if own.epoch != rec.get("epoch"):  # taken again after a lapse: the record names its epoch
                            rec["epoch"] = own.epoch
                            write_json(a.record, rec)
                    except StoreAuthFailed as e:  # never "asked again" forever: a fresh token once, else the end
                        if store_reauth():
                            env["AGENTCTL_STORE_TOKEN"] = os.environ["AGENTCTL_STORE_TOKEN"]
                            print(f"{ts()} the store refused the token ({e}): a fresh store token taken", flush=True)
                        else:
                            stop = f"store auth failed: {e}"
                    except StoreUnavailable as e:
                        print(f"{ts()} the store did not answer ({e}): asked again at the next poll", flush=True)
                if stop:
                    verdict = stop
        except Exception as ex:  # never a dead wrapper with a live session and its leases held
            verdict = f"STOPPED: the fixer's wrapper failed ({type(ex).__name__}: {str(ex)[:200]})"
        bg_stop(cli, bid)  # done or stopped: the session ends (`claude agents --all` keeps it, `claude attach` reopens it)
    if not rec.get("transcript") and rec.get("session"):
        rec["transcript"] = bg_transcript(rec["session"])
    text = bg_turn(rec.get("transcript"))["final"]  # THIS turn's: a resumed session's transcript holds earlier ones
    failure = bg_failure(rec.get("bg_id"))  # claude's own word on a failed session: the crash's actual error
    verdict = verdict or fixer_verdict(text) or ("ended with no LANDED / GAVE UP line"
                                                 + (f" (session {rec['bg_id']}: failed: {failure})" if failure else ""))
    line = f"RED FIXER {rec['workflow']} #{num}: {verdict[:300]}"
    tail = [l[:300] for l in text.splitlines()[-INBOX_TAIL:]]
    if not fixer_quiet(verdict):  # the red ended under it, already fixed, or the owner's decision: the reconciler's
        inbox_write("red fixer", line, [line], tail, 0 if verdict.startswith("LANDED") else 1, cwd=wt)
    if meta_due:  # never sent while the session ran: before the release, so a resume finds its session
        fixer_meta_sent(st, leases(), meta)
    for n_, e_, s_ in leases():
        for left in range(RED_RELEASE_ASKS, 0, -1):  # a store that does not answer is asked again, a poll apart
            try:
                st.release(n_, e_, s_, outcome=verdict_cut(verdict), notes="\n".join(tail[-8:]))
                break
            except StoreAuthFailed as e:  # refused, not unanswered: the laptop's tick revokes it (red_reap_orphans)
                print(f"{ts()} could not release {n_} ({e}): the tick revokes it", flush=True)
                break
            except StoreUnavailable as e:  # never answered: the lease lapses by its TTL, and the red is offered again
                print(f"{ts()} could not release {n_} ({e}){'' if left > 1 else ': it lapses by its TTL'}", flush=True)
                time.sleep(poll)
    rec.update(ended=ts(), rc=0 if verdict.startswith("LANDED") else 1, verdict=verdict)
    write_json(a.record, rec)
    if not rec.get("orphan"):  # an orphan's tree is its author's, never litter
        mark_internal(wt)  # its fixer ended: the tree is agentctl's litter now, never a fixer or owner
    print(line)
    return 0 if verdict.startswith("LANDED") else 1


# -- interactive: claim, beat, release
def red_targets(store, o_r, target, create=True):
    """The non-terminal red items a target names: a red key, or a run (number, id, or `local`); a run not yet seen by
    the tick is opened here (its observation), so a claim always has its item."""
    t = str(target).lstrip("#")
    active = red_active(store)
    if t in active:
        return [active[t]]
    # A RUN NUMBER is per repo: o_r's reds when it is another watched repo, else the primary's (as before 2026-10-30)
    mine = o_r if red_is_other(o_r) else RED_PRIMARY
    hits = [h for h in red_item_of_run(store, t) if red_repo_of(h["data"]) == mine]
    if hits or not create:
        return hits
    name, r, units = red_run_units(o_r, t)
    out = []
    for key, ob in red_obs_for(o_r, red_obs_of_units(units)).items():
        it = active.get(key) or red_open(store, ob, None, store.clock(), o_r)
        out.append(red_attach_obs(store, it, ob))
    return out


def root_checkout(wt, o_r):
    """True for a repo's own clone (the root checkout): a claim names a worktree of its own (audit #3)."""
    roots = set()
    for f in (lambda: scan_repo_for(o_r), lambda: repo_path_for(o_r), movies_repo_path):
        try:
            roots.add(os.path.realpath(f()))
        except (Exception, SystemExit):
            pass
    return os.path.realpath(wt) in roots


def own_fixer_record_path():
    """This session's own fixer record, or None. The session id decides -- its binding (bind_fixer_session), else a
    record naming it; with a session, the environment never does: a `claude --bg` session can start in a spare
    process spawned with ANOTHER fixer's environment (2026-10-29: #2426's fixer carried #2432's AGENTCTL_FIXER_RECORD
    and wrote its notes onto #2432's lease), so the environment's record counts only when its session is this one, or
    when no record names this session (a record not yet told its session id)."""
    sid = os.environ.get("CLAUDE_CODE_SESSION_ID")
    env = os.environ.get("AGENTCTL_FIXER_RECORD")
    if sid:
        bound = (read_record(fixer_session_path(sid)) or {}).get("record")
        rec = read_record(bound) if bound else None
        if rec and not rec.get("ended") and record_session_is(rec, sid, unset=True):
            return bound
        mine = [r for r in fixer_records() if r.get("session") == sid and not r.get("ended")]
        if mine:
            return max(mine, key=lambda r: r.get("started") or "")["path"]
        return None  # a session no record or binding names is no fixer, whatever its environment says
    return env or None


def fixer_session_path(sid):
    return reds_dir("sessions", f"{re.sub(r'[^A-Za-z0-9_.-]', '_', sid)}.json")


def record_session_is(rec, sid, unset=False):
    """Fixer record rec is session sid's: it names sid, or (its wrapper's first poll not yet done) the bg session it
    launched, whose id sid begins with (issue 20261119T025305-21375d); unset: a record naming no session yet too."""
    have = rec.get("session")
    return have == sid or (unset and have is None) or \
        bool(sid and rec.get("bg_id") and not rec.get("transcript") and str(sid).startswith(str(rec["bg_id"])))


def bind_fixer_session(sid, record):
    """The session -> fixer record binding (reds/sessions/<session id>.json): written by the wrapper once `claude
    agents` names its session, and by the fixer's own first command (`red claim N --fixer <record>`, in its prompt).
    Refused (None) for a record that ended or names another session."""
    rec = read_record(record) if sid and record else None
    if not rec or rec.get("ended") or not record_session_is(rec, sid, unset=True):
        return None
    write_json(fixer_session_path(sid), {"record": record, "at": ts()})
    return record


def red_claim(store, o_r, target, note="", wt=None):
    """(rc, lines): the red's lease to the caller (a claim, renewed by its worktree's activity). Inside a dispatched
    fixer, a red its wrapper already holds (its own, or a family / run sibling under the same holder) gets the fixer's
    note; ANY OTHER red N is claimed for the fixer, in its worktree -- how a fixer (the convergence family's) takes a
    second red. 2026-10-29: `red claim <N>` in a fixer wrote the note on its own lease and ignored N."""
    own = own_fixer_record_path()
    rec = (read_record(own) or {}) if own else {}
    here = safe_cwd()
    items = None
    if own:
        t = str(target).lstrip("#")
        if t in (str(rec.get("run")), str(rec.get("lease") or "").split("/", 1)[-1]):  # its own red, by run or key
            rec["note"] = note or rec.get("note") or "working"
            write_json(own, rec)
            return 0, [f"noted on your lease {rec.get('lease')}: {rec['note']}"]
        items = red_targets(store, o_r, target)
        primary = store.get(rec["lease"]) if rec.get("lease") else None
        mine = primary["holder"] if lease_is_live(primary, store.clock()) else None

        def held_by_me(it):
            row = store.get(red_lease(it["data"]["key"]))
            return lease_is_live(row, store.clock()) and (row["holder"] == mine or row["name"] == rec.get("lease"))
        if not items or all(held_by_me(it) for it in items):
            rec["note"] = note or rec.get("note") or "working"
            write_json(own, rec)
            for it in items or []:  # a red joined to its lease, worked on: no longer one the backstop releases
                row = store.get(red_lease(it["data"]["key"]))
                if lease_meta(row).get("joined_at"):
                    red_joined_progress(store, row, f"note: {rec['note']}")
            return 0, [f"noted on your lease {rec.get('lease')}: {rec['note']}"]
        items = [it for it in items if not held_by_me(it)]
        wt = wt or rec.get("worktree")
    wt = os.path.realpath(wt or worktree_of(here) or here)
    if root_checkout(wt, o_r) or not os.path.isdir(wt):
        return 2, [f"NOT CLAIMED: {wt.replace(HOME, '~')} is not a worktree of your own (a claim is renewed by its "
                   "worktree's activity; the root checkout counts for nobody): `--worktree <your worktree>`"]
    items = items if items is not None else red_targets(store, o_r, target)
    if not items:
        return 1, [f"NOT CLAIMED: #{target} names no red on main"]
    who = claimant(here)
    holder = f"laptop:claim:{who}@{wt}"
    rc, lines = 0, []
    for it in items:
        name = red_lease(it["data"]["key"])
        row = store.get(name)
        if lease_is_live(row, store.clock()) and row["holder"] == holder:
            store.renew(name, row["epoch"], row["secret"], f"beat {time.time()}", RED_CLAIM_TTL, note=note or None)
            lines.append(f"renewed {red_what(it['data'])} ({name})")
            continue
        r = store.acquire(name, holder, RED_CLAIM_TTL, RED_CLAIM_MAX, progress=red_wt_mark(wt), item=f"red:{it['id']}",
                          meta={"kind": "claim", "worktree": wt, "who": who, "note": note,
                                "session": os.environ.get("CLAUDE_CODE_SESSION_ID"), "gate": bool(it["data"].get("gate")),
                                # made inside a dispatched fixer: its family's holder (red_family_holder); an
                                # interactive session's claim takes only its red (the owner, 2026-11-08)
                                **({"fixer": own} if own else {})})
        if "held" in r:
            rc = 1
            lines.append(f"NOT CLAIMED: {red_what(it['data'])} is held by {r['held']['holder']} until "
                         f"{time.strftime('%H:%M', time.localtime(r['held']['expires_at']))}")
            continue
        store.close_offer(f"red:{it['id']}", "taken")
        lines.append(f"claimed {red_what(it['data'])} for {who} ({wt.replace(HOME, '~')}) as {name}"
                     + (f": {note}" if note else "") + f"; it lapses after {RED_CLAIM_TTL // 60} min without activity in "
                     f"your worktree; release: ~/bin/agentctl red release {it['data']['key']}")
    return rc, lines


def red_beat(store, o_r, target, note):
    if own_fixer_record_path():
        return red_claim(store, o_r, target, note)
    out, rc = [], 1
    for it in red_targets(store, o_r, target, create=False):
        name = red_lease(it["data"]["key"])
        row = store.get(name)
        if not lease_is_live(row, store.clock()):
            out.append(f"{red_what(it['data'])}: no live lease (`red claim` it)")
            continue
        r = store.renew(name, row["epoch"], row["secret"], f"note:{note}", RED_CLAIM_TTL, note=note)
        rc = 0 if r == "ok" else rc
        out.append(f"{red_what(it['data'])}: {r}" + (" (the same note is no progress)" if r == "no_progress" else ""))
    return rc, out or [f"#{target} names no red"]


def red_release(store, o_r, target, any_=False):
    me = claimant()
    out, rc = [], 1
    for it in red_targets(store, o_r, target, create=False):
        name = red_lease(it["data"]["key"])
        row = store.get(name)
        blk = red_block_of(it, store) if any_ and not lease_is_live(row, store.clock()) else None
        if blk and (blk["meta"].get("crash_loop") or blk["meta"].get("decision")):
            # THE OWNER LIFTS A BLOCK OF THE HARNESS'S OR A DECISION: the red is offered again at the next tick
            keys = [k for k in blk["meta"].get("keys") or [] if k != it["data"]["key"]]
            brow = store.get(blk["name"])
            if keys:  # the block holds other reds too: only this one leaves it
                store.set_meta(blk["name"], brow["epoch"], brow["secret"], {"keys": keys})
            else:
                store.revoke(blk["name"], f"RELEASED by {me}: offered again")
            rc = 0
            out.append(f"lifted the owner block of {red_what(it['data'])} ({blk['cause']}): offered again at the next tick")
            continue
        if not lease_is_live(row, store.clock()):
            continue
        if not any_ and lease_meta(row).get("who") != me:
            out.append(f"{red_what(it['data'])} is held by {row['holder']}, not {me} (--any releases anyone's)")
            continue
        store.revoke(name, f"RELEASED by {me}")
        rc = 0
        out.append(f"released {red_what(it['data'])} ({row['holder']})")
    return rc, out or [f"red: no live lease on #{target}"]


# -- how a red reads (ci-why, the status line, `red status`): its state, holder or offer, deadline. Never NOBODY.
def gate_red_runs():
    """[(workflow name, decided run)] of the gate's red workflow(s) in the snapshot."""
    return [(g.get("name"), g["decided"]) for g in snapshot_gate_reds()]


def gate_red_now_label():
    """'convergence #2473' for the gate's red workflow(s) in the snapshot, else None."""
    return ", ".join(f"{short_flow(n)} #{dd.get('number')}" for n, dd in gate_red_runs()) or None


def gate_own_red_label(d):
    """'convergence #2550' when red d IS the gate's red (a run of the gate's red workflow carries its key), else None:
    its Fixes-Red fix lands on red (whichever run of the key it names), never blocked by that gate."""
    names = set(map(str, red_names(d)))
    own = [f"{short_flow(n)} #{dd.get('number')}" for n, dd in gate_red_runs()
           if n == d.get("workflow") and run_names(dd) & names]
    return ", ".join(own) or None


def red_view(store, it, t=None):
    """(text, short) of one red, its blame (a revert, its lander revived: blame.py) said with it."""
    text, short = red_decided_view(it["data"], *red_view_state(store, it, t))
    bl = blame_status(it["data"])
    return (f"{text} -- {bl[0]}", f"{short}; {bl[1]}") if bl else (text, short)


def red_view_state(store, it, t=None):
    """(text, short) of one red's state."""
    t = t if t is not None else store.clock()
    d, st = it["data"], it["state"]
    at = time.strftime("%H:%M", time.localtime(it["deadline"]))
    rec = f" -- recurring: {len(red_refuted(d))} fixes refuted" if len(red_refuted(d)) >= RED_RECURRING else ""
    if st == "red":
        return "observed: offered at the next tick", "offering"
    if st == "offered":
        off = store.offer_of(f"red:{it['id']}") or {}
        tier = d.get("tier") or "fresh"
        reason = off.get("reason") or "awaiting a taker"
        late = " (ESCALATED)" if it.get("level") else ""
        return (f"offered{late}: {tier} tier, {reason}; escalates {at} (`agentctl red claim {d['key']} --worktree .`){rec}",
                f"offered{late}: {tier}, {reason}")
    if st == "leased":
        row = store.get(red_lease(d["key"])) or {}
        m = lease_meta(row)
        who = m.get("who") or str(row.get("holder") or "?").split(":")[-1]
        wt = m.get("worktree")
        if not wt and m.get("kind") == "owned" and m.get("family"):  # an owner's red: the owner's ONE worktree
            wt = (read_record(owner_record_path(m["family"])) or {}).get("worktree")
        wt = str(wt or "?").replace(HOME, "~")
        exp = time.strftime("%H:%M", time.localtime(row.get("expires_at") or t))
        prim = str(m.get("primary") or "").split("/")[-1]  # a sibling's primary red; an owner's red has none
        fam = f" [family {m['family']}{', with ' + prim if prim else ''}]" if m.get("family") else ""
        return (f"{who} ({wt}, leased{fam}; lapses {exp} without progress)" + (f" -- {m['note']}" if m.get("note") else "")
                + rec,
                f"{who.replace('movies-wt-', '').replace('red-fixer-', 'fixer ')} leased")
    if st == "fix-pending" and (d.get("fix") or {}).get("unproven"):  # A FIX WITH NO REPLAY KEEPS ITS FIXER (redmove.py)
        f = d["fix"]
        return (f"fix-unproven: {f.get('entry') or str(f.get('sha'))[:9]} ({f.get('status')}) carries no replay; "
                f"{d.get('holder') or 'its holder'} keeps it until a {d.get('workflow')} run decides it{rec}",
                f"fix-unproven {str(f.get('sha') or f.get('entry') or '')[:7]}")
    if st == "fix-pending":
        f = d.get("fix") or {}
        if f.get("status") == "landed" and str(f.get("verify") or "").startswith("dispatched") \
                and t < float(it["deadline"]):  # VERIFY PROMPTLY BY DEFAULT (red.py): its dispatched run decides it,
            at = str(f.get("verify_dispatched") or "")[11:16]  # never the cron slot (2026-11-21, Order independence
            return (f"fix landed in {str(f.get('sha'))[:9]}, verify run dispatched {at}Z ({d.get('workflow')}; "  # #99)
                    f"verified by {hhmmz(f.get('verify_by') or it['deadline'])}){rec}",
                    f"fix {str(f.get('sha'))[:7]} verify run dispatched {at}Z")
        if f.get("status") == "landed" and f.get("next_run_at") and t < float(it["deadline"]):  # red_verify_plan
            return (f"fix landed in {str(f.get('sha'))[:9]}, awaiting next scheduled run at {hhmmz(f['next_run_at'])} "
                    f"({d.get('workflow')}; verified by {hhmmz(f.get('verify_by') or it['deadline'])}){rec}",
                    f"fix {str(f.get('sha'))[:7]} awaiting next scheduled run at {hhmmz(f['next_run_at'])}")
        if f.get("status") == "landed":
            return (f"fix landed in {str(f.get('sha'))[:9]}, awaiting a {d.get('workflow')} run containing it{rec}",
                    f"fix {str(f.get('sha'))[:7]} awaiting its run")
        if f.get("status") == RED_WAITS_ON:
            return (f"waits on red {f.get('red')}'s fix {str(f.get('sha'))[:10]}: verified against the next "
                    f"{d.get('workflow')} run once main has it{rec}", f"waits on red {f.get('red')}'s fix {str(f.get('sha'))[:10]}")
        if f.get("status") == "verify":
            return (f"verifying: the next decided {d.get('workflow')} run says whether it is fixed ({f.get('verify') or 'awaiting it'}); "
                    f"offered again if none by {time.strftime('%H:%M', time.localtime(it['entered_at'] + env_num('AGENTCTL_RED_FIX_GIVEUP', RED_FIX_GIVEUP)))}{rec}",
                    "verifying")
        if f.get("status") in FIX_OPEN:  # queued, not landed: nothing has tested it yet -- it holds the red
            # THE FIX OF THE GATE'S OWN RED lands on red (2026-10-29 19:20: convergence #2550's fix, Fixes-Red: 2527,
            # read "blocked by gate convergence #2550" -- blocked by the red it fixes)
            own = gate_own_red_label(d)
            if own:
                return (f"fix {f.get('entry')} landing on red (Fixes-Red), {f.get('status')}: it fixes the gate's own "
                        f"red ({own}); verified once it lands{rec}",
                        f"landing on red (Fixes-Red), {f.get('status')}")
            by = gate_red_now_label()
            return (f"fix {f.get('entry')} waiting to land ({f.get('status')}" + (f"; blocked by gate {by}" if by else "")
                    + f"): verified once it lands (Fixes-Red){rec}",
                    "fix waiting to land" + (f" (blocked by gate {by})" if by else ""))
        return f"fix {f.get('entry')} {f.get('status')} (Fixes-Red){rec}", f"fix {f.get('status')}"
    if st == "blocked-external":
        b = red_block_of(it, store) or {}
        ask = red_ask(it, b)
        if ask:  # WAITING ON YOU (ask.py): the question itself, never a bare cause
            more = "" if str(b.get("text") or "").startswith("WAITING ON YOU") else f" -- {b.get('text')}"
            return (f"WAITING ON YOU: {ask['q']} -- {ask['how']}" + (f"; auto-clears when {ask['until']}" if ask.get("until")
                                                                     else "") + more,
                    f"WAITING ON YOU: {trim_words(ask['q'])} -- {ask['how']}")
        if red_decision(d):  # answered: its block is lifted, offered again at the next tick
            return "the owner's answer is in: offered again at the next tick", "re-dispatching"
        if not b:  # its block lapsed (the tick offers it again): never a stale BLOCKED ON OWNER on the status line
            return "no block holds it now: offered again at the next tick", "re-offering"
        if (b.get("meta") or {}).get("harness"):  # agentctl's own fault (redcrash.py red_harness_issue): its maintainer's
            return b.get("text"), f"HARNESS FAULT: agentctl issue {(b.get('meta') or {}).get('issue')}"
        return b.get("text") or "BLOCKED ON OWNER", (f"BLOCKED: {d.get('cause')} down" if d.get("cause") else "BLOCKED ON OWNER")
    return st, st


def red_view_of_units(store, units):
    """{(workflow, run id): {state, text, short, cause}} for ci-why's red rows, from the red items their keys name."""
    out, active, settled = {}, red_active(store), None
    groups = {}
    for key, ob in red_obs_of_units(units).items():
        groups.setdefault((ob["workflow"], ob["run"].get("databaseId")), []).append(key)
    for k, keys in groups.items():
        its = [active[x] for x in keys if x in active]
        if not its:
            settled = red_settled_runs(store) if settled is None else settled
            done = [settled.get((x, str(k[1]))) for x in keys]
            if all(done):  # its newest run for good, proven fixed by its callers' runs: never offered again
                proof = done[0]["data"].get("proof") or "proven fixed by its callers' runs"
                out[k] = {"state": "verified", "text": f"verified: {proof}", "short": "verified", "cause": None}
            else:
                out[k] = {"state": "red", "text": "observed: offered at the next tick", "short": "offering", "cause": None}
            continue
        rank = {"leased": 0, "fix-pending": 1, "blocked-external": 2, "offered": 3, "red": 4}
        it = min(its, key=lambda x: rank.get(x["state"], 5))
        text, short = red_view(store, it)
        more = f" (+{len(its) - 1} more red{'s' if len(its) > 2 else ''} in this run)" if len(its) > 1 else ""
        blk = red_block_of(it, store) if it["state"] == "blocked-external" else None
        ask = red_ask(it, blk)
        out[k] = {"state": it["state"], "text": text + more, "short": short, "cause": it["data"].get("cause"),
                  "until": (blk or {}).get("until"), "ask": ask["q"] if ask else None, "ask_how": ask["how"] if ask else None}
    return out


def red_lines(st, indent="  "):
    return [f"{indent}fixing: {st['text']}"] if st else []


# DISPATCH OFF PER REPO (the owner, 2026-11-19 ~16:20Z: "no bitcashier builds" -- the laptop CPU-starved). The store's
# meta `repo_dispatch_off` names the repos whose reds are recorded and shown but never offered, dispatched or put to the
# owner (no vault Touch ID ask): red_step holds each in `dispatch-off` (its offer closed, its lease and its owner block
# released), the dispatcher skips any offer of one, and `red auto on --repo R` resumes them (offered at the next tick).
REPO_DISPATCH_OFF = "repo_dispatch_off"


def repo_dispatch_off_all(store):
    try:
        v = json.loads(store.meta_get(REPO_DISPATCH_OFF) or "{}")
        return v if isinstance(v, dict) else {}
    except (ValueError, TypeError):
        return {}


def repo_dispatch_set(store, o_r, off, by):
    cur = repo_dispatch_off_all(store)
    if off:
        cur[o_r] = {"at": ts(), "by": by}
    else:
        cur.pop(o_r, None)
    store.meta_set(REPO_DISPATCH_OFF, json.dumps(cur, sort_keys=True))


def repo_dispatch_off(store, o_r):
    """The record of o_r's dispatch being off (the owner's), or None."""
    return repo_dispatch_off_all(store).get(o_r)


def red_status(store, verbose=False):
    """`agentctl red status`: rendered from the status model (its reds, families and dispatcher sections)."""
    print(render_red_status(status_data(store=store, now_=store.clock(), only=("reds", "families", "owners", "dispatcher",
                                                                                 "untracked", "reverts")),
                            verbose))
    return 0


# -- the tick's red step (the processor; ci-why never acts)
_red_lock = threading.Lock()
_last_red_tick = [0.0]


def red_tick(store, o_r, repo, snap, now_=None, dispatch=False, facts=None, sync=False):
    """One level-triggered pass: observe (unless the gate is unread: nothing opens or clears on an unknown), each red one
    step (per item, inside try: one bad item never stops the tick), the claims renewed, the dispatcher. Returns what it
    did."""
    t = now_ if now_ is not None else store.clock()
    acts = []
    obs = None
    # THE PRIMARY'S TICK also runs the store-wide passes (the migration, the blocks, the invariant); another watched
    # repo's tick only observes, opens and steps ITS OWN reds -- the primary's observation never clears another repo's
    # red (it is simply absent from it), nor the other way round
    primary = not red_is_other(o_r)
    if snap:
        try:
            obs = red_obs_for(o_r, red_observe(o_r, repo, snap) if primary else red_observe_gate(o_r, snap))
        except (Exception, SystemExit) as e:
            err(f"red tick: could not observe ({str(e)[:160]}): nothing opens or clears this tick")
    untracked = {}
    if obs is not None:  # A CHECK NEVER GREEN ON MAIN IS NOT TRACKED (reduntracked.py): set apart, listed for red status -v
        obs, untracked = red_split_untracked(store, o_r, obs, t)
        try:
            red_note_untracked(store, o_r, untracked)
        except (Exception, SystemExit) as e:
            err(f"red tick: could not note the untracked reds ({str(e)[:160]})")
    try:
        n = red_migrate(store) if primary else 0
        if n:
            acts.append(f"red model {RED_MODEL}: {n} open red(s) migrated in place")
    except (Exception, SystemExit) as e:
        err(f"red tick: the red model migration failed ({str(e)[:160]}); tried again next tick")
    if facts is None and primary:
        try:
            red_main_current(repo, snap)
        except (Exception, SystemExit) as e:
            err(f"red tick: could not bring main up to date ({str(e)[:160]})")
    facts = facts if facts is not None else red_fix_facts(repo, store)
    if primary:
        try:  # THE LANDER (lander.py): each landed change's commits on main, noted while its keys still find them
            acts += lander_note_landed(store, repo, o_r, t)
        except (Exception, SystemExit) as e:
            err(f"red tick: noting the landed changes' commits failed ({str(e)[:160]})")
    active, settled = red_active_of(store, o_r), None
    for key, ob in (obs or {}).items():
        try:
            it = active.get(key)
            if it:
                active[key] = red_attach_obs(store, it, ob)
                continue
            settled = red_settled_runs(store) if settled is None else settled
            if (key, str(ob["run"].get("databaseId"))) not in settled \
                    and not red_retired(o_r, {"red_kind": ob["red_kind"], "workflow": ob["workflow"],
                                              "last_run": ob["run"], "jobs": ob["jobs"]}, t):
                active[key] = red_open(store, ob, repo, t, o_r)  # a retired check's run opens no red
        except (Exception, SystemExit) as e:
            err(f"red tick: {key}: {str(e)[:160]}")
    try:
        acts += red_merge_rekeyed(store, active, obs)
    except (Exception, SystemExit) as e:
        err(f"red tick: merging re-keyed reds failed ({str(e)[:160]})")
    try:
        acts += red_renew_blocks(store, t) if primary else []
    except (Exception, SystemExit) as e:
        err(f"red tick: renewing the blocks failed ({str(e)[:160]})")
    try:
        acts += red_quota_waits(store, o_r, repo, t)
    except (Exception, SystemExit) as e:
        err(f"red tick: the quota waits failed ({str(e)[:160]})")
    for key, it in list(active.items()):
        try:
            why = red_active_untracked(store, o_r, it, untracked, obs, t)
            if why:
                red_untrack(store, it, why)
                acts.append(f"untracked {red_what(it['data'])}: {why}")
                continue
            acts += red_step(store, it, obs, snap, facts, repo, o_r, t)
        except StaleVersion:
            pass  # another writer moved it: the next tick
        except (Exception, SystemExit) as e:
            err(f"red tick: {key}: {type(e).__name__}: {str(e)[:200]}")
    try:  # WAITING ON YOU: each red on the owner told once, then reminded (ask.py)
        acts += red_owner_reminders(store, t) if primary else []
    except (Exception, SystemExit) as e:
        err(f"red tick: the owner's reminders failed ({str(e)[:160]})")
    try:
        acts += red_enforce(store, facts, t) if primary else []
    except (Exception, SystemExit) as e:
        err(f"red tick: the invariant pass failed ({str(e)[:160]})")
    if sync or dispatch:  # the sync's half on the same store (claims renewed; with dispatch its dispatcher): the tests'
        try:  # one-process stand-in for the two hosts. In production the laptop's sync runs these (sync_steps)
            red_renew_holders(store, t)
            acts += red_reap_orphans(store, t)
        except (Exception, SystemExit) as e:
            err(f"red tick: renewing the claims failed ({str(e)[:160]})")
    if dispatch:  # fixers run on the laptop (owner decision 8): its sync dispatches
        try:
            acts += red_dispatcher(store, o_r, repo, t)
        except (Exception, SystemExit) as e:
            err(f"red tick: the dispatcher failed ({str(e)[:160]})")
    return acts


def red_tick_repo(repo, snap=None, dispatch=False):
    o_r = slug(repo)
    if not gated_repo(o_r):
        return []
    with _red_lock:
        _last_red_tick[0] = time.time()
        recheck_local(repo, wait=False)
        acts = red_tick(Store(), o_r, repo, snap or ci_state_load(o_r), dispatch=dispatch)
    for line in acts:
        err(f"red: {line}")
    return acts


def red_open_flows(o_r, store=None):
    """The workflows of o_r's open reds that are not its gate's: their newest decided run on main must be read too, or
    such a red can never clear (core Dependabot Updates #154, 2026-11-02: green #155/#156 came, nothing read them)."""
    try:
        its = (store or Store()).items("red")
    except (Exception, SystemExit):
        return []
    gate = {n for n, _f in red_gate_flows(o_r)}
    return sorted({it["data"].get("workflow") for it in its if it["state"] not in RED_TERMINAL
                   and it["data"].get("repo") == o_r and it["data"].get("workflow")} - gate)


def newest_decided_main(o_r, workflow):
    """The newest decided (success / failure / timed_out) run of a workflow on main, or None."""
    # listing: None here means none decided among the newest 10 -- the callers treat it as unread, not green
    runs = gh_json(o_r, "run", "list", "--workflow", workflow, "--branch", "main", "--limit", "10", "--json", RUN_FIELDS)
    return next((r for r in runs if r.get("status") == "completed"
                 and r.get("conclusion") in ("success", "failure", "timed_out")), None)


def red_gate_snap(o_r, store=None):
    """Another watched repo's snapshot: its gate workflows only (red-repos.json), read once. The shared run lists the
    primary's snapshot fills (_gate_lists, _outside_lists) are put back as they were: workflow names repeat across
    repos (movies has a `Stage NixOS closures` too)."""
    saved = (dict(_gate_lists), dict(_outside_lists), dict(_gate_no_verdict))
    try:
        gate = gate_runs(o_r, red_gate_flows(o_r))
        fields = ("number", "databaseId", "conclusion", "headSha", "updatedAt")
        latest = {n: {k: d.get(k) for k in fields} for n, d, _nw, _st in gate if d}
        for wf in red_open_flows(o_r, store):  # an open red's own workflow, outside the gate: read so it can clear
            try:
                r = newest_decided_main(o_r, wf)
            except (Exception, SystemExit):
                r = None
            if r:
                latest[wf] = {k: r.get(k) for k in fields}
        return {"at": time.time(), "repo": o_r, "outside": [], "outside_error": None,
                "gate": [{"name": n, "decided": d, "newer": nw, "since": st[0], "count": st[1],
                          "runs": _gate_lists.get(n, []), "no_verdict": _gate_no_verdict.get(n)} for n, d, nw, st in gate],
                "latest": latest}
    finally:
        for live, old in zip((_gate_lists, _outside_lists, _gate_no_verdict), saved):
            live.clear()
            live.update(old)


def red_observe_gate(o_r, snap):
    """{key: observation} of another watched repo: its gate workflows' red runs (no outside workflows, local checks,
    branch predictions or convergence kicks -- those are the primary's), and its stuck gate runs. The shared run lists
    are restored afterwards (red_gate_snap)."""
    saved = (dict(_gate_lists), dict(_outside_lists), dict(_gate_no_verdict))
    try:
        rows = [(n, d, _gate_lists.get(n, []), True) for n, d, _nw, _s in gate_from_snap(snap)]
        obs = red_obs_of_units(gate_red_units(o_r, rows))
        obs.update(stuck_obs(snap))
        return obs
    finally:
        for live, old in zip((_gate_lists, _outside_lists, _gate_no_verdict), saved):
            live.clear()
            live.update(old)


RED_OTHERS_EVERY = 60        # seconds between the other watched repos' red ticks: one gate read each ($AGENTCTL_RED_OTHERS_EVERY)
_last_others_tick = [0.0]


def red_tick_others(store=None, now_=None, dispatch=False, force=True):
    """The red tick of every watched repo but the primary (red-repos.json): its gate read, its reds observed and
    stepped, each repo inside its own try (one unreadable repo never stops the others or the primary). Its clone is
    this machine's when there is one (Fixes-Red trailers, authors); without one (ci-runner) its reds open and clear
    by its gate runs alone."""
    acts = []
    if not force and time.time() - _last_others_tick[0] < env_num("AGENTCTL_RED_OTHERS_EVERY", RED_OTHERS_EVERY):
        return acts
    _last_others_tick[0] = time.time()
    unread = []
    for c in red_repos():
        o_r = c["repo"]
        if o_r == RED_PRIMARY:
            continue
        try:
            red_read_token(o_r)
        except NoReadAccess:
            unread.append(o_r)  # one condition for all of them, below: no line per tick
            continue
        try:
            snap = red_gate_snap(o_r, store)
            clone = red_repo_clone(o_r)
            with _red_lock:
                acts += [f"{red_repo_short(o_r)}: {x}" for x in
                         red_tick(store or Store(), o_r, clone, snap, now_=now_, dispatch=dispatch)]
            _others_said.pop(o_r, None)
        except (Exception, SystemExit) as e:
            red_others_skipped(o_r, str(e).replace('agentctl: ', '')[:200])
    if unread:
        try:
            red_no_read_access(unread, store)
        except (Exception, SystemExit) as e:
            err(f"red tick: could not note the missing read access ({str(e)[:120]})")
    for line in acts:
        err(f"red: {line}")
    return acts


_others_said = process_cache({})  # repo -> (why, when said): a skipped repo's line is said when it changes, else every RED_SKIP_SAY s
RED_SKIP_SAY = 600  # s between the lines of another repo whose tick keeps failing for the same reason


def red_others_skipped(o_r, why, now_=None):
    """Another repo's tick failed: said on a new reason, else at most every RED_SKIP_SAY s (it was every tick)."""
    t = now_ if now_ is not None else time.time()
    last = _others_said.get(o_r)
    if last and last[0] == why and t - last[1] < env_num("AGENTCTL_RED_SKIP_SAY", RED_SKIP_SAY):
        return None
    _others_said[o_r] = (why, t)
    line = f"red tick: {o_r}: skipped ({why})" + (" (still)" if last and last[0] == why else "")
    err(line)
    return line


def red_no_read_access(repos, store=None):
    """ONE condition while ci-runner cannot read the other watched repos: cleared by the red_read probe (the reader App
    configured on the host, and their runs read)."""
    return open_condition("UNKNOWN", f"NO READ ACCESS: {', '.join(repos)}: the reconciler cannot read their runs -- "
                                     "the lander App is push-only on movies (needs the agentctl-reader App); their "
                                     "reds are not watched until then",
                          run_probe("red_read", {"repos": list(repos)}), [], key="red-read-access", command="red",
                          exit_code=2, store=store)


def probe_red_read(a):
    """Every repo in a["repos"] has a reader token and its runs read."""
    for o_r in a.get("repos") or []:
        try:
            red_read_token(o_r)
            # listing: a read probe only (any answer proves the token reads)
            gh_json(o_r, "run", "list", "--limit", "1", "--json", "databaseId")
        except (Exception, SystemExit) as e:
            return False, f"{o_r}: {str(e).replace('agentctl: ', '')[:160]}"
    return True, "every watched repo's runs read"


def maybe_red_tick(repo):
    """At most every RED_TICK_INTERVAL s, from the processor's duties. Bounded; never fatal."""
    if time.time() - _last_red_tick[0] < env_num("AGENTCTL_RED_TICK", RED_TICK_INTERVAL):
        return

    def work():
        try:
            red_tick_repo(repo)
        except (Exception, SystemExit) as e:
            err(f"red tick: skipped ({str(e).replace('agentctl: ', '')[:200]})")
            try:
                gate_unread_spell(slug(repo), str(e)[:200])
            except (Exception, SystemExit):
                pass
        red_tick_others(force=False)  # the other watched repos (red-repos.json); never fatal, never the primary's gate spell
    if not run_bounded("red-tick", work, env_num("AGENTCTL_STEP_BOUND", 120)):
        err("red tick: still running from an earlier cycle (the loop goes on)")


def red_am_fixer():
    """`agentctl red am-fixer`: 0 inside a dispatched red fixer -- known by its session's binding or record, else (no
    Claude session) its fixer environment (74e2ec5's in_fixer) -- printing who; 1 for anybody else."""
    if in_fixer():
        print(agent_name() or own_fixer_record_path() or "a red fixer")
        return 0
    return 1


def cmd_red(a):
    if a.rcmd == "am-fixer":
        return red_am_fixer()
    if a.rcmd == "probe":
        return cmd_red_probe(a)
    o_r = slug(a.repo) if getattr(a, "repo", None) and a.rcmd not in ("fixer", "owner") else default_repo()
    st = Store()
    if a.rcmd == "block":
        return red_block(o_r, a)
    if a.rcmd == "unblock":
        rc, lines = red_unblock(st, o_r, a.run, a.why)
        print("\n".join(lines))
        return rc
    if a.rcmd == "owner-push":
        rc, lines = red_owner_push(st, o_r, a.run, a.note or "", getattr(a, "worktree", None))
        print("\n".join(lines))
        return rc
    if a.rcmd in ("claim", "beat", "release"):
        if a.rcmd == "claim":
            if getattr(a, "fixer", None):  # the dispatched fixer says who it is (its prompt): bound to its session
                sid = os.environ.get("CLAUDE_CODE_SESSION_ID")
                if sid and not bind_fixer_session(sid, os.path.realpath(a.fixer)):
                    print(f"NOT CLAIMED: {a.fixer} is no live fixer record of this session ({sid[:8]})")
                    return 2
            rc, lines = red_claim(st, o_r, a.run, a.note or "", getattr(a, "worktree", None))
        elif a.rcmd == "beat":
            rc, lines = red_beat(st, o_r, a.run, a.note or "")
        else:
            rc, lines = red_release(st, o_r, a.run, a.any)
        print("\n".join(lines))
        return rc
    if a.rcmd == "status":
        if a.rcmd != "status":
            print(f"(`red {a.rcmd}` is now `red status`: the tick acts on every red)")
        return red_status(st, getattr(a, "verbose", False))
    if a.rcmd == "auto" and getattr(a, "repo", None):  # ONE REPO's dispatch (the owner, 2026-11-19: "no bitcashier
        # builds"), in the store: the server's tick reads it
        if a.state in ("on", "off"):
            repo_dispatch_set(st, o_r, a.state == "off", claimant())
        offs = repo_dispatch_off_all(st)
        print(f"dispatch off for {o_r} (owner, since {offs[o_r].get('at')})" if o_r in offs else
              f"dispatch on for {o_r}")
        return 0
    if a.rcmd == "auto":
        off = reds_dir("DISPATCH_OFF")
        os.makedirs(reds_dir(), exist_ok=True)
        if a.state == "off":
            open(off, "w").write(ts() + "\n")
        elif a.state == "on" and os.path.exists(off):
            os.unlink(off)
        _blocker_cache.clear()
        b = dispatch_blocker()
        print(f"automatic dispatch: {'ON' if not b else 'OFF -- ' + b}")
        return 0
    if a.rcmd == "owner":
        return red_owner(a)
    if a.rcmd == "fixer":
        return red_fixer(a)
    if a.rcmd == "unblame":
        items = red_targets(st, o_r, a.run, create=False)
        if not items:
            print(f"NOT WITHDRAWN: #{a.run} names no red")
            return 1
        rc = 0
        for it in items:
            ok, line = blame_withdraw(st, it, a.why, scan_repo_for(o_r, None))
            print(line)
            rc = rc or (0 if ok else 1)
        return rc
    if a.rcmd == "dispatch":
        b = dispatch_blocker()
        if b:
            print(f"NOT DISPATCHED: {b}")
            return 2
        items = red_targets(st, o_r, a.run)
        if not items:
            print(f"NOT DISPATCHED: #{a.run} names no red on main")
            return 1
        for it in items:  # its offer at top priority: the dispatcher takes it now
            if it["state"] == "red":
                it = red_offer(st, it, f"{red_what(it['data'])}: dispatched by hand")
            store_it = st.item("red", it["id"])
            st.update_item("red", it["id"], store_it["version"], {"priority": -1, "author_tried": True})
            if it["state"] == "offered":
                red_put_offer(st, st.item("red", it["id"]))
        repo = scan_repo_for(o_r, getattr(a, "repo", None) and os.path.abspath(a.repo))
        lines = red_dispatcher(st, o_r, repo, st.clock())
        print("\n".join(lines) if lines else "NOT DISPATCHED: " + "; ".join(
            (st.offer_of(f"red:{it['id']}") or {}).get("reason") or it["state"] for it in items))
        return 0 if lines else 1
    return 2


def culprit_of(repo, u):
    """The commit that turned this red, when one alone can have: the only commit between the last green run's head and
    the first red run's head of the streak. None when unknown."""
    runs = u.get("runs") or []
    decided = decided_runs(runs)
    streak = streak_runs(runs)
    if not repo or not streak or len(decided) <= len(streak):
        return None
    first, green = streak[-1], decided[len(streak)]
    if green.get("conclusion") != "success":
        return None
    r = git(repo, "rev-list", f"{green.get('headSha')}..{first.get('headSha')}", check=False)
    shas = r.stdout.split() if r.returncode == 0 else []
    return shas[0] if len(shas) == 1 else None


def red_revert(o_r, repo, u, culprit):
    """The revert fallback: a revert of the culprit with `Fixes-Red: N`, landed by a detached `agentctl land` through
    the normal hook (its verdicts reach the inbox). (ok, message)."""
    num = u["run"].get("number")
    if red_owner_only(u.get("repo") or o_r):
        return False, f"no revert in {u.get('repo') or o_r}: its pushes are the owner's (red-repos.json owner-only)"
    _d, clone, _w = waiting_state(repo)
    wt, branch = f"{clone}-wt-revert-{num}", f"revert-{num}"
    if os.path.exists(wt) or git_ok(repo, "rev-parse", "--verify", "-q", f"refs/heads/{branch}"):
        return False, f"revert worktree {wt} or branch {branch} exists already"
    git(repo, "fetch", "--quiet", remote(), "main", check=False)
    if git(repo, "worktree", "add", "-q", "-b", branch, wt, f"{remote()}/main", check=False).returncode != 0:
        return False, "git worktree add failed"
    with WorktreeGuard(wt):  # EVERY EXIT PUTS THE WORKTREE BACK (slots.py) -- the land starts after it, never inside
        r = git(wt, *NO_SIGN, "revert", "--no-edit", culprit, check=False)
        if r.returncode != 0:
            git(wt, "revert", "--abort", check=False)
            return False, f"git revert {culprit[:10]} does not apply ({(r.stderr or r.stdout).strip()[:120]})"
        msg = git(wt, "log", "-1", "--format=%B").strip()
        why = u.get("revert_why") or f"{u['workflow']} #{num} still failed after two attempts (its tier ladder)."
        git(wt, *NO_SIGN, "commit", "--amend", "-q", "-m", f"{msg}\n\nThe revert fallback (agentctl red): {why}"
            f"\n\nFixes-Red: {num}\n"
            + agentctl_revert_fix_replay(f"agentctl's revert fallback of {culprit[:10]}: {u['workflow']} #{num} outlived "
                                         "two fix attempts, so main goes back to the code before it; nothing new to replay")
            + "Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>")
    pid = spawn_land(wt, fixer_env(o_r, num), reds_dir(f"revert-{num}.log"))
    return True, f"landing a revert of {culprit[:10]} from {wt} (pid {pid})"


def spawn_land(wt, env, log):
    """A detached `agentctl land` of wt (the normal hook; its verdicts reach the inbox). Its pid."""
    with open(log, "a") as logf:
        return spawn_detached([sys.executable, TOOL_PATH, "land"], cwd=wt, stdin=subprocess.DEVNULL, stdout=logf,
                              stderr=logf, start_new_session=True, env=env).pid


PATHISH = re.compile(r"(?:\./)?((?:[\w.-]+/)+[\w.-]+)")


def run_workflows():
    """{run number or id: workflow} of every run whose signatures are known."""
    out = {}
    for p in glob.glob(reds_dir("runsig", "*.json")):
        rec = read_record(p)
        if rec:
            for n in (rec.get("run"), rec.get("number")):
                if n is not None:
                    out[str(n)] = rec.get("workflow")
    return out


def fix_between(repo, rec, red_head, green_head, causes, workflows):
    """The commit between a red run's head and the next passing run's head that FIXED this red rather than it passing
    by chance (the owner, 2026-10-27: live drift #2051 was called a flake, and 03d646e63, `Fixes-Red: 2049`, which fixed
    the same failure in another workflow, had landed between): a `Fixes-Red:` naming one of its runs, a run with the
    same cause (any workflow) or a run of the same workflow, or a change to a file its failure names, or to CI's own
    workflows and actions -- a `Fixes-Red:` beside `Red-Unrelated:` names the red only to pass the gate, and fixes
    nothing (`landed_fix_commits`). (sha or None, known): known False when the commits between cannot be read here."""
    if not repo or not red_head or not green_head:
        return None, False
    if red_head == green_head:
        return None, True
    r = git(repo, "log", f"{red_head}..{green_head}", "--format=%x00%H%x09%(trailers:key=Red-Unrelated,valueonly,separator=%x2C)"
            "%x09%(trailers:key=Fixes-Red,valueonly,separator=%x2C)", "--name-only", check=False)
    if r.returncode != 0:
        return None, False
    names = set(map(str, rec.get("runs") or []))
    cause = cause_of(rec.get("job"), rec.get("line"))
    named = {m.group(1) for m in PATHISH.finditer(rec.get("line") or "")}
    for chunk in r.stdout.split("\0")[1:]:
        head, _, files = chunk.partition("\n")
        sha, _, rest = head.partition("\t")
        unrelated, _, vals = rest.partition("\t")
        vals = set() if unrelated.strip() else trailer_values(vals)
        if vals & names or (cause and any(cause in causes.get(v, ()) for v in vals)) \
                or any(workflows.get(v) == rec.get("workflow") for v in vals):
            return sha, True
        for f in files.split():
            if f.startswith((".github/workflows/", ".github/actions/")) or any(f.endswith(n) or n.endswith(f) for n in named):
                return sha, True
    return None, True


# -- an unreadable gate: no dispatch, one UNKNOWN per spell, an infra check
def infra_check(o_r):
    """The gate is read from github.com with gh's own auth: no tunnel, no vault token in the way."""
    return f"read with {GH_CLI} (`gh auth status` names its auth; https://www.githubstatus.com for an outage)"


def gate_unread_spell(o_r, why):
    """The patrol could not read the gate: no dispatch, ONE `UNKNOWN:` condition per spell, with an infra check; the
    gate_read probe (a snapshot written after it opened) clears it."""
    if why is None or condition_item_of(Store(), "gate-unread"):
        return None
    line = (f"UNKNOWN: the red patrol cannot read the gate ({why[:200]}): nothing dispatched, pushes fail closed. "
            f"Infra check: {infra_check(o_r)}")
    c = open_condition("UNKNOWN", line, run_probe("gate_read", {"repo": o_r, "since": time.time()}), [],
                       key="gate-unread", command="red", exit_code=2)
    return line if c else None


def resolve_run(o_r, n):
    """(workflow, run) of the run on main a number or id names, else (None, None)."""
    n = str(n).lstrip("#")
    for key in ("number", "databaseId"):  # a run NUMBER first (the repo's convention), an id only failing that
        for lists in (_gate_lists, _outside_lists):
            for name, runs in lists.items():
                for r in runs:
                    if str(r.get(key)) == n:
                        return name, r
    try:
        # listing: a number not in the newest 200 falls back to its run id (run_id_of_number), then says unknown
        runs = gh_json(o_r, "run", "list", "--branch", "main", "--limit", "200", "--json", RUN_FIELDS)
        r = next((r for r in runs if str(r.get("number")) == n), None)
        if r is None:
            rid = run_id_of_number(o_r, n) if n.isdigit() else None
            r = next((r for r in runs if str(r.get("databaseId")) == str(rid or n)), None)
            if r is None:
                r = gh_json(o_r, "run", "view", str(rid or n), "--json", RUN_FIELDS)
        return r.get("workflowName"), r
    except (Exception, SystemExit) as e:
        err(f"red: could not look up run {n} ({str(e)[:120]}): claimed by its number only")
        return None, None


def red_run_units(o_r, n):
    """The units of one red run (for claim / dispatch / block by run); `local`: main's own local-checks red."""
    if str(n) == "local":
        us = local_units(scan_repo_for(o_r))
        return LOCAL_FLOW, (us[0]["run"] if us else {"number": "local"}), us
    name, r = resolve_run(o_r, n)
    if r is None:
        return name, None, []
    if r.get("status") == "completed" and r.get("conclusion") in RED_CONCLUSIONS:
        runs = _gate_lists.get(name) or _outside_lists.get(name) or [r]
        gate = name in {g for g, _ in red_gate_flows(o_r)}
        try:
            sigs = run_sigs(o_r, r, name)
        except (Exception, SystemExit):
            sigs = []
        return name, r, red_units(o_r, name, r, runs, sigs, gate)
    return name, r, []


# BLOCKED ON OWNER IS FOR THE OWNER'S CAUSES ONLY (the owner, 2026-10-28: "fix this: main #2283 → BLOCKED ON OWNER ...
# root cause and structural fix", "fix structurally so it doesn't false-flag this way" -- an escalation block raised for
# the CdpLaunchSpec crash on #2164 followed the red onto #2283 and hid its live fixer). A code red is never the owner's:
# a block needs an OWNER CAUSE (outside the code, that only the owner can act on), a red whose verdict is not a test
# failure, no live fixer, and the owner's exact command -- checked at the block AND again every tick (block_invalid:
# the tick renews a block lease only while it holds): a block that lost its justification lapses at once.
OWNER_CAUSES = {"external-account": "an external account only the owner holds (a provider dashboard, a plan)",
                "credentials": "credentials or secrets only the owner can issue or rotate",
                "infra-apply": "an infra apply only the owner may run",
                "deploy": "a deploy only the owner may run",
                "billing": "a billing action in an account only the owner holds",
                "dashboard": "an action in a dashboard only the owner can reach"}


# AN OWNER COMMAND THAT DELETES IS SCOPED AND GUARDED (issue 20261110T084827-38f01b, P0). `red block` handed the owner
# `sudo rm -rf /run/github-runner/bitcashier-gh-builder-{1,4,6,7,8,12}/core/core/docker`; he ran it 8 h later, while
# builders 4, 6 and 12 ran Main Pipeline #939, and docker/ vanished under all three jobs (red #939). An owner command
# runs whenever the owner gets to it, so what was idle when the fixer looked may be busy then: a deletion names each
# path (no brace, glob, bare variable or shallow root), carries its own check before it deletes (if / test / [ /
# pgrep), and one inside a CI runner's workspace skips a slot whose Runner.Worker runs. `red block` refuses anything
# else, and a block raised before this rule carrying one lapses at the next tick (block_invalid).
OWNER_CMD_DELETES = re.compile(r"^(rm|rmdir|unlink|shred|truncate)$")
OWNER_CMD_GUARDS = {"if", "test", "[", "[[", "pgrep", "while", "until"}
OWNER_CMD_RUNNER_WS = re.compile(r"github-runner|actions-runner|/_work(/|$)")
OWNER_CMD_SPLIT = re.compile(r"\s*(?:&&|\|\||;|\||\n|\bdo\b|\bthen\b|\belse\b)\s*")


def owner_cmd_target_unscoped(arg):
    """Why one path a deletion names is not one named path, or None."""
    if re.search(r"[*?\[\]{}`]|\$\(", arg):
        return f"{arg!r} expands (a glob, brace or substitution): name each path"
    if re.fullmatch(r"\$\{?\w+\}?/?", arg) or arg in (".", "..", "-"):
        return f"{arg!r} names no path"
    literal = re.sub(r"\$\{?\w+\}?", "x", arg)
    if literal.startswith(("~", "$HOME", "/")) and len([p for p in literal.lstrip("~").split("/") if p]) < 3:
        return f"{arg!r} is too near the root"
    return None


def owner_cmd_unsafe(cmd):
    """Why the owner must not be handed this command as it stands (it deletes unscoped or unguarded), or None."""
    flat = str(cmd or "").replace("'", " ").replace('"', " ")
    segments = [s.split() for s in OWNER_CMD_SPLIT.split(flat)]
    deleting, guarded = [], False
    for words in segments:
        verbs = [w for w in words if w not in ("sudo", "exec", "command", "nice", "ionice") and not w.startswith("-")]
        if not verbs:
            continue
        head = verbs[0]
        guarded = guarded or bool(OWNER_CMD_GUARDS & {words[0], head})
        if head in ("docker", "podman") and "prune" in words or head == "find" and "-delete" in words or \
                head == "kubectl" and "delete" in words and ("--all" in words or "-A" in words) or \
                re.search(r"\bdrop(Database|Collection)\b|\b(DROP|TRUNCATE)\s+(TABLE|DATABASE)\b", " ".join(words)):
            return (f"unscoped: `{' '.join(words)[:80]}` deletes whatever it matches, not named paths -- hand the owner "
                    "a command naming each thing it removes")
        if OWNER_CMD_DELETES.match(head) or head == "git" and ("clean" in words or "--hard" in words) or \
                head in ("docker", "podman", "kubectl") and ({"rm", "rmi", "delete"} & set(words)):
            targets = verbs[1:] if OWNER_CMD_DELETES.match(head) else []
            for arg in targets:
                why = owner_cmd_target_unscoped(arg)
                if why:
                    return f"unscoped: {why}"
            deleting.append((targets, guarded))
    for targets, was_guarded in deleting:
        if any(OWNER_CMD_RUNNER_WS.search(t) for t in targets) and "Runner.Worker" not in flat:
            return ("it deletes inside a CI runner's workspace without skipping a busy slot: check each slot's "
                    "Runner.Worker first (`pgrep -f \"<slot>/.*Runner.Worker\" >/dev/null || rm -rf <its path>`), as the "
                    "owner may run it hours later, mid-job")
        if not was_guarded:
            return ("unguarded: it deletes with no check before it (if / test / [ / pgrep): the owner runs it whenever "
                    "he gets to it, so it must check what it removes is still unused then")
    return None


def owner_details(o_r, n, name, units, cmd, words, owner_cause):
    """The owner's whole picture, one line each part: what fails, what was tried, why only the owner, the command, how
    it clears (the owner, 2026-10-28: "more details in the future pls")."""
    u = units[0] if units else {}
    failing = f"{u.get('job') or '?'}: {str(u.get('line') or 'no error line read')[:160]}"
    try:
        its = red_item_of_run(Store(), n, name)
    except Exception:
        its = []
    hist = [h for it in its for h in it["data"].get("history") or []]
    verdicts = [f"{h.get('holder')}: {str(h.get('outcome'))[:90]}" for h in hist]
    refuted = sorted({x[:9] for it in its for x in it["data"].get("refuted") or []})
    tried = (f"{len(refuted)} fix(es) refuted ({', '.join(refuted)})" if refuted else "no fix landed") + \
        (f"; fixers: {' | '.join(verdicts[-4:])}" if verdicts else "; no fixer dispatched")
    return (f"failing: {failing}. tried: {tried}. only the owner: {OWNER_CAUSES[owner_cause]}. run: `! {cmd}`. "
            f"auto-clears when {words}")


# -- owner blocks are leases (design §5.3 blocked-external; phase 1 step 11)
# A block is a lease `block/<key>` (one red) or `block/cause/<name>` (ONE external cause holding every red it explains),
# holder `owner:<owner cause>`, TTL 10 min, max 7 d. Only the tick renews it, and only while its probe still fails AND
# its justification holds for a red it holds (an owner cause, the owner's exact command, never a TEST FAILURE, no live
# fixer). Its probe passing, the justification gone, or the max hold: it lapses, and its reds go on (verify / offered).
BLOCK_TTL, BLOCK_MAX = 10 * 60, 7 * 86400
BLOCK_PROBE_EVERY = 60


def block_lease(key=None, cause=None):
    return ("block/cause/" + re.sub(r'[^\w.-]+', '_', cause)) if cause else f"block/{key}"


def red_block_of(it, store=None):
    """{name, text, cause, until, meta} of the live block lease holding this red, or None."""
    st = store or Store()
    d = it["data"]
    for row in st.list("block/"):
        if lease_is_live(row, st.clock()):
            m = lease_meta(row)
            if d.get("key") in (m.get("keys") or []):
                return {"name": row["name"], "text": m.get("text") or "BLOCKED ON OWNER", "cause": m.get("cause"),
                        "until": m.get("until"), "meta": m}
    return None


def block_invalid(store, it, m, t):
    """Why this block no longer justifies holding red `it`, or None (checked at the block AND every tick)."""
    d = it["data"]
    if m.get("vault"):  # it waits on a vault (red_vault_block): the owner's `decide vault-<ns>`, any red kind
        row = store.get(red_lease(d["key"]))
        return f"a live fixer is on it: {row['holder']}" if lease_is_live(row, t) else None
    if m.get("crash_loop") or m.get("decision"):  # the fixers crash-looped on it (red_crash_loop), or it waits on the
        # owner's decision (red_decision_block): the harness, or the owner's call, never the code, holds it
        if m.get("decision") and ask_refusal(ask_category(m), m.get("decide") or m.get("question")):
            return "its question names no owner-only category (ask.py OWNER_ONLY): its fixers proceed"
        if m.get("crash_loop") and not m.get("harness"):  # an owner block of a crash loop from before 2026-11-19
            return "a fixer crash loop is a harness fault, agentctl's own, never the owner's"
        iss = store.item("issue", m["issue"]) if m.get("issue") else None
        if iss and iss["state"] == "closed":  # A HARNESS FAULT FIXED: its maintainer closed the issue (redcrash.py)
            return f"its harness issue {m['issue']} is closed ({str((iss.get('data') or {}).get('note') or '')[:120]})"
        row = store.get(red_lease(d["key"]))
        return f"a live fixer is on it: {row['holder']}" if lease_is_live(row, t) else None
    if m.get("wait_cause") in WAIT_CAUSES:  # an external wait (a spent quota): no owner cause, no owner command
        if d.get("kind") == "test":
            return "its red is a TEST FAILURE: a code red is a fixer's"
        row = store.get(red_lease(d["key"]))
        return f"a live fixer is on it: {row['holder']}" if lease_is_live(row, t) else None
    if QUOTA_LOG.search(str(d.get("line") or "")) or QUOTA_LOG.search(str(m.get("details") or m.get("text") or "")):
        return "a spent quota resets on its own: an external wait, never the owner's"
    if m.get("owner_cause") not in OWNER_CAUSES:
        return f"no owner cause (one of {', '.join(OWNER_CAUSES)}): only the owner's causes may block on the owner"
    if not str(m.get("cmd") or "").strip():
        return "no owner command"
    unsafe = owner_cmd_unsafe(m.get("cmd"))
    if unsafe:
        return f"its owner command is unsafe: {unsafe}"
    if d.get("kind") == "test":
        return "its red is a TEST FAILURE: a code red is never the owner's"
    row = store.get(red_lease(d["key"]))
    if lease_is_live(row, t):
        return f"a live fixer is on it: {row['holder']}"
    return None


def red_renew_blocks(store, t):
    """The tick's look at every live block: its probe (at most every BLOCK_PROBE_EVERY s) and its justification per red.
    Still failing and still justified: renewed (the probe's fresh failing result is its progress). Otherwise it lapses
    now (revoked, the reason in its outcome) or a red that lost its justification is detached from it."""
    out = []
    active = {it["data"]["key"]: it for it in red_active(store).values()}
    for row in store.list("block/"):
        if not lease_is_live(row, t):
            continue
        m = lease_meta(row)
        keys = [k for k in m.get("keys") or [] if k in active]
        dropped = [(k, block_invalid(store, active[k], m, t)) for k in keys]
        dropped = [(k, why) for k, why in dropped if why]
        keep = [k for k in keys if k not in {k_ for k_, _w in dropped}]
        for k, why in dropped:
            out.append(f"dropped the owner block of {red_what(active[k]['data'])}: {why}")
        if not keep:
            why = dropped[0][1] if dropped else "it holds no open red"
            store.revoke(row["name"], f"INVALID: {why}")
            out.append(f"block {row['name']} lapsed: {why}")
            continue
        if keep != m.get("keys"):
            store.set_meta(row["name"], row["epoch"], row["secret"], {"keys": keep})
        if t - float(m.get("probed_at") or 0) < BLOCK_PROBE_EVERY:
            store.renew(row["name"], row["epoch"], row["secret"], f"held@{int(t // BLOCK_PROBE_EVERY)}", BLOCK_TTL)
            continue
        pr = m.get("probe") or {}
        r = run_probe(pr.get("kind"), pr.get("args")) if pr.get("kind") in PROBES else None
        store.set_meta(row["name"], row["epoch"], row["secret"], {"probed_at": t, "last": r.detail if r else "no probe"})
        if r is not None and r.ok:
            store.revoke(row["name"], f"PROBE PASSED: {r.detail}")
            out.append(f"RECOVERED: {m.get('until')} ({r.detail}): the block {row['name']} lapsed")
            line = f"RECOVERED: {m.get('until')} ({r.detail}): {m.get('text', '')[:200]}"
            inbox_write("recover", line, [line], [], 0)
            continue
        store.renew(row["name"], row["epoch"], row["secret"], f"{r.detail if r else 'cannot tell'}@{int(t)}", BLOCK_TTL)
    return out


# A VAULT ASK IS ONE OWNER ITEM PER VAULT, ANSWERED BY ONE LINE (2026-11-16, Order independence #77 [ios]: its fixer's
# tested fix waited on commit signing while the `movies` vault was locked; it ended BLOCKED ON OWNER five turns in four
# minutes, then sat as an owner decision until the owner hand-ran `agentctl decide` at 05:01Z). A fixer's ask whose one
# condition is a vault (`secrets unlock <ns>` in a BLOCKED ON OWNER verdict or a DIAGNOSED decide) puts the red, whatever
# its kind, on ONE lease per vault, block/cause/vault-<ns>: the owner is told once for every red it holds (ask.py), with
# the one line that resumes them all, `! ~/bin/agentctl decide vault-<ns> here` (red_vault_decide).
# NO UNLOCK RESUMES THEM BY ITSELF (review round 3, 2026-11-16). An unlock is pinned to ONE session anchor, the caller's
# nearest Claude Code ancestor, and signing asks Touch ID once per anchor (~/projects/secrets README, "Session pinning"):
# the owner's `secrets unlock` in his own session never lets a fixer sign. The secrets-log watch that resumed the reds on
# any unlock (2a8139c) read only "the owner touched the vault somewhere", never fired once, and would have had a fixer
# raise a Touch ID prompt minutes later that the owner had not asked for -- the prompt secrets' README says never to
# approve. `decide vault-<ns>` says what matters: the owner is at the machine now. Each resumed fixer signs a probe
# commit FIRST (its own Touch ID prompt, raised while he is there) and ends BLOCKED ON OWNER at once if that fails: back
# on the vault's one lease, told once again.
# `decide vault-<ns>` too (2026-11-22, Order independence #105: turns 69-71 ended `BLOCKED ON OWNER: ~/bin/agentctl
# decide vault-movies "here"`, the cmd the owner is told, not `secrets unlock`; unmatched, each was re-offered ~2 min later)
VAULT_ASK = re.compile(r"secrets\s+unlock\s+([A-Za-z0-9_-]+)|decide\s+vault-([A-Za-z0-9_-]+)")
VAULT_TARGET = re.compile(r"vault-([A-Za-z0-9_-]+)")


def vault_ask_ns(text):
    """The vault an owner ask waits on (`secrets unlock <ns>` in it), or None."""
    m = VAULT_ASK.search(str(text or ""))
    return (m.group(1) or m.group(2)) if m else None


def vault_decide_cmd(ns):
    return decide_cmd(f"vault-{ns}", "here")


def red_vault_resume(store, it, ns, answer, t):
    """The owner is at the machine (`decide vault-<ns>`): red `it`, waiting on vault ns, offered again as a decide
    would, its fixer briefed to prove its OWN session can sign before anything else."""
    d = it["data"]
    q = (d.get("ask") or {}).get("q") or f"agents' work waits on the {ns} vault"
    # a repo whose main is unsigned (fixer_env commits unsigned there): no signing probe, which would itself sign and
    # raise a prompt for nothing; the vault was for a secret, so the probe is the command that needed it
    probe = ("run `git commit-tree -S HEAD^{tree} -m 'signing probe' < /dev/null` in your worktree"
             if repo_signs(d.get("repo") or RED_PRIMARY) else
             "run again the one command that needed the vault (your commits here are unsigned: never sign one)")
    dec = {"answer": f"the owner is at the machine (`decide vault-{ns}`: {answer}). No other session's unlock lets "
                     "yours use the vault: your first use raises a Touch ID prompt for YOUR session, which the owner "
                     f"answers now. So FIRST {probe}; if it fails, end `BLOCKED ON OWNER: ! secrets unlock {ns}` at "
                     "once. Then go on with the fix and land it.", "question": str(q)[:600], "at": t,
           "status": "in-progress", "category": "vault"}
    store.close_offer(f"red:{it['id']}", "resumed")
    red_offer(store, it, f"{red_what(d)}: the owner resumed it (the {ns} vault): offered again",
              data={"cause": None, "block": None, "ask": None, "decision": dec, "dispatch_after": 0})
    return [f"resumed {red_what(d)} (the {ns} vault)"]


def red_vault_decide(store, ns, answer):
    """`agentctl decide vault-<ns> "<answer>"`: every red on vault ns's lease resumed (red_vault_resume), the lease
    released. (rc, lines)."""
    lname, t = block_lease(cause=f"vault-{ns}"), store.clock()
    row = store.get(lname)
    if not lease_is_live(row, t) or lease_meta(row).get("vault") != ns:
        return 1, [f"NOT DECIDED: no red waits on the {ns} vault (`~/bin/agentctl red status`)"]
    keys = lease_meta(row).get("keys") or []
    # RESUMED FIRST, RELEASED AFTER (2026-11-19 03:01Z, core #1053: the owner's `decide vault-bitcashier here` revoked
    # the lease, the tick's invariant saw the red blocked-external with no block in that same second and re-offered it
    # bare -- and the decide, finding no red still blocked, said "NOT DECIDED" while its red went on without the
    # signing brief). While the lease is live no invariant touches its reds; one the tick re-offered meanwhile (offered,
    # no decision of its own) is still the owner's answer's: resumed too.
    out = []
    for key in keys:
        for _try in range(10):  # StaleVersion: the store ticks meanwhile -- re-read and re-apply
            it = red_of_key(store, key)
            if not it or it["state"] not in ("blocked-external", "offered") or \
                    (it["state"] == "offered" and red_decision(it["data"])):
                break
            try:
                out += red_vault_resume(store, it, ns, answer, t)
                break
            except StaleVersion:
                continue
    store.revoke(lname, f"RELEASED: DECIDED by the owner: {answer[:200]}")
    if not out:
        return 1, [f"NOT DECIDED: the {ns} vault's lease held no red still waiting on it (released)"]
    return 0, [f"DECIDED: {line} -- IN PROGRESS: each fixer's first signature asks Touch ID for its own session: answer "
               "it; if the action fails you are told" for line in out]


def red_vault_block(store, it, upd, ns, why, t):
    """The red waits on vault ns: it joins the vault's ONE lease (opened when none is live). Moved quietly: the tick's
    red_owner_reminders tells the owner once, with `decide vault-<ns>`."""
    d = {**it["data"], **upd}
    key = d["key"]
    lname = block_lease(cause=f"vault-{ns}")
    row = store.get(lname)
    if lease_is_live(row, t) and lease_meta(row).get("vault") == ns:
        store.set_meta(lname, row["epoch"], row["secret"], {"keys": sorted(set(lease_meta(row).get("keys") or []) | {key})})
    else:
        got = store.acquire(lname, "owner:vault", BLOCK_TTL, BLOCK_MAX, progress=f"locked@{int(t)}",
                            meta={"kind": "block", "vault": ns, "category": "vault", "cause": f"vault-{ns}",
                                  "cmd": vault_decide_cmd(ns),
                                  "question": f"agents' work waits on the {ns} vault (commit signing, its secrets) and "
                                              "only you can answer its Touch ID prompts", "keys": [key], "probed_at": t,
                                  "text": f"BLOCKED: the {ns} vault is locked for its fixers (`decide vault-{ns} here`)",
                                  "details": " ".join(str(why).split())[:400]})
        if "held" in got:  # somebody else's lease by that name: never this red's to take over
            red_offer(store, it, f"{red_what(d)}: {lname} is held by {got['held'].get('holder')}: offered again", data=upd)
            return [f"re-offered {red_what(d)} ({lname} held)"]
    store.close_offer(f"red:{it['id']}", "blocked")
    red_move(store, it, "blocked-external", f"{red_what(d)}: waits on the {ns} vault (the owner's `decide vault-{ns}`)",
             data={**upd, "cause": f"vault-{ns}", "block": lname})
    return [f"{red_what(d)}: waits on the {ns} vault"]


# A SPENT QUOTA IS AN EXTERNAL WAIT, NOT THE OWNER'S (the owner's coordinator, 2026-10-29: Record scrape fixtures #2426
# sat BLOCKED ON OWNER for OMDb's `HTTP 401: Request limit reached` -- a daily quota, which resets on its own; only an
# invalid key is the owner's). A red whose error line says a quota is spent is held by a WAIT lease (block/cause/quota-
# <service>, holder `wait:quota`), never offered to a fixer and never an owner block. Its probe re-checks the key the
# cheap way the workflow itself does: the red's workflow dispatched on main once the quota's window has reset (OMDb's
# daily window: midnight UTC), whose first job (the recording preflight: one OMDb call) fails within minutes if the key
# is still refused; the wait clears when a run of the workflow created after the block got past that job.
WAIT_CAUSES = {"quota": "an outside service's quota for the run's key, spent; it resets on its own"}
QUOTA_RESET_HOUR_UTC = 0


def quota_service(line):
    m = re.search(r"::error[^:]*::(\w+) refuses the key", line or "") or re.search(r"\b(OMDb|TMDB|Decodo)\b", line or "", re.I)
    return m.group(1) if m else "the service"


def next_quota_reset(after):
    """The first quota window reset (QUOTA_RESET_HOUR_UTC, daily) after unix time `after`."""
    d = dt.datetime.fromtimestamp(after, dt.timezone.utc)
    r = d.replace(hour=QUOTA_RESET_HOUR_UTC, minute=0, second=0, microsecond=0)
    if r <= d:
        r += dt.timedelta(days=1)
    return r.timestamp()


def probe_quota_reset(a):
    """The key accepted again: a run of a['workflow'] on main created after a['since'] got past its first job
    (a['job'], the preflight) -- or the whole run is green."""
    o_r = a.get("repo") or SHORT["movies"]
    # listing: runs created since the block; none listed keeps the wait (a probe that cannot tell never clears it)
    runs = gh_json(o_r, "run", "list", "--workflow", a["workflow"], "--branch", "main", "--json", RUN_FIELDS, "-L", "10")
    since = float(a.get("since") or 0)
    for r in runs or []:
        try:
            if parse_time(r["createdAt"]).timestamp() <= since:
                continue
        except (KeyError, ValueError, TypeError):
            continue
        if r.get("conclusion") == "success":
            return True, f"{a['workflow']} #{r.get('number')} green"
        jobs = run_jobs(o_r, r) or []
        first = next((j for j in jobs if j.get("name") == a.get("job")), None)
        if first and first.get("conclusion") == "success":
            return True, f"{a['workflow']} #{r.get('number')}: {a.get('job')} passed (the key is accepted again)"
        if first and first.get("conclusion") in ("failure", "timed_out"):
            return False, f"{a['workflow']} #{r.get('number')}: {a.get('job')} failed again (still spent)"
    return False, (f"no run of {a['workflow']} since the quota was spent; the next window opens "
                   f"{time.strftime('%Y-%m-%d %H:%M', time.localtime(next_quota_reset(since)))}")


def red_quota_waits(store, o_r, repo, t):
    """Each open red whose line says a quota is spent, held by nobody (no fixer lease, no block): put on the WAIT lease
    of its service; each wait past its window's reset with no run since: its workflow dispatched once per window."""
    out = []
    for it in red_active_of(store, o_r).values():
        d = it["data"]
        if not QUOTA_LOG.search(str(d.get("line") or "")) or d.get("kind") == "test":
            continue
        if lease_is_live(store.get(red_lease(d["key"])), t) or red_block_of(it, store):
            continue
        svc = quota_service(d.get("line"))
        lname = block_lease(cause=f"quota-{svc.lower()}")
        row = store.get(lname)
        if lease_is_live(row, t):
            m = lease_meta(row)
            store.set_meta(lname, row["epoch"], row["secret"], {"keys": sorted(set(m.get("keys") or []) | {d["key"]})})
        else:
            probe = {"kind": "quota_reset", "args": {"repo": o_r, "workflow": d["workflow"], "since": t, "job": d.get("job")
                                                     if d.get("job") else "preflight"}}
            until = f"{svc}'s quota resets and {d['workflow']}'s {probe['args']['job']} passes"
            store.acquire(lname, "wait:quota", BLOCK_TTL, BLOCK_MAX, progress=f"spent@{int(t)}",
                          meta={"kind": "block", "wait_cause": "quota", "cause": f"quota-{svc.lower()}", "probe": probe,
                                "until": until, "keys": [d["key"]], "probed_at": t, "redispatch": d["workflow"],
                                "text": f"WAITING: {svc}'s quota for the run's key is spent; it resets on its own "
                                        f"(auto-clears when {until})"})
            line = f"RED WAITING: {red_what(d)}: {svc}'s quota is spent -- an external wait, not the owner's; " \
                   f"{d['workflow']} is dispatched again after the reset"
            inbox_write("red", line, [line], [], 0)
        cur = store.item("red", it["id"])
        store.update_item("red", it["id"], cur["version"], {"block": lname})
        out.append(f"{red_what(d)}: waits on {svc}'s quota ({lname})")
    for row in store.list("block/cause/quota-"):
        m = lease_meta(row)
        if not lease_is_live(row, t) or not m.get("redispatch"):
            continue
        since = float((m.get("probe") or {}).get("args", {}).get("since") or t)
        last = float(m.get("dispatched_at") or since)
        if t >= next_quota_reset(last) + 300:
            msg = red_verify_dispatch(o_r, repo, m["redispatch"], "quota-reset")
            out.append(f"{m['redispatch']} after {m.get('cause')}'s reset (re-checks the key): {msg}")
            store.set_meta(row["name"], row["epoch"], row["secret"], {"dispatched_at": t})
    return out


def red_owner_push(store, o_r, target, note="", wt=None):
    """(rc, lines) of `red owner-push`: an owner-only repo's fix (red-repos.json; infra, whose push is a deploy), prepared
    on the worktree's local branch, filed with the owner. The red goes to fix-pending with a fix of status `owner` (its
    branch, head and the owner's one-liner) and one escalation reaches the inbox with the command; a green gate run
    verifies it, and unproven after RED_FIX_GIVEUP it is offered again (red_fix_pending). Never pushes."""
    if not red_owner_only(o_r):
        return 2, [f"NOT FILED: {o_r} is not owner-only (red-repos.json): land the fix with `~/bin/agentctl land`"]
    here = safe_cwd()
    wt = os.path.realpath(wt or worktree_of(here) or here)
    if not os.path.isdir(wt) or root_checkout(wt, o_r):
        return 2, [f"NOT FILED: {wt.replace(HOME, '~')} is not a worktree of your own: `--worktree <yours>`"]
    try:
        if slug(wt) != o_r:
            return 2, [f"NOT FILED: {wt.replace(HOME, '~')} is a clone of {slug(wt)}, not {o_r}"]
    except (Exception, SystemExit) as e:
        return 2, [f"NOT FILED: {wt.replace(HOME, '~')} is no clone of {o_r} ({str(e)[:120]})"]
    if git(wt, "status", "--porcelain", "--untracked-files=no", check=False).stdout.strip():
        return 1, ["NOT FILED: the worktree has uncommitted changes: commit each step first"]
    git(wt, "fetch", "--quiet", remote(), "main", check=False)
    ahead = unlanded_commits(wt)
    if not ahead:
        return 1, [f"NOT FILED: nothing in {wt.replace(HOME, '~')} that is not on {remote()}/main"]
    items = red_targets(store, o_r, target, create=False)
    if not items:
        return 1, [f"NOT FILED: #{target} names no open red of {o_r}"]
    head = git(wt, "rev-parse", "HEAD").strip()
    branch = git(wt, "rev-parse", "--abbrev-ref", "HEAD", check=False).stdout.strip() or head[:10]
    tw = wt.replace(HOME, "~")
    cmd = f"git -C {tw} pull --rebase {remote()} main && git -C {tw} push {remote()} HEAD:main"
    t = store.clock()
    what = ", ".join(red_what(it["data"]) for it in items)
    line = (f"RED OWNER PUSH: {what}: a fix is ready on {branch} ({len(ahead)} commit(s), "
            f"{', '.join(s[:10] for s, _ in ahead[:6])}) in {wt.replace(HOME, '~')}"
            + (f" -- {note}" if note else "") + f". {o_r} is owner-only (a push is a deploy): `! {cmd}`")
    for it in items:
        cur = store.item("red", it["id"])
        red_move(store, cur, "fix-pending", line, data={"fix": {"status": "owner", "entry": f"owner-push:{branch}",
                                                                "sha": head, "at": t, "worktree": wt, "cmd": cmd,
                                                                "note": note, "verify_since": t}})
    inbox_write("red", line, [line], [], 1, cwd=wt)
    return 0, [line, f"BLOCKED ON OWNER: {cmd}"]


# A BLOCK NOBODY CAN SEE CLEAR (2026-11-07: Verify scrape recording #14's reds read "BLOCKED ON OWNER: ... re-pin movies
# runner scheduler to 362196b15a" hours after the owner had re-pinned, and past it -- ci-runner ran a later scheduler).
# That block was a turn's VERDICT, not a block lease: no probe could clear it, so it stayed the red's last word. A
# verdict with no live block now reads with its age and how to strike it (block_verdict_note), and `red unblock` strikes
# it -- or ends a live block lease (RELEASED: its reds go on at the next tick, re-offered, never escalated) -- leaving
# the reason in the red's history.
def red_blocked_verdict(d):
    """The red's last word when it is a BLOCKED ON OWNER verdict (its history's newest entry), else None."""
    last = (d.get("history") or [{}])[-1]
    return last if str(last.get("outcome") or "").startswith("BLOCKED ON OWNER") else None


def block_verdict_note(it, t, store=None):
    """A BLOCKED ON OWNER verdict that no live block lease holds, shown as what it is: an old word, its age, and how to
    strike it; None when the red's last word is no such verdict or a block holds it."""
    d = it["data"]
    last = red_blocked_verdict(d)
    if not last or red_block_of(it, store):
        return None
    at = iso_epoch(last.get("at"))
    age = t - at if at is not None else None
    said = f"said {fmt_age(age)} ago" if age is not None else f"said at {last.get('at')}"
    return (f"{str(last.get('outcome'))[:300]} -- {said}, no block holds it (no probe clears it): if it is done or "
            f"superseded, `agentctl red unblock {d.get('key')} --why \"<what changed>\"`")


def red_unblock(store, o_r, target, why, who=None):
    """(rc, lines) of `red unblock`: the reds a target names are no longer the owner's -- a live block lease holding
    them is ended (RELEASED: the tick offers them again, never escalated) and an UNBLOCKED entry ends their history, so a
    stale BLOCKED ON OWNER verdict is no longer their last word."""
    if not str(why or "").strip():
        return 2, ["NOT UNBLOCKED: --why \"<what changed>\" (the owner did it, it is superseded by ...)"]
    items = red_targets(store, o_r, target, create=False)
    if not items:
        return 1, [f"NOT UNBLOCKED: #{target} names no open red"]
    who = who or claimant(safe_cwd())
    t = store.clock()
    lines = []
    for it in items:
        d = it["data"]
        blk = red_block_of(it, store)
        if blk:
            row = store.get(blk["name"])
            keep = [k for k in lease_meta(row).get("keys") or [] if k != d.get("key")]
            if keep:  # an outage's ONE lease holding other reds too: only this red leaves it
                store.set_meta(row["name"], row["epoch"], row["secret"], {"keys": keep})
            else:
                store.revoke(row["name"], f"RELEASED: unblocked by {who}: {why}")
        elif not red_blocked_verdict(d):
            lines.append(f"{red_what(d)} ({d.get('key')}): not blocked on the owner -- left as it is")
            continue
        cur = store.item("red", it["id"])
        h = {"at": iso_at(t), "holder": who, "outcome": f"UNBLOCKED: {why}"}
        store.update_item("red", it["id"], cur["version"], {"history": (cur["data"].get("history") or []) + [h]})
        lines.append(f"unblocked {red_what(d)} ({d.get('key')})" + (f": ended {blk['name']}" if blk else
                                                                    ": its BLOCKED ON OWNER verdict struck") + f" -- {why}")
    return 0, lines


def red_block(o_r, a):
    """`red block`: the red waits on the OWNER, for an owner cause only, as a block lease the tick renews while its
    probe -- named (--until-green / --until-http / --until-tcp), an open cause's, or the red's own workflow going green
    -- still fails and its justification holds. --cause NAME: ONE lease for that outage, every red it explains attached."""
    st = Store()
    n = str(a.run).lstrip("#")
    name, units = None, []
    known = red_targets(st, o_r, n, create=False)  # a red key, or a run the tick has seen: as `red claim` finds it
    try:
        name, _r, units = red_run_units(o_r, n)
    except (Exception, SystemExit):
        if not known:
            name, _r = resolve_run(o_r, n)
    if known and name and name != known[0]["data"].get("workflow"):  # a bare NUMBER another workflow's run also has
        name, units = None, []  # (issue 20261107T072345-7bca40: `red block 14` read Report run timings #14): the red's
    if known and not units:  # issue 20261119T030011-293b9a: a key, or a run whose units no longer read (a green rerun)
        name = name or known[0]["data"].get("workflow")
        units = [{"kind": it["data"].get("kind"), "line": it["data"].get("line"), "job": it["data"].get("job")}
                 for it in known]
    cause = getattr(a, "cause", None)
    lname = block_lease(cause=cause) if cause else None
    old = st.get(lname) if lname else None
    old = old if lease_is_live(old, st.clock()) else None
    om = lease_meta(old)
    owner_cause = getattr(a, "owner_cause", None) or om.get("owner_cause")
    vault = vault_ask_ns(a.owner_cmd)  # a locked vault: no owner cause, any red kind (red_vault_block, below)
    if owner_cause not in OWNER_CAUSES and not vault:
        print(f"NOT BLOCKED: a block on the owner needs --owner-cause, one of: {', '.join(OWNER_CAUSES)} (a cause outside "
              "the code that only the owner can act on). A code red is yours: claim it and fix it.")
        return 2
    if any(QUOTA_LOG.search(str(x.get("line") or "")) for x in units):
        print(f"NOT BLOCKED: #{n}'s key hit a spent quota ({str(units[0].get('line'))[:120]}): it resets on its own, so "
              "it is no owner action -- the tick holds the red as an external wait and re-runs its workflow after the "
              "reset (`agentctl red status`)")
        return 2
    if any(x.get("kind") == "test" for x in units) and not vault:
        print(f"NOT BLOCKED: #{n} is a TEST FAILURE ({units[0].get('job')}): a code red is never the owner's -- "
              f"`~/bin/agentctl red claim {n}` and fix it")
        return 2
    unsafe = owner_cmd_unsafe(a.owner_cmd or om.get("cmd"))
    if unsafe:
        print(f"NOT BLOCKED: the owner command is unsafe -- {unsafe}. A command that deletes names each path and "
              "checks, inside the command, that it is still unused when the owner runs it.")
        return 2
    items = known or (red_targets(st, o_r, n) if units else [])
    own = read_record(own_fixer_record_path() or "") or {}
    handed = [] if not items else [red_lease(it["data"]["key"]) for it in items]
    fam = own.get("family")  # A FAMILY OWNER blocks a red it holds (issues 20261106T210149-680984,
    # 20261119T015532-983695: "a live fixer is on #1005: laptop:owner:convergence" -- the owner itself)
    handed = [x for x in handed if lease_is_live(st.get(x), st.clock()) and (
        x == own.get("lease") and st.get(x)["epoch"] == own.get("epoch")
        or fam and st.get(x)["holder"] == owner_holder(fam) and lease_meta(st.get(x)).get("family") == fam)]
    live = [st.get(red_lease(it["data"]["key"]))["holder"] for it in items
            if red_lease(it["data"]["key"]) not in handed and lease_is_live(st.get(red_lease(it["data"]["key"])), st.clock())]
    if live:
        print(f"NOT BLOCKED: a live fixer is on #{n}: {live[0]}")
        return 1
    ns = vault
    if ns and items:  # A LOCKED VAULT IS ONE WAIT, never an owner cause (issue 20261119T015532-983695): the red joins the
        # vault's lease (red_vault_block), which the owner's `decide vault-<ns>` lifts for every red on it
        t = st.clock()
        for it in items:
            for line in red_vault_block(st, st.item("red", it["id"]), {}, ns, f"`red block`: {a.owner_cmd}", t):
                print(line)
        for x in handed:
            st.revoke(x, f"BLOCKED ON OWNER (handed over by its fixer): the {ns} vault is locked")
        return 0
    if old and not (a.until_green or a.until_http or a.until_tcp):  # attach to the outage's ONE lease: its probe
        iprobe = run_probe(om["probe"]["kind"], om["probe"]["args"])
    elif cause and not (a.until_green or a.until_http or a.until_tcp):
        print(f"NOT BLOCKED: no open cause {cause!r} to attach to; open it with its probe: --cause {cause} "
              "--until-green <workflow> | --until-http <url> | --until-tcp <host:port>")
        return 2
    elif a.until_green:
        iprobe = run_probe("workflow_green", {"repo": o_r, "workflow": a.until_green})
    elif a.until_http:
        iprobe = run_probe("http", {"url": a.until_http})
    elif a.until_tcp:
        host, _, port = a.until_tcp.rpartition(":")
        iprobe = run_probe("tcp", {"host": host, "port": int(port)})
    elif name:
        iprobe = run_probe("workflow_green", {"repo": o_r, "workflow": name})
    else:
        print(f"NOT BLOCKED: #{n}'s workflow is unknown, so no probe can clear the block; name one: --until-green "
              "<workflow> | --until-http <url> | --until-tcp <host:port>")
        return 2
    words = probe_words(iprobe.kind, iprobe.args)
    if iprobe.ok:
        print(f"NOT BLOCKED: {words} already ({iprobe.detail}): nothing waits on the owner -- `red claim {n}` and fix it")
        return 1
    if not items:
        print(f"NOT BLOCKED: #{n} names no red on main")
        return 1
    details = owner_details(o_r, n, name, units, a.owner_cmd or om.get("cmd"), words, owner_cause)
    keys = [it["data"]["key"] for it in items]
    what = ", ".join(red_what(it["data"]) for it in items)
    text = (f"BLOCKED: {cause} down (auto-clears when {words})" if cause else
            f"BLOCKED ON OWNER: {details}")
    verify = not (iprobe.kind == "workflow_green" and iprobe.args.get("workflow") == name)
    if old:
        st.set_meta(lname, old["epoch"], old["secret"], {"keys": sorted(set(om.get("keys") or []) | set(keys))})
        print(f"attached {what} to {cause!r}: {om.get('text')} ({iprobe.detail}; `agentctl red status`)")
    else:
        lname = lname or block_lease(keys[0])
        r = st.acquire(lname, f"owner:{owner_cause}", BLOCK_TTL, BLOCK_MAX, progress=f"{iprobe.detail}@{int(time.time())}",
                       meta={"kind": "block", "owner_cause": owner_cause, "category": OWNER_CAUSE_CATEGORY[owner_cause],
                             "cmd": a.owner_cmd, "cause": cause,
                             "question": f"run `! {a.owner_cmd}` ({OWNER_CAUSES[owner_cause]})" +
                                         (f" -- {a.note}" if getattr(a, "note", "") else ""),
                             "probe": {"kind": iprobe.kind, "args": iprobe.args}, "until": words, "keys": keys,
                             "text": text, "details": details, "verify": verify, "probed_at": time.time()})
        same = st.get(lname) if "held" in r else None
        if "held" in r and lease_meta(same).get("kind") != "block":
            print(f"NOT BLOCKED: {lname} is held by {r['held']['holder']}")
            return 1
        if same:  # the run's block is already up (named after its first red): these reds join it
            sm = lease_meta(same)
            st.set_meta(lname, same["epoch"], same["secret"], {"keys": sorted(set(sm.get("keys") or []) | set(keys))})
            print(f"already blocked: {what} joined {lname}: {sm.get('text')}")
        else:
            cat = OWNER_CAUSE_CATEGORY[owner_cause]
            line = (f"RED BLOCKED ON OWNER [{cat}]: {cause} down (blocks {what}): {details}" if cause else
                    f"RED BLOCKED ON OWNER [{cat}]: {what}: {details}")
            # the owner is told by the tick (red_owner_reminders: ONE inbox entry and a notification, then reminders)
            print(line + f" ({iprobe.detail}; `agentctl red status`)")
    for it in items:
        cur = st.item("red", it["id"])
        st.update_item("red", it["id"], cur["version"], {"block": lname})
    for x in handed:  # the dispatched fixer's own lease, handed to the owner: its wrapper ends with the block's verdict
        st.revoke(x, f"BLOCKED ON OWNER (handed over by its fixer): {a.owner_cmd or om.get('cmd')}")
    return 0

