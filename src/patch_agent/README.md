# patch_agent — DTS Patch pipeline（DTS_agent 第二段）

自動化產生並驗證目標板的 Linux kernel Device Tree (DTS) patch（多板；
預設 stm32mp257f-ev1）。原獨立專案 DTS_patch_agent，2026-06 併入 DTS_agent
成為第二段（合併記錄見根目錄 [MERGE_PLAN.md](../../MERGE_PLAN.md)；
多板化見 [MULTI_BOARD_PLAN.md](../../MULTI_BOARD_PLAN.md) Phase 2）。

## 目的

輸入一份 **pin 規劃表 `output/plan/plan.csv`**（每列一個 `peripheral, signal, pin, af`，
由第一段求解產生——web 流程按「產生 DTS patch」時伺服器自動落地），
本 pipeline 比對官方 baseline DTS，自動產出：

- `output/generated/generated.patch` — 可直接餵給 Yocto（`SRC_URI += "file://generated.patch"`）的 kernel DT patch
- `output/generated/<board>.generated.dts` — patch 套用後的完整 DTS（可直接用 `dtc` 編譯；檔名隨板變）

並在產出前後完成定位、生成、結構驗證、`cpp`/`dtc` 編譯驗證與錯誤自動修復
（LLM 修復迴圈，最多 3 輪）。

## 使用方式

Web：第一段 plan 表格 → 表格下方的是/否行內反問（`/api/dts/generate`，
背景執行、前端輪詢）。CLI（於專案根目錄）：

```bash
# 完整 pipeline：Locator → Generator(LLM) → Validator → Repairer
PYTHONPATH=src venv/bin/python -m patch_agent run    # 預設讀 output/plan/plan.csv

PYTHONPATH=src venv/bin/python -m patch_agent locate   # 只跑定位（不用 LLM）：產出 diff plan
PYTHONPATH=src venv/bin/python -m patch_agent dry-run  # 印出將送給 LLM 的 prompt，不呼叫 API
PYTHONPATH=src venv/bin/python -m patch_agent validate # 只跑驗證

# 多板：--board（各子命令通用）或環境變數 PATCH_BOARD；不帶＝stm32mp257f-ev1
PYTHONPATH=src venv/bin/python -m patch_agent run --board <id>
```

- Exit code：`0` 成功、`1` 失敗（修復耗盡）、`2` 需要人工介入（pin 衝突／資訊不足）。
- 板級 `.dts` 定位規則（`config._find_board_dts`）：`baseline/dts/<board>.dts`
  存在就用；否則取目錄下**唯一**的 `*.dts`（0 或多個→回慣例路徑讓錯誤自然
  浮現，不猜）。patch diff 檔頭的 kernel 樹路徑（`config.KERNEL_DTS_PATH`）
  可由 board.yaml 的 `kernel_dts_path` 覆寫，缺時以 vendor 推
  `arch/arm64/boot/dts/<vendor>/<board>.dts`。
- LLM provider/model 設定在根目錄 `llm_modules.ini`（`[dts_patch]` 區段），API key 放根目錄 `.env`（與第一段共用 `src/llm_provider/`）。
- 編譯驗證需要 `dtc`（`brew install dtc`）與 `gcc`；可用 `--no-compile` 跳過（web 路徑在工具缺席時自動跳過並回報 `compiled=false`）。

## Pipeline 架構

```
output/plan/plan.csv ──► m5_locator   Stage 1-2：plan 驗證 + 目標節點定位 → diff_plan.json
                              │        （內部使用 m2_validation_harness、m3_target_resolution）
data/…/baseline/dts ──► m1_dts_parser 共用 baseline DTS parser / node index
                              ▼
                         m6_generator Stage 3：LLM 產生 structured edits，
                              │        deterministic renderer（m4_patch_generation）轉成 DTS/patch
                              ▼
                         m7_validator Stage 4：結構檢查 + cpp/dtc 編譯 → validation_report.json
                              ▼
                         m8_repairer  Stage 5：驗證失敗時的 LLM 錯誤修復迴圈（≤3 輪）
                              ▼
                    output/generated/ 全部產物
```

只有 m6_generator 與 m8_repairer 會呼叫 LLM；能重用 baseline 設定的 peripheral 走純
deterministic 路徑。LLM 回應會快取在 `output/generated/llm_cache/`。

**產出語意（2026-07-15 起）**：最終 DT ＝ **官方預設 ＋ plan 疊加**。

- plan.csv 可含**明確停用指令列** `<peripheral>,DISABLE,,`（signal 欄放關鍵字
  `DISABLE`、大小寫不拘，pin/af 留空，不加欄位）：該週邊在最終 DT 一定被
  `status = "disabled"`（M6 deterministic 渲染、M7 第 4 層逐筆驗證
  `DISABLE_NOT_APPLIED`）。boot-required／board-locked 節點不准關（硬錯）；
  與一般腳位列同時出現同一週邊是自相矛盾（硬錯）；baseline 本來就沒開則為
  冪等 no-op（warning，歸入 untouched）。

- 官方 baseline 已啟用、但 plan 沒提到的節點**一律保留**（untouched），
  **只有** plan 把該節點正在用的腳搶去做別的功能時（M2 `baseline_owner`
  衝突偵測）才 disable 整個節點（`source: pin_conflict`）；boot/board-locked
  節點被搶腳是硬錯（`boot_conflict`），不會被靜默關閉。
- plan 與 baseline 完全一致（全 noop、零 disable）是**合法成功**：
  `no patch needed`——不產 `generated.patch`（並清掉舊殘留），
  `*.generated.dts` ＝ baseline 全文，M7 回報
  `managed_region: skipped (no changes needed)`（web 顯示「不需要 patch」）。

## 資料與路徑

**所有路徑集中在 [`config.py`](config.py)**（唯一權威，模組一律 import 常數、
以 `config.X` 屬性存取，不自行拼字串）。多板：`config.init_board(board_id)`
重算全部板相關路徑（m1–m8 零改動）——**非執行緒安全**，只准在 CLI 入口與
web 的 single-flight DTS 工作者呼叫；`config.board_ready(board)` 是純路徑
檢查（web 查可用性用，不動全域）。合併後的資料佈局（詳見
[data/README.md](../../data/README.md)）：

| config 常數 | 位置 | 說明 |
|---|---|---|
| `AF_TABLE` / `ALL_PERIPHERAL` / `PERIPHERAL_PROFILES` | `data/<b>/base/` | 與第一段共用的 solver 正本 |
| `SIGNAL_TO_PIN` / `OFFICIAL_DTS_PERIPHERAL` | `data/<b>/dts/` | 官方 DTS 解析（共用） |
| `BASELINE_CSV`、`BOARD_DTS` 等 | `data/<b>/baseline/` | 官方 kernel DTS 快照（本段專用） |
| `REQUIRE`（= boot_requirements.json）、`GPIO_PINS`、render 資料 | `data/<b>/dts_generation/` | 本段專用知識。**注意**：`boot_requirements.json` 是 DTS node 級開機知識，與 solver 的 `base/require.json` **同名不同物**（僅曾共用檔名，知識內容互不相干），永不合併 |
| `PLAN_CSV` | `output/plan/plan.csv` | 兩段交棒點（輸入） |
| `OUTPUT_GEN` | `output/generated/` | 本段全部產物（覆寫制） |

資料重建工具在根目錄 `tools/`（grab_kernel_dts.sh、extract_board_data.py、
extract_fixed_connections.py、derive_alias.py），平常不需執行。

## 主要輸出檔案（output/generated/）

| 檔案 | 說明 |
|---|---|
| `generated.patch` | 在 board DTS 結尾 append 一個 managed region 的 patch，給 Yocto bbappend 使用 |
| `<board>.generated.dts` | baseline + patch 合併後的完整 DTS（檔名隨板變） |
| `structured_edits.json` | patch 的程式可讀版（每個 peripheral 一組 edit） |
| `diff_plan.json` / `locator_report.json` | Locator 的定位結果與報告 |
| `generation_report.json` | Generator 報告（reuse/generate、LLM 用量） |
| `validation_report.json` | Validator 報告（各項檢查 + 編譯結果） |
| `failure_report.json` | 僅在 pipeline 失敗時產生 |
