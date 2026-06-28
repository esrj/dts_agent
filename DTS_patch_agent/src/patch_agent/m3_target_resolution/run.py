#!/usr/bin/env python3
"""M3 standalone runner — 手動測試「Stage 2 Target Resolution」這一段。

跑 Stage 1（驗證）+ Stage 2（定位/決策）：對每個 peripheral 印出 target node、
是否在 baseline 啟用、既有 pinctrl group、reuse-or-generate 決策、以及跨 peripheral
的腳位衝突。

用法：
    python src/patch_agent/m3_target_resolution/run.py                 # 預設 plan.csv
    PYTHONPATH=src python -m patch_agent.m3_target_resolution.run --plan myplan.csv

選項：
    --plan <path.csv>     指定 plan（預設 output/plan/plan.csv）
    --peripheral <NAME>   只看某個 peripheral 的細節
    --dump <path.json>    把 Stage2Result 寫成 JSON
"""
import os
import sys
import json
import argparse

_SRC = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

from patch_agent import config                          # noqa: E402
from patch_agent.m3_target_resolution import run        # noqa: E402


def show_detail(tr):
    print(f"\n● {tr.peripheral}  ->  {tr.target_node}   (file: {tr.target_file})")
    print(f"    family={tr.family}  enabled_in_baseline={tr.enabled_in_baseline}")
    print(f"    existing pinctrl-0 : {tr.existing_pinctrl_0}")
    print(f"    existing group set : {tr.existing_group_set}")
    print(f"    plan set           : {tr.plan_set}")
    print(f"    decision           : {tr.reuse_or_generate.upper()}", end="")
    if tr.reuse_or_generate == "reuse":
        print(f"  -> 重用 {tr.reuse_group}")
    else:
        print(f"  -> 生成 {tr.new_group_label}")
    for c in tr.conflicts:
        print(f"    ⚠ conflict: {c.pin} ({c.plan_signal}) 目前屬於 &{c.baseline_owner}"
              f" [{c.owner_group}] -> {c.resolution}")


def main(argv=None):
    ap = argparse.ArgumentParser(description="M3 standalone — Stage 2 Target Resolution")
    ap.add_argument("--plan", default=str(config.PLAN_CSV))
    ap.add_argument("--peripheral")
    ap.add_argument("--dump")
    args = ap.parse_args(argv)

    print(f"[stage2] plan = {args.plan}")
    s1, s2 = run(plan_csv=args.plan)

    print("=" * 70)
    print(f"  Stage1: {'PASS' if s1.passed else 'FAIL (' + ','.join(s1.error_types()) + ')'}"
          f"   |   Stage2 blocking_conflict: {s2.has_blocking_conflict}")
    print("=" * 70)

    if args.peripheral:
        tr = s2.resolutions.get(args.peripheral)
        if tr:
            show_detail(tr)
        else:
            print(f"  (plan 沒有 {args.peripheral})")
    else:
        print(f"\n{'peripheral':10} {'node':12} {'enabled':8} {'decision':9} group / conflicts")
        print("-" * 70)
        for per, tr in s2.resolutions.items():
            grp = tr.reuse_group if tr.reuse_or_generate == "reuse" else [tr.new_group_label]
            cf = f"  ⚠{len(tr.conflicts)}" if tr.conflicts else ""
            print(f"{per:10} {tr.target_node:12} {str(tr.enabled_in_baseline):8} "
                  f"{tr.reuse_or_generate:9} {grp}{cf}")
        print("\n(用 --peripheral <NAME> 看單一 peripheral 的完整細節)")

    if args.dump:
        json.dump(s2.to_dict(), open(args.dump, "w"), indent=2, ensure_ascii=False, default=str)
        print(f"\n  dumped Stage2Result -> {args.dump}")

    return 0 if not s2.has_blocking_conflict else 1


if __name__ == "__main__":
    sys.exit(main())
