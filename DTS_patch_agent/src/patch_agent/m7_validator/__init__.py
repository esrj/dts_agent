"""M7 — Validator: six-layer deterministic gate + dtc compile on the assembled DTS.

Public API:
    validate(generated_dts, diff_plan, structured_edits=None, ...) -> ValidationReport
    run(plan_csv=None, ...) -> (DiffPlan, GeneratedArtifacts, ValidationReport)
    write_report(report)
"""
from .validate import (ValidationReport, VError, validate, run, write_report,
                       SCHEMA_VIOLATION, PLAN_MISMATCH, AF_MISMATCH, BINDING_ERROR,
                       MISSING_STATE, PROTECTED_GPIO_CONFLICT, REQUIRE_CONFLICT,
                       REGRESSION_OUTSIDE_REGION, DISABLE_NOT_APPLIED,
                       UNTOUCHED_MODIFIED, DUPLICATE_LABEL, LABEL_NOT_FOUND,
                       MACRO_EXPANDED, SYNTAX_ERROR, DTC_ERROR)

__all__ = ["ValidationReport", "VError", "validate", "run", "write_report"]
