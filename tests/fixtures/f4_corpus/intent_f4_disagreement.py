"""F4 fixture — declared clean, actually colliding.

Two sensors at 0x48 on one bus: the same intent-level static error as the
examples corpus's intent_03_addr_collision, but declared ``expected_l1 = ()``
(clean). The engine emits anyway — the intent's own declaration is what emit
gates the M2 data path on, and whether the ORACLE agrees with the declaration
is the harness gate's verdict, not the engine's (RunnerSplit.md; the F4
finding). The gate must exit nonzero with an expected-codes FAIL naming
ADDR_COLLISION.

Self-contained on purpose: the library roles are re-declared inline (copied
from wyred-examples/corpus/lib_parts.py) so this fixture package needs no
cross-import and therefore no coupling to any other corpus package's name.
"""

from wyred import Module, bus, demand, ground, late, param, provide, rail, use


class Supply(Module):
    """A power source driving one design rail (lib_parts.Supply)."""

    kind = "supply"
    volts = param(3.3)
    rail_name = param("+3V3")
    out = provide("power", volts=late("volts"), rail=late("rail_name"))


class Mcu(Module):
    """The base MCU role (lib_parts.Mcu)."""

    kind = "mcu"
    supply_volts = param(3.3)
    pwr = demand("power", volts=late("supply_volts"),
                 companions=("decoupling_cap",))


class I2cMcu(Mcu):
    """An MCU that is an I2C master (lib_parts.I2cMcu)."""

    i2c = provide("i2c_master")


class TempSensor(Module):
    """An I2C temperature sensor role (lib_parts.TempSensor)."""

    kind = "sensor"
    addr = param(0x48)
    bus_name = param("I2C0")
    supply_volts = param(3.3)
    pwr = demand("power", volts=late("supply_volts"),
                 companions=("decoupling_cap",))
    i2c = demand("i2c", bus=late("bus_name"), i2c_addr=late("addr"))


class F4Disagreement(Module, intent="intent_f4_disagreement"):
    expected_l1 = ()                     # DECLARED clean — the lie under test

    v33 = rail("+3V3", 3.3)
    gnd = ground("GND")
    i2c0 = bus("I2C0", "i2c")

    supply = use(Supply)
    mcu = use(I2cMcu)
    s1 = use(TempSensor)                 # 0x48 (default)
    s2 = use(TempSensor)                 # 0x48 again — the collision
