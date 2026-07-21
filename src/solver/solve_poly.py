"""
solve_poly.py — Algorithm 2: polynomial matching solver for I = (P, Sigma, AF, R, G, mu)

Algorithm 1 (`solve.PinAssign`) is AC propagation + MRV backtracking on the
residual sub-problem — sound and complete, but O(d^k) worst case where k is
whatever AC fails to resolve. The residual sub-problem is exactly an
AllDifferent CSP over a bipartite graph B'' = (R'', P_R'', E''), and
AllDifferent feasibility over a bipartite graph is a *perfect matching*
question — solvable in polynomial time (Hopcroft-Karp, O(E'' * sqrt(|R''|))),
with no backtracking needed at all.

This module is a drop-in alternative living next to `solve.py`, not a
replacement: `PinAssignPoly` produces the same `Result` shape `PinAssign`
does, so callers can switch between them without any other change. It adds
two further capabilities the search-based solver has no direct route to:

  solve_optimal(cost)   — minimum-cost feasible assignment (Hungarian,
                           O(|R''|^3))
  enumerate_all(limit)  — every feasible assignment, streamed one at a time
                           (Uno's algorithm), so a caller can stop early
                           without ever holding the full solution set in
                           memory
"""

from __future__ import annotations

import time
from collections import deque
from dataclasses import dataclass
from typing import Callable, Dict, Iterator, List, Optional, Set, Tuple

from solver.solve import Instance, Result


# --------------------------------------------------------------------------- #
# Phase 1+2 (shared with Algorithm 1): residual graph B'' = (R'', D'', E'')
# --------------------------------------------------------------------------- #
def _residual(instance: Instance) -> Tuple[Optional[Result], Dict[str, str],
                                            Dict[str, Set[str]], List[str]]:
    """
    Build the residual graph exactly as Algorithm 1's Phases 1-2 do.

    Returns (early_result, forced, domains, residual_signals):
      - early_result is set (others are meaningless) if an UNSAT was already
        found during domain init or must_bind validation.
      - forced       : pin -> signal, already committed by must_bind.
      - domains      : signal -> candidate pins, for every signal still in
                       residual_signals (forced signals are NOT included).
      - residual_signals : R'' = R minus every must_bind-forced signal.
    """
    seen: Set[str] = set()
    required = [s for s in instance.required if not (s in seen or seen.add(s))]

    universe = instance.signals
    unknown = [s for s in required if s not in universe]
    if unknown:
        return Result(sat=False, unknown=unknown,
                      reason=f"{len(unknown)} required signal(s) are not in "
                             f"Sigma: {unknown}"), {}, {}, []

    domains: Dict[str, Set[str]] = {}
    for sigma in required:
        domains[sigma] = {
            p for p in instance.pins
            if p not in instance.must_gpio and sigma in instance.af.get(p, set())
        }
        if not domains[sigma]:
            return Result(sat=False, reason=f"signal '{sigma}' has no "
                          f"candidate pin (empty domain)."), {}, {}, []

    forced: Dict[str, str] = {}
    for pin, sigma in instance.must_bind.items():
        if pin not in domains[sigma]:
            return Result(sat=False, reason=f"forced binding {pin} -> "
                          f"{sigma} is infeasible (pin not a candidate for "
                          f"the signal)."), {}, {}, []
        forced[pin] = sigma

    forced_pins = set(forced)
    residual = [s for s in required if s not in forced.values()]
    for sigma in residual:
        domains[sigma] -= forced_pins
        if not domains[sigma]:
            return Result(sat=False, reason=f"domain of '{sigma}' is empty "
                          f"once forced pins {sorted(forced_pins)} are "
                          f"removed."), {}, {}, []

    return None, forced, domains, residual


# --------------------------------------------------------------------------- #
# Hopcroft-Karp maximum bipartite matching — O(E * sqrt(V))
# --------------------------------------------------------------------------- #
def _hopcroft_karp(signals: List[str],
                   domains: Dict[str, Set[str]]) -> Dict[str, str]:
    """Maximum matching signal -> pin. Incomplete iff |R''| has no perfect
    matching (Hall's condition fails)."""
    INF = float("inf")
    match_sig: Dict[str, str] = {}
    match_pin: Dict[str, str] = {}

    def bfs() -> Tuple[bool, Dict[str, float]]:
        dist: Dict[str, float] = {}
        q: deque[str] = deque()
        for s in signals:
            if s not in match_sig:
                dist[s] = 0
                q.append(s)
            else:
                dist[s] = INF
        found = False
        while q:
            s = q.popleft()
            for p in domains.get(s, ()):
                s2 = match_pin.get(p)
                if s2 is None:
                    found = True
                elif dist.get(s2, INF) == INF:
                    dist[s2] = dist[s] + 1
                    q.append(s2)
        return found, dist

    def dfs(s: str, dist: Dict[str, float]) -> bool:
        for p in domains.get(s, ()):
            s2 = match_pin.get(p)
            if s2 is None or (dist.get(s2, INF) == dist[s] + 1
                              and dfs(s2, dist)):
                match_sig[s] = p
                match_pin[p] = s
                return True
        dist[s] = INF
        return False

    while True:
        found, dist = bfs()
        if not found:
            break
        for s in signals:
            if s not in match_sig:
                dfs(s, dist)

    return dict(match_sig)


def _to_assignment(forced: Dict[str, str], matching: Dict[str, str]
                   ) -> Dict[str, str]:
    """signal -> pin, merging must_bind's forced pins with the matching."""
    out = {sigma: pin for pin, sigma in forced.items()}
    out.update(matching)
    return out


# --------------------------------------------------------------------------- #
# Algorithm 2 : PinAssignPoly(I)
# --------------------------------------------------------------------------- #
class PinAssignPoly:
    """
    Polynomial-time counterpart to `solve.PinAssign`.

    Feasibility, once the residual graph B'' is built (Phases 1-2, shared
    with Algorithm 1), is a single Hopcroft-Karp call — no MRV ordering, no
    backtracking, no exponential worst case.
    """

    def __init__(self, instance: Instance):
        self.I = instance

    def solve(self) -> Result:
        I = self.I
        I.validate()
        t0 = time.perf_counter()

        early, forced, domains, residual = _residual(I)
        if early is not None:
            early.elapsed = time.perf_counter() - t0
            return early

        if not residual:
            assignment = _to_assignment(forced, {})
            return Result(sat=True, assignment=assignment, k=0,
                         propagated=len(forced), nodes=0,
                         elapsed=time.perf_counter() - t0)

        matching = _hopcroft_karp(residual, domains)
        if len(matching) < len(residual):
            unmatched = [s for s in residual if s not in matching]
            from solver.solve import hall_violator
            witness = hall_violator(I)
            reason = (f"no AllDifferent assignment exists for the residual "
                      f"sub-problem (Hall's condition violated): "
                      f"{unmatched} unmatched.")
            if witness is not None:
                S, NS = witness
                reason += f" Hall witness: |S|={len(S)} > |N(S)|={len(NS)}."
            return Result(sat=False, reason=reason, k=len(residual),
                         propagated=len(forced), nodes=0,
                         elapsed=time.perf_counter() - t0)

        assignment = _to_assignment(forced, matching)
        # k/propagated keep Algorithm 1's field names for drop-in
        # compatibility, but here they mean "resolved by matching" /
        # "resolved by forced binding" -- there is no backtracking, so
        # `nodes` is always 0.
        return Result(sat=True, assignment=assignment, k=len(residual),
                     propagated=len(forced), nodes=0,
                     elapsed=time.perf_counter() - t0)

    # ------------------------------------------------------------------- #
    # Obj-1 : minimum-cost feasible assignment (Hungarian)
    # ------------------------------------------------------------------- #
    def solve_optimal(self, cost: Callable[[str, str], float]) -> "ScoredResult":
        """
        Minimum-cost assignment under `cost(signal, pin) -> float`.
        Requires scipy: `pip install scipy`.
        """
        I = self.I
        I.validate()
        t0 = time.perf_counter()

        early, forced, domains, residual = _residual(I)
        if early is not None:
            return ScoredResult(sat=early.sat, reason=early.reason,
                                unknown=early.unknown, cost=0.0,
                                elapsed=time.perf_counter() - t0)

        if not residual:
            return ScoredResult(sat=True, assignment=_to_assignment(forced, {}),
                                k=0, propagated=len(forced), nodes=0,
                                cost=0.0, elapsed=time.perf_counter() - t0)

        # Feasibility preflight -- required. scipy's linear_sum_assignment
        # silently assigns only min(rows, cols) pairs on a rectangular /
        # infeasible cost matrix rather than raising, which would drop a
        # signal instead of correctly reporting UNSAT.
        matching = _hopcroft_karp(residual, domains)
        if len(matching) < len(residual):
            unmatched = [s for s in residual if s not in matching]
            return ScoredResult(sat=False, k=len(residual),
                                propagated=len(forced), cost=0.0,
                                reason=f"no feasible assignment exists "
                                      f"(preflight): {unmatched} unmatched.",
                                elapsed=time.perf_counter() - t0)

        try:
            import numpy as np
            from scipy.optimize import linear_sum_assignment
        except ImportError as exc:
            raise RuntimeError(
                "solve_optimal() requires scipy: pip install scipy") from exc

        pins = sorted({p for d in domains.values() for p in d})
        pin_index = {p: i for i, p in enumerate(pins)}
        BIG = 1e6  # cost for a (signal, pin) pair outside the signal's domain
        matrix = np.full((len(residual), len(pins)), BIG)
        for i, sigma in enumerate(residual):
            for p in domains[sigma]:
                matrix[i, pin_index[p]] = cost(sigma, p)

        row_idx, col_idx = linear_sum_assignment(matrix)
        assignment = _to_assignment(
            forced, {residual[r]: pins[c] for r, c in zip(row_idx, col_idx)})
        total = float(sum(matrix[r, c] for r, c in zip(row_idx, col_idx)))

        return ScoredResult(sat=True, assignment=assignment, k=len(residual),
                            propagated=len(forced), nodes=0, cost=total,
                            elapsed=time.perf_counter() - t0)

    # ------------------------------------------------------------------- #
    # Obj-Enum : every feasible assignment (Uno), streamed
    # ------------------------------------------------------------------- #
    def enumerate_all(self, limit: Optional[int] = None
                      ) -> Iterator[Dict[str, str]]:
        """
        Yield every feasible assignment (signal -> pin dict) exactly once,
        stopping after `limit` if given. Memory stays O(|R''|) regardless
        of how many solutions exist -- nothing is materialised up front.
        """
        early, forced, domains, residual = _residual(self.I)
        if early is not None or not residual:
            if early is None:
                yield _to_assignment(forced, {})
            return

        count = 0

        def walk(local: Dict[str, Set[str]]) -> Iterator[Dict[str, str]]:
            nonlocal count
            matching = _hopcroft_karp(residual, local)
            if len(matching) < len(residual):
                return
            edge = next(((s, p) for s in residual for p in local.get(s, ())
                        if matching.get(s) != p), None)
            if edge is None:
                yield matching  # unique matching in this sub-graph: a leaf
                return
            sigma, pin = edge
            # INCLUDE branch: force sigma -> pin
            include = {s: ({pin} if s == sigma else local[s] - {pin})
                      for s in local}
            yield from walk(include)
            # EXCLUDE branch: sigma cannot use pin
            exclude = dict(local)
            exclude[sigma] = local[sigma] - {pin}
            yield from walk(exclude)

        for matching in walk(domains):
            if limit is not None and count >= limit:
                return
            count += 1
            yield _to_assignment(forced, matching)


@dataclass
class ScoredResult(Result):
    """`Result` plus the assignment's total cost (Obj-1)."""
    cost: float = 0.0
