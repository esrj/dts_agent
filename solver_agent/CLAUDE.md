# solver_agent — STM32MP257F-EV1 pin-mux 求解與編排系統

自然語言 → pin assignment 的雙路徑系統：確定性 pipeline（`/api/solve`）與
LLM tool-use 編排（`/api/chat`）共用同一個 CSP 求解核心。
**完整架構、資料夾結構、API/工具清單、CubeMX 整合要點見
[PROJECT_OVERVIEW.md](PROJECT_OVERVIEW.md)（單一總覽文件）。**

## 常用指令

```bash
venv/bin/python src/web/app.py           # 起 Flask 服務（web UI，預設 :5001）
FEATURE_IC_BINDING=1 venv/bin/python src/web/app.py   # 啟用 G4 IC binding 功能
```

（2026-07-04 清理：tests/ 與 docs/ 已刪除——本專案目前沒有測試套件；
改動核心邏輯後請以 `src/main.py` 或 `/api/solve` 實測驗證。）

## 架構不變式（紅線，改碼前先讀）

1. **LLM 永不直接指派腳位**——進 CSP solver 的需求必經 `solver/resolver.py`
   反腐層；LLM 只負責理解、編排、解釋。
2. **領域知識零寫死**——腳位/訊號/AF/周邊知識一律來自 `data/<board>/`
   知識庫（分類與路徑規則見 [data/README.md](data/README.md)；路徑唯一權威
   是 `util/dataio._BOARD_FILES`，程式一律走 `board_paths()`）。
3. **輸出防偽**——`emit_plan` / `run_validator` 只接受伺服器保存的已驗證解，
   LLM 無法提供任意 rows。
4. **編排動作集鎖定**——`orchestrator/tools.py` 六個工具，不隨意加寬；
   工具行為改了要同步 `orchestrator/system_prompt.md`。
5. **會寫 `output/` 的腳本一律把 OUTPUT 導到暫存目錄**（歷史事故：
   假 error 污染真產物，見 PROJECT_OVERVIEW.md §7）。

## 目錄地圖（速查）

| 位置 | 內容 |
|---|---|
| `src/solver/` | 確定性核心：CSP solver（Hall 證據）、resolver 反腐層、周邊三層展開、count 降階、反問 |
| `src/service.py` | 確定性 pipeline；optional（A + 可選 B）拆解在這層 |
| `src/orchestrator/` | tool-use loop（`MAX_STEPS=12`、validator 停損 `MAX_VALIDATOR_RUNS=3`）、鎖定工具集、session store |
| `src/validator/` | CubeMX 官方驗證：script_gen → runner → report（csv diff 唯一真相）+ DT 生成 |
| `src/llm_provider/` | 多 provider 抽象（`llm_modules.ini` 選型）；parse 的 IntentIR prompt |
| `src/web/` | Flask + 前端（聊天、plan 表格、建議卡片、clarify 按鈕、下載） |
| `data/<board>/` | 知識庫：`base/`（手工核心）、`dts/`（官方 DTS 解析）、`bindings/`（IC 知識）、`cache/`（可拋棄） |
| `output/` | 執行期產物（覆寫制、gitignore 排除）：`plan/`、`validator/` |

## 功能旗標

- `FEATURE_IC_BINDING`（預設 0）：G4 IC binding / plan ic 欄，實作完整但停用。
  四個 gate 點與恢復方式見 PROJECT_OVERVIEW.md §6。
- `STM32CUBEMX_PATH`：覆寫 CubeMX 執行檔位置；未安裝時 validator 回 error、
  DT 生成靜默跳過，不影響其他功能。
