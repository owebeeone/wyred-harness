"""Pure-python .kicad_pcb pads->nets extractor for harness v3 (Gen4 section 4).

``extract_pcb(path) -> CanonicalGraph`` reads a KiCad board file and produces
the neutral ``schema.CanonicalGraph`` used by the whole oracle stack. It is a
CONNECTIVITY extractor: per-footprint reference / value / per-pad net, plus
kind heuristics for components (refdes prefix) and nets (name shape). It never
invokes kicad-cli and never touches the network -- pure Python 3 stdlib.

Handles BOTH board dialects:

  * legacy   ``(module LIB:FP (layer Top) ... (fp_text reference R1 ...)
              ... (pad 1 smd rect ... (net 12 /DC/DC/SW_NODE)))``
              (e.g. version 20171130, KiCad 4/5 -- net names may be BARE atoms)
  * modern   ``(footprint "LIB:FP" ... (fp_text reference "R1" ...) /
              (property "Reference" "R1" ...) ... (pad "1" smd rect ...
              (net 12 "/+12v") (pintype "power_in")))``
              (KiCad 6/7/8 -- quoted atoms, optional property-based refdes)

Parsing is a real s-expression tokenizer + stack parser (quoted strings with
backslash escapes, arbitrary nesting) -- NOT a line/regex scrape, so nested
parens inside quoted net names like "Net-(C19-Pad1)" are handled correctly.

Heuristics (documented so downstream layers know what they are getting):

  component.kind from the refdes alphabetic prefix (longest match):
      R->resistor C->capacitor L->inductor D/LED->diode Q->mosfet U->ic
      J/P/CN->connector Y/X->crystal T->transformer F->fuse FB->inductor
      TP->test_point SW/S->switch BT->battery K->relay JP->jumper
      H/MH->mechanical  (anything else -> "other")

  net.kind from the LOCAL net name (hierarchical "/sheet/NAME" is stripped):
      *GND* / VSS-family                     -> "ground" (voltage 0.0, plus the
                                                v3 ground_kind/ground_role tags)
      +3V3 / +12V / -5V / 3V3_RTC / VBUS ... -> "power" (voltage parsed when
                                                numeric; VBUS assumed 5.0)
      everything else                        -> "signal"

  terminal.role: ground net -> "ground"; else the modern (pintype ...) when
  present (power_in/power_out/passive kept, pin directions -> "signal");
  else "passive" for R/C/L parts, "signal" otherwise.

All components are ``authored=True`` (everything on a real board was placed by
its author). Pads that repeat a pad name/number (multi-pad pins, thermal pads)
collapse into ONE terminal; a repeated pad name on a DIFFERENT net gets a
disambiguated terminal name "<name>#<k>" so no connectivity is lost.

Degradation marker (F8): net-kind classification is NAME-based. When a board
carries no net names (stripped/minimal exports) the extractor falls back to
placeholder ``NET<id>`` identities; when such placeholders DOMINATE (more
than half the nets) the returned graph carries an explicit
``graph.escalations`` entry starting ``"PCB_NET_NAMES_MISSING: classification
degraded"`` so downstream consumers never mistake a name-blind extraction for
a confidently classified one.

Plain imports, flat harness directory:  from pcb_extract import extract_pcb
"""

from __future__ import annotations

import re
from typing import Any, Dict, List, Optional, Tuple, Union

from schema import CanonicalGraph, Component, Net, Terminal


# ---------------------------------------------------------------------------
# S-expression tokenizer + parser
# ---------------------------------------------------------------------------

SExpr = Union[str, List["SExpr"]]

# One token per match: a quoted string (backslash escapes allowed, may contain
# parens/whitespace), a single paren, or a bare atom. The tokenizer only
# SPLITS the text -- all structure is built by the stack parser below.
_TOKEN_RE = re.compile(r'"(?:\\.|[^"\\])*"|\(|\)|[^\s()"]+')

_ESCAPES = {"n": "\n", "t": "\t", "r": "\r"}


def _unquote(tok: str) -> str:
    """Decode a quoted-string token (strip quotes, resolve backslash escapes)."""
    body = tok[1:-1]
    if "\\" not in body:
        return body
    out: List[str] = []
    i, n = 0, len(body)
    while i < n:
        ch = body[i]
        if ch == "\\" and i + 1 < n:
            nxt = body[i + 1]
            out.append(_ESCAPES.get(nxt, nxt))
            i += 2
        else:
            out.append(ch)
            i += 1
    return "".join(out)


def parse_sexpr(text: str) -> List[SExpr]:
    """Parse s-expression text into nested Python lists of string atoms.

    Returns the LIST of top-level expressions (a .kicad_pcb has exactly one:
    the ``(kicad_pcb ...)`` form). Raises ValueError on unbalanced parens.
    Iterative (explicit stack) -- no recursion-depth limit.
    """
    root: List[SExpr] = []
    stack: List[List[SExpr]] = [root]
    for m in _TOKEN_RE.finditer(text):
        tok = m.group(0)
        if tok == "(":
            new: List[SExpr] = []
            stack[-1].append(new)
            stack.append(new)
        elif tok == ")":
            if len(stack) == 1:
                raise ValueError("unbalanced ')' at offset %d" % m.start())
            stack.pop()
        elif tok[0] == '"':
            stack[-1].append(_unquote(tok))
        else:
            stack[-1].append(tok)
    if len(stack) != 1:
        raise ValueError("unbalanced '(' -- %d unclosed" % (len(stack) - 1))
    return root


# ---------------------------------------------------------------------------
# Heuristics
# ---------------------------------------------------------------------------

# refdes alphabetic prefix -> component kind. Checked LONGEST prefix first, so
# "LED3" -> diode (not inductor), "TP22" -> test_point (not transformer),
# "FB1" -> inductor (not fuse).
_PREFIX_KINDS: Dict[str, str] = {
    "LED": "diode",
    "XTAL": "crystal",
    "CON": "connector",
    "BAT": "battery",
    "REF": "other",       # library-default "REF**" placeholders
    "TP": "test_point",
    "FB": "inductor",     # ferrite bead
    "RV": "resistor",     # varistor / trimmer
    "RN": "resistor",     # resistor network
    "CN": "connector",
    "BT": "battery",
    "SW": "switch",
    "JP": "jumper",
    "MH": "mechanical",
    "MK": "mechanical",
    "R": "resistor",
    "C": "capacitor",
    "L": "inductor",
    "D": "diode",
    "Q": "mosfet",
    "U": "ic",
    "J": "connector",
    "P": "connector",
    "Y": "crystal",
    "X": "crystal",
    "T": "transformer",
    "F": "fuse",
    "S": "switch",
    "K": "relay",
    "H": "mechanical",
}


def component_kind(refdes: str) -> str:
    """Heuristic component kind from the refdes alphabetic prefix."""
    i = 0
    while i < len(refdes) and refdes[i].isalpha():
        i += 1
    prefix = refdes[:i].upper()
    while prefix:
        kind = _PREFIX_KINDS.get(prefix)
        if kind is not None:
            return kind
        prefix = prefix[:-1]
    return "other"


# Whole-name voltage shapes on the LOCAL net name (case-insensitive):
#   +3V3  -12V  5V  3.3V  1V8  +5V0  +3V3_RTC  (optional _SUFFIX allowed)
_VOLT_RE = re.compile(r"^([+-]?)(\d+(?:\.\d+)?)V(\d+)?(?:_[0-9A-Z].*)?$", re.I)

# Ground-family names (exact local-name matches beyond the *GND* test).
_GROUND_NAMES = frozenset({"VSS", "AVSS", "DVSS", "VEE", "PE", "EARTH", "CHASSIS"})

# Power-family names that carry no parseable number. VBUS is USB bus power.
_POWER_NAMES: Dict[str, Optional[float]] = {
    "VBUS": 5.0,
    "VBAT": None, "VBATT": None, "VSYS": None, "VIN": None, "VOUT": None,
    "VCC": None, "VDD": None, "VDDA": None, "AVDD": None, "DVDD": None,
    "VREF": None, "PWR": None,
}


def _parse_voltage(local_upper: str) -> Optional[float]:
    m = _VOLT_RE.match(local_upper)
    if m is None:
        return None
    sign = -1.0 if m.group(1) == "-" else 1.0
    whole = m.group(2)
    frac = m.group(3)
    if frac is not None:            # 3V3 / 1V8 / 5V0 style
        if "." in whole:
            return None             # "3.3V3" is nonsense; refuse
        return sign * float(whole + "." + frac)
    return sign * float(whole)      # 12V / 3.3V style


def classify_net(name: str) -> Tuple[str, Optional[float], Optional[str], Optional[str]]:
    """(kind, voltage, ground_kind, ground_role) from a net name.

    Hierarchical names classify by their LOCAL segment ("/CAN_GND" -> CAN_GND,
    "/+12v" -> +12v). Non-ground nets return (kind, voltage, None, None).
    """
    local = name.rsplit("/", 1)[-1].strip()
    u = local.upper()
    if not u:
        return ("signal", None, None, None)

    # -- ground family -------------------------------------------------------
    if "GND" in u or u in _GROUND_NAMES:
        gk = "ground"
        if "CHASSIS" in u:
            gk = "chassis"
        elif "EARTH" in u or u == "PE":
            gk = "earth"
        gr = "none"
        if "GNDREF" in u or u.endswith("REF"):
            gr = "reference"
        elif "AGND" in u or u.startswith("GNDA"):
            gr = "analog"
        elif "DGND" in u or u.startswith("GNDD"):
            gr = "digital"
        elif "PGND" in u or u.startswith("GNDP"):
            gr = "power"
        return ("ground", 0.0, gk, gr)

    # -- power family --------------------------------------------------------
    v = _parse_voltage(u)
    if v is not None:
        return ("power", v, None, None)
    if u in _POWER_NAMES:
        return ("power", _POWER_NAMES[u], None, None)

    return ("signal", None, None, None)


# modern (pintype "...") -> schema Terminal.role
_PINTYPE_ROLES: Dict[str, str] = {
    "power_in": "power_in",
    "power_out": "power_out",
    "passive": "passive",
    "input": "signal",
    "output": "signal",
    "bidirectional": "signal",
    "tri_state": "signal",
    "open_collector": "signal",
    "open_emitter": "signal",
}

_PASSIVE_KINDS = frozenset({"resistor", "capacitor", "inductor"})


def _terminal_role(pintype: Optional[str], net_kind: Optional[str],
                   comp_kind: str) -> str:
    if net_kind == "ground":
        return "ground"      # KiCad tags GND pins power_in; ground is truer
    if pintype is not None:
        mapped = _PINTYPE_ROLES.get(pintype)
        if mapped is not None:
            return mapped
    if comp_kind in _PASSIVE_KINDS:
        return "passive"
    return "signal"


# ---------------------------------------------------------------------------
# Footprint walking
# ---------------------------------------------------------------------------

def _is_form(node: SExpr, head: str) -> bool:
    return isinstance(node, list) and len(node) >= 1 and node[0] == head


def _footprint_fields(fp: List[SExpr]):
    """Pull (reference, value, pads) out of one module/footprint form.

    pads is a list of (pad_name, net_id, net_name, pintype); net_id is None
    for unconnected pads. Both fp_text (v4-v7) and property "Reference"/"Value"
    (v7/v8) carriers are read; whichever appears first wins (they agree when
    both are present).
    """
    ref: Optional[str] = None
    val: Optional[str] = None
    pads: List[Tuple[str, Optional[int], str, Optional[str]]] = []

    for child in fp[1:]:
        if not isinstance(child, list) or not child:
            continue
        head = child[0]
        if head == "fp_text" and len(child) >= 3:
            if child[1] == "reference" and ref is None and isinstance(child[2], str):
                ref = child[2]
            elif child[1] == "value" and val is None and isinstance(child[2], str):
                val = child[2]
        elif head == "property" and len(child) >= 3 \
                and isinstance(child[1], str) and isinstance(child[2], str):
            if child[1] == "Reference" and ref is None:
                ref = child[2]
            elif child[1] == "Value" and val is None:
                val = child[2]
        elif head == "pad" and len(child) >= 2 and isinstance(child[1], str):
            pad_name = child[1]
            net_id: Optional[int] = None
            net_name = ""
            pintype: Optional[str] = None
            for sub in child[2:]:
                if not isinstance(sub, list) or not sub:
                    continue
                if sub[0] == "net" and len(sub) >= 2:
                    try:
                        net_id = int(sub[1])
                    except (TypeError, ValueError):
                        net_id = None
                    if len(sub) >= 3 and isinstance(sub[2], str):
                        net_name = sub[2]
                elif sub[0] == "pintype" and len(sub) >= 2 \
                        and isinstance(sub[1], str):
                    pintype = sub[1]
            pads.append((pad_name, net_id, net_name, pintype))

    return ref, val, pads


def _refdes_sort_key(refdes: str) -> Tuple[str, int, str]:
    """Natural sort: alpha prefix, then numeric part, then the rest."""
    i = 0
    while i < len(refdes) and not refdes[i].isdigit():
        i += 1
    j = i
    while j < len(refdes) and refdes[j].isdigit():
        j += 1
    num = int(refdes[i:j]) if j > i else -1
    return (refdes[:i], num, refdes[j:])


# ---------------------------------------------------------------------------
# The extractor
# ---------------------------------------------------------------------------

def extract_pcb_text(text: str) -> CanonicalGraph:
    """Extract a CanonicalGraph from .kicad_pcb SOURCE TEXT (both dialects)."""
    top = parse_sexpr(text)
    board: Optional[List[SExpr]] = None
    for node in top:
        if _is_form(node, "kicad_pcb"):
            board = node
            break
    if board is None:
        raise ValueError("not a .kicad_pcb document: no (kicad_pcb ...) form")

    # Pass 1: declared nets (authoritative id->name) + footprint forms.
    declared: Dict[int, str] = {}
    fp_forms: List[List[SExpr]] = []
    for node in board[1:]:
        if not isinstance(node, list) or not node:
            continue
        head = node[0]
        if head == "net" and len(node) >= 2:
            try:
                nid = int(node[1])
            except (TypeError, ValueError):
                continue
            name = node[2] if len(node) >= 3 and isinstance(node[2], str) else ""
            declared[nid] = name
        elif head in ("module", "footprint"):
            fp_forms.append(node)

    # Pass 2: footprints -> components; pads -> net membership.
    components: List[Component] = []
    seen_refdes: Dict[str, int] = {}
    nets_by_id: Dict[int, Net] = {}
    net_meta: Dict[int, Tuple[Optional[str], Optional[str]]] = {}
    placeholder_ids: set = set()      # nets with NO name anywhere (F8)

    for idx, fp in enumerate(fp_forms):
        lib_id = fp[1] if len(fp) >= 2 and isinstance(fp[1], str) else ""
        ref, val, pads = _footprint_fields(fp)

        if not ref or ref.startswith("REF*"):
            ref = "FP%d" % (idx + 1)          # placeholder / missing refdes
        if ref in seen_refdes:                # duplicate refdes on the board
            seen_refdes[ref] += 1
            ref = "%s_DUP%d" % (ref, seen_refdes[ref])
        else:
            seen_refdes[ref] = 1

        kind = component_kind(ref)
        comp = Component(
            refdes=ref, kind=kind, value=(val or ""), authored=True,
            attrs=({"lib_id": lib_id} if lib_id else {}),
        )

        term_net: Dict[str, Optional[int]] = {}   # terminal name -> net id
        anon = 0
        for (pad_name, net_id, net_name, pintype) in pads:
            if net_id == 0:
                net_id = None                  # net 0 == "no net" in KiCad
            if not pad_name:
                anon += 1
                pad_name = "~PAD%d" % anon     # NPTH / unnamed pads
            tname = pad_name
            if tname in term_net:
                if term_net[tname] == net_id:
                    continue                   # same pin, extra pad: collapse
                k = 2
                while ("%s#%d" % (pad_name, k)) in term_net:
                    k += 1
                tname = "%s#%d" % (pad_name, k)

            net_kind: Optional[str] = None
            if net_id is not None:
                if net_id not in nets_by_id:
                    name = declared.get(net_id) or net_name
                    if not name:
                        # No name anywhere: placeholder identity only. The
                        # classifier cannot see ground/power through NET<id>
                        # (F8: an explicit degradation marker is set below
                        # when placeholders dominate).
                        name = "NET%d" % net_id
                        placeholder_ids.add(net_id)
                    nkind, volts, gk, gr = classify_net(name)
                    nets_by_id[net_id] = Net(
                        name=name, kind=nkind, voltage=volts,
                        ground_kind=gk, ground_role=gr,
                    )
                    net_meta[net_id] = (gk, gr)
                net = nets_by_id[net_id]
                net.nodes.append((ref, tname))
                net_kind = net.kind

            term_net[tname] = net_id
            comp.terminals.append(Terminal(
                name=tname,
                role=_terminal_role(pintype, net_kind, kind),
            ))

        components.append(comp)

    components.sort(key=lambda c: _refdes_sort_key(c.refdes))
    nets = [nets_by_id[i] for i in sorted(nets_by_id)]

    # F8 degradation marker: when net names are absent/stripped, placeholder
    # NET<id> names dominate and the name-based ground/power classification
    # is BLIND. Downstream consumers must see the degradation explicitly —
    # never a confident-looking all-"signal" graph.
    escalations: List[str] = []
    if nets and 2 * len(placeholder_ids) > len(nets):
        escalations.append(
            "PCB_NET_NAMES_MISSING: classification degraded — %d of %d nets "
            "carry placeholder NET<id> names (no net name in the board "
            "file); ground/power kind and voltage classification is "
            "name-based and therefore unreliable for this graph."
            % (len(placeholder_ids), len(nets)))
    return CanonicalGraph(components=components, nets=nets,
                          escalations=escalations)


def extract_pcb(path: str) -> CanonicalGraph:
    """Read a .kicad_pcb file (legacy module or modern footprint dialect) and
    return the pads->nets connectivity as a ``schema.CanonicalGraph``."""
    with open(path, "r", encoding="utf-8", errors="replace") as fh:
        return extract_pcb_text(fh.read())


__all__ = [
    "extract_pcb",
    "extract_pcb_text",
    "parse_sexpr",
    "classify_net",
    "component_kind",
]


# ---------------------------------------------------------------------------
# smoke test:  python pcb_extract.py
# ---------------------------------------------------------------------------

if __name__ == "__main__":  # pragma: no cover
    import os
    import sys

    failures: List[str] = []

    def check(label: str, ok: bool, detail: str = "") -> None:
        print("  [%s] %s%s" % ("ok" if ok else "FAIL", label,
                               (" -- " + detail) if detail else ""))
        if not ok:
            failures.append(label)

    # -- 1. synthetic round-trip of BOTH dialects (no files needed) ----------
    print("synthetic dialect checks:")
    legacy = """
    (kicad_pcb (version 20171130) (host pcbnew 5.1.9)
      (net 0 "")
      (net 1 GND)
      (net 2 +3V3)
      (net 3 "Net-(C1-Pad1)")
      (module Lib:R_0603 (layer Top)
        (fp_text reference R1 (at 0 0) (layer F.SilkS))
        (fp_text value 10k (at 0 1) (layer F.Fab))
        (pad 1 smd rect (at -1 0) (size 1 1) (layers Top) (net 2 +3V3))
        (pad 2 smd rect (at 1 0) (size 1 1) (layers Top) (net 3 "Net-(C1-Pad1)")))
      (module Lib:C_0603 (layer Top)
        (fp_text reference C1 (at 0 0) (layer F.SilkS))
        (fp_text value 100n (at 0 1) (layer F.Fab))
        (pad 1 smd rect (at -1 0) (size 1 1) (layers Top) (net 3 "Net-(C1-Pad1)"))
        (pad 2 smd rect (at 1 0) (size 1 1) (layers Top) (net 1 GND)))
    )
    """
    g = extract_pcb_text(legacy)
    check("legacy: 2 components", len(g.components) == 2)
    check("legacy: R1 kind resistor value 10k",
          g.component("R1") is not None and g.component("R1").kind == "resistor"
          and g.component("R1").value == "10k")
    check("legacy: 3 live nets (net 0 skipped)", len(g.nets) == 3)
    gnd = [n for n in g.nets if n.name == "GND"]
    p33 = [n for n in g.nets if n.name == "+3V3"]
    check("legacy: GND ground/0.0", bool(gnd) and gnd[0].kind == "ground"
          and gnd[0].voltage == 0.0 and gnd[0].ground_kind == "ground")
    check("legacy: +3V3 power/3.3", bool(p33) and p33[0].kind == "power"
          and p33[0].voltage == 3.3)
    mid = g.net_of(("R1", "2"))
    check("legacy: R1.2 and C1.1 share Net-(C1-Pad1)",
          mid is not None and mid.name == "Net-(C1-Pad1)"
          and ("C1", "1") in [tuple(x) for x in mid.nodes])

    modern = """
    (kicad_pcb (version 20211014) (generator pcbnew)
      (net 0 "")
      (net 1 "GND")
      (net 2 "/+12v")
      (footprint "Lib:SOT-23" (layer "F.Cu")
        (property "Reference" "Q1")
        (property "Value" "IRLML6402")
        (pad "1" smd rect (at 0 0) (size 1 1) (layers "F.Cu")
          (net 2 "/+12v") (pintype "power_in"))
        (pad "2" smd rect (at 1 0) (size 1 1) (layers "F.Cu")
          (net 1 "GND") (pintype "passive"))
        (pad "2" smd rect (at 1 1) (size 1 1) (layers "F.Cu")
          (net 1 "GND") (pintype "passive"))
        (pad "3" smd rect (at 2 0) (size 1 1) (layers "F.Cu"))))
    """
    g = extract_pcb_text(modern)
    q1 = g.component("Q1")
    check("modern: Q1 via property, kind mosfet",
          q1 is not None and q1.kind == "mosfet" and q1.value == "IRLML6402")
    check("modern: duplicate pad 2 collapsed -> 3 terminals",
          q1 is not None and len(q1.terminals) == 3)
    p12 = [n for n in g.nets if n.name == "/+12v"]
    check("modern: hierarchical /+12v -> power 12.0",
          bool(p12) and p12[0].kind == "power" and p12[0].voltage == 12.0)
    check("modern: pintype power_in kept",
          q1 is not None and q1.terminal("1").role == "power_in")
    check("modern: GND pad role ground",
          q1 is not None and q1.terminal("2").role == "ground")
    check("modern: unconnected pad 3 dangling",
          g.net_of(("Q1", "3")) is None)

    # net-name classifier spot checks
    for name, want in [("AGND", ("ground", "analog")),
                       ("PGND", ("ground", "power")),
                       ("/sheet/DGND", ("ground", "digital")),
                       ("CHASSIS_GND", ("ground", "none"))]:
        k, v, gk, gr = classify_net(name)
        check("classify %s" % name, (k, gr) == want or (name.startswith("CHASSIS") and k == "ground"))
    # F8 degradation marker: stripped net names -> explicit escalation
    stripped = """
    (kicad_pcb (version 20211014) (generator pcbnew)
      (net 0 "") (net 1 "") (net 2 "")
      (footprint "Lib:R" (layer "F.Cu")
        (property "Reference" "R1") (property "Value" "1k")
        (pad "1" smd rect (at 0 0) (size 1 1) (layers "F.Cu") (net 1 ""))
        (pad "2" smd rect (at 1 0) (size 1 1) (layers "F.Cu") (net 2 ""))))
    """
    gs = extract_pcb_text(stripped)
    check("stripped names: placeholder NET<id> identities",
          sorted(n.name for n in gs.nets) == ["NET1", "NET2"])
    check("stripped names: PCB_NET_NAMES_MISSING degradation marker set",
          any(e.startswith("PCB_NET_NAMES_MISSING") for e in gs.escalations))
    check("named boards carry NO degradation marker",
          extract_pcb_text(modern).escalations == []
          and extract_pcb_text(legacy).escalations == [])
    check("classify VBUS -> power 5.0", classify_net("VBUS")[:2] == ("power", 5.0))
    check("classify -12V -> power -12.0", classify_net("-12V")[:2] == ("power", -12.0))
    check("classify 1V8 -> power 1.8", classify_net("1V8")[:2] == ("power", 1.8))
    check("classify SDA -> signal", classify_net("SDA")[0] == "signal")

    # -- 2. real third-party boards (skipped if the trees are absent) --------
    tp = os.environ.get("WYRED_THIRD_PARTY") or os.path.normpath(
        os.path.join(os.path.dirname(os.path.abspath(__file__)),
                     "..", "..", "third-party"))
    boards = [
        ("mppt-2420-hc (legacy v5 'module')",
         os.path.join(tp, "libre-solar-mppt-2420-hc/kicad/mppt-2420-hc.kicad_pcb"),
         150, 175),
        ("rpi-cm4io CM4IOv5 (modern 'footprint')",
         os.path.join(tp, "rpi-cm4io/CM4IOv5.kicad_pcb"),
         140, 160),
    ]
    for label, path, lo, hi in boards:
        print("%s:" % label)
        if not os.path.exists(path):
            print("  [skip] %s not present" % path)
            continue
        g = extract_pcb(path)
        ncomp, nnets = len(g.components), len(g.nets)
        pads = sum(len(n.nodes) for n in g.nets)
        kinds: Dict[str, int] = {}
        for c in g.components:
            kinds[c.kind] = kinds.get(c.kind, 0) + 1
        print("  components=%d  nets=%d  connected-pads=%d" % (ncomp, nnets, pads))
        print("  kinds: %s" % ", ".join(
            "%s=%d" % kv for kv in sorted(kinds.items(), key=lambda kv: -kv[1])))
        check("component count in [%d, %d]" % (lo, hi), lo <= ncomp <= hi,
              "got %d" % ncomp)
        gnds = [n for n in g.nets
                if n.kind == "ground" and n.name.rsplit("/", 1)[-1].upper() == "GND"]
        check("GND net present as ground", bool(gnds))
        pwr = {n.name.rsplit("/", 1)[-1].upper(): n for n in g.nets
               if n.kind == "power"}
        v12 = [n for n in pwr.values() if n.voltage == 12.0]
        check("a 12V power net present", bool(v12),
              "power nets: %s" % sorted(pwr)[:8])
        biggest = max(g.nets, key=lambda n: len(n.nodes))
        print("  largest net: %s (%d pads)" % (biggest.name, len(biggest.nodes)))

    # -- 3. GEOMETRY extension smoke (WyredPlanPlacement step 2.1) -----------
    # The placement-geometry extractor (pcb_geometry.py) rides on THIS
    # module's s-expression parser. Exercising its shared smoke here keeps the
    # new synthetic-dialect + real-board (Watchy/MPPT) geometry checks inside
    # the pcb_extract smoke entry point, as the step 2.1 acceptance requires,
    # while leaving every connectivity check above untouched. Imported inside
    # __main__ so the module-level pcb_extract<-pcb_geometry import stays one
    # way (additive-only: pcb_extract never depends on pcb_geometry at load).
    print()
    from pcb_geometry import run_geometry_smoke
    run_geometry_smoke(check)

    print()
    if failures:
        print("pcb_extract smoke test: FAIL (%d): %s"
              % (len(failures), "; ".join(failures)))
        sys.exit(1)
    print("pcb_extract smoke test: PASS")
    sys.exit(0)
