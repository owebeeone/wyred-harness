"""Allocation-record oracle for harness v3 — Gen4 spec section 4.2 / 2.3 / 2.5.

``check_allocations(l1_doc, l2_graph, record) -> list[Violation]`` verifies the
solver's Realized allocation view (schema_l1.AllocationRecord) against the
layer-1 intent document (and, conservatively, against a bound layer-2 graph
when one exists). An empty list means the record is clean.

Codes emitted (all Tier-A unless noted):

    ALLOC_UNKNOWN_UNIT     an entry names no real pool unit: unknown pool name,
                           unit index outside 0..unit_count-1, or a demand id
                           that exists nowhere in the document.
    ALLOC_BIJECTION        the demand->unit map is not a legal bijection per
                           pool (a unit double-booked by two demands, a demand
                           served by more entries than its qty, a duplicate
                           entry row), or the mapping is an illegal port-set
                           mapping (the demand's iface does not match the
                           pool's `provides`; or — with an l2 graph that USES
                           the pool's iface vocabulary — the bound design
                           exposes fewer tagged provide terminals than
                           allocated units, so at least one allocated unit has
                           no whole typed port set: bundles swap whole, never
                           split. A graph exposing NO terminal of the iface is
                           unjudgeable — a different binding vocabulary — and
                           is never flagged, the same policy the ERC layer and
                           spec_satisfaction apply. The contract does not
                           require every signature port to carry the iface
                           tag, so the count is per-unit-anchor, not
                           per-signature-port).
    ALLOC_NO_PROVENANCE    an entry's `chosen_by` is not "solver"/"author", its
                           ladder `state` is not free/sticky/pinned, or
                           solver-chosen entries exist with an empty
                           `solver_version` (the re-solve is not re-derivable).
    ALLOC_NONDETERMINISTIC caller supplied a second record (a re-solve); the
                           two canonical serializations are not byte-identical
                           (Gen4 grader floor: deterministic re-solve).
    LOCK_VIOLATION         a decision covered by a LockGroup changed vs the
                           group's snapshot without a `series` bump (section
                           2.5, the taut-gate diff); also mechanical lock
                           metadata breakage: `locked_by` malformed / naming an
                           unknown group / a version the group never reached, a
                           locked entry demoted from `pinned`, a `covers` list
                           naming an unknown decision class (a lock that
                           silently protects nothing), or snapshot/version
                           inconsistency.
    LOCK_UNVERIFIED        warning-violation (F5): the record carries LOCKED
                           groups (version >= 1) but the caller supplied NO
                           external baseline. The embedded snapshot is
                           INFORMATIONAL ONLY — a tamperer who edits a locked
                           entry can rewrite the embedded snapshot to match,
                           so lock checking REQUIRES the external
                           ``snapshot_locks`` baseline the grader retained.
                           Emitted instead of silently passing.
    SERIES_UNJUSTIFIED     (F6) the document's `series` differs from the
                           external baseline's but the doc carries NO
                           ``forked_from`` record naming the baseline series:
                           hand-editing the series string does NOT legalize
                           locked edits — only ``break_lock`` (which records
                           the fork) does.

Lock semantics implemented here (Gen4 section 2.5):

  * ``fire_sync_point(doc, group)``  — locking = batch promotion: every covered
    Realized entry becomes state="pinned", locked_by="<group>@<version+1>", the
    group's version increments and its snapshot freezes the covered entries'
    canonical JSON forms.
  * ``break_lock(doc, group, new_series, reason=...)`` — the ONLY legal way to
    edit a locked decision: forks a new series (board-spin semantics), records
    the fork in ``doc.forked_from = {"series": <old>, "reason": <why>}``,
    demotes the covered entries back to sticky, clears the group snapshot.
  * ``snapshot_locks(doc)`` — an external, diffable snapshot of the doc's
    lock-relevant state ``{series, solver_version, groups}``; feed it back as
    ``baseline=`` (or to ``check_lock_violations``) after edits: covered
    decisions that changed while ``series`` stayed the same are flagged.
    THE EXTERNAL BASELINE IS THE PROTOCOL (F5): the grader retains
    ``snapshot_locks`` output externally at each lock/emit point; the
    embedded per-group snapshot is informational only (it travels with the
    document and can be rewritten alongside a tampered entry). When locked
    groups exist and no baseline is supplied, ``check_allocations`` emits
    LOCK_UNVERIFIED rather than silently passing. A series that differs from
    the baseline is legal only with a ``forked_from`` record naming the
    baseline series (SERIES_UNJUSTIFIED otherwise, F6).

Only the ``pool_allocation`` decision class materializes in the allocation
record; groups covering only other classes (pin_map, footprint, ...) cover no
allocation entries here — those artifacts diff at their own emit points.

Pure Python 3 stdlib. Plain imports only (flat harness directory).
"""

from __future__ import annotations

import json
from typing import Any, Dict, List, Optional, Tuple

import schema_l1 as _l1
from erc import Violation
from schema import CanonicalGraph
from schema_l1 import (
    ALLOC_STATES,
    CHOSEN_BY,
    DECISION_CLASSES,
    AllocationEntry,
    AllocationRecord,
    IntentDoc,
    LockGroup,
)


# ---------------------------------------------------------------------------
# Canonical forms
# ---------------------------------------------------------------------------

def canonical_allocation_json(record: AllocationRecord) -> str:
    """The canonical byte form of an allocation record (entries sorted by
    (pool, unit, demand), lock groups by name — schema_l1's emit order).
    Two deterministic re-solves must compare equal on this string."""
    return json.dumps(_l1._allocation_to_json(record), sort_keys=True)


def _entry_dict(e: AllocationEntry) -> Dict[str, Any]:
    return _l1._alloc_entry_to_json(e)


def _covers_allocations(group: LockGroup) -> bool:
    """Does this group's covers-predicate reach allocation entries?"""
    return "pool_allocation" in group.covers


def covered_entries(record: AllocationRecord,
                    group: LockGroup) -> List[Dict[str, Any]]:
    """The canonical JSON forms of the record entries covered by ``group``,
    sorted by (pool, unit, demand) — exactly the shape a lock snapshot
    freezes. Empty when the group covers no allocation decision class."""
    if not _covers_allocations(group):
        return []
    ents = sorted(record.entries, key=lambda e: (e.pool, e.unit, e.demand))
    return [_entry_dict(e) for e in ents]


# ---------------------------------------------------------------------------
# Lock lifecycle helpers (the section-2.5 machine, mechanical form)
# ---------------------------------------------------------------------------

def fire_sync_point(doc: IntentDoc, group_name: str) -> LockGroup:
    """Fire ``group_name``'s sync point on ``doc``: batch-promote every
    covered entry to pinned with ``locked_by="<group>@<new version>"``, bump
    the group version, freeze the snapshot. Returns the mutated group.
    Raises KeyError for an unknown group."""
    group = None
    for g in doc.allocation.lock_groups:
        if g.name == group_name:
            group = g
            break
    if group is None:
        raise KeyError("no LockGroup named %r" % group_name)
    group.version += 1
    tag = "%s@%d" % (group.name, group.version)
    if _covers_allocations(group):
        for e in doc.allocation.entries:
            e.state = "pinned"
            e.locked_by = tag
    group.snapshot = covered_entries(doc.allocation, group)
    return group


def break_lock(doc: IntentDoc, group_name: str, new_series: str,
               reason: str = "") -> LockGroup:
    """The explicit escape hatch: fork a new series so the group's locked
    decisions may be edited. Records the fork in ``doc.forked_from``
    (``{"series": <old series>, "reason": ...}`` — the F6 series fork record
    an external baseline gate demands), demotes covered entries to sticky,
    clears the embedded snapshot (version history is kept). Raises KeyError
    for an unknown group, ValueError if the series does not actually
    change."""
    if new_series == doc.series:
        raise ValueError("break_lock must fork a NEW series "
                         "(got %r == current)" % new_series)
    group = None
    for g in doc.allocation.lock_groups:
        if g.name == group_name:
            group = g
            break
    if group is None:
        raise KeyError("no LockGroup named %r" % group_name)
    doc.forked_from = {
        "series": doc.series,
        "reason": reason or ("break_lock(%r)" % group_name),
    }
    doc.series = new_series
    tag_prefix = group.name + "@"
    if _covers_allocations(group):
        for e in doc.allocation.entries:
            if e.locked_by is not None and e.locked_by.startswith(tag_prefix):
                e.state = "sticky"
                e.locked_by = None
    group.snapshot = None
    return group


def snapshot_locks(doc: IntentDoc) -> Dict[str, Any]:
    """An external, JSON-safe snapshot of the document's lock-relevant state:

        {"series": ..., "solver_version": ...,
         "groups": {name: {"version": int, "covers": [...],
                           "entries": [canonical covered entry dicts]}}}

    Take one at a lock point (or artifact emit), then diff a later document
    against it with ``check_lock_violations`` / ``diff_locks_against``."""
    rec = doc.allocation
    groups: Dict[str, Any] = {}
    for g in sorted(rec.lock_groups, key=lambda g: g.name):
        groups[g.name] = {
            "version": g.version,
            "covers": sorted(g.covers),
            "entries": covered_entries(rec, g),
        }
    return {
        "series": doc.series,
        "solver_version": rec.solver_version,
        "groups": groups,
    }


# ---------------------------------------------------------------------------
# Diff helpers — entries are keyed by DEMAND (the decision "which unit serves
# demand D" is the thing a lock freezes; ECO semantics).
# ---------------------------------------------------------------------------

def diff_entry_lists(before: List[Dict[str, Any]],
                     after: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Diff two lists of canonical entry dicts, keyed by demand id.

    Returns {"added": [demand...], "removed": [demand...],
             "changed": [demand...]} with each list sorted; all three empty
    means no drift. A demand served by several entries (qty > 1) compares as
    the sorted tuple of its entries."""
    def by_demand(entries: List[Dict[str, Any]]) -> Dict[str, Any]:
        m: Dict[str, List[str]] = {}
        for d in entries:
            key = str(d.get("demand"))
            m.setdefault(key, []).append(json.dumps(d, sort_keys=True))
        return {k: sorted(v) for k, v in m.items()}

    b, a = by_demand(before), by_demand(after)
    added = sorted(k for k in a if k not in b)
    removed = sorted(k for k in b if k not in a)
    changed = sorted(k for k in a if k in b and a[k] != b[k])
    return {"added": added, "removed": removed, "changed": changed}


def diff_lock_snapshots(before: Dict[str, Any],
                        after: Dict[str, Any]) -> Dict[str, Any]:
    """Diff two ``snapshot_locks`` outputs, per group. Returns only the groups
    that differ: {group: {"version": (vb, va), "entries": <diff_entry_lists>,
    "missing": "before"|"after"|None}}. Empty dict == identical lock state."""
    out: Dict[str, Any] = {}
    gb = before.get("groups", {})
    ga = after.get("groups", {})
    for name in sorted(set(gb) | set(ga)):
        b, a = gb.get(name), ga.get(name)
        if b is None or a is None:
            out[name] = {
                "version": (b["version"] if b else None,
                            a["version"] if a else None),
                "entries": diff_entry_lists(b["entries"] if b else [],
                                            a["entries"] if a else []),
                "missing": "before" if b is None else "after",
            }
            continue
        ediff = diff_entry_lists(b["entries"], a["entries"])
        drift = any(ediff.values()) or b["version"] != a["version"]
        if drift:
            out[name] = {"version": (b["version"], a["version"]),
                         "entries": ediff, "missing": None}
    return out


def diff_locks_against(baseline: Dict[str, Any],
                       doc: IntentDoc) -> Dict[str, Any]:
    """Convenience: diff a baseline ``snapshot_locks`` output against the
    current state of ``doc``."""
    return diff_lock_snapshots(baseline, snapshot_locks(doc))


def check_lock_violations(baseline: Dict[str, Any],
                          doc: IntentDoc) -> List[Violation]:
    """The section-2.5 gate against an EXTERNAL baseline snapshot: for every
    group that was LOCKED in the baseline (version >= 1), its covered
    decisions in ``doc`` must be identical unless ``doc.series`` was
    LEGITIMATELY forked. A version rollback or a deleted locked group is
    likewise flagged.

    F6 series fork record: a ``doc.series`` differing from the baseline's is
    legal ONLY when ``doc.forked_from`` names the baseline series (that IS
    ``break_lock``); a differing series without that record is
    SERIES_UNJUSTIFIED — locked edits are never legalized by hand-editing
    the series string."""
    out: List[Violation] = []
    if doc.series != baseline.get("series"):
        ff = doc.forked_from
        if (isinstance(ff, dict)
                and ff.get("series") == baseline.get("series")):
            return out  # legal fork (break_lock): locked edits permitted
        out.append(Violation(
            "SERIES_UNJUSTIFIED",
            "document series %r differs from the baseline series %r but "
            "carries no forked_from record naming the baseline series "
            "(forked_from=%r) — a series fork is legal only through "
            "break_lock, never by hand-editing the series string"
            % (doc.series, baseline.get("series"), ff)))
        return out
    cur = snapshot_locks(doc)
    for name, bg in sorted(baseline.get("groups", {}).items()):
        if bg["version"] < 1:
            continue  # never locked; free to drift
        ag = cur["groups"].get(name)
        if ag is None:
            out.append(Violation(
                "LOCK_VIOLATION",
                "lock group '%s' (locked at v%d) deleted without a series "
                "bump (series still %r)" % (name, bg["version"], doc.series)))
            continue
        if ag["version"] < bg["version"]:
            out.append(Violation(
                "LOCK_VIOLATION",
                "lock group '%s' version rolled back %d -> %d without a "
                "series bump" % (name, bg["version"], ag["version"])))
        ediff = diff_entry_lists(bg["entries"], ag["entries"])
        if any(ediff.values()):
            out.append(Violation(
                "LOCK_VIOLATION",
                "locked decisions in group '%s' changed without a series "
                "bump (series %r): added=%s removed=%s changed=%s"
                % (name, doc.series, ediff["added"], ediff["removed"],
                   ediff["changed"])))
    return out


# ---------------------------------------------------------------------------
# The main check
# ---------------------------------------------------------------------------

def check_allocations(l1_doc: IntentDoc,
                      l2_graph: Optional[CanonicalGraph] = None,
                      record: Optional[AllocationRecord] = None,
                      record2: Optional[AllocationRecord] = None,
                      baseline: Optional[Dict[str, Any]] = None,
                      ) -> List[Violation]:
    """Run the Gen4 section-4.2 allocation checks.

    ``l1_doc``   the layer-1 intent document (pools / demands / lock groups
                 resolve against it).
    ``l2_graph`` the bound layer-2 CanonicalGraph, or None while scoring layer
                 1 standalone. Only a conservative port-set capacity check
                 keys off it (see ALLOC_BIJECTION).
    ``record``   the allocation record under test; None means
                 ``l1_doc.allocation`` (the embedded record).
    ``record2``  a SECOND record from an independent re-solve of the same
                 (document, solver_version); when provided the two canonical
                 serializations must be byte-identical (ALLOC_NONDETERMINISTIC
                 otherwise).
    ``baseline`` an external ``snapshot_locks`` output to gate against
                 (LOCK_VIOLATION on covered drift without a legitimate series
                 fork; SERIES_UNJUSTIFIED on a series differing without a
                 forked_from record). REQUIRED for lock checking: when locked
                 groups exist and baseline is None, LOCK_UNVERIFIED is
                 emitted rather than silently passing (F5 — the embedded
                 snapshot is informational only).

    Returns a deterministic (canonically ordered) list of Violations; empty ==
    clean."""
    rec = record if record is not None else l1_doc.allocation
    out: List[Violation] = []

    pools = {p.name: p for p in l1_doc.pools}
    demands = {}
    for role in l1_doc.roles:
        for d in role.demands:
            demands[d.id] = d

    entries = sorted(rec.entries, key=lambda e: (e.pool, e.unit if
                     isinstance(e.unit, int) else -1, e.demand))

    # -- per-entry legality + provenance ------------------------------------
    unit_users: Dict[Tuple[str, int], List[str]] = {}
    demand_serving: Dict[str, List[Tuple[str, int]]] = {}
    seen_rows: Dict[Tuple[str, Any, str], int] = {}
    solver_chosen = False

    for e in entries:
        where = "entry (pool=%r, unit=%r, demand=%r)" % (e.pool, e.unit,
                                                         e.demand)
        pool = pools.get(e.pool)
        known = True
        if pool is None:
            out.append(Violation(
                "ALLOC_UNKNOWN_UNIT",
                "%s names unknown pool %r" % (where, e.pool)))
            known = False
        elif not (isinstance(e.unit, int) and not isinstance(e.unit, bool)
                  and 0 <= e.unit < pool.unit_count):
            out.append(Violation(
                "ALLOC_UNKNOWN_UNIT",
                "%s: unit %r is not a real unit of pool %r "
                "(valid: 0..%d)" % (where, e.unit, e.pool,
                                    pool.unit_count - 1)))
            known = False
        dem = demands.get(e.demand)
        if dem is None:
            out.append(Violation(
                "ALLOC_UNKNOWN_UNIT",
                "%s names unknown demand %r" % (where, e.demand)))
            known = False

        # provenance / ladder metadata
        if e.chosen_by not in CHOSEN_BY:
            out.append(Violation(
                "ALLOC_NO_PROVENANCE",
                "%s has no valid provenance: chosen_by=%r "
                "(must be one of %s)" % (where, e.chosen_by,
                                         sorted(CHOSEN_BY))))
        elif e.chosen_by == "solver":
            solver_chosen = True
        if e.state not in ALLOC_STATES:
            out.append(Violation(
                "ALLOC_NO_PROVENANCE",
                "%s has invalid ladder state %r (must be one of %s)"
                % (where, e.state, sorted(ALLOC_STATES))))

        if not known:
            continue

        # iface legality: the demanded interface must be what one pool unit's
        # typed port set provides — anything else cannot be a legal port-set
        # bijection.
        if dem.iface != pool.provides:
            out.append(Violation(
                "ALLOC_BIJECTION",
                "%s: illegal port-set mapping — demand %r wants iface %r "
                "but pool %r provides %r" % (where, e.demand, dem.iface,
                                             e.pool, pool.provides)))

        row = (e.pool, e.unit, e.demand)
        seen_rows[row] = seen_rows.get(row, 0) + 1
        unit_users.setdefault((e.pool, e.unit), []).append(e.demand)
        demand_serving.setdefault(e.demand, []).append((e.pool, e.unit))

    if solver_chosen and not rec.solver_version:
        out.append(Violation(
            "ALLOC_NO_PROVENANCE",
            "record has solver-chosen entries but an empty solver_version — "
            "the re-solve is not re-derivable (determinism is defined per "
            "(document, solver_version))"))

    # -- bijection per pool --------------------------------------------------
    for row, n in sorted(seen_rows.items()):
        if n > 1:
            out.append(Violation(
                "ALLOC_BIJECTION",
                "duplicate allocation row (pool=%r, unit=%r, demand=%r) "
                "appears %d times" % (row[0], row[1], row[2], n)))
    for (pname, unit), dlist in sorted(unit_users.items()):
        distinct = sorted(set(dlist))
        if len(distinct) > 1:
            out.append(Violation(
                "ALLOC_BIJECTION",
                "unit %d of pool %r double-booked by demands %s"
                % (unit, pname, distinct)))
    for did, units in sorted(demand_serving.items()):
        dem = demands.get(did)
        qty = dem.qty if dem is not None else 1
        if len(units) > qty:
            out.append(Violation(
                "ALLOC_BIJECTION",
                "demand %r (qty=%d) is served by %d entries: %s"
                % (did, qty, len(units), sorted(units))))

    # -- conservative layer-2 port-set capacity (bundles swap whole) ---------
    # A bound design need not tag EVERY signature port with the pool's iface
    # (EMIT_CONTRACT only requires the provide/require anchors); the sound
    # lower bound is one tagged provide terminal per allocated unit. A graph
    # with NO terminal of the iface at all used a different binding
    # vocabulary and is unjudgeable here (same policy as spec_satisfaction's
    # _share_iface_net and ERC's R1) — never flagged.
    if l2_graph is not None:
        provide_count: Dict[str, int] = {}
        for c in l2_graph.components:
            for t in c.terminals:
                if t.iface is not None and t.iface_member == "provide":
                    provide_count[t.iface] = provide_count.get(t.iface, 0) + 1
        for pname in sorted({e.pool for e in entries if e.pool in pools}):
            pool = pools[pname]
            if not pool.port_signature:
                continue
            alloc_units = {e.unit for e in entries
                           if e.pool == pname and isinstance(e.unit, int)
                           and not isinstance(e.unit, bool)
                           and 0 <= e.unit < pool.unit_count}
            needed = len(alloc_units)
            have = provide_count.get(pool.provides, 0)
            if needed and have and have < needed:
                out.append(Violation(
                    "ALLOC_BIJECTION",
                    "illegal port-set mapping: pool %r allocates %d unit(s) "
                    "(signature %s) but the bound graph exposes only %d %r "
                    "provide terminal(s) — at least one allocated unit has "
                    "no port set (bundles swap whole, never split)"
                    % (pname, needed, pool.port_signature, have,
                       pool.provides)))

    # -- determinism: two re-solves must be byte-identical -------------------
    if record2 is not None:
        s1 = canonical_allocation_json(rec)
        s2 = canonical_allocation_json(record2)
        if s1 != s2:
            ediff = diff_entry_lists([_entry_dict(e) for e in rec.entries],
                                     [_entry_dict(e) for e in record2.entries])
            out.append(Violation(
                "ALLOC_NONDETERMINISTIC",
                "two re-solves of the same document are not byte-identical "
                "(added=%s removed=%s changed=%s; solver_version %r vs %r)"
                % (ediff["added"], ediff["removed"], ediff["changed"],
                   rec.solver_version, record2.solver_version)))

    # -- lock metadata + intra-document snapshot gate -------------------------
    groups = {g.name: g for g in rec.lock_groups}
    for g in sorted(rec.lock_groups, key=lambda g: g.name):
        for cls in g.covers:
            if cls not in DECISION_CLASSES:
                out.append(Violation(
                    "LOCK_VIOLATION",
                    "lock group %r covers unknown decision class %r (known: "
                    "%s) — the lock silently protects nothing"
                    % (g.name, cls, sorted(DECISION_CLASSES))))
        if g.version < 1 and g.snapshot is not None:
            out.append(Violation(
                "LOCK_VIOLATION",
                "lock group %r has a snapshot but version %d (0 == never "
                "locked): inconsistent lock metadata" % (g.name, g.version)))
        if g.version >= 1 and g.snapshot is not None:
            ediff = diff_entry_lists(g.snapshot, covered_entries(rec, g))
            if any(ediff.values()):
                out.append(Violation(
                    "LOCK_VIOLATION",
                    "decisions covered by lock group %r@%d changed vs its "
                    "snapshot without a series bump (series %r): added=%s "
                    "removed=%s changed=%s — editing a locked decision "
                    "requires break_lock -> new series"
                    % (g.name, g.version, l1_doc.series, ediff["added"],
                       ediff["removed"], ediff["changed"])))

    for e in entries:
        if e.locked_by is None:
            continue
        where = "entry (pool=%r, unit=%r, demand=%r)" % (e.pool, e.unit,
                                                         e.demand)
        gname, sep, ver = e.locked_by.rpartition("@")
        if sep != "@" or not gname or not ver.isdigit():
            out.append(Violation(
                "LOCK_VIOLATION",
                "%s has malformed locked_by %r (expected '<group>@<version>')"
                % (where, e.locked_by)))
            continue
        g = groups.get(gname)
        if g is None:
            out.append(Violation(
                "LOCK_VIOLATION",
                "%s locked_by %r names unknown lock group %r"
                % (where, e.locked_by, gname)))
            continue
        if not 1 <= int(ver) <= g.version:
            out.append(Violation(
                "LOCK_VIOLATION",
                "%s locked_by %r: group %r has never reached version %s "
                "(current version %d)" % (where, e.locked_by, gname, ver,
                                          g.version)))
        if e.state != "pinned":
            out.append(Violation(
                "LOCK_VIOLATION",
                "%s is locked (%r) but state is %r, not 'pinned' — a locked "
                "decision was demoted without break_lock"
                % (where, e.locked_by, e.state)))

    # -- external baseline gate (F5: the baseline IS the protocol) -----------
    if baseline is not None:
        out.extend(check_lock_violations(baseline, l1_doc))
    else:
        locked = sorted(g.name for g in rec.lock_groups if g.version >= 1)
        if locked:
            out.append(Violation(
                "LOCK_UNVERIFIED",
                "warning: lock group(s) %s are locked (version >= 1) but no "
                "EXTERNAL baseline was supplied — the embedded snapshot is "
                "informational only (it can be rewritten alongside a "
                "tampered entry); pass the grader-retained snapshot_locks() "
                "output as baseline= to actually verify the locks"
                % ", ".join("'%s'" % n for n in locked)))

    return out


__all__ = [
    "check_allocations",
    "check_lock_violations",
    "snapshot_locks",
    "diff_lock_snapshots",
    "diff_locks_against",
    "diff_entry_lists",
    "covered_entries",
    "canonical_allocation_json",
    "fire_sync_point",
    "break_lock",
    "Violation",
]


# ---------------------------------------------------------------------------
# self-check:  python allocation.py
# ---------------------------------------------------------------------------

if __name__ == "__main__":  # pragma: no cover
    import sys

    from schema import Component, Terminal
    from schema_l1 import (Demand, Pool, Role, from_json, to_json)

    failures = []

    def check(label, cond):
        print("  [%s] %s" % ("ok " if cond else "FAIL", label))
        if not cond:
            failures.append(label)

    def codes(violations):
        return sorted(v.code for v in violations)

    def base_doc() -> IntentDoc:
        return IntentDoc(
            series="A",
            roles=[
                Role(id="mcu", kind="mcu",
                     demands=[Demand(id="mcu.pwr", iface="power", volts=3.3)]),
                Role(id="periph", kind="uart_device",
                     demands=[Demand(id="periph.pwr", iface="power",
                                     volts=3.3),
                              Demand(id="periph.uart", iface="uart")]),
                Role(id="periph2", kind="uart_device",
                     demands=[Demand(id="periph2.uart", iface="uart"),
                              Demand(id="periph2.i2c", iface="i2c")]),
            ],
            rails=[_l1.Rail(name="+3V3", volts=3.3)],
            grounds=[_l1.Ground(name="GND")],
            pools=[Pool(name="mcu.uarts", role="mcu", provides="uart",
                        unit_count=3, port_signature=["tx:out", "rx:in"])],
            allocation=AllocationRecord(
                entries=[
                    AllocationEntry(pool="mcu.uarts", unit=0,
                                    demand="periph.uart",
                                    chosen_by="solver", state="sticky"),
                    AllocationEntry(pool="mcu.uarts", unit=1,
                                    demand="periph2.uart",
                                    chosen_by="author", state="pinned"),
                ],
                lock_groups=[LockGroup(name="firmware-facing",
                                       covers=["pool_allocation",
                                               "pin_map"])],
                solver_version="trivial-1",
            ),
        )

    def copy_doc(doc: IntentDoc) -> IntentDoc:
        return from_json(to_json(doc))

    print("allocation.py self-check")

    # 1. clean record
    doc = base_doc()
    check("clean record -> no violations",
          check_allocations(doc, None, doc.allocation) == [])

    # 2-4. ALLOC_UNKNOWN_UNIT
    d = copy_doc(doc)
    d.allocation.entries[0].pool = "nope.pool"
    check("unknown pool -> ALLOC_UNKNOWN_UNIT",
          "ALLOC_UNKNOWN_UNIT" in codes(check_allocations(d, None,
                                                          d.allocation)))
    d = copy_doc(doc)
    d.allocation.entries[0].unit = 3
    check("unit out of range -> ALLOC_UNKNOWN_UNIT",
          "ALLOC_UNKNOWN_UNIT" in codes(check_allocations(d, None,
                                                          d.allocation)))
    d = copy_doc(doc)
    d.allocation.entries[0].demand = "ghost.demand"
    check("unknown demand -> ALLOC_UNKNOWN_UNIT",
          "ALLOC_UNKNOWN_UNIT" in codes(check_allocations(d, None,
                                                          d.allocation)))

    # 5-7. ALLOC_BIJECTION
    d = copy_doc(doc)
    d.allocation.entries[1].unit = 0  # both demands on unit 0
    check("unit double-booked -> ALLOC_BIJECTION",
          "ALLOC_BIJECTION" in codes(check_allocations(d, None,
                                                       d.allocation)))
    d = copy_doc(doc)
    d.allocation.entries.append(
        AllocationEntry(pool="mcu.uarts", unit=2, demand="periph.uart",
                        chosen_by="solver", state="sticky"))
    check("demand double-served (qty=1) -> ALLOC_BIJECTION",
          "ALLOC_BIJECTION" in codes(check_allocations(d, None,
                                                       d.allocation)))
    d = copy_doc(doc)
    d.allocation.entries[0].demand = "periph2.i2c"  # i2c demand on uart pool
    check("iface mismatch -> ALLOC_BIJECTION (illegal port-set mapping)",
          "ALLOC_BIJECTION" in codes(check_allocations(d, None,
                                                       d.allocation)))

    # 8-9. ALLOC_NO_PROVENANCE
    d = copy_doc(doc)
    d.allocation.entries[0].chosen_by = "vibes"
    check("bad chosen_by -> ALLOC_NO_PROVENANCE",
          "ALLOC_NO_PROVENANCE" in codes(check_allocations(d, None,
                                                           d.allocation)))
    d = copy_doc(doc)
    d.allocation.solver_version = ""
    check("solver entries + empty solver_version -> ALLOC_NO_PROVENANCE",
          "ALLOC_NO_PROVENANCE" in codes(check_allocations(d, None,
                                                           d.allocation)))

    # 10. ALLOC_NONDETERMINISTIC
    d = copy_doc(doc)
    d2 = copy_doc(doc)
    d2.allocation.entries[0].unit = 2  # a re-solve that drifted
    check("differing re-solve -> ALLOC_NONDETERMINISTIC",
          "ALLOC_NONDETERMINISTIC" in codes(
              check_allocations(d, None, d.allocation,
                                record2=d2.allocation)))
    d3 = copy_doc(doc)
    d3.allocation.entries.reverse()  # same record, scrambled order
    check("order-scrambled identical re-solve -> clean",
          check_allocations(d, None, d.allocation,
                            record2=d3.allocation) == [])

    # 11. fire_sync_point: batch promotion + still clean
    d = copy_doc(doc)
    g = fire_sync_point(d, "firmware-facing")
    promoted = all(e.state == "pinned" and
                   e.locked_by == "firmware-facing@1"
                   for e in d.allocation.entries)
    check("fire_sync_point promotes covered entries (pinned, tagged, v1)",
          promoted and g.version == 1 and g.snapshot is not None)
    baseline0 = snapshot_locks(d)
    check("locked document + external baseline is clean",
          check_allocations(d, None, d.allocation, baseline=baseline0) == [])
    check("locked document WITHOUT a baseline -> LOCK_UNVERIFIED (F5)",
          codes(check_allocations(d, None, d.allocation))
          == ["LOCK_UNVERIFIED"])

    # 12. intra-document snapshot drift
    baseline = snapshot_locks(d)
    tampered = copy_doc(d)
    tampered.allocation.entries[0].unit = 2  # edit a locked decision
    v = check_allocations(tampered, None, tampered.allocation)
    check("editing a locked decision (same series) -> LOCK_VIOLATION",
          "LOCK_VIOLATION" in codes(v))

    # 13. external baseline gate; series bump legalizes the edit
    check("baseline gate flags the same tamper",
          "LOCK_VIOLATION" in codes(check_lock_violations(baseline,
                                                          tampered)))
    forked = copy_doc(d)
    break_lock(forked, "firmware-facing", "B")
    forked.allocation.entries[0].unit = 2  # now a legal edit
    check("break_lock records the series fork (forked_from)",
          forked.forked_from is not None
          and forked.forked_from.get("series") == "A"
          and bool(forked.forked_from.get("reason")))
    check("break_lock -> new series: edit passes the baseline gate",
          check_lock_violations(baseline, forked) == [])
    check("forked document + baseline is clean",
          check_allocations(forked, None, forked.allocation,
                            baseline=baseline) == [])
    forged = copy_doc(d)
    forged.allocation.entries[0].unit = 2
    forged.series = "B"                    # hand-edited, NO forked_from
    check("hand-edited series without forked_from -> SERIES_UNJUSTIFIED (F6)",
          codes(check_lock_violations(baseline, forged))
          == ["SERIES_UNJUSTIFIED"])
    forged.forked_from = {"series": "Z", "reason": "made up"}
    check("forked_from naming the WRONG series -> SERIES_UNJUSTIFIED",
          codes(check_lock_violations(baseline, forged))
          == ["SERIES_UNJUSTIFIED"])

    # 14. diff helpers
    dd = diff_locks_against(baseline, tampered)
    check("diff helper reports the changed demand",
          dd.get("firmware-facing", {}).get("entries", {}).get("changed")
          == ["periph.uart"])
    check("diff of identical snapshots is empty",
          diff_lock_snapshots(baseline, snapshot_locks(d)) == {})

    # 15. conservative l2 port-set capacity check
    def uart_graph(tagged_terminals: int) -> CanonicalGraph:
        """A graph exposing N terminals tagged (iface=uart, provide)."""
        terms = []
        for u in range(tagged_terminals):
            terms.append(Terminal(name="UP%d" % u, role="signal",
                                  iface="uart", iface_member="provide"))
        return CanonicalGraph(components=[
            Component(refdes="U1", kind="mcu", value="MCU", authored=True,
                      terminals=terms)])

    d = copy_doc(doc)  # allocates units 0 and 1 (needs >= 2 provide anchors)
    check("l2 graph with a provide anchor per allocated unit -> clean",
          check_allocations(d, uart_graph(4), d.allocation) == [])
    check("l2 graph with fewer provide terminals than allocated units "
          "-> ALLOC_BIJECTION",
          "ALLOC_BIJECTION" in codes(check_allocations(d, uart_graph(1),
                                                       d.allocation)))
    check("l2 graph with NO uart-tagged terminal is unjudgeable -> clean "
          "(different binding vocabulary)",
          check_allocations(d, uart_graph(0), d.allocation) == [])

    # 16. lock metadata breakage
    d = copy_doc(doc)
    d.allocation.entries[1].locked_by = "no-such-group@1"
    check("locked_by naming unknown group -> LOCK_VIOLATION",
          "LOCK_VIOLATION" in codes(check_allocations(d, None,
                                                      d.allocation)))
    d = copy_doc(doc)
    fire_sync_point(d, "firmware-facing")
    d.allocation.entries[0].state = "sticky"  # demote a locked entry
    check("locked entry demoted from pinned -> LOCK_VIOLATION",
          "LOCK_VIOLATION" in codes(check_allocations(d, None,
                                                      d.allocation)))
    d = copy_doc(doc)
    d.allocation.lock_groups[0].covers = ["pool_allocation", "vibes"]
    check("unknown decision class in covers -> LOCK_VIOLATION",
          "LOCK_VIOLATION" in codes(check_allocations(d, None,
                                                      d.allocation)))

    # 17. record defaults to the embedded one
    d = copy_doc(doc)
    d.allocation.entries[0].pool = "nope.pool"
    check("record=None uses l1_doc.allocation",
          "ALLOC_UNKNOWN_UNIT" in codes(check_allocations(d)))

    print()
    if failures:
        print("RESULT: FAIL (%d)" % len(failures))
        sys.exit(1)
    print("RESULT: PASS (all checks passed)")
    sys.exit(0)
