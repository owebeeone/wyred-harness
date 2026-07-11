"""Self-test for harness v3 (Gen4 two-phase intent model) — ADDITIVE to v2.

Run from INSIDE wyred-harness/harness/ ::

    python selftest_v3.py

It asserts (and prints a readable summary of):

  (a) v2 REGRESSION — ``python selftest.py`` still passes (all 35 checks) in a
      subprocess: v3 is additive; nothing here may break the v2 stack.
  (b) L1 ORACLE — every GOLDENS_V3 layer-1 doc yields its expected layer-1
      verdict through the intent oracle (PASS / FAIL:<CODE> / ESCALATE), with
      the corpus-pinned specifics: #3 (ADDR_COLLISION) and #7
      (VOLTAGE_MISMATCH) fail AT layer 1 (they never reach binding), #5b
      ESCALATES carrying its UNSAT-core explanation (conflict + relaxation),
      and #9's 4-demand/4-unit pool is sufficient (spare visible, no
      POOL_INSUFFICIENT).
  (c) SPEC-SATISFACTION (the PRIMARY v3 verdict) — every positive intent's
      (l1, l2, record) triple satisfies; every KNOWN_BAD_V3 corruption is
      caught with its expected code by its owning layer (l1 -> intent oracle,
      record -> allocation checks) AND surfaces through the composed
      ``satisfies`` verdict.
  (d) ALLOCATION DETERMINISM — an identical (order-scrambled) re-solve record
      is accepted; a perturbed re-solve triggers ALLOC_NONDETERMINISTIC.
  (e) #9 SPARE UNIT — pool unit 3 is present in the realized record view with
      ladder state "free" (no allocation entry; unit_count keeps it visible;
      never silently deleted).
  (f) PCB EXTRACTOR on REAL flagship boards — the MPPT 2420 HC board yields
      150-175 components, a GND ground net, and a +12V power net at 12.0V;
      the RPi CM4IO board the same shape with relaxed component bounds.
  (g) kicad-cli DIFFERENTIAL (OPTIONAL) — if kicad-cli is present, Watchy's
      root .kicad_sch (copied out of the READ-ONLY third-party tree first) is
      netlisted by KiCad's own engine and compared against pcb_extract's
      reading of Watchy's .kicad_pcb: the two engines must AGREE
      (verdict "engines_agree" — a Rand-index similarity, NOT a wiring
      gate). If kicad-cli is absent the section prints SKIPPED and does not
      fail.
  (h) v3 HARDENING (F1..F8) — one check block per adversarially-verified
      loophole fix: F1 VACUOUS_INTENT (zero-demand docs are not clean); F2
      demand traceability (provenance attrs on every L2 component; a rogue
      fully-wired MCU+relay fails UNDEMANDED_COMPONENT; dangling companion
      provenance fails ORPHAN_GENERATED); F3 typed defaults (the corpus-#7
      default dodge fails at L1; default='GND' on a uart demand is
      DEFAULT_INCOMPATIBLE); F4 verdict/demand-map consistency (PASS <=>
      zero violations <=> every demand satisfied-or-escalated; the silent
      fall-through emits UNMET_DEMAND); F5 external lock baseline as the
      protocol (tamper+rewrite-embedded-snapshot is caught via the external
      baseline; baseline=None on a locked doc is LOCK_UNVERIFIED, never a
      silent pass); F6 series fork record (hand-edited series without
      forked_from is SERIES_UNJUSTIFIED; break_lock's record legalizes);
      F7 differential honesty (dead-short GND->+3V3 surfaces a
      named_net_merge warning although similarity stays high); F8 PCB
      degradation markers (stripped net names set PCB_NET_NAMES_MISSING).
  (i) ADVERSARIAL RE-VERIFICATION (F9) — regression anchors from the
      independent re-run of the original 8 exploits: the bindings-omission
      dodge is closed (realization checks run over provenance-DERIVED
      role->refdes candidates when the caller supplies no bindings; an
      impostor claiming an already-bound role cannot reopen it); provenance
      keys are per authorship class (authored -> l1_role, generated ->
      for_demand — the other key never substitutes); the full lock cover-up
      (entry edit + version rollback + cleared metadata, or deleting the
      locked group) is caught by the external baseline; an explained
      escalation cannot launder DEMAND_UNSATISFIABLE; the F8 degradation
      marker fires on a REAL legacy-dialect board stripped of net names; and
      (in section g) the dead-short probe runs on the REAL Watchy sch/pcb
      pair, not just the synthetic one.

Exit status is 0 iff every (non-skipped) check passes, non-zero otherwise.
Pure Python 3 stdlib; the only external touchpoints are the ``python
selftest.py`` subprocess and (optionally) the kicad-cli binary via
kicad_adapter. Plain imports (flat harness directory).
"""

from __future__ import annotations

import glob
import os
import shutil
import subprocess
import sys
import tempfile
from typing import Dict, List, Optional

import schema
import schema_l1
from allocation import (check_allocations, check_lock_violations,
                        covered_entries, fire_sync_point, break_lock,
                        snapshot_locks)
from goldens_v3 import GOLDENS_V3, KNOWN_BAD_V3, spare_units
from intent_oracle import check_intent
from kicad_adapter import find_kicad_cli, sch_netlist, compare
from pcb_extract import extract_pcb, extract_pcb_text
from schema import CanonicalGraph, Component, Net, Terminal
from spec_satisfaction import satisfies


HARNESS_DIR = os.path.dirname(os.path.abspath(__file__))
THIRD_PARTY = os.path.normpath(
    os.path.join(HARNESS_DIR, "..", "..", "third-party"))

MPPT_PCB = os.path.join(
    THIRD_PARTY, "libre-solar-mppt-2420-hc", "kicad", "mppt-2420-hc.kicad_pcb")
CM4IO_PCB = os.path.join(THIRD_PARTY, "rpi-cm4io", "CM4IOv5.kicad_pcb")
WATCHY_DIR = os.path.join(THIRD_PARTY, "watchy")

#: Optional env var naming a writable work directory for the Watchy copy
#: (third-party is READ-ONLY; the schematic + its libs are copied out before
#: kicad-cli touches anything). Defaults to a private mkdtemp.
WORKDIR_ENV = "SELFTEST_V3_WORKDIR"


# ---------------------------------------------------------------------------
# tiny assertion harness (collects failures instead of aborting on the first)
# ---------------------------------------------------------------------------

class _Results:
    def __init__(self) -> None:
        self.failures: List[str] = []
        self.checks = 0
        self.skipped: List[str] = []

    def check(self, ok: bool, label: str) -> None:
        self.checks += 1
        print("  [%s] %s" % ("ok  " if ok else "FAIL", label))
        if not ok:
            self.failures.append(label)

    def skip(self, label: str) -> None:
        self.skipped.append(label)
        print("  [SKIP] %s" % label)


R = _Results()


# ---------------------------------------------------------------------------
# (a) v2 regression: `python selftest.py` must keep passing (35 checks)
# ---------------------------------------------------------------------------

def test_v2_regression() -> None:
    print("\n(a) v2 regression: `python selftest.py` (subprocess) must pass")
    proc = subprocess.run(
        [sys.executable, "selftest.py"],
        cwd=HARNESS_DIR,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        timeout=600,
    )
    tail = next((ln for ln in reversed(proc.stdout.splitlines())
                 if ln.startswith("RESULT:")), "")
    R.check(proc.returncode == 0,
            "selftest.py exit code 0 (got %d)" % proc.returncode)
    R.check("all 35 checks passed" in proc.stdout,
            "selftest.py reports all 35 checks passed (last line: %r)" % tail)


# ---------------------------------------------------------------------------
# (b) L1 oracle: every goldens_v3 layer-1 doc -> its expected verdict
# ---------------------------------------------------------------------------

def _l1_verdict(doc: schema_l1.IntentDoc) -> str:
    """Layer-1 verdict per the score.verdict_for convention: FAIL:<codes>
    (deduped, first-seen order) beats ESCALATE beats PASS."""
    codes: List[str] = []
    for v in check_intent(doc):
        if v.code not in codes:
            codes.append(v.code)
    if codes:
        return "FAIL:" + ",".join(codes)
    if doc.escalations:
        return "ESCALATE"
    return "PASS"


def test_l1_oracle() -> None:
    print("\n(b) L1 oracle: every goldens_v3 layer-1 doc yields its expected "
          "verdict")
    for iid, entry in GOLDENS_V3.items():
        expected = entry["expected"]["l1"]
        got = _l1_verdict(entry["l1"])
        R.check(got == expected,
                "#%-2s l1 verdict: expected %-24s got %s"
                % (iid, expected, got))
        # the composed primary oracle, scored layer-1 standalone, must agree
        got_sat = satisfies(entry["l1"], None)["verdict"]
        R.check(got_sat == expected,
                "#%-2s satisfies(l1, None) agrees: %s" % (iid, got_sat))

    # -- corpus-pinned specifics -------------------------------------------
    for iid, code in (("3", "ADDR_COLLISION"), ("7", "VOLTAGE_MISMATCH")):
        entry = GOLDENS_V3[iid]
        codes = {v.code for v in check_intent(entry["l1"])}
        R.check(code in codes and entry["l2"] is None
                and entry["expected"]["l2"] is None,
                "#%s fails AT L1 with %s and never reaches binding "
                "(l2 is None)" % (iid, code))

    d5b = GOLDENS_V3["5b"]["l1"]
    esc = d5b.escalations[0] if d5b.escalations else None
    R.check(
        _l1_verdict(d5b) == "ESCALATE"
        and esc is not None
        and any(str(c).strip() for c in esc.conflict)
        and bool(esc.relaxation.strip())
        and "ESCALATION_UNEXPLAINED"
        not in {v.code for v in check_intent(d5b)},
        "#5b ESCALATES WITH explanation: UNSAT-core conflict (%d clause(s)) "
        "+ minimal relaxation present" % (len(esc.conflict) if esc else 0))

    d9 = GOLDENS_V3["9"]["l1"]
    pool9 = d9.pool("glue.nands")
    nand_demands = [d for r in d9.roles for d in r.demands
                    if d.iface == "nand"]
    R.check(
        "POOL_INSUFFICIENT" not in {v.code for v in check_intent(d9)}
        and len(nand_demands) == 4 and pool9.unit_count == 4,
        "#9 pool-sufficient: 4 NAND demands vs 4-unit pool, no "
        "POOL_INSUFFICIENT")


# ---------------------------------------------------------------------------
# (c) spec-satisfaction: positives satisfy; KNOWN_BAD_V3 caught by code
# ---------------------------------------------------------------------------

def test_spec_satisfaction() -> None:
    print("\n(c) spec-satisfaction: positive (l1, l2, record) triples satisfy")
    for iid, entry in GOLDENS_V3.items():
        if entry["l2"] is None:
            continue
        res = satisfies(entry["l1"], entry["l2"], entry["record"])
        R.check(res["verdict"] == "PASS",
                "#%-2s satisfies(l1, l2, record) == PASS (got %s)"
                % (iid, res["verdict"]))
        unsat = sorted(d for d, i in res["demands"].items()
                       if i["status"] != "satisfied")
        R.check(not unsat,
                "#%-2s every demand satisfied in the bound graph%s"
                % (iid, "" if not unsat else " (unsatisfied: %s)" % unsat))

    print("\n    KNOWN_BAD_V3: each corruption caught with its expected code")
    for kb in KNOWN_BAD_V3:
        doc, code, layer = kb["l1"], kb["expected_code"], kb["layer"]
        if layer == "l1":
            got = {v.code for v in check_intent(doc)}
            owner = "intent_oracle.check_intent"
        else:  # "record"
            got = {v.code for v in
                   check_allocations(doc, None, record=kb["record"])}
            owner = "allocation.check_allocations"
        R.check(code in got,
                "%-20s caught by %s (codes=%s) :: %s"
                % (code, owner, ",".join(sorted(got)) or "<none>",
                   kb["note"]))
        # and the composed primary verdict surfaces the same code
        sat_codes = {v.code for v in
                     satisfies(doc, None, kb["record"])["violations"]}
        R.check(code in sat_codes,
                "%-20s surfaces through satisfies() (codes=%s)"
                % (code, ",".join(sorted(sat_codes)) or "<none>"))


# ---------------------------------------------------------------------------
# (d) allocation determinism: identical re-solve accepted; perturbed flagged
# ---------------------------------------------------------------------------

def _copy_doc(doc: schema_l1.IntentDoc) -> schema_l1.IntentDoc:
    return schema_l1.from_json(schema_l1.to_json(doc))


def test_allocation_determinism() -> None:
    print("\n(d) allocation determinism (byte-identical canonical re-solve)")
    for iid in ("5a", "8", "9"):
        doc = _copy_doc(GOLDENS_V3[iid]["l1"])
        resolve = _copy_doc(doc).allocation      # an independent re-solve
        resolve.entries.reverse()                # entry order must not matter
        clean = check_allocations(doc, None, record=doc.allocation,
                                  record2=resolve)
        R.check(clean == [],
                "#%-2s identical (order-scrambled) re-solve record accepted "
                "(codes=%s)" % (iid, ",".join(sorted({v.code for v in clean}))
                                or "<none>"))

    doc = _copy_doc(GOLDENS_V3["5a"]["l1"])
    drifted = _copy_doc(doc).allocation
    drifted.entries[0].unit = 2                  # the re-solve drifted
    codes = {v.code for v in check_allocations(doc, None,
                                               record=doc.allocation,
                                               record2=drifted)}
    R.check("ALLOC_NONDETERMINISTIC" in codes,
            "#5a perturbed re-solve (unit 0 -> 2) -> ALLOC_NONDETERMINISTIC "
            "(codes=%s)" % ",".join(sorted(codes)))


# ---------------------------------------------------------------------------
# (e) #9 spare unit: present in the record view, ladder state "free"
# ---------------------------------------------------------------------------

def test_spare_unit() -> None:
    print("\n(e) #9 spare unit: visible in the record, state=free")
    doc = GOLDENS_V3["9"]["l1"]
    pool = doc.pool("glue.nands")
    rec = GOLDENS_V3["9"]["record"]
    R.check(rec is doc.allocation,
            "#9 record is the l1 doc's embedded allocation record")

    # the realized per-unit view the record + pool declaration denote:
    by_unit: Dict[int, str] = {e.unit: e.state
                               for e in rec.entries if e.pool == pool.name}
    view = {u: by_unit.get(u, "free") for u in range(pool.unit_count)}
    R.check(spare_units(pool, rec) == [3],
            "#9 spare_units(glue.nands) == [3] (uncommitted, never deleted)")
    R.check(view[3] == "free" and 3 not in by_unit,
            "#9 unit 3 state in the record view: 'free' (no allocation "
            "entry; ladder state free by definition)")
    R.check(view[0] == view[1] == view[2] == "sticky",
            "#9 allocated units 0/1/2 are sticky solver choices")
    R.check(pool.unit_count == 4,
            "#9 pool keeps declaring unit_count=4 (spare capacity VISIBLE)")


# ---------------------------------------------------------------------------
# (f) PCB extractor on the REAL flagship boards
# ---------------------------------------------------------------------------

PCB_SUMMARIES: Dict[str, str] = {}


def _extract_board(label: str, path: str, lo: int, hi: int) -> None:
    if not os.path.isfile(path):
        R.check(False, "%s: board file present (%s)" % (label, path))
        PCB_SUMMARIES[label] = "MISSING: %s" % path
        return
    g = extract_pcb(path)
    ncomp, nnets = len(g.components), len(g.nets)
    gnds = [n for n in g.nets if n.kind == "ground"
            and n.name.rsplit("/", 1)[-1].upper() == "GND"]
    v12 = [n for n in g.nets if n.kind == "power" and n.voltage == 12.0]
    summary = ("components=%d nets=%d GND=%s 12V-power=%s"
               % (ncomp, nnets, [n.name for n in gnds],
                  [n.name for n in v12]))
    PCB_SUMMARIES[label] = summary
    print("    %s: %s" % (label, summary))
    R.check(lo <= ncomp <= hi,
            "%s: component count %d within [%d, %d]" % (label, ncomp, lo, hi))
    R.check(bool(gnds),
            "%s: GND ground net present" % label)
    R.check(bool(v12),
            "%s: a 12V power net present with voltage 12.0 (%s)"
            % (label, [n.name for n in v12]))


def test_pcb_extract() -> None:
    print("\n(f) pcb_extract on the real flagships")
    _extract_board("mppt-2420-hc", MPPT_PCB, 150, 175)
    # relaxed bounds for the CM4IO carrier (modern 'footprint' dialect)
    _extract_board("rpi-cm4io", CM4IO_PCB, 100, 250)


# ---------------------------------------------------------------------------
# (g) kicad-cli differential on Watchy (OPTIONAL: SKIPPED when cli absent)
# ---------------------------------------------------------------------------

KICAD_DIFFERENTIAL = "not run"


def _watchy_root_sch() -> Optional[str]:
    """The ROOT .kicad_sch of the Watchy project: prefer the schematic whose
    stem matches a sibling .kicad_pro (the project root), else the shallowest
    .kicad_sch in the tree."""
    cands = sorted(glob.glob(os.path.join(WATCHY_DIR, "**", "*.kicad_sch"),
                             recursive=True))
    if not cands:
        return None
    for c in cands:
        pro = os.path.splitext(c)[0] + ".kicad_pro"
        if os.path.isfile(pro):
            return c
    return min(cands, key=lambda p: (p.count(os.sep), p))


def test_kicad_differential() -> None:
    global KICAD_DIFFERENTIAL
    print("\n(g) kicad-cli differential: Watchy schematic netlist vs "
          "pcb_extract")
    cli = find_kicad_cli()
    if cli is None:
        KICAD_DIFFERENTIAL = "SKIPPED (kicad-cli unavailable)"
        R.skip("kicad-cli not found -- differential SKIPPED (graceful "
               "degradation, not a failure)")
        return

    root_sch = _watchy_root_sch()
    watchy_pcb = os.path.join(WATCHY_DIR, "Watchy.kicad_pcb")
    R.check(root_sch is not None,
            "Watchy root .kicad_sch located (%s)" % root_sch)
    R.check(os.path.isfile(watchy_pcb),
            "Watchy .kicad_pcb present (%s)" % watchy_pcb)
    if root_sch is None or not os.path.isfile(watchy_pcb):
        KICAD_DIFFERENTIAL = "FAILED (watchy inputs missing)"
        return

    # third-party is READ-ONLY: copy the schematic + its libs to a private
    # writable work dir before kicad-cli goes anywhere near it.
    env_dir = os.environ.get(WORKDIR_ENV)
    owned_tmp: Optional[str] = None
    if env_dir:
        work = os.path.join(env_dir, "watchy")
    else:
        owned_tmp = tempfile.mkdtemp(prefix="selftest_v3_watchy_")
        work = owned_tmp
    os.makedirs(work, exist_ok=True)
    try:
        src_dir = os.path.dirname(root_sch)
        stem = os.path.splitext(os.path.basename(root_sch))[0]
        for name in os.listdir(src_dir):
            full = os.path.join(src_dir, name)
            if not os.path.isfile(full):
                continue
            # the root sheet, any sibling sheets, and the symbol/footprint libs
            if (name.endswith((".kicad_sch", ".kicad_pro", ".lib",
                               ".kicad_sym", ".dcm"))
                    or name == "sym-lib-table"
                    or name.startswith(stem + ".")):
                if name.endswith(".kicad_pcb"):
                    continue                      # not needed for netlisting
                shutil.copy(full, os.path.join(work, name))

        sch_copy = os.path.join(work, os.path.basename(root_sch))
        g_sch = sch_netlist(sch_copy, kicad_cli=cli, work_dir=work)
        R.check(g_sch is not None,
                "kicad-cli netlisted the Watchy schematic (judged by exit "
                "code + non-empty output; stderr ignored)")
        if g_sch is None:
            KICAD_DIFFERENTIAL = "FAILED (netlist export returned None)"
            return

        g_pcb = extract_pcb(watchy_pcb)
        res = compare(g_sch, g_pcb)
        KICAD_DIFFERENTIAL = (
            "verdict=%s component_overlap=%.3f net_partition_similarity=%.3f "
            "shared_nodes=%d (sch: %d comps/%d nets; pcb: %d comps/%d nets)"
            % (res["verdict"], res["component_overlap"],
               res["net_partition_similarity"], res["shared_node_count"],
               len(g_sch.components), len(g_sch.nets),
               len(g_pcb.components), len(g_pcb.nets)))
        print("    %s" % KICAD_DIFFERENTIAL)
        R.check(res["verdict"] == "engines_agree",
                "cross-engine differential verdict == 'engines_agree' "
                "(overlap=%.3f, net-sim=%.3f; Rand-index similarity — "
                "gross-disagreement detector, NOT a wiring gate)"
                % (res["component_overlap"],
                   res["net_partition_similarity"]))

        # re-verify probe 7 ON THE REAL PAIR: dead-short GND into +3V3 in
        # the PCB reading; the sch-vs-pcb compare() must surface the merge
        # as a named_net_merge warning (similarity alone stays high).
        R.check(not any("named_net_merge" in w for w in res["warnings"]),
                "re-verify: the UNSHORTED Watchy pair raises no "
                "named_net_merge warning")
        shorted = schema.from_json(schema.to_json(g_pcb))
        s_gnd = next(n for n in shorted.nets
                     if n.kind == "ground" and n.name.upper().endswith("GND"))
        s_33 = next(n for n in shorted.nets if "3V3" in n.name.upper())
        s_33.nodes += s_gnd.nodes
        shorted.nets.remove(s_gnd)
        sres = compare(g_sch, shorted)
        R.check(any("named_net_merge" in w for w in sres["warnings"]),
                "re-verify: dead-short GND->%s on the REAL Watchy pair "
                "surfaces a named_net_merge warning (sim stays %.3f)"
                % (s_33.name, sres["net_partition_similarity"]))
    finally:
        if owned_tmp is not None:
            shutil.rmtree(owned_tmp, ignore_errors=True)


# ---------------------------------------------------------------------------
# (h) v3 hardening: the 8 adversarially-verified loophole fixes (F1..F8)
# ---------------------------------------------------------------------------

def _copy_graph(g: CanonicalGraph) -> CanonicalGraph:
    return schema.from_json(schema.to_json(g))


def _sat_codes(res) -> set:
    return {v.code for v in res["violations"]}


def test_hardening() -> None:
    from schema_l1 import (Bus, Capability, Demand, Ground, IntentDoc, Rail,
                           Role)

    print("\n(h) v3 hardening: F1..F8 loophole fixes")

    # ---- F1: vacuous intent ------------------------------------------------
    vac = IntentDoc(
        roles=[Role(id="box", kind="enclosure"),
               Role(id="lid", kind="enclosure")],
        rails=[Rail(name="+3V3", volts=3.3)],
        grounds=[Ground(name="GND")])
    R.check("VACUOUS_INTENT" in {v.code for v in check_intent(vac)},
            "F1: zero-demand doc -> intent_oracle emits VACUOUS_INTENT")
    res = satisfies(vac, None)
    R.check(res["verdict"].startswith("FAIL")
            and "VACUOUS_INTENT" in _sat_codes(res),
            "F1: spec_satisfaction refuses PASS on a vacuous doc (got %s)"
            % res["verdict"])
    R.check(all(any(d.demands for d in e["l1"].roles)
                for e in GOLDENS_V3.values()),
            "F1: every corpus golden declares at least one demand "
            "(none is vacuous)")

    # ---- F2: demand traceability (provenance attrs) ------------------------
    for iid, e in GOLDENS_V3.items():
        if e["l2"] is None:
            continue
        doc, g = e["l1"], e["l2"]
        bad = [c.refdes for c in g.components
               if not ((isinstance(c.attrs.get("l1_role"), str)
                        and doc.role(c.attrs["l1_role"]) is not None)
                       or (isinstance(c.attrs.get("for_demand"), str)
                           and (doc.demand(c.attrs["for_demand"]) is not None
                                or doc.role(c.attrs["for_demand"])
                                is not None)))]
        R.check(not bad,
                "F2: #%-2s every L2 component carries resolving provenance "
                "(l1_role / for_demand)%s"
                % (iid, "" if not bad else " -- missing: %s" % bad))

    # the verifier's probe: a rogue second MCU + relay, FULLY WIRED (clean
    # under ERC), must now FAIL with UNDEMANDED_COMPONENT.
    doc5a = GOLDENS_V3["5a"]["l1"]
    rogue = _copy_graph(GOLDENS_V3["5a"]["l2"])
    rogue.components.append(Component(
        refdes="U9", kind="mcu", value="rogue-mcu", authored=True,
        terminals=[Terminal("VDD", "power_in", req_v=3.3),
                   Terminal("GND", "ground")]))
    rogue.components.append(Component(
        refdes="K1", kind="relay", value="rogue-relay", authored=True,
        terminals=[Terminal("COIL_A", "passive"),
                   Terminal("COIL_B", "passive")]))
    for n in rogue.nets:
        if n.name == "+3V3":
            n.nodes += [("U9", "VDD"), ("K1", "COIL_A")]
        elif n.name == "GND":
            n.nodes += [("U9", "GND"), ("K1", "COIL_B")]
    res = satisfies(doc5a, rogue, doc5a.allocation)
    R.check(res["verdict"].startswith("FAIL")
            and "UNDEMANDED_COMPONENT" in _sat_codes(res),
            "F2: rogue second MCU + relay (fully wired) -> "
            "FAIL:UNDEMANDED_COMPONENT (got %s)" % res["verdict"])
    base_res = satisfies(doc5a, GOLDENS_V3["5a"]["l2"], doc5a.allocation)
    R.check(base_res["verdict"] == "PASS",
            "F2: the un-corrupted #5a binding still PASSes")

    orphan = _copy_graph(GOLDENS_V3["5a"]["l2"])
    orphan.components.append(Component(
        refdes="C9", kind="capacitor", value="100nF", authored=False,
        terminals=[Terminal("+", "passive"), Terminal("-", "passive")],
        attrs={"for_demand": "ghost.demand"}))
    for n in orphan.nets:
        if n.name == "+3V3":
            n.nodes.append(("C9", "+"))
        elif n.name == "GND":
            n.nodes.append(("C9", "-"))
    res = satisfies(doc5a, orphan, doc5a.allocation)
    R.check("ORPHAN_GENERATED" in _sat_codes(res),
            "F2: generated part with dangling for_demand -> ORPHAN_GENERATED")

    # ---- F3: typed defaults -------------------------------------------------
    dodge = _copy_doc(GOLDENS_V3["7"]["l1"])           # 5V demand, 3.3V scope
    dodge.demand("dev.pwr").default = "supply"         # non-rail default
    codes = {v.code for v in check_intent(dodge)}
    R.check("DEFAULT_INCOMPATIBLE" in codes,
            "F3: corpus-#7 dodge (5V demand, 3.3V scope, non-rail default) "
            "FAILS AT L1 with DEFAULT_INCOMPATIBLE (codes=%s)"
            % ",".join(sorted(codes)))
    R.check(satisfies(dodge, None)["verdict"].startswith("FAIL"),
            "F3: the dodge cannot PASS through satisfies() either")
    dodge2 = _copy_doc(GOLDENS_V3["7"]["l1"])
    dodge2.demand("dev.pwr").default = "+3V3"          # wrong-voltage RAIL
    R.check("VOLTAGE_MISMATCH" in {v.code for v in check_intent(dodge2)},
            "F3: voltage-incompatible rail default still fires "
            "VOLTAGE_MISMATCH")
    gnd_default = IntentDoc(
        roles=[Role(id="periph", kind="uart_device",
                    demands=[Demand(id="periph.uart", iface="uart",
                                    default="GND")])],
        grounds=[Ground(name="GND")])
    R.check("DEFAULT_INCOMPATIBLE"
            in {v.code for v in check_intent(gnd_default)},
            "F3: default='GND' on a uart demand -> DEFAULT_INCOMPATIBLE")
    R.check(all(v.code != "DEFAULT_INCOMPATIBLE"
                for e in GOLDENS_V3.values()
                for v in check_intent(e["l1"])),
            "F3: corpus defaults (#9 pool + provider role) stay compatible")

    # ---- F4: verdict/demand-map consistency ---------------------------------
    def _consistent(res) -> bool:
        unsat = [d for d, i in res["demands"].items()
                 if i["status"] == "unsatisfied"]
        if res["verdict"] == "PASS":
            return not res["violations"] and not unsat
        if unsat:  # any unsatisfied demand must surface as a FAIL
            return bool(res["violations"]) and res["verdict"].startswith(
                "FAIL")
        return True

    all_res = []
    for e in GOLDENS_V3.values():
        all_res.append(satisfies(e["l1"], None))
        if e["l2"] is not None:
            all_res.append(satisfies(e["l1"], e["l2"], e["record"]))
    for kb in KNOWN_BAD_V3:
        all_res.append(satisfies(kb["l1"], None, kb["record"]))
    R.check(all(_consistent(r) for r in all_res),
            "F4: invariant over corpus + known-bads: PASS <=> zero "
            "violations <=> every demand satisfied-or-escalated")

    # the historical fall-through: a bus-attached demand with NO provider is
    # 'satisfiable' to the L1 oracle but unrealizable in the demand map — it
    # must now surface as UNMET_DEMAND, never a silent PASS.
    fall = IntentDoc(
        roles=[Role(id="dev", kind="sensor",
                    demands=[Demand(id="dev.i2c", iface="i2c",
                                    bus="I2C0")])],
        buses=[Bus(name="I2C0", iface="i2c")])
    R.check(check_intent(fall) == [],
            "F4: the fall-through doc is CLEAN to the L1 oracle alone")
    res = satisfies(fall, None)
    R.check(res["verdict"] == "FAIL:UNMET_DEMAND"
            and res["demands"]["dev.i2c"]["status"] == "unsatisfied",
            "F4: ...but satisfies() emits UNMET_DEMAND — the verdict cannot "
            "be PASS while a demand is unsatisfied (got %s)"
            % res["verdict"])

    # ---- F5: external lock baseline as THE protocol -------------------------
    locked = _copy_doc(GOLDENS_V3["5a"]["l1"])
    fire_sync_point(locked, "firmware-facing")
    baseline = snapshot_locks(locked)               # grader-retained
    R.check(check_allocations(locked, None, record=locked.allocation,
                              baseline=baseline) == [],
            "F5: locked doc + external baseline -> clean")
    codes = {v.code for v in check_allocations(locked, None,
                                               record=locked.allocation)}
    R.check(codes == {"LOCK_UNVERIFIED"},
            "F5: locked doc WITHOUT baseline -> LOCK_UNVERIFIED "
            "(never a silent pass; codes=%s)" % ",".join(sorted(codes)))

    tampered = _copy_doc(locked)
    tampered.allocation.entries[0].unit = 2         # edit the locked decision
    grp = tampered.allocation.lock_groups[0]
    grp.snapshot = covered_entries(tampered.allocation, grp)  # rewrite snap!
    embedded_only = {v.code for v in check_allocations(
        tampered, None, record=tampered.allocation)}
    R.check("LOCK_VIOLATION" not in embedded_only
            and "LOCK_UNVERIFIED" in embedded_only,
            "F5: tamper-entry + rewrite-embedded-snapshot fools the embedded "
            "diff (only LOCK_UNVERIFIED without a baseline: %s)"
            % ",".join(sorted(embedded_only)))
    external = {v.code for v in check_allocations(
        tampered, None, record=tampered.allocation, baseline=baseline)}
    R.check("LOCK_VIOLATION" in external,
            "F5: ...but the EXTERNAL baseline path catches it -> "
            "LOCK_VIOLATION (codes=%s)" % ",".join(sorted(external)))

    # ---- F6: series fork record ---------------------------------------------
    forged = _copy_doc(tampered)
    forged.series = "B"                             # hand-edited, no record
    codes = {v.code for v in check_lock_violations(baseline, forged)}
    R.check(codes == {"SERIES_UNJUSTIFIED"},
            "F6: hand-editing the series string does NOT legalize locked "
            "edits -> SERIES_UNJUSTIFIED (codes=%s)" % ",".join(sorted(codes)))
    forged.forked_from = {"series": "Z", "reason": "invented"}
    R.check("SERIES_UNJUSTIFIED" in
            {v.code for v in check_lock_violations(baseline, forged)},
            "F6: forked_from naming the WRONG parent series is still "
            "SERIES_UNJUSTIFIED")
    legit = _copy_doc(locked)
    break_lock(legit, "firmware-facing", "B")
    legit.allocation.entries[0].unit = 2            # THAT is break_lock
    R.check(legit.forked_from is not None
            and legit.forked_from.get("series") == "A"
            and check_lock_violations(baseline, legit) == []
            and check_allocations(legit, None, record=legit.allocation,
                                  baseline=baseline) == [],
            "F6: break_lock records forked_from naming the baseline series "
            "-> locked edits permitted")

    # ---- F7: differential honesty (named_net_merge on a dead short) ---------
    comps = [Component("R%d" % i, "resistor", "1k", True,
                       [Terminal("1", "passive"), Terminal("2", "passive")])
             for i in range(1, 13)]
    base_nets = [
        Net("+3V3", "power", 3.3, [("R%d" % i, "1") for i in (1, 2, 3)]),
        Net("GND", "ground", 0.0, [("R%d" % i, "2") for i in (1, 2, 3)]),
    ]
    for i in range(4, 13):
        base_nets.append(Net("S%d" % i, "signal", None,
                             [("R%d" % i, "1"), ("R%d" % i, "2")]))
    base_g = CanonicalGraph(components=comps, nets=base_nets)
    shorted = _copy_graph(base_g)
    gnd = next(n for n in shorted.nets if n.name == "GND")
    p33 = next(n for n in shorted.nets if n.name == "+3V3")
    p33.nodes += gnd.nodes                          # merge GND into +3V3
    shorted.nets.remove(gnd)
    res = compare(base_g, shorted)
    R.check(res["verdict"] == "engines_agree"
            and res["net_partition_similarity"] >= 0.9,
            "F7: dead-short probe keeps similarity high (%.3f, verdict %s) "
            "-- the Rand index alone cannot gate wiring"
            % (res["net_partition_similarity"], res["verdict"]))
    R.check(any("named_net_merge" in w for w in res["warnings"]),
            "F7: ...but the merge of differently-named GND/+3V3 surfaces a "
            "named_net_merge warning")
    R.check(compare(base_g, base_g)["warnings"] == [],
            "F7: identical graphs raise no named_net_merge warning")

    # ---- F8: PCB degradation marker -----------------------------------------
    stripped = """
    (kicad_pcb (version 20211014) (generator pcbnew)
      (net 0 "") (net 1 "") (net 2 "") (net 3 "")
      (footprint "Lib:R" (layer "F.Cu")
        (property "Reference" "R1") (property "Value" "1k")
        (pad "1" smd rect (at 0 0) (size 1 1) (layers "F.Cu") (net 1 ""))
        (pad "2" smd rect (at 1 0) (size 1 1) (layers "F.Cu") (net 2 "")))
      (footprint "Lib:C" (layer "F.Cu")
        (property "Reference" "C1") (property "Value" "100n")
        (pad "1" smd rect (at 0 0) (size 1 1) (layers "F.Cu") (net 2 ""))
        (pad "2" smd rect (at 1 0) (size 1 1) (layers "F.Cu") (net 3 ""))))
    """
    gs = extract_pcb_text(stripped)
    R.check(any(e.startswith("PCB_NET_NAMES_MISSING: classification degraded")
                for e in gs.escalations),
            "F8: stripped net names -> explicit "
            "'PCB_NET_NAMES_MISSING: classification degraded' escalation")
    named = stripped.replace('(net 1 "")', '(net 1 "GND")').replace(
        '(net 2 "")', '(net 2 "+3V3")').replace('(net 3 "")', '(net 3 "SDA")')
    R.check(extract_pcb_text(named).escalations == [],
            "F8: the same board WITH names carries no degradation marker")
    if os.path.isfile(MPPT_PCB):
        R.check(not any(e.startswith("PCB_NET_NAMES_MISSING")
                        for e in extract_pcb(MPPT_PCB).escalations),
                "F8: a real named board (mppt-2420-hc) is not flagged")


# ---------------------------------------------------------------------------
# (i) adversarial RE-VERIFICATION probes (independent re-run of the original
#     8 exploits + the new-loophole hunt's regression anchors, F9)
# ---------------------------------------------------------------------------

def test_reverify() -> None:
    from schema_l1 import Escalation, IntentDoc, Role, Demand

    print("\n(i) adversarial re-verification probes (F9)")

    e5a = GOLDENS_V3["5a"]

    # F9a: the bindings-omission dodge is CLOSED. Severing the demanded uart
    # wiring must fail even when the caller supplies NO bindings map — the
    # role->refdes candidates are derived from the mandatory F2 provenance.
    cut = _copy_graph(e5a["l2"])
    cut.nets = [n for n in cut.nets if not n.name.startswith("UART0")]
    res = satisfies(e5a["l1"], cut, e5a["record"])          # no bindings!
    R.check(res["verdict"].startswith("FAIL")
            and "UNMET_DEMAND" in _sat_codes(res)
            and res["demands"]["periph.uart"]["status"] == "unsatisfied",
            "F9a: severed uart wiring, bindings OMITTED -> FAIL:UNMET_DEMAND "
            "via provenance-derived bindings (got %s)" % res["verdict"])
    res = satisfies(e5a["l1"], cut, {"allocation": e5a["record"],
                                     "bindings": {"mcu": "U1",
                                                  "periph": "U2",
                                                  "supply": "PS1"}})
    R.check("UNMET_DEMAND" in _sat_codes(res),
            "F9a: ...and with EXPLICIT bindings likewise")
    R.check(satisfies(e5a["l1"], e5a["l2"], e5a["record"])["verdict"]
            == "PASS",
            "F9a: the intact #5a pair still PASSes without bindings")

    # F9b: an ambiguity attack cannot reopen it — an authored impostor
    # claiming the already-bound 'mcu' role (making the claim multi-valued)
    # plus the severed wiring still fails.
    imp = _copy_graph(e5a["l2"])
    imp.nets = [n for n in imp.nets if not n.name.startswith("UART0")]
    imp.components.append(Component(
        refdes="U77", kind="mcu", value="impostor", authored=True,
        terminals=[Terminal("VDD", "power_in", req_v=3.3),
                   Terminal("GND", "ground")],
        attrs={"l1_role": "mcu"}))
    for n in imp.nets:
        if n.name == "+3V3":
            n.nodes.append(("U77", "VDD"))
        elif n.name == "GND":
            n.nodes.append(("U77", "GND"))
    res = satisfies(e5a["l1"], imp, e5a["record"])
    R.check("UNMET_DEMAND" in _sat_codes(res),
            "F9b: impostor claiming the bound 'mcu' role cannot dodge the "
            "severed-wiring check (got %s)" % res["verdict"])

    # F9c: provenance keys are per authorship class (the contract's words):
    # a GENERATED part carrying only l1_role, and an AUTHORED part carrying
    # only for_demand, are both UNDEMANDED_COMPONENT.
    gen = _copy_graph(e5a["l2"])
    gen.components.append(Component(
        refdes="C77", kind="capacitor", value="100n", authored=False,
        terminals=[Terminal("+", "passive"), Terminal("-", "passive")],
        attrs={"l1_role": "mcu"}))                    # wrong key for class
    for n in gen.nets:
        if n.name == "+3V3":
            n.nodes.append(("C77", "+"))
        elif n.name == "GND":
            n.nodes.append(("C77", "-"))
    res = satisfies(e5a["l1"], gen, e5a["record"])
    R.check("UNDEMANDED_COMPONENT" in _sat_codes(res),
            "F9c: generated part with only l1_role (no for_demand) -> "
            "UNDEMANDED_COMPONENT")
    auth = _copy_graph(e5a["l2"])
    auth.components.append(Component(
        refdes="K77", kind="relay", value="rogue", authored=True,
        terminals=[Terminal("COIL_A", "passive"),
                   Terminal("COIL_B", "passive")],
        attrs={"for_demand": "periph.uart"}))         # real demand, wrong key
    for n in auth.nets:
        if n.name == "+3V3":
            n.nodes.append(("K77", "COIL_A"))
        elif n.name == "GND":
            n.nodes.append(("K77", "COIL_B"))
    res = satisfies(e5a["l1"], auth, e5a["record"])
    R.check("UNDEMANDED_COMPONENT" in _sat_codes(res),
            "F9c: AUTHORED rogue laundering via for_demand naming a real "
            "demand -> UNDEMANDED_COMPONENT")

    # F9d: the FULL lock cover-up (edit a locked entry, roll the group
    # version back to 0, clear snapshot and locked_by tags) is still caught
    # by the external baseline; so is deleting the locked group outright.
    locked = _copy_doc(GOLDENS_V3["5a"]["l1"])
    fire_sync_point(locked, "firmware-facing")
    baseline = snapshot_locks(locked)
    cover = _copy_doc(locked)
    cover.allocation.entries[0].unit = 2
    for en in cover.allocation.entries:
        en.state = "sticky"
        en.locked_by = None
    cover.allocation.lock_groups[0].version = 0
    cover.allocation.lock_groups[0].snapshot = None
    codes = {v.code for v in check_allocations(
        cover, None, record=cover.allocation, baseline=baseline)}
    R.check("LOCK_VIOLATION" in codes,
            "F9d: full cover-up (entry edit + version rollback + cleared "
            "metadata) -> LOCK_VIOLATION vs external baseline (codes=%s)"
            % ",".join(sorted(codes)))
    gone = _copy_doc(locked)
    gone.allocation.entries[0].unit = 2
    gone.allocation.lock_groups = []
    for en in gone.allocation.entries:
        en.state = "sticky"
        en.locked_by = None
    codes = {v.code for v in check_allocations(
        gone, None, record=gone.allocation, baseline=baseline)}
    R.check("LOCK_VIOLATION" in codes,
            "F9d: deleting the locked group outright -> LOCK_VIOLATION "
            "vs external baseline")

    # F9e: an escalation cannot LAUNDER a rung-2 load error — an
    # unsatisfiable demand stays DEMAND_UNSATISFIABLE even when a fully
    # explained escalation covers it (rung 4 is for ambiguity, not for
    # unsatisfiability).
    laun = IntentDoc(
        roles=[Role(id="m", kind="mcu",
                    demands=[Demand(id="m.x", iface="exotic")])],
        escalations=[Escalation(code="X", msg="cover story",
                                subjects=["m.x"], conflict=["a vs b"],
                                relaxation="pick one")])
    res = satisfies(laun, None)
    R.check(res["verdict"].startswith("FAIL")
            and "DEMAND_UNSATISFIABLE" in _sat_codes(res),
            "F9e: explained escalation does NOT launder an unsatisfiable "
            "demand (got %s)" % res["verdict"])

    # F9f: the F8 marker fires on a REAL board in the LEGACY dialect too —
    # strip every net name (quoted AND unquoted) from the MPPT board text.
    if os.path.isfile(MPPT_PCB):
        import re
        with open(MPPT_PCB, "r", encoding="utf-8", errors="replace") as fh:
            mtext = fh.read()
        stripped = re.sub(r'\(net\s+(\d+)\s+"[^"]*"', r'(net \1 ""', mtext)
        stripped = re.sub(r'\(net\s+(\d+)\s+[^\s")][^\s)]*\)',
                          r'(net \1 "")', stripped)
        g_str = extract_pcb_text(stripped)
        R.check(any(e.startswith("PCB_NET_NAMES_MISSING: classification "
                                 "degraded") for e in g_str.escalations),
                "F9f: name-stripped REAL mppt-2420-hc (legacy dialect) -> "
                "PCB_NET_NAMES_MISSING degradation marker")
        R.check(len(g_str.components) == len(extract_pcb(MPPT_PCB).components),
                "F9f: stripping names degrades CLASSIFICATION only — the "
                "component extraction is unchanged")
    else:
        R.skip("mppt board absent -- F9f legacy-dialect strip probe skipped")


# ---------------------------------------------------------------------------
# driver
# ---------------------------------------------------------------------------

def main() -> int:
    print("=" * 70)
    print("harness v3 self-test (additive; v2 must keep passing)")
    print("=" * 70)

    test_v2_regression()
    test_l1_oracle()
    test_spec_satisfaction()
    test_allocation_determinism()
    test_spare_unit()
    test_pcb_extract()
    test_kicad_differential()
    test_hardening()
    test_reverify()

    print("\n" + "=" * 70)
    for label, summary in PCB_SUMMARIES.items():
        print("pcb_extract %-14s %s" % (label + ":", summary))
    print("kicad differential:        %s" % KICAD_DIFFERENTIAL)
    if R.skipped:
        print("skipped (non-failing):     %d" % len(R.skipped))
    if R.failures:
        print("RESULT: FAIL  (%d/%d checks failed)"
              % (len(R.failures), R.checks))
        for f in R.failures:
            print("  - %s" % f)
        print("=" * 70)
        return 1
    print("RESULT: PASS  (all %d checks passed)" % R.checks)
    print("=" * 70)
    return 0


if __name__ == "__main__":
    sys.exit(main())
