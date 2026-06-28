"""Centralized filesystem paths for the DTS Patch Agent.

Every milestone imports paths from here instead of hard-coding strings, so the
data reorg (base / baseline/ / dts_generation/ and output/generated/) lives in
exactly one place.
"""
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]      # src/patch_agent/config.py -> repo
BOARD = "stm32mp257f-ev1"

# ---- data root -----------------------------------------------------------
DATA = REPO_ROOT / "data" / BOARD
BASELINE = DATA / "baseline"
DTS_DIR = BASELINE / "dts"
DTS_GEN = DATA / "dts_generation"

# ---- base / solver-constraint data (生成 plan 用) -----------------------
AF_TABLE = DATA / "af_table.json"
ALL_PERIPHERAL = DATA / "all_peripheral.json"
PERIPHERAL_PROFILES = DATA / "peripheral_profiles.json"
REQUIRE = DATA / "require.json"
GPIO_PINS = DATA / "gpio_pins.json"

# ---- baseline (官方預設快照) --------------------------------------------
BASELINE_CSV = BASELINE / "baseline.csv"
OFFICIAL_DTS_PERIPHERAL = BASELINE / "offiicial_dts_peripheral.json"   # note: upstream filename typo
SIGNAL_TO_PIN = BASELINE / "signal_to_pin.json"
BOARD_DTS = DTS_DIR / "stm32mp257f-ev1.dts"
SOC_DTSI = DTS_DIR / "stm32mp251.dtsi"
PINCTRL_DTSI = DTS_DIR / "stm32mp25-pinctrl.dtsi"
DTS_INCLUDE = DTS_DIR / "include"

# ---- dts_generation (render 用) -----------------------------------------
PERIPHERAL_NODE_ALIAS = DTS_GEN / "peripheral_node_alias.json"
BOARD_CONFIG = DTS_GEN / "board_config.json"
DTS_PROPERTY_BINDINGS = DTS_GEN / "dts_property_bindings.json"
FIXED_CONNECTIONS = DTS_GEN / "fixed_connections.json"

# ---- input plan ----------------------------------------------------------
OUTPUT = REPO_ROOT / "output"
PLAN_CSV = OUTPUT / "plan" / "plan.csv"

# ---- output (Stage 3/4 產物) --------------------------------------------
OUTPUT_GEN = OUTPUT / "generated"
GENERATED_DTS = OUTPUT_GEN / "stm32mp257f-ev1.generated.dts"
GENERATED_PATCH = OUTPUT_GEN / "generated.patch"
STRUCTURED_EDITS = OUTPUT_GEN / "structured_edits.json"
GENERATION_REPORT = OUTPUT_GEN / "generation_report.json"
VALIDATION_REPORT = OUTPUT_GEN / "validation_report.json"
DIFF_PLAN = OUTPUT_GEN / "diff_plan.json"
LOCATOR_REPORT = OUTPUT_GEN / "locator_report.json"
LLM_CACHE = OUTPUT_GEN / "llm_cache"
FAILURE_REPORT = OUTPUT_GEN / "failure_report.json"

# ---- tooling -------------------------------------------------------------
DTC = "dtc"   # external; install via `brew install dtc`
CPP_CMD = ["gcc", "-E", "-nostdinc", "-undef", "-D__DTS__",
           "-x", "assembler-with-cpp", "-I", str(DTS_INCLUDE)]
