"""Invariant layer (oracle layer 5): combinational model-checking of structural
safety invariants declared on a CanonicalGraph.

The only invariant kind currently modelled is ``mutual_exclusion`` (the BLDC
shoot-through case, spec intent #8). For each such invariant we build the
combinational boolean network that feeds the two gate-drive OUTPUT nodes ``a``
and ``b``, tracing back through ``logic_gate`` components (evaluating their
``logic_fn``) until we reach one of the declared free ``inputs``. We then
enumerate ALL 2**len(inputs) input assignments. The invariant HOLDS iff ``a``
and ``b`` are never simultaneously logic-high; otherwise we emit a
``SHOOT_THROUGH`` violation carrying a concrete counterexample assignment.

This is what catches a candidate that DECLARES the interlock invariant but
fails to actually wire the interlock: the exhaustive evaluation finds a
both-high assignment and the check fires.

VACUOUS-INVARIANT GUARD (anti-gaming). A ``mutual_exclusion`` invariant is only
meaningful if both guarded outputs can ACTUALLY be asserted -- otherwise a
candidate can anchor ``a``/``b`` on UNDRIVEN nodes (no logic gate drives them),
which are always-low and therefore "never both high" vacuously. Before ruling on
mutual exclusion we verify that each guarded output CAN be driven logic-high by
SOME input assignment. If a guarded output can never rise (undriven, or not
reachable from the inputs through real gates), we emit ``VACUOUS_INVARIANT``: the
interlock is anchored on nodes the logic does not drive, so the guarantee is
meaningless. Declaring an interlock on untraceable nodes is thus CAUGHT, not
rewarded.

Pure Python 3 stdlib. Plain imports only (flat directory).
"""

from __future__ import annotations

from itertools import product
from typing import Dict, List, Optional, Tuple

from schema import CanonicalGraph, Component, Net, Invariant


# ---------------------------------------------------------------------------
# Violation type.
#
# The canonical home of Violation is erc.py (``run_erc(graph)->list[Violation]``
# with fields ``code`` and ``msg``). We import it from there when available so
# the whole oracle stack shares one type; otherwise we fall back to a
# structurally identical local dataclass so this module runs standalone.
# ---------------------------------------------------------------------------

try:  # pragma: no cover - trivial import shim
    from erc import Violation  # type: ignore
except Exception:  # erc.py not present yet
    from dataclasses import dataclass

    @dataclass
    class Violation:  # type: ignore[no-redef]
        """A single oracle finding: an error ``code`` plus a human ``msg``."""

        code: str
        msg: str


Node = Tuple[str, str]


# ---------------------------------------------------------------------------
# Combinational evaluation
# ---------------------------------------------------------------------------

class _Evaluator:
    """Evaluates gate-drive OUTPUT nodes of a graph over a fixed assignment of
    the free input nodes, tracing back through logic_gate components.

    A *node* is a (refdes, terminal_name) pair. A node's boolean value is the
    value carried by the net it sits on. A net's value is whatever drives it:
      * if any free input node sits on the net -> that input's assigned value;
      * else if a logic_gate's logic_out terminal sits on the net -> evaluate
        that gate (its logic_in terminals recurse to their own nets);
      * else the net is undriven for the purposes of this network (None).

    Undriven inputs are treated as logic-low (0) so a *partially* wired
    interlock (e.g. a dropped NOT term) still evaluates and can be caught, but
    an undriven value never spuriously reads high.
    """

    def __init__(self, graph: CanonicalGraph):
        self.graph = graph

        # refdes -> Component (fast lookup)
        self._by_refdes: Dict[str, Component] = {c.refdes: c for c in graph.components}

        # (refdes, terminal) -> index of the net it sits on
        self._net_of_node: Dict[Node, int] = {}
        for i, net in enumerate(graph.nets):
            for nd in net.nodes:
                self._net_of_node[(nd[0], nd[1])] = i

        # Per-node driver terminal for a net: the logic_out terminal (if any)
        # that sources a given net index. Precompute net_index -> (refdes, term)
        # for every logic_out terminal sitting on that net.
        self._driver_of_net: Dict[int, Node] = {}
        for c in graph.components:
            if c.kind != "logic_gate":
                continue
            for t in c.terminals:
                if t.role == "logic_out":
                    node = (c.refdes, t.name)
                    idx = self._net_of_node.get(node)
                    if idx is not None:
                        # First driver wins; a well-formed net has exactly one.
                        self._driver_of_net.setdefault(idx, node)

        # per-evaluation state (reset each call to eval_output)
        self._input_net_val: Dict[int, int] = {}
        self._memo_net: Dict[int, int] = {}   # net index -> resolved 0/1
        self._visiting: set = set()

    # -- public ----------------------------------------------------------
    def eval_output(self, node: Node, assignment: Dict[Node, int]) -> int:
        """Value (0/1) of a gate-drive output node under an input assignment."""
        self._memo_net = {}
        self._visiting = set()
        # map each free-input net to its assigned value
        self._input_net_val = {}
        for inode, val in assignment.items():
            idx = self._net_of_node.get(inode)
            if idx is not None:
                self._input_net_val[idx] = val
        return self._eval_node(node)

    # -- internals -------------------------------------------------------
    def _eval_node(self, node: Node) -> int:
        """Boolean value carried at ``node`` = the value on its net."""
        idx = self._net_of_node.get(node)
        if idx is None:
            # Node sits on no net at all -> undriven -> treat as low.
            return 0
        return self._eval_net(idx)

    def _eval_net(self, idx: int) -> int:
        if idx in self._memo_net:
            return self._memo_net[idx]
        # cycle guard: a net currently being resolved -> break as low.
        if idx in self._visiting:
            return 0
        self._visiting.add(idx)

        val: int
        if idx in self._input_net_val:
            # A free command input sits on this net: it sources the value.
            val = self._input_net_val[idx]
        else:
            driver = self._driver_of_net.get(idx)
            if driver is None:
                # Nothing traceable drives this net -> low.
                val = 0
            else:
                val = self._eval_gate(driver)

        self._visiting.discard(idx)
        self._memo_net[idx] = val
        return val

    def _eval_gate(self, out_node: Node) -> int:
        """Evaluate the gate whose logic_out terminal is ``out_node``."""
        refdes, _term = out_node
        comp = self._by_refdes.get(refdes)
        if comp is None or comp.kind != "logic_gate":
            return 0
        fn = (comp.logic_fn or "buf").lower()

        # gather logic_in terminal values in declared order
        in_vals: List[int] = []
        for t in comp.terminals:
            if t.role == "logic_in":
                in_vals.append(self._eval_node((refdes, t.name)))

        return _apply_logic(fn, in_vals)


def _apply_logic(fn: str, ins: List[int]) -> int:
    """Evaluate a boolean gate function over a list of 0/1 inputs."""
    if fn == "not":
        a = ins[0] if ins else 0
        return 0 if a else 1
    if fn == "buf":
        return 1 if (ins and ins[0]) else 0
    if fn == "and":
        return 1 if (ins and all(v for v in ins)) else 0
    if fn == "or":
        return 1 if any(v for v in ins) else 0
    if fn == "nand":
        return 0 if (ins and all(v for v in ins)) else 1
    if fn == "nor":
        return 0 if any(v for v in ins) else 1
    if fn == "xor":
        return 1 if (sum(1 for v in ins if v) % 2 == 1) else 0
    # unknown function -> conservative low
    return 0


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def check_invariants(graph: CanonicalGraph) -> List[Violation]:
    """Model-check every declared invariant on ``graph``.

    Returns a list of Violation (empty == all invariants hold). For each
    ``mutual_exclusion`` invariant, exhaustively evaluates the combinational
    network feeding nodes ``a`` and ``b`` over all 2**len(inputs) assignments of
    the free command inputs and reports SHOOT_THROUGH with a counterexample if
    the two outputs are ever both logic-high.

    Before that, a VACUOUS-INVARIANT guard verifies that each guarded output can
    be driven high by SOME assignment; an output that can never rise (undriven /
    unreachable from the inputs) makes the mutual-exclusion guarantee vacuous, so
    we emit VACUOUS_INVARIANT for that invariant and do not additionally rule on
    (never-fireable) shoot-through for it.
    """
    violations: List[Violation] = []
    ev = _Evaluator(graph)

    for inv in graph.invariants:
        if inv.kind != "mutual_exclusion":
            # Unknown invariant kinds are ignored by this layer (v1).
            continue

        inputs: List[Node] = [(r, t) for (r, t) in inv.inputs]
        a: Node = (inv.a[0], inv.a[1])
        b: Node = (inv.b[0], inv.b[1])

        n = len(inputs)
        # Enumerate all 2**n assignments (n==0 -> single empty assignment).
        # One pass records whether each guarded output is EVER high (vacuity
        # guard) AND captures a both-high counterexample if one exists.
        a_ever_high = False
        b_ever_high = False
        both_high_bits: Optional[Tuple[int, ...]] = None
        for bits in product((0, 1), repeat=n):
            assignment = {inputs[i]: bits[i] for i in range(n)}
            va = ev.eval_output(a, assignment)
            vb = ev.eval_output(b, assignment)
            if va:
                a_ever_high = True
            if vb:
                b_ever_high = True
            if va and vb and both_high_bits is None:
                both_high_bits = bits

        # --- vacuity guard: a guarded output that can NEVER rise means the
        #     interlock is anchored on nodes the logic does not drive. --------
        dead: List[str] = []
        if not a_ever_high:
            dead.append(_fmt_node(a))
        if not b_ever_high:
            dead.append(_fmt_node(b))
        if dead:
            violations.append(Violation(
                code="VACUOUS_INVARIANT",
                msg=(
                    "mutual_exclusion is VACUOUS: guarded output(s) {dead} can "
                    "NEVER be driven logic-high under any assignment of inputs "
                    "{ins}. The interlock is anchored on nodes the logic does "
                    "not drive (undriven / unreachable from the inputs through "
                    "real gates); the never-both-high guarantee is therefore "
                    "meaningless."
                ).format(
                    dead=", ".join(dead),
                    ins=_format_input_list(inputs),
                ),
            ))
            # A vacuous invariant proves nothing; do not also report a
            # shoot-through that (by construction) can never fire.
            continue

        # --- real mutual-exclusion check -----------------------------------
        if both_high_bits is not None:
            cex = _format_counterexample(inputs, both_high_bits)
            violations.append(Violation(
                code="SHOOT_THROUGH",
                msg=(
                    "mutual_exclusion violated: outputs {a} and {b} are BOTH "
                    "logic-high under input assignment {cex}. The declared "
                    "interlock does not actually prevent both gate drives "
                    "from asserting together (shoot-through)."
                ).format(
                    a=_fmt_node(a), b=_fmt_node(b), cex=cex,
                ),
            ))

    return violations


# ---------------------------------------------------------------------------
# formatting helpers
# ---------------------------------------------------------------------------

def _fmt_node(node: Node) -> str:
    return "{0}.{1}".format(node[0], node[1])


def _format_input_list(inputs: List[Node]) -> str:
    if not inputs:
        return "{} (no free inputs)"
    return "{" + ", ".join(_fmt_node(nd) for nd in inputs) + "}"


def _format_counterexample(inputs: List[Node], bits: Tuple[int, ...]) -> str:
    if not inputs:
        return "{} (no free inputs)"
    parts = [
        "{0}={1}".format(_fmt_node(inputs[i]), bits[i])
        for i in range(len(inputs))
    ]
    return "{" + ", ".join(parts) + "}"


__all__ = ["check_invariants", "Violation"]
