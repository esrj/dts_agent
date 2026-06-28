"""Standalone M7 Validator runner.

Usage:
    # full chain: Locator -> Generator (LLM, cache-hit if unchanged) -> Validator
    PYTHONPATH=src python3 -m patch_agent.m7_validator.run [--plan P] [--no-compile]

    # validate the artifacts already on disk (needs diff_plan.json from
    # `m5_locator.run --write` and the generated dts from `m6_generator.run --write`)
    PYTHONPATH=src python3 -m patch_agent.m7_validator.run --from-output
"""
import argparse
import json
import sys

from .. import config
from .validate import validate, run as full_run, write_report


def _print(rep):
    print(f"passed={rep.passed}  failed_layer={rep.failed_layer}")
    for name, status in rep.checks.items():
        mark = {"pass": "✓", "fail": "✗", "skipped": "-"}.get(status, "?")
        print(f"  [{mark}] {name}: {status}")
    for e in rep.errors:
        loc = f" @{e.file}:{e.line}" if e.line else ""
        per = f" [{e.peripheral}]" if e.peripheral else ""
        print(f"  ERROR L{e.layer} {e.error_type}{per}{loc}: {e.message}")
    for w in rep.warnings[:5]:
        print(f"  warn: {w}")
    if len(rep.warnings) > 5:
        print(f"  ... {len(rep.warnings) - 5} more warnings (see validation_report.json)")


def main():
    ap = argparse.ArgumentParser(description="M7 Validator — six-layer gate + dtc")
    ap.add_argument("--plan", default=None)
    ap.add_argument("--from-output", action="store_true",
                    help="validate output/generated/ artifacts already on disk")
    ap.add_argument("--no-compile", action="store_true", help="skip cpp/dtc layer")
    args = ap.parse_args()

    if args.from_output:
        try:
            dts = open(config.GENERATED_DTS).read()
            dp = json.load(open(config.DIFF_PLAN))
            edits = json.load(open(config.STRUCTURED_EDITS))
        except FileNotFoundError as e:
            print(f"missing artifact: {e.filename}\n"
                  f"run `python3 -m patch_agent.m5_locator.run --write` and "
                  f"`python3 -m patch_agent.m6_generator.run --write` first")
            sys.exit(2)
        rep = validate(dts, dp, edits, run_compile=not args.no_compile)
        write_report(rep)
    else:
        _, _, rep = full_run(plan_csv=args.plan, run_compile=not args.no_compile)

    _print(rep)
    print(f"\nwrote {config.VALIDATION_REPORT}")
    sys.exit(0 if rep.passed else 1)


if __name__ == "__main__":
    main()
