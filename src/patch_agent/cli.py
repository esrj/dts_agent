"""M9 — single CLI entry for the whole v3 pipeline (plan.md §1 / ROADMAP M9).

    python3 -m patch_agent [run] --plan output/plan/plan.csv
    python3 -m patch_agent locate   [--plan P]        # Locator only (no LLM)
    python3 -m patch_agent dry-run  [--plan P]        # show LLM prompts, no API
    python3 -m patch_agent validate [--from-output]   # Validator only

`run` (default) drives Locator -> Generator -> Validator -> Repairer (≤3
rounds) and writes the full artifact set to output/generated/:
    stm32mp257f-ev1.generated.dts, generated.patch, structured_edits.json,
    generation_report.json, validation_report.json, diff_plan.json,
    locator_report.json          (failure_report.json only when it fails)

Exit codes:  0 = pass · 1 = failed (retry_exhausted / unrepairable)
             2 = needs human input (locator_blocked / needs_info / boot_conflict)
"""
import argparse
import json
import sys

from . import config

ASK_USER_REASONS = {"locator_blocked", "needs_info", "boot_conflict"}


def exit_code(res):
    if res.passed:
        return 0
    return 2 if res.stop_reason in ASK_USER_REASONS else 1


# ---- run --------------------------------------------------------------------
def _write_locator_reports(res):
    config.OUTPUT_GEN.mkdir(parents=True, exist_ok=True)
    json.dump(res.dp.to_dict(), open(config.DIFF_PLAN, "w"),
              indent=2, ensure_ascii=False)
    if res.stage1 is not None and res.stage2 is not None:
        report = {"board": res.dp.board, "passed": res.dp.passed,
                  "stage1": res.stage1.to_dict(), "stage2": res.stage2.to_dict()}
        json.dump(report, open(config.LOCATOR_REPORT, "w"),
                  indent=2, ensure_ascii=False, default=str)


def summarize(res):
    lines = []
    dp = res.dp
    if res.art is not None and res.art.report.get("peripherals"):
        lines.append("peripherals:")
        for r in res.art.report["peripherals"]:
            extra = " [cache]" if r.get("cache_hit") else ""
            lines.append(f"  {r['peripheral']:12s} {r['action']:10s} "
                         f"lm={str(r.get('lm_used', False)):5s}{extra}")
    if dp is not None:
        if dp.to_disable:
            lines.append(f"disabled ({len(dp.to_disable)}): "
                         + ", ".join(d["node"] for d in dp.to_disable))
        lines.append(f"untouched: {len(dp.untouched)} nodes (reasons in diff_plan.json)")
    usage = dict((res.art.report.get("usage_total") or {})) if res.art else {}
    for k, v in (res.repair_usage or {}).items():
        usage[k] = usage.get(k, 0) + v
    if usage:
        lines.append(f"llm usage: {usage}")
    if res.validation is not None:
        checks = " ".join(f"{k}={v}" for k, v in res.validation.checks.items())
        lines.append(f"validation: {checks}")
    lines.append(f"result: passed={res.passed}  repair_rounds={res.rounds}"
                 + (f"  stop={res.stop_reason}" if res.stop_reason else ""))
    return "\n".join(lines)


def cmd_run(args, provider=None, repair_provider=None):
    from .m8_repairer import run as m8_run
    if provider is None and args.provider:
        from llm_provider.factory import get_provider
        provider = get_provider(args.provider, model=args.model)
    res = m8_run(plan_csv=args.plan, provider=provider,
                 repair_provider=repair_provider, model=args.model,
                 max_rounds=args.max_rounds, run_compile=not args.no_compile,
                 use_cache=not args.no_cache, write=True)
    _write_locator_reports(res)

    print(summarize(res))
    if res.ask_user:
        print("\nNEEDS HUMAN INPUT:")
        for a in res.ask_user:
            print(f"  {a}")
    if res.passed:
        if getattr(res, "no_changes", False):
            print("\nno patch needed — baseline already satisfies the plan")
            print(f"wrote {config.GENERATED_DTS}  (== baseline)")
        else:
            print(f"\nwrote {config.GENERATED_DTS}")
            print(f"wrote {config.GENERATED_PATCH}")
        print(f"wrote {config.STRUCTURED_EDITS}")
        print(f"wrote {config.GENERATION_REPORT}")
        print(f"wrote {config.VALIDATION_REPORT}")
    else:
        print(f"\nwrote {config.FAILURE_REPORT} (no patch artifacts)")
    return exit_code(res)


# ---- locate -------------------------------------------------------------------
def cmd_locate(args):
    from .m5_locator import run as locate_run
    dp, s1, s2 = locate_run(plan_csv=args.plan)
    config.OUTPUT_GEN.mkdir(parents=True, exist_ok=True)
    json.dump(dp.to_dict(), open(config.DIFF_PLAN, "w"), indent=2, ensure_ascii=False)
    json.dump({"board": dp.board, "passed": dp.passed,
               "stage1": s1.to_dict(), "stage2": s2.to_dict()},
              open(config.LOCATOR_REPORT, "w"), indent=2, ensure_ascii=False,
              default=str)
    print(f"passed={dp.passed}  blocking_conflict={dp.has_blocking_conflict}")
    for t in dp.to_enable_or_update:
        print(f"  {t.peripheral:12s} {t.action:10s} {t.target_node}")
    for d in dp.to_disable:
        print(f"  {d['node']:12s} disable    [{d['source']}]")
    print(f"untouched: {len(dp.untouched)}")
    for e in dp.errors:
        print(f"  ERROR [{e.error_type}] {e.message}")
    print(f"\nwrote {config.DIFF_PLAN}\nwrote {config.LOCATOR_REPORT}")
    return 0 if dp.passed and not dp.has_blocking_conflict else 2


# ---- dry-run --------------------------------------------------------------------
def cmd_dry_run(args):
    from dataclasses import asdict
    from .m5_locator import run as locate_run
    from .m6_generator.prompt import SYSTEM_PROMPT, build_user_prompt
    from .m6_generator.schema import PROMPT_VERSION
    dp, _, _ = locate_run(plan_csv=args.plan)
    llm_targets = [t for t in dp.to_enable_or_update
                   if t.action == "generate" or any(r["extras"] for r in t.plan_rows)]
    print(f"prompt_version={PROMPT_VERSION}  "
          f"llm_targets={[t.peripheral for t in llm_targets]}")
    print(f"deterministic: "
          f"{[t.peripheral for t in dp.to_enable_or_update if t not in llm_targets]}"
          f" + {len(dp.to_disable)} disable(s)\n")
    print("--- SYSTEM ---\n" + SYSTEM_PROMPT)
    if llm_targets:
        print(f"\n--- USER ({llm_targets[0].peripheral}) ---")
        print(build_user_prompt(asdict(llm_targets[0])))
    return 0


# ---- validate --------------------------------------------------------------------
def cmd_validate(args):
    from .m7_validator import validate, write_report
    from .m7_validator.run import _print
    if args.from_output:
        try:
            dts = open(config.GENERATED_DTS).read()
            dp = json.load(open(config.DIFF_PLAN))
            edits = json.load(open(config.STRUCTURED_EDITS))
        except FileNotFoundError as e:
            print(f"missing artifact: {e.filename} — run `python3 -m patch_agent run` first")
            return 2
        rep = validate(dts, dp, edits, run_compile=not args.no_compile)
        write_report(rep)
    else:
        from .m7_validator import run as m7_run
        _, _, rep = m7_run(plan_csv=args.plan, run_compile=not args.no_compile)
    _print(rep)
    return 0 if rep.passed else 1


# ---- parser -----------------------------------------------------------------------
def build_parser():
    ap = argparse.ArgumentParser(
        prog="patch_agent",
        description="DTS Patch Agent — plan.csv in, validated kernel DT patch out")
    sub = ap.add_subparsers(dest="cmd")

    def common(p, llm=False):
        p.add_argument("--plan", default=None,
                       help="plan.csv path (default: output/plan/plan.csv)")
        if llm:
            p.add_argument("--model", default=None)
            p.add_argument("--provider", default=None,
                           help="override llm_modules.ini [dts_patch] provider")
            p.add_argument("--no-cache", action="store_true")
        p.add_argument("--no-compile", action="store_true",
                       help="skip the cpp/dtc compile layer")

    p_run = sub.add_parser("run", help="full pipeline with repair loop (default)")
    common(p_run, llm=True)
    p_run.add_argument("--max-rounds", type=int, default=3)

    common(sub.add_parser("locate", help="Locator only: diff plan, no LLM"))
    common(sub.add_parser("dry-run", help="print the LLM prompts, no API call"))
    p_val = sub.add_parser("validate", help="Validator only")
    common(p_val)
    p_val.add_argument("--from-output", action="store_true",
                       help="validate the artifacts already in output/generated/")
    return ap


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv or argv[0].startswith("-"):
        argv.insert(0, "run")                          # default subcommand
    args = build_parser().parse_args(argv)
    cmd = {"run": cmd_run, "locate": cmd_locate,
           "dry-run": cmd_dry_run, "validate": cmd_validate}[args.cmd]
    return cmd(args)


if __name__ == "__main__":
    sys.exit(main())
