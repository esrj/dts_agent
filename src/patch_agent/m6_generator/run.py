"""Standalone M6 Generator runner.

Usage:
    PYTHONPATH=src python3 -m patch_agent.m6_generator.run \
        [--plan PATH] [--write] [--provider anthropic|mock|...] [--model M]
        [--no-cache] [--dry-run]

Default provider/model come from llm_modules.ini [dts_patch]
(anthropic / claude-opus-4-8); ANTHROPIC_API_KEY via env or project .env.
--dry-run prints the system prompt + first LLM target's user prompt and exits
without calling any API.
"""
import argparse
import json

from .generate import run as gen_run, write_outputs
from .prompt import SYSTEM_PROMPT, build_user_prompt
from .schema import PROMPT_VERSION


def main():
    ap = argparse.ArgumentParser(description="M6 Generator — LLM patch generation")
    ap.add_argument("--plan", default=None)
    ap.add_argument("--write", action="store_true")
    ap.add_argument("--provider", default=None,
                    help="override llm_modules.ini [dts_patch] provider")
    ap.add_argument("--model", default=None)
    ap.add_argument("--no-cache", action="store_true")
    ap.add_argument("--dry-run", action="store_true",
                    help="print prompts for the LLM targets, no API call")
    args = ap.parse_args()

    if args.dry_run:
        from ..m5_locator import run as locate_run
        from dataclasses import asdict
        dp, _, _ = locate_run(plan_csv=args.plan)
        llm_targets = [t for t in dp.to_enable_or_update
                       if t.action == "generate" or any(r["extras"] for r in t.plan_rows)]
        print(f"prompt_version={PROMPT_VERSION}  llm_targets="
              f"{[t.peripheral for t in llm_targets]}\n")
        print("--- SYSTEM ---\n" + SYSTEM_PROMPT)
        if llm_targets:
            print(f"\n--- USER ({llm_targets[0].peripheral}) ---")
            print(build_user_prompt(asdict(llm_targets[0])))
        return

    provider = None
    if args.provider:
        from llm_provider.factory import get_provider
        provider = get_provider(args.provider, model=args.model)

    dp, art = gen_run(plan_csv=args.plan, provider=provider, model=args.model,
                      use_cache=not args.no_cache)

    rep = art.report
    print(f"passed={art.passed}  provider={rep.get('llm_provider')}  "
          f"model={rep.get('llm_model')}  usage={rep.get('usage_total')}")
    for r in rep.get("peripherals", []):
        extra = ""
        if r.get("cache_hit"):
            extra += " [cache]"
        if r.get("guard_errors"):
            extra += f" guard_errors={len(r['guard_errors'])}"
        if r.get("needs_info"):
            extra += f" needs_info={[n['field'] for n in r['needs_info']]}"
        print(f"  {r['peripheral']:12s} {r['action']:10s} lm={str(r.get('lm_used', False)):5s}{extra}")
    if rep.get("failed"):
        print(f"\nFAILED: {rep['failed']} — no patch written")
        for r in rep.get("peripherals", []):
            for e in r.get("guard_errors", []):
                print(f"  [{r['peripheral']}] {e}")
    if args.write and art.passed and art.patch:
        write_outputs(art)
        from .. import config
        print(f"\nwrote {config.GENERATED_DTS}\nwrote {config.GENERATED_PATCH}\n"
              f"wrote {config.STRUCTURED_EDITS}\nwrote {config.GENERATION_REPORT}")


if __name__ == "__main__":
    main()
