# Prompt 3: Knowledge Extractor 知識庫完整性升級（am6548 實測回饋）

> 背景：2026-07 以 extractor 產出的 am6548 知識庫接入 DTS_agent 實測，
> 發現五個缺口——症狀是「plan 不帶開機必備腳」與「產生 DTS 反問不出現」。
> 五個缺口**全部可由手冊／kernel DTS 生成**，本計劃把它們納入 extractor
> 的輸出規格。與 [2_upgrade_knowledge_extractor.md](2_upgrade_knowledge_extractor.md)
> 的關係：本計劃是其 Phase 2/3 的**規格增補與修正**，非取代。

## 實測症狀 → 根因對照（為什麼要做這五項）

| 症狀 | 根因 |
|---|---|
| 「給我一個 I2C」的 plan 真的只有 I2C，無開機組 | require.json 五個 boot 群組全標 `reserve_only`，無一標 `emit_fixed_assignment` |
| （潛在）reserve/emit 的 AF 不可靠 | pin_map 的 AF 全填 0（K3 mode 0 碰巧常是 pad 預設功能，僥倖沒炸） |
| web 不出現「產生 DTS」反問 | `dts_generation/` 整夾沒產；`board_ready()` 判 false |
| （必然會撞到）DTS 定位/編譯失敗 | `baseline/dts/` 只有板檔，缺 SoC dtsi 鏈與 include/ headers |
| patch diff 檔頭 vendor 目錄錯 | 沒產 `board.yaml`（vendor 推 kernel 樹路徑用） |

---

## 增項 A：require.json 的 boot 群組 action 判定（最重要）

**現況錯誤**：所有 boot 群組一律 `reserve_only` → solver 只保留腳位、
不把開機組排進 plan。

**兩個 action 的語意（DTS_agent 消費端，不可搞混）**：

| solver_action | 語意 | 效果 |
|---|---|---|
| `emit_fixed_assignment` | 開機必備**且 kernel 也要用** | pin_map 注入需求＋鎖定官方腳位，**每份 plan 自動帶上** |
| `reserve_only` | bootloader/secure 持有，kernel 不碰 | 腳位移出候選域＋instance 封鎖，**不進 plan** |

**判定規則（寫進 extractor）**：

1. 從 kernel DTS 收集 boot 相關節點：
   - TI K3：節點帶 `bootph-all` / `bootph-pre-ram` 屬性
   - 通用 fallback：手冊 boot 章節列出的週邊（boot media、boot mode 腳、console）
2. 分類：
   - 該節點在 kernel 板 DTS `status = "okay"`（kernel 會驅動）→ `emit_fixed_assignment`
     （典型：eMMC/SD 的 MMC0/MMC1、console UART）
   - 只有 bootloader 用、kernel DTS 未啟用或明確 disabled → `reserve_only`
     （典型：boot flash OSPI、BOOTMODE 腳、strapping pins）
3. **輸出時每個群組附 `_review` 註記**（判定依據＋信心），整檔標「待審查」——
   emit/reserve 分錯的後果是「開機組缺席」或「kernel 搶 bootloader 的腳」，
   必須人工確認一次。

**pin_map 格式（三元組，逐欄）**：

```json
"MMC0": {
  "solver_action": "emit_fixed_assignment",
  "pin_map": [["MMC0_CLK", "<pad名>", <真實mux mode>], ...]
}
```

- 欄 1 signal：必須與 af_table 的 signal 命名**逐字一致**
- 欄 2 pin：必須是 af_table 的 pin 鍵（K3 的 pad 名恰等於預設 signal 名是
  巧合，不可依賴——一律從 af_table 反查）
- 欄 3 AF：**手冊 pinmux 表的真實 mux mode**，不准填 0 佔位
  （extractor 產 af_table 時已有這份資料，回填即可）

---

## 增項 B：baseline/dts/ 補齊 include 鏈（全自動）

**現況錯誤**：只複製了板級 .dts 一個檔。板檔引用的 `&main_i2c0` 等 label
定義在 SoC dtsi——DTS_agent 的 m5 定位與 m7 編譯都需要完整檔組。

**生成邏輯**：

1. 從板檔起，遞迴解析 `#include`（`.dtsi` 與 `.h` 都要），
   把整條鏈從 kernel 樹複製進 `baseline/dts/`（headers 放 `include/` 子夾，
   保持 kernel 樹的相對路徑結構——DTS_agent 用 `cpp -I include/` 展開）
2. 產 `MANIFEST.md`：kernel 版本 tag、來源路徑、抓取時間、檔案清單
3. 板級 .dts 檔名維持 kernel 原名即可（DTS_agent 的定位規則：
   `<board>.dts` 優先，否則取目錄下**唯一**的 .dts——所以**一夾只放一個 .dts**）

**驗收**：`cpp -nostdinc -undef -D__DTS__ -x assembler-with-cpp -I include/ <板檔>`
能展開零 error（有 dtc 環境時再 `dtc` 編譯一次更好）。

---

## 增項 C：dts_generation/ 六檔（5 自動＋1 半自動）

缺任一檔第二段不可用。各檔最小 schema（頂層鍵照 stm32mp257f-ev1 樣板）：

| 檔案 | 生成來源 | 最小骨架（產不出內容時的合法空值） | 自動化 |
|---|---|---|---|
| `peripheral_node_alias.json` | DTS：label ↔ peripheral instance 對照（`i2c0: i2c@...` → I2C0） | `{"aliases": {}}` | 全自動 |
| `gpio_pins.json` | DTS：gpio-hog／官方保護腳 | `{"protected_pins": [], "reserved_by_disabled_only": []}` | 全自動 |
| `board_config.json` | DTS：板級常數 | `{"peripherals": {}}` | 全自動 |
| `dts_property_bindings.json` | DTS：逐 family 歸納 pinctrl/節點屬性模式 | `{"families": {}}` | 全自動 |
| `fixed_connections.json` | 板 DTS：phandle 跨節點連線（PHY、regulator…） | `{"connections": []}` | 半自動 |
| `boot_requirements.json` | DTS boot 節點＋**手冊交叉查證** | 不准空殼——至少涵蓋增項 A 判為 boot 的節點 | 半自動，**標待審查** |

規則：**寧可產「schema 正確的空骨架」也不要缺檔**（DTS_agent 端對空骨架
走 LLM 補償路徑；缺檔則功能直接關閉）。每檔帶 `board`/`source`/`description`
三個溯源欄位（照 stm32 樣板）。

注意：`boot_requirements.json` 與增項 A 的 `require.json` 是**不同檔、
不同 schema、不同消費者**（solver 腳位級 vs patch 的 DTS node 級），
永不合併——但兩者的「哪些週邊是 boot」判定來源相同，extractor 內部
可共用增項 A 的判定結果。

---

## 增項 D：board.yaml 產出（全自動）

```yaml
board_id: am6548
vendor: TI                 # 推 kernel 樹 vendor 目錄（ST->st、Nuvoton->nuvoton、TI->ti）
name: AM6548
knowledge_base: .
kernel_dts_path: arch/arm64/boot/dts/ti/k3-am654-base-board.dts   # 板檔在 kernel 樹的實際路徑
validation:
  enabled: false           # 非 ST 板一律 false（CubeMX 僅適用 ST）
  type: none
  script: null
```

- `kernel_dts_path` 填**板檔在 kernel 樹的真實相對路徑**（extractor 抓 DTS
  時就知道）——這是 patch diff `a/ b/` 檔頭與 Yocto 打補丁的目標路徑；
  明寫比讓 DTS_agent 用 vendor 推導可靠（推導只是 fallback）。

---

## 增項 E：產出自我驗收（extractor 內建 lint，出廠前擋錯）

每次產完知識庫，跑以下檢查，任一 FAIL 不出貨：

1. **交叉一致性**
   - require.json 全部 pin_map：欄 1 signal ∈ af_table 的 Σ、欄 2 pin ∈ af_table 鍵、欄 3 AF ∈ 該 pin 在 af_table 的合法 AF
   - signal_to_pin.json：全部 signal ∈ Σ、全部 pin ∈ af_table
   - baseline.csv：無 pin 重複（一腳一 signal）、每列 (pin, af, signal) 與 af_table 一致
2. **schema**
   - `solver_action` ∈ {emit_fixed_assignment, reserve_only}
   - dts_generation 六檔存在且頂層鍵齊全（照增項 C 表）
   - board.yaml `validation.type` ∈ {cubemx, script, none}
3. **baseline 完整性**
   - baseline/dts 恰一個 .dts；cpp 展開零 error（增項 B 驗收）
   - board 檔引用的每個 `&label` 都能在檔組內找到定義
4. **待審查清單輸出**：emit/reserve 判定、boot_requirements 內容
   ——列成 `REVIEW.md` 附在產出夾，人工簽核後才算完成

（DTS_agent 端也會有一個對應的 `kb lint` 進場檢查——規則以本節為準，
兩邊對齊，見 DTS_agent 的 KB_ROBUSTNESS_PLAN.md。）

---

## 交付順序與驗收

| 順序 | 項目 | 驗收方式 |
|---|---|---|
| 1 | 增項 A（require.json action＋AF 真值） | 重新產 am6548 → DTS_agent「給我一個 I2C」的 plan 帶出 MMC/UART 開機組 |
| 2 | 增項 B（baseline 補齊）＋ D（board.yaml） | cpp 展開零 error；`patch_agent locate --board am6548` 定位跑通 |
| 3 | 增項 C（dts_generation 六檔） | web 選 am6548 → solve → 出現「產生 DTS」反問 → patch 生成收斂 |
| 4 | 增項 E（自我 lint） | 對 am6548/ma35d1 兩板全綠；故意弄壞一欄能攔下 |

**最終驗收（端到端）**：am6548 從 extractor 產出 → 丟進 data/ → 重啟 →
solve（含開機組）→ 產生 DTS → patch 通過 m7 驗證，全程不改 DTS_agent 程式碼。
