"""M6 — Generator: LLM-backed structured-edit generation over the M5 diff plan.

Public API:
    generate(diff_plan, provider=None, ...) -> GeneratedArtifacts
    generate_target(target_dict, provider, all_labels, ...) -> (report, groups, override)
    run(plan_csv=None, provider=None, write=False) -> (DiffPlan, GeneratedArtifacts)

The provider comes from llm_modules.ini [dts_patch] via llm_provider.factory
(get_provider(module="dts_patch")) unless one is injected explicitly.
"""
from .generate import (GeneratedArtifacts, check_edits, generate,
                       generate_target, run, write_outputs)
from .schema import EMIT_TOOL, TOOL_NAME, PROMPT_VERSION

__all__ = ["GeneratedArtifacts", "check_edits", "generate", "generate_target",
           "run", "write_outputs", "EMIT_TOOL", "TOOL_NAME", "PROMPT_VERSION"]
