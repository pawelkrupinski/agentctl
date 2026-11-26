# agentctl, part `storecalls`: THE STORE'S CALLS, one table: each call's name, arguments, kind and the API version that added it
# A real module (lib/agentctl/__init__.py MODULES): its own imports, the standard library's and MODULES' only.
#
# ONE CALL SURFACE (review 2026-11-26, C 1.9): the calls were spelled four times -- Store's methods, the server's
# STORE_RPC, the client's retry-safe subset and the tests -- and the client took its methods from the server's module.
# This table is the one spelling: the server serves exactly its calls (StoreAPI: a name outside it is UnknownCall,
# the 404 a newer client reads as a call the store lacks), RemoteStore's methods are made from it where the class is
# defined, a call's retry safety is its kind, and tests/test_storecalls.py holds the table to Store's own methods and
# their parameters. Adding a call is one row: its Store method, then the row with the next version.
#
# VERSIONS: a laptop and ci-runner run different revisions for a while (ci-runner's pin holds while agentctl's main is
# red), so a client may ask a store older than its call. `since` is the API version that added the call; a server
# serves the calls up to its STORE_API_VERSION (served_calls), and the client catches the older store's refusal
# (StoreLacksCall) and falls back where it has a way to (eventtrace.read_trace). An old client's calls stay served:
# a row is never removed while a pinned revision may still send it.

from typing import NamedTuple

READ = "read"              # reads only: safe to send again
IDEMPOTENT = "idempotent"  # writes, and the same call twice leaves what one leaves (a renew, a meta set): safe again
WRITE = "write"            # writes, and twice may differ (an acquire, a transition): sent again only when unsent


class StoreCall(NamedTuple):
    name: str
    args: str   # Store's parameters, in order (tests/test_storecalls.py holds them to its signature)
    kind: str   # READ | IDEMPOTENT | WRITE
    since: int  # the API version that added it

    @property
    def retry_safe(self) -> bool:
        """Whether a call that may have reached the store is sent again after a transient failure."""
        return self.kind != WRITE


# version 1: every call up to this table (2026-11-26); 2: the event model's indexed reads (eventtrace.py)
STORE_CALLS = (
    StoreCall("acquire", "name holder ttl max_hold progress item meta", WRITE, 1),
    StoreCall("renew", "name epoch secret progress ttl note", IDEMPOTENT, 1),
    StoreCall("note", "name epoch secret note", IDEMPOTENT, 1),
    StoreCall("release", "name epoch secret outcome notes", IDEMPOTENT, 1),
    StoreCall("revoke", "name outcome", WRITE, 1),
    StoreCall("set_meta", "name epoch secret meta", IDEMPOTENT, 1),
    StoreCall("get", "name", READ, 1),
    StoreCall("list", "prefix", READ, 1),
    StoreCall("archive_free_leases", "older", WRITE, 1),
    StoreCall("offer", "kind item lease tier priority deadline picture", WRITE, 1),
    StoreCall("offers", "kind", READ, 1),
    StoreCall("offer_of", "item", READ, 1),
    StoreCall("offer_reason", "oid reason", IDEMPOTENT, 1),
    StoreCall("close_offer", "item why", IDEMPOTENT, 1),
    StoreCall("take", "oid holder ttl max_hold meta", WRITE, 1),
    StoreCall("item", "typ iid", READ, 1),
    StoreCall("items", "typ states", READ, 1),
    StoreCall("put_item", "typ iid state data", WRITE, 1),
    StoreCall("open_singleton", "typ state data seq_key prefix deadline_s", WRITE, 1),
    StoreCall("transition", "typ iid to expect_version data event deadline_s level", WRITE, 1),
    StoreCall("update_item", "typ iid expect_version data", WRITE, 1),
    StoreCall("meta_get", "k default", READ, 1),
    StoreCall("meta_set", "k v", IDEMPOTENT, 1),
    StoreCall("next_seq", "key", WRITE, 1),
    StoreCall("events", "after limit", READ, 1),
    StoreCall("item_events", "typ iid", READ, 1),
    StoreCall("mark_notified", "seq", IDEMPOTENT, 1),
    StoreCall("record_events", "rows", IDEMPOTENT, 1),
    StoreCall("derived", "", READ, 1),
    StoreCall("trace_events", "query", READ, 2),
    StoreCall("derived_check_runs", "", READ, 2),
)

# Store's public methods no client calls: the server's own (its tick, its API's contact clock, the connection)
SERVER_ONLY = ("backfill", "backfill_check_runs", "contact", "db", "prepare")

STORE_API_VERSION = max(c.since for c in STORE_CALLS)


def served_calls(version: int = STORE_API_VERSION) -> dict[str, StoreCall]:
    """The calls a store of API `version` serves, by name (an older version: what a pinned server answers)."""
    return {c.name: c for c in STORE_CALLS if c.since <= version}
