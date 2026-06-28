"""M8 — Repairer (plan.md v3 §8 / ROADMAP M8).

Error-feedback repair loop around the v3 pipeline:

    Locator -> Generator -> Validator
                   ^            |
                   |        classify errors
              ReplayProvider    |
                   ^            v
                   +---- LLM repairs the affected targets' EDITS

Repairs happen at the structured-edits level, never on rendered text: the LLM
fixes a target's edits, then the artifact is re-rendered by running M6 again
with a ReplayProvider that feeds the stored edits back through the normal
forced-tool protocol — so the M6 guards re-vet every repair and the assembly
logic exists in exactly one place. Re-validation cap: 3 rounds (plan §8.3).

Stop-and-ask (never auto-repaired):
    REQUIRE_CONFLICT / PROTECTED_GPIO_CONFLICT  -> boot/board safety, ask user
    needs_info                                  -> required data has no source
    Locator blocked (plan invalid / blocking conflict)

On success: writes the M6 outputs + validation_report.json.
On failure: writes failure_report.json and NO patch artifacts.
"""
import json
import re
from dataclasses import dataclass, field

from .. import config
from ..m6_generator import generate as m6_generate, write_outputs
from ..m6_generator.generate import _call_llm, check_edits
from ..m6_generator.schema import TOOL_NAME
from ..m7_validator import validate as m7_validate, write_report
from ..m7_validator.validate import (
    ValidationReport, REQUIRE_CONFLICT, PROTECTED_GPIO_CONFLICT)
from .prompt import REPAIR_SYSTEM_PROMPT, build_repair_prompt

STOP_TYPES = {REQUIRE_CONFLICT, PROTECTED_GPIO_CONFLICT}
GUARD_REJECTED = "GUARD_REJECTED"

_TARGET_JSON_RE = re.compile(r"```json\n(.*?)\n```", re.S)


# ---- replay provider: re-render stored edits through the M6 pipeline --------
class ReplayProvider:
    """Feeds stored per-target edits back through M6's forced-tool protocol,
    so re-rendering a repaired artifact reuses the M6 guards + assembly."""
    name = "replay"
    model_name = "replay"

    def __init__(self, edits_by_target):
        self.edits_by_target = edits_by_target

    def complete_raw(self, *, system, messages, tools=None, tool_choice=None,
                     model=None, temperature=0.0, max_tokens=0):
        from llm_provider.base import LLMResponse, ToolCall
        m = _TARGET_JSON_RE.search(messages[0]["content"])
        per = json.loads(m.group(1))["peripheral"] if m else None
        edits = self.edits_by_target.get(per)
        calls = [ToolCall(id="replay", name=TOOL_NAME, input=edits)] if edits else []
        return LLMResponse(content="", provider=self.name, model=self.model_name,
                           usage={}, tool_calls=calls, stop_reason="tool_use",
                           raw_blocks=[{"type": "tool_use", "id": "replay",
                                        "name": TOOL_NAME, "input": edits}] if edits else [])


# ---- error classification (plan.md §8.2) --------------------------------------
@dataclass
class Classification:
    stop: list = field(default_factory=list)          # REQUIRE/PROTECTED errors
    by_target: dict = field(default_factory=dict)     # peripheral -> [error dicts]
    unattributed: list = field(default_factory=list)  # repairable but no target


def _norm_error(e):
    return e if isinstance(e, dict) else e.to_dict()


def _attribute_by_line(art_text, line, per_of_node, edits_by_target):
    """Walk upward from an error line (e.g. from dtc) to the enclosing
    top-level `&node {` override or `label:` pinctrl group, and map it to a
    plan peripheral."""
    if not art_text or not line:
        return None
    for ln in reversed(art_text.split("\n")[:line]):
        m = re.match(r"^&(\w+)\s*\{", ln)
        if m:
            return per_of_node.get(m.group(1))
        m = re.match(r"^\t(\w+):", ln)                 # group label under &pinctrl
        if m:
            lbl = m.group(1)
            for p, ed in edits_by_target.items():
                if lbl in json.dumps(ed):
                    return p
            return None
    return None


def classify(errors, dp, edits_by_target, art_text=None):
    """Split validator/guard errors into stop / per-target repair / unattributed."""
    cls = Classification()
    plan_pers = {t["peripheral"] for t in dp["to_enable_or_update"]}
    per_of_node = {t["target_node"].lstrip("&"): t["peripheral"]
                   for t in dp["to_enable_or_update"]}
    for e in map(_norm_error, errors):
        et = e.get("error_type")
        if et in STOP_TYPES:
            cls.stop.append(e)
            continue
        per = e.get("peripheral")
        if per in per_of_node:                        # error tagged with node label
            per = per_of_node[per]
        if per not in plan_pers:
            per = None
        if per is None:
            # attribute by quoted identifiers appearing in exactly one target's edits
            tokens = re.findall(r"'([\w@-]+)'", e.get("message", ""))
            hits = {p for p, ed in edits_by_target.items()
                    if any(tok in json.dumps(ed) for tok in tokens)}
            per = hits.pop() if len(hits) == 1 else None
        if per is None:                               # e.g. dtc errors carry only a line
            per = _attribute_by_line(art_text, e.get("line"), per_of_node,
                                     edits_by_target)
        if per is None:
            cls.unattributed.append(e)
        else:
            cls.by_target.setdefault(per, []).append(e)
    return cls


# ---- one LLM repair --------------------------------------------------------------
def repair_target(target_dict, current_edits, errors, provider, all_labels,
                  *, model=None, max_attempts=2):
    """Ask the LLM to fix one target's edits. Returns (edits|None, report)."""
    rep = {"peripheral": target_dict["peripheral"], "lm_used": True,
           "usage": {}, "guard_errors": []}
    messages = [{"role": "user",
                 "content": build_repair_prompt(target_dict, current_edits, errors)}]
    attempts = 0
    total = {}
    while attempts < max_attempts:
        attempts += 1
        edits, usage, resp = _call_llm_with_system(
            provider, messages, REPAIR_SYSTEM_PROMPT, model=model)
        for k, v in (usage or {}).items():
            total[k] = total.get(k, 0) + v
        if edits is None:
            rep["guard_errors"] = ["model returned no tool call"]
            break
        if not edits.get("edits") and edits.get("needs_info"):
            rep["needs_info"] = edits["needs_info"]
            rep["usage"] = total
            return None, rep
        errs = check_edits(target_dict, edits, all_labels)
        if not errs:
            rep["usage"] = total
            return edits, rep
        rep["guard_errors"] = errs
        if resp is not None and getattr(resp, "raw_blocks", None):
            messages.append({"role": "assistant", "content": resp.raw_blocks})
            messages.append({"role": "user", "content": [
                {"type": "tool_result",
                 "tool_use_id": resp.tool_calls[0].id if resp.tool_calls else "r1",
                 "content": "rejected by deterministic guards", "is_error": True},
                {"type": "text", "text": "Guard violations:\n- " + "\n- ".join(errs)}]})
        else:
            messages.append({"role": "user",
                             "content": "Guard violations:\n- " + "\n- ".join(errs)})
    rep["usage"] = total
    return None, rep


def _call_llm_with_system(provider, messages, system, model=None):
    """M6's _call_llm but with the repair system prompt."""
    if hasattr(provider, "complete_raw"):
        from ..m6_generator.schema import EMIT_TOOL
        resp = provider.complete_raw(
            system=system, messages=messages, tools=[EMIT_TOOL],
            tool_choice={"type": "tool", "name": TOOL_NAME},
            model=model, temperature=0.0, max_tokens=16384)
        edits = resp.tool_calls[0].input if resp.tool_calls else None
        return edits, resp.usage, resp
    return _call_llm(provider, messages, model=model)


# ---- the loop -----------------------------------------------------------------------
@dataclass
class RepairResult:
    passed: bool = False
    rounds: int = 0                       # repair rounds actually executed
    stop_reason: str = None               # None | locator_blocked | needs_info |
                                          # boot_conflict | unrepairable | retry_exhausted
    ask_user: list = field(default_factory=list)
    dp: object = None
    art: object = None
    validation: object = None             # last ValidationReport (or None)
    history: list = field(default_factory=list)
    repair_usage: dict = field(default_factory=dict)
    stage1: object = None                 # M2 Stage1Result (for locator_report)
    stage2: object = None                 # M3 Stage2Result

    def failure_report(self):
        return {
            "passed": self.passed,
            "stop_reason": self.stop_reason,
            "rounds": self.rounds,
            "ask_user": self.ask_user,
            "history": self.history,
            "last_validation": self.validation.to_dict() if self.validation else None,
            "note": "no patch artifacts were written (§8.3: no bad patch)",
        }


def _extract_edits(art):
    """peripheral -> {'edits': [...]} for every LLM-generated target."""
    out = {}
    for r in art.report.get("peripherals", []):
        if r.get("edits"):
            out[r["peripheral"]] = {"edits": r["edits"]}
    return out


def _generation_errors(art):
    """Turn M6 per-target failures into classifier-consumable error dicts."""
    errors, needs_info = [], []
    for r in art.report.get("peripherals", []):
        if r.get("action") == "needs_info":
            needs_info.append({"peripheral": r["peripheral"],
                               "missing": r.get("needs_info", [])})
        elif r.get("action") == "failed":
            for g in r.get("guard_errors", ["generation failed"]):
                errors.append({"error_type": GUARD_REJECTED, "message": g,
                               "peripheral": r["peripheral"]})
    return errors, needs_info


def _sum_usage(tot, part):
    for k, v in (part or {}).items():
        tot[k] = tot.get(k, 0) + v


def run(plan_csv=None, provider=None, repair_provider=None, model=None,
        max_rounds=3, write=True, run_compile=True, validator=None,
        use_cache=True):
    """Full pipeline with the repair loop. Returns RepairResult.

    provider        : generation LLM (default: llm_modules.ini [dts_patch])
    repair_provider : repair LLM (default: same as provider)
    validator       : injectable for tests; default M7 validate
    """
    from ..m5_locator import run as locate_run
    from ..m1_dts_parser import build_index

    res = RepairResult()
    dp_obj, s1, s2 = locate_run(plan_csv=plan_csv)
    dp = dp_obj.to_dict()
    res.dp = dp_obj
    res.stage1, res.stage2 = s1, s2
    if not dp_obj.passed or dp_obj.has_blocking_conflict:
        res.stop_reason = "locator_blocked"
        res.ask_user = [_norm_error(e) if hasattr(e, "to_dict") else
                        {"error_type": getattr(e, "error_type", "ERROR"),
                         "message": getattr(e, "message", str(e))}
                        for e in dp_obj.errors]
        if write:
            _write_failure(res)
        return res

    if provider is None:
        from llm_provider.factory import get_provider
        provider = get_provider(module="dts_patch", allow_mock=False)
    repair_provider = repair_provider or provider
    validator = validator or (lambda art: m7_validate(
        art.generated_dts, dp, art.structured_edits, run_compile=run_compile))
    index = build_index()

    art = m6_generate(dp, provider=provider, index=index, model=model,
                      use_cache=use_cache)
    res.art = art
    edits_by_target = _extract_edits(art)

    for rnd in range(0, max_rounds + 1):
        # ---- validate this round's artifact (or collect generation failures) --
        if art.passed:
            rep = validator(art)
            res.validation = rep
            if rep.passed:
                res.passed = True
                res.rounds = rnd
                res.history.append({"round": rnd, "validation": "pass"})
                if write:
                    write_outputs(art)
                    write_report(rep)
                    _clear_failure()
                return res
            errors = rep.errors
            needs_info = []
        else:
            errors, needs_info = _generation_errors(art)
            res.validation = None

        if needs_info:
            res.stop_reason = "needs_info"
            res.ask_user = needs_info
            res.history.append({"round": rnd, "validation": "generation_needs_info"})
            break

        cls = classify(errors, dp, edits_by_target,
                       art_text=art.generated_dts if art.passed else None)
        res.history.append({
            "round": rnd, "validation": "fail",
            "errors": [_norm_error(e).get("error_type") for e in errors],
            "stop": [e["error_type"] for e in cls.stop],
            "repair_targets": sorted(cls.by_target),
            "unattributed": len(cls.unattributed)})

        if cls.stop:
            res.stop_reason = "boot_conflict"
            res.ask_user = cls.stop
            break
        if not cls.by_target:
            res.stop_reason = "unrepairable"
            res.ask_user = cls.unattributed
            break
        if rnd == max_rounds:
            res.stop_reason = "retry_exhausted"
            break

        # ---- repair the affected targets' edits -------------------------------
        res.rounds = rnd + 1
        targets = {t["peripheral"]: t for t in dp["to_enable_or_update"]}
        for per, errs in sorted(cls.by_target.items()):
            new_edits, rrep = repair_target(
                targets[per], edits_by_target.get(per), errs,
                repair_provider, index.all_labels, model=model)
            _sum_usage(res.repair_usage, rrep.get("usage"))
            if rrep.get("needs_info"):
                res.stop_reason = "needs_info"
                res.ask_user = [{"peripheral": per, "missing": rrep["needs_info"]}]
                res.history.append({"round": rnd + 1, "repair": per,
                                    "result": "needs_info"})
                break
            if new_edits is not None:
                edits_by_target[per] = new_edits
            res.history.append({"round": rnd + 1, "repair": per,
                                "result": "edits_updated" if new_edits else "repair_failed",
                                "guard_errors": rrep.get("guard_errors", [])})
        if res.stop_reason:
            break

        # ---- re-render deterministically from the (repaired) edits ------------
        art = m6_generate(dp, provider=ReplayProvider(edits_by_target),
                          index=index, use_cache=False)
        res.art = art

    if not res.stop_reason:
        res.stop_reason = "retry_exhausted"
    if write:
        _write_failure(res)
    return res


def _write_failure(res):
    config.OUTPUT_GEN.mkdir(parents=True, exist_ok=True)
    json.dump(res.failure_report(), open(config.FAILURE_REPORT, "w"),
              indent=2, ensure_ascii=False)
    if res.validation is not None:
        write_report(res.validation)


def _clear_failure():
    try:
        config.FAILURE_REPORT.unlink()
    except FileNotFoundError:
        pass
