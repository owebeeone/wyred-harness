# The gate CLI — `harness/gate.py`

`gate.py` is the **verdict half** of the end-to-end runner (see
`dev-docs/RunnerSplit.md`). It gates one *emitted artifact tree* — a
`wyred.emit --out` directory — and exits `0` iff every check passed **and** the
counter-probe battery fully fired.

Every verdict is computed from artifacts **re-read from disk**. The gate never
imports the engine: composition is at the **process level**, so the only way it
touches `wyred` is by running `python3 -m wyred.emit` and
`python3 -m wyred.crosscheck` as subprocesses over shared artifact directories.

## Synopsis

<!-- cwd: wyred-harness -->
```console
$ python3 harness/gate.py --help
usage: gate.py [-h] --tree TREE --corpus-dir CORPUS_DIR
               [--engine-src ENGINE_SRC]
# expect: --tree TREE
# expect: --corpus-dir CORPUS_DIR
# expect: --engine-src ENGINE_SRC
```

## Flags

| flag | required | meaning |
|---|---|---|
| `--tree TREE` | yes | the emitted artifact tree to gate (a `wyred.emit --out` directory). |
| `--corpus-dir DIR` | yes | the corpus the tree was emitted from. Read-only: it supplies each intent's own declarations *and* is re-emitted for the determinism check. |
| `--engine-src DIR` | no | the `wyred` engine `src/` dir, invoked **only** as a subprocess. Defaults to the sibling `../wyred/src` relative to `gate.py` (i.e. `<workspace>/wyred/src`). |

There are no other flags — this table is the whole surface of
`gate.py`'s argument parser.

## Exit codes

| code | meaning |
|---|---|
| `0` | every stage passed **and** every battery probe fired at least once. |
| `1` | one or more verdict failures (a mismatch, a differential fire, an unfired probe, a SPICE rail off). |
| `2` | a setup error before gating: `--tree`/`--corpus-dir` is not a directory, or `--engine-src` has no `wyred` package under it. |

## Running it

Emit a corpus, then gate the tree. Point `PYTHONPATH` at the engine so
`python3 -m wyred.emit` resolves, write the tree to a scratch directory, and let
`--engine-src` default to the sibling checkout:

<!-- cwd: wyred-examples -->
<!-- pythonpath: wyred/src -->
```console
$ python3 -m wyred.emit --corpus-dir corpus --out "$WYRED_DOCS_SANDBOX/tree"
$ python3 ../wyred-harness/harness/gate.py --tree "$WYRED_DOCS_SANDBOX/tree" --corpus-dir corpus
PASS cross-path + spice counter-probe battery: 30/30 probes fired
RESULT: PASS (20 artifact(s), 0 failure(s))
# expect: counter-probe battery
# expect: RESULT: PASS
```

To run all the composed stages (emit → gate → audit → probes → placement →
KiCad round-trip) end-to-end, use the driver in the examples repo,
`wyred-examples/run_gate.py`; the gate is its stage 2.

## What each stage checks

The gate runs six stages over the tree. Each is summarised here at reference
level; the **normative** definitions of the artifacts and codes are in
`wyred-contract/EMIT_CONTRACT.md` and the schemas under
`wyred-contract/schemas/` — this page links to them and never restates them.

1. **Determinism.** The corpus is emitted **twice** via
   `python3 -m wyred.emit` subprocesses into two temp dirs and the trees are
   byte-compared — the from-disk analogue of an emit-twice / resolve-twice
   check. Any file that differs is a failure.
2. **Layer 1 (the public intent oracle).** The oracle runs over each
   `<name>.l1.json`; its codes must equal the intent's **own** declared
   `expected_l1` (a self-check *against intent*, not self-consistency). A
   declared escalation must be present **with** its conflict + relaxation
   explanation. The L1 intent shape is EMIT_CONTRACT Part B
   (`schemas/l1.schema.json`).
3. **Layer 2 (the v3 stack, from disk).** For every declared-clean,
   oracle-clean artifact, `spec_satisfaction` / `erc` / `invariant` (including
   declared-vs-lowered invariant counts) / `check_allocations` — the last with
   an independent `record2` from the second re-emit and the retained external
   `<name>.baseline.json` — must agree with the declared verdict (`ESCALATE`
   for a rung-4 intent, `PASS` otherwise). Locked groups with no retained
   baseline yield `LOCK_UNVERIFIED` rather than a silent pass. A document that
   fails at L1 still gets its embedded allocation record checked standalone.
   The allocation-record schema is EMIT_CONTRACT Part C
   (`schemas/alloc.schema.json`, `schemas/baseline.schema.json`).
4. **The cross-path differential.** Re-run **from disk** by the engine's own
   `python3 -m wyred.crosscheck` subprocess: the netlist, BOM, pin-map, records
   and the emitted L1 must describe **one** model, and connector locks are
   re-verified against the external baselines. This stage also drives the
   counter-probe battery (below).
5. **The lobotomy verdict.** Every battery probe must have fired at least once
   this run, or the run **fails** — "all codes negatively probed" is a per-run
   *mechanical fact*, not a claim (below).
6. **The SPICE DC operating point.** For every artifact carrying a `.cir` deck,
   ngspice (reached only as a subprocess, via the harness `spice_run` runner)
   computes the DC operating point, and each declared power rail must sit within
   its declared tolerance (±2 % default, `sim_tol_pct` override) of the
   simulated node voltage (`SPICE_RAIL_OFF` / `SPICE_RAIL_UNRESOLVED`). See
   *Loud skip vs failure* below for the ngspice-absent and `.cir`-absent cases.

## The counter-probe battery and the lobotomy verdict

Passing the differential is not enough: the gate also proves the differential
can still *catch* things. For each reserved differential code, a **negative
probe** doctors exactly one field of a **copy** of a clean artifact set (written
under a temp dir — never the input tree) and asserts that the differential fires
that specific code. A probe that applied but stayed silent fails the run; and if
any probe never found an applicable artifact this run, that fails the run too.
That is the **lobotomy verdict** — the gate only certifies a code as
"negatively probed" when it mechanically watched it fire.

Two batteries share one verdict:

- the **XPATH_\*** cross-path probes (`_xpath_probe_battery()` in `gate.py`),
  which doctor the netlist / BOM / pin-map / records / L1 set, and
- the **XCIR_\*** SPICE-deck probes (`_xcir_probe_battery()` in `gate.py`),
  which doctor a clean `.cir` deck + its `.cir.json` confession sidecar.

The authoritative probe→code lists are those two functions in
`wyred-harness/harness/gate.py`; the codes themselves are specified in
`wyred-contract/EMIT_CONTRACT.md` (the `XCIR_*` structural-oracle codes are also
detailed in `dev-docs/WyredSpiceContract.md`, and the `.cir` sidecar is Part E /
`schemas/cir.schema.json`). This page does not reproduce them — read them there.

The battery is why the run needs at least one spice-modelled artifact and the
board modules: drop the `.cir`-emitting intent and the XCIR probes go unfired;
drop a board and a connector probe goes unfired — either way the lobotomy
verdict turns the run red. The reference corpus in `wyred-examples/corpus`
satisfies both, so its battery line reads `30/30 probes fired` (26 XPATH + 4
XCIR at the time of writing; `gate.py` is the source of truth for the count).

## Loud skip vs failure (SPICE)

The SPICE stage is wired to *degrade loudly*, never green-by-omission:

- **`.cir` absent** for an artifact → the stage is **not applicable** to it
  (Phase-1 gating already made deck absence honest).
- **ngspice absent** (not on `PATH` and `WYRED_NGSPICE` unset/unresolvable) →
  a **loud SKIP** banner names every deck and every declared rail that was
  *not* verified, and the stage contributes **no failure**. So a machine
  without a simulator still gates green, but can never mistake an unrun oracle
  for a passing one.
- A broken run (timeout / crash / unparseable output) is a **structured
  failure** — red — never laundered into a skip.

Install ngspice (`brew install ngspice`) or point `WYRED_NGSPICE` at a binary to
turn the skip into a real check.

## The failure path is live

A setup error exits `2`. The following is asserted to *fail* — the doc-test
runner is green only because the gate really does reject a missing tree:

<!-- cwd: wyred-harness -->
<!-- expect-fail: a missing --tree directory is a setup error; the gate exits 2, never 0 -->
```console
$ python3 harness/gate.py --tree ./no-such-tree --corpus-dir ./no-such-corpus
not a directory: ./no-such-tree
```

## See also

- **[Running the selftests](selftests.md)** — the harness's own selftests and
  the gate regression suite `tests/run_gate_tests.py`.
- `dev-docs/RunnerSplit.md` — why emit, gate, and audit live in three repos and
  meet only at artifacts on disk.
- `wyred-contract/EMIT_CONTRACT.md` + `wyred-contract/schemas/` — the normative
  artifact shapes and differential codes.
