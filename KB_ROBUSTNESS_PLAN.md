# KB 強韌化改造計劃（DTS_agent 端；am6548 實測回饋）

> 背景：2026-07 接入 extractor 產的 am6548 知識庫實測，暴露出 DTS_agent
> 對「不完整／格式錯的知識庫」的三類弱點：就緒判定太鬆、缺檔行為不一致、
> 格式錯誤靜默失效。本計劃只改 DTS_agent 程式碼；知識庫內容缺口由
> extractor 端處理（[prompts/3_kb_completeness_upgrade.md](prompts/3_kb_completeness_upgrade.md)），
> 兩份計劃的 lint 規則互相對齊。
>
> **原則**：與多板化同一條紅線——原有 stm32mp257f-ev1 行為不變；
> 只是把「壞知識庫 → 難懂的炸法／靜默失效」改成「明確報錯或優雅降級」。

---

## 0. 實測確認的現況行為（改前事實，2026-07-25 盤點）

| 位置 | 現況 | 問題 |
|---|---|---|
| `patch_agent/config.py` `board_ready()` | 只檢查 `baseline.csv`＋`baseline/dts/*.dts`＋**`dts_generation/` 目錄存在** | 目錄在但六檔缺 → 反問亮起 → 按下去 worker 直接炸，使用者只看到 exception 字串 |
| `m5_locator/locate.py` `_load()` | `try: json.load … except: return None`——缺檔**靜默回 None** | 下游對 None 做 `.get()` → AttributeError，錯誤點離根因很遠；部分檢查可能拿 None 靜默跳過 |
| `m2_validation_harness/harness.py` `_load()` | `json.load(open(path))`——缺檔**直接 raise** | 同一包內兩個 `_load` 行為相反；FileNotFoundError 沒說「該補哪個檔」 |
| 知識庫進場 | 無任何 schema/一致性檢查 | extractor 產錯格式（如 pin_map 欄位錯）→ solver 端**靜默失效**（am6548 是 pad 名＝signal 名僥倖沒炸） |
| `service._Board` | 依 board id 快取、無 hot-reload | 調 require.json 需重啟——迭代摩擦（已文件化，非 bug） |

---

## 改動 1：`board_ready()` 檢查最小檔案集（~10 行）

**檔案**：`src/patch_agent/config.py`

**改法**：在現有三條件之外，補查 `dts_generation/` 的**必要檔**：

```python
_DTS_GEN_REQUIRED = ("boot_requirements.json",)          # 缺=不可用（boot 保護不能沒有）
_DTS_GEN_OPTIONAL = ("gpio_pins.json", "peripheral_node_alias.json",
                     "board_config.json", "dts_property_bindings.json",
                     "fixed_connections.json")           # 缺=空骨架降級（改動 2）

def board_ready(board_id=None) -> bool:
    ...現有三條件...
    and all((d / "dts_generation" / f).is_file() for f in _DTS_GEN_REQUIRED)
```

- 語意：**必要檔清單只放 boot_requirements.json**——它是唯一「缺了會做出
  危險輸出（boot node 不受保護）」的檔；其餘五檔缺席走改動 2 的降級。
- `/api/dts/status` 不用改（已呼叫 board_ready）。

**驗收**：假板只放 `dts_generation/`（空夾）→ available=false；
補 boot_requirements.json → true；stm32 照舊 true。

---

## 改動 2：統一 `_load` 的缺檔語意（~25 行）

**檔案**：`src/patch_agent/m5_locator/locate.py`、`m2_validation_harness/harness.py`

**改法**：兩處 `_load` 統一為同一套（可放 m5 供 m2 import，或各自同構）：

```python
_KB_DEFAULTS = {                       # 選配檔：缺檔＝schema 正確的空骨架（LLM 補償路徑）
    "peripheral_node_alias.json": {"aliases": {}},
    "gpio_pins.json": {"protected_pins": [], "reserved_by_disabled_only": []},
    "board_config.json": {"peripherals": {}},
    "dts_property_bindings.json": {"families": {}},
    "fixed_connections.json": {"connections": []},
}

def _load(path):
    try:
        return json.load(open(path))
    except FileNotFoundError:
        name = os.path.basename(str(path))
        if name in _KB_DEFAULTS:
            return dict(_KB_DEFAULTS[name])            # 優雅降級，行為明確
        raise FileNotFoundError(
            f"知識庫必要檔缺失：{path}——此檔無法降級（boot 保護／solver 正本），"
            f"請由 Knowledge Extractor 產出或手工補齊") from None
```

- **必要檔**（af_table、profiles、signal_to_pin、boot_requirements）：缺檔
  raise，但訊息**指名路徑與補救方式**——不再是裸 FileNotFoundError 或
  遠處的 AttributeError。
- **選配五檔**：缺檔回空骨架（與 extractor 計劃增項 C 的骨架定義逐字對齊），
  m6 生成走 LLM 補償。
- m5 現行「except 全吞回 None」一併廢除——壞 JSON（非缺檔）也要 raise
  帶路徑的錯，不准靜默。

**驗收**：假板刪掉 alias/fixed 等五檔 → `locate --board <fake>` 照跑；
刪 boot_requirements.json → 明確錯誤訊息含檔案路徑；壞 JSON → 同樣明確報錯。

---

## 改動 3：`tools/kb_lint.py` 知識庫進場檢查（新工具，~150 行）

**定位**：手動執行的檢查工具（放 `tools/`，與其他 data 重建工具同層）；
extractor 端也會內建同規則 lint（其計劃增項 E）——兩邊規則以下表為準。

```bash
venv/bin/python tools/kb_lint.py <board>        # 全綠 exit 0；任一 FAIL exit 1
```

**檢查規則**：

| 類別 | 規則 |
|---|---|
| 交叉一致性 | require.json 全部 pin_map：signal ∈ Σ、pin ∈ af_table 鍵、af ∈ 該 pin 合法 AF 集 |
| | signal_to_pin.json：signal ∈ Σ、pin ∈ af_table |
| | baseline.csv：無 pin 重複；每列 (signal, pin, af) 與 af_table 一致 |
| schema | `solver_action` ∈ {emit_fixed_assignment, reserve_only} |
| | dts_generation 六檔頂層鍵齊全（缺檔：必要檔 FAIL、選配檔 WARN） |
| | board.yaml `validation.type` ∈ {cubemx, script, none} |
| baseline | baseline/dts 恰一個 .dts；板檔引用的 `&label` 都能在檔組內找到定義 |
| 提示性 | boot 群組全 reserve_only（無 emit）→ WARN「此板 plan 不會帶開機組，是否刻意？」（正是 am6548 這次的坑） |

**實作注意**：
- 一律走 `dataio.board_paths()` 與 `patch_agent.config`（紅線 2：不拼路徑）；
  查任意板用 `init_board` 前先 fork 環境變數或直接以路徑計算——**本工具
  只讀不寫**，不碰 output/（紅線 5 自然滿足）。
- 輸出格式照本專案煙霧測試慣例：逐條 PASS/WARN/FAIL＋總結。

**驗收**：stm32 全綠；am6548 現況跑出「全 reserve_only」WARN 與
dts_generation 缺檔 FAIL；故意改壞一個 pin 名能攔下。

---

## 改動 4（選配、低優先）：`_Board` 快取 mtime 失效

**檔案**：`src/util/dataio.py`／`src/service.py`

現況：改既有板的知識庫檔需重啟服務。可在 `_Board` 快取記錄各檔 mtime，
命中時比對、變了就重載。**先不做**——調知識庫是開發期行為，重啟成本低；
文件已註明。等 extractor→data/ 的自動落地流程（階段 B）上線再評估。

---

## 改動 5（驗證任務，非程式碼）：空骨架下的 m6 生成品質實測

改動 1–3 落地、am6548 知識庫補齊後，實跑：

```bash
PYTHONPATH=src venv/bin/python -m patch_agent run --board am6548
```

觀察：dts_generation 五檔為空骨架時，m6 的 LLM context pack 變薄——
m7 驗證能否通過、m8 修復輪能否在 3 輪內收斂。**不能收斂**才立案強化
m6 prompt（把 baseline DTS 相關節選塞進 context）；能收斂就結案。

---

## 執行順序與驗收總表

| 順序 | 改動 | 規模 | 驗收 |
|---|---|---|---|
| 1 | 改動 2（_load 統一） | ~25 行 | 假板缺檔矩陣測試（選配降級／必要明確報錯／壞 JSON 報錯） |
| 2 | 改動 1（board_ready） | ~10 行 | 假板 available 判定矩陣；stm32 回歸 |
| 3 | 改動 3（kb_lint） | ~150 行 | stm32 全綠、am6548 攔出已知問題、壞資料能攔 |
| 4 | 改動 5（實測） | — | am6548 端到端 patch 收斂 |
| 5 | 文件同步 | — | CLAUDE.md（常用指令加 kb_lint）、data/README.md（缺檔語意表）、patch_agent README |

煙霧測試慣例照舊（專案無測試套件）：臨時假板放 `data/_fakeboard`
測完即刪；會寫 output/ 的測試一律導到暫存目錄（紅線 5）。

## 紅線對照

| 紅線 | 本計劃的遵守方式 |
|---|---|
| 2 路徑唯一權威 | kb_lint 與 _load 全走 dataio/`config` 常數；`_KB_DEFAULTS` 只是內容預設值，不是路徑 |
| 3 輸出防偽 | 不動 emit_plan／run_validator／/api/dts/generate |
| 4 工具集鎖定 | 不加編排工具；kb_lint 是 CLI 工具不是 LLM 工具 |
| 5 output 導暫存 | kb_lint 只讀；煙霧測試導暫存 |
| 6 兩份 boot 檔 | boot_requirements.json 列必要檔正是因為它是 patch 端唯一 boot 保護——與 require.json 依舊兩檔兩 schema |
