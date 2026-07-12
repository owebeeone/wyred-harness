#!/usr/bin/env python3
"""wyred-harness gate — the VERDICT half of ga019's runner, from disk.

``python3 gate.py --tree <dir> --corpus-dir <dir> [--engine-src <dir>]``
                              gate an emitted artifact tree (a
                              ``wyred.emit --out`` directory). Every
                              verdict is computed from artifacts RE-READ
                              FROM DISK — never from an engine's
                              in-memory opinion of itself:

                              1. determinism — the corpus is emitted
                                 TWICE via subprocess
                                 (``python3 -m wyred.emit``) into two
                                 temp dirs and the trees byte-compared
                                 (the from-disk analog of the runner's
                                 emit-twice / resolve-twice checks);
                              2. layer 1 — the PUBLIC intent oracle runs
                                 over each <name>.l1.json and its codes
                                 must equal the intent's OWN declared
                                 ``expected_l1`` (self-check against
                                 intent, not self-consistency — law 10);
                                 a declared escalation must be present
                                 WITH its conflict + relaxation
                                 explanation;
                              3. layer 2 — for every declared-clean,
                                 oracle-clean artifact, the v3 stack
                                 (spec_satisfaction / erc / invariant
                                 incl. declared-vs-lowered counts /
                                 check_allocations with an independent
                                 ``record2`` and the retained EXTERNAL
                                 <name>.baseline.json — LOCK_UNVERIFIED
                                 when locked groups have no baseline)
                                 must agree with the declared verdict
                                 (ESCALATE for rung-4 intents, PASS
                                 otherwise); a failing-at-L1 document
                                 still gets its embedded allocation
                                 record checked standalone;
                              4. the cross-path differential re-runs
                                 FROM DISK via the engine's own
                                 ``python3 -m wyred.crosscheck``
                                 subprocess (netlist <-> BOM <-> pin-map
                                 <-> records <-> l1 must describe one
                                 model; connector locks re-verified
                                 against the external baselines), and
                                 the XPATH counter-probe battery doctors
                                 COPIES of each clean artifact set under
                                 a temp dir — NEVER the input tree — and
                                 requires every probe's code to fire;
                              5. the lobotomy verdict: every battery
                                 probe must have fired at least once
                                 this run or the run FAILS — "all codes
                                 negatively probed" is a per-run
                                 mechanical fact, not a claim;
                              6. the SPICE DC operating-point oracle
                                 (WyredPlanSpice 2.2): for every artifact
                                 carrying a ``.cir`` deck, ngspice (reached
                                 only as a subprocess, via the harness
                                 ``spice_run`` runner) computes the DC
                                 operating point and each declared power
                                 rail must sit within its declared
                                 tolerance (±2 % default, ``sim_tol_pct``
                                 override) of the simulated node voltage
                                 (``SPICE_RAIL_OFF`` / ``SPICE_RAIL_UNRESOLVED``).
                                 ngspice absent -> a LOUD SKIP naming every
                                 unverified rail (never green-by-omission,
                                 never a failure); ``.cir`` absent -> stage
                                 not applicable (Phase-1 gating made deck
                                 absence honest).

Exit 0 iff every check passed AND the battery fully fired.

Boundary fences (wyred-harness/CLAUDE.md + dev-docs/RunnerSplit.md): this
gate NEVER imports wyred (the engine). Composition is at the PROCESS
level: the engine is only ever invoked as a subprocess
(``python3 -m wyred.emit``, ``python3 -m wyred.crosscheck``) over shared
artifact directories.

Recorded porting choices (vs /Users/owebeeone/limbo/elecscad/rl/ga019/
runner.py, the reference):

* expectations — ``wyred.emit`` writes NO expectations artifact, so the
  intents' own ``expected_l1`` / ``expect_escalation`` declarations are
  obtained by importing the corpus READ-ONLY (reading DATA) in a
  SUBPROCESS under the engine's interpreter path; the gate process
  itself never imports the engine or the corpus.
* record2 — the runner's second in-process resolve (rr2) becomes the
  allocation record of the second determinism re-emit: "a SECOND record
  from an independent re-solve of the same (document, solver_version)",
  exactly what check_allocations documents record2 to be.
* the layer-1 oracle runs on the ON-DISK l1.json (for L2-bearing
  artifacts that is the freeze-updated document the resolver wrote —
  the artifact of record), where the runner ran it on the pre-resolve
  in-memory doc; for declared-failing documents the two are the same
  bytes.
* engine Diagnostics (res.diagnostics) are emit-time output and are not
  re-printed here; determinism of the emit is checked by byte-compare
  instead of in-process double elaboration.
"""

from __future__ import annotations

import argparse
import copy
import itertools
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent

import allocation as alloc_oracle           # noqa: E402  (flat siblings)
import erc as erc_oracle                    # noqa: E402
import intent_oracle                        # noqa: E402
import invariant as invariant_oracle        # noqa: E402
import schema                               # noqa: E402
import schema_l1                            # noqa: E402
import spec_satisfaction                    # noqa: E402
import spice_oracle                         # noqa: E402  (SPICE op-oracle, 2.2)
import spice_run                            # noqa: E402  (ngspice by subprocess)

DEFAULT_ENGINE_SRC = HERE.parents[1] / "wyred" / "src"

_PROBE_SEQ = itertools.count(1)


# ---------------------------------------------------------------------------
# subprocess plumbing — the ONLY way this gate touches the engine
# ---------------------------------------------------------------------------

def _engine_env(engine_src: Path, corpus_dir: Path) -> dict:
    """The engine subprocess environment: the engine's src and the corpus
    package's parent on PYTHONPATH (corpus modules ``from wyred import``
    and cross-import ``from <corpus>.lib_... import``)."""
    env = dict(os.environ)
    parts = [str(engine_src), str(Path(corpus_dir).resolve().parent)]
    if env.get("PYTHONPATH"):
        parts.append(env["PYTHONPATH"])
    env["PYTHONPATH"] = os.pathsep.join(parts)
    return env


def _emit_subprocess(engine_src: Path, corpus_dir: Path, out_dir: Path):
    return subprocess.run(
        [sys.executable, "-m", "wyred.emit",
         "--corpus-dir", str(corpus_dir), "--out", str(out_dir)],
        env=_engine_env(engine_src, corpus_dir),
        capture_output=True, text=True)


def _crosscheck_subprocess(engine_src: Path, corpus_dir: Path,
                           art_dir: Path, name: str):
    """Run the engine's own from-disk differential over one artifact set;
    -> (returncode, [{"code", "msg"}...] parsed from stdout)."""
    proc = subprocess.run(
        [sys.executable, "-m", "wyred.crosscheck",
         "--dir", str(art_dir), "--artifact", name],
        env=_engine_env(engine_src, corpus_dir),
        capture_output=True, text=True)
    fails = []
    prefix = name + " "
    for line in proc.stdout.splitlines():
        if line.startswith(prefix):
            code, _, msg = line[len(prefix):].partition(": ")
            fails.append({"code": code, "msg": msg})
    return proc.returncode, fails


# The corpus declarations, read as DATA. This snippet runs as a SUBPROCESS
# under the engine's interpreter path (the gate process never imports the
# engine or the corpus); it is runner.py's discover() + _expectations(),
# emitted as JSON: {"order": [canonical job order], "expect":
# {name: {"expected_l1": [...], "expect_escalation": bool}}}.
_EXPECTATIONS_SRC = r'''
import importlib, json, sys
from pathlib import Path
engine_src, corpus = sys.argv[1], sys.argv[2]
sys.path.insert(0, engine_src)
corpus = Path(corpus).resolve()
sys.path.insert(0, str(corpus.parent))
for py in sorted(corpus.glob("*.py")):
    if py.stem != "__init__":
        importlib.import_module("%s.%s" % (corpus.name, py.stem))
import wyred
order, expect = [], {}
for name, cls in sorted(wyred.INTENTS.items()):
    order.append(name)
    expect[name] = {"expected_l1": sorted(cls.expected_l1),
                    "expect_escalation": bool(cls.expect_escalation)}
for name, ref in sorted(wyred.REFINEMENTS.items()):
    base = wyred.INTENTS[ref.of]
    exp = (ref.expected_l1 if ref.expected_l1 is not None
           else base.expected_l1)
    esc = (ref.expect_escalation if ref.expect_escalation is not None
           else base.expect_escalation)
    order.append(name)
    expect[name] = {"expected_l1": sorted(exp),
                    "expect_escalation": bool(esc)}
json.dump({"order": order, "expect": expect}, sys.stdout)
'''


def _load_expectations(engine_src: Path, corpus_dir: Path) -> dict:
    proc = subprocess.run(
        [sys.executable, "-c", _EXPECTATIONS_SRC,
         str(engine_src), str(corpus_dir)],
        env=_engine_env(engine_src, corpus_dir),
        capture_output=True, text=True)
    if proc.returncode != 0:
        raise RuntimeError(
            "expectations subprocess exited %d: %s"
            % (proc.returncode, proc.stderr.strip().splitlines()[-1:]))
    return json.loads(proc.stdout)


def _byte_compare(dir_a: Path, dir_b: Path):
    """-> (sorted list of relative paths that are missing on one side or
    differ in bytes, total file count in dir_a)."""
    fa = {p.relative_to(dir_a).as_posix(): p
          for p in Path(dir_a).rglob("*") if p.is_file()}
    fb = {p.relative_to(dir_b).as_posix(): p
          for p in Path(dir_b).rglob("*") if p.is_file()}
    diffs = set(fa) ^ set(fb)
    for rel in set(fa) & set(fb):
        if fa[rel].read_bytes() != fb[rel].read_bytes():
            diffs.add(rel)
    return sorted(diffs), len(fa)


def _read_json(art_dir: Path, name: str, kind: str):
    return json.loads(
        (art_dir / ("%s.%s.json" % (name, kind))).read_text())


def _crosscheck_doctored(engine_src, corpus_dir, probe_root: Path,
                         name: str, g, b, p, r, l):
    """Write ONE doctored quintuple as a fresh artifact set under the
    probe temp dir (never the input tree) and re-run the engine's
    from-disk differential over it; -> the set of codes that fired."""
    d = probe_root / ("probe_%03d" % next(_PROBE_SEQ))
    d.mkdir(parents=True)
    for kind, obj in (("l2", g), ("bom", b), ("pinmap", p),
                      ("records", r), ("l1", l)):
        (d / ("%s.%s.json" % (name, kind))).write_text(json.dumps(obj))
    _rc, fails = _crosscheck_subprocess(engine_src, corpus_dir, d, name)
    return {f["code"] for f in fails}


def _crosscheck_doctored_cir(engine_src, corpus_dir, probe_root: Path,
                             name: str, clean_set: dict, deck_text: str,
                             sidecar: dict):
    """Write a CLEAN artifact quintuple plus a DOCTORED ``.cir`` deck + its
    ``.cir.json`` sidecar as a fresh set under the probe temp dir (never the
    input tree) and re-run the engine's from-disk differential over it; ->
    the set of codes that fired. The non-spice paths stay clean, so only the
    ``XCIR_*`` codes can fire — the same isolation the XPATH battery relies
    on. (WyredPlanSpice 1.3.)"""
    d = probe_root / ("cir_probe_%03d" % next(_PROBE_SEQ))
    d.mkdir(parents=True)
    for kind, obj in clean_set.items():
        (d / ("%s.%s.json" % (name, kind))).write_text(json.dumps(obj))
    (d / ("%s.cir" % name)).write_text(deck_text)
    (d / ("%s.cir.json" % name)).write_text(json.dumps(sidecar))
    _rc, fails = _crosscheck_subprocess(engine_src, corpus_dir, d, name)
    return {f["code"] for f in fails}


# ---------------------------------------------------------------------------
# the SPICE deck counter-probe battery (WyredPlanSpice step 1.3): the XCIR_*
# analogue of the XPATH battery. Each probe doctors ONE aspect of a clean
# ``.cir`` deck / ``.cir.json`` sidecar and names the code the SPICE structural
# oracle MUST fire; they JOIN the same lobotomy verdict (every probe fires
# every run, or the run fails — applicability conditioned on corpus content is
# exactly the hole the verdict closes, so a spice-modelled artifact must always
# be present). A mutator returns True when it applied, False to skip. It edits
# the deck LINE LIST and/or the sidecar dict in place.
# ---------------------------------------------------------------------------

def _xcir_probe_battery():
    """The committed NEGATIVE-probe battery for the ``.cir`` structural oracle
    (WyredSpiceContract §10). Four probes, one per code — a dropped element, a
    rewired node, a rewritten value, a forged confession — each isolated so it
    fires its code and no other (verified in wyred-harness/tests)."""

    def _element_card_indices(deck):
        """Indices of the deck lines that are element cards (not comments, dot
        cards, continuations, or inside a ``.subckt`` definition body)."""
        idxs = []
        in_subckt = False
        for i, ln in enumerate(deck):
            s = ln.strip()
            if not s or s.startswith("*") or s.startswith("+"):
                continue
            if s.startswith("."):
                low = s.split()[0].lower()
                if low == ".subckt":
                    in_subckt = True
                elif low == ".ends":
                    in_subckt = False
                continue
            if in_subckt:
                continue
            idxs.append(i)
        return idxs

    def cir_element_dropped(deck, sidecar, l2):
        """Drop one element card -> its refdes vanishes from the deck while the
        L2 still models it and the confession does not name it -> the deck's
        refdes set is short one unconfessed part (XCIR_COMPONENTS)."""
        idxs = _element_card_indices(deck)
        if not idxs:
            return False
        del deck[idxs[0]]
        return True

    def cir_node_rewired(deck, sidecar, l2):
        """Rewire ONE node of one element to ground (``0``) -> that terminal
        leaves its L2 net and joins the ground class -> the deck node partition
        no longer matches the L2 net partition (XCIR_NET_PARTITION)."""
        for i in _element_card_indices(deck):
            toks = deck[i].split()
            for j in (1, 2):                     # the two node slots (v0 2-term)
                if j < len(toks) and toks[j] != "0":
                    toks[j] = "0"
                    deck[i] = " ".join(toks)
                    return True
        return False

    def cir_value_rewritten(deck, sidecar, l2):
        """Rewrite the value token of one primitive element -> the deck value
        no longer matches the L2 model's canonicalized value for that
        component's kind (XCIR_ELEMENT)."""
        for i in _element_card_indices(deck):
            toks = deck[i].split()
            if toks and toks[0][:1].upper() in "RCLDVI" and len(toks) >= 4:
                toks[-1] = "TAMPERV"
                deck[i] = " ".join(toks)
                return True
        return False

    def cir_confession_forged(deck, sidecar, l2):
        """Forge a not_simulated entry for a part that is NOT in the L2 -> the
        confession claims a component was unmodelled that the L2 never had
        (a forged/stale confession, XCIR_CONFESSION). A refdes absent from the
        L2 keeps XCIR_COMPONENTS silent (expected deck set unchanged), so the
        probe isolates the confession code."""
        sidecar.setdefault("not_simulated", []).append(
            {"refdes": "PHANTOM_NS", "kind": "asic", "reason": "no_model"})
        return True

    return [
        ("cir_element_dropped",   "XCIR_COMPONENTS",    cir_element_dropped),
        ("cir_node_rewired",      "XCIR_NET_PARTITION", cir_node_rewired),
        ("cir_value_rewritten",   "XCIR_ELEMENT",       cir_value_rewritten),
        ("cir_confession_forged", "XCIR_CONFESSION",    cir_confession_forged),
    ]


# ---------------------------------------------------------------------------
# the XPATH counter-probe battery (runner.py:198-446, ported verbatim)
# ---------------------------------------------------------------------------

def _xpath_probe_battery():
    """The committed NEGATIVE-probe battery for the cross-path differential
    (M3 review P3, extended by the M4 review P3): each probe doctors ONE
    field of a clean (graph, bom, pinmap, records, l1) quintuple in place
    and names the ``XPATH_*`` code that the cross-path differential MUST
    fire on the doctored artifacts. A mutator returns True when its
    precondition held and the doctoring was applied (e.g. "an allocation
    row with a landed node exists"), False to skip on this artifact. The
    gate runs every not-yet-fired probe against every clean quintuple and
    FAILS the run if (a) an applied probe does not produce its code, or
    (b) any probe never found an applicable artifact — so "all codes
    negatively probed" is a per-run mechanical fact, not a claim."""

    def bom_qty(g, b, p, r, l):
        items = b.get("line_items", [])
        if not items:
            return False
        items[0]["qty"] = items[0].get("qty", 0) + 1
        return True

    def bom_refdes_drop(g, b, p, r, l):
        for item in b.get("line_items", []):
            if item.get("refdes"):
                item["refdes"] = item["refdes"][:-1]
                item["qty"] = item.get("qty", 1) - 1    # keep qty==len:
                return True                             # isolate COMPONENTS
        return False

    def bom_authored_flip(g, b, p, r, l):
        for item in b.get("line_items", []):
            if item.get("refdes"):
                item["authored"] = not item.get("authored")
                return True
        return False

    def bom_value_rewrite(g, b, p, r, l):
        for item in b.get("line_items", []):
            if item.get("refdes"):
                item["value"] = "TAMPERED"
                return True
        return False

    def bom_kind_rewrite(g, b, p, r, l):
        for item in b.get("line_items", []):
            if item.get("refdes"):
                item["kind"] = "TAMPERED_KIND"
                return True
        return False

    def bom_refdes_swap(g, b, p, r, l):
        """Move one refdes onto ANOTHER line (the critic's C11 probe:
        the part-to-buy payload changes while sets/counts stay legal)."""
        items = [i for i in b.get("line_items", []) if i.get("refdes")]
        if len(items) < 2:
            return False
        src, dst = items[0], items[1]
        moved = src["refdes"].pop()
        src["qty"] = src.get("qty", 1) - 1
        dst["refdes"].append(moved)
        dst["qty"] = dst.get("qty", 0) + 1
        (src.get("derived") or {}).pop(moved, None)
        return True

    def bom_derived_tamper(g, b, p, r, l):
        for item in b.get("line_items", []):
            der = item.get("derived")
            if der:
                der[sorted(der)[0]] = "TAMPERED_DERIVATION"
                return True
        return False

    def pinmap_comp_drop(g, b, p, r, l):
        if not p.get("components"):
            return False
        p["components"].pop()
        return True

    def pinmap_net_rewrite(g, b, p, r, l):
        for c in p.get("components", []):
            for t in c.get("terminals", []):
                if t.get("net") is not None:
                    t["net"] = "TAMPERED_NET"
                    return True
        return False

    def pinmap_role_rewrite(g, b, p, r, l):
        for c in p.get("components", []):
            for t in c.get("terminals", []):
                t["role"] = "TAMPERED_ROLE"
                return True
        return False

    def pinmap_term_drop_unwired(g, b, p, r, l):
        """Silently drop an UNWIRED terminal (the critic's J1.3 probe:
        hiding spare capacity must be caught, per the visible-spares
        tenet)."""
        for c in p.get("components", []):
            for i, t in enumerate(c.get("terminals", [])):
                if t.get("net") is None:
                    c["terminals"].pop(i)
                    return True
        return False

    def pinmap_term_phantom(g, b, p, r, l):
        for c in p.get("components", []):
            c.setdefault("terminals", []).append(
                {"name": "PHANTOM_T", "role": "", "function": "",
                 "net": None})
            return True
        return False

    def pinmap_value_rewrite(g, b, p, r, l):
        for c in p.get("components", []):
            c["value"] = "TAMPERED"
            return True
        return False

    def alloc_row_drop(g, b, p, r, l):
        if not p.get("allocations"):
            return False
        p["allocations"].pop(0)
        return True

    def alloc_net_rewrite(g, b, p, r, l):
        for row in p.get("allocations", []):
            if row.get("node") is not None:
                row["net"] = "TAMPERED_ALLOC_NET"
                return True
        return False

    def stamp_tamper(g, b, p, r, l):
        b["stamp"] = dict(b.get("stamp") or {})
        b["stamp"]["series"] = str(b["stamp"].get("series", "")) + "Z"
        return True

    def connector_row_rewrite(g, b, p, r, l):
        for row in r.get("connector_pinout", []):
            if row.get("net") is not None:
                row["net"] = "TAMPERED_CONN_NET"
                return True
        return False

    def connector_row_drop(g, b, p, r, l):
        rows = r.get("connector_pinout", [])
        if not rows:
            return False
        rows.pop(0)
        return True

    # ---- M4 review P3 closure: the LOCK-STATUS / provenance payload ------
    def alloc_state_forged(g, b, p, r, l):
        """Rewrite one pin-map allocation row's state (the firmware-facing
        consumer's lock story) while records keep the truth."""
        for row in p.get("allocations", []):
            row["state"] = "TAMPERED_STATE"
            return True
        return False

    def alloc_lock_stripped(g, b, p, r, l):
        """Strip locked_by from a LOCKED pin-map allocation row — the
        firmware-facing artifact claims the decision is free to drift."""
        for row in p.get("allocations", []):
            if row.get("locked_by"):
                row["locked_by"] = None
                return True
        return False

    def alloc_chosen_by_forged(g, b, p, r, l):
        """Forge chosen_by on the RECORDS side (provenance laundering:
        an authored pin passed off as the solver's choice)."""
        for e in r.get("allocation", {}).get("entries", []):
            e["chosen_by"] = "TAMPERED_CHOOSER"
            return True
        return False

    def connector_lock_stripped(g, b, p, r, l):
        """Strip locked_by from a FROZEN connector-pinout row while the
        record still says the covering group fired."""
        for row in r.get("connector_pinout", []) or []:
            if row.get("locked_by"):
                row["locked_by"] = None
                return True
        return False

    def connector_lock_forged(g, b, p, r, l):
        """Forge a lock tag onto a connector row of an artifact whose
        record has NO fired connector_pinout group."""
        groups = r.get("allocation", {}).get("lock_groups", []) or []
        fired = any("connector_pinout" in (grp.get("covers") or [])
                    and int(grp.get("version", 0)) >= 1 for grp in groups)
        rows = r.get("connector_pinout", []) or []
        if fired or not rows:
            return False
        rows[0]["locked_by"] = "FORGED-GROUP@1"
        return True

    def connector_function_rewritten(g, b, p, r, l):
        """Rewrite a connector row's function label (the ICD's meaning
        payload) away from the netlist terminal's."""
        for row in r.get("connector_pinout", []) or []:
            row["function"] = "TAMPERED_FUNCTION"
            return True
        return False

    def records_fork_forged(g, b, p, r, l):
        """Forge a forked_from lineage record into the records path (the
        shape that would legalize a locked edit to a from-disk consumer)."""
        r["forked_from"] = {"series": "FORGED_PARENT",
                            "reason": "records-side lineage forgery"}
        return True

    def stamp_locks_desynced(g, b, p, r, l):
        """Bump one lock-group version in the records while every path's
        stamp still carries the old summary."""
        groups = r.get("allocation", {}).get("lock_groups", []) or []
        if not groups:
            return False
        groups[0]["version"] = int(groups[0].get("version", 0)) + 1
        return True

    return [
        ("bom_qty_wrong",          "XPATH_BOM_COUNT",         bom_qty),
        ("bom_refdes_dropped",     "XPATH_BOM_COMPONENTS",    bom_refdes_drop),
        ("bom_authored_flipped",   "XPATH_BOM_AUTHORED",      bom_authored_flip),
        ("bom_value_rewritten",    "XPATH_BOM_FIELDS",        bom_value_rewrite),
        ("bom_kind_rewritten",     "XPATH_BOM_FIELDS",        bom_kind_rewrite),
        ("bom_refdes_swapped",     "XPATH_BOM_FIELDS",        bom_refdes_swap),
        ("bom_derived_tampered",   "XPATH_BOM_FIELDS",        bom_derived_tamper),
        ("pinmap_comp_dropped",    "XPATH_PINMAP_COMPONENTS", pinmap_comp_drop),
        ("pinmap_net_rewritten",   "XPATH_PINMAP_NET",        pinmap_net_rewrite),
        ("pinmap_role_rewritten",  "XPATH_PINMAP_TERMS",      pinmap_role_rewrite),
        ("pinmap_unwired_dropped", "XPATH_PINMAP_TERMS",      pinmap_term_drop_unwired),
        ("pinmap_phantom_added",   "XPATH_PINMAP_TERMS",      pinmap_term_phantom),
        ("pinmap_value_rewritten", "XPATH_PINMAP_TERMS",      pinmap_value_rewrite),
        ("alloc_row_dropped",      "XPATH_ALLOC_MISMATCH",    alloc_row_drop),
        ("alloc_net_rewritten",    "XPATH_ALLOC_NET",         alloc_net_rewrite),
        ("stamp_tampered",         "XPATH_STAMP_MISMATCH",    stamp_tamper),
        ("connector_row_rewritten", "XPATH_CONNECTOR_NET",    connector_row_rewrite),
        ("connector_row_dropped",  "XPATH_CONNECTOR_NET",     connector_row_drop),
        ("alloc_state_forged",     "XPATH_ALLOC_MISMATCH",    alloc_state_forged),
        ("alloc_lock_stripped",    "XPATH_ALLOC_MISMATCH",    alloc_lock_stripped),
        ("alloc_chosen_by_forged", "XPATH_ALLOC_MISMATCH",    alloc_chosen_by_forged),
        ("connector_lock_stripped", "XPATH_CONNECTOR_LOCK",   connector_lock_stripped),
        ("connector_lock_forged",  "XPATH_CONNECTOR_LOCK",    connector_lock_forged),
        ("connector_function_rewritten", "XPATH_CONNECTOR_NET",
         connector_function_rewritten),
        ("records_fork_forged",    "XPATH_RECORDS_L1",        records_fork_forged),
        ("stamp_locks_desynced",   "XPATH_STAMP_MISMATCH",    stamp_locks_desynced),
    ]


# ---------------------------------------------------------------------------
# the SPICE DC operating-point oracle (WyredPlanSpice step 2.2)
# ---------------------------------------------------------------------------
# For every emitted artifact carrying a ``.cir`` deck, delegate the DC
# operating point to ngspice (via the harness ``spice_run`` runner — the engine
# is never imported, ngspice is never imported, both are reached only as
# subprocesses) and require each declared power rail to sit within its declared
# tolerance of the simulated node voltage (``spice_oracle.check_rails`` ->
# ``SPICE_RAIL_OFF`` / ``SPICE_RAIL_UNRESOLVED``). Gate-wiring discipline
# (WyredPlanSpice ground rules): ``.cir`` absent -> stage not applicable
# (Phase-1 gating already made deck absence honest); ngspice absent -> a LOUD
# SKIP naming every deck and every declared rail NOT verified (never a silent
# green, never a failure); a broken run (timeout/crash/unparseable) is
# spice_run's STRUCTURED FAILURE, red — never laundered into a skip.
# ---------------------------------------------------------------------------

def _print_spice_skip_banner(tree: Path, decks: list) -> None:
    """The loud SKIP banner for the SPICE op-oracle (the run_gate
    ``_skip_probes_warning`` shape): names every deck NOT simulated and every
    declared rail NOT verified, so an absent ngspice can never read as a silent
    green."""
    bang = "!" * 72
    print(bang)
    print("!!! SKIPPED: SPICE DC operating-point vs declared-rails oracle")
    print("!!! ngspice was NOT found (PATH / %s)." % spice_run.NGSPICE_ENV)
    print("!!! The decks below are structurally present and gate-clean, but")
    print("!!! their DC operating points were NOT computed and these declared")
    print("!!! rails were NOT checked against simulation:")
    for name in decks:
        try:
            l2 = _read_json(tree, name, "l2")
        except (OSError, ValueError):
            l2 = {"nets": []}
        rails = spice_oracle.declared_power_rails(l2)
        print("!!!   - %s.cir" % name)
        if rails:
            for r in rails:
                print("!!!       rail %s = %gV +/-%g%% NOT VERIFIED"
                      % (r["name"], float(r["voltage"]),
                         spice_oracle.rail_tol_pct(r)))
        else:
            print("!!!       (no declared power rails)")
    print("!!! Install ngspice (brew install ngspice) or set %s to a binary "
          "to verify." % spice_run.NGSPICE_ENV)
    print(bang, flush=True)


def _spice_op_stage(tree: Path, gate_names: list, work_dir: Path) -> int:
    """Run the SPICE DC operating-point oracle over every gated artifact that
    carries a ``.cir`` deck. Returns a failure count (0 on a loud skip)."""
    print("\n== gate: SPICE DC operating point vs declared rails "
          "(ngspice, when a .cir exists) ==")
    decks = [n for n in gate_names if (tree / ("%s.cir" % n)).is_file()]
    if not decks:
        print("     no .cir deck in the tree; SPICE op-oracle not applicable")
        return 0

    binary = spice_run.find_ngspice()
    if binary is None:
        _print_spice_skip_banner(tree, decks)
        return 0

    spice_run.print_version_pin(binary, spice_run.probe_version(binary))
    failures = 0
    op_root = work_dir / "spiceop"
    for name in decks:
        deck = tree / ("%s.cir" % name)
        sidecar = _read_json(tree, name, "cir")
        l2 = _read_json(tree, name, "l2")
        result = spice_run.run_op(deck, ngspice=binary,
                                  work_dir=op_root / name)
        if result.status == spice_run.STATUS_SKIP:
            # ngspice vanished between discovery and this run — treat as skip,
            # never a silent pass.
            _print_spice_skip_banner(tree, [name])
            continue
        if result.status == spice_run.STATUS_FAIL:
            failures += 1
            print("FAIL %-28s SPICE run failed (%s): %s"
                  % (name, result.error_kind, result.error))
            continue

        record = result.to_record()
        findings = spice_oracle.check_rails(l2, sidecar, record)
        meas = spice_oracle.rail_measurements(l2, sidecar, record)
        if findings:
            failures += 1
            print("FAIL %-28s SPICE op-oracle: %d rail finding(s)"
                  % (name, len(findings)))
            for f in findings:
                print("       spice: %s: %s" % (f.code, f.msg))
        else:
            summary = ", ".join(
                "%s=%sV" % (m["net"], ("%.6g" % m["simulated"])
                            if m["simulated"] is not None else "?")
                for m in meas)
            print("PASS %-28s SPICE op-oracle: %d rail(s) on-rail%s"
                  % (name, len(meas), (" (%s)" % summary) if summary else ""))
    return failures


# ---------------------------------------------------------------------------
# the gate
# ---------------------------------------------------------------------------

def run_gate(tree: Path, corpus_dir: Path, engine_src: Path,
             work_dir: Path) -> tuple:
    """Gate one emitted artifact tree. Returns (artifact_count, failures)."""
    failures = 0

    # ---- determinism: two fresh subprocess emits, byte-compared ----------
    print("== gate: determinism (corpus emitted twice via subprocess, "
          "byte-compared) ==")
    emit_a = work_dir / "emit1"
    emit_b = work_dir / "emit2"
    det_bad = {}                  # artifact -> set of differing path kinds
    emits_ok = True
    for d in (emit_a, emit_b):
        proc = _emit_subprocess(engine_src, corpus_dir, d)
        if proc.returncode != 0:
            failures += 1
            emits_ok = False
            print("FAIL emit subprocess (%s) exited %d"
                  % (d.name, proc.returncode))
            for line in (proc.stdout + proc.stderr).splitlines()[-8:]:
                print("       emit: %s" % line)
    if emits_ok:
        diffs, nfiles = _byte_compare(emit_a, emit_b)
        if diffs:
            failures += 1
            print("FAIL determinism: %d file(s) differ between the two "
                  "emits:" % len(diffs))
            for rel in diffs:
                print("       %s" % rel)
                base = rel[:-len(".json")] if rel.endswith(".json") else rel
                name, _, kind = base.rpartition(".")
                det_bad.setdefault(name, set()).add(kind)
        else:
            print("PASS determinism: two emits byte-identical (%d files)"
                  % nfiles)

    # ---- expectations: the corpus's OWN declarations, read as data -------
    try:
        exp = _load_expectations(engine_src, corpus_dir)
    except (RuntimeError, ValueError) as exc:
        failures += 1
        print("FAIL expectations: %s" % exc)
        return 0, failures
    order, expect = exp["order"], exp["expect"]

    tree_names = sorted(p.name[:-len(".l1.json")]
                        for p in tree.glob("*.l1.json"))
    for n in tree_names:
        if n not in expect:
            failures += 1
            print("FAIL %-28s in the tree but not declared by the corpus"
                  % n)
    for n in order:
        if n not in tree_names:
            failures += 1
            print("FAIL %-28s declared by the corpus but the tree has no "
                  "%s.l1.json" % (n, n))
    gate_names = [n for n in order if n in set(tree_names)]

    battery = _xpath_probe_battery()
    cir_battery = _xcir_probe_battery()
    all_probes = battery + cir_battery
    xprobe_fired = {}             # probe id -> artifact it fired on
    probe_root = work_dir / "probes"

    print("\n== gate: layer 1 (public oracle) -> layer 2 (v3 stack + "
          "cross-path differential, from disk) ==")
    for name in gate_names:
        l1 = _read_json(tree, name, "l1")
        expected_codes = list(expect[name]["expected_l1"])
        expect_esc = bool(expect[name]["expect_escalation"])
        codes = sorted(v.code for v in intent_oracle.check_intent(l1))
        escs = l1.get("escalations", [])
        esc_ok = (not expect_esc) or any(
            e.get("conflict") and e.get("relaxation") for e in escs)
        l1_det = "l1" not in det_bad.get(name, ())
        l1_ok = (codes == expected_codes and esc_ok and l1_det)

        # ---- the M2 data path: only a CLEAN layer 1 earns a netlist ------
        wants_l2 = not expected_codes
        l2_ok = True
        l2_verdict = None
        alloc_codes = None
        if wants_l2 and codes == expected_codes:
            if not (tree / ("%s.l2.json" % name)).exists():
                failures += 1
                print("FAIL %-28s declared clean but the tree has no "
                      "%s.l2.json (resolver refused at emit?)"
                      % (name, name))
                continue
            expected_verdict = "ESCALATE" if expect_esc else "PASS"
            l2 = _read_json(tree, name, "l2")
            alloc = _read_json(tree, name, "alloc")

            # cross-path differential (the architectural oracle, Gen4
            # section 1.5), re-run FROM DISK by the ENGINE'S OWN
            # ``python3 -m wyred.crosscheck`` subprocess: the netlist,
            # BOM, pin-map, records AND the emitted l1 (the provenance
            # anchor for the records path) must describe one model.
            rc, xfails = _crosscheck_subprocess(engine_src, corpus_dir,
                                                tree, name)
            if xfails:
                failures += 1
                print("FAIL %-28s cross-path differential:" % name)
                for f in xfails:
                    print("       xpath: %s: %s" % (f["code"], f["msg"]))
            elif rc != 0:
                failures += 1
                print("FAIL %-28s crosscheck subprocess exited %d with no "
                      "parsable codes" % (name, rc))
            else:
                # the NEGATIVE-probe battery (P3): every XPATH_* code must
                # be shown to FIRE on a doctored COPY of a clean quintuple
                # (written under the temp dir — never the input tree).
                # Each probe runs until it has fired once this run; an
                # applied probe whose code stays silent is a failure.
                clean = (l2,
                         _read_json(tree, name, "bom"),
                         _read_json(tree, name, "pinmap"),
                         _read_json(tree, name, "records"),
                         l1)
                for probe_id, want_code, mutate in battery:
                    if probe_id in xprobe_fired:
                        continue
                    g, b, p, r, l = (copy.deepcopy(x) for x in clean)
                    if not mutate(g, b, p, r, l):
                        continue        # precondition unmet here; try later
                    fired = _crosscheck_doctored(
                        engine_src, corpus_dir, probe_root, name,
                        g, b, p, r, l)
                    if want_code in fired:
                        xprobe_fired[probe_id] = name
                    else:
                        failures += 1
                        print("FAIL %-28s xpath counter-probe %r applied "
                              "but %s did NOT fire (fired: %s)"
                              % (name, probe_id, want_code,
                                 sorted(fired) or "[]"))

                # the SPICE deck battery (WyredPlanSpice 1.3): where the emit
                # wrote a ``.cir`` for this artifact, prove every XCIR_* code
                # fires on a doctored COPY of the clean deck + sidecar (written
                # under the temp dir — never the input tree). Same once-per-run
                # accounting and lobotomy verdict as the XPATH battery.
                if (tree / ("%s.cir" % name)).exists():
                    clean_deck = (tree / ("%s.cir" % name)).read_text()
                    clean_side = _read_json(tree, name, "cir")
                    clean_set = {"l2": clean[0], "bom": clean[1],
                                 "pinmap": clean[2], "records": clean[3],
                                 "l1": clean[4]}
                    for probe_id, want_code, mutate in cir_battery:
                        if probe_id in xprobe_fired:
                            continue
                        deck_lines = clean_deck.splitlines()
                        side = copy.deepcopy(clean_side)
                        if not mutate(deck_lines, side, clean[0]):
                            continue        # precondition unmet here; try later
                        doctored = "\n".join(deck_lines) + "\n"
                        fired = _crosscheck_doctored_cir(
                            engine_src, corpus_dir, probe_root, name,
                            clean_set, doctored, side)
                        if want_code in fired:
                            xprobe_fired[probe_id] = name
                        else:
                            failures += 1
                            print("FAIL %-28s spice counter-probe %r applied "
                                  "but %s did NOT fire (fired: %s)"
                                  % (name, probe_id, want_code,
                                     sorted(fired) or "[]"))

            # ---- the v3 stack, every input re-read from disk -------------
            doc_obj = schema_l1.from_json(l1)
            graph_obj = schema.from_json(l2)

            # the grader-retained EXTERNAL baseline (locked emits only);
            # when locked groups exist and no baseline was retained,
            # check_allocations fires LOCK_UNVERIFIED rather than
            # silently passing.
            baseline = None
            bpath = tree / ("%s.baseline.json" % name)
            if bpath.exists():
                baseline = json.loads(bpath.read_text())

            # record2: the second re-emit's record — an independent
            # re-solve of the same (document, solver_version).
            alloc2 = None
            a2path = emit_b / ("%s.alloc.json" % name)
            if a2path.exists():
                alloc2 = json.loads(a2path.read_text())
            else:
                failures += 1
                print("FAIL %-28s re-emit produced no %s.alloc.json (no "
                      "independent record2)" % (name, name))

            record = {"allocation": alloc["allocation"],
                      "bindings": alloc["bindings"]}
            if alloc2 is not None:
                record["record2"] = alloc2["allocation"]
            if baseline is not None:
                record["baseline"] = baseline
            sat = spec_satisfaction.satisfies(l1, l2, record)
            erc_v = erc_oracle.run_erc(graph_obj)
            inv_v = invariant_oracle.check_invariants(graph_obj)
            inv_declared = len(l1.get("invariants", []))
            inv_lowered = len(l2.get("invariants", []))
            record2 = None
            if alloc2 is not None:
                record2 = schema_l1.from_json(
                    {"allocation": alloc2["allocation"]}).allocation
            alloc_v = alloc_oracle.check_allocations(
                doc_obj, graph_obj, record2=record2, baseline=baseline)
            alloc_codes = sorted(v.code for v in alloc_v)

            l2_det = not (det_bad.get(name, set())
                          & {"l2", "alloc", "bom", "pinmap", "records"})
            l2_ok = (sat["verdict"] == expected_verdict
                     and not erc_v and not inv_v
                     and inv_lowered == inv_declared
                     and not alloc_v and l2_det)
            l2_verdict = sat["verdict"]
            if not l2_ok:
                failures += 1
                print("FAIL %-28s L2 verdict=%s (expected %s)"
                      % (name, sat["verdict"], expected_verdict))
                for v in sat["violations"]:
                    print("       sat: %s: %s" % (v.code, v.msg))
                for v in erc_v:
                    print("       erc: %s: %s" % (v.code, v.msg))
                for v in inv_v:
                    print("       inv: %s: %s" % (v.code, v.msg))
                for v in alloc_v:
                    print("       alloc: %s: %s" % (v.code, v.msg))
                if inv_lowered != inv_declared:
                    print("       invariants: declared %d, lowered %d"
                          % (inv_declared, inv_lowered))
                if not l2_det:
                    print("       L2 NOT deterministic: two emits differ")
        else:
            # no netlist gating for a failing layer 1 — the embedded
            # allocation record is still checked standalone
            doc_obj = schema_l1.from_json(l1)
            alloc_codes = sorted(
                v.code for v in alloc_oracle.check_allocations(doc_obj))

        l1_alloc_ok = (alloc_codes == [] if alloc_codes is not None
                       else True)
        ok = l1_ok and l2_ok and l1_alloc_ok
        if not (l1_ok and l1_alloc_ok):     # l2 failures counted above
            failures += 1

        status = "PASS" if ok else "FAIL"
        l2_note = ""
        if l2_verdict is not None:
            l2_note = "  L2=%s" % l2_verdict
        elif not wants_l2:
            l2_note = "  L2=(none: fails at L1 by design)"
        print("%s %-28s codes=%s (expected %s)%s"
              % (status, name, codes or "[]", expected_codes or "[]",
                 l2_note))
        if not l1_det:
            print("       L1 NOT deterministic: two emits differ")
        if alloc_codes:
            print("       allocation record dirty: %s" % alloc_codes)
        if expect_esc:
            print("       escalation-with-explanation: %s"
                  % ("present" if esc_ok else "MISSING"))

    # the battery verdict: every probe must have fired SOMEWHERE this run.
    # The XPATH cross-path probes and the WyredPlanSpice 1.3 XCIR deck probes
    # share ONE lobotomy verdict — a spice-modelled artifact that stops
    # emitting (or a blanked check) leaves an XCIR probe unfired and turns the
    # run red, exactly as a removed board module does for a connector probe.
    unfired = [(pid, code) for pid, code, _ in all_probes
               if pid not in xprobe_fired]
    if unfired:
        failures += 1
        print("\nFAIL cross-path + spice counter-probe battery: %d/%d "
              "probe(s) never found an applicable artifact or never fired:"
              % (len(unfired), len(all_probes)))
        for pid, code in unfired:
            print("       %s (expects %s)" % (pid, code))
    else:
        codes = sorted({code for _, code, _ in all_probes})
        print("\nPASS cross-path + spice counter-probe battery: %d/%d probes "
              "fired (%d codes negatively probed: %s)"
              % (len(xprobe_fired), len(all_probes), len(codes),
                 ", ".join(codes)))

    # the SPICE DC operating-point oracle (WyredPlanSpice 2.2): gate-wired for
    # every artifact with a ``.cir``; ngspice absent -> loud skip (never a
    # failure), so a machine without a simulator still gates green.
    failures += _spice_op_stage(tree, gate_names, work_dir)
    return len(gate_names), failures


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--tree", required=True,
                    help="emitted artifact tree to gate (a wyred.emit "
                         "--out directory)")
    ap.add_argument("--corpus-dir", required=True,
                    help="corpus the tree was emitted from (read-only: "
                         "declarations + the determinism re-emits)")
    ap.add_argument("--engine-src", default=str(DEFAULT_ENGINE_SRC),
                    help="wyred engine src dir, invoked ONLY as a "
                         "subprocess (default: %(default)s)")
    args = ap.parse_args(argv)

    tree = Path(args.tree)
    if not tree.is_dir():
        print("not a directory: %s" % tree, file=sys.stderr)
        return 2
    corpus_dir = Path(args.corpus_dir)
    if not corpus_dir.is_dir():
        print("not a directory: %s" % corpus_dir, file=sys.stderr)
        return 2
    engine_src = Path(args.engine_src)
    if not (engine_src / "wyred").is_dir():
        print("no wyred package under engine src: %s" % engine_src,
              file=sys.stderr)
        return 2

    with tempfile.TemporaryDirectory(prefix="wyred-gate-") as td:
        count, failures = run_gate(tree, corpus_dir, engine_src, Path(td))
    print("\nRESULT: %s (%d artifact(s), %d failure(s))"
          % ("PASS" if failures == 0 else "FAIL", count, failures))
    return 0 if failures == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
