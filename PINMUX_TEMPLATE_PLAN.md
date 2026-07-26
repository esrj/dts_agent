# Pinmux 格式知識資料化計劃（template style：LLM 產規格＋通用引擎）

> 目標：消滅「每個新 vendor 要手寫一個 pinmux Style class」——把 pinctrl
> 格式知識變成**宣告式規格**存進 `data/<board>/dts_generation/
> pinmux_style.json`，程式端只留**一個通用模板引擎**。上傳新板時由 LLM
> 讀官方 baseline 實例產出規格、以 **round-trip gate**（baseline 幾百條
> 真實條目 100% 對賬）驗證後落地。
>
> 之後：**runtime 零 per-vendor 分支**——每板讀自己的知識庫；新 vendor
> ＝新資料檔，不改程式（production 大量上傳的前提）。
>
> 紅線同構：LLM 只寫**資料**（格式規格），執行與驗證全部確定性——與
> 紅線 1「LLM 永不直接指派腳位」、紅線 2「領域知識零寫死」一致。

---

## 0. 現況：格式邏輯散在三處程式（本計劃要搬進資料的東西）

| 位置 | 內容 | 本計劃處置 |
|---|---|---|
| `src/patch_agent/pinmux_style.py` | Stm32Style（L56）／K3IopadStyle（L120）／NuvotonMfpStyle（L238）／get_style 工廠（L335） | 新增 **TemplateStyle 通用引擎**；三個手寫 class **保留**（既有板回歸零風險），未知 vendor 走 template |
| `src/knowledge_extract/dts_extract.py` | decode_group 三家 vendor 分支（L224–253）＋ regex（L188–196） | 加 **template 解碼路徑**（吃同一份規格）；三家分支保留 |
| `src/knowledge_extract/pad_supply.py` | ti_style／nuvoton_style／build_supply 分支（L136/199/233） | 未知 vendor 改走 **style_infer（LLM 產規格）** |
| `data/<board>/dts_generation/pinmux_style.json` | 現只存「style 名＋參數」 | **擴為完整宣告式規格**（見 §2）——格式知識的唯一存放處 |

關鍵事實：**一份規格、兩個消費者**——extractor 的解碼與 DTS_agent 的
渲染/解析讀同一份 spec，天然一致（現在三家 vendor 在兩邊各寫一次，
新 vendor 會兩邊同時卡）。

## 1. 為什麼這樣做是安全的（設計核心）

- **LLM 不產程式碼、不直接產 DTS**——產的是受限的宣告式規格（JSON）；
  執行是確定性引擎；m7 的 plan-consistency 防偽鏈（解析 pinmux 回來對賬）
  完整保留。
- **完美的驗證 oracle**：官方 baseline DTS 本身有數百條真實 pinmux 條目
  ＋pinfunc headers＋af_table。規格必須通過 **round-trip gate**：
  1. 用規格解析 baseline 全部條目 → (pin, af) 與 af_table 交叉一致；
  2. 把解析結果重渲染 → cpp 展開後與原條目**逐 cell 等值**；
  3. dtc 編譯通過。
  百分之百全中才出貨；不中→上傳 job 明確失敗、附「此 binding 需人工
  style」——手寫 class 從「每家必寫」降級為罕見逃生門。

## 2. 規格 schema（存 `data/<board>/dts_generation/pinmux_style.json`）

```json
{
  "style": "template",
  "source": "<baseline dts>＋<header>（LLM 推導；round-trip 驗證通過）",
  "group_prop": "nuvoton,pins",          // 群組承載屬性（"pinmux"＝ST subnode 形）
  "containers": ["pinctrl"],             // pinctrl 容器節點 label（可多個，如 K3 各 pmx）
  "entry": {
    "parse": "SYS_GP(?P<bank>\\w)_MFP[LH]_P\\w?(?P<num>\\d+)MFP_(?P<signal>\\w+)",
    "pin": "P{bank}{num}",               // named groups → pin 名模板
    "af_source": "af_table",             // af_table 反查 | 具名群組 "mux" | pad_params 欄位
    "render": "<SYS_GP{bank}_MFP{half}_{pin}MFP_{signal}\t&pcfg_default>",
    "computed": {                        // 受限的計算欄位（固定函式集，非圖靈完備）
      "half": {"switch_on": "num", "le": 7, "then": "L", "else": "H"},
      "bank": {"from": "pin", "slice": [1, 2]}
    },
    "lookups": {                         // 查表欄位（pad_params / macros 對照）
      "offset": {"table": "pad_params", "field": "offset"},
      "macro":  {"table": "macros", "key": "domain"}
    }
  },
  "group_shape": "flat | subnode",       // 條目直列 vs ST 的 pins 子節點形
  "role_flags": { "default": "&pcfg_default" }
}
```

**表達力邊界（刻意受限）**：named-group regex、字串模板、數值分段
switch、slice、pad_params/macros 查表、af_table 反查——剛好覆蓋
「每腳一個巨集/元組」類 binding（ST/TI/Nuvoton/Rockchip/i.MX 的主流形態）。
不做條件巢狀、不做迴圈、不做任意運算——表達不了的格式就 round-trip
失敗、走逃生門，**不讓 DSL 膨脹**。

## 3. 工作包

### T1.【DTS_agent】`TemplateStyle` 通用引擎（pinmux_style.py）
- 實作 §2 規格的解析（`iter_pinmux`）、渲染（`render_group`/`render_pinmux`）、
  `containers()`／`GROUP_PROPS`／`RAW_NUMBER_RE`（由 group_prop 推導）。
- 工廠：`style: "template"` → TemplateStyle(spec, af_table, pad_params)。
- 內部 mode 詞彙沿用 `AF<n>`（m6 guard／m7 對賬零改動——與三家手寫
  style 同一決策）。

### T2.【DTS_agent】表達力驗證（用已知三家當測試集）
以 template spec **重新表達 stm32／k3-iopad／nuvoton-mfp**，各自對三板
baseline 跑 round-trip：解析條目數、(pin, af) 集合、重渲染等值必須與
手寫 class **完全一致**。這一步證明 DSL 表達力足夠、也給 LLM 推導
三個 few-shot 範例。（驗證用，不切換既有板的執行路徑。）

### T3.【extractor】decode_group 的 template 路徑（dts_extract.py）
- `decode_group(vendor, group, spec=None)`：spec 給定時走通用解碼
  （同一 regex／pin 模板），未給時走既有三家分支。
- vendor 偵測不到已知家族時：進 T4 推導流程。

### T4.【extractor】`style_infer.py`——LLM 規格推導＋round-trip gate
1. 取材：baseline 的 pinctrl 群組節選（全部群組、截斷長度）＋include 中
   相關 header 節選＋af_table 摘要＋§2 schema 說明＋T2 的三家 few-shot。
2. LLM 產 spec（`llm_modules.ini [knowledge_extract]`；jsonutil 既有
   重試機制）。
3. **round-trip gate**（確定性）：解析覆蓋率 100%＋af_table 交叉一致＋
   重渲染 cpp 等值＋dtc 編譯。失敗→重試一次（把差異回饋進 prompt）→
   仍失敗→job 明確失敗（訊息：「此 pinctrl binding 超出模板表達力，
   需人工 style——參見 pinmux_style.py」）。
4. 通過→spec 寫入 staging 的 pinmux_style.json，隨知識庫落地。

### T5.【extractor】pad_supply 與 lint 接線
- build_supply：未知 vendor → T4；pad_params 需求由 spec 的 lookups
  宣告（用不到就不強制）。
- 出廠 lint／DTS_agent kb_lint：`style: "template"` 板必附 round-trip
  報告（通過筆數/覆蓋率），lint 重放抽查 N 條。

### T6. 文件與收尾
- data/README（pinmux_style.json 規格章節）、extractor README、
  PROJECT_OVERVIEW 一行；本檔留執行記錄。

## 4. 相容性不變式

1. **既有三板（stm32/am6548/ma35d1）零改動**：手寫 class 保留、預設
   路徑不變；template 只在「spec 明示 `style: "template"`」時啟用。
2. m6 schema／m7 防偽對賬不動（mode 詞彙統一 `AF<n>`）。
3. spec 是資料不是路徑（紅線 2）；LLM 產物必經確定性 gate 才落地
   （紅線 1／3 同構）。
4. 逃生門保留：round-trip 不過的 binding 仍可手寫 class——工廠優先序
   「已知 style 名 → template → 明確報錯」。

## 5. 風險

| 風險 | 對策 |
|---|---|
| DSL 表達力不足以蓋下一家 | T2 先用三家自證；不足時擴 computed 函式集（小步），絕不塞任意運算 |
| LLM 產的 regex 過擬合節選樣本 | round-trip 對 **baseline 全部條目**驗，不是樣本 |
| 兩邊引擎行為漂移 | 解析引擎**單一實作**（extractor import DTS_agent 的 TemplateStyle——同 repo，kb_lint 已有先例） |
| spec 被手改壞 | kb_lint 重放 round-trip 抽查；壞了 FAIL 不出貨 |

## 6. 順序與估時

```
T1 引擎 ──→ T2 三家自證 ──→ T3 解碼接線 ──→ T4 LLM 推導＋gate ──→ T5 lint ──→ T6 文件
```

| 包 | 估時 |
|---|---|
| T1 | 1.5 天 |
| T2 | 1 天（同時產出 few-shot） |
| T3 | 0.5 天 |
| T4 | 1.5 天（含 prompt 調試） |
| T5＋T6 | 1 天 |
| **合計** | **5–6 天** |

---

**狀態**：計劃定稿，待批准動工。價值兌現點＝下一家未知 vendor 上傳時
（現有三家已由手寫 class 覆蓋，不受影響）。
