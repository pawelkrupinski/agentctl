# agentctl, part `storeauth`: the store API's auth: its secret and the per-user tokens it issues, revokes and reads back
# A real module (lib/agentctl/__init__.py MODULES): its own imports, the standard library's and MODULES' only.

import json
import os
import re
from agentctl.timefmt import ts
from agentctl.statefile import load_record, write_json


# THE STORE'S OWN TOKENS (github-return plan §4.1 W5b): `store serve` checks each caller's token itself -- no forge in
# the way. A token is `ast.<id>.<user>.<mac>`, mac = HMAC-SHA256 of
# "<id>.<user>" under the host's secret ($AGENTCTL_STORE_SECRET, default /var/lib/agentctl/store-secret: 32 random
# bytes, 0600, made by the first `store token issue`), and counts while <id> is in the registry beside the secret
# (store-tokens.json, 0600) unrevoked. Issued and revoked on the host as the store's user:
#   agentctl store token issue --user pawel [--name laptop]   (prints the token once; the laptop keeps it in the vault
#                                                              as bitcashier/AGENTCTL_STORE_TOKEN)
#   agentctl store token revoke <id>   /   agentctl store token list
# The check is a file read and an HMAC, so it is never cached: a revoke counts at the next request.
STORE_SECRET = "/var/lib/agentctl/store-secret"


STORE_AUTHS = ("store-tokens",)  # `store serve --auth`: the one way the store checks a token


def store_secret_path():
    return os.environ.get("AGENTCTL_STORE_SECRET") or STORE_SECRET


def store_tokens_path():
    return os.path.join(os.path.dirname(store_secret_path()), "store-tokens.json")


def store_secret(create=False):
    """The HMAC secret's bytes; made (32 random bytes, 0600, never overwritten) when create and absent."""
    path = store_secret_path()
    try:
        with open(path, "rb") as f:
            return f.read()
    except FileNotFoundError:
        if not create:
            return None
    os.makedirs(os.path.dirname(path), exist_ok=True)
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "wb") as f:
            f.write(os.urandom(32))
    except FileExistsError:
        pass
    with open(path, "rb") as f:
        return f.read()


def store_token_mac(secret, tid, user):
    import hashlib, hmac
    return hmac.new(secret, f"{tid}.{user}".encode(), hashlib.sha256).hexdigest()


def store_tokens_load():
    try:
        return load_record(store_tokens_path())
    except FileNotFoundError:
        return {}


def store_tokens_save(reg):
    write_json(store_tokens_path(), reg, mode=0o600, sort_keys=True)


def store_token_issue(user, name=""):
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", user or ""):
        raise SystemExit(f"agentctl store token: {user!r} is not a user name (letters, digits, _ and -)")
    secret = store_secret(create=True)
    import secrets as _secrets
    tid = _secrets.token_hex(6)
    reg = store_tokens_load()
    reg[tid] = {"user": user, "name": name or "", "issued": ts()}
    store_tokens_save(reg)
    return tid, f"ast.{tid}.{user}.{store_token_mac(secret, tid, user)}"


def store_token_revoke(tid):
    reg = store_tokens_load()
    if tid not in reg:
        raise SystemExit(f"agentctl store token: no token {tid}")
    reg[tid]["revoked"] = ts()
    store_tokens_save(reg)
    return reg[tid]


def store_token_user(tok):
    """The user a store token names, or None (malformed, a wrong mac, unknown, revoked, or no secret here)."""
    import hmac
    parts = (tok or "").split(".")
    if len(parts) != 4 or parts[0] != "ast":
        return None
    _, tid, user, mac = parts
    secret = store_secret()
    if not secret or not hmac.compare_digest(mac, store_token_mac(secret, tid, user)):
        return None
    try:
        rec = store_tokens_load().get(tid)
    except (OSError, ValueError):
        return None
    if not rec or rec.get("revoked") or rec.get("user") != user:
        return None
    return user
