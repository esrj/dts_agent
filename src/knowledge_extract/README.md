# knowledge_extract — 手冊 + DTS → 板級知識庫（DTS_agent 第 0 段）

把一塊板子的 **官方手冊 PDF** 與 **kernel 官方 DTS** 轉成 `data/<board>/`
自包含知識庫（供第一段求解與第二段 patch 生成消費）。原獨立專案，
2026-07-25 併入 DTS_agent（合併記錄見根目錄
[EXTRACTOR_MERGE_PLAN.md](../../EXTRACTOR_MERGE_PLAN.md)）。

兩種觸發方式：

- **web（推薦）**：左上角板子選單「＋ 上傳新板子…」——上傳 PDF＋DTS
  資料夾＋板名，背景生成 → REVIEW 確認 → 自動落地（`src/web/board_create.py`）。
- **CLI**：材料放 `input/`，`PYTHONPATH=src venv/bin/python -m
  knowledge_extract`（步驟選擇 `--steps af,profiles,require,dts`）。

**輸出紅線**：本模組**只寫 `output/staging/<board>/`**，永不直接寫
`data/<board>/`——落地（staging → data/）由 web 的 REVIEW 確認、或人工
搬移完成（`paths.py` D3）。

## 相關根目錄檔案

```
input/           CLI 待處理:一塊板的 手冊 PDF + 整份 DTS 資料夾(一次一板)
output/staging/  生成輸出(落地前暫存;lint 全綠+確認後才進 data/)
archive/         收集品:手冊(manual/)與 DTS 源碼(dts/<board>/,含 headers)
cache/knowledge_extract/  PDF 轉文字快取(可整夾刪除,自動重建)
boards.ini       pdf→board / board→SoC / board→baseline dts 對應(自動寫入,可手改)
llm_modules.ini  LLM 選型([knowledge_extract] 區段;與兩段共用 src/llm_provider)
```

## 重要檔案導覽（src/knowledge_extract/）

**進入點與流程**

- [`__main__.py`](src/knowledge_extract/__main__.py) — CLI。掃 `input/`、
  步驟選擇（`--steps af,profiles,require,dts`）、單一執行鎖（防雙跑燒 API）、
  LLM provider 惰性建立（純 dts 步驟不需要 API key）、lint FAIL → exit 1。
- [`pipeline.py`](src/knowledge_extract/pipeline.py) — 一塊板的主流程編排：
  手冊三步驟（LLM）→ dts 步驟（純程式），已存在的輸出一律跳過。

**手冊路徑（LLM）**

- [`pdf_text.py`](src/knowledge_extract/pdf_text.py) — PDF → 逐頁文字
  （`pdftotext -layout` 保表格對齊，sha256 快取於 `cache/`）。
- [`identify.py`](src/knowledge_extract/identify.py) — 板名/SoC 辨識與
  `boards.ini` 讀寫（有登錄零 LLM 呼叫）。
- [`locate.py`](src/knowledge_extract/locate.py) — 啟發式定位 pinmux 表與
  boot 章節頁面（ST/TI/Nuvoton 三家措辭的加權關鍵字）。
- [`extract_af.py`](src/knowledge_extract/extract_af.py) — LLM 分塊抽取
  pin→mux→signal（af_table）。明確編號優先、序列式只補缺；mux 限 0–15；
  單功能專用腳一律保留（漏掉會破壞下游 Σ 交叉驗證）。
- [`derive.py`](src/knowledge_extract/derive.py) — af_table → all_peripheral
  純程式衍生（周邊→signals 索引）。
- [`profiles.py`](src/knowledge_extract/profiles.py) — LLM 草擬
  peripheral_profiles / require（標 `needs_confirmation`，人工核對後定稿）。
- [`jsonutil.py`](src/knowledge_extract/jsonutil.py) — LLM JSON 回覆解析
  ＋暫時性錯誤退避重試。

**DTS 路徑（純程式，零 LLM）**

- [`dts_extract.py`](src/knowledge_extract/dts_extract.py) — dts 步驟核心：
  輕量遞迴 DTS parser（屬性保留原文，行內註解是解碼素材）→ 有效 status
  判定（含 SoC dtsi 預設與 Nuvoton 非標準 `"disable"` 拼寫）→ 三家廠商
  pinmux 解碼器（自動偵測）→ **af_table 為命名權威**的同源生成（查不到
  即剔除進 REVIEW，不寫進輸出）→ 產 `dts/` 兩檔、`baseline.csv`、
  完整 baseline 檔組（include 鏈＋headers＋MANIFEST＋上游缺定義修復）。
- [`af_repair.py`](src/knowledge_extract/af_repair.py) — af_table 品質工具：
  Nuvoton 板用廠商 pinfunc header（每腳×每功能的 MFP 真值）整表權威重建
  （LLM 解析序列式 pin 表掉 token 會整列位移，header 是正解）；
  族群完整性自查（instance 編號空洞 → REVIEW）。
- [`require_enrich.py`](src/knowledge_extract/require_enrich.py) — boot 群組
  判定：kernel 有效啟用 → `emit_fixed_assignment`（plan 必帶開機組）、
  否則 `reserve_only`；pin_map 回填真實 AF。只動 `needs_confirmation`
  草稿，人工定案的檔案不碰；每群組附 `_review`。
- [`dts_generation.py`](src/knowledge_extract/dts_generation.py) —
  `dts_generation/` 六檔（alias、gpio_pins、board_config、property_bindings、
  fixed_connections、boot_requirements；schema 對齊 DTS_agent 樣板）＋
  `board.yaml`（vendor、kernel_dts_path）。
- [`pad_supply.py`](src/knowledge_extract/pad_supply.py) — pinmux 渲染供料
  （僅非 ST 板）：`pinmux_style.json`（k3-iopad / nuvoton-mfp 樣式與詞彙）＋
  `pad_params.json`（pad→offset 全表；K3 用 datasheet PADCONFIG 位址表 ×
  pmx 節點 reg 視窗，**domain＝pmx 節點 label**——AM65 的 MAIN 有
  main_pmx0/main_pmx1 兩節點各自起算；MA35 用 pinfunc 的 (reg, shift)）。
- [`kb_lint.py`](src/knowledge_extract/kb_lint.py) — 出廠 gate：交叉一致性
  （signal/pin/AF 對 af_table，Σ 完整性、af 必整數）、schema、baseline
  完整性（cpp 展開零 error、label 閉包在 cpp 展開後稽核）、渲染供料規則
  （覆蓋率缺一 FAIL、詞彙須見於官方 DTS、3 pad 抽查）。FAIL＝不出貨
  （exit 1）；WARN＋剔除清單寫入 `REVIEW.md` 待人工簽核。
- [`paths.py`](src/knowledge_extract/paths.py) — 路徑唯一權威。

## 用法

```bash
# 1. 把板子的手冊 PDF 和「整份 DTS 資料夾」放進 input/
cp archive/manual/am6548.pdf input/
cp -r archive/dts/am6548 input/

# 2. 跑 pipeline(需要 .env 有 API key;模型設定在 llm_modules.ini)
python src/knowledge_extract

# 只跑部分步驟 / 重生
python src/knowledge_extract --steps dts --force          # 只重解 DTS(不動 LLM)
python src/knowledge_extract --steps dts --board am6548   # 無手冊、只有 DTS 時
python src/knowledge_extract --steps af --force --manual am6548.pdf  # 重抽 af_table
```

已存在的輸出檔一律跳過（`--force` 才重生），可重複執行。
`--out <dir>` 可把輸出導到別處 staging。

## 流程與產出

| 步驟 | 方式 | 產出(data/<board>/) |
|---|---|---|
| `af` | LLM | `base/af_table.json`、`base/all_peripheral.json` |
| `profiles` | LLM | `base/peripheral_profiles.json`（草稿待核） |
| `require` | LLM | `base/require.json`（草稿；dts 步驟會回填 boot 判定） |
| `dts` | 純程式 | `dts/` 兩檔、`baseline/`（csv＋完整 DTS 檔組）、`dts_generation/` 六檔＋渲染供料兩檔（非 ST）、`board.yaml`、`REVIEW.md` |

## 新板子最小步驟

1. 手冊 PDF ＋ kernel DTS 整份資料夾（含 `include/` headers）→ `input/`
2. `python src/knowledge_extract` → 全部產出＋lint
3. 打開 `data/<board>/REVIEW.md`：核對 boot 群組 emit/reserve 判定、
   剔除清單、族群空洞，簽核；核對 `profiles`/`require` 草稿
4. lint 全綠後把 `data/<board>/` 複製給 DTS_agent；
   手冊與 DTS 收進 `archive/` 留存，清空 `input/`
