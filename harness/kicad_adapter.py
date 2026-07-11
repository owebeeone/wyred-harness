"""OPTIONAL kicad-cli adapter: cross-engine differential via KiCad's netlister.

Harness v3 (Gen4 section 4: the data paths ARE the denotations -- cross-path
consistency is an architectural differential oracle). This module lets the
harness run a design's ``.kicad_sch`` through KiCad's own netlist engine and
compare the result against a graph produced by any other path (neutral JSON,
the elaborator, a golden).

EVERYTHING HERE IS OPTIONAL. The harness must never REQUIRE kicad-cli:

  * :func:`find_kicad_cli` returns ``None`` when the binary is absent.
  * :func:`sch_netlist` returns ``None`` (never raises) when kicad-cli is
    unavailable, so callers degrade to the note in :data:`UNAVAILABLE_NOTE`.
  * :func:`compare` is pure Python and always works.

Hard-won operational rules (encoded below, do not "fix" them away):

  * kicad-cli ALWAYS emits harmless Fontconfig noise on stderr. Success is
    judged by EXIT CODE plus existence of a NON-EMPTY output file -- stderr is
    captured and discarded, never inspected.
  * Legacy Eeschema v4 ``.sch`` files CANNOT be netlisted headlessly: kicad-cli
    exits 0 and writes a silently EMPTY netlist. :func:`sch_netlist` therefore
    REFUSES ``.sch`` inputs outright (raises :class:`KicadCliError`) instead of
    returning a plausible-looking empty graph. Modern ``.kicad_sch`` works.
  * Cross-engine EXACT equivalence is the wrong test: real schematics carry
    testpoints, mounting holes, logos, fiducials that other paths do not.
    :func:`compare` computes tolerant similarity metrics instead and calls the
    engines in agreement at >= 0.9 overlap.

Parsing reuses :mod:`normalize`'s S-expression machinery and extends its
``parse_kicad_netlist`` for the ``(export (version ...`` dialect that
``kicad-cli sch export netlist --format kicadsexpr`` produces:
``(comp (ref/value/libsource/units))`` and
``(net (code/name) (node (ref)(pin)(pintype)))`` are parsed fully, pin
universes are recovered from libparts/units (so UNCONNECTED pins survive), and
terminal roles are recovered from KiCad pin types where possible. normalize.py
itself is untouched (v3 is additive; the v2 selftest keeps passing).

Pure Python 3 stdlib; the only external touchpoint is the kicad-cli binary,
invoked via subprocess. Plain imports (flat harness directory).
"""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
from math import comb
from typing import Any, Dict, List, Optional, Tuple

import normalize
from pcb_extract import classify_net as _classify_net_name
from schema import CanonicalGraph, Component, Net, Terminal


# ---------------------------------------------------------------------------
# Locating kicad-cli
# ---------------------------------------------------------------------------

# Checked AFTER $PATH, in order. The Homebrew symlink is normally on PATH
# already; the app-bundle path is the stock macOS install location.
KICAD_CLI_FALLBACKS: Tuple[str, ...] = (
    "/opt/homebrew/bin/kicad-cli",
    "/Applications/KiCad/KiCad.app/Contents/MacOS/kicad-cli",
)

#: The graceful-degradation note callers should surface when kicad-cli is
#: absent (e.g. append to a report / escalation list). Never an error.
UNAVAILABLE_NOTE = "kicad-cli unavailable"

#: Optional env var naming a directory for the temporary export file. When
#: unset, a private tempfile.mkdtemp() directory is created and removed.
WORKDIR_ENV = "KICAD_ADAPTER_WORKDIR"


class KicadCliError(RuntimeError):
    """kicad-cli was FOUND but the export failed (or the input is unusable).

    Distinct from unavailability: :func:`sch_netlist` returns ``None`` when the
    binary is absent, and raises this only for real failures worth reporting
    (non-zero exit, empty/missing output, legacy .sch input, timeout).
    """


def find_kicad_cli() -> Optional[str]:
    """Return an executable kicad-cli path, or ``None`` if not installed.

    Search order: $PATH (``shutil.which``), then :data:`KICAD_CLI_FALLBACKS`
    (Homebrew path, then the macOS app bundle). Never raises.
    """
    found = shutil.which("kicad-cli")
    if found:
        return found
    for cand in KICAD_CLI_FALLBACKS:
        if os.path.isfile(cand) and os.access(cand, os.X_OK):
            return cand
    return None


# ---------------------------------------------------------------------------
# Hardened parser for the exported "(export (version ..." dialect
# ---------------------------------------------------------------------------

_ADAPTER_STUB_NOTE = (
    "STUBBED(kicad-cli export): connectivity (refdes/value/pins/net partition) "
    "is faithful; terminal roles are recovered from KiCad pin types where "
    "present (power_in/power_out/passive; GND-named pins -> ground) and are "
    "heuristic elsewhere; kinds are inferred from refdes prefixes. i2c "
    "addresses, iface/req_v/prov_v intent, authored flags and invariants do "
    "not exist in a KiCad netlist and are NOT invented."
)

# KiCad electrical pin type -> canonical Terminal.role. Types absent from the
# map (input/output/bidirectional/tri_state/open_collector/open_emitter/...)
# become plain "signal"; unknown/free/unspecified stay "passive" -- we never
# invent intent the netlist does not carry.
_PINTYPE_ROLE = {
    "power_in": "power_in",
    "power_out": "power_out",
    "passive": "passive",
    "input": "signal",
    "output": "signal",
    "bidirectional": "signal",
    "tri_state": "signal",
    "open_collector": "signal",
    "open_emitter": "signal",
}

# Pin NAMES that mean "this is the ground pin" regardless of pin type.
_GROUND_PIN_NAMES = frozenset({"GND", "VSS", "AGND", "DGND", "PGND", "GNDA", "GNDD"})


def _role_from_pintype(pintype: Optional[str]) -> Optional[str]:
    """Map a KiCad pintype token to a canonical role, or None if unknown.

    KiCad may suffix types with "+no_connect" (e.g. "passive+no_connect");
    only the base type carries meaning here.
    """
    if not pintype:
        return None
    base = pintype.split("+", 1)[0].strip().lower()
    return _PINTYPE_ROLE.get(base)


def _net_kind(name: str) -> Tuple[str, Optional[float]]:
    """(kind, voltage) heuristic for an exported net name.

    Same rules as normalize.parse_kicad_netlist, applied to the LAST path
    segment (exports prefix local labels with the sheet path, e.g. "/TOP").
    """
    base = name.rsplit("/", 1)[-1] if name else name
    low = base.lower()
    if "gnd" in low or low in ("0", "vss"):
        return "ground", 0.0
    if low.startswith("v") or low.startswith("+") or "vcc" in low \
            or "vdd" in low or "rail" in low:
        return "power", None
    return "signal", None


def parse_exported_netlist(text: str) -> CanonicalGraph:
    """Parse ``kicad-cli sch export netlist --format kicadsexpr`` output.

    Hardened extension of :func:`normalize.parse_kicad_netlist` (whose
    S-expression helpers it reuses) for the ``(export (version "E") ...``
    dialect:

      * ``(components (comp (ref)(value)(libsource)(units ...)))`` -- refdes,
        value, and the FULL declared pin universe (units/pins), so pins on no
        net still appear as terminals (dangling-pin visibility).
      * ``(libparts (libpart (lib)(part)(pins (pin (num)(name)(type)))))`` --
        per-pin names and electrical types; names land in the free-form
        ``function`` label, types drive the role mapping.
      * ``(nets (net (code)(name)(node (ref)(pin)(pintype))))`` -- the net
        partition; missing/empty net names fall back to ``N$<code>``; duplicate
        nodes are deduped; nodes naming an undeclared component synthesize one
        (connectivity is never dropped).

    Anything that is not an ``(export ...)`` form falls through to
    :func:`normalize.parse_kicad_netlist` unchanged.
    """
    root = normalize._sexpr_parse(text)
    if not (isinstance(root, list) and root
            and not isinstance(root[0], list)
            and normalize._atom(root[0]) == "export"):
        return normalize.parse_kicad_netlist(text)

    find_all, field, atom = normalize._find_all, normalize._field, normalize._atom

    # ---- libparts: (lib, part) -> ordered [(num, name, type)] ---------------
    libpart_pins: Dict[Tuple[str, str], List[Tuple[str, str, str]]] = {}
    for lp in find_all(root, "libpart"):
        lib = field(lp, "lib") or ""
        part = field(lp, "part") or ""
        pins: List[Tuple[str, str, str]] = []
        for pin in find_all(lp, "pin"):
            num = field(pin, "num")
            if num is None:
                continue
            pins.append((num, field(pin, "name") or "", field(pin, "type") or ""))
        libpart_pins[(lib, part)] = pins

    # ---- components: ref -> (value, declared pin nums, libsource key) -------
    comp_value: Dict[str, str] = {}
    comp_lib: Dict[str, Tuple[str, str]] = {}
    comp_pins: Dict[str, List[str]] = {}       # ordered, deduped pin numbers
    ref_order: List[str] = []

    def _note_pin(ref: str, pin: str) -> None:
        lst = comp_pins.setdefault(ref, [])
        if pin not in lst:
            lst.append(pin)

    for comp in find_all(root, "comp"):
        ref = field(comp, "ref")
        if ref is None:
            continue
        if ref not in comp_value:
            ref_order.append(ref)
        comp_value[ref] = field(comp, "value") or ""
        for ls in find_all(comp, "libsource"):
            comp_lib[ref] = (field(ls, "lib") or "", field(ls, "part") or "")
            break
        # declared pin universe: prefer the libpart (has names/types), fall
        # back to the comp's own (units (unit (pins (pin (num ...))))) list.
        for num, _name, _type in libpart_pins.get(comp_lib.get(ref, ("", "")), []):
            _note_pin(ref, num)
        for pin in find_all(comp, "pin"):
            num = field(pin, "num")
            if num is not None:
                _note_pin(ref, num)

    # ---- nets: partition + per-node pintypes --------------------------------
    nets: List[Net] = []
    node_pintype: Dict[Tuple[str, str], str] = {}
    nets_sections = find_all(root, "nets")
    net_forms = find_all(nets_sections[0], "net") if nets_sections else []
    for net in net_forms:
        code = field(net, "code") or ""
        name = field(net, "name") or ""
        if not name:
            name = "N$%s" % (code or len(nets))
        nodes: List[Tuple[str, str]] = []
        for node in find_all(net, "node"):
            ref = field(node, "ref")
            pin = field(node, "pin")
            if ref is None or pin is None:
                continue
            if (ref, pin) in nodes:
                continue  # dedupe defensively
            nodes.append((ref, pin))
            pt = field(node, "pintype")
            if pt:
                node_pintype[(ref, pin)] = pt
            if ref not in comp_value:
                # node names an undeclared component (hierarchical quirk /
                # hand-edited file): synthesize it, never drop connectivity.
                comp_value[ref] = ""
                ref_order.append(ref)
            _note_pin(ref, pin)
        kind, voltage = _net_kind(name)
        nets.append(Net(name=name, kind=kind, voltage=voltage, nodes=nodes))

    # ---- build components with named/typed terminals -------------------------
    components: List[Component] = []
    for ref in ref_order:
        lib_key = comp_lib.get(ref, ("", ""))
        pin_meta = {num: (nm, tp) for num, nm, tp in libpart_pins.get(lib_key, [])}
        terms: List[Terminal] = []
        for pin in comp_pins.get(ref, []):
            pin_name, pin_type = pin_meta.get(pin, ("", ""))
            role = (_role_from_pintype(pin_type)
                    or _role_from_pintype(node_pintype.get((ref, pin)))
                    or "passive")
            if pin_name.upper() in _GROUND_PIN_NAMES or pin.upper() in _GROUND_PIN_NAMES:
                role = "ground"
            # pin NAME is a human label only -> function (contract-safe).
            terms.append(Terminal(name=pin, role=role, function=pin_name))
        attrs: Dict[str, Any] = {"stub": "kicad_export"}
        if lib_key != ("", ""):
            attrs["libsource"] = "%s:%s" % lib_key
        components.append(Component(
            refdes=ref,
            kind=normalize._kind_from_refdes(ref),
            value=comp_value.get(ref, ""),
            authored=True,   # a netlist cannot distinguish generated companions
            terminals=terms,
            attrs=attrs,
        ))

    return CanonicalGraph(
        components=components,
        nets=nets,
        escalations=[_ADAPTER_STUB_NOTE],
    )


# ---------------------------------------------------------------------------
# sch -> CanonicalGraph via kicad-cli
# ---------------------------------------------------------------------------

def sch_netlist(
    kicad_sch_path: str,
    *,
    kicad_cli: Optional[str] = None,
    work_dir: Optional[str] = None,
    timeout: float = 120.0,
) -> Optional[CanonicalGraph]:
    """Netlist a modern ``.kicad_sch`` through kicad-cli; return the graph.

    Returns ``None`` -- gracefully, no exception -- when kicad-cli cannot be
    found (see :data:`UNAVAILABLE_NOTE`). Raises :class:`KicadCliError` only
    for REAL failures on an available toolchain:

      * legacy Eeschema v4 ``.sch`` input (headless netlisting is silently
        EMPTY -- refused up front rather than returning a lying empty graph),
      * non-zero exit code,
      * exit 0 but missing/empty output file,
      * subprocess timeout.

    Success is judged by exit code + non-empty output file ONLY; stderr is
    discarded (kicad-cli always prints harmless Fontconfig noise there).

    ``work_dir``: directory for the temporary export file. Defaults to
    ``$KICAD_ADAPTER_WORKDIR`` if set, else a private ``mkdtemp`` directory
    that is removed afterwards (caller-supplied/env dirs are left alone).
    """
    src = os.path.abspath(kicad_sch_path)
    if not os.path.isfile(src):
        raise KicadCliError("schematic not found: %s" % src)
    low = src.lower()
    if low.endswith(".sch") and not low.endswith(".kicad_sch"):
        raise KicadCliError(
            "legacy Eeschema v4 .sch cannot be netlisted headlessly (kicad-cli "
            "exits 0 with a silently EMPTY netlist); convert to .kicad_sch: %s"
            % src)

    cli = kicad_cli or find_kicad_cli()
    if cli is None:
        return None  # graceful degradation: UNAVAILABLE_NOTE, never an error

    env_dir = os.environ.get(WORKDIR_ENV)
    owned_tmp: Optional[str] = None
    if work_dir is not None:
        out_dir = work_dir
        os.makedirs(out_dir, exist_ok=True)
    elif env_dir:
        out_dir = env_dir
        os.makedirs(out_dir, exist_ok=True)
    else:
        owned_tmp = tempfile.mkdtemp(prefix="kicad_adapter_")
        out_dir = owned_tmp

    stem = os.path.splitext(os.path.basename(src))[0]
    out_path = os.path.join(out_dir, "%s.%d.net" % (stem, os.getpid()))
    try:
        try:
            proc = subprocess.run(
                [cli, "sch", "export", "netlist",
                 "--format", "kicadsexpr", "-o", out_path, src],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,   # ALWAYS noisy (Fontconfig); ignored
                timeout=timeout,
                check=False,
            )
        except subprocess.TimeoutExpired:
            raise KicadCliError(
                "kicad-cli netlist export timed out after %.0fs on %s"
                % (timeout, src))
        except OSError as exc:
            raise KicadCliError("failed to invoke kicad-cli (%s): %s" % (cli, exc))

        # Judge by EXIT CODE + output file, never stderr.
        if proc.returncode != 0:
            raise KicadCliError(
                "kicad-cli netlist export failed with exit code %d on %s "
                "(stderr intentionally ignored)" % (proc.returncode, src))
        if not os.path.isfile(out_path) or os.path.getsize(out_path) == 0:
            raise KicadCliError(
                "kicad-cli exited 0 but produced no netlist output for %s "
                "(empty/missing %s)" % (src, out_path))

        with open(out_path, "r", encoding="utf-8") as fh:
            text = fh.read()
        return parse_exported_netlist(text)
    finally:
        if owned_tmp is not None:
            shutil.rmtree(owned_tmp, ignore_errors=True)
        else:
            # caller/env-owned dir: remove only our own temp file
            try:
                os.remove(out_path)
            except OSError:
                pass


# ---------------------------------------------------------------------------
# Tolerant cross-engine comparison
# ---------------------------------------------------------------------------

#: Similarity at or above which two engines are said to agree. Exact
#: equivalence is deliberately NOT required: testpoints, mounting holes,
#: fiducials and logo parts legitimately exist in one path and not another.
AGREE_THRESHOLD = 0.9


def _named_power_nodes(graph: CanonicalGraph) -> Dict[Tuple[str, str], str]:
    """(refdes, pin) -> canonical LOCAL name for every node sitting on a
    net whose NAME classifies as ground or power (pcb_extract's name
    heuristic). Signal nets contribute nothing — only functionally-named
    ground/power rails participate in the named-net-merge check."""
    out: Dict[Tuple[str, str], str] = {}
    for net in graph.nets:
        name = net.name or ""
        if _classify_net_name(name)[0] not in ("ground", "power"):
            continue
        local = name.rsplit("/", 1)[-1].strip().upper()
        if not local:
            continue
        for nd in net.nodes:
            out.setdefault(tuple(nd), local)
    return out


def _named_net_merge_warnings(graph_a: CanonicalGraph,
                              graph_b: CanonicalGraph) -> List[str]:
    """Cheap shorted-named-nets check: if two DIFFERENTLY-named ground/power
    nets (by name heuristic) land in one connectivity group in either graph
    — e.g. GND dead-shorted into +3V3 by one engine's view — report it. The
    Rand-index similarity barely moves for such a merge on a large board, so
    this is surfaced as an explicit warning instead."""
    warnings: List[str] = []
    for label, g, other in (("a", graph_a, graph_b), ("b", graph_b, graph_a)):
        names_other = _named_power_nodes(other)
        for net in g.nets:
            hit = sorted({names_other[t] for t in map(tuple, net.nodes)
                          if t in names_other})
            if len(hit) >= 2:
                warnings.append(
                    "named_net_merge: net %r of graph_%s joins nodes that "
                    "the other graph splits across differently-named "
                    "ground/power nets %s — a likely dead short / engine "
                    "connectivity merge" % (net.name, label, hit))
    return warnings


def _refdes_set(graph: CanonicalGraph) -> set:
    refs = {c.refdes for c in graph.components}
    for net in graph.nets:
        for node in net.nodes:
            refs.add(tuple(node)[0])
    return refs


def _node_blocks(graph: CanonicalGraph) -> Dict[Tuple[str, str], int]:
    """Map every net node (refdes, pin) to the index of its net (first wins)."""
    blocks: Dict[Tuple[str, str], int] = {}
    for i, net in enumerate(graph.nets):
        for node in net.nodes:
            blocks.setdefault(tuple(node), i)
    return blocks


def compare(graph_a: CanonicalGraph, graph_b: CanonicalGraph) -> Dict[str, Any]:
    """Tolerant cross-engine differential between two CanonicalGraphs.

    CAVEAT (differential honesty, F7): the similarity here is a RAND INDEX
    over the shared-node net partitions. It detects GROSS engine
    disagreement only — it is emphatically NOT a wiring gate. A handful of
    swapped or shorted pins on a large board barely moves the index, so an
    'engines_agree' verdict never certifies correct wiring; correctness is
    the ERC / spec-satisfaction stack's job. One cheap targeted check IS
    included: differently-named ground/power nets landing in one
    connectivity group (a dead short like GND merged into +3V3) are
    surfaced as ``named_net_merge`` warnings even when the similarity stays
    high.

    Returns a dict with (at least):

      ``component_overlap``        Jaccard similarity of the refdes sets
                                   (1.0 when both are empty).
      ``net_partition_similarity`` Rand index of the two net partitions
                                   restricted to the SHARED (refdes, pin)
                                   nodes -- do shared nodes group the same
                                   way? (1.0 when < 2 shared nodes: vacuous).
      ``verdict``                  "engines_agree" if BOTH similarities are
                                   >= AGREE_THRESHOLD (0.9), else
                                   "engines_disagree".
      ``warnings``                 list of warning strings; each
                                   ``named_net_merge:`` entry flags two
                                   differently-named ground/power nets fused
                                   into one connectivity group in one graph.

    Extra diagnostic keys (not part of the stable contract):
    ``components_only_in_a`` / ``components_only_in_b`` (sorted refdes lists)
    and ``shared_node_count``.

    Names of nets never matter to the SIMILARITY metrics; neither do values,
    kinds, roles or terminal order -- this is a pure connectivity
    comparison, per the v2 equivalence philosophy, but SIMILARITY-based
    instead of exact so that engine-specific extras (testpoints, mounting
    holes) do not flip the verdict. (Ground/power net NAMES are read only by
    the named_net_merge warning heuristic.)
    """
    refs_a, refs_b = _refdes_set(graph_a), _refdes_set(graph_b)
    union = refs_a | refs_b
    inter = refs_a & refs_b
    component_overlap = (len(inter) / len(union)) if union else 1.0

    blocks_a, blocks_b = _node_blocks(graph_a), _node_blocks(graph_b)
    shared = sorted(set(blocks_a) & set(blocks_b))
    n = len(shared)
    if n < 2:
        net_similarity = 1.0
    else:
        count_a: Dict[int, int] = {}
        count_b: Dict[int, int] = {}
        count_ab: Dict[Tuple[int, int], int] = {}
        for node in shared:
            la, lb = blocks_a[node], blocks_b[node]
            count_a[la] = count_a.get(la, 0) + 1
            count_b[lb] = count_b.get(lb, 0) + 1
            count_ab[(la, lb)] = count_ab.get((la, lb), 0) + 1
        pairs = comb(n, 2)
        same_a = sum(comb(c, 2) for c in count_a.values())
        same_b = sum(comb(c, 2) for c in count_b.values())
        same_both = sum(comb(c, 2) for c in count_ab.values())
        # Rand index: pairs classified identically (together-together +
        # apart-apart) over all pairs of shared nodes.
        net_similarity = (pairs + 2 * same_both - same_a - same_b) / pairs

    verdict = ("engines_agree"
               if component_overlap >= AGREE_THRESHOLD
               and net_similarity >= AGREE_THRESHOLD
               else "engines_disagree")
    return {
        "component_overlap": component_overlap,
        "net_partition_similarity": net_similarity,
        "verdict": verdict,
        "warnings": _named_net_merge_warnings(graph_a, graph_b),
        "components_only_in_a": sorted(refs_a - refs_b),
        "components_only_in_b": sorted(refs_b - refs_a),
        "shared_node_count": n,
    }


__all__ = [
    "find_kicad_cli",
    "sch_netlist",
    "parse_exported_netlist",
    "compare",
    "KicadCliError",
    "UNAVAILABLE_NOTE",
    "AGREE_THRESHOLD",
    "KICAD_CLI_FALLBACKS",
    "WORKDIR_ENV",
]


# ===========================================================================
# self-check
# ===========================================================================

# Trimmed-but-faithful sample of `kicad-cli sch export netlist` output
# (KiCad 10.0.4), exercising: libpart pin names/types, an UNCONNECTED pin
# (U1 pin 1 is on no net), quoted parens in a value, an empty net name, a
# pintype-only role, and a node whose component is undeclared (TP1).
_CANNED_EXPORT = '''
(export (version "E")
  (design (source "canned.kicad_sch") (tool "Eeschema 10.0.4"))
  (components
    (comp (ref "R1") (value "4.7k (1%)")
      (libsource (lib "Device") (part "R") (description ""))
      (sheetpath (names "/") (tstamps "/"))
      (units (unit (name "A") (pins (pin (num "1")) (pin (num "2"))))))
    (comp (ref "U1") (value "REG-3V3")
      (libsource (lib "Regulator") (part "LDO") (description ""))
      (units (unit (name "A")
        (pins (pin (num "1")) (pin (num "2")) (pin (num "3")))))))
  (libparts
    (libpart (lib "Device") (part "R")
      (pins (pin (num "1") (name "~") (type "passive"))
            (pin (num "2") (name "~") (type "passive"))))
    (libpart (lib "Regulator") (part "LDO")
      (pins (pin (num "1") (name "VIN") (type "power_in"))
            (pin (num "2") (name "GND") (type "power_in"))
            (pin (num "3") (name "VOUT") (type "power_out")))))
  (libraries)
  (nets
    (net (code "1") (name "+3V3")
      (node (ref "U1") (pin "3") (pintype "power_out"))
      (node (ref "R1") (pin "1") (pintype "passive")))
    (net (code "2") (name "GND")
      (node (ref "U1") (pin "2") (pintype "power_in"))
      (node (ref "TP1") (pin "1") (pintype "passive")))
    (net (code "3") (name "")
      (node (ref "R1") (pin "2") (pintype "passive")))))
'''

# Minimal modern .kicad_sch (verified against kicad-cli 10.0.4): two resistors
# joined by two wires -> nets {R1.1,R2.1} and {R1.2,R2.2}.
_PROBE_KICAD_SCH = '''
(kicad_sch (version 20231120) (generator "kicad_adapter_selfcheck")
  (uuid "aaaaaaaa-1111-2222-3333-444444444444")
  (paper "A4")
  (lib_symbols
    (symbol "Device:R" (pin_numbers hide) (pin_names (offset 0))
      (exclude_from_sim no) (in_bom yes) (on_board yes)
      (property "Reference" "R" (at 2.032 0 90) (effects (font (size 1.27 1.27))))
      (property "Value" "R" (at 0 0 90) (effects (font (size 1.27 1.27))))
      (symbol "R_0_1"
        (rectangle (start -1.016 -2.54) (end 1.016 2.54)
          (stroke (width 0.254) (type default)) (fill (type none))))
      (symbol "R_1_1"
        (pin passive line (at 0 3.81 270) (length 1.27)
          (name "~" (effects (font (size 1.27 1.27))))
          (number "1" (effects (font (size 1.27 1.27)))))
        (pin passive line (at 0 -3.81 90) (length 1.27)
          (name "~" (effects (font (size 1.27 1.27))))
          (number "2" (effects (font (size 1.27 1.27))))))))
  (symbol (lib_id "Device:R") (at 100 100 0) (unit 1)
    (exclude_from_sim no) (in_bom yes) (on_board yes) (dnp no)
    (uuid "bbbbbbbb-1111-2222-3333-444444444444")
    (property "Reference" "R1" (at 102 98 0) (effects (font (size 1.27 1.27))))
    (property "Value" "4.7k" (at 102 102 0) (effects (font (size 1.27 1.27))))
    (pin "1" (uuid "bbbbbbbb-aaaa-2222-3333-444444444401"))
    (pin "2" (uuid "bbbbbbbb-aaaa-2222-3333-444444444402")))
  (symbol (lib_id "Device:R") (at 120 100 0) (unit 1)
    (exclude_from_sim no) (in_bom yes) (on_board yes) (dnp no)
    (uuid "cccccccc-1111-2222-3333-444444444444")
    (property "Reference" "R2" (at 122 98 0) (effects (font (size 1.27 1.27))))
    (property "Value" "10k" (at 122 102 0) (effects (font (size 1.27 1.27))))
    (pin "1" (uuid "cccccccc-aaaa-2222-3333-444444444401"))
    (pin "2" (uuid "cccccccc-aaaa-2222-3333-444444444402")))
  (wire (pts (xy 100 96.19) (xy 120 96.19))
    (stroke (width 0) (type default)) (uuid "dddddddd-1111-2222-3333-444444444401"))
  (wire (pts (xy 100 103.81) (xy 120 103.81))
    (stroke (width 0) (type default)) (uuid "dddddddd-1111-2222-3333-444444444402"))
  (label "TOP" (at 110 96.19 0) (effects (font (size 1.27 1.27)))
    (uuid "eeeeeeee-1111-2222-3333-444444444401"))
  (sheet_instances (path "/" (page "1"))))
'''


def _selfcheck() -> int:  # pragma: no cover - exercised via __main__
    checks = [0]

    def ok(cond: bool, what: str) -> None:
        checks[0] += 1
        status = "ok" if cond else "FAIL"
        print("  [%2d] %-4s %s" % (checks[0], status, what))
        if not cond:
            raise AssertionError(what)

    print("kicad_adapter self-check")

    # ---- 1. hardened parser on canned export --------------------------------
    g = parse_exported_netlist(_CANNED_EXPORT)
    refs = {c.refdes for c in g.components}
    ok(refs == {"R1", "U1", "TP1"},
       "canned: components incl. synthesized TP1 (%s)" % sorted(refs))
    u1 = g.component("U1")
    ok(u1 is not None and [t.name for t in u1.terminals] == ["1", "2", "3"],
       "canned: U1 keeps UNCONNECTED pin 1 (from libpart/units)")
    ok(u1.terminal("1").role == "power_in" and u1.terminal("3").role == "power_out",
       "canned: roles recovered from libpart pin types")
    ok(u1.terminal("2").role == "ground",
       "canned: GND-named pin mapped to role ground")
    ok(u1.terminal("1").function == "VIN",
       "canned: libpart pin name lands in function label only")
    ok(g.component("R1").value == "4.7k (1%)",
       "canned: quoted value with parentheses survives tokenizer")
    names = [n.name for n in g.nets]
    ok(names == ["+3V3", "GND", "N$3"],
       "canned: empty net name falls back to N$<code> (%s)" % names)
    kinds = {n.name: n.kind for n in g.nets}
    ok(kinds["+3V3"] == "power" and kinds["GND"] == "ground",
       "canned: net kind heuristic (power/ground)")
    ok(g.net_of(("TP1", "1")) is not None and g.net_of(("TP1", "1")).name == "GND",
       "canned: undeclared TP1 node kept on its net")
    ok(any("STUBBED" in e for e in g.escalations),
       "canned: STUBBED escalation recorded (no silent defaults)")

    # ---- 2. compare(): identity, tolerance, disagreement --------------------
    def _mk(n_comps: int) -> CanonicalGraph:
        comps = [Component(refdes="R%d" % i, kind="resistor", value="1k",
                           authored=True,
                           terminals=[Terminal("1", "passive"),
                                      Terminal("2", "passive")])
                 for i in range(1, n_comps + 1)]
        nets = [Net(name="N%d" % i, kind="signal", voltage=None,
                    nodes=[("R%d" % i, "1"), ("R%d" % (i % n_comps + 1), "2")])
                for i in range(1, n_comps + 1)]
        return CanonicalGraph(components=comps, nets=nets)

    base = _mk(10)
    r = compare(base, base)
    ok(r["component_overlap"] == 1.0 and r["net_partition_similarity"] == 1.0
       and r["verdict"] == "engines_agree",
       "compare: graph vs itself -> engines_agree(1.0, 1.0)")

    import copy
    with_tp = copy.deepcopy(base)
    with_tp.components.append(Component(
        refdes="TP1", kind="resistor", value="testpoint", authored=True,
        terminals=[Terminal("1", "passive")]))
    with_tp.nets.append(Net(name="TPN", kind="signal", voltage=None,
                            nodes=[("TP1", "1")]))
    r = compare(base, with_tp)
    ok(r["verdict"] == "engines_agree" and 0.9 <= r["component_overlap"] < 1.0,
       "compare: extra testpoint tolerated (overlap=%.3f)" % r["component_overlap"])
    ok(r["components_only_in_b"] == ["TP1"], "compare: diagnostics name the extra part")

    rewired = copy.deepcopy(base)
    for i, net in enumerate(rewired.nets):   # shuffle the partition badly
        net.nodes = [("R%d" % (i + 1), "1"),
                     ("R%d" % ((i + 5) % 10 + 1), "2")]
    r = compare(base, rewired)
    ok(r["verdict"] == "engines_disagree" or r["net_partition_similarity"] < 1.0,
       "compare: rewired partition detected (sim=%.3f)"
       % r["net_partition_similarity"])

    two_a = CanonicalGraph(
        components=[Component("R1", "resistor", "1k", True,
                              [Terminal("1", "passive"), Terminal("2", "passive")]),
                    Component("R2", "resistor", "1k", True,
                              [Terminal("1", "passive"), Terminal("2", "passive")])],
        nets=[Net("A", "signal", None, [("R1", "1"), ("R2", "1")]),
              Net("B", "signal", None, [("R1", "2"), ("R2", "2")])])
    two_b = CanonicalGraph(
        components=two_a.components,
        nets=[Net("X", "signal", None, [("R1", "1"), ("R2", "2")]),
              Net("Y", "signal", None, [("R1", "2"), ("R2", "1")])])
    r = compare(two_a, two_b)
    ok(r["verdict"] == "engines_disagree",
       "compare: crossed 2x2 partition -> engines_disagree (sim=%.3f)"
       % r["net_partition_similarity"])
    ok(compare(CanonicalGraph(), CanonicalGraph())["verdict"] == "engines_agree",
       "compare: two empty graphs vacuously agree")

    # ---- 3. kicad-cli path (optional; degrades gracefully) ------------------
    cli = find_kicad_cli()
    work = os.environ.get(WORKDIR_ENV) or tempfile.mkdtemp(prefix="kicad_adapter_sc_")
    os.makedirs(work, exist_ok=True)

    legacy = os.path.join(work, "legacy_selfcheck.sch")
    with open(legacy, "w", encoding="utf-8") as fh:
        fh.write("EESchema Schematic File Version 4\n")
    try:
        sch_netlist(legacy, kicad_cli=cli)
        legacy_refused = False
    except KicadCliError:
        legacy_refused = True
    ok(legacy_refused, "sch_netlist: legacy v4 .sch REFUSED (silently-empty trap)")

    # Forced-unavailable path (deterministic regardless of installation):
    # with $PATH lookup and fallbacks both defeated, find_kicad_cli() must
    # return None and sch_netlist() must return None -- never raise.
    dummy_sch = os.path.join(work, "dummy_selfcheck.kicad_sch")
    with open(dummy_sch, "w", encoding="utf-8") as fh:
        fh.write("(kicad_sch)\n")
    _orig_which, _orig_fallbacks = shutil.which, KICAD_CLI_FALLBACKS
    try:
        shutil.which = lambda *_a, **_k: None
        globals()["KICAD_CLI_FALLBACKS"] = ()
        ok(find_kicad_cli() is None,
           "find_kicad_cli: returns None when binary absent")
        ok(sch_netlist(dummy_sch) is None,
           "sch_netlist: unavailable toolchain -> None (graceful, no raise)")
    finally:
        shutil.which = _orig_which
        globals()["KICAD_CLI_FALLBACKS"] = _orig_fallbacks

    if cli is None:
        print("  note: %s -- subprocess checks skipped (graceful degradation)"
              % UNAVAILABLE_NOTE)
    else:
        sch_path = os.path.join(work, "probe_selfcheck.kicad_sch")
        with open(sch_path, "w", encoding="utf-8") as fh:
            fh.write(_PROBE_KICAD_SCH)
        exported = sch_netlist(sch_path, kicad_cli=cli, work_dir=work)
        ok(exported is not None, "sch_netlist: kicad-cli export succeeded (%s)" % cli)
        refs = {c.refdes for c in exported.components}
        ok(refs == {"R1", "R2"}, "sch_netlist: components R1,R2 (%s)" % sorted(refs))
        parts = sorted(sorted(tuple(nd) for nd in n.nodes) for n in exported.nets)
        ok(parts == [[("R1", "1"), ("R2", "1")], [("R1", "2"), ("R2", "2")]],
           "sch_netlist: net partition {R1.1,R2.1}/{R1.2,R2.2}")
        neutral = CanonicalGraph(
            components=[
                Component("R1", "resistor", "4.7k", True,
                          [Terminal("1", "passive"), Terminal("2", "passive")]),
                Component("R2", "resistor", "10k", True,
                          [Terminal("1", "passive"), Terminal("2", "passive")]),
            ],
            nets=[Net("TOP", "signal", None, [("R1", "1"), ("R2", "1")]),
                  Net("BOT", "signal", None, [("R1", "2"), ("R2", "2")])])
        r = compare(neutral, exported)
        ok(r["verdict"] == "engines_agree" and r["component_overlap"] == 1.0
           and r["net_partition_similarity"] == 1.0,
           "cross-engine differential: neutral graph vs kicad-cli export agree")

    print("kicad_adapter self-check: OK (%d checks)" % checks[0])
    return 0


if __name__ == "__main__":
    raise SystemExit(_selfcheck())
