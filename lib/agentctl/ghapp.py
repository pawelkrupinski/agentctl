# agentctl, part `ghapp`: THE RECONCILER'S GITHUB IDENTITY -- GitHub App installation tokens (forge = github).
# A real module (lib/agentctl/__init__.py MODULES): its own imports, the standard library's and MODULES' only.

import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request
from agentctl.core import NoReadAccess, err, process_cache, state_root
from agentctl.timefmt import parse_time
from agentctl.statefile import read_record, write_json
from agentctl.selfcode import TOOL_PATH


# ---------------------------------------------------------------- github-app (github-return plan §4.1 W5b, §4.2)
# On ci-runner the reconciler pushes and reads as GitHub Apps, never as a person or a machine user:
#   lander   kinowo-lander (Contents RW, Workflows RW, Actions RW) on the repos in $AGENTCTL_GITHUB_LANDER_REPOS
#            (default pawelkrupinski/movies): it pushes land/*, waiting, check/* and main, and cancels its own runs
#   reader   agentctl-reader (Actions R, Contents R) on every other watched repo: the red tick of infra and core
# Each App is ($AGENTCTL_GITHUB_<ROLE>_APP_ID, $AGENTCTL_GITHUB_<ROLE>_KEY_FILE: the App's private key, PEM). A token is
# minted as GitHub documents it: a JWT signed RS256 by `openssl dgst -sha256 -sign` (no Python crypto dependency)
# -> GET /repos/<o_r>/installation -> POST /app/installations/<id>/access_tokens, scoped to that one repository. Tokens
# live 1 h; one is reused until GHAPP_REFRESH s before it expires, from memory and from a 0600 cache file under the
# state root (git's credential helper runs as a fresh process for every fetch and push).
#
# ONLY WITH $AGENTCTL_IDENTITY=github-app (infra's agentctl.nix sets it on ci-runner once forge = github): the laptop
# keeps gh's own auth.
#
# GIT never sees the token on its command line or in .git/config: agentctl puts itself in as git's credential helper
# for https://github.com (GIT_CONFIG_* in its own environment, inherited by every git it runs), and the helper answers
# username x-access-token, password <the installation token> -- the documented
# https://x-access-token:<token>@github.com/<o_r>.git, without the token in a URL.
GHAPP_ROLES = ("lander", "reader")
GHAPP_REFRESH = 600  # s before expiry a cached installation token is replaced
GHAPP_LANDER_REPOS_DEFAULT = "pawelkrupinski/movies"


def github_api_base():
    return (os.environ.get("AGENTCTL_GITHUB_API") or "https://api.github.com").rstrip("/")


def ghapp_enabled():
    return (os.environ.get("AGENTCTL_IDENTITY") or "").strip().lower() == "github-app"


def ghapp_config(role):
    """(app id, key file) of a role, or None when it is not configured."""
    app = (os.environ.get(f"AGENTCTL_GITHUB_{role.upper()}_APP_ID") or "").strip()
    key = (os.environ.get(f"AGENTCTL_GITHUB_{role.upper()}_KEY_FILE") or "").strip()
    return (app, key) if app and key else None


def ghapp_lander_repos():
    return [r.strip() for r in (os.environ.get("AGENTCTL_GITHUB_LANDER_REPOS") or GHAPP_LANDER_REPOS_DEFAULT).split(",")
            if r.strip()]


def ghapp_role(o_r):
    """The App that acts on o_r: the lander on its repos, the reader everywhere else."""
    return "lander" if o_r in ghapp_lander_repos() else "reader"


def _b64url(b):
    import base64
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


def ghapp_jwt(app_id, key_file, now_=None):
    """The App's JWT (RS256, 9 min, iat 60 s back for clock drift), signed by openssl with the key file."""
    t = int(now_ if now_ is not None else time.time())
    head = _b64url(json.dumps({"alg": "RS256", "typ": "JWT"}, separators=(",", ":")).encode())
    body = _b64url(json.dumps({"iat": t - 60, "exp": t + 540, "iss": str(app_id)}, separators=(",", ":")).encode())
    signing = f"{head}.{body}".encode()
    r = subprocess.run(["openssl", "dgst", "-sha256", "-sign", key_file], input=signing, capture_output=True, timeout=30)
    if r.returncode != 0 or not r.stdout:
        raise RuntimeError(f"github-app: openssl could not sign with {key_file}: "
                           f"{r.stderr.decode(errors='replace').strip()[:200]}")
    return f"{head}.{body}.{_b64url(r.stdout)}"


def _ghapp_http(method, path, bearer, body=None):
    req = urllib.request.Request(f"{github_api_base()}/{path.lstrip('/')}", method=method,
                                 data=json.dumps(body).encode() if body is not None else None,
                                 headers={"Authorization": f"Bearer {bearer}", "Accept": "application/vnd.github+json",
                                          "X-GitHub-Api-Version": "2022-11-28", "User-Agent": "agentctl",
                                          **({"Content-Type": "application/json"} if body is not None else {})})
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read() or b"null")
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"github-app: {method} {path}: HTTP {e.code} {e.read()[:200].decode(errors='replace')}") \
            from None


_ghapp_tokens = process_cache({})  # (role, o_r) -> {"token", "expires"}


def _ghapp_cache_path(role, o_r):
    return state_root("github-app", f"{role}-{o_r.replace('/', '_')}.json")


def _ghapp_cached(role, o_r, now_):
    hit = _ghapp_tokens.get((role, o_r))
    if not hit:
        hit = read_record(_ghapp_cache_path(role, o_r))
    if hit and hit.get("token") and float(hit.get("expires") or 0) - GHAPP_REFRESH > now_:
        _ghapp_tokens[(role, o_r)] = hit
        return hit["token"]
    return None


def ghapp_token(o_r, role=None, now_=None):
    """An installation token of o_r's App (the lander on its repos, else the reader), scoped to o_r alone. Raises
    NoReadAccess (a SystemExit, like every missing credential) when that App is not configured."""
    role = role or ghapp_role(o_r)
    t = now_ if now_ is not None else time.time()
    tok = _ghapp_cached(role, o_r, t)
    if tok:
        return tok
    cfg = ghapp_config(role)
    if not cfg:
        raise NoReadAccess(f"agentctl: no GitHub App for {o_r} (role {role}: set AGENTCTL_GITHUB_{role.upper()}_APP_ID "
                           f"and _KEY_FILE)")
    jwt = ghapp_jwt(*cfg, now_=t)
    inst = _ghapp_http("GET", f"repos/{o_r}/installation", jwt)["id"]
    got = _ghapp_http("POST", f"app/installations/{inst}/access_tokens", jwt,
                      {"repositories": [o_r.split("/", 1)[1]]})
    expires = parse_time(got.get("expires_at")).timestamp() if got.get("expires_at") else t + 3600
    hit = {"token": got["token"], "expires": expires}
    _ghapp_tokens[(role, o_r)] = hit
    path = _ghapp_cache_path(role, o_r)
    try:
        os.makedirs(os.path.dirname(path), mode=0o700, exist_ok=True)
        write_json(path, hit, indent=None, mode=0o600)
    except OSError as e:
        err(f"github-app: token cache not written ({e})")
    return hit["token"]


def ghapp_git_env(env=None):
    """env (default os.environ, changed in place) with agentctl as git's credential helper for https://github.com, per
    repository path (useHttpPath): every git agentctl runs then authenticates as the repo's App. A no-op without
    github-app."""
    env = os.environ if env is None else env
    if not ghapp_enabled() or env.get("AGENTCTL_GHAPP_GIT") == "1":
        return env
    # the ENTRY, never this file's own path: a real module run as a script cannot import its package (2026-11-22)
    pairs = [("credential.https://github.com.helper", ""),  # no other helper (the osxkeychain) answers first
             ("credential.https://github.com.helper", f"!{sys.executable} {TOOL_PATH} github-app credential"),
             ("credential.https://github.com.useHttpPath", "true")]
    n = int(env.get("GIT_CONFIG_COUNT") or 0)
    for i, (k, v) in enumerate(pairs):
        env[f"GIT_CONFIG_KEY_{n + i}"], env[f"GIT_CONFIG_VALUE_{n + i}"] = k, v
    env["GIT_CONFIG_COUNT"] = str(n + len(pairs))
    env["AGENTCTL_GHAPP_GIT"] = "1"
    return env


def ghapp_credential(stdin, stdout):
    """git's credential helper protocol (`get` only): host github.com and path <o>/<r>[.git] -> x-access-token and
    the installation token of that repo's App. Anything else: no answer (git tries its next source)."""
    fields = dict(line.split("=", 1) for line in stdin.read().splitlines() if "=" in line)
    if fields.get("protocol") != "https" or fields.get("host") != "github.com" or not fields.get("path"):
        return 0
    o_r = re.sub(r"\.git$", "", fields["path"].strip("/"))
    stdout.write(f"username=x-access-token\npassword={ghapp_token(o_r)}\n")
    return 0


def cmd_github_app(a):
    """`agentctl github-app token --repo O/R [--role lander|reader]` (prints a token: for the host's own scripts) or
    `agentctl github-app credential get` (git's credential helper)."""
    if a.gcmd == "credential":
        if (a.op or "get") != "get":
            return 0  # store / erase: tokens are minted, never stored by git
        return ghapp_credential(sys.stdin, sys.stdout)
    print(ghapp_token(a.repo, role=a.role))
    return 0


ghapp_git_env()  # every agentctl process (and every git it runs) on a github-app host authenticates through the helper
