#!/usr/bin/env python3
"""wyred-harness SPICE runner tests (WyredPlanSpice step 2.1).

``python3 tests/spice_run_tests.py``   exercise ``harness/spice_run.py`` on
                              the hand-written conforming fixture deck
                              ``tests/fixtures/spice/rc_divider.cir`` and
                              assert every run outcome:

    1. SKIP        with ngspice genuinely absent (env scrubbed of PATH +
                   WYRED_NGSPICE) the CLI exits EXIT_SKIP (3, distinct from
                   PASS=0 and FAIL=1), prints the loud banner naming the
                   unverified deck, and writes NO record — an absent
                   simulator is never a silent green;
    2. OK          with a STUB ngspice that emits a conforming ASCII
                   rawfile, run_op parses the divider's analytic op
                   (V(P3V3) = 3.3 V, V(P12V) = 12 V, source branch = -1 mA),
                   writes <name>.spiceop.json with the on-disk deck's
                   sha256 and the pinned version, and the CLI exits 0 —
                   tested for both v(node)-wrapped and bare node-voltage
                   rawfile naming;
    3. CRASH       a stub exiting nonzero -> structured FAIL (error_kind
                   nonzero_exit), not a skip;
    4. TIMEOUT     a stub that hangs past the budget -> structured FAIL
                   (error_kind timeout);
    5. GARBAGE     a stub writing a non-rawfile -> structured FAIL
                   (error_kind unparseable) — a corrupted result is a red
                   verdict, never a skip;
    6. NO-RAWFILE  a stub exiting 0 but writing nothing -> structured FAIL
                   (error_kind no_rawfile).

Since ngspice is DELEGATED (subprocess only), the present-simulator paths
are exercised with tiny stub executables honoring spice_run's ngspice CLI
contract (``--version`` -> a version line; ``-b <rundeck>`` -> read the
control block's ``write <path>`` and act) — no real ngspice required, so the
whole suite is hermetic and runs on a machine without ngspice.

Deliberately NOT wired into selftest.py / selftest_v3.py (their 149-check
contract is frozen, per the R3 pattern). Pure Python 3.10 stdlib; runtime a
few seconds; the repo is never written (everything under one TemporaryDirectory).
"""

from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
import tempfile
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent               # wyred-harness/tests/
HARNESS = HERE.parent / "harness"                    # flat sibling modules
SPICE_RUN = HARNESS / "spice_run.py"
FIXTURE = HERE / "fixtures" / "spice" / "rc_divider.cir"

sys.path.insert(0, str(HARNESS))
import spice_run                                      # noqa: E402

FAILURES = []

STUB_VERSION = "ngspice-stub-40 : wyred test stub (not a real simulator)"

# The analytic op of the fixture divider, keyed by ngspice-lowercased node.
EXPECT_NODES = {"p12v": 12.0, "p3v3": 3.3}
EXPECT_BRANCH = -1.0e-3


def check(cond: bool, msg: str) -> bool:
    print("  %-12s %s" % ("ok" if cond else "CHECK FAILED", msg))
    if not cond:
        FAILURES.append(msg)
    return cond


# ---------------------------------------------------------------------------
# stub ngspice builders — each honors spice_run's CLI contract
# ---------------------------------------------------------------------------

_STUB_HEADER = r'''#!/usr/bin/env python3
import sys, time
from pathlib import Path

def raw_path_from_deck():
    # spice_run writes "-b <rundeck>"; the rundeck's .control block has a
    # line "write <rawpath>". Recover it the way real ngspice would honor it.
    deck = sys.argv[-1]
    for line in Path(deck).read_text().splitlines():
        s = line.strip()
        if s.lower().startswith("write "):
            return s.split(None, 1)[1].strip()
    return None

if "--version" in sys.argv:
    print(%(version)r)
    sys.exit(0)
'''


def _write_stub(dirpath: Path, name: str, body: str) -> Path:
    """Materialize an executable python stub honoring the ngspice contract."""
    path = dirpath / name
    src = (_STUB_HEADER % {"version": STUB_VERSION}) + body
    path.write_text(src)
    path.chmod(path.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP
               | stat.S_IXOTH)
    return path


def _ascii_raw(wrapped: bool) -> str:
    """A conforming ngspice ASCII rawfile for the fixture's op point. When
    ``wrapped`` the node-voltage vectors are ``v(node)``; else bare ``node``
    — spice_run must classify by the TYPE column either way."""
    v0 = "v(p12v)" if wrapped else "p12v"
    v1 = "v(p3v3)" if wrapped else "p3v3"
    return (
        "Title: * wyred spice denotation (stub)\n"
        "Date: Thu Jan  1 00:00:00 1970\n"
        "Plotname: Operating Point\n"
        "Flags: real\n"
        "No. Variables: 3\n"
        "No. Points: 1\n"
        "Variables:\n"
        "\t0\t%s\tvoltage\n" % v0 +
        "\t1\t%s\tvoltage\n" % v1 +
        "\t2\tvvin#branch\tcurrent\n"
        "Values:\n"
        "0\t1.200000000000000e+01\n"
        "\t3.300000000000000e+00\n"
        "\t-1.000000000000000e-03\n")


def stub_ok(dirpath: Path, wrapped: bool) -> Path:
    body = (
        "raw = raw_path_from_deck()\n"
        "Path(raw).write_text(%r)\n"
        "sys.exit(0)\n" % _ascii_raw(wrapped))
    return _write_stub(dirpath, "ngspice_ok", body)


def stub_crash(dirpath: Path) -> Path:
    body = (
        'sys.stderr.write("stub: deliberate ngspice crash\\n")\n'
        "sys.exit(1)\n")
    return _write_stub(dirpath, "ngspice_crash", body)


def stub_hang(dirpath: Path) -> Path:
    body = "time.sleep(30)\nsys.exit(0)\n"
    return _write_stub(dirpath, "ngspice_hang", body)


def stub_garbage(dirpath: Path) -> Path:
    body = (
        "raw = raw_path_from_deck()\n"
        'Path(raw).write_text("not a rawfile at all\\n")\n'
        "sys.exit(0)\n")
    return _write_stub(dirpath, "ngspice_garbage", body)


def stub_norawfile(dirpath: Path) -> Path:
    body = 'sys.stdout.write("ran but wrote nothing\\n")\nsys.exit(0)\n'
    return _write_stub(dirpath, "ngspice_norawfile", body)


# ---------------------------------------------------------------------------
# tests
# ---------------------------------------------------------------------------

def test_skip(tmp: Path) -> None:
    """ngspice genuinely absent -> EXIT_SKIP, loud banner, no record."""
    print("[1/6] SKIP: ngspice absent -> loud skip, exit 3, no record")
    out = tmp / "skip_out"
    out.mkdir()
    env = dict(os.environ)
    env.pop("WYRED_NGSPICE", None)
    env["PATH"] = str(tmp / "empty-path")          # nothing named ngspice
    proc = subprocess.run(
        [sys.executable, str(SPICE_RUN), str(FIXTURE), "--out", str(out)],
        capture_output=True, text=True, env=env)
    check(proc.returncode == spice_run.EXIT_SKIP,
          "CLI exits EXIT_SKIP=%d (got %d)"
          % (spice_run.EXIT_SKIP, proc.returncode))
    check(proc.returncode not in (spice_run.EXIT_OK, spice_run.EXIT_FAIL),
          "skip exit is machine-distinguishable from PASS(0)/FAIL(1)")
    check("SKIPPED: ngspice" in proc.stdout,
          "loud skip banner present")
    check("rc_divider.cir" in proc.stdout,
          "banner names the unverified deck")
    check(not (out / "rc_divider.spiceop.json").exists(),
          "no spiceop.json written on skip (nothing was measured)")


def test_ok(tmp: Path, wrapped: bool, tag: str) -> None:
    """Stub ngspice -> parsed op, record written, CLI exit 0."""
    print("[ok/%s] OK: stub op parses to the analytic divider point" % tag)
    bindir = tmp / ("ok_%s_bin" % tag)
    bindir.mkdir()
    stub = stub_ok(bindir, wrapped)

    # in-process run_op: parsing + record content
    res = spice_run.run_op(FIXTURE, ngspice=str(stub))
    check(res.status == spice_run.STATUS_OK,
          "[%s] run_op status ok (got %s: %s)"
          % (tag, res.status, res.error))
    for node, want in EXPECT_NODES.items():
        got = res.node_voltages.get(node)
        check(got is not None and abs(got - want) < 1e-9,
              "[%s] V(%s) = %s (want %s)" % (tag, node, got, want))
    check(abs(res.branch_currents.get("vvin#branch", 0.0)
              - EXPECT_BRANCH) < 1e-12,
          "[%s] source branch current = %s (want %s)"
          % (tag, res.branch_currents.get("vvin#branch"), EXPECT_BRANCH))
    check(res.ngspice_version == STUB_VERSION,
          "[%s] pinned the stub version string" % tag)

    # deck_sha256 is of the ON-DISK deck bytes (artifact of record)
    import hashlib
    want_sha = hashlib.sha256(FIXTURE.read_bytes()).hexdigest()
    check(res.deck_sha256 == want_sha,
          "[%s] deck_sha256 hashes the on-disk deck bytes" % tag)

    # CLI: exit 0 and a written, well-formed record
    out = tmp / ("ok_%s_out" % tag)
    out.mkdir()
    proc = subprocess.run(
        [sys.executable, str(SPICE_RUN), str(FIXTURE),
         "--out", str(out), "--ngspice", str(stub)],
        capture_output=True, text=True)
    check(proc.returncode == spice_run.EXIT_OK,
          "[%s] CLI exits 0 (got %d): %s"
          % (tag, proc.returncode, proc.stderr[-300:]))
    check("[ngspice] %s" % STUB_VERSION in proc.stdout,
          "[%s] version pinned on stdout before the run" % tag)
    rec_path = out / "rc_divider.spiceop.json"
    if check(rec_path.exists(), "[%s] wrote rc_divider.spiceop.json" % tag):
        rec = json.loads(rec_path.read_text())
        check(rec["status"] == "ok" and rec["schema"] == "wyred.spiceop/1"
              and rec["analysis"] == "op",
              "[%s] record status/schema/analysis correct" % tag)
        check(abs(rec["node_voltages"]["p3v3"] - 3.3) < 1e-9,
              "[%s] record carries V(p3v3)=3.3" % tag)
        check(rec["deck_sha256"] == want_sha,
              "[%s] record deck_sha256 matches on-disk deck" % tag)
        # canonical, sorted-keys, trailing newline
        raw = rec_path.read_text()
        check(raw.endswith("\n") and raw == json.dumps(
            rec, indent=2, sort_keys=True) + "\n",
            "[%s] record is canonical (sorted keys, indent 2, newline)" % tag)


def _fail_case(tmp: Path, tag: str, stub_path: Path, want_kind: str,
               timeout: float = spice_run.DEFAULT_TIMEOUT_S) -> None:
    res = spice_run.run_op(FIXTURE, ngspice=str(stub_path), timeout=timeout)
    check(res.status == spice_run.STATUS_FAIL,
          "[%s] status fail (got %s)" % (tag, res.status))
    check(res.status != spice_run.STATUS_SKIP,
          "[%s] a broken run is FAIL, never SKIP" % tag)
    check(res.error_kind == want_kind,
          "[%s] error_kind=%s (got %s)" % (tag, want_kind, res.error_kind))
    # the CLI surfaces it as EXIT_FAIL and writes a failure record
    out = tmp / ("%s_out" % tag)
    out.mkdir()
    proc = subprocess.run(
        [sys.executable, str(SPICE_RUN), str(FIXTURE),
         "--out", str(out), "--ngspice", str(stub_path),
         "--timeout", str(timeout)],
        capture_output=True, text=True)
    check(proc.returncode == spice_run.EXIT_FAIL,
          "[%s] CLI exits EXIT_FAIL=1 (got %d)"
          % (tag, proc.returncode))
    rec_path = out / "rc_divider.spiceop.json"
    if check(rec_path.exists(), "[%s] failure record written" % tag):
        rec = json.loads(rec_path.read_text())
        check(rec["status"] == "fail" and rec["error_kind"] == want_kind,
              "[%s] failure record status/error_kind correct" % tag)


def test_crash(tmp: Path) -> None:
    print("[3/6] CRASH: nonzero exit -> structured FAIL (nonzero_exit)")
    _fail_case(tmp, "crash", stub_crash(_bin(tmp, "crash")), "nonzero_exit")


def test_timeout(tmp: Path) -> None:
    print("[4/6] TIMEOUT: a hung run -> structured FAIL (timeout)")
    t0 = time.monotonic()
    _fail_case(tmp, "timeout", stub_hang(_bin(tmp, "timeout")), "timeout",
               timeout=1.0)
    check(time.monotonic() - t0 < 20.0,
          "timeout fired promptly (did not wait for the 30s hang)")


def test_garbage(tmp: Path) -> None:
    print("[5/6] GARBAGE: non-rawfile output -> structured FAIL (unparseable)")
    _fail_case(tmp, "garbage", stub_garbage(_bin(tmp, "garbage")),
               "unparseable")


def test_norawfile(tmp: Path) -> None:
    print("[6/6] NO-RAWFILE: exit 0 without a rawfile -> FAIL (no_rawfile)")
    _fail_case(tmp, "norawfile", stub_norawfile(_bin(tmp, "norawfile")),
               "no_rawfile")


def _bin(tmp: Path, tag: str) -> Path:
    d = tmp / ("%s_bin" % tag)
    d.mkdir()
    return d


def main() -> int:
    if not FIXTURE.is_file():
        print("setup error: fixture deck not found at %s" % FIXTURE)
        return 2
    t0 = time.monotonic()
    with tempfile.TemporaryDirectory(prefix="wyred-spice-tests-") as td:
        tmp = Path(td)
        test_skip(tmp)
        test_ok(tmp, wrapped=True, tag="wrapped")
        test_ok(tmp, wrapped=False, tag="bare")
        test_crash(tmp)
        test_timeout(tmp)
        test_garbage(tmp)
        test_norawfile(tmp)
    dt = time.monotonic() - t0
    if FAILURES:
        print("\nSPICE RUN TESTS: FAIL — %d check(s) failed (%.1fs):"
              % (len(FAILURES), dt))
        for msg in FAILURES:
            print("  - %s" % msg)
        return 1
    print("\nSPICE RUN TESTS: PASS (%.1fs)" % dt)
    return 0


if __name__ == "__main__":
    sys.exit(main())
