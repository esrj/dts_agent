# 階段 A：多板支援改造計劃

> 目標：讓專案能為不同板子生成 plan 與 DTS patch。知識庫自動化（Knowledge
> Extractor）留到階段 B；本階劃以**手動放置板子資料**為主。
> **紅線：原有 stm32mp257f-ev1 的所有功能行為不變**——只重構程式碼使其可擴充。

---

## 0. 現況盤點（改前必讀：哪些已經是多板、哪些寫死）

### ✅ 已經是多板的（不需要動）

| 元件 | 現況 |
|---|---|
| 知識庫佈局 | `data/<board>/` 一板一資料夾，`dataio.list_boards()` 以「五個必要檔齊全」自動偵測 |
| 路徑解析 | `dataio.board_paths(board)`（`_BOARD_FILES` 唯一權威） |
| 第一段求解 | `service._Board` 依 board id 載入＋快取；`/api/solve`、`/api/chat` 每輪帶 board（無狀態） |
| 前端板子選單 | **已存在**：`app.js` 的 `board-select` 下拉、`/api/boards` 自動偵測、`syncBoard()` 每回應同步 |
| 編排工具 | 六工具全部收 `board` 參數 |

### ❌ 寫死單板的（本次改造對象）

| 鎖點 | 位置 | 問題 |
|---|---|---|
| CubeMX 驗證寫死在流程 | `src/orchestrator/tools.py` `run_validator` / `_run_validator_locked` | 驗證方式不可替換；非 ST 板只能拿到 error |
| 自動背景驗證 | `src/web/app.py` `_kick_validation` | 每個 SAT plan 都排 CubeMX，非 ST 板會一直背景 error |
| 第二段 board 常數 | `src/patch_agent/config.py` `BOARD = "stm32mp257f-ev1"` | CLI 與 web 都只能跑這塊板 |
| 第二段可用性檢查 | `src/web/app.py` `_dts_available`：`board == pconfig.BOARD` | 等值檢查把第二段鎖死在單板 |
| 板名硬編碼檔名 | `config.py` `BOARD_DTS = DTS_DIR / "stm32mp257f-ev1.dts"`、`GENERATED_DTS = OUTPUT_GEN / "stm32mp257f-ev1.generated.dts"` | 檔名不隨 board 變 |

### 📎 有利事實（讓改造變小）

- m1–m8 全部以 `config.X` **屬性存取**（`from .. import config`），呼叫時才讀值
  → `init_board()` 重算 config module globals 即可，**八個 milestone 模組零改動**。
- `SOC_DTSI`、`PINCTRL_DTSI` 在 config.py 之外**零引用**（grep 確認）→ 可安全移除或降級為註解。
- `/api/dts/generate` 是 single-flight（背景單工作者）→ `init_board()` 的全域狀態
  變更在第二段天然序列化，無並發競態。
- `_kick_validation` 的 job 本來就帶 `(rows, board)` → 引擎選擇可逐 job 決定。

---

## 1. 設計決策（先定案再動工）

### D1. 資料夾維持 `data/<board_id>/`，不搬到 `data/board_data/<board_id>/`

原規劃寫 `data/board_data/`，但現有自動偵測根目錄就是 `data/`，搬家要同時改
`dataio`、`patch_agent/config.py`、全部文件，零功能收益。**維持現狀。**

### D2. board.yaml 是「策略檔」，不是「路徑檔」

CLAUDE.md 紅線 2：路徑唯一權威是 `dataio._BOARD_FILES` 與 `patch_agent/config.py`。
board.yaml **只描述策略與身份**（廠商、驗證方式），**不描述知識庫內部佈局**——
否則會出現第三個路徑權威。`knowledge_base` 欄位保留（階段 B 相容），
現階段固定為 `.`（知識庫就在板子資料夾本身，佈局照 data/README.md 慣例）。

格式（採用原規劃的扁平 schema）：

```yaml
board_id: stm32mp257f-ev1
vendor: ST
knowledge_base: .
validation:
  enabled: true
  type: cubemx        # cubemx | script | none
  script: null        # type: script 時填腳本相對路徑（階段 B 用）
```

### D3. 缺 board.yaml 時的預設值＝現行為（零遷移成本）

`load_board_manifest()` 對沒有 board.yaml 的板子回傳預設：

- `stm32mp257f-ev1`（DEFAULT_BOARD）→ `{enabled: true, type: cubemx}`
- 其他板 → `{enabled: false, type: none}`

**效果：一個 board.yaml 都還沒寫的時候，系統行為與今天完全一致。**
board.yaml 歸入 `_BOARD_FILES_OPTIONAL`（缺檔不影響板子偵測）。

### D4. NullValidator 也「跑完整驗證流程」，只是結果是 skipped

不是在入口擋掉驗證，而是讓驗證引擎可替換、**佇列／輪詢／fingerprint 對賬機制
全部照舊**：NullValidator 一樣寫 `output/validator/result.json`，內容為
`{status: "skipped", board, validated: {fingerprint}, message}`。
result.json 的 status 詞彙表由 `pass|fail|error` 擴為 `pass|fail|error|skipped`；
前端與 orchestrator system prompt 同步（紅線 4：工具行為改了要同步 prompt）。

### D5. 第二段輸出命名隨 board，plan.csv 交棒契約不變

- `GENERATED_DTS` → `output/generated/<board>.generated.dts`（動態）
- `output/plan/plan.csv` **維持單一位置覆寫制**（兩段交棒契約，動它牽連太廣）；
  result / report 類 JSON 都已帶 `board` 欄位，可溯源。
- 每板獨立 output 子目錄**不做**（現行 latest-wins 覆寫制語意不變），列為未來選項。

---

## 2. 三階段執行

# Phase 1 — Manifest ＋ Validator 引擎抽象（不改行為）

> **狀態：✅ 已完成（2026-07-22）。** 煙霧測試 16/16 通過（manifest 載入／
> 預設值／引擎分派／NullEngine 全鏈／CubeMX 早退／tools 分派層）；
> `list_boards()`、`patch_agent locate`、web app import 回歸正常。
> 待辦：真板 CubeMX 完整驗證一次（web 求解一份 plan 看背景驗證 pass/fail
> 照舊）——程式碼為平移，風險低，但上線前應實跑確認。

**目標：** 引入 board.yaml 與可替換驗證器；全部接完後 stm32mp257f-ev1 行為
與今天 bit-for-bit 相同。

### 1.1 依賴：pyyaml

- `requirements.txt` 加 `pyyaml`；`venv/bin/pip install pyyaml`。
  （注意：專案跑的是 `venv/bin/python`，別裝到系統 python。）

### 1.2 `dataio.load_board_manifest(board)`（src/util/dataio.py）

```python
_BOARD_FILES_OPTIONAL = {
    ...,
    "manifest": "board.yaml",          # 板子策略檔（階段 A 新增）
}

def load_board_manifest(board: str = DEFAULT_BOARD) -> dict:
    """data/<board>/board.yaml -> dict；缺檔/壞檔回預設值（＝現行為）。

    預設：DEFAULT_BOARD -> cubemx 驗證；其他板 -> 不驗證。
    只補 validation 結構，不驗證多餘欄位（階段 B 的欄位原樣保留）。
    """
```

- 防禦式：YAML 壞檔 → `_warn` ＋ 回預設（照 dataio 現有 loader 慣例）。
- `validation.type` 白名單 `{cubemx, script, none}`，未知值 → warn ＋ 降為 none。

### 1.3 驗證引擎（新檔 src/validator/engines.py）

```python
class ValidationEngine:                      # 介面（duck-type 即可，不強制 ABC）
    def validate(self, assignment, board) -> dict: ...
    # 回傳 result.json 同構 dict：{status, conflicts, checked_pins, ...}

class CubeMXEngine(ValidationEngine):
    """搬運 tools._run_validator_locked 的現有主體：script_gen -> runner ->
    report ＋ DT 生成。行為不變（含 CubeMX 未裝回 error＋安裝提示、
    cubemx.json 缺檔回 error、全域鎖序列化）。"""

class ScriptEngine(ValidationEngine):
    """階段 A 只放骨架＋文檔（board.yaml type: script 時執行自訂腳本，
    subprocess + timeout，腳本 stdout 回 result.json 同構 JSON）。
    階段 B（Knowledge Extractor 上傳驗證腳本）才啟用。"""

class NullEngine(ValidationEngine):
    """回 {status:"skipped", checked_pins:[...], message:"此板未啟用驗證"}。"""

def engine_for(board: str) -> ValidationEngine:
    """load_board_manifest -> enabled/type -> 引擎實例。"""
```

**關鍵：CubeMXEngine 是「搬家」不是「重寫」**——把 `_run_validator_locked`
的主體平移過來，tools.py 留薄殼。`_VALIDATOR_LOCK`（CubeMX 一次只能跑一個）
跟著 CubeMXEngine 走；NullEngine 不需要鎖。

### 1.4 tools.py `run_validator` 改為引擎分派

- 工具**簽名與個數不變**（紅線 4：六工具鎖定）。
- `run_validator(assignment, board)` → `engine_for(board).validate(...)` ＋
  照舊落地 `output/validator/result.json`。
- `MAX_VALIDATOR_RUNS=3` 停損照舊；skipped 視同「一次已完成的驗證」（模型不應重試）。
- **同步 `orchestrator/system_prompt.md`**：說明 run_validator 可能回
  `status: "skipped"`（該板未啟用官方驗證），拿到 skipped 就直接回報使用者，不重試。

### 1.5 web 自動驗證掛鉤（app.py）

- `_kick_validation` 照舊排 job（帶 board）；worker 內由引擎分派決定行為。
  Null 板寫 skipped result.json → 前端輪詢 `/api/validator/status` 自然收斂。
- `/api/validator/status` 回應加 `board` 欄位（result.json 已含）。

### 1.6 前端 status 詞彙擴充（app.js）

- validator 徽章／面板處理 `status: "skipped"`：顯示「此板未啟用驗證」樣式
  （中性色，不是 error 紅）。

### 1.7 落地第一份 board.yaml

- `data/stm32mp257f-ev1/board.yaml`（enabled: true / type: cubemx）。
- 其他板的 board.yaml 等板子資料夾真的存在時再寫（D3 預設已涵蓋缺檔情形）。

### Phase 1 驗收（實測，專案無測試套件——照 CLAUDE.md 用煙霧測試）

```bash
# 1. 起服務，/api/boards 應只回 stm32mp257f-ev1（現況不變）
venv/bin/python src/web/app.py

# 2. /api/solve 出一個 SAT plan → 背景 CubeMX 驗證照跑，result.json status=pass|fail
curl -X POST :5001/api/solve -d '{"text":"我要 1 個 I2C","board":"stm32mp257f-ev1"}'
curl ':5001/api/validator/status'

# 3. 臨時假板驗證 Null 路徑：cp -r data/stm32mp257f-ev1 data/_fakeboard、
#    寫 enabled:false 的 board.yaml → solve 後 result.json status=skipped、
#    前端顯示「未啟用驗證」。驗完整個 _fakeboard 資料夾刪除。

# 4. /api/chat 讓模型顯式 run_validator 一次，確認 skipped 不觸發重試迴圈
```

---

# Phase 2 — 第二段（patch_agent）多板化 ＋ web/前端接線

> **狀態：✅ 已完成（2026-07-22）。** 煙霧測試 27/27 通過；CLI 實跑三路徑
> （`--board`／`PATCH_BOARD`／不帶參數回歸）全過，diff_plan.json 的 board
> 欄位正確標記。追加發現並修復：m4 的 KERNEL_DTS_PATH 硬編碼（收編為
> config＋board.yaml `kernel_dts_path` 選配欄位，預設由 vendor 推 kernel
> 樹路徑）、m5 DiffPlan.board 與 m7 VError.file 兩處 dataclass 預設值
> import-time 定格（改 default_factory）。
> 待辦（併入 Phase 3 全回歸）：web 全流程實測一次（選板 → solve →
> 產生 DTS → 下載）。

**目標：** `board_id` 一路傳到 patch 生成與輸出命名；stm32mp257f-ev1 不帶
參數時行為不變。

### 2.1 `patch_agent/config.py` 動態化

```python
BOARD = os.environ.get("PATCH_BOARD", "stm32mp257f-ev1")

def init_board(board_id: str) -> None:
    """重算全部路徑 globals。呼叫點：CLI 入口、/api/dts/generate 工作者。
    非執行緒安全——依靠既有 single-flight 鎖序列化（web）；CLI 是一次性行程。"""

def _recalc() -> None:
    global DATA, BASE, OFFICIAL, BASELINE, DTS_DIR, DTS_GEN, AF_TABLE, ...
    DATA = REPO_ROOT / "data" / BOARD
    ...
    BOARD_DTS = _find_board_dts()          # 見下
    GENERATED_DTS = OUTPUT_GEN / f"{BOARD}.generated.dts"

_recalc()   # module import 時以預設 BOARD 初始化（＝現行為）
```

- `BOARD_DTS` 解析規則：`DTS_DIR / f"{BOARD}.dts"` 存在就用；否則取
  `DTS_DIR` 下**唯一**的 `*.dts`（0 個或多個 → 明確報錯，不猜）。
- `SOC_DTSI` / `PINCTRL_DTSI`：config 外零引用 → **刪除**（改成註解記錄
  「include 鏈由 CPP_CMD -I include/ 解決」）。
- `PLAN_CSV`、`OUTPUT_GEN` 等 output 路徑**不隨 board 變**（D5）。

### 2.2 CLI（patch_agent/cli.py）加 `--board`

```bash
PYTHONPATH=src venv/bin/python -m patch_agent run --board ma35d1
# 等價：PATCH_BOARD=ma35d1 ... -m patch_agent run
```

- 入口第一件事 `config.init_board(args.board)`，之後 m1–m8 照舊（零改動）。
- `run / locate / dry-run / validate` 四個子命令都吃這個旗標。

### 2.3 web `/api/dts/*` 解鎖單板

- `_dts_available(board)`：拿掉 `board == pconfig.BOARD` 等值檢查，改為
  **純路徑檢查**（不動 config 全域）：`data/<board>/baseline/baseline.csv`、
  `baseline/dts/*.dts`、`dts_generation/` 是否齊全。
- `/api/dts/generate`：single-flight 工作者起跑時 `pconfig.init_board(board)`，
  再進 m5→m6→m7→m8。fingerprint 防偽紅線照舊（只收伺服器保存解）。
- `/api/dts/status`：回應帶 `board`（已有 query 參數），`result` 照舊帶
  fingerprint 對回 plan。
- `/api/dts/file` / `/api/dts/download`：`GENERATED_DTS` 檔名已隨 board，
  下載打包邏輯不變（掃 `output/generated/` 整夾）。

### 2.4 前端小改（app.js）

- 「產生 DTS」行內反問與輪詢已帶 board query（現有 `?board=` 邏輯）→
  核對 `/api/dts/generate` POST body 也帶 `board: currentBoard`，補齊即可。
- 切板時清掉上一塊板的 validator／DTS 狀態顯示（避免 A 板結果掛在 B 板畫面上；
  以回應中的 `board`／fingerprint 對賬，錯板結果不渲染）。

### Phase 2 驗收

```bash
# 1. 既有板不帶參數：行為不變（回歸）
PYTHONPATH=src venv/bin/python -m patch_agent locate
PYTHONPATH=src venv/bin/python -m patch_agent run          # 產物名不變：
ls output/generated/stm32mp257f-ev1.generated.dts

# 2. --board 假板（用 _fakeboard 複本含 baseline/）：locate 能跑通、
#    產物名為 _fakeboard.generated.dts
PYTHONPATH=src venv/bin/python -m patch_agent locate --board _fakeboard

# 3. web 全流程：選板 → solve → 產生 DTS → 下載，fingerprint 對賬正確
# 4. 驗畢刪除 _fakeboard
```

---

# Phase 3 — 收尾：文件同步 ＋ 全回歸

> **狀態：文件同步 ✅ 已完成（2026-07-22）**——CLAUDE.md、data/README.md
> （board.yaml 規範＋新增板子步驟）、PROJECT_OVERVIEW.md（§1/§4/§6/§8）、
> patch_agent README、orchestrator/system_prompt.md（開頭板名通用化）、
> MERGE_PLAN §10.2 標記已執行。
> **未完：§3.2 全回歸煙霧測試**（真板 CubeMX 驗證、web 全流程、/api/chat
> 六工具、CubeMX/dtc 缺席路徑）——需要起服務＋LLM key＋CubeMX 環境，待人工執行。

### 3.1 文件同步（改了行為就要同步——紅線 4 與專案慣例）

| 檔案 | 更新內容 |
|---|---|
| `CLAUDE.md` | 常用指令加 `--board`；功能旗標段落加 board.yaml 驗證策略；紅線 2 註明 board.yaml 是策略檔不是路徑權威 |
| `data/README.md` | board.yaml 規範（欄位、預設值、選配地位）；「新增板子的最小步驟」補 manifest |
| `PROJECT_OVERVIEW.md` | §4 `/api/dts/status` 拿掉「寫死 stm32mp257f-ev1」註記；validator 段落改為引擎分派 |
| `src/patch_agent/README.md` | CLI `--board`、`PATCH_BOARD`、BOARD_DTS 解析規則 |
| `src/orchestrator/system_prompt.md` | run_validator 的 skipped 語意（Phase 1 已做，此處覆核） |
| `MERGE_PLAN.md` | §10.2（第二段多板化）標記為已執行 |

### 3.2 全回歸煙霧測試（stm32mp257f-ev1 一項都不能變）

- [ ] `/api/solve` 求解 ＋ 背景 CubeMX 驗證 pass/fail 照舊
- [ ] `/api/chat` 六工具全走一輪（含 run_validator 停損 3 次）
- [ ] 反問（clarify）來回、建議卡片採納
- [ ] `emit_plan` csv/xlsx、`/api/export`
- [ ] `/api/dts/generate` → patch 產出 → `/api/dts/download`
- [ ] `patch_agent run` / `locate` / `dry-run` CLI（不帶 --board）
- [ ] CubeMX 未安裝路徑：validator 回 error＋提示（找一台沒裝的環境或暫時改 STM32CUBEMX_PATH）
- [ ] `dtc` 缺席：web 跳過編譯、CLI `--no-compile`

### 3.3 新板就緒定義（階段 A 出口條件）

一塊新板（如 ma35d1）要能跑通，需要手動放置：

```
data/<board>/
├── board.yaml                    # enabled: false / type: none
├── base/     af_table.json、peripheral_profiles.json、require.json、all_peripheral.json
├── dts/      signal_to_pin.json、official_dts_peripheral.json
├── baseline/ baseline.csv ＋ dts/（含 include headers、MANIFEST.md）   # patch 需要
└── dts_generation/               # patch 需要；boot_requirements.json 手工，
                                  # 其餘四檔可先 {}（LLM 補償路徑，前議已定）
```

→ 板子出現在下拉選單、能 solve（驗證 skipped）、能產 patch。
知識庫內容本身由階段 B 的 Knowledge Extractor 自動生成（見 prompts/）。

---

## 3. 相容性保證清單（改造的「不變式」）

1. 不帶 board.yaml、不帶 `--board`、不帶 `PATCH_BOARD` 時，全系統行為＝今天。
2. 六個編排工具的名稱、個數、簽名不變。
3. `output/plan/plan.csv` 位置與 schema 不變（兩段交棒契約）。
4. `/api/dts/generate` 防偽（fingerprint 409）不變。
5. 路徑權威仍是 `dataio._BOARD_FILES` 與 `patch_agent/config.py`；board.yaml 不含路徑。
6. `base/require.json` 與 `dts_generation/boot_requirements.json` 依然是兩個檔（MERGE_PLAN §0.1）。

## 4. 風險與對策

| 風險 | 對策 |
|---|---|
| CubeMX 主體搬進引擎時弄壞現行驗證 | 平移不重寫；Phase 1 驗收先跑真板 pass/fail 回歸 |
| `init_board()` 全域狀態在並發下錯亂 | 只在 single-flight 工作者與一次性 CLI 呼叫；在 docstring 明示非執行緒安全 |
| skipped 狀態讓模型進重試迴圈 | system_prompt.md 明確指示 skipped＝終態；MAX_VALIDATOR_RUNS 兜底 |
| 前端把 A 板驗證結果渲染到 B 板 | 以回應 board＋fingerprint 對賬，不匹配不渲染 |
| pyyaml 裝錯環境 | 一律 `venv/bin/pip`；requirements.txt 記錄 |

## 5. 工時估算

| 階段 | 內容 | 估時 |
|---|---|---|
| Phase 1 | manifest ＋ 引擎抽象 ＋ prompt/前端 status 同步 | 2–3 天 |
| Phase 2 | patch_agent 多板 ＋ /api/dts/* ＋ 前端接線 | 2–3 天 |
| Phase 3 | 文件同步 ＋ 全回歸 | 1–2 天 |
| **合計** | | **5–8 天** |

---

**版本：** v2.0（2026-07-22，依實際程式碼盤點重寫；v1.0 假設前端無選單、
規劃 pytest 套件等與現況不符，已作廢）
