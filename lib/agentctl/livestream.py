# agentctl, part `livestream`: an agent's live stream: its transcript tailed, its secrets masked, served as server-sent events
# A real module (lib/agentctl/__init__.py MODULES): its own imports, the standard library's and MODULES' only.

import json
import os
import re
import threading
import time
from contextlib import suppress
from agentctl.core import env_num
from agentctl.statefile import mtime
from agentctl.live import live_registry, live_state, live_tail_info, live_track


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


LIVE_POLL = 1.0                 # s between looks at a quiet file ($AGENTCTL_LIVE_POLL)


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


def live_mask(v, key=None):
    """v with every credential-looking string masked (a JSON value under a secret-ish key of 8+ characters wholly)."""
    if isinstance(v, str):
        if key and LIVE_SECRET_KEY.search(key) and len(v) >= 8 and not v.startswith("$"):
            return LIVE_MASKED
        return live_mask_text(v)
    if isinstance(v, dict):
        return {k: live_mask(x, k if isinstance(k, str) else None) for k, x in v.items()}
    if isinstance(v, list):
        return [live_mask(x) for x in v]
    return v


def live_entry(line):
    """The entry the page shows for one transcript line (bytes), masked; None for a bookkeeping line."""
    text = line.decode("utf-8", "replace")
    try:
        rec = json.loads(text)
    except ValueError:
        rec = None
    if not isinstance(rec, dict):
        return {"type": "raw", "text": live_mask_text(text)} if text.strip() else None
    if rec.get("type") in LIVE_HIDDEN:
        return None
    out = {k: rec[k] for k in LIVE_KEEP if k in rec}
    m = rec.get("message")
    if isinstance(m, dict):
        out["message"] = {k: m[k] for k in ("role", "content", "model", "stop_reason") if k in m}
    elif m is not None:
        out["message"] = m
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

    def poll(self):
        """(lines, reset, more): lines = [(end offset, bytes)] or [(end offset, int skipped bytes)] for a line over
        line_max; reset = "truncated"/"rotated"/None; more = a full chunk was read (call again at once)."""
        reset = None
        try:
            st = os.stat(self.path)
        except OSError:
            st = None
        if self.f is None:
            if st is None:
                return [], None, False
            self._open()
        elif st is not None and (st.st_dev, st.st_ino) != self.ino:
            self.close()
            self._open()
            self._restart()
            reset = "rotated"
        pos = self.offset + len(self.buf) + self.skip
        if os.fstat(self.f.fileno()).st_size < pos:
            self._restart()
            reset, pos = reset or "truncated", 0
        self.f.seek(pos)
        data = self.f.read(self.chunk)
        out = []
        while data:
            i = data.find(b"\n")
            if i < 0:
                if self.skip:
                    self.skip += len(data)
                else:
                    self.buf += data
                    if len(self.buf) > self.line_max:
                        self.skip, self.buf = len(self.buf), b""
                break
            part, data = data[:i], data[i + 1:]
            if self.skip:
                n = self.skip + len(part) + 1
                self.offset += n
                self.skip = 0
                out.append((self.offset, n))
                continue
            line = self.buf + part
            self.buf = b""
            if len(line) > self.line_max:
                self.offset += len(line) + 1
                out.append((self.offset, len(line) + 1))
                continue
            self.offset += len(line) + 1
            out.append((self.offset, line))
        return out, reset, pos + self.chunk <= os.fstat(self.f.fileno()).st_size


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


def live_stream(e, start, write, closed=lambda: False, poll=None, ping=LIVE_PING, max_secs=LIVE_STREAM_MAX):
    """Write the transcript of entry e from `start` as SSE frames through write() until closed(), max_secs, or a write
    raises (a gone or slow client)."""
    poll = poll if poll is not None else env_num("AGENTCTL_LIVE_POLL", LIVE_POLL)
    tail = LiveTail(e["transcript"], start)
    info = {"stop": None, "last_ts": None, "api_error": False}
    if start:
        info.update({k: v for k, v in live_tail_info(e["transcript"]).items() if k in ("stop", "last_ts", "api_error")})
    write(b"retry: 3000\n\n" + sse_frame("meta", {"id": e["id"], "kind": e.get("kind"), "label": e.get("label"),
                                                  "transcript": e["transcript"], "from": start}))
    t0 = last_out = time.time()
    synced, said, refreshed = False, None, time.time()
    try:
        while not closed() and time.time() - t0 < max_secs:
            lines, reset, more = tail.poll()
            frames = []
            if reset:
                info = {"stop": None, "last_ts": None, "api_error": False}
                frames.append(sse_frame("reset", {"why": reset}, 0))
            for off, line in lines:
                if isinstance(line, int):
                    frames.append(sse_frame("entry", {"type": "oversize", "bytes": line}, off))
                    continue
                ent = live_entry(line)
                if ent is None:
                    continue
                live_track(info, ent)
                frames.append(sse_frame("entry", ent, off))
            if not more and not synced:
                synced = True
                frames.append(sse_frame("synced", {"offset": tail.offset}, tail.offset))
            if synced and (frames or time.time() - refreshed >= 5 or said is None):
                if time.time() - refreshed >= 5:
                    with suppress(Exception, SystemExit):
                        e = live_registry().get(e["id"], e)
                refreshed = time.time()
                info["mtime"] = mtime(e["transcript"]) or time.time()
                st = live_state(e, info)
                if st != said:
                    said = st
                    frames.append(sse_frame("state", {"state": st[0], "why": st[1], "last_ts": info.get("last_ts")}))
            if frames:
                write(b"".join(frames))
                last_out = time.time()
            elif time.time() - last_out >= ping:
                write(b": ping\n\n")
                last_out = time.time()
            if not more:
                time.sleep(poll)
    finally:
        tail.close()
