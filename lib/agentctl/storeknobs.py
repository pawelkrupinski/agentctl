# agentctl, part `storeknobs`: the store, machine and forge knobs: the store's contact and token, the roles, the state root, the GitHub apps
# A real module (lib/agentctl/__init__.py MODULES): its own imports, the standard library's and MODULES' only.

import os
from agentctl.knobread import env_number, env_text


# A MISSING TOKEN IS SAID ON ITS TRANSITION (2026-10-30: the laptop processor's service.log held ~842,000 lines of
# "could not read the movies token" -- every step of every tick, and the inbox note's own failure, said it
# again). A long-lived process says it once when the token goes missing (and notes the condition then), then at most a
# summary every TOKEN_MISSING_SUMMARY s, and once more when it is back. Its callers' NoToken is quiet (run_step).
TOKEN_MISSING_SUMMARY = 600  # s between the summaries of a still-missing token (said on its transition first)


def knob_token_summary() -> float:
    return env_number("AGENTCTL_TOKEN_SUMMARY", TOKEN_MISSING_SUMMARY)


# A SILENT MACHINE'S LEASE CLOCKS PAUSE (2026-11-03: the family owners restarted 22 / 11 / 3 times, ended "STOPPED: the
# owner's heartbeat lease was lost" -- the laptop asleep on battery, or the tunnel to this store down (Errno 61 every
# ~30 min that day), past OWNER_TTL: every laptop lease lapsed at once and the reconciler re-offered their reds). The
# store records each machine's last contact (contact(): every store RPC the laptop's agentctl makes says which machine
# it is). A lease whose holder's machine (its holder's prefix, `laptop:`) has been silent past CONTACT_GAP has its clock
# paused from then on: read (get, list, renew, acquire, take) as extended by the silence beyond CONTACT_GAP, and on the
# machine's next contact extended by that for real -- so a lease live when its machine went quiet is still live when it
# comes back, and nobody takes it meanwhile. A machine still talking (its sync loop beats every minute) pauses nothing:
# a dead wrapper's leases on a laptop that is awake lapse as before. Bounded: CONTACT_PAUSE_MAX, then they lapse.
CONTACT_GAP = 120               # s of a machine's silence before its leases' clocks pause ($AGENTCTL_CONTACT_GAP)


CONTACT_PAUSE_MAX = 12 * 3600   # s a silent machine's leases are paused at most ($AGENTCTL_CONTACT_PAUSE_MAX)


def knob_contact_gap() -> float:
    return env_number("AGENTCTL_CONTACT_GAP", CONTACT_GAP)


def knob_contact_pause_max() -> float:
    return env_number("AGENTCTL_CONTACT_PAUSE_MAX", CONTACT_PAUSE_MAX)


DEFAULT_REMOTE = "origin"  # GitHub's remote name in every clone (laptop and ci-runner)


GHAPP_LANDER_REPOS_DEFAULT = "pawelkrupinski/movies"


def knob_sock_dir() -> str:
    return env_text("AGENTCTL_SOCK_DIR")  # the token hand-off sockets' directory (default /tmp/agentctl-<uid>)


def knob_remote() -> str:
    return env_text("AGENTCTL_REMOTE").strip() or DEFAULT_REMOTE  # the push and fetch remote of every clone


def knob_state_root() -> str:
    return env_text("AGENTCTL_STATE_ROOT")  # the state root (default ~/.cache/agentctl)


def knob_scratch() -> str:
    return env_text("AGENTCTL_SCRATCH")  # the scratch directory (default: the Claude session's, else the state root)


def knob_host() -> str:
    return env_text("AGENTCTL_HOST")  # ci-runner on the reconciler's host (infra sets it)


def knob_role() -> str:
    return env_text("AGENTCTL_ROLE")  # server forces the server role


def knob_github_api() -> str:
    return env_text("AGENTCTL_GITHUB_API", "https://api.github.com")  # the GitHub API base


def knob_identity() -> str:
    return env_text("AGENTCTL_IDENTITY")  # github-app: credentials come from the GitHub Apps (ci-runner)


def knob_github_app(role: str) -> tuple[str, str]:
    """(app id, private key file) of a GitHub App role ($AGENTCTL_GITHUB_<ROLE>_APP_ID / _KEY_FILE), "" when unset."""
    return (env_text(f"AGENTCTL_GITHUB_{role.upper()}_APP_ID").strip(),
            env_text(f"AGENTCTL_GITHUB_{role.upper()}_KEY_FILE").strip())


def knob_github_lander_repos() -> str:
    return env_text("AGENTCTL_GITHUB_LANDER_REPOS", GHAPP_LANDER_REPOS_DEFAULT)  # the repos the lander App writes to, comma-separated


def knob_store_token() -> str:
    return env_text("AGENTCTL_STORE_TOKEN")  # the store token an agent's environment carries


# A RUN NO SCHEDULER CAN SEE IS NO CHECK (2026-11-20, phase7-houses: precommit-check run 44682642375 sat `queued` from
# 05:41Z, its check jobs never started, while later runs' jobs ran: GitHub's own run listing -- `actions/runs?status=
# queued` and `?status=in_progress` -- left it out, and ci-runner's runner scheduler, which reads exactly those listings,
# never started a runner with its pin label. agentctl waited for REMOTE_TIMEOUT (2 h) on it, the change's deadline
# laps meanwhile). A run whose check jobs have none started and which neither listing names for REMOTE_GHOST is
# cancelled and given up (no verdict: re-checked, keeping its place), never waited on for hours.
REMOTE_GHOST = 10 * 60  # s ($AGENTCTL_REMOTE_GHOST)


# TRANSIENT STORE FAILURES ARE RETRIED (2026-10-29: fixer -8's watcher died on one `POST /v1/store/get: [Errno 54]
# Connection reset by peer`). Reads and the calls that are harmless twice (a renew answers no_progress, a release of a
# released lease is a no-op) retry on any transient failure; the rest only when the request never left (refused).
STORE_RETRIES = 4


STORE_RETRY_BASE = 0.5   # s; doubled each attempt: 0.5 + 1 + 2 + 4 = 7.5 s at most


CLONE_TIMEOUT = 1800


CLONE_REPAIR_RETRY = 15 * 60  # a repair that failed is tried again after this


CLONE_LOCK_WAIT = 300         # the swap waits this long for the clone's users to finish, then tries again next tick


def knob_clone_lock_wait() -> float:
    return env_number("AGENTCTL_CLONE_LOCK_WAIT", CLONE_LOCK_WAIT)


def knob_clone_repair_retry() -> float:
    return env_number("AGENTCTL_CLONE_REPAIR_RETRY", CLONE_REPAIR_RETRY)


def knob_clone_timeout() -> float:
    return env_number("AGENTCTL_CLONE_TIMEOUT", CLONE_TIMEOUT)


def knob_remote_ghost() -> float:
    return env_number("AGENTCTL_REMOTE_GHOST", REMOTE_GHOST)


def knob_store_retries() -> float:
    return env_number("AGENTCTL_STORE_RETRIES", STORE_RETRIES)


def knob_store_retry_base() -> float:
    return env_number("AGENTCTL_STORE_RETRY_BASE", STORE_RETRY_BASE)


def knob_state_file() -> str:
    return env_text("AGENTCTL_STATE_FILE")  # the CI state file (default state.json under the state root)


def knob_movies_remote() -> str:
    return env_text("AGENTCTL_MOVIES_REMOTE")  # the movies repository's clone URL the server's clone fetches from (default: the clone's own)


def knob_bin_repo() -> str:
    return env_text("AGENTCTL_BIN_REPO")  # the agentctl checkout (default: the one the tool runs from)


def knob_gh_cli() -> str:
    return os.environ.get("AGENTCTL_GH_CLI", "gh")  # the GitHub CLI


def knob_runners_page_cache() -> str:
    return env_text("AGENTCTL_RUNNERS_PAGE_CACHE")  # the runners page's last good copy (default ~/.cache/ci-runners-page/last-good.json)


def knob_movies_repo() -> str:
    return env_text("AGENTCTL_MOVIES_REPO")  # the movies checkout (default ~/projects/movies)


def knob_store_url() -> str:
    return env_text("AGENTCTL_STORE_URL")  # the remote store's URL (set: every process uses it)


GITHUB_STATUS_URL = "https://www.githubstatus.com/api/v2/incidents.json"


def knob_githubstatus_url() -> str:
    return os.environ.get("AGENTCTL_GITHUBSTATUS_URL", GITHUB_STATUS_URL)  # the GitHub status incidents feed


def knob_stats_url() -> str:
    return env_text("AGENTCTL_STATS_URL")  # the stats collector's URL


def knob_store_secret() -> str:
    return env_text("AGENTCTL_STORE_SECRET")  # the store's shared secret (default: the built-in one)


def knob_tunnel_state() -> str:
    return env_text("AGENTCTL_TUNNEL_STATE")  # the tunnel's health file (default tunnel-health.json under the state root)


def knob_internal_trees_dir() -> str:
    return env_text("AGENTCTL_INTERNAL_TREES_DIR")  # the internal checkouts' directory (default internal-trees under the state root)


def knob_hostname() -> str:
    return env_text("AGENTCTL_HOSTNAME")  # this host's name (default: the system's)
