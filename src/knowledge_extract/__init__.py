"""knowledge_extract — input/ 的 手冊 PDF + kernel DTS → data/<board>/ 知識庫。

流程(每次執行都重掃 input/):
  1. pdf_text    : PDF -> 逐頁純文字(pdftotext -layout,快取於 cache/knowledge_extract/)
  2. identify    : 手冊 -> 板名 slug(<root>/boards.ini 為準;缺項時用 LLM 辨識並回寫)
  3. locate      : 啟發式定位 pinmux 表 / boot 章節頁面
  4. extract_af  : LLM 分塊抽取 pin -> mux 編號 -> signal,合併為 base/af_table.json
  5. derive      : 由 af_table.json 衍生 all_peripheral.json(純程式,不經 LLM)
  6. profiles    : LLM 依 stm32mp257f-ev1 範本草擬 peripheral_profiles.json / require.json
                   (標記 needs_confirmation,人工確認後才算定稿)
  7. dts_extract : kernel 官方 DTS -> dts/signal_to_pin.json、
                   dts/official_dts_peripheral.json、baseline/(純程式,零 LLM)

LLM 一律經 llm_provider.get_provider(module="knowledge_extract") 取得,
供應商/模型由 llm_modules.ini 的 [knowledge_extract](缺項回落 [default])決定。

用法:python src/knowledge_extract --help(整體說明見根 README.md)
"""
