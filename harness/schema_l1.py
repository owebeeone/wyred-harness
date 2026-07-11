"""LAYER-1 (intent) graph model for harness v3 — Gen4 spec section 4.4(a)/(b).

This is the neutral, candidate-agnostic representation of an INTENT document:
abstract ROLES with capabilities and demands (no refdes, no part numbers, no
pin names), scoped RAILS and GROUNDS, buses/interfaces, POOLS of declared-
equivalent units, invariant DECLARATIONS, escalations-with-explanations, and
the ALLOCATION RECORD (entries + lock groups + document series).

Layer 1 is valid and scoreable STANDALONE (Gen4 section 4.1): the layer-1
oracle checks demand-satisfiability, rail-scope consistency, pool sufficiency,
address uniqueness, voltage compatibility, invariant well-formedness, and
escalation quality over THIS model — no netlist exists yet. Layer 2 (the
binding/refinement layer) elaborates an IntentDoc down to the v2
``schema.CanonicalGraph``; a refinement may only NARROW layer-1 semantics,
never change them.

Identity conventions (all authored ids are plain strings, unique per kind
within one document):

  * Role.id, Demand.id, Rail.name, Ground.name, Bond.name, Bus.name, Pool.name
    are the referenceable identities. NOTHING here is a refdes — refdes are
    minted at layer 2 by the substrate (composition law 8).
  * An intent-level node reference (used by InvariantDecl) is the string
    ``"<role_id>.<signal>"`` — a role-local abstract signal label, NOT a pin
    name of any concrete part. A bare ``"<role_id>"`` is also legal where the
    whole role is the subject.
  * A pool UNIT is identified by its integer index ``0 .. unit_count-1`` in
    canonical order. Unit indices are the stable identity the allocation
    record and pinning ladder use ("author pins UART2" == pins unit 2).

Pure Python 3 stdlib. Plain imports only (flat harness directory).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from schema import GROUND_KINDS, GROUND_ROLES  # shared ground vocabulary


# ---------------------------------------------------------------------------
# Controlled vocabularies (documented, not hard-enforced — same convention as
# schema.py: an over-strict schema must not break candidates, but the oracle
# layers agree on the meaning of each string via these constants).
# ---------------------------------------------------------------------------

# Demand / Capability iface examples (open vocabulary; these are the corpus's):
#   "power", "i2c", "uart", "oscillator", "nand", "pwm", "gate_drive", ...

# AllocationEntry.chosen_by
CHOSEN_BY = frozenset({"solver", "author"})

# AllocationEntry.state — the pinning ladder (Gen4 section 2.3)
ALLOC_STATES = frozenset({"free", "sticky", "pinned"})

# LockGroup.covers members — decision classes (Gen4 section 2.5). The covers
# predicate is a SIMPLE LIST of these classes: an allocation entry / binding
# whose decision class is in the list is covered by the group.
DECISION_CLASSES = frozenset({
    "pool_allocation",    # which pool unit serves which demand
    "part_binding",       # role -> concrete part choice (layer 2)
    "pin_map",            # emitted pin-map rows
    "footprint",          # footprint choice (layer 2 / T2)
    "connector_pinout",   # external-interface pinouts / ICD nets
    "design_rule",        # named design-rule values
})

# InvariantDecl.kind — layer-1 declarations; only mutual_exclusion is
# model-checked (at layer 2) this generation.
INVARIANT_KINDS = frozenset({"mutual_exclusion"})


# ---------------------------------------------------------------------------
# Intent-side dataclasses
# ---------------------------------------------------------------------------

@dataclass
class Capability:
    """Something a role PROVIDES.

    ``iface``  interface / concept name provided (e.g. "i2c_master", "power").
    ``volts``  for a power capability: the voltage it can source (a regulator
               role bridging VBUS->+3V3 has Capability(iface="power",
               volts=3.3, rail="+3V3")).
    ``rail``   the in-scope rail name this capability DRIVES, if it is a rail
               source (rail-tree consistency keys off this), else None.
    ``attrs``  free dict of salient properties.

    Multiplicity is NOT expressed here — a multi-unit provision (3 UARTs,
    4 NAND gates) is a Pool, never a repeated capability.
    """

    iface: str
    volts: Optional[float] = None
    rail: Optional[str] = None
    attrs: Dict[str, Any] = field(default_factory=dict)


@dataclass
class Demand:
    """Something a role NEEDS. Layer 1 demands are symbolic: "a UART",
    "a NAND", "power at 3.3V" — never a unit name, never a pin.

    ``id``      unique demand id within the document (allocation entries
                reference it).
    ``iface``   what is demanded (e.g. "power", "uart", "nand", "i2c",
                "oscillator").
    ``volts``   for a power demand: the rail voltage required (drives the
                layer-1 voltage-compatibility and rail-scope checks).
    ``bus``     name of the Bus this demand attaches to, if any (address
                uniqueness is checked per (bus, i2c_addr)).
    ``qty``     how many units of the interface are demanded (pool
                sufficiency compares sum(qty) against Pool.unit_count).
    ``default`` a DECLARED default satisfier (a rail name, role id, or pool
                name), or None. Rule 2: an unmet demand with no declared
                default is a load error at layer 1.
    ``attrs``   free dict, e.g. {"i2c_addr": 0x48}.
    """

    id: str
    iface: str
    volts: Optional[float] = None
    bus: Optional[str] = None
    qty: int = 1
    default: Optional[str] = None
    attrs: Dict[str, Any] = field(default_factory=dict)


@dataclass
class Role:
    """An abstract part/module in the intent graph. NO refdes, NO part number,
    NO pin names — those exist only at layer 2.

    ``id``           unique role id (e.g. "mcu", "sensor_a", "bridge1").
    ``kind``         abstract capability class (e.g. "mcu", "regulator",
                     "sensor", "half_bridge_module"). Open vocabulary.
    ``scope``        scope id the role lives in ("" = document root). Power
                     demands auto-satisfy from the NEAREST in-scope compatible
                     rail (the cascade, composition law 3).
    ``capabilities`` what the role provides.
    ``demands``      what the role needs.
    ``attrs``        free dict of salient properties.
    """

    id: str
    kind: str
    scope: str = ""
    capabilities: List[Capability] = field(default_factory=list)
    demands: List[Demand] = field(default_factory=list)
    attrs: Dict[str, Any] = field(default_factory=dict)

    def demand(self, did: str) -> Optional[Demand]:
        for d in self.demands:
            if d.id == did:
                return d
        return None


@dataclass
class Scope:
    """A named lexical scope for rails/grounds/buses. The root scope is the
    implicit ``""`` and needs no Scope entry; declare Scope objects only for
    nested scopes (e.g. a motor-driver island bringing its own rail).

    ``id``     scope id (non-empty).
    ``parent`` enclosing scope id ("" = root).
    """

    id: str
    parent: str = ""


@dataclass
class Rail:
    """A power net at a nominal voltage, FUNCTIONALLY named in design
    vocabulary (KiCad style, no dots): "+3V3", "+5V", "VBUS", "VBAT", "VSYS",
    "VIN". Never a pin name ("VCC"/"VDD" are part-definition pin vocabulary
    and must not appear as rail names).

    ``name``  functional rail name.
    ``volts`` nominal voltage in volts.
    ``scope`` scope id where declared ("" = root). Nested scopes override.
    ``attrs`` free dict.
    """

    name: str
    volts: float
    scope: str = ""
    attrs: Dict[str, Any] = field(default_factory=dict)


@dataclass
class Ground:
    """One 0 V return, tagged by kind and role — NOT a split net (one plane,
    partitioned by role; roles are joined only by authored Bonds).

    ``name``  ground identity (e.g. "GND", "GNDA", "CHASSIS").
    ``kind``  one of GROUND_KINDS: "ground" (signal/power 0V return),
              "chassis", "earth".
    ``role``  one of GROUND_ROLES: "none", "analog", "digital", "power",
              "reference" (GNDA/GNDD/PGND/GNDREF). Soft this generation:
              grader-noted, not oracle-gated.
    ``scope`` scope id where declared.
    ``attrs`` free dict.
    """

    name: str
    kind: str = "ground"
    role: str = "none"
    scope: str = ""
    attrs: Dict[str, Any] = field(default_factory=dict)


@dataclass
class Bond:
    """A first-class AUTHORED connection object joining ground roles/kinds at
    a single point (star point / net-tie). Grounds are never implicitly
    merged; every join is a declared Bond.

    ``name``  bond id (e.g. "star1").
    ``joins`` list of Ground names joined at this bond (>= 2).
    ``attrs`` free dict (e.g. {"style": "net_tie"}).
    """

    name: str
    joins: List[str] = field(default_factory=list)
    attrs: Dict[str, Any] = field(default_factory=dict)


@dataclass
class Bus:
    """A shared multi-drop interface instance (e.g. one I2C bus). Roles attach
    via demands/capabilities carrying ``bus=<name>``.

    ``name``  bus id (e.g. "I2C0").
    ``iface`` interface type (e.g. "i2c", "spi", "can").
    ``scope`` scope id where declared.
    ``attrs`` free dict.
    """

    name: str
    iface: str
    scope: str = ""
    attrs: Dict[str, Any] = field(default_factory=dict)


@dataclass
class Pool:
    """A declared swap group of interchangeable units on one role: the 7400's
    four NANDs, an MCU's three UARTs, six PWM channels. Equivalence is
    DECLARED here, never inferred. Units are identified by integer index
    ``0 .. unit_count-1``; uncommitted units remain visible spare capacity.

    ``name``           unique pool name (e.g. "mcu.uarts").
    ``role``           owning Role.id.
    ``provides``       the iface one unit satisfies (matches Demand.iface,
                       e.g. "uart", "nand", "pwm").
    ``unit_count``     number of interchangeable units.
    ``port_signature`` the TYPED port set of ONE unit, as "name:type" strings
                       (e.g. ["tx:out", "rx:in"]). A unit swap is a bijection
                       between port sets with equal signatures; bundles swap
                       whole (a diff-pair never splits its half).
    ``attrs``          free dict.
    """

    name: str
    role: str
    provides: str
    unit_count: int = 1
    port_signature: List[str] = field(default_factory=list)
    attrs: Dict[str, Any] = field(default_factory=dict)


@dataclass
class InvariantDecl:
    """A layer-1 invariant DECLARATION over intent-level references.

    Distinct from ``schema.Invariant`` (layer 2), whose anchors are concrete
    (refdes, pin) nodes: here every reference is an intent-level string
    ``"<role_id>.<signal>"`` (or a bare role id). Layer-1 well-formedness =
    known kind, the right subject arity for the kind, and every referenced
    role id existing in the document. The layer-2 elaboration lowers this
    declaration to a concrete ``schema.Invariant`` for model checking.

    ``kind``     one of INVARIANT_KINDS ("mutual_exclusion": exactly 2
                 subjects that must never both be asserted).
    ``subjects`` the guarded intent-level references.
    ``inputs``   the free command inputs the (layer-2) checker enumerates.
    ``attrs``    free dict.
    """

    kind: str
    subjects: List[str] = field(default_factory=list)
    inputs: List[str] = field(default_factory=list)
    attrs: Dict[str, Any] = field(default_factory=dict)


@dataclass
class Escalation:
    """A ladder-rung-4 escalation WITH EXPLANATION (Gen4 section 2.4): an
    ambiguity across non-equivalent alternatives (or no-policy situation)
    that must be surfaced to the author, never silently resolved.

    ``code``       stable short code (e.g. "AMBIGUOUS_NONEQUIV").
    ``msg``        human-readable statement of the ambiguity.
    ``subjects``   the role/demand ids involved.
    ``conflict``   UNSAT-core style: WHICH requirements conflict (list of
                   requirement statements / demand ids).
    ``relaxation`` what MINIMAL relaxation would resolve it.
    """

    code: str
    msg: str
    subjects: List[str] = field(default_factory=list)
    conflict: List[str] = field(default_factory=list)
    relaxation: str = ""


# ---------------------------------------------------------------------------
# Allocation record — Gen4 section 4.4(b) / 2.3 / 2.5
# ---------------------------------------------------------------------------

@dataclass
class AllocationEntry:
    """One realized allocation: pool unit -> demand, with provenance and the
    pinning-ladder state.

    ``pool``      Pool.name.
    ``unit``      integer unit index within the pool (0-based, canonical).
    ``demand``    Demand.id being served.
    ``chosen_by`` "solver" (engine decision, re-derivable) or "author"
                  (an explicit pin; author pins outrank the solver).
    ``state``     "free" -> "sticky" -> "pinned" (the ladder). Promotion of a
                  Realized entry to Asserted == pinning.
    ``locked_by`` "<group>@<version>" when a lock-group sync point promoted
                  this entry, else None. Within a series, locked entries are
                  immutable; editing one requires break_lock -> new series.
    """

    pool: str
    unit: int
    demand: str
    chosen_by: str = "solver"
    state: str = "free"
    locked_by: Optional[str] = None


@dataclass
class LockGroup:
    """Declared UP FRONT as document metadata (Gen4 section 2.5).

    ``name``       group name (e.g. "firmware-facing").
    ``covers``     the covers-predicate as a SIMPLE decision-class list
                   (members of DECISION_CLASSES): an entry whose decision
                   class is listed is covered.
    ``version``    integer lock version, bumped at each sync-point firing.
                   0 == never locked.
    ``snapshot``   the frozen JSON forms of the covered allocation entries at
                   the moment of the last lock (list of dicts, exactly what
                   ``_alloc_entry_to_json`` emits), or None if never locked.
                   Lock-violation detection is a mechanical diff of the
                   current covered entries against this snapshot, per group.
    ``owner``      optional owner label.
    ``sync_point`` optional lock-event label (e.g. "placement-start",
                   "firmware-freeze", "spin-release").
    """

    name: str
    covers: List[str] = field(default_factory=list)
    version: int = 0
    snapshot: Optional[List[Dict[str, Any]]] = None
    owner: str = ""
    sync_point: str = ""


@dataclass
class AllocationRecord:
    """The solver's Realized allocation view — first-class, diffable.

    ``entries``        the allocation entries. Serialization is CANONICAL:
                       ``to_json`` sorts by (pool, unit, demand), so two
                       deterministic re-solves emit byte-identical records.
    ``lock_groups``    declared lock groups (sorted by name on emit).
    ``solver_version`` provenance: re-solves are deterministic given
                       (document, solver-version).
    """

    entries: List[AllocationEntry] = field(default_factory=list)
    lock_groups: List[LockGroup] = field(default_factory=list)
    solver_version: str = ""

    def entry_for_demand(self, demand_id: str) -> Optional[AllocationEntry]:
        for e in self.entries:
            if e.demand == demand_id:
                return e
        return None

    def entries_for_pool(self, pool_name: str) -> List[AllocationEntry]:
        return [e for e in self.entries if e.pool == pool_name]


# ---------------------------------------------------------------------------
# The document
# ---------------------------------------------------------------------------

@dataclass
class IntentDoc:
    """One layer-1 intent document + its allocation record.

    ``series``      the document series identity (board-spin semantics,
                    Gen4 section 2.5): breaking a locked decision forks a new
                    series. Every emitted artifact is stamped with
                    (series, lock-group versions).
    ``forked_from`` the SERIES FORK RECORD: ``{"series": <parent series>,
                    "reason": <why>}`` or None. A legal series fork (the
                    ``break_lock`` escape hatch) MUST record the series it
                    forked from; a document whose series differs from an
                    external lock baseline WITHOUT a forked_from naming that
                    baseline series is SERIES_UNJUSTIFIED — locked edits are
                    never legalized by hand-editing the series string.
    ``scopes``      declared NESTED scopes (root "" is implicit).
    ``roles``       abstract roles (no refdes / part numbers / pin names).
    ``rails``       scoped, functionally-named power rails.
    ``grounds``     ground objects (kind + role tags).
    ``bonds``       authored star-point / net-tie joins between grounds.
    ``buses``       shared interface instances.
    ``pools``       declared swap groups of interchangeable units.
    ``invariants``  layer-1 invariant declarations.
    ``escalations`` ladder-rung-4 escalations with explanations.
    ``allocation``  the allocation record (entries + lock groups).
    ``attrs``       free document metadata.
    """

    series: str = "A"
    forked_from: Optional[Dict[str, str]] = None
    scopes: List[Scope] = field(default_factory=list)
    roles: List[Role] = field(default_factory=list)
    rails: List[Rail] = field(default_factory=list)
    grounds: List[Ground] = field(default_factory=list)
    bonds: List[Bond] = field(default_factory=list)
    buses: List[Bus] = field(default_factory=list)
    pools: List[Pool] = field(default_factory=list)
    invariants: List[InvariantDecl] = field(default_factory=list)
    escalations: List[Escalation] = field(default_factory=list)
    allocation: AllocationRecord = field(default_factory=AllocationRecord)
    attrs: Dict[str, Any] = field(default_factory=dict)

    # -- convenience accessors (not part of the serialization) --------------
    def role(self, rid: str) -> Optional[Role]:
        for r in self.roles:
            if r.id == rid:
                return r
        return None

    def rail(self, name: str) -> Optional[Rail]:
        for r in self.rails:
            if r.name == name:
                return r
        return None

    def ground(self, name: str) -> Optional[Ground]:
        for g in self.grounds:
            if g.name == name:
                return g
        return None

    def pool(self, name: str) -> Optional[Pool]:
        for p in self.pools:
            if p.name == name:
                return p
        return None

    def bus(self, name: str) -> Optional[Bus]:
        for b in self.buses:
            if b.name == name:
                return b
        return None

    def demand(self, did: str) -> Optional[Demand]:
        for r in self.roles:
            d = r.demand(did)
            if d is not None:
                return d
        return None

    def scope_chain(self, scope_id: str) -> List[str]:
        """The scope id and its ancestors, innermost first, ending at ""
        (root). Used by the nearest-in-scope rail cascade. A dangling parent
        link terminates the chain (the layer-1 oracle flags it separately)."""
        chain = [scope_id]
        by_id = {s.id: s for s in self.scopes}
        seen = {scope_id}
        cur = scope_id
        while cur != "":
            s = by_id.get(cur)
            nxt = s.parent if s is not None else ""
            if nxt in seen:
                break  # cycle guard
            chain.append(nxt)
            seen.add(nxt)
            cur = nxt
        return chain


# ---------------------------------------------------------------------------
# Serialization: neutral JSON <-> IntentDoc.
#
# Hand-rolled (same policy as schema.py): we control exactly which fields
# appear, from_json is tolerant of missing optionals, and the allocation
# record serializes CANONICALLY (sorted) so deterministic re-solves are
# byte-comparable. Optional/None fields are emitted only when set, keeping
# minimal documents minimal.
# ---------------------------------------------------------------------------

def _put(d: Dict[str, Any], key: str, value: Any, default: Any) -> None:
    """Emit ``key`` only when ``value`` differs from its default."""
    if value != default:
        d[key] = value


def _capability_to_json(c: Capability) -> Dict[str, Any]:
    d: Dict[str, Any] = {"iface": c.iface}
    _put(d, "volts", c.volts, None)
    _put(d, "rail", c.rail, None)
    _put(d, "attrs", dict(c.attrs), {})
    return d


def _capability_from_json(d: Dict[str, Any]) -> Capability:
    return Capability(
        iface=d["iface"],
        volts=d.get("volts"),
        rail=d.get("rail"),
        attrs=dict(d.get("attrs", {})),
    )


def _demand_to_json(x: Demand) -> Dict[str, Any]:
    d: Dict[str, Any] = {"id": x.id, "iface": x.iface}
    _put(d, "volts", x.volts, None)
    _put(d, "bus", x.bus, None)
    _put(d, "qty", x.qty, 1)
    _put(d, "default", x.default, None)
    _put(d, "attrs", dict(x.attrs), {})
    return d


def _demand_from_json(d: Dict[str, Any]) -> Demand:
    return Demand(
        id=d["id"],
        iface=d["iface"],
        volts=d.get("volts"),
        bus=d.get("bus"),
        qty=int(d.get("qty", 1)),
        default=d.get("default"),
        attrs=dict(d.get("attrs", {})),
    )


def _role_to_json(r: Role) -> Dict[str, Any]:
    d: Dict[str, Any] = {"id": r.id, "kind": r.kind}
    _put(d, "scope", r.scope, "")
    _put(d, "capabilities", [_capability_to_json(c) for c in r.capabilities], [])
    _put(d, "demands", [_demand_to_json(x) for x in r.demands], [])
    _put(d, "attrs", dict(r.attrs), {})
    return d


def _role_from_json(d: Dict[str, Any]) -> Role:
    return Role(
        id=d["id"],
        kind=d.get("kind", ""),
        scope=d.get("scope", ""),
        capabilities=[_capability_from_json(c) for c in d.get("capabilities", [])],
        demands=[_demand_from_json(x) for x in d.get("demands", [])],
        attrs=dict(d.get("attrs", {})),
    )


def _scope_to_json(s: Scope) -> Dict[str, Any]:
    d: Dict[str, Any] = {"id": s.id}
    _put(d, "parent", s.parent, "")
    return d


def _scope_from_json(d: Dict[str, Any]) -> Scope:
    return Scope(id=d["id"], parent=d.get("parent", ""))


def _rail_to_json(r: Rail) -> Dict[str, Any]:
    d: Dict[str, Any] = {"name": r.name, "volts": r.volts}
    _put(d, "scope", r.scope, "")
    _put(d, "attrs", dict(r.attrs), {})
    return d


def _rail_from_json(d: Dict[str, Any]) -> Rail:
    return Rail(
        name=d["name"],
        volts=float(d["volts"]),
        scope=d.get("scope", ""),
        attrs=dict(d.get("attrs", {})),
    )


def _ground_to_json(g: Ground) -> Dict[str, Any]:
    d: Dict[str, Any] = {"name": g.name}
    _put(d, "kind", g.kind, "ground")
    _put(d, "role", g.role, "none")
    _put(d, "scope", g.scope, "")
    _put(d, "attrs", dict(g.attrs), {})
    return d


def _ground_from_json(d: Dict[str, Any]) -> Ground:
    return Ground(
        name=d["name"],
        kind=d.get("kind", "ground"),
        role=d.get("role", "none"),
        scope=d.get("scope", ""),
        attrs=dict(d.get("attrs", {})),
    )


def _bond_to_json(b: Bond) -> Dict[str, Any]:
    d: Dict[str, Any] = {"name": b.name, "joins": list(b.joins)}
    _put(d, "attrs", dict(b.attrs), {})
    return d


def _bond_from_json(d: Dict[str, Any]) -> Bond:
    return Bond(
        name=d["name"],
        joins=list(d.get("joins", [])),
        attrs=dict(d.get("attrs", {})),
    )


def _bus_to_json(b: Bus) -> Dict[str, Any]:
    d: Dict[str, Any] = {"name": b.name, "iface": b.iface}
    _put(d, "scope", b.scope, "")
    _put(d, "attrs", dict(b.attrs), {})
    return d


def _bus_from_json(d: Dict[str, Any]) -> Bus:
    return Bus(
        name=d["name"],
        iface=d.get("iface", ""),
        scope=d.get("scope", ""),
        attrs=dict(d.get("attrs", {})),
    )


def _pool_to_json(p: Pool) -> Dict[str, Any]:
    d: Dict[str, Any] = {
        "name": p.name,
        "role": p.role,
        "provides": p.provides,
        "unit_count": p.unit_count,
        "port_signature": list(p.port_signature),
    }
    _put(d, "attrs", dict(p.attrs), {})
    return d


def _pool_from_json(d: Dict[str, Any]) -> Pool:
    return Pool(
        name=d["name"],
        role=d.get("role", ""),
        provides=d.get("provides", ""),
        unit_count=int(d.get("unit_count", 1)),
        port_signature=list(d.get("port_signature", [])),
        attrs=dict(d.get("attrs", {})),
    )


def _invariant_decl_to_json(i: InvariantDecl) -> Dict[str, Any]:
    d: Dict[str, Any] = {"kind": i.kind, "subjects": list(i.subjects)}
    _put(d, "inputs", list(i.inputs), [])
    _put(d, "attrs", dict(i.attrs), {})
    return d


def _invariant_decl_from_json(d: Dict[str, Any]) -> InvariantDecl:
    return InvariantDecl(
        kind=d.get("kind", "mutual_exclusion"),
        subjects=list(d.get("subjects", [])),
        inputs=list(d.get("inputs", [])),
        attrs=dict(d.get("attrs", {})),
    )


def _escalation_to_json(e: Escalation) -> Dict[str, Any]:
    d: Dict[str, Any] = {"code": e.code, "msg": e.msg}
    _put(d, "subjects", list(e.subjects), [])
    _put(d, "conflict", list(e.conflict), [])
    _put(d, "relaxation", e.relaxation, "")
    return d


def _escalation_from_json(d: Dict[str, Any]) -> Escalation:
    return Escalation(
        code=d.get("code", "ESCALATE"),
        msg=d.get("msg", ""),
        subjects=list(d.get("subjects", [])),
        conflict=list(d.get("conflict", [])),
        relaxation=d.get("relaxation", ""),
    )


def _alloc_entry_to_json(e: AllocationEntry) -> Dict[str, Any]:
    return {
        "pool": e.pool,
        "unit": e.unit,
        "demand": e.demand,
        "chosen_by": e.chosen_by,
        "state": e.state,
        "locked_by": e.locked_by,
    }


def _alloc_entry_from_json(d: Dict[str, Any]) -> AllocationEntry:
    return AllocationEntry(
        pool=d["pool"],
        unit=int(d["unit"]),
        demand=d["demand"],
        chosen_by=d.get("chosen_by", "solver"),
        state=d.get("state", "free"),
        locked_by=d.get("locked_by"),
    )


def _lock_group_to_json(g: LockGroup) -> Dict[str, Any]:
    d: Dict[str, Any] = {
        "name": g.name,
        "covers": list(g.covers),
        "version": g.version,
        "snapshot": (None if g.snapshot is None
                     else [dict(x) for x in g.snapshot]),
    }
    _put(d, "owner", g.owner, "")
    _put(d, "sync_point", g.sync_point, "")
    return d


def _lock_group_from_json(d: Dict[str, Any]) -> LockGroup:
    snap = d.get("snapshot")
    return LockGroup(
        name=d["name"],
        covers=list(d.get("covers", [])),
        version=int(d.get("version", 0)),
        snapshot=None if snap is None else [dict(x) for x in snap],
        owner=d.get("owner", ""),
        sync_point=d.get("sync_point", ""),
    )


def _allocation_to_json(a: AllocationRecord) -> Dict[str, Any]:
    # CANONICAL emit: entries sorted by (pool, unit, demand), lock groups by
    # name — deterministic re-solves must be byte-identical.
    entries = sorted(a.entries, key=lambda e: (e.pool, e.unit, e.demand))
    groups = sorted(a.lock_groups, key=lambda g: g.name)
    return {
        "entries": [_alloc_entry_to_json(e) for e in entries],
        "lock_groups": [_lock_group_to_json(g) for g in groups],
        "solver_version": a.solver_version,
    }


def _allocation_from_json(d: Dict[str, Any]) -> AllocationRecord:
    return AllocationRecord(
        entries=[_alloc_entry_from_json(e) for e in d.get("entries", [])],
        lock_groups=[_lock_group_from_json(g) for g in d.get("lock_groups", [])],
        solver_version=d.get("solver_version", ""),
    )


def to_json(doc: IntentDoc) -> Dict[str, Any]:
    """Serialize an IntentDoc to a plain JSON-safe dict (layer-1 shape)."""
    d: Dict[str, Any] = {
        "layer": 1,
        "series": doc.series,
        "roles": [_role_to_json(r) for r in doc.roles],
        "rails": [_rail_to_json(r) for r in doc.rails],
        "grounds": [_ground_to_json(g) for g in doc.grounds],
        "allocation": _allocation_to_json(doc.allocation),
    }
    _put(d, "forked_from",
         None if doc.forked_from is None else dict(doc.forked_from), None)
    _put(d, "scopes", [_scope_to_json(s) for s in doc.scopes], [])
    _put(d, "bonds", [_bond_to_json(b) for b in doc.bonds], [])
    _put(d, "buses", [_bus_to_json(b) for b in doc.buses], [])
    _put(d, "pools", [_pool_to_json(p) for p in doc.pools], [])
    _put(d, "invariants",
         [_invariant_decl_to_json(i) for i in doc.invariants], [])
    _put(d, "escalations",
         [_escalation_to_json(e) for e in doc.escalations], [])
    _put(d, "attrs", dict(doc.attrs), {})
    return d


def from_json(d: Dict[str, Any]) -> IntentDoc:
    """Rebuild an IntentDoc from a dict produced by ``to_json``.

    Tolerant of missing optional fields; a bare ``{}`` yields an empty doc."""
    ff = d.get("forked_from")
    return IntentDoc(
        series=d.get("series", "A"),
        forked_from=None if ff is None else dict(ff),
        scopes=[_scope_from_json(s) for s in d.get("scopes", [])],
        roles=[_role_from_json(r) for r in d.get("roles", [])],
        rails=[_rail_from_json(r) for r in d.get("rails", [])],
        grounds=[_ground_from_json(g) for g in d.get("grounds", [])],
        bonds=[_bond_from_json(b) for b in d.get("bonds", [])],
        buses=[_bus_from_json(b) for b in d.get("buses", [])],
        pools=[_pool_from_json(p) for p in d.get("pools", [])],
        invariants=[_invariant_decl_from_json(i)
                    for i in d.get("invariants", [])],
        escalations=[_escalation_from_json(e)
                     for e in d.get("escalations", [])],
        allocation=_allocation_from_json(d.get("allocation", {})),
        attrs=dict(d.get("attrs", {})),
    )


__all__ = [
    "Capability",
    "Demand",
    "Role",
    "Scope",
    "Rail",
    "Ground",
    "Bond",
    "Bus",
    "Pool",
    "InvariantDecl",
    "Escalation",
    "AllocationEntry",
    "LockGroup",
    "AllocationRecord",
    "IntentDoc",
    "to_json",
    "from_json",
    "GROUND_KINDS",
    "GROUND_ROLES",
    "CHOSEN_BY",
    "ALLOC_STATES",
    "DECISION_CLASSES",
    "INVARIANT_KINDS",
]


# ---------------------------------------------------------------------------
# smoke test:  python schema_l1.py   (round-trip + canonical-emit determinism)
# ---------------------------------------------------------------------------

if __name__ == "__main__":  # pragma: no cover
    import json
    import sys

    doc = IntentDoc(
        series="A",
        roles=[
            Role(id="mcu", kind="mcu",
                 capabilities=[Capability(iface="i2c_master")],
                 demands=[Demand(id="mcu.pwr", iface="power", volts=3.3)]),
            Role(id="periph", kind="uart_device",
                 demands=[Demand(id="periph.pwr", iface="power", volts=3.3),
                          Demand(id="periph.uart", iface="uart")]),
        ],
        rails=[Rail(name="+3V3", volts=3.3)],
        grounds=[Ground(name="GND"),
                 Ground(name="PGND", role="power")],
        bonds=[Bond(name="star1", joins=["GND", "PGND"])],
        pools=[Pool(name="mcu.uarts", role="mcu", provides="uart",
                    unit_count=3, port_signature=["tx:out", "rx:in"])],
        invariants=[InvariantDecl(kind="mutual_exclusion",
                                  subjects=["b1.hs_gate", "b1.ls_gate"],
                                  inputs=["mcu.in_h1", "mcu.in_l1"])],
        escalations=[Escalation(
            code="AMBIGUOUS_NONEQUIV",
            msg="hw UART vs bit-banged GPIO; no policy",
            subjects=["periph.uart"],
            conflict=["periph.uart demands 'uart'",
                      "providers 'mcu.uarts' and 'gpio-bitbang' are not "
                      "declared equivalent"],
            relaxation="declare a preference policy or pin one provider")],
        allocation=AllocationRecord(
            entries=[AllocationEntry(pool="mcu.uarts", unit=0,
                                     demand="periph.uart",
                                     chosen_by="solver", state="sticky")],
            lock_groups=[LockGroup(name="firmware-facing",
                                   covers=["pool_allocation", "pin_map"])],
            solver_version="trivial-1",
        ),
    )

    j1 = to_json(doc)
    doc2 = from_json(j1)
    j2 = to_json(doc2)
    s1 = json.dumps(j1, sort_keys=True)
    s2 = json.dumps(j2, sort_keys=True)
    ok = (s1 == s2) and doc2.role("mcu") is not None \
        and doc2.pool("mcu.uarts").unit_count == 3 \
        and doc2.allocation.entry_for_demand("periph.uart").unit == 0 \
        and doc2.ground("PGND").role == "power" \
        and doc2.scope_chain("") == [""]
    # canonical-emit determinism: reversed entry order must emit identically
    doc3 = from_json(j1)
    doc3.allocation.entries.reverse()
    ok = ok and json.dumps(to_json(doc3), sort_keys=True) == s1
    # forked_from (series fork record) round-trips; absent by default
    ok = ok and "forked_from" not in j1
    doc4 = from_json(j1)
    doc4.series = "B"
    doc4.forked_from = {"series": "A", "reason": "break_lock demo"}
    ok = ok and from_json(to_json(doc4)).forked_from == \
        {"series": "A", "reason": "break_lock demo"}
    print("schema_l1 smoke test:", "PASS" if ok else "FAIL")
    sys.exit(0 if ok else 1)
