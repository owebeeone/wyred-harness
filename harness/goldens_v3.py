"""v3 corpus goldens for harness v3 — Gen4 spec section 3, authored at INTENT altitude.

Every corpus problem is now ONE document with two ordered layers (Gen4 section 1):

  * layer 1 — INTENT   : a ``schema_l1.IntentDoc`` (roles / rails / grounds /
                         bonds / buses / pools / invariant decls / escalations
                         + the embedded allocation record). Valid and scoreable
                         STANDALONE by the layer-1 oracle (Gen4 section 4.1).
  * layer 2 — BINDING  : the elaborated ``schema.CanonicalGraph`` netlist,
                         scored by the v2 hardened stack (ERC / invariants /
                         round-trip) plus the v3 allocation checks
                         (Gen4 section 4.2). None where the document never
                         reaches binding (#3, #7 fail at L1; #5b escalates).

Exports
-------

GOLDENS_V3 : OrderedDict keyed by intent id STRING ("1","2","3","4","5a","5b",
             "6","7","8","9"), each value::

                {"l1":       schema_l1.IntentDoc,
                 "l2":       schema.CanonicalGraph or None,
                 "record":   schema_l1.AllocationRecord   # SAME OBJECT as
                                                          # l1.allocation
                 "expected": {"l1": <verdict>, "l2": <verdict or None>},
                 "note":     str}

             Verdict vocabulary is the v2 one, applied PER LAYER:
                 "PASS"          clean at that layer
                 "FAIL:<CODE>"   a Tier-A violation of the named code
                 "ESCALATE"      a rung-4 ambiguity surfaced with explanation
             ``expected["l2"] is None`` exactly when ``l2 is None`` (the
             document legitimately never produces a binding).

KNOWN_BAD_V3 : list of {"l1", "record", "expected_code", "layer", "note"} —
             corrupted layer-1 / allocation-record documents the v3 oracle
             layers must flag. Stable expected codes (the layer-1 /
             allocation-check vocabulary this corpus pins down):

                 ADDR_COLLISION       (#3, also a golden)  same (bus, i2c_addr) twice
                 VOLTAGE_MISMATCH     (#7, also a golden)  demand volts vs scoped rail
                 POOL_INSUFFICIENT    pool overflow: matching demand qty exceeds
                                      pool capacity (intent_oracle's code)
                 ALLOC_BIJECTION      demand->unit map is not a legal bijection
                 LOCK_VIOLATION       a lock-covered decision changed with NO series bump
                 DEMAND_UNSATISFIABLE a demand with no in-scope satisfier and no
                                      declared default (resolution-ladder rung 2;
                                      intent_oracle's code — distinct from
                                      spec_satisfaction's UNMET_DEMAND, which is
                                      the graph-realization failure of a demand
                                      that IS satisfiable at layer 1)

spare_units(pool, record) : the pool's unallocated unit indices — the VISIBLE
             spare capacity (uncommitted units are never silently deleted).

Conventions this corpus fixes (consumed by the sibling v3 oracle modules)
-------------------------------------------------------------------------

* An ambiguity among DECLARED-EQUIVALENT pool units is an ALLOCATION
  (ladder rung 3): solver assigns deterministically (canonical stable order:
  demands sorted by id, lowest free unit first), records provenance, and the
  record round-trips byte-identically (schema_l1.to_json is canonical).
  #5a and #8 exercise this.
* An ambiguity across NON-EQUIVALENT alternatives with no policy ESCALATES
  with an UNSAT-core-style explanation (rung 4). A declared ``Demand.default``
  (rail / role / pool name) IS a policy and resolves the ambiguity without
  escalation. #5b vs #9 exercise the contrast.
* A SPARE pool unit is a unit with NO allocation entry: state "free" by
  definition on the pinning ladder, visible because the Pool declaration keeps
  its ``unit_count`` (EMIT_CONTRACT Part B: uncommitted units remain visible
  spare capacity). ``spare_units`` derives them. At layer 2 the spare unit's
  hardware stays in the netlist (inputs tied per practice, output on an
  explicit "open" net) — never silently dropped. #9 exercises this.
* Lock-time series stamp: when a lock group's sync point fires (version >= 1,
  snapshot frozen), the document records the series it was locked in under
  ``doc.attrs["lock_series"][<group name>]``. Lock-violation detection is the
  mechanical per-group diff of current covered entries against ``snapshot``;
  a difference while ``doc.series == attrs["lock_series"][group]`` (no series
  bump / no break_lock fork) is LOCK_VIOLATION.
* Ground ROLE checks are SOFT this generation: the corpus only guarantees
  PRESENCE (#8 declares Ground(role="power") for the gate-drive return plus an
  authored Bond joining it to GND at a star point, and the bound netlist
  carries the optional Net.ground_kind/ground_role/bond fields).

v3 is ADDITIVE: this module never mutates goldens.py state (the v2 builders
return fresh graphs per call) and nothing in the v2 stack imports it.

Pure Python 3 stdlib. Plain imports only. Run from inside rl/harness.
"""

from __future__ import annotations

from collections import OrderedDict
from typing import Dict, List, Optional

from schema import CanonicalGraph, Component, Net, Terminal
from schema_l1 import (
    AllocationEntry,
    AllocationRecord,
    Bond,
    Bus,
    Capability,
    Demand,
    Escalation,
    Ground,
    IntentDoc,
    InvariantDecl,
    LockGroup,
    Pool,
    Rail,
    Role,
    Scope,
)

# v2 golden builders reused as the layer-2 bindings for intents 1/2/4/6/8
# (selftest.py imports these same privates; the flat harness treats goldens.py
# builder helpers as shared corpus infrastructure).
from goldens import (
    _T,
    _decoupling_cap,
    _golden_1,
    _golden_2,
    _golden_4,
    _golden_6,
    _golden_8,
    _source,
)


_SOLVER = "trivial-1"     # Gen4 section 2.3: trivial-deterministic policy


# ===========================================================================
# small layer-1 builder helpers
# ===========================================================================

def _pwr(did: str, volts: float) -> Demand:
    """A power demand at the given rail voltage."""
    return Demand(id=did, iface="power", volts=volts)


def _supply(role_id: str, rail: str, volts: float, scope: str = "") -> Role:
    """An abstract supply role DRIVING the named in-scope rail (rail-tree
    consistency: every rail has a driving power capability)."""
    return Role(
        id=role_id, kind="supply", scope=scope,
        capabilities=[Capability(iface="power", volts=volts, rail=rail)],
    )


def _i2c_demand(did: str, bus: str, addr: int) -> Demand:
    return Demand(id=did, iface="i2c", bus=bus, attrs={"i2c_addr": addr})


def _rec(entries: Optional[List[AllocationEntry]] = None,
         lock_groups: Optional[List[LockGroup]] = None) -> AllocationRecord:
    return AllocationRecord(
        entries=list(entries or []),
        lock_groups=list(lock_groups or []),
        solver_version=_SOLVER,
    )


def spare_units(pool: Pool, record: AllocationRecord) -> List[int]:
    """The pool's unallocated unit indices, sorted — the VISIBLE spare
    capacity (a spare unit has no allocation entry; ladder state "free" by
    definition). Never deleted: the Pool keeps declaring unit_count."""
    taken = {e.unit for e in record.entries if e.pool == pool.name}
    return [u for u in range(pool.unit_count) if u not in taken]


def _prov(g: CanonicalGraph, l1_role: Dict[str, str],
          for_demand: Dict[str, str]) -> CanonicalGraph:
    """F2 demand traceability: stamp EMIT_CONTRACT provenance attrs onto a
    bound layer-2 graph. Authored parts carry ``attrs["l1_role"]`` (the L1
    role they bind); generated companions (authored=False) carry
    ``attrs["for_demand"]`` (the L1 demand — or role — whose declared
    companion produced them). spec_satisfaction emits UNDEMANDED_COMPONENT /
    ORPHAN_GENERATED when provenance is missing or dangling."""
    for c in g.components:
        if c.refdes in l1_role:
            c.attrs["l1_role"] = l1_role[c.refdes]
        if c.refdes in for_demand:
            c.attrs["for_demand"] = for_demand[c.refdes]
    return g


# ===========================================================================
# Intent 1 — sensor node                       L1 PASS / L2 PASS
#   L1: scope with VBUS(5V) + demanded +3V3 rail; a regulator role bridging
#       them; MCU role (I2C master); two sensor roles @0x48/0x49 on the bus.
#   L2: v2 golden 1 (bind regulator->LDO, MCU/sensors->parts; decoupling +
#       one shared pull-up pair generated).
# ===========================================================================

def _l1_1() -> IntentDoc:
    return IntentDoc(
        series="A",
        roles=[
            _supply("vbus_supply", "VBUS", 5.0),
            Role(id="reg", kind="regulator",
                 capabilities=[Capability(iface="power", volts=3.3,
                                          rail="+3V3")],
                 demands=[_pwr("reg.vin", 5.0)]),
            Role(id="mcu", kind="mcu",
                 capabilities=[Capability(iface="i2c_master",
                                          attrs={"bus": "I2C0"})],
                 demands=[_pwr("mcu.pwr", 3.3)]),
            Role(id="sensor_a", kind="sensor",
                 demands=[_pwr("sensor_a.pwr", 3.3),
                          _i2c_demand("sensor_a.i2c", "I2C0", 0x48)]),
            Role(id="sensor_b", kind="sensor",
                 demands=[_pwr("sensor_b.pwr", 3.3),
                          _i2c_demand("sensor_b.i2c", "I2C0", 0x49)]),
        ],
        rails=[Rail(name="VBUS", volts=5.0), Rail(name="+3V3", volts=3.3)],
        grounds=[Ground(name="GND")],
        buses=[Bus(name="I2C0", iface="i2c")],
        allocation=_rec(),
    )


# ===========================================================================
# Intent 2 — power tree                        L1 PASS / L2 PASS
#   L1: two rails (+3V3, +1V8) each with a load, chained regulators off
#       VBUS(5V), one ground.  L2: v2 golden 2.
# ===========================================================================

def _l1_2() -> IntentDoc:
    return IntentDoc(
        series="A",
        roles=[
            _supply("vin_supply", "VBUS", 5.0),
            Role(id="reg_a", kind="regulator",
                 capabilities=[Capability(iface="power", volts=3.3,
                                          rail="+3V3")],
                 demands=[_pwr("reg_a.vin", 5.0)]),
            Role(id="reg_b", kind="regulator",
                 capabilities=[Capability(iface="power", volts=1.8,
                                          rail="+1V8")],
                 demands=[_pwr("reg_b.vin", 3.3)]),
            Role(id="load_a", kind="mcu", demands=[_pwr("load_a.pwr", 3.3)]),
            Role(id="load_b", kind="sensor",
                 demands=[_pwr("load_b.pwr", 1.8)]),
        ],
        rails=[Rail(name="VBUS", volts=5.0),
               Rail(name="+3V3", volts=3.3),
               Rail(name="+1V8", volts=1.8)],
        grounds=[Ground(name="GND")],
        allocation=_rec(),
    )


# ===========================================================================
# Intent 3 — I2C address collision             L1 FAIL:ADDR_COLLISION / no L2
#   Three sensor roles on one bus, two pinned to 0x48. Address collision is
#   an INTENT-level static error: the document never reaches binding.
# ===========================================================================

def _l1_3() -> IntentDoc:
    return IntentDoc(
        series="A",
        roles=[
            _supply("supply", "+3V3", 3.3),
            Role(id="mcu", kind="mcu",
                 capabilities=[Capability(iface="i2c_master",
                                          attrs={"bus": "I2C0"})],
                 demands=[_pwr("mcu.pwr", 3.3)]),
            Role(id="sensor_a", kind="sensor",
                 demands=[_pwr("sensor_a.pwr", 3.3),
                          _i2c_demand("sensor_a.i2c", "I2C0", 0x48)]),
            Role(id="sensor_b", kind="sensor",
                 demands=[_pwr("sensor_b.pwr", 3.3),
                          _i2c_demand("sensor_b.i2c", "I2C0", 0x48)]),  # !!
            Role(id="sensor_c", kind="sensor",
                 demands=[_pwr("sensor_c.pwr", 3.3),
                          _i2c_demand("sensor_c.i2c", "I2C0", 0x4A)]),
        ],
        rails=[Rail(name="+3V3", volts=3.3)],
        grounds=[Ground(name="GND")],
        buses=[Bus(name="I2C0", iface="i2c")],
        allocation=_rec(),
    )


# ===========================================================================
# Intent 4 — crystal / oscillator demand       L1 PASS / L2 PASS
#   MCU role demanding an OscillatorSource; a crystal role provides it (the
#   load-cap companions are GENERATED at layer 2 — v2 golden 4).
# ===========================================================================

def _l1_4() -> IntentDoc:
    return IntentDoc(
        series="A",
        roles=[
            _supply("supply", "+3V3", 3.3),
            Role(id="mcu", kind="mcu",
                 demands=[_pwr("mcu.pwr", 3.3),
                          Demand(id="mcu.osc", iface="oscillator")]),
            Role(id="xtal", kind="crystal",
                 capabilities=[Capability(iface="oscillator",
                                          attrs={"freq": "8MHz"})]),
        ],
        rails=[Rail(name="+3V3", volts=3.3)],
        grounds=[Ground(name="GND")],
        allocation=_rec(),
    )


# ===========================================================================
# Intent 5a — UART pool ALLOCATION             L1 PASS / L2 PASS
#   MCU role providing a POOL of 3 declared-equivalent UARTs; one peripheral
#   demanding a UART. Rung 3: the trivial-deterministic solver assigns the
#   canonically-first free unit (unit 0), provenance "solver", state
#   "sticky"; units 1-2 remain visible spare capacity. NOT an escalation.
#   (An author pin would become {"unit": 2, "chosen_by": "author",
#   "state": "pinned"} and outrank the solver on every re-solve.)
#   L2 follows EMIT_CONTRACT Part D.3: the UART edges land on unit 0's ports
#   BECAUSE the record says so — the L1 source never names "U0_TX".
# ===========================================================================

def _l1_5a() -> IntentDoc:
    return IntentDoc(
        series="A",
        roles=[
            _supply("supply", "+3V3", 3.3),
            Role(id="mcu", kind="mcu",
                 demands=[_pwr("mcu.pwr", 3.3)]),
            Role(id="periph", kind="uart_device",
                 demands=[_pwr("periph.pwr", 3.3),
                          Demand(id="periph.uart", iface="uart")]),
        ],
        rails=[Rail(name="+3V3", volts=3.3)],
        grounds=[Ground(name="GND")],
        pools=[Pool(name="mcu.uarts", role="mcu", provides="uart",
                    unit_count=3, port_signature=["tx:out", "rx:in"])],
        allocation=_rec(
            entries=[AllocationEntry(pool="mcu.uarts", unit=0,
                                     demand="periph.uart",
                                     chosen_by="solver", state="sticky")],
            lock_groups=[LockGroup(name="firmware-facing",
                                   covers=["pool_allocation", "pin_map"],
                                   version=0, snapshot=None,
                                   owner="fw-team",
                                   sync_point="firmware-freeze")],
        ),
    )


def _l2_5a() -> CanonicalGraph:
    """EMIT_CONTRACT Part D.3: allocation applied — unit 0 of mcu.uarts binds
    periph.uart to U1's U0_TX/U0_RX pair; units 1-2 are legitimately unwired
    provide terminals (spare capacity)."""
    src = _source("PS1", 3.3)
    mcu_terms = [
        _T("VDD", "power_in", req_v=3.3),
        _T("GND", "ground"),
    ]
    for u in (0, 1, 2):
        mcu_terms.append(_T(f"U{u}_TX", "signal",
                            iface="uart", iface_member="provide"))
        mcu_terms.append(_T(f"U{u}_RX", "signal",
                            iface="uart", iface_member="provide"))
    mcu = Component(
        refdes="U1", kind="mcu", value="MCU-3UART", authored=True,
        terminals=mcu_terms, attrs={"uart_ports": 3},
    )
    dev = Component(
        refdes="U2", kind="uart_device", value="uart-peripheral",
        authored=True,
        terminals=[
            _T("VDD", "power_in", req_v=3.3),
            _T("GND", "ground"),
            _T("TX", "signal", iface="uart", iface_member="require"),
            _T("RX", "signal", iface="uart", iface_member="require"),
        ],
    )
    nets = [
        Net("+3V3", "power", 3.3,
            [("PS1", "VOUT"), ("U1", "VDD"), ("U2", "VDD")]),
        Net("GND", "ground", 0.0,
            [("PS1", "GND"), ("U1", "GND"), ("U2", "GND")],
            ground_kind="ground", ground_role="none"),
        # allocation record: unit 0 serves periph.uart
        Net("UART0_TX", "signal", None, [("U1", "U0_TX"), ("U2", "RX")]),
        Net("UART0_RX", "signal", None, [("U1", "U0_RX"), ("U2", "TX")]),
    ]
    return CanonicalGraph(components=[src, mcu, dev], nets=nets)


# ===========================================================================
# Intent 5b — NON-equivalent alternatives      L1 ESCALATE / no L2
#   The peripheral's serial link could be a hardware UART OR a bit-banged
#   GPIO pair. The two implementations are NOT in a pool (equivalence is
#   DECLARED, never inferred) and no policy / default exists -> ladder rung
#   4: ESCALATE with an UNSAT-core-style explanation. No allocation entry
#   for the demand, no binding.
# ===========================================================================

def _l1_5b() -> IntentDoc:
    return IntentDoc(
        series="A",
        roles=[
            _supply("supply", "+3V3", 3.3),
            Role(id="mcu", kind="mcu",
                 capabilities=[
                     # two capabilities with the same iface, NOT a pool: they
                     # are non-equivalent alternatives.
                     Capability(iface="serial", attrs={"impl": "hw_uart"}),
                     Capability(iface="serial",
                                attrs={"impl": "gpio_bitbang"}),
                 ],
                 demands=[_pwr("mcu.pwr", 3.3)]),
            Role(id="periph", kind="uart_device",
                 demands=[_pwr("periph.pwr", 3.3),
                          Demand(id="periph.serial", iface="serial")]),
        ],
        rails=[Rail(name="+3V3", volts=3.3)],
        grounds=[Ground(name="GND")],
        escalations=[Escalation(
            code="AMBIGUOUS_NONEQUIV",
            msg="periph needs a serial link; hardware UART vs bit-banged "
                "GPIO are viable and no selection policy exists",
            subjects=["periph.serial"],
            conflict=[
                "periph.serial demands iface 'serial'",
                "mcu offers 'serial' via impl=hw_uart AND impl=gpio_bitbang",
                "the two implementations are not declared equivalent "
                "(no pool joins them)",
            ],
            relaxation="declare the implementations as one equivalent pool, "
                       "OR set periph.serial.default to one provider, OR "
                       "add a selection policy",
        )],
        allocation=_rec(),
    )


# ===========================================================================
# Intent 6 — reusable module role x2           L1 PASS / L2 PASS
#   A "sensor-with-decoupling" MODULE role instantiated twice, each instance
#   drawing its rail from scope (closure + scoped-rail cascade). L2: v2
#   golden 6.
# ===========================================================================

def _l1_6() -> IntentDoc:
    def module_role(n: int, addr: int) -> Role:
        return Role(
            id=f"mod{n}", kind="sensor_module",
            attrs={"module": "sensor_with_decoupling", "instance": n},
            demands=[_pwr(f"mod{n}.pwr", 3.3),          # drawn from scope
                     _i2c_demand(f"mod{n}.i2c", "I2C0", addr)],
        )

    return IntentDoc(
        series="A",
        roles=[
            _supply("supply", "+3V3", 3.3),
            Role(id="mcu", kind="mcu",
                 capabilities=[Capability(iface="i2c_master",
                                          attrs={"bus": "I2C0"})],
                 demands=[_pwr("mcu.pwr", 3.3)]),
            module_role(1, 0x48),
            module_role(2, 0x49),
        ],
        rails=[Rail(name="+3V3", volts=3.3)],
        grounds=[Ground(name="GND")],
        buses=[Bus(name="I2C0", iface="i2c")],
        allocation=_rec(),
    )


# ===========================================================================
# Intent 7 — voltage mismatch                  L1 FAIL:VOLTAGE_MISMATCH / no L2
#   A 5V-only device role placed in a 3.3V rail scope. The scope's ONLY
#   power rail is +3V3; the demand wants 5.0V. A power rail EXISTS (so this
#   is NOT UNMET_DEMAND) but is volts-incompatible: an intent-level concept
#   error, caught before any binding.
# ===========================================================================

def _l1_7() -> IntentDoc:
    return IntentDoc(
        series="A",
        roles=[
            _supply("supply", "+3V3", 3.3),
            Role(id="dev", kind="sensor",
                 attrs={"vin_required": 5.0},
                 demands=[_pwr("dev.pwr", 5.0)]),       # 5V-only device
        ],
        rails=[Rail(name="+3V3", volts=3.3)],           # the ONLY rail
        grounds=[Ground(name="GND")],
        allocation=_rec(),
    )


# ===========================================================================
# Intent 8 — BLDC half-bridges (stretch)       L1 PASS / L2 PASS
#   L1: three half-bridge module roles in a nested "power_stage" scope with
#       rails +48V/+12V (the motor island brings its own rails; +3V3 at
#       root); a 6-channel MCU PWM POOL — WHICH channel drives WHICH bridge
#       is an allocation, never authored; per-bridge mutual_exclusion
#       invariant declarations; the gate-drive return is Ground(role=
#       "power") joined to GND by an AUTHORED Bond at a star point (role
#       checks SOFT this generation — presence only).
#   L2: the v2 hardened golden's 2-input cross-inhibit interlock topology,
#       UNCHANGED, with the ground return split GND/PGND joined by bond
#       "star1" (v3 optional Net fields; ERC/invariant behavior identical).
#
#   Canonical solver order (demands sorted by id, lowest free unit first):
#     bridge1.pwm_h->0  bridge1.pwm_l->1  bridge2.pwm_h->2
#     bridge2.pwm_l->3  bridge3.pwm_h->4  bridge3.pwm_l->5
#   which at L2 maps channel k to MCU pins IN_H1,IN_L1,IN_H2,IN_L2,IN_H3,
#   IN_L3 in that order.
# ===========================================================================

def _l1_8() -> IntentDoc:
    roles = [
        _supply("logic_supply", "+3V3", 3.3),
        _supply("hv_supply", "+48V", 48.0, scope="power_stage"),
        _supply("gate_supply", "+12V", 12.0, scope="power_stage"),
        Role(id="mcu", kind="mcu",
             demands=[_pwr("mcu.pwr", 3.3)],
             attrs={"pwm_channels": 6}),
    ]
    invariants = []
    entries = []
    unit = 0
    for b in (1, 2, 3):
        roles.append(Role(
            id=f"bridge{b}", kind="half_bridge_module", scope="power_stage",
            demands=[
                _pwr(f"bridge{b}.hv", 48.0),
                _pwr(f"bridge{b}.gate_pwr", 12.0),
                Demand(id=f"bridge{b}.pwm_h", iface="pwm"),
                Demand(id=f"bridge{b}.pwm_l", iface="pwm"),
            ],
        ))
        invariants.append(InvariantDecl(
            kind="mutual_exclusion",
            subjects=[f"bridge{b}.hs_gate", f"bridge{b}.ls_gate"],
            inputs=[f"mcu.in_h{b}", f"mcu.in_l{b}"],
        ))
        for d in (f"bridge{b}.pwm_h", f"bridge{b}.pwm_l"):
            entries.append(AllocationEntry(pool="mcu.pwm", unit=unit,
                                           demand=d, chosen_by="solver",
                                           state="sticky"))
            unit += 1

    return IntentDoc(
        series="A",
        scopes=[Scope(id="power_stage", parent="")],
        roles=roles,
        rails=[Rail(name="+3V3", volts=3.3),
               Rail(name="+48V", volts=48.0, scope="power_stage"),
               Rail(name="+12V", volts=12.0, scope="power_stage")],
        grounds=[Ground(name="GND"),
                 Ground(name="PGND", role="power", scope="power_stage")],
        bonds=[Bond(name="star1", joins=["GND", "PGND"],
                    attrs={"style": "star_point"})],
        pools=[Pool(name="mcu.pwm", role="mcu", provides="pwm",
                    unit_count=6, port_signature=["out:out"])],
        invariants=invariants,
        allocation=_rec(
            entries=entries,
            lock_groups=[LockGroup(name="firmware-facing",
                                   covers=["pool_allocation", "pin_map"],
                                   version=0, snapshot=None,
                                   sync_point="firmware-freeze")],
        ),
    )


def _l2_8() -> CanonicalGraph:
    """v2 hardened golden 8 (2-input cross-inhibit interlock, unchanged
    topology) with the single GND net split into the logic return (GND) and
    the gate-drive / power return (PGND, role=power), joined by the authored
    bond "star1" — exercising the v3 optional Net ground fields. Nothing in
    the v2 oracle stack keys off these fields."""
    g = _golden_8()
    logic_nodes = {("PS3", "GND"), ("U1", "GND"), ("C1", "-")}
    old = None
    for n in g.nets:
        if n.name == "GND":
            old = n
            break
    assert old is not None
    gnd_nodes = [nd for nd in old.nodes if tuple(nd) in logic_nodes]
    pgnd_nodes = [nd for nd in old.nodes if tuple(nd) not in logic_nodes]
    g.nets.remove(old)
    g.nets.append(Net("GND", "ground", 0.0, gnd_nodes,
                      ground_kind="ground", bond="star1"))
    g.nets.append(Net("PGND", "ground", 0.0, pgnd_nodes,
                      ground_kind="ground", ground_role="power",
                      bond="star1"))
    return g


# ===========================================================================
# Intent 9 (new) — the quad-NAND pool          L1 PASS / L2 PASS
#   Four independent NAND demands across the design; ONE quad-NAND part role
#   (pool of 4). Declared policy on every demand (Demand.default), so
#   nothing escalates:
#     * ctrl.nand_a / _b / _c default to the POOL "glue.nands"  -> rung 3
#       ALLOCATION: legal bijection units 0/1/2, chosen_by solver, sticky.
#     * ctrl.nand_d defaults to the discrete provider ROLE "aux_nand"
#       (a declared default satisfier — rung 2 never fires) and therefore
#       draws NOTHING from the pool.
#   Unit 3 is the SPARE: no allocation entry (ladder state "free" by
#   definition), visible in the record via the pool's declared unit_count
#   (spare_units -> [3]) — never deleted. At L2 the spare gate's inputs are
#   tied to GND per practice and its output sits on an explicit "open" net:
#   present, terminated, unused.
# ===========================================================================

def _l1_9() -> IntentDoc:
    return IntentDoc(
        series="A",
        roles=[
            _supply("supply", "+3V3", 3.3),
            Role(id="ctrl", kind="controller",
                 demands=[
                     _pwr("ctrl.pwr", 3.3),
                     Demand(id="ctrl.nand_a", iface="nand",
                            default="glue.nands"),
                     Demand(id="ctrl.nand_b", iface="nand",
                            default="glue.nands"),
                     Demand(id="ctrl.nand_c", iface="nand",
                            default="glue.nands"),
                     Demand(id="ctrl.nand_d", iface="nand",
                            default="aux_nand"),
                 ]),
            Role(id="glue", kind="quad_nand",
                 demands=[_pwr("glue.pwr", 3.3)]),
            Role(id="aux_nand", kind="logic_gate",
                 capabilities=[Capability(iface="nand")],
                 demands=[_pwr("aux_nand.pwr", 3.3)]),
        ],
        rails=[Rail(name="+3V3", volts=3.3)],
        grounds=[Ground(name="GND")],
        pools=[Pool(name="glue.nands", role="glue", provides="nand",
                    unit_count=4,
                    port_signature=["a:in", "b:in", "y:out"])],
        allocation=_rec(entries=[
            AllocationEntry(pool="glue.nands", unit=0, demand="ctrl.nand_a",
                            chosen_by="solver", state="sticky"),
            AllocationEntry(pool="glue.nands", unit=1, demand="ctrl.nand_b",
                            chosen_by="solver", state="sticky"),
            AllocationEntry(pool="glue.nands", unit=2, demand="ctrl.nand_c",
                            chosen_by="solver", state="sticky"),
            # unit 3: SPARE — no entry; visible via Pool.unit_count.
        ]),
    )


def _l2_9() -> CanonicalGraph:
    """#9 bound: ctrl->U1 (controller MCU), glue->U2 (7400-class quad NAND,
    one part = the whole pool), aux_nand->U3 (single-gate NAND). Gates 1-3 of
    U2 serve nand_a/b/c per the allocation record; gate 4 is the SPARE
    (inputs tied to GND per practice, output on an explicit open net — never
    silently dropped). U3 serves nand_d (the declared default)."""
    src = _source("PS1", 3.3)

    mcu_terms = [_T("VDD", "power_in", req_v=3.3), _T("GND", "ground")]
    for i in range(1, 9):                       # D1..D8 command outputs
        mcu_terms.append(_T(f"D{i}", "logic_out"))
    for i in range(1, 5):                       # S1..S4 gated senses
        mcu_terms.append(_T(f"S{i}", "logic_in",
                            iface="nand", iface_member="require"))
    ctrl = Component(refdes="U1", kind="mcu", value="controller",
                     authored=True, terminals=mcu_terms)

    quad_terms = [_T("VDD", "power_in", req_v=3.3), _T("GND", "ground")]
    for u in range(1, 5):                       # gates 1..4 (pool units 0..3)
        quad_terms.append(_T(f"A{u}", "logic_in"))
        quad_terms.append(_T(f"B{u}", "logic_in"))
        quad_terms.append(_T(f"Y{u}", "logic_out",
                             iface="nand", iface_member="provide"))
    quad = Component(refdes="U2", kind="logic_gate", value="74HC00",
                     authored=True, logic_fn="nand", terminals=quad_terms,
                     attrs={"units": 4})

    aux = Component(
        refdes="U3", kind="logic_gate", value="1G00", authored=True,
        logic_fn="nand",
        terminals=[
            _T("VDD", "power_in", req_v=3.3),
            _T("GND", "ground"),
            _T("A", "logic_in"), _T("B", "logic_in"),
            _T("Y", "logic_out", iface="nand", iface_member="provide"),
        ],
    )

    c1 = _decoupling_cap("C1", "U1")
    c2 = _decoupling_cap("C2", "U2")
    c3 = _decoupling_cap("C3", "U3")

    nets = [
        Net("+3V3", "power", 3.3, [
            ("PS1", "VOUT"), ("U1", "VDD"), ("U2", "VDD"), ("U3", "VDD"),
            ("C1", "+"), ("C2", "+"), ("C3", "+"),
        ]),
        # spare gate 4 inputs TIED to ground per practice (never floating).
        Net("GND", "ground", 0.0, [
            ("PS1", "GND"), ("U1", "GND"), ("U2", "GND"), ("U3", "GND"),
            ("C1", "-"), ("C2", "-"), ("C3", "-"),
            ("U2", "A4"), ("U2", "B4"),
        ], ground_kind="ground", ground_role="none"),
    ]
    # gates 1..3 = pool units 0..2 serving nand_a/b/c
    for u in (1, 2, 3):
        d = 2 * u - 1
        nets.append(Net(f"NAND{u}_A", "signal", None,
                        [("U1", f"D{d}"), ("U2", f"A{u}")]))
        nets.append(Net(f"NAND{u}_B", "signal", None,
                        [("U1", f"D{d + 1}"), ("U2", f"B{u}")]))
        nets.append(Net(f"NAND{u}_Y", "signal", None,
                        [("U2", f"Y{u}"), ("U1", f"S{u}")]))
    # nand_d served by the discrete default provider U3
    nets.append(Net("AUX_A", "signal", None, [("U1", "D7"), ("U3", "A")]))
    nets.append(Net("AUX_B", "signal", None, [("U1", "D8"), ("U3", "B")]))
    nets.append(Net("AUX_Y", "signal", None, [("U3", "Y"), ("U1", "S4")]))
    # SPARE gate output: explicit open net — visible, never deleted.
    nets.append(Net("NAND4_Y_spare", "open", None, [("U2", "Y4")]))

    return CanonicalGraph(components=[src, ctrl, quad, aux, c1, c2, c3],
                          nets=nets)


# ===========================================================================
# Assemble GOLDENS_V3 (ordered by corpus id; keys are STRINGS: "5a"/"5b")
# ===========================================================================

def _entry(l1: IntentDoc, l2, exp_l1: str, exp_l2, note: str) -> Dict:
    return {
        "l1": l1,
        "l2": l2,
        "record": l1.allocation,      # same object as l1.allocation
        "expected": {"l1": exp_l1, "l2": exp_l2},
        "note": note,
    }


# F2 provenance maps: refdes -> L1 role (authored parts) and refdes -> L1
# demand/role (generated companions), per golden. EMIT_CONTRACT Part A.
def _l2_8_prov() -> CanonicalGraph:
    roles = {"PS1": "hv_supply", "PS2": "gate_supply",
             "PS3": "logic_supply", "U1": "mcu"}
    companions = {"C1": "mcu.pwr"}
    for b in (1, 2, 3):
        for ref in (f"G{b}A", f"G{b}B", f"G{b}H", f"G{b}L",
                    f"DRV{b}", f"Q{b}H", f"Q{b}L"):
            roles[ref] = f"bridge{b}"
        companions[f"Cb{b}"] = f"bridge{b}.gate_pwr"
        companions[f"Db{b}"] = f"bridge{b}.gate_pwr"
    return _prov(_l2_8(), roles, companions)


GOLDENS_V3: "OrderedDict[str, Dict]" = OrderedDict()
GOLDENS_V3["1"] = _entry(
    _l1_1(),
    _prov(_golden_1(),
          {"PS1": "vbus_supply", "U1": "reg", "U2": "mcu",
           "U3": "sensor_a", "U4": "sensor_b"},
          {"C1": "mcu.pwr", "C2": "sensor_a.pwr", "C3": "sensor_b.pwr",
           "R1": "mcu", "R2": "mcu"}),
    "PASS", "PASS",
    "VBUS(5V)+demanded +3V3, regulator bridges; I2C MCU + sensors 0x48/0x49; "
    "L2 = v2 golden 1 (decoupling + one shared pull-up pair generated).")
GOLDENS_V3["2"] = _entry(
    _l1_2(),
    _prov(_golden_2(),
          {"PS1": "vin_supply", "U1": "reg_a", "U2": "reg_b",
           "U3": "load_a", "U4": "load_b"},
          {"C1": "load_a.pwr", "C2": "load_b.pwr"}),
    "PASS", "PASS",
    "Power tree: rail-tree consistency at L1; L2 = v2 golden 2.")
GOLDENS_V3["3"] = _entry(
    _l1_3(), None, "FAIL:ADDR_COLLISION", None,
    "Two sensor roles pinned to 0x48 on bus I2C0 — an intent-level static "
    "error; the document never reaches binding.")
GOLDENS_V3["4"] = _entry(
    _l1_4(),
    _prov(_golden_4(),
          {"PS1": "supply", "U1": "mcu", "Y1": "xtal"},
          {"C1": "mcu.osc", "C2": "mcu.osc", "C3": "mcu.pwr"}),
    "PASS", "PASS",
    "MCU demands an oscillator; crystal role satisfies; load-cap companions "
    "generated at L2 (v2 golden 4).")
GOLDENS_V3["5a"] = _entry(
    _l1_5a(),
    _prov(_l2_5a(),
          {"PS1": "supply", "U1": "mcu", "U2": "periph"}, {}),
    "PASS", "PASS",
    "3-UART pool: rung-3 ALLOCATION (not escalation) — deterministic solver "
    "assigns unit 0, record present, re-solve byte-stable, pin promotable.")
GOLDENS_V3["5b"] = _entry(
    _l1_5b(), None, "ESCALATE", None,
    "hw UART vs bit-banged GPIO: NON-equivalent alternatives, no policy — "
    "rung-4 ESCALATE with UNSAT-core-style conflict + minimal relaxation.")
GOLDENS_V3["6"] = _entry(
    _l1_6(),
    _prov(_golden_6(),
          {"PS1": "supply", "U1": "mcu", "U2": "mod1", "U3": "mod2"},
          {"C1": "mod1.pwr", "C2": "mod2.pwr", "R1": "mcu", "R2": "mcu"}),
    "PASS", "PASS",
    "sensor-with-decoupling module role x2 drawing its rail from scope "
    "(closure + cascade); L2 = v2 golden 6.")
GOLDENS_V3["7"] = _entry(
    _l1_7(), None, "FAIL:VOLTAGE_MISMATCH", None,
    "5V-only device in a 3.3V rail scope — a rail EXISTS (not UNMET_DEMAND) "
    "but is volts-incompatible; caught at L1.")
GOLDENS_V3["8"] = _entry(
    _l1_8(), _l2_8_prov(), "PASS", "PASS",
    "3 half-bridges, 6-channel PWM pool (channel->bridge is an allocation), "
    "per-bridge mutual_exclusion decls, PGND role=power bonded at star1 "
    "(role soft: presence only); L2 = v2 hardened 2-input interlock.")
GOLDENS_V3["9"] = _entry(
    _l1_9(),
    _prov(_l2_9(),
          {"PS1": "supply", "U1": "ctrl", "U2": "glue", "U3": "aux_nand"},
          {"C1": "ctrl.pwr", "C2": "glue.pwr", "C3": "aux_nand.pwr"}),
    "PASS", "PASS",
    "Quad-NAND pool: four NAND demands, 4-unit pool; three allocated "
    "(legal bijection 0/1/2), the fourth satisfied by its DECLARED default "
    "provider; unit 3 is the visible SPARE (free, never deleted; inputs "
    "tied at L2).")


# ===========================================================================
# KNOWN_BAD_V3 — corrupted L1 / allocation-record documents (Gen4 4.3's
# intent-level mutations, hand-built as stable anchors like v2 KNOWN_BAD).
# ===========================================================================

def _kb_pool_overflow() -> IntentDoc:
    """#9 with 5 demands: five NAND demands against the 4-unit pool (the pool
    is the unique provider — no ambiguity, just insufficiency). Pool
    sufficiency (sum qty <= unit_count) must fail: 5 > 4."""
    doc = _l1_9()
    # make the pool the unique satisfier: drop the aux provider + defaults
    doc.roles = [r for r in doc.roles if r.id != "aux_nand"]
    ctrl = doc.role("ctrl")
    ctrl.demands = [_pwr("ctrl.pwr", 3.3)] + [
        Demand(id=f"ctrl.nand_{s}", iface="nand")
        for s in ("a", "b", "c", "d", "e")            # five demands
    ]
    # solver managed units 0..3; the fifth demand is left unserved.
    doc.allocation = _rec(entries=[
        AllocationEntry(pool="glue.nands", unit=i,
                        demand=f"ctrl.nand_{s}",
                        chosen_by="solver", state="sticky")
        for i, s in enumerate(("a", "b", "c", "d"))
    ])
    return doc


def _kb_alloc_bijection() -> IntentDoc:
    """#9 with a corrupted record: unit 0 double-booked (serves nand_a AND
    nand_b) while nand_c's entry is intact — demand->unit is no longer a
    legal bijection per pool. (An out-of-range unit index or a pool/demand
    iface mismatch is the same ALLOC_BIJECTION class.)"""
    doc = _l1_9()
    doc.allocation = _rec(entries=[
        AllocationEntry(pool="glue.nands", unit=0, demand="ctrl.nand_a",
                        chosen_by="solver", state="sticky"),
        AllocationEntry(pool="glue.nands", unit=0, demand="ctrl.nand_b",
                        chosen_by="solver", state="sticky"),      # !!
        AllocationEntry(pool="glue.nands", unit=1, demand="ctrl.nand_c",
                        chosen_by="solver", state="sticky"),
    ])
    return doc


def _kb_lock_violation() -> IntentDoc:
    """#5a after firmware-freeze fired (version 1, snapshot frozen, entry
    pinned + stamped), then the covered decision was EDITED (unit 0 -> 2)
    WITHOUT a series bump (series still "A" == attrs["lock_series"]).
    Mechanical per-group diff of covered entries vs snapshot must flag it.
    A legal edit would break_lock and fork series "B"."""
    doc = _l1_5a()
    locked_entry_json = {
        "pool": "mcu.uarts", "unit": 0, "demand": "periph.uart",
        "chosen_by": "solver", "state": "pinned",
        "locked_by": "firmware-facing@1",
    }
    doc.allocation = _rec(
        entries=[AllocationEntry(pool="mcu.uarts", unit=2,        # !!
                                 demand="periph.uart",
                                 chosen_by="author", state="pinned",
                                 locked_by="firmware-facing@1")],
        lock_groups=[LockGroup(name="firmware-facing",
                               covers=["pool_allocation", "pin_map"],
                               version=1,
                               snapshot=[locked_entry_json],
                               owner="fw-team",
                               sync_point="firmware-freeze")],
    )
    doc.series = "A"                                   # NO series bump
    doc.attrs["lock_series"] = {"firmware-facing": "A"}
    return doc


def _kb_unmet_demand() -> IntentDoc:
    """#5a with the provider POOL deleted (Gen4 4.3: 'delete a provider role
    -> L1 unsatisfiable'): periph.uart has no in-scope satisfier and no
    declared default — resolution-ladder rung 2, a load error."""
    doc = _l1_5a()
    doc.pools = []
    doc.allocation = _rec()          # nothing to allocate from
    return doc


KNOWN_BAD_V3: List[Dict] = [
    {
        "l1": _kb_pool_overflow(),
        "record": None,
        "expected_code": "POOL_INSUFFICIENT",
        "layer": "l1",
        "note": "#9 with 5 NAND demands against the 4-unit pool "
                "(pool sufficiency: 5 > 4).",
    },
    {
        "l1": _kb_alloc_bijection(),
        "record": None,
        "expected_code": "ALLOC_BIJECTION",
        "layer": "record",
        "note": "#9 record corrupted: pool unit 0 double-booked "
                "(serves ctrl.nand_a AND ctrl.nand_b).",
    },
    {
        "l1": _kb_lock_violation(),
        "record": None,
        "expected_code": "LOCK_VIOLATION",
        "layer": "record",
        "note": "#5a locked at firmware-facing@1 (snapshot: unit 0); covered "
                "entry changed to unit 2 with NO series bump.",
    },
    {
        "l1": _kb_unmet_demand(),
        "record": None,
        "expected_code": "DEMAND_UNSATISFIABLE",
        "layer": "l1",
        "note": "#5a with the UART pool deleted: periph.uart has no "
                "satisfier and no declared default (rung 2).",
    },
]
for _kb in KNOWN_BAD_V3:
    _kb["record"] = _kb["l1"].allocation      # same-object convention
del _kb


__all__ = ["GOLDENS_V3", "KNOWN_BAD_V3", "spare_units"]


# ===========================================================================
# self-check:  python goldens_v3.py
#   Structural sanity of the corpus itself. Layer-2 graphs are pushed through
#   the REAL v2 oracle stack (verdict must match expected["l2"]); layer-1
#   docs are checked for canonical round-trip + the structural facts each
#   golden/known-bad exists to exhibit. (The layer-1 oracle proper is a
#   sibling v3 module; this self-check must not depend on it.)
# ===========================================================================

if __name__ == "__main__":  # pragma: no cover
    import json
    import sys

    from schema_l1 import from_json as l1_from_json
    from schema_l1 import to_json as l1_to_json
    from score import verdict_for

    _n = [0]
    _fails: List[str] = []

    def check(ok: bool, label: str) -> None:
        _n[0] += 1
        print("  [%s] %s" % ("ok  " if ok else "FAIL", label))
        if not ok:
            _fails.append(label)

    def canon(doc: IntentDoc) -> str:
        return json.dumps(l1_to_json(doc), sort_keys=True)

    print("goldens_v3 self-check")
    print("-" * 60)

    check(list(GOLDENS_V3.keys()) ==
          ["1", "2", "3", "4", "5a", "5b", "6", "7", "8", "9"],
          "corpus covers intents 1,2,3,4,5a,5b,6,7,8,9 in order")

    for iid, e in GOLDENS_V3.items():
        check(e["record"] is e["l1"].allocation,
              "#%s record is the l1 doc's allocation object" % iid)
        check((e["l2"] is None) == (e["expected"]["l2"] is None),
              "#%s l2 presence matches expected" % iid)
        s1 = canon(e["l1"])
        check(canon(l1_from_json(json.loads(json.dumps(l1_to_json(e["l1"]))))) == s1,
              "#%s l1 doc round-trips canonically" % iid)

    check([i for i, e in GOLDENS_V3.items() if e["l2"] is None] ==
          ["3", "5b", "7"],
          "exactly #3, #5b, #7 never reach binding")

    # layer-2 verdicts through the REAL v2 oracle stack
    for iid, e in GOLDENS_V3.items():
        if e["l2"] is None:
            continue
        v = verdict_for(e["l2"], e["l2"])
        check(v == e["expected"]["l2"],
              "#%s l2 verdict == %s (got %s)" % (iid, e["expected"]["l2"], v))

    # deterministic canonical emit: scrambling entry order changes nothing
    for iid in ("5a", "8", "9"):
        doc = GOLDENS_V3[iid]["l1"]
        s1 = canon(doc)
        doc2 = l1_from_json(l1_to_json(doc))
        doc2.allocation.entries.reverse()
        check(canon(doc2) == s1,
              "#%s allocation record emit is order-independent (byte-stable)"
              % iid)

    # ---- per-intent structural facts --------------------------------------
    d3 = GOLDENS_V3["3"]["l1"]
    addrs = [(d.bus, d.attrs.get("i2c_addr"))
             for r in d3.roles for d in r.demands if d.iface == "i2c"]
    check(len(addrs) == 3 and addrs.count(("I2C0", 0x48)) == 2,
          "#3 two of three sensors collide at (I2C0, 0x48)")

    d7 = GOLDENS_V3["7"]["l1"]
    check([r.volts for r in d7.rails] == [3.3]
          and d7.demand("dev.pwr").volts == 5.0,
          "#7 only rail is 3.3V; device demands 5.0V")

    d5a = GOLDENS_V3["5a"]["l1"]
    e0 = d5a.allocation.entry_for_demand("periph.uart")
    check(e0 is not None and (e0.pool, e0.unit, e0.chosen_by, e0.state) ==
          ("mcu.uarts", 0, "solver", "sticky"),
          "#5a solver allocated unit 0 (sticky, provenance recorded)")
    check(spare_units(d5a.pool("mcu.uarts"), d5a.allocation) == [1, 2],
          "#5a units 1-2 remain visible spare capacity")
    check(d5a.allocation.lock_groups[0].version == 0
          and d5a.allocation.lock_groups[0].snapshot is None,
          "#5a lock group declared up front, never locked")

    d5b = GOLDENS_V3["5b"]["l1"]
    esc = d5b.escalations[0]
    check(len(d5b.allocation.entries) == 0 and len(d5b.escalations) == 1
          and len(esc.conflict) >= 2 and esc.relaxation != "",
          "#5b escalates with UNSAT-core conflict + relaxation, no entry")

    d8 = GOLDENS_V3["8"]["l1"]
    pool8 = d8.pool("mcu.pwm")
    ents8 = d8.allocation.entries_for_pool("mcu.pwm")
    check(pool8.unit_count == 6 and len(ents8) == 6
          and len({x.unit for x in ents8}) == 6
          and len({x.demand for x in ents8}) == 6
          and all(d8.demand(x.demand) is not None
                  and d8.demand(x.demand).iface == pool8.provides
                  and 0 <= x.unit < pool8.unit_count for x in ents8),
          "#8 6-channel PWM pool fully allocated as a legal bijection")
    check(d8.ground("PGND") is not None
          and d8.ground("PGND").role == "power"
          and sorted(d8.bonds[0].joins) == ["GND", "PGND"],
          "#8 gate-drive ground role=power with a declared bond (presence)")
    check(len(d8.invariants) == 3 and all(
        i.kind == "mutual_exclusion" and len(i.subjects) == 2
        and all(d8.role(ref.split(".")[0]) is not None
                for ref in i.subjects + i.inputs)
        for i in d8.invariants),
          "#8 per-bridge mutual_exclusion decls are well-formed")
    g8 = GOLDENS_V3["8"]["l2"]
    gnets = [n for n in g8.nets if n.kind == "ground"]
    check(len(gnets) == 2 and all(n.bond == "star1" for n in gnets)
          and any(n.ground_role == "power" for n in gnets)
          and len(g8.invariants) == 3,
          "#8 l2 split GND/PGND joined by bond star1; 3 invariants intact")

    d9 = GOLDENS_V3["9"]["l1"]
    nand_demands = [d for r in d9.roles for d in r.demands
                    if d.iface == "nand"]
    check(len(nand_demands) == 4 and all(d.default for d in nand_demands),
          "#9 four NAND demands, every one carrying a declared policy")
    check(d9.demand("ctrl.nand_d").default == "aux_nand"
          and d9.role("aux_nand") is not None,
          "#9 fourth demand defaults to the discrete provider role")
    ents9 = d9.allocation.entries_for_pool("glue.nands")
    check(len(ents9) == 3 and {x.unit for x in ents9} == {0, 1, 2}
          and len({x.demand for x in ents9}) == 3,
          "#9 legal bijection: units 0/1/2 serve nand_a/b/c")
    check(spare_units(d9.pool("glue.nands"), d9.allocation) == [3],
          "#9 unit 3 is the visible spare (free, never deleted)")
    g9 = GOLDENS_V3["9"]["l2"]
    gnd9 = next(n for n in g9.nets if n.kind == "ground")
    open9 = [n for n in g9.nets if n.kind == "open"]
    check(("U2", "A4") in [tuple(x) for x in gnd9.nodes]
          and ("U2", "B4") in [tuple(x) for x in gnd9.nodes]
          and len(open9) == 1
          and [tuple(x) for x in open9[0].nodes] == [("U2", "Y4")],
          "#9 l2 spare gate inputs tied, output on an explicit open net")

    # ---- known-bad structural facts ----------------------------------------
    check([kb["expected_code"] for kb in KNOWN_BAD_V3] ==
          ["POOL_INSUFFICIENT", "ALLOC_BIJECTION", "LOCK_VIOLATION",
           "DEMAND_UNSATISFIABLE"],
          "KNOWN_BAD_V3 covers the four v3 corruption classes")

    kb_of = _kb_pool_overflow()
    pool = kb_of.pool("glue.nands")
    qty = sum(d.qty for r in kb_of.roles for d in r.demands
              if d.iface == pool.provides)
    check(qty == 5 and pool.unit_count == 4,
          "pool-overflow doc demands 5 NANDs of a 4-unit pool")

    kb_bij = _kb_alloc_bijection()
    pairs = [(x.pool, x.unit) for x in kb_bij.allocation.entries]
    check(len(pairs) != len(set(pairs)),
          "bijection doc double-books a (pool, unit)")

    kb_lock = _kb_lock_violation()
    grp = kb_lock.allocation.lock_groups[0]
    cur = [x for x in kb_lock.allocation.entries]
    check(grp.version == 1 and grp.snapshot is not None
          and grp.snapshot[0]["unit"] == 0 and cur[0].unit == 2
          and kb_lock.series ==
          kb_lock.attrs["lock_series"]["firmware-facing"],
          "lock doc: covered decision changed, series NOT bumped")

    kb_unmet = _kb_unmet_demand()
    d = kb_unmet.demand("periph.uart")
    providers = ([p for p in kb_unmet.pools if p.provides == d.iface] +
                 [c for r in kb_unmet.roles for c in r.capabilities
                  if c.iface == d.iface])
    check(d is not None and d.default is None and not providers,
          "unmet doc: no satisfier anywhere, no declared default")

    for kb in KNOWN_BAD_V3:
        check(kb["record"] is kb["l1"].allocation
              and canon(l1_from_json(l1_to_json(kb["l1"]))) == canon(kb["l1"]),
              "known-bad %s round-trips" % kb["expected_code"])

    print("-" * 60)
    if _fails:
        print("RESULT: FAIL  (%d of %d checks failed)" %
              (len(_fails), _n[0]))
        sys.exit(1)
    print("RESULT: PASS  (all %d checks passed)" % _n[0])
    sys.exit(0)
