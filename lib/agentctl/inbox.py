# agentctl, part `inbox`: the inbox.
# Runs inside agentctl's one namespace, after the parts before it in lib/agentctl/__init__.py's PARTS (it uses their names).

VERDICT_LINE = re.compile(r"^(LANDED \S+|NOT LANDED|WAITING \S+|QUEUED \S+|WATCH (OK|RED|UNKNOWN|STALLED)\b|run \S+#\d+: |file \S+: )")
INBOX_TAIL = 15


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


WAIT_KINDS_DEFAULT = "INVARIANT,NETWORK,RED_STARVED,RED,BRANCH_RED,FIX_FAILED,FIX_NOT_COUNTED,NOT_LANDED,PRECHECK_REFUSED,STALLED,UNKNOWN,RED_UNCLAIMED,RED_BUDGET,RED_AUTHOR,RED_ESCALATE,BLOCKED,FIXER_FAILED,DIAGNOSED,UNLANDED_ORPHAN,ORPHAN_SUBMITTED,RED_REVERT,LAUNCH_FAILED,REVERTED,REVERT_NOTICE"


def inbox_command(a):
    """The inbox's name for a parsed command, or None when it writes no entry. `land status|hold|release|cancel` write
    none: a queue subcommand answers its caller there and then, and a monitor polling `land status` wrote 2,692 entries
    in three days, acked by hand (2026-11-16). Only a verdict that may outlive the agent that started it goes there."""
    if a.cmd == "land" and getattr(a, "lcmd", None):
        return None
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


def entry_line(e):
    where = (e.get("cwd") or "").replace(HOME, "~")
    state = f"exit {e.get('exit')}" if e.get("final", True) else "still running"
    return f"{e['id']} {'read  ' if e['read'] else 'UNREAD'} {e.get('command')} {where} {state}: {e.get('verdict')}"


INBOX_WAIT_POLL = 1.0  # seconds between stat()s of the inbox directory ($AGENTCTL_INBOX_POLL)


def inbox_wait(kinds=None, since_ack=False, timeout=0, verbose=False, forever=False, audience="owner"):
    """THE NOTIFIER: block until an unread entry of `kinds` exists, print it (every one there is), exit 0; 2 at the
    timeout. kinds None: WAIT_KINDS_DEFAULT, and every alert addressed to the owner whatever its kind (an issue's
    escalation is OTHER, and it is the owner's to act on).
    UNREAD IS PER ENTRY (2026-11-19: issue 55799d's owner alert sat unread a day): an entry not acknowledged wakes it,
    whatever was acknowledged after it. `--since-ack` once meant "newer than the newest ack", and the orchestrator acks
    one id at a time, so every later-acked entry hid an older unread one; it is accepted and means just that now. So
    every arming prints the whole unread backlog first: a re-armed watcher (a compaction, a restart) never misses it.
    Cheap: one stat() of the inbox directory (and of acked/) a second, the entries read again only when either changed
    (a new entry is an os.replace into the directory, an ack a file in acked/). No forge, no git.
    forever: never exits; streams each such entry once, one line each, as it appears (one background task a session).
    audience (THE ALERT MODEL, alerts.py): only entries addressed to it -- the owner's by default; "all" every one. An
    alert updated in place is streamed again only when it is SURFACED again (it became the owner's, a new tier)."""
    d = inbox_dir()
    os.makedirs(d, exist_ok=True)
    owner_any = kinds is None
    want = {k.strip().upper().replace("-", "_") for k in (kinds or WAIT_KINDS_DEFAULT).split(",") if k.strip()}
    auds = None if audience in (None, "", "all") else {x.strip() for x in str(audience).split(",") if x.strip()}
    with alert_lock(d):
        alert_index(d)  # the first use migrates the backlog into the model: no flood
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
            hits = [e for e in inbox_entries() if not e["read"]
                    and (auds is None or alert_audience(e) in auds)
                    and ((e.get("kind") or inbox_kind(e.get("verdict"), e.get("final", True))) in want
                         or (owner_any and alert_audience(e) == "owner"))]
            if forever:
                for e in hits:
                    if (e["id"], e.get("surfaced") or 1) not in printed:
                        printed.add((e["id"], e.get("surfaced") or 1))
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
