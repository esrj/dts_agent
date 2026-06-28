"""M3 — Stage 2: Target Resolution (ROADMAP M3 / plan.md §6).

alias -> &node; read node current state via M1 index; reuse-vs-generate decision;
cross-peripheral conflict detection.

Public API:
    resolve(validated_plan_ir, index, boot_required_nodes=None) -> Stage2Result
    run(plan_csv=None) -> (Stage1Result, Stage2Result)

Status: DONE (M3). Tests in tests/test_m3_target_resolution.py.
"""
from .resolve import (
    resolve, run, Stage2Result, TargetResolution, Conflict,
)

__all__ = ["resolve", "run", "Stage2Result", "TargetResolution", "Conflict"]
