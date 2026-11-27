# The process's context

Status: the design of the DI lane's last four locators (2026-11-27). `tests/test_no_service_locators.py` holds the
ones left.

## What it is

`context.Context` (lib/agentctl/context.py) holds the collaborators a call tree reaches that are no parameter of
their own. cli, the composition root, builds ONE per `main` (`cli.process_context`) and hands it to every command
and to the processor's tick. Each function between the root and a reader takes it as ONE keyword-only parameter,
`ctx`; a class whose methods read it takes it in its constructor (`self.ctx`). Nothing keeps it in a module cell,
and nothing reads it from one.

Its fields join as the locators go, one push each:

| field | replaces | readers (functions reaching them) |
|---|---|---|
| `runs: CiRuns` | `ciruns.wire_ci_runs` / `ci_runs()` / `main_runs()` / `main_runs_of()` | 257 functions, 16 classes, 61 modules |
| the inbox writer | `inboxentry.wire_inbox_writer` | 262 functions (52 of them shared with `runs`) |
| the process runner | `procrunner.wire_runner` / `process_runner()` | 1,439 functions: nearly everything, through `core.git` |
| the clock | `clock.wire_clock` / `epoch_now()` | 1,120 functions |

The store, the condition registry and an explicit `clock` stay their own parameters where they are already passed
(830 functions take a store). Folding them into the context is a later cleanup, made where a function is changed
anyway, never in a sweep.

## One type, not one per subsystem

The trees meet. The tick's red step, its watches, its batch and its change pushes, and the `red`, `land`,
`land-precheck`, `ci-why`, `status`, `decide` and `watchdog` commands all reach the same readers: the gate's reds
(`redseen.gate_reds`), a run's resolution (`ciwhy.resolve_run`), a red's fix (`redfixes`). Even the store server
reaches them, through its metrics' red holds (`storemetrics.longest_unleased_red`). A `TickContext` and a
`CommandContext` would meet in those readers, which would need one type anyway, or a conversion at every junction.
So there is ONE `Context`. Code that needs only a value computed from a collaborator, such as a formatted time, takes
the value, not the context.

## Rules

- A context is built only by cli (`process_context`). A test builds its own: `tests/harness.py case_context()`, one per
  test (a fresh `CiRuns`, so what one test read never answers the next), or `A.Context(runs=...)` for a reader it fakes.
- `ctx` is keyword-only and has no default. A default would be a locator again, and a forgotten pass would read
  another process's reads.
- An untyped function takes `ctx` unannotated, like its other parameters. A typed one annotates it `Context`
  (`from agentctl.context import Context`).
- A table of callbacks keeps its contract and binds the context where it is called: the watchdog's invariants
  (`invariants.READS_CI`: `fn(clock, st, ctx=ctx)`), the red holds (`redhold.RED_STATE_HOLDS`:
  `(store, it, t, facts, ctx)`), the `red` subcommands (`functools.partial(..., ctx=ctx)`).
- The golden digests record what was asked, not who asked it. `tests/golden_cases.py` drops `ctx` from a faked call's
  arguments, so every digest reads as it did before the context was passed.

## What it costs: fan-out

A module that passes the context on now imports `context`, a collaborator because it holds the CI reader. Its
callees' reads used to reach that reader out of sight. The fan-out ratchet counts that dependency now that it is
explicit: removing `runs`'s locator raises 40 modules' fan-out by one (+51 in all, two modules newly past 9). It
falls back where the context replaces an ambient import: `clock` (`epoch_now()`), `procrunner` (`process_runner()`)
and `inboxentry` (`inbox_write`). Of the 79 modules importing `epoch_now` alone from `clock`, only 5 read it nowhere
but in a function holding `ctx` today. So this fall needs the runner and clock fields, whose readers cover nearly
every module.
