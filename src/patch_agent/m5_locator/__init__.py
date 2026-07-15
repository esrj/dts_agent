"""M5 — Locator: deterministic diff plan + context packs for the LLM Generator.

Public API:
    load_plan_flexible(path)  -> (rows, extra_columns, disable_requests)
    locate(rows, extra_columns, **data)  -> (DiffPlan, Stage1Result, Stage2Result)
    run(plan_csv=None)        -> same, loading every input from config
"""
from .locate import DiffPlan, Target, load_plan_flexible, locate, run

__all__ = ["DiffPlan", "Target", "load_plan_flexible", "locate", "run"]
