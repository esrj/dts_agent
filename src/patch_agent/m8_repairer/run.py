"""Standalone M8 Repairer runner — the full self-healing pipeline.

Usage:
    PYTHONPATH=src python3 -m patch_agent.m8_repairer.run \
        [--plan P] [--max-rounds N] [--no-compile] [--no-cache]

Locator -> Generator (LLM) -> Validator; on failure the Repairer fixes the
affected targets' edits and re-validates, up to --max-rounds (default 3).
Boot-safety conflicts and needs_info stop immediately and ask the user.
Exit code: 0 pass, 1 fail/stopped.
"""
import argparse
import sys

from .. import config
from .repair import run as repair_run


def main():
    ap = argparse.ArgumentParser(description="M8 Repairer — generate/validate/repair loop")
    ap.add_argument("--plan", default=None)
    ap.add_argument("--max-rounds", type=int, default=3)
    ap.add_argument("--no-compile", action="store_true")
    ap.add_argument("--no-cache", action="store_true")
    ap.add_argument("--model", default=None)
    args = ap.parse_args()

    res = repair_run(plan_csv=args.plan, model=args.model,
                     max_rounds=args.max_rounds,
                     run_compile=not args.no_compile,
                     use_cache=not args.no_cache)

    for h in res.history:
        print(f"  round {h.get('round')}: " +
              ", ".join(f"{k}={v}" for k, v in h.items() if k != "round"))
    print(f"\npassed={res.passed}  repair_rounds={res.rounds}  "
          f"stop_reason={res.stop_reason or '-'}")
    if res.repair_usage:
        print(f"repair usage: {res.repair_usage}")
    if res.ask_user:
        print("\nNEEDS HUMAN INPUT:")
        for a in res.ask_user:
            print(f"  {a}")
    if res.passed:
        print(f"\nwrote {config.GENERATED_DTS}\nwrote {config.GENERATED_PATCH}\n"
              f"wrote {config.VALIDATION_REPORT}")
    else:
        print(f"\nwrote {config.FAILURE_REPORT} (no patch artifacts)")
    sys.exit(0 if res.passed else 1)


if __name__ == "__main__":
    main()
