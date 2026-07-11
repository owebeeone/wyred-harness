"""LVS-lite structural equivalence for the connectivity-oracle harness.

``equivalent(g1, g2) -> bool`` decides whether two CanonicalGraphs describe the
SAME circuit up to naming.  Per SPEC this is a "Layout-Versus-Schematic"-lite
isomorphism:

    equivalent iff there is a refdes bijection that

        * preserves each component's (kind, value, logic_fn, authored, salient
          attrs, and the multiset of STRUCTURED terminal signatures --
          role + req_v + prov_v + iface + iface_member), and
        * induces a matching partition of terminal-nodes into nets.

Three things are deliberately IGNORED:

    * net NAMES               (a net is only its set of member terminal-nodes),
    * generated-refdes NAMES  (authored=False companions are matched by
                               kind + value + connection signature, never by
                               their auto-assigned refdes like "C1"/"R2"),
    * terminal ``function`` strings (a human label only; ALL electrical meaning
                               lives in the structured Terminal fields, so two
                               netlists that differ only in function text -- an
                               "off-label" emit -- still compare equivalent).

The problem is graph isomorphism, which is worst-case hard, but the harness
graphs are tiny and highly typed.  We therefore:

    1. cheaply reject on multiset invariants (component-signature multiset,
       net-shape multiset),
    2. compute a stable per-component colour via iterated colour refinement
       (a Weisfeiler-Lehman style fixpoint over the component<->net incidence),
    3. backtrack-search for a full bijection, using the colours to prune the
       candidate set for each component and checking net consistency
       incrementally.

Pure Python 3 stdlib.  Plain imports only (flat directory).
"""

from __future__ import annotations

import itertools
from collections import defaultdict
from typing import Any, Dict, List, Optional, Tuple

from schema import CanonicalGraph, Component


# ---------------------------------------------------------------------------
# What counts as "salient" for matching.
#
# We compare the FULL attrs dict except for a small deny-list of keys that are
# purely cosmetic bookkeeping and reference generated refdes NAMES (which we are
# explicitly told to ignore).  In particular companion parts carry
# attrs["for"] = "<owner refdes>" and attrs["role"] = "decoupling"/... .  The
# "role" is salient (it distinguishes a decoupling cap from a bootstrap cap),
# but "for" points at a possibly-renamed owner refdes, so it must NOT be part of
# the raw signature -- the owner relationship is instead captured structurally
# by the net partition / colour refinement.
# ---------------------------------------------------------------------------

_ATTR_IGNORE = frozenset({"for"})


def _norm_attr_value(v: Any) -> Any:
    """Make an attr value hashable & order-independent for signature use."""
    if isinstance(v, dict):
        return tuple(sorted((k, _norm_attr_value(x)) for k, x in v.items()))
    if isinstance(v, (list, tuple)):
        return tuple(_norm_attr_value(x) for x in v)
    if isinstance(v, bool):
        # keep bools distinct from ints in the signature
        return ("bool", v)
    if isinstance(v, float):
        # 48.0 and 48 should compare equal; canonicalise integral floats
        if v.is_integer():
            return ("num", float(v))
        return ("num", float(v))
    if isinstance(v, int):
        return ("num", float(v))
    return v


def _salient_attrs(c: Component) -> Tuple:
    items = []
    for k in sorted(c.attrs.keys()):
        if k in _ATTR_IGNORE:
            continue
        items.append((k, _norm_attr_value(c.attrs[k])))
    return tuple(items)


def _terminal_sig(t) -> Tuple:
    """Naming-independent STRUCTURED signature of one terminal.

    Matches on the structured electrical fields the ERC layer keys off --
    role + req_v + prov_v + iface + iface_member -- and DELIBERATELY IGNORES the
    free-form ``function`` string (a human label only) and the terminal ``name``
    (a per-component-internal label a valid relabelling may permute).
    """
    def _v(x):
        # canonicalise a nullable voltage to a sortable string tag
        return "-" if x is None else ("v%g" % float(x))

    def _s(x):
        return "-" if x is None else str(x)

    return (
        _s(t.role),
        _v(t.req_v),
        _v(t.prov_v),
        _s(t.iface),
        _s(t.iface_member),
    )


def _terminal_role_multiset(c: Component) -> Tuple[Tuple, ...]:
    """Multiset of the structured terminal signatures over the component.

    Terminal NAMES and function strings are ignored (see :func:`_terminal_sig`);
    the structured electrical fields carry the meaning the ERC layer keys off.
    """
    pairs = sorted(_terminal_sig(t) for t in c.terminals)
    return tuple(pairs)


def _base_signature(c: Component) -> Tuple:
    """The naming-independent identity of a component.

    Two components may be swapped by the bijection only if their base
    signatures are equal.  Deliberately excludes refdes (and, for companions,
    excludes the auto-refdes) but includes everything electrically salient.
    """
    return (
        c.kind,
        c.value,
        c.logic_fn,
        bool(c.authored),
        _terminal_role_multiset(c),
        _salient_attrs(c),
    )


# ---------------------------------------------------------------------------
# Node / net helpers
# ---------------------------------------------------------------------------

def _node_key(node) -> Tuple[str, str]:
    return (node[0], node[1])


def _terminal_role_map(c: Component) -> Dict[str, Tuple]:
    """terminal-name -> structured terminal signature for one component.

    The signature is the naming-independent electrical identity of the terminal
    (see :func:`_terminal_sig`); the ``function`` string is ignored.
    """
    return {t.name: _terminal_sig(t) for t in c.terminals}


def _build_indices(g: CanonicalGraph):
    """Return (by_refdes, net_of_node, terminal_role_of_node).

    net_of_node maps (refdes, term) -> net index.  A terminal that appears on no
    declared net is treated as its own singleton net so that "unconnected here"
    vs "connected there" is itself a distinguishing feature.
    """
    by_refdes: Dict[str, Component] = {c.refdes: c for c in g.components}

    net_of_node: Dict[Tuple[str, str], int] = {}
    for i, net in enumerate(g.nets):
        for node in net.nodes:
            net_of_node[_node_key(node)] = i

    # role/function per node (only for nodes whose component/terminal exist)
    role_of_node: Dict[Tuple[str, str], Tuple[str, str]] = {}
    for c in g.components:
        rm = _terminal_role_map(c)
        for tname, rf in rm.items():
            role_of_node[(c.refdes, tname)] = rf

    return by_refdes, net_of_node, role_of_node


# ---------------------------------------------------------------------------
# Cheap global invariants (fast reject before the expensive search)
# ---------------------------------------------------------------------------

def _component_signature_multiset(g: CanonicalGraph):
    d = defaultdict(int)
    for c in g.components:
        d[_base_signature(c)] += 1
    return d


def _net_shape_multiset(g: CanonicalGraph, role_of_node):
    """Multiset of net shapes.

    A net's naming-independent shape is the sorted multiset of the STRUCTURED
    terminal signature (role + req_v + prov_v + iface + iface_member; NOT the
    function string) of every member terminal-node, plus its size. Net
    kind/voltage are also salient (a power net at 3.3V differs from one at 5V),
    so include them.
    """
    d = defaultdict(int)
    for net in g.nets:
        members = []
        for node in net.nodes:
            rf = role_of_node.get(_node_key(node), ("?",))
            members.append(rf)
        members.sort()
        shape = (
            net.kind,
            _norm_attr_value(net.voltage) if net.voltage is not None else None,
            tuple(members),
        )
        d[shape] += 1
    return d


# ---------------------------------------------------------------------------
# Colour refinement (Weisfeiler-Lehman style) over component<->net incidence.
#
# Each component starts coloured by its base signature.  Each net starts
# coloured by its (kind, voltage) plus the multiset of (member-role, member's
# current component-colour, member terminal role/function).  We iterate:
#
#   * a net's new colour folds in the multiset of its incident component colours
#     (tagged by the connecting terminal's role/function),
#   * a component's new colour folds in the multiset of colours of the nets its
#     terminals sit on (tagged by that terminal's role/function),
#
# until the component-colour partition stabilises.  These stable colours are a
# necessary condition for two components to be matchable and dramatically prune
# the backtracking search.
# ---------------------------------------------------------------------------

class _Incidence:
    """Static per-graph structure the colour refinement folds over.

    ``comp_terms[refdes]`` = list of (terminal role, function, LOCAL net index).
    ``net_members[i]``      = list of (refdes-or-None, terminal role, function).
    Net indices [0, n_nets) are declared nets; [n_nets, total_nets) are the
    per-terminal singleton nets synthesised for unconnected terminals (they
    carry no members, since their sole terminal is captured on the component
    side).  ``net_base[i]`` is the naming-independent seed colour of net i.
    """

    __slots__ = ("refdeses", "comp_terms", "net_members", "net_base",
                 "n_nets", "total_nets")

    def __init__(self, g: CanonicalGraph, role_of_node):
        comps = g.components
        nets = g.nets
        self.n_nets = len(nets)
        self.refdeses = [c.refdes for c in comps]

        net_index_of: Dict[Tuple[str, str], int] = {}
        for i, net in enumerate(nets):
            for node in net.nodes:
                net_index_of[_node_key(node)] = i

        singleton_counter = self.n_nets
        self.comp_terms: Dict[str, List[Tuple[str, str, int]]] = {}
        for c in comps:
            tlist = []
            for t in c.terminals:
                key = (c.refdes, t.name)
                if key in net_index_of:
                    ni = net_index_of[key]
                else:
                    ni = singleton_counter
                    singleton_counter += 1
                # carry the STRUCTURED terminal signature (not the function
                # string) as the terminal's colour tag on the incidence edge.
                tlist.append((t.role, _terminal_sig(t), ni))
            self.comp_terms[c.refdes] = tlist
        self.total_nets = singleton_counter

        known = set(self.refdeses)
        # each member: (refdes-or-None, role, structured-terminal-signature)
        self.net_members: List[List[Tuple[Optional[str], str, Tuple]]] = []
        _UNKNOWN_SIG = ("?",)
        for net in nets:
            members = []
            for node in net.nodes:
                r, t = _node_key(node)
                rf = role_of_node.get((r, t))
                role = rf[0] if rf is not None else "?"
                sig = rf if rf is not None else _UNKNOWN_SIG
                members.append((r if r in known else None, role, sig))
            self.net_members.append(members)
        for _ in range(self.n_nets, self.total_nets):
            self.net_members.append([])

        # seed colours as STRINGS so they compare across graphs by value.
        self.net_base: List[str] = []
        for net in nets:
            vol = (_norm_attr_value(net.voltage)
                   if net.voltage is not None else None)
            self.net_base.append(str((net.kind, vol)))
        for _ in range(self.n_nets, self.total_nets):
            self.net_base.append("SINGLETON")


def _refine_pair(g1, role1, g2, role2):
    """Colour-refine g1 and g2 in ONE shared namespace.

    Returns ``(col1, col2)`` where each maps refdes -> integer colour and the
    integers are COMPARABLE across the two graphs: two components (in either
    graph) share an integer iff colour refinement could not tell them apart.
    Refining together is what makes the cross-graph comparison in ``equivalent``
    sound -- per-graph integer ids would be meaningless to compare.
    """
    inc1 = _Incidence(g1, role1)
    inc2 = _Incidence(g2, role2)
    return _refine_pair_locked(g1, inc1, g2, inc2)


def _net_raw(inc, comp_colour, net_colour):
    net_raw = []
    for i in range(inc.total_nets):
        base = net_colour[i]
        if i < inc.n_nets:
            incident = sorted(
                (rr, rf, comp_colour[ref])
                for (ref, rr, rf) in inc.net_members[i]
                if ref is not None
            )
            phantoms = sorted(
                (rr, rf)
                for (ref, rr, rf) in inc.net_members[i]
                if ref is None
            )
            net_raw.append((i, (base, tuple(incident), tuple(phantoms))))
        else:
            net_raw.append((i, (base, (), ())))
    return net_raw


def _comp_raw(g, inc, comp_colour, net_colour):
    comp_raw = []
    for refdes in inc.refdeses:
        base = comp_colour[refdes]
        incident = sorted(
            (role, fn, net_colour[ni])
            for (role, fn, ni) in inc.comp_terms[refdes]
        )
        comp_raw.append((refdes, (base, tuple(incident))))
    return comp_raw


def _refine_pair_locked(g1, inc1, g2, inc2):
    """Step-locked WL refinement of two graphs sharing one colour namespace."""

    def compress_shared(items_a, items_b):
        """Compress raw colours from BOTH graphs through one order dict, so the
        resulting integer colours are comparable across the two graphs."""
        order: Dict[Any, int] = {}
        out_a: Dict[Any, int] = {}
        out_b: Dict[Any, int] = {}
        for key, raw in items_a:
            cid = order.get(raw)
            if cid is None:
                cid = len(order)
                order[raw] = cid
            out_a[key] = cid
        for key, raw in items_b:
            cid = order.get(raw)
            if cid is None:
                cid = len(order)
                order[raw] = cid
            out_b[key] = cid
        return out_a, out_b

    # seed
    comp1, comp2 = compress_shared(
        ((c.refdes, str(_base_signature(c))) for c in g1.components),
        ((c.refdes, str(_base_signature(c))) for c in g2.components),
    )
    net1, net2 = compress_shared(
        ((i, b) for i, b in enumerate(inc1.net_base)),
        ((i, b) for i, b in enumerate(inc2.net_base)),
    )

    prev_classes = -1
    max_rounds = (len(g1.components) + inc1.total_nets +
                  len(g2.components) + inc2.total_nets + 2)
    for _ in range(max_rounds):
        net1, net2 = compress_shared(
            _net_raw(inc1, comp1, net1),
            _net_raw(inc2, comp2, net2),
        )
        comp1, comp2 = compress_shared(
            _comp_raw(g1, inc1, comp1, net1),
            _comp_raw(g2, inc2, comp2, net2),
        )
        num_classes = len(set(comp1.values()) | set(comp2.values()))
        if num_classes == prev_classes:
            break
        prev_classes = num_classes

    return comp1, comp2


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def equivalent(g1: CanonicalGraph, g2: CanonicalGraph) -> bool:
    """Return True iff g1 and g2 are the same circuit up to naming (LVS-lite)."""
    # ---- 0. trivial size checks --------------------------------------------
    if len(g1.components) != len(g2.components):
        return False
    if len(g1.nets) != len(g2.nets):
        return False

    _, _, role1 = _build_indices(g1)
    _, _, role2 = _build_indices(g2)

    # ---- 1. cheap multiset invariants --------------------------------------
    if _component_signature_multiset(g1) != _component_signature_multiset(g2):
        return False
    if _net_shape_multiset(g1, role1) != _net_shape_multiset(g2, role2):
        return False

    # ---- 2. colour refinement (shared namespace -> comparable colours) ------
    col1, col2 = _refine_pair(g1, role1, g2, role2)

    # The multiset of stable colours must match, else no bijection exists.
    def colour_multiset(col):
        d = defaultdict(int)
        for v in col.values():
            d[v] += 1
        return d

    if colour_multiset(col1) != colour_multiset(col2):
        return False

    # candidate map: for each g1 component, the g2 components sharing its colour.
    by_colour2: Dict[int, List[str]] = defaultdict(list)
    for ref, v in col2.items():
        by_colour2[v].append(ref)

    candidates: Dict[str, List[str]] = {}
    for c in g1.components:
        cand = list(by_colour2[col1[c.refdes]])
        if not cand:
            return False
        candidates[c.refdes] = cand

    # ---- 3. backtracking isomorphism search --------------------------------
    return _search_bijection(g1, g2, candidates, role1, role2)


# ---------------------------------------------------------------------------
# Backtracking search: find a refdes bijection g1 -> g2 consistent with the
# net partition.
#
# Net-partition consistency: two g1 nodes on the same net must map to two g2
# nodes on the same net, and vice-versa.  We enforce this incrementally.  As we
# assign refdes r1 -> r2, every terminal (r1, tname) maps to some (r2, tname').
# Terminal NAMES may differ between the two graphs, so for each assigned
# component we must ALSO choose a terminal permutation -- but terminals are
# matched by (role, function): within a component, terminals sharing the same
# (role, function) are interchangeable, so we resolve the terminal mapping
# lazily via the net constraints rather than committing a permutation up front.
#
# Concretely we track a union between g1-net-ids and g2-net-ids: when a g1 node
# and its image g2 node are placed, their nets must correspond 1:1.  We build
# the net correspondence as a partial bijection and reject on conflict.
# ---------------------------------------------------------------------------

def _search_bijection(g1, g2, candidates, role1, role2) -> bool:
    comps1 = g1.components

    # net id lookups
    net_of_1: Dict[Tuple[str, str], int] = {}
    for i, net in enumerate(g1.nets):
        for node in net.nodes:
            net_of_1[_node_key(node)] = i
    net_of_2: Dict[Tuple[str, str], int] = {}
    for i, net in enumerate(g2.nets):
        for node in net.nodes:
            net_of_2[_node_key(node)] = i

    # structured-terminal-signature -> terminal names, per component (for the
    # terminal-permutation matching). Function strings are NOT part of the key:
    # two terminals are interchangeable iff their structured signatures match.
    def rf_terminals(comp: Component) -> Dict[Tuple, List[str]]:
        d: Dict[Tuple, List[str]] = defaultdict(list)
        for t in comp.terminals:
            d[_terminal_sig(t)].append(t.name)
        return d

    rf_terms1 = {c.refdes: rf_terminals(c) for c in g1.components}
    rf_terms2 = {c.refdes: rf_terminals(c) for c in g2.components}

    # order g1 components by fewest candidates first (most-constrained variable)
    order = sorted(comps1, key=lambda c: len(candidates[c.refdes]))

    used2: set = set()                    # g2 refdes already taken
    # net correspondence, built incrementally, must stay a bijection
    net_map: Dict[Any, Any] = {}          # g1 net id -> g2 net id
    net_rev: Dict[Any, Any] = {}          # g2 net id -> g1 net id

    def netid1(refdes, name):
        return net_of_1.get((refdes, name), ("s1", refdes, name))

    def netid2(refdes, name):
        return net_of_2.get((refdes, name), ("s2", refdes, name))

    def undo(journal):
        for (kind, key) in reversed(journal):
            if kind == "fwd":
                net_map.pop(key, None)
            else:
                net_rev.pop(key, None)

    def apply_pairing(r1, r2, pairing, journal) -> bool:
        """Apply one full terminal pairing (dict g1-term-name -> g2-term-name)
        for r1->r2 as net-link constraints.  On any conflict, roll back the
        additions made SO FAR (leaving the caller's journal empty of them) and
        return False.  On success, additions are recorded in `journal`.
        """
        local: list = []
        for tname1, tname2 in pairing.items():
            na = netid1(r1, tname1)
            nb = netid2(r2, tname2)
            if not _link_nets(na, nb, net_map, net_rev, local):
                undo(local)
                return False
        journal.extend(local)
        return True

    def group_pairings(r1, r2):
        """Yield every full terminal pairing (as a dict) for r1->r2 that lines
        up same-(role,function) terminals.  A pairing is the product over groups
        of the per-group permutations.  Groups are tiny (<=~6), so the product
        is small in practice.  Consistency with the net bijection is checked
        later by apply_pairing -- here we only enumerate structural pairings.
        """
        g1g = rf_terms1[r1]
        g2g = rf_terms2[r2]
        if set(g1g) != set(g2g):
            return
        keys = list(g1g)
        for k in keys:
            if len(g1g[k]) != len(g2g[k]):
                return

        # per group, all bijections from names1 to names2
        def group_options(k):
            names1 = g1g[k]
            names2 = g2g[k]
            for perm in itertools.permutations(names2):
                yield list(zip(names1, perm))

        # cartesian product across groups
        per_group = [list(group_options(k)) for k in keys]
        for combo in itertools.product(*per_group):
            pairing = {}
            for pairs in combo:
                for a, b in pairs:
                    pairing[a] = b
            yield pairing

    def backtrack(idx: int) -> bool:
        if idx == len(order):
            return True
        c1 = order[idx]
        r1 = c1.refdes
        for r2 in candidates[r1]:
            if r2 in used2:
                continue
            used2.add(r2)
            for pairing in group_pairings(r1, r2):
                journal: list = []
                if apply_pairing(r1, r2, pairing, journal):
                    if backtrack(idx + 1):
                        return True
                undo(journal)
            used2.discard(r2)
        return False

    return backtrack(0)


def _link_nets(na, nb, net_map, net_rev, added) -> bool:
    """Assert g1-net na corresponds to g2-net nb, keeping a bijection.

    na/nb may be real net ids (ints) or singleton markers (tuples).  Singleton
    nets on both sides are unconstrained provided both are singletons, but we
    still record the pairing so a single g1 singleton can't map to two distinct
    g2 nets and vice-versa -- treat them uniformly through net_map/net_rev.
    Records additions into `added` for rollback.  Returns False on conflict.
    """
    existing_fwd = net_map.get(na, _MISSING)
    existing_rev = net_rev.get(nb, _MISSING)

    if existing_fwd is _MISSING and existing_rev is _MISSING:
        net_map[na] = nb
        net_rev[nb] = na
        added.append(("fwd", na))
        added.append(("rev", nb))
        return True
    if existing_fwd is not _MISSING and existing_fwd == nb \
            and existing_rev is not _MISSING and existing_rev == na:
        # already consistently linked
        return True
    # any partial/conflicting state is a violation
    return False


_MISSING = object()


__all__ = ["equivalent"]
