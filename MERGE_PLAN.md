# MERGE_PLAN — solver_agent ＋ DTS_patch_agent 合併計劃

> **✅ 已於 2026-06-28 執行完畢**（Phase 0–6 全數完成，每 Phase 一個 git commit）。
> 兩個原專案的 git 歷史保存在 `.archive/*.bundle`（`git clone <bundle>` 可還原）。
> 驗證結果：locate 輸出與合併前基準逐字一致、diff_plan.json 語意一致、
> /api/solve 端到端正常、DTS 生成端到端 passed（dtc 編譯過、6 項檢查全 pass、
> 防偽 409 與 single-flight 409 皆驗證）。本文件保留作為合併的決策與設計記錄。

> 目標：把 `solver_agent/`（自然語言 → plan.csv）與 `DTS_patch_agent/`（plan.csv → DTS patch）
> 合併為**單一 `DTS_agent/` 專案**，形成「需求 → plan.csv →（使用者確認）→ DTS patch」
> 的一條龍、可分階段確認的流程。
>
> **本文件只是計劃，尚未執行任何搬移或改碼。** 撰寫日期：2026-07-05。
> 所有「差異事實」皆經實際 diff / schema 比對驗證（見 §3）。

---

## 0. TL;DR

1. **合併是低風險的**：兩專案的 `src/` 頂層套件名互不衝突（唯一重疊的
   `llm_provider/` 兩邊只差一行註解）；`.env` 兩邊 **byte-identical**（md5 相同）；
   `llm_modules.ini` 兩邊幾乎相同，且 solver 版**已預留 `[dts_patch]` 區段**。
2. **兩邊的路徑都集中管理**，且都以「專案根目錄」為錨點推算：
   patch 的 `src/patch_agent/config.py`（`parents[2]`）與 solver 的
   `src/util/dataio.py`（`src/../../`）搬進同一個根目錄後**錨點自動正確**，
   需要改的只有 config.py 裡 data 子路徑字串。
3. **data/ 一律以 solver 版為正本**：4 個檔案 byte-identical 直接去重；
   `peripheral_profiles.json` 的 patch 副本是**未同步的過時快照**（使用者確認），
   直接採 solver 版刪除舊快照（已知後果見 §3.2b）。唯一例外是 `require.json`
   ——兩邊**同名不同物**（schema 與用途完全不同），patch 版改名
   `boot_requirements.json` 完整保留（§5.3）。
4. 兩專案的介面本來就是 `output/plan/plan.csv`（欄位相同；patch 端讀檔已用
   `utf-8-sig`，BOM 有無皆相容）——合併後這個介面從「人工複製檔案」變成
   「同一顆 output/ 內自動銜接」。
5. 流程整合的核心是在既有 Flask（`src/web/`）加 3 支 API ＋前端一顆
   「產生 DTS」按鈕，複用 solver 既有的背景驗證（background thread）模式；
   **不改任何求解／產 patch 的核心邏輯**。

### 0.1 已拍板決策（2026-07-05 使用者確認）

| # | 決策 | 落點 |
|---|---|---|
| 1 | `require.json` 這個名字歸 **solver 版**（`base/require.json` 原名原位保留）；patch 版內容**完整保留**但**改名**為 `boot_requirements.json`（兩份是不同知識，不合併、不丟棄任一份） | §3.2a、§5.2、§5.3 |
| 2 | **`base/` 類資料一律以 solver 版為準**：patch 端的 af_table / all_peripheral / peripheral_profiles 平鋪副本是「solver 知識庫後來更新、patch 未同步」的**過時快照**，合併後**直接採 solver 版、刪除 patch 副本**（含 `peripheral_profiles.json`，不做過渡保留）。已知後果（OCTOSPI 家族檢查覆蓋）見 §3.2b，補救小修列 §10.1 | §3.2b、§5.2、§5.3、§10.1 |
| 3 | `output/validated/` 與 patch 的 plan.csv BOM 快照：**確認零功能影響後移除**（驗證證據見 §3.3；`output/validated/` 是 CubeMX 副產物、日後執行可能再生，無害） | §3.3、§8 Phase 1 |

---

## 1. 範圍與原則

### 1.1 本次要做

- 兩專案合併成單一 `DTS_agent/`（`data/`、`src/`、`output/` 三大夾），
  不再保留 `solver_agent/`、`DTS_patch_agent/` 兩個子資料夾。
- 去除重複資源：`llm_provider/`、data 檔、`llm_modules.ini`、`.env`、
  `.gitignore`、輸出資料夾。
- data/ 以 **solver_agent 的分類格式為標準**（`base/ dts/ bindings/ cache/`，
  權威文件 `data/README.md`），patch 專用資料以**新增分類**方式併入，
  不打散 solver 既有分類。
- 流程整合：前端顯示 plan.csv 後，加「是否繼續產生 DTS」的確認步驟，
  是 → 呼叫 patch pipeline；否 → 停在 plan 階段。

### 1.2 原則（紅線）

- **零功能改動**：合併期間不改任何求解、驗證、產 patch 的行為。
  唯一允許的程式碼修改是「路徑 repoint」（patch 的 `config.py`）與
  web 層**新增**端點/按鈕（不動既有端點）。
  （唯一知情例外：profiles 換用 solver 正本的 §3.2b 後果，§0.1 決策 2 已確認接受。）
- **尊重 solver 既有架構不變式**（`CLAUDE.md`）：特別是
  「輸出防偽——只接受伺服器保存的已驗證解」，新的 DTS 觸發 API 也必須遵守（§7.3）。
- **知識庫以 solver 為正本**：`base/`（與 `dts/`）類資料一律採 solver 版，
  patch 端的過時副本淘汰。唯一「同名不同物」的 `require.json` 不硬併：
  patch 版改名 `boot_requirements.json` 完整保留（它是 patch 驗證讀取的
  另一種知識，丟棄會壞功能）。

---

## 2. 現況盤點

| | solver_agent | DTS_patch_agent |
|---|---|---|
| 角色 | 自然語言 → pin assignment（plan.csv）＋ CubeMX 驗證 | plan.csv → kernel DTS patch（含編譯驗證與 LLM 修復） |
| 入口 | `venv/bin/python src/web/app.py`（Flask :5001）；CLI `src/main.py` | `PYTHONPATH=src python3 -m patch_agent [run\|locate\|dry-run\|validate]` |
| src 頂層套件 | `main.py`、`service.py`、`solver/`、`orchestrator/`、`validator/`、`llm_provider/`、`util/`、`web/` | `patch_agent/`（m1–m8＋cli）、`llm_provider/` |
| 路徑權威 | `src/util/dataio.py`（`_BOARD_FILES`／`board_paths()`；DATA=`src/../../data`） | `src/patch_agent/config.py`（`REPO_ROOT = parents[2]`，全部常數集中） |
| data 佈局 | `data/<board>/{base,dts,bindings,cache}/`（分類制，`data/README.md` 為權威） | `data/<board>/` 平鋪 ＋ `baseline/`（含官方 DTS 原始檔樹）＋ `dts_generation/` |
| output | `output/plan/`（plan.csv/xlsx）、`output/validator/`（CubeMX＋四套 DT）、`output/validated/`（**CubeMX 執行副產物，無程式引用**，見 §3.3） | `output/plan/plan.csv`（輸入，人工複製來的快照，帶 BOM）、`output/generated/`（產物） |
| LLM 設定 | `llm_modules.ini`：`[default][parse][dts_patch][orchestrator]` | `llm_modules.ini`：同上（僅註解差一字） |
| Python | `venv/`＝Python 3.10.11（flask、anthropic、google-genai、openpyxl…） | 無 venv；`patch_agent` 套件**純標準庫**（LLM SDK 走共用 `llm_provider`），另需外部 `dtc`/`gcc` |
| 文件 | `PROJECT_OVERVIEW.md`（總覽）、`CLAUDE.md`、`data/README.md` | `README.md` |
| 版控 | 皆非 git repo（各有 `.gitignore`） | 同左 |

兩專案文件都已明載「合併預定」，且介面一致：
`output/plan/plan.csv`，欄位 `peripheral,signal,pin,af`（G4 開啟時 solver 可能多
選填 `ic` 欄——patch 端用 `csv.DictReader`，多欄不影響）。

---

## 3. 重複／差異盤點（已驗證的事實）

### 3.1 byte-identical（可直接去重，零風險）

| 檔案 | solver 位置 | patch 位置 | 驗證 |
|---|---|---|---|
| `af_table.json` | `data/<b>/base/` | `data/<b>/`（平鋪） | `diff` 無差異 |
| `all_peripheral.json` | `data/<b>/base/` | `data/<b>/`（平鋪） | `diff` 無差異 |
| `signal_to_pin.json` | `data/<b>/dts/` | `data/<b>/baseline/` | `diff` 無差異 |
| `official_dts_peripheral.json` | `data/<b>/dts/` | `data/<b>/baseline/offiicial_dts_peripheral.json`（**檔名 typo**） | `diff` 無差異 |
| `.env` | 根目錄 | 根目錄 | **md5 相同** |
| `llm_provider/`（7 檔中 6 檔） | `src/llm_provider/` | `src/llm_provider/` | 僅 `anthropic.py` 差**一行註解**（patch 版多 `(the SDK, not this module)`） |
| `llm_modules.ini` | 根目錄 | 根目錄 | 僅註解差異（solver 版 `[dts_patch]` 標了「(future)」） |

附帶紅利：去重後 patch 端引用改指 `dts/official_dts_peripheral.json`，
**上游檔名 typo（offiicial）順帶消失**。

### 3.2 同名不同物（絕不可互相覆蓋）

**(a) `require.json` — 兩邊是完全不同的知識檔**

| | solver `base/require.json` | patch `require.json` |
|---|---|---|
| top-level keys | `boot_pin_locked`（groups＋pin_map 常數＋solver_action）、`gpio_must_pins` | `board_pin_locked`（peripherals 清單）、`peripherals`（開機必備 **DTS node** 知識：pwr/rcc/ddr/scmi…）、`recommended_not_strictly_mandatory` |
| 回答的問題 | 「solver 要把哪些**腳位**鎖成常數／保留？」 | 「DTS 裡哪些 **node** 是開機必備、哪些週邊必須留在官方腳位？」 |
| 消費者 | `util/dataio.py`（load_pin_locked / load_reserved / load_require_signals / load_gpio_pins） | `m2_validation_harness`、`m7_validator`（`require["peripherals"]`、`require["board_pin_locked"]`） |

→ 處置：**兩份都保留**，patch 版**改名**遷至 patch 專用分類（§5.3），消除同名混淆。

**(b) `peripheral_profiles.json` — patch 版是未同步的過時快照（2026-07-05 確認），直接採 solver 版**

- **差異的根因（使用者確認）**：solver 的知識庫後來有更新，patch 端沒有同步——
  patch 的副本是**舊快照**，不是刻意維護的另一套資料。`base/` 類資料一律以
  solver 版為唯一正本。
- 比對佐證與此一致：`families` 區塊經正規化比對**語意完全相同**
  （11 個 family、default/modes 皆同——共用核心沒漂移）；solver 版多出的
  `board`、`dts_label_aliases`、`system_policy`、`peripherals`（per-instance
  profile）是後來新增的知識，patch 端程式不讀這些欄位，直接換版**無害**。
- **唯一需要知情的行為差異在 `aliases`**（新舊快照方向相反）：
  - solver（新）：`{"CAN":"FDCAN", "OCTOSPI":"OCTOSPIM", "OCTOSPI1":"OCTOSPIM"}`（正規化到 OCTOSPIM）
  - patch（舊）：`{"CAN":"FDCAN", "OCTOSPIM":"OCTOSPI1"}`（正規化到 OCTOSPI1）
- 換版後果：patch 的 `_profile_family()`（`m2_validation_harness/harness.py:83`）
  是「查 alias → 去尾碼數字 → 小寫 → 查 families」。舊快照下
  `OCTOSPIM → OCTOSPI1 → octospi ✓`；採 solver 版後
  `OCTOSPIM →（無此 alias）→ octospim ✗ 不在 families → 回 None`，
  該家族的 **mode 完整性檢查會被靜默跳過**。影響範圍極小：EV1 上
  OCTOSPIM_P1 是 reserve_only、正常 plan 不會出現該家族；其他所有家族
  （I2C/SPI/UART/ETH/FDCAN/SDMMC…）行為完全不變。

→ 處置（§0.1 決策 2）：**合併時直接採 `base/peripheral_profiles.json`，
patch 副本刪除、config repoint**。OCTOSPI 家族檢查覆蓋的恢復是一行小修，
列 §10.1（後續、非合併必要）。

### 3.3 其他確認過的事實

- **plan.csv BOM**：patch 端現存快照帶 UTF-8 BOM（推測是從 solver 的
  `/api/export` 下載——該端點刻意輸出 `utf-8-sig` 給 Excel）。patch 讀取
  一律 `encoding="utf-8-sig"`（`harness.py:69`、`locate.py:41`），**有無 BOM 皆可**。
- **llm_provider 路徑錨點**：`.env` 與 `llm_modules.ini` 都從
  「`src/llm_provider/config.py` 往上三層＝專案根」解析
  （可用 `LLM_CONFIG_FILE` 覆寫）——合併後根目錄各留一份即可，**零改碼**。
- **patch 的 `REPO_ROOT = Path(__file__).resolve().parents[2]`**：
  合併後檔案位於 `DTS_agent/src/patch_agent/config.py` → `parents[2]` ＝
  `DTS_agent/` 根，**不需修改**。
- **patch_agent 套件無任何第三方 import**（純標準庫；LLM 走共用 provider）
  → 合併 venv 不需新增套件；只需外部工具 `dtc`、`gcc`（編譯驗證用，
  已有 `--no-compile` 逃生口）。
- **solver `output/validated/` 可安全刪除（零功能影響，已驗證）**：
  - 全 `src/`（含前端 app.js）grep `validated` —— 所有命中都是
    `output/validator/validated.ioc`（`script_gen.py` 的 saveas 目標）或
    `result.json` 的 `validated` 欄位；**沒有任何程式讀寫 `output/validated/` 路徑**。
  - 實際執行過的 `cubemx_script.txt` 中 saveas 目標只有 `output/validator/validated.ioc`；
    `output/validated/` 是 **CubeMX `config saveas` 自己產生的專案骨架副產物**
    （內含與 `output/validator/validated.ioc` **byte-identical** 的 ioc ＋
    空的 `CA35/DeviceTree/` 資料夾）。
  - 注意：因為是 CubeMX 的行為，**日後每次跑驗證可能再生成**——無害
    （位於覆寫制、gitignore 排除的 `output/` 內），刪掉即可、再出現不用理。
- **搬移不會破壞 validator 的絕對路徑**：`cubemx_script.txt` 裡記錄的是上次執行時
  的絕對路徑（甚至還是專案搬進 `DTS_agent/` 前的舊位置 `/Users/sam/Desktop/solver_agent/…`），
  證明 script 是每次執行由 `script_gen.py` 用**當下**根目錄重新組出來的——
  output/ 內殘存的舊路徑純屬歷史紀錄，合併搬移後自動正確。
- 兩專案皆無測試套件（solver 的 tests/ 於 2026-07-04 清理）→ 合併驗證靠
  §9 的煙霧測試清單。

---

## 4. 合併後目標結構

```
DTS_agent/
├── README.md                    ★新：合併系統總覽＋快速上手（兩段式流程）
├── PROJECT_OVERVIEW.md          solver 總覽（§5「與另一專案的合併介面」改寫為「已合併」）
├── CLAUDE.md                    工作說明（solver 版為底，補 patch pipeline 紅線與指令）
├── MERGE_PLAN.md                本文件（合併完成後可刪或歸檔）
├── llm_modules.ini              ★單一份（採 solver 版，去掉「(future)」註解）
├── .env                         ★單一份（兩邊本來就相同）
├── .gitignore                   ★合併版（見 §6.4）
├── requirements.txt             ★新：由 solver venv pip freeze 產生（patch 零新增依賴）
├── venv/                        ★重建（Python 3.10；venv 不可搬移，見 §8 Phase 0）
├── .claude/                     solver 的 .claude/（settings.json、skill/、tool/af_extract.py…）
│
├── data/
│   ├── README.md                分類權威文件（新增兩個分類的說明，見 §5.4）
│   └── stm32mp257f-ev1/
│       ├── base/                （不動）af_table / require / peripheral_profiles /
│       │                        all_peripheral / cubemx —— solver 手工核心
│       ├── dts/                 （不動）signal_to_pin / official_dts_peripheral
│       │                        —— 官方 DTS 解析；★成為兩專案共用（patch 端 repoint 到這）
│       ├── bindings/            （不動）board_components —— G4 IC 知識
│       ├── cache/               （不動）binding_cache —— 可拋棄
│       ├── baseline/            ★patch 移入：baseline.csv ＋ dts/（官方 kernel DTS
│       │                        原始檔樹 + include/ headers + MANIFEST.md）
│       └── dts_generation/      ★patch 移入：board_config / dts_property_bindings /
│                                fixed_connections / peripheral_node_alias
│                                ＋ gpio_pins.json（自平鋪層移入）
│                                ＋ boot_requirements.json（原 patch require.json 改名）
│
├── src/
│   ├── main.py  service.py      （solver，不動）
│   ├── solver/  orchestrator/  validator/  util/  web/     （solver，不動）
│   ├── llm_provider/            ★單一份（solver 版＋補回 patch 那行註解，含
│   │                            solver 專屬 system_prompt.md / test.py）
│   └── patch_agent/             ★patch 整包移入（m1–m8、cli、config；
│                                只改 config.py 的 data 子路徑，見 §6.1）
│
├── tools/                       ★patch 移入：grab_kernel_dts.sh / extract_board_data.py /
│                                extract_fixed_connections.py / derive_alias.py
│                                （重建 data 用；內部路徑字串同步 §6.2）
│
└── output/                      （覆寫制、gitignore 排除）
    ├── plan/                    plan.csv + plan.xlsx   ← 兩段流程的交棒點（單一位置）
    ├── validator/               CubeMX 驗證產物＋四套 device tree（solver 既有）
    └── generated/               kernel DTS patch 產物（patch 既有：generated.patch、
                                 *.generated.dts、各 report、llm_cache/）
```

命名區辨（文件要寫清楚，避免混淆）：
- `output/validator/devicetree/` ＝ **CubeMX 附帶生成**的四套 DT（kernel/u-boot/tf-a/optee-os，
  需放回 OpenSTLinux BSP 編譯）。
- `output/generated/` ＝ **patch pipeline 的正式產物**（可餵 Yocto 的 kernel patch＋
  完整 .dts）。兩者並存、用途不同，皆保留。

---

## 5. data/ 檔案處置對照表

### 5.1 solver 檔案：全部原地保留

`base/`、`dts/`、`bindings/`、`cache/`、`data/README.md` 完全不動
（solver 的 `dataio._BOARD_FILES` 也因此零修改）。

### 5.2 patch 檔案逐一處置

| 現位置（`DTS_patch_agent/data/<b>/`） | 動作 | 合併後位置 / 理由 |
|---|---|---|
| `af_table.json` | **刪除** | 與 `base/af_table.json` byte-identical；config repoint |
| `all_peripheral.json` | **刪除** | 與 `base/all_peripheral.json` byte-identical；config repoint |
| `baseline/signal_to_pin.json` | **刪除** | 與 `dts/signal_to_pin.json` byte-identical；config repoint |
| `baseline/offiicial_dts_peripheral.json` | **刪除** | 與 `dts/official_dts_peripheral.json` byte-identical；repoint 順帶修 typo |
| `peripheral_profiles.json` | **刪除** | 未同步的過時快照（§0.1 決策 2）；直接採 `base/peripheral_profiles.json`，config repoint；已知後果與補救見 §3.2b／§10.1 |
| `require.json` | **改名移入** `dts_generation/boot_requirements.json` | 與 solver require.json 同名不同物（§3.2a）；改名消歧義 |
| `gpio_pins.json` | **移入** `dts_generation/gpio_pins.json` | 它由 `tools/extract_board_data.py` 抽取，與 board_config / dts_property_bindings 同一產源，歸同夾 |
| `baseline/baseline.csv` | **移入** `baseline/baseline.csv`（整夾平移） | 官方預設 pin 快照，patch 專用 |
| `baseline/dts/**`（.dts/.dtsi/include/MANIFEST/.source_provenance） | **整夾平移** `baseline/dts/**` | 官方 kernel DTS 原始檔樹；m1 parser 的輸入 |
| `dts_generation/{board_config,dts_property_bindings,fixed_connections,peripheral_node_alias}.json` | **整夾平移** | patch 渲染用衍生資料 |

### 5.3 特殊檔案的最終狀態（§0.1 決策 1、2 已確認）

- `require.json` 名字歸 solver 版：`base/require.json` **原名原位、內容不動**，
  仍是 solver 的 require 知識唯一來源（`dataio._BOARD_FILES` 零修改）。
- `boot_requirements.json`（原 patch require.json）：**永久保留為獨立檔**——
  它裝的是另一種知識（開機必備 **DTS node** 清單、board_pin_locked 週邊表），
  是 patch 驗證模組（m2/m7）實際讀取的資料，與 solver 的 require.json
  回答不同問題，不存在「統一成一份」的目標；只是改名讓語意自明。
  **內容一字不動**（m2/m7 仍讀到完全相同的知識）。
- `peripheral_profiles.json`：**單一正本 `base/peripheral_profiles.json`**（solver 版），
  patch 過時快照刪除——合併後 data/ **不再有任何內容重複**。

### 5.4 `data/README.md` 增補

新增兩個分類的說明（沿用既有「依資料責任分類」的寫法）：

- `baseline/`：官方 kernel DTS 快照（原始 .dts/.dtsi＋include headers＋出處
  MANIFEST）＋官方預設腳位表 baseline.csv。**機械抓取**（`tools/grab_kernel_dts.sh`），
  錯了重抓不手改。DTS patch pipeline（m1 parser）的唯一 DTS 來源。
- `dts_generation/`：DTS 生成／驗證專用知識。工具抽取（gpio_pins、board_config、
  dts_property_bindings、fixed_connections、peripheral_node_alias ← `tools/`）＋
  手工整理（boot_requirements.json ← datasheet 交叉查證）。僅 `patch_agent` 消費。

並註明：`base/`＋`dts/` 五檔仍是板子偵測的必要檔（不變）；`baseline/`、
`dts_generation/` 是 **DTS-patch 功能的選配檔**——缺夾時 plan 流程照常，
只有「產生 DTS」功能不可用（web 層以此決定是否顯示按鈕，見 §7.2）。

---

## 6. 程式碼與設定變更點（唯一允許的修改）

### 6.1 `src/patch_agent/config.py` — 路徑 repoint（約 8 行字串）

| 常數 | 現值（相對 `data/<b>/`） | 改為 |
|---|---|---|
| `AF_TABLE` | `af_table.json` | `base/af_table.json` |
| `ALL_PERIPHERAL` | `all_peripheral.json` | `base/all_peripheral.json` |
| `PERIPHERAL_PROFILES` | `peripheral_profiles.json` | `base/peripheral_profiles.json`（直接採 solver 正本，§0.1 決策 2） |
| `REQUIRE` | `require.json` | `dts_generation/boot_requirements.json` |
| `GPIO_PINS` | `gpio_pins.json` | `dts_generation/gpio_pins.json` |
| `SIGNAL_TO_PIN` | `baseline/signal_to_pin.json` | `dts/signal_to_pin.json` |
| `OFFICIAL_DTS_PERIPHERAL` | `baseline/offiicial_dts_peripheral.json` | `dts/official_dts_peripheral.json` |
| `BASELINE`／`DTS_DIR`／`DTS_GEN`／`BASELINE_CSV`／`PLAN_CSV`／`OUTPUT*` | — | **不變**（相對層級在新根目錄下依然正確） |

`REPO_ROOT`（`parents[2]`）不需改（§3.3）。這是**全部**必要的程式碼修改；
m1–m8 一律經 config 取路徑，無散落字串（已 grep 驗證）。

### 6.2 `tools/` 內的路徑字串

`extract_board_data.py`、`derive_alias.py` 等輸出檔的目的地與 `source` 註記字串
指向舊佈局（`data/<b>/gpio_pins.json`、`data/<b>/baseline/dts/`）。
`baseline/dts/` 不變；**輸出目的地**需同步改為 `dts_generation/…`。
（這些是離線重建工具，平常不執行；改動不影響 runtime。）

### 6.3 `src/llm_provider/` 合併

以 solver 版為準（多 `system_prompt.md`、`test.py`），`anthropic.py` 採 patch 版
那行較清楚的註解。其餘 6 檔 byte-identical，直接單一份。

### 6.4 根目錄設定檔

- `llm_modules.ini`：留 solver 版，把 `[dts_patch]` 的「(future)」註解拿掉。
- `.env`：兩邊相同，留一份。
- `.gitignore`：合併為——`venv/ .venv/ env/`、`__pycache__/ *.pyc`、`.DS_Store`、
  `.pytest_cache/`、`node_modules/`、`output/`（整夾，涵蓋原 patch 的
  `output/generated/`）、`plan.inc`、`.env`。
- `requirements.txt`（新）：搬移前先在 solver venv `pip freeze` 產出；
  patch 零新增依賴（§3.3）。外部工具需求寫進 README：`dtc`（brew install dtc）、`gcc`。

### 6.5 文件

- 新根 `README.md`：合併系統定位、兩段式流程圖、三個入口
  （web UI／`src/main.py`／`python -m patch_agent`）、外部依賴（CubeMX、dtc/gcc 皆選配）。
- `PROJECT_OVERVIEW.md` §5「與另一專案的合併介面」→ 改寫為「兩段流程的交棒介面」。
- 原 patch `README.md` → 併入根 README 或移作 `src/patch_agent/README.md`
  （pipeline 細節仍有價值，建議後者，保持模組自帶說明）。
- `CLAUDE.md`：solver 版為底，追加：patch pipeline 指令、
  「patch 路徑一律走 `patch_agent/config.py`」紅線、兩種 DT 產物的區辨（§4 末）。

---

## 7. 端到端流程整合設計（新增，不動既有功能）

### 7.1 目標使用者流程

```
1. 前端輸入需求（/api/solve 或 /api/chat，既有）
2. 顯示 plan 表格（既有）＋ CubeMX 自動驗證狀態（既有）
3. 表格旁新增「產生 DTS」按鈕 → 確認對話框
4a. 使用者按「是」→ POST /api/dts/generate → 背景執行 patch pipeline
    → 前端輪詢進度 → 顯示結果摘要 ＋ generated.patch 預覽/下載
4b. 使用者按「否」／不按 → 流程停在 plan 階段（現狀行為）
```

### 7.2 新增 API（`src/web/app.py`，沿用背景驗證的既有模式）

| 端點 | 行為 |
|---|---|
| `POST /api/dts/generate` | 將**伺服器端保存的當前已驗證解**寫入 `output/plan/plan.csv`（複用 `emit_plan` 同一條防偽路徑），然後背景執行 patch pipeline。已在跑則回 409（single-flight，與 CubeMX 全域鎖同精神）。 |
| `GET /api/dts/status` | `{running, result:{passed, stop_reason, ask_user[], repair_rounds, checks{}, artifacts[], plan_fingerprint, ran_at}}` — 供前端輪詢；`plan_fingerprint` 沿用 validator 的 fingerprint 概念，把結果對回它所根據的 plan。 |
| `GET /api/dts/download` | 打包 `output/generated/` 為 zip（比照 `/api/validator/download`）。 |

實作載體二選一（建議 A）：
- **A. in-process**：`from patch_agent.m8_repairer import run as m8_run`，
  在背景 thread 呼叫 `m8_run(plan_csv=…, write=True)`（cli.py `cmd_run` 已示範完整用法，
  含報告落盤）。優點：直接拿到結構化結果物件；與既有 `_auto_worker` 模式一致。
- B. subprocess `python -m patch_agent run`：隔離性好、直接得 exit code（0/1/2），
  但要另行解析 report JSON。

結果對映（沿用 patch 既定語意）：`passed` → 成功；
`stop_reason ∈ {locator_blocked, needs_info, boot_conflict}` → 前端顯示
「需要人工介入」清單（`ask_user`）；其餘 → 失敗＋顯示 `failure_report.json` 摘要。

按鈕顯示條件：該板 `data/<board>/baseline/` 與 `dts_generation/` 齊備才顯示
（§5.4 的選配檔語意）；`dtc` 未安裝時仍可跑（pipeline 自帶 `--no-compile` 語意，
狀態回報中如實標示未編譯）。

### 7.3 必守不變式

- **防偽紅線延伸**：`/api/dts/generate` **不接受 client 提供的 rows**。
  plan 來源只能是伺服器 session 中保存的已驗證解（與 `emit_plan`／`run_validator`
  同一供給鏈）；「前端顯示的 plan」與「patch pipeline 讀到的 plan.csv」由伺服器
  保證一致（fingerprint 可驗）。
- patch pipeline 對 plan.csv 是唯讀消費者；產物只寫 `output/generated/`，
  與 CubeMX 產物（`output/validator/`）互不覆蓋。
- 歷史事故教訓照舊適用：任何測試/腳本要寫 output/ 一律導向暫存目錄。

### 7.4 前端（`src/web/static/`）

- plan 表格區塊新增「產生 DTS」按鈕＋確認框（「將以目前這份 plan 產生 kernel DTS patch，
  過程會呼叫 LLM，約需數十秒～數分鐘」）。
- 進行中：按鈕轉 spinner，輪詢 `/api/dts/status`（比照 validator 輪詢）。
- 完成：摘要卡（各 peripheral reuse/generate、validation checks、repair 輪數、
  LLM 用量）＋ `generated.patch` 內容預覽＋「下載 zip」。
- `needs human input`（exit 2 語意）：黃色警示卡列出 `ask_user` 項目。

---

## 8. 執行步驟（分階段，每階段可獨立驗證）

> 建議一次會話做完 Phase 0–4；Phase 5（流程整合）可獨立成第二個工作段。

### Phase 0 — 保全（不可跳過）

1. `git init` ＋ 首次 commit：把兩個專案現狀全部入庫（`.gitignore` 先就位，
   排除 venv/、output/ 的大檔快照可另酌）——**目前不是 git repo，這是唯一的回滾保障**。
   替代方案：整夾 `cp -R` 備份到桌面外部位置。
2. `solver_agent/venv/bin/pip freeze > requirements.txt`（venv 不可搬移，先留種子）。
3. 記錄基準行為（合併後比對）：
   - `cd solver_agent && venv/bin/python src/web/app.py` 可啟動、`/api/boards` 有板子；
   - `cd DTS_patch_agent && PYTHONPATH=src python3 -m patch_agent locate` 跑通
     （純定位、不需 LLM/API key）並保留輸出摘要。

### Phase 1 — 建立合併骨架（純搬移）

1. 根目錄放置：`llm_modules.ini`、`.env`、`.gitignore`（合併版）、`requirements.txt`。
2. `solver_agent/src/ → src/`、`solver_agent/data/ → data/`、
   `solver_agent/output/ → output/`、`solver_agent/.claude/ → .claude/`、
   `solver_agent/{PROJECT_OVERVIEW,CLAUDE}.md → 根`。
3. `DTS_patch_agent/src/patch_agent/ → src/patch_agent/`；
   `DTS_patch_agent/tools/ → tools/`；patch 的 `src/llm_provider/` **不搬**（用 solver 版，
   僅把 anthropic.py 那行註解補上）。
4. patch 的 data 依 §5.2 對照表搬移／刪除／改名。
5. 刪除確認過零功能影響的項目（§0.1 決策 3、證據 §3.3）：
   - `output/validated/`（CubeMX 副產物，無任何讀者；日後驗證再生成屬正常、無害）；
   - patch 的 `output/plan/plan.csv`（BOM 快照，被 solver 的 live plan 取代——
     patch 端讀的是同一路徑的現行檔，無功能影響）；
   - 兩專案空殼資料夾、`.DS_Store`。

**驗證**：`find . -name "*.json" | sort` 對照 §4 目標樹；
`git status` 檢查無意外遺留。

### Phase 2 — venv 重建

1. 根目錄 `python3.10 -m venv venv`（基準 Python 3.10.11）。
2. `venv/bin/pip install -r requirements.txt`。
3. 刪除舊 `solver_agent/venv/`。

**驗證**：`venv/bin/python -c "import flask, anthropic"` 成功。

### Phase 3 — patch 路徑 repoint

1. 依 §6.1 修改 `src/patch_agent/config.py`。
2. 依 §6.2 修改 `tools/` 輸出路徑字串。

**驗證**（皆不需 API key）：
- `venv/bin/python -c "from patch_agent import config; import os; [print(k, os.path.exists(str(v))) for k,v in vars(config).items() if k.isupper() and hasattr(v,'exists')]"`
  —— 所有 data 常數 True（output 類允許 False）。
- `PYTHONPATH=src venv/bin/python -m patch_agent locate` 輸出與 Phase 0 基準**一致**
  （現行 plan 不含 OCTOSPI 家族，profiles 換版不影響本比對；若日後 plan 含該家族，
  唯一預期差異是 §3.2b 所述 mode 檢查跳過）。
- `PYTHONPATH=src venv/bin/python -m patch_agent dry-run` prompt 正常組出。

### Phase 4 — solver 回歸

1. `venv/bin/python src/web/app.py` 啟動；`/api/boards` 列出 `stm32mp257f-ev1`
   （新增的 `baseline/`、`dts_generation/` 資料夾**不影響**板子偵測——
   `list_boards()` 只驗必要五檔存在）。
2. 走一次 `/api/solve`（例：「一組 I2C」）→ plan 正常；有裝 CubeMX 則看自動驗證。
3. CLI `venv/bin/python src/main.py` 煙霧測試。

### Phase 5 — 流程整合（§7 的新功能）

1. web 層新增三支 `/api/dts/*` 端點＋背景執行（建議 in-process，複用 `_auto_worker` 模式）。
2. 前端加「產生 DTS」按鈕、輪詢與結果卡。
3. 端到端：輸入需求 → plan → 按「產生 DTS」→ `output/generated/generated.patch`
   產出且 `validation_report.json` passed；按「否」路徑確認停在 plan（現狀不變）。

### Phase 6 — 文件收尾

§6.5 的 README／OVERVIEW／CLAUDE.md 更新；`data/README.md` 增補 §5.4；
本文件標記完成或移除。

---

## 9. 合併驗證清單（總表）

| # | 驗證 | 指令／方式 | 預期 |
|---|---|---|---|
| 1 | patch 定位（無 LLM） | `PYTHONPATH=src venv/bin/python -m patch_agent locate` | 與 Phase 0 基準輸出一致 |
| 2 | patch prompt 組裝 | `… -m patch_agent dry-run` | 正常列印 SYSTEM/USER prompt |
| 3 | config 路徑齊全 | Phase 3 的存在性檢查 | data 常數全 True |
| 4 | solver web | 啟動＋`/api/boards`＋一次 `/api/solve` | 板子偵測、求解、表格如常 |
| 5 | solver CLI | `venv/bin/python src/main.py` | 如常 |
| 6 | LLM 設定解析 | `venv/bin/python -c "from llm_provider.config import module_config; print(module_config('dts_patch'), module_config('orchestrator'))"` | 兩模組都解析到 provider/model |
| 7 | 完整 patch run（需 key＋dtc） | `… -m patch_agent run` | exit 0，產物齊（或 `--no-compile` 版通過） |
| 8 | CubeMX 驗證（選配） | web 自動驗證或 `run_validator` | result.json pass |
| 9 | 端到端（Phase 5 後） | UI 需求→plan→產生 DTS | generated.patch + report passed |

---

## 10. 後續（合併完成後的第二階段，非本次範圍）

### 10.1 恢復 OCTOSPI 家族的 mode 檢查覆蓋（選配小修）

profiles 已在合併時直接統一為 `base/` 正本（§0.1 決策 2）；殘留的唯一缺口是
§3.2b 所述：solver 版 aliases 下，patch 的 `_profile_family()` 對 OCTOSPIM／OCTOSPI
token 解不出 family，該家族的 mode 完整性檢查被跳過（EV1 正常 plan 不含該家族，
故非急件）。要恢復覆蓋，兩個等價選項擇一：
- **A（推薦，動 patch 一行碼）**：`_profile_family()` 在 alias 查完後，若結果
  去數字後不在 families，追加一次「OCTOSPIM→octospi」類的正規化（或直接
  strip `_P\d+`＋尾碼 M）。solver 檔案零改動。
- B（動資料）：在 `base/peripheral_profiles.json` 的 families 補一個 `octospim` 鍵
  （內容同 `octospi`）。需先確認 solver 的 `peripherals.py` 對多出來的 family 鍵無感。

完成判準：對含 OCTOSPI 家族列的測試 plan，`locate`／`validate` 會執行
mode 完整性檢查（不再回 None 跳過）。

### 10.2 其他機會（順記，均非必要）

- patch 的 `boot_requirements.json` 中 `board_pin_locked.peripherals`
  （SDMMC1/SDMMC2/OCTOSPIM_P1）與 solver `require.json` 的 boot 群組語意重疊，
  可改為由 solver 檔推導，消除第二處知識重複。
- 多板化 patch pipeline：`config.py` 的 `BOARD` 目前寫死 EV1，可改吃參數並沿用
  `dataio.board_paths()` 的偵測邏輯。
- 一鍵入口：根目錄加 `run.sh` 或 console_scripts，免記 `PYTHONPATH=src`。

---

## 11. 風險與對策

| 風險 | 等級 | 對策 |
|---|---|---|
| 無版控下搬移出錯無法回滾 | 高 | Phase 0 先 `git init`＋commit（或整夾備份）；每 Phase 一個 commit |
| 誤把兩份 `require.json` 當同物合併 | 高（已排除） | §3.2a 已確認不同物；計劃改名隔離，永不合併 |
| `peripheral_profiles.json` 換版後 OCTOSPI 家族 mode 檢查被跳過 | 低（已知並接受） | §0.1 決策 2：patch 副本為過時快照、直接採 solver 正本；EV1 正常 plan 不含該家族，實際影響≈0；§10.1 一行小修可恢復覆蓋 |
| venv 搬移後直接壞（activate/shebang 絕對路徑） | 中 | 不搬 venv；pip freeze → 根目錄重建（Phase 2） |
| patch config repoint 漏改某常數 | 中 | §6.1 對照表逐項核；Phase 3 存在性檢查腳本全綠才前進 |
| `list_boards()` 因新增資料夾誤判 | 低（已排除） | 偵測只驗必要五檔**存在**，多出的資料夾/檔案無影響 |
| plan.csv BOM／`ic` 選填欄相容性 | 低（已排除） | patch 端 `utf-8-sig`＋`DictReader`，實測相容 |
| 兩種 DT 產物（CubeMX vs patch）被混淆 | 低 | §4 末命名區辨寫入 README；下載 zip 分開 |
| DTS 產生誤用了與畫面不同步的 plan | 中（設計面） | §7.3：只吃伺服器保存解＋fingerprint 對账 |

---

## 12. 本次明確不做

- 不改任何求解／CubeMX 驗證／patch 生成的演算法與行為
  （唯一例外：profiles 換用 solver 正本帶來的 §3.2b 已知後果，使用者已確認接受）。
- 不做 §10.1 的 OCTOSPI 檢查覆蓋恢復小修（列後續）、不合併兩份 boot 知識檔（永不）。
- 不做多板化 patch pipeline、不加新 LLM provider、不動 G4（IC binding）旗標現狀。
- 不引入測試框架（維持煙霧測試清單），不重排 m1–m8 模組結構。
