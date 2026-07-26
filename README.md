# DTS_agent — 手冊＋DTS → 知識庫 → pin-mux 求解 → kernel DTS patch

多板嵌入式開發輔助系統：上傳一塊板子的 **官方手冊＋kernel DTS** 自動建立
知識庫；用**自然語言**描述週邊需求得到可行的 **pin assignment（plan）**；
確認後產出**經驗證的 kernel DTS patch**。三段式、每段可獨立確認。

```
【第 0 段】上傳新板（web「＋ 上傳新板子…」／CLI knowledge_extract）
   │  手冊 PDF + kernel DTS 整包 → 知識庫生成 → 雙重 lint → REVIEW 確認 → 落地 data/<board>/
   ▼
【第一段】pin-mux 求解（solver + orchestrator + service）
   │  自然語言（「三個 I2C 一個 SPI」）→ CSP 求解（自動帶開機必備腳）
   │  plan 表格顯示（ST 板附 CubeMX 官方驗證徽章）
   ▼
使用者確認 ──「要產生 DTS patch 嗎？」──►【第二段】DTS patch（patch_agent m1–m8）
   │ 否：停在 plan.csv                      │ 定位→生成→結構/dtc 驗證→修復(≤3輪)
   ▼                                       ▼
output/plan/plan.csv                  output/generated/generated.patch
                                      output/generated/<board>.generated.dts
```

## 快速上手（web，推薦）

```bash
venv/bin/pip install -r requirements.txt      # 首次；另需 pdftotext（brew install poppler）
venv/bin/python src/web/app.py                # http://127.0.0.1:5001
```

1. **新增板子**：左上角板子選單 →「＋ 上傳新板子…」→ 輸入板名、選手冊
   PDF 與 DTS 整包資料夾（多個 .dts 時再選 baseline 板檔）→ 上傳。
   「進行 plan 驗證」勾選：ST 板勾（走 STM32CubeMX）；其他板不勾。
   生成完成後確認 boot 判定（emit/reserve 可改）→ 落地，板子自動出現。
2. **求解**：選板後用自然語言描述需求（「三個 I2C 一個 SPI」「官方預設
   再多一組 SPI」）——plan 自動含開機必備腳位。
3. **產 patch**：plan 表格下方點「產生 DTS patch」→ 下載
   `generated.patch`（可直接餵 Yocto `SRC_URI += "file://generated.patch"`）。

## CLI 對照表

```bash
# 第 0 段：知識庫生成（材料放 input/；輸出進 output/staging/，落地另行）
PYTHONPATH=src venv/bin/python -m knowledge_extract [--board <id>] [--steps af,profiles,require,dts]

# 知識庫進場檢查（新板落地前後都可跑；--data-root output/staging 檢查落地前產出）
venv/bin/python tools/kb_lint.py <board>

# 第二段：DTS patch（讀 output/plan/plan.csv）
PYTHONPATH=src venv/bin/python -m patch_agent run [--board <id>] [--no-compile]
PYTHONPATH=src venv/bin/python -m patch_agent locate   # 只定位（不用 LLM）——煙霧測試
```

## 多板機制

- 一板一資料夾 `data/<board>/`（規範見 [data/README.md](data/README.md)），
  自動偵測；`board.yaml` 記板子身份與驗證策略。
- **驗證引擎**（`validation.type`）：`cubemx`（ST 官方驗證）／`none`
  （回 skipped，非錯誤）／`script`（預留：自訂驗證腳本）。
- **pinmux 渲染 style**（第二段 patch 產出格式）支援矩陣：

| style | 板例 | 狀態 |
|---|---|---|
| `stm32`（STM32_PINMUX 巨集） | stm32mp257f-ev1 | ✅ |
| `k3-iopad`（AM65X_IOPAD） | am6548 | ✅ |
| `nuvoton-mfp` | ma35d1 | 規劃中（該板可求解，patch 待實作） |

## 相關文件

| 文件 | 內容 |
|---|---|
| [PROJECT_OVERVIEW.md](PROJECT_OVERVIEW.md) | 完整架構、資料夾結構、API/工具清單、CubeMX 整合要點 |
| [data/README.md](data/README.md) | 知識庫格式規範（board.yaml、六檔缺檔語意、新增板子步驟） |
| [src/knowledge_extract/README.md](src/knowledge_extract/README.md) | 第 0 段：知識庫生成 pipeline 與檔案導覽 |
| [src/patch_agent/README.md](src/patch_agent/README.md) | 第二段：DTS patch pipeline（m1–m8） |
| [EXTRACTOR_MERGE_PLAN.md](EXTRACTOR_MERGE_PLAN.md) | Knowledge Extractor 合併計劃與執行記錄 |

## 環境需求

- Python 3.10（venv；`requirements.txt`）；`.env` 放 LLM API keys、
  `llm_modules.ini` 選型（`[knowledge_extract]/[parse]/[orchestrator]/[dts_patch]`）。
- `pdftotext`（poppler）——第 0 段手冊解析。
- `dtc`＋`gcc`——第二段編譯驗證（缺席時 web 自動跳過、CLI 加 `--no-compile`）。
- STM32CubeMX（選配）——ST 板官方驗證與 CubeMX DT 生成；未安裝不影響其他功能。
