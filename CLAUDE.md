# agentctl conventions for Claude

agentctl (`~/bin`) holds itself to the movies project's coding and testing standards (the owner, 2026-11-22: "same
coding / testing standards as movies project"). The rules are movies' `~/projects/movies/CLAUDE.md`; this page names
the sections and gives agentctl's own mechanics. The tool's reference is `AGENTCTL.md`.

## Work in a worktree, never in ~/bin

`~/bin` is the LIVE agentctl every process runs (the laptop's processor re-execs onto its commits; ci-runner's
auto-pin ships main within ~5 min). Work in `git -C ~/bin worktree add -b <name> ~/bin-wt-<name> master`, Bash edits
included (an edit in ~/bin trips watchdog WB). Land: `git -C ~/bin-wt-<name> rebase master && git -C ~/bin merge
--ff-only <name> && git -C ~/bin push origin master:main`, then remove the worktree and `git branch -d` the branch.
Push in small, coherent steps (a regression must be easy to bisect and revert); after a push check
`~/bin/agentctl status` and `~/bin/agentctl red status` still work.

## The tests

- **The commit gate** (movies "Tests: the commit gate"): no change without a test that fails before it and passes
  after it, written first whenever feasible. agentctl's layers: `tests/` unit tests through the harness
  (`tests/harness.py`: hermetic HOME, PATH, git config, per-test timeout), the queue simulator (`tests/queue_sim.py`)
  and its gate (`tests/sim_gate.py`).
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

`quality/ratchet.py` holds the production code (`agentctl`, `lib/agentctl/*.py`) to two checked-in baselines, the
movies model (movies "Code quality ratchets" and "Coupling"):

| ratchet | baseline | kinds and limits | trailer for a rise |
|---|---|---|---|
| quality | `quality/quality-baseline.tsv` | cognitive 10, cyclomatic 10, inner-defs 2 | `Complexity:` |
| | | method-lines 50, file-lines 500 | `Length:` |
| | | copies 0 (clone groups of 50+ tokens) | `Duplication:` |
| | | fan-out 9 (other parts a part uses), cycle 0 (a part in a reference cycle) | `Coupling:` |
| types | `quality/types-baseline.tsv` | mypy-errors 0 per part (`mypy --strict`) | `Types:` |

- A NEW row past a limit or a frozen row RISING refuses the push; a fall passes and is recorded with
  `"$(python3 quality/tools.py)" quality/ratchet.py update` (commit the baseline). A rise lands only with the kind's
  trailer in the commit's FINAL trailer block, beside `Co-Authored-By:`; each commit growing a baseline is judged by its
  own step (one commit's trailer never excuses another's).
- A function is keyed by its qualified name, so moving it to another part keeps its row; a new part split off a big
  one starts its own `file-lines` / `fan-out` / `cycle` rows.
- The tools are pinned with hashes in `quality/tools.lock` and installed by `quality/tools.py` into
  `~/.cache/agentctl-quality/tools-<lock digest>-py<ver>/`, by the hook itself. A tool that cannot be installed blocks
  the push with the fix; it is never skipped. `ratchet.py selftest` (the hook runs it first) seeds a violation of every
  kind and must see each refused. Measures: `quality/measure.py` (`--all` for every row), `quality/typecheck.py
  --errors` (every mypy error, mapped back to its part and line).
- REAL MODULES (2026-11-22; `lib/agentctl/__init__.py`): every part is a module importing the standard library and
  the modules before it in `MODULES` (import order, written by `devtools/placemodules.py`), so a part's coupling is the
  modules it imports (a FOUNDATION -- a module importing no module but other foundations, a leaf helper like
  `statefile` -- is no collaborator: extracting a concept into its own module never raises its users' fan-out), and
  mypy checks the modules concatenated in `MODULES` order. What a module must not import -- the remote store, a
  missing token's inbox condition -- is wired at the composition root (cli: `wire_store(process_store)`,
  `wire_token_missing_note(token_unavailable_note)`), never reached by a module calling up. The tests read and patch
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

- **True simplicity over metric numbers** (the owner's standing rule): no indirection or interface just to move a
  number; split functions at real seams.
- **SOLID, Dependency Inversion load-bearing**: collaborators -- `gh`, `git`, `ssh`, `claude` launches, the clock, the
  store -- are passed in and wired at one composition root, not called from deep inside (`subprocess`, `time.time()`,
  module globals). An abstraction needs two implementations (a test fake counts as the second); no `Impl` names, no
  `Manager` / `Handler` / `Util` / `Helper` names. A read-only caller gets a reader. A new red family or check kind is a
  new entry, not edits across modules. A fake shares the real logic and differs only at the IO boundary (movies'
  `writing-fakes` skill).
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
