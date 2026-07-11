"""Round-trip agreement: do several representations of the SAME design all
normalize to the SAME connectivity?

``agree(graphs)`` takes a list of CanonicalGraphs (each typically produced by a
different normalization front-end, or by a serialize/re-normalize cycle of the
same design) and returns True iff they are ALL structurally equivalent to one
another under the LVS-lite :func:`equivalence.equivalent` relation.

This is the harness's "oracle quality" probe: if the neutral-JSON serializer,
the SPICE reader, and the KiCad reader can be fed the same intent and disagree
on its connectivity, the oracle stack is standing on sand. For a connectivity
harness the strongest, cheapest agreement check we can make is:

    to_json(g) -> from_neutral_json(...) must be equivalent to g,

and, more generally, every graph in a candidate agreement set must be pairwise
equivalent. Equivalence is transitive here (it is a true isomorphism relation),
so it suffices to compare every graph against the first.

Pure Python 3 stdlib. Plain imports only (flat harness directory).
"""

from __future__ import annotations

from typing import List

from schema import CanonicalGraph
from equivalence import equivalent


def agree(graphs: List[CanonicalGraph]) -> bool:
    """Return True iff every graph in ``graphs`` is structurally equivalent.

    An empty list or a singleton trivially agrees (nothing to disagree with).
    Otherwise every element is compared against the first; equivalence is an
    equivalence relation, so pairwise-with-the-first implies all-pairwise.
    """
    if len(graphs) <= 1:
        return True
    first = graphs[0]
    for other in graphs[1:]:
        if not equivalent(first, other):
            return False
    return True


__all__ = ["agree"]
