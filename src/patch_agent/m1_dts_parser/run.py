#!/usr/bin/env python3
"""M1 standalone runner — 手動測試「共用 Baseline DTS Parser」這一段。

只跑 M1：把 baseline DTS 解析成結構化 index，然後讓你查任何 group / node / def /
signal，或把整個 index dump 成 JSON。不依賴其他 milestone。

用法（兩種都可以）：
    python src/patch_agent/m1_dts_parser/run.py            # 總覽 + usart2 範例
    PYTHONPATH=src python -m patch_agent.m1_dts_parser.run --group usart2_pins_a

常用：
    --summary                 總覽（預設）
    --group  <label>          顯示某個 pinctrl group 的腳位/AF/signal/電氣屬性
    --node   <label>          顯示某個 &override 的現狀（status/pinctrl-0/props…）
    --def    <label>          顯示 SoC 節點定義（compatible/reg/#cells）
    --signal <PIN> <AF>       (pin, af) -> signal 查表，如 --signal PA4 6
    --family <label>          顯示 node label 屬於哪個 family
    --groups   [substr]       列出所有 pinctrl group label（可過濾）
    --overrides               列出 board 啟用的 &override
    --labels   [substr]       列出所有定義過的 label（可過濾）
    --dump   <path.json>      把整個 index 序列化成 JSON 方便檢視
"""
import os
import sys
import json
import argparse
from dataclasses import asdict

# 讓「直接用路徑執行」也能 import：把 repo 的 src/ 加進 path
_SRC = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

from patch_agent.m1_dts_parser import build_index, family_of   # noqa: E402
from patch_agent import config                                  # noqa: E402


# ---- pretty printers -----------------------------------------------------
def show_group(idx, label):
    g = idx.group(label)
    if not g:
        print(f"  (找不到 pinctrl group: {label})")
        cand = [l for l in idx.pinctrl_groups if label.lower() in l.lower()][:8]
        if cand:
            print("  你是不是要找：", ", ".join(cand))
        return
    print(f"pinctrl group  {g.label}  ({g.name})   @ {g.file}:{g.line}")
    for e in g.entries:
        af = f"AF{e.af}" if e.af is not None else e.mode
        elec = " ".join(e.electrical) or "-"
        print(f"  {e.pin:5} {af:7} {str(e.signal or '?'):28} [{e.subnode}]  電氣: {elec}")


def show_node(idx, label):
    ov = idx.override(label)
    if not ov:
        print(f"  (board 檔沒有 &{label} 的 override；可能未在板級啟用)")
        return
    print(f"&{ov.label}  @ {ov.file}:{ov.line}   status={ov.status}  enabled={ov.enabled}")
    print(f"  pinctrl-names: {ov.pinctrl_names}")
    for k, v in ov.pinctrl.items():
        print(f"  {k}: {v}")
    other = {k: v for k, v in ov.props.items() if not k.startswith('pinctrl') and k != 'status'}
    if other:
        print("  其他屬性:")
        for k, v in other.items():
            print(f"    {k} = {v}")
    if ov.deletions:
        print(f"  /delete-property/: {ov.deletions}")
    if ov.children:
        print(f"  child nodes: {[c.name for c in ov.children]}")


def show_def(idx, label):
    d = idx.node_def(label)
    if not d:
        print(f"  (找不到 node 定義: {label})")
        return
    print(f"node def  {d.label}: {d.name}   @ {d.file}:{d.line}")
    print(f"  compatible      = {d.compatible}")
    print(f"  reg             = {d.reg}")
    print(f"  unit_address    = {d.unit_address}")
    print(f"  #address-cells  = {d.address_cells}   #size-cells = {d.size_cells}")


def summary(idx):
    print("=" * 64)
    print("M1 — Baseline DTS Index 總覽")
    print("=" * 64)
    print(f"  解析檔案數      : {len(idx.files)}  ({', '.join(sorted(idx.files))})")
    print(f"  pinctrl groups  : {len(idx.pinctrl_groups)}")
    print(f"  board overrides : {len(idx.overrides)}  (enabled: {len(idx.enabled_overrides())})")
    print(f"  node defs       : {len(idx.defs)}")
    print(f"  all labels      : {len(idx.all_labels)}")
    print()
    print("範例 ── USART2:")
    print("-" * 64)
    show_node(idx, "usart2")
    print()
    show_group(idx, "usart2_pins_a")
    print()
    print("提示：用 --group/--node/--def/--signal 查任何節點；--dump 匯出整個 index。")


def serialize(idx):
    return {
        "files": {k: len(v) for k, v in idx.files.items()},
        "pinctrl_groups": {l: asdict(g) for l, g in idx.pinctrl_groups.items()},
        "overrides": {
            l: {
                "label": o.label, "file": o.file, "line": o.line,
                "status": o.status, "enabled": o.enabled,
                "pinctrl_names": o.pinctrl_names, "pinctrl": o.pinctrl,
                "props": {k: v for k, v in o.props.items() if not k.startswith('pinctrl')},
                "deletions": o.deletions,
                "children": [c.name for c in o.children],
            } for l, o in idx.overrides.items()
        },
        "defs": {l: asdict(d) for l, d in idx.defs.items()},
        "all_labels": sorted(idx.all_labels),
    }


def main(argv=None):
    ap = argparse.ArgumentParser(description="M1 standalone — Baseline DTS parser/index")
    ap.add_argument("--summary", action="store_true")
    ap.add_argument("--group", metavar="LABEL")
    ap.add_argument("--node", metavar="LABEL")
    ap.add_argument("--def", dest="ndef", metavar="LABEL")
    ap.add_argument("--signal", nargs=2, metavar=("PIN", "AF"))
    ap.add_argument("--family", metavar="LABEL")
    ap.add_argument("--groups", nargs="?", const="", metavar="SUBSTR")
    ap.add_argument("--overrides", action="store_true")
    ap.add_argument("--labels", nargs="?", const="", metavar="SUBSTR")
    ap.add_argument("--dump", metavar="PATH")
    args = ap.parse_args(argv)

    print(f"[build_index] dts_dir = {config.DTS_DIR}")
    idx = build_index()

    did = False
    if args.group:
        show_group(idx, args.group); did = True
    if args.node:
        show_node(idx, args.node); did = True
    if args.ndef:
        show_def(idx, args.ndef); did = True
    if args.signal:
        pin, af = args.signal
        print(f"  {pin} AF{af} -> {idx.signal_for(pin, int(af))}"); did = True
    if args.family:
        print(f"  family_of({args.family}) = {family_of(args.family)}"); did = True
    if args.groups is not None:
        for l in sorted(idx.pinctrl_groups):
            if args.groups in l:
                print(" ", l)
        did = True
    if args.overrides:
        for l, o in sorted(idx.enabled_overrides().items()):
            print(f"  &{l:12} pinctrl-0={o.pinctrl_0}")
        did = True
    if args.labels is not None:
        for l in sorted(idx.all_labels):
            if args.labels in l:
                print(" ", l)
        did = True
    if args.dump:
        json.dump(serialize(idx), open(args.dump, "w"), indent=2, ensure_ascii=False)
        print(f"  dumped index -> {args.dump}"); did = True

    if not did or args.summary:
        summary(idx)


if __name__ == "__main__":
    main()
