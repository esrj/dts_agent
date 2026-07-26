"""Prompt 5(pin_data_supply.md):pinmux 渲染供料兩檔——純程式,零 LLM。

- pinmux_style.json:該板渲染樣式宣告(style 白名單 k3-iopad / nuvoton-mfp;
  ST 板不產,DTS_agent 缺檔預設 stm32)。
- pad_params.json:pad → 渲染參數全表。
  * K3:datasheet「Pad Configuration Registers」表(快取頁面 regex 直抓,
    與 R1 同源)+ baseline DTS 錨點校準 offset 基底(位址 − DTS offset)。
    鍵空間 (domain, offset)——MAIN/WKUP 各自從 0 起算。
  * MA35D1:ma35d1-pinfunc.h 的 (reg, shift) 位段(每 pad 一個 4-bit 欄;
    形狀已依官方 DTS 實例確認:nuvoton,pins = <巨集 &pcfg>,巨集展開為
    (reg, shift, value) 三 cell,value 即 af_table 的 mux)。
"""
from __future__ import annotations

import json
import os
import re
from configparser import ConfigParser

from .paths import BOARDS_INI, CACHE_DIR

_TI_PAD_ROW = re.compile(
    r"(0x[0-9A-Fa-f]{7,8})\s+CTRLMMR\S*PADCONFI\S*\s+(\S+)\s+([A-Za-z]\w+)"
)
_PINFUNC_ROW = re.compile(
    r"^#define\s+SYS_GP\w_MFP[LH]_P([A-N])(\d+)MFP_\w+\s+"
    r"(0x[0-9A-Fa-f]+)\s+(0x[0-9A-Fa-f]+)\s+0x[0-9A-Fa-f]+",
    re.M,
)


def _pages_cache_for(board: str) -> str | None:
    """boards.ini 反查該板的手冊 PDF → 頁面快取 JSON 路徑。"""
    ini = ConfigParser()
    ini.optionxform = str
    if os.path.exists(BOARDS_INI):
        ini.read(BOARDS_INI, encoding="utf-8")
    if not ini.has_section("boards"):
        return None
    for pdf, b in ini.items("boards"):
        if b == board:
            stem = os.path.splitext(pdf)[0]
            path = os.path.join(CACHE_DIR, "pages", f"{stem}.json")
            if os.path.isfile(path):
                return path
    return None


# --------------------------------------------------------------------------- #
# K3(TI)
# --------------------------------------------------------------------------- #
def ti_pmx_nodes(result: dict) -> dict[str, tuple[int, int]]:
    """closure 中 pinctrl-single 節點 → {label: (位址基底, 長度)}。

    domain 就是 pmx 節點 label(AM65 的 MAIN 有 main_pmx0/main_pmx1 兩個
    節點,offset 各自從自身 reg 基底起算——(MAIN, offset) 平表必撞鍵)。
    """
    out: dict[str, tuple[int, int]] = {}
    for tree in result["trees"].values():
        for node in tree.walk():
            if not node.label or "pinctrl-single" not in \
                    (node.props.get("compatible") or ""):
                continue
            nums = [int(x, 16) for x in
                    re.findall(r"0x[0-9A-Fa-f]+",
                               node.props.get("reg") or "")]
            # reg = <hi base hi size>(2-cell 位址):取非零的 (base, size) 對
            if len(nums) >= 4:
                out[node.label] = (nums[1], nums[3])
            elif len(nums) >= 2:
                out[node.label] = (nums[0], nums[1])
    return out


def ti_pad_params(
    board: str,
    af_table: dict,
    result: dict,
    warnings: list[str],
) -> dict[str, dict] | None:
    """datasheet PADCONFIG 位址表 × pmx 節點 reg 視窗 → {pad: {domain, offset}}。

    基底與視窗直接取自官方 DTS 的 pinctrl-single reg 屬性(不靠推斷);
    datasheet 每列位址落在哪個視窗,pad 就屬哪個 domain,offset = 位址 − 基底。
    """
    pmx = ti_pmx_nodes(result)
    if not pmx:
        warnings.append("pad_params:closure 中找不到 pinctrl-single 節點")
        return None
    cache = _pages_cache_for(board)
    rows: list[tuple[int, str]] = []
    if cache is None:
        warnings.append("pad_params:找不到手冊頁面快取,退回僅 baseline 錨點")
    else:
        with open(cache, "r", encoding="utf-8") as f:
            pages = json.load(f)["pages"]
        rows = [(int(addr, 16), pad)
                for p in pages
                for addr, _ball, pad in _TI_PAD_ROW.findall(p)]

    canon = {p.upper(): p for p in af_table}
    pads: dict[str, dict] = {}
    unmapped = 0
    for addr, pad in rows:
        key = canon.get(pad.upper())
        if key is None:
            continue                     # 非 mux pad(PORz、電源腳等)
        domain = next((lbl for lbl, (base, size) in pmx.items()
                       if base <= addr < base + size), None)
        if domain is None:
            unmapped += 1
            continue
        base = pmx[domain][0]
        pads[key] = {"domain": domain, "offset": f"0x{addr - base:04x}"}
    if unmapped:
        warnings.append(f"pad_params:{unmapped} 列位址不落在任何 pmx reg 視窗"
                        "(通常是保留 pad)")
    # 錨點兜底(datasheet 缺列或無快取時仍保證 baseline 覆蓋率)。
    # (domain, offset) 鍵空間 dedup:datasheet 的 pad 正名優先——DTS 群組
    # 註解常寫「該 mux 的功能名」(同一 pad 在 GPIO 群組叫 WKUP_GPIO0_24、
    # OSPI 群組叫 MCU_OSPI0_CSn1),異名重登會撞鍵空間(P4 擴掃後浮現)。
    taken = {(v["domain"], v["offset"]) for v in pads.values()}
    for pad_u, (domain, offset) in (result.get("ti_pads") or {}).items():
        key = canon.get(pad_u, pad_u)
        slot = (domain, f"0x{offset:04x}")
        if key in pads or slot in taken:
            continue
        pads[key] = {"domain": domain, "offset": slot[1]}
        taken.add(slot)

    if not pads:
        return None
    return dict(sorted(pads.items()))


def ti_style(closure_text: str, baseline: str, result: dict,
             warnings: list[str]) -> dict:
    pmx = ti_pmx_nodes(result)
    macros = {}
    nodes = {}
    for label in sorted(pmx):
        macro = "AM65X_WKUP_IOPAD" if "wkup" in label else "AM65X_IOPAD"
        if macro in closure_text:
            macros[label] = macro
            nodes[label] = f"&{label}"
    if not macros:
        warnings.append("pinmux_style:baseline 中找不到 IOPAD 巨集")
    flags_present = {t for t in
                     ("PIN_INPUT", "PIN_OUTPUT", "PIN_INPUT_PULLUP",
                      "PIN_OUTPUT_PULLUP", "PIN_INPUT_PULLDOWN")
                     if t in closure_text}
    role_flags = {
        "rx": "PIN_INPUT",
        "tx": "PIN_OUTPUT",
        "clk": "PIN_OUTPUT",
        "bidir": "PIN_INPUT",
        "default": "PIN_INPUT",
    }
    for role, flag in role_flags.items():
        if flag not in flags_present:
            warnings.append(f"pinmux_style:{flag}(role {role})未出現於官方 DTS")
    return {
        "style": "k3-iopad",
        "description": (
            "TI K3 AM65x pinctrl-single IOPAD 巨集。domain = pmx 節點 label"
            "(MAIN 有 main_pmx0/main_pmx1 兩個節點,offset 各自起算);"
            "渲染的群組須放進 nodes 對應的 &節點下。"
        ),
        "source": f"{baseline}(kernel 官方 DTS 實抓)",
        "macros": macros,
        "nodes": nodes,
        "role_flags": role_flags,
    }


# --------------------------------------------------------------------------- #
# Nuvoton MA35(形狀依官方 DTS 實例 + pinfunc.h 確認)
# --------------------------------------------------------------------------- #
def nuvoton_pad_params(pinfunc_path: str, af_table: dict) -> dict[str, dict]:
    """pinfunc.h → {pin: {domain:"SYS", offset(MFP reg), shift}}。

    每 pin 的 (reg, shift) 為常數(4-bit MFP 位段);渲染值 = af_table mux。
    """
    with open(pinfunc_path, "r", encoding="utf-8", errors="replace") as f:
        text = f.read()
    canon = {p.upper(): p for p in af_table}
    pads: dict[str, dict] = {}
    for bank, num, reg, shift in _PINFUNC_ROW.findall(text):
        pin = f"P{bank}{int(num)}"
        key = canon.get(pin.upper(), pin)
        pads.setdefault(key, {
            "domain": "SYS",
            "offset": f"0x{int(reg, 16):03x}",
            "shift": f"0x{int(shift, 16):02x}",
        })
    return dict(sorted(pads.items()))


def nuvoton_style(closure_text: str, baseline: str,
                  warnings: list[str]) -> dict:
    conf_labels = sorted(set(re.findall(r"&(pcfg_[\w]+)", closure_text)))
    if "SYS_GP" not in closure_text:
        warnings.append("pinmux_style:baseline 中找不到 SYS_GP MFP 巨集")
    return {
        "style": "nuvoton-mfp",
        "description": (
            "Nuvoton MA35 nuvoton,pins 條目:<MFP巨集 &pcfg組態>;巨集展開為 "
            "(reg, shift, value) 三 cell,value 即 af_table 的 mux"
        ),
        "source": f"{baseline} + dt-bindings/pinctrl/ma35d1-pinfunc.h",
        "macros": {
            "SYS": "SYS_GP{bank}_MFP{half}_P{bank}{num}MFP_{signal}",
        },
        "macro_rules": {
            "half": "num 0-7 -> L;num 8-15 -> H",
            "cells": "(offset, shift, value);offset/shift 見 pad_params,"
                     "value = af_table 的 mux",
        },
        "role_flags": {"default": "&pcfg_default"},
        "conf_labels": conf_labels,
    }


def build_supply(
    vendor: str,
    board: str,
    result: dict,
    af_table: dict,
    pinfunc_path: str | None,
    warnings: list[str],
) -> tuple[dict | None, dict | None]:
    """回傳 (pinmux_style, pad_params) 內容;ST 板回 (None, None)。"""
    if vendor == "st":
        return None, None
    closure_text = "\n".join(t for _, t in result["closure"])
    baseline = result["baseline"]
    if vendor == "ti":
        style = ti_style(closure_text, baseline, result, warnings)
        pads = ti_pad_params(board, af_table, result, warnings)
    elif vendor == "nuvoton":
        style = nuvoton_style(closure_text, baseline, warnings)
        pads = nuvoton_pad_params(pinfunc_path, af_table) \
            if pinfunc_path else None
        if pads is None:
            warnings.append("pad_params:找不到 pinfunc header,無法生成")
    else:
        return None, None
    pad_obj = None
    if pads:
        pad_obj = {
            "board": board,
            "source": ("datasheet Pad Configuration Registers 表 + baseline "
                       "DTS 錨點校準" if vendor == "ti"
                       else "dt-bindings/pinctrl pinfunc header(MFP 位段)"),
            "description": (
                "pad → pinmux 渲染參數;鍵與 af_table 的 pin 鍵逐字一致。"
                "K3 鍵空間為 (domain, offset);MA35 為 (offset, shift) 位段。"
            ),
            "pads": pads,
        }
    return style, pad_obj
