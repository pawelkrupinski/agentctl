# Landing reconciler, phase 2: owner runbook

Status: prepared on 2026-10-28 and updated the same day with the owner's answers. Updated 2026-10-29: the
agentctl side is implemented and tested (~/bin master, "agentctl phase 2" commits; `tests/test_phase2.py`), and
the agentctl alerts go to a null receiver (owner: "No Slack integration"). Nothing is deployed, created or
granted yet.
Design: `~/bin/docs/landing-reconciler-design.md` (§3, §6.3, §10, §13 phase 2, §15, owner decisions).

## What is prepared

Two local infra branches. Neither is pushed. The commit ids below are from before the rebases in
steps 1 and 4.

**`agentctl-phase2`** changes no host when merged. The module is not imported, and the settings
changes do nothing until `bin/forgejo-settings` runs.

- `fa01826b`: declares the repository `pawelkrupinski/agentctl`.
- `33212b68`: adds `nix/hosts/ci-runner/agentctl.nix` and `agentctl-pin.nix`.
- `340b50b1`: adds `ci-metrics`, the read-only metrics identity. ci-lander becomes push-only.
- `7792b415`: moves the `ci-metrics` declaration so the step 4 rebase applies cleanly.

**`agentctl-phase2-enable`** sits on top of the first branch and **turns phase 2 on** at the next
ci-runner deploy. Auto-apply may also start the new units on its own.

- `81b8e623`: adds the `ci-lander` user, the `lander` team (write on movies only), and push
  protection on movies `main` (pawel and ci-lander may push).
- `da5a7e38`: adds `agentctl.rules` and its tests.
- `60a1bc1b`: imports `agentctl.nix` and adds 3014 to the tunnel's `permitOpen`.
- `13b78ede`: merges `agentctl-phase2` in.
- `2c8049ce`: removes the Slack route. Removes the escalation alert. W1 now excludes the time main was
  red.
- `05be2dee` (2026-10-29): routes every `component="agentctl"` alert to `agentctl-null`, a receiver with
  no integration, ahead of the env routes (owner: "No Slack integration"). `amtool check-config` and
  `amtool config routes test component=agentctl` (→ `agentctl-null`) pass, and so does
  `bin/check-promtool-rules`.

Both rebases were rehearsed in a scratch clone. They apply cleanly and give exactly this tree.

### The units on ci-runner (`agentctl.nix`)

| Unit | What it does |
|---|---|
| `agentctl-src` | Exports the pinned ~/bin commit from Forgejo's own repository on disk. |
| `agentctl-lander-bot` | Creates `ci-lander`, the push identity. Its token has only `write:repository`. Its password, token and ssh key live only in `/var/lib/agentctl-lander` (0700). |
| `agentctl-metrics-bot` | Creates `ci-metrics`, the scrape identity. It belongs to no team and can read no repository. Its token has only `read:user`. The token lives only in `/var/lib/agentctl-metrics-bot` (0700). No password is kept. |
| `agentctl-store` | Binds `10.1.0.3:3014` and checks every caller's token with Forgejo. `pawel` gets the full API. `ci-metrics` may call `GET /metrics` and nothing else. |
| `agentctl-reconciler` | Runs as `ci-lander`. `Type=notify`, `WatchdogSec=120`, `Restart=always`. |
| `agentctl-store-backup` | Makes an hourly sqlite backup. |
| `agentctl-metrics` | Every minute, copies `/metrics` into the node-exporter textfile directory as `ci-metrics`. |

Logs go to the journal, which is shipped to VictoriaLogs. Query them with
`_SYSTEMD_UNIT:agentctl-reconciler.service`.

### Alerts and escalations

There is no Slack integration for agentctl (owner, 2026-10-29). Every agentctl alert carries
`component: agentctl`, which alertmanager's first route sends to `agentctl-null`: a receiver with no
integration. The alerts stay visible in Prometheus (`ALERTS{component="agentctl"}`) and reach no Slack
channel.

Escalations are delivered by agentctl itself, on the laptop, as an inbox entry plus a macOS
notification (design §8.2). The laptop watchdog owns W5, W6 and W7.

### Backups: nothing leaves ci-runner

The store's 24 hourly copies stay in `/var/lib/agentctl/backup`. Neither the store nor any bot
credential is copied off the host, on purpose:

- **The store is rebuildable.** Losing it loses only attempt counters and terminal history. Items are
  re-derived from the `land/*` refs and the Forgejo runs (design §16).
- **The credentials can be re-minted.**
  - ci-lander: delete the Forgejo user and `/var/lib/agentctl-lander`, then restart
    `agentctl-lander-bot`. It mints a new user, token and key.
  - ci-metrics: delete `/var/lib/agentctl-metrics-bot/token.env` and restart `agentctl-metrics-bot`.

## Owner steps

Run these in order. Each one is a full `!` line. Steps 1 to 3 can run any time. Step 4 must wait until
phase 1 is done.

**1. Merge the inert branch.** This changes no host.

```
! cd ~/bitcashier/infra && git pull --rebase -q forgejo main && git rebase -q main agentctl-phase2 && git checkout -q main && git merge --ff-only -q agentctl-phase2 && git push forgejo main
```

**2. Create the repository.** The dry run must show `+ repo pawelkrupinski/agentctl`. It also shows the
warning `user ci-metrics missing: created on ci-runner by its own unit`, which is expected.

```
! cd ~/bitcashier/infra && bin/forgejo-settings --dry-run
```

```
! cd ~/bitcashier/infra && bin/forgejo-settings
```

**3. Push ~/bin.** The local branch `master` is pushed as Forgejo's `main`. ~/bin's history was scanned
on 2026-10-28 for keys and tokens, and none were found.

```
! git -C ~/bin remote add forgejo ssh://forgejo-git/pawelkrupinski/agentctl.git && git -C ~/bin push forgejo master:main && git -C ~/bin branch -q --set-upstream-to=forgejo/main master && git -C ~/bin log --oneline -1 forgejo/main
```

**4. Pin the phase-1 revision and merge the enable branch.** Do this once phase 1 is done and its code
implements the contract below.

```
! cd ~/bitcashier/infra && git -C ~/bin push forgejo master:main && rev=$(git -C ~/bin rev-parse forgejo/main) && git pull --rebase -q forgejo main && git rebase -q main agentctl-phase2-enable && git checkout -q main && git merge --ff-only -q agentctl-phase2-enable && sed -i '' "s/rev = \"[0-9a-f]*\";/rev = \"$rev\";/" nix/hosts/ci-runner/agentctl-pin.nix && git diff --stat && git commit -qam "agentctl phase 2: pin ~/bin $rev" && git push forgejo main
```

**5. Deploy ci-runner.** This creates `ci-lander` and `ci-metrics`. Preview first, then apply. The
preview should start the seven `agentctl-*` units and restart no runner.

```
! cd ~/bitcashier/infra && bin/deploy ci-runner
```

```
! cd ~/bitcashier/infra && bin/deploy ci-runner --apply
```

**6. Grant ci-lander its permissions.** This step needs the users from step 5. The dry run must show:

- `+ team pawelkrupinski/lander` and its member `ci-lander`;
- `+ repo pawelkrupinski/movies branch protection main`;
- `= user ci-lander` and `= user ci-metrics`.

`ci-metrics` gets no team. Nothing is granted to it.

```
! cd ~/bitcashier/infra && bin/forgejo-settings --dry-run
```

```
! cd ~/bitcashier/infra && bin/forgejo-settings
```

**7. Deploy monitoring.** This loads `agentctl.rules` and the `agentctl-null` route: the alerts reach no
Slack channel.

```
! cd ~/bitcashier/infra && bin/deploy monitoring && bin/deploy monitoring --apply
```

**8. Laptop: forward port 3014 through the tunnel.**

```
! perl -0pi -e 's|(    LocalForward 3010 10\.1\.0\.3:3010\n)|$1    # the agentctl store (laptop sync: leases, submissions, events); infra ci-runner permitOpen\n    LocalForward 3014 10.1.0.3:3014\n|' ~/.ssh/config && grep -n 'LocalForward 3014' ~/.ssh/config && launchctl kickstart -k gui/$(id -u)/net.bitcashier.forgejo-tunnel
```

**9. Verify.**

Check the units, the file permissions and the metrics file:

```
! ssh ci-runner 'systemctl is-active agentctl-src agentctl-lander-bot agentctl-metrics-bot agentctl-store agentctl-reconciler agentctl-metrics.timer agentctl-store-backup.timer; sudo stat -c "%a %U %n" /var/lib/agentctl-lander /var/lib/agentctl-lander/id_ed25519 /var/lib/agentctl-metrics-bot/token.env; grep -c "^agentctl_" /etc/node-exporter/textfile.d/agentctl.prom'
```

Expected:

- every unit is `active`;
- the lander directory is `700 ci-lander`, and its key is `400 ci-lander`;
- the ci-metrics token is `400 root`;
- the count is more than 0.

Check what ci-metrics can and cannot do. Its token never leaves the host:

```
! ssh ci-runner 'sudo bash -c '\''. /var/lib/agentctl-metrics-bot/token.env; for u in http://10.1.0.3:3014/metrics http://10.1.0.3:3014/v1/state http://forgejo.internal:3000/api/v1/repos/pawelkrupinski/movies; do echo "$(curl -s -o /dev/null -w %{http_code} -H "Authorization: token $GH_TOKEN" $u) $u"; done'\'''
```

Expected: `200` for `/metrics`, and `403` for `/v1/state`. Movies answers `404`, because Forgejo hides
a private repository from a user who cannot read it.

Check the store as pawel, and without a token:

```
! echo "no token: $(curl -s -o /dev/null -w '%{http_code}' http://localhost:3014/v1/state)" && curl -sf -H "Authorization: token $(secrets get movies FORGEJO_API_TOKEN)" http://localhost:3014/v1/state | jq '{tick_at, version}'
```

Expected: `no token: 401`, then a `tick_at` less than 15 s old.

Check that Forgejo has no drift left:

```
! cd ~/bitcashier/infra && bin/forgejo-settings --dry-run 2>&1 | grep -E 'lander|metrics|agentctl'
```

Expected: every line starts with `=`.

**10. Laptop: move onto ci-runner's store.** (Done 2026-10-29. Since then `store import` and `store use --local` are gone with the laptop's store, docs/simplification-2026-10.md item 2: the line below is history, not a runnable step. Rollback is the revert below alone; the laptop's sync keeps pointing at :3014.) Until now the laptop keeps its own queue, and ci-runner's
reconciler ticks an empty one. This stops the laptop's processor, copies its live leases (same epoch and
secret, so their holders keep renewing them), open reds and conditions and its queued changes into the
remote store, switches every agentctl process on the laptop to it (`~/.cache/agentctl/store.url`), and
starts the processor again as the sync. The sync needs the token handed over once, like after any restart.

```
! launchctl bootout gui/$(id -u)/net.bitcashier.agentctl-waiting; cd ~/projects/movies && AGENTCTL_INTERACTIVE=1 ~/bin/agentctl store import --to http://localhost:3014 && AGENTCTL_INTERACTIVE=1 ~/bin/agentctl store use http://localhost:3014 && ~/bin/agentctl tick service install && sleep 5 && AGENTCTL_INTERACTIVE=1 ~/bin/agentctl land status | head -5
```

Expected: `store import -> ...: N live lease(s), ...`, `store: http://localhost:3014 ...`, then `land
status` listing the changes ci-runner now holds. Back to the local store: `~/bin/agentctl store use
--local` and `launchctl kickstart -k gui/$(id -u)/net.bitcashier.agentctl-waiting`.

**Rollback.** Revert the enabling commit and deploy again. The store's database stays in
`/var/lib/agentctl`.

```
! cd ~/bitcashier/infra && git pull --rebase -q forgejo main && git revert --no-edit $(git log --format=%H -1 --grep='ENABLES phase 2') && git push forgejo main && bin/deploy ci-runner
```

## What the module expects from agentctl (the phase-2 code contract)

The module runs `python3 /var/lib/agentctl-src/<rev>/agentctl`. If the phase-2 code names things
differently, change `agentctl.nix`.

### Commands

**`agentctl store serve --listen ADDR:PORT --forgejo-url URL --allow-user U... --metrics-user U...`**

- Serves the §6.3 API and `GET /metrics`, from `$AGENTCTL_STATE_ROOT/store.db`.
- Every request carries `Authorization: token`. The store checks it with `/api/v1/user` and caches the
  answer for 5 min.
- An `--allow-user` gets the whole API. A `--metrics-user` gets `GET /metrics` only.
- Any other user gets 403. A request with no token gets 401.

**`agentctl tick`**

- The reconciler loop.
- Sends sd_notify `READY=1` when it is ready, and `WATCHDOG=1` after every tick (through
  `$NOTIFY_SOCKET`).
- On SIGTERM, exits cleanly between ticks.

### The reconciler's environment

- `AGENTCTL_STATE_ROOT=/var/lib/agentctl/state`
- `HOME=/var/lib/agentctl`
- `AGENTCTL_HOST=ci-runner`
- `AGENTCTL_FORGEJO_URL`
- `AGENTCTL_FORGEJO_GH`: a gh-shim wrapper for the actions API on `127.0.0.1:3010`
- `AGENTCTL_MOVIES_REMOTE`: ssh to Forgejo's port 2222
- `GIT_SSH_COMMAND`: ci-lander's key, with pinned `known_hosts`
- `GIT_COMMITTER_NAME` and `GIT_COMMITTER_EMAIL`: ci-lander
- `GH_TOKEN`: ci-lander's token, `write:repository` only

### Gauges `/metrics` must export

The header of `agentctl.rules` defines each one.

| Gauge | Notes |
|---|---|
| `agentctl_reconciler_tick_timestamp` | |
| `agentctl_oldest_change_age_seconds` | Excludes the time main was red since the change was submitted. |
| `agentctl_red_unleased_seconds` | |
| `agentctl_leases_expired_unreaped` | |
| `agentctl_items_past_deadline` | |

### What the code does (2026-10-29), and where it differs from the design

- `store serve`: the §6.3 routes, plus `POST /v1/store/<method>` for every Store method (RemoteStore's
  transport) and `GET /v1/leases`. A store error (StaleVersion, TerminalState, LeaseLost) answers 409 and is
  raised as itself on the laptop. At start every live lease is extended by the store's downtime (§6.2).
  `POST /v1/reds/{sig}:block` is not a route: `red block` runs on the laptop through the RPC.
- `tick` (AGENTCTL_HOST=ci-runner): clones `$AGENTCTL_MOVIES_REMOTE` to `$HOME/movies` as remote `forgejo`
  on its first start, sends `READY=1`, then `WATCHDOG=1` after every bounded step (not only per tick, so a
  long tick never trips `WatchdogSec`), and ends between ticks on SIGTERM. It never runs the laptop's
  steps: the inbox projector, the notifier, the network and tunnel watches, the claims' renewal (a claim's
  activity is in the laptop's worktrees) and the fixer dispatcher. Its checks, and the batch's, run on the
  CI tier (`precommit-check.yml`, decision 1). A change's commits are fetched from `land/<id>`.
- The clone heals itself (2026-10-29: a clone cut off by a crash loop passed for a clone and blocked all
  landing for hours). It is cloned into `.<name>.clone-tmp-<pid>-<ms>` beside the target, verified
  (`forgejo/main` resolves, its commits and tip tree are readable) and renamed into place. A fetch failing
  with a corruption signature marks it suspect (`.<name>.suspect.json`); the "clone health" step probes it
  (`clone_health`) and, broken, re-clones and swaps under `.<name>.clone.lock` (every clone user holds it
  shared). The broken copy stays as `<repo>.broken-<ts>` (one only); a CLONE condition carries git's
  error and closes when the probe passes. A change re-queued 3 times (or 30 min) for the same infra reason
  opens a STALLED condition with the exact error.
- `AGENTCTL_FORGEJO_URL` is read for the API (`<url>/api/v1`) when `AGENTCTL_FORGEJO_API` is unset.
- The laptop: `store use URL` (the switch, kept in the state root so agents' own `land` / `red claim` agree; the
  default is http://localhost:3014). `store import` and `--local` were removed with the laptop's store (2026-10-29). As the sync, `land` pushes
  `land/<id>` and POSTs the change; with the store down it prints `QUEUED-LOCAL` and the sync sends it
  from the outbox.
- WATCH OK on ci-runner closes the laptop worktree the change was landed from: the `watch.ok` event names
  it, and the sync closes it (proven merged and clean only).
- W7 in the laptop watchdog: an event older than 5 min still past the sync's projector cursor is an
  INVARIANT condition, closed by the same check.
- The batch's check on the CI tier is the fast-only set, as on the laptop: its check branch is
  `check/fast-<id>`. movies' `precommit-check.yml` must map that name to `--fast-only`; until that lands
  there, such a branch runs the full `--checks-only` set (stricter, never weaker).

## The laptop after phase 2 (design note)

| launchd job | After phase 2 |
|---|---|
| `net.bitcashier.agentctl-waiting` | **Stays, as the sync.** The label is kept. It submits `land/<entry>`, pulls events into the inbox, and mirrors `/v1/state` into `state.json` every 10 s. It also reports orphans and claim activity, and it is **the escalation notifier**: inbox entry, macOS notification, `:ack`. It never ticks. Decision 7 means no standby and one writer, so the laptop's tick code is deleted. |
| fixer dispatcher | **Stays**, inside the sync or as its own job. It takes `fixer` offers over `RemoteStore`, starts each fixer as a Claude Code background session (`claude --bg`, listed in `claude agents`), and renews its lease by its transcript and worktree. Fixers stay on the laptop (decision 8). |
| `net.bitcashier.agentctl-watchdog` | **Stays, with W5, W6 and W7.** W5 is the tick age from the mirrored `state.json`. W6 is escalations left unnotified, read from the event feed. W7 is the sync's cursor lag. It reports `ci-runner unreachable` and `tunnel down` as two different conditions. It no longer kickstarts the processor. |
| `net.bitcashier.agentctl-maintain` | **Stays** for the reap and the sweep. Its orphan facts go to the sync. |
| `net.bitcashier.forgejo-tunnel` | **Stays**, plus `LocalForward 3014` (step 8). |
| local check executor | **Goes.** Checks run on the CI check tier (decision 1). |

The laptop's local `store.db` goes away after the one-shot import (§13 phase 2, step 3). What stays on
the laptop: the inbox, `state.json` (now a mirror, with `fetched_at`), and the outbox
(`QUEUED-LOCAL`).

## Owner decisions on the open questions (2026-10-28)

1. **No Slack integration.** agentctl alerts go to the `agentctl-null` receiver (2026-10-29). Escalations are delivered
   by agentctl's own notifications on the laptop.
2. **The metrics scrape uses its own read-only identity, `ci-metrics`.** ci-lander stays push-only.
3. **`land/*` is not protected.** ci-lander's write access on movies covers it. Only `main` is
   protected.
4. **There is no off-host backup** of the store or the keys. The store is rebuildable, and the keys can
   be re-minted (see above).
5. **W1 excludes the time main was red.** It is computed in agentctl's gauge.
6. **The pin stays a commit exported from Forgejo's repository on disk.**

## Auto-pin: ci-runner follows agentctl main's green revisions (Agent C, 2026-10-29)

Owner steps, kept in step with ~/Desktop/"claude docs"/agentctl-autopin.md (+ .pdf beside it).
ci-runner runs one pinned revision of ~/bin (agentctl store and reconciler). The auto-pin moves that pin to agentctl
main's newest revision whose `tests.yml` run is GREEN, and restarts only the agentctl units. You keep the veto.

### What is prepared (infra, local, NOT pushed)

Branch `agentctl-autopin` (worktree `~/bitcashier/infra-agentctl-autopin`), on top of `agentctl-ci`:

| commit | what |
|---|---|
| a539a9f1 | `forgejo-settings.nix`: Actions on for `pawelkrupinski/agentctl` (its `tests.yml` runs on the `ubuntu-26.04` tier; no secret or new label needed) |
| 736ce8a5 | `agentctl.nix`: `agentctl-autopin.timer` (every 5 min) + the `agentctl-pin` tool; `bin/prove-agentctl-pin` |
| 6d46c9e2 | `bin/agentctl-pin-bump`: the manual bump, refusing a revision whose CI is not green |

How it decides: a commit of agentctl main that descends from what runs now, is not marked bad, and whose latest
`tests.yml` run on main is `success` (read read-only from Forgejo's database on ci-runner; no token). Red, running,
cancelled or untested revisions are never shipped; it never goes backwards. It restarts `agentctl-src`,
`agentctl-store`, `agentctl-reconciler` and nothing else, and writes an agentctl event (`pin.bumped`). A revision
that does not stay up 45 s is rolled back from automatically, marked bad, and raised as an `escalate` event.

Two pins: infra's `agentctl-pin.nix` (yours) always wins; the host pin counts only on top of the infra pin it was
written over. So a manual bump or an infra rollback always takes effect, and auto-bumps continue from it.

### 1. Push and apply (one push: Actions on + the auto-pin)

```
! cd ~/bitcashier/infra && git fetch -q forgejo && git -C ../infra-agentctl-autopin rebase -q forgejo/main && git checkout -q main && git pull --rebase -q forgejo main && git merge --ff-only -q agentctl-autopin && git push forgejo main && bin/forgejo-settings --dry-run && bin/forgejo-settings && bin/deploy ci-runner
```

The settings dry run should show agentctl's `has_actions` changing to true. The deploy preview should restart only
`agentctl-src`, `agentctl-store`, `agentctl-reconciler` (their ExecStart now runs `/var/lib/agentctl-src/current`)
and start `agentctl-autopin.timer`. Then:

```
! cd ~/bitcashier/infra && bin/deploy ci-runner --apply && ssh ci-runner sudo agentctl-pin status
```

From then on every push to ~/bin main runs `tests.yml`; once it is green, ci-runner moves to it within ~5 minutes.
Until the first green run nothing moves.

### 2. Hold, release, roll back (on ci-runner)

```
! ssh ci-runner sudo agentctl-pin status
! ssh ci-runner sudo agentctl-pin hold "reason"
! ssh ci-runner sudo agentctl-pin release
! ssh ci-runner sudo agentctl-pin rollback
! ssh ci-runner journalctl -t agentctl-autopin -n 40 --no-pager
```

- `hold` writes `/var/lib/agentctl-pin/hold`: auto-bumps stop until `release`.
- `rollback` goes back to the previous revision (or `rollback <rev>`), marks the current one bad (never auto-shipped
  again) and HOLDS. `release` resumes; the next green revision after the bad one ships.
- A wrongly-marked revision: `ssh ci-runner sudo sed -i '/<rev>/d' /var/lib/agentctl-pin/bad`.

A rollback that survives anything, through infra (also the manual bump while the auto-pin is held):

```
! cd ~/bitcashier/infra && bin/agentctl-pin-bump <rev> --backwards --apply
```

`bin/agentctl-pin-bump [<rev>] [--apply]` without `--backwards` is the forward manual bump: it refuses a revision
whose `tests.yml` run is not completed/success (`--untested` only when no run exists at all).
