# DTS_agent — STM32MP257F-EV1 pin-mux 求解 ＋ DTS patch 生成

兩段式單一系統（2026-06 由 solver_agent 與 DTS_patch_agent 合併）：
第一段自然語言 → pin assignment（確定性 pipeline `/api/solve` 與 LLM tool-use
編排 `/api/chat` 共用同一個 CSP 求解核心）；第二段 plan.csv → 經驗證的
kernel DTS patch（`src/patch_agent/` m1–m8 pipeline，web 由 `/api/dts/*` 觸發）。
**完整架構、資料夾結構、API/工具清單、CubeMX 整合要點見
[PROJECT_OVERVIEW.md](PROJECT_OVERVIEW.md)；第二段 pipeline 細節見
[src/patch_agent/README.md](src/patch_agent/README.md)。**

## 常用指令

```bash
venv/bin/python src/web/app.py           # 起 Flask 服務（web UI，預設 :5001，兩段全流程）
FEATURE_IC_BINDING=1 venv/bin/python src/web/app.py   # 啟用 G4 IC binding 功能

PYTHONPATH=src venv/bin/python -m patch_agent run     # 第二段 CLI（讀 output/plan/plan.csv）
PYTHONPATH=src venv/bin/python -m patch_agent locate  # 只定位（不用 LLM）——改動後的煙霧測試
PYTHONPATH=src venv/bin/python -m patch_agent dry-run # 印 LLM prompt，不呼叫 API
```

（2026-07-04 清理：tests/ 與 docs/ 已刪除——本專案目前沒有測試套件；
改動核心邏輯後請以 `src/main.py`、`/api/solve` 或 `patch_agent locate` 實測驗證。）

## 架構不變式（紅線，改碼前先讀）

1. **LLM 永不直接指派腳位**——進 CSP solver 的需求必經 `solver/resolver.py`
   反腐層；LLM 只負責理解、編排、解釋。
2. **領域知識零寫死**——腳位/訊號/AF/周邊知識一律來自 `data/<board>/`
   知識庫（分類與路徑規則見 [data/README.md](data/README.md)）。路徑唯一權威：
   第一段是 `util/dataio._BOARD_FILES`（一律走 `board_paths()`）；
   第二段是 `patch_agent/config.py`（一律 import config 常數）——兩邊都
   **不得自行拼路徑字串**。
3. **輸出防偽**——`emit_plan` / `run_validator` / `POST /api/dts/generate`
   只接受伺服器保存的已驗證解；LLM 與 client 都無法提供任意 rows
   （/api/dts/generate 只收 fingerprint，與伺服器保存解不符即 409）。
4. **編排動作集鎖定**——`orchestrator/tools.py` 六個工具，不隨意加寬；
   工具行為改了要同步 `orchestrator/system_prompt.md`。
5. **會寫 `output/` 的腳本一律把 OUTPUT 導到暫存目錄**（歷史事故：
   假 error 污染真產物，見 PROJECT_OVERVIEW.md §7）。
6. **兩份 boot 知識檔不可混淆**——`base/require.json`（solver：腳位級開機常數）
   與 `dts_generation/boot_requirements.json`（patch：DTS node 級開機知識）
   是**不同的檔案、不同的 schema、不同的消費者**，永不合併（MERGE_PLAN §0.1）。

## 目錄地圖（速查）

| 位置 | 內容 |
|---|---|
| `src/solver/` | 確定性核心：CSP solver（Hall 證據）、resolver 反腐層、周邊三層展開、count 降階、反問 |
| `src/service.py` | 確定性 pipeline；optional（A + 可選 B）拆解在這層 |
| `src/orchestrator/` | tool-use loop（`MAX_STEPS=12`、validator 停損 `MAX_VALIDATOR_RUNS=3`）、鎖定工具集、session store |
| `src/validator/` | CubeMX 官方驗證：script_gen → runner → report（csv diff 唯一真相）+ DT 生成 |
| `src/patch_agent/` | 第二段：m5 定位（無 LLM）→ m6 生成（LLM＋deterministic 渲染）→ m7 結構/編譯驗證 → m8 修復迴圈（≤3 輪）；路徑集中 `config.py` |
| `src/llm_provider/` | 多 provider 抽象（`llm_modules.ini` 選型，兩段共用）；parse 的 IntentIR prompt |
| `src/web/` | Flask + 前端（聊天、plan 表格、建議卡片、clarify 按鈕、下載、產生 DTS 行內反問（是/否）與輪詢） |
| `data/<board>/` | 知識庫：`base/`（手工核心）、`dts/`（官方 DTS 解析）、`bindings/`（IC 知識）、`cache/`（可拋棄）、`baseline/`＋`dts_generation/`（第二段專用） |
| `output/` | 執行期產物（覆寫制、gitignore 排除）：`plan/`（交棒點）、`validator/`、`generated/` |
| `tools/` | 第二段 data/ 重建工具（平常不需執行） |

## 功能旗標

- `FEATURE_IC_BINDING`（預設 0）：G4 IC binding / plan ic 欄，實作完整但停用。
  四個 gate 點與恢復方式見 PROJECT_OVERVIEW.md §6。
- `STM32CUBEMX_PATH`：覆寫 CubeMX 執行檔位置；未安裝時 validator 回 error、
  DT 生成靜默跳過，不影響其他功能。
- `dtc`／`gcc` 缺席時：**web 路徑**自動跳過編譯驗證（結構檢查照跑），
  狀態回報 `compiled=false`；**CLI** `patch_agent run` 不會自動跳過，
  需自行加 `--no-compile`（否則編譯層 FileNotFoundError）。
