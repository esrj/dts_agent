# DTS Patch Agent

自動化產生並驗證 STM32MP257F-EV1 板的 Linux kernel Device Tree (DTS) patch。

## 專案目的

輸入一份 **pin 規劃表 `output/plan/plan.csv`**（每列一個 `peripheral, signal, pin, af`），
本專案會比對官方 baseline DTS，自動產出：

- `output/generated/generated.patch` — 可直接餵給 Yocto（`SRC_URI += "file://generated.patch"`）的 kernel DT patch
- `output/generated/stm32mp257f-ev1.generated.dts` — patch 套用後的完整 DTS（可直接用 `dtc` 編譯）

並在產出前後完成定位、生成、結構驗證、`cpp`/`dtc` 編譯驗證與錯誤自動修復（LLM 修復迴圈，最多 3 輪）。

### 與另一個專案的分工（合併預定）

```
[另一個專案：Plan 產生器]                     [本專案：DTS Patch Agent]
 客戶需求 / delta 表  ──►  output/plan/plan.csv  ──►  output/generated/*.patch / *.dts
```

- 另一個專案負責**生成 `output/plan/plan.csv`**（pin 規劃 / 排腳求解）。
- 本專案只**消費 plan.csv**，負責產生**經過驗證的 DTS patch**。
- 兩專案唯一的介面是 `output/plan/plan.csv`（欄位：`peripheral,signal,pin,af`）。

## 使用方式

```bash
# 完整 pipeline：Locator → Generator(LLM) → Validator → Repairer
PYTHONPATH=src python3 -m patch_agent run            # 預設讀 output/plan/plan.csv

PYTHONPATH=src python3 -m patch_agent locate         # 只跑定位（不用 LLM）：產出 diff plan
PYTHONPATH=src python3 -m patch_agent dry-run        # 印出將送給 LLM 的 prompt，不呼叫 API
PYTHONPATH=src python3 -m patch_agent validate       # 只跑驗證
```

- Exit code：`0` 成功、`1` 失敗（修復耗盡）、`2` 需要人工介入（pin 衝突／資訊不足）。
- LLM provider/model 設定在 `llm_modules.ini`（`[dts_patch]` 區段），API key 放 `.env`。
- 編譯驗證需要 `dtc`（`brew install dtc`）與 `gcc`；可用 `--no-compile` 跳過。

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

## 資料夾結構

```
DTS_patch_agent/
├── README.md                    本文件
├── .env                         API keys（不進版控）
├── llm_modules.ini              各模組使用的 LLM provider/model 設定
├── data/stm32mp257f-ev1/        板子靜態資料（人工/工具抽取，視為唯讀輸入）
│   ├── af_table.json                AF 對照表
│   ├── all_peripheral.json          全部 peripheral 清單
│   ├── peripheral_profiles.json     peripheral 訊號需求
│   ├── require.json                 REQUIRE/PROTECTED 約束
│   ├── gpio_pins.json               GPIO pin 資料
│   ├── baseline/                    官方預設快照
│   │   ├── baseline.csv                 官方預設 pin 配置
│   │   ├── offiicial_dts_peripheral.json（上游檔名 typo，保留）
│   │   ├── signal_to_pin.json
│   │   └── dts/                         官方 kernel DTS/DTSI + include headers（含 MANIFEST.md 出處說明）
│   └── dts_generation/              產 patch 用的衍生資料
│       ├── board_config.json
│       ├── dts_property_bindings.json
│       ├── fixed_connections.json
│       └── peripheral_node_alias.json
├── output/
│   ├── plan/plan.csv            ★ 輸入：另一個專案產生的 pin 規劃表
│   └── generated/               ★ 輸出：每次 run 重新生成（.dts / .patch / 各種 report / llm_cache）
├── src/
│   ├── patch_agent/             主套件（python3 -m patch_agent）
│   │   ├── __main__.py / cli.py     CLI 入口（run / locate / dry-run / validate）
│   │   ├── config.py                所有檔案路徑集中於此
│   │   ├── m1_dts_parser/           baseline DTS parser / node index
│   │   ├── m2_validation_harness/   plan.csv 驗證（供 locator 使用）
│   │   ├── m3_target_resolution/    peripheral → DTS 節點解析（供 locator 使用）
│   │   ├── m4_patch_generation/     deterministic patch 渲染（供 generator 使用）
│   │   ├── m5_locator/              Stage 1-2：diff plan（無 LLM）
│   │   ├── m6_generator/            Stage 3：LLM 生成 + 渲染
│   │   ├── m7_validator/            Stage 4：結構 + 編譯驗證
│   │   └── m8_repairer/             Stage 5：錯誤修復迴圈（完整 pipeline 入口）
│   └── llm_provider/            LLM 抽象層（anthropic / gemini / local_lm），factory 讀 llm_modules.ini
└── tools/                       資料抽取工具（重建 data/ 用，平常不需執行）
    ├── grab_kernel_dts.sh           從 kernel source 抓 baseline DTS
    ├── extract_board_data.py        從 baseline DTS 抽 gpio_pins / board_config / bindings
    ├── extract_fixed_connections.py 抽 fixed_connections.json
    └── derive_alias.py              抽 peripheral_node_alias.json
```

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
