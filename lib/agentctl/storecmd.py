# agentctl, part `storecmd`: the `agentctl store` commands: serve, token, use
# A real module (lib/agentctl/__init__.py MODULES): its own imports, the standard library's and MODULES' only.

import os
import threading
import time
from typing import Any
from agentctl.context import Context
from agentctl.clock import epoch_now
from agentctl.storeauth import (
    store_secret, store_secret_path, store_token_issue, store_token_revoke, store_tokens_load)
from agentctl.core import err, state_root
from agentctl.jvmreap import signal
from agentctl.store import Store
from agentctl.remotestore import RemoteStore, STORE_PORT
from agentctl.changesubmit import store_grace
from agentctl.storeapi import StoreAPI, store_server


def cmd_store(a: Any, *, ctx: Context) -> Any:
    """`agentctl store serve --listen ADDR:PORT --allow-user U... --metrics-user U...` (ci-runner);
    `agentctl store use URL` (the laptop: which store its sync and agents use)."""
    if a.scmd == "token":
        return store_token_command(a)
    if a.scmd == "use":
        return store_use_command(a)
    return store_serve_command(a, ctx=ctx)


def store_token_command(a: Any) -> int:
    """`agentctl store token issue|revoke|list`: the store's tokens, the issued one printed on stdout."""
    if a.tcmd == "issue":
        tid, tok = store_token_issue(a.user, a.name)
        err(f"store token {tid} for {a.user} issued (revoke: agentctl store token revoke {tid})")
        print(tok)
    elif a.tcmd == "revoke":
        rec = store_token_revoke(a.id)
        print(f"revoked {a.id} ({rec.get('user')} {rec.get('name') or ''})".rstrip())
    else:
        for tid, rec in sorted(store_tokens_load().items(), key=lambda x: x[1].get("issued", "")):
            print(f"{tid}  {rec.get('user'):<14} {rec.get('name') or '-':<14} issued {rec.get('issued')}"
                  + (f"  REVOKED {rec['revoked']}" if rec.get("revoked") else ""))
    return 0


def store_use_command(a: Any) -> int:
    """`agentctl store use URL`: this machine's store from now on, once it answers with this token."""
    RemoteStore(a.url).state()  # it answers, with this token, before anything relies on it
    os.makedirs(state_root(), exist_ok=True)
    with open(state_root("store.url"), "w") as f:
        f.write(a.url + "\n")
    print(f"store: {a.url} (every agentctl process here; the sync re-reads it at its next start)")
    return 0


def store_serve_command(a: Any, *, ctx: Context) -> int:
    """`agentctl store serve`: the file store behind its API, its leases extended by the downtime, until stopped."""
    host, _, port = a.listen.rpartition(":")
    st = Store(path=state_root("store.db"))
    gap = store_grace(st)
    if gap:
        err(f"store: down {int(gap)} s; every live lease extended by that")
    if not store_secret():
        raise SystemExit(f"agentctl store serve: no secret at {store_secret_path()} yet "
                         "(`agentctl store token issue --user <login>` makes it)")
    api = StoreAPI(st, a.allow_user or [], a.metrics_user or [], ctx=ctx)
    srv = store_server(api, host or "127.0.0.1", int(port or STORE_PORT))
    threading.Thread(target=store_alive_beat, args=(st,), daemon=True).start()
    signal.signal(signal.SIGTERM, lambda *_: threading.Thread(target=srv.shutdown, daemon=True).start())
    err(f"store: serving {state_root('store.db')} on {host}:{port}, auth {a.auth} (allow {','.join(api.allow) or '-'}; metrics "
        f"{','.join(api.metrics) or '-'})")
    srv.serve_forever()
    return 0


def store_alive_beat(st: Any) -> None:
    """Every 30 s the store's `last_alive` (store_grace reads it at the next start to extend the leases by the downtime)."""
    while True:
        time.sleep(30)  # wall: waits on and times real processes and threads
        try:
            st.meta_set("last_alive", epoch_now())
        except Exception as e:
            err(f"store: last_alive not written ({e})")
