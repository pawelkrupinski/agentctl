# agentctl, part `inbox`: the inbox.
# Runs inside agentctl's one namespace, after the parts before it in lib/agentctl/__init__.py's PARTS (it uses their names).

# ---------------------------------------------------------------- inbox
# The owner (2026-10-27): three agents ended their turns while their land/watch still ran, and the WATCH verdicts went
# nowhere. Every `land`, `watch`, `wait` and `waiting await` writes its verdict to an inbox when it ends -- normally, on
# a signal, or on a crash -- and land's WAITING line too (it may wait for hours): one JSON file per verdict under
# ~/.cache/agentctl/inbox/ ($AGENTCTL_INBOX_DIR; beside $AGENTCTL_OWNERS_DIR when only that is set). The orchestrating
# session reads it with `agentctl inbox` whether or not the agent that started the command is still alive. A restart
# on new code (os.execv) writes nothing: the resumed process writes the verdict. Only a SIGKILL leaves no entry (its
# owner record stays, and `agentctl orphans` names it).
INBOX_COMMANDS = {"land", "watch", "wait", "waiting await"}
VERDICT_LINE = re.compile(r"^(LANDED \S+|NOT LANDED|WAITING \S+|QUEUED \S+|WATCH (OK|RED|UNKNOWN|STALLED)\b|run \S+#\d+: |file \S+: )")
INBOX_TAIL = 15


def inbox_dir():
    if os.environ.get("AGENTCTL_INBOX_DIR"):
        return os.environ["AGENTCTL_INBOX_DIR"]
    if os.environ.get("AGENTCTL_OWNERS_DIR"):
        return os.path.join(os.path.dirname(os.path.abspath(os.environ["AGENTCTL_OWNERS_DIR"])), "inbox")
    return state_root("inbox")


class VerdictTee:
    """stdout, with every line also kept: the verdict lines, and the last INBOX_TAIL lines for context."""

    def __init__(self, stream, on_line=None):
        self.stream, self.on_line, self.partial = stream, on_line, ""
        self.verdicts, self.tail = [], []

    def write(self, s):
        n = self.stream.write(s)
        self.partial += s
        while "\n" in self.partial:
            line, self.partial = self.partial.split("\n", 1)
            self._line(line)
        return n

    def _line(self, line):
        self.tail = (self.tail + [line])[-INBOX_TAIL:]
        if VERDICT_LINE.match(line):
            self.verdicts.append(line)
            if self.on_line:
                self.on_line(line)

    def finish(self):
        if self.partial:
            self._line(self.partial)
            self.partial = ""

    def flush(self):
        return self.stream.flush()

    def __getattr__(self, name):
        return getattr(self.stream, name)


def safe_cwd():
    """The cwd, or what it was: a watch's WATCH OK closes the worktree it runs in, and os.getcwd() then raises (the
    verdict was lost with it)."""
    try:
        return os.getcwd()
    except OSError:
        return os.environ.get("PWD") or "(removed)"


INBOX_KINDS = [("INVARIANT", r"INVARIANT "), ("CLOSED", r"CLOSED"), ("UNLANDED_ORPHAN", r"UNLANDED ORPHAN:"), ("RECOVERED", r"RECOVERED:"), ("RELANDING", r"RELANDING:"), ("NETWORK", r"NETWORK:"),  # first: they quote others
               ("BRANCH_RED", r"BRANCH RED"), ("RED_UNCLAIMED", r"RED UNCLAIMED"), ("RED_BUDGET", r"RED BUDGET"), ("RED_AUTHOR", r"RED AUTHOR"), ("RED_ESCALATE", r"RED ESCALATE"), ("BLOCKED", r"RED BLOCKED ON OWNER"),
               ("RED_DISPATCHED", r"RED DISPATCHED"), ("RED_REVERT", r"RED REVERT"), ("RED_FLAKE", r"RED FLAKE"), ("FIXER_LANDED", r"RED FIXER [^:]*: LANDED "), ("DIAGNOSED", r"RED FIXER [^:]*: DIAGNOSED:"),
               ("FIXER_FAILED", r"RED FIXER "), ("RED_CLEARED", r"RED CLEARED"), ("FIX_FAILED", r"FIX FAILED"), ("FIX_NOT_COUNTED", r"FIX NOT COUNTED"), ("FIX_OK", r"FIX OK"), ("FIX_UNVERIFIED", r"FIX UNVERIFIED"),
               ("RED", r"WATCH RED"), ("OK", r"WATCH OK"), ("STALLED", r"WATCH STALLED|STALLED:|.*\bstalled\b"),
               # a crash, a signal or an exit with no verdict: whatever it watched is unknown now
               ("UNKNOWN", r"WATCH UNKNOWN|UNKNOWN:|crashed:|exited:|ended by signal|exit \d+ with no verdict"),
               ("NOT_LANDED", r"NOT LANDED"), ("SUPERSEDED", r"SUPERSEDED "), ("LANDED", r"LANDED "), ("WAITING", r"WAITING "), ("QUEUED", r"QUEUED "), ("HELD", r"HELD:"),
               ("CHECK", r"CHECK CAP")]
WAIT_KINDS_DEFAULT = "RED,BRANCH_RED,FIX_FAILED,FIX_NOT_COUNTED,NOT_LANDED,STALLED,UNKNOWN,RED_UNCLAIMED,RED_BUDGET,RED_AUTHOR,RED_ESCALATE,BLOCKED,FIXER_FAILED,DIAGNOSED,UNLANDED_ORPHAN,RED_REVERT"


def inbox_kind(verdict, final=True):
    """An entry's kind, from its verdict line: what `inbox wait --kinds` selects on."""
    v = verdict or ""
    if not final:
        return "QUEUED" if v.startswith("QUEUED ") else "WAITING"
    for kind, pat in INBOX_KINDS:
        if re.match(pat, v):
            return kind
    return "OTHER"


def inbox_write(command, verdict, verdicts, tail, exit_code, final=True, cwd=None, reply=False):
    """One inbox entry, written atomically; returns its id (None when the inbox cannot be written: never fatal).
    A CONDITION_KINDS entry only from open_condition (ConditionBypass otherwise: it would have no probe to clear it);
    reply=True: a command's own verdict to the agent that ran it (with_inbox), not a standing condition."""
    if final and not reply and not _OPENING[0] and inbox_kind(verdict, final) in CONDITION_KINDS:
        raise ConditionBypass(f"a {inbox_kind(verdict, final)} inbox entry outside open_condition: {str(verdict)[:160]}")
    now = dt.datetime.now(dt.timezone.utc)
    eid = (f"{now.strftime('%Y%m%dT%H%M%S%f')}-{os.getpid()}-{re.sub(r'[^a-z]+', '-', command)}"
           + ("" if final else "-waiting"))
    here = safe_cwd()
    rec = {"id": eid, "time": now.strftime("%Y-%m-%dT%H:%M:%SZ"), "at": now.timestamp(), "command": command, "argv": sys.argv[1:],
           "cwd": cwd or here, "pid": os.getpid(), "final": final, "exit": exit_code, "verdict": verdict,
           "kind": inbox_kind(verdict, final),
           "owner": os.environ.get("CLAUDE_CODE_SESSION_ID") or f"{os.environ.get('USER', '?')}@{here}",
           "claude_pid": int(os.environ["CLAUDE_PID"]) if (os.environ.get("CLAUDE_PID") or "").isdigit() else None,
           "agent": agent_name(), "verdicts": verdicts, "tail": tail}
    try:
        d = inbox_dir()
        os.makedirs(d, exist_ok=True)
        tmp = os.path.join(d, f".{eid}.tmp")
        with open(tmp, "w") as f:
            json.dump(rec, f, indent=1)
        os.replace(tmp, os.path.join(d, eid + ".json"))
        return eid
    except OSError as e:
        err(f"could not write the inbox entry ({e})")
        return None


def inbox_command(a):
    """The inbox's name for a parsed command, or None when it writes no entry."""
    name = a.cmd + (f" {a.wcmd}" if a.cmd == "waiting" else "")
    return name if name in INBOX_COMMANDS else None


def with_inbox(command, handler, a):
    """Run handler(a) with its stdout teed; whatever way it ends, write its verdict to the inbox."""
    waiting_line = []

    def on_line(line):
        if line.startswith(("WAITING ", "QUEUED ")):
            inbox_write(command, line, [line], tee.tail, None, final=False)
            waiting_line.append(line)
    tee = VerdictTee(sys.stdout, on_line)
    errs = VerdictTee(sys.stderr)  # its last lines name a cause when stdout has no verdict
    sys.stdout, sys.stderr, code, crash = tee, errs, 1, None
    try:
        code = handler(a)
        return code
    except SystemExit as e:
        code = e.code if isinstance(e.code, int) else (0 if e.code is None else 1)
        if code >= 128:
            crash = f"ended by signal {code - 128}"
        elif e.code is not None and not isinstance(e.code, int):  # SystemExit("agentctl: ..."): the message is the cause
            crash = f"exited: {str(e.code).replace('agentctl: ', '')}"
        raise
    except BaseException as e:
        crash = f"crashed: {type(e).__name__}: {e}"
        raise
    finally:
        sys.stdout, sys.stderr = tee.stream, errs.stream
        tee.finish()
        errs.finish()
        last = next((l for l in reversed(tee.tail) if l.strip()), None)
        last_err = next((l.replace("agentctl: ", "") for l in reversed(errs.tail) if l.strip()), None)
        if tee.verdicts:
            verdict = crash or tee.verdicts[-1]
        elif crash:
            verdict = crash
        elif code:  # never a bare exit: what it said last, on stderr first
            verdict = f"exit {code} with no verdict line; it said last: {last_err or last or 'nothing'}"
        else:
            verdict = last or last_err or "exit 0 with no output"
        if not (crash is None and code == 0 and waiting_line and verdict == waiting_line[-1]):  # land ended AT WAITING:
            inbox_write(command, verdict, tee.verdicts, tee.tail, code if crash is None else (code or 1),  # written once
                        cwd=getattr(a, "inbox_cwd", None), reply=True)


def inbox_entries():
    d = inbox_dir()
    acked_dir = os.path.join(d, "acked")
    acked = set(os.listdir(acked_dir)) if os.path.isdir(acked_dir) else set()
    out = []
    for p in glob.glob(os.path.join(d, "*.json")):
        rec = read_record(p)
        if rec is not None and rec.get("id"):
            rec["read"] = rec["id"] in acked
            out.append(rec)
    return sorted(out, key=lambda r: (r.get("at") or 0, r["id"]))


def entry_line(e):
    where = (e.get("cwd") or "").replace(HOME, "~")
    state = f"exit {e.get('exit')}" if e.get("final", True) else "still running"
    return f"{e['id']} {'read  ' if e['read'] else 'UNREAD'} {e.get('command')} {where} {state}: {e.get('verdict')}"


def last_ack(d):
    """When anything was last acknowledged (the newest file in acked/), 0 if never."""
    acked = os.path.join(d, "acked")
    try:
        return max([os.stat(acked).st_mtime] + [os.stat(os.path.join(acked, n)).st_mtime for n in os.listdir(acked)])
    except OSError:
        return 0.0


INBOX_WAIT_POLL = 1.0  # seconds between stat()s of the inbox directory ($AGENTCTL_INBOX_POLL)


def inbox_wait(kinds, since_ack=False, timeout=0, verbose=False, forever=False):
    """THE NOTIFIER: block until an unread entry of `kinds` exists, print it (every one there is), exit 0; 2 at the
    timeout. Cheap: one stat() of the inbox directory (and of acked/) a second, the entries read again only when either
    changed (a new entry is an os.replace into the directory, an ack a file in acked/). No forge, no git.
    forever: never exits; streams each such entry once, one line each, as it appears (one background task a session)."""
    d = inbox_dir()
    os.makedirs(d, exist_ok=True)
    want = {k.strip().upper().replace("-", "_") for k in kinds.split(",") if k.strip()}
    floor = last_ack(d) if since_ack else 0.0
    poll = env_num("AGENTCTL_INBOX_POLL", INBOX_WAIT_POLL)
    t0, seen, printed = time.time(), None, set()
    while True:
        try:
            stamp = (os.stat(d).st_mtime_ns, os.stat(os.path.join(d, "acked")).st_mtime_ns
                     if os.path.isdir(os.path.join(d, "acked")) else 0)
        except OSError:
            stamp = None
        if stamp != seen:
            seen = stamp
            hits = [e for e in inbox_entries() if not e["read"] and (e.get("at") or 0) > floor
                    and (e.get("kind") or inbox_kind(e.get("verdict"), e.get("final", True))) in want]
            if forever:
                for e in hits:
                    if e["id"] not in printed:
                        printed.add(e["id"])
                        print(entry_line(e), flush=True)
                        if verbose:
                            for line in e.get("tail") or []:
                                print(f"    {line}", flush=True)
                hits = []
            if hits:
                for e in hits:
                    print(entry_line(e))
                    if verbose:
                        for line in e.get("tail") or []:
                            print(f"    {line}")
                print(f"inbox: {len(hits)} to route; then `~/bin/agentctl inbox <id> --ack` and run `inbox wait` again")
                return 0
        if timeout and time.time() - t0 >= timeout and not forever:
            print(f"inbox: no unread {','.join(sorted(want))} entry within {int(timeout)} s")
            return 2
        time.sleep(poll)
