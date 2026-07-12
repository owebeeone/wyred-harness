#!/usr/bin/env python3
"""wyred-harness SPICE operating-point oracle tests (WyredPlanSpice step 2.2).

``python3 tests/spice_op_oracle_tests.py``   exercise the DC operating-point
                              vs declared-rails oracle in BOTH layers:

  A. UNIT — ``spice_oracle.check_rails`` graded against synthetic L2 +
     ``.cir.json`` sidecar + op-record fixtures (no ngspice needed):

       1. ON-RAIL     every declared power rail within tolerance -> no finding;
       2. OFF-RAIL    a rail outside the ±2 % default -> exactly
                      ``SPICE_RAIL_OFF`` naming net / declared / simulated / tol;
       3. WITHIN-TOL  a rail just inside the default band -> no finding
                      (the boundary is honoured, not the nominal only);
       4. UNRESOLVED  a declared rail absent from the op point and NOT confessed
                      -> ``SPICE_RAIL_UNRESOLVED``;
       5. CONFESSED   the same absent rail, every part named in
                      ``not_simulated`` -> NO finding (confession over absence);
       6. OVERRIDE    per-rail ``sim_tol_pct`` (WyredSpiceContract §8): a loose
                      override PASSes a deviation the ±2 % default fails, and a
                      tight override FAILs one the default passes — three
                      numbers, three meanings;
       7. ISOLATION   in a three-rail power tree (+3V3 / +5V / VIN) exactly the
                      one off-rail net fires, the other two stay silent.

  B. GATE — the wiring in ``harness/gate.py`` over the REAL emitted examples
     tree (``intent_10_spice_divider`` carries the only ``.cir``), driven by a
     STUB ngspice (spice_run reaches ngspice only as a subprocess, so a stub
     honouring its CLI contract exercises the present-simulator path with no
     real simulator — the whole suite is hermetic and runs without ngspice):

       8. GATE ON-RAIL   stub reports VIN=12 -> the gate prints the per-artifact
                         ``PASS ... SPICE op-oracle`` line, exits 0, and the
                         battery line + ``RESULT: PASS`` are UNCHANGED;
       9. GATE OFF-RAIL  stub reports VIN=10 -> ``SPICE_RAIL_OFF`` fires, the
                         gate exits 1 (``RESULT: FAIL``) while the battery still
                         reads 30/30 (the spice stage is the sole new failure);
      10. GATE FAIL      a stub that crashes -> the gate reports a STRUCTURED
                         ``SPICE run failed`` and exits 1, never a skip;
      11. GATE SKIP      ngspice forced absent -> the gate LOUD-SKIPs naming the
                         unverified VIN rail, exits 0, and every other verdict
                         (battery, ``RESULT: PASS``) is unchanged.

Exit 0 iff every check observes its expected behaviour.

Boundary fences (CLAUDE.md + dev-docs/RunnerSplit.md): the gate-level stages
run gate.py as a SUBPROCESS from a neutral cwd; the unit stages import only the
harness ``spice_oracle`` (which imports neither the engine nor ngspice). The
repo is never written: the emitted tree, the stubs and every gate run live
under one TemporaryDirectory, with ``PYTHONDONTWRITEBYTECODE=1``.

Deliberately NOT wired into selftest.py / selftest_v3.py — their 149-check
contract is frozen (port acceptance); this is its own entry point, the R3
pattern for the SPICE op-oracle. Pure Python 3.10 stdlib.
"""

from __future__ import annotations

import os
import stat
import subprocess
import sys
import tempfile
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent               # wyred-harness/tests/
HARNESS = HERE.parent / "harness"                     # flat sibling modules
GATE_PY = HARNESS / "gate.py"
WORKSPACE = HERE.parents[1]                            # wyred-wz/
ENGINE_SRC = WORKSPACE / "wyred" / "src"
EXAMPLES_CORPUS = WORKSPACE / "wyred-examples" / "corpus"
ARTIFACT = "intent_10_spice_divider"                  # the census .cir artifact

sys.path.insert(0, str(HARNESS))
import spice_oracle                                    # noqa: E402

FAILURES = []


def check(cond: bool, msg: str) -> bool:
    print("  %-12s %s" % ("ok" if cond else "CHECK FAILED", msg))
    if not cond:
        FAILURES.append(msg)
    return cond


# ---------------------------------------------------------------------------
# synthetic fixtures for the unit layer
# ---------------------------------------------------------------------------

def _divider_l2(sim_tol_pct=None):
    """A one-rail L2 in the shape the engine emits for the divider: VIN is the
    declared 12 V power net, GND is ground, div.VOUT an internal signal tap
    (voltage None). Optionally stamp a per-rail ``sim_tol_pct`` override."""
    vin = {"name": "VIN", "kind": "power", "voltage": 12.0,
           "nodes": [["V1", "P"], ["R1", "A"]]}
    if sim_tol_pct is not None:
        vin["sim_tol_pct"] = sim_tol_pct
    return {"nets": [
        {"name": "GND", "kind": "ground", "voltage": 0.0,
         "nodes": [["V1", "N"], ["R2", "B"], ["R3", "B"]]},
        vin,
        {"name": "div.VOUT", "kind": "signal", "voltage": None,
         "nodes": [["R1", "B"], ["R2", "A"], ["R3", "A"]]},
    ]}


def _divider_sidecar(not_simulated=None):
    return {"artifact": ARTIFACT, "node_map": {"GND": "0", "VIN": "VIN",
                                               "div.VOUT": "div_VOUT"},
            "not_simulated": not_simulated or []}


def _rec(nodes):
    """A minimal op-record payload (ngspice lowercases node names)."""
    return {"node_voltages": {k.lower(): v for k, v in nodes.items()}}


def _power_tree_l2():
    """A three-rail power tree (+3V3 / +5V / VIN=12) for the isolation test."""
    return {"nets": [
        {"name": "+3V3", "kind": "power", "voltage": 3.3,
         "nodes": [["U1", "OUT"], ["R1", "A"]]},
        {"name": "+5V", "kind": "power", "voltage": 5.0,
         "nodes": [["U2", "OUT"], ["R2", "A"]]},
        {"name": "VIN", "kind": "power", "voltage": 12.0,
         "nodes": [["J1", "1"]]},
        {"name": "GND", "kind": "ground", "voltage": 0.0, "nodes": []},
    ]}


def _power_tree_sidecar():
    return {"node_map": {"+3V3": "P3V3", "+5V": "P5V", "VIN": "VIN",
                         "GND": "0"}, "not_simulated": []}


# ---------------------------------------------------------------------------
# A. unit tests of spice_oracle.check_rails
# ---------------------------------------------------------------------------

def test_unit_on_rail():
    print("[1/11] UNIT ON-RAIL: rail within tolerance -> no finding")
    f = spice_oracle.check_rails(_divider_l2(), _divider_sidecar(),
                                 _rec({"vin": 12.0, "div_vout": 5.714}))
    check(f == [], "VIN=12.0 (declared 12) -> clean (got %r)" % f)


def test_unit_off_rail():
    print("[2/11] UNIT OFF-RAIL: rail outside ±2%% -> SPICE_RAIL_OFF")
    f = spice_oracle.check_rails(_divider_l2(), _divider_sidecar(),
                                 _rec({"vin": 12.5}))
    codes = [x.code for x in f]
    check(codes == [spice_oracle.SPICE_RAIL_OFF],
          "exactly SPICE_RAIL_OFF fires (got %s)" % codes)
    if f:
        m = f[0]
        check(m.net == "VIN" and abs(m.declared - 12.0) < 1e-9
              and abs(m.simulated - 12.5) < 1e-9 and abs(m.tol_pct - 2.0) < 1e-9,
              "finding names net/declared/simulated/tol (%r)" % (m.msg,))
        check("declared 12V" in m.msg and "simulated 12.5V" in m.msg
              and "+/-2%" in m.msg, "message is self-explanatory: %r" % m.msg)


def test_unit_within_tol():
    print("[3/11] UNIT WITHIN-TOL: just inside the ±2%% band -> no finding")
    # 12 * 2% = 0.24 V band; 12.2 is inside, 12.25 is outside.
    inside = spice_oracle.check_rails(_divider_l2(), _divider_sidecar(),
                                      _rec({"vin": 12.2}))
    outside = spice_oracle.check_rails(_divider_l2(), _divider_sidecar(),
                                       _rec({"vin": 12.25}))
    check(inside == [], "12.2 V inside the band -> clean")
    check([x.code for x in outside] == [spice_oracle.SPICE_RAIL_OFF],
          "12.25 V outside the band -> SPICE_RAIL_OFF")


def test_unit_unresolved():
    print("[4/11] UNIT UNRESOLVED: rail absent from op, unconfessed -> "
          "SPICE_RAIL_UNRESOLVED")
    f = spice_oracle.check_rails(_divider_l2(), _divider_sidecar(),
                                 _rec({"div_vout": 5.7}))   # no VIN node
    codes = [x.code for x in f]
    check(codes == [spice_oracle.SPICE_RAIL_UNRESOLVED],
          "exactly SPICE_RAIL_UNRESOLVED fires (got %s)" % codes)
    if f:
        check(f[0].net == "VIN" and "not covered by the not_simulated" in f[0].msg,
              "finding names the net + the confession gap: %r" % f[0].msg)


def test_unit_confessed_absence():
    print("[5/11] UNIT CONFESSED: absent rail, all parts confessed -> "
          "no finding (confession over absence)")
    # VIN's parts V1+R1 both named in not_simulated -> its absence is honest.
    side = _divider_sidecar(not_simulated=[
        {"refdes": "V1", "kind": "source", "reason": "no_model"},
        {"refdes": "R1", "kind": "resistor", "reason": "no_model"}])
    f = spice_oracle.check_rails(_divider_l2(), side, _rec({"div_vout": 5.7}))
    check(f == [], "fully-confessed absent rail is clean (got %r)" % f)
    # but a PARTIAL confession (R1 only) still fires UNRESOLVED — V1 was modelled
    side2 = _divider_sidecar(not_simulated=[
        {"refdes": "R1", "kind": "resistor", "reason": "no_model"}])
    f2 = spice_oracle.check_rails(_divider_l2(), side2, _rec({"div_vout": 5.7}))
    check([x.code for x in f2] == [spice_oracle.SPICE_RAIL_UNRESOLVED],
          "a partially-confessed rail still fires UNRESOLVED")


def test_unit_tol_override():
    print("[6/11] UNIT OVERRIDE: per-rail sim_tol_pct (WyredSpiceContract §8)")
    # default ±2% FAILs 12.5; a 5% override PASSes it.
    loose = spice_oracle.check_rails(_divider_l2(sim_tol_pct=5.0),
                                     _divider_sidecar(), _rec({"vin": 12.5}))
    check(loose == [], "sim_tol_pct=5 PASSes a 12.5 V rail the default fails")
    check(abs(spice_oracle.rail_tol_pct(_divider_l2(sim_tol_pct=5.0)["nets"][1])
              - 5.0) < 1e-9, "rail_tol_pct reads the override (5.0)")
    check(abs(spice_oracle.rail_tol_pct(_divider_l2()["nets"][1]) - 2.0) < 1e-9,
          "rail_tol_pct falls back to the ±2% default")
    # a TIGHT override FAILs a deviation the default passes (12.1 within 2%).
    tight = spice_oracle.check_rails(_divider_l2(sim_tol_pct=0.5),
                                     _divider_sidecar(), _rec({"vin": 12.1}))
    check([x.code for x in tight] == [spice_oracle.SPICE_RAIL_OFF],
          "sim_tol_pct=0.5 FAILs a 12.1 V rail the ±2% default passes")
    # an attrs-nested override is also honoured (forward-compat)
    net_attrs = {"name": "VIN", "kind": "power", "voltage": 12.0, "nodes": [],
                 "attrs": {"sim_tol_pct": 10.0}}
    check(abs(spice_oracle.rail_tol_pct(net_attrs) - 10.0) < 1e-9,
          "rail_tol_pct reads an attrs-nested override")


def test_unit_isolation():
    print("[7/11] UNIT ISOLATION: one off-rail net fires; the others stay "
          "silent")
    l2, side = _power_tree_l2(), _power_tree_sidecar()
    # +5V off by 0.5 (>2% of 5 = 0.1); +3V3 and VIN on-rail.
    f = spice_oracle.check_rails(
        l2, side, _rec({"p3v3": 3.3, "p5v": 5.5, "vin": 12.0}))
    check([x.code for x in f] == [spice_oracle.SPICE_RAIL_OFF]
          and f and f[0].net == "+5V",
          "only +5V fires SPICE_RAIL_OFF (got %s)"
          % [(x.code, x.net) for x in f])
    # all three on-rail -> clean
    clean = spice_oracle.check_rails(
        l2, side, _rec({"p3v3": 3.3, "p5v": 5.0, "vin": 12.0}))
    check(clean == [], "all three rails on-rail -> clean")


# ---------------------------------------------------------------------------
# B. gate-level tests — the wiring in gate.py, driven by a stub ngspice
# ---------------------------------------------------------------------------

_STUB = r'''#!/usr/bin/env python3
import sys
from pathlib import Path
if "--version" in sys.argv:
    print("ngspice-stub-40 : wyred spice_op_oracle test stub"); sys.exit(0)
%(crash)s
deck = sys.argv[-1]
raw = None
for line in Path(deck).read_text().splitlines():
    s = line.strip()
    if s.lower().startswith("write "):
        raw = s.split(None, 1)[1].strip(); break
if raw is None:
    sys.exit(3)
nodes = %(nodes)r
head = ["Title: stub", "Date: x", "Plotname: Operating Point", "Flags: real",
        "No. Variables: %%d" %% len(nodes), "No. Points: 1", "Variables:"]
for i, name in enumerate(nodes):
    head.append("\t%%d\tv(%%s)\tvoltage" %% (i, name))
head.append("Values:")
vals = ["0\t%%.15e" %% nodes[list(nodes)[0]]]
for name in list(nodes)[1:]:
    vals.append("\t%%.15e" %% nodes[name])
Path(raw).write_text("\n".join(head + vals) + "\n")
sys.exit(0)
'''


def _write_stub(path: Path, nodes=None, crash=False) -> Path:
    body = ('sys.stderr.write("stub: deliberate crash\\n"); sys.exit(1)'
            if crash else "")
    path.write_text(_STUB % {"crash": body, "nodes": nodes or {}})
    path.chmod(path.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    return path


def _emit(out_dir: Path, cwd: Path):
    env = dict(os.environ)
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    parts = [str(ENGINE_SRC), str(EXAMPLES_CORPUS.resolve().parent)]
    if env.get("PYTHONPATH"):
        parts.append(env["PYTHONPATH"])
    env["PYTHONPATH"] = os.pathsep.join(parts)
    return subprocess.run(
        [sys.executable, "-m", "wyred.emit", "--corpus-dir",
         str(EXAMPLES_CORPUS), "--out", str(out_dir)],
        cwd=str(cwd), env=env, capture_output=True, text=True)


def _gate(tree: Path, cwd: Path, ngspice_env: str):
    env = dict(os.environ)
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    env["WYRED_NGSPICE"] = ngspice_env               # stub, or a missing path
    return subprocess.run(
        [sys.executable, str(GATE_PY), "--tree", str(tree), "--corpus-dir",
         str(EXAMPLES_CORPUS), "--engine-src", str(ENGINE_SRC)],
        cwd=str(cwd), env=env, capture_output=True, text=True)


def test_gate_on_rail(tree, cwd, tmp):
    print("[8/11] GATE ON-RAIL: stub VIN=12 -> gate PASS, verdicts unchanged")
    stub = _write_stub(tmp / "ngspice_onrail",
                       nodes={"vin": 12.0, "div_vout": 5.714})
    proc = _gate(tree, cwd, str(stub))
    check(proc.returncode == 0, "gate exits 0 (got %d)" % proc.returncode)
    check(("PASS %s" % ARTIFACT) in proc.stdout
          and "SPICE op-oracle" in proc.stdout,
          "gate prints the per-artifact SPICE op-oracle PASS line")
    check("30/30 probes fired" in proc.stdout,
          "the counter-probe battery line is unchanged (30/30)")
    check("\nRESULT: PASS" in proc.stdout, "RESULT: PASS unchanged")
    if proc.returncode != 0:
        print(proc.stdout[-2500:] + proc.stderr[-800:])


def test_gate_off_rail(tree, cwd, tmp):
    print("[9/11] GATE OFF-RAIL: stub VIN=10 -> SPICE_RAIL_OFF, gate FAIL")
    stub = _write_stub(tmp / "ngspice_offrail",
                       nodes={"vin": 10.0, "div_vout": 4.76})
    proc = _gate(tree, cwd, str(stub))
    check(proc.returncode == 1, "gate exits 1 (got %d)" % proc.returncode)
    check("SPICE_RAIL_OFF" in proc.stdout and "net VIN" in proc.stdout,
          "SPICE_RAIL_OFF fires naming net VIN")
    check("30/30 probes fired" in proc.stdout,
          "the battery still fully fired (spice is the sole new failure)")
    import re
    check(re.search(r"RESULT: FAIL \(\d+ artifact\(s\), 1 failure\(s\)\)",
                    proc.stdout) is not None,
          "the off-rail is the run's ONLY failure")
    if not ("SPICE_RAIL_OFF" in proc.stdout):
        print(proc.stdout[-2500:] + proc.stderr[-800:])


def test_gate_structured_fail(tree, cwd, tmp):
    print("[10/11] GATE FAIL: a crashing ngspice -> structured FAIL, not skip")
    stub = _write_stub(tmp / "ngspice_crash", crash=True)
    proc = _gate(tree, cwd, str(stub))
    check(proc.returncode == 1, "gate exits 1 (got %d)" % proc.returncode)
    check("SPICE run failed" in proc.stdout and "nonzero_exit" in proc.stdout,
          "the broken run is a STRUCTURED failure (nonzero_exit), never a skip")
    check("SKIPPED: SPICE" not in proc.stdout,
          "a crash is not laundered into a loud skip")


def test_gate_skip(tree, cwd, tmp):
    print("[11/11] GATE SKIP: ngspice absent -> loud skip, gate PASS unchanged")
    missing = tmp / "no-such-ngspice-binary"          # forces find_ngspice None
    proc = _gate(tree, cwd, str(missing))
    check(proc.returncode == 0, "gate exits 0 on the skip (got %d)"
          % proc.returncode)
    check("SKIPPED: SPICE DC operating-point" in proc.stdout,
          "the loud SKIP banner is present")
    check("rail VIN = 12V" in proc.stdout and "NOT VERIFIED" in proc.stdout,
          "the banner names the unverified VIN rail")
    check("30/30 probes fired" in proc.stdout and "\nRESULT: PASS" in proc.stdout,
          "every other verdict is unchanged (battery 30/30, RESULT: PASS)")
    if proc.returncode != 0:
        print(proc.stdout[-2500:] + proc.stderr[-800:])


def main() -> int:
    for path, what in ((ENGINE_SRC / "wyred", "wyred engine src"),
                       (GATE_PY, "harness gate"),
                       (EXAMPLES_CORPUS / ("%s.py" % ARTIFACT),
                        "the %s corpus module" % ARTIFACT)):
        if not path.exists():
            print("setup error: %s not found at %s" % (what, path))
            return 2

    t0 = time.monotonic()
    # unit layer (no ngspice, no tree)
    test_unit_on_rail()
    test_unit_off_rail()
    test_unit_within_tol()
    test_unit_unresolved()
    test_unit_confessed_absence()
    test_unit_tol_override()
    test_unit_isolation()

    # gate layer — one emit of the real examples corpus, reused across stubs
    with tempfile.TemporaryDirectory(prefix="wyred-spice-op-") as td:
        tmp = Path(td)
        cwd = tmp / "neutral-cwd"
        cwd.mkdir()
        tree = tmp / "out"
        proc = _emit(tree, cwd)
        if not check(proc.returncode == 0,
                     "emit of examples corpus exits 0 (got %d)"
                     % proc.returncode):
            print(proc.stdout[-2000:] + proc.stderr[-2000:])
            return 1
        if not check((tree / ("%s.cir" % ARTIFACT)).exists(),
                     "%s emitted a .cir deck (the gate stage has an artifact)"
                     % ARTIFACT):
            return 1
        test_gate_on_rail(tree, cwd, tmp)
        test_gate_off_rail(tree, cwd, tmp)
        test_gate_structured_fail(tree, cwd, tmp)
        test_gate_skip(tree, cwd, tmp)

    dt = time.monotonic() - t0
    if FAILURES:
        print("\nSPICE OP-ORACLE TESTS: FAIL — %d check(s) failed (%.1fs):"
              % (len(FAILURES), dt))
        for msg in FAILURES:
            print("  - %s" % msg)
        return 1
    print("\nSPICE OP-ORACLE TESTS: PASS (11 tests, %.1fs)" % dt)
    return 0


if __name__ == "__main__":
    sys.exit(main())
