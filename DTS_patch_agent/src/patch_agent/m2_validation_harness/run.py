#!/usr/bin/env python3
"""M2 standalone runner — 手動測試「Stage 1 Validation Harness」這一段。

只跑 Stage 1：吃一份 plan.csv，對 af_table / gpio_pins / require / profiles 做驗證，
印出 pass/fail、錯誤清單、validated_plan_ir 摘要、regression checks、與 baseline 的 diff。
不依賴其他 milestone。

用法：
    python src/patch_agent/m2_validation_harness/run.py                  # 用預設 plan.csv
    PYTHONPATH=src python -m patch_agent.m2_validation_harness.run --plan myplan.csv

選項：
    --plan <path.csv>     指定要驗的 plan（預設 output/plan/plan.csv）
    --no-baseline         不做與 baseline.csv 的 diff
    --ir                  印出完整 validated_plan_ir（每個 peripheral）
    --dump <path.json>    把整個 Stage1Result 寫成 JSON
"""
import os
import sys
import json
import argparse

_SRC = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

from patch_agent import config                                  # noqa: E402
from patch_agent.m2_validation_harness import run               # noqa: E402


def main(argv=None):
    ap = argparse.ArgumentParser(description="M2 standalone — Stage 1 Validation Harness")
    ap.add_argument("--plan", default=str(config.PLAN_CSV))
    ap.add_argument("--no-baseline", action="store_true")
    ap.add_argument("--ir", action="store_true")
    ap.add_argument("--dump")
    args = ap.parse_args(argv)

    print(f"[stage1] plan = {args.plan}")
    r = run(plan_csv=args.plan, with_baseline=not args.no_baseline)

    print("=" * 64)
    print(f"  RESULT: {'PASS ✅' if r.passed else 'FAIL ❌'}    "
          f"({len(r.validated_plan_ir['peripherals'])} peripherals, {len(r.errors)} errors)")
    print("=" * 64)

    if r.errors:
        print("\n錯誤:")
        for e in r.errors:
            loc = "/".join(x for x in (e.peripheral, e.signal, e.pin) if x)
            print(f"  [{e.error_type}] {loc}: {e.message}")

    print("\nvalidated_plan_ir (摘要):")
    for per, info in r.validated_plan_ir["peripherals"].items():
        nsig = len(info["signals"])
        print(f"  {per:9} -> {str(info['target_node']):14} family={str(info['family']):8} "
              f"mode={str(info['mode']):11} signals={nsig}")
        if args.ir:
            for s, v in info["signals"].items():
                print(f"        {s:24} {v['pin']:5} AF{v['af']}")

    if r.diff is not None:
        print(f"\n與 baseline 的 diff: changed={len(r.diff['changed'])} "
              f"added={len(r.diff['added'])} removed={len(r.diff['removed'])}")

    print("\nboard_regression_checks:")
    rc = r.board_regression_checks
    print(f"  protected_pins   : {len(rc['protected_pins'])} 個")
    print(f"  board_pin_locked : {rc['board_pin_locked']}")
    print(f"  boot_required    : {rc['boot_required_nodes']}")

    if args.dump:
        json.dump(r.to_dict(), open(args.dump, "w"), indent=2, ensure_ascii=False, default=str)
        print(f"\n  dumped Stage1Result -> {args.dump}")

    return 0 if r.passed else 1


if __name__ == "__main__":
    sys.exit(main())
