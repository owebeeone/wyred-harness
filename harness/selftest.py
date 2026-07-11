"""Self-test for the circuit-connectivity oracle harness.

Run from INSIDE wyred-harness/harness/ ::

    python selftest.py

It asserts (and prints a readable summary of):

  (a) every GOLDEN yields its expected top-level verdict via
      run_erc + check_invariants + equivalence;
  (b) every KNOWN_BAD is caught with its expected code;
  (c) intent #8's mutual_exclusion invariant HOLDS on the clean golden, and the
      interlock-removed variant yields SHOOT_THROUGH;
  (d) a golden serialized via to_json and re-normalized round-trips (agrees)
      structurally with itself;
  and additionally, as a smoke test of the scoring layer, that the "perfect
  candidate" (the goldens fed back in) scores a full 8/8 verdict match.

Exit status is 0 iff every check passes, non-zero otherwise. Pure stdlib.
"""

from __future__ import annotations

import sys
from typing import List

from schema import to_json, Component, Invariant, Net, Terminal
from erc import run_erc
from invariant import check_invariants
from equivalence import equivalent
from normalize import from_neutral_json
from roundtrip import agree
from goldens import GOLDENS, KNOWN_BAD, _golden_1, _golden_8, _kb_shoot_through
from score import verdict_for, perfect_candidate, score_candidate


# ---------------------------------------------------------------------------
# tiny assertion harness (collects failures instead of aborting on the first)
# ---------------------------------------------------------------------------

class _Results:
    def __init__(self) -> None:
        self.failures: List[str] = []
        self.checks = 0

    def check(self, ok: bool, label: str) -> None:
        self.checks += 1
        status = "ok  " if ok else "FAIL"
        print("  [%s] %s" % (status, label))
        if not ok:
            self.failures.append(label)


R = _Results()


# ---------------------------------------------------------------------------
# (a) every golden yields its expected verdict
# ---------------------------------------------------------------------------

def test_goldens_verdicts() -> None:
    print("\n(a) golden verdicts (run_erc + check_invariants + equivalence)")
    for iid, entry in GOLDENS.items():
        golden = entry["graph"]
        expected = entry["expected"]
        got = verdict_for(golden, golden)
        R.check(
            got == expected,
            "intent %d: expected %-24s got %s" % (iid, expected, got),
        )


# ---------------------------------------------------------------------------
# (b) every known-bad is caught with its expected code
# ---------------------------------------------------------------------------

def test_known_bad_caught() -> None:
    print("\n(b) KNOWN_BAD variants each caught with expected code")
    for kb in KNOWN_BAD:
        graph = kb["graph"]
        expected_code = kb["expected_code"]
        codes = {v.code for v in run_erc(graph)}
        codes |= {v.code for v in check_invariants(graph)}
        R.check(
            expected_code in codes,
            "%-22s caught (codes=%s) :: %s" % (
                expected_code,
                ",".join(sorted(codes)) or "<none>",
                kb.get("note", ""),
            ),
        )


# ---------------------------------------------------------------------------
# (c) #8 invariant holds clean; interlock-removed variant -> SHOOT_THROUGH
# ---------------------------------------------------------------------------

def test_invariant_holds_and_breaks() -> None:
    print("\n(c) intent #8 mutual_exclusion invariant")
    golden8 = GOLDENS[8]["graph"]
    inv_viol = check_invariants(golden8)
    R.check(
        len(inv_viol) == 0,
        "clean golden 8: invariant HOLDS (no SHOOT_THROUGH), got %d violation(s)"
        % len(inv_viol),
    )
    # also confirm ERC is clean on golden 8 so its verdict is a real PASS
    R.check(
        len(run_erc(golden8)) == 0,
        "clean golden 8: ERC clean (no structural violations)",
    )

    # locate the hand-built shoot-through known-bad
    st = None
    for kb in KNOWN_BAD:
        if kb["expected_code"] == "SHOOT_THROUGH":
            st = kb["graph"]
            break
    R.check(st is not None, "SHOOT_THROUGH known-bad variant present")
    if st is not None:
        codes = {v.code for v in check_invariants(st)}
        R.check(
            "SHOOT_THROUGH" in codes,
            "interlock-removed golden 8: yields SHOOT_THROUGH (codes=%s)"
            % (",".join(sorted(codes)) or "<none>"),
        )


# ---------------------------------------------------------------------------
# (d) round-trip: to_json -> from_neutral_json agrees structurally
# ---------------------------------------------------------------------------

def test_roundtrip() -> None:
    print("\n(d) round-trip agreement (to_json -> from_neutral_json)")
    for iid, entry in GOLDENS.items():
        golden = entry["graph"]
        reparsed = from_neutral_json(to_json(golden))
        R.check(
            agree([golden, reparsed]),
            "intent %d: serialized+re-normalized graph agrees with original" % iid,
        )


# ---------------------------------------------------------------------------
# (extra) scoring layer smoke test: perfect candidate scores 8/8
# ---------------------------------------------------------------------------

def test_score_perfect_candidate() -> None:
    print("\n(e) scoring layer: perfect candidate (goldens fed back in)")
    result = score_candidate(perfect_candidate())
    R.check(
        result["n_match"] == result["n_intents"],
        "perfect candidate matches all verdicts: %d/%d"
        % (result["n_match"], result["n_intents"]),
    )
    hist = result["catch_histogram"]
    R.check(
        hist["silent"] == 0,
        "catch histogram has NO silent (uncaught) mutants: %s" % (hist,),
    )
    R.check(
        result["oracle_quality"] == 1.0,
        "oracle_quality (round-trip agreement) is 1.0: %.3f (%s)"
        % (result["oracle_quality"], result["oracle_quality_detail"]),
    )
    # print the per-intent verdict table for visibility
    print("      per-intent verdicts:")
    for iid, info in result["per_intent"].items():
        print("        intent %d: %-24s (expected %s)%s" % (
            iid, info["verdict"], info["expected"],
            "" if info["match"] else "   <-- MISMATCH",
        ))


# ---------------------------------------------------------------------------
# (f) FIX 1 -- a mutual_exclusion invariant anchored on UNDRIVEN nodes is
#     rejected as VACUOUS_INVARIANT (cannot be gamed by anchoring on always-low
#     nodes so the never-both-high check passes vacuously).
# ---------------------------------------------------------------------------

def _vacuous_invariant_graph():
    """Golden 8 plus a mutual_exclusion invariant whose guarded outputs are
    anchored on nodes NO gate ever drives high (an AND gate whose own inputs are
    undriven) -> both outputs are always-low -> the guarantee is vacuous."""
    g = _golden_8()
    dead = Component(
        refdes="UND", kind="logic_gate", value="DEAD", authored=True,
        logic_fn="and",
        terminals=[
            Terminal("A", "logic_in"), Terminal("B", "logic_in"),
            Terminal("YA", "logic_out"), Terminal("YB", "logic_out"),
        ],
    )
    g.components.append(dead)
    # UND.A / UND.B sit on nets with no driver -> always low -> YA/YB always low
    g.nets.append(Net("undA", "signal", None, [("UND", "A")]))
    g.nets.append(Net("undB", "signal", None, [("UND", "B")]))
    g.nets.append(Net("undYA", "signal", None, [("UND", "YA")]))
    g.nets.append(Net("undYB", "signal", None, [("UND", "YB")]))
    g.invariants.append(Invariant(
        kind="mutual_exclusion",
        a=("UND", "YA"), b=("UND", "YB"),
        inputs=[("U1", "IN_H1"), ("U1", "IN_L1")],
    ))
    return g


def test_vacuous_invariant_rejected() -> None:
    print("\n(f) FIX 1: interlock anchored on undriven nodes -> VACUOUS_INVARIANT")
    g = _vacuous_invariant_graph()
    codes = {v.code for v in check_invariants(g)}
    R.check(
        "VACUOUS_INVARIANT" in codes,
        "undriven-anchored mutual_exclusion is caught VACUOUS (codes=%s)"
        % (",".join(sorted(codes)) or "<none>"),
    )
    # the clean golden 8 (real cross-inhibit interlock) must NOT be flagged
    # vacuous: both guarded outputs CAN be driven high.
    codes8 = {v.code for v in check_invariants(_golden_8())}
    R.check(
        "VACUOUS_INVARIANT" not in codes8,
        "clean golden 8 interlock is NOT vacuous (both gates fireable); codes=%s"
        % (",".join(sorted(codes8)) or "<none>"),
    )


# ---------------------------------------------------------------------------
# (g) FIX 2 -- STRUCTURED ERC catches VOLTAGE_MISMATCH and ADDR_COLLISION on an
#     "off-label" netlist: function strings are blanked/garbage, but the
#     structured fields (req_v / iface / i2c_addr) are correct. A string-keyed
#     ERC would read this as vacuously clean; the structured ERC does not.
# ---------------------------------------------------------------------------

def _off_label_graph():
    """Golden 1 with EVERY function string replaced by garbage, then two faults
    injected purely via STRUCTURED fields."""
    g = _golden_1()
    for c in g.components:
        for t in c.terminals:
            t.function = "zzz_off_label"        # nothing may key off this
    for c in g.components:
        if c.refdes == "U2":                    # MCU: now requires 5V structurally
            c.terminal("VDD").req_v = 5.0       # while still on the 3.3V rail
        if c.refdes == "U4":                    # sensor: addr collides with U3
            c.attrs["i2c_addr"] = 0x48
    return g


def test_structured_erc_off_label() -> None:
    print("\n(g) FIX 2: structured ERC on an OFF-LABEL netlist (garbage function "
          "strings)")
    g = _off_label_graph()
    codes = {v.code for v in run_erc(g)}
    R.check(
        "VOLTAGE_MISMATCH" in codes,
        "off-label VOLTAGE_MISMATCH caught via structured req_v (codes=%s)"
        % (",".join(sorted(codes)) or "<none>"),
    )
    R.check(
        "ADDR_COLLISION" in codes,
        "off-label ADDR_COLLISION caught via structured iface+i2c_addr (codes=%s)"
        % (",".join(sorted(codes)) or "<none>"),
    )


# ---------------------------------------------------------------------------
# (h) FIX 3 -- the 2-input interlock golden #8 PASSES, and a PASS-THROUGH
#     2-input interlock (no cross-inhibit) yields SHOOT_THROUGH at (1,1).
# ---------------------------------------------------------------------------

def test_two_input_interlock() -> None:
    print("\n(h) FIX 3: 2-input cross-inhibit interlock (#8) PASSES; pass-through "
          "-> SHOOT_THROUGH")
    golden8 = GOLDENS[8]["graph"]
    # each bridge's invariant enumerates over 2 INDEPENDENT command inputs
    two_input = all(
        len(inv.inputs) == 2
        for inv in golden8.invariants
        if inv.kind == "mutual_exclusion"
    )
    R.check(
        two_input and len(golden8.invariants) >= 1,
        "golden 8 declares a 2-input interlock per bridge (%d invariant(s))"
        % len(golden8.invariants),
    )
    clean = {v.code for v in check_invariants(golden8)}
    clean |= {v.code for v in run_erc(golden8)}
    R.check(
        not clean,
        "golden 8 (2-input cross-inhibit) is fully clean, codes=%s"
        % (",".join(sorted(clean)) or "<none>"),
    )

    # pass-through 2-input interlock -> shoot-through at command (1,1)
    kb = _kb_shoot_through()
    st_viols = check_invariants(kb)
    st_codes = {v.code for v in st_viols}
    R.check(
        "SHOOT_THROUGH" in st_codes,
        "pass-through 2-input interlock yields SHOOT_THROUGH (codes=%s)"
        % (",".join(sorted(st_codes)) or "<none>"),
    )
    hit_11 = any(
        v.code == "SHOOT_THROUGH" and "IN_H1=1" in v.msg and "IN_L1=1" in v.msg
        for v in st_viols
    )
    R.check(
        hit_11,
        "pass-through counterexample is the (IN_H1=1, IN_L1=1) command",
    )


# ---------------------------------------------------------------------------
# driver
# ---------------------------------------------------------------------------

def main() -> int:
    print("=" * 70)
    print("circuit-oracle harness self-test")
    print("=" * 70)

    test_goldens_verdicts()
    test_known_bad_caught()
    test_invariant_holds_and_breaks()
    test_roundtrip()
    test_score_perfect_candidate()
    test_vacuous_invariant_rejected()
    test_structured_erc_off_label()
    test_two_input_interlock()

    print("\n" + "=" * 70)
    if R.failures:
        print("RESULT: FAIL  (%d/%d checks failed)" % (len(R.failures), R.checks))
        for f in R.failures:
            print("  - %s" % f)
        print("=" * 70)
        return 1
    print("RESULT: PASS  (all %d checks passed)" % R.checks)
    print("=" * 70)
    return 0


if __name__ == "__main__":
    sys.exit(main())
