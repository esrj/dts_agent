# am6548 — baseline kernel DTS 檔組(knowledge_extract 自動產出)

完整可編譯的 kernel device-tree 源碼組:板級 .dts + include 鏈(.dtsi/.h)+ `include/` 下的 dt-bindings headers。
展開方式:`cpp -nostdinc -undef -D__DTS__ -x assembler-with-cpp -I include -I . k3-am654-base-board.dts`

## 來源

- repo:https://github.com/torvalds/linux.git
- branch/tag:v6.6
- commit:ffc253263a1375a65fa6c9f62a893e9767fbebfa
- 產出時間:2026-07-29 10:42

## 匯入時的修復(Processing applied on import)

- 無

## 檔案清單

- k3-pinctrl.h
- k3-am65-main.dtsi
- k3-am65-mcu.dtsi
- k3-am65-wakeup.dtsi
- k3-am65.dtsi
- k3-am654-industrial-thermal.dtsi
- k3-am654.dtsi
- k3-am654-base-board.dts
- include/dt-bindings/gpio/gpio.h
- include/dt-bindings/input/input.h
- include/dt-bindings/input/linux-event-codes.h
- include/dt-bindings/interrupt-controller/arm-gic.h
- include/dt-bindings/interrupt-controller/irq.h
- include/dt-bindings/net/ti-dp83867.h
- include/dt-bindings/phy/phy-am654-serdes.h
- include/dt-bindings/soc/ti,sci_pm_domain.h
- include/dt-bindings/thermal/thermal.h
