# PROJECT_OVERVIEW — DTS_agent（pin-mux 求解段）

> STM32MP257F-EV1 的 pin-mux 求解與編排系統：把「自然語言的週邊需求」變成
> 「可佈線、經官方 STM32CubeMX 驗證的 pin assignment ＋ device tree 產物」。
> 本文是**第一段（求解）**的總覽文件（原 docs/ 里程碑文檔已於 2026-07-04 清理，
> 營運必要知識已濃縮至本文）。
>
> 2026-06 起本專案與 DTS_patch_agent 合併為單一 DTS_agent（見根目錄
> [README.md](README.md) 與 [MERGE_PLAN.md](MERGE_PLAN.md)）：第二段
> 「plan.csv → kernel DTS patch」由 `src/patch_agent/` 承接
> （其 pipeline 細節見 [src/patch_agent/README.md](src/patch_agent/README.md)），
> 兩段的交棒介面見 §5。

---

## 1. 專案目的與功能

**目的**：使用者用自然語言描述週邊需求（「我要 2 個 ETH、1 個 I2C，可以的話加
1 個 CAN」），系統輸出一份**正確、可開機、可溯源**的腳位分配（pin assignment），
並可用官方 STM32CubeMX 驗證、產出四套 device tree（kernel / u-boot / tf-a /
optee-os）。

**功能清單**：

| 功能 | 說明 |
|---|---|
| 自然語言 → IntentIR | parse LLM 把需求解析成結構化 intent（count / peripheral / signal 三種 level、optional 條件式需求、bootable_default） |
| CSP 求解 | AC propagation + backtracking；UNSAT 時產 Hall 鴿籠證據（哪些訊號擠在哪些腳） |
| 開機／安全世界約束 | 開機必備群組（SDMMC1/SDMMC2/USART2）以 pin_map 常數鎖定；secure/bootloader 保留週邊（I2C7=PMIC、OCTOSPIM_P1=U-Boot flash）instance 級封鎖；GPIO 鎖腳 |
| 歧義反問 | shared_pin / count_af / loose_pin 等六種歧義偵測，選項只來自合法候選，前端渲染成可點按鈕 |
| LLM 編排（agentic loop） | 模型自行決定呼叫工具的次數與順序：UNSAT 讀證據調整重試、條件式需求逐一退場、比較式需求各解一次 |
| 修改建議卡片 | 無解時提交「伺服器重解驗證過」的替代方案，前端一鍵採納 |
| CubeMX 官方驗證 | 解出的 assignment 逐腳送 CubeMX 套用官方規則，匯出 pinout diff 判定 pass/fail |
| Device tree 生成 | 驗證同時用 CubeMX 隱藏指令 `generate deviceTree` 產 kernel / u-boot / tf-a / optee-os 四套 DT（附加產物，失敗不影響驗證判定） |
| 外部 IC binding（停用中） | 板上 IC 對照（如 ETH PHY = RTL8211F-CG）＋ DT binding 查詢，見「功能旗標」 |
| 多板架構 | `data/<board>/` 一板一資料夾，自動偵測；換板不改程式 |

**核心設計不變式（紅線）**：

1. **LLM 永不直接指派腳位**——進 CSP solver 的需求必經 resolver 反腐層；LLM 只負責理解、編排、解釋。
2. **領域知識零寫死**——腳位/訊號/AF/週邊知識一律來自 `data/<board>/` 知識庫。
3. **輸出防偽**——`emit_plan` / `run_validator` 只接受伺服器保存的已驗證解，LLM 無法提供任意 rows。
4. **編排動作集鎖定**——六個工具，不隨意加寬。
5. **CubeMX 是綠色（確定性）元件**——只認它套用後的 pinout，LLM 不能改寫驗證結果。

---

## 2. 系統架構

```
                       ┌──────────────────────────────────────────────┐
 使用者（web UI）──────►│  Flask（src/web/）                            │
                       │   /api/solve      /api/chat                  │
                       └──────┬─────────────────┬─────────────────────┘
                              │                 │
              ┌───────────────▼──┐   ┌──────────▼──────────────────┐
              │ 確定性 pipeline    │   │ LLM 編排（orchestrator/）      │
              │ (service.py)      │   │ tool-use loop, MAX_STEPS=12  │
              │ parse LLM→Intent  │   │ 六個鎖定工具；trace 全記錄      │
              │ →澄清→求解         │   └──────────┬──────────────────┘
              └───────────────┬──┘              │ solve_pinmux（同一條路徑）
                              │                 │
                       ┌──────▼─────────────────▼─────────┐
                       │ 反腐層 resolver / counts           │  ← IntentIR 驗證、
                       │ （solver/）                        │     count 降階、保留週邊拒絕
                       │ CSP solver（Hall 證據）             │
                       └──────┬───────────────────────────┘
                              │ 讀
                       ┌──────▼───────────┐     ┌───────────────────────────┐
                       │ data/<board>/     │     │ validator/（CubeMX）        │
                       │ 知識庫（見 §4）     │     │ script_gen→runner→report   │
                       └──────────────────┘     │ + generate deviceTree      │
                                                └──────────┬────────────────┘
                                                           ▼
                                        output/plan/  +  output/validator/
```

兩條路徑共用同一個求解核心；編排路徑的每次 `solve_pinmux` 都重入
service 同一條反腐＋求解路徑，所以「LLM 自由重試」與「結果正確」不衝突。

---

## 3. 資料夾結構

```
DTS_agent/
├── README.md                合併系統總覽＋快速上手（兩段式流程）
├── PROJECT_OVERVIEW.md      本文（第一段：求解總覽）
├── CLAUDE.md                Claude Code 工作說明（指令、紅線、慣例）
├── MERGE_PLAN.md            兩專案合併計劃與決策記錄（2026-06 執行完畢）
├── llm_modules.ini          LLM provider 選型（[parse]/[orchestrator]/[dts_patch]）
├── .env                     API keys 等環境變數
├── requirements.txt         Python 相依（venv 重建用）
├── data/                    板級知識庫（詳見 data/README.md）
│   └── stm32mp257f-ev1/
│       ├── base/            手工核心：af_table.json（pin↔AF↔signal 全表）、
│       │                    require.json（開機鎖定/保留/GPIO 鎖腳）、
│       │                    peripheral_profiles.json（週邊展開模板）、
│       │                    all_peripheral.json（參考索引）、
│       │                    cubemx.json（CubeMX 板級常數 + DT mode 對照）
│       ├── dts/             官方 DTS 解析：signal_to_pin.json（官方預設腳位）、
│       │                    official_dts_peripheral.json（官方啟用週邊）
│       ├── bindings/        board_components.json（板上外部 IC 對照，雙重驗證）
│       ├── cache/           binding_cache.json（自動快取，可整夾刪除）
│       ├── baseline/        官方 kernel DTS 快照（第二段專用）：baseline.csv＋
│       │                    dts/（.dts/.dtsi＋include headers＋MANIFEST）
│       └── dts_generation/  DTS 生成/驗證知識（第二段專用）：board_config、
│                            dts_property_bindings、fixed_connections、
│                            peripheral_node_alias、gpio_pins、boot_requirements
├── src/
│   ├── main.py              CLI 入口（solver 實驗）
│   ├── service.py           確定性 pipeline（parse→澄清→optional 拆解→求解）
│   ├── solver/              CSP 核心：solve.py（求解+Hall）、resolver.py（反腐層）、
│   │                        peripherals.py（三層展開）、counts.py（count 降階）、
│   │                        clarify.py（歧義偵測與反問）、runner.py
│   ├── orchestrator/        agent.py（tool-use loop）、tools.py（六工具）、
│   │                        session.py（伺服器端 session）、system_prompt.md
│   ├── validator/           script_gen.py / runner.py / report.py（CubeMX 三段式）
│   ├── patch_agent/         第二段：DTS patch pipeline（m1–m8＋cli＋config.py
│   │                        路徑集中；細節見其 README.md）
│   ├── llm_provider/        anthropic / gemini / local_lm 抽象（兩段共用）、
│   │                        parse 的 system_prompt.md
│   ├── util/                dataio.py（知識庫路徑唯一權威 + I/O）、csv2xlsx.py
│   └── web/                 app.py（Flask，含 /api/dts/* 第二段觸發）+ static/
├── tools/                   第二段 data/ 重建工具（grab_kernel_dts、extract_*、
│                            derive_alias；平常不需執行）
├── output/                  執行期產物（覆寫制，可整夾刪除；.gitignore 排除）
│   ├── plan/                plan.csv + plan.xlsx        ← 兩段流程的交棒點
│   ├── validator/           cubemx.log、pinout.csv、result.json、validated.ioc、
│   │                        devicetree/{kernel,u-boot,tf-a,optee-os}/
│   └── generated/           第二段產物：generated.patch、*.generated.dts、
│                            diff_plan/locator/generation/validation report、llm_cache/
└── venv/                    Python 虛擬環境（3.10；requirements.txt 重建）
```

---

## 4. API 端點與編排工具

**端點**（`src/web/app.py`）：

| 端點 | 用途 |
|---|---|
| `GET /api/boards` | 板子自動偵測（data/ 下必要檔齊全的資料夾） |
| `POST /api/solve` | 確定性路徑：text 或 intent+question+option（反問接續） |
| `POST /api/chat` | 編排路徑：message／adopt（採納建議卡）；回 reply/plan/suggestions/clarify/validator |
| `POST /api/export` | per-message 匯出 csv/xlsx |
| `GET /api/validator/status` | 最近一次驗證結果摘要（result.json）＋ `running`（背景自動驗證進行中，前端輪詢用） |
| `GET /api/validator/download` | 打包 output/validator/ 為 zip（遞迴含 devicetree/） |
| `POST /api/dts/generate` | 第二段觸發：以**伺服器保存的最後一份 SAT plan** 產生 kernel DTS patch（client 只傳 fingerprint 指認畫面上的 plan，不一致回 409——防偽紅線延伸）；single-flight 背景執行 |
| `GET /api/dts/status` | `{available, running, result}`——available=該板具備 baseline/＋dts_generation/ **且等於 patch_agent/config.py 的 BOARD（現寫死 stm32mp257f-ev1；多板化見 MERGE_PLAN §10.2）**；result 帶 fingerprint 對回它所根據的 plan |
| `GET /api/dts/download` | 打包 output/generated/ 為 zip（generated.patch、generated.dts、各 report） |

**編排工具**（鎖定動作集，`src/orchestrator/tools.py`）：

| 工具 | 說明 |
|---|---|
| `solve_pinmux(intent, board)` | 唯一取得腳位的方式；sat/unsat(+Hall)/clarify/invalid |
| `get_capabilities(board)` | families/modes/standalone/boot_provided/reserved_instances，全資料驅動 |
| `emit_plan(board, format)` | 寫 output/plan/（防偽：只寫伺服器保存的解） |
| `run_validator(board)` | CubeMX 驗證＋DT 生成；每輪最多 3 次（初驗+2 輪修復，超過回 blocked，伺服器強制）。**另有自動驗證**：web 層對每個新 SAT plan 排入背景驗證（latest-wins 佇列、CubeMX 全域鎖序列化），result.json 的 `validated.fingerprint` 讓前端把結果對回正確的 plan |
| `propose_suggestion(summary, intent)` | 建議卡片；伺服器重解驗證 sat 才收，每輪最多 3 張 |
| `lookup_binding(peripheral)` | 外部 IC/binding 查詢（G4，flag 停用中，見 §6） |

---

## 5. 兩段流程的交棒介面（原「與另一專案的合併介面」，2026-06 合併完成）

第一段（求解）產生兩組產物；第二段（`src/patch_agent/`）經同一顆 `output/`
自動銜接，不再需要人工複製檔案：

**`output/plan/plan.csv`**——pin assignment 的正式輸出，也是第二段的唯一輸入
（plan.xlsx 由 `write_plan`／`emit_plan(fmt=xlsx)` 另行產生，非每條路徑都更新）。
web 流程中由 `POST /api/dts/generate` 在觸發當下以**伺服器保存的解**覆寫落地
（防偽；同時在 output/generated/plan.used.csv 留一份 run 專屬快照供 pipeline
讀取與溯源），CLI 流程則由 `emit_plan`／`write_plan` 產生：

```csv
peripheral,signal,pin,af
I2C2,I2C2_SCL,PB5,9
I2C2,I2C2_SDA,PB4,9
...
```

- 欄位：`peripheral`（instance 名）、`signal`（完整 signal 名）、`pin`（pad）、
  `af`（整數 alternate function 號）。G4 開啟時多一個選填 `ic` 欄（向後相容）。
- 覆寫制、單一位置；`/api/export` 亦可 per-message 產出同格式。

**`output/validator/`**——CubeMX 驗證與 DT 產物：

- `result.json`：`{status: pass|fail|error, conflicts[{pin,signal,message}],
  checked_pins, devicetree{status,files[]}, ran_at, …}`
- `pinout.csv`（CubeMX 實際套用的 pinout）、`cubemx.log`、`validated.ioc`
- `devicetree/{kernel,u-boot,tf-a,optee-os}/`：`stm32mp257f-*-mx.dts(i)` ＋
  Makefile/conf.mk。**注意**：這些 DT 不是自包含的——它們 `#include` 各元件
  原始碼樹裡的 SoC 基底 dtsi（如 `stm32mp257.dtsi`），需放回對應版本的
  OpenSTLinux BSP 各元件樹編譯；內容以 plan/pinout.csv 為準（CubeMX 板模板
  會帶入少量板級節點）。

---

## 6. 功能旗標與環境變數

| 變數 | 預設 | 說明 |
|---|---|---|
| `FEATURE_IC_BINDING` | `0` | G4 外部 IC binding／plan ic 欄。**實作完整但停用**。gate 四點：service 的 assignment ic 欄、emit_plan 的 ic 欄、`_active_tools()` 是否註冊 lookup_binding、system prompt 區塊剔除（`<!-- IC_BINDING:BEGIN/END -->` 標記對）。設 `1` 即全鏈路恢復。 |
| `STM32CUBEMX_PATH` | 自動掃描 | 覆寫 CubeMX 執行檔位置。未安裝時 validator 回 error、DT 生成靜默跳過，不影響其他功能。 |
| `PORT` | `5001` | Flask 埠。 |

啟動：`venv/bin/python src/web/app.py`

---

## 7. CubeMX 整合要點（維護必讀）

實測結論（2026-07-03/04 spike + 端到端驗證），`src/validator/` 依此實作：

- **執行方式**：`<binary> -i` + stdin 灌 script（macOS 的 `-q <script>` 停在 GUI
  事件圈不可用）。macOS 執行檔 `MacOs/STM32CubeMX` 是指向 `Resources/STM32CubeMX`
  的 symlink，自動更新器替換 bundle 有短暫懸空視窗——runner 掃兩輪、symlink
  目標列為獨立候選。每次啟動 JVM+db 約 60–90 秒。
- **驗證判定**：`set pin` 非法指派會被 CubeMX **靜默拒絕**（`0 KO`），所以不解析
  自由文字，而是 `csv pinout` 匯出實際 pinout 與期望 diff = 衝突清單（唯一真相）。
- **DT 生成配方**（隱藏指令 `generate deviceTree <dbPath>/ <projectPath>
  <manifestVersion> <dtGenDir>`），四個坑：
  1. 基底必須 `config load <AllConfig 板 .ioc>`——tinyload 缺 RIF context 會
     NPE；**非 AllConfig 版生成的 DT 沒有 pinctrl 群組**；`loadboard` 走進
     10 分鐘以上的 pack 載入。載入後 `clearpinout`（殘留僅 OCTOSPIM_P1 開機
     flash 腳，與 reserve_only 語意一致）。
  2. 必須先 `project name` + `project path`（DTGen 讀 ProjectManager.ProjectName；
     dts 檔名取 project path 的目錄名）。
  3. dbPath 結尾必須帶 `/`（內部天真字串串接）。
  4. DT 節點由 IP mode 驅動——順序鐵律 `set pin → csv pinout（判定定案）→
     set context/set mode → generate deviceTree`；mode 失敗只損失該週邊的 DT
     節點，不影響驗證。mode 對照表在 `data/<board>/base/cubemx.json` 的
     `dt_modes`（family 前綴→mode 名，來源 `db/mcu/IP/<family>-<ver>_Modes.xml`）。
- **已知限制**：mode 對映是 family 級預設（USART 一律 Asynchronous——要求
  CTS/RTS 時腳位仍會驗證，但 DT pinctrl 群組可能不含它）；`ETH → RGMII
  (Reduced GMII)` 含空白的 mode 名未經真跑驗證，失敗僅 ETH 不進 DT。

**歷史事故教訓**：會寫 `output/` 的測試/腳本一律把 OUTPUT 導到暫存目錄——
曾發生測試以假 error 覆寫真 result.json、把前端狀態打壞的事故（三次）。

---

## 8. 知識庫維護（摘要，詳見 data/README.md）

- 路徑唯一權威：`util/dataio.py` 的 `_BOARD_FILES` / `_BOARD_FILES_OPTIONAL`，
  程式一律走 `board_paths(board)`。
- 分類語意：`base/`+`dts/` 五檔必要（缺一該板不出現在清單）；`bindings/`、
  `cache/` 選配（缺檔優雅降級）。`cache/` 隨時可整夾刪除。
- 新增板子：`mkdir data/<board>/{base,dts}` 備齊五檔即自動偵測，
  agent / solver 架構零改動。
