#!/usr/bin/env python3
"""wyred SPICE structural-oracle regression tests (WyredPlanSpice step 1.3).

``python3 tests/run_spice_oracle_tests.py``   exercise the ``.cir`` structural
                              oracle END-TO-END — real ``python3 -m wyred.emit``
                              and ``python3 -m wyred.crosscheck`` subprocesses,
                              real artifact trees — and assert both directions:

    1. CLEAN       the emitted ``intent_10_spice_divider`` deck + sidecar
                   crosscheck CLEAN from disk (exit 0, no XCIR_* codes) —
                   the third denotation agrees with the L2;
    2. CODES FIRE  four doctored copies of that artifact set — a dropped
                   element, a rewired node, a rewritten value, a forged
                   confession — each make ``python3 -m wyred.crosscheck``
                   exit 1 and name EXACTLY its XCIR_* code (isolation, so
                   the harness battery's one-code-per-probe accounting is
                   sound);
    3. LOBOTOMY    with ``paths.crosscheck_cir`` BLANKED (a temp engine copy
                   whose function is overridden to ``return []``), the gate
                   over the clean tree goes RED: the four spice counter-probes
                   can no longer fire, so the lobotomy verdict fails the run —
                   proving the checks are load-bearing, not decorative (the
                   R3/DecisionLog restoration shape, applied to XCIR).

Exit 0 iff every check observes its expected behavior.

Boundary fences (CLAUDE.md + dev-docs/RunnerSplit.md): this runner imports
NOTHING from the engine or harness — every stage is a subprocess meeting the
others only at artifacts on disk, from a neutral cwd. The repo is never
written: all emits, tree copies, doctored decks, and the blanked engine copy
live under one TemporaryDirectory, and every subprocess runs with
PYTHONDONTWRITEBYTECODE=1.

Deliberately NOT wired into selftest.py / selftest_v3.py: their 149-check
contract is frozen (port acceptance); this file is its own entry point, the
R3 pattern for the SPICE oracle.

Pure Python 3.10 stdlib.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent            # wyred-harness/tests/
HARNESS = HERE.parent / "harness"
GATE_PY = HARNESS / "gate.py"
WORKSPACE = HERE.parents[1]                        # wyred-wz/
ENGINE_SRC = WORKSPACE / "wyred" / "src"
EXAMPLES_CORPUS = WORKSPACE / "wyred-examples" / "corpus"
ARTIFACT = "intent_10_spice_divider"              # the census spice artifact

FAILURES = []


def check(cond: bool, msg: str) -> bool:
    print("  %-12s %s" % ("ok" if cond else "CHECK FAILED", msg))
    if not cond:
        FAILURES.append(msg)
    return cond


def _env(extra_pythonpath=None) -> dict:
    env = dict(os.environ)
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    parts = [str(extra_pythonpath)] if extra_pythonpath else [str(ENGINE_SRC)]
    parts.append(str(EXAMPLES_CORPUS.resolve().parent))     # corpus package parent
    if env.get("PYTHONPATH"):
        parts.append(env["PYTHONPATH"])
    env["PYTHONPATH"] = os.pathsep.join(parts)
    return env


def _emit(out_dir: Path, cwd: Path, engine_src=ENGINE_SRC):
    return subprocess.run(
        [sys.executable, "-m", "wyred.emit",
         "--corpus-dir", str(EXAMPLES_CORPUS), "--out", str(out_dir)],
        cwd=str(cwd), env=_env(engine_src), capture_output=True, text=True)


def _crosscheck(tree: Path, name: str, cwd: Path):
    return subprocess.run(
        [sys.executable, "-m", "wyred.crosscheck",
         "--dir", str(tree), "--artifact", name],
        cwd=str(cwd), env=_env(), capture_output=True, text=True)


def _codes(proc, name: str):
    """XCIR_* / XPATH_* codes the crosscheck CLI printed for ``name``."""
    prefix = name + " "
    out = set()
    for ln in proc.stdout.splitlines():
        if ln.startswith(prefix):
            out.add(ln[len(prefix):].split(":", 1)[0])
    return out


# --- deck / sidecar doctoring (independent of the harness battery) -----------

def _element_lines(deck: str):
    """(index, line) for each element card in the deck text (skip comments,
    dot cards, continuations, subckt-definition bodies)."""
    out = []
    in_subckt = False
    for i, ln in enumerate(deck.splitlines()):
        s = ln.strip()
        if not s or s.startswith("*") or s.startswith("+"):
            continue
        if s.startswith("."):
            low = s.split()[0].lower()
            in_subckt = (low == ".subckt") or (in_subckt and low != ".ends")
            continue
        if in_subckt:
            continue
        out.append((i, ln))
    return out


def _drop_element(deck: str) -> str:
    lines = deck.splitlines()
    idx, _ = _element_lines(deck)[0]
    del lines[idx]
    return "\n".join(lines) + "\n"


def _rewire_node(deck: str) -> str:
    lines = deck.splitlines()
    idx, ln = _element_lines(deck)[0]
    toks = ln.split()
    for j in (1, 2):
        if j < len(toks) and toks[j] != "0":
            toks[j] = "0"
            break
    lines[idx] = " ".join(toks)
    return "\n".join(lines) + "\n"


def _rewrite_value(deck: str) -> str:
    lines = deck.splitlines()
    for idx, ln in _element_lines(deck):
        toks = ln.split()
        if toks and toks[0][:1].upper() in "RCLDVI" and len(toks) >= 4:
            toks[-1] = "TAMPERV"
            lines[idx] = " ".join(toks)
            break
    return "\n".join(lines) + "\n"


def _forge_confession(side: dict) -> dict:
    s = json.loads(json.dumps(side))
    s.setdefault("not_simulated", []).append(
        {"refdes": "PHANTOM_NS", "kind": "asic", "reason": "no_model"})
    return s


# ---------------------------------------------------------------------------

def test_1_clean(tree: Path, cwd: Path) -> None:
    print("[1/3] CLEAN: the emitted deck crosschecks clean from disk")
    proc = _crosscheck(tree, ARTIFACT, cwd)
    check(proc.returncode == 0,
          "crosscheck of clean %s exits 0 (got %d)" % (ARTIFACT, proc.returncode))
    check(not _codes(proc, ARTIFACT),
          "no XCIR_* code fires on the clean deck (got %s)"
          % (sorted(_codes(proc, ARTIFACT)) or "[]"))


def test_2_codes_fire(tree: Path, tmp: Path, cwd: Path) -> None:
    print("[2/3] CODES FIRE: each doctored deck fires exactly its XCIR_* code")
    deck = (tree / ("%s.cir" % ARTIFACT)).read_text()
    side = json.loads((tree / ("%s.cir.json" % ARTIFACT)).read_text())
    cases = [
        ("XCIR_COMPONENTS",    "drop one element card",
         _drop_element(deck), side),
        ("XCIR_NET_PARTITION", "rewire one node to ground",
         _rewire_node(deck), side),
        ("XCIR_ELEMENT",       "rewrite one element value",
         _rewrite_value(deck), side),
        ("XCIR_CONFESSION",    "forge a not_simulated entry",
         deck, _forge_confession(side)),
    ]
    for i, (want, what, dtext, dside) in enumerate(cases):
        d = tmp / ("doctored_%d" % i)
        shutil.copytree(tree, d)
        (d / ("%s.cir" % ARTIFACT)).write_text(dtext)
        (d / ("%s.cir.json" % ARTIFACT)).write_text(json.dumps(dside))
        proc = _crosscheck(d, ARTIFACT, cwd)
        fired = _codes(proc, ARTIFACT)
        check(proc.returncode == 1 and fired == {want},
              "%s -> exit 1 and ONLY %s fires (got exit %d, codes %s)"
              % (what, want, proc.returncode, sorted(fired) or "[]"))


def test_3_lobotomy(tree: Path, tmp: Path, cwd: Path) -> None:
    print("[3/3] LOBOTOMY: crosscheck_cir blanked -> the gate goes red")
    # Copy the engine and OVERRIDE paths.crosscheck_cir to return [] (a
    # module-level redefinition wins; crosscheck.py looks the attribute up at
    # call time). build_cir is untouched, so the tree still emits a valid deck.
    lobo = tmp / "engine_lobo" / "src"
    shutil.copytree(ENGINE_SRC, lobo)
    with open(lobo / "wyred" / "paths.py", "a", encoding="utf-8") as f:
        f.write("\n\n# LOBOTOMY (run_spice_oracle_tests): blank the SPICE "
                "structural oracle\n"
                "def crosscheck_cir(graph, deck_text, sidecar):\n"
                "    return []\n")
    proc = subprocess.run(
        [sys.executable, str(GATE_PY), "--tree", str(tree),
         "--corpus-dir", str(EXAMPLES_CORPUS), "--engine-src", str(lobo)],
        cwd=str(cwd), env=_env(lobo), capture_output=True, text=True)
    check(proc.returncode == 1,
          "gate with crosscheck_cir blanked exits 1 (got %d)" % proc.returncode)
    cir_probes = ("cir_element_dropped", "cir_node_rewired",
                  "cir_value_rewritten", "cir_confession_forged")
    named = [p for p in cir_probes if p in proc.stdout]
    check(len(named) == len(cir_probes),
          "all four spice counter-probes are reported as not firing (named: %s)"
          % named)
    if proc.returncode != 1:
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
    with tempfile.TemporaryDirectory(prefix="wyred-spice-oracle-") as td:
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
                     "%s emitted a .cir deck (fully modelled)" % ARTIFACT):
            return 1
        test_1_clean(tree, cwd)
        test_2_codes_fire(tree, tmp, cwd)
        test_3_lobotomy(tree, tmp, cwd)

    dt = time.monotonic() - t0
    if FAILURES:
        print("\nSPICE ORACLE TESTS: FAIL — %d check(s) failed (%.1fs):"
              % (len(FAILURES), dt))
        for msg in FAILURES:
            print("  - %s" % msg)
        return 1
    print("\nSPICE ORACLE TESTS: PASS (3 tests, %.1fs)" % dt)
    return 0


if __name__ == "__main__":
    sys.exit(main())
