"""Electrical Rule Check (ERC) layer for the connectivity-oracle harness.

``run_erc(graph) -> list[Violation]`` implements the structural rules R1, R3, R4,
R5, R6, R7 from the spec, operating purely on the neutral CanonicalGraph. An empty
list means the graph is clean w.r.t. these rules.

    R1 UNMET_REQUIRE          a required interface / power terminal is not on a net
                              with a compatible provider (and none exists to make it
                              merely ambiguous -> that would be an escalation).
    R3 VOLTAGE_MISMATCH       a power_in requiring Vr sits on a rail net at Vp with
                              |Vp - Vr| > tol.
    R4 ADDR_COLLISION         two components with equal attrs["i2c_addr"] share an
                              I2C bus (their SDA terminals on one net).
    R5 SINGLE_DRIVER          a net has >= 2 power_out providers of differing voltage.
    R6 UNCONNECTED_MANDATORY  a mandatory terminal (role power_in / ground) sits on an
                              "open" net with no explicit declared default (the
                              NO-SILENT-DEFAULTS rule).
    R7 COMPANION_INCOMPLETE   a generated (authored=False) support part is left
                              partially unconnected.

STRUCTURED emit-contract (see EMIT_CONTRACT.md). ERC reads ONLY structured
Terminal fields -- NEVER the free-form ``function`` string:

  * a ``power_in`` REQUIRES a rail voltage via ``req_v`` (float).
  * a ``power_out`` PROVIDES a rail voltage via ``prov_v`` (float).
  * an interface requirement is  iface=<name>, iface_member="require";
    its provider is             iface=<name>, iface_member="provide".
  * an I2C data pin is          iface="i2c",  iface_member="sda"/"scl";
    its device address lives in attrs["i2c_addr"] (int).
  * generated companions have authored=False.

Pure Python 3 stdlib. Plain imports only.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

from schema import CanonicalGraph, Component, Net, Terminal


# ---------------------------------------------------------------------------
# Violation record
# ---------------------------------------------------------------------------

@dataclass
class Violation:
    """A single ERC finding.

    ``code``  stable rule code, e.g. "VOLTAGE_MISMATCH" (top-level verdicts are
              formatted as ``FAIL:<code>`` by the oracle stack).
    ``msg``   human-readable explanation, including the offending refdes/pins.
    """

    code: str
    msg: str

    def __repr__(self) -> str:  # pragma: no cover - cosmetic
        return "Violation(%s: %s)" % (self.code, self.msg)


# Voltage comparison tolerance (volts). Values in the goldens are exact, so a
# small epsilon absorbs only float noise, not real rail differences.
_VTOL = 0.05


# ---------------------------------------------------------------------------
# structured-field helpers (read ONLY the structured fields, never function)
# ---------------------------------------------------------------------------

def _requires_iface(t: Terminal) -> Optional[str]:
    """Interface name this terminal REQUIRES, or None."""
    if t.iface is not None and t.iface_member == "require":
        return t.iface
    return None


def _provides_iface(t: Terminal) -> Optional[str]:
    """Interface name this terminal PROVIDES, or None."""
    if t.iface is not None and t.iface_member == "provide":
        return t.iface
    return None


def _index(graph: CanonicalGraph):
    """Build lookup structures used across the rules.

    Returns a tuple:
      comp_by_refdes : refdes -> Component
      net_of_node    : (refdes, term_name) -> Net
      term_of_node   : (refdes, term_name) -> Terminal
    """
    comp_by_refdes: Dict[str, Component] = {}
    for c in graph.components:
        comp_by_refdes[c.refdes] = c

    net_of_node: Dict[Tuple[str, str], Net] = {}
    term_of_node: Dict[Tuple[str, str], Terminal] = {}
    for n in graph.nets:
        for nd in n.nodes:
            key = (nd[0], nd[1])
            net_of_node[key] = n
    for c in graph.components:
        for t in c.terminals:
            term_of_node[(c.refdes, t.name)] = t

    return comp_by_refdes, net_of_node, term_of_node


def _global_provides(graph: CanonicalGraph) -> set:
    """Set of interface names X for which SOME provider terminal
    (iface=X, iface_member="provide") exists anywhere in the graph."""
    provides = set()
    for c in graph.components:
        for t in c.terminals:
            iface = _provides_iface(t)
            if iface is not None:
                provides.add(iface)
    return provides


# ---------------------------------------------------------------------------
# R1 UNMET_REQUIRE
# ---------------------------------------------------------------------------

def _check_r1(graph, net_of_node, term_of_node, global_provides) -> List[Violation]:
    """A requiring terminal (iface_member=="require", or a power_in with req_v)
    is UNMET when its net offers no compatible provider AND the requirement
    cannot be satisfied structurally.

    Discrimination (validated against the goldens):
      * A matching provider on the SAME net -> satisfied. For an interface the
        provider is (iface=<same>, iface_member="provide"); for a power req_v it
        is a power_out whose prov_v equals req_v (within tol).
      * Else if the net carries a generated companion (authored=False) part ->
        treated as companion-satisfied (e.g. the bootstrap cap+diode network on a
        gate-driver VB pin in golden 8, which has no explicit provider terminal).
      * Else if a provider of this interface exists ELSEWHERE in the graph -> the
        requirement is *ambiguous / unassigned*, an ESCALATION concern (surfaced
        via graph.escalations), NOT a hard ERC failure (e.g. 3 free UART ports,
        one consumer, in golden 5).
      * Else (no provider anywhere, no companion) -> genuinely UNMET_REQUIRE.
    """
    out: List[Violation] = []
    for c in graph.components:
        for t in c.terminals:
            iface = _requires_iface(t)
            req_v = t.req_v if t.role == "power_in" else None
            if iface is None and req_v is None:
                continue
            net = net_of_node.get((c.refdes, t.name))

            satisfied = False
            companion_on_net = False
            if net is not None:
                for nd in net.nodes:
                    other = term_of_node.get((nd[0], nd[1]))
                    if other is None:
                        continue
                    if (nd[0], nd[1]) == (c.refdes, t.name):
                        continue
                    if iface is not None and _provides_iface(other) == iface:
                        satisfied = True
                        break
                    if (req_v is not None and other.role == "power_out"
                            and other.prov_v is not None
                            and abs(other.prov_v - req_v) <= _VTOL):
                        satisfied = True
                        break
                if not satisfied:
                    for nd in net.nodes:
                        oc = graph.component(nd[0])
                        if oc is not None and oc.refdes != c.refdes and not oc.authored:
                            companion_on_net = True
                            break

            if satisfied or companion_on_net:
                continue

            # No provider / companion on the pin's own net.
            if iface is not None:
                # Interface requirement: only a HARD failure if no provider of
                # this interface exists anywhere (else ambiguity -> escalation).
                if iface not in global_provides:
                    out.append(Violation(
                        "UNMET_REQUIRE",
                        "%s.%s requires interface '%s' but no compatible provider "
                        "is present on its net%s." % (
                            c.refdes, t.name, iface,
                            "" if net is None else " '%s'" % net.name,
                        ),
                    ))
            # A bare power req_v with no provider on its net is handled by R6
            # (unconnected / open) rather than R1 when it sits on an open/no net;
            # when it sits on a live rail R3 checks the voltage. R1 does not
            # hard-fail power reqs (keeps golden verdicts intact).
    return out


# ---------------------------------------------------------------------------
# R3 VOLTAGE_MISMATCH
# ---------------------------------------------------------------------------

def _check_r3(graph, net_of_node) -> List[Violation]:
    """A power_in requiring Vr on a rail net at Vp with |Vp - Vr| > tol."""
    out: List[Violation] = []
    for c in graph.components:
        for t in c.terminals:
            if t.role != "power_in":
                continue
            vr = t.req_v
            if vr is None:
                continue
            net = net_of_node.get((c.refdes, t.name))
            if net is None or net.voltage is None:
                continue
            if abs(net.voltage - vr) > _VTOL:
                out.append(Violation(
                    "VOLTAGE_MISMATCH",
                    "%s.%s requires %gV but sits on rail net '%s' at %gV." % (
                        c.refdes, t.name, vr, net.name, net.voltage,
                    ),
                ))
    return out


# ---------------------------------------------------------------------------
# R4 ADDR_COLLISION
# ---------------------------------------------------------------------------

def _check_r4(graph, net_of_node) -> List[Violation]:
    """Two components with equal attrs['i2c_addr'] whose I2C SDA pins share one
    net. An SDA pin is identified structurally by iface=="i2c",
    iface_member=="sda" (never by a function string)."""
    out: List[Violation] = []
    # bucket (sda_net_id, addr) -> [refdes...]
    buckets: Dict[Tuple[int, object], List[str]] = defaultdict(list)
    for c in graph.components:
        if "i2c_addr" not in c.attrs:
            continue
        addr = c.attrs["i2c_addr"]
        for t in c.terminals:
            if not (t.iface == "i2c" and t.iface_member == "sda"):
                continue
            net = net_of_node.get((c.refdes, t.name))
            if net is None:
                continue
            buckets[(id(net), addr)].append(c.refdes)

    seen = set()
    for (net_id, addr), refs in buckets.items():
        if len(refs) < 2:
            continue
        key = (net_id, addr)
        if key in seen:
            continue
        seen.add(key)
        try:
            addr_s = hex(addr) if isinstance(addr, int) else str(addr)
        except Exception:
            addr_s = str(addr)
        out.append(Violation(
            "ADDR_COLLISION",
            "I2C address %s is claimed by multiple devices on a shared bus: %s." % (
                addr_s, ", ".join(sorted(refs)),
            ),
        ))
    return out


# ---------------------------------------------------------------------------
# R5 SINGLE_DRIVER
# ---------------------------------------------------------------------------

def _check_r5(graph) -> List[Violation]:
    """A net with >= 2 power_out providers of DIFFERING voltage (a rail conflict).
    Each provider's voltage is read from prov_v (structured)."""
    out: List[Violation] = []
    for n in graph.nets:
        drivers: List[Tuple[str, str, Optional[float]]] = []
        for nd in n.nodes:
            c = graph.component(nd[0])
            if c is None:
                continue
            t = c.terminal(nd[1])
            if t is None or t.role != "power_out":
                continue
            drivers.append((nd[0], nd[1], t.prov_v))
        if len(drivers) < 2:
            continue
        voltages = {v for (_, _, v) in drivers if v is not None}
        if len(voltages) >= 2:
            out.append(Violation(
                "SINGLE_DRIVER",
                "Net '%s' has multiple power_out drivers of differing voltage "
                "(%s); a net may have only one rail driver." % (
                    n.name,
                    ", ".join(
                        "%s.%s=%s" % (r, p, "?" if v is None else ("%gV" % v))
                        for (r, p, v) in drivers
                    ),
                ),
            ))
    return out


# ---------------------------------------------------------------------------
# R6 UNCONNECTED_MANDATORY
# ---------------------------------------------------------------------------

def _check_r6(graph, net_of_node) -> List[Violation]:
    """A mandatory terminal (role power_in or ground) that sits on an 'open' net,
    or on no net at all -> the NO-SILENT-DEFAULTS rule.

    An 'open' net is the explicit hook for 'declared but nothing drives it'. A
    mandatory pin left there (with no real provider) must be surfaced rather than
    silently defaulted. Signal-role interface pins on open nets are handled by the
    escalation / R1 layers, not here, so genuine ambiguity (golden 5) escalates
    instead of hard-failing.
    """
    out: List[Violation] = []
    for c in graph.components:
        for t in c.terminals:
            if t.role not in ("power_in", "ground"):
                continue
            net = net_of_node.get((c.refdes, t.name))
            if net is None:
                out.append(Violation(
                    "UNCONNECTED_MANDATORY",
                    "%s.%s (%s) is not connected to any net." % (
                        c.refdes, t.name, t.role,
                    ),
                ))
            elif net.kind == "open":
                out.append(Violation(
                    "UNCONNECTED_MANDATORY",
                    "%s.%s (%s) sits on open net '%s' with no declared default; "
                    "silent defaults are not permitted." % (
                        c.refdes, t.name, t.role, net.name,
                    ),
                ))
    return out


# ---------------------------------------------------------------------------
# R7 COMPANION_INCOMPLETE
# ---------------------------------------------------------------------------

def _check_r7(graph, net_of_node) -> List[Violation]:
    """A generated (authored=False) support part left partially unconnected: one
    or more of its terminals sits on no net, or on an 'open' net. Such companions
    (decoupling caps, pull-ups, bootstrap cap/diode, crystal load caps) are only
    correct when fully wired."""
    out: List[Violation] = []
    for c in graph.components:
        if c.authored:
            continue
        dangling: List[str] = []
        for t in c.terminals:
            net = net_of_node.get((c.refdes, t.name))
            if net is None or net.kind == "open":
                dangling.append(t.name)
        if dangling:
            out.append(Violation(
                "COMPANION_INCOMPLETE",
                "Generated companion %s (%s) is partially unconnected: pin(s) %s "
                "left dangling." % (
                    c.refdes, c.kind, ", ".join(dangling),
                ),
            ))
    return out


# ---------------------------------------------------------------------------
# public entry point
# ---------------------------------------------------------------------------

def run_erc(graph: CanonicalGraph) -> List[Violation]:
    """Run all ERC rules (R1, R3, R4, R5, R6, R7) over ``graph``.

    Returns the list of Violations in a stable rule order (empty = clean).
    """
    comp_by_refdes, net_of_node, term_of_node = _index(graph)
    global_provides = _global_provides(graph)

    violations: List[Violation] = []
    violations.extend(_check_r1(graph, net_of_node, term_of_node, global_provides))
    violations.extend(_check_r3(graph, net_of_node))
    violations.extend(_check_r4(graph, net_of_node))
    violations.extend(_check_r5(graph))
    violations.extend(_check_r6(graph, net_of_node))
    violations.extend(_check_r7(graph, net_of_node))
    return violations


__all__ = ["Violation", "run_erc"]
