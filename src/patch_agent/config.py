"""Centralized filesystem paths for the DTS Patch Agent（多板支援階段 A）.

Every milestone imports paths from here instead of hard-coding strings, so the
data reorg (base / baseline/ / dts_generation/ and output/generated/) lives in
exactly one place.

多板：`BOARD` 由環境變數 `PATCH_BOARD`（預設 stm32mp257f-ev1）或 runtime
`init_board()` 決定。m1–m8 一律以 `config.X` **屬性存取**（呼叫時讀值），
所以 `init_board()` 重算本模組 globals 後，整條 pipeline 即切到新板，
八個 milestone 模組零改動。

執行緒安全性：`init_board()` 會改模組全域——只能在（a）CLI 入口（一次性
行程）或（b）web 的 single-flight DTS 工作者內呼叫；不得在並發情境使用。
"""
from pathlib import Path
import os

REPO_ROOT = Path(__file__).resolve().parents[2]      # src/patch_agent/config.py -> repo
DEFAULT_BOARD = "stm32mp257f-ev1"
BOARD = os.environ.get("PATCH_BOARD", DEFAULT_BOARD)

# ---- board 無關（不隨 init_board 改變）------------------------------------
# output 是覆寫制單一位置（plan.csv 是兩段交棒契約，不隨板變——MULTI_BOARD_PLAN §D5）
OUTPUT = REPO_ROOT / "output"
PLAN_CSV = OUTPUT / "plan" / "plan.csv"
OUTPUT_GEN = OUTPUT / "generated"
GENERATED_PATCH = OUTPUT_GEN / "generated.patch"
STRUCTURED_EDITS = OUTPUT_GEN / "structured_edits.json"
GENERATION_REPORT = OUTPUT_GEN / "generation_report.json"
VALIDATION_REPORT = OUTPUT_GEN / "validation_report.json"
DIFF_PLAN = OUTPUT_GEN / "diff_plan.json"
LOCATOR_REPORT = OUTPUT_GEN / "locator_report.json"
LLM_CACHE = OUTPUT_GEN / "llm_cache"
FAILURE_REPORT = OUTPUT_GEN / "failure_report.json"

# ---- tooling ---------------------------------------------------------------
DTC = "dtc"   # external; install via `brew install dtc`


def _find_board_dts(dts_dir: Path, board: str) -> Path:
    """baseline/dts/ 裡的板級 .dts：慣例名 <board>.dts 存在就用；否則目錄下
    「唯一」的 *.dts。0 個或多個都回慣例路徑——讓下游 FileNotFoundError
    自然浮現（訊息帶路徑），不猜。"""
    conv = dts_dir / f"{board}.dts"
    if conv.is_file():
        return conv
    cands = sorted(dts_dir.glob("*.dts")) if dts_dir.is_dir() else []
    return cands[0] if len(cands) == 1 else conv


def _kernel_dts_path(board: str) -> str:
    """patch diff 檔頭的 kernel 樹相對路徑（a/<path>、b/<path>；Yocto 打補丁
    的目標路徑）。優先取 board.yaml 的選配欄位 `kernel_dts_path`；否則以
    manifest 的 vendor 推 vendor 目錄（ST->st、Nuvoton->nuvoton、TI->ti），
    缺 vendor 時退回 st。"""
    vendor = "st"
    try:
        from util.dataio import load_board_manifest      # lazy：dataio 是葉模組
        m = load_board_manifest(board)
        if m.get("kernel_dts_path"):
            return str(m["kernel_dts_path"])
        vendor = (m.get("vendor") or "st").lower()
    except Exception:
        pass                                             # 缺 manifest／dataio 不可用：用預設
    return f"arch/arm64/boot/dts/{vendor}/{board}.dts"


def _recalc() -> None:
    """依 BOARD 重算所有板相關路徑（module import 時以預設 BOARD 跑一次）。"""
    global DATA, BASE, OFFICIAL, BASELINE, DTS_DIR, DTS_GEN
    global AF_TABLE, ALL_PERIPHERAL, PERIPHERAL_PROFILES, REQUIRE, GPIO_PINS
    global BASELINE_CSV, OFFICIAL_DTS_PERIPHERAL, SIGNAL_TO_PIN, BOARD_DTS
    global DTS_INCLUDE, PERIPHERAL_NODE_ALIAS, BOARD_CONFIG
    global DTS_PROPERTY_BINDINGS, FIXED_CONNECTIONS
    global GENERATED_DTS, KERNEL_DTS_PATH, CPP_CMD

    # Layout follows the solver-standard board taxonomy (see data/README.md):
    #   base/ = hand-maintained core (solver 正本, shared)
    #   dts/ = official-DTS derived (shared)
    #   baseline/ + dts_generation/ = DTS-patch specific (this package only)
    DATA = REPO_ROOT / "data" / BOARD
    BASE = DATA / "base"
    OFFICIAL = DATA / "dts"
    BASELINE = DATA / "baseline"
    DTS_DIR = BASELINE / "dts"
    DTS_GEN = DATA / "dts_generation"

    # base / solver-constraint data (生成 plan 用)
    AF_TABLE = BASE / "af_table.json"
    ALL_PERIPHERAL = BASE / "all_peripheral.json"
    PERIPHERAL_PROFILES = BASE / "peripheral_profiles.json"
    REQUIRE = DTS_GEN / "boot_requirements.json"   # boot-required DTS-node knowledge (patch-specific; NOT base/require.json)
    GPIO_PINS = DTS_GEN / "gpio_pins.json"

    # baseline (官方預設快照)。include 鏈由 CPP_CMD 的 -I include/ 解決，
    # 不需逐檔列 SoC dtsi（舊常數 SOC_DTSI / PINCTRL_DTSI 已移除——零引用）。
    BASELINE_CSV = BASELINE / "baseline.csv"
    OFFICIAL_DTS_PERIPHERAL = OFFICIAL / "official_dts_peripheral.json"
    SIGNAL_TO_PIN = OFFICIAL / "signal_to_pin.json"
    BOARD_DTS = _find_board_dts(DTS_DIR, BOARD)
    DTS_INCLUDE = DTS_DIR / "include"

    # dts_generation (render 用)
    PERIPHERAL_NODE_ALIAS = DTS_GEN / "peripheral_node_alias.json"
    BOARD_CONFIG = DTS_GEN / "board_config.json"
    DTS_PROPERTY_BINDINGS = DTS_GEN / "dts_property_bindings.json"
    FIXED_CONNECTIONS = DTS_GEN / "fixed_connections.json"

    # 板相關輸出（其餘 output 路徑不隨板變）
    GENERATED_DTS = OUTPUT_GEN / f"{BOARD}.generated.dts"
    KERNEL_DTS_PATH = _kernel_dts_path(BOARD)

    CPP_CMD = ["gcc", "-E", "-nostdinc", "-undef", "-D__DTS__",
               "-x", "assembler-with-cpp", "-I", str(DTS_INCLUDE)]


def init_board(board_id: str) -> None:
    """Runtime 切板：重算所有板相關路徑。呼叫點限 CLI 入口與 web 的
    single-flight DTS 工作者（見模組 docstring 的執行緒安全性說明）。"""
    global BOARD
    BOARD = board_id or DEFAULT_BOARD
    _recalc()


def board_ready(board_id: str | None = None) -> bool:
    """該板是否具備第二段（DTS patch）知識庫——純路徑檢查、不動模組全域，
    web 的 /api/dts/status 可對任意板詢問。條件：baseline.csv、baseline/dts/
    至少一個 .dts、dts_generation/ 目錄齊全。"""
    b = board_id or BOARD
    d = REPO_ROOT / "data" / b
    dts_dir = d / "baseline" / "dts"
    return ((d / "baseline" / "baseline.csv").is_file()
            and dts_dir.is_dir() and any(dts_dir.glob("*.dts"))
            and (d / "dts_generation").is_dir())


_recalc()
