# am6548 知識庫待審查清單(REVIEW)

產出時間:2026-07-26 02:32(knowledge_extract 自動生成)

## boot 群組 emit/reserve 判定(增項 A)

分錯的後果:「開機組缺席」或「kernel 搶 bootloader 的腳」。請逐列確認後在方框打勾。

| ✓ | 群組 | action | DTS 節點 | 依據 | 信心 |
|---|---|---|---|---|---|
| ☐ | BOOTMODE_MAIN | reserve_only | — | DTS 無對應節點或未啟用——視為 bootloader/strap 持有,kernel 不碰 | low |
| ☐ | BOOTMODE_MCU | reserve_only | — | DTS 無對應節點或未啟用——視為 bootloader/strap 持有,kernel 不碰 | low |
| ☐ | MMC0 | emit_fixed_assignment | &sdhci0 | compatible "ti,am654-sdhci-5.1" ＝開機媒體（eMMC/SD）且有效啟用 | medium |
| ☐ | MMC1 | emit_fixed_assignment | &sdhci1 | compatible "ti,am654-sdhci-5.1" ＝開機媒體（eMMC/SD）且有效啟用 | medium |
| ☐ | UART0 | emit_fixed_assignment | &main_uart0 | /chosen stdout-path 指向 &main_uart0（開機 console） | medium |

## lint 結果

- FAIL:無
- WARN:無

## 抽取警告(dts 步驟)

- 無

---
簽核:上述項目已逐一確認 ☐  簽核人:________  日期:________
