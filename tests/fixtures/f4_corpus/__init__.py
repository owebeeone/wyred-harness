"""F4 regression fixture corpus (see tests/run_gate_tests.py).

One intent that DECLARES a clean layer 1 (``expected_l1 = ()``) while its
composition carries an address collision. The engine trusts the declaration
(emit exits 0, the collision surfaces only as an emit-time diagnostic); the
gate's public L1 oracle must disagree and fail the run. Modeled on the
examples corpus's intent_03_addr_collision, with the declaration flipped.
"""
