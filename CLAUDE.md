# agentctl conventions for Claude

agentctl (`~/bin`) holds itself to the movies project's coding and testing standards (the owner, 2026-11-22: "same
coding / testing standards as movies project"). The rules are movies' `~/projects/movies/CLAUDE.md`; this page names
the sections and gives agentctl's own mechanics. The tool's reference is `AGENTCTL.md`.

## Work in a worktree, never in ~/bin

`~/bin` is the LIVE agentctl every process runs (the laptop's processor re-execs onto its commits; ci-runner's
auto-pin ships main within ~5 min). Work in `git -C ~/bin worktree add -b <name> ~/bin-wt-<name> master`, Bash edits
included (an edit in ~/bin trips watchdog WB; `hooks/pre-commit` refuses any commit in the main checkout). Land: `git -C ~/bin-wt-<name> rebase master && git -C ~/bin merge
--ff-only <name> && git -C ~/bin push origin master:main`, then remove the worktree and `git branch -d` the branch.
**Push from your worktree with `python3 devtools/selfpush.py`** (the owner, 2026-11-25): one repository-wide push lock
(flock, `~/.cache/agentctl/selfpush.lock`; waiting for it costs no cores), then under it fetch, rebase onto
origin/main and `git push origin HEAD:main`, so the pre-push hook's full suite runs ONCE on the tree that lands: main
cannot move under the lock. A rebase conflict is aborted and the lock released; a push still refused (a pusher that
skipped the lock moved main) is reported and not retried. A direct `git push` still works, gated by the same hook,
which prints a hint to use this tool. The hook's steps (suite, stress lane, simulator gate) run through
`devtools/govstep.py`: the governor-ticket wait has its own bound (30 min, `$AGENTCTL_GOV_WAIT`) and each step's run
bound starts when the ticket is granted; a wait past it is the distinct refusal "governor busy" (exit 75: load, nothing
was judged), never a test failure. Brief other agents with this command.
Push in small, coherent steps (a regression must be easy to bisect and revert); after a push check
`~/bin/agentctl status` and `~/bin/agentctl red status` still work.

## The tests

- **The commit gate** (movies "Tests: the commit gate"): no change without a test that fails before it and passes
  after it, written first whenever feasible. agentctl's layers: `tests/` unit tests through the harness
  (`tests/harness.py`: hermetic HOME, PATH, git config, per-test timeout), the queue simulator (`tests/queue_sim.py`)
  and its gate (`tests/sim_gate.py`), and the END-TO-END layer (`tests/e2e/`, the owner 2026-11-27): a real `store
  serve`, the server's tick, the laptop's sync and CLI and a real git remote in one temp root per scenario, GitHub a
  stub (`ghstub.py` through `$AGENTCTL_GH_CLI`); `python3 tests/e2e/run.py` (~1 min, the hook's `prepush-e2e` step and
  tests.yml's `e2e` job), `tests/e2e/mutations.py` the proof that each scenario fails when its behaviour breaks.
- **The suite**: `~/bin/agentctl suite [pattern]` from the worktree (= `python3 tests/run.py`), the touched modules
  while working and the full suite before every push, from a non-git cwd with `AGENTCTL_RED_FIXER`, `FIXER_RECORD` and
  `AGENT_NAME` unset. The pre-push hook runs it again on exactly the pushed commit.
- **Coverage before refactoring**: measure the code you are about to move or split (branch coverage, coverage.py in
  `~/.cache/agentctl-quality/venv`; a run: `COVERAGE_PROCESS_START=<rc with parallel = True> <venv>/bin/python
  tests/run.py`, then `coverage combine && coverage report`). Thin code gets characterisation tests first; a state
  machine (red, fixer, queue, batch, watchdog) always.
- **Never rerun to green** (movies "Investigate every red; never rerun to green"): a flaky test is fixed, not rerun;
  read a run's exit status, not a filtered tail.

## The ratchets (hooks/pre-push)

`quality/ratchet.py` holds the production code (`agentctl`, `lib/agentctl/*.py`, the tools of `quality/` and `devtools/`) to three checked-in baselines, the
movies model (movies "Code quality ratchets" and "Coupling"), on the movies repository's own ratchet engine (vendored, below):

| ratchet | baseline | kinds and limits | trailer for a rise |
|---|---|---|---|
| quality | `quality/quality-baseline.tsv` | cognitive 10, cyclomatic 10, inner-defs 2 | `Complexity:` |
| | | method-lines 50, file-lines 500 | `Length:` |
| | | copies 0 (clone groups of 50+ tokens) | `Duplication:` |
| | | cycle 0 (a part in a reference cycle) | `Coupling:` |
| types | `quality/types-baseline.tsv` | mypy-errors 0 per part (`mypy --strict`) | `Typing:` |
| design | `quality/design-baseline.tsv` | suffix / abbreviation 0 (the design rules, below) | `Naming:` |
| | | effect / state / import-io 0 | `Design:` |
| | | fanout 9 (COLLABORATORS a part imports), distance 0.2, layer, databag, datafanout | `Coupling:` |

- A NEW row past a limit or a frozen row RISING refuses the push; a fall passes and is recorded with
  `"$(python3 quality/tools.py)" quality/ratchet.py update` (commit the baseline). A rise lands only with the kind's
  trailer in the commit's FINAL trailer block, beside `Co-Authored-By:`; each commit growing a baseline is judged by its
  own step (one commit's trailer never excuses another's).
- A function is keyed by its qualified name, so moving it to another part keeps its row; a new part split off a big
  one starts its own `file-lines` / `fanout` / `cycle` rows.
- The tools are pinned with hashes in `quality/tools.lock` and installed by `quality/tools.py` into
  `~/.cache/agentctl-quality/tools-<lock digest>-py<ver>/`, by the hook itself. A tool that cannot be installed blocks
  the push with the fix; it is never skipped. `ratchet.py selftest` (the hook runs it first) seeds a violation of every
  kind and must see each refused. Measures: `quality/measure.py` (`--all` for every row), `quality/typecheck.py
  --errors` (every mypy error, mapped back to its part and line).
- REAL MODULES (2026-11-22; `lib/agentctl/__init__.py`): every part is a module importing the standard library and
  the modules before it in `MODULES` (import order, written by `devtools/placemodules.py`), so a part's coupling is the
  modules it imports (a FOUNDATION -- a module importing no module but other foundations, a leaf helper like
  `statefile` -- is no collaborator: extracting a concept into its own module never raises its users' fan-out), and
  mypy checks the modules concatenated in `MODULES` order. What a module must not import -- the remote store -- is
  wired at the composition root (cli: `CurrentStore(process_store)`; the process's `Context`, passed down a call tree as
  ONE parameter `ctx`: `docs/design/contexts.md`) and passed in, never reached by a module calling up nor kept in a cell
  a `wire_*` fills (`tests/test_no_service_locators.py`); a lower module's event for a higher one is
  pulled, not called back (a missing token's note: `storetoken.take_token_notes`, taken by the tick's recovery step). The tests read and patch
  the modules as one object, `A` (`tests/agentctl_view.py`: `A.<name> = fake` replaces the name in every module
  binding it), until the collaborators are injected.
- The restructuring's tools (`devtools/`, tested by `tests/test_devtools.py`): `intra.py` (each top-level definition's
  reads of its own module's names: a split's seams), `movedefs.py` (a pure move of top-level definitions between
  modules, `--new` making one, `--from` the one module to cut a twice-bound name from), then `mkmodule.py` (the imports a module now lacks),
  `retarget.py` (repoint the importers), `unused.py` (drop the dead imports) and `placemodules.py` (MODULES in import
  order); `sameast.py` / `samecode.py` (the proofs: the same statements, the same bytecode); `split.sh` (a split by a spec of
  `module|description|names` lines: the moves and every import tool above) and `prove.sh` (sameast, samecode, every
  module importing, the ratchets' check); `suite-at.sh` (the full
  suite on a snapshot of a revision; `cov`: branch coverage, the venv's python running it); `stats.sh` (the
  complexity stats against the owner's start).

## Design (movies "Design rules")

**The movies design rules apply here unchanged** (the owner, 2026-11-26), and agentctl never depends on the movies
repository: the general rule core (`scripts/design-rules/`, its engine, trailer rule and rules table) is copied
BYTE-EQUAL into `devtools/design_rules/` (`python3 devtools/vendor.py sync` copies it and records the digests in
`VENDORED.tsv`; `check` and `tests/test_vendored.py` fail on an edited copy, and on a drifted one (the movies checkout
ahead) only for a push that itself touches a copy or `VENDORED.tsv`: other pushes report drift and pass). A movies change
to a vendored file is followed by agentctl's automatic sync: the laptop's sync loop opens a `VENDOR_DRIFT` condition
(`lib/agentctl/vendordrift.py`), the escalation table routes it to a Sonnet agent that runs `vendor.py sync`, freezes any new
ratchet rows with the right trailer and pushes. Never edit a copy: change it in movies, then sync; an improvement made here is written down
for a movies lane. `quality/design.py` is agentctl's extractor over the core (the Python modules, `agentctl.<part>` imports),
`quality/design-rules.tsv` its table (the limits, suffixes, abbreviations and idioms equal movies' own; the adapters,
distance and layers agentctl's), `quality/design-baseline.tsv` what is past a rule today, frozen (rows only fall;
a rise needs `Naming:` / `Design:` / `Coupling:`), `quality/design-exemptions.tsv` a NEW row admitted with a dated,
specific why. The rules: no `Impl` / `Manager` / `Helper` / `Util` / `Processor` name, no abbreviation (idioms allowed),
two names of a package in another word order BLOCK; no direct clock, subprocess or `os.environ` outside an `adapter` row's
module; no module-level state a function writes; no IO at import; fan-out counts COLLABORATORS only (a module touching
the world, holding state or an interface, or using one), at most 9; a directory's distance D with abstractness over
collaborators, its layers. `tests/test_design_rules.py` holds a test per rule and `tests/test_design_rules_mutation.py`
proves each rule's removal is caught.

- **True simplicity over metric numbers** (the owner's standing rule): no indirection or interface just to move a
  number; split functions at real seams.
- **SOLID, Dependency Inversion load-bearing**: collaborators -- `gh`, `git`, `ssh`, `claude` launches, the clock, the
  store -- are passed in and wired at one composition root, not called from deep inside (`subprocess`, `time.time()`,
  module globals). An abstraction needs two implementations (a test fake counts as the second); no `Impl` names, no
  `Manager` / `Handler` / `Util` / `Helper` names. A read-only caller gets a reader. A new red family or check kind is a
  new entry, not edits across modules. A fake shares the real logic and differs only at the IO boundary (movies'
  `writing-fakes` skill).
- **One parameter per dependency** (the owner, 2026-11-27): whenever a method is changed, migrate it, and every downstream
  function on its call path, to ONE PARAMETER PER DEPENDENCY (`clock`, `runner`, `inbox`, `runs`, `store`, ...) instead of
  `ctx` / `Context` or an ambient `wire_*` locator. A caller unpacks from `ctx` only at a boundary that still holds one.
  No NEW function takes `ctx`: `tests/test_no_new_ctx_parameters.py` freezes the per-module count, which only falls.
- **Side effects live in leaves, with fakes** (the owner, 2026-11-27: "move side effects to leaves, with fake
  implementations for testing (repos etc.)"): git, gh, files, the store, the network, processes and the clock are touched
  only by LEAF adapters; decision logic above them is pure and takes the adapters as parameters. Each adapter has a fake
  or in-memory implementation that SHARES its logic (movies' `writing-fakes`): for git, `lib/agentctl/gitrepo.py`'s
  `GitRepo` (movies' `scripts/lib/tool_git` names and signatures: `SubprocessGitRepo` asks git, `InMemoryGitRepo` holds
  commits and refs in memory, `tests/test_gitrepo.py` the one contract over both) so unit tests need no temp repository; real temp git repos stay for the integration tests and `tests/e2e/`. When a function mixing decisions
  with IO is changed, split it: the decision goes up as a pure function, the IO into the adapter. The design ratchet
  measures only the module-level half today (effect rows outside an `adapter` module); a per-FUNCTION "mixed" row (a
  function that performs an effect and also branches past a trivial condition, frozen as a ceiling that only falls) needs
  `python_facts.effects` to report per function and a `mixed` kind in `design_lint` -- written down for a movies
  `scripts/design-rules` lane, never edited in the vendored copy.
- **One source of truth per concept** (movies "Extract repeated patterns at two uses"): no two pieces of code doing
  the same job differently -- deciding whether the gate is red, classifying a transient error, running `gh` / `git`
  with a timeout and retries, reading or writing a state file, formatting a time, an age or a verdict, finding a
  worktree. Before adding one, find the existing one (`grep` its concept); when two disagree, characterise both, keep
  one, and say which and why in the commit.

## Cleanup is part of every task

Movies "Cleanup and displaced-code audit": remove what the change made obsolete (dead functions, unreachable
branches, stale comments and tests, now-redundant copies) in the same change; name what you saw and did not do.
`vulture` (in the quality venv) finds candidates; check each by hand (the tests' `A.<name>` hides call sites from it:
`grep -rn '<name>' agentctl lib tests`).
