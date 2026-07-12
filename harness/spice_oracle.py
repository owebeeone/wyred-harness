#!/usr/bin/env python3
"""wyred-harness SPICE operating-point oracle — declared rails vs simulation.

WyredPlanSpice step 2.2 (WyredWorkflowDesign §3.4, the cheap DC sanity
differential of the third denotation). This is the ASSERTED-vs-REALIZED
*verdict*: given an artifact set's declared power rails (the L2 ``nets``),
its ``.cir.json`` sidecar (the net->deck-node map + the ``not_simulated``
confession) and a DC operating-point measurement record (``spice_run``'s
``<name>.spiceop.json`` payload — node voltages read back from ngspice), it
requires each declared power rail to sit within its declared tolerance of the
simulated node voltage. The runner (step 2.1, ``harness/spice_run.py``)
MEASURES; this module GRADES.

Two codes (WyredSpiceContract §8, the contract's third and distinct
tolerance — ``sim``, unconfusable with design-time ``VTOL`` and the testplan
measurement tolerances):

* ``SPICE_RAIL_OFF`` — a declared rail's simulated node voltage is outside
  tolerance (``|simulated - declared| > declared * tol%``). The finding names
  the net, the declared volts, the simulated volts and the tolerance.
* ``SPICE_RAIL_UNRESOLVED`` — a declared rail's deck node is absent from the
  op results AND the rail is not covered by the ``not_simulated`` confession
  (confession over absence, again: a rail whose parts were all honestly
  confessed as unmodelled is legitimately unsimulated; a modelled rail that
  vanishes from the op point is an anomaly, never a silent pass).

Tolerance (WyredSpiceContract §8): **contract default ±2 %**, overridable per
rail via the L2 net attr ``sim_tol_pct``.

Fences (wyred-harness/CLAUDE.md, RunnerSplit.md): this oracle NEVER imports
the engine (``wyred``) and never runs a simulator — it is a pure function of
artifacts re-read from disk (the L2, the sidecar, the measurement record).
ngspice delegation lives in ``spice_run``; this module only compares numbers.
Pure Python 3.10 stdlib.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List, Optional

# WyredSpiceContract §8: the contract default sim tolerance (percent).
DEFAULT_SIM_TOL_PCT = 2.0

SPICE_RAIL_OFF = "SPICE_RAIL_OFF"
SPICE_RAIL_UNRESOLVED = "SPICE_RAIL_UNRESOLVED"


@dataclass
class RailFinding:
    """One rail verdict. ``code`` is the load-bearing field
    (``SPICE_RAIL_OFF`` / ``SPICE_RAIL_UNRESOLVED``); ``msg`` names the net,
    the declared and simulated volts, and the tolerance so a gate line is
    self-explanatory. The structured fields let the gate/tests assert on the
    numbers without re-parsing ``msg``."""

    code: str
    msg: str
    net: str
    declared: Optional[float] = None
    simulated: Optional[float] = None
    tol_pct: Optional[float] = None
    node: Optional[str] = None

    def __repr__(self) -> str:  # pragma: no cover - cosmetic
        return "RailFinding(%s: %s)" % (self.code, self.msg)


def _fmt(x: float) -> str:
    return "%.6g" % x


def rail_tol_pct(net: dict, default: float = DEFAULT_SIM_TOL_PCT) -> float:
    """The sim tolerance (percent) for one rail: the per-rail override
    ``sim_tol_pct`` (WyredSpiceContract §8) if declared on the L2 net — as a
    flat field or under an ``attrs`` dict — else the contract default (±2 %).
    An unparseable override falls back to the default rather than crashing the
    gate."""
    val = net.get("sim_tol_pct")
    if val is None:
        attrs = net.get("attrs")
        if isinstance(attrs, dict):
            val = attrs.get("sim_tol_pct")
    if val is None:
        return default
    try:
        return float(val)
    except (TypeError, ValueError):
        return default


def declared_power_rails(l2: dict) -> List[dict]:
    """The L2 nets that are declared power rails: kind ``power`` with a
    declared numeric ``voltage``. Ground (kind ``ground`` -> deck node ``0``,
    the reference) and signal nets (``voltage`` is ``None``, e.g. an internal
    divider tap) are NOT rails and are not graded."""
    return [net for net in l2.get("nets", [])
            if net.get("kind") == "power" and net.get("voltage") is not None]


def _node_voltages(op_record: dict) -> Dict[str, float]:
    """The op record's node voltages, keyed lowercased — ngspice lowercases
    node names, so the deck-node lookup must be case-insensitive."""
    return {str(k).lower(): v
            for k, v in (op_record.get("node_voltages") or {}).items()}


def _net_refdes(net: dict) -> set:
    """The refdes set of the components attached to a net (from ``nodes``,
    each ``[refdes, terminal]``)."""
    return {node[0] for node in net.get("nodes", []) if node}


def _resolve_sim(net: dict, node_map: dict,
                 nv: Dict[str, float]) -> tuple:
    """Map an L2 net to (deck_node, simulated_volts): the sidecar ``node_map``
    gives the deck node; ground (``0``) is the reference at 0 V; any other node
    is looked up (case-insensitively) in the op results. Returns
    ``(node, None)`` when the node is absent from the op point."""
    node = node_map.get(net["name"])
    if node == "0":
        return node, 0.0                 # ground reference, definitionally 0 V
    if node is not None and node.lower() in nv:
        return node, nv[node.lower()]
    return node, None


def rail_measurements(l2: dict, sidecar: dict, op_record: dict, *,
                      default_tol_pct: float = DEFAULT_SIM_TOL_PCT) -> List[dict]:
    """Per declared power rail: ``{net, declared, simulated|None, tol_pct,
    node}`` — the read-back the gate prints (and the skip banner names). Pure
    display; no verdict."""
    node_map = sidecar.get("node_map") or {}
    nv = _node_voltages(op_record)
    out = []
    for net in declared_power_rails(l2):
        node, sim = _resolve_sim(net, node_map, nv)
        out.append({"net": net["name"], "declared": float(net["voltage"]),
                    "simulated": sim, "node": node,
                    "tol_pct": rail_tol_pct(net, default_tol_pct)})
    return out


def check_rails(l2: dict, sidecar: dict, op_record: dict, *,
                default_tol_pct: float = DEFAULT_SIM_TOL_PCT
                ) -> List[RailFinding]:
    """Grade each declared power rail against its simulated DC node voltage.

    For every L2 net that is a declared power rail (``declared_power_rails``):

    * resolve its deck node (sidecar ``node_map``) and the simulated voltage
      (op record, case-insensitive; ground node ``0`` is 0 V by definition);
    * node absent from the op results -> ``SPICE_RAIL_UNRESOLVED`` UNLESS every
      component on the rail is named in the ``not_simulated`` confession (a
      fully-confessed rail is legitimately unsimulated — confession over
      absence);
    * node present but ``|simulated - declared| > declared * tol%`` ->
      ``SPICE_RAIL_OFF``, where ``tol%`` is the per-rail ``sim_tol_pct``
      override or the ±2 % contract default.

    Returns the findings (empty == every rail on-rail). A pure function of the
    three on-disk artifacts; no engine, no simulator.
    """
    node_map = sidecar.get("node_map") or {}
    not_sim = {e.get("refdes")
               for e in (sidecar.get("not_simulated") or [])}
    nv = _node_voltages(op_record)

    findings: List[RailFinding] = []
    for net in declared_power_rails(l2):
        name = net["name"]
        declared = float(net["voltage"])
        tol_pct = rail_tol_pct(net, default_tol_pct)
        node, sim = _resolve_sim(net, node_map, nv)

        if sim is None:
            # The rail's node is absent from the op point. Legitimate ONLY when
            # the rail is confessed — every component on it named in
            # not_simulated. Otherwise a modelled rail vanished: UNRESOLVED.
            refdes = _net_refdes(net)
            if refdes and refdes <= not_sim:
                continue
            where = ("%r" % node) if node is not None else "(no node_map entry)"
            findings.append(RailFinding(
                code=SPICE_RAIL_UNRESOLVED,
                msg=("net %s (declared %sV): deck node %s absent from the DC "
                     "operating-point results and not covered by the "
                     "not_simulated confession" % (name, _fmt(declared), where)),
                net=name, declared=declared, simulated=None,
                tol_pct=tol_pct, node=node))
            continue

        tol_v = abs(declared) * tol_pct / 100.0
        if abs(sim - declared) > tol_v:
            findings.append(RailFinding(
                code=SPICE_RAIL_OFF,
                msg=("net %s: declared %sV, simulated %sV, |delta|=%sV > "
                     "tol +/-%s%% (+/-%sV)"
                     % (name, _fmt(declared), _fmt(sim),
                        _fmt(abs(sim - declared)), _fmt(tol_pct), _fmt(tol_v))),
                net=name, declared=declared, simulated=sim,
                tol_pct=tol_pct, node=node))
    return findings
