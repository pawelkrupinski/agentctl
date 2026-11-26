# agentctl, part `livestream`: an agent's live stream: its transcript tailed, its secrets masked, served as server-sent events
# A real module (lib/agentctl/__init__.py MODULES): its own imports, the standard library's and MODULES' only.

from agentctl.clock import epoch_now, wall_epoch
from typing import Any
import json
import os
import re
import threading
import time
from contextlib import suppress
from agentctl.core import attempt
from agentctl.statefile import mtime
from agentctl.live import live_registry, live_state, live_tail_info, live_track
from agentctl.watchknobs import knob_live_poll


# /stream?agent=<id>[&from=<offset>]: Server-Sent Events following the transcript as it grows, from its first byte
# (or `from`, or the Last-Event-ID a reconnecting EventSource sends). Each transcript line is one `entry` event whose
# id is the byte offset just past it; bookkeeping lines (API request dumps, cost, mode, titles) are skipped, the rest
# sent whole, with token-, key- and password-like strings masked. `synced` once the history is sent, `state` when the
# agent's state changes, `reset` when the file was truncated or replaced (the client clears and the stream starts
# again at 0). BOUNDED: LIVE_CHUNK bytes read per step, one line held at most LIVE_LINE_MAX (a longer one is an
# `oversize` entry with its size), LIVE_STREAMS streams at once (503 beyond), and a client that does not take a frame
# within LIVE_SEND_TIMEOUT s is dropped (its EventSource reconnects where it left off).
LIVE_CHUNK = 256 << 10


LIVE_LINE_MAX = 16 << 20


LIVE_STREAMS = 12


LIVE_SEND_TIMEOUT = 10          # s


LIVE_PING = 15                  # s between keep-alive comments


LIVE_STREAM_MAX = 6 * 3600      # s one stream lasts (the EventSource reconnects from its last id)


LIVE_HIDDEN = frozenset(("api-request", "api-request-shape", "api-request-blob", "file-history-snapshot", "cost-state",
                         "tools-used", "mode", "permission-mode", "atis-latch", "last-prompt", "custom-title",
                         "agent-name", "summary"))


LIVE_KEEP = ("type", "subtype", "timestamp", "uuid", "isMeta", "isSidechain", "isApiErrorMessage", "level", "content",
             "operation", "attachment", "agentId", "toolUseID")


_live_streams = threading.BoundedSemaphore(LIVE_STREAMS)


# MASKING (conservative: only what looks like a credential): known token shapes, PEM private keys, the value of a
# password/secret/token/api-key assignment or JSON key when it looks like one (12+ token characters with a letter and
# a digit), Bearer/Basic/token headers, and the password of a URL.
LIVE_MASKED = "‹masked›"


LIVE_MASK_SHAPES = re.compile("|".join((
    r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----[\s\S]*?(?:-----END [A-Z0-9 ]*PRIVATE KEY-----|\Z)",
    r"\bgh[pousr]_[A-Za-z0-9]{30,}", r"\bgithub_pat_[A-Za-z0-9_]{40,}", r"\bglpat-[A-Za-z0-9_-]{20,}",
    r"\bsk-(?:ant-)?[A-Za-z0-9_-]{20,}", r"\bxox[abprs]-[A-Za-z0-9-]{10,}", r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b",
    r"\bhv[sbr]\.[A-Za-z0-9_-]{20,}", r"\beyJ[A-Za-z0-9_-]{8,}\.eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}",
    r"\bAGE-SECRET-KEY-1[0-9A-Z]{40,}", r"\bAIza[0-9A-Za-z_-]{35}")))


LIVE_MASK_KV = re.compile(r"(?i)(\b[a-z0-9_.-]*(?:password|passwd|passphrase|secret|token|api[_-]?key|access[_-]?key"
                          r"|private[_-]?key|auth[_-]?key)[a-z0-9_]*\b[\"']?\s*(?::|=|:=)\s*[\"']?)"
                          r"([A-Za-z0-9+/=_.~-]{12,})")


LIVE_MASK_AUTH = re.compile(r"(?i)(\b(?:bearer|basic|token)\s+)([A-Za-z0-9+/=_.~-]{20,})")


LIVE_MASK_URL = re.compile(r"(://[^/\s:@'\"]+:)([^@\s/'\"]{4,})(@)")


LIVE_SECRET_KEY = re.compile(r"(?i)(passw|passphrase|secret|api[_-]?key|access[_-]?key|private[_-]?key|authorization"
                             r"|(?:^|[_-])token$)")


def live_secretish(v):
    return bool(re.search(r"[A-Za-z]", v) and re.search(r"[0-9]", v))


def live_mask_text(s):
    if not isinstance(s, str) or not s:
        return s
    s = LIVE_MASK_SHAPES.sub(LIVE_MASKED, s)
    s = LIVE_MASK_KV.sub(lambda m: m.group(1) + (LIVE_MASKED if live_secretish(m.group(2)) else m.group(2)), s)
    s = LIVE_MASK_AUTH.sub(lambda m: m.group(1) + (LIVE_MASKED if live_secretish(m.group(2)) else m.group(2)), s)
    return LIVE_MASK_URL.sub(lambda m: m.group(1) + LIVE_MASKED + m.group(3), s)


def masked_whole(v: str, key: Any) -> bool:
    """Whether string v is masked wholly: under a secret-ish key, 8+ characters, no `$` reference."""
    return bool(key and LIVE_SECRET_KEY.search(key) and len(v) >= 8 and not v.startswith("$"))


def live_mask(v, key=None):
    """v with every credential-looking string masked (a JSON value under a secret-ish key of 8+ characters wholly)."""
    if isinstance(v, str):
        if masked_whole(v, key):
            return LIVE_MASKED
        return live_mask_text(v)
    if isinstance(v, dict):
        return {k: live_mask(x, k if isinstance(k, str) else None) for k, x in v.items()}
    if isinstance(v, list):
        return [live_mask(x) for x in v]
    return v


def kept_message(m: Any) -> dict[str, Any]:
    """{"message": ...} of a transcript entry's message m: a dict's role, content, model and stop_reason; {} for none."""
    if isinstance(m, dict):
        return {"message": {k: m[k] for k in ("role", "content", "model", "stop_reason") if k in m}}
    return {} if m is None else {"message": m}


def live_entry(line):
    """The entry the page shows for one transcript line (bytes), masked; None for a bookkeeping line."""
    text = line.decode("utf-8", "replace")
    rec = attempt(lambda: json.loads(text), None, catch=(ValueError,))
    if not isinstance(rec, dict):
        return {"type": "raw", "text": live_mask_text(text)} if text.strip() else None
    if rec.get("type") in LIVE_HIDDEN:
        return None
    out = {k: rec[k] for k in LIVE_KEEP if k in rec}
    out.update(kept_message(rec.get("message")))
    if rec.get("type") not in ("user", "assistant", "system", "queue-operation", "attachment"):
        out = rec  # an entry this page does not know: all of it
    return live_mask(out)


class LiveTail:
    """Follows a file by path: poll() returns the complete lines appended since the last call (at most `chunk` bytes
    read), each with the offset just past it. A file truncated below what was read, or replaced by another (rotation),
    is read again from 0 and poll says so."""

    def __init__(self, path, offset=0, chunk=LIVE_CHUNK, line_max=LIVE_LINE_MAX):
        self.path, self.offset, self.chunk, self.line_max = path, max(0, int(offset)), chunk, line_max
        self.f, self.ino, self.buf, self.skip = None, None, b"", 0

    def _open(self):
        self.f = open(self.path, "rb")
        st = os.fstat(self.f.fileno())
        self.ino = (st.st_dev, st.st_ino)

    def close(self):
        if self.f:
            self.f.close()
            self.f = None

    def _restart(self):
        self.offset, self.buf, self.skip = 0, b"", 0

    def poll(self) -> Any:
        """(lines, reset, more): lines = [(end offset, bytes)] or [(end offset, int skipped bytes)] for a line over
        line_max; reset = "truncated"/"rotated"/None; more = a full chunk was read (call again at once)."""
        st = attempt(lambda: os.stat(self.path), None, catch=(OSError,))
        if self.f is None and st is None:
            return [], None, False
        reset = self._follow(st)
        pos = self.offset + len(self.buf) + self.skip
        if os.fstat(self.f.fileno()).st_size < pos:
            self._restart()
            reset, pos = reset or "truncated", 0
        self.f.seek(pos)
        out = self._lines(self.f.read(self.chunk))
        return out, reset, pos + self.chunk <= os.fstat(self.f.fileno()).st_size

    def _follow(self, st: Any) -> Any:
        """Open the file (st: its stat now) on the first look; a file replaced by another is opened again from 0
        ("rotated"). The reset to report, or None."""
        if self.f is None:
            self._open()
        elif st is not None and (st.st_dev, st.st_ino) != self.ino:
            self.close()
            self._open()
            self._restart()
            return "rotated"
        return None

    def _lines(self, data: Any) -> Any:
        """[(end offset, line)] of the complete lines of data, the rest held in buf (or counted in skip once it is over line_max)."""
        out = []
        while data:
            i = data.find(b"\n")
            if i < 0:
                self._hold(data)
                break
            part, data = data[:i], data[i + 1:]
            out.append(self._end_line(part))
        return out

    def _hold(self, data: Any) -> Any:
        """The end of a chunk with no newline: part of the line being read, or of the over-long one being skipped."""
        if self.skip:
            self.skip += len(data)
            return
        self.buf += data
        if len(self.buf) > self.line_max:
            self.skip, self.buf = len(self.buf), b""

    def _end_line(self, part: Any) -> Any:
        """(end offset, the line's bytes -- its size in bytes when it was over line_max, skipped) of the line `part` ends."""
        skipped, self.skip = self.skip, 0
        line, self.buf = self.buf + part, b""
        size = skipped + len(line) + 1
        self.offset += size
        return self.offset, line if not skipped and len(line) <= self.line_max else size


def sse_frame(event, data, eid=None):
    """One Server-Sent Events frame: data is JSON-encoded (one line); every line of it gets its `data:` prefix."""
    body = data if isinstance(data, str) else json.dumps(data, default=str)
    head = (f"id: {eid}\n" if eid is not None else "") + f"event: {event}\n"
    return (head + "".join(f"data: {ln}\n" for ln in body.split("\n")) + "\n").encode()


def live_peer_closed(conn):
    """True when the client has closed its end (readable with nothing to read)."""
    import select
    import socket
    try:
        r, _w, _x = select.select([conn], [], [], 0)
        return bool(r) and conn.recv(1, socket.MSG_PEEK) == b""
    except (OSError, ValueError):
        return True


def _blank_info() -> Any:
    return {"stop": None, "last_ts": None, "api_error": False}


def live_stream(e: Any, start: Any, write: Any, closed: Any = lambda: False, poll: Any = None, ping: Any = LIVE_PING,
                max_secs: Any = LIVE_STREAM_MAX) -> Any:
    """Write the transcript of entry e from `start` as SSE frames through write() until closed(), max_secs, or a write
    raises (a gone or slow client)."""
    poll = poll if poll is not None else knob_live_poll()
    stream = LiveStream(e, start, write, ping)
    t0 = wall_epoch()
    try:
        while not closed() and wall_epoch() - t0 < max_secs:
            if not stream.step():
                time.sleep(poll)  # wall: waits on and times real processes and threads
    finally:
        stream.tail.close()


class LiveStream:
    """One stream of an agent's transcript: its tail, what is known of the agent (info), the frames of each step."""

    def __init__(self, e: Any, start: Any, write: Any, ping: Any) -> None:
        self.e, self.write, self.ping = e, write, ping
        self.tail = LiveTail(e["transcript"], start)
        self.info = _blank_info()
        if start:
            self.info.update({k: v for k, v in live_tail_info(e["transcript"]).items() if k in ("stop", "last_ts", "api_error")})
        write(b"retry: 3000\n\n" + sse_frame("meta", {"id": e["id"], "kind": e.get("kind"), "label": e.get("label"),
                                                      "transcript": e["transcript"], "from": start}))
        self.last_out = self.refreshed = epoch_now()
        self.synced, self.said = False, None

    def step(self) -> Any:
        """One look at the transcript: its new lines as frames, `synced` once the history is sent, the state when it
        changed, a ping when nothing was sent for `ping` seconds. True when a full chunk was read (look again at once)."""
        lines, reset, more = self.tail.poll()
        frames = self._entries(lines, reset)
        if not more and not self.synced:
            self.synced = True
            frames.append(sse_frame("synced", {"offset": self.tail.offset}, self.tail.offset))
        if self.synced and (frames or epoch_now() - self.refreshed >= 5 or self.said is None):
            frames += self._state()
        if frames:
            self.write(b"".join(frames))
            self.last_out = epoch_now()
        elif epoch_now() - self.last_out >= self.ping:
            self.write(b": ping\n\n")
            self.last_out = epoch_now()
        return more

    def _entries(self, lines: Any, reset: Any) -> Any:
        """The frames of one poll: a reset, then an entry per line (an over-long one as `oversize`)."""
        frames = []
        if reset:
            self.info = _blank_info()
            frames.append(sse_frame("reset", {"why": reset}, 0))
        for off, line in lines:
            if isinstance(line, int):
                frames.append(sse_frame("entry", {"type": "oversize", "bytes": line}, off))
                continue
            ent = live_entry(line)
            if ent is None:
                continue
            live_track(self.info, ent)
            frames.append(sse_frame("entry", ent, off))
        return frames

    def _state(self) -> Any:
        """The `state` frame when the agent's state changed since it was last said (the registry entry re-read every 5 s)."""
        if epoch_now() - self.refreshed >= 5:
            with suppress(Exception, SystemExit):
                self.e = live_registry().get(self.e["id"], self.e)
        self.refreshed = epoch_now()
        self.info["mtime"] = mtime(self.e["transcript"]) or wall_epoch()
        st = live_state(self.e, self.info)
        if st == self.said:
            return []
        self.said = st
        return [sse_frame("state", {"state": st[0], "why": st[1], "last_ts": self.info.get("last_ts")})]
