#!/usr/bin/env python3
"""M4 standalone runner — 手動測試「Stage 3 Patch Generation」這一段。

跑 Stage 1→2→3：對每個 peripheral 決定 noop/reuse/generate，渲染 managed region
（含 idle/sleep 多狀態），產出合併後的 .dts 與 patch。

用法：
    python src/patch_agent/m4_patch_generation/run.py            # 印 report + managed region
    python src/patch_agent/m4_patch_generation/run.py --write    # 寫入 output/generated/
    python src/patch_agent/m4_patch_generation/run.py --region   # 只印 managed region
    python src/patch_agent/m4_patch_generation/run.py --patch     # 只印 unified diff
    PYTHONPATH=src python -m patch_agent.m4_patch_generation.run --plan myplan.csv --write
"""
import os
import sys
import argparse

_SRC = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

from patch_agent import config                          # noqa: E402
from patch_agent.m4_patch_generation import run         # noqa: E402


def main(argv=None):
    ap = argparse.ArgumentParser(description="M4 standalone — Stage 3 Patch Generation")
    ap.add_argument("--plan", default=str(config.PLAN_CSV))
    ap.add_argument("--write", action="store_true", help="write to output/generated/")
    ap.add_argument("--region", action="store_true", help="print managed region only")
    ap.add_argument("--patch", action="store_true", help="print unified diff only")
    args = ap.parse_args(argv)

    print(f"[stage3] plan = {args.plan}")
    s1, s2, art = run(plan_csv=args.plan, write=args.write)

    if args.region:
        print(art.managed_region or "(無變更)")
        return 0
    if args.patch:
        print(art.patch or "(無變更，無 patch)")
        return 0

    print("=" * 64)
    print(f"  Stage1: {'PASS' if s1.passed else 'FAIL ('+','.join(s1.error_types())+')'}"
          f"   |   blocking_conflict: {s2.has_blocking_conflict}")
    print("=" * 64)
    print(f"\n{'peripheral':10} {'action':9} states / note")
    print("-" * 64)
    for r in art.report["peripherals"]:
        info = r.get("states") or r.get("warnings") or ""
        print(f"  {r['peripheral']:10} {r['action']:9} {info}")

    if art.managed_region:
        nlines = art.managed_region.count("\n") + 1
        print(f"\nmanaged region: {nlines} 行；patch: {art.patch.count(chr(10))} 行（1 hunk, append at EOF）")
        print("（--region 看完整 region，--patch 看 diff，--write 寫入 output/generated/）")
    else:
        print("\n沒有需要變更的 peripheral。")

    if args.write:
        print(f"\n已寫入：\n  {config.GENERATED_DTS}\n  {config.GENERATED_PATCH}"
              f"\n  {config.STRUCTURED_EDITS}\n  {config.GENERATION_REPORT}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
