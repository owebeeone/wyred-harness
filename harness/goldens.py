"""Golden circuits for the connectivity-oracle harness.

The 8 intents from spec section 3.1, each built as a CanonicalGraph from the
intent description + standard EE knowledge ONLY (nothing from any candidate
package is imported or referenced here).

GOLDENS   : ordered mapping intent-id -> {"graph", "expected"} where expected is
            the top-level verdict the oracle stack should return for that graph.
KNOWN_BAD : list of {"graph", "expected_code", "note"} corrupted variants used by
            the self-test to prove the ERC / invariant layers actually fire.

Verdict vocabulary (top-level, produced by the oracle stack, NOT this file):
    "PASS"                    clean, no violations, no escalations
    "FAIL:<CODE>"             an ERC / invariant violation of the named code
    "ESCALATE"                ambiguity the candidate must surface, not resolve

Conventions used consistently across all goldens so the downstream ERC layer can
read intent off the graph WITHOUT parsing any function string (see
EMIT_CONTRACT.md). All electrical meaning lives in STRUCTURED Terminal fields:

  * A power_in pin encodes the voltage it REQUIRES in ``req_v`` (float volts).
    A power_out pin that PROVIDES a rail encodes ``prov_v`` (float volts).
  * An interface requirement is a terminal with ``iface=<name>`` and
    ``iface_member="require"``; the matching provider has ``iface=<name>`` and
    ``iface_member="provide"``  (e.g. iface="oscillator"/"uart").
  * I2C data pins carry ``iface="i2c"`` with ``iface_member="sda"/"scl"``; a
    device's address lives in ``attrs["i2c_addr"]`` (int). ADDR_COLLISION is
    keyed on equal i2c_addr on a shared SDA net.
  * Decoupling / pull-up / bootstrap parts are authored=False (generated
    companions).

The ``function`` field is retained only as a human label; NOTHING keys off it.

Pure Python 3 stdlib. Plain imports only.
"""

from __future__ import annotations

from collections import OrderedDict
from typing import Dict, List

from schema import (
    CanonicalGraph,
    Component,
    Invariant,
    Net,
    Terminal,
)


# ===========================================================================
# small builder helpers (keep the circuit definitions readable)
# ===========================================================================

def _T(name, role, function="", req_v=None, prov_v=None,
       iface=None, iface_member=None):
    return Terminal(
        name=name, role=role, function=function,
        req_v=req_v, prov_v=prov_v, iface=iface, iface_member=iface_member,
    )


def _decoupling_cap(refdes, vdd_node_refdes, value="100nF"):
    """A generated 100nF decoupling cap. Returns (Component,). The caller wires
    it: '+' to the device VDD net, '-' to ground."""
    return Component(
        refdes=refdes,
        kind="capacitor",
        value=value,
        authored=False,
        terminals=[_T("+", "passive"), _T("-", "passive")],
        attrs={"role": "decoupling", "for": vdd_node_refdes},
    )


def _source(refdes, volts, value=None):
    return Component(
        refdes=refdes,
        kind="source",
        value=value or f"{volts}V",
        authored=True,
        terminals=[
            _T("VOUT", "power_out", f"vout:{volts}", prov_v=float(volts)),
            _T("GND", "ground"),
        ],
        attrs={"voltage": float(volts)},
    )


def _ldo(refdes, vin, vout, value=None):
    return Component(
        refdes=refdes,
        kind="ldo",
        value=value or f"{vout}V LDO",
        authored=True,
        terminals=[
            _T("VIN", "power_in", f"vin:{vin}", req_v=float(vin)),
            _T("VOUT", "power_out", f"vout:{vout}", prov_v=float(vout)),
            _T("GND", "ground"),
        ],
        attrs={"vin": float(vin), "vout": float(vout)},
    )


def _i2c_sensor(refdes, addr, value="sensor"):
    return Component(
        refdes=refdes,
        kind="sensor",
        value=value,
        authored=True,
        terminals=[
            _T("VDD", "power_in", "vin:3.3", req_v=3.3),
            _T("GND", "ground"),
            _T("SDA", "signal", "i2c_sda", iface="i2c", iface_member="sda"),
            _T("SCL", "signal", "i2c_scl", iface="i2c", iface_member="scl"),
        ],
        attrs={"i2c_addr": addr},
    )


def _pullup(refdes, net_label):
    """A generated I2C pull-up resistor: one leg to the bus signal, other to 3V3."""
    return Component(
        refdes=refdes,
        kind="resistor",
        value="4.7k",
        authored=False,
        terminals=[_T("1", "passive"), _T("2", "passive")],
        attrs={"role": "i2c_pullup", "for": net_label},
    )


# ===========================================================================
# Intent 1 — sensor_node   -> PASS
#   5V source -> 3.3V LDO -> MCU (I2C master) + 2 I2C sensors (0x48,0x49)
#   each device a decoupling cap; ONE shared I2C pull-up pair on the bus.
# ===========================================================================

def _golden_1() -> CanonicalGraph:
    src = _source("PS1", 5.0)
    ldo = _ldo("U1", 5.0, 3.3)

    mcu = Component(
        refdes="U2", kind="mcu", value="MCU", authored=True,
        terminals=[
            _T("VDD", "power_in", "vin:3.3", req_v=3.3),
            _T("GND", "ground"),
            _T("SDA", "signal", "i2c_sda", iface="i2c", iface_member="sda"),
            _T("SCL", "signal", "i2c_scl", iface="i2c", iface_member="scl"),
        ],
        attrs={"i2c_master": True},
    )
    s1 = _i2c_sensor("U3", 0x48)
    s2 = _i2c_sensor("U4", 0x49)

    # one decoupling cap per powered device (MCU + 2 sensors)
    c_mcu = _decoupling_cap("C1", "U2")
    c_s1 = _decoupling_cap("C2", "U3")
    c_s2 = _decoupling_cap("C3", "U4")

    # ONE shared pull-up pair on the bus (SDA + SCL)
    r_sda = _pullup("R1", "I2C_SDA")
    r_scl = _pullup("R2", "I2C_SCL")

    comps = [src, ldo, mcu, s1, s2, c_mcu, c_s1, c_s2, r_sda, r_scl]

    nets = [
        Net("V5", "power", 5.0, [("PS1", "VOUT"), ("U1", "VIN")]),
        Net("V3V3", "power", 3.3, [
            ("U1", "VOUT"),
            ("U2", "VDD"), ("U3", "VDD"), ("U4", "VDD"),
            ("C1", "+"), ("C2", "+"), ("C3", "+"),
            ("R1", "2"), ("R2", "2"),        # pull-ups reference 3V3
        ]),
        Net("GND", "ground", 0.0, [
            ("PS1", "GND"), ("U1", "GND"),
            ("U2", "GND"), ("U3", "GND"), ("U4", "GND"),
            ("C1", "-"), ("C2", "-"), ("C3", "-"),
        ]),
        Net("I2C_SDA", "signal", None, [
            ("U2", "SDA"), ("U3", "SDA"), ("U4", "SDA"), ("R1", "1"),
        ]),
        Net("I2C_SCL", "signal", None, [
            ("U2", "SCL"), ("U3", "SCL"), ("U4", "SCL"), ("R2", "1"),
        ]),
    ]
    return CanonicalGraph(components=comps, nets=nets)


# ===========================================================================
# Intent 2 — power tree   -> PASS
#   source -> LDO chain -> 2 rails, each load decoupled; grounds unified.
#   5V source -> 3.3V LDO ; 3.3V -> 1.8V LDO. Load A on 3.3V, load B on 1.8V.
# ===========================================================================

def _golden_2() -> CanonicalGraph:
    src = _source("PS1", 5.0)
    ldo_a = _ldo("U1", 5.0, 3.3)
    ldo_b = _ldo("U2", 3.3, 1.8)   # chained off the 3.3V rail

    load_a = Component(
        refdes="U3", kind="mcu", value="load3v3", authored=True,
        terminals=[_T("VDD", "power_in", "vin:3.3", req_v=3.3),
                   _T("GND", "ground")],
    )
    load_b = Component(
        refdes="U4", kind="sensor", value="load1v8", authored=True,
        terminals=[_T("VDD", "power_in", "vin:1.8", req_v=1.8),
                   _T("GND", "ground")],
    )

    c_a = _decoupling_cap("C1", "U3")
    c_b = _decoupling_cap("C2", "U4")

    comps = [src, ldo_a, ldo_b, load_a, load_b, c_a, c_b]
    nets = [
        Net("V5", "power", 5.0, [("PS1", "VOUT"), ("U1", "VIN")]),
        Net("V3V3", "power", 3.3, [
            ("U1", "VOUT"), ("U2", "VIN"), ("U3", "VDD"), ("C1", "+"),
        ]),
        Net("V1V8", "power", 1.8, [("U2", "VOUT"), ("U4", "VDD"), ("C2", "+")]),
        Net("GND", "ground", 0.0, [
            ("PS1", "GND"), ("U1", "GND"), ("U2", "GND"),
            ("U3", "GND"), ("U4", "GND"), ("C1", "-"), ("C2", "-"),
        ]),
    ]
    return CanonicalGraph(components=comps, nets=nets)


# ===========================================================================
# Intent 3 — I2C bus, 3 sensors, two sharing 0x48   -> FAIL(ADDR_COLLISION)
# ===========================================================================

def _golden_3() -> CanonicalGraph:
    src = _source("PS1", 3.3)  # already-3.3V rail source, keeps it simple

    mcu = Component(
        refdes="U1", kind="mcu", value="MCU", authored=True,
        terminals=[
            _T("VDD", "power_in", "vin:3.3", req_v=3.3),
            _T("GND", "ground"),
            _T("SDA", "signal", "i2c_sda", iface="i2c", iface_member="sda"),
            _T("SCL", "signal", "i2c_scl", iface="i2c", iface_member="scl"),
        ],
        attrs={"i2c_master": True},
    )
    s1 = _i2c_sensor("U2", 0x48)
    s2 = _i2c_sensor("U3", 0x48)   # <-- collision with U2
    s3 = _i2c_sensor("U4", 0x4A)

    c1 = _decoupling_cap("C1", "U2")
    c2 = _decoupling_cap("C2", "U3")
    c3 = _decoupling_cap("C3", "U4")
    r_sda = _pullup("R1", "I2C_SDA")
    r_scl = _pullup("R2", "I2C_SCL")

    comps = [src, mcu, s1, s2, s3, c1, c2, c3, r_sda, r_scl]
    nets = [
        Net("V3V3", "power", 3.3, [
            ("PS1", "VOUT"),
            ("U1", "VDD"), ("U2", "VDD"), ("U3", "VDD"), ("U4", "VDD"),
            ("C1", "+"), ("C2", "+"), ("C3", "+"),
            ("R1", "2"), ("R2", "2"),
        ]),
        Net("GND", "ground", 0.0, [
            ("PS1", "GND"),
            ("U1", "GND"), ("U2", "GND"), ("U3", "GND"), ("U4", "GND"),
            ("C1", "-"), ("C2", "-"), ("C3", "-"),
        ]),
        Net("I2C_SDA", "signal", None, [
            ("U1", "SDA"), ("U2", "SDA"), ("U3", "SDA"), ("U4", "SDA"),
            ("R1", "1"),
        ]),
        Net("I2C_SCL", "signal", None, [
            ("U1", "SCL"), ("U2", "SCL"), ("U3", "SCL"), ("U4", "SCL"),
            ("R2", "1"),
        ]),
    ]
    return CanonicalGraph(components=comps, nets=nets)


# ===========================================================================
# Intent 4 — crystal + MCU   -> PASS
#   MCU needs an oscillator; crystal pulls its 2 load caps.
# ===========================================================================

def _golden_4() -> CanonicalGraph:
    src = _source("PS1", 3.3)

    mcu = Component(
        refdes="U1", kind="mcu", value="MCU", authored=True,
        terminals=[
            _T("VDD", "power_in", "vin:3.3", req_v=3.3),
            _T("GND", "ground"),
            _T("XIN", "signal", "require:oscillator",
               iface="oscillator", iface_member="require"),
            _T("XOUT", "signal", "require:oscillator",
               iface="oscillator", iface_member="require"),
        ],
    )
    # crystal PROVIDES an oscillator across its two pins
    xtal = Component(
        refdes="Y1", kind="crystal", value="8MHz", authored=True,
        terminals=[
            _T("1", "signal", "provide:oscillator",
               iface="oscillator", iface_member="provide"),
            _T("2", "signal", "provide:oscillator",
               iface="oscillator", iface_member="provide"),
        ],
        attrs={"freq": "8MHz"},
    )
    # crystal pulls its 2 load caps (companions)
    cl1 = Component(
        refdes="C1", kind="capacitor", value="18pF", authored=False,
        terminals=[_T("+", "passive"), _T("-", "passive")],
        attrs={"role": "xtal_load", "for": "Y1"},
    )
    cl2 = Component(
        refdes="C2", kind="capacitor", value="18pF", authored=False,
        terminals=[_T("+", "passive"), _T("-", "passive")],
        attrs={"role": "xtal_load", "for": "Y1"},
    )
    c_mcu = _decoupling_cap("C3", "U1")

    comps = [src, mcu, xtal, cl1, cl2, c_mcu]
    nets = [
        Net("V3V3", "power", 3.3, [
            ("PS1", "VOUT"), ("U1", "VDD"), ("C3", "+"),
        ]),
        Net("GND", "ground", 0.0, [
            ("PS1", "GND"), ("U1", "GND"),
            ("C1", "-"), ("C2", "-"), ("C3", "-"),
        ]),
        # XIN side: MCU XIN + crystal pin1 + load cap C1 top
        Net("OSC_XIN", "signal", None, [
            ("U1", "XIN"), ("Y1", "1"), ("C1", "+"),
        ]),
        # XOUT side: MCU XOUT + crystal pin2 + load cap C2 top
        Net("OSC_XOUT", "signal", None, [
            ("U1", "XOUT"), ("Y1", "2"), ("C2", "+"),
        ]),
    ]
    return CanonicalGraph(components=comps, nets=nets)


# ===========================================================================
# Intent 5 — MCU with 3 UARTs + one device needing a UART   -> ESCALATE
#   Ambiguous: three candidate UART ports, one consumer; must surface, not pick.
# ===========================================================================

def _golden_5() -> CanonicalGraph:
    src = _source("PS1", 3.3)

    mcu = Component(
        refdes="U1", kind="mcu", value="MCU-3UART", authored=True,
        terminals=[
            _T("VDD", "power_in", "vin:3.3", req_v=3.3),
            _T("GND", "ground"),
            # three provide:uart ports, none wired to the device
            _T("U0_TX", "signal", "provide:uart",
               iface="uart", iface_member="provide"),
            _T("U0_RX", "signal", "provide:uart",
               iface="uart", iface_member="provide"),
            _T("U1_TX", "signal", "provide:uart",
               iface="uart", iface_member="provide"),
            _T("U1_RX", "signal", "provide:uart",
               iface="uart", iface_member="provide"),
            _T("U2_TX", "signal", "provide:uart",
               iface="uart", iface_member="provide"),
            _T("U2_RX", "signal", "provide:uart",
               iface="uart", iface_member="provide"),
        ],
        attrs={"uart_ports": 3},
    )
    dev = Component(
        refdes="U2", kind="uart_device", value="uart-peripheral", authored=True,
        terminals=[
            _T("VDD", "power_in", "vin:3.3", req_v=3.3),
            _T("GND", "ground"),
            _T("TX", "signal", "require:uart",
               iface="uart", iface_member="require"),
            _T("RX", "signal", "require:uart",
               iface="uart", iface_member="require"),
        ],
    )
    c_mcu = _decoupling_cap("C1", "U1")
    c_dev = _decoupling_cap("C2", "U2")

    comps = [src, mcu, dev, c_mcu, c_dev]
    nets = [
        Net("V3V3", "power", 3.3, [
            ("PS1", "VOUT"), ("U1", "VDD"), ("U2", "VDD"),
            ("C1", "+"), ("C2", "+"),
        ]),
        Net("GND", "ground", 0.0, [
            ("PS1", "GND"), ("U1", "GND"), ("U2", "GND"),
            ("C1", "-"), ("C2", "-"),
        ]),
        # The device's UART require pins sit on OPEN nets: nothing chosen.
        # This is the ambiguity that must ESCALATE (3 equally-valid ports).
        Net("DEV_TX", "open", None, [("U2", "TX")]),
        Net("DEV_RX", "open", None, [("U2", "RX")]),
    ]
    esc = [
        "UART assignment ambiguous: device U2 requires 1 UART but MCU U1 exposes "
        "3 free UART ports (U0/U1/U2); no unique mapping. Must be resolved by the "
        "author, not auto-picked.",
    ]
    return CanonicalGraph(components=comps, nets=nets, escalations=esc)


# ===========================================================================
# Intent 6 — reusable "sensor-with-decoupling" module, instantiated x2  -> PASS
#   The module = 1 sensor + its decoupling cap. Two instances on a shared 3V3
#   rail and I2C bus, with distinct addresses. (module x2)
# ===========================================================================

def _sensor_module(inst, sensor_refdes, cap_refdes, addr):
    """Return (components, extra_bus_nodes) for one module instance.
    Nodes for the shared rails/bus are returned to the caller to splice in."""
    sensor = _i2c_sensor(sensor_refdes, addr, value=f"sensor-mod-{inst}")
    cap = _decoupling_cap(cap_refdes, sensor_refdes)
    return [sensor, cap]


def _golden_6() -> CanonicalGraph:
    src = _source("PS1", 3.3)
    mcu = Component(
        refdes="U1", kind="mcu", value="MCU", authored=True,
        terminals=[
            _T("VDD", "power_in", "vin:3.3", req_v=3.3),
            _T("GND", "ground"),
            _T("SDA", "signal", "i2c_sda", iface="i2c", iface_member="sda"),
            _T("SCL", "signal", "i2c_scl", iface="i2c", iface_member="scl"),
        ],
        attrs={"i2c_master": True},
    )

    m1 = _sensor_module(1, "U2", "C1", 0x48)
    m2 = _sensor_module(2, "U3", "C2", 0x49)

    r_sda = _pullup("R1", "I2C_SDA")
    r_scl = _pullup("R2", "I2C_SCL")

    comps = [src, mcu] + m1 + m2 + [r_sda, r_scl]
    nets = [
        Net("V3V3", "power", 3.3, [
            ("PS1", "VOUT"), ("U1", "VDD"),
            ("U2", "VDD"), ("U3", "VDD"),
            ("C1", "+"), ("C2", "+"),
            ("R1", "2"), ("R2", "2"),
        ]),
        Net("GND", "ground", 0.0, [
            ("PS1", "GND"), ("U1", "GND"), ("U2", "GND"), ("U3", "GND"),
            ("C1", "-"), ("C2", "-"),
        ]),
        Net("I2C_SDA", "signal", None, [
            ("U1", "SDA"), ("U2", "SDA"), ("U3", "SDA"), ("R1", "1"),
        ]),
        Net("I2C_SCL", "signal", None, [
            ("U1", "SCL"), ("U2", "SCL"), ("U3", "SCL"), ("R2", "1"),
        ]),
    ]
    return CanonicalGraph(components=comps, nets=nets)


# ===========================================================================
# Intent 7 — a 5V-only device on the 3.3V rail   -> FAIL(VOLTAGE_MISMATCH)
# ===========================================================================

def _golden_7() -> CanonicalGraph:
    src = _source("PS1", 5.0)
    ldo = _ldo("U1", 5.0, 3.3)

    # This device REQUIRES 5V (vin:5) but is placed on the 3.3V rail.
    dev = Component(
        refdes="U2", kind="sensor", value="5V-only-device", authored=True,
        terminals=[
            _T("VDD", "power_in", "vin:5", req_v=5.0),
            _T("GND", "ground"),
        ],
        attrs={"vin_required": 5.0},
    )
    c_dev = _decoupling_cap("C1", "U2")

    comps = [src, ldo, dev, c_dev]
    nets = [
        Net("V5", "power", 5.0, [("PS1", "VOUT"), ("U1", "VIN")]),
        # 5V device VDD sits on the 3.3V rail -> mismatch
        Net("V3V3", "power", 3.3, [
            ("U1", "VOUT"), ("U2", "VDD"), ("C1", "+"),
        ]),
        Net("GND", "ground", 0.0, [
            ("PS1", "GND"), ("U1", "GND"), ("U2", "GND"), ("C1", "-"),
        ]),
    ]
    return CanonicalGraph(components=comps, nets=nets)


# ===========================================================================
# Intent 8 — BLDC, 3 half-bridges   -> PASS AND invariant holds
#
#   Per bridge:
#     * high-side N-mosfet {"side":"high"} + low-side N-mosfet {"side":"low"}
#       in series across the 48V HV rail (HS drain=HV, HS source=phase,
#       LS drain=phase, LS source=GND).
#     * a bootstrap gate_driver + bootstrap cap + bootstrap diode
#       (companions of the HS driver), driven from the 12V gate-drive rail.
#     * interlock logic so HS_gate and LS_gate can never both be high.
#
#   Interlock chosen so the invariant PROVABLY holds:
#       HS_gate = AND(IN_H, NOT(IN_L))
#       LS_gate = AND(IN_L, NOT(IN_H))
#   For any (IN_H, IN_L) these are never both 1 (AND-with-complement structure).
#
#   Rails: MCU logic 3.3V, gate-drive 12V, HV 48V (3 rails).
#   Declare one mutual_exclusion Invariant per bridge over the HS/LS gate-drive
#   OUTPUT nodes, with that bridge's 2 MCU command inputs.
# ===========================================================================

def _bldc_bridge(idx, comps, nets, invariants, mcu_refdes):
    """Append one half-bridge (index idx=1..3) to comps/nets/invariants.

    Component refdes scheme (b = bridge index):
        NOT gate for IN_H path :  G{b}A   (not)   -> nIN_H
        NOT gate for IN_L path :  G{b}B   (not)   -> nIN_L
        HS interlock AND       :  G{b}H   (and)   -> HS_gate  (IN_H & !IN_L)
        LS interlock AND       :  G{b}L   (and)   -> LS_gate  (IN_L & !IN_H)
        gate driver (bootstrap):  DRV{b}
        HS mosfet              :  Q{b}H   side=high
        LS mosfet              :  Q{b}L   side=low
        bootstrap cap          :  Cb{b}   (companion of DRV{b})
        bootstrap diode        :  Db{b}   (companion of DRV{b})
    MCU command pins for this bridge: IN_H{b}, IN_L{b} (logic_out on the MCU).
    """
    b = idx
    in_h = (mcu_refdes, f"IN_H{b}")
    in_l = (mcu_refdes, f"IN_L{b}")

    # --- interlock logic ---------------------------------------------------
    not_h = Component(
        refdes=f"G{b}A", kind="logic_gate", value="NOT", authored=True,
        logic_fn="not",
        terminals=[_T("A", "logic_in"), _T("Y", "logic_out")],
    )
    not_l = Component(
        refdes=f"G{b}B", kind="logic_gate", value="NOT", authored=True,
        logic_fn="not",
        terminals=[_T("A", "logic_in"), _T("Y", "logic_out")],
    )
    and_h = Component(
        refdes=f"G{b}H", kind="logic_gate", value="AND", authored=True,
        logic_fn="and",
        terminals=[_T("A", "logic_in"), _T("B", "logic_in"), _T("Y", "logic_out")],
    )
    and_l = Component(
        refdes=f"G{b}L", kind="logic_gate", value="AND", authored=True,
        logic_fn="and",
        terminals=[_T("A", "logic_in"), _T("B", "logic_in"), _T("Y", "logic_out")],
    )

    # --- gate driver + bootstrap companions --------------------------------
    drv = Component(
        refdes=f"DRV{b}", kind="gate_driver", value="halfbridge-driver",
        authored=True,
        terminals=[
            _T("VDD", "power_in", "vin:12", req_v=12.0),   # gate-drive 12V rail
            _T("GND", "ground"),
            _T("HIN", "logic_in"),               # HS gate command in
            _T("LIN", "logic_in"),               # LS gate command in
            _T("HO", "logic_out"),               # HS gate drive out
            _T("LO", "logic_out"),               # LS gate drive out
            _T("VB", "power_in", "require:bootstrap",       # bootstrap node (top)
               iface="bootstrap", iface_member="require"),
            _T("VS", "signal"),                  # phase / HS source ref
        ],
    )
    cb = Component(
        refdes=f"Cb{b}", kind="capacitor", value="1uF", authored=False,
        terminals=[_T("+", "passive"), _T("-", "passive")],
        attrs={"role": "bootstrap", "for": f"DRV{b}"},
    )
    db = Component(
        refdes=f"Db{b}", kind="diode", value="bootstrap-diode", authored=False,
        terminals=[_T("A", "passive"), _T("K", "passive")],   # anode / cathode
        attrs={"role": "bootstrap", "for": f"DRV{b}"},
    )

    # --- power mosfets -----------------------------------------------------
    qh = Component(
        refdes=f"Q{b}H", kind="mosfet", value="N-FET", authored=True,
        terminals=[
            _T("G", "logic_in"),
            _T("D", "power_in", "vin:48", req_v=48.0),
            _T("S", "power_out"),
        ],
        attrs={"side": "high"},
    )
    ql = Component(
        refdes=f"Q{b}L", kind="mosfet", value="N-FET", authored=True,
        terminals=[
            _T("G", "logic_in"),
            _T("D", "power_in"),
            _T("S", "ground"),
        ],
        attrs={"side": "low"},
    )

    comps.extend([not_h, not_l, and_h, and_l, drv, cb, db, qh, ql])

    # --- logic nets: wire the interlock ------------------------------------
    # IN_H command fans out (ONE net) to the NOT gate G{b}A.A and the HS AND
    # gate G{b}H.A.  IN_L command fans out (ONE net) to G{b}B.A and G{b}L.A.
    nets.append(Net(f"IN_H{b}", "signal", None, [in_h, (f"G{b}A", "A"), (f"G{b}H", "A")]))
    nets.append(Net(f"IN_L{b}", "signal", None, [in_l, (f"G{b}B", "A"), (f"G{b}L", "A")]))

    # HS_gate = AND(IN_H, NOT(IN_L)) : G{b}H.A=IN_H (above), G{b}H.B = NOT(IN_L)
    nets.append(Net(f"nIN_L{b}", "signal", None, [(f"G{b}B", "Y"), (f"G{b}H", "B")]))
    # LS_gate = AND(IN_L, NOT(IN_H)) : G{b}L.A=IN_L (above), G{b}L.B = NOT(IN_H)
    nets.append(Net(f"nIN_H{b}", "signal", None, [(f"G{b}A", "Y"), (f"G{b}L", "B")]))

    # interlock outputs feed the driver's HIN/LIN
    hs_gate_node = (f"G{b}H", "Y")
    ls_gate_node = (f"G{b}L", "Y")
    nets.append(Net(f"HS_cmd{b}", "signal", None, [hs_gate_node, (f"DRV{b}", "HIN")]))
    nets.append(Net(f"LS_cmd{b}", "signal", None, [ls_gate_node, (f"DRV{b}", "LIN")]))

    # driver outputs drive the mosfet gates
    nets.append(Net(f"HO{b}", "signal", None, [(f"DRV{b}", "HO"), (f"Q{b}H", "G")]))
    nets.append(Net(f"LO{b}", "signal", None, [(f"DRV{b}", "LO"), (f"Q{b}L", "G")]))

    # --- power / bootstrap nets --------------------------------------------
    # phase node: HS source, LS drain, driver VS, bootstrap cap '-'
    nets.append(Net(f"PHASE{b}", "signal", None, [
        (f"Q{b}H", "S"), (f"Q{b}L", "D"), (f"DRV{b}", "VS"), (f"Cb{b}", "-"),
    ]))
    # bootstrap node VB: driver VB, cap '+', diode cathode
    nets.append(Net(f"VB{b}", "power", None, [
        (f"DRV{b}", "VB"), (f"Cb{b}", "+"), (f"Db{b}", "K"),
    ]))

    # HS drain on the 48V HV rail (returned to caller to merge into HV net)
    hv_nodes = [(f"Q{b}H", "D")]
    # diode anode fed from the 12V gate-drive rail (returned to merge into V12)
    v12_nodes = [(f"DRV{b}", "VDD"), (f"Db{b}", "A")]
    gnd_nodes = [(f"DRV{b}", "GND"), (f"Q{b}L", "S")]

    # invariant for this bridge: HS gate-drive out vs LS gate-drive out
    invariants.append(Invariant(
        kind="mutual_exclusion",
        a=hs_gate_node,      # (G{b}H, Y)
        b=ls_gate_node,      # (G{b}L, Y)
        inputs=[in_h, in_l],
    ))

    return hv_nodes, v12_nodes, gnd_nodes


def _golden_8() -> CanonicalGraph:
    # three rails
    src48 = _source("PS1", 48.0, value="48V-HV")
    src12 = _source("PS2", 12.0, value="12V-gate")
    src3v3 = _source("PS3", 3.3, value="3.3V-logic")

    # MCU with 6 PWM command outputs (2 per bridge)
    mcu_terms = [
        _T("VDD", "power_in", "vin:3.3", req_v=3.3),
        _T("GND", "ground"),
    ]
    for b in (1, 2, 3):
        mcu_terms.append(_T(f"IN_H{b}", "logic_out", "pwm"))
        mcu_terms.append(_T(f"IN_L{b}", "logic_out", "pwm"))
    mcu = Component(
        refdes="U1", kind="mcu", value="motor-mcu", authored=True,
        terminals=mcu_terms, attrs={"pwm_channels": 6},
    )
    c_mcu = _decoupling_cap("C1", "U1")

    comps: List[Component] = [src48, src12, src3v3, mcu, c_mcu]
    nets: List[Net] = []
    invariants: List[Invariant] = []

    hv_all: List = []
    v12_all: List = []
    gnd_all: List = [("PS1", "GND"), ("PS2", "GND"), ("PS3", "GND"),
                     ("U1", "GND"), ("C1", "-")]

    for b in (1, 2, 3):
        hv_nodes, v12_nodes, gnd_nodes = _bldc_bridge(b, comps, nets, invariants, "U1")
        hv_all.extend(hv_nodes)
        v12_all.extend(v12_nodes)
        gnd_all.extend(gnd_nodes)

    # rail nets
    nets.append(Net("V3V3", "power", 3.3, [
        ("PS3", "VOUT"), ("U1", "VDD"), ("C1", "+"),
    ]))
    nets.append(Net("V12", "power", 12.0, [("PS2", "VOUT")] + v12_all))
    nets.append(Net("HV48", "power", 48.0, [("PS1", "VOUT")] + hv_all))
    nets.append(Net("GND", "ground", 0.0, gnd_all))

    return CanonicalGraph(components=comps, nets=nets, invariants=invariants)


# ===========================================================================
# Assemble GOLDENS (ordered by intent id)
# ===========================================================================

GOLDENS: "OrderedDict[int, Dict]" = OrderedDict()
GOLDENS[1] = {"graph": _golden_1(), "expected": "PASS"}
GOLDENS[2] = {"graph": _golden_2(), "expected": "PASS"}
GOLDENS[3] = {"graph": _golden_3(), "expected": "FAIL:ADDR_COLLISION"}
GOLDENS[4] = {"graph": _golden_4(), "expected": "PASS"}
GOLDENS[5] = {"graph": _golden_5(), "expected": "ESCALATE"}
GOLDENS[6] = {"graph": _golden_6(), "expected": "PASS"}
GOLDENS[7] = {"graph": _golden_7(), "expected": "FAIL:VOLTAGE_MISMATCH"}
GOLDENS[8] = {"graph": _golden_8(), "expected": "PASS"}


# ===========================================================================
# KNOWN_BAD — hand-built corrupted variants for the self-test.
# (mutate.py generates more programmatically; these are stable anchors that do
#  not depend on mutate.py so the schema/goldens layer is self-verifiable.)
# ===========================================================================

def _kb_addr_collision() -> CanonicalGraph:
    """Start from golden 1 (clean) and duplicate an i2c_addr -> ADDR_COLLISION."""
    g = _golden_1()
    # U4 had 0x49; retarget it to 0x48 to collide with U3.
    for c in g.components:
        if c.refdes == "U4":
            c.attrs["i2c_addr"] = 0x48
    return g


def _kb_unconnected_mandatory() -> CanonicalGraph:
    """Golden 1 with the MCU power_in removed from the 3V3 rail -> its VDD sits
    on an open net with no provider -> UNCONNECTED_MANDATORY."""
    g = _golden_1()
    for n in g.nets:
        if n.name == "V3V3":
            n.nodes = [nd for nd in n.nodes if tuple(nd) != ("U2", "VDD")]
    # put the now-dangling MCU VDD on an explicit OPEN net (no default)
    g.nets.append(Net("U2_VDD_open", "open", None, [("U2", "VDD")]))
    return g


def _kb_voltage_mismatch() -> CanonicalGraph:
    """Golden 1 with the MCU require retargeted from 3.3V to 5V while still on the
    3.3V rail -> VOLTAGE_MISMATCH."""
    g = _golden_1()
    for c in g.components:
        if c.refdes == "U2":
            t = c.terminal("VDD")
            t.req_v = 5.0          # now wants 5V but sits on the 3.3V rail
            t.function = "vin:5"   # (human label only; nothing keys off it)
    return g


def _kb_shoot_through() -> CanonicalGraph:
    """Golden 8 bridge-1 with a PASS-THROUGH 2-input interlock (FIX 3).

    The MCU still issues two INDEPENDENT commands per bridge (IN_H1, IN_L1). A
    *correct* interlock cross-inhibits: HS_gate = IN_H & !IN_L, LS_gate =
    IN_L & !IN_H, so the (1,1) command can never drive both gates high. This
    known-bad replaces the cross-inhibit with a PASS-THROUGH: each output gate
    simply repeats its own raw command and ignores the other, so:

        G1H.Y (HS gate)  == IN_H1        (no NOT(IN_L) inhibit)
        G1L.Y (LS gate)  == IN_L1        (no NOT(IN_H) inhibit)

    Both gates remain genuine 2-input logic gates (the interlock is present in
    form, absent in function) -- but under command (IN_H1=1, IN_L1=1) BOTH gate
    drives go high -> the exhaustive invariant checker finds the (1,1)
    counterexample and fires SHOOT_THROUGH.
    """
    g = _golden_8()
    # Rewire bridge-1 so each interlock AND reads its OWN command on BOTH inputs
    # (a pass-through: AND(x, x) == x) with NO cross-inhibit term. Both gates
    # stay 2-input logic_gates; only the wiring changes.
    #   G1H.A already carries IN_H1 (net "IN_H1"); repoint G1H.B to IN_H1 too.
    #   G1L.A already carries IN_L1 (net "IN_L1"); repoint G1L.B to IN_L1 too.
    # Drop the complement feed nets (nIN_L1 -> G1H.B, nIN_H1 -> G1L.B).
    drop = {"nIN_L1", "nIN_H1"}
    g.nets = [n for n in g.nets if n.name not in drop]
    for n in g.nets:
        if n.name == "IN_H1":
            n.nodes = list(n.nodes) + [("G1H", "B")]   # G1H = AND(IN_H1, IN_H1)
        elif n.name == "IN_L1":
            n.nodes = list(n.nodes) + [("G1L", "B")]   # G1L = AND(IN_L1, IN_L1)
    # Now G1H.Y = IN_H1 and G1L.Y = IN_L1. IN_H1=IN_L1=1 -> both high.
    return g


KNOWN_BAD: List[Dict] = [
    {
        "graph": _kb_addr_collision(),
        "expected_code": "ADDR_COLLISION",
        "note": "Golden 1 with U4 i2c_addr duplicated to 0x48 (collides with U3).",
    },
    {
        "graph": _kb_unconnected_mandatory(),
        "expected_code": "UNCONNECTED_MANDATORY",
        "note": "Golden 1 with MCU U2 VDD moved off the 3V3 rail onto an open net.",
    },
    {
        "graph": _kb_voltage_mismatch(),
        "expected_code": "VOLTAGE_MISMATCH",
        "note": "Golden 1 with MCU U2 requiring 5V while sitting on the 3.3V rail.",
    },
    {
        "graph": _kb_shoot_through(),
        "expected_code": "SHOOT_THROUGH",
        "note": "Golden 8 bridge-1 interlock bypassed (ANDs -> buffers); "
                "IN_H1=IN_L1=1 makes HS & LS gate outputs both high.",
    },
]


__all__ = ["GOLDENS", "KNOWN_BAD"]
