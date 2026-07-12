"""Pure-python .kicad_pcb GEOMETRY extractor for placement verification.

Companion to ``pcb_extract.py`` (the CONNECTIVITY extractor). Where
``extract_pcb`` reads footprints -> refdes/value/pads->nets and is deliberately
geometry-blind, ``extract_geometry(path) -> PcbGeometry`` reads back the
*realized placement*: per-footprint position + rotation + side, courtyard
polygons, the board outline, filled/keepout zones, and per-pad geometry.

It exists so the Phase-2 placement-agreement oracle (WyredPlanPlacement step
2.2) can measure the laid-out board against a ``.placement.json`` intent using
the metrics fixed in WyredPlacementSemantics.md §3 (courtyard nearest-edge
distance for ``near``; board-outline bounding box for ``edge``; named
keepout/rule-area polygons for ``keepout``; filled-zone area for ``thermal``;
pad+zone clearance for ``separation``).

ADDITIVE by construction (WyredPlanPlacement step 2.1): this module imports the
s-expression parser from ``pcb_extract`` but touches nothing in it. It does not
import or extend ``schema.CanonicalGraph`` -- the geometry rides in the NEW
dataclasses below, keyed by refdes / net, so every existing consumer of the
connectivity graph is unchanged.

Handles BOTH board dialects, exactly like ``pcb_extract``:

  * legacy   ``(module LIB:FP (layer Top) ... (at X Y ROT) ...
              (fp_line ... (layer F.CrtYd) (width ...)) ...
              (pad 1 thru_hole circle (at PX PY PROT) (size W H) ...))``
              (v20171130, KiCad 4/5 -- BARE atoms, ``(width ..)`` strokes,
              ``(gr_arc (start CENTER) (end POINT) (angle SWEEP))`` arcs,
              ``(layer Top/Bottom)`` sides, single ``(layer ..)`` zones)
  * modern   ``(footprint "LIB:FP" (layer "F.Cu") ... (at X Y ROT) ...
              (fp_line ... (stroke ..) (layer "F.CrtYd")) ...
              (pad "1" smd roundrect (at PX PY PROT) (size W H) ...))``
              (v202x, KiCad 6/7/8 -- quoted atoms, ``(stroke (width ..))``,
              ``(gr_arc (start P) (mid P) (end P))`` 3-point arcs,
              ``(layer "F.Cu"/"B.Cu")`` sides, plural ``(layers ..)`` zones)

Coordinate frame (WyredPlacementSemantics §3): all output coordinates are the
board-file xy plane (KiCad's y grows DOWNWARD). Footprint-local geometry
(courtyard lines, pads) is transformed to absolute board coordinates by the
footprint's ``(at X Y ROT)`` using KiCad's ``RotatePoint`` convention
(``x' = x*cos+y*sin ; y' = -x*sin+y*cos`` for a positive/CCW-on-screen angle).

Recorded v0 limitations (fail-loud, never silent -- the F8 discipline):
  * back-side footprint-local geometry (courtyard/pads of ``(layer B.Cu)`` /
    ``(layer Bottom)`` parts) is NOT mirrored for the back placement flip in
    v0; ``side`` is recorded per footprint and, per WyredPlacementSemantics §3,
    is ignored by the v0 metrics (a top/bottom pair measures in xy projection).
    The footprint POSITION is exact regardless of side.
  * a footprint with no F/B.CrtYd geometry gets a pad-bounding-box courtyard
    and ``courtyard_fallback=True`` (measured, flagged, per §3's near fallback).
  * degradation markers ride in ``PcbGeometry.escalations`` when courtyards are
    missing or the Edge.Cuts outline does not assemble into a closed loop --
    the same explicit-escalation discipline as ``pcb_extract``'s F8 marker.

Plain imports, flat harness directory:  from pcb_geometry import extract_geometry
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from pcb_extract import parse_sexpr, SExpr

Point = Tuple[float, float]


# ---------------------------------------------------------------------------
# s-expression access helpers (thin, over the shared parser)
# ---------------------------------------------------------------------------

def _num(atom: Any) -> Optional[float]:
    """Parse a numeric atom; None when it is not a number."""
    if not isinstance(atom, str):
        return None
    try:
        return float(atom)
    except ValueError:
        return None


def _children(node: SExpr, head: str) -> List[List[SExpr]]:
    """All DIRECT children of ``node`` whose head atom equals ``head``."""
    out: List[List[SExpr]] = []
    if isinstance(node, list):
        for child in node[1:]:
            if isinstance(child, list) and child and child[0] == head:
                out.append(child)
    return out


def _child(node: SExpr, head: str) -> Optional[List[SExpr]]:
    """First direct child of ``node`` with the given head, or None."""
    kids = _children(node, head)
    return kids[0] if kids else None


def _xy(form: List[SExpr], i: int = 1) -> Optional[Point]:
    """A 2-float payload starting at index ``i`` (``(at X Y ..)``, ``(xy X Y)``)."""
    if len(form) < i + 2:
        return None
    x, y = _num(form[i]), _num(form[i + 1])
    if x is None or y is None:
        return None
    return (x, y)


def _pts(node: SExpr) -> List[Point]:
    """Points of a ``(pts (xy X Y) ...)`` sub-form found under ``node``."""
    pts_form = _child(node, "pts")
    if pts_form is None:
        return []
    out: List[Point] = []
    for xy in _children(pts_form, "xy"):
        p = _xy(xy)
        if p is not None:
            out.append(p)
    return out


def _layers_of(node: SExpr) -> List[str]:
    """Layer name(s): a singular ``(layer A)`` or plural ``(layers A B ..)``."""
    single = _child(node, "layer")
    if single is not None:
        return [a for a in single[1:] if isinstance(a, str)]
    plural = _child(node, "layers")
    if plural is not None:
        return [a for a in plural[1:] if isinstance(a, str)]
    return []


# ---------------------------------------------------------------------------
# Geometry primitives
# ---------------------------------------------------------------------------

_ARC_SAMPLES = 16          # per-arc polyline subdivisions (outline + courtyard)
_CIRCLE_SAMPLES = 32       # full-circle polyline subdivisions


def _rotate(x: float, y: float, deg: float) -> Point:
    """Rotate (x, y) by ``deg`` using KiCad's RotatePoint convention.

    ``x' = x*cos + y*sin ; y' = -x*sin + y*cos`` -- a positive angle is
    counter-clockwise ON SCREEN in KiCad's y-grows-down frame (verified: a
    +90 deg footprint sends a local +x pad to screen-up = file -y).
    """
    a = math.radians(deg)
    ca, sa = math.cos(a), math.sin(a)
    return (x * ca + y * sa, -x * sa + y * ca)


def _shoelace_area(ring: List[Point]) -> float:
    """Absolute polygon area of a ring (shoelace); 0.0 for < 3 points."""
    n = len(ring)
    if n < 3:
        return 0.0
    s = 0.0
    for i in range(n):
        x0, y0 = ring[i]
        x1, y1 = ring[(i + 1) % n]
        s += x0 * y1 - x1 * y0
    return abs(s) * 0.5


def _bbox(points: List[Point]) -> Optional[Tuple[float, float, float, float]]:
    """(min_x, min_y, max_x, max_y) over points, or None when empty."""
    if not points:
        return None
    xs = [p[0] for p in points]
    ys = [p[1] for p in points]
    return (min(xs), min(ys), max(xs), max(ys))


def _point_in_ring(pt: Point, ring: List[Point]) -> bool:
    """Ray-cast even/odd test: is ``pt`` inside the polygon ``ring``?"""
    if len(ring) < 3:
        return False
    x, y = pt
    inside = False
    n = len(ring)
    j = n - 1
    for i in range(n):
        xi, yi = ring[i]
        xj, yj = ring[j]
        if (yi > y) != (yj > y):
            xint = xi + (y - yi) * (xj - xi) / (yj - yi)
            if x < xint:
                inside = not inside
        j = i
    return inside


def _sample_arc_3pt(start: Point, mid: Point, end: Point,
                    n: int = _ARC_SAMPLES) -> List[Point]:
    """Sample a modern (start, mid, end) arc into a polyline start..end.

    Degrades gracefully to [start, mid, end] when the three points are
    collinear (no finite circle)."""
    (x1, y1), (x2, y2), (x3, y3) = start, mid, end
    d = 2.0 * (x1 * (y2 - y3) + x2 * (y3 - y1) + x3 * (y1 - y2))
    if abs(d) < 1e-9:
        return [start, mid, end]
    ux = ((x1 * x1 + y1 * y1) * (y2 - y3) + (x2 * x2 + y2 * y2) * (y3 - y1)
          + (x3 * x3 + y3 * y3) * (y1 - y2)) / d
    uy = ((x1 * x1 + y1 * y1) * (x3 - x2) + (x2 * x2 + y2 * y2) * (x1 - x3)
          + (x3 * x3 + y3 * y3) * (x2 - x1)) / d
    cx, cy = ux, uy
    a1 = math.atan2(y1 - cy, x1 - cx)
    am = math.atan2(y2 - cy, x2 - cx)
    a3 = math.atan2(y3 - cy, x3 - cx)
    r = math.hypot(x1 - cx, y1 - cy)

    def _norm(a: float) -> float:
        while a <= -math.pi:
            a += 2 * math.pi
        while a > math.pi:
            a -= 2 * math.pi
        return a

    # Sweep from a1 to a3 through am: pick the direction whose mid lands on am.
    sweep_ccw = _norm(a3 - a1) % (2 * math.pi)
    mid_ccw = _norm(am - a1) % (2 * math.pi)
    if mid_ccw <= sweep_ccw:
        total = sweep_ccw
        step = total / n
    else:
        total = sweep_ccw - 2 * math.pi
        step = total / n
    out: List[Point] = [start]
    for k in range(1, n):
        a = a1 + step * k
        out.append((cx + r * math.cos(a), cy + r * math.sin(a)))
    out.append(end)
    return out


def _sample_arc_legacy(center: Point, endpoint: Point, angle_deg: float,
                       n: int = _ARC_SAMPLES) -> List[Point]:
    """Sample a legacy ``(gr_arc (start CENTER) (end POINT) (angle SWEEP))``.

    ``endpoint`` is one arc end; the other is ``endpoint`` swept about
    ``center`` by ``angle_deg``. KiCad's legacy sweep sign is OPPOSITE the
    ``RotatePoint`` sense (verified on the mppt-2420-hc outline: a -90 sweep of
    (100,137) about (103,137) must land on (103,140), the bottom edge -- so the
    stored angle is negated before rotation)."""
    cx, cy = center
    vx, vy = endpoint[0] - cx, endpoint[1] - cy
    out: List[Point] = [endpoint]
    for k in range(1, n + 1):
        rx, ry = _rotate(vx, vy, -angle_deg * k / n)
        out.append((cx + rx, cy + ry))
    return out


def _sample_circle(center: Point, radius: float,
                   n: int = _CIRCLE_SAMPLES) -> List[Point]:
    """A full circle sampled into a closed ring of ``n`` points."""
    cx, cy = center
    return [(cx + radius * math.cos(2 * math.pi * k / n),
             cy + radius * math.sin(2 * math.pi * k / n)) for k in range(n)]


def _rect_ring(a: Point, b: Point) -> List[Point]:
    """The four corners of the axis-aligned rectangle spanned by a and b."""
    (x1, y1), (x2, y2) = a, b
    return [(x1, y1), (x2, y1), (x2, y2), (x1, y2)]


def _assemble_rings(segments: List[Tuple[Point, Point]],
                    eps: float = 1e-3) -> List[Tuple[List[Point], bool]]:
    """Chain undirected segments into (ring, is_closed) by endpoint matching.

    Greedy: pick an unused segment, walk from either end matching the next
    segment whose endpoint coincides (within ``eps``), stop when the walk
    returns to the start (``is_closed=True``, closing vertex dropped) or no
    further segment matches (``is_closed=False`` -- an open chain, still
    returned so callers can see the gap)."""
    def key(p: Point) -> Tuple[int, int]:
        return (round(p[0] / eps), round(p[1] / eps))

    # adjacency: endpoint-key -> list of (segment index, this-end, other-end)
    adj: Dict[Tuple[int, int], List[Tuple[int, Point, Point]]] = {}
    for i, (p, q) in enumerate(segments):
        adj.setdefault(key(p), []).append((i, p, q))
        adj.setdefault(key(q), []).append((i, q, p))

    used = [False] * len(segments)
    rings: List[Tuple[List[Point], bool]] = []
    for start_i in range(len(segments)):
        if used[start_i]:
            continue
        used[start_i] = True
        p0, p1 = segments[start_i]
        ring: List[Point] = [p0, p1]
        cur = p1
        while key(cur) != key(p0):
            nxt = None
            for (j, this_end, other_end) in adj.get(key(cur), ()):
                if not used[j]:
                    nxt = (j, other_end)
                    break
            if nxt is None:
                break
            j, other_end = nxt
            used[j] = True
            ring.append(other_end)
            cur = other_end
        closed = key(cur) == key(p0) and len(ring) >= 3
        if closed:
            ring.pop()          # drop the duplicated closing vertex
        rings.append((ring, closed))
    return rings


# ---------------------------------------------------------------------------
# Output dataclasses (the NEW structures; schema.CanonicalGraph untouched)
# ---------------------------------------------------------------------------

@dataclass
class Pad:
    """One pad in ABSOLUTE board coordinates.

    ``name``     pad name/number (may repeat within a footprint).
    ``net_id``   KiCad net id (None / 0 -> unconnected).
    ``net_name`` net name string ("" when unnamed).
    ``shape``    KiCad pad shape ("rect"/"roundrect"/"circle"/"oval"/...).
    ``position`` absolute pad center (board xy).
    ``size``     (w, h) in the pad's own frame.
    ``rotation`` absolute pad orientation in degrees.
    ``layers``   copper/other layer atoms ("F.Cu", "*.Cu", ...).
    """

    name: str
    net_id: Optional[int]
    net_name: str
    shape: str
    position: Point
    size: Tuple[float, float]
    rotation: float
    layers: List[str] = field(default_factory=list)

    def corners(self) -> List[Point]:
        """Absolute corners of the pad's bounding rectangle (w x h, rotated)."""
        w, h = self.size
        hw, hh = w / 2.0, h / 2.0
        px, py = self.position
        out: List[Point] = []
        for lx, ly in ((-hw, -hh), (hw, -hh), (hw, hh), (-hw, hh)):
            rx, ry = _rotate(lx, ly, self.rotation)
            out.append((px + rx, py + ry))
        return out


@dataclass
class Footprint:
    """A placed footprint's geometry in ABSOLUTE board coordinates.

    ``courtyard`` is a list of rings (usually one). When the footprint carried
    no F/B.CrtYd geometry it is a single pad-bounding-box ring and
    ``courtyard_fallback`` is True (WyredPlacementSemantics §3 near fallback)."""

    refdes: str
    lib_id: str
    side: str                              # "top" | "bottom"
    position: Point
    rotation: float
    courtyard: List[List[Point]] = field(default_factory=list)
    courtyard_fallback: bool = False
    pads: List[Pad] = field(default_factory=list)

    def courtyard_points(self) -> List[Point]:
        pts: List[Point] = []
        for ring in self.courtyard:
            pts.extend(ring)
        return pts

    def bbox(self) -> Optional[Tuple[float, float, float, float]]:
        pts = self.courtyard_points()
        if not pts:
            for pad in self.pads:
                pts.extend(pad.corners())
        return _bbox(pts)


@dataclass
class Zone:
    """A copper / keepout zone in ABSOLUTE board coordinates.

    ``outline``  the zone's defined polygon ring (xy already board-absolute).
    ``filled``   list of (layer, ring) actual poured-copper polygons.
    ``is_keepout`` True when the zone carries a ``(keepout ...)`` sub-form
                 (a rule / keepout area, NOT poured copper).
    ``name``     the rule-area name when present, else None (an UNNAMED
                 keepout is unverifiable by the placement checker, per §3).
    """

    net_id: Optional[int]
    net_name: str
    layers: List[str]
    outline: List[Point] = field(default_factory=list)
    filled: List[Tuple[str, List[Point]]] = field(default_factory=list)
    is_keepout: bool = False
    name: Optional[str] = None
    keepout_flags: Dict[str, str] = field(default_factory=dict)

    def area(self) -> float:
        """Outline area (mm^2)."""
        return _shoelace_area(self.outline)

    def filled_area(self) -> float:
        """Total poured-copper area (mm^2) over all filled polygons."""
        return sum(_shoelace_area(ring) for (_layer, ring) in self.filled)


@dataclass
class Outline:
    """The Edge.Cuts board outline in ABSOLUTE board coordinates."""

    rings: List[List[Point]] = field(default_factory=list)
    closed: bool = False
    bbox: Optional[Tuple[float, float, float, float]] = None

    def main_ring(self) -> List[Point]:
        """The largest-area ring (the outer boundary), or [] when none."""
        if not self.rings:
            return []
        return max(self.rings, key=_shoelace_area)

    def contains(self, pt: Point) -> bool:
        """Is ``pt`` inside the outer boundary? Falls back to the bbox when no
        ring assembled."""
        ring = self.main_ring()
        if len(ring) >= 3:
            return _point_in_ring(pt, ring)
        if self.bbox is None:
            return False
        x0, y0, x1, y1 = self.bbox
        return x0 <= pt[0] <= x1 and y0 <= pt[1] <= y1


@dataclass
class PcbGeometry:
    """The realized placement: footprints + zones + outline (+ escalations)."""

    footprints: Dict[str, Footprint] = field(default_factory=dict)
    zones: List[Zone] = field(default_factory=list)
    outline: Outline = field(default_factory=Outline)
    escalations: List[str] = field(default_factory=list)

    def footprint(self, refdes: str) -> Optional[Footprint]:
        return self.footprints.get(refdes)


# ---------------------------------------------------------------------------
# Extraction
# ---------------------------------------------------------------------------

_CRTYD_LAYERS = frozenset({"F.CrtYd", "B.CrtYd"})
_KEEPOUT_FLAG_HEADS = ("tracks", "vias", "pads", "copperpour", "footprints")


def _side_of(layers: List[str]) -> str:
    for lyr in layers:
        if lyr in ("B.Cu", "Bottom"):
            return "bottom"
    return "top"


def _crtyd_segments_and_rings(
        fp: List[SExpr], fpos: Point, frot: float,
        mirror: bool) -> Tuple[List[Tuple[Point, Point]], List[List[Point]]]:
    """Collect a footprint's courtyard as absolute (segments, direct-rings).

    fp_line / fp_arc -> segments (assembled into rings by the caller);
    fp_poly / fp_rect / fp_circle -> complete rings directly."""
    segs: List[Tuple[Point, Point]] = []
    rings: List[List[Point]] = []

    def to_abs(p: Point) -> Point:
        lx = -p[0] if mirror else p[0]
        rx, ry = _rotate(lx, p[1], frot)
        return (fpos[0] + rx, fpos[1] + ry)

    for head in ("fp_line", "fp_arc", "fp_poly", "fp_rect", "fp_circle"):
        for form in _children(fp, head):
            if any(lyr in _CRTYD_LAYERS for lyr in _layers_of(form)) is False:
                continue
            if head == "fp_line":
                s = _child(form, "start")
                e = _child(form, "end")
                sp, ep = (_xy(s) if s else None), (_xy(e) if e else None)
                if sp and ep:
                    segs.append((to_abs(sp), to_abs(ep)))
            elif head == "fp_arc":
                s = _child(form, "start")
                m = _child(form, "mid")
                e = _child(form, "end")
                sp = _xy(s) if s else None
                ep = _xy(e) if e else None
                mp = _xy(m) if m else None
                if sp and ep and mp is not None:
                    poly = _sample_arc_3pt(sp, mp, ep)
                elif sp and ep:                       # legacy center/point/angle
                    ang = _child(form, "angle")
                    a = _num(ang[1]) if ang and len(ang) > 1 else 0.0
                    poly = _sample_arc_legacy(sp, ep, a or 0.0)
                else:
                    poly = []
                for i in range(len(poly) - 1):
                    segs.append((to_abs(poly[i]), to_abs(poly[i + 1])))
            elif head == "fp_poly":
                ring = [to_abs(p) for p in _pts(form)]
                if len(ring) >= 3:
                    rings.append(ring)
            elif head == "fp_rect":
                s = _child(form, "start")
                e = _child(form, "end")
                sp, ep = (_xy(s) if s else None), (_xy(e) if e else None)
                if sp and ep:
                    rings.append([to_abs(p) for p in _rect_ring(sp, ep)])
            elif head == "fp_circle":
                c = _child(form, "center")
                e = _child(form, "end")
                cp, ep = (_xy(c) if c else None), (_xy(e) if e else None)
                if cp and ep:
                    r = math.hypot(ep[0] - cp[0], ep[1] - cp[1])
                    rings.append([to_abs(p) for p in _sample_circle(cp, r)])
    return segs, rings


def _extract_footprint(fp: List[SExpr], idx: int,
                       seen: Dict[str, int]) -> Footprint:
    lib_id = fp[1] if len(fp) >= 2 and isinstance(fp[1], str) else ""
    layers = _layers_of(fp)
    side = _side_of(layers)

    at = _child(fp, "at")
    fpos: Point = (0.0, 0.0)
    frot = 0.0
    if at is not None:
        p = _xy(at)
        if p is not None:
            fpos = p
        if len(at) >= 4 and _num(at[3]) is not None:
            frot = _num(at[3])            # type: ignore[assignment]

    # reference: fp_text reference (legacy/modern) or property "Reference"
    ref: Optional[str] = None
    for t in _children(fp, "fp_text"):
        if len(t) >= 3 and t[1] == "reference" and isinstance(t[2], str):
            ref = t[2]
            break
    if ref is None:
        for pr in _children(fp, "property"):
            if len(pr) >= 3 and pr[1] == "Reference" and isinstance(pr[2], str):
                ref = pr[2]
                break
    if not ref or ref.startswith("REF*"):
        ref = "FP%d" % (idx + 1)
    if ref in seen:
        seen[ref] += 1
        ref = "%s_DUP%d" % (ref, seen[ref])
    else:
        seen[ref] = 1

    # v0 does NOT mirror back-side local geometry (recorded limitation); the
    # flag is threaded through so a future rung can turn it on in one place.
    mirror = False
    segs, direct_rings = _crtyd_segments_and_rings(fp, fpos, frot, mirror)
    courtyard: List[List[Point]] = list(direct_rings)
    courtyard.extend(r for (r, _closed) in _assemble_rings(segs)
                     if len(r) >= 3)

    pads: List[Pad] = []
    for pad in _children(fp, "pad"):
        name = pad[1] if len(pad) >= 2 and isinstance(pad[1], str) else ""
        shape = pad[3] if len(pad) >= 4 and isinstance(pad[3], str) else ""
        pat = _child(pad, "at")
        local: Point = (0.0, 0.0)
        prot = frot                       # pad inherits footprint orientation
        if pat is not None:
            lp = _xy(pat)
            if lp is not None:
                local = lp
            if len(pat) >= 4 and _num(pat[3]) is not None:
                prot = _num(pat[3])       # type: ignore[assignment]
        lx = -local[0] if mirror else local[0]
        rx, ry = _rotate(lx, local[1], frot)
        pos = (fpos[0] + rx, fpos[1] + ry)
        size_form = _child(pad, "size")
        size: Tuple[float, float] = (0.0, 0.0)
        if size_form is not None:
            sp = _xy(size_form)
            if sp is not None:
                size = sp
        net_form = _child(pad, "net")
        net_id: Optional[int] = None
        net_name = ""
        if net_form is not None and len(net_form) >= 2:
            try:
                net_id = int(net_form[1])
            except (TypeError, ValueError):
                net_id = None
            if net_id == 0:
                net_id = None
            if len(net_form) >= 3 and isinstance(net_form[2], str):
                net_name = net_form[2]
        pads.append(Pad(name=name, net_id=net_id, net_name=net_name,
                        shape=shape, position=pos, size=size, rotation=prot,
                        layers=_layers_of(pad)))

    fallback = False
    if not courtyard:
        fallback = True
        pad_pts: List[Point] = []
        for pad in pads:
            pad_pts.extend(pad.corners())
        bb = _bbox(pad_pts)
        if bb is not None:
            x0, y0, x1, y1 = bb
            courtyard = [_rect_ring((x0, y0), (x1, y1))]

    return Footprint(refdes=ref, lib_id=lib_id, side=side, position=fpos,
                     rotation=frot, courtyard=courtyard,
                     courtyard_fallback=fallback, pads=pads)


def _extract_zone(zone: List[SExpr]) -> Zone:
    net_form = _child(zone, "net")
    net_id: Optional[int] = None
    if net_form is not None and len(net_form) >= 2:
        try:
            net_id = int(net_form[1])
        except (TypeError, ValueError):
            net_id = None
    nn = _child(zone, "net_name")
    net_name = nn[1] if nn is not None and len(nn) >= 2 \
        and isinstance(nn[1], str) else ""
    layers = _layers_of(zone)

    outline: List[Point] = []
    poly = _child(zone, "polygon")
    if poly is not None:
        outline = _pts(poly)

    filled: List[Tuple[str, List[Point]]] = []
    for fp_poly in _children(zone, "filled_polygon"):
        flayers = _layers_of(fp_poly) or layers
        ring = _pts(fp_poly)
        if len(ring) >= 3:
            filled.append((flayers[0] if flayers else "", ring))

    keepout = _child(zone, "keepout")
    is_keepout = keepout is not None
    keepout_flags: Dict[str, str] = {}
    if keepout is not None:
        for h in _KEEPOUT_FLAG_HEADS:
            k = _child(keepout, h)
            if k is not None and len(k) >= 2 and isinstance(k[1], str):
                keepout_flags[h] = k[1]
    name_form = _child(zone, "name")
    name = name_form[1] if name_form is not None and len(name_form) >= 2 \
        and isinstance(name_form[1], str) else None

    return Zone(net_id=net_id, net_name=net_name, layers=layers,
                outline=outline, filled=filled, is_keepout=is_keepout,
                name=name, keepout_flags=keepout_flags)


def _extract_outline(board: List[SExpr]) -> Outline:
    segs: List[Tuple[Point, Point]] = []
    # (ring, is_closed): direct rects/polys/circles are inherently closed.
    rings_cl: List[Tuple[List[Point], bool]] = []
    all_pts: List[Point] = []

    def is_edge(form: List[SExpr]) -> bool:
        return "Edge.Cuts" in _layers_of(form)

    for form in _children(board, "gr_line"):
        if not is_edge(form):
            continue
        s = _child(form, "start")
        e = _child(form, "end")
        sp, ep = (_xy(s) if s else None), (_xy(e) if e else None)
        if sp and ep:
            segs.append((sp, ep))
            all_pts.extend((sp, ep))
    for form in _children(board, "gr_arc"):
        if not is_edge(form):
            continue
        s = _child(form, "start")
        m = _child(form, "mid")
        e = _child(form, "end")
        sp = _xy(s) if s else None
        ep = _xy(e) if e else None
        mp = _xy(m) if m else None
        if sp and ep and mp is not None:
            poly = _sample_arc_3pt(sp, mp, ep)
        elif sp and ep:
            ang = _child(form, "angle")
            a = _num(ang[1]) if ang and len(ang) > 1 else 0.0
            poly = _sample_arc_legacy(sp, ep, a or 0.0)
        else:
            poly = []
        for i in range(len(poly) - 1):
            segs.append((poly[i], poly[i + 1]))
        all_pts.extend(poly)
    for form in _children(board, "gr_rect"):
        if not is_edge(form):
            continue
        s = _child(form, "start")
        e = _child(form, "end")
        sp, ep = (_xy(s) if s else None), (_xy(e) if e else None)
        if sp and ep:
            rings_cl.append((_rect_ring(sp, ep), True))
            all_pts.extend((sp, ep))
    for form in _children(board, "gr_poly"):
        if not is_edge(form):
            continue
        ring = _pts(form)
        if len(ring) >= 3:
            rings_cl.append((ring, True))
            all_pts.extend(ring)
    for form in _children(board, "gr_circle"):
        if not is_edge(form):
            continue
        c = _child(form, "center")
        e = _child(form, "end")
        cp, ep = (_xy(c) if c else None), (_xy(e) if e else None)
        if cp and ep:
            r = math.hypot(ep[0] - cp[0], ep[1] - cp[1])
            ring = _sample_circle(cp, r)
            rings_cl.append((ring, True))
            all_pts.extend(ring)

    for (r, is_closed) in _assemble_rings(segs):
        if len(r) >= 3:
            rings_cl.append((r, is_closed))

    rings = [r for (r, _c) in rings_cl]
    # The board is "closed" iff its OUTER boundary closes: tie the flag to the
    # largest-area ring, so a stray closed slot cannot mask an open outline.
    closed = False
    if rings_cl:
        best_ring, best_closed = max(
            rings_cl, key=lambda rc: _shoelace_area(rc[0]))
        closed = best_closed and _shoelace_area(best_ring) > 1e-6

    return Outline(rings=rings, closed=closed, bbox=_bbox(all_pts))


def extract_geometry_text(text: str) -> PcbGeometry:
    """Extract a ``PcbGeometry`` from .kicad_pcb SOURCE TEXT (both dialects)."""
    top = parse_sexpr(text)
    board: Optional[List[SExpr]] = None
    for node in top:
        if isinstance(node, list) and node and node[0] == "kicad_pcb":
            board = node
            break
    if board is None:
        raise ValueError("not a .kicad_pcb document: no (kicad_pcb ...) form")

    footprints: Dict[str, Footprint] = {}
    seen: Dict[str, int] = {}
    idx = 0
    for node in board[1:]:
        if isinstance(node, list) and node and node[0] in ("module", "footprint"):
            fpg = _extract_footprint(node, idx, seen)
            footprints[fpg.refdes] = fpg
            idx += 1

    zones = [_extract_zone(z) for z in _children(board, "zone")]
    outline = _extract_outline(board)

    escalations: List[str] = []
    n_fallback = sum(1 for f in footprints.values() if f.courtyard_fallback)
    if n_fallback:
        escalations.append(
            "PCB_COURTYARD_MISSING: %d of %d footprints carry no F/B.CrtYd "
            "geometry; a pad-bounding-box courtyard was substituted for those "
            "and flagged courtyard_fallback=True (courtyard-based metrics "
            "degrade to the pad bbox for them)."
            % (n_fallback, len(footprints)))
    if not outline.closed:
        escalations.append(
            "PCB_OUTLINE_OPEN: the Edge.Cuts geometry did not assemble into a "
            "closed loop; edge/containment metrics fall back to the outline "
            "bounding box and are unreliable for this board.")

    return PcbGeometry(footprints=footprints, zones=zones, outline=outline,
                       escalations=escalations)


def extract_geometry(path: str) -> PcbGeometry:
    """Read a .kicad_pcb file (both dialects) and return its ``PcbGeometry``."""
    with open(path, "r", encoding="utf-8", errors="replace") as fh:
        return extract_geometry_text(fh.read())


__all__ = [
    "extract_geometry",
    "extract_geometry_text",
    "PcbGeometry",
    "Footprint",
    "Pad",
    "Zone",
    "Outline",
    "Point",
    "run_geometry_smoke",
]


# ---------------------------------------------------------------------------
# Shared smoke test -- exercised by THIS module's __main__ and, per the step
# 2.1 acceptance, also by pcb_extract.py's __main__ (both dialects + real
# Watchy/MPPT boards). ``check(label, ok, detail="")`` matches pcb_extract's.
# ---------------------------------------------------------------------------

def run_geometry_smoke(check) -> None:
    import os

    # -- 1. synthetic BOTH dialects (no files) -------------------------------
    print("geometry synthetic dialect checks:")

    # modern: a footprint rotated +90 deg, an ASYMMETRIC courtyard rectangle,
    # a filled + a keepout zone, and a closed Edge.Cuts rectangle. The +90
    # rotation locks the RotatePoint convention: local (+2,0) -> file (0,-2).
    modern = """
    (kicad_pcb (version 20221018) (generator pcbnew)
      (gr_line (start 0 0) (end 40 0) (stroke (width 0.15)) (layer "Edge.Cuts"))
      (gr_line (start 40 0) (end 40 30) (stroke (width 0.15)) (layer "Edge.Cuts"))
      (gr_line (start 40 30) (end 0 30) (stroke (width 0.15)) (layer "Edge.Cuts"))
      (gr_line (start 0 30) (end 0 0) (stroke (width 0.15)) (layer "Edge.Cuts"))
      (footprint "Lib:U" (layer "F.Cu")
        (at 10 10 90)
        (property "Reference" "U1")
        (fp_line (start -2 -1) (end 2 -1) (stroke (width 0.05)) (layer "F.CrtYd"))
        (fp_line (start 2 -1) (end 2 1) (stroke (width 0.05)) (layer "F.CrtYd"))
        (fp_line (start 2 1) (end -2 1) (stroke (width 0.05)) (layer "F.CrtYd"))
        (fp_line (start -2 1) (end -2 -1) (stroke (width 0.05)) (layer "F.CrtYd"))
        (pad "1" smd rect (at 2 0) (size 1 1) (layers "F.Cu") (net 2 "+3V3")))
      (footprint "Lib:R" (layer "B.Cu")
        (at 25 20)
        (property "Reference" "R1")
        (pad "1" smd rect (at -1 0) (size 1 1) (layers "B.Cu") (net 1 "GND"))
        (pad "2" smd rect (at 1 0) (size 1 1) (layers "B.Cu") (net 2 "+3V3")))
      (zone (net 1) (net_name "GND") (layers "F.Cu")
        (polygon (pts (xy 5 5) (xy 15 5) (xy 15 12) (xy 5 12)))
        (filled_polygon (layer "F.Cu")
          (pts (xy 5 5) (xy 15 5) (xy 15 12) (xy 5 12))))
      (zone (net 0) (net_name "") (layers "F.Cu")
        (name "antenna_ko")
        (keepout (tracks allowed) (vias allowed) (pads allowed)
                 (copperpour not_allowed) (footprints allowed))
        (polygon (pts (xy 30 2) (xy 38 2) (xy 38 8) (xy 30 8)))))
    """
    g = extract_geometry_text(modern)
    check("modern: 2 footprints extracted", len(g.footprints) == 2)
    u1 = g.footprint("U1")
    check("modern: U1 position is the board-absolute at()",
          u1 is not None and u1.position == (10.0, 10.0) and u1.rotation == 90.0)
    check("modern: U1 side top, R1 side bottom",
          u1 is not None and u1.side == "top"
          and g.footprint("R1").side == "bottom")
    # local (+2,0) rotated +90 -> (0,-2); absolute pad = (10, 8)
    pad = u1.pads[0] if u1 and u1.pads else None
    check("modern: +90 rotation places pad local (2,0) at (10, 8) "
          "[RotatePoint convention]",
          pad is not None and abs(pad.position[0] - 10.0) < 1e-6
          and abs(pad.position[1] - 8.0) < 1e-6,
          "" if pad is None else "got %s" % (pad.position,))
    # courtyard rectangle -2..2 x -1..1, rotated 90 about (10,10) ->
    # bbox 9..11 x 8..12
    bb = u1.bbox() if u1 else None
    check("modern: U1 rotated courtyard bbox is (9,8,11,12)",
          bb is not None and all(abs(a - b) < 1e-6 for a, b in
                                 zip(bb, (9.0, 8.0, 11.0, 12.0))),
          "got %s" % (bb,))
    check("modern: R1 courtyard falls back to pad bbox (no CrtYd)",
          g.footprint("R1").courtyard_fallback is True)
    check("modern: courtyard-missing escalation set",
          any(e.startswith("PCB_COURTYARD_MISSING") for e in g.escalations))
    check("modern: outline closes, no PCB_OUTLINE_OPEN",
          g.outline.closed and not any(
              e.startswith("PCB_OUTLINE_OPEN") for e in g.escalations))
    check("modern: outline bbox is the 40x30 rectangle",
          g.outline.bbox == (0.0, 0.0, 40.0, 30.0))
    check("modern: footprint positions inside the outline",
          all(g.outline.contains(f.position) for f in g.footprints.values()))
    gnd = [z for z in g.zones if z.net_name == "GND"]
    check("modern: GND zone area = 10x7 = 70 mm^2",
          bool(gnd) and abs(gnd[0].area() - 70.0) < 1e-6
          and abs(gnd[0].filled_area() - 70.0) < 1e-6)
    ko = [z for z in g.zones if z.is_keepout]
    check("modern: named keepout 'antenna_ko' with copperpour not_allowed",
          bool(ko) and ko[0].name == "antenna_ko"
          and ko[0].keepout_flags.get("copperpour") == "not_allowed")

    # legacy: module dialect, (layer Top), bare atoms, fp_circle courtyard,
    # a gr_arc-rounded Edge.Cuts, single-layer zone.
    legacy = """
    (kicad_pcb (version 20171130) (host pcbnew 5.1.9)
      (gr_line (start 0 0) (end 50 0) (layer Edge.Cuts) (width 0.15))
      (gr_line (start 50 0) (end 50 40) (layer Edge.Cuts) (width 0.15))
      (gr_line (start 50 40) (end 0 40) (layer Edge.Cuts) (width 0.15))
      (gr_line (start 0 40) (end 0 0) (layer Edge.Cuts) (width 0.15))
      (module Lib:L (layer Top) (tedit 0) (tstamp 0)
        (at 20 20 45)
        (fp_text reference L1 (at 0 0) (layer F.SilkS))
        (fp_circle (center 0 0) (end 3 0) (layer F.CrtYd) (width 0.1))
        (pad 1 thru_hole circle (at -3 0 45) (size 2 2) (drill 1)
          (layers *.Cu *.Mask) (net 1 GND)))
      (module Lib:R (layer Bottom) (tedit 0) (tstamp 0)
        (at 35 30 0)
        (fp_text reference R1 (at 0 0) (layer F.SilkS))
        (fp_line (start -1.5 -0.8) (end 1.5 -0.8) (layer F.CrtYd) (width 0.05))
        (fp_line (start 1.5 -0.8) (end 1.5 0.8) (layer F.CrtYd) (width 0.05))
        (fp_line (start 1.5 0.8) (end -1.5 0.8) (layer F.CrtYd) (width 0.05))
        (fp_line (start -1.5 0.8) (end -1.5 -0.8) (layer F.CrtYd) (width 0.05))
        (pad 1 smd rect (at -1 0) (size 1 1) (layers Top) (net 2 +3V3)))
      (zone (net 1) (net_name GND) (layer Top) (hatch edge 0.5)
        (polygon (pts (xy 2 2) (xy 20 2) (xy 20 20) (xy 2 20)))
        (filled_polygon (pts (xy 2 2) (xy 20 2) (xy 20 20) (xy 2 20)))))
    """
    g = extract_geometry_text(legacy)
    check("legacy: 2 modules extracted", len(g.footprints) == 2)
    l1 = g.footprint("L1")
    check("legacy: L1 at (20,20) rot 45, side top",
          l1 is not None and l1.position == (20.0, 20.0)
          and l1.rotation == 45.0 and l1.side == "top")
    check("legacy: R1 side bottom (layer Bottom)",
          g.footprint("R1") is not None and g.footprint("R1").side == "bottom")
    # fp_circle radius 3 courtyard -> bbox 17..23 x 17..23 (rotation-invariant)
    bb = l1.bbox() if l1 else None
    check("legacy: L1 circular courtyard bbox ~ (17,17,23,23)",
          bb is not None and all(abs(a - b) < 1e-3 for a, b in
                                 zip(bb, (17.0, 17.0, 23.0, 23.0))),
          "got %s" % (bb,))
    check("legacy: R1 courtyard from fp_lines (no fallback)",
          g.footprint("R1").courtyard_fallback is False
          and len(g.footprint("R1").courtyard) >= 1)
    check("legacy: outline closes (rectangle)",
          g.outline.closed and g.outline.bbox == (0.0, 0.0, 50.0, 40.0))
    zg = [z for z in g.zones if z.net_name == "GND"]
    check("legacy: single-layer zone area = 18x18 = 324 mm^2",
          bool(zg) and abs(zg[0].area() - 324.0) < 1e-6
          and zg[0].layers == ["Top"]
          and abs(zg[0].filled_area() - 324.0) < 1e-6)

    # open-outline escalation: a board whose Edge.Cuts is a bare open chain
    open_board = """
    (kicad_pcb (version 20221018) (generator pcbnew)
      (gr_line (start 0 0) (end 10 0) (stroke (width 0.15)) (layer "Edge.Cuts"))
      (gr_line (start 10 0) (end 10 10) (stroke (width 0.15)) (layer "Edge.Cuts")))
    """
    go = extract_geometry_text(open_board)
    check("open outline: PCB_OUTLINE_OPEN escalation set, closed=False",
          not go.outline.closed
          and any(e.startswith("PCB_OUTLINE_OPEN") for e in go.escalations))

    # -- 2. real boards (skipped if the third-party trees are absent) --------
    tp = os.environ.get("WYRED_THIRD_PARTY") or os.path.normpath(
        os.path.join(os.path.dirname(os.path.abspath(__file__)),
                     "..", "..", "third-party"))
    boards = [
        ("watchy (modern 'footprint')",
         os.path.join(tp, "watchy/Watchy.kicad_pcb")),
        ("mppt-2420-hc (legacy 'module')",
         os.path.join(tp, "libre-solar-mppt-2420-hc/kicad/mppt-2420-hc.kicad_pcb")),
    ]
    for label, path in boards:
        print("%s:" % label)
        if not os.path.exists(path):
            print("  [skip] %s not present" % path)
            continue
        g = extract_geometry(path)
        nfp = len(g.footprints)
        nz = len(g.zones)
        bb = g.outline.bbox
        print("  footprints=%d  zones=%d  outline_closed=%s  bbox=%s"
              % (nfp, nz, g.outline.closed,
                 None if bb is None else tuple(round(v, 2) for v in bb)))
        if g.escalations:
            for e in g.escalations:
                print("  escalation: %s" % e.split(":", 1)[0])
        check("%s: footprints extracted" % label, nfp > 0)
        nonzero = [f for f in g.footprints.values()
                   if f.position != (0.0, 0.0)]
        check("%s: all component positions nonzero" % label,
              len(nonzero) == nfp, "%d/%d nonzero" % (len(nonzero), nfp))
        check("%s: Edge.Cuts outline closes" % label, g.outline.closed)
        inside = [f for f in g.footprints.values()
                  if g.outline.contains(f.position)]
        check("%s: all component positions within the board outline" % label,
              len(inside) == nfp, "%d/%d inside" % (len(inside), nfp))
        zareas = [z.area() for z in g.zones if z.outline]
        check("%s: every defined zone has positive area" % label,
              bool(zareas) and all(a > 0.0 for a in zareas),
              "min area %.4f" % (min(zareas) if zareas else 0.0))
        # a courtyard actually surrounds its footprint position (near metric
        # relies on courtyards centred on the placement)
        surround = 0
        checked = 0
        for f in g.footprints.values():
            if f.courtyard_fallback or not f.courtyard:
                continue
            checked += 1
            if g_contains_any(f.courtyard, f.position):
                surround += 1
        if checked:
            check("%s: courtyards surround their footprint position "
                  "(>=95%%)" % label, surround >= 0.95 * checked,
                  "%d/%d" % (surround, checked))


def g_contains_any(rings: List[List[Point]], pt: Point) -> bool:
    return any(_point_in_ring(pt, r) for r in rings)


if __name__ == "__main__":  # pragma: no cover
    import sys

    failures: List[str] = []

    def check(label: str, ok: bool, detail: str = "") -> None:
        print("  [%s] %s%s" % ("ok" if ok else "FAIL", label,
                               (" -- " + detail) if detail else ""))
        if not ok:
            failures.append(label)

    run_geometry_smoke(check)

    print()
    if failures:
        print("pcb_geometry smoke test: FAIL (%d): %s"
              % (len(failures), "; ".join(failures)))
        sys.exit(1)
    print("pcb_geometry smoke test: PASS")
    sys.exit(0)
