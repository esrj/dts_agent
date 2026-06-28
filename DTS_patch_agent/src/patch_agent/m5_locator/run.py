"""Standalone M5 Locator runner.

Usage:
    PYTHONPATH=src python3 -m patch_agent.m5_locator.run [--plan PATH] [--write]

--write dumps diff_plan.json + locator_report.json to output/generated/.
"""
import argparse
import json

from .. import config
from .locate import run as locate_run


def main():
    ap = argparse.ArgumentParser(description="M5 Locator — diff plan + context packs")
    ap.add_argument("--plan", default=None, help="plan.csv path (default: config.PLAN_CSV)")
    ap.add_argument("--write", action="store_true", help="write JSON outputs to output/generated/")
    args = ap.parse_args()

    dp, s1, s2 = locate_run(plan_csv=args.plan)

    print(f"passed={dp.passed}  blocking_conflict={dp.has_blocking_conflict}  "
          f"extra_columns={dp.extra_columns or '-'}")
    print(f"\nto_enable_or_update ({len(dp.to_enable_or_update)}):")
    for t in dp.to_enable_or_update:
        extras = sum(1 for r in t.plan_rows if r["extras"])
        fc = t.context_pack.get("fixed_connection") or {}
        ics = len(fc.get("ics", []))
        print(f"  {t.peripheral:10s} {t.action:8s} {t.target_node:12s} "
              f"enabled={str(t.enabled_in_baseline):5s} rows={len(t.plan_rows)} "
              f"extra_rows={extras} ics={ics}")
    print(f"\nto_disable ({len(dp.to_disable)}):")
    for d in dp.to_disable:
        print(f"  {d['node']:12s} [{d['source']}] {d['reason']}")
    print(f"\nuntouched ({len(dp.untouched)}):")
    for u in dp.untouched[:8]:
        print(f"  {u['node']:16s} {u['reason']}")
    if len(dp.untouched) > 8:
        print(f"  ... and {len(dp.untouched) - 8} more (see locator_report.json)")
    if dp.errors:
        print(f"\nerrors ({len(dp.errors)}):")
        for e in dp.errors:
            print(f"  [{e.error_type}] {e.message}")
    for w in dp.warnings:
        print(f"  WARN {w}")

    if args.write:
        config.OUTPUT_GEN.mkdir(parents=True, exist_ok=True)
        json.dump(dp.to_dict(), open(config.DIFF_PLAN, "w"),
                  indent=2, ensure_ascii=False)
        report = {"board": dp.board, "passed": dp.passed,
                  "stage1": s1.to_dict(), "stage2": s2.to_dict()}
        json.dump(report, open(config.LOCATOR_REPORT, "w"),
                  indent=2, ensure_ascii=False, default=str)
        print(f"\nwrote {config.DIFF_PLAN}")
        print(f"wrote {config.LOCATOR_REPORT}")


if __name__ == "__main__":
    main()
