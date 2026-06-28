"""M4 — Stage 3: Patch Generation (ROADMAP M4 / plan.md §7).

Render managed region (pinctrl groups for all declared states + node overrides),
apply to a board-file copy, emit generated .dts / .patch / structured_edits /
report into output/generated/.

Public API:
    run(plan_csv=None, write=False) -> (Stage1Result, Stage2Result, GeneratedArtifacts)
    generate(validated_plan_ir, stage2, index, skip_peripherals=None) -> GeneratedArtifacts
    write_outputs(artifacts)

Status: DONE (M4). Tests in tests/test_m4_patch_generation.py.
"""
from .generate import (
    run, generate, write_outputs, GeneratedArtifacts, resolve_electrical,
)

__all__ = ["run", "generate", "write_outputs", "GeneratedArtifacts", "resolve_electrical"]
