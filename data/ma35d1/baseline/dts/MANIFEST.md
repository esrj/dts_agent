# ma35d1 — baseline kernel DTS 檔組(knowledge_extract 自動產出)

完整可編譯的 kernel device-tree 源碼組:板級 .dts + include 鏈(.dtsi/.h)+ `include/` 下的 dt-bindings headers。
展開方式:`cpp -nostdinc -undef -D__DTS__ -x assembler-with-cpp -I include -I . ma35d1-evb.dts`

## 來源

- repo:https://github.com/OpenNuvoton/MA35D1_linux-5.10.y.git
- branch/tag:master
- commit:c69ae453dac8f15f3eaf568e13b5a263660328c8
- 產出時間:2026-07-26 14:42

## 匯入時的修復(Processing applied on import)

- 補上 pcfg_sdhci_drive1_3_3V(依 pcfg_sdhci_drive2_3_3V 樣式,寫入 ma35d1.dtsi)

## 檔案清單

- ma35d1.dtsi
- ma35d1-evb.dts
- include/dt-bindings/clock/nuvoton,ma35d1-clk.h
- include/dt-bindings/gpio/gpio.h
- include/dt-bindings/input/input.h
- include/dt-bindings/input/linux-event-codes.h
- include/dt-bindings/interrupt-controller/arm-gic.h
- include/dt-bindings/interrupt-controller/irq.h
- include/dt-bindings/pinctrl/ma35d1-pinfunc.h
- include/dt-bindings/reset/nuvoton,ma35d1-reset.h
