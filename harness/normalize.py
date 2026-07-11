"""Normalization front-ends: various source formats -> CanonicalGraph.

The oracle stack (ERC / invariant / equivalence layers) only ever consumes the
neutral :class:`schema.CanonicalGraph`.  This module is the single place that
turns an *external* representation into that canonical form, so a candidate may
emit whichever netlist dialect it likes and still be scored by exactly one
oracle.

Three front-ends are provided:

  * :func:`from_neutral_json` -- the primary, FULLY-SPECIFIED path.  It is a thin
    wrapper over :func:`schema.from_json` (the neutral netlist serialization),
    so anything ``schema.to_json`` produced round-trips losslessly.  This is the
    format the goldens, mutate, and self-test all exchange.

  * :func:`parse_spice` -- a MINIMAL SPICE-deck reader.  SPICE has no concept of
    terminal *roles*, interface *functions*, i2c addresses, logic functions, or
    safety invariants, so a real design intent cannot survive a round-trip
    through it.  It is implemented only far enough to normalize simple
    resistor / capacitor / source / sub-circuit-instance topologies into a graph
    whose *connectivity* (net partition) is correct.  Everything that SPICE
    cannot express is marked STUBBED below and surfaced on
    ``graph.escalations`` so no information is silently invented.

  * :func:`parse_kicad_netlist` -- a MINIMAL KiCad ``.net`` (S-expression) reader.
    KiCad *does* carry refdes, value, and named pins with net membership, so it
    reconstructs the net partition and per-component pin lists faithfully.  It
    still cannot recover terminal roles / functions / attrs / invariants, so
    those are heuristically inferred where unambiguous and otherwise left blank
    with an escalation note.  Marked STUBBED where inference is used.

Design rule honoured throughout: **no silent defaults**.  Whenever a front-end
cannot recover a piece of intent, it does NOT guess a plausible value quietly --
it either leaves the field empty/OPEN or records a note on
``CanonicalGraph.escalations`` so a downstream reader knows the graph is only a
connectivity skeleton.

Pure Python 3 stdlib.  Plain imports only (flat harness directory).
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple

from schema import (
    CanonicalGraph,
    Component,
    Net,
    Terminal,
    from_json as _schema_from_json,
)


# ===========================================================================
# 1. Neutral JSON  (COMPLETE / authoritative path)
# ===========================================================================

def from_neutral_json(data: Dict[str, Any]) -> CanonicalGraph:
    """Rebuild a :class:`CanonicalGraph` from the neutral-JSON dict.

    This is the canonical, fully-specified normalizer: it delegates to
    :func:`schema.from_json`, so any dict produced by :func:`schema.to_json`
    round-trips with no loss (components, nets, escalations, invariants).

    Accepts either a decoded ``dict`` (preferred) or a JSON ``str``/``bytes``
    for convenience; anything else raises ``TypeError``.
    """
    if isinstance(data, (str, bytes, bytearray)):
        import json

        data = json.loads(data)
    if not isinstance(data, dict):
        raise TypeError(
            "from_neutral_json expects a decoded dict (or JSON text), "
            f"got {type(data).__name__}"
        )
    return _schema_from_json(data)


# ===========================================================================
# 2. SPICE  (MINIMAL / STUBBED -- connectivity only)
# ===========================================================================
#
# Supported subset (enough to normalize simple RC / source / subckt-instance
# decks; NOT a general SPICE parser):
#
#   * Line-based; ``*`` starts a full-line comment; ``+`` continues the prior
#     line; blank lines ignored; case-insensitive element letters.
#   * ``.title``/``.end``/``.ends`` and other dot-cards are ignored except that
#     ``.subckt`` .. ``.ends`` blocks are skipped (definitions, not instances).
#   * Two-terminal elements:  R/C/L/D  name n1 n2 [value]
#         R -> resistor, C -> capacitor, L -> resistor(stub), D -> diode.
#         pins named "1","2" (D -> "A","K").
#   * Independent sources:  V/I  name n+ n- [DC value]
#         V -> kind "source" with terminals VOUT(n+)/GND(n-); value parsed.
#   * Subcircuit instances:  X  name node... subcktname
#         -> kind "sensor" placeholder with generic pins P1..Pk (STUBBED: we do
#            not expand the subckt; we only preserve the instance's connectivity).
#
# The node named "0" (SPICE global ground) becomes a ground net "GND".
#
# STUBBED / not recoverable from SPICE and therefore escalated:
#   terminal roles & functions (except the obvious source/ground/diode cases),
#   i2c addresses, logic functions, mutual-exclusion invariants, authored flag
#   (everything defaults authored=True), rail voltages on passive nets.
# ===========================================================================

_SPICE_STUB_NOTE = (
    "STUBBED(SPICE): SPICE decks carry no terminal roles/functions, i2c "
    "addresses, logic functions or safety invariants; this graph is a "
    "connectivity skeleton only. Do not treat absence of ERC intent as PASS."
)


def _spice_logical_lines(text: str) -> List[str]:
    """Split a SPICE deck into logical lines.

    Handles ``+`` continuation (folded onto the previous physical line) and
    strips full-line ``*`` comments and blank lines. Trailing ``;`` inline
    comments are dropped.
    """
    logical: List[str] = []
    for raw in text.splitlines():
        line = raw.rstrip("\n")
        stripped = line.strip()
        if not stripped:
            continue
        if stripped.startswith("*"):
            continue
        # inline comment: everything after ';' is a comment in many dialects
        if ";" in stripped:
            stripped = stripped.split(";", 1)[0].strip()
            if not stripped:
                continue
        if stripped.startswith("+"):
            cont = stripped[1:].strip()
            if logical:
                logical[-1] = logical[-1] + " " + cont
            else:
                logical.append(cont)
        else:
            logical.append(stripped)
    return logical


def _spice_parse_value(tokens: List[str]) -> str:
    """Best-effort value string from the trailing tokens of an element line.

    We keep the raw token(s) verbatim rather than converting units, because the
    canonical ``value`` field is a human/EE string (e.g. "100nF", "4.7k").
    Recognizes an optional leading "DC"/"dc" keyword for sources.
    """
    if not tokens:
        return ""
    toks = list(tokens)
    if toks and toks[0].lower() in ("dc", "ac"):
        toks = toks[1:]
    return " ".join(toks).strip()


def _spice_source_volts(value: str) -> Optional[float]:
    """Parse a leading float (volts) out of a source value string, else None."""
    if not value:
        return None
    tok = value.split()[0]
    # strip a trailing unit letter set if present (e.g. "5", "5.0", "5V")
    num = []
    for ch in tok:
        if ch.isdigit() or ch in ".+-eE":
            num.append(ch)
        else:
            break
    try:
        return float("".join(num))
    except ValueError:
        return None


def _add_node(nets_by_node: Dict[str, List[Tuple[str, str]]], node: str,
              ref: str, pin: str) -> None:
    nets_by_node.setdefault(node, []).append((ref, pin))


def parse_spice(text: str) -> CanonicalGraph:
    """Parse a MINIMAL SPICE deck into a CanonicalGraph (connectivity only).

    This is intentionally a *stub-grade* front-end (see module docstring): it
    reconstructs the net partition and a plausible per-element terminal list,
    but cannot recover ERC intent. It records a STUBBED escalation so the
    caller never mistakes the resulting clean-looking graph for verified intent.
    """
    components: List[Component] = []
    # node-name -> list of (refdes, pin) sitting on it
    nets_by_node: Dict[str, List[Tuple[str, str]]] = {}
    # node-name -> True if it is a ground node (SPICE "0" or explicit gnd)
    ground_nodes = {"0", "gnd", "GND"}

    in_subckt = False
    for line in _spice_logical_lines(text):
        toks = line.split()
        if not toks:
            continue
        head = toks[0]
        low = head.lower()

        # --- dot cards ------------------------------------------------------
        if head.startswith("."):
            if low == ".subckt":
                in_subckt = True
            elif low == ".ends":
                in_subckt = False
            # all other dot-cards (.title/.end/.model/.tran/...) ignored
            continue
        if in_subckt:
            # we skip subckt *definitions*; instances (X...) live outside
            continue

        letter = low[0]

        # --- two-terminal passive/diode elements ---------------------------
        if letter in ("r", "c", "l", "d"):
            if len(toks) < 3:
                continue  # malformed; skip defensively
            ref = head
            n1, n2 = toks[1], toks[2]
            value = _spice_parse_value(toks[3:])
            if letter == "r" or letter == "l":
                kind = "resistor"
                pins = [("1", n1), ("2", n2)]
                terms = [Terminal("1", "passive"), Terminal("2", "passive")]
            elif letter == "c":
                kind = "capacitor"
                pins = [("+", n1), ("-", n2)]
                terms = [Terminal("+", "passive"), Terminal("-", "passive")]
            else:  # d -> diode; anode/cathode
                kind = "diode"
                pins = [("A", n1), ("K", n2)]
                terms = [Terminal("A", "passive"), Terminal("K", "passive")]
            components.append(Component(
                refdes=ref, kind=kind, value=value, authored=True,
                terminals=terms,
            ))
            for pin, node in pins:
                _add_node(nets_by_node, node, ref, pin)
            continue

        # --- independent sources V / I -------------------------------------
        if letter in ("v", "i"):
            if len(toks) < 3:
                continue
            ref = head
            nplus, nminus = toks[1], toks[2]
            value = _spice_parse_value(toks[3:])
            volts = _spice_source_volts(value)
            attrs: Dict[str, Any] = {}
            if volts is not None:
                attrs["voltage"] = volts
            components.append(Component(
                refdes=ref, kind="source",
                value=value or (f"{volts}V" if volts is not None else ""),
                authored=True,
                terminals=[
                    Terminal("VOUT", "power_out",
                             f"vout:{volts}" if volts is not None else "",
                             prov_v=volts),   # structured provided-voltage
                    Terminal("GND", "ground"),
                ],
                attrs=attrs,
            ))
            _add_node(nets_by_node, nplus, ref, "VOUT")
            _add_node(nets_by_node, nminus, ref, "GND")
            continue

        # --- subcircuit instance X -----------------------------------------
        if letter == "x":
            # X<name> node1 node2 ... subcktname
            if len(toks) < 3:
                continue
            ref = head
            nodes = toks[1:-1]        # last token is the subckt name
            subckt_name = toks[-1]
            terms = [Terminal(f"P{i + 1}", "passive") for i in range(len(nodes))]
            components.append(Component(
                refdes=ref, kind="sensor",   # generic placeholder kind
                value=subckt_name, authored=True, terminals=terms,
                attrs={"stub": "spice_subckt_instance"},
            ))
            for i, node in enumerate(nodes):
                _add_node(nets_by_node, node, ref, f"P{i + 1}")
            continue

        # Unknown element letter (Q/M/E/G/...): preserve connectivity with a
        # generic multi-pin placeholder so the net partition stays complete.
        if len(toks) >= 3:
            ref = head
            nodes = toks[1:]
            # heuristically drop a trailing non-node model token if it is not a
            # number and not already used as a node elsewhere -- but to stay
            # safe and lossless on connectivity we keep every token as a node.
            terms = [Terminal(f"P{i + 1}", "passive") for i in range(len(nodes))]
            components.append(Component(
                refdes=ref, kind="mosfet" if letter in ("m", "q") else "sensor",
                value="", authored=True, terminals=terms,
                attrs={"stub": f"spice_element_{letter}"},
            ))
            for i, node in enumerate(nodes):
                _add_node(nets_by_node, node, ref, f"P{i + 1}")
            continue

    nets = _nets_from_node_map(nets_by_node, ground_nodes)
    return CanonicalGraph(
        components=components,
        nets=nets,
        escalations=[_SPICE_STUB_NOTE],
    )


# ===========================================================================
# 3. KiCad netlist  (MINIMAL / partially STUBBED)
# ===========================================================================
#
# KiCad exports an S-expression ``.net``. We read the two sections we need:
#
#   (components (comp (ref "U1") (value "MCU") ...) ...)
#   (nets (net (code "1") (name "V3V3") (node (ref "U1") (pin "1")) ...) ...)
#
# From these we can reconstruct: refdes, value, the net partition, and each
# component's pin list (union of pins seen across nets). What KiCad does NOT
# carry (and we therefore CANNOT recover): terminal roles/functions, kinds,
# i2c addresses, logic functions, authored flags, safety invariants.
#
# We infer ``kind`` heuristically from the refdes prefix (R->resistor,
# C->capacitor, etc.) and mark everything else STUBBED with an escalation.
# ===========================================================================

_KICAD_STUB_NOTE = (
    "STUBBED(KiCad): KiCad netlists carry refdes/value/pin-net membership but "
    "no terminal roles/functions, kinds, i2c addresses, logic functions or "
    "safety invariants; kind is inferred from the refdes prefix and all pins "
    "default to role 'passive'. Connectivity is faithful; ERC intent is not."
)

# refdes-prefix -> canonical kind (best-effort; documented as heuristic)
_REFDES_KIND = {
    "R": "resistor",
    "C": "capacitor",
    "L": "resistor",
    "D": "diode",
    "Q": "mosfet",
    "M": "mosfet",
    "Y": "crystal",
    "X": "crystal",
    "U": "sensor",     # generic IC placeholder (mcu/ldo/sensor indistinguishable)
    "PS": "source",
    "V": "source",
    "BT": "source",
}


# Sentinel prefix marking a token that came from a quoted string literal.
# A NUL byte can never appear in a legitimate KiCad atom, so it unambiguously
# distinguishes  (a quoted "(" or "" )  from a real list opener / bare atom.
_STR_MARK = "\x00"


def _sexpr_tokenize(text: str) -> List[str]:
    """Tokenize an S-expression into '(' , ')' and atom/string tokens.

    Quoted string tokens are returned with a leading ``_STR_MARK`` sentinel so
    the parser and :func:`_atom` can recover the exact literal (including one
    that contains parentheses or is empty)."""
    tokens: List[str] = []
    i = 0
    n = len(text)
    while i < n:
        ch = text[i]
        if ch in "()":
            tokens.append(ch)
            i += 1
        elif ch.isspace():
            i += 1
        elif ch == '"':
            # quoted string; handle backslash escapes
            j = i + 1
            buf = []
            while j < n and text[j] != '"':
                if text[j] == "\\" and j + 1 < n:
                    buf.append(text[j + 1])
                    j += 2
                else:
                    buf.append(text[j])
                    j += 1
            # Prefix with a NUL sentinel that can never occur in a real atom,
            # so the reader can tell a quoted "(" literal from a list opener.
            tokens.append(_STR_MARK + "".join(buf))
            i = j + 1
        else:
            j = i
            while j < n and not text[j].isspace() and text[j] not in "()":
                j += 1
            tokens.append(text[i:j])
            i = j
    return tokens


def _sexpr_parse(text: str) -> Any:
    """Parse S-expression text into nested lists. Strings keep a leading '"'
    marker so the reader can distinguish "(" atoms from quoted literals."""
    tokens = _sexpr_tokenize(text)
    pos = 0

    def parse() -> Any:
        nonlocal pos
        if pos >= len(tokens):
            raise ValueError("unexpected end of S-expression")
        tok = tokens[pos]
        if tok == "(":
            pos += 1
            lst: List[Any] = []
            while pos < len(tokens) and tokens[pos] != ")":
                lst.append(parse())
            if pos >= len(tokens):
                raise ValueError("unbalanced '(' in S-expression")
            pos += 1  # consume ')'
            return lst
        elif tok == ")":
            raise ValueError("unexpected ')' in S-expression")
        else:
            pos += 1
            return tok

    # A .net file is a single top-level (export ...) form; parse it.
    result = parse()
    return result


def _atom(x: Any) -> str:
    """Return the plain string value of a leaf token (strip the string marker)."""
    if isinstance(x, str) and x.startswith(_STR_MARK):
        return x[len(_STR_MARK):]
    return x


def _find_all(node: Any, head: str) -> List[List[Any]]:
    """Return every sub-list whose first element atom equals ``head``."""
    out: List[List[Any]] = []
    if isinstance(node, list):
        if node and not isinstance(node[0], list) and _atom(node[0]) == head:
            out.append(node)
        for child in node:
            out.extend(_find_all(child, head))
    return out


def _field(node: List[Any], head: str) -> Optional[str]:
    """Return the single atom value of the direct child list ``(head value)``."""
    for child in node:
        if (isinstance(child, list) and child
                and not isinstance(child[0], list)
                and _atom(child[0]) == head):
            if len(child) >= 2:
                return _atom(child[1])
            return ""
    return None


def _kind_from_refdes(ref: str) -> str:
    """Infer a canonical kind from the refdes prefix (heuristic, STUBBED)."""
    # longest matching alpha prefix first (so "PS" beats "P")
    prefix = "".join(ch for ch in ref if ch.isalpha())
    for cand in (prefix, prefix[:2], prefix[:1]):
        if cand in _REFDES_KIND:
            return _REFDES_KIND[cand]
    return "sensor"


def parse_kicad_netlist(text: str) -> CanonicalGraph:
    """Parse a MINIMAL KiCad ``.net`` S-expression into a CanonicalGraph.

    Faithfully reconstructs refdes / value / net-partition / per-component pin
    lists. Terminal roles default to 'passive' and kind is inferred from the
    refdes prefix (both STUBBED and flagged on ``escalations``).
    """
    root = _sexpr_parse(text)

    # ---- components section -------------------------------------------------
    # value + declared kind (if any) keyed by refdes
    comp_value: Dict[str, str] = {}
    for comp in _find_all(root, "comp"):
        ref = _field(comp, "ref")
        if ref is None:
            continue
        comp_value[ref] = _field(comp, "value") or ""

    # ---- nets section: build partition + collect pins per component ---------
    nets: List[Net] = []
    pins_by_ref: Dict[str, List[str]] = {}
    # preserve first-seen order of refdes for deterministic component list
    ref_order: List[str] = list(comp_value.keys())

    for net in _find_all(root, "net"):
        net_name = _field(net, "name") or ""
        nodes: List[Tuple[str, str]] = []
        for node in _find_all(net, "node"):
            ref = _field(node, "ref")
            pin = _field(node, "pin")
            if ref is None or pin is None:
                continue
            nodes.append((ref, pin))
            pins_by_ref.setdefault(ref, [])
            if pin not in pins_by_ref[ref]:
                pins_by_ref[ref].append(pin)
            if ref not in ref_order:
                ref_order.append(ref)
        # classify net kind from its name heuristically (STUBBED)
        low = net_name.lower()
        if "gnd" in low or low in ("0", "vss"):
            kind, voltage = "ground", 0.0
        elif low.startswith("v") or low.startswith("+") or "vcc" in low \
                or "vdd" in low or "rail" in low:
            kind, voltage = "power", None
        else:
            kind, voltage = "signal", None
        nets.append(Net(name=net_name, kind=kind, voltage=voltage, nodes=nodes))

    # ---- build components with their (union) pin lists ----------------------
    components: List[Component] = []
    for ref in ref_order:
        pins = pins_by_ref.get(ref, [])
        # ground-ish pin gets a ground role; everything else stays 'passive'
        terms: List[Terminal] = []
        for pin in pins:
            role = "ground" if pin.upper() in ("GND", "VSS") else "passive"
            terms.append(Terminal(name=pin, role=role))
        components.append(Component(
            refdes=ref,
            kind=_kind_from_refdes(ref),
            value=comp_value.get(ref, ""),
            authored=True,
            terminals=terms,
            attrs={"stub": "kicad_import"},
        ))

    return CanonicalGraph(
        components=components,
        nets=nets,
        escalations=[_KICAD_STUB_NOTE],
    )


# ===========================================================================
# shared helper
# ===========================================================================

def _nets_from_node_map(
    nets_by_node: Dict[str, List[Tuple[str, str]]],
    ground_nodes: set,
) -> List[Net]:
    """Turn a node-name -> [(ref,pin)] map into a list of Net objects.

    Ground nodes (SPICE "0", "gnd") become a "ground" net at 0.0V; everything
    else becomes a "signal" net (SPICE cannot tell us rail voltages reliably).
    Deterministic order: ground first, then remaining nodes in insertion order.
    """
    nets: List[Net] = []
    # ground(s) first
    for node, members in nets_by_node.items():
        if node in ground_nodes:
            nets.append(Net(name="GND", kind="ground", voltage=0.0, nodes=list(members)))
    for node, members in nets_by_node.items():
        if node in ground_nodes:
            continue
        nets.append(Net(name=node, kind="signal", voltage=None, nodes=list(members)))
    return nets


__all__ = [
    "from_neutral_json",
    "parse_spice",
    "parse_kicad_netlist",
]
