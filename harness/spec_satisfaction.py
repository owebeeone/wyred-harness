"""Spec-satisfaction oracle — the PRIMARY v3 verdict (Gen4 spec section 4.3).

``satisfies(l1_doc, l2_graph, record=None) -> dict`` decides whether a bound
layer-2 graph SATISFIES its layer-1 intent document. Golden-equivalence is
demoted to a SECONDARY check (many bindings satisfy one intent): it runs only
when the caller supplies a pinned golden, never as the primary gate.

Primary correctness = spec-satisfaction, composed from the v3 oracle stack
(every sub-oracle is delegated to, never re-implemented):

  1. every layer-1 CONSTRAINT held           -> intent_oracle.check_intent
     (demand-satisfiability, rail scope, voltage compatibility, pool
     sufficiency, address uniqueness, invariant well-formedness,
     escalation quality, scope sanity — Gen4 section 4.1)
  2. every layer-1 DEMAND met in the bound graph  -> this module
     (the per-demand satisfaction map: power demands realized on a
     compatible power net; interface demands realized through a legal
     allocation / capability edge in the graph)
  3. declared INVARIANTS present and checkable    -> this module (presence)
     + invariant.check_invariants (SHOOT_THROUGH / VACUOUS_INVARIANT)
  4. ALLOCATIONS legal                      -> allocation.check_allocations
     (unit legality, bijection, provenance, determinism, lock violations —
     Gen4 section 4.2)
  5. ERC clean                              -> erc.run_erc (R1..R7)
  6. SECONDARY: golden equivalence          -> equivalence.equivalent,
     only when ``record`` carries a pinned golden.

Codes emitted by THIS module (everything else carries its sub-oracle's code):

    UNMET_DEMAND       a demand that layer 1 declares satisfiable was NOT
                       realized in the bound graph (power pin not on a
                       compatible power net; no net joining the demander to
                       its allocated/capable provider). ALSO the F4
                       consistency net: ANY demand whose status is not
                       satisfied/escalated is guaranteed to surface as a
                       violation — the verdict can never be PASS while the
                       demand map shows an unsatisfied demand.
    UNDEMANDED_COMPONENT (F2 demand traceability) an L2 component carrying
                       NO valid provenance FOR ITS AUTHORSHIP CLASS: authored
                       parts must carry attrs["l1_role"] naming a real L1
                       role; generated parts (authored=False) must carry
                       attrs["for_demand"] naming a real L1 demand or role
                       (F9 re-verify: the OTHER class's key never
                       substitutes — an authored part cannot launder itself
                       as a companion, nor a companion as a binding). A
                       fully-wired rogue part that no demand asked for is
                       caught here, not tolerated.
    ORPHAN_GENERATED   (F2) an authored=False part whose attrs["for_demand"]
                       is present but resolves to NO L1 demand/role — a
                       companion claiming provenance that does not exist.
    UNALLOCATED_DEMAND a pool-servable demand the solver never allocated (or
                       under-allocated vs qty) and that is not escalated —
                       resolution-ladder rung 3 requires an ALLOCATION.
    INVARIANT_MISSING  a well-formed layer-1 invariant declaration was not
                       lowered to a concrete schema.Invariant in the graph.
    BAD_BINDING        the caller's role->refdes binding names a refdes that
                       does not exist in the layer-2 graph.
    GOLDEN_NONEQUIV    (secondary) a pinned golden was supplied and the bound
                       graph is not equivalent to it.

The call
--------

``l1_doc``   a ``schema_l1.IntentDoc`` (or its ``to_json`` dict form).
``l2_graph`` a ``schema.CanonicalGraph`` (or its ``to_json`` dict form), or
             None to score layer 1 standalone (graph-side checks skipped).
``record``   optional refinement record. Accepted forms:
               * None                        -> use ``l1_doc.allocation``;
               * a schema_l1.AllocationRecord (or its JSON dict) -> the
                 allocation record under test;
               * a dict with any of the keys:
                   "allocation" : AllocationRecord | JSON dict (under test)
                   "bindings"   : {role_id: refdes} — the layer-2 binding
                                  map (refdes are minted at layer 2; this is
                                  the only place they meet role ids). F9
                                  re-verify hardening: OMITTING bindings no
                                  longer skips graph-realization checks —
                                  candidate bindings are DERIVED from the
                                  mandatory F2 provenance attrs
                                  (attrs["l1_role"]) for every role the
                                  caller left unbound, so a candidate cannot
                                  dodge wiring checks by withholding the map
                   "record2"    : AllocationRecord | JSON dict — a second,
                                  independent re-solve (determinism check)
                   "baseline"   : allocation.snapshot_locks output — lock
                                  gate baseline
                   "golden"     : CanonicalGraph | JSON dict — a pinned
                                  golden; triggers the SECONDARY equivalence
                                  check.

Returns ``{"verdict", "violations", "demands", "secondary"}``:

  * ``verdict``    "FAIL:<CODE>[,<CODE>...]" (deduped, first-seen order,
                   same convention as score.verdict_for) | "ESCALATE"
                   (clean but the doc/graph surfaces escalations) | "PASS".
  * ``violations`` the full list of erc.Violation records.
  * ``demands``    per-demand satisfaction map for diagnostics:
                   {demand_id: {"role", "iface", "qty", "status", "via"}}
                   with status in {"satisfied", "escalated", "unsatisfied"}
                   and via e.g. "rail:+3V3", "allocation:mcu.uarts[0]",
                   "capability:mcu/i2c_master", "default:+5V",
                   "escalation:AMBIGUOUS_NONEQUIV".
  * ``secondary``  {"golden_checked": bool, "golden_equivalent": bool|None}.

Pure Python 3 stdlib. Plain imports only (flat harness directory).
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple

import schema
import schema_l1
from schema import CanonicalGraph, Component
from schema_l1 import (
    AllocationRecord,
    Demand,
    Escalation,
    IntentDoc,
    Role,
    INVARIANT_KINDS,
)
from erc import Violation, run_erc
from invariant import check_invariants
from equivalence import equivalent
from intent_oracle import check_intent, default_satisfier_ok
from allocation import check_allocations


# Voltage comparison tolerance (volts) — same policy as erc.py /
# intent_oracle.py: absorbs float noise only, never a real rail difference.
_VTOL = 0.05


# ---------------------------------------------------------------------------
# input coercion
# ---------------------------------------------------------------------------

def _coerce_doc(l1_doc) -> IntentDoc:
    if isinstance(l1_doc, IntentDoc):
        return l1_doc
    if isinstance(l1_doc, dict):
        return schema_l1.from_json(l1_doc)
    raise TypeError("l1_doc must be an IntentDoc or its to_json dict form")


def _coerce_graph(l2_graph) -> Optional[CanonicalGraph]:
    if l2_graph is None or isinstance(l2_graph, CanonicalGraph):
        return l2_graph
    if isinstance(l2_graph, dict):
        return schema.from_json(l2_graph)
    raise TypeError("l2_graph must be a CanonicalGraph, its to_json dict "
                    "form, or None")


def _alloc_from_any(a) -> Optional[AllocationRecord]:
    if a is None or isinstance(a, AllocationRecord):
        return a
    if isinstance(a, dict):
        # Reuse the public schema_l1 loader (tolerant of omissions).
        return schema_l1.from_json({"allocation": a}).allocation
    raise TypeError("allocation record must be an AllocationRecord or its "
                    "JSON dict form")


def _golden_from_any(g) -> Optional[CanonicalGraph]:
    if g is None or isinstance(g, CanonicalGraph):
        return g
    if isinstance(g, dict):
        return schema.from_json(g)
    raise TypeError("golden must be a CanonicalGraph or its JSON dict form")


def _coerce_record(record) -> Tuple[Optional[AllocationRecord],
                                    Optional[AllocationRecord],
                                    Optional[Dict[str, Any]],
                                    Dict[str, str],
                                    Optional[CanonicalGraph]]:
    """-> (allocation, record2, baseline, bindings, golden)."""
    if record is None:
        return None, None, None, {}, None
    if isinstance(record, AllocationRecord):
        return record, None, None, {}, None
    if isinstance(record, dict):
        keys = ("allocation", "bindings", "record2", "baseline", "golden")
        if any(k in record for k in keys):
            return (
                _alloc_from_any(record.get("allocation")),
                _alloc_from_any(record.get("record2")),
                record.get("baseline"),
                dict(record.get("bindings") or {}),
                _golden_from_any(record.get("golden")),
            )
        # A bare allocation-record JSON dict.
        return _alloc_from_any(record), None, None, {}, None
    raise TypeError("record must be None, an AllocationRecord, or a "
                    "refinement-record dict")


# ---------------------------------------------------------------------------
# layer-1 lookups (mirroring intent_oracle's satisfier policy)
# ---------------------------------------------------------------------------

def _iface_match(provided: str, wanted: str) -> bool:
    """Same family rule as intent_oracle._cap_matches: exact match, or the
    provider is a QUALIFIED member of the demanded family ("i2c_master"
    satisfies "i2c")."""
    return provided == wanted or provided.startswith(wanted + "_")


def _escalation_for(doc: IntentDoc, role: Role,
                    d: Demand) -> Optional[Escalation]:
    """The escalation covering this demand, if any: a subject naming the
    demand id, the role id, or a "<role_id>.<signal>" of the role."""
    for e in doc.escalations:
        for s in e.subjects:
            if s == d.id or s == role.id or s.split(".", 1)[0] == role.id:
                return e
    return None


def _compatible_rails(doc: IntentDoc, role: Role, d: Demand):
    """In-scope rails compatible with a power demand (any-in-chain, the same
    visibility rule intent_oracle applies)."""
    chain = doc.scope_chain(role.scope)
    in_scope = [r for r in doc.rails if r.scope in chain]
    return [r for r in in_scope
            if d.volts is None or abs(r.volts - d.volts) <= _VTOL]


def _power_l1_satisfier(doc: IntentDoc, role: Role,
                        d: Demand) -> Tuple[bool, str]:
    """(satisfiable-at-L1, via). Power satisfies from RAILS (or an explicit
    voltage-compatible default rail) only — never silently."""
    compatible = _compatible_rails(doc, role, d)
    if compatible:
        return True, "rail:%s" % compatible[0].name
    if d.default:
        d_rail = doc.rail(d.default)
        if d_rail is not None and (d.volts is None
                                   or abs(d_rail.volts - d.volts) <= _VTOL):
            return True, "default:%s" % d_rail.name
    return False, ""


def _capability_satisfier(doc: IntentDoc, role: Role, d: Demand):
    """-> (provider_role, capability) or (None, None). A demand attached to a
    bus may also be satisfied by a capability of the bus's iface family."""
    wants = [d.iface]
    if d.bus is not None:
        b = doc.bus(d.bus)
        if b is not None and b.iface and b.iface not in wants:
            wants.append(b.iface)
    for r in doc.roles:
        for cap in r.capabilities:
            for w in wants:
                if _iface_match(cap.iface, w):
                    return r, cap
    return None, None


def _pools_for(doc: IntentDoc, iface: str):
    return [p for p in doc.pools if _iface_match(p.provides, iface)]


# ---------------------------------------------------------------------------
# graph-side realization checks
# ---------------------------------------------------------------------------

def _power_realized(graph: CanonicalGraph, cands: List[Component],
                    volts: Optional[float]) -> bool:
    """Is a power demand realized in the graph: a power_in pin (of one of the
    ``cands`` candidate components when the role is bound/claimed, of any
    component otherwise) sits on a kind=="power" net at a compatible
    voltage."""
    comps = cands if cands else graph.components
    for c in comps:
        for t in c.terminals:
            if t.role != "power_in":
                continue
            if (volts is not None and t.req_v is not None
                    and abs(t.req_v - volts) > _VTOL):
                continue
            net = graph.net_of((c.refdes, t.name))
            if net is None or net.kind != "power":
                continue
            if volts is None:
                return True
            if net.voltage is not None and abs(net.voltage - volts) <= _VTOL:
                return True
    return False


def _share_iface_net(graph: CanonicalGraph, dem: Component,
                     prov: Component, iface: str) -> Optional[bool]:
    """Does some net join an ``iface`` terminal of the demander to an
    ``iface`` terminal of the provider? None == unjudgeable (either side
    exposes no terminal of this iface — the binding used a different
    vocabulary; the ERC layer still covers hard misses)."""
    dem_pins = {t.name for t in dem.terminals if t.iface == iface}
    prov_pins = {t.name for t in prov.terminals if t.iface == iface}
    if not dem_pins or not prov_pins:
        return None
    for n in graph.nets:
        has_d = any(r == dem.refdes and t in dem_pins for (r, t) in n.nodes)
        has_p = any(r == prov.refdes and t in prov_pins for (r, t) in n.nodes)
        if has_d and has_p:
            return True
    return False


def _share_iface_net_any(graph: CanonicalGraph, dems: List[Component],
                         provs: List[Component],
                         iface: str) -> Optional[bool]:
    """Tri-state ``_share_iface_net`` over CANDIDATE component lists (F9:
    a role left unbound by the caller is judged through every component
    claiming it via provenance). True as soon as ANY (demander, provider)
    pair shares an ``iface`` net; False when at least one pair was judgeable
    and none was joined; None when no pair was judgeable (or a side has no
    candidates at all)."""
    if not dems or not provs:
        return None
    saw_false = False
    for dc in dems:
        for pc in provs:
            r = _share_iface_net(graph, dc, pc, iface)
            if r is True:
                return True
            if r is False:
                saw_false = True
    return False if saw_false else None


# ---------------------------------------------------------------------------
# the per-demand satisfaction map (check 2)
# ---------------------------------------------------------------------------

def _demand_map(doc: IntentDoc, graph: Optional[CanonicalGraph],
                rec: AllocationRecord, bindings: Dict[str, str]
                ) -> Tuple[Dict[str, Dict[str, Any]], List[Violation]]:
    out_map: Dict[str, Dict[str, Any]] = {}
    viols: List[Violation] = []

    # F9: candidate components per role. An EXPLICIT binding wins; a role the
    # caller left unbound is judged through the components CLAIMING it via
    # the mandatory F2 provenance attr (authored parts' attrs["l1_role"]) —
    # omitting the bindings map no longer skips realization checks.
    claims: Dict[str, List[Component]] = {}
    if graph is not None:
        for c in graph.components:
            rid = c.attrs.get("l1_role")
            if c.authored and isinstance(rid, str) \
                    and doc.role(rid) is not None:
                claims.setdefault(rid, []).append(c)

    def bound(role_id: str) -> List[Component]:
        """Candidate components for a role; [] == unjudgeable by role."""
        if graph is None:
            return []
        ref = bindings.get(role_id)
        if ref is not None:
            c = graph.component(ref)
            return [c] if c is not None else []
        return list(claims.get(role_id, []))

    for role in doc.roles:
        for d in role.demands:
            info: Dict[str, Any] = {"role": role.id, "iface": d.iface,
                                    "qty": d.qty, "status": "unsatisfied",
                                    "via": ""}
            esc = _escalation_for(doc, role, d)

            if d.iface == "power":
                ok, via = _power_l1_satisfier(doc, role, d)
                if ok:
                    info["status"], info["via"] = "satisfied", via
                    if graph is not None and not _power_realized(
                            graph, bound(role.id), d.volts):
                        info["status"], info["via"] = "unsatisfied", ""
                        viols.append(Violation(
                            "UNMET_DEMAND",
                            "Power demand '%s' on role '%s' (%s) is "
                            "satisfiable at layer 1 via %s but is not "
                            "realized in the bound graph (no compatible "
                            "power_in on a matching power net)." % (
                                d.id, role.id,
                                "any volts" if d.volts is None
                                else "%gV" % d.volts, via)))
                elif esc is not None:
                    info["status"] = "escalated"
                    info["via"] = "escalation:%s" % esc.code
                # else: unsatisfiable at layer 1 — intent_oracle emits
                # RAIL_SCOPE / VOLTAGE_MISMATCH; no duplicate here.
                out_map[d.id] = info
                continue

            # ---- interface demand -------------------------------------
            entries = sorted((e for e in rec.entries if e.demand == d.id),
                             key=lambda e: (e.pool, str(e.unit)))
            if entries:
                if len(entries) >= max(1, d.qty):
                    info["status"] = "satisfied"
                    info["via"] = "allocation:" + ",".join(
                        "%s[%s]" % (e.pool, e.unit) for e in entries)
                    # graph realization: the demander must actually reach
                    # its allocated pool's owner through a net.
                    dem_cs = bound(role.id)
                    for e in entries:
                        pool = doc.pool(e.pool)
                        prov_cs = bound(pool.role) if pool is not None else []
                        if _share_iface_net_any(graph, dem_cs, prov_cs,
                                                d.iface) is False:
                            info["status"], info["via"] = "unsatisfied", ""
                            viols.append(Violation(
                                "UNMET_DEMAND",
                                "Demand '%s' on role '%s' is allocated "
                                "unit %s of pool '%s' but no net joins "
                                "'%s' terminals of %s to %s in the bound "
                                "graph." % (
                                    d.id, role.id, e.unit, e.pool, d.iface,
                                    "/".join(c.refdes for c in dem_cs),
                                    "/".join(c.refdes for c in prov_cs))))
                            break
                elif esc is not None:
                    info["status"] = "escalated"
                    info["via"] = "escalation:%s" % esc.code
                else:
                    viols.append(Violation(
                        "UNALLOCATED_DEMAND",
                        "Demand '%s' on role '%s' (iface '%s', qty=%d) is "
                        "under-allocated: only %d entr%s in the allocation "
                        "record (ladder rung 3: pool ambiguity must resolve "
                        "as an allocation)." % (
                            d.id, role.id, d.iface, d.qty, len(entries),
                            "y" if len(entries) == 1 else "ies")))
            elif esc is not None:
                info["status"] = "escalated"
                info["via"] = "escalation:%s" % esc.code
            else:
                prov_role, cap = _capability_satisfier(doc, role, d)
                if prov_role is not None:
                    info["status"] = "satisfied"
                    info["via"] = "capability:%s/%s" % (prov_role.id,
                                                        cap.iface)
                    dem_cs, prov_cs = bound(role.id), bound(prov_role.id)
                    if _share_iface_net_any(graph, dem_cs, prov_cs,
                                            d.iface) is False:
                        info["status"], info["via"] = "unsatisfied", ""
                        viols.append(Violation(
                            "UNMET_DEMAND",
                            "Demand '%s' on role '%s' is satisfiable via "
                            "capability %s/%s but no net joins '%s' "
                            "terminals of %s to %s in the bound graph." % (
                                d.id, role.id, prov_role.id, cap.iface,
                                d.iface,
                                "/".join(c.refdes for c in dem_cs),
                                "/".join(c.refdes for c in prov_cs))))
                elif _pools_for(doc, d.iface):
                    viols.append(Violation(
                        "UNALLOCATED_DEMAND",
                        "Demand '%s' on role '%s' (iface '%s') is servable "
                        "by pool(s) %s but has NO allocation entry and no "
                        "escalation (ladder rung 3: the solver must "
                        "allocate, deterministically and provenanced)." % (
                            d.id, role.id, d.iface,
                            sorted(p.name for p in
                                   _pools_for(doc, d.iface)))))
                elif default_satisfier_ok(doc, d):
                    # F3 typed defaults: only a TYPE-COMPATIBLE default (a
                    # provider of the demanded iface) satisfies; a dangling
                    # or wrong-typed default never does.
                    info["status"] = "satisfied"
                    info["via"] = "default:%s" % d.default
                # else: unsatisfiable at layer 1 — intent_oracle emits
                # DEMAND_UNSATISFIABLE / DEFAULT_INCOMPATIBLE (and the F4
                # consistency net in ``satisfies`` guarantees the verdict
                # cannot be PASS).
            out_map[d.id] = info
    return out_map, viols


# ---------------------------------------------------------------------------
# invariant presence (check 3, the presence half; checkability is delegated
# to invariant.check_invariants)
# ---------------------------------------------------------------------------

def _invariant_presence(doc: IntentDoc, graph: Optional[CanonicalGraph],
                        bindings: Dict[str, str]) -> List[Violation]:
    if graph is None:
        return []
    # Only WELL-FORMED declarations demand a lowered counterpart; malformed
    # ones are already INVARIANT_MALFORMED via intent_oracle.
    decls = []
    for decl in doc.invariants:
        if decl.kind not in INVARIANT_KINDS:
            continue
        if decl.kind == "mutual_exclusion" and len(decl.subjects) != 2:
            continue
        role_ids = [s.split(".", 1)[0] for s in decl.subjects]
        if any(doc.role(rid) is None for rid in role_ids):
            continue
        decls.append((decl, role_ids))

    unused = list(graph.invariants)
    matched = [False] * len(decls)

    # pass 1: exact refdes anchoring where every subject role is bound
    for i, (decl, role_ids) in enumerate(decls):
        want = sorted(bindings.get(rid, "") for rid in role_ids)
        if "" in want:
            continue
        for inv in unused:
            if (inv.kind == decl.kind
                    and sorted([inv.a[0], inv.b[0]]) == want):
                unused.remove(inv)
                matched[i] = True
                break

    # pass 2: kind-level presence for the rest (unbound subjects tolerated)
    out: List[Violation] = []
    for i, (decl, role_ids) in enumerate(decls):
        if matched[i]:
            continue
        hit = None
        for inv in unused:
            if inv.kind == decl.kind:
                hit = inv
                break
        if hit is not None:
            unused.remove(hit)
        else:
            out.append(Violation(
                "INVARIANT_MISSING",
                "Layer-1 %s invariant over %s was not lowered to a concrete "
                "schema.Invariant in the bound graph — a declared guarantee "
                "must live in the emitted artifact." % (
                    decl.kind, decl.subjects)))
    return out


# ---------------------------------------------------------------------------
# F2: demand traceability — every L2 component carries provenance
# (composition law 7's oracle support: nothing lands in the binding that the
# intent did not ask for).
# ---------------------------------------------------------------------------

def _provenance_violations(doc: IntentDoc,
                           graph: Optional[CanonicalGraph]) -> List[Violation]:
    """EMIT_CONTRACT provenance: authored parts carry attrs["l1_role"] naming
    a real L1 role; generated parts (authored=False) carry
    attrs["for_demand"] naming the real L1 demand (or role) whose declared
    companion produced them. F9 re-verify hardening: the check is PER
    AUTHORSHIP CLASS — an authored part is valid only through a resolving
    l1_role and a generated part only through a resolving for_demand (the
    contract's own words); the other class's key never substitutes.

      * UNDEMANDED_COMPONENT — the class-required provenance key is missing
        or does not resolve to a real L1 role/demand (a rogue part no demand
        asked for, or a dangling l1_role).
      * ORPHAN_GENERATED     — an authored=False part whose for_demand is
        PRESENT but resolves to nothing.
    """
    if graph is None:
        return []
    out: List[Violation] = []
    for c in graph.components:
        l1r = c.attrs.get("l1_role")
        fd = c.attrs.get("for_demand")
        l1r_ok = isinstance(l1r, str) and doc.role(l1r) is not None
        fd_ok = isinstance(fd, str) and (doc.demand(fd) is not None
                                         or doc.role(fd) is not None)
        if (l1r_ok if c.authored else fd_ok):
            continue
        if not c.authored and fd is not None:
            out.append(Violation(
                "ORPHAN_GENERATED",
                "Generated component '%s' (%s, authored=False) claims "
                "for_demand=%r, which resolves to no L1 demand or role — a "
                "companion must trace to the declaration that produced it."
                % (c.refdes, c.kind, fd)))
        else:
            missing = ("attrs['for_demand']" if not c.authored
                       else "attrs['l1_role']")
            out.append(Violation(
                "UNDEMANDED_COMPONENT",
                "Component '%s' (%s, authored=%s) carries no valid "
                "provenance (%s naming a real L1 %s): no L1 demand asked "
                "for this part (l1_role=%r, for_demand=%r)."
                % (c.refdes, c.kind, c.authored, missing,
                   "demand/role" if not c.authored else "role", l1r, fd)))
    return out


# ---------------------------------------------------------------------------
# public entry point
# ---------------------------------------------------------------------------

def satisfies(l1_doc, l2_graph, record=None) -> Dict[str, Any]:
    """PRIMARY v3 oracle: does the bound layer-2 graph satisfy the layer-1
    intent document? See the module docstring for arguments, the returned
    ``{"verdict", "violations", "demands", "secondary"}`` shape, and the
    codes this module adds on top of the delegated sub-oracles."""
    doc = _coerce_doc(l1_doc)
    graph = _coerce_graph(l2_graph)
    alloc, record2, baseline, bindings, golden = _coerce_record(record)
    rec = alloc if alloc is not None else doc.allocation

    violations: List[Violation] = []

    # 0. binding sanity — a binding must land on a real refdes.
    if graph is not None:
        for rid in sorted(bindings):
            if graph.component(bindings[rid]) is None:
                violations.append(Violation(
                    "BAD_BINDING",
                    "Role '%s' is bound to refdes '%s', which does not "
                    "exist in the layer-2 graph." % (rid, bindings[rid])))

    # 1. every constraint held (layer-1 Tier-A oracle, delegated). This is
    #    also where a VACUOUS doc (zero demands) is refused: an intent that
    #    asks for nothing can never earn a PASS here (F1).
    violations.extend(check_intent(doc))

    # 2. every demand met in the bound graph (per-demand map).
    demands, demand_viols = _demand_map(doc, graph, rec, bindings)
    violations.extend(demand_viols)

    # 2b. F2 demand traceability: every L2 component carries provenance
    #     (attrs["l1_role"] / attrs["for_demand"]) resolving into the doc.
    violations.extend(_provenance_violations(doc, graph))

    # 3. declared invariants present and checkable.
    violations.extend(_invariant_presence(doc, graph, bindings))
    if graph is not None:
        violations.extend(check_invariants(graph))

    # 4. allocations legal (delegated; includes determinism + lock gates).
    violations.extend(check_allocations(doc, graph, record=alloc,
                                        record2=record2, baseline=baseline))

    # 5. ERC clean (delegated).
    if graph is not None:
        violations.extend(run_erc(graph))

    # 6. SECONDARY: golden equivalence, only when a pinned golden exists.
    secondary: Dict[str, Any] = {"golden_checked": False,
                                 "golden_equivalent": None}
    if golden is not None and graph is not None:
        eq = bool(equivalent(graph, golden))
        secondary = {"golden_checked": True, "golden_equivalent": eq}
        if not eq:
            violations.append(Violation(
                "GOLDEN_NONEQUIV",
                "Bound graph is not equivalent to the pinned golden "
                "(secondary check; primary spec-satisfaction verdicts are "
                "unaffected by golden absence)."))

    # F4: verdict/demand-map consistency. An unsatisfied demand must NEVER
    # fall through silently: if no sub-oracle already reported it (every
    # sub-oracle message quotes the demand id), emit UNMET_DEMAND here so
    # that verdict PASS <=> zero violations <=> every demand
    # satisfied-or-escalated holds by construction.
    for did in sorted(demands):
        if demands[did]["status"] != "unsatisfied":
            continue
        needle = "'%s'" % did
        if any(needle in v.msg for v in violations):
            continue
        violations.append(Violation(
            "UNMET_DEMAND",
            "Demand '%s' on role '%s' (iface '%s') is not satisfied and not "
            "escalated, and no sub-oracle accounted for it — an unsatisfied "
            "demand can never coexist with a PASS verdict (F4 consistency)."
            % (did, demands[did]["role"], demands[did]["iface"])))

    codes: List[str] = []
    for v in violations:
        if v.code not in codes:
            codes.append(v.code)
    if codes:
        verdict = "FAIL:" + ",".join(codes)
    elif doc.escalations or (graph is not None and graph.escalations):
        verdict = "ESCALATE"
    else:
        verdict = "PASS"

    return {"verdict": verdict, "violations": violations,
            "demands": demands, "secondary": secondary}


__all__ = ["satisfies", "Violation"]


# ---------------------------------------------------------------------------
# self-check:  python spec_satisfaction.py
# (the EMIT_CONTRACT.md Part D worked example, plus targeted corruptions)
# ---------------------------------------------------------------------------

def _demo_doc() -> IntentDoc:
    from schema_l1 import (AllocationEntry, Capability, Ground, Pool, Rail)
    return IntentDoc(
        series="A",
        roles=[
            Role(id="mcu", kind="mcu",
                 demands=[Demand(id="mcu.pwr", iface="power", volts=3.3)]),
            Role(id="periph", kind="uart_device",
                 demands=[Demand(id="periph.pwr", iface="power", volts=3.3),
                          Demand(id="periph.uart", iface="uart")]),
            Role(id="supply", kind="supply",
                 capabilities=[Capability(iface="power", volts=3.3,
                                          rail="+3V3")]),
        ],
        rails=[Rail(name="+3V3", volts=3.3)],
        grounds=[Ground(name="GND")],
        pools=[schema_l1.Pool(name="mcu.uarts", role="mcu", provides="uart",
                              unit_count=3,
                              port_signature=["tx:out", "rx:in"])],
        allocation=AllocationRecord(
            entries=[AllocationEntry(pool="mcu.uarts", unit=0,
                                     demand="periph.uart",
                                     chosen_by="solver", state="sticky")],
            solver_version="trivial-1"),
    )


def _demo_graph() -> CanonicalGraph:
    T = schema.Terminal
    ps1 = schema.Component(
        refdes="PS1", kind="source", value="3.3V", authored=True,
        terminals=[T("VOUT", "power_out", prov_v=3.3), T("GND", "ground")],
        attrs={"voltage": 3.3, "l1_role": "supply"})
    u1_terms = [T("VDD", "power_in", req_v=3.3), T("GND", "ground")]
    for i in range(3):
        u1_terms.append(T("U%d_TX" % i, "signal", iface="uart",
                          iface_member="provide"))
        u1_terms.append(T("U%d_RX" % i, "signal", iface="uart",
                          iface_member="provide"))
    u1 = schema.Component(refdes="U1", kind="mcu", value="MCU-3UART",
                          authored=True, terminals=u1_terms,
                          attrs={"uart_ports": 3, "l1_role": "mcu"})
    u2 = schema.Component(
        refdes="U2", kind="uart_device", value="uart-peripheral",
        authored=True,
        terminals=[T("VDD", "power_in", req_v=3.3), T("GND", "ground"),
                   T("TX", "signal", iface="uart", iface_member="require"),
                   T("RX", "signal", iface="uart", iface_member="require")],
        attrs={"l1_role": "periph"})
    nets = [
        schema.Net("+3V3", "power", 3.3,
                   [("PS1", "VOUT"), ("U1", "VDD"), ("U2", "VDD")]),
        schema.Net("GND", "ground", 0.0,
                   [("PS1", "GND"), ("U1", "GND"), ("U2", "GND")],
                   ground_kind="ground", ground_role="none"),
        schema.Net("UART0_TX", "signal", None,
                   [("U1", "U0_TX"), ("U2", "RX")]),
        schema.Net("UART0_RX", "signal", None,
                   [("U1", "U0_RX"), ("U2", "TX")]),
    ]
    return CanonicalGraph(components=[ps1, u1, u2], nets=nets)


def _copy_doc(doc: IntentDoc) -> IntentDoc:
    return schema_l1.from_json(schema_l1.to_json(doc))


def _copy_graph(g: CanonicalGraph) -> CanonicalGraph:
    return schema.from_json(schema.to_json(g))


if __name__ == "__main__":  # pragma: no cover
    import sys
    from schema_l1 import (AllocationEntry, Capability, InvariantDecl)

    doc, graph = _demo_doc(), _demo_graph()
    bindings = {"mcu": "U1", "periph": "U2"}
    failures: List[str] = []
    n_checks = 0

    def check(name: str, cond: bool) -> None:
        global n_checks
        n_checks += 1
        print("  %-58s %s" % (name, "ok" if cond else "FAIL"))
        if not cond:
            failures.append(name)

    def codes_of(res) -> List[str]:
        return [v.code for v in res["violations"]]

    print("spec_satisfaction self-check:")

    # 1. the Part D pair satisfies its intent
    res = satisfies(doc, graph, {"bindings": bindings})
    check("good pair -> PASS", res["verdict"] == "PASS")
    check("all demands satisfied",
          all(d["status"] == "satisfied" for d in res["demands"].values()))
    check("uart met via the allocation record",
          res["demands"]["periph.uart"]["via"] == "allocation:mcu.uarts[0]")
    check("power met via the in-scope rail",
          res["demands"]["mcu.pwr"]["via"] == "rail:+3V3")

    # 2. deterministic + JSON-dict input tolerant
    res_j = satisfies(schema_l1.to_json(doc), schema.to_json(graph),
                      {"bindings": bindings})
    check("JSON-form inputs -> identical verdict + demand map",
          res_j["verdict"] == res["verdict"]
          and res_j["demands"] == res["demands"])

    # 3. layer-1 standalone scoring (no graph)
    res = satisfies(doc, None)
    check("layer-1 standalone (graph=None) -> PASS",
          res["verdict"] == "PASS"
          and not res["secondary"]["golden_checked"])

    # 4. SECONDARY golden equivalence (only when pinned)
    res = satisfies(doc, graph, {"bindings": bindings,
                                 "golden": _copy_graph(graph)})
    check("pinned matching golden -> PASS, secondary True",
          res["verdict"] == "PASS"
          and res["secondary"]["golden_equivalent"] is True)
    wrong = _copy_graph(graph)
    wrong.nets = [n for n in wrong.nets
                  if n.name not in ("UART0_TX", "UART0_RX")]
    wrong.components = [c for c in wrong.components if c.refdes != "U2"]
    res = satisfies(doc, graph, {"bindings": bindings, "golden": wrong})
    check("pinned divergent golden -> GOLDEN_NONEQUIV",
          "GOLDEN_NONEQUIV" in codes_of(res)
          and res["secondary"]["golden_equivalent"] is False)

    # 5. allocation legality is delegated (corrupt unit index)
    bad_rec = AllocationRecord(
        entries=[AllocationEntry(pool="mcu.uarts", unit=7,
                                 demand="periph.uart")],
        solver_version="trivial-1")
    res = satisfies(doc, graph, bad_rec)
    check("record with unit 7 of 3 -> ALLOC_UNKNOWN_UNIT",
          "ALLOC_UNKNOWN_UNIT" in codes_of(res))

    # 6. layer-1 constraint checks are delegated to intent_oracle
    d2 = _copy_doc(doc)
    d2.rails = []
    res = satisfies(d2, graph, {"bindings": bindings})
    check("no rails -> RAIL_SCOPE (constraint held check)",
          "RAIL_SCOPE" in codes_of(res)
          and res["demands"]["mcu.pwr"]["status"] == "unsatisfied")
    d3 = _copy_doc(doc)
    d3.rails[0].volts = 5.0
    res = satisfies(d3, graph, {"bindings": bindings})
    check("5V rail vs 3.3V demand -> VOLTAGE_MISMATCH",
          "VOLTAGE_MISMATCH" in codes_of(res))

    # 7. declared invariants must be present in the emitted graph
    d4 = _copy_doc(doc)
    d4.invariants = [InvariantDecl(kind="mutual_exclusion",
                                   subjects=["mcu.hs", "mcu.ls"],
                                   inputs=["mcu.a", "mcu.b"])]
    res = satisfies(d4, graph, {"bindings": bindings})
    check("declared invariant not lowered -> INVARIANT_MISSING",
          "INVARIANT_MISSING" in codes_of(res))
    g4 = _copy_graph(graph)
    g4.invariants = [schema.Invariant(
        kind="mutual_exclusion", a=("U1", "U0_TX"), b=("U1", "U1_TX"),
        inputs=[("U1", "U0_RX"), ("U1", "U1_RX")])]
    res = satisfies(d4, g4, {"bindings": bindings})
    check("lowered invariant -> INVARIANT_MISSING absent",
          "INVARIANT_MISSING" not in codes_of(res))

    # 8. ERC + graph-realization: disconnect the peripheral's supply pin
    g5 = _copy_graph(graph)
    g5.nets[0].nodes = [nd for nd in g5.nets[0].nodes if nd != ("U2", "VDD")]
    res = satisfies(doc, g5, {"bindings": bindings})
    check("dangling U2.VDD -> UNCONNECTED_MANDATORY (ERC delegated)",
          "UNCONNECTED_MANDATORY" in codes_of(res))
    check("dangling U2.VDD -> UNMET_DEMAND (demand not realized)",
          "UNMET_DEMAND" in codes_of(res)
          and res["demands"]["periph.pwr"]["status"] == "unsatisfied")

    # 9. rung 3: a pool-servable demand the solver never allocated
    d6 = _copy_doc(doc)
    d6.allocation.entries = []
    res = satisfies(d6, graph, {"bindings": bindings})
    check("pool demand without allocation -> UNALLOCATED_DEMAND",
          "UNALLOCATED_DEMAND" in codes_of(res))

    # 10. rung 4: non-equivalent ambiguity escalates (corpus #5b, L1-only)
    d7 = _copy_doc(doc)
    d7.allocation.entries = []
    d7.roles[0].capabilities.append(Capability(iface="gpio_bitbang"))
    d7.escalations = [schema_l1.Escalation(
        code="AMBIGUOUS_NONEQUIV",
        msg="hardware UART vs bit-banged GPIO; no policy",
        subjects=["periph.uart"],
        conflict=["periph.uart demands 'uart'",
                  "'mcu.uarts' and 'gpio_bitbang' are not declared "
                  "equivalent"],
        relaxation="declare a selection policy, or pin one provider")]
    res = satisfies(d7, None)
    check("explained non-equivalent ambiguity -> ESCALATE",
          res["verdict"] == "ESCALATE"
          and res["demands"]["periph.uart"]["status"] == "escalated")
    d7.escalations[0].conflict = []
    res = satisfies(d7, None)
    check("unexplained escalation -> ESCALATION_UNEXPLAINED",
          "ESCALATION_UNEXPLAINED" in codes_of(res))

    # 11. binding sanity
    res = satisfies(doc, graph, {"bindings": {"mcu": "U9"}})
    check("binding to absent refdes -> BAD_BINDING",
          "BAD_BINDING" in codes_of(res))

    print("spec_satisfaction self-check: %d/%d %s"
          % (n_checks - len(failures), n_checks,
             "PASS" if not failures else
             "FAIL (%s)" % ", ".join(failures)))
    sys.exit(0 if not failures else 1)
