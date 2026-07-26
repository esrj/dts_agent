from __future__ import annotations

import re

# 與 stm32mp257f-ev1 範本一致的分類原則:signal 取第一個 '_' 前的 token 當
# peripheral;沒有 '_' 的(EVENTOUT 等)與 debug/trace/clock-out 類歸 system。
_SYSTEM_PREFIXES = {
    "AUDIOCLK", "DBTRGI", "DBTRGO", "EVENTOUT", "HDP", "MCO", "SDVSEL",
    "TRACECLK", "TRACED", "TRACECTL", "JTAG", "SWCLK", "SWDIO", "SWO",
    "TCK", "TMS", "TDI", "TDO", "NMI", "RSTOUT", "CLKO", "ICE", "OBSCLK",
    "SYSCLKOUT", "XI", "XO",
}

_TRAILING_DIGITS = re.compile(r"\d+$")


def _prefix(signal: str) -> str:
    """instance token：支援多底線 instance 名（P3，2026-07-25 am6548 事故）。

    K3/域前綴命名的第一個 token 沒有 instance 編號（MCU_I2C0_SCL 的 MCU、
    WKUP_UART0_RXD 的 WKUP）——此時 instance＝前兩個 token（MCU_I2C0）。
    規則：token1 無尾數字且 token2 有尾數字 → 取兩段；否則取第一段
    （I2C2_SCL→I2C2、GPMC0_AD0→GPMC0——ST/主域行為與舊版完全相同）。
    ST 的 OCTOSPIM_P1_CLK 也因此正確歸 OCTOSPIM_P1（port 級 instance）。"""
    parts = signal.split("_")
    if (len(parts) >= 3 and not _TRAILING_DIGITS.search(parts[0])
            and _TRAILING_DIGITS.search(parts[1])):
        return f"{parts[0]}_{parts[1]}"
    return parts[0]


def _is_system(signal: str) -> bool:
    p = _prefix(signal)
    return p in _SYSTEM_PREFIXES or _TRAILING_DIGITS.sub("", p) in _SYSTEM_PREFIXES


def derive_all_peripheral(af_table: dict[str, dict[str, str]], soc: str) -> dict:
    """
    由 af_table 衍生 周邊 -> signals 全索引(對齊範本 all_peripheral.json 的
    結構)。純程式衍生、不經 LLM;af_table 重生後應一併重生本檔。
    """
    peripherals: dict[str, set[str]] = {}
    system: dict[str, set[str]] = {}

    all_signals = [
        s.strip()
        for muxes in af_table.values()
        for cell in muxes.values()
        for s in cell.split("/") if s.strip()
    ]
    # 第一趟:收集「域前綴」集合——雙 token instance(MCU_I2C0…)的 token1
    # (MCU、WKUP)。之後落單的裸域 token(MCU_RESETz → MCU)歸 system,
    # 但**非域**的無編號真週邊(DCMI、PCIE、USBH…)仍是週邊(P3;
    # stm32 回歸靠這個區分)。
    domains = {s.split("_", 1)[0] for s in all_signals
               if "_" in s and _prefix(s) != s.split("_", 1)[0]}

    for signal in all_signals:
        inst = _prefix(signal)
        if ("_" not in signal or _is_system(signal)
                or (inst in domains and not _TRAILING_DIGITS.search(inst))):
            key = _TRAILING_DIGITS.sub("", _prefix(signal)) or _prefix(signal)
            system.setdefault(key, set()).add(signal)
        else:
            peripherals.setdefault(inst, set()).add(signal)

    def _sorted(groups: dict[str, set[str]]) -> dict[str, list[str]]:
        return {
            k: sorted(groups[k], key=lambda s: (len(s), s)) for k in sorted(groups)
        }

    signal_count = sum(len(v) for v in peripherals.values()) + sum(
        len(v) for v in system.values()
    )
    return {
        "soc": soc,
        "source": (
            "Derived from af_table.json by knowledge_extract. Keys are the "
            "peripheral prefix of each signal (token before the first '_'); "
            "'system' holds non-peripheral SoC functions (debug/trace/clock-out/"
            "EVENTOUT etc.)."
        ),
        "peripheral_count": len(peripherals),
        "system_count": len(system),
        "signal_count": signal_count,
        "peripherals": _sorted(peripherals),
        "system": _sorted(system),
    }
