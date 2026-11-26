#!/usr/bin/env python3
"""THE SIMULATOR GATE (the owner, 2026-11-20: "fix agentctl issues structurally"): every agentctl change is held to the
queue simulator's invariants, kind by kind, against master.

  python3 tests/sim_gate.py                 run the fixed scenarios, compare with tests/sim-invariants.tsv
  python3 tests/sim_gate.py --base REF      also hold the registry itself to REF's (the pre-push hook: the remote's main)
  python3 tests/sim_gate.py --update        write the measured counts and fingerprints into the registry
  python3 tests/sim_gate.py --range A..B    the commit rule alone (below), over the commits of A..B

THE FIXED SCENARIOS (scenarios()): the suite's 200 random seeds, the calm, load and chaos profiles (with a wider chaos
sweep than the suite's, so a rare violation has a count to rise from), every replayed day of real arrivals
(tests/fixtures/landing-replay-7d.json), the 2026-11-17 collisions, the 2026-11-19 baselines, the 08010c timeline
and (v2) the mutation tests' own profiles: loadlone, chaoshold, the red fix beside an ordinary change.
Each runs in COUNT MODE (Sim.run(stop=False): on past a violation) and a kind's count is the number of scenarios that
violate it -- the measure lanes used by hand ("invariant 9: 15/60 vs master's 8/60").

THE REGISTRY (tests/sim-invariants.tsv): one row per invariant kind -- its VERSION, the FINGERPRINT of its definition
(the source of every Sim method that can raise it, and of the scenario list), and its BASELINE count. The gate fails
when:
- a kind's count rises past its baseline;
- a kind's definition changed (its fingerprint) and its row was not bumped -- an invariant is VERSIONED: a changed
  definition is a new version, its baseline measured anew;
- (--base) a row's baseline rose against REF's while its version did not change: loosening an invariant's numbers is
  a new version, never a silent edit;
- (--range, and with --base over base..HEAD) one commit both changes an invariant's row (version, fingerprint or
  baseline rising) and lib/agentctl/: an invariant's definition changes in a commit OF ITS OWN, never beside the code it
  measures (2026-11-20: the batching work loosened invariants 4 and 9 in the commit that needed them looser).
A fall is no failure: `--update` records it (the ratchet tightens).
"""
import argparse, ast, concurrent.futures, hashlib, json, os, subprocess, sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
REGISTRY = os.path.join(HERE, "sim-invariants.tsv")
REGISTRY_REL = "tests/sim-invariants.tsv"
HEADER = "# kind\tversion\tfingerprint\tbaseline\twhat"

# the scenarios: (name, Sim kwargs as a JSON-able dict; "profile" a name of PROFILES, "script" a name of SCRIPTS)
CHAOS_SWEEP = 30


def scenarios():
    out = [(f"random-{s}", {"seed": s}) for s in range(200)]
    out += [(f"calm-{s}-t{t}", {"seed": s, "calm": True, "trees": t}) for s in range(8) for t in (1, 2, 4)]
    out += [(f"load-{s}", {"seed": s, "calm": True, "trees": 1, "profile": "LOAD", "n_changes": "LOAD.n"})
            for s in range(4)]
    out += [(f"chaos-fixed-{s}", {"seed": s, "trees": 1, "profile": "CHAOS", "n_changes": 14}) for s in (36, 52, 121)]
    out += [(f"chaos-{s}-t{t}", {"seed": s, "trees": t, "profile": "CHAOS_OUTSIDE", "n_changes": 14})
            for s in range(CHAOS_SWEEP) for t in (1, 4)]
    out += [(f"replay-{i}", {"seed": 1000 + i, "script": f"replay:{i}"}) for i in range(len(replay_days()))]
    out += [("collisions-2026-11-17", {"seed": 17, "calm": True, "script": "COLLISIONS_2026_11_17"})]
    out += [(f"baselines-2026-11-19-{s}", {"seed": s, "calm": True, "trees": 4, "script": "BASELINES_2026_11_19"})
            for s in range(4)]
    out += [(f"timeline-08010c-{s}", {"seed": s, "calm": True, "script": "TIMELINE_08010C"}) for s in range(4)]
    # v2 (2026-11-20): the mutation tests' own profiles, so a rule they need is held here too -- the load with many
    # lone changes (the older-batch rule: I5 fails without it), the chaos seeds where a push waits for a batch, the red
    # fix beside an ordinary change
    out += [(f"loadlone-{s}", {"seed": s, "calm": True, "trees": 1, "profile": "LOADLONE", "n_changes": "LOAD.n"})
            for s in range(3)]
    out += [(f"chaoshold-{s}", {"seed": s, "trees": 4, "profile": "CHAOS_OUTSIDE", "n_changes": 14}) for s in (3, 4)]
    out += [(f"redfix-behind-ordinary-{s}", {"seed": s, "calm": True, "trees": 2, "script": "T.REDFIX_BEHIND_ORDINARY"})
            for s in range(4)]
    # v3 (2026-11-25): several runs of one red -- the fix naming an older run, the fix let go when its red learns the
    # run, the fix of another failure (IW)
    out += [(f"redepisode-{n}-{s}", {"seed": s, "calm": True, "trees": 2, "script": name})
            for n, name in (("older", "REDFIX_OLDER_RUN"), ("heals", "REDFIX_HEALS"), ("other", "REDFIX_OTHER_FAILURE"))
            for s in range(4)]
    # v4 (2026-11-26): the red-landing rule -- a fix beside a bare production commit waits for green (IR)
    out += [(f"redfix-beside-bare-{s}", {"seed": s, "calm": True, "trees": 2, "script": "REDFIX_BESIDE_BARE"})
            for s in range(4)]
    # v5 (2026-11-26): one RedEpisode -- a fix naming a run only a stale cached signature puts in the episode (IW)
    out += [(f"redfix-stale-episode-{s}", {"seed": s, "calm": True, "trees": 2, "script": "REDFIX_STALE_EPISODE"})
            for s in range(4)]
    # v6 (2026-11-26): two land holders at once, at each interleaving of the store's seams (IH; tests #289 / #292)
    out += [(f"two-holders-{o}-{s}", {"seed": s, "calm": True, "trees": 2, "script": f"TWO_HOLDERS_{o.upper()}"})
            for o in ("apart", "extend_first", "move_between") for s in range(2)]
    # v10 (2026-11-26): the hold found expires before the other holder's extension is written (IH)
    out += [(f"two-holders-expire-between-{s}", {"seed": s, "calm": True, "trees": 2, "script": "TWO_HOLDERS_EXPIRE_BETWEEN"})
            for s in range(2)]
    # v7 (2026-11-26): a hold on a change parked on `waiting` past HOLD_IDLE_TTL (IT)
    out += [(f"hold-on-parked-{s}", {"seed": s, "calm": True, "trees": 2, "script": "HOLD_ON_PARKED",
                                         "profile": "PARKED"}) for s in range(4)]
    # v8 (2026-11-26): the escalation dispatcher beside the queue -- clears, fails then clears, never clears, hangs, two
    # subjects of a kind, past the cap, owner-only kinds, the repo untrusted a while (IE, IO, IG)
    out += [(f"escalations-{s}", {"seed": s, "calm": True, "trees": 2, "script": "ESCALATIONS"}) for s in range(4)]
    out += [(f"escalations-untrusted-{s}", {"seed": s, "calm": True, "trees": 2, "script": "ESCALATIONS_UNTRUSTED"})
            for s in range(2)]
    # v9 (2026-11-26): fixers dying at launch -- of the vault, of one other cause, of a different cause each time (IL)
    out += [(f"crash-loops-{s}", {"seed": s, "calm": True, "trees": 2, "script": "CRASH_LOOPS"}) for s in range(4)]
    return out


def replay_days():
    with open(os.path.join(HERE, "fixtures", "landing-replay-7d.json")) as f:
        return json.load(f)["days"]


def run_one(spec):
    """One scenario in count mode: {kind: violations}, its first lines, and its landing latency (mean ticks)."""
    import queue_sim
    import test_queue_sim as T
    profiles = {"LOAD": T.LOAD, "CHAOS": T.CHAOS, "CHAOS_OUTSIDE": {**T.CHAOS, "outside_red": 0.08},
                "LOADLONE": dict(T.LOAD, nobatch=0.4), "PARKED": {"land_bound": 200}}
    kw = dict(spec)
    if "profile" in kw:
        kw["profile"] = profiles[kw["profile"]]
    if kw.get("n_changes") == "LOAD.n":
        kw["n_changes"] = T.LOAD["n"]
    if "script" in kw:
        name = kw["script"]
        kw["script"] = (queue_sim.replay_script(queue_sim.replay_days()[int(name.split(":")[1])])
                        if name.startswith("replay:") else list(getattr(T, name[2:])) if name.startswith("T.")
                        else list(getattr(queue_sim, name)))
    seed = kw.pop("seed")
    sim = queue_sim.Sim(seed, **kw).run(stop=False)
    waits = [sim.done_at[c.cid] - c.arrived for c in sim.changes if c.cid in sim.done_at]
    return {"kinds": dict(sim.kinds_seen), "lines": sim.violations[:3],
            "latency": (sum(waits) / len(waits)) if waits else None}


def worker(chunk):
    sys.path.insert(0, HERE)
    return {name: run_one(spec) for name, spec in chunk}


def measure(jobs=None, only=None):
    """{scenario: run_one result}, the scenarios spread over subprocesses (PYTHONHASHSEED=0: a seed replays exactly)."""
    todo = [s for s in scenarios() if only is None or s[0] in only]
    sys.path.insert(0, os.path.join(os.path.dirname(HERE), "lib"))
    from agentctl.govpool import governed  # THE GOVERNED FAN-OUT: its chunks under one ticket, or the one above it
    with governed("sim-gate", jobs or min(8, os.cpu_count() or 2), f"tests/sim_gate.py {os.path.dirname(HERE)}") as pool:
        return measure_chunks(todo, pool.width, pool.env(os.environ))


def measure_chunks(todo, jobs, env):
    chunks = [todo[i::jobs] for i in range(jobs)]
    env = {**env, "PYTHONHASHSEED": "0"}
    for k in ("AGENTCTL_RED_FIXER", "FIXER_RECORD", "AGENT_NAME"):
        env.pop(k, None)

    def run_chunk(chunk):
        p = subprocess.run([sys.executable, os.path.abspath(__file__), "--worker"], input=json.dumps(chunk),
                           capture_output=True, text=True, env=env, cwd="/", timeout=1200)
        if p.returncode != 0:
            raise RuntimeError(f"sim_gate worker failed (rc {p.returncode}): {p.stderr[-2000:]}")
        return json.loads(p.stdout.strip().splitlines()[-1])

    out = {}
    with concurrent.futures.ThreadPoolExecutor(jobs) as ex:
        for got in ex.map(run_chunk, [c for c in chunks if c]):
            out.update(got)
    return out


def counts(results):
    """{kind: scenarios violating it}, every kind present (0 when none)."""
    import queue_sim
    c = {k: 0 for k in queue_sim.INVARIANT_KINDS}
    for r in results.values():
        for k in r["kinds"]:
            c[k] = c.get(k, 0) + 1
    return c


# ---------------------------------------------------------------- the fingerprints
def fingerprints(src=None, gate_src=None, profiles_src=None):
    """{kind: sha1[:12] of its definition}: the source of every Sim method that raises it (violate("<kind>", ...)),
    plus what every kind shares -- the violate helper, the run loop and the scenario list (this file's scenarios(), the
    profiles in test_queue_sim.py)."""
    src = src if src is not None else open(os.path.join(HERE, "queue_sim.py")).read()
    gate_src = gate_src if gate_src is not None else open(os.path.abspath(__file__)).read()
    profiles_src = profiles_src if profiles_src is not None else open(os.path.join(HERE, "test_queue_sim.py")).read()
    tree = ast.parse(src)
    sim = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == "Sim")
    by_kind, shared = {}, []
    for fn in sim.body:
        if not isinstance(fn, ast.FunctionDef):
            continue
        seg = ast.get_source_segment(src, fn)
        if fn.name in ("violate", "run", "quiet"):
            shared.append(seg)
        for node in ast.walk(fn):
            if isinstance(node, ast.Call) and getattr(node.func, "attr", None) == "violate" and node.args \
                    and isinstance(node.args[0], ast.Constant):
                by_kind.setdefault(node.args[0].value, set()).add(seg)
    gtree = ast.parse(gate_src)
    shared += [ast.get_source_segment(gate_src, n) for n in gtree.body
               if isinstance(n, ast.FunctionDef) and n.name in ("scenarios", "run_one")]
    ptree = ast.parse(profiles_src)
    shared += [ast.get_source_segment(profiles_src, n) for n in ptree.body if isinstance(n, ast.Assign)
               and any(getattr(t, "id", None) in ("LOAD", "CHAOS", "REDFIX_BEHIND_ORDINARY") for t in n.targets)]
    base = "\n".join(shared)
    return {k: hashlib.sha1((base + "\n".join(sorted(v))).encode()).hexdigest()[:12] for k, v in by_kind.items()}


# ---------------------------------------------------------------- the registry
def read_registry(text):
    rows = {}
    for line in text.splitlines():
        if not line.strip() or line.startswith("#"):
            continue
        k, version, fp, baseline, what = (line.split("\t") + [""] * 5)[:5]
        rows[k] = {"version": int(version), "fingerprint": fp, "baseline": int(baseline), "what": what}
    return rows


def write_registry(rows):
    import queue_sim
    with open(REGISTRY, "w") as f:
        f.write("# THE QUEUE SIMULATOR'S INVARIANTS (tests/sim_gate.py). One row per kind: its version, its definition's\n"
                "# fingerprint, the number of the gate's fixed scenarios violating it (its baseline). A changed\n"
                "# definition is a new version, in a commit of its own; a baseline only falls without one.\n")
        f.write(HEADER + "\n")
        for k in queue_sim.INVARIANT_KINDS:
            r = rows[k]
            f.write(f"{k}\t{r['version']}\t{r['fingerprint']}\t{r['baseline']}\t{queue_sim.INVARIANT_KINDS[k]}\n")


def git(*args):
    return subprocess.run(["git", *args], cwd=ROOT, capture_output=True, text=True)


def registry_at(ref):
    r = git("show", f"{ref}:{REGISTRY_REL}")
    return read_registry(r.stdout) if r.returncode == 0 else None


def row_loosened(old, new):
    """Whether registry row `new` changes `old`'s definition or loosens its numbers: a version or fingerprint changed,
    or its baseline rose."""
    if old is None:
        return new is not None
    return new is None or new["version"] != old["version"] or new["fingerprint"] != old["fingerprint"] \
        or new["baseline"] > old["baseline"]


def commit_rule(rng):
    """THE COMMIT RULE: the commits of rng that both change an invariant row and lib/agentctl/ -- [(sha, kinds)]."""
    bad = []
    for sha in git("rev-list", "--reverse", rng).stdout.split():
        files = git("diff-tree", "--no-commit-id", "--name-only", "-r", sha).stdout.split()
        if REGISTRY_REL not in files or not any(f.startswith("lib/agentctl/") for f in files):
            continue
        before, after = registry_at(f"{sha}^") or {}, registry_at(sha) or {}
        kinds = sorted(k for k in set(before) | set(after) if row_loosened(before.get(k), after.get(k)))
        if kinds:
            bad.append((sha, kinds))
    return bad


def check(results, rows, base_rows=None):
    """The gate's failures (empty: it passes) and its notes (falls the --update would record)."""
    import queue_sim
    fails, notes = [], []
    fps = fingerprints()
    got = counts(results)
    first = {}
    for name, r in sorted(results.items()):
        for k in r["kinds"]:
            first.setdefault(k, (name, r["lines"][:1]))
    for k in queue_sim.INVARIANT_KINDS:
        row = rows.get(k)
        if row is None:
            fails.append(f"{k}: no row in {REGISTRY_REL} (a new invariant: `--update`, in a commit of its own)")
            continue
        if fps.get(k) and fps[k] != row["fingerprint"]:
            fails.append(f"{k}: its definition changed (fingerprint {fps[k]}, the registry says {row['fingerprint']} at "
                         f"version {row['version']}): bump its version and baseline (`--update --bump {k}`) in a "
                         "commit of its own, and report its old-definition numbers beside the new ones")
        if got[k] > row["baseline"]:
            name, lines = first.get(k, ("?", []))
            fails.append(f"{k} ({queue_sim.INVARIANT_KINDS[k]}): {got[k]} scenario(s) violate it, baseline "
                         f"{row['baseline']} -- first {name}: {lines[0][:300] if lines else '?'}")
        elif got[k] < row["baseline"]:
            notes.append(f"{k}: {got[k]} scenario(s), below its baseline {row['baseline']} (`--update` records the fall)")
        old = (base_rows or {}).get(k)
        if base_rows is not None and old and old["version"] == row["version"] and row["baseline"] > old["baseline"]:
            fails.append(f"{k}: its baseline rose from {old['baseline']} to {row['baseline']} at the same version "
                         f"{row['version']}: loosening an invariant is a new version, in a commit of its own")
    return fails, notes


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--worker", action="store_true")
    ap.add_argument("--update", action="store_true")
    ap.add_argument("--bump", action="append", default=[], help="with --update: these kinds take a new version")
    ap.add_argument("--base", help="hold the registry to this ref's, and the commit rule over REF..HEAD")
    ap.add_argument("--range", help="the commit rule alone, over A..B")
    ap.add_argument("-j", "--jobs", type=int)
    ap.add_argument("--json", help="write every scenario's result here")
    a = ap.parse_args(argv)
    if a.worker:
        chunk = json.loads(sys.stdin.read())
        print(json.dumps(worker(chunk)))
        return 0
    sys.path.insert(0, HERE)
    if a.range:
        bad = commit_rule(a.range)
        for sha, kinds in bad:
            print(f"sim gate: commit {sha[:10]} changes invariant row(s) {', '.join(kinds)} AND lib/agentctl/: an "
                  "invariant's definition changes in a commit of its own")
        return 1 if bad else 0
    results = measure(a.jobs)
    if a.json:
        with open(a.json, "w") as f:
            json.dump(results, f, indent=1, sort_keys=True)
    rows = read_registry(open(REGISTRY).read()) if os.path.exists(REGISTRY) else {}
    got = counts(results)
    lat = [r["latency"] for r in results.values() if r["latency"] is not None]
    print(f"sim gate: {len(results)} scenarios; violations by kind: "
          + ", ".join(f"{k} {v}" for k, v in got.items()) + f"; mean landing latency {sum(lat) / len(lat):.2f} ticks")
    if a.update:
        fps = fingerprints()
        new = {}
        for k in got:
            old = rows.get(k)
            if old and fps.get(k, old["fingerprint"]) != old["fingerprint"] and k not in a.bump:
                print(f"sim gate: {k}'s definition changed: pass --bump {k} (a new version) to record it")
                return 1
            if old and k not in a.bump and got[k] > old["baseline"]:
                print(f"sim gate: {k} rose {old['baseline']} -> {got[k]}: no --update admits a rise; fix it, or --bump "
                      f"{k} for a changed definition")
                return 1
            new[k] = {"version": (old["version"] + 1 if old and k in a.bump else old["version"] if old else 1),
                      "fingerprint": fps.get(k, old["fingerprint"] if old else "-"), "baseline": got[k]}
        write_registry(new)
        print(f"sim gate: {REGISTRY_REL} updated")
        return 0
    base_rows = registry_at(a.base) if a.base else None
    fails, notes = check(results, rows, base_rows)
    if a.base:
        for sha, kinds in commit_rule(f"{a.base}..HEAD"):
            fails.append(f"commit {sha[:10]} changes invariant row(s) {', '.join(kinds)} AND lib/agentctl/: an "
                         "invariant's definition changes in a commit of its own")
    for n in notes:
        print(f"sim gate: note: {n}")
    for f in fails:
        print(f"sim gate: FAIL {f}")
    print("sim gate: " + ("REFUSED" if fails else "ok"))
    return 1 if fails else 0


if __name__ == "__main__":
    sys.path.insert(0, os.path.join(os.path.dirname(HERE), "lib"))
    from agentctl.govpool import run_main
    sys.exit(run_main(main))
