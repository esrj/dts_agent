"""
test_solve_poly.py — cross-checks Algorithm 2 (PinAssignPoly) against
Algorithm 1 (PinAssign) and against brute-force ground truth on small,
fully-controlled synthetic instances.

Run: venv/bin/pytest src/solver/test_solve_poly.py -v
(from repo root, with PYTHONPATH=src so `solver.*` resolves — matches the
convention already used by patch_agent's `-m` invocation.)
"""
import itertools
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))  # -> src/, if not already on path

from solver.solve import Instance, PinAssign, load_af_table
from solver.solve_poly import PinAssignPoly


REPO_ROOT = Path(__file__).parent.parent.parent
REAL_AF_TABLE = REPO_ROOT / "data" / "stm32mp257f-ev1" / "base" / "af_table.json"


# --------------------------------------------------------------------------- #
# Synthetic instances — small enough to reason about and brute-force exactly
# --------------------------------------------------------------------------- #
def _synthetic_af():
    """
    4 pins, 4 signals, deliberately overlapping domains so there is more
    than one feasible assignment (needed to make solve_optimal/enumerate_all
    tests meaningful, not just "the only possible answer").

        P1: {A, B}      P2: {A, C}      P3: {B, C, D}      P4: {D}
    """
    return {
        "P1": {"A", "B"},
        "P2": {"A", "C"},
        "P3": {"B", "C", "D"},
        "P4": {"D"},
    }


def _brute_force_all(af, required, must_gpio=frozenset(), must_bind=None):
    """Exhaustive reference: every injective signal->pin assignment."""
    must_bind = must_bind or {}
    pins = [p for p in af if p not in must_gpio]
    results = []
    for perm in itertools.permutations(pins, len(required)):
        assignment = dict(zip(required, perm))
        ok = all(sig in af[pin] for sig, pin in assignment.items())
        ok = ok and all(assignment.get(sig) == pin
                        for pin, sig in must_bind.items())
        if ok:
            results.append(dict(assignment))
    return results


# --------------------------------------------------------------------------- #
# Feasibility: PinAssignPoly agrees with PinAssign, on both real and
# synthetic data
# --------------------------------------------------------------------------- #
class TestFeasibilityAgreement:
    def test_sat_case_agrees_with_algorithm_1(self):
        inst = Instance(af=_synthetic_af(), required=["A", "B", "C", "D"])
        r1 = PinAssign(inst).solve()
        r2 = PinAssignPoly(inst).solve()
        assert r1.sat and r2.sat
        # Each solver's own assignment must be independently valid (they
        # need not pick the *same* pins -- both P1:A,P2:C or P2:A,P1:B... are
        # legal), so validate structurally rather than comparing dicts.
        for sig, pin in r2.assignment.items():
            assert sig in inst.af[pin]
        assert len(set(r2.assignment.values())) == len(r2.assignment)

    def test_unsat_case_agrees_with_algorithm_1(self):
        # P4 is D's only candidate; must_gpio blocks it -> D has empty domain.
        inst = Instance(af=_synthetic_af(), required=["A", "B", "C", "D"],
                       must_gpio={"P4"})
        r1 = PinAssign(inst).solve()
        r2 = PinAssignPoly(inst).solve()
        assert not r1.sat and not r2.sat

    def test_hall_violation_agrees_with_algorithm_1(self):
        # N({A, C}) = {P1} only -- two signals, one shared pin, no
        # alternative for either -- a genuine Hall violation.
        af = {"P1": {"A", "C"}}
        inst = Instance(af=af, required=["A", "C"])
        r1 = PinAssign(inst).solve()
        r2 = PinAssignPoly(inst).solve()
        assert not r1.sat and not r2.sat

    def test_must_bind_is_respected(self):
        inst = Instance(af=_synthetic_af(), required=["A", "B", "C", "D"],
                       must_bind={"P2": "A"})
        r = PinAssignPoly(inst).solve()
        assert r.sat
        assert r.assignment["A"] == "P2"

    @pytest.mark.skipif(not REAL_AF_TABLE.exists(), reason="board data absent")
    def test_real_board_data_sat_case(self):
        af = load_af_table(str(REAL_AF_TABLE))
        required = ["USART3_TX", "TIM5_CH2", "EVENTOUT"]
        inst = Instance(af=af, required=required)
        r1 = PinAssign(inst).solve()
        r2 = PinAssignPoly(inst).solve()
        assert r1.sat == r2.sat
        if r2.sat:
            for sig, pin in r2.assignment.items():
                assert sig in af[pin]


# --------------------------------------------------------------------------- #
# Obj-Enum : enumerate_all against brute force
# --------------------------------------------------------------------------- #
class TestEnumerateAll:
    def test_matches_brute_force_exactly(self):
        af = _synthetic_af()
        required = ["A", "B", "C", "D"]
        inst = Instance(af=af, required=required)
        got = list(PinAssignPoly(inst).enumerate_all())
        expected = _brute_force_all(af, required)

        def key(a):
            return tuple(sorted(a.items()))
        assert {key(a) for a in got} == {key(a) for a in expected}
        assert len(got) == len(expected)  # no duplicates

    def test_limit_truncates_without_changing_membership(self):
        inst = Instance(af=_synthetic_af(), required=["A", "B", "C", "D"])
        full = {tuple(sorted(a.items()))
               for a in PinAssignPoly(inst).enumerate_all()}
        limited = list(PinAssignPoly(inst).enumerate_all(limit=2))
        assert len(limited) == 2
        assert all(tuple(sorted(a.items())) in full for a in limited)

    def test_unsat_yields_nothing(self):
        inst = Instance(af=_synthetic_af(), required=["A", "B", "C", "D"],
                       must_gpio={"P4"})
        assert list(PinAssignPoly(inst).enumerate_all()) == []


# --------------------------------------------------------------------------- #
# Obj-1 : solve_optimal, incl. the silent-drop-on-infeasible guard
# --------------------------------------------------------------------------- #
scipy = pytest.importorskip("scipy")


class TestSolveOptimal:
    def test_minimum_cost_matches_brute_force(self):
        af = _synthetic_af()
        required = ["A", "B", "C", "D"]
        inst = Instance(af=af, required=required)
        cost = {("A", "P1"): 5, ("A", "P2"): 1, ("B", "P1"): 2,
               ("B", "P3"): 9, ("C", "P2"): 3, ("C", "P3"): 1,
               ("D", "P3"): 4, ("D", "P4"): 0}
        cost_fn = lambda sig, pin: cost.get((sig, pin), 100)

        result = PinAssignPoly(inst).solve_optimal(cost_fn)
        assert result.sat

        all_costs = [
            sum(cost_fn(sig, pin) for sig, pin in a.items())
            for a in _brute_force_all(af, required)
        ]
        assert result.cost == min(all_costs)

    def test_result_is_a_member_of_enumerate_all(self):
        inst = Instance(af=_synthetic_af(), required=["A", "B", "C", "D"])
        cost_fn = lambda sig, pin: hash((sig, pin)) % 7  # arbitrary but fixed
        result = PinAssignPoly(inst).solve_optimal(cost_fn)
        all_solutions = {tuple(sorted(a.items()))
                         for a in PinAssignPoly(inst).enumerate_all()}
        assert tuple(sorted(result.assignment.items())) in all_solutions

    def test_infeasible_instance_reports_unsat_not_a_partial_assignment(self):
        """
        Regression guard: scipy.optimize.linear_sum_assignment silently
        assigns only min(rows, cols) pairs on an infeasible cost matrix
        instead of raising -- solve_optimal's Hopcroft-Karp preflight must
        catch this and report UNSAT rather than returning a plan that
        silently drops a required signal.
        """
        af = {"P1": {"A", "C"}}  # Hall violation: A, C share the one pin, no alternative
        inst = Instance(af=af, required=["A", "C"])
        result = PinAssignPoly(inst).solve_optimal(lambda s, p: 1.0)
        assert not result.sat
        assert "C" not in result.assignment or "A" not in result.assignment
