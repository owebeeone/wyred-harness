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
                                 mechanical fact, not a claim.

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
    unfired = [(pid, code) for pid, code, _ in battery
               if pid not in xprobe_fired]
    if unfired:
        failures += 1
        print("\nFAIL xpath counter-probe battery: %d/%d probe(s) never "
              "found an applicable artifact or never fired:"
              % (len(unfired), len(battery)))
        for pid, code in unfired:
            print("       %s (expects %s)" % (pid, code))
    else:
        codes = sorted({code for _, code, _ in battery})
        print("\nPASS xpath counter-probe battery: %d/%d probes fired "
              "(%d codes negatively probed: %s)"
              % (len(xprobe_fired), len(battery), len(codes),
                 ", ".join(codes)))
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
