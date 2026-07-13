# Running the selftests

The harness ships its own selftests: pure-Python-3.10-stdlib scripts that assert
the oracle stack behaves as specified and print a readable per-check summary.
They are the harness's regression floor — `wyred-harness/CLAUDE.md` makes the
combined **149-check** contract a port-acceptance invariant.

Run them from **inside** `harness/` (the modules use flat sibling imports, so
the working directory must be the `harness/` directory itself).

## `selftest.py` — the v2 oracle (35 checks)

Exercises the layer-2 oracle: every golden yields its expected verdict; every
known-bad is caught with its expected code; intent #8's mutual-exclusion
interlock holds clean and breaks to `SHOOT_THROUGH`; graphs round-trip through
`to_json`; and the hardening fixes (vacuous-invariant rejection, structured ERC
on an off-label netlist, the two-input interlock). Exit status is `0` iff every
check passes. See the module docstring for the full list — this page does not
restate it.

<!-- cwd: wyred-harness/harness -->
```console
$ python3 selftest.py
RESULT: PASS  (all 35 checks passed)
# expect: RESULT: PASS
# expect: all 35 checks passed
```

## `selftest_v3.py` — the Gen-4 two-phase stack (114 checks)

Additive to v2. It **first runs `selftest.py` as a subprocess** (v3 may not
break the v2 stack), then asserts the layer-1 oracle, spec-satisfaction,
allocation determinism, the external-baseline lock protocol, and the F1–F9
hardening fixes. Exit status is `0` iff every non-skipped check passes.

Two external touchpoints shape what runs:

- **Real board fixtures (required).** Section (f) reads the flagship boards
  under `<workspace>/third-party` (e.g. `libre-solar-mppt-2420-hc`, `rpi-cm4io`)
  via `pcb_extract`. These must be materialized; see `wyred-harness/README.md`
  for the fixture story.
- **`kicad-cli` (optional).** Section (g) netlists Watchy's schematic with
  KiCad's own engine and differences it against `pcb_extract`'s reading of the
  board. If `kicad-cli` is absent this section prints `SKIPPED` and does **not**
  fail; when present, the two engines must agree.

<!-- cwd: wyred-harness/harness -->
```console
$ python3 selftest_v3.py
RESULT: PASS  (all 114 checks passed)
# expect: RESULT: PASS
# expect: all 114 checks passed
```

`35 + 114 = 149` — the frozen contract `wyred-harness/CLAUDE.md` and
`README.md` refer to.

## `tests/run_gate_tests.py` — the gate regression suite

End-to-end regression tests for `harness/gate.py` (run from the repo root, not
`harness/`). Everything runs in temp dirs via subprocesses — no engine or
harness imports, no repo writes — in roughly ten seconds. It asserts that the
clean examples corpus gates green with the **full battery line**; that the
committed `tests/fixtures/f4_corpus/` (an intent declaring a clean layer 1 that
actually carries an `ADDR_COLLISION`) emits fine but **fails** the gate's
expected-codes check; that a reduced corpus without the board modules fails the
battery's "probe never found an applicable artifact" lobotomy verdict *and only
that*; and that a dropped BOM line in a copy of a clean tree fails the cross-path
differential.

<!-- cwd: wyred-harness -->
```console
$ python3 tests/run_gate_tests.py
GATE TESTS: PASS (4 tests, 6.9s)
# expect: GATE TESTS: PASS
```

This suite is deliberately **not** wired into the selftests above — their
149-check contract is frozen — so it is counted and run separately.

## See also

- **[The gate CLI](gate.md)** — what `gate.py` checks and the counter-probe
  battery it exercises.
- `wyred-harness/README.md` — the flat-layout rationale and the third-party
  fixture / `kicad-cli` notes.
