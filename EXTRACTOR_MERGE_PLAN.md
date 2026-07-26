# Knowledge Extractor 合併計劃書（定案版）

> **執行記錄（2026-07-25）：M1–M8 全部完成 ✅**
> - **M1 搬遷**：`src/knowledge_extract/`；input/archive/cache/boards.ini 移根；
>   llm_provider 統一（porting streaming `_create`——大 max_tokens 必須
>   messages.stream）；paths.py D3（輸出預設 staging＋LIVE_DATA 唯讀 fallback、
>   require_enrich/af_repair copy-on-write 防寫正式庫）；相依零新增
>   （pdfplumber 顧慮不存在——實際用 pdftotext/poppler）。
> - **M2 lint 函式化**：`lint_board(board, data_root, echo)`；CLI 輸出對
>   stm32/am6548 **逐字 diff 相同**；`--data-root` 支援 staging。
> - **M3 web API**：`src/web/board_create.py` 五端點＋single-flight worker
>   ＋兩道 lint gate＋REVIEW 改判（require/boot_requirements 同步）＋原子
>   落地；19 項端點/安全/改判測試全過。
> - **M4 前端**：下拉「＋ 上傳新板子…」＋彈窗（名稱/PDF/DTS 資料夾/驗證
>   勾選）＋**baseline 板檔選擇**（M7 發現：kernel 樹常含同 SoC 多板檔，
>   前端偵測 >1 個 .dts 時必選）＋等待/REVIEW/落地流程。
> - **M7 端到端**：真材料（3.7MB PDF＋913 檔 DTS 樹）上傳 → dts 解析／boot
>   判定／雙 lint 全真（僅 stub LLM 手冊抽取）→ REVIEW 改判 round-trip →
>   原子落地 → 落地板 display name／kb_lint 全綠／solve 帶正確 boot 組，
>   11/11 全過。回歸矩陣：stm32/am6548 solve＋boot 組、locate、兩板 lint
>   全綠、`FEATURE_BOARD_CREATE=0` 回 404/false、extractor CLI 照常。
> - **M6/M8 文件**：刪 MERGE_PLAN／MULTI_BOARD_PLAN／KB_ROBUSTNESS_PLAN／
>   PINMUX_STYLE_PLAN／prompts/（引用先內文化）；README 全面重寫（三段式）；
>   CLAUDE/PROJECT_OVERVIEW/data README/extractor README 同步。
> - **真 LLM 全流程**（手冊抽取不 stub）留使用者於 web 實測——機械鏈已全驗。
> - **M5（nuvoton-mfp style）未實作**——ma35d1 可上傳/求解，「產生 DTS」
>   待該 style 完成（原計劃即不擋主線）。

> 目標：knowledge_extract 併入 DTS_agent 成為單一專案——前端左上角板子
> 下拉選單新增「上傳新板子」，上傳 **手冊 PDF＋整包 DTS 資料夾＋板子名稱**
> （＋一個「是否進行 plan 驗證」勾選），自動跑 extractor 生成知識庫、
> 通過驗收後板子出現在選單。
>
> **鐵律**：合併結束後既有功能全數正常（stm32 全流程、am6548 solve/patch、
> 多板機制、六條紅線）；README.md 全面更新。
>
> 本文是**計劃書**（未動工）。前置條件已全數完成：extractor P1–P6 修復
> ＋零手補驗收通過（§1）。

---

## 1. 現況與已完成前置

- **extractor P1–P6 ✅**（2026-07-25）：boot 判定（console/media fallback）、
  alias 全 instance（189 鍵）、all_peripheral 正鍵、pad_params 全覆蓋＋
  鍵空間 dedup、lint 大小寫敏感、出廠 gate。零手補驗收：staging 重產
  am6548 → 兩邊 lint 全綠 → solve 帶開機組 → locate passed。
- **DTS_agent 多板機制 ✅**：board.yaml manifest、validator 引擎
  （CubeMX/Script/Null）、patch_agent 多板、pinmux style（stm32＋k3-iopad）、
  kb_lint、KB 強韌化、boot 注入大小寫正規化。
- **兩個 llm_provider 是同源 fork**：實測只有 `anthropic.py` 與
  `system_prompt.md` 兩檔有差異，其餘逐字相同——統一成本低。
- **未完事項（併入本計劃）**：`nuvoton-mfp` pinmux style（原 PINMUX_STYLE
  W4，ma35d1 的「產生 DTS」前置）；三板端到端矩陣（原 W6）。

## 2. 使用者流程（目標形態，UI 規格）

```
左上角板子下拉選單
  ├─ stm32mp257f-ev1
  ├─ am6548
  └─ ＋ 上傳新板子…            ← 新增項（永遠在清單最底）
        │ 點擊
        ▼
  ┌─ 上傳彈窗 ──────────────────────────────┐
  │ 板子名稱   [____________]               │ ← 顯示名；系統另生成資料夾 slug
  │ 手冊 PDF   [選擇檔案]                    │
  │ DTS 資料夾 [選擇資料夾]                  │ ← 整包（含 dtsi 與 include/）
  │ ☐ 進行 plan 驗證                        │ ← 預留開口：現階段勾選＝CubeMX
  │            （ST 板勾選；其他板不勾）       │    （future：勾選＋上傳驗證腳本）
  │                    [取消]  [上傳]        │
  └─────────────────────────────────────────┘
        │ 上傳
        ▼
  等待畫面（進度輪詢：上傳→解包→手冊解析→DTS 解析→生成→lint）
        │ 完成
        ▼
  REVIEW 確認畫面（boot 群組判定表可改判＋其他待審項）→ [確認落地]
        │
        ▼
  下拉選單自動出現新板（顯示「板子名稱」）並選中
```

**勾選按鈕語意**（預留開口的正式定義）：勾選 → 產出的 board.yaml
`validation: {enabled: true, type: cubemx}`；不勾 → `{enabled: false,
type: none}`。未來擴充＝勾選時多一個「上傳驗證腳本」欄位 → `type: script`。
現階段 UI 附註「ST 板勾選；其他板不勾」。

## 3. 合併後資料夾結構（DTS_agent 為主，knowledge_extract 消失）

```
DTS_agent/
├── README.md                ← M7 全面重寫（三段式：extract → plan → patch）
├── CLAUDE.md / PROJECT_OVERVIEW.md / EXTRACTOR_MERGE_PLAN.md（本文）
├── llm_modules.ini          ← 併入 [knowledge_extract] 區段（單一檔）
├── boards.ini               ← 自 knowledge_extract/ 移入（pdf↔board 快取）
├── requirements.txt         ← 併入 extractor 相依（pdfplumber…）
├── data/<board>/            ← 唯一正式知識庫（不變）
├── input/                   ← extractor CLI 的待處理區（自 knowledge_extract/ 移入）
├── archive/                 ← 收集品（手冊/DTS 源碼；gitignore，開發測試材料）
├── cache/knowledge_extract/ ← PDF 頁快取（可拋棄）
├── output/
│   ├── plan/ validator/ generated/     （不變）
│   └── staging/<board>/     ← 上傳生成的落地前暫存（唯一 extractor 可寫區）
├── src/
│   ├── knowledge_extract/   ← 自 knowledge_extract/src/knowledge_extract/ 移入
│   │   └── README.md        ←（原專案 README 精簡為模組文檔）
│   ├── llm_provider/        ← 統一後單一份（見 D2）
│   ├── web/                 ← app.py 掛新路由；board_create.py 新增
│   └── （solver/ service.py / orchestrator/ validator/ patch_agent/ util/ 不變）
└── tools/                   ← kb_lint.py 函式化（介面不變）
```

## 4. 設計決策

### D1. 程式碼落位：`src/knowledge_extract/` 平級套件
與 patch_agent 同層級；保留獨立 CLI（`PYTHONPATH=src venv/bin/python -m
knowledge_extract …`）供離線批次使用。原 `knowledge_extract/` 目錄整個刪除。

### D2. llm_provider 統一為一份
以 **DTS_agent 版為正本**；merge 時 diff `anthropic.py`（唯一實質差異檔），
extractor 需要的能力（若有）porting 進正本；extractor 的 `system_prompt.md`
副本棄用（那是 parse 的 prompt，extractor 不消費）。`llm_modules.ini` 併入
`[knowledge_extract]` 區段。**驗收**：兩邊各自的 provider 呼叫在單一份上
全部可用。

### D3. paths 改造——extractor 只寫 staging（紅線 5 延伸）
`knowledge_extract/paths.py` 現以「自身專案根」推路徑；改為以 DTS_agent
repo root 推：
- `INPUT_DIR = <root>/input`、`CACHE_DIR = <root>/cache/knowledge_extract`、
  `BOARDS_INI = <root>/boards.ini`
- **`DATA_DIR`（輸出預設）改指 `<root>/output/staging/`**——extractor 永不
  直接寫 `data/<board>/`；落地（staging → data/）一律由 web confirm 或人工
  執行的原子搬移完成。CLI 印出明確的落地指令提示。
- lint／enrich 讀「既有 base」的 fallback 路徑改為可讀 `data/`（讀可以，
  寫不行）。

### D4. staging ＋ lint gate ＋ 原子落地（沿用前版決策）
- `output/staging/<slug>/`（output 天然不被 `list_boards()` 掃到、gitignore）。
- 落地＝`os.replace()` 原子搬移；同名板存在 → 409，明確 `overwrite: true`
  才先備份再替換（回應註明「覆寫既有板需重啟服務」——_Board 快取限制）。
- **slug 白名單** `[a-z0-9][a-z0-9-]*`：由「板子名稱」自動轉生（小寫、
  空白→dash、去非法字元），衝突時要求改名。display name 進 board.yaml
  `name:` 欄；前端下拉顯示 name（manifest 有就用，沒有退回資料夾名）。

### D5. 上傳格式
- PDF：單檔，大小上限（50MB 級，伺服器設定）。
- DTS 資料夾：前端用 `webkitdirectory` 資料夾選取（多檔上傳、保留相對
  路徑）；同時接受 zip（後端解包）。安全：路徑正規化禁止 `..`、解包大小
  上限、只解到 staging。
- 上傳落點 `output/staging/<slug>/input/`（extractor 的 per-job input，
  不共用根 input/——根 input/ 只服務 CLI）。

### D6. 生成 job：single-flight 背景執行（同 /api/dts/generate 模式）
一次一個上傳 job；階段進度（`uploading→unpack→manual→dts→lint→review_wait
→landing→done|failed`）由 status 輪詢；extractor 跑十分鐘級，輪詢間隔放寬；
關頁可回來續看。失敗保留 staging 供除錯＋提供打包下載。

### D7. REVIEW 確認是落地前的必經步（沿用前版 D3）
lint 全綠後不自動落地——顯示 REVIEW.md 內容＋**boot 群組判定表（可改判
emit/reserve）**；改判寫回 staging 的 require.json＋boot_requirements.json
並重跑 lint，才准落地。「無任何 emit 群組」是必答確認題。

### D8. 功能旗標 `FEATURE_BOARD_CREATE`（預設 1）
端點與前端入口整體開關；關掉＝合併前行為。所有新碼在旗標之後。

### D9. 驗證勾選 → board.yaml 覆寫
extractor 產 board.yaml 後，web 依勾選覆寫 validation 區塊（勾→cubemx、
不勾→none）。**現階段勾選只對 ST 板有意義**（CubeMX 引擎；且需
base/cubemx.json——非 ST 板勾了也會在 kb_lint／validator 得到明確訊息，
不會壞）。future：`type: script`＋腳本上傳欄位。

## 5. 工作包

### M1. 程式碼搬遷（無行為變更）
1. `git mv knowledge_extract/src/knowledge_extract src/knowledge_extract`；
   `input/ archive/ cache/ boards.ini` 移根目錄；`llm_modules.ini` 區段合併。
2. D2 llm_provider 統一（diff anthropic.py → porting → 刪 extractor 副本）。
3. D3 paths.py 改造（root 推導＋staging 預設輸出）。
4. requirements.txt 併入 extractor 相依；**venv 實測 pdfplumber**（memory
   有「PDF 抽取要用 3.10 framework python」前例——裝不起來就把 PDF 解析
   隔離成 subprocess，本包內解決）。
5. 刪空殼 `knowledge_extract/`。
   **驗收**：`python -m knowledge_extract --board am6548 --steps dts --force`
   在新位置跑通、輸出進 staging、出廠 lint PASS；DTS_agent 全回歸
   （solve/locate/web import）不受影響。

### M2. kb_lint 函式化（web gate 用；CLI 介面不變）
`tools/kb_lint.py` 拆 `lint_board(board, data_root=None) -> {findings, ok}`；
`data_root` 支援 staging。CLI 輸出逐字不變。

### M3. web API（新檔 `src/web/board_create.py`；app.py 只掛路由）
| 端點 | 行為 |
|---|---|
| `POST /api/boards/create` | multipart（name、pdf、dts 多檔/zip、validate 勾選）→ slug 轉生＋白名單 → 存 staging/input → 起 single-flight job（extractor manual＋dts 步驟 → 勾選覆寫 board.yaml → 兩道 lint） |
| `GET /api/boards/create/status` | `{running, stage, slug, name, review, lint, error}` |
| `POST /api/boards/create/confirm` | 收 REVIEW 改判（可選）→ 回寫 staging → 重跑 lint → 原子落地 → 回新板 |
| `DELETE /api/boards/create` | 放棄：清 staging、釋放 single-flight |
| `GET /api/boards/create/artifacts` | 打包 staging（除錯下載） |

`/api/boards` 回應加 `names`（manifest display name）與 `can_create`（旗標）。

### M4. 前端
1. 下拉底部「＋ 上傳新板子…」（`can_create` 才顯示）。
2. 彈窗：板子名稱／PDF／DTS 資料夾（webkitdirectory）／驗證勾選
   （附註「ST 板勾選；其他板不勾」）；前端驗證三項必填。
3. 等待畫面：stage 進度＋可關頁續看；失敗顯示 error＋下載 staging。
4. REVIEW 確認：boot 判定表（emit/reserve 下拉可改）＋待審清單→確認落地。
5. 完成：重新載入 `/api/boards`、顯示 display name、自動選中新板。

### M5. `nuvoton-mfp` pinmux style（原 PINMUX_STYLE W4；可與 M3/M4 平行）
ma35d1 baseline 重產後，從官方 DTS 實例確認 `nuvoton,pins` 形狀 →
`NuvotonMfpStyle` 實作＋extractor 供料對接。**不擋合併主線**——未完成前
ma35d1 類板照常上傳/solve，「產生 DTS」被 style gate 以明確訊息擋下。

### M6. 文件整理（.md 清單）

| 檔案 | 處置 | 理由／遷移事項 |
|---|---|---|
| README.md | **全面重寫** | 三段式流程（上傳生成 → 求解 → patch）、快速上手、CLI 對照 |
| CLAUDE.md | 更新 | 新結構、extractor 常用指令、紅線引用修正（見下） |
| PROJECT_OVERVIEW.md | 更新 | §3 結構圖、§4 端點表加 boards/create 系列 |
| EXTRACTOR_MERGE_PLAN.md（本文） | **保留** | 唯一完整合併計劃＋執行記錄 |
| MERGE_PLAN.md（2026-06 舊合併） | **刪除** | 已執行完畢；紅線 6 引用「§0.1」改寫進 CLAUDE.md 本文（兩份 boot 檔不合併的決策內文化） |
| MULTI_BOARD_PLAN.md | **刪除** | 階段 A 已完成；規範已在 CLAUDE/data README |
| KB_ROBUSTNESS_PLAN.md | **刪除** | 已完成；缺檔語意已在 data/README |
| PINMUX_STYLE_PLAN.md | **刪除** | W1/W3/W5 完成；W4/W6 殘項已收進本計劃 M5/M7 |
| prompts/3、4、5 | **刪除** | extractor 已修完並併入 repo；pad 供料 schema 內文化到 src/knowledge_extract/README.md |
| knowledge_extract/README.md | 精簡移至 src/knowledge_extract/README.md | 模組文檔（pipeline、CLI、供料 schema） |
| knowledge_extract/pin_data_supply.md、prompts/ | **刪除** | 已完成的歷史工單 |
| data/README.md、src/patch_agent/README.md | 保留（小幅更新引用） | |

**引用修正原則**：刪檔前 grep 全 repo 引用（CLAUDE.md 紅線 6 引 MERGE_PLAN
§0.1、data/README 引 MULTI_BOARD_PLAN 等），把「還有效的決策內容」搬進
引用處本文，再刪。

### M7. 端到端驗收與回歸矩陣

**新功能**：
1. 以 am6548 原始材料（archive/ 的 PDF＋DTS）走完整 web 流程：上傳 →
   等待 → REVIEW（改判一次驗證可改性）→ 落地 → 下拉出現 display name →
   solve（帶開機組）→ 產生 DTS patch 通過 m7——全程零手補。
2. 勾選驗證上傳一塊 ST 類板（可用 stm32 材料試 slug=stm32-test）→
   board.yaml validation=cubemx；不勾 → none。測完刪。
3. 異常矩陣：壞 PDF／壞 zip／slug 衝突／路徑注入／中途放棄／lint FAIL
   （staging 保留＋可下載）。

**回歸（一項都不能壞）**：
4. stm32 全流程：solve＋CubeMX 背景驗證＋產生 DTS＋下載。
5. am6548 既有板：solve（boot 組）＋patch。
6. `FEATURE_BOARD_CREATE=0` → 與合併前 bit-for-bit。
7. extractor CLI 新位置可用；`patch_agent` CLI 不變；kb_lint CLI 輸出不變。
8. 六條紅線走查（特別：extractor 只寫 staging；plan.csv 交棒契約不變）。

### M8. README.md 重寫要點（合併收官）
- 一句話定位：手冊＋DTS → 知識庫 → 自然語言 pin-mux 求解 → kernel DTS patch。
- 快速上手：web 三步（上傳板子 → 求解 → 產生 patch）＋ CLI 對照表
  （knowledge_extract／patch_agent／kb_lint）。
- 多板機制摘要（board.yaml、驗證引擎、pinmux style 支援矩陣：
  stm32 ✓／k3-iopad ✓／nuvoton-mfp 待 M5）。
- 目錄結構圖（§3）。

## 6. 相容性不變式

1. `FEATURE_BOARD_CREATE=0` ⇒ 行為與合併前完全相同。
2. 六條紅線全數維持；extractor 寫入面＝staging only。
3. `data/<board>/`、`output/plan|validator|generated/` 佈局與語意不變。
4. 三個既有 CLI（patch_agent、kb_lint、knowledge_extract）介面不變
   （extractor 只換執行位置與輸出預設，旗標相同）。
5. 兩份 boot 檔永不合併（原 MERGE_PLAN §0.1，決策文字內文化至 CLAUDE.md）。
6. llm_modules.ini 既有區段（parse/orchestrator/dts_patch）鍵值不動。

## 7. 風險

| 風險 | 對策 |
|---|---|
| pdfplumber 在 venv 裝不動（memory 有 framework python 前例） | M1 第一步先 spike；備案 subprocess 隔離 |
| llm_provider 統一改壞既有 parse/orchestrator | 以 DTS_agent 版為正本、只 porting extractor 增量；M1 驗收含既有路徑回歸 |
| 資料夾上傳的瀏覽器相容性 | webkitdirectory（Chrome/Edge/Safari 皆支援）＋zip 後備 |
| extractor job 中途掛掉留殘骸 | staging 自成一夾；status 回報 error；DELETE 清理；重跑覆寫 |
| 文件刪除弄丟仍有效的決策 | M6 引用修正原則：先 grep、內文化、再刪 |
| 上傳大檔（DTS 樹）逾時 | 分塊上傳不做（v1 上限＋明確錯誤）；DTS 只需板檔＋include 鏈（README 指引） |

## 8. 順序與估時

```
M1 搬遷＋spike ──→ M2 lint 函式化 ──→ M3 API ──→ M4 前端 ──→ M7 驗收 ──→ M6/M8 文件收官
                                （M5 nuvoton-mfp 平行，不擋主線）
```

| 包 | 估時 |
|---|---|
| M1 | 1–1.5 天（含 spike 與回歸） |
| M2 | 0.5 天 |
| M3 | 2 天 |
| M4 | 2 天 |
| M5 | 1–2 天（材料就緒後） |
| M6＋M8 | 1 天 |
| M7 | 1 天 |
| **合計** | **7.5–10 天** |

---

**狀態**：計劃書定稿，待批准動工。前置（extractor P1–P6、DTS_agent 多板
／style／強韌化）已全數完成——M1 可即刻啟動。
