# ma35d1 知識庫待審查清單(REVIEW)

產出時間:2026-07-26 14:42(knowledge_extract 自動生成)

## boot 群組 emit/reserve 判定(增項 A)

分錯的後果:「開機組缺席」或「kernel 搶 bootloader 的腳」。請逐列確認後在方框打勾。

| ✓ | 群組 | action | DTS 節點 | 依據 | 信心 |
|---|---|---|---|---|---|
| ☐ | NAND_FLASH_BOOT | emit_fixed_assignment | &nand | kernel 板 DTS &nand 有效啟用,kernel 會驅動 → plan 必帶 | high |
| ☐ | POWER_ON_SETTING_STRAPS | reserve_only | — | DTS 無對應節點或未啟用——視為 bootloader/strap 持有,kernel 不碰 | low |
| ☐ | QSPI0_SPI_FLASH_BOOT | emit_fixed_assignment | &qspi0 | kernel 板 DTS &qspi0 有效啟用,kernel 會驅動 → plan 必帶 | high |
| ☐ | SD0_EMMC0_BOOT | emit_fixed_assignment | &sdhci0 | kernel 板 DTS &sdhci0 有效啟用,kernel 會驅動 → plan 必帶 | high |
| ☐ | SD1_EMMC1_BOOT | emit_fixed_assignment | &sdhci1 | kernel 板 DTS &sdhci1 有效啟用,kernel 會驅動 → plan 必帶 | high |
| ☐ | USB_BOOT | reserve_only | — | DTS 無對應節點或未啟用——視為 bootloader/strap 持有,kernel 不碰 | low |

## af_table 權威重建(R1)

- 來源:ma35d1-pinfunc.h(廠商 pinfunc header,MFP 真值)
- 規模:222 pins;相對舊表 +1008 / -864 條目
- 舊表為 LLM 序列式解析,掉 token 會使整列 mux 位移——以 header 為準
- 新增樣本:[('PA0', '2', 'UART1_nCTS'), ('PA0', '3', 'UART16_RXD'), ('PA0', '6', 'NAND_DATA0'), ('PA0', '7', 'EBI_AD0')]
- 移除樣本:[('NRESET', '1', 'WDT_nRST'), ('PA0', '1', 'UART1_nCTS'), ('PA0', '2', 'UART16_RXD'), ('PA0', '3', 'NAND_DATA0')]

## lint 結果

- FAIL:無
- WARN:無

## 抽取警告(dts 步驟)

- 無

---
簽核:上述項目已逐一確認 ☐  簽核人:________  日期:________
