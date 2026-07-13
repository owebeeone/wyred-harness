# wyred-harness reference

`wyred-harness` is the **read-only oracle stack**: it decides verdicts on an
emitted artifact tree by re-reading the artifacts *from disk* — never from an
engine's in-memory opinion of itself. It depends on `wyred-contract` only and
**never imports the engine** (`wyred`); it reaches the engine solely as a
subprocess (`python3 -m wyred.emit`, `python3 -m wyred.crosscheck`). The split
that puts verdicts here and emission in `wyred` is `dev-docs/RunnerSplit.md`.

These pages are reference material for the harness's command surface. They are
**not normative**: the shapes of the artifacts and the meaning of every
differential code live in `wyred-contract/EMIT_CONTRACT.md` and the JSON Schemas
under `wyred-contract/schemas/`. The pages here explain how to *run* the harness
and *point at* those contracts; where a page and a contract disagree, the page
is the defect.

## Pages

- **[The gate CLI](gate.md)** — `harness/gate.py`: flags, exit codes, the six
  verdict stages, and the counter-probe battery + lobotomy verdict (pointed at,
  not restated).
- **[Running the selftests](selftests.md)** — `selftest.py` (35 checks),
  `selftest_v3.py` (114 checks; the frozen 149-check port-acceptance contract),
  and the gate regression suite `tests/run_gate_tests.py`.

## Layout note

`harness/` is a flat directory of pure-stdlib Python 3.10 modules using flat
sibling imports (`import intent_oracle`, `from schema import ...`); there is
deliberately no packaging. Run the modules from inside the directory, exactly as
documented on each page. See `wyred-harness/README.md` for the rationale.
