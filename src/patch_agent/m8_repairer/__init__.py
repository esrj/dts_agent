"""M8 — Repairer: error-feedback repair loop (Generator <-> Validator, ≤3 rounds).

Public API:
    run(plan_csv=None, provider=None, repair_provider=None, max_rounds=3, ...)
        -> RepairResult
    classify(errors, dp, edits_by_target) -> Classification
    repair_target(target, current_edits, errors, provider, all_labels) -> (edits, report)
    ReplayProvider — re-renders stored edits through the M6 pipeline
"""
from .repair import (RepairResult, Classification, ReplayProvider,
                     classify, repair_target, run)

__all__ = ["RepairResult", "Classification", "ReplayProvider",
           "classify", "repair_target", "run"]
