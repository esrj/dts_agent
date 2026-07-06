# DTS_agent — STM32MP257F-EV1 pin-mux 求解 ＋ DTS patch 生成

自然語言需求 → **可行的 pin assignment（plan.csv）** → 使用者確認 →
**經驗證的 kernel DTS patch**。單一專案、兩段式可分階段確認的流程
（2026-06 由 solver_agent 與 DTS_patch_agent 兩專案合併而成，
合併計劃與決策記錄見 [MERGE_PLAN.md](MERGE_PLAN.md)）。

```
使用者（web UI）
   │  自然語言需求（「我要 2 個 ETH、1 個 I2C」）
   ▼
【第一段】pin-mux 求解（src/solver + orchestrator + service）
   │  plan 表格顯示（＋CubeMX 自動驗證徽章）
   ▼
使用者確認 ──行內反問「要產生 DTS patch 嗎？」點「是」──►【第二段】DTS patch pipeline（src/patch_agent）
   │ 否：停在 plan.csv                     │ 定位→生成→結構/dtc 編譯驗證→修復(≤3輪)
   ▼                                      ▼
output/plan/plan.csv                 output/generated/generated.patch
                                     output/generated/stm32mp257f-ev1.generated.dts
```

## 快速上手

```bash
venv/bin/python src/web/app.py          # web UI：http://127.0.0.1:5001
```

在輸入框描述需求 → 得到 plan 表格 → 表格下方反問「要接著產生 kernel DTS patch 嗎？」點「是」→
下載 `generated.patch`（可直接給 Yocto：`SRC_URI += "file://generated.patch"`）。

CLI 入口：

```bash
venv/bin/python src/main.py                          # solver 快速實驗
PYTHONPATH=src venv/bin/python -m patch_agent run    # DTS pipeline（讀 output/plan/plan.csv）
PYTHONPATH=src venv/bin/python -m patch_agent locate # 只定位，不用 LLM
```

## 需求與設定

| 項目 | 說明 |
|---|---|
| Python | 3.10（`venv/`；重建：`python3.10 -m venv venv && venv/bin/pip install -r requirements.txt`） |
| `.env` | `ANTHROPIC_API_KEY` / `GEMINI_API_KEY` / `LOCAL_LM_API_KEY` |
| `llm_modules.ini` | 各模組 LLM 選型：`[parse]`（需求解析）、`[orchestrator]`（對話編排）、`[dts_patch]`（DTS 生成/修復） |
| STM32CubeMX | 選配——未安裝時 pin 驗證徽章顯示離線，其餘功能不受影響 |
| `dtc`＋`gcc` | 選配（`brew install dtc`）——**web 路徑**缺席時自動跳過編譯驗證（結構檢查照跑、回報 compiled=false）；**CLI** `patch_agent run` 不會自動跳過，需自行加 `--no-compile` |

## 目錄結構

```
├── src/
│   ├── service.py  solver/  orchestrator/   第一段：確定性 pipeline 與 LLM 編排
│   ├── validator/                           CubeMX 官方驗證（＋四套 DT 附產物）
│   ├── patch_agent/                         第二段：DTS patch pipeline（m1–m8，見其 README）
│   ├── llm_provider/                        共用 LLM 抽象層（anthropic/gemini/local_lm）
│   ├── util/dataio.py                       板級知識庫路徑唯一權威
│   └── web/                                 Flask ＋ 前端（兩段流程的 UI）
├── data/<board>/                            板級知識庫（分類說明見 data/README.md）
├── output/                                  執行期產物（覆寫制，可整夾刪）
│   ├── plan/        plan.csv（兩段流程交棒點）
│   ├── validator/   CubeMX 驗證產物＋devicetree/{kernel,u-boot,tf-a,optee-os}
│   └── generated/   DTS patch 產物（generated.patch、*.generated.dts、各 report）
└── tools/                                   data/ 重建工具（平常不需執行）
```

**兩種 DT 產物的區別**：`output/validator/devicetree/` 是 CubeMX 驗證時附帶生成
的四套 DT（需放回 OpenSTLinux BSP 各元件樹編譯）；`output/generated/` 才是
本專案第二段的正式產物（自包含 patch，可直接餵 Yocto / `dtc` 編譯）。

架構細節、API 端點、CubeMX 整合要點見 [PROJECT_OVERVIEW.md](PROJECT_OVERVIEW.md)；
開發守則（紅線）見 [CLAUDE.md](CLAUDE.md)。
