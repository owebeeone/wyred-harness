"""LAYER-1 (intent) oracle for harness v3 — Gen4 spec section 4.1.

``check_intent(l1_doc) -> list[Violation]`` runs every Tier-A intent-level
check over a ``schema_l1.IntentDoc`` (or its ``to_json`` dict form). No
netlist exists yet: everything here is decided from roles / capabilities /
demands / scoped rails / pools / buses / invariant declarations /
escalations alone. This is the cheap inner loop.

Codes emitted (all Tier-A):

    VACUOUS_INTENT         the document declares ZERO demands: an intent that
                           asks for nothing is not scoreable as clean (a
                           "clean" verdict over an empty ask is vacuous —
                           spec_satisfaction refuses PASS on such a doc).
    DEMAND_UNSATISFIABLE   a non-power demand with no in-scope satisfier
                           (capability / pool / attached bus) and no declared
                           COMPATIBLE default — the resolution-ladder rung-2
                           load error (Gen4 §2.4).
    DEFAULT_INCOMPATIBLE   a Demand.default naming a known entity that is NOT
                           a type-compatible satisfier for the demand: a
                           power demand's default must be a declared RAIL of
                           compatible voltage (a non-rail default never
                           satisfies power); a non-power demand's default
                           must be a PROVIDER of the demanded iface (a pool
                           providing it, a role with a matching capability,
                           or a bus of that iface). default="GND" on a uart
                           demand fires this.
    RAIL_SCOPE             a power demand whose scope chain contains NO rail
                           at all (and no resolving default), OR a power
                           capability that claims to drive a rail that is not
                           declared anywhere in the document.
    VOLTAGE_MISMATCH       a power demand for V volts whose scope chain has
                           rails, but none within tolerance (corpus #7 —
                           intent-level concept error); a declared default
                           rail at the wrong voltage; or a power capability
                           driving a declared rail at a different voltage
                           (rail-tree consistency, corpus #2).
    POOL_INSUFFICIENT      pool-servable demands exceed pool capacity: for
                           each provided iface, the demand->pool assignment
                           is checked by exact bipartite feasibility
                           (max-flow), so split pools are handled correctly,
                           not just aggregate sums.
    ADDR_COLLISION         two roles claim the same i2c_addr on the same bus
                           (per (bus, addr); corpus #3 — caught at L1).
    INVARIANT_MALFORMED    an invariant declaration with an unknown kind,
                           wrong subject arity, duplicate subjects, or a
                           subject/input reference whose role id does not
                           exist in the document.
    ESCALATION_UNEXPLAINED a ladder-rung-4 escalation lacking its UNSAT-core
                           style explanation (empty ``conflict`` and/or empty
                           ``relaxation``) — Gen4 §2.4 rung 4 requires both.
    SCOPE_DANGLING         a role/rail/ground/bus references an undeclared
                           scope, or a Scope's parent is undeclared (the
                           dangling-chain case ``schema_l1.scope_chain``
                           documents as "flagged separately by the oracle").

Scope semantics: an entity declared in scope S is visible to a role whose
``scope_chain`` contains S (nearest-in-scope cascade, composition law 3).
Power demands satisfy from RAILS only (plus an explicit declared default) —
never silently from a bare power capability; the rail is the design object.

Deliberately NOT checked here (they belong to other v3 layers): allocation
record legality / bijection / determinism / lock violations (§4.2), ground
ROLE discipline (soft this generation), and layer-2 ERC.

Pure Python 3 stdlib. Plain imports only (flat harness directory).
"""

from __future__ import annotations

from collections import defaultdict
from typing import Any, Dict, List, Optional, Set, Tuple

from erc import Violation
from schema_l1 import (
    Capability,
    Demand,
    IntentDoc,
    InvariantDecl,
    Pool,
    Role,
    INVARIANT_KINDS,
    from_json,
)


# Voltage comparison tolerance (volts) — same policy as erc.py: absorbs float
# noise only, never a real rail difference.
_VTOL = 0.05


# ---------------------------------------------------------------------------
# scope helpers
# ---------------------------------------------------------------------------

def _declared_scopes(doc: IntentDoc) -> Set[str]:
    """All declared scope ids, plus the implicit root ""."""
    return {""} | {s.id for s in doc.scopes}


def _check_scopes(doc: IntentDoc) -> List[Violation]:
    """SCOPE_DANGLING: any reference to a scope id that was never declared."""
    out: List[Violation] = []
    declared = _declared_scopes(doc)

    for s in doc.scopes:
        if s.parent not in declared:
            out.append(Violation(
                "SCOPE_DANGLING",
                "Scope '%s' declares parent '%s', which is not a declared "
                "scope." % (s.id, s.parent)))

    refs = (
        [("role", r.id, r.scope) for r in doc.roles]
        + [("rail", r.name, r.scope) for r in doc.rails]
        + [("ground", g.name, g.scope) for g in doc.grounds]
        + [("bus", b.name, b.scope) for b in doc.buses]
    )
    for kind, name, scope in refs:
        if scope not in declared:
            out.append(Violation(
                "SCOPE_DANGLING",
                "%s '%s' is declared in scope '%s', which is not a declared "
                "scope." % (kind, name, scope)))
    return out


# ---------------------------------------------------------------------------
# satisfier discovery (non-power demands)
# ---------------------------------------------------------------------------

def _cap_matches(cap_iface: str, dem_iface: str) -> bool:
    """A capability satisfies a demand on exact iface match, or when the
    capability is a QUALIFIED provider of the same interface family
    ("i2c_master" satisfies an "i2c" demand; "uart" satisfies "uart")."""
    return cap_iface == dem_iface or cap_iface.startswith(dem_iface + "_")


def _default_resolves(doc: IntentDoc, name: Optional[str]) -> bool:
    """True when a declared default names a known rail / role / pool / bus /
    ground. A default naming nothing is NOT a satisfier (a dangling default
    is a silent default in disguise)."""
    if not name:
        return False
    return (doc.rail(name) is not None or doc.role(name) is not None
            or doc.pool(name) is not None or doc.bus(name) is not None
            or doc.ground(name) is not None)


def default_satisfier_ok(doc: IntentDoc, dem: Demand) -> bool:
    """F3 TYPED DEFAULTS: is the demand's declared default a TYPE-COMPATIBLE
    satisfier? (Shared with spec_satisfaction — one policy, one place.)

      * power demand  -> the default must be a declared RAIL at a compatible
                         voltage (a role/pool/bus/ground default never
                         satisfies power; a wrong-voltage rail default is
                         VOLTAGE_MISMATCH, checked by _check_power).
      * other ifaces  -> the default must be a PROVIDER of the demanded
                         iface: a pool whose ``provides`` matches, a role
                         with a matching capability, or a bus of that iface.

    False when there is no default, the default dangles, or it names an
    entity of the wrong type."""
    name = dem.default
    if not name:
        return False
    if dem.iface == "power":
        rail = doc.rail(name)
        return rail is not None and (
            dem.volts is None or abs(rail.volts - dem.volts) <= _VTOL)
    pool = doc.pool(name)
    if pool is not None:
        return _cap_matches(pool.provides, dem.iface)
    role = doc.role(name)
    if role is not None:
        return any(_cap_matches(c.iface, dem.iface) for c in role.capabilities)
    bus = doc.bus(name)
    if bus is not None:
        return bus.iface == dem.iface
    return False  # rail/ground on a non-power demand, or dangling


def _check_defaults(doc: IntentDoc) -> List[Violation]:
    """DEFAULT_INCOMPATIBLE — a default naming a KNOWN entity that cannot
    satisfy the demand's type. (A dangling default stays the rung-2
    DEMAND_UNSATISFIABLE path; a wrong-voltage RAIL default on a power demand
    stays VOLTAGE_MISMATCH via _check_power.)"""
    out: List[Violation] = []
    for role in doc.roles:
        for dem in role.demands:
            if not dem.default or not _default_resolves(doc, dem.default):
                continue
            if dem.iface == "power":
                if doc.rail(dem.default) is None:
                    out.append(Violation(
                        "DEFAULT_INCOMPATIBLE",
                        "Power demand '%s' on role '%s' declares default "
                        "'%s', which is not a declared rail — a power "
                        "demand's default must be a voltage-compatible RAIL."
                        % (dem.id, role.id, dem.default)))
                # wrong-voltage rail default -> VOLTAGE_MISMATCH (elsewhere)
            elif not default_satisfier_ok(doc, dem):
                out.append(Violation(
                    "DEFAULT_INCOMPATIBLE",
                    "Demand '%s' on role '%s' (iface '%s') declares default "
                    "'%s', which is not a provider of that iface (no "
                    "matching pool/capability/bus)." % (
                        dem.id, role.id, dem.iface, dem.default)))
    return out


def _check_vacuous(doc: IntentDoc) -> List[Violation]:
    """VACUOUS_INTENT — a document with ZERO demands asks for nothing and is
    therefore not scoreable as clean."""
    if any(r.demands for r in doc.roles):
        return []
    return [Violation(
        "VACUOUS_INTENT",
        "The document declares no demands at all (%d role(s), 0 demands): "
        "an intent that asks for nothing is vacuous and cannot be scored "
        "clean." % len(doc.roles))]


def _pool_scope(doc: IntentDoc, pool: Pool) -> str:
    """A pool lives in its owning role's scope (root if the owner dangles —
    kept tolerant; ownership hygiene is a §4.2 concern)."""
    owner = doc.role(pool.role)
    return owner.scope if owner is not None else ""


class _DemandView:
    """Per-(role, demand) satisfier summary used by the demand and pool
    checks (computed once, shared)."""

    def __init__(self, role: Role, demand: Demand, chain: List[str]):
        self.role = role
        self.demand = demand
        self.chain = chain
        self.cap_ok = False          # in-scope capability match
        self.bus_ok = False          # attached to a declared in-scope bus
        self.default_ok = False      # declared default resolving to an entity
        self.pools: List[str] = []   # in-scope pools providing the iface

    @property
    def non_pool_satisfied(self) -> bool:
        return self.cap_ok or self.bus_ok or self.default_ok

    @property
    def satisfiable(self) -> bool:
        return self.non_pool_satisfied or bool(self.pools)


def _demand_views(doc: IntentDoc) -> List[_DemandView]:
    """Build satisfier summaries for every NON-POWER demand (power demands
    are the rail checks' domain, so they are never double-reported here)."""
    views: List[_DemandView] = []
    for role in doc.roles:
        chain = doc.scope_chain(role.scope)
        for dem in role.demands:
            if dem.iface == "power":
                continue
            v = _DemandView(role, dem, chain)
            for other in doc.roles:
                if other.scope not in chain:
                    continue
                for cap in other.capabilities:
                    if _cap_matches(cap.iface, dem.iface):
                        v.cap_ok = True
                        break
                if v.cap_ok:
                    break
            if dem.bus is not None:
                bus = doc.bus(dem.bus)
                if (bus is not None and bus.iface == dem.iface
                        and bus.scope in chain):
                    v.bus_ok = True
            v.default_ok = default_satisfier_ok(doc, dem)
            v.pools = [p.name for p in doc.pools
                       if p.provides == dem.iface
                       and _pool_scope(doc, p) in chain]
            views.append(v)
    return views


def _check_demands(views: List[_DemandView]) -> List[Violation]:
    """DEMAND_UNSATISFIABLE — rung 2: no in-scope satisfier, no declared
    (resolving) default."""
    out: List[Violation] = []
    for v in views:
        if v.satisfiable:
            continue
        extra = ""
        if v.demand.default:
            extra = (" (its declared default '%s' is not a compatible "
                     "satisfier)" % v.demand.default)
        elif v.demand.bus is not None:
            extra = (" (its bus '%s' is not a declared in-scope '%s' bus)"
                     % (v.demand.bus, v.demand.iface))
        out.append(Violation(
            "DEMAND_UNSATISFIABLE",
            "Demand '%s' on role '%s' requires iface '%s' but has no "
            "in-scope satisfier and no declared default%s." % (
                v.demand.id, v.role.id, v.demand.iface, extra)))
    return out


# ---------------------------------------------------------------------------
# power demands vs scoped rails: RAIL_SCOPE / VOLTAGE_MISMATCH
# ---------------------------------------------------------------------------

def _check_power(doc: IntentDoc) -> List[Violation]:
    out: List[Violation] = []

    # (a) demand side: every power demand needs a compatible in-scope rail
    #     (the cascade) or an explicit, voltage-compatible declared default.
    for role in doc.roles:
        chain = doc.scope_chain(role.scope)
        for dem in role.demands:
            if dem.iface != "power":
                continue
            in_scope = [r for r in doc.rails if r.scope in chain]
            compatible = [r for r in in_scope
                          if dem.volts is None
                          or abs(r.volts - dem.volts) <= _VTOL]
            if compatible:
                continue

            if dem.default:
                d_rail = doc.rail(dem.default)
                if d_rail is not None:
                    if (dem.volts is None
                            or abs(d_rail.volts - dem.volts) <= _VTOL):
                        continue  # explicit compatible default rail
                    out.append(Violation(
                        "VOLTAGE_MISMATCH",
                        "Power demand '%s' on role '%s' requires %gV but its "
                        "declared default rail '%s' is %gV." % (
                            dem.id, role.id, dem.volts,
                            d_rail.name, d_rail.volts)))
                    continue
                # F3: a NON-RAIL default never satisfies a power demand
                # (DEFAULT_INCOMPATIBLE via _check_defaults when it resolves;
                # fall through to RAIL_SCOPE / VOLTAGE_MISMATCH regardless —
                # the corpus-#7 dodge is closed).

            if not in_scope:
                out.append(Violation(
                    "RAIL_SCOPE",
                    "Power demand '%s' on role '%s' (scope '%s') has no rail "
                    "in scope and no declared default." % (
                        dem.id, role.id, role.scope)))
            else:
                seen = ", ".join("%s=%gV" % (r.name, r.volts)
                                 for r in in_scope)
                out.append(Violation(
                    "VOLTAGE_MISMATCH",
                    "Power demand '%s' on role '%s' requires %gV but no "
                    "in-scope rail is compatible (in scope: %s)." % (
                        dem.id, role.id, dem.volts, seen)))

    # (b) provider side: rail-tree consistency. A power capability that
    #     drives a named rail must drive a DECLARED rail at ITS voltage.
    for role in doc.roles:
        for cap in role.capabilities:
            if cap.iface != "power" or cap.rail is None:
                continue
            rail = doc.rail(cap.rail)
            if rail is None:
                out.append(Violation(
                    "RAIL_SCOPE",
                    "Role '%s' declares a power capability driving rail "
                    "'%s', which is not a declared rail." % (
                        role.id, cap.rail)))
            elif (cap.volts is not None
                    and abs(cap.volts - rail.volts) > _VTOL):
                out.append(Violation(
                    "VOLTAGE_MISMATCH",
                    "Role '%s' drives rail '%s' at %gV but the rail is "
                    "declared at %gV." % (
                        role.id, cap.rail, cap.volts, rail.volts)))
    return out


# ---------------------------------------------------------------------------
# POOL_INSUFFICIENT — exact bipartite feasibility (tiny max-flow)
# ---------------------------------------------------------------------------

def _max_flow(caps: Dict[Tuple[int, int], int], n: int, src: int,
              sink: int) -> int:
    """Edmonds-Karp on a dense edge-capacity dict over nodes 0..n-1.
    Graphs here are tiny (a handful of demands and pools)."""
    flow = 0
    residual = dict(caps)
    while True:
        # BFS for an augmenting path.
        parent: Dict[int, int] = {src: src}
        queue = [src]
        while queue and sink not in parent:
            u = queue.pop(0)
            for v in range(n):
                if v not in parent and residual.get((u, v), 0) > 0:
                    parent[v] = u
                    queue.append(v)
        if sink not in parent:
            return flow
        # Bottleneck along the path, then augment.
        path: List[Tuple[int, int]] = []
        v = sink
        while v != src:
            path.append((parent[v], v))
            v = parent[v]
        bottleneck = min(residual[(u, w)] for (u, w) in path)
        for (u, w) in path:
            residual[(u, w)] = residual.get((u, w), 0) - bottleneck
            residual[(w, u)] = residual.get((w, u), 0) + bottleneck
        flow += bottleneck


def _check_pools(doc: IntentDoc, views: List[_DemandView]) -> List[Violation]:
    """For each pool-provided iface, the demands that can ONLY be served by
    pools must fit: assignment feasibility is exact (max-flow), so a demand
    seeing one pool cannot borrow capacity from a pool out of its scope.
    Demands with a non-pool satisfier (capability / bus / default) never
    charge pool capacity."""
    out: List[Violation] = []
    by_iface: Dict[str, List[_DemandView]] = defaultdict(list)
    for v in views:
        if v.pools and not v.non_pool_satisfied:
            by_iface[v.demand.iface].append(v)

    for iface in sorted(by_iface):
        group = by_iface[iface]
        pool_names = sorted({p for v in group for p in v.pools})
        pools = [doc.pool(p) for p in pool_names]
        # nodes: 0=src | 1..D demands | D+1..D+P pools | D+P+1=sink
        n_dem, n_pool = len(group), len(pools)
        src, sink = 0, n_dem + n_pool + 1
        caps: Dict[Tuple[int, int], int] = {}
        total_demand = 0
        for i, v in enumerate(group):
            qty = max(int(v.demand.qty), 0)
            total_demand += qty
            caps[(src, 1 + i)] = qty
            for j, p in enumerate(pools):
                if p.name in v.pools:
                    caps[(1 + i, 1 + n_dem + j)] = qty
        total_capacity = 0
        for j, p in enumerate(pools):
            units = max(int(p.unit_count), 0)
            total_capacity += units
            caps[(1 + n_dem + j, sink)] = units

        served = _max_flow(caps, sink + 1, src, sink)
        if served < total_demand:
            out.append(Violation(
                "POOL_INSUFFICIENT",
                "Iface '%s': %d unit(s) demanded (%s) but only %d can be "
                "served by pool(s) %s (capacity %d) — short by %d." % (
                    iface, total_demand,
                    ", ".join(sorted(v.demand.id for v in group)),
                    served, ", ".join(pool_names), total_capacity,
                    total_demand - served)))
    return out


# ---------------------------------------------------------------------------
# ADDR_COLLISION at intent level — per (bus, i2c_addr)
# ---------------------------------------------------------------------------

def _check_addrs(doc: IntentDoc) -> List[Violation]:
    """Two ROLES claiming one i2c_addr on one bus (corpus #3). The address
    lives in Demand.attrs["i2c_addr"] (or Role.attrs as a fallback); the bus
    key is the demand's ``bus`` name, or "" (the implicit shared bus) when
    unattached. One role never collides with itself."""
    buckets: Dict[Tuple[str, Any], Set[str]] = defaultdict(set)
    for role in doc.roles:
        role_buses = [d.bus for d in role.demands if d.bus is not None]
        for dem in role.demands:
            if "i2c_addr" in dem.attrs:
                buckets[(dem.bus or "", dem.attrs["i2c_addr"])].add(role.id)
        if "i2c_addr" in role.attrs:
            bus = role_buses[0] if role_buses else ""
            buckets[(bus, role.attrs["i2c_addr"])].add(role.id)

    out: List[Violation] = []
    for (bus, addr) in sorted(buckets, key=lambda k: (k[0], str(k[1]))):
        roles = buckets[(bus, addr)]
        if len(roles) < 2:
            continue
        addr_s = hex(addr) if isinstance(addr, int) else str(addr)
        out.append(Violation(
            "ADDR_COLLISION",
            "I2C address %s on bus '%s' is claimed by multiple roles: %s." % (
                addr_s, bus or "<default>", ", ".join(sorted(roles)))))
    return out


# ---------------------------------------------------------------------------
# INVARIANT_MALFORMED — layer-1 declaration well-formedness
# ---------------------------------------------------------------------------

def _ref_role_exists(doc: IntentDoc, ref: str) -> bool:
    """An intent-level reference is "<role_id>.<signal>" or a bare
    "<role_id>". Role ids may themselves contain dots, so every dot split is
    tried (longest role prefix first)."""
    if doc.role(ref) is not None:
        return True
    parts = ref.split(".")
    for cut in range(len(parts) - 1, 0, -1):
        if doc.role(".".join(parts[:cut])) is not None:
            return True
    return False


def _check_invariants(doc: IntentDoc) -> List[Violation]:
    out: List[Violation] = []
    for idx, inv in enumerate(doc.invariants):
        label = "invariants[%d]" % idx
        if inv.kind not in INVARIANT_KINDS:
            out.append(Violation(
                "INVARIANT_MALFORMED",
                "%s has unknown kind '%s' (known: %s)." % (
                    label, inv.kind, ", ".join(sorted(INVARIANT_KINDS)))))
            continue
        # mutual_exclusion: exactly 2 DISTINCT subjects.
        if len(inv.subjects) != 2:
            out.append(Violation(
                "INVARIANT_MALFORMED",
                "%s (mutual_exclusion) needs exactly 2 subjects, got %d." % (
                    label, len(inv.subjects))))
            continue
        if inv.subjects[0] == inv.subjects[1]:
            out.append(Violation(
                "INVARIANT_MALFORMED",
                "%s (mutual_exclusion) guards '%s' against itself." % (
                    label, inv.subjects[0])))
            continue
        bad = [ref for ref in list(inv.subjects) + list(inv.inputs)
               if not ref or not _ref_role_exists(doc, ref)]
        if bad:
            out.append(Violation(
                "INVARIANT_MALFORMED",
                "%s references unknown role id(s): %s." % (
                    label, ", ".join("'%s'" % b for b in bad))))
    return out


# ---------------------------------------------------------------------------
# ESCALATION_UNEXPLAINED — rung 4 must carry its UNSAT-core explanation
# ---------------------------------------------------------------------------

def _check_escalations(doc: IntentDoc) -> List[Violation]:
    out: List[Violation] = []
    for idx, esc in enumerate(doc.escalations):
        missing = []
        if not any(str(c).strip() for c in esc.conflict):
            missing.append("conflict (which requirements clash)")
        if not esc.relaxation.strip():
            missing.append("relaxation (what minimal change resolves it)")
        if not esc.msg.strip():
            missing.append("msg")
        if missing:
            out.append(Violation(
                "ESCALATION_UNEXPLAINED",
                "Escalation '%s' (escalations[%d], subjects: %s) lacks its "
                "explanation: missing %s." % (
                    esc.code, idx,
                    ", ".join(esc.subjects) if esc.subjects else "none",
                    "; ".join(missing))))
    return out


# ---------------------------------------------------------------------------
# public entry point
# ---------------------------------------------------------------------------

def check_intent(l1_doc) -> List[Violation]:
    """Run every layer-1 (intent) Tier-A check over ``l1_doc``.

    ``l1_doc`` is a ``schema_l1.IntentDoc`` (or the plain dict its
    ``to_json`` emits — dicts are loaded via ``from_json`` for convenience).
    Returns Violations in a stable check order; empty list == the intent
    layer is clean and standalone-scoreable (Gen4 §4.1).
    """
    doc: IntentDoc = from_json(l1_doc) if isinstance(l1_doc, dict) else l1_doc

    views = _demand_views(doc)
    violations: List[Violation] = []
    violations.extend(_check_vacuous(doc))
    violations.extend(_check_scopes(doc))
    violations.extend(_check_demands(views))
    violations.extend(_check_power(doc))
    violations.extend(_check_defaults(doc))
    violations.extend(_check_pools(doc, views))
    violations.extend(_check_addrs(doc))
    violations.extend(_check_invariants(doc))
    violations.extend(_check_escalations(doc))
    return violations


__all__ = ["check_intent", "default_satisfier_ok", "Violation"]


# ---------------------------------------------------------------------------
# smoke test:  python intent_oracle.py
# ---------------------------------------------------------------------------

if __name__ == "__main__":  # pragma: no cover
    import sys
    from schema_l1 import (AllocationEntry, AllocationRecord, Bus, Escalation,
                           Ground, Rail, Scope, to_json)

    failures = []

    def check(name, cond):
        print("  [%s] %s" % ("ok " if cond else "FAIL", name))
        if not cond:
            failures.append(name)

    def codes(doc):
        return sorted(v.code for v in check_intent(doc))

    # 1. clean doc (EMIT_CONTRACT Part D shape, corpus #5a) — no violations,
    #    and the dict form is accepted too.
    clean = IntentDoc(
        roles=[
            Role(id="mcu", kind="mcu",
                 demands=[Demand(id="mcu.pwr", iface="power", volts=3.3)]),
            Role(id="periph", kind="uart_device",
                 demands=[Demand(id="periph.pwr", iface="power", volts=3.3),
                          Demand(id="periph.uart", iface="uart")]),
        ],
        rails=[Rail(name="+3V3", volts=3.3)],
        grounds=[Ground(name="GND")],
        pools=[Pool(name="mcu.uarts", role="mcu", provides="uart",
                    unit_count=3, port_signature=["tx:out", "rx:in"])],
        allocation=AllocationRecord(
            entries=[AllocationEntry(pool="mcu.uarts", unit=0,
                                     demand="periph.uart", state="sticky")],
            solver_version="trivial-1"),
    )
    check("clean #5a doc has no violations", codes(clean) == [])
    check("dict form accepted", codes(to_json(clean)) == [])

    # 2. DEMAND_UNSATISFIABLE — oscillator demand, no provider, no default
    #    (corpus #4 minus its satisfier); with a declared default it clears.
    unsat = IntentDoc(
        roles=[Role(id="mcu", kind="mcu",
                    demands=[Demand(id="mcu.osc", iface="oscillator")])])
    check("unmet demand -> DEMAND_UNSATISFIABLE",
          codes(unsat) == ["DEMAND_UNSATISFIABLE"])
    withdef = IntentDoc(
        roles=[Role(id="mcu", kind="mcu",
                    demands=[Demand(id="mcu.osc", iface="oscillator",
                                    default="xtal")]),
               Role(id="xtal", kind="crystal",
                    capabilities=[Capability(iface="oscillator")])])
    check("declared resolving default clears it", codes(withdef) == [])
    badtype = IntentDoc(
        roles=[Role(id="periph", kind="uart_device",
                    demands=[Demand(id="periph.uart", iface="uart",
                                    default="GND")])],
        grounds=[Ground(name="GND")])
    check("default='GND' on a uart demand -> DEFAULT_INCOMPATIBLE (F3)",
          "DEFAULT_INCOMPATIBLE" in codes(badtype))
    vac = IntentDoc(roles=[Role(id="box", kind="enclosure")],
                    rails=[Rail(name="+3V3", volts=3.3)],
                    grounds=[Ground(name="GND")])
    check("zero-demand doc -> VACUOUS_INTENT (F1)",
          codes(vac) == ["VACUOUS_INTENT"])
    dangling = IntentDoc(
        roles=[Role(id="mcu", kind="mcu",
                    demands=[Demand(id="mcu.osc", iface="oscillator",
                                    default="ghost")])])
    check("dangling default is NOT a satisfier",
          codes(dangling) == ["DEMAND_UNSATISFIABLE"])

    # 3. RAIL_SCOPE — power demand, no rail anywhere in the chain.
    norail = IntentDoc(
        roles=[Role(id="mcu", kind="mcu",
                    demands=[Demand(id="mcu.pwr", iface="power",
                                    volts=3.3)])],
        grounds=[Ground(name="GND")])
    check("no in-scope rail -> RAIL_SCOPE", codes(norail) == ["RAIL_SCOPE"])

    # 4. VOLTAGE_MISMATCH — corpus #7: 5V-only device in a 3.3V rail scope.
    mismatch = IntentDoc(
        roles=[Role(id="dev5v", kind="sensor",
                    demands=[Demand(id="dev5v.pwr", iface="power",
                                    volts=5.0)])],
        rails=[Rail(name="+3V3", volts=3.3)],
        grounds=[Ground(name="GND")])
    check("5V device in 3.3V scope -> VOLTAGE_MISMATCH (corpus #7)",
          codes(mismatch) == ["VOLTAGE_MISMATCH"])

    # ... and the scoped-rail cascade + nested override (corpus #6 shape):
    # island role satisfies from ITS +12V rail, root role from root +3V3.
    cascade = IntentDoc(
        scopes=[Scope(id="island", parent="")],
        roles=[Role(id="mcu", kind="mcu",
                    demands=[Demand(id="mcu.pwr", iface="power",
                                    volts=3.3)]),
               Role(id="drv", kind="driver", scope="island",
                    demands=[Demand(id="drv.pwr", iface="power",
                                    volts=12.0),
                             Demand(id="drv.lpwr", iface="power",
                                    volts=3.3)])],
        rails=[Rail(name="+3V3", volts=3.3),
               Rail(name="+12V", volts=12.0, scope="island")],
        grounds=[Ground(name="GND")])
    check("nested scope cascade satisfies both rails", codes(cascade) == [])

    # ... rail-tree consistency (corpus #2): a capability driving a declared
    # rail at the wrong voltage.
    railtree = IntentDoc(
        roles=[Role(id="reg", kind="regulator",
                    capabilities=[Capability(iface="power", volts=5.0,
                                             rail="+3V3")],
                    demands=[Demand(id="reg.in", iface="power",
                                    volts=12.0)])],
        rails=[Rail(name="+3V3", volts=3.3), Rail(name="VIN", volts=12.0)],
        grounds=[Ground(name="GND")])
    check("driver volts != rail volts -> VOLTAGE_MISMATCH",
          codes(railtree) == ["VOLTAGE_MISMATCH"])

    # 5. POOL_INSUFFICIENT — corpus #9 mutated: 4 NAND demands, pool of 3.
    def nand_doc(units):
        return IntentDoc(
            roles=[Role(id="glue", kind="logic",
                        demands=[Demand(id="glue.n%d" % i, iface="nand")
                                 for i in range(4)]),
                   Role(id="quad", kind="logic_gate",
                        demands=[Demand(id="quad.pwr", iface="power",
                                        volts=3.3)])],
            rails=[Rail(name="+3V3", volts=3.3)],
            grounds=[Ground(name="GND")],
            pools=[Pool(name="quad.nands", role="quad", provides="nand",
                        unit_count=units,
                        port_signature=["a:in", "b:in", "y:out"])])
    check("4 demands / 4 units is sufficient (spare stays visible)",
          codes(nand_doc(4)) == [])
    check("4 demands / 3 units -> POOL_INSUFFICIENT",
          codes(nand_doc(3)) == ["POOL_INSUFFICIENT"])

    # 6. ADDR_COLLISION — corpus #3: two of three sensors pinned to 0x48.
    coll = IntentDoc(
        roles=[Role(id="mcu", kind="mcu",
                    capabilities=[Capability(iface="i2c_master")],
                    demands=[Demand(id="mcu.pwr", iface="power",
                                    volts=3.3)])] +
              [Role(id="s%d" % i, kind="sensor",
                    demands=[Demand(id="s%d.pwr" % i, iface="power",
                                    volts=3.3),
                             Demand(id="s%d.i2c" % i, iface="i2c",
                                    bus="I2C0", attrs={"i2c_addr": addr})])
               for i, addr in ((1, 0x48), (2, 0x48), (3, 0x49))],
        rails=[Rail(name="+3V3", volts=3.3)],
        grounds=[Ground(name="GND")],
        buses=[Bus(name="I2C0", iface="i2c")])
    check("two roles @0x48 on one bus -> ADDR_COLLISION (corpus #3)",
          codes(coll) == ["ADDR_COLLISION"])
    check("collision message names both roles",
          all(s in check_intent(coll)[0].msg for s in ("s1", "s2", "0x48")))

    # 7. INVARIANT_MALFORMED — unknown kind / bad arity / dangling role ref;
    #    a well-formed corpus #8 style declaration is clean.
    inv_doc = IntentDoc(
        roles=[Role(id="b1", kind="half_bridge_module"),
               Role(id="mcu", kind="mcu",
                    demands=[Demand(id="mcu.pwr", iface="power",
                                    volts=3.3)])],
        rails=[Rail(name="+3V3", volts=3.3)],
        grounds=[Ground(name="GND")],
        invariants=[InvariantDecl(kind="mutual_exclusion",
                                  subjects=["b1.hs_gate", "b1.ls_gate"],
                                  inputs=["mcu.in_h1", "mcu.in_l1"])])
    check("well-formed mutual_exclusion is clean", codes(inv_doc) == [])
    bad_invs = IntentDoc(
        roles=[Role(id="b1", kind="half_bridge_module",
                    demands=[Demand(id="b1.pwr", iface="power",
                                    volts=3.3)])],
        rails=[Rail(name="+3V3", volts=3.3)],
        grounds=[Ground(name="GND")],
        invariants=[
            InvariantDecl(kind="wibble", subjects=["b1.x", "b1.y"]),
            InvariantDecl(kind="mutual_exclusion", subjects=["b1.x"]),
            InvariantDecl(kind="mutual_exclusion",
                          subjects=["b1.x", "b1.x"]),
            InvariantDecl(kind="mutual_exclusion",
                          subjects=["b1.hs", "ghost.ls"]),
        ])
    check("4 malformed invariants -> 4 INVARIANT_MALFORMED",
          codes(bad_invs) == ["INVARIANT_MALFORMED"] * 4)

    # 8. ESCALATION_UNEXPLAINED — corpus #5b: rung-4 with a full UNSAT-core
    #    explanation is clean; without conflict/relaxation it is flagged.
    def esc_doc(conflict, relaxation):
        return IntentDoc(
            roles=[Role(id="mcu", kind="mcu",
                        capabilities=[Capability(iface="gpio")]),
                   Role(id="periph", kind="uart_device",
                        demands=[Demand(id="periph.uart", iface="uart")])],
            rails=[Rail(name="+3V3", volts=3.3)],
            grounds=[Ground(name="GND")],
            pools=[Pool(name="mcu.uarts", role="mcu", provides="uart",
                        unit_count=1)],
            escalations=[Escalation(
                code="AMBIGUOUS_NONEQUIV",
                msg="hw UART vs bit-banged GPIO; no policy",
                subjects=["periph.uart"],
                conflict=conflict, relaxation=relaxation)])
    check("explained rung-4 escalation is clean (corpus #5b)",
          codes(esc_doc(["uart vs gpio_bitbang not declared equivalent"],
                        "declare a policy or pin one provider")) == [])
    check("unexplained escalation -> ESCALATION_UNEXPLAINED",
          codes(esc_doc([], "")) == ["ESCALATION_UNEXPLAINED"])

    # 9. SCOPE_DANGLING — undeclared scope reference.
    dang = IntentDoc(
        roles=[Role(id="mcu", kind="mcu", scope="ghost_island",
                    demands=[Demand(id="mcu.pwr", iface="power",
                                    volts=3.3)])],
        rails=[Rail(name="+3V3", volts=3.3)],
        grounds=[Ground(name="GND")])
    check("undeclared scope -> SCOPE_DANGLING",
          "SCOPE_DANGLING" in codes(dang))

    # 10. split pools are checked exactly, not by aggregate sum: two demands
    #     in an island seeing only a 1-unit pool cannot borrow the root
    #     pool's spare capacity.
    split = IntentDoc(
        scopes=[Scope(id="island", parent="")],
        roles=[Role(id="root_mcu", kind="mcu"),
               Role(id="isl_mcu", kind="mcu", scope="island"),
               Role(id="p1", kind="uart_device", scope="island",
                    demands=[Demand(id="p1.uart", iface="uart")]),
               Role(id="p2", kind="uart_device", scope="island",
                    demands=[Demand(id="p2.uart", iface="uart")])],
        rails=[Rail(name="+3V3", volts=3.3)],
        grounds=[Ground(name="GND")],
        pools=[Pool(name="isl_mcu.uarts", role="isl_mcu", provides="uart",
                    unit_count=1)])
    # NOTE: a root pool exists with plenty of units, but it is owned by a
    # role at ROOT scope, so it IS visible to the island demands (cascade).
    # Make it invisible instead: own it from a sibling scope.
    split.scopes.append(Scope(id="other", parent=""))
    split.roles.append(Role(id="oth_mcu", kind="mcu", scope="other"))
    split.pools.append(Pool(name="oth_mcu.uarts", role="oth_mcu",
                            provides="uart", unit_count=8))
    check("scoped pool capacity is exact -> POOL_INSUFFICIENT",
          codes(split) == ["POOL_INSUFFICIENT"])

    print()
    if failures:
        print("intent_oracle self-check: FAIL (%d): %s"
              % (len(failures), "; ".join(failures)))
        sys.exit(1)
    print("intent_oracle self-check: PASS (all checks passed)")
    sys.exit(0)
