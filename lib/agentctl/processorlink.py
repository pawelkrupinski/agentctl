# agentctl, part `processorlink`: the processor from outside: its lock, its start, its token hand-off socket, land's wait on its entry
# A real module (lib/agentctl/__init__.py MODULES): its own imports, the standard library's and MODULES' only.

import os
import socket
import subprocess
import sys
import threading
import time
from agentctl.core import env_num, err, slug, spawn_detached
from agentctl.store import StoreUnavailable, machine_state, open_store
from agentctl.selfcode import TOOL_PATH
from agentctl.waitingdir import WAITING_BRANCH, waiting_state
from agentctl.storetoken import (
    TOKEN_REQUEST, TOKEN_SHAPE, detached_env, hand_token, token, token_back, token_env_name, token_ns,
    token_sock_path)
from agentctl.owned import Owned, backoff, lock_held, restart_if_changed
from agentctl.waiting import WAITING_AWAIT_TIMEOUT


def processor_owned(d: str, repo: str | None = None) -> Owned:
    """The processor's ownership: one per waiting state directory (one per repository)."""
    return Owned("waiting", os.path.basename(d.rstrip("/")) or "repo", ["waiting", "run", "--repo", repo or ""])


def waiting_processor_alive(d: str) -> bool:
    return lock_held(processor_owned(d).lockpath)


def spawn_waiting_processor(repo: str) -> None:
    d, clone, _w = waiting_state(repo)
    if waiting_processor_alive(d):
        return
    repo = clone if os.path.isdir(clone) else repo  # the clone, not a joiner's worktree: that one closes after WATCH OK
    with open(os.path.join(d, "processor.log"), "a") as logf:
        spawn_detached([sys.executable, TOOL_PATH, "waiting", "run", "--repo", repo],
                         stdin=subprocess.DEVNULL, stdout=logf, stderr=logf, start_new_session=True, cwd=repo,
                         env=detached_env(repo))


class TokenDrop:
    """The processor's end of the hand-off."""

    def __init__(self, d: str, o_r: str) -> None:
        self.path, self.o_r = token_sock_path(d), o_r
        self.sock: socket.socket | None = None
        self.thread: threading.Thread | None = None

    def open(self) -> "TokenDrop":
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
            # served on a thread of its own: no step of the main loop can leave a client waiting
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

    def nap(self, secs: float) -> None:
        """Sleep secs (the socket is served by its own thread)."""
        end = time.time() + secs
        while True:
            left = end - time.time()
            if left <= 0:
                return
            time.sleep(min(left, 10))

    def take(self):
        if self.sock is None:  # closed under the serving thread
            return
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


def offer_token(repo: str) -> bool:
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


def await_entry(repo: str, eid: str, close: str | None = None) -> tuple[int, str | None, float | None]:
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


def _await_entry(repo: str, eid: str, own: Owned) -> tuple[int, str | None, float | None]:
    """The change's STORE ITEM decides (a RemoteStore on the laptop): an entry's JSON is only on the host whose queue
    runs it -- ci-runner's for every change since ONE QUEUE (2026-11-16: `waiting await` on the laptop crashed with
    FileNotFoundError on one). A joined change's item follows its waiting entry (project_changes)."""
    t0, delays = time.time(), backoff(15, 60)
    timeout = env_num("AGENTCTL_WAITING_TIMEOUT", WAITING_AWAIT_TIMEOUT)
    state, said = "not in the store yet", None
    while True:
        try:
            it = open_store().item("change", eid)
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
