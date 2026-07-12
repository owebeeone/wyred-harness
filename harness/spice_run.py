#!/usr/bin/env python3
"""wyred-harness SPICE runner — ngspice DC operating point, from disk.

WyredPlanSpice step 2.1 (WyredWorkflowDesign §3.4, third denotation). This
is the ASSERTED-vs-REALIZED read-back of a ``<name>.cir`` deck: locate
ngspice, run it in batch mode over an ``.op`` control section, parse the
node voltages out of an ASCII rawfile, and record them in a
``<name>.spiceop.json`` measurement record — *recorded, not trusted*. The
op-oracle (step 2.2) reads that record from disk and compares each declared
rail to its simulated node voltage; this module only measures.

Fences (wyred-harness/CLAUDE.md, RunnerSplit.md, DecisionLog F5):

* ngspice is DELEGATED, never imported and never vendored — reached only as
  a subprocess (``ngspice --version`` / ``ngspice -b``). Its availability is
  a RUNTIME fact.
* **Absence is a LOUD SKIP, never a silent green** (WyredPlanSpice ground
  rule "confession over absence" + the ``run_gate.py _skip_probes_warning``
  pattern): no ngspice -> a banner naming every artifact NOT simulated and
  every rail NOT verified, with a status machine-distinguishable from PASS
  and FAIL (exit ``EXIT_SKIP``). When ngspice IS present its version is
  pinned on stdout before any run (the ``_print_ground_truth_pin`` /
  simulator-pin pattern, WyredSpiceContract §9).
* A hung or crashed or unparseable simulation is a STRUCTURED FAILURE (a red
  verdict on the run, exit ``EXIT_FAIL``), NOT a skip — a corrupted deck
  must never be laundered into a silent pass or a skip.

Deck conventions consumed here are WyredSpiceContract's: name-preserving
sanitized nodes with ground -> ``0`` (§3); the deck is a purely STRUCTURAL
netlist (no analysis directive baked in — the ``.op`` control section is a
simulation/verdict concern and therefore lives HERE in the harness, not in
the engine-emitted artifact). The recorded ``deck_sha256`` is of the deck
bytes AS THEY SIT ON DISK (the artifact of record), not of the augmented
run-deck this module hands to ngspice.

CLI::

    python3 spice_run.py <deck.cir> [--out DIR] [--timeout SECONDS]
                                    [--ngspice PATH]

    exit 0  (EXIT_OK)    ngspice ran, op parsed, record written
    exit 1  (EXIT_FAIL)  ngspice present but the run failed (timeout,
                         nonzero exit, no/garbled rawfile) — structured
    exit 2  (EXIT_USAGE) bad invocation / deck not found
    exit 3  (EXIT_SKIP)  ngspice absent — loud skip, nothing verified

ngspice is located via, in order: an explicit ``--ngspice`` / the
``run_op(ngspice=...)`` argument, the ``WYRED_NGSPICE`` env override, then
``shutil.which("ngspice")``. Pure Python 3.10 stdlib.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional

# ---- run-level status / exit semantics (machine-distinguishable) ----------
STATUS_OK = "ok"
STATUS_SKIP = "skip"
STATUS_FAIL = "fail"

EXIT_OK = 0
EXIT_FAIL = 1
EXIT_USAGE = 2
EXIT_SKIP = 3

DEFAULT_TIMEOUT_S = 60.0
NGSPICE_ENV = "WYRED_NGSPICE"
RECORD_SCHEMA = "wyred.spiceop/1"


# ---------------------------------------------------------------------------
# result record
# ---------------------------------------------------------------------------

@dataclass
class SpiceRunResult:
    """One deck's measurement outcome. ``status`` is the load-bearing field:
    ``ok`` (parsed op), ``skip`` (no ngspice — nothing verified), ``fail``
    (present but the run broke — structured)."""

    status: str
    artifact: str
    deck_path: str
    deck_sha256: Optional[str] = None
    ngspice: Optional[str] = None
    ngspice_version: Optional[str] = None
    node_voltages: Dict[str, float] = field(default_factory=dict)
    branch_currents: Dict[str, float] = field(default_factory=dict)
    error_kind: Optional[str] = None      # fail only: timeout|nonzero_exit|...
    error: Optional[str] = None           # fail only: human-readable detail
    unverified: List[str] = field(default_factory=list)   # skip only

    def to_record(self) -> dict:
        """The canonical ``<name>.spiceop.json`` payload (ok / fail; a skip
        writes no record — the banner + exit code carry it)."""
        rec = {
            "artifact": self.artifact,
            "schema": RECORD_SCHEMA,
            "analysis": "op",
            "status": self.status,
            "deck": Path(self.deck_path).name,
            "deck_sha256": self.deck_sha256,
            "ngspice": self.ngspice,
            "ngspice_version": self.ngspice_version,
            "node_voltages": self.node_voltages,
            "branch_currents": self.branch_currents,
        }
        if self.status == STATUS_FAIL:
            rec["error_kind"] = self.error_kind
            rec["error"] = self.error
        return rec


# ---------------------------------------------------------------------------
# ngspice discovery + version pin (delegation, never import)
# ---------------------------------------------------------------------------

def find_ngspice(ngspice: Optional[str] = None,
                 env: Optional[Dict[str, str]] = None) -> Optional[str]:
    """Resolve the ngspice binary: explicit arg, then ``WYRED_NGSPICE`` env
    override, then ``PATH``. Returns an absolute path, or ``None`` when
    ngspice cannot be found (-> the loud-skip path)."""
    env = os.environ if env is None else env
    for cand in (ngspice, env.get(NGSPICE_ENV)):
        if cand:
            resolved = shutil.which(cand) or (
                cand if os.path.isfile(cand) and os.access(cand, os.X_OK)
                else None)
            if resolved:
                return os.path.abspath(resolved)
            return None            # an explicit request that does not resolve
    found = shutil.which("ngspice")
    return os.path.abspath(found) if found else None


def probe_version(ngspice: str, timeout: float = 15.0) -> str:
    """Pin the simulator: ``ngspice --version`` -> a single version line.

    Best-effort — an odd build that fails ``--version`` still yields a
    non-empty pin so the record never claims an unknown simulator silently."""
    try:
        proc = subprocess.run([ngspice, "--version"],
                              capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return "ngspice (version probe failed: %s)" % exc
    blob = (proc.stdout or "") + "\n" + (proc.stderr or "")
    lines = [ln.strip() for ln in blob.splitlines() if ln.strip()]
    for ln in lines:                       # prefer the line naming ngspice
        if "ngspice" in ln.lower():
            return ln
    return lines[0] if lines else "ngspice (no version output)"


# ---------------------------------------------------------------------------
# deck handling — hash the on-disk bytes, augment a COPY with the .op section
# ---------------------------------------------------------------------------

def deck_sha256(deck_bytes: bytes) -> str:
    return hashlib.sha256(deck_bytes).hexdigest()


def build_run_deck(deck_text: str, raw_path: Path) -> str:
    """Return an augmented run-deck: the STRUCTURAL deck plus an ``.op``
    control section that runs the operating point and writes an ASCII
    rawfile to ``raw_path``. The engine-emitted deck carries no analysis
    directive (fence: emission is structural, simulation is the harness's);
    this is where the ``.op`` is supplied, on a copy ngspice never persists.

    The trailing top-level ``.end`` (if any) is removed and re-appended after
    the control block, since ngspice wants ``.control`` blocks before
    ``.end``."""
    lines = deck_text.splitlines()
    while lines and lines[-1].strip().lower() == ".end":
        lines.pop()
    control = [
        ".control",
        "op",
        "set filetype=ascii",
        "write %s" % raw_path,
        ".endc",
        ".end",
    ]
    return "\n".join(lines + control) + "\n"


# ---------------------------------------------------------------------------
# ASCII rawfile parser (ngspice `write` with filetype=ascii)
# ---------------------------------------------------------------------------

class RawParseError(Exception):
    """The rawfile is missing sections or is not ASCII / not real-valued."""


def parse_ascii_rawfile(text: str):
    """Parse an ngspice ASCII rawfile into (node_voltages, branch_currents).

    The ASCII raw format is stable across ngspice versions: a header
    (``No. Variables``, ``No. Points``, ``Flags``), a ``Variables:`` block of
    ``<index> <name> <type>`` rows, then a ``Values:`` block. Variables are
    classified by their TYPE column, robust to whether node voltages are
    written bare (``p3v3``) or wrapped (``v(p3v3)``): ``voltage`` -> a node
    voltage (any ``v(...)`` wrapper stripped), ``current`` -> a branch
    current. Only the first analysis point (the op point) is read.

    Raises RawParseError on anything unexpected — the caller turns that into
    a STRUCTURED failure, never a skip."""
    lines = text.splitlines()
    n_vars = n_points = None
    flags = ""
    i = 0
    while i < len(lines):
        line = lines[i].strip()
        low = line.lower()
        if low.startswith("no. variables:"):
            n_vars = int(line.split(":", 1)[1].strip())
        elif low.startswith("no. points:"):
            n_points = int(line.split(":", 1)[1].strip())
        elif low.startswith("flags:"):
            flags = line.split(":", 1)[1].strip().lower()
        elif low == "variables:":
            break
        i += 1

    if n_vars is None or n_points is None:
        raise RawParseError(
            "missing 'No. Variables'/'No. Points' header (not an ASCII "
            "rawfile?)")
    if "complex" in flags:
        raise RawParseError(
            "complex-valued rawfile (flags=%r); the DC operating point is "
            "expected to be real" % flags)
    if i >= len(lines) or lines[i].strip().lower() != "variables:":
        raise RawParseError("no 'Variables:' section")

    # --- Variables: <n_vars> rows of "<index> <name> <type>" ---------------
    var_names: List[str] = []
    var_types: List[str] = []
    i += 1
    while i < len(lines) and len(var_names) < n_vars:
        row = lines[i].strip()
        i += 1
        if not row:
            continue
        if row.lower() == "values:":
            break
        parts = row.split()
        if len(parts) < 3:
            raise RawParseError("malformed variable row: %r" % row)
        var_names.append(parts[1])
        var_types.append(parts[2].lower())
    if len(var_names) != n_vars:
        raise RawParseError(
            "declared %d variables, found %d" % (n_vars, len(var_names)))

    # --- Values: a flat token stream, N+1 tokens per point (index + N vals).
    # Reading tokens (not lines) is robust to ngspice's line wrapping.
    while i < len(lines) and lines[i].strip().lower() != "values:":
        i += 1
    if i >= len(lines):
        raise RawParseError("no 'Values:' section")
    tokens: List[str] = []
    for row in lines[i + 1:]:
        tokens.extend(row.split())
    need = n_points * (n_vars + 1)
    if len(tokens) < need:
        raise RawParseError(
            "truncated Values block: expected >= %d tokens, got %d"
            % (need, len(tokens)))

    # point 0: skip its leading index token, take the next n_vars values
    point0 = tokens[1:1 + n_vars]
    node_voltages: Dict[str, float] = {}
    branch_currents: Dict[str, float] = {}
    for name, typ, raw_val in zip(var_names, var_types, point0):
        try:
            val = float(raw_val)
        except ValueError as exc:
            raise RawParseError(
                "non-float value %r for %r" % (raw_val, name)) from exc
        if typ == "voltage":
            node_voltages[_strip_v_wrapper(name)] = val
        elif typ == "current":
            branch_currents[name] = val
        # other vector types (time, frequency, ...) are irrelevant to an op
    return node_voltages, branch_currents


def _strip_v_wrapper(name: str) -> str:
    """``v(p3v3)`` / ``V(P3V3)`` -> ``p3v3``; bare names pass through."""
    low = name.lower()
    if low.startswith("v(") and name.endswith(")"):
        return name[2:-1]
    return name


# ---------------------------------------------------------------------------
# the run
# ---------------------------------------------------------------------------

def run_op(deck_path, *, ngspice: Optional[str] = None,
           timeout: float = DEFAULT_TIMEOUT_S,
           env: Optional[Dict[str, str]] = None,
           work_dir: Optional[Path] = None) -> SpiceRunResult:
    """Run the DC operating point of ``deck_path`` and read back node
    voltages. Returns a SpiceRunResult whose ``status`` is one of
    ``ok`` / ``skip`` (no ngspice) / ``fail`` (structured). Never raises for
    an absent or misbehaving simulator — those are encoded in the result."""
    deck_path = Path(deck_path)
    artifact = deck_path.name
    if artifact.endswith(".cir"):
        artifact = artifact[:-len(".cir")]

    binary = find_ngspice(ngspice, env)
    if binary is None:
        return SpiceRunResult(
            status=STATUS_SKIP, artifact=artifact, deck_path=str(deck_path),
            unverified=[
                "ngspice not found (PATH / %s / --ngspice); the DC "
                "operating point of %s was NOT computed and its declared "
                "rails were NOT verified" % (NGSPICE_ENV, deck_path.name)])

    try:
        deck_bytes = deck_path.read_bytes()
    except OSError as exc:
        return SpiceRunResult(
            status=STATUS_FAIL, artifact=artifact, deck_path=str(deck_path),
            ngspice=binary, error_kind="deck_unreadable", error=str(exc))
    sha = deck_sha256(deck_bytes)
    version = probe_version(binary)

    tmpctx = (tempfile.TemporaryDirectory(prefix="wyred-spice-")
              if work_dir is None else None)
    run_root = Path(tmpctx.name) if tmpctx is not None else Path(work_dir)
    try:
        run_root.mkdir(parents=True, exist_ok=True)
        raw_path = run_root / ("%s.raw" % artifact)
        run_deck = run_root / ("%s.run.cir" % artifact)
        run_deck.write_text(
            build_run_deck(deck_bytes.decode("utf-8", "replace"), raw_path))

        try:
            proc = subprocess.run(
                [binary, "-b", str(run_deck)],
                capture_output=True, text=True, timeout=timeout,
                cwd=str(run_root))
        except subprocess.TimeoutExpired:
            return SpiceRunResult(
                status=STATUS_FAIL, artifact=artifact,
                deck_path=str(deck_path), deck_sha256=sha, ngspice=binary,
                ngspice_version=version, error_kind="timeout",
                error="ngspice did not finish within %.1fs" % timeout)
        except OSError as exc:
            return SpiceRunResult(
                status=STATUS_FAIL, artifact=artifact,
                deck_path=str(deck_path), deck_sha256=sha, ngspice=binary,
                ngspice_version=version, error_kind="spawn_failed",
                error=str(exc))

        if proc.returncode != 0:
            tail = _tail(proc.stderr or proc.stdout, 12)
            return SpiceRunResult(
                status=STATUS_FAIL, artifact=artifact,
                deck_path=str(deck_path), deck_sha256=sha, ngspice=binary,
                ngspice_version=version, error_kind="nonzero_exit",
                error="ngspice exited %d: %s" % (proc.returncode, tail))

        if not raw_path.is_file():
            tail = _tail(proc.stderr or proc.stdout, 12)
            return SpiceRunResult(
                status=STATUS_FAIL, artifact=artifact,
                deck_path=str(deck_path), deck_sha256=sha, ngspice=binary,
                ngspice_version=version, error_kind="no_rawfile",
                error="ngspice exited 0 but wrote no rawfile at %s; "
                      "output tail: %s" % (raw_path.name, tail))

        try:
            nodes, branches = parse_ascii_rawfile(
                raw_path.read_text(errors="replace"))
        except (RawParseError, ValueError) as exc:
            return SpiceRunResult(
                status=STATUS_FAIL, artifact=artifact,
                deck_path=str(deck_path), deck_sha256=sha, ngspice=binary,
                ngspice_version=version, error_kind="unparseable",
                error="could not parse ngspice rawfile: %s" % exc)

        if not nodes:
            return SpiceRunResult(
                status=STATUS_FAIL, artifact=artifact,
                deck_path=str(deck_path), deck_sha256=sha, ngspice=binary,
                ngspice_version=version, error_kind="no_nodes",
                error="ngspice op produced no node voltages")

        return SpiceRunResult(
            status=STATUS_OK, artifact=artifact, deck_path=str(deck_path),
            deck_sha256=sha, ngspice=binary, ngspice_version=version,
            node_voltages=nodes, branch_currents=branches)
    finally:
        if tmpctx is not None:
            tmpctx.cleanup()


def _tail(text: Optional[str], n: int) -> str:
    if not text:
        return "(no output)"
    return " | ".join(text.strip().splitlines()[-n:])


# ---------------------------------------------------------------------------
# record writing + the loud skip banner
# ---------------------------------------------------------------------------

def write_record(result: SpiceRunResult, out_dir: Path) -> Optional[Path]:
    """Write ``<artifact>.spiceop.json`` for an ``ok`` or ``fail`` result
    (canonical, sorted keys). A ``skip`` writes nothing — nothing was
    measured; the banner + exit code carry the skip. Returns the path
    written, or None."""
    if result.status == STATUS_SKIP:
        return None
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / ("%s.spiceop.json" % result.artifact)
    path.write_text(
        json.dumps(result.to_record(), indent=2, sort_keys=True) + "\n")
    return path


def print_skip_banner(results: List[SpiceRunResult], stream=sys.stdout) -> None:
    """The loud SKIP banner (the run_gate ``_skip_probes_warning`` shape):
    names every deck NOT simulated and every check NOT verified, so an absent
    ngspice can never read as a silent green."""
    bang = "!" * 72
    print(bang, file=stream)
    print("!!! SKIPPED: ngspice DC operating-point runs", file=stream)
    print("!!! ngspice was NOT found (PATH / %s / --ngspice)." % NGSPICE_ENV,
          file=stream)
    print("!!! NOT VERIFIED this run:", file=stream)
    for r in results:
        print("!!!   - %s" % r.deck_path, file=stream)
        for note in r.unverified:
            print("!!!       %s" % note, file=stream)
    print("!!! The decks are structurally present but their DC operating", file=stream)
    print("!!! points were NOT computed and their declared rails were NOT", file=stream)
    print("!!! checked against simulation. Install ngspice (brew install", file=stream)
    print("!!! ngspice) or set %s to a binary to verify." % NGSPICE_ENV,
          file=stream)
    print(bang, file=stream, flush=True)


def print_version_pin(binary: str, version: str, stream=sys.stdout) -> None:
    """Pin the simulator on stdout BEFORE any run (WyredSpiceContract §9)."""
    print("[ngspice] %s" % version, file=stream)
    print("[ngspice] binary: %s" % binary, file=stream, flush=True)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        prog="spice_run", description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("deck", help="the <name>.cir deck to simulate")
    ap.add_argument("--out", metavar="DIR", default=None,
                    help="directory to write <name>.spiceop.json into "
                         "(default: alongside the deck)")
    ap.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT_S,
                    metavar="SECONDS",
                    help="ngspice wall-clock budget (default: %(default)s)")
    ap.add_argument("--ngspice", default=None, metavar="PATH",
                    help="explicit ngspice binary (else %s / PATH)"
                         % NGSPICE_ENV)
    args = ap.parse_args(argv)

    deck_path = Path(args.deck)
    if not deck_path.is_file():
        print("spice_run: deck not found: %s" % deck_path, file=sys.stderr)
        return EXIT_USAGE
    out_dir = Path(args.out) if args.out else deck_path.parent

    # Pin the simulator (or announce its absence) BEFORE any run.
    binary = find_ngspice(args.ngspice)
    if binary is not None:
        print_version_pin(binary, probe_version(binary))

    result = run_op(deck_path, ngspice=args.ngspice, timeout=args.timeout)

    if result.status == STATUS_SKIP:
        print_skip_banner([result])
        return EXIT_SKIP

    path = write_record(result, out_dir)
    if result.status == STATUS_OK:
        print("[spice] %s: op OK — %d node(s): %s"
              % (result.artifact, len(result.node_voltages),
                 ", ".join("%s=%.6g" % (n, v) for n, v
                           in sorted(result.node_voltages.items()))))
        print("[spice] wrote %s" % path)
        return EXIT_OK

    # STATUS_FAIL — structured, red, never a skip
    print("[spice] %s: FAILED (%s): %s"
          % (result.artifact, result.error_kind, result.error),
          file=sys.stderr)
    if path is not None:
        print("[spice] wrote failure record %s" % path, file=sys.stderr)
    return EXIT_FAIL


if __name__ == "__main__":
    sys.exit(main())
