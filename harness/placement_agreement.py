"""Placement-agreement oracle: measure a laid-out board against a
``.placement.json`` intent (WyredPlanPlacement step 2.2).

The declare-don't-route half lives in the engine (``wyred``): an author declares
``near`` / ``keepout`` / ``edge`` / ``thermal`` / ``separation`` constraints and
the engine emits ``<name>.placement.json`` (the *asserted* placement intent).
This module is the *verify-from-the-board* half: it reads that artifact back
from disk together with the REALIZED board's geometry (``pcb_geometry``) and
connectivity (``pcb_extract``), computes each constraint's measurement using the
metrics FIXED in ``dev-docs/WyredPlacementSemantics.md`` §3, and reports the
reserved ``PLACEMENT_*`` structured codes with measured-vs-declared values in
every finding. Layout stays delegated forever; conformance becomes mechanical.

Consumes artifacts on disk only -- it imports the two harness EXTRACTORS
(``pcb_geometry``, ``pcb_extract``), never the engine (harness fence: verdicts
are computed from artifacts re-read from disk, never from an engine's in-memory
opinion of itself). Cross-repo composition is subprocess-only: ``run_gate.py``
invokes this file's CLI; ``run_gate`` imports nothing from the harness.

Subject identity (WyredPlacementSemantics §1). A constraint names *role ids*;
the checker joins them to realized *refdes* through a validated mapping. Two
modes were specified: the pin-map ``l1_role`` join for wyred-emitted boards, and
an explicit ``{role_id: [refdes, ...]}`` correspondence table for EXOGENOUS
boards. Everything this plan exercises -- the worked example and the Watchy demo
(step 2.3) -- is an exogenous board, so the correspondence-table path is the one
implemented and tested here; the pin-map join has no consumer in this plan (no
wyred-emitted board is laid out in the demos) and is deliberately left to the
day a laid-out wyred board needs it, rather than shipped against an unbuilt
artifact shape. ``separation`` subjects are net-CLASS names (rail/ground) and
are matched to board copper by net name directly, not through the mapping.

Fail-closed, loudly (the F8 / PCB_NET_NAMES_MISSING discipline): an unresolvable
subject is ``PLACEMENT_SUBJECT_UNRESOLVED``; missing geometry (a named keepout
area absent from the board, no board outline) is ``PLACEMENT_GEOMETRY_MISSING``.
Both are gate-red exactly like a per-kind violation -- never a silent skip.

Metrics (WyredPlacementSemantics §3), all in mm in the board xy plane, side
ignored in v0:
  * ``near``       courtyard-to-courtyard nearest-EDGE gap (0.0 when the two
                   courtyards touch/overlap), all cross pairs, worst must hold;
                   a fallback pad-bbox courtyard is measured and flagged.
  * ``edge``       courtyard nearest-edge distance to the board-outline bounding
                   -box side (KiCad y grows down: north=min-y, south=max-y,
                   west=min-x, east=max-x) <= ``tol_mm`` (default 1.0).
  * ``keepout``    no listed role's courtyard may intersect the board's named
                   keepout/rule area (exact-name match; unnamed area is
                   unverifiable -> fail closed).
  * ``thermal``    sum of FILLED-zone area on the net of the subject's
                   ``power_out`` pads (``power_in`` when none), on those pads'
                   copper layers only, must be >= ``copper_mm2``.
  * ``separation`` minimum straight-line copper-to-copper distance between the
                   two net classes' pads+zones (v0 = clearance over pads+zones;
                   tracks excluded; the scope is CONFESSED in every result row)
                   must be >= ``min_mm``.

Plain imports, flat harness directory:
    from placement_agreement import check_placement, check_placement_board
Pure Python 3.10 stdlib. ``python3 placement_agreement.py`` runs the self-test.
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence, Tuple

import pcb_extract
from pcb_geometry import (Footprint, PcbGeometry, Point, extract_geometry,
                          extract_geometry_text, _bbox, _point_in_ring)

EPS = 1e-6

# --- reserved result codes (WyredPlacementSemantics §8) ----------------------
NEAR_VIOLATION = "PLACEMENT_NEAR_VIOLATION"
KEEPOUT_VIOLATION = "PLACEMENT_KEEPOUT_VIOLATION"
EDGE_VIOLATION = "PLACEMENT_EDGE_VIOLATION"
THERMAL_VIOLATION = "PLACEMENT_THERMAL_VIOLATION"
SEPARATION_VIOLATION = "PLACEMENT_SEPARATION_VIOLATION"
SUBJECT_UNRESOLVED = "PLACEMENT_SUBJECT_UNRESOLVED"
GEOMETRY_MISSING = "PLACEMENT_GEOMETRY_MISSING"

_SIDES = frozenset({"north", "south", "east", "west"})


# ---------------------------------------------------------------------------
# Geometry distance primitives (polygon-to-polygon, over rings of Points)
# ---------------------------------------------------------------------------

def _point_seg_dist(p: Point, a: Point, b: Point) -> float:
    """Distance from point ``p`` to segment ``a``->``b``."""
    ax, ay = a
    bx, by = b
    dx, dy = bx - ax, by - ay
    if dx == 0.0 and dy == 0.0:
        return math.hypot(p[0] - ax, p[1] - ay)
    t = ((p[0] - ax) * dx + (p[1] - ay) * dy) / (dx * dx + dy * dy)
    t = max(0.0, min(1.0, t))
    cx, cy = ax + t * dx, ay + t * dy
    return math.hypot(p[0] - cx, p[1] - cy)


def _orient(a: Point, b: Point, c: Point) -> float:
    return (b[0] - a[0]) * (c[1] - a[1]) - (b[1] - a[1]) * (c[0] - a[0])


def _on_seg(a: Point, b: Point, c: Point) -> bool:
    """``c`` (assumed collinear with a-b) lies within the a-b bounding box."""
    return (min(a[0], b[0]) - EPS <= c[0] <= max(a[0], b[0]) + EPS
            and min(a[1], b[1]) - EPS <= c[1] <= max(a[1], b[1]) + EPS)


def _seg_intersect(p1: Point, p2: Point, p3: Point, p4: Point) -> bool:
    """Do segments p1-p2 and p3-p4 intersect (proper or endpoint-touching)?"""
    d1 = _orient(p3, p4, p1)
    d2 = _orient(p3, p4, p2)
    d3 = _orient(p1, p2, p3)
    d4 = _orient(p1, p2, p4)
    if ((d1 > EPS) != (d2 > EPS)) and ((d3 > EPS) != (d4 > EPS)) \
            and ((d1 < -EPS) != (d2 < -EPS)) and ((d3 < -EPS) != (d4 < -EPS)):
        return True
    if abs(d1) <= EPS and _on_seg(p3, p4, p1):
        return True
    if abs(d2) <= EPS and _on_seg(p3, p4, p2):
        return True
    if abs(d3) <= EPS and _on_seg(p1, p2, p3):
        return True
    if abs(d4) <= EPS and _on_seg(p1, p2, p4):
        return True
    return False


def _seg_seg_dist(a1: Point, a2: Point, b1: Point, b2: Point) -> float:
    if _seg_intersect(a1, a2, b1, b2):
        return 0.0
    return min(_point_seg_dist(a1, b1, b2), _point_seg_dist(a2, b1, b2),
               _point_seg_dist(b1, a1, a2), _point_seg_dist(b2, a1, a2))


def _ring_edges(ring: Sequence[Point]):
    n = len(ring)
    for i in range(n):
        yield ring[i], ring[(i + 1) % n]


def _rings_overlap(a: List[List[Point]], b: List[List[Point]]) -> bool:
    """True when the two polygon SETS overlap (one contains a vertex of the
    other) -- containment the edge-distance loop alone would miss."""
    for ring in a:
        for pt in ring:
            if any(_point_in_ring(pt, rb) for rb in b):
                return True
    for ring in b:
        for pt in ring:
            if any(_point_in_ring(pt, ra) for ra in a):
                return True
    return False


def _rings_min_distance(a: List[List[Point]], b: List[List[Point]]) -> float:
    """Minimum gap between two polygon sets; 0.0 when they touch/overlap."""
    if _rings_overlap(a, b):
        return 0.0
    best = math.inf
    for ra in a:
        for a1, a2 in _ring_edges(ra):
            for rb in b:
                for b1, b2 in _ring_edges(rb):
                    d = _seg_seg_dist(a1, a2, b1, b2)
                    if d < best:
                        best = d
                        if best <= EPS:
                            return 0.0
    return best if best != math.inf else math.inf


def _rings_intersect(a: List[List[Point]], b: List[List[Point]]) -> bool:
    return _rings_min_distance(a, b) <= EPS


def _fp_rings(fp: Footprint) -> List[List[Point]]:
    """A footprint's courtyard rings (real or the flagged pad-bbox fallback);
    a last-ditch bbox ring if even that is empty."""
    rings = [r for r in fp.courtyard if len(r) >= 3]
    if rings:
        return rings
    bb = fp.bbox()
    if bb is None:
        return []
    x0, y0, x1, y1 = bb
    return [[(x0, y0), (x1, y0), (x1, y1), (x0, y1)]]


def _edge_distance(rings: List[List[Point]],
                   board_bbox: Tuple[float, float, float, float],
                   side: str) -> float:
    """Nearest-edge distance from a courtyard to a named board-bbox side."""
    pts = [p for r in rings for p in r]
    bb = _bbox(pts)
    if bb is None:
        return math.inf
    x0, y0, x1, y1 = bb
    bx0, by0, bx1, by1 = board_bbox
    if side == "north":
        return y0 - by0
    if side == "south":
        return by1 - y1
    if side == "west":
        return x0 - bx0
    if side == "east":
        return bx1 - x1
    return math.inf


def _copper_layer_match(a: List[str], b: List[str]) -> bool:
    """Do two layer lists share a copper layer? ``*.Cu`` is a wildcard over
    every copper layer."""
    for la in a:
        for lb in b:
            if la == lb:
                return True
            if la.endswith(".Cu") and lb.endswith(".Cu") \
                    and (la.startswith("*") or lb.startswith("*")):
                return True
    return False


# ---------------------------------------------------------------------------
# Result shape (WyredPlacementSemantics §6)
# ---------------------------------------------------------------------------

@dataclass
class ConstraintResult:
    id: str
    kind: str
    verdict: str                       # PASS | FAIL | UNRESOLVED
    code: Optional[str]                # a PLACEMENT_* code, or None on PASS
    declared: Dict[str, object] = field(default_factory=dict)
    measured: Dict[str, object] = field(default_factory=dict)
    flags: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, object]:
        return {"id": self.id, "kind": self.kind, "verdict": self.verdict,
                "code": self.code, "declared": self.declared,
                "measured": self.measured, "flags": self.flags}


def _unresolved(c: dict, reason: str,
                code: str = SUBJECT_UNRESOLVED) -> ConstraintResult:
    return ConstraintResult(c.get("id", "?"), c.get("kind", "?"), "UNRESOLVED",
                            code, dict(c.get("params", {})), {"reason": reason})


def _geometry_missing(c: dict, reason: str) -> ConstraintResult:
    return ConstraintResult(c.get("id", "?"), c.get("kind", "?"), "FAIL",
                            GEOMETRY_MISSING, dict(c.get("params", {})),
                            {"reason": reason})


# ---------------------------------------------------------------------------
# Subject resolution (correspondence table -> refdes -> Footprint)
# ---------------------------------------------------------------------------

def _resolve(role: str, mapping: Dict[str, List[str]],
             geo: PcbGeometry) -> Tuple[Optional[List[Footprint]], str]:
    """Resolve a role id to its board footprints, or (None, reason).

    Empty match, an unmapped role, or a mapped refdes absent from the extract
    all fail closed (WyredPlacementSemantics §1: no fuzzy matching, ever)."""
    refs = mapping.get(role)
    if not refs:
        return None, "role %r has no correspondence entry (empty match)" % role
    fps: List[Footprint] = []
    for rd in refs:
        fp = geo.footprint(rd)
        if fp is None:
            return None, ("refdes %r (role %r) is absent from the board extract"
                          % (rd, role))
        fps.append(fp)
    return fps, ""


def _power_nets(refdes: str, graph) -> Tuple[set, Optional[str]]:
    """Nets carrying the component's ``power_out`` pads (``power_in`` when it
    has none), from the CONNECTIVITY extract's terminal roles."""
    if graph is None:
        return set(), None
    comp = next((c for c in graph.components if c.refdes == refdes), None)
    if comp is None:
        return set(), None
    outs = {t.name for t in comp.terminals if t.role == "power_out"}
    ins = {t.name for t in comp.terminals if t.role == "power_in"}
    terms = outs or ins
    which = "power_out" if outs else ("power_in" if ins else None)
    if not terms:
        return set(), None
    nets: set = set()
    for net in graph.nets:
        for (r, tname) in net.nodes:
            if r == refdes and tname in terms:
                nets.add(net.name)
    return nets, which


# ---------------------------------------------------------------------------
# Per-kind metrics
# ---------------------------------------------------------------------------

def _check_near(c: dict, geo: PcbGeometry,
                mapping: Dict[str, List[str]]) -> ConstraintResult:
    subs = c.get("subjects", [])
    if len(subs) != 2:
        return _unresolved(c, "near expects exactly 2 subjects, got %d"
                           % len(subs))
    max_mm = float(c["params"]["max_mm"])
    a_fps, err = _resolve(subs[0], mapping, geo)
    if err:
        return _unresolved(c, err)
    b_fps, err = _resolve(subs[1], mapping, geo)
    if err:
        return _unresolved(c, err)
    pairs: List[dict] = []
    worst = 0.0
    fallback = False
    for fa in a_fps:
        for fb in b_fps:
            gap = _rings_min_distance(_fp_rings(fa), _fp_rings(fb))
            pairs.append({"a": fa.refdes, "b": fb.refdes,
                          "gap_mm": round(gap, 4)})
            worst = max(worst, gap)
            fallback = fallback or fa.courtyard_fallback or fb.courtyard_fallback
    ok = worst <= max_mm + EPS
    flags = ["metric_fallback:pad_bbox"] if fallback else []
    return ConstraintResult(
        c["id"], "near", "PASS" if ok else "FAIL",
        None if ok else NEAR_VIOLATION, {"max_mm": max_mm},
        {"gap_mm": round(worst, 4), "per_pair": pairs}, flags)


def _check_edge(c: dict, geo: PcbGeometry,
                mapping: Dict[str, List[str]]) -> ConstraintResult:
    side = c["params"].get("side", "")
    tol = float(c["params"].get("tol_mm", 1.0))
    if side not in _SIDES:
        return _unresolved(c, "unknown edge side %r (want north/south/east/west)"
                           % side, code=EDGE_VIOLATION)
    if geo.outline.bbox is None:
        return _geometry_missing(c, "board has no Edge.Cuts outline bounding box")
    subs = c.get("subjects", [])
    if len(subs) != 1:
        return _unresolved(c, "edge expects exactly 1 subject, got %d"
                           % len(subs))
    fps, err = _resolve(subs[0], mapping, geo)
    if err:
        return _unresolved(c, err)
    per: List[dict] = []
    worst = 0.0
    fallback = False
    for f in fps:
        d = _edge_distance(_fp_rings(f), geo.outline.bbox, side)
        per.append({"refdes": f.refdes, "distance_mm": round(d, 4)})
        worst = max(worst, d)
        fallback = fallback or f.courtyard_fallback
    ok = worst <= tol + EPS
    flags = ["metric_fallback:pad_bbox"] if fallback else []
    return ConstraintResult(
        c["id"], "edge", "PASS" if ok else "FAIL",
        None if ok else EDGE_VIOLATION, {"side": side, "tol_mm": tol},
        {"distance_mm": round(worst, 4), "side": side, "per_subject": per},
        flags)


def _check_keepout(c: dict, geo: PcbGeometry,
                   mapping: Dict[str, List[str]]) -> ConstraintResult:
    zone_name = c["params"]["zone"]
    zones = [z for z in geo.zones if z.is_keepout and z.name == zone_name]
    if not zones:
        return _geometry_missing(
            c, "no named keepout/rule area %r on the realized board" % zone_name)
    per: List[dict] = []
    violated = False
    fallback = False
    for role in c.get("subjects", []):
        fps, err = _resolve(role, mapping, geo)
        if err:
            return _unresolved(c, err)
        for f in fps:
            hit = any(_rings_intersect(_fp_rings(f), [z.outline])
                      for z in zones if z.outline)
            per.append({"refdes": f.refdes, "role": role, "intersects": hit})
            violated = violated or hit
            fallback = fallback or f.courtyard_fallback
    ok = not violated
    flags = ["metric_fallback:pad_bbox"] if fallback else []
    return ConstraintResult(
        c["id"], "keepout", "PASS" if ok else "FAIL",
        None if ok else KEEPOUT_VIOLATION, {"zone": zone_name},
        {"zone": zone_name, "per_role": per}, flags)


def _check_thermal(c: dict, geo: PcbGeometry, mapping: Dict[str, List[str]],
                   graph) -> ConstraintResult:
    subs = c.get("subjects", [])
    if len(subs) != 1:
        return _unresolved(c, "thermal expects exactly 1 subject, got %d"
                           % len(subs))
    need = float(c["params"]["copper_mm2"])
    fps, err = _resolve(subs[0], mapping, geo)
    if err:
        return _unresolved(c, err)
    power_nets: set = set()
    which: Optional[str] = None
    for f in fps:
        nets, w = _power_nets(f.refdes, graph)
        power_nets |= nets
        which = which or w
    if not power_nets:
        return _unresolved(
            c, "subject %r has no power_out/power_in pads (no power net to "
               "count copper on)" % subs[0])
    pad_layers: set = set()
    for f in fps:
        for pad in f.pads:
            if pad.net_name in power_nets:
                pad_layers.update(pad.layers)
    area = 0.0
    counted: List[str] = []
    for z in geo.zones:
        if z.net_name in power_nets and _copper_layer_match(
                z.layers, list(pad_layers)):
            za = z.filled_area() or z.area()
            area += za
            counted.append(z.net_name)
    ok = area >= need - EPS
    return ConstraintResult(
        c["id"], "thermal", "PASS" if ok else "FAIL",
        None if ok else THERMAL_VIOLATION, {"copper_mm2": need},
        {"area_mm2": round(area, 4), "power_role": which,
         "nets": sorted(power_nets), "layers": sorted(pad_layers)}, [])


def _copper_shapes(geo: PcbGeometry, net: str) -> List[List[Point]]:
    """Every pad + zone copper ring on ``net`` (v0 separation scope)."""
    rings: List[List[Point]] = []
    for f in geo.footprints.values():
        for pad in f.pads:
            if pad.net_name == net:
                rings.append(pad.corners())
    for z in geo.zones:
        if z.net_name == net:
            if z.filled:
                rings.extend(r for (_layer, r) in z.filled if len(r) >= 3)
            elif len(z.outline) >= 3:
                rings.append(z.outline)
    return rings


def _check_separation(c: dict, geo: PcbGeometry) -> ConstraintResult:
    subs = c.get("subjects", [])
    if len(subs) != 2:
        return _unresolved(c, "separation expects exactly 2 subjects, got %d"
                           % len(subs))
    class_a, class_b = subs[0], subs[1]
    need = float(c["params"]["min_mm"])
    a = _copper_shapes(geo, class_a)
    b = _copper_shapes(geo, class_b)
    if not a:
        return _unresolved(c, "net class %r has no copper (pads/zones) on the "
                           "board" % class_a)
    if not b:
        return _unresolved(c, "net class %r has no copper (pads/zones) on the "
                           "board" % class_b)
    d = _rings_min_distance(a, b)
    ok = d >= need - EPS
    return ConstraintResult(
        c["id"], "separation", "PASS" if ok else "FAIL",
        None if ok else SEPARATION_VIOLATION, {"min_mm": need},
        {"clearance_mm": round(d, 4)}, ["copper_scope:pads+zones"])


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def check_placement(artifact: dict, geometry: PcbGeometry,
                    mapping: Dict[str, List[str]],
                    connectivity=None) -> List[ConstraintResult]:
    """Check every constraint in a loaded ``.placement.json`` against the
    realized board geometry (+ connectivity for the thermal power-net join)."""
    results: List[ConstraintResult] = []
    for c in artifact.get("constraints", []):
        kind = c.get("kind")
        if kind == "near":
            r = _check_near(c, geometry, mapping)
        elif kind == "edge":
            r = _check_edge(c, geometry, mapping)
        elif kind == "keepout":
            r = _check_keepout(c, geometry, mapping)
        elif kind == "thermal":
            r = _check_thermal(c, geometry, mapping, connectivity)
        elif kind == "separation":
            r = _check_separation(c, geometry)
        else:
            r = _unresolved(c, "unknown constraint kind %r" % kind)
        results.append(r)
    return results


def check_placement_board(artifact_path: str, board_path: str,
                          mapping: Dict[str, List[str]]
                          ) -> List[ConstraintResult]:
    """Load an artifact + a board file (geometry AND connectivity) and check."""
    with open(artifact_path, "r", encoding="utf-8") as fh:
        artifact = json.load(fh)
    geo = extract_geometry(board_path)
    conn = pcb_extract.extract_pcb(board_path)
    return check_placement(artifact, geo, mapping, conn)


def summarize(results: List[ConstraintResult]) -> Tuple[int, int, List[str]]:
    """(#holding, #total, [codes that fired]) over a result list."""
    codes = [r.code for r in results if r.verdict != "PASS" and r.code]
    holding = sum(1 for r in results if r.verdict == "PASS")
    return holding, len(results), codes


# ---------------------------------------------------------------------------
# CLI (run_gate.py and the step-2.3 Watchy probe invoke this)
# ---------------------------------------------------------------------------

def _print_results(name: str, results: List[ConstraintResult]) -> int:
    for r in results:
        mark = {"PASS": "ok", "FAIL": "FAIL",
                "UNRESOLVED": "UNRESOLVED"}[r.verdict]
        detail = "; ".join("%s=%s" % (k, v) for k, v in r.measured.items()
                           if k not in ("per_pair", "per_subject", "per_role"))
        line = "  [%s] %s (%s) declared=%s  %s" % (
            mark, r.id, r.kind, r.declared, detail)
        if r.code:
            line += "  code=%s" % r.code
        if r.flags:
            line += "  flags=%s" % ",".join(r.flags)
        print(line)
    holding, total, codes = summarize(results)
    if holding == total:
        print("placement_agreement: PASS -- %d/%d constraints hold against %s"
              % (holding, total, name))
        return 0
    print("placement_agreement: FAIL -- %d/%d constraints hold against %s "
          "(codes fired: %s)" % (holding, total, name,
                                 ", ".join(sorted(set(codes))) or "none"))
    return 1


def main(argv: Optional[Sequence[str]] = None) -> int:
    ap = argparse.ArgumentParser(
        prog="placement_agreement", description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--placement", metavar="ARTIFACT",
                    help="path to a <name>.placement.json artifact")
    ap.add_argument("--board", metavar="KICAD_PCB",
                    help="the realized .kicad_pcb board to verify against")
    ap.add_argument("--correspondence", metavar="JSON",
                    help="explicit {role_id: [refdes, ...]} table (exogenous "
                         "boards, WyredPlacementSemantics §1)")
    ap.add_argument("--selftest", action="store_true",
                    help="run the built-in fixture-board unit probes")
    args = ap.parse_args(argv)

    if args.selftest or not args.placement:
        return _selftest()

    if not args.board:
        ap.error("--board is required with --placement")
    mapping: Dict[str, List[str]] = {}
    if args.correspondence:
        with open(args.correspondence, "r", encoding="utf-8") as fh:
            mapping = json.load(fh)
    results = check_placement_board(args.placement, args.board, mapping)
    return _print_results(args.board, results)


# ---------------------------------------------------------------------------
# Self-test: one satisfied + one violated fixture board per constraint class,
# every PLACEMENT_* code demonstrated firing (the all-codes-must-fire discipline
# the harness runs everywhere), plus the WyredPlacementSemantics §5 worked
# example verified against a board that satisfies it.
# ---------------------------------------------------------------------------

def _crtyd(hw: float, hh: float) -> str:
    """Four F.CrtYd fp_lines forming a footprint-LOCAL rectangle of half-extent
    (hw,hh) about the origin -- the extractor adds the footprint ``(at ..)``."""
    x0, y0, x1, y1 = -hw, -hh, hw, hh
    seg = ('        (fp_line (start {ax} {ay}) (end {bx} {by}) '
           '(stroke (width 0.05)) (layer "F.CrtYd"))\n')
    return (seg.format(ax=x0, ay=y0, bx=x1, by=y0)
            + seg.format(ax=x1, ay=y0, bx=x1, by=y1)
            + seg.format(ax=x1, ay=y1, bx=x0, by=y1)
            + seg.format(ax=x0, ay=y1, bx=x0, by=y0))


_OUTLINE = (
    '  (gr_line (start 0 0) (end 40 0) (stroke (width 0.15)) (layer "Edge.Cuts"))\n'
    '  (gr_line (start 40 0) (end 40 30) (stroke (width 0.15)) (layer "Edge.Cuts"))\n'
    '  (gr_line (start 40 30) (end 0 30) (stroke (width 0.15)) (layer "Edge.Cuts"))\n'
    '  (gr_line (start 0 30) (end 0 0) (stroke (width 0.15)) (layer "Edge.Cuts"))\n')


def _board(body: str, outline: bool = True) -> PcbGeometry:
    text = ('(kicad_pcb (version 20221018) (generator pcbnew)\n'
            + (_OUTLINE if outline else "") + body + ")\n")
    return extract_geometry_text(text)


def _fp(ref: str, cx: float, cy: float, hw: float, hh: float,
        pads: str = "") -> str:
    return ('  (footprint "Lib:%s" (layer "F.Cu")\n'
            '    (at %s %s)\n'
            '    (property "Reference" "%s")\n'
            '%s%s)\n' % ("FP", cx, cy, ref, _crtyd(hw, hh), pads))


# artifact builders (mirror the emitted <name>.placement.json shape) ----------

def _stamp() -> dict:
    return {"series": "A", "locks": {}}


def _art(name: str, constraints: List[dict]) -> dict:
    return {"artifact": name, "path": "placement", "stamp": _stamp(),
            "constraints": constraints}


def run_placement_smoke(check) -> None:
    fired: set = set()

    def note(res: ConstraintResult) -> None:
        if res.code:
            fired.add(res.code)

    # -- WyredPlacementSemantics §5 worked example ---------------------------
    # UsbAtEdge: near("pmu","mcu",max_mm=8.0) + edge("usb",side="south").
    print("worked example (WyredPlacementSemantics §5):")
    worked = _art("placement_example", [
        {"id": "near_pmu_mcu", "kind": "near",
         "declared_by": {"module": "UsbAtEdge", "instance": ""},
         "subjects": ["pmu", "mcu"], "params": {"max_mm": 8.0}},
        {"id": "usb_edge", "kind": "edge",
         "declared_by": {"module": "UsbAtEdge", "instance": ""},
         "subjects": ["usb"], "params": {"side": "south", "tol_mm": 1.0}},
    ])
    # J1 (usb) courtyard bbox y 28..30 -> south dist 0; U2 (pmu) 8..12, U3
    # (mcu) 13..17 -> gap 1mm <= 8.
    geo = _board(_fp("J1", 20, 29, 3, 1) + _fp("U2", 10, 10, 2, 2)
                 + _fp("U3", 15, 10, 2, 2))
    res = check_placement(worked, geo,
                          {"usb": ["J1"], "pmu": ["U2"], "mcu": ["U3"]})
    for r in res:
        note(r)
    check("worked example: both constraints PASS",
          all(r.verdict == "PASS" for r in res),
          "; ".join("%s=%s" % (r.id, r.verdict) for r in res))
    check("worked example: usb edge south distance ~0",
          any(r.id == "usb_edge"
              and r.measured["distance_mm"] <= 1.0 for r in res))

    # -- near ---------------------------------------------------------------
    print("near:")
    near_art = _art("near", [{"id": "ab", "kind": "near",
                              "subjects": ["a", "b"],
                              "params": {"max_mm": 8.0}}])
    m = {"a": ["U1"], "b": ["U2"]}
    ok = _board(_fp("U1", 10, 10, 2, 2) + _fp("U2", 16, 10, 2, 2))
    r = check_placement(near_art, ok, m)[0]
    note(r)
    check("near satisfied: PASS (gap 2.0 <= 8.0)",
          r.verdict == "PASS" and abs(r.measured["gap_mm"] - 2.0) < 1e-6,
          "verdict=%s measured=%s" % (r.verdict, r.measured))
    bad = _board(_fp("U1", 10, 10, 2, 2) + _fp("U2", 30, 10, 2, 2))
    r = check_placement(near_art, bad, m)[0]
    note(r)
    check("near violated: FAIL + PLACEMENT_NEAR_VIOLATION (gap 16.0 > 8.0)",
          r.verdict == "FAIL" and r.code == NEAR_VIOLATION
          and abs(r.measured["gap_mm"] - 16.0) < 1e-6,
          "verdict=%s code=%s measured=%s" % (r.verdict, r.code, r.measured))

    # -- edge ---------------------------------------------------------------
    print("edge:")
    edge_art = _art("edge", [{"id": "j_edge", "kind": "edge",
                              "subjects": ["j"],
                              "params": {"side": "south", "tol_mm": 1.0}}])
    m = {"j": ["J1"]}
    ok = _board(_fp("J1", 20, 29, 3, 1))       # bbox y 28..30 -> south dist 0
    r = check_placement(edge_art, ok, m)[0]
    note(r)
    check("edge satisfied: PASS (south distance 0 <= 1.0)",
          r.verdict == "PASS" and r.measured["distance_mm"] <= 1.0,
          "verdict=%s measured=%s" % (r.verdict, r.measured))
    bad = _board(_fp("J1", 20, 15, 3, 1))      # bbox y 14..16 -> south dist 14
    r = check_placement(edge_art, bad, m)[0]
    note(r)
    check("edge violated: FAIL + PLACEMENT_EDGE_VIOLATION (distance 14 > 1)",
          r.verdict == "FAIL" and r.code == EDGE_VIOLATION,
          "verdict=%s code=%s measured=%s" % (r.verdict, r.code, r.measured))

    # -- keepout ------------------------------------------------------------
    print("keepout:")
    ko_zone = ('  (zone (net 0) (net_name "") (layers "F.Cu") (name "ant_ko")\n'
               '    (keepout (tracks allowed) (vias allowed) (pads allowed)\n'
               '             (copperpour not_allowed) (footprints not_allowed))\n'
               '    (polygon (pts (xy 30 2) (xy 38 2) (xy 38 8) (xy 30 8))))\n')
    ko_art = _art("keepout", [{"id": "ant", "kind": "keepout",
                               "subjects": ["rf"],
                               "params": {"zone": "ant_ko"}}])
    m = {"rf": ["U1"]}
    ok = _board(_fp("U1", 10, 10, 1.5, 1.5) + ko_zone)   # far from the area
    r = check_placement(ko_art, ok, m)[0]
    note(r)
    check("keepout satisfied: PASS (courtyard clear of the area)",
          r.verdict == "PASS", "verdict=%s measured=%s" % (r.verdict,
                                                           r.measured))
    bad = _board(_fp("U1", 34, 5, 1.5, 1.5) + ko_zone)   # courtyard inside area
    r = check_placement(ko_art, bad, m)[0]
    note(r)
    check("keepout violated: FAIL + PLACEMENT_KEEPOUT_VIOLATION",
          r.verdict == "FAIL" and r.code == KEEPOUT_VIOLATION,
          "verdict=%s code=%s" % (r.verdict, r.code))
    missing = _board(_fp("U1", 34, 5, 1.5, 1.5))          # no keepout on board
    r = check_placement(ko_art, missing, m)[0]
    note(r)
    check("keepout geometry-missing: FAIL + PLACEMENT_GEOMETRY_MISSING "
          "(named area absent, fail closed)",
          r.verdict == "FAIL" and r.code == GEOMETRY_MISSING,
          "verdict=%s code=%s" % (r.verdict, r.code))

    # -- thermal ------------------------------------------------------------
    print("thermal:")
    # A regulator U1 with a power_out pad on VREG_OUT (F.Cu) + a filled zone on
    # VREG_OUT. Connectivity (pcb_extract) reads the pintype -> power_out role.
    reg_pads = ('    (pad "1" smd rect (at -2 0) (size 1 1) (layers "F.Cu")\n'
                '      (net 1 "+3V3") (pintype "power_in"))\n'
                '    (pad "2" smd rect (at 2 0) (size 1 1) (layers "F.Cu")\n'
                '      (net 3 "VREG_OUT") (pintype "power_out"))\n')
    thermal_art = _art("thermal", [{"id": "reg", "kind": "thermal",
                                    "subjects": ["reg"],
                                    "params": {"copper_mm2": 150.0}}])
    m = {"reg": ["U1"]}
    big_zone = ('  (zone (net 3) (net_name "VREG_OUT") (layers "F.Cu")\n'
                '    (polygon (pts (xy 3 3) (xy 18 3) (xy 18 15) (xy 3 15)))\n'
                '    (filled_polygon (layer "F.Cu")\n'
                '      (pts (xy 3 3) (xy 18 3) (xy 18 15) (xy 3 15))))\n')  # 180
    small_zone = ('  (zone (net 3) (net_name "VREG_OUT") (layers "F.Cu")\n'
                  '    (polygon (pts (xy 3 3) (xy 13 3) (xy 13 13) (xy 3 13)))\n'
                  '    (filled_polygon (layer "F.Cu")\n'
                  '      (pts (xy 3 3) (xy 13 3) (xy 13 13) (xy 3 13))))\n')  # 100
    ok = _board(_fp("U1", 10, 10, 2, 2, reg_pads) + big_zone)
    conn = pcb_extract.extract_pcb_text(
        '(kicad_pcb (version 20221018)\n'
        + _fp("U1", 10, 10, 2, 2, reg_pads) + big_zone + ")")
    r = check_placement(thermal_art, ok, m, conn)[0]
    note(r)
    check("thermal satisfied: PASS (copper 180 >= 150 on VREG_OUT/power_out)",
          r.verdict == "PASS" and abs(r.measured["area_mm2"] - 180.0) < 1e-6
          and r.measured["power_role"] == "power_out",
          "verdict=%s measured=%s" % (r.verdict, r.measured))
    bad = _board(_fp("U1", 10, 10, 2, 2, reg_pads) + small_zone)
    conn_bad = pcb_extract.extract_pcb_text(
        '(kicad_pcb (version 20221018)\n'
        + _fp("U1", 10, 10, 2, 2, reg_pads) + small_zone + ")")
    r = check_placement(thermal_art, bad, m, conn_bad)[0]
    note(r)
    check("thermal violated: FAIL + PLACEMENT_THERMAL_VIOLATION (100 < 150)",
          r.verdict == "FAIL" and r.code == THERMAL_VIOLATION
          and abs(r.measured["area_mm2"] - 100.0) < 1e-6,
          "verdict=%s code=%s measured=%s" % (r.verdict, r.code, r.measured))
    # subject with no power pads -> fail-closed unresolved
    plain_pads = ('    (pad "1" smd rect (at -2 0) (size 1 1) (layers "F.Cu")\n'
                  '      (net 5 "NETA"))\n'
                  '    (pad "2" smd rect (at 2 0) (size 1 1) (layers "F.Cu")\n'
                  '      (net 6 "NETB"))\n')
    nopow = _board(_fp("U9", 10, 10, 2, 2, plain_pads))
    conn_np = pcb_extract.extract_pcb_text(
        '(kicad_pcb (version 20221018)\n'
        + _fp("U9", 10, 10, 2, 2, plain_pads) + ")")
    r = check_placement(thermal_art, nopow, {"reg": ["U9"]}, conn_np)[0]
    note(r)
    check("thermal no-power-pads: UNRESOLVED + PLACEMENT_SUBJECT_UNRESOLVED",
          r.verdict == "UNRESOLVED" and r.code == SUBJECT_UNRESOLVED,
          "verdict=%s code=%s" % (r.verdict, r.code))

    # -- separation ---------------------------------------------------------
    print("separation:")
    sep_pads_ok = ('    (pad "1" smd rect (at -5 0) (size 2 2) (layers "F.Cu")\n'
                   '      (net 1 "HV"))\n'
                   '    (pad "2" smd rect (at 5 0) (size 2 2) (layers "F.Cu")\n'
                   '      (net 2 "GND"))\n')      # HV corners x -6..-4, GND 4..6
    sep_art = _art("separation", [{"id": "hv_gnd", "kind": "separation",
                                   "subjects": ["HV", "GND"],
                                   "params": {"min_mm": 6.0}}])
    ok = _board(_fp("U1", 15, 10, 8, 3, sep_pads_ok))   # clearance 8 >= 6
    r = check_placement(sep_art, ok, {})[0]
    note(r)
    check("separation satisfied: PASS (clearance 8.0 >= 6.0)",
          r.verdict == "PASS" and abs(r.measured["clearance_mm"] - 8.0) < 1e-6
          and "copper_scope:pads+zones" in r.flags,
          "verdict=%s measured=%s flags=%s" % (r.verdict, r.measured, r.flags))
    sep_pads_bad = sep_pads_ok.replace("(at 5 0)", "(at 0 0)")  # GND x -1..1
    bad = _board(_fp("U1", 15, 10, 8, 3, sep_pads_bad))         # clearance 3
    r = check_placement(sep_art, bad, {})[0]
    note(r)
    check("separation violated: FAIL + PLACEMENT_SEPARATION_VIOLATION (3 < 6)",
          r.verdict == "FAIL" and r.code == SEPARATION_VIOLATION,
          "verdict=%s code=%s measured=%s" % (r.verdict, r.code, r.measured))
    # a net class absent from the board -> fail-closed unresolved
    absent_art = _art("separation", [{"id": "x", "kind": "separation",
                                      "subjects": ["HV", "SELV"],
                                      "params": {"min_mm": 6.0}}])
    r = check_placement(absent_art, ok, {})[0]
    note(r)
    check("separation absent-class: UNRESOLVED + PLACEMENT_SUBJECT_UNRESOLVED",
          r.verdict == "UNRESOLVED" and r.code == SUBJECT_UNRESOLVED,
          "verdict=%s code=%s" % (r.verdict, r.code))

    # -- explicit fail-closed: unmapped / absent-refdes subject -------------
    print("subject resolution (fail-closed):")
    r = check_placement(near_art, _board(_fp("U1", 10, 10, 2, 2)),
                        {"a": ["U1"]})[0]      # role "b" unmapped
    note(r)
    check("near unmapped subject: UNRESOLVED + PLACEMENT_SUBJECT_UNRESOLVED",
          r.verdict == "UNRESOLVED" and r.code == SUBJECT_UNRESOLVED,
          "verdict=%s code=%s" % (r.verdict, r.code))
    r = check_placement(near_art, _board(_fp("U1", 10, 10, 2, 2)),
                        {"a": ["U1"], "b": ["U404"]})[0]   # U404 not on board
    note(r)
    check("near absent-refdes subject: UNRESOLVED (no fuzzy matching)",
          r.verdict == "UNRESOLVED" and r.code == SUBJECT_UNRESOLVED,
          "verdict=%s code=%s" % (r.verdict, r.code))

    # -- all seven reserved codes must have fired (lobotomy discipline) ------
    expected = {NEAR_VIOLATION, EDGE_VIOLATION, KEEPOUT_VIOLATION,
                THERMAL_VIOLATION, SEPARATION_VIOLATION, SUBJECT_UNRESOLVED,
                GEOMETRY_MISSING}
    print("code coverage:")
    check("every reserved PLACEMENT_* code fired (%d/%d)"
          % (len(fired & expected), len(expected)),
          expected <= fired,
          "missing: %s" % ", ".join(sorted(expected - fired)) or "none")


def _selftest() -> int:
    failures: List[str] = []

    def check(label: str, ok: bool, detail: str = "") -> None:
        print("  [%s] %s%s" % ("ok" if ok else "FAIL", label,
                               (" -- " + detail) if detail and not ok else ""))
        if not ok:
            failures.append(label)

    run_placement_smoke(check)
    print()
    if failures:
        print("placement_agreement self-test: FAIL (%d): %s"
              % (len(failures), "; ".join(failures)))
        return 1
    print("placement_agreement self-test: PASS")
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
