"""Intent-level corruptions of a *correct* golden circuit.

``mutate(golden_graph)`` returns a list of ``(mutant_graph, expected_code)``
pairs.  Each mutant is a deep copy of the input with ONE targeted, intent-level
defect injected, paired with the ERC / invariant code that defect SHOULD make
the oracle stack fire.  These drive the self-test's "the checkers actually
catch things" property: a correct golden yields zero violations, and every
mutant here yields exactly the named code.

The corruptions mirror the spec's catalogue:

  * ADDR_COLLISION       -- duplicate an ``i2c_addr`` onto a shared I2C bus.
  * UNCONNECTED_MANDATORY -- disconnect an MCU ``power_in`` from its rail and
                            strand it on an explicit ``open`` net (no silent
                            default).
  * VOLTAGE_MISMATCH     -- retarget a 3.3V ``vin`` require onto the (still
                            3.3V) rail but demand 5V -> the require no longer
                            matches the rail it sits on.
  * SHOOT_THROUGH        -- delete / bypass the #8 interlock gates so both
                            gate-drive outputs can go high together.

Not every corruption applies to every golden.  ``mutate`` inspects the graph
and only emits the mutants that are actually realizable on it (e.g. no
ADDR_COLLISION mutant is produced for a graph with fewer than two I2C
devices, and no SHOOT_THROUGH mutant for a graph without a
``mutual_exclusion`` invariant).  This keeps every returned pair a genuine,
checkable expectation regardless of which golden is passed in.

Pure Python 3 stdlib.  Plain imports only.
"""

from __future__ import annotations

import copy
from typing import List, Optional, Tuple

from schema import (
    CanonicalGraph,
    Component,
    Net,
    to_json,
    from_json,
)


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _clone(graph: CanonicalGraph) -> CanonicalGraph:
    """Deep, independent copy of a graph so a mutation never touches the input.

    We round-trip through the neutral JSON serialization when possible (it is
    the canonical, tuple-preserving copy path); ``copy.deepcopy`` is an equally
    valid fallback for anything the serializer might not carry.  Using the
    serializer keeps mutants byte-for-byte reconstructable from JSON.
    """
    try:
        return from_json(to_json(graph))
    except Exception:
        return copy.deepcopy(graph)


def _i2c_sda_net(graph: CanonicalGraph) -> Optional[Net]:
    """Return the net that looks like an I2C SDA bus, or None.

    Identified STRUCTURALLY by carrying a terminal with iface=="i2c",
    iface_member=="sda" (never by a function string).
    """
    for net in graph.nets:
        for (refdes, term_name) in net.nodes:
            comp = graph.component(refdes)
            if comp is None:
                continue
            term = comp.terminal(term_name)
            if term is not None and term.iface == "i2c" and term.iface_member == "sda":
                return net
    return None


def _i2c_addr_devices_on_bus(graph: CanonicalGraph, sda_net: Net) -> List[Component]:
    """Components that (a) sit on the given SDA net and (b) carry an i2c_addr."""
    on_bus_refs = {refdes for (refdes, _t) in sda_net.nodes}
    out: List[Component] = []
    for comp in graph.components:
        if comp.refdes in on_bus_refs and "i2c_addr" in comp.attrs:
            out.append(comp)
    return out


def _find_power_in_terminal(comp: Component):
    """First terminal on ``comp`` whose role is ``power_in``, else None."""
    for term in comp.terminals:
        if term.role == "power_in":
            return term
    return None


def _req_v(term) -> Optional[float]:
    """The required voltage of a power_in terminal (structured), else None."""
    if term.role == "power_in" and term.req_v is not None:
        return float(term.req_v)
    return None


# ---------------------------------------------------------------------------
# individual mutators -- each returns (mutant, code) or None if inapplicable
# ---------------------------------------------------------------------------

def _mut_addr_collision(graph: CanonicalGraph) -> Optional[Tuple[CanonicalGraph, str]]:
    """Duplicate an i2c_addr of one bus device onto another -> ADDR_COLLISION.

    Requires at least two distinct-address devices on a shared SDA net.
    """
    sda = _i2c_sda_net(graph)
    if sda is None:
        return None

    mutant = _clone(graph)
    sda_m = _i2c_sda_net(mutant)
    if sda_m is None:
        return None
    devices = _i2c_addr_devices_on_bus(mutant, sda_m)
    if len(devices) < 2:
        return None

    # Find two devices whose addresses currently DIFFER, then copy the first
    # device's address onto the second so they collide.
    for i in range(len(devices)):
        for j in range(len(devices)):
            if i == j:
                continue
            if devices[i].attrs.get("i2c_addr") != devices[j].attrs.get("i2c_addr"):
                devices[j].attrs["i2c_addr"] = devices[i].attrs["i2c_addr"]
                return mutant, "ADDR_COLLISION"

    # All addresses already equal (shouldn't happen for a clean golden): nothing
    # to corrupt into a *new* collision.
    return None


def _mut_unconnected_mandatory(
    graph: CanonicalGraph,
) -> Optional[Tuple[CanonicalGraph, str]]:
    """Strand an MCU power_in on an explicit open net -> UNCONNECTED_MANDATORY.

    We prefer an ``mcu`` component; failing that, any authored component with a
    ``power_in`` terminal.  The pin is removed from whatever rail carries it and
    placed on a fresh ``open`` net (no declared default), which is precisely the
    condition R6 fires on.
    """
    mutant = _clone(graph)

    target: Optional[Component] = None
    target_term = None
    # prefer an MCU
    for comp in mutant.components:
        if comp.kind == "mcu":
            t = _find_power_in_terminal(comp)
            if t is not None:
                target, target_term = comp, t
                break
    # fall back to any authored component with a power_in
    if target is None:
        for comp in mutant.components:
            if not comp.authored:
                continue
            t = _find_power_in_terminal(comp)
            if t is not None:
                target, target_term = comp, t
                break
    if target is None or target_term is None:
        return None

    node = (target.refdes, target_term.name)

    # locate & detach from its current rail
    host = mutant.net_of(node)
    if host is None:
        # already floating with no net at all -> synthesize the open net anyway
        pass
    else:
        host.nodes = [nd for nd in host.nodes if tuple(nd) != node]

    # place it on an explicit OPEN net with no default provider
    open_name = f"{target.refdes}_{target_term.name}_open"
    mutant.nets.append(Net(open_name, "open", None, [node]))
    return mutant, "UNCONNECTED_MANDATORY"


def _mut_voltage_mismatch(
    graph: CanonicalGraph,
) -> Optional[Tuple[CanonicalGraph, str]]:
    """Make a power_in require a voltage that differs from its rail -> R3.

    We find a ``power_in`` pin encoding ``vin:<v>`` that currently sits on a
    power rail at (approximately) that same voltage, then bump its required
    voltage well away from the rail so |Vp - Vr| exceeds any sane tolerance,
    WITHOUT moving the pin off the rail.
    """
    mutant = _clone(graph)

    for comp in mutant.components:
        for term in comp.terminals:
            req = _req_v(term)
            if req is None:
                continue
            net = mutant.net_of((comp.refdes, term.name))
            if net is None or net.kind != "power" or net.voltage is None:
                continue
            rail_v = float(net.voltage)
            # only mutate when the require currently MATCHES the rail (clean),
            # so we introduce a genuine, new mismatch.
            if abs(rail_v - req) > 0.25:
                continue
            # choose a clearly-different required voltage.
            new_req = 5.0 if abs(rail_v - 5.0) > 0.5 else 12.0
            if abs(new_req - rail_v) <= 0.5:
                new_req = rail_v + 10.0  # last-resort large delta
            term.req_v = new_req                 # structured require (drives R3)
            term.function = f"vin:{new_req:g}"   # human label only
            # keep any mirrored attr in sync so intent stays coherent
            if "vin_required" in comp.attrs:
                comp.attrs["vin_required"] = new_req
            if "vin" in comp.attrs:
                comp.attrs["vin"] = new_req
            return mutant, "VOLTAGE_MISMATCH"

    return None


def _mut_shoot_through(
    graph: CanonicalGraph,
) -> Optional[Tuple[CanonicalGraph, str]]:
    """Break a #8 interlock so both gate-drive outputs can be high -> SHOOT_THROUGH.

    Strategy (matches goldens._kb_shoot_through): for the first
    ``mutual_exclusion`` invariant, turn the two gate-driving logic gates
    (whose outputs are the invariant's ``a`` and ``b`` nodes) into buffers of
    their raw command, and delete the interlock feed nets to their now-removed
    second input.  With both outputs following raw commands, the assignment
    IN_H = IN_L = 1 makes both high -> the invariant model-checker finds a
    counterexample.
    """
    if not graph.invariants:
        return None

    inv = None
    for candidate in graph.invariants:
        if candidate.kind == "mutual_exclusion":
            inv = candidate
            break
    if inv is None:
        return None

    mutant = _clone(graph)
    # invariants survived the clone; grab the matching one
    inv_m = None
    for candidate in mutant.invariants:
        if (
            candidate.kind == "mutual_exclusion"
            and tuple(candidate.a) == tuple(inv.a)
            and tuple(candidate.b) == tuple(inv.b)
        ):
            inv_m = candidate
            break
    if inv_m is None:
        return None

    gate_refs = {inv_m.a[0], inv_m.b[0]}

    # The invariant's free command inputs are the "raw" signals we want each
    # output gate to follow directly once its interlock is stripped.
    command_nodes = {tuple(node) for node in inv_m.inputs}

    def _keep_input(comp: Component) -> Optional[str]:
        """Pick the logic_in pin of ``comp`` that traces to a raw command.

        Prefer the input whose net also carries one of the invariant's command
        nodes; that is the pin we retain when collapsing the gate to a buffer,
        so the buffered output follows the raw command.  Fall back to the first
        input if the trace is inconclusive (keeps the mutator robust to
        alternative wirings).
        """
        for term in comp.terminals:
            if term.role != "logic_in":
                continue
            net = mutant.net_of((comp.refdes, term.name))
            if net is None:
                continue
            if any(tuple(nd) in command_nodes for nd in net.nodes):
                return term.name
        for term in comp.terminals:
            if term.role == "logic_in":
                return term.name
        return None

    changed = False
    for comp in mutant.components:
        if comp.refdes not in gate_refs or comp.kind != "logic_gate":
            continue
        logic_outs = [t for t in comp.terminals if t.role == "logic_out"]
        if not logic_outs:
            continue
        keep_in = _keep_input(comp)
        if keep_in is None:
            continue
        keep_out = logic_outs[0].name

        drop_in_names = {
            t.name for t in comp.terminals
            if t.role == "logic_in" and t.name != keep_in
        }

        # Delete / strip the interlock complement feeds to the dropped pins.  A
        # feed net that ONLY connected the dropped pin to a complement driver is
        # removed wholesale (matching the golden known-bad); any net that also
        # carries live nodes just loses the dropped pin.
        surviving_nets: List[Net] = []
        for net in mutant.nets:
            if any(nd[0] == comp.refdes and nd[1] in drop_in_names
                   for nd in net.nodes):
                others = [
                    nd for nd in net.nodes
                    if not (nd[0] == comp.refdes and nd[1] in drop_in_names)
                ]
                # keep the net only if it still joins >=2 live nodes; a lone
                # dangling complement driver output is dropped so no inert net
                # lingers in the mutant.
                if len(others) >= 2:
                    net.nodes = others
                    surviving_nets.append(net)
                # else: net is now trivial/empty -> drop it entirely
            else:
                surviving_nets.append(net)
        mutant.nets = surviving_nets

        # Collapse the gate into a pass-through buffer of the retained command.
        comp.logic_fn = "buf"
        comp.terminals = [
            t for t in comp.terminals
            if t.name in (keep_in, keep_out)
        ]
        changed = True

    if not changed:
        return None
    return mutant, "SHOOT_THROUGH"


# ---------------------------------------------------------------------------
# public API
# ---------------------------------------------------------------------------

_MUTATORS = (
    _mut_addr_collision,
    _mut_unconnected_mandatory,
    _mut_voltage_mismatch,
    _mut_shoot_through,
)


def mutate(golden_graph: CanonicalGraph) -> List[Tuple[CanonicalGraph, str]]:
    """Return the applicable ``(mutant_graph, expected_code)`` pairs.

    Each mutant is an independent deep copy of ``golden_graph`` carrying exactly
    one injected defect.  Only corruptions realizable on the given graph are
    emitted, so every returned pair is a genuine, checkable expectation.  The
    input graph is never modified.
    """
    results: List[Tuple[CanonicalGraph, str]] = []
    for mutator in _MUTATORS:
        try:
            out = mutator(golden_graph)
        except Exception:
            # A mutator that cannot apply cleanly is simply skipped rather than
            # crashing the whole harness; the self-test still exercises the
            # mutators that do apply.
            out = None
        if out is not None:
            results.append(out)
    return results


__all__ = ["mutate"]
