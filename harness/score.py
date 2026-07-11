"""Scoring layer: turn a candidate's emitted circuits into per-intent verdicts,
a catch-phase histogram, and an oracle-quality figure.

The scorer is deliberately candidate-AGNOSTIC. A "candidate" is nothing more
than a mapping::

    emitted : dict[intent_id -> list[CanonicalGraph]]

i.e. for each of the 8 spec intents, the graph(s) the candidate produced. Since
there is no real candidate yet, the scorer is exercisable by feeding the GOLDENS
themselves as a "perfect candidate" (see :func:`perfect_candidate`).

For each intent the top-level verdict is computed by the SAME oracle stack the
self-test uses:

    1. run_erc(graph)            -> structural ERC violations
    2. check_invariants(graph)   -> model-checked safety invariants (#8)
    3. graph.escalations         -> ambiguities the candidate must surface
    4. equivalent(graph, golden) -> is it actually the intended circuit?

Verdict precedence (a graph gets exactly one):

    * any ERC / invariant violation           -> "FAIL:<CODE>[,<CODE>...]"
    * else non-empty escalations              -> "ESCALATE"
    * else equivalent to the golden           -> "PASS"
    * else (clean but wrong circuit)          -> "WRONG" (structurally divergent)

The catch-phase histogram is built by running :func:`mutate.mutate` over the
goldens and checking that each injected defect is caught. For this connectivity
harness every ERC / invariant catch is classified as a **static** catch (there
is no compile/runtime execution phase); the four buckets are kept so the shape
matches the wider spec vocabulary:

    deleted  : the mutant could not even be constructed (mutator inapplicable)
    static   : caught by run_erc / check_invariants (the expected code fired)
    runtime  : caught only by dynamic execution (N/A here -> always 0)
    silent   : NOT caught at all (a defect slipped through -> oracle blind spot)

``oracle_quality`` is the round-trip agreement rate: for each golden we
serialize via ``to_json`` and re-normalize via ``from_neutral_json`` and check
the pair still :func:`roundtrip.agree`. A perfect oracle scores 1.0.

Pure Python 3 stdlib. Plain imports only (flat harness directory).
"""

from __future__ import annotations

from collections import OrderedDict
from typing import Dict, List, Tuple

from schema import CanonicalGraph, to_json
from erc import run_erc
from invariant import check_invariants
from equivalence import equivalent
from normalize import from_neutral_json
from roundtrip import agree
from mutate import mutate
from goldens import GOLDENS


# ---------------------------------------------------------------------------
# core verdict
# ---------------------------------------------------------------------------

def verdict_for(graph: CanonicalGraph, golden: CanonicalGraph) -> str:
    """Compute the top-level verdict string for ``graph`` against ``golden``.

    See the module docstring for the precedence rules. Codes are de-duplicated
    but kept in first-seen order for a stable, readable ``FAIL:...`` string.
    """
    codes: List[str] = []
    for v in run_erc(graph):
        if v.code not in codes:
            codes.append(v.code)
    for v in check_invariants(graph):
        if v.code not in codes:
            codes.append(v.code)

    if codes:
        return "FAIL:" + ",".join(codes)
    if graph.escalations:
        return "ESCALATE"
    if golden is not None and equivalent(graph, golden):
        return "PASS"
    if golden is None:
        # No reference to compare against: clean + no escalation -> PASS.
        return "PASS"
    return "WRONG"


# ---------------------------------------------------------------------------
# catch-phase histogram (from mutate over the goldens)
# ---------------------------------------------------------------------------

def _catch_phase_histogram() -> Dict[str, int]:
    """Run every mutator over every golden and bucket the outcomes.

    A mutant is 'static'-caught when the oracle stack (run_erc +
    check_invariants) emits the mutator's expected code; 'silent' when that
    code is absent (a blind spot). 'runtime' is always 0 for this connectivity
    harness. 'deleted' counts golden/mutator combinations where no mutant could
    be constructed (mutator inapplicable to that golden).
    """
    hist = {"deleted": 0, "static": 0, "runtime": 0, "silent": 0}

    for _iid, entry in GOLDENS.items():
        golden = entry["graph"]
        mutants = mutate(golden)
        if not mutants:
            # No corruption was realizable on this golden at all.
            hist["deleted"] += 1
            continue
        for mutant, expected_code in mutants:
            caught_codes = {v.code for v in run_erc(mutant)}
            caught_codes |= {v.code for v in check_invariants(mutant)}
            if expected_code in caught_codes:
                hist["static"] += 1
            else:
                hist["silent"] += 1

    return hist


# ---------------------------------------------------------------------------
# oracle quality (round-trip agreement over the goldens)
# ---------------------------------------------------------------------------

def _oracle_quality() -> Tuple[float, int, int]:
    """Fraction of goldens that survive to_json -> from_neutral_json unchanged
    (structurally). Returns (rate, agreed, total)."""
    agreed = 0
    total = 0
    for _iid, entry in GOLDENS.items():
        golden = entry["graph"]
        total += 1
        reparsed = from_neutral_json(to_json(golden))
        if agree([golden, reparsed]):
            agreed += 1
    rate = (agreed / total) if total else 1.0
    return rate, agreed, total


# ---------------------------------------------------------------------------
# public API
# ---------------------------------------------------------------------------

def perfect_candidate() -> "OrderedDict[int, List[CanonicalGraph]]":
    """Build the trivial 'perfect candidate' from the goldens themselves.

    Maps each intent id to a single-element list holding that intent's golden
    graph. Feeding this to :func:`score_candidate` must yield each golden's
    expected verdict (this is what the self-test asserts)."""
    emitted: "OrderedDict[int, List[CanonicalGraph]]" = OrderedDict()
    for iid, entry in GOLDENS.items():
        emitted[iid] = [entry["graph"]]
    return emitted


def score_candidate(
    emitted: Dict[int, List[CanonicalGraph]],
) -> Dict[str, object]:
    """Score a candidate's emitted circuits.

    ``emitted`` maps intent_id -> list of CanonicalGraphs the candidate produced
    for that intent. Per intent we take the FIRST emitted graph (candidates emit
    one design per intent here) and compute its verdict against the golden.

    Returns a dict with::

        {
          "per_intent": {intent_id: {"verdict": str,
                                     "expected": str,
                                     "match": bool}},
          "n_intents": int,
          "n_match": int,
          "catch_histogram": {"deleted":..,"static":..,"runtime":..,"silent":..},
          "oracle_quality": float,           # round-trip agreement rate 0..1
          "oracle_quality_detail": {"agreed":int,"total":int},
        }
    """
    per_intent: "OrderedDict[int, Dict[str, object]]" = OrderedDict()
    n_match = 0

    for iid, entry in GOLDENS.items():
        golden = entry["graph"]
        expected = entry["expected"]
        graphs = emitted.get(iid) or []
        if not graphs:
            verdict = "MISSING"
        else:
            verdict = verdict_for(graphs[0], golden)
        match = (verdict == expected)
        if match:
            n_match += 1
        per_intent[iid] = {
            "verdict": verdict,
            "expected": expected,
            "match": match,
        }

    hist = _catch_phase_histogram()
    quality, agreed, total = _oracle_quality()

    return {
        "per_intent": per_intent,
        "n_intents": len(GOLDENS),
        "n_match": n_match,
        "catch_histogram": hist,
        "oracle_quality": quality,
        "oracle_quality_detail": {"agreed": agreed, "total": total},
    }


__all__ = ["verdict_for", "perfect_candidate", "score_candidate"]
