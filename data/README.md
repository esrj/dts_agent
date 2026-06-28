# data/ — 板級知識庫（per-board knowledge base）

每一塊板子一個資料夾（`data/<board>/`），**自包含**：新增板子＝丟一個新資料夾進來，
web UI 由 `dataio.list_boards()` 自動偵測，agent / solver 架構零改動。
路徑對照的唯一權威是 [src/util/dataio.py](../src/util/dataio.py) 的
`_BOARD_FILES` / `_BOARD_FILES_OPTIONAL`——程式一律經 `board_paths(board)` 取路徑，
不得自行拼字串。

## 資料夾結構（依資料責任分類）

```
data/<board>/
├── base/        手工維護核心 —— solver 與 agent 判定的主要依據
│   ├── af_table.json               pin ↔ AF ↔ signal 全表（求解候選域唯一來源）
│   ├── require.json                開機鎖定（boot_pin_locked：pin_map + solver_action）、
│   │                               reserve_only 保留群組、gpio_must_pins
│   ├── peripheral_profiles.json    families 模板 + per-instance profile（周邊三層展開）
│   ├── all_peripheral.json         由 af_table 再生的 周邊→signals 全索引（參考用，
│   │                               程式目前未直接消費）
│   └── cubemx.json                 CubeMX validator 板級常數（MCU 名、板 ioc、
│                                   dt_modes 對照）——手工維護、非衍生非快取
├── dts/         官方 DTS 解析出的 board-proven 資料（來源：st kernel 板 DTS）
│   ├── signal_to_pin.json          官方預設腳位對照（bootable_default 直接輸出）
│   └── official_dts_peripheral.json 官方 DTS 啟用的周邊（周邊展開第 1 優先序）
├── bindings/    板上外部 IC 與 DT binding 知識（G4；手工整理 + 雙重驗證）
│   └── board_components.json       instance ↔ IC ↔ compatible ↔ binding_doc ↔ source
│                                   （驗證來源：原理圖 + 官方 kernel DTS）
├── cache/       自動產生 —— 整個資料夾可刪除，程式會自動重建
│   └── binding_cache.json          lookup_binding 查過即存（doc 摘要 + source_url +
│                                   fetched_at）；離線時命中仍可用
├── baseline/    官方 kernel DTS 快照（第二段 DTS patch 專用；機械抓取，
│                錯了重抓不手改——tools/grab_kernel_dts.sh）
│   ├── baseline.csv                官方預設 pin 配置表
│   └── dts/                        官方 .dts/.dtsi ＋ include/ headers ＋
│                                   MANIFEST.md（出處與版本說明）
└── dts_generation/  DTS 生成/驗證知識（第二段專用；路徑權威是
    │                src/patch_agent/config.py，不歸 dataio 管）
    ├── board_config.json           板級 DTS 常數（tools/extract_board_data.py）
    ├── dts_property_bindings.json  family→DTS 屬性模板（同上工具）
    ├── gpio_pins.json              官方 DTS 保護的 GPIO 腳（同上工具）
    ├── fixed_connections.json      板上實體連線（tools/extract_fixed_connections.py）
    ├── peripheral_node_alias.json  peripheral↔DTS node 對照（tools/derive_alias.py）
    └── boot_requirements.json      開機必備 DTS node 知識（手工整理，datasheet
                                    交叉查證）。注意：與 base/require.json 是
                                    **不同的檔案**——那份是 solver 的腳位級開機
                                    常數，這份是 DTS node 級知識，永不合併
                                    （MERGE_PLAN §0.1 決策 1）
```

`base/` + `dts/` 五檔是**必要檔**（缺任一檔該板不會出現在板子清單）；
`bindings/`、`cache/` 是**選配檔**（缺檔時 loader 回空值，功能優雅降級：
ic 欄留空、lookup_binding 回 no_ic / 現查）；`baseline/`、`dts_generation/`
是**第二段（DTS patch）的選配檔**——缺夾時 plan 流程照常，只有「產生 DTS」
功能不可用（web 依 `/api/dts/status` 的 available 決定是否顯示按鈕）。

## 為什麼這樣分類

1. **資料責任分離**：四類資料的「誰產生、誰修改、錯了找誰」完全不同——
   `base/` 是人手維護的板級事實（錯了改這裡）；`dts/` 是官方 DTS 的機械解析
   （錯了重新解析，不手改）；`bindings/` 是查證過的外部 IC 知識（每筆帶 source
   可溯源）；`cache/` 是程式的自動產物（錯了直接刪掉重建）。混在同一層時，
   「這個檔能不能手改」要靠記憶；分開後資料夾名稱就是答案。
2. **核心與快取不混放**：`cache/` 永遠可以整夾刪除而不損失任何知識；
   反過來說，備份／版控時 `base/` + `dts/` + `bindings/` 就是這塊板的完整知識。
3. **支援多板切換**：換板要做的事一目了然——`base/` 與 `dts/` 必備（重新產生），
   `bindings/` 視板上 IC 而定（可留空），`cache/` 不用帶。`dataio.board_paths()`
   統一解析路徑，agent / solver / web 全部不感知目錄結構。
4. **方便後續 agent 讀取與擴充**：agent 要「查板子限制」讀 `base/require.json`、
   「查官方預設」讀 `dts/`、「查外部 IC」讀 `bindings/`——按用途直達，
   不需要先讀說明文件猜哪個檔是什麼。

## 新增一塊板子的最小步驟

1. `mkdir data/<new-board>/{base,dts}`，備齊五個必要檔（格式照本板現有檔案）。
2. （選配）有外部 IC 知識就加 `bindings/board_components.json`；
   要用 CubeMX validator 就加 `base/cubemx.json`。
3. 重啟服務——板子自動出現在下拉選單（`list_boards()` 以「五個必要檔齊全」為準）。
