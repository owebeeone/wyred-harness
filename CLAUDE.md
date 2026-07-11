# wyred-harness (oracle stack) — boundary rules

- Depends on wyred-contract only. **NEVER import wyred (the engine).** Verdicts are computed from artifacts re-read from disk — never from an engine's in-memory opinion of itself.
- Port source: /Users/owebeeone/limbo/elecscad/rl/harness/ . Port acceptance: all 149 selftest checks pass AND verdicts on wyred-contract/goldens/ are identical to the elecscad run.
- The negative battery must fire every code every run or the run fails (lobotomy-proof). Preserve this property through the port.
