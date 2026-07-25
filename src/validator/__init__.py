"""validator — 板級官方驗證（G5，多板：引擎可替換）。

CubeMX 流程：script_gen（assignment -> console script，純函式）→ runner
（subprocess 執行、產物落 output/validator/）→ report（pinout.csv diff ->
結構化衝突）。綠色（確定性）元件：LLM 只讀結果，不能改寫。

engines：驗證方式由 data/<board>/board.yaml 決定——CubeMXEngine（ST 板）／
ScriptEngine（自訂腳本，階段 B）／NullEngine（不驗證，回 skipped）。
呼叫端一律走 engine_for(board)。
"""
from validator.script_gen import (build_script, dt_mode_plan, expected_pin_map,
                                  plan_instances)
from validator.runner import (cubemx_binary, cubemx_resources_dir, run_script,
                              DEFAULT_TIMEOUT)
from validator.report import (parse_result, plan_fingerprint,
                              read_pinout_csv, log_errors, validated_summary)
from validator.engines import (CubeMXEngine, ScriptEngine, NullEngine,
                               engine_for)

__all__ = ["build_script", "dt_mode_plan", "expected_pin_map", "plan_instances",
           "cubemx_binary", "cubemx_resources_dir", "run_script",
           "DEFAULT_TIMEOUT", "parse_result", "plan_fingerprint",
           "read_pinout_csv", "log_errors", "validated_summary",
           "CubeMXEngine", "ScriptEngine", "NullEngine", "engine_for"]
