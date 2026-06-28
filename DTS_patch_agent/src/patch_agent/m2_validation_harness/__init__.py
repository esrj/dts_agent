"""M2 — Stage 1: Validation Harness (ROADMAP M2 / plan.md §5).

plan.csv/baseline.csv -> validated_plan_ir; validate vs af_table /
peripheral_profiles / gpio_pins / require; build expected_dts_requirements +
board_regression_checks.

Public API:
    run(plan_csv=None) -> Stage1Result        # load everything from config + validate
    validate_plan(rows, *, af_table, ...) -> Stage1Result
    load_plan(path) -> [ {peripheral, signal, pin, af} ]

Status: DONE (M2). Tests in tests/test_m2_validation_harness.py.
"""
from .harness import (
    run, validate_plan, load_plan, Stage1Result, Error,
    AF_MISMATCH, INVALID_PIN, PROTECTED_GPIO_CONFLICT,
    PIN_DOUBLE_BOOKED, INCOMPLETE_PERIPHERAL, REQUIRE_CONFLICT,
)

__all__ = [
    "run", "validate_plan", "load_plan", "Stage1Result", "Error",
    "AF_MISMATCH", "INVALID_PIN", "PROTECTED_GPIO_CONFLICT",
    "PIN_DOUBLE_BOOKED", "INCOMPLETE_PERIPHERAL", "REQUIRE_CONFLICT",
]
