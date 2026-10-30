# agentctl: a dependency-injection plan

Status: a plan for the owner to review (2026-10-30). No code has changed for it. It follows the item left open in
docs/simplification-2026-10.md: "injecting Store / Clock / Runner / Forgejo instead of module globals and env reads,
and moving the tests from monkeypatching to fakes".

## 1. Why

agentctl is 18,400 lines in 29 parts that run in ONE namespace (lib/agentctl/__init__.py). Every function reaches its
collaborators through that namespace: `Store()`, `time.time()`, `git(...)`, `gh_json(...)` and `os.environ`. That
works, and the one namespace is deliberate, because a test's `A.x = fake` patch reaches every part. The costs:

- **Tests patch by name.** Tests make 322 `A.<name> = ...` assignments, each with its own save and restore. The
  most patched names are `slug` 14, `Store` 13, `gh_json` 11, `process_cwds` 10, `record_dispatch` 8 and
  `launchctl_kick` 7. A patch that a test forgets to restore leaks into the next test in the same worker. A rename
  breaks tests without any error at the patch site.
- **The clock is everywhere.** There are 191 `time.time()` calls. Only 28 functions take `now_=None`, and each one
  then calls other functions that read the real clock. Deadline and expiry tests have to pass `now_` through by hand,
  or wait for real time to pass.
- **The store is chosen by a hook.** `Store.__new__` is replaced in server.py: `Store()` returns a `RemoteStore`
  whenever `store_url()` names one. Whether a call is local or remote depends on environment variables read at call
  time (`AGENTCTL_ROLE`, `AGENTCTL_STORE_URL`, the state root's store.url). `CurrentStore` exists only to resolve
  that choice again on every call.
- **Subprocesses go through many paths.** There are 186 `git(` calls, 35 `git_ok(`, 45 raw `subprocess.run/Popen`,
  8 `spawn_detached` and 8 `sh(`. Some are bounded by `run_cmd` and some are not. Tests fake them with PATH shims
  (fake_claude.py, fake_forgejo_gh.py) or with real temporary git repositories.
- **Environment reads are scattered.** There are 153 `os.environ` reads and 113 `env_num(...)` reads at call sites.
  config.py's knob scan finds them, but nothing holds them in one place.

The goal is the same behaviour with seams that are explicit: a test builds a context with fakes and nothing global
is patched.

## 2. Coupling map today

Counts are per part (lib/agentctl/<part>.py), from grep, rounded. A dash means none.

| part | Store() | time.time() | git/git_ok | subprocess | Forgejo (gh_json/api/FORGEJO_GH) | os.environ / env_num |
|---|---|---|---|---|---|---|
| core | - | - | 3 | 7 | 3 (+ api, token) | 27 / - |
| branches | 4 | 16 | 13 | 3 | 5 | 24 / 7 |
| ciwhy | 2 | 4 | 7 | - | 9 | 2 / - |
| red | 2 | 6 | 16 | 1 | 1 | 3 / 11 |
| fixer | 9 (+CurrentStore) | 12 | 17 | 3 | 3 | 12 / 9 |
| owner | 1 (+CurrentStore) | 4 | 5 | - | - | 1 / 4 |
| gov | - | 9 | - | 2 | - | 8 / 10 |
| slots | 2 | 9 | 33 (+8 sh) | 1 | - | 10 / 9 |
| landwatch | - | 8 | 5 | - | 1 | - / 5 |
| waiting | - | - | 19 | - | - | - / - |
| queue | 8 | 8 | 28 | 3 | - | 3 / 6 |
| remote | 3 | 15 | 8 | 1 | 1 | 3 / 3 |
| batch | 8 (+1 RemoteStore) | 10 | 13 | - | - | 5 / 6 |
| tools | 1 | 2 | 15 | 6 | 2 | 2 / 1 |
| cleanup | - | 3 | 26 | 1 | 1 | 2 / 2 |
| store | 5 | 3 | - | - | - | - / - |
| server | 9 (+2 RemoteStore) | 11 | 9 | 2 | - | 11 / 11 |
| recovery | 7 | 8 | 1 | 3 | - | 6 / 1 |
| status, live | 2 (+1 RemoteStore) | 35 | - | 2 | - | 7 / 4 |
| watchdog | 5 | 15 | 2 | 2 | 1 | 3 / 14 |
| others (wait, owned, inbox, upkeep, maint, config, cli) | 2 | 16 | 1 | 9 | 5 | 22 / 9 |

The seams that already exist:

- `Store(path, clock)` already takes a clock, and `RemoteStore(url, tok, clock)` has the same methods (`STORE_RPC`).
  Store is already an interface in practice. Only the way callers obtain one is global.
- `run_cmd` is a bounded runner for short commands, and `git` and `git_ok` are bounded wrappers. They are the natural
  Runner, but they are not passed in.
- `gh_json`, `api` and `token` together are the whole Forgejo surface: CLI reads through forgejo-gh and REST through
  urllib. Tests fake them with `A.gh_json = ...` or `AGENTCTL_FORGEJO_GH=fake_forgejo_gh.py`.
- Many functions already take `now_=None` (28 of them) and `store=None`. This plan makes that habit uniform.

## 3. Target interfaces

These are small Protocol-style classes in a new part `ports.py`, loaded first after `core`. Each has one real
implementation (today's code, moved) and one fake in `tests/fakes.py`.

```python
class Clock:            # the real one: time.time / time.monotonic / time.sleep
    def now(self) -> float: ...
    def monotonic(self) -> float: ...
    def sleep(self, s: float) -> None: ...

class Runner:           # the real one: today's run_cmd / git / git_ok / spawn_detached, bounded
    def run(self, argv, *, cwd=None, env=None, input=None, timeout=60) -> CompletedProcess: ...
    def git(self, repo, *args, check=True, input=None, timeout=600): ...   # str when check, else CompletedProcess
    def git_ok(self, repo, *args) -> bool: ...
    def spawn(self, argv, *, cwd, env, log) -> int: ...                     # detached, reaped (today's spawn_detached)

class Forgejo:          # the real one: gh_json + api + token, with tunnel_retry
    def runs(self, repo, workflow=None, branch=None, limit=20) -> list: ...
    def run(self, repo, run_id) -> dict: ...
    def jobs(self, repo, run_id) -> list: ...
    def log(self, repo, run_id, job=None) -> str: ...
    def commit(self, repo, sha) -> dict: ...
    def api(self, repo, method, path, params=None, body=None): ...          # the escape hatch, as today
    def token(self, repo) -> str: ...

# Store: no new class. Store and RemoteStore already share STORE_RPC; a `StorePort` Protocol lists those methods so
# the fake can be checked against it.

class Env:              # the knobs, read once per process (config.py already lists them)
    def num(self, name, default) -> float: ...
    def get(self, name, default=None) -> str | None: ...

class Ctx:              # what a command or a tick carries
    store: StorePort; clock: Clock; runner: Runner; forgejo: Forgejo; env: Env; role: str
```

How it is wired:

- `ctx()` builds the real Ctx once per process: the role from `host_role()`, the Store from `store_url()`. It replaces
  the `Store.__new__` hook and `CurrentStore`. `ctx().store` resolves again when `store.url` changes, which is what
  CurrentStore does today.
- New and migrated functions take `ctx` as their FIRST parameter: `tick_batch(ctx, repo, d)`. Lower helpers take only
  the port they use: `land_hold(store, clock)`. The tick loop passes one Ctx down the steps.
- During the migration, a module-level `CTX` (the real ctx) is the default: `def f(..., ctx=None): ctx = ctx or CTX`.
  Callers that have not moved yet keep working, and each step can land on its own.
- The one namespace stays. Injection does not need packages or imports. The parts remain one namespace, and the
  `ports` part is just loaded early.

## 4. Migration order

Each step is one commit (or a few), behaviour-preserving, with the full suite green, landed and auto-pinned before
the next step. The order goes from smallest blast radius and most test pain to largest.

1. **ports.py with real implementations only.** Add Clock, Runner, Forgejo, Env and Ctx, each delegating to today's
   functions (`RealRunner.git = git`). Add `CTX`. No call site changes. Add tests that check the real ports delegate.
2. **tests/fakes.py.** Add FakeClock (settable, with `advance`), FakeRunner (records argv and returns scripted
   results, and it can also drive a real temporary git repository), FakeForgejo (in-memory runs, jobs and logs:
   fake_forgejo_gh.py's data model reused), an in-memory Store (today's `Store(path=tmp)` already serves), and
   FakeEnv. Add a `make_ctx(**overrides)` helper. Write no migrated tests yet, only the fakes' own tests.
3. **Clock first, in the store and the machines.** Store already takes `clock`. Thread `ctx.clock` through store.py,
   the machines' deadline handling, `land_hold`, conditions and recovery. These are the deadline and expiry paths,
   where tests now pass `now_` or wait. Remove `now_=None` parameters as each function gets the clock.
4. **Store acquisition.** Replace `Store()` (69 calls) with `ctx.store` part by part: batch, queue, recovery,
   watchdog, server, fixer and owner, status. Then delete `Store.__new__` and `CurrentStore`, because
   `ctx().store` resolves the URL. The server-mode tests (Phase2Base) build a Ctx with a RemoteStore, instead of
   setting AGENTCTL_ROLE and AGENTCTL_STORE_URL in os.environ.
5. **Forgejo.** Route `gh_json`, `api` and `token` users (ciwhy, red, branches, landwatch, wait, watchdog, tools)
   through `ctx.forgejo`. Tests that set `A.gh_json = ...` (11) and the fake forgejo-gh subprocess users move to
   FakeForgejo. The PATH-shim fake stays for the end-to-end tests that run the real CLI.
6. **Runner: subprocess first, then git.** Move raw `subprocess.run/Popen` (45) and `spawn_detached` (8) to
   `ctx.runner`, then `git` and `git_ok` (221), one part per commit. The slots and cleanup parts (59 git calls) go
   last. Tests that need real git keep a real temporary repository behind the real Runner. Only process-shaped
   fakes (`process_cwds`, `launchctl_kick`, `run_cmd`, the hung-sbt fakes) move to FakeRunner.
7. **Env.** Replace `env_num("AGENTCTL_X", D)` and `os.environ.get("AGENTCTL_X")` with `ctx.env.num` and
   `ctx.env.get`. config.py's knob scan learns the new call shape, and its test keeps AGENTCTL.md's table in step.
   Identity and environment values for child processes (FIXER_IDENTITY_ENV, `token_env`, `detached_env`) stay
   explicit environment handling: they are what a child receives, not knobs.
8. **Clean-up.** Remove the `ctx=None` defaults where every caller passes a Ctx, then the module-level `CTX` users
   outside `cli.main` and the tick loop. Add a lint test, like DocCommentPlacement in movies, that fails on new
   `time.time()`, `Store()`, `subprocess.` or `os.environ` reads outside ports.py and the allow-listed places.

## 5. How tests move

- **New tests** use `make_ctx(clock=FakeClock(t0), forgejo=FakeForgejo(runs=[...]))` and call the function with it.
  Nothing global is patched, so there is no save and restore.
- **Existing tests move with their part**, in the same commit as the part's migration: each `A.x = fake` whose `x`
  is now a port becomes a ctx override. Tests whose patches are not ports (`A.record_dispatch`, `A.start_watch`,
  `A.close_worktree`: domain functions) keep patching. They are the next seam, outside this plan.
- **The harness** (tests/harness.py: HOME, PATH, the state root, per-test timeout) stays. The state root and HOME
  isolation remain the backstop for anything that still touches disk.
- **Time-based tests** (deadlines, expiry, nudges, leases) use FakeClock.advance, never sleeps. Expected effect: the
  slowest tests (watchdog, landwatch, fixer wrapper) get faster, and NoSleep-style flakes go away.
- **Measure:** the count of `A.<name> = ` assignments (322 today) and the suite time (about 80 s on 6 workers). The
  plan's success criterion is fewer than 120 assignments, no assignment to a port name, and no suite-time regression.

## 6. Risks

- **A silent change of behaviour in a tick path.** The reconciler on ci-runner is the one writer. Mitigation: one part
  per commit, the full suite on every push (pre-push and tests.yml), and the auto-pin only on green. Keep each
  step's diff mechanical, with no logic edits mixed in.
- **Store resolution.** Removing `Store.__new__` touches every caller at once if it is done carelessly. Mitigation:
  step 4 keeps the hook until the last `Store()` call is gone. A test asserts there are zero bare `Store()` calls
  before the hook is deleted.
- **Long-running processes and re-exec.** The processor and the fixer wrappers re-exec onto new code
  (`restart_if_changed`). The Ctx is rebuilt at exec, so this is safe, but a Ctx must never be pickled into a record.
  Records keep holding plain data.
- **Child process environments.** Fixers, executors and lands get their identity through the environment. Env
  injection must not change what children inherit. Mitigation: step 7 excludes child-environment builders, and
  test_red_keying's environment assertions stay as they are.
- **Concurrent agents editing ~/bin.** The migration touches every part. Mitigation: one maintainer (as in the
  simplification), short-lived commits, and a note in AGENTCTL.md while it runs.
- **Scope creep into domain seams.** `record_dispatch`, `start_watch` and similar are tempting next. They are out of
  scope here.

## 7. Estimate

| step | size | effort (one maintainer agent) |
|---|---|---|
| 1 ports.py | about 200 lines | 0.5 day |
| 2 fakes + make_ctx | about 300 lines of test code | 0.5 day |
| 3 Clock | 191 call sites; about 60 functions touched in store, recovery, batch and watchdog first | 1 day |
| 4 Store | 69 + 2 call sites, the hook, Phase2Base | 1 day |
| 5 Forgejo | about 32 call sites, 11 + 6 test files | 1 day |
| 6 Runner | 45 subprocess + 221 git calls | 2 days |
| 7 Env | 266 reads, config scan | 1 day |
| 8 clean-up + lint | | 0.5 day |
| **total** | about 40 commits | **about 7.5 working days**, landed step by step |

Each step can stop at a green state, so the owner can pause after any step. Steps 1 to 4 (about 3 days) buy most of
the test simplification: clock-driven deadline tests, and no environment-switched store. Steps 5 to 8 are worth
doing only if steps 1 to 4 deliver as measured.

## 8. Decisions for the owner

1. Ctx as the first parameter everywhere (proposed), or ports only where used (smaller diffs, but less uniform)?
2. Keep the one-namespace loading (proposed), or use this migration to turn the parts into real modules? That is a
   separate and larger change, and this plan does not need it.
3. Is the lint ban in step 8 wanted? It is what keeps the seams from eroding.
4. Go ahead with steps 1 to 4 now, and decide on 5 to 8 after measuring?
