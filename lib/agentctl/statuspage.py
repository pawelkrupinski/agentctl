# agentctl, part `statuspage`: the read-only status page: its HTTP server on a daemon thread, its URL, `agentctl status` and `--open`
# A real module (lib/agentctl/__init__.py MODULES): its own imports, the standard library's and MODULES' only.

import json
import os
import re
import threading
import time
import urllib.parse
from typing import Any
from agentctl.procrunner import process_runner
from agentctl.core import err, on_ci_runner, process_cache, reds_dir, state_root
from agentctl.statefile import read_json_file, write_json
from agentctl.live import live_lookup
from agentctl.laptoprole import laptop_role_standby
from agentctl.storestate import port_open
from agentctl.statusagents import STATUS_LOG_MAX, STATUS_LOG_SUFFIXES, status_log_body
from agentctl.status import render_status, status_cached, status_header
from agentctl.livepage import AGENT_PAGE_HTML, STATUS_PAGE_HTML
from agentctl.livestream import (
    LIVE_SEND_TIMEOUT, LIVE_STREAMS, _live_streams, live_mask_text, live_peer_closed, live_stream)


# ---------------------------------------------------------------- the status page (2026-10-29)
# The owner: "write a status page for the agentctl - all reds and agents assigned, status, progress, logs etc". The
# laptop's processor (the sync; phase 1's local processor too) serves a READ-ONLY page at http://agentctl.localhost/:
# 127.0.0.1:80, else 127.0.0.1:3015 (logged) when port 80 cannot be bound or something already listens on it (Rancher
# Desktop's ssh forward holds *:80 on this laptop: a loopback bind beside it would shadow it). *.localhost resolves to
# loopback in Chrome and curl without /etc/hosts. Routes: `/` (the page; it renders /api/status every 15 s),
# `/api/status` (JSON), `/log?path=<a .log/.out/.txt file under the state root>` (its tail). Only the Host names
# agentctl.localhost, 127.0.0.1 and localhost are answered (DNS rebinding). Every source is read in a thread of its own
# against a bound; a failing source is an error in its own section and never the page's or the tick's. It only reads:
# no `secrets` (the remote store's token is the one the sync already holds), no transitions, no TCC-protected folders,
# and lease secrets never leave the process.
STATUS_PAGE_HOST = "agentctl.localhost"


STATUS_PAGE_PORT = 80


STATUS_PAGE_FALLBACK = 3015


STATUS_PAGE_HOSTS = (STATUS_PAGE_HOST, "127.0.0.1", "localhost")


def cmd_status(a):
    if getattr(a, "open", False):
        return status_page_open()
    t = time.time()
    h = status_header(t, probes=False)
    if a.line:  # THE STATUS LINE IS THE SNAPSHOT ALONE: no store RPC, no network (2026-11-22: 2-4.6 s, "gate ?")
        print(h["line"])
        return 0
    standby = None if on_ci_runner() else laptop_role_standby()  # ONE MACHINE HOLDS THE LAPTOP ROLE
    print((f"STANDBY: {standby}\n" if standby else "") + render_status(h, t))
    return 0


def status_host_ok(host):
    """Only agentctl.localhost, 127.0.0.1 and localhost (any port): another name pointing here is a rebinding page."""
    h = (host or "").strip().lower()
    if not h or h.startswith("["):
        return False
    name, _, port = h.partition(":")
    return name in STATUS_PAGE_HOSTS and (not port or port.isdigit())


def status_log_path(p):
    """The real path of a log /log may serve: a regular .log/.out/.txt file under the state root or the reds dir (after
    symlinks: no traversal out); else None."""
    if not p or "\0" in p or not os.path.isabs(os.path.expanduser(p)):
        return None
    real = os.path.realpath(os.path.expanduser(p))
    if not (real.endswith(STATUS_LOG_SUFFIXES) or re.search(r"\.log\.\d+$", real)) or not os.path.isfile(real):
        return None
    for root in {os.path.realpath(state_root()), os.path.realpath(reds_dir())}:
        if real.startswith(root.rstrip(os.sep) + os.sep):
            return real
    return None


def status_page_server(host, port):
    """The page's HTTP server (not started)."""
    import http.server

    class Handler(http.server.BaseHTTPRequestHandler):
        server_version = "agentctl-status"

        def log_message(self, *_a):
            pass

        def send(self, code, body, ctype):
            data = body.encode() if isinstance(body, str) else body
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            self.guard_headers()
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(data)

        def guard_headers(self):
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("X-Frame-Options", "DENY")
            self.send_header("Content-Security-Policy", "default-src 'none'; script-src 'unsafe-inline'; "
                             "style-src 'unsafe-inline'; connect-src 'self'; base-uri 'none'; form-action 'none'")

        def do_POST(self):  # read-only: GET (and HEAD) alone, so no request can change anything (no CSRF to guard)
            self.send(405, "read-only: GET only\n", "text/plain; charset=utf-8")
        do_PUT = do_DELETE = do_PATCH = do_POST

        def stream(self, u):
            q = urllib.parse.parse_qs(u.query)
            e = live_lookup((q.get("agent") or [""])[0])
            if not e:
                return self.send(404, "no such agent: an id from the page's Agents section\n", "text/plain; charset=utf-8")
            start = (self.headers.get("Last-Event-ID") or (q.get("from") or ["0"])[0] or "0").strip()
            if not start.isdigit() or len(start) > 15:
                return self.send(400, "from: a byte offset\n", "text/plain; charset=utf-8")
            if self.command == "HEAD":
                return self.send(200, "", "text/event-stream")
            if not _live_streams.acquire(blocking=False):
                return self.send(503, f"{LIVE_STREAMS} live streams already open\n", "text/plain; charset=utf-8")
            try:
                self.close_connection = True
                self.connection.settimeout(LIVE_SEND_TIMEOUT)  # a client that does not take a frame is dropped
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream; charset=utf-8")
                self.send_header("X-Accel-Buffering", "no")
                self.guard_headers()
                self.end_headers()

                def write(b):
                    self.wfile.write(b)
                    self.wfile.flush()
                live_stream(e, int(start), write, closed=lambda: live_peer_closed(self.connection))
            except OSError:  # gone, or too slow to take a frame
                pass
            finally:
                _live_streams.release()

        def do_HEAD(self):
            self.do_GET()

        def do_GET(self):
            try:
                if not status_host_ok(self.headers.get("Host")):
                    return self.send(403, "forbidden host\n", "text/plain; charset=utf-8")
                u = urllib.parse.urlsplit(self.path)
                if u.path == "/":
                    return self.send(200, STATUS_PAGE_HTML, "text/html; charset=utf-8")
                if u.path == "/api/status":
                    return self.send(200, json.dumps(status_cached(), default=str), "application/json")
                if u.path == "/stream":
                    return self.stream(u)
                if u.path.startswith("/agent/"):
                    if not live_lookup(urllib.parse.unquote(u.path[len("/agent/"):])):
                        return self.send(404, "no such agent: an id from the page's Agents section\n",
                                         "text/plain; charset=utf-8")
                    return self.send(200, AGENT_PAGE_HTML, "text/html; charset=utf-8")
                if u.path == "/log":
                    q = urllib.parse.parse_qs(u.query).get("path") or [""]
                    real = status_log_path(q[0])
                    if not real:
                        return self.send(403, "only a .log/.out/.txt file under ~/.cache/agentctl\n",
                                         "text/plain; charset=utf-8")
                    body, size = status_log_body(real)
                    body = live_mask_text(body.decode("utf-8", "replace")).encode()
                    head = f"{real} ({size} bytes" + (f"; the last {STATUS_LOG_MAX} shown" if size > STATUS_LOG_MAX else "") + ")\n\n"
                    return self.send(200, head.encode() + body, "text/plain; charset=utf-8")
                return self.send(404, "not found\n", "text/plain; charset=utf-8")
            except (BrokenPipeError, ConnectionResetError):
                pass
            except Exception as e:  # never the processor's: one request's error is that request's
                try:
                    self.send(500, f"error: {type(e).__name__}: {str(e)[:300]}\n", "text/plain; charset=utf-8")
                except OSError:
                    pass

    class Server(http.server.ThreadingHTTPServer):
        daemon_threads = True
        allow_reuse_address = True
    return Server((host, port), Handler)


_status_page = process_cache({})


def status_page_file():
    return state_root("status-page.json")


def status_page_start() -> Any:
    """Serve the page from a daemon thread of this process, once: 127.0.0.1:80, else :3015 ($AGENTCTL_STATUS_PORT: that
    port alone; AGENTCTL_STATUS_PAGE=off: none). Writes status-page.json (its URL, for `status --open`). Returns the URL
    or None. Never raises."""
    if _status_page.get("url"):
        return _status_page["url"]
    if os.environ.get("AGENTCTL_STATUS_PAGE", "").lower() in ("0", "off", "no"):
        return None
    ports = ([int(os.environ["AGENTCTL_STATUS_PORT"])] if os.environ.get("AGENTCTL_STATUS_PORT")
             else [STATUS_PAGE_PORT, STATUS_PAGE_FALLBACK])
    why = []
    for p in ports:
        try:
            if p == STATUS_PAGE_PORT and port_open(p, timeout=0.5):
                why.append(f"port {p}: something already listens on it (not shadowed)")
                continue
            srv = status_page_server("127.0.0.1", p)
        except (OSError, ValueError) as e:
            why.append(f"port {p}: {e}")
            continue
        threading.Thread(target=srv.serve_forever, daemon=True, name="status-page").start()
        threading.Thread(target=status_warm, daemon=True, name="status-warm").start()  # the first refresh has data
        url = f"http://{STATUS_PAGE_HOST}/" if p == 80 else f"http://{STATUS_PAGE_HOST}:{p}/"
        _status_page.update(url=url, server=srv)
        if why:
            err(f"status page: {'; '.join(why)}: serving {url} instead")
        err(f"status page: {url} (127.0.0.1:{p})")
        try:
            write_json(status_page_file(), {"url": url, "port": p, "pid": os.getpid(), "at": time.time(), "fallback": why})
        except OSError as e:
            err(f"status page: {status_page_file()} not written: {e}")
        return url
    err(f"status page: not served ({'; '.join(why)})")
    return None


def status_warm():
    try:
        status_cached()
    except (Exception, SystemExit) as e:
        err(f"status page: the first read failed: {str(e)[:200]}")


def status_page_url():
    return read_json_file(status_page_file()).get("url") or f"http://{STATUS_PAGE_HOST}/"


def status_page_open():
    url = status_page_url()
    print(url)
    process_runner().run([os.environ.get("AGENTCTL_OPEN", "open"), url], capture_output=True, timeout=30)
    return 0
