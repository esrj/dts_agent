"""DTS Patch Agent — implementation package.

Pipeline (entry: python3 -m patch_agent, see cli.py):
    m1_dts_parser         shared baseline DTS parser / node index
    m2_validation_harness plan.csv validation harness (used by locator)
    m3_target_resolution  peripheral -> DTS node target resolution
    m4_patch_generation   deterministic patch rendering primitives
    m5_locator            Stage 1-2: diff plan (baseline vs plan.csv), no LLM
    m6_generator          Stage 3: structured edits + DTS/patch generation (LLM)
    m7_validator          Stage 4: structural checks + cpp/dtc compile
    m8_repairer           Stage 5: error-feedback repair loop (<=3 rounds)

All filesystem paths are centralized in patch_agent.config.
"""
