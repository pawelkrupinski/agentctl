#!/usr/bin/env python3
"""The e2e layer's `gh` (agentctl's GitHub client is the gh CLI, `$AGENTCTL_GH_CLI`): every call goes to the scenario's
stub GitHub API ($E2E_GH_URL, tests/e2e/ghstub.py), never to GitHub.

  gh api [-X M] [-H h]... [-i] <path> [--input -]   -> that REST request to the stub; -i prints the status line and the
                                                      headers first, as gh does; a status >= 400 exits 1 naming it
  gh <anything else>                                -> POST /cli {"argv": [...]}: the stub answers {rc, stdout, stderr}
"""
import json, os, sys, urllib.error, urllib.request

BASE = os.environ["E2E_GH_URL"].rstrip("/")
argv = sys.argv[1:]


def request(method, url, body=None, headers=None):
    req = urllib.request.Request(url, data=body, method=method, headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return r.status, dict(r.headers), r.read().decode()
    except urllib.error.HTTPError as e:
        return e.code, dict(e.headers), e.read().decode()


def api(args):
    method, headers, include, body, path = "GET", {}, False, None, None
    i = 0
    while i < len(args):
        a = args[i]
        if a == "-X":
            method, i = args[i + 1], i + 2
        elif a == "-H":
            k, _, v = args[i + 1].partition(":")
            headers[k.strip()] = v.strip()
            i += 2
        elif a == "-i":
            include, i = True, i + 1
        elif a == "--input":
            body, i = sys.stdin.buffer.read(), i + 2
        elif a == "-R":
            sys.stderr.write("unknown shorthand flag: 'R' in -R\n")
            return 1
        else:
            path, i = a, i + 1
    status, hdrs, text = request(method, f"{BASE}/{path.lstrip('/')}", body, headers)
    if include:
        sys.stdout.write(f"HTTP/1.1 {status} X\r\n" + "".join(f"{k}: {v}\r\n" for k, v in hdrs.items()) + "\r\n")
    if status == 304:
        return 0
    sys.stdout.write(text)
    if status >= 400:
        sys.stderr.write(f"gh: {text[:200]} (HTTP {status})\n")
        return 1
    return 0


def other(args):
    status, _h, text = request("POST", f"{BASE}/cli", json.dumps({"argv": args}).encode(),
                               {"Content-Type": "application/json"})
    if status != 200:
        sys.stderr.write(f"gh stub: HTTP {status}: {text[:200]}\n")
        return 1
    out = json.loads(text)
    sys.stdout.write(out.get("stdout", ""))
    sys.stderr.write(out.get("stderr", ""))
    return int(out.get("rc", 0))


sys.exit(api(argv[1:]) if argv[:1] == ["api"] else other(argv))
