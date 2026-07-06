---
name: stm-agent-project-goals
description: stm_agent (BSP Agent) 的整體專案目標、三階段定義與現有 runtime 模組對應關係。當需要解釋專案目的、判斷某需求屬於哪個階段、或確認某個模組是否該負責某項功能時,先讀這份。
---

# stm_agent — BSP Agent 專案目標與現況

> ⚠ **過時聲明（2026-06 合併時標記）**：本文所述的 `stm_agent/` 17 模組樹
> 已不存在。現行專案是合併後的 `DTS_agent/`（src/ = solver、orchestrator、
> validator、patch_agent、llm_provider、util、web），架構請以根目錄
> README.md 與 PROJECT_OVERVIEW.md 為準。本文僅保留「三階段目標」的
> 歷史脈絡參考，模組對應關係一律勿再引用。

## 1. 專案目的 (來自《清大產碩 — 專案需求說明》PDF)

開發**通用的 BSP Agent**,協助 RD 在 STM32MP25 系列 MPU 開發初期,從自然語言需求自動完成:

1. **可行性 & 衝突性評估** — 判斷 MPU 本身能否滿足需求 (Bus / Pin / AF)
2. **Device Tree 生成** — SoC Pin Assignment + Peripheral IC
3. **編譯與驗證** — 跑 dtc / dt-schema / kernel build
4. **除錯與自我修正** — 採 ReAct / Refiner 架構 (本專案實作為 **PAVR**:Plan-Act-Verify-Reflect)

**最終使用場景**:`需求 → BSP Agent → 評估報告 + DTS → 電路設計 → Layout → 板子`。
**開發階段**:因需要實板驗證,先以現有板子配置作為 ground truth 反推驗證。

---

## 2. 三階段定義 (專案推進路線)

### 階段一 — STM32MP257F-EV1 官方板,反推驗證

**輸入形式**(兩種):

| 子模式 | 輸入範例 | 預期輸出 |
|---|---|---|
| Bulk Spec (不指定腳位) | `HDMI*1 / ADC*1 / ETH*2 / Switch*1 / I2C*3 / I3C*1 / CAN*2 …` | 可行/衝突性評估 Excel(含選定的 Bus & 腳位)+ 基礎 DTS |
| 明確腳位 | `SPI8 腳位 PZ2(AF3) & PZ0(AF3) / usart6 腳位 PF13(AF3) & PG5(AF3)` | 完整 Device Tree |

**驗證方式**:
- 與 SoC Spec 比對 Bus / 腳位是否衝突
- LLM 產出的 DTS 與**官方 EV1 DTS**比對應一致

**官方參考檔**:
- 周邊介面:`stm32mp2-dis/.../linux-stm32mp/arch/arm64/boot/dts/st/stm32mp257f-ev1.dts`
- Pinctrl:`stm32mp2-dis/.../linux-stm32mp/arch/arm64/boot/dts/st/stm32mp25-pinctrl.dtsi`

### 階段二 — Delta Custom Board (台達自製板)

**輸入形式**:同階段一兩種,但 ground truth 改為 `DeltaCustom.xlsx` (148 pin rows / 22 周邊)。

**追加需求**:支援週邊 IC 描述,例如「ETH1 接 DP83867 PHY Addr 0x01 / ETH2 接 DP83867 PHY Addr 0x02 / I2C2 接 RTC S-35390A」→ 加入完整 DTS(含 child node)。

**驗證方式**:
- 與 `DeltaCustom.xlsx` 比對選擇是否符合實板
- 與 golden sample DTS 比對 + 上板開機 + 功能測試

### 階段三 — 泛用性測試 (尚未上實板)

**輸入形式**:**不指定腳位**讓 Agent 自行規劃,輸入更多樣的 IC(DP83867 / DP83862 / S-35390A / SSD1306 …)。

**驗證方式**:人工 Review(無實板)。

---

## 3. Runtime 模組對應 (現況 — 以實際 [stm_agent/src/](../../../stm_agent/src/) 為準)

> 模組成熟度請以原始碼為準,不要憑記憶。Decompiled / `.broken` 檔案是舊版備份,可忽略。

| 模組 | 階段對應 | 角色 |
|---|---|---|
| [requirement_parser/](../../../stm_agent/src/requirement_parser/) | 1 / 2 / 3 | NL 需求 → 結構化 entries;含 `clarification.py` (多輪澄清對話) 處理 Bulk Spec |
| [knowledge_retriever/](../../../stm_agent/src/knowledge_retriever/) | 1 / 2 | 載入 `data/boards/<board>/` 的 board_profile / pinmux_constraints / dts_property_bindings;`--board {stm32mp257f-ev1,delta_custom}` |
| [feasibility/](../../../stm_agent/src/feasibility/) | 1 / 2 / 3 | **可行/衝突性評估表** 的核心:`assessor.py` 產 17 欄 `FeasibilityRow`,含 AF 查表 / 板層 baseline / boot-critical / verdict;`csv_writer.py` 出 UTF-8-BOM Excel-friendly CSV |
| [constraint_solver/](../../../stm_agent/src/constraint_solver/) | 1 / 2 | Pin/AF 衝突解算與 remap;產 `plan.json` 的 projection |
| [synthesis/](../../../stm_agent/src/synthesis/) | 2 / 3 | 3-tier patch 合成:`router.py` deterministic → catalog (`ic_catalog/`) → LLM (`llm_synthesizer.py`,強制 binding citation) |
| [dts_patch_generator/](../../../stm_agent/src/dts_patch_generator/) | 1 / 2 / 3 | 產 `&label { … }` overlay fragments,只動有提到的 peripheral |
| [verification_agent/](../../../stm_agent/src/verification_agent/) | 1 / 2 / 3 | 配 `tools/dtc_runner.py` / `tools/stm32_pin_checker.py` / `tools/dt_validate.py` 跑 L1(dtc) + L2(dt-schema) + L3(re-parse) + L4(by-IC-type rules) |
| [workflow/](../../../stm_agent/src/workflow/) | 1 / 2 / 3 | PAVR orchestrator:`orchestrator.py` 主流程、`reflection.py` 反思迴圈 (N ≤ 3)、`budget.py` token 預算、`state.py` 狀態機;`batch.py` 跑批次 |
| [memory/episodic.py](../../../stm_agent/src/memory/episodic.py) | 1 / 2 / 3 | Reflexion 用的 episodic memory,JSONL keyed by 16-hex requirement fingerprint |
| [yocto_build_agent/](../../../stm_agent/src/yocto_build_agent/) | 1 / 2 | 階段「編譯與驗證」中的 build 段:bitbake wrapper + log 規則分類 |
| [flash_agent/](../../../stm_agent/src/flash_agent/) | 2 | 燒錄到實板 (STM32CubeProgrammer / USB DFU);階段三無實板 → 不跑 |
| [llm_provider/](../../../stm_agent/src/llm_provider/) | 1 / 2 / 3 | provider 抽象 (mock / gemini / anthropic / openai);多輪對話 + JSON mode |
| [stage_planner/](../../../stm_agent/src/stage_planner/) | — | 目前仍是 stub (dataclass only),計畫由 `workflow/orchestrator.py` 接管 |
| [env_checker/](../../../stm_agent/src/env_checker/) | 1 / 2 | 啟動時檢查 host 工具鏈 (dtc / kernel YAML / SDK) |
| [workspace_adapter/](../../../stm_agent/src/workspace_adapter/) | 1 / 2 | 對接 `stm32mp2-dis/` 原始 BSP workspace |
| [tools/](../../../stm_agent/src/tools/) | 1 / 2 / 3 | 純工具(不可呼叫 LLM):dtc / pin checker / dt_validate |
| [util/console.py](../../../stm_agent/src/util/console.py) | — | 多輪對話 stdout/stderr/warnings 捕獲,避免 UI 框格被污染 |

**Agent vs Tool 邊界**:`agents/`(及 `synthesis/`、`workflow/`)可呼叫 LLM;`tools/` 不行。若需在 tool 加 LLM,改放到對應 agent。

**`requirement_parser/` 內的 LLM 邊界(2026-06-04 更新)**:`count_expander.py` / `clarification_gate.py` 純 deterministic **不呼叫 LLM**;`parser.py`(NL→Requirement)、`count_intent.py`(NL→(type,count),見 [[stm-agent-nl-count]])、`interactive_repair.py`(僅**答案解讀**,問題產生 deterministic,見 [[stm-agent-interactive-repair]])可呼叫 LLM,皆 deterministic-first、離線安全 fallback。`constraint_solver/global_allocator.py`(CSP 配 pin)純 deterministic。

---

## 4. 階段 ↔ 輸出檔對應

| 輸出檔 | 用途 | 對應階段 |
|---|---|---|
| `outputs/latest/parse.json` | `requirement_parser` 結構化結果 | 1 / 2 / 3 |
| `outputs/latest/plan.json` | constraint_solver projection (含 selected pin/AF) | 1 / 2 / 3 |
| `outputs/latest/plan.csv` | **可行/衝突性評估 Excel** (PDF 預期輸出) | 1 / 2 / 3 |
| `outputs/dts/<name>.dts` & `outputs/patches/…` | DTS overlay 或完整檔 | 1 / 2 / 3 |
| `outputs/latest/dialogue.log` | Bulk Spec 多輪澄清對話的 log | 1 / 2 / 3 |
| `outputs/memory/episodic.jsonl` | Reflexion 失敗教訓累積 | 1 / 2 / 3 |

---

## 5. Board Selection 規則 (硬性,不可違反)

- 預設 `--board stm32mp257f-ev1` (階段一 EV1)
- 階段二 / 用戶**明確要求** Delta Custom 時才用 `--board delta_custom`
- 沒提及板子的 NL 需求 → 一律走 EV1,不要假設使用 Custom Board

---

## 6. 還沒到位的地方 (對應 PDF 三階段檢核)

1. **階段一 Bulk Spec** 已改 baseline-aware 展開 (`count_expander`,2026-05-27);i2c3 已在 profile (舊「缺 i2c3」已解)。剩 HDMI / Switch 無原生 block:Plan 的 suggestion generator 會點名 ADV7533 / KSZ9563 並標 unmatched,但**還沒自動補進 DTS** — 詳見 [[project-bulk-spec]] 與 [[project-stm-agent-baseline-plan]]
2. **階段二的 child node 合成** (PHY Addr / RTC IC) 已可用 catalog + LLM router,但 catalog 只有 5 個種子 IC;suggestion generator 已會點名 ADV7533 / KSZ9563 / DP83867 / S-35390A,惟這些**種子檔尚未建立** (影響 suggestion 與 child-node 合成品質)
3. ~~**階段三泛用性**:在不指定腳位時,GlobalPinAllocator 尚未實作~~ ✅ **已實作 (2026-06-04,[[stm-agent-global-pin-allocator]] 全 5 步)**:L1 `plan_merger._reconcile_pin_occupancy`(揪同次 plan 多列撞同一 pin、止血) + L2 `constraint_solver/global_allocator.py`(CSP 回溯 + MRV + baseline/boot-critical 預佔,不指定腳位時跨週邊一起配 variant、源頭避撞)。已修真實 silent bug:同次 enable spi8+i3c4(共用 PZ0/PZ1)現自動把 i3c4 配到 i3c4_pins_a、0 撞。
4. **stage_planner 仍是 stub**,跨檔編輯走 dts_patch_generator 的 ad-hoc 邏輯
5. **flash 階段** 僅支援開發板;階段三人工 review 無需此模組
6. **互動式澄清/修復**:已重做完成 (2026-06-04,[[stm-agent-interactive-repair]] Steps 1–5)。裸 bus 自動選、boot-critical deterministic Fail、只在「數量湊不到 / peripheral 不存在」兩底線才問、移除 `--clarify` 依賴、LLM 只解讀回答。舊的 C1/C2 反問 + 多輪 LLM 對話已退役。

---

## 6.5 Plan / Feasibility 流程的權威 spec

整個 Plan flow (board baseline 載入 / merge / conflict resolver / TUI logging) 的詳細規劃在 [stm-agent-baseline-plan](../stm-agent-baseline-plan/SKILL.md)。任何 `cmd_plan` / `feasibility/` / `constraint_solver/` 修改前都先讀那份。**互動式澄清/修復**(clarification_gate / clarification / interactive_repair / `_run_repair`)的權威是 [stm-agent-interactive-repair](../stm-agent-interactive-repair/SKILL.md)(已實作 Steps 1–5)。

---

## 7. 何時讀這份 skill

- 用戶提到「PDF」「階段一/二/三」「BSP Agent」「Charlie」「清大產碩」「專案目標」「Delta Custom」「DeltaCustom.xlsx」「golden sample」
- 接到不確定屬於哪個階段的需求(例如:有 PHY Addr → 階段二;有腳位但無 IC → 階段一/三)
- 規劃新模組前,要先看現有 17 個 src/ 子模組是否已涵蓋
- 寫驗證腳本前,要先看該階段的「驗證方式」(EV1 比對官方 DTS / Delta Custom 比對 xlsx + 上板 / 階段三人工 review)
