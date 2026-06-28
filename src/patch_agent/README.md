# patch_agent — DTS Patch pipeline（DTS_agent 第二段）

自動化產生並驗證 STM32MP257F-EV1 板的 Linux kernel Device Tree (DTS) patch。
原獨立專案 DTS_patch_agent，2026-06 併入 DTS_agent 成為第二段
（合併記錄見根目錄 [MERGE_PLAN.md](../../MERGE_PLAN.md)）。

## 目的

輸入一份 **pin 規劃表 `output/plan/plan.csv`**（每列一個 `peripheral, signal, pin, af`，
由第一段求解產生——web 流程按「產生 DTS patch」時伺服器自動落地），
本 pipeline 比對官方 baseline DTS，自動產出：

- `output/generated/generated.patch` — 可直接餵給 Yocto（`SRC_URI += "file://generated.patch"`）的 kernel DT patch
- `output/generated/stm32mp257f-ev1.generated.dts` — patch 套用後的完整 DTS（可直接用 `dtc` 編譯）

並在產出前後完成定位、生成、結構驗證、`cpp`/`dtc` 編譯驗證與錯誤自動修復
（LLM 修復迴圈，最多 3 輪）。

## 使用方式

Web：第一段 plan 表格 →「⚙ 產生 DTS patch」按鈕（`/api/dts/generate`，
背景執行、前端輪詢）。CLI（於專案根目錄）：

```bash
# 完整 pipeline：Locator → Generator(LLM) → Validator → Repairer
PYTHONPATH=src venv/bin/python -m patch_agent run    # 預設讀 output/plan/plan.csv

PYTHONPATH=src venv/bin/python -m patch_agent locate   # 只跑定位（不用 LLM）：產出 diff plan
PYTHONPATH=src venv/bin/python -m patch_agent dry-run  # 印出將送給 LLM 的 prompt，不呼叫 API
PYTHONPATH=src venv/bin/python -m patch_agent validate # 只跑驗證
```

- Exit code：`0` 成功、`1` 失敗（修復耗盡）、`2` 需要人工介入（pin 衝突／資訊不足）。
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

## 資料與路徑

**所有路徑集中在 [`config.py`](config.py)**（唯一權威，模組一律 import 常數，
不自行拼字串）。合併後的資料佈局（詳見 [data/README.md](../../data/README.md)）：

| config 常數 | 位置 | 說明 |
|---|---|---|
| `AF_TABLE` / `ALL_PERIPHERAL` / `PERIPHERAL_PROFILES` | `data/<b>/base/` | 與第一段共用的 solver 正本 |
| `SIGNAL_TO_PIN` / `OFFICIAL_DTS_PERIPHERAL` | `data/<b>/dts/` | 官方 DTS 解析（共用） |
| `BASELINE_CSV`、`BOARD_DTS` 等 | `data/<b>/baseline/` | 官方 kernel DTS 快照（本段專用） |
| `REQUIRE`（= boot_requirements.json）、`GPIO_PINS`、render 資料 | `data/<b>/dts_generation/` | 本段專用知識。**注意**：`boot_requirements.json` 是 DTS node 級開機知識，與 solver 的 `base/require.json` 同源不同物，永不合併 |
| `PLAN_CSV` | `output/plan/plan.csv` | 兩段交棒點（輸入） |
| `OUTPUT_GEN` | `output/generated/` | 本段全部產物（覆寫制） |

資料重建工具在根目錄 `tools/`（grab_kernel_dts.sh、extract_board_data.py、
extract_fixed_connections.py、derive_alias.py），平常不需執行。

## 主要輸出檔案（output/generated/）

| 檔案 | 說明 |
|---|---|
| `generated.patch` | 在 board DTS 結尾 append 一個 managed region 的 patch，給 Yocto bbappend 使用 |
| `stm32mp257f-ev1.generated.dts` | baseline + patch 合併後的完整 DTS |
| `structured_edits.json` | patch 的程式可讀版（每個 peripheral 一組 edit） |
| `diff_plan.json` / `locator_report.json` | Locator 的定位結果與報告 |
| `generation_report.json` | Generator 報告（reuse/generate、LLM 用量） |
| `validation_report.json` | Validator 報告（各項檢查 + 編譯結果） |
| `failure_report.json` | 僅在 pipeline 失敗時產生 |
