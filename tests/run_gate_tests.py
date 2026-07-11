#!/usr/bin/env python3
"""wyred-harness gate regression tests (Phase-2 critic residual R3).

``python3 tests/run_gate_tests.py``   exercise ``harness/gate.py``
                              END-TO-END — real ``python3 -m wyred.emit``
                              subprocesses, real gate subprocesses, real
                              artifact trees — and assert the gate's
                              headline verdicts in BOTH directions:

    1. CLEAN       the examples corpus emits and the gate passes it,
                   with the full "26/26 probes fired" battery line —
                   the everything-green baseline;
    2. F4          the committed fixture corpus (tests/fixtures/
                   f4_corpus/) declares a clean layer 1 that actually
                   carries an address collision. The ENGINE trusts the
                   declaration (emit exits 0; the collision is only an
                   emit-time diagnostic) — the GATE must exit 1 with the
                   expected-codes FAIL naming ADDR_COLLISION. This is
                   the F4 declared-vs-oracle disagreement as a
                   committed regression, not an ad-hoc doctored proof;
    3. LOBOTOMY    a reduced corpus (the examples corpus minus the
                   watchy/mppt board modules, so no connector artifacts
                   exist anywhere) must FAIL the gate with the
                   "probe(s) never found an applicable artifact"
                   battery verdict as the run's ONLY failure — proving
                   the all-probes-must-fire lobotomy verdict is live,
                   not decorative;
    4. TAMPER      one dropped BOM line in a copy of the clean tree
                   must fail the gate (cross-path differential).

Exit 0 iff all four tests observe their expected pass/fail behavior.

Boundary fences (CLAUDE.md + dev-docs/RunnerSplit.md) hold here too:
this runner imports NOTHING from the engine, the harness, or the audit —
every stage is a subprocess (``python3 -m wyred.emit``,
``python3 harness/gate.py``) meeting the others only at artifacts on
disk, from a neutral cwd (run_gate.py's sys.path[0] lesson).

The repo is never written: all emits, corpus copies, and doctored trees
live under one TemporaryDirectory, the committed fixture corpus is
COPIED there before the engine ever imports it, and every subprocess
runs with PYTHONDONTWRITEBYTECODE=1 so no __pycache__ lands in-repo.

Deliberately NOT wired into selftest.py / selftest_v3.py: their
149-check contract is frozen (port acceptance); this file is its own
entry point.

Pure Python 3.10 stdlib. Runtime ~15s (each gate run re-emits its corpus
twice for the determinism check).
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent            # wyred-harness/tests/
HARNESS = HERE.parent / "harness"                 # flat sibling modules
GATE_PY = HARNESS / "gate.py"
WORKSPACE = HERE.parents[1]                       # wyred-wz/
ENGINE_SRC = WORKSPACE / "wyred" / "src"
EXAMPLES_CORPUS = WORKSPACE / "wyred-examples" / "corpus"
F4_FIXTURE = HERE / "fixtures" / "f4_corpus"

# the board-encoding modules whose removal leaves a corpus with NO
# connector artifacts (nothing else in the examples corpus references
# them) — the applicability half of the lobotomy verdict must then fire
BOARD_MODULES = ("lib_watchy.py", "lib_mppt.py",
                 "watchy_v1.py", "mppt_2420_hc.py")
CONNECTOR_PROBES = ("connector_row_rewritten", "connector_row_dropped",
                    "connector_lock_stripped", "connector_lock_forged",
                    "connector_function_rewritten")

FAILURES = []


def check(cond: bool, msg: str) -> bool:
    tag = "ok" if cond else "CHECK FAILED"
    print("  %-12s %s" % (tag, msg))
    if not cond:
        FAILURES.append(msg)
    return cond


def _env() -> dict:
    """Subprocess env: engine on PYTHONPATH, no in-repo bytecode."""
    env = dict(os.environ)
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    return env


def _run(argv, cwd: Path):
    return subprocess.run([str(a) for a in argv], cwd=str(cwd),
                          env=_env(), capture_output=True, text=True)


def emit(corpus_dir: Path, out_dir: Path, cwd: Path):
    """One engine emit, the subprocess way (PYTHONPATH = engine src +
    the corpus package's parent, exactly gate.py's _engine_env)."""
    env = _env()
    parts = [str(ENGINE_SRC), str(corpus_dir.resolve().parent)]
    if env.get("PYTHONPATH"):
        parts.append(env["PYTHONPATH"])
    env["PYTHONPATH"] = os.pathsep.join(parts)
    return subprocess.run(
        [sys.executable, "-m", "wyred.emit",
         "--corpus-dir", str(corpus_dir), "--out", str(out_dir)],
        cwd=str(cwd), env=env, capture_output=True, text=True)


def gate(tree: Path, corpus_dir: Path, cwd: Path):
    return _run([sys.executable, GATE_PY, "--tree", tree,
                 "--corpus-dir", corpus_dir, "--engine-src", ENGINE_SRC],
                cwd)


def copy_corpus(src: Path, dst: Path, skip=()) -> None:
    """Copy a corpus package's *.py (never __pycache__) into dst."""
    dst.mkdir(parents=True)
    for py in sorted(src.glob("*.py")):
        if py.name not in skip:
            shutil.copy2(py, dst / py.name)


# ---------------------------------------------------------------------------
# the tests
# ---------------------------------------------------------------------------

def test_1_clean(tmp: Path, cwd: Path) -> Path | None:
    """The examples corpus gates green, battery fully fired.

    Returns the emitted tree (test 4 doctors a copy of it), or None if
    the emit itself failed."""
    print("[1/4] CLEAN: examples corpus emits; gate passes with 26/26")
    out = tmp / "clean_out"
    proc = emit(EXAMPLES_CORPUS, out, cwd)
    if not check(proc.returncode == 0,
                 "emit of examples corpus exits 0 (got %d)"
                 % proc.returncode):
        print(proc.stdout[-2000:] + proc.stderr[-2000:])
        return None
    proc = gate(out, EXAMPLES_CORPUS, cwd)
    ok = check(proc.returncode == 0,
               "gate over clean tree exits 0 (got %d)" % proc.returncode)
    check("26/26 probes fired" in proc.stdout,
          'gate output has the "26/26 probes fired" battery line')
    check("\nRESULT: PASS" in proc.stdout, "gate output has RESULT: PASS")
    if not ok:
        print(proc.stdout[-3000:] + proc.stderr[-1000:])
    return out


def test_2_f4(tmp: Path, cwd: Path) -> None:
    """The committed F4 fixture: engine trusts the clean declaration,
    the gate's L1 oracle disagrees and names ADDR_COLLISION."""
    print("[2/4] F4: declared-clean/oracle-dirty fixture corpus")
    corpus = tmp / "f4" / "f4_corpus"
    copy_corpus(F4_FIXTURE, corpus)
    out = tmp / "f4" / "out"
    proc = emit(corpus, out, cwd)
    check(proc.returncode == 0,
          "emit exits 0 — the engine trusts the declaration (got %d)"
          % proc.returncode)
    check((out / "intent_f4_disagreement.l2.json").exists(),
          "engine even emitted the L2 data path for the lying intent")
    proc = gate(out, corpus, cwd)
    check(proc.returncode == 1,
          "gate exits 1 on the disagreement (got %d)" % proc.returncode)
    fail_line = next((ln for ln in proc.stdout.splitlines()
                      if ln.startswith("FAIL intent_f4_disagreement")), "")
    check("'ADDR_COLLISION'" in fail_line and "(expected [])" in fail_line,
          "expected-codes FAIL names ADDR_COLLISION vs declared [] "
          "(line: %r)" % fail_line)


def test_3_lobotomy(tmp: Path, cwd: Path) -> None:
    """A corpus with no connector artifacts anywhere must fail the
    battery's applicability verdict — and ONLY that."""
    print("[3/4] LOBOTOMY: reduced corpus (no boards) fails the battery")
    corpus = tmp / "reduced" / "corpus"
    copy_corpus(EXAMPLES_CORPUS, corpus, skip=BOARD_MODULES)
    out = tmp / "reduced" / "out"
    proc = emit(corpus, out, cwd)
    check(proc.returncode == 0,
          "emit of reduced corpus exits 0 (got %d)" % proc.returncode)
    proc = gate(out, corpus, cwd)
    check(proc.returncode == 1,
          "gate exits 1 (got %d)" % proc.returncode)
    check("probe(s) never found an applicable artifact" in proc.stdout,
          "battery applicability verdict fired")
    for pid in CONNECTOR_PROBES:
        check(pid in proc.stdout, "unfired probe named: %s" % pid)
    check(re.search(r"RESULT: FAIL \(\d+ artifact\(s\), 1 failure\(s\)\)",
                    proc.stdout) is not None,
          "the battery verdict is the run's ONLY failure "
          "(every per-artifact check passed)")


def test_4_tamper(tmp: Path, cwd: Path, clean_out: Path) -> None:
    """One dropped BOM line in a copy of the clean tree fails the gate."""
    print("[4/4] TAMPER: dropped BOM line in a tree copy fails the gate")
    tampered = tmp / "tampered"
    shutil.copytree(clean_out, tampered)
    bom_path = tampered / "intent_01_sensor_node.bom.json"
    bom = json.loads(bom_path.read_text())
    dropped = bom["line_items"].pop(0)
    bom_path.write_text(json.dumps(bom))
    print("  (dropped %s line %s from intent_01_sensor_node.bom.json)"
          % (dropped.get("kind"), dropped.get("refdes")))
    proc = gate(tampered, EXAMPLES_CORPUS, cwd)
    check(proc.returncode == 1,
          "gate exits 1 on the doctored tree (got %d)" % proc.returncode)
    check(any(ln.startswith("FAIL intent_01_sensor_node")
              and "cross-path differential" in ln
              for ln in proc.stdout.splitlines()),
          "cross-path differential FAIL names the doctored artifact")


def main() -> int:
    for path, what in ((ENGINE_SRC / "wyred", "wyred engine src"),
                       (GATE_PY, "harness gate"),
                       (EXAMPLES_CORPUS, "examples corpus"),
                       (F4_FIXTURE, "f4 fixture corpus")):
        if not path.exists():
            print("setup error: %s not found at %s" % (what, path))
            return 2

    t0 = time.monotonic()
    with tempfile.TemporaryDirectory(prefix="wyred-gate-tests-") as td:
        tmp = Path(td)
        cwd = tmp / "neutral-cwd"       # nothing shadowable on sys.path[0]
        cwd.mkdir()
        clean_out = test_1_clean(tmp, cwd)
        test_2_f4(tmp, cwd)
        test_3_lobotomy(tmp, cwd)
        if clean_out is None:
            FAILURES.append("tamper test skipped: no clean tree")
            print("[4/4] TAMPER: SKIPPED (clean emit failed)")
        else:
            test_4_tamper(tmp, cwd, clean_out)

    dt = time.monotonic() - t0
    if FAILURES:
        print("\nGATE TESTS: FAIL — %d check(s) failed (%.1fs):"
              % (len(FAILURES), dt))
        for msg in FAILURES:
            print("  - %s" % msg)
        return 1
    print("\nGATE TESTS: PASS (4 tests, %.1fs)" % dt)
    return 0


if __name__ == "__main__":
    sys.exit(main())
