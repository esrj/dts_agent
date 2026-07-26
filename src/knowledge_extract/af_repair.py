"""R1:af_table 全量修復。

兩個工具:
1. Nuvoton pinfunc 權威重建——kernel 的 dt-bindings/pinctrl/<soc>-pinfunc.h
   把每支腳 × 每個功能的真實 MFP 值寫成巨集(reg offset, shift, MFP 值),
   比 LLM 解析手冊「序列式 pin 表」可靠(序列式掉一個 token 整列編號位移,
   ma35d1 的 CAN1/RGMII1/EADC0 漏抓與 mux 錯位都源於此)。有 pinfunc 就整表
   重建 af_table,手冊 LLM 抽取只當無 header 板子的 fallback。
2. 族群完整性自查——instance 編號出現空洞(有 CAN0/CAN3 沒 CAN1)通常是漏抓,
   列进 REVIEW 供人工確認。
"""
from __future__ import annotations

import glob
import json
import os
import re

_PINFUNC_DEFINE = re.compile(
    r"^#define\s+SYS_GP\w_MFP[LH]_P([A-N])(\d+)MFP_(\w+)\s+"
    r"0x[0-9A-Fa-f]+\s+0x[0-9A-Fa-f]+\s+(0x[0-9A-Fa-f]+)",
    re.M,
)


def find_pinfunc_header(include_root: str | None, soc_hint: str) -> str | None:
    """在 include/dt-bindings/pinctrl/ 下找該 SoC 的 pinfunc header。"""
    if not include_root:
        return None
    hint = re.sub(r"[^a-z0-9]", "", soc_hint.lower())[:6]
    for path in sorted(glob.glob(
            os.path.join(include_root, "dt-bindings", "pinctrl", "*pinfunc*"))):
        stem = re.sub(r"[^a-z0-9]", "", os.path.basename(path).lower())
        if hint and hint in stem:
            return path
    return None


def parse_pinfunc(path: str) -> dict[str, dict[str, str]]:
    """pinfunc header → af_table 結構 {pin: {mux(str): signal}}。

    跳過 GPIO 條目(MFP 0 = GPIO 本身,af_table 慣例不列);同 (pin, mux)
    多個訊號名以 '/' 併列(與手冊 cell 慣例一致)。
    """
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        text = f.read()
    table: dict[str, dict[str, str]] = {}
    for bank, num, signal, mux_hex in _PINFUNC_DEFINE.findall(text):
        if signal == "GPIO":
            continue
        pin = f"P{bank}{int(num)}"
        mux = str(int(mux_hex, 16))
        slot = table.setdefault(pin, {})
        if mux in slot:
            existing = slot[mux].split("/")
            if signal not in existing:
                slot[mux] = "/".join(existing + [signal])
        else:
            slot[mux] = signal
    return {
        pin: {m: table[pin][m] for m in sorted(table[pin], key=int)}
        for pin in sorted(table)
    }


def rebuild_af_table(
    af_path: str, pinfunc_path: str, soc: str, *, log=print
) -> dict | None:
    """用 pinfunc 整表重建 af_table;無差異回 None,有差異回 diff 摘要。"""
    rebuilt = parse_pinfunc(pinfunc_path)
    old: dict = {}
    if os.path.isfile(af_path):
        with open(af_path, "r", encoding="utf-8") as f:
            old = json.load(f)

    def flat(t):
        return {(p, m, s.strip())
                for p, muxes in t.items() if isinstance(muxes, dict)
                for m, cell in muxes.items() for s in str(cell).split("/")}

    old_set, new_set = flat(old), flat(rebuilt)
    if old_set == new_set:
        return None
    added, removed = new_set - old_set, old_set - new_set

    with open(af_path, "w", encoding="utf-8") as f:
        json.dump(rebuilt, f, ensure_ascii=False, indent=1)
        f.write("\n")
    diff = {
        "source": os.path.basename(pinfunc_path),
        "pins": len(rebuilt),
        "added": len(added),
        "removed": len(removed),
        "sample_added": sorted(added)[:8],
        "sample_removed": sorted(removed)[:8],
    }
    log(f"[{soc}] af_table 以 {diff['source']} 權威重建:"
        f"{diff['pins']} pins,+{diff['added']}/-{diff['removed']} 條目"
        "(舊表為 LLM 序列式解析,mux 編號在掉 token 後整列位移——以 header 為準)")
    return diff


def family_gaps(af_table: dict) -> list[str]:
    """instance 編號空洞檢查:回傳警告列表(空洞常見即漏抓)。"""
    fams: dict[str, set[int]] = {}
    for muxes in af_table.values():
        if not isinstance(muxes, dict):
            continue
        for cell in muxes.values():
            for sig in str(cell).split("/"):
                m = re.match(r"([A-Z][A-Z_]*?)(\d+)_", sig.strip())
                if m:
                    fams.setdefault(m.group(1), set()).add(int(m.group(2)))
    out = []
    for fam, nums in sorted(fams.items()):
        if len(nums) < 2:
            continue
        full = set(range(min(nums), max(nums) + 1))
        holes = sorted(full - nums)
        if holes:
            out.append(f"family {fam} 有 instance {sorted(nums)},"
                       f"缺 {holes}——空洞常見即漏抓,請對照手冊確認")
    return out
