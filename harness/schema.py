"""Canonical connectivity graph schema for the circuit-connectivity oracle harness.

This is the neutral, candidate-agnostic netlist representation. Every candidate
(and every parser / golden) targets THESE dataclasses, so the ERC / invariant /
equivalence layers never need to know how the graph was produced.

Pure Python 3 stdlib only. Plain imports only (this module is imported as
``from schema import CanonicalGraph`` etc. from a flat directory).
"""

from __future__ import annotations

from dataclasses import dataclass, field, asdict
from typing import Any, Dict, List, Optional, Tuple


# ---------------------------------------------------------------------------
# Controlled vocabularies (kept as plain constants; not enforced hard so that
# candidates cannot be broken by an over-strict schema, but documented here so
# the oracle layers agree on the meaning of each string).
# ---------------------------------------------------------------------------

# Component.kind
COMPONENT_KINDS = frozenset({
    "source", "ldo", "mcu", "sensor", "resistor", "capacitor", "crystal",
    "mosfet", "gate_driver", "logic_gate", "uart_device", "diode",
})

# Terminal.role
TERMINAL_ROLES = frozenset({
    "power_in", "power_out", "ground", "signal", "passive",
    "logic_in", "logic_out",
})

# Net.kind
NET_KINDS = frozenset({"power", "ground", "signal", "open"})

# logic_gate.logic_fn
LOGIC_FNS = frozenset({"and", "or", "not", "nand", "nor", "xor", "buf"})

# Net.ground_kind (v3, optional; Gen4 section 2.1). "ground" is the ordinary
# 0V return; "chassis" and "earth" are distinct kinds, never merged silently.
GROUND_KINDS = frozenset({"ground", "chassis", "earth"})

# Net.ground_role (v3, optional; soft this generation -- grader-noted, not
# oracle-gated). One unified 0V return partitioned by role tags, NOT split
# nets: analog->GNDA, digital->GNDD, power->PGND, reference->GNDREF.
GROUND_ROLES = frozenset({"none", "analog", "digital", "power", "reference"})


# ---------------------------------------------------------------------------
# Core dataclasses
# ---------------------------------------------------------------------------

@dataclass
class Terminal:
    """A single connection point (pin) on a component.

    ``name``     pin name, unique within its owning component (e.g. "VDD", "SDA").
    ``role``     one of TERMINAL_ROLES.
    ``function`` free-form HUMAN LABEL only. NOTHING in the oracle stack (ERC /
                 equivalence) keys off this string any more -- it exists purely
                 so a netlist stays readable. All electrical meaning lives in the
                 STRUCTURED fields below (see EMIT_CONTRACT.md).

    Structured fields (the emit-contract gen-2 candidates target):

    ``req_v``    float volts a ``power_in`` REQUIRES (drives R3 / part of R1).
    ``prov_v``   float volts a ``power_out`` PROVIDES (drives R5 / rail voltage).
    ``iface``    interface name this terminal participates in, e.g. "i2c",
                 "oscillator", "uart", "bootstrap" (bus detection for R4; the
                 require/provide relation for R1).
    ``iface_member``
                 the terminal's ROLE WITHIN the interface. For a bus like I2C the
                 member is the wire, e.g. "sda"/"scl". For a require/provide
                 relationship it is the literal string "require" (this terminal
                 NEEDS the interface) or "provide" (this terminal SUPPLIES it).
    """

    name: str
    role: str
    function: str = ""
    req_v: Optional[float] = None
    prov_v: Optional[float] = None
    iface: Optional[str] = None
    iface_member: Optional[str] = None


@dataclass
class Component:
    """A single instance in the design.

    ``refdes``   reference designator, unique within a graph (e.g. "U1", "R3").
    ``kind``     one of COMPONENT_KINDS.
    ``value``    human/EE value string (e.g. "3.3V", "100nF", "0x48", "STM32").
    ``authored`` True  = placed/declared by the intent author (a first-class part).
                 False = generated companion / support part (decoupling cap,
                         pull-up, bootstrap diode, ...). R7 cares about these.
    ``terminals`` ordered list of Terminal.
    ``attrs``    free dict of salient properties, e.g. {"i2c_addr": 0x48},
                 {"side": "high"}, {"voltage": 48.0}.
    ``logic_fn`` only meaningful for kind=="logic_gate"; one of LOGIC_FNS.
    """

    refdes: str
    kind: str
    value: str
    authored: bool
    terminals: List[Terminal] = field(default_factory=list)
    attrs: Dict[str, Any] = field(default_factory=dict)
    logic_fn: Optional[str] = None

    def terminal(self, name: str) -> Optional[Terminal]:
        for t in self.terminals:
            if t.name == name:
                return t
        return None


@dataclass
class Net:
    """A logical node: the set of terminals electrically joined together.

    ``name``    net name (IGNORED by equivalence; purely cosmetic).
    ``kind``    one of NET_KINDS. "open" means "declared but nothing drives it"
                and is the hook R6 uses to catch silent defaults.
    ``voltage`` rail voltage in volts for power nets, else None.
    ``nodes``   list of (refdes, terminal_name) tuples that sit on this net.

    v3 OPTIONAL ground fields (Gen4 section 2.1; backward-compatible defaults
    -- v2 documents that omit them load and serialize unchanged, and nothing
    in the v2 oracle stack keys off them; ground-role ERC is SOFT this
    generation, grader-noted not oracle-gated):

    ``ground_kind`` for kind=="ground" nets: one of GROUND_KINDS
                    ("ground"/"chassis"/"earth"), else None.
    ``ground_role`` for kind=="ground" nets: one of GROUND_ROLES
                    ("none"/"analog"/"digital"/"power"/"reference"), else None.
    ``bond``        name of the AUTHORED bond (star point / net-tie) object
                    this ground net participates in, or None. Ground
                    roles/kinds are only ever joined through a named bond,
                    never merged silently.
    """

    name: str
    kind: str
    voltage: Optional[float]
    nodes: List[Tuple[str, str]] = field(default_factory=list)
    ground_kind: Optional[str] = None
    ground_role: Optional[str] = None
    bond: Optional[str] = None


@dataclass
class Invariant:
    """A structural safety invariant to be model-checked by the invariant layer.

    For the BLDC shoot-through case (#8):
      ``kind``   == "mutual_exclusion"
      ``a``, ``b`` == the two gate-drive OUTPUT nodes (refdes, terminal) that
                   must NEVER both be logic-high simultaneously.
      ``inputs`` == the free command input nodes (the MCU PWM command pins for
                   that bridge) over which the check enumerates 2^n assignments.
    """

    kind: str
    a: Tuple[str, str]
    b: Tuple[str, str]
    inputs: List[Tuple[str, str]] = field(default_factory=list)


@dataclass
class CanonicalGraph:
    """The whole design: components + nets (+ escalations + invariants)."""

    components: List[Component] = field(default_factory=list)
    nets: List[Net] = field(default_factory=list)
    escalations: List[str] = field(default_factory=list)
    invariants: List[Invariant] = field(default_factory=list)

    # -- convenience accessors (not part of the serialization) --------------
    def component(self, refdes: str) -> Optional[Component]:
        for c in self.components:
            if c.refdes == refdes:
                return c
        return None

    def net_of(self, node: Tuple[str, str]) -> Optional[Net]:
        """Return the net carrying (refdes, terminal_name), or None."""
        for n in self.nets:
            if tuple(node) in [tuple(x) for x in n.nodes]:
                return n
        return None


# ---------------------------------------------------------------------------
# Serialization: neutral-JSON <-> CanonicalGraph
#
# We hand-roll (rather than blindly asdict()) so that:
#   * tuples survive as JSON arrays and come back as tuples,
#   * we control exactly which fields appear,
#   * from_json is tolerant of missing optional fields.
# ---------------------------------------------------------------------------

def _terminal_to_json(t: Terminal) -> Dict[str, Any]:
    return {
        "name": t.name,
        "role": t.role,
        "function": t.function,
        "req_v": t.req_v,
        "prov_v": t.prov_v,
        "iface": t.iface,
        "iface_member": t.iface_member,
    }


def _component_to_json(c: Component) -> Dict[str, Any]:
    return {
        "refdes": c.refdes,
        "kind": c.kind,
        "value": c.value,
        "authored": c.authored,
        "terminals": [_terminal_to_json(t) for t in c.terminals],
        "attrs": dict(c.attrs),
        "logic_fn": c.logic_fn,
    }


def _net_to_json(n: Net) -> Dict[str, Any]:
    d = {
        "name": n.name,
        "kind": n.kind,
        "voltage": n.voltage,
        "nodes": [[r, t] for (r, t) in n.nodes],
    }
    # v3 optional ground fields: emitted ONLY when set, so v2 documents
    # (which never set them) serialize byte-for-byte as before.
    if n.ground_kind is not None:
        d["ground_kind"] = n.ground_kind
    if n.ground_role is not None:
        d["ground_role"] = n.ground_role
    if n.bond is not None:
        d["bond"] = n.bond
    return d


def _invariant_to_json(inv: Invariant) -> Dict[str, Any]:
    return {
        "kind": inv.kind,
        "a": [inv.a[0], inv.a[1]],
        "b": [inv.b[0], inv.b[1]],
        "inputs": [[r, t] for (r, t) in inv.inputs],
    }


def to_json(graph: CanonicalGraph) -> Dict[str, Any]:
    """Serialize a CanonicalGraph to a plain JSON-safe dict."""
    return {
        "components": [_component_to_json(c) for c in graph.components],
        "nets": [_net_to_json(n) for n in graph.nets],
        "escalations": list(graph.escalations),
        "invariants": [_invariant_to_json(i) for i in graph.invariants],
    }


def _terminal_from_json(d: Dict[str, Any]) -> Terminal:
    return Terminal(
        name=d["name"],
        role=d["role"],
        function=d.get("function", ""),
        req_v=d.get("req_v"),
        prov_v=d.get("prov_v"),
        iface=d.get("iface"),
        iface_member=d.get("iface_member"),
    )


def _component_from_json(d: Dict[str, Any]) -> Component:
    return Component(
        refdes=d["refdes"],
        kind=d["kind"],
        value=d.get("value", ""),
        authored=bool(d.get("authored", True)),
        terminals=[_terminal_from_json(t) for t in d.get("terminals", [])],
        attrs=dict(d.get("attrs", {})),
        logic_fn=d.get("logic_fn"),
    )


def _net_from_json(d: Dict[str, Any]) -> Net:
    return Net(
        name=d.get("name", ""),
        kind=d.get("kind", "signal"),
        voltage=d.get("voltage"),
        nodes=[(n[0], n[1]) for n in d.get("nodes", [])],
        ground_kind=d.get("ground_kind"),
        ground_role=d.get("ground_role"),
        bond=d.get("bond"),
    )


def _invariant_from_json(d: Dict[str, Any]) -> Invariant:
    a = d["a"]
    b = d["b"]
    return Invariant(
        kind=d.get("kind", "mutual_exclusion"),
        a=(a[0], a[1]),
        b=(b[0], b[1]),
        inputs=[(n[0], n[1]) for n in d.get("inputs", [])],
    )


def from_json(d: Dict[str, Any]) -> CanonicalGraph:
    """Rebuild a CanonicalGraph from a dict produced by ``to_json``."""
    return CanonicalGraph(
        components=[_component_from_json(c) for c in d.get("components", [])],
        nets=[_net_from_json(n) for n in d.get("nets", [])],
        escalations=list(d.get("escalations", [])),
        invariants=[_invariant_from_json(i) for i in d.get("invariants", [])],
    )


__all__ = [
    "Terminal",
    "Component",
    "Net",
    "Invariant",
    "CanonicalGraph",
    "to_json",
    "from_json",
    "COMPONENT_KINDS",
    "TERMINAL_ROLES",
    "NET_KINDS",
    "LOGIC_FNS",
    "GROUND_KINDS",
    "GROUND_ROLES",
]
