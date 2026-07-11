# wyred-harness — the public read-only oracle stack (intent oracle, allocation, equivalence, ERC, invariant, mutate, crosscheck, selftests). Port of elecscad rl/harness/.

## The gate (BUILT)

`harness/gate.py` is the verdict half of ga019's runner per
`wyred-wz/dev-docs/RunnerSplit.md`: determinism (double subprocess emit,
byte-compared), L1 oracle vs each intent's declared expected codes, the v3
stack (spec_satisfaction / erc / invariant / check_allocations with the
external baselines), the from-disk cross-path differential, and the 26-probe
XPATH counter-battery with the all-probes-must-fire lobotomy verdict. It never
imports the engine — engine runs are subprocesses (`python3 -m wyred.emit`,
`python3 -m wyred.crosscheck`).

```sh
python3 harness/gate.py --tree <emitted-dir> --corpus-dir <corpus> \
    [--engine-src <wyred-src>]   # default: sibling ../wyred/src
# exit 0 iff every check passed AND the battery fully fired; 1 on failure; 2 on setup error
```

## Layout

`harness/` is a plain flat directory of pure-stdlib Python 3.10 modules using
flat sibling imports (`import intent_oracle`, `from schema import ...`).
There is deliberately no packaging (no `pyproject.toml`, no `__init__.py`):
the modules are run from inside the directory, exactly as in the elecscad
source tree. Do not convert them to a package — that would force import
rewrites and break the byte-identical-port property.

## Gate regression tests

```sh
python3 tests/run_gate_tests.py
```

End-to-end regression tests for `harness/gate.py` (Phase-2 critic residual
R3): the clean examples corpus must gate green with the full 26/26 battery
line; the committed `tests/fixtures/f4_corpus/` (an intent DECLARING a clean
layer 1 that actually carries an ADDR_COLLISION) must emit fine but FAIL the
gate's expected-codes check — the F4 declared-vs-oracle disagreement as a
committed regression; a reduced corpus without the board modules must fail
the battery's "probe(s) never found an applicable artifact" lobotomy verdict
(and only that); and a dropped BOM line in a copy of the clean tree must fail
the cross-path differential. Pure stdlib, ~10s; everything runs in temp dirs
via subprocesses (no engine/harness imports, no repo writes). Deliberately
NOT wired into the selftests below — their 149-check contract is frozen.

## Running the selftests

```sh
cd harness
python3 selftest.py      # 35 checks
python3 selftest_v3.py   # 114 checks (runs selftest.py as a subprocess first)
```

No dependencies beyond the Python 3.10 standard library. The kicad-related
checks in `selftest_v3.py` additionally use `kicad-cli` if present on PATH
(they self-skip otherwise) and read third-party board fixtures resolved at
`../../third-party` relative to `harness/` (i.e. `<workspace-root>/third-party`;
currently a symlink to the read-only `/Users/owebeeone/limbo/elecscad/third-party`
tree — fixture migration is deferred). `pcb_extract.py`'s own `--selftest`
also reads that elecscad tree via a hardcoded path.
