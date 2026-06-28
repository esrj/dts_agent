"""validator — STM32CubeMX 官方編譯/腳位驗證（G5）。

流程：script_gen（assignment -> console script，純函式）→ runner（subprocess
執行、產物落 output/validator/）→ report（pinout.csv diff -> 結構化衝突）。
綠色（確定性）元件：LLM 只讀結果，不能改寫。
"""
from validator.script_gen import (build_script, dt_mode_plan, expected_pin_map,
                                  plan_instances)
from validator.runner import (cubemx_binary, cubemx_resources_dir, run_script,
                              DEFAULT_TIMEOUT)
from validator.report import (parse_result, plan_fingerprint,
                              read_pinout_csv, log_errors)

__all__ = ["build_script", "dt_mode_plan", "expected_pin_map", "plan_instances",
           "cubemx_binary", "cubemx_resources_dir", "run_script",
           "DEFAULT_TIMEOUT", "parse_result", "plan_fingerprint",
           "read_pinout_csv", "log_errors"]
