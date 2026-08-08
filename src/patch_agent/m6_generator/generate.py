"""M6 — Generator (plan.md v3 §6 / ROADMAP M6).

LLM-backed content generation over M5's diff plan, with the control flow fully
deterministic:

  noop targets            -> skipped
  reuse targets, no extras-> deterministic node_override (no LLM; pure pointer edit)
  generate / has-extras   -> LLM emits structured edits (forced tool call,
                             temperature 0, prompt versioned, context-hash cache)
  to_disable              -> deterministic `status = "disabled"` overrides

Every LLM output passes the guards (plan-consistency, state completeness, label
collisions, no invented props) before rendering; guard failures are fed back to
the model once, then the target is marked failed and NO patch is written.
Rendering / managed-region assembly / patch reuse the M4 pipeline conventions.
"""
import hashlib
import json
import re
import difflib
from dataclasses import dataclass, field

from .. import config
from ..m1_dts_parser import build_index
from ..m4_patch_generation.generate import MARK_BEGIN, MARK_END
from ..pinmux_style import get_style, assemble_group_sections
from ..supersede import apply as supersede_apply
from .schema import EMIT_TOOL, TOOL_NAME, PROMPT_VERSION
from .prompt import SYSTEM_PROMPT, build_user_prompt, build_retry_prompt


# ---- LLM call --------------------------------------------------------------
def _call_llm(provider, messages, model=None):
    """One forced-tool call. Returns (edits_dict|None, usage, raw_response)."""
    if hasattr(provider, "complete_raw"):
        resp = provider.complete_raw(
            system=SYSTEM_PROMPT, messages=messages, tools=[EMIT_TOOL],
            tool_choice={"type": "tool", "name": TOOL_NAME},
            model=model, temperature=0.0, max_tokens=16384)
        edits = resp.tool_calls[0].input if resp.tool_calls else None
        return edits, resp.usage, resp
    # fallback for providers without native tool use: JSON mode
    from llm_provider.base import Message, Role
    msgs = [Message(Role.SYSTEM, SYSTEM_PROMPT)] + [
        Message(Role.USER if m["role"] == "user" else Role.ASSISTANT,
                m["content"] if isinstance(m["content"], str) else json.dumps(m["content"]))
        for m in messages]
    resp = provider.complete(msgs, model=model, temperature=0.0, json_mode=True)
    try:
        return json.loads(resp.content), resp.usage, resp
    except json.JSONDecodeError:
        return None, resp.usage, resp


# ---- guards (§6.3 hard rules, enforced deterministically) ------------------
def _norm_refs(v):
    return v if isinstance(v, list) else [v]


def _hexnorm(s):
    s = str(s).strip().lower().replace("0x", "")
    return s.lstrip("0") or "0"


def check_edits(target, edits, all_labels):
    """Return a list of guard-violation strings (empty = pass)."""
    errs = []
    if not isinstance(edits, dict) or not isinstance(edits.get("edits"), list):
        return ["output is not an object with an `edits` array"]
    cp = target["context_pack"]
    states = cp["declared_pinctrl_states"]
    plan_rows = target["plan_rows"]
    style = get_style()                 # mode 詞彙依板的 pinmux style（stm32=AF<n>）
    plan_set = {(r["pin"], style.mode_token(r["af"])) for r in plan_rows}
    plan_pins = {r["pin"]: style.mode_token(r["af"]) for r in plan_rows}

    groups = [e for e in edits["edits"] if e.get("type") == "pinctrl_group"]
    overrides = [e for e in edits["edits"] if e.get("type") == "node_override"]
    others = [e for e in edits["edits"]
              if e.get("type") not in ("pinctrl_group", "node_override")]
    if others:
        errs.append(f"unsupported edit types: {[e.get('type') for e in others]} "
                    f"(only pinctrl_group / node_override allowed)")
    for e in edits["edits"]:
        if not e.get("source") or not e.get("reason"):
            errs.append(f"edit missing source/reason: {e.get('type')} "
                        f"{e.get('label') or e.get('target')}")

    # --- node_override ---
    if len(overrides) != 1:
        errs.append(f"expected exactly 1 node_override, got {len(overrides)}")
    ov = overrides[0] if overrides else {}
    if ov:
        if ov.get("target") != target["target_node"]:
            errs.append(f"node_override target {ov.get('target')!r} != assigned "
                        f"{target['target_node']!r}")
        if ov.get("status") != "okay":
            errs.append(f"node_override status must be \"okay\", got {ov.get('status')!r}")
        if ov.get("pinctrl_names") != states:
            errs.append(f"pinctrl_names {ov.get('pinctrl_names')} != declared states {states}")

    # --- pinctrl groups: one per state, plan-exact pins ---
    by_state = {}
    for g in groups:
        st = g.get("state")
        if st in by_state:
            errs.append(f"duplicate pinctrl_group for state {st!r}")
        by_state[st] = g
        if g.get("label") in all_labels:
            errs.append(f"group label {g.get('label')!r} collides with a baseline label")
    if sorted(by_state) != sorted(states):
        errs.append(f"group states {sorted(by_state)} != declared states {sorted(states)}")

    for st, g in by_state.items():
        got = {(sn.get("pin"), sn.get("mode")) for sn in g.get("subnodes", [])}
        pins = {p for p, _ in got}
        if st == states[0]:                       # default state: exact plan set
            if got != plan_set:
                errs.append(f"default group pinmux {sorted(got)} != plan {sorted(plan_set)}")
        else:                                     # other states: same pins, low-power or the plan AF
            if pins != set(plan_pins):
                errs.append(f"state {st!r} pins {sorted(pins)} != plan pins {sorted(plan_pins)}")
            lp = style.lowpower_token()           # stm32=ANALOG；無此概念的 style 回 None
            for pin, mode in got:
                if mode != lp and mode != plan_pins.get(pin):
                    errs.append(f"state {st!r}: {pin} mode {mode!r} is neither {lp} "
                                f"nor the plan AF {plan_pins.get(pin)!r}")

    # --- pinctrl-N must reference this edit set's groups, in state order ---
    if ov:
        pmap = ov.get("pinctrl") or {}
        own = {g.get("label") for g in groups}
        for i, st in enumerate(states):
            refs = _norm_refs(pmap.get(f"pinctrl-{i}", []))
            if not refs:
                errs.append(f"pinctrl-{i} ({st}) missing")
                continue
            bad = [r for r in refs if r not in own]
            if bad:
                errs.append(f"pinctrl-{i} references unknown group(s) {bad}")
            g = by_state.get(st)
            if g and g.get("label") not in refs:
                errs.append(f"pinctrl-{i} does not reference the {st!r} group "
                            f"{g.get('label')!r}")

    # --- no invented node props / IC identities: must have a source in context ---
    ctx_blob = json.dumps({"cp": cp, "rows": plan_rows}, ensure_ascii=False)
    extras = {}
    for r in plan_rows:
        extras.update(r.get("extras") or {})
    if ov:
        for k in (ov.get("props") or {}):
            if k not in ctx_blob:
                errs.append(f"node property {k!r} has no source in the provided context")

        # IC children need an identity source. When the plan itself binds an IC
        # (connected_ic / ic_address extras), mapping the part number to a DT
        # compatible IS the LLM's job — the Validator/bindings check it later.
        # Otherwise the compatible must come verbatim from the context
        # (fixed_connection.ics / child_devices / baseline source).
        plan_binds_ic = bool(extras.get("connected_ic") or extras.get("ic_address"))

        def walk_children(cs):
            for c in cs or []:
                compat = (c.get("props") or {}).get("compatible")
                if compat and not plan_binds_ic and compat.strip('"\\ ') not in ctx_blob:
                    errs.append(f"IC child {c.get('name')!r} compatible {compat} "
                                f"has no source in the provided context")
                walk_children(c.get("children"))
        walk_children(ov.get("children"))

    # --- plan extras (L2-L4) must actually be applied ---
    if extras.get("dt_property") and ov:
        key = extras["dt_property"].split("=", 1)[0].strip()
        if key not in (ov.get("props") or {}):
            errs.append(f"plan dt_property {key!r} not applied on the node_override")
    if (extras.get("connected_ic") or extras.get("ic_address")) and ov:
        kids = ov.get("children") or []
        if not kids:
            errs.append("plan specifies an IC binding but no child node was emitted")
        elif extras.get("ic_address"):
            want = _hexnorm(extras["ic_address"])
            regs = " ".join(str((c.get("props") or {}).get("reg", "")) for c in kids)
            if want not in [_hexnorm(x) for x in
                            re.findall(r"0x[0-9a-fA-F]+|\d+", regs)]:
                errs.append(f"no IC child has reg matching ic_address {extras['ic_address']}")
    return errs


# ---- rendering --------------------------------------------------------------
def _render_props(props, depth):
    pad = "\t" * depth
    return [f"{pad}{k};" if v is None else f"{pad}{k} = {v};"
            for k, v in (props or {}).items()]


def render_group(e):
    """LLM structured edit -> (容器 ref, 群組文字)。渲染形狀委派給該板的
    pinmux style（stm32 subnode 形＝原輸出逐字相同；K3 IOPAD 陣列形）。"""
    label = e["label"]
    name = e.get("name") or label.replace("_", "-")
    entries = [(sn["name"], sn["pin"], sn["mode"], sn.get("props") or {},
                sn.get("signal")) for sn in e.get("subnodes", [])]
    return get_style().render_group(label, name, entries)


def _render_child(c, depth):
    pad = "\t" * depth
    head = f"{c['label']}: {c['name']}" if c.get("label") else c["name"]
    out = [f"{pad}{head} {{"]
    out += _render_props(c.get("props"), depth + 1)
    for ch in c.get("children") or []:
        out += _render_child(ch, depth + 1)
    out.append(pad + "};")
    return out


def render_override(e):
    label = e["target"].lstrip("&")
    out = [f"&{label} {{"]
    names = e.get("pinctrl_names") or []
    if names:
        out.append("\tpinctrl-names = " + ", ".join(f'"{n}"' for n in names) + ";")
    pmap = e.get("pinctrl") or {}
    for i in range(len(names)):
        key = f"pinctrl-{i}"
        if key in pmap:
            out.append(f"\t{key} = " +
                       ", ".join(f"<&{r}>" for r in _norm_refs(pmap[key])) + ";")
    out += _render_props(e.get("props"), 1)
    out.append(f"\tstatus = \"{e.get('status', 'okay')}\";")
    for c in e.get("children") or []:
        out += _render_child(c, 1)
    out.append("};")
    return "\n".join(out)


def render_disable(node):
    return f"&{node.lstrip('&')} {{\n\tstatus = \"disabled\";\n}};"


def render_reuse(target):
    """Deterministic reuse: point pinctrl-0 at the existing group(s), enable."""
    res = target["resolution"]
    return render_override({
        "target": target["target_node"], "status": "okay",
        "pinctrl_names": ["default"],
        "pinctrl": {"pinctrl-0": res["reuse_group"]},
    })


# ---- cache ------------------------------------------------------------------
def _cache_key(target_dict, model):
    blob = json.dumps({"v": PROMPT_VERSION, "model": model, "t": target_dict},
                      sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(blob.encode()).hexdigest()[:24]


def _cache_get(key):
    p = config.LLM_CACHE / f"{key}.json"
    if p.exists():
        try:
            return json.load(open(p))
        except Exception:
            return None
    return None


def _cache_put(key, edits, usage, model):
    config.LLM_CACHE.mkdir(parents=True, exist_ok=True)
    json.dump({"edits": edits, "usage": usage, "model": model,
               "prompt_version": PROMPT_VERSION},
              open(config.LLM_CACHE / f"{key}.json", "w"),
              indent=2, ensure_ascii=False)


# ---- per-target -------------------------------------------------------------
def generate_target(target_dict, provider, all_labels, *, model=None,
                    use_cache=True, max_attempts=2):
    """LLM path for one target. Returns (report, group_texts, override_text).
    On guard failure after retries: report['action']='failed', texts empty."""
    per = target_dict["peripheral"]
    rep = {"peripheral": per, "node": target_dict["target_node"],
           "action": "generate", "lm_used": True, "source": "llm",
           "prompt_version": PROMPT_VERSION, "usage": {}, "warnings": [],
           "needs_info": [], "guard_errors": [], "cache_hit": False}

    key = _cache_key(target_dict, model or getattr(provider, "model_name", None))
    edits = usage = None
    if use_cache:
        hit = _cache_get(key)
        if hit:
            edits, usage, rep["cache_hit"] = hit["edits"], hit.get("usage", {}), True

    messages = [{"role": "user", "content": build_user_prompt(target_dict)}]
    attempts = 0
    resp = None
    total_usage = dict(usage or {})
    while True:
        if edits is None:
            attempts += 1
            edits, usage, resp = _call_llm(provider, messages, model=model)
            for k, v in (usage or {}).items():
                total_usage[k] = total_usage.get(k, 0) + v
            if edits is None:
                rep["guard_errors"] = ["model returned no tool call / invalid JSON"]
                if attempts >= max_attempts:
                    break
                messages.append({"role": "user",
                                 "content": build_retry_prompt(rep["guard_errors"])})
                continue
        # a needs_info-only answer is a legitimate "cannot proceed", not a guard fail
        if not edits.get("edits") and edits.get("needs_info"):
            rep["needs_info"] = edits["needs_info"]
            rep["action"] = "needs_info"
            rep["usage"] = total_usage
            return rep, [], None
        errs = check_edits(target_dict, edits, all_labels)
        if not errs:
            break
        rep["guard_errors"] = errs
        rep["cache_hit"] = False           # a cached-but-invalid result is not a hit
        edits = None
        if attempts >= max_attempts:
            break
        # feed the rejection back in the native tool protocol when possible;
        # a stale cache entry (attempts == 0) just retries fresh, no fake history
        if resp is not None and resp.raw_blocks and hasattr(provider, "complete_raw"):
            messages.append({"role": "assistant", "content": resp.raw_blocks})
            messages.append({"role": "user", "content": [
                {"type": "tool_result",
                 "tool_use_id": resp.tool_calls[0].id if resp.tool_calls else "call_1",
                 "content": "rejected by deterministic guards", "is_error": True},
                {"type": "text", "text": build_retry_prompt(errs)}]})
        elif attempts > 0:
            messages.append({"role": "user", "content": build_retry_prompt(errs)})

    rep["usage"] = total_usage
    if edits is None:
        rep["action"] = "failed"
        return rep, [], None

    if use_cache and not rep["cache_hit"]:
        _cache_put(key, edits, total_usage, model or getattr(provider, "model_name", None))
    rep["needs_info"] = edits.get("needs_info") or []

    groups = [e for e in edits["edits"] if e["type"] == "pinctrl_group"]
    ov = next(e for e in edits["edits"] if e["type"] == "node_override")
    if not groups and rep["needs_info"]:
        rep["action"] = "needs_info"
        return rep, [], None
    rep["groups"] = [g["label"] for g in groups]
    rep["states"] = [g.get("state") for g in groups]
    rep["edits"] = edits["edits"]
    rep["pinmux"] = [
        {"signal": sn.get("signal"), "pin": sn["pin"], "mode": sn["mode"],
         "state": g.get("state"), "source": g.get("source")}
        for g in groups for sn in g.get("subnodes", [])]
    return rep, [render_group(g) for g in groups], render_override(ov)


# ---- top-level ----------------------------------------------------------------
@dataclass
class GeneratedArtifacts:
    passed: bool = True
    managed_region: str = ""
    generated_dts: str = ""
    patch: str = ""
    structured_edits: list = field(default_factory=list)
    report: dict = field(default_factory=dict)


def generate(diff_plan, provider=None, index=None, *, model=None,
             use_cache=True, max_attempts=2):
    """Run the Generator over an M5 DiffPlan (object or dict)."""
    dp = diff_plan.to_dict() if hasattr(diff_plan, "to_dict") else diff_plan
    index = index or build_index()
    if provider is None:
        from llm_provider.factory import get_provider
        provider = get_provider(module="dts_patch", allow_mock=False)

    all_groups, all_overrides, reports, edits_out = [], [], [], []
    failed = []
    for t in dp["to_enable_or_update"]:
        per = t["peripheral"]
        has_extras = any(r.get("extras") for r in t["plan_rows"])
        if t["action"] == "noop":
            reports.append({"peripheral": per, "node": t["target_node"],
                            "action": "noop", "lm_used": False,
                            "warnings": ["already correct in baseline; unchanged"]})
            continue
        if t["action"] == "reuse" and not has_extras:
            ov = render_reuse(t)
            all_overrides.append(ov)
            reports.append({"peripheral": per, "node": t["target_node"],
                            "action": "reuse", "lm_used": False,
                            "source": "deterministic:reuse",
                            "groups": t["resolution"]["reuse_group"], "warnings": []})
            edits_out.append({"type": "node_override", "target": t["target_node"],
                              "action": "reuse", "source": "deterministic:reuse",
                              "reason": "plan pinmux set equals existing baseline group"})
            continue
        rep, gts, ovt = generate_target(
            t, provider, index.all_labels, model=model,
            use_cache=use_cache, max_attempts=max_attempts)
        reports.append(rep)
        if rep["action"] in ("failed", "needs_info"):
            failed.append(per)
            continue
        all_groups += gts
        all_overrides.append(ovt)
        for e in rep.get("edits", []):
            edits_out.append({**e, "peripheral": per})

    for d in dp["to_disable"]:
        all_overrides.append(render_disable(d["node"]))
        reports.append({"peripheral": d["node"].lstrip("&"), "node": d["node"],
                        "action": "disable", "lm_used": False,
                        "source": f"deterministic:{d['source']}",
                        "warnings": [d["reason"]]})
        edits_out.append({"type": "disable_override", "target": d["node"],
                          "source": f"deterministic:{d['source']}",
                          "reason": d["reason"]})

    art = GeneratedArtifacts()
    art.report = {
        "board": dp.get("board", config.BOARD),
        "generator": "m6-llm",
        "prompt_version": PROMPT_VERSION,
        "llm_model": model or getattr(provider, "model_name", None),
        "llm_provider": getattr(provider, "name", None),
        "peripherals": reports,
        "untouched": dp.get("untouched", []),
        "failed": failed,
        "usage_total": _sum_usage(reports),
    }
    if failed:
        art.passed = False
        art.report["note"] = ("generation failed for: " + ", ".join(failed) +
                              " — no patch written (§8.3: no bad patch)")
        return art
    if not all_overrides:
        art.generated_dts = open(config.BOARD_DTS).read()
        art.report["note"] = "no changes needed"
        return art

    changed = [r for r in reports if r["action"] in ("generate", "reuse", "disable")]
    summary = "; ".join(f"{r['peripheral']}={r['action']}" for r in changed)
    body = [MARK_BEGIN, "/*",
            " * Auto-generated by stm-agent (Generator / M6, LLM-backed).",
            f" * Board: {art.report['board']}",
            f" * Model: {art.report['llm_provider']}:{art.report['llm_model']} "
            f"(prompt {PROMPT_VERSION})",
            f" * Changes: {summary}",
            " */"]
    if all_groups:
        # all_groups = [(容器 ref, 群組文字)]；stm32 單容器時與舊版逐字相同
        body += assemble_group_sections(all_groups)
    for i, ov in enumerate(all_overrides):
        if i:
            body.append("")
        body.append(ov)
    body.append(MARK_END)
    managed = "\n".join(body)

    baseline = open(config.BOARD_DTS).read()
    # 換腳周邊（generate/reuse）的官方 pinctrl 行註解掉（supersede 政策）；
    # m7 layer-4 以同一 supersede.apply 對 baseline 重算 expected prefix。
    sup_targets = [t["target_node"] for t in dp["to_enable_or_update"]
                   if t["action"] in ("generate", "reuse")]
    base_sup, sup_changed = supersede_apply(baseline, sup_targets)
    generated = base_sup.rstrip("\n") + "\n\n" + managed + "\n"
    art.managed_region = managed
    art.generated_dts = generated
    art.patch = "".join(difflib.unified_diff(
        baseline.splitlines(keepends=True), generated.splitlines(keepends=True),
        fromfile=f"a/{config.KERNEL_DTS_PATH}", tofile=f"b/{config.KERNEL_DTS_PATH}"))
    edits_out.insert(0, {"type": "append_managed_region",
                         "file": config.BOARD_DTS.name,
                         "region_id": "stm-agent", "content": managed})
    for lb in sup_changed:
        edits_out.append({"type": "comment_out_pinctrl", "target": f"&{lb}",
                          "source": "deterministic:supersede",
                          "reason": "official pinctrl superseded by the managed "
                                    "region (pins changed by plan)"})
    art.structured_edits = edits_out
    return art


def _sum_usage(reports):
    tot = {}
    for r in reports:
        for k, v in (r.get("usage") or {}).items():
            tot[k] = tot.get(k, 0) + v
    return tot


def run(plan_csv=None, provider=None, model=None, write=False,
        use_cache=True, max_attempts=2):
    """Locator (M5) -> Generator (M6). Returns (DiffPlan, GeneratedArtifacts)."""
    from ..m5_locator import run as locate_run
    dp, _, _ = locate_run(plan_csv=plan_csv)
    if not dp.passed or dp.has_blocking_conflict:
        art = GeneratedArtifacts(passed=False)
        art.report = {"board": dp.board, "note": "Locator blocked generation",
                      "errors": [vars(e) for e in dp.errors]}
        return dp, art
    art = generate(dp, provider=provider, model=model,
                   use_cache=use_cache, max_attempts=max_attempts)
    if write and art.passed:
        write_outputs(art)
    return dp, art


def write_outputs(art):
    config.OUTPUT_GEN.mkdir(parents=True, exist_ok=True)
    open(config.GENERATED_DTS, "w").write(art.generated_dts)
    if art.patch:
        open(config.GENERATED_PATCH, "w").write(art.patch)
    else:
        # 本輪沒有 patch（no-changes）→ 清掉上一輪殘留的 patch 檔，
        # 避免舊產物被誤當成這份 plan 的結果
        config.GENERATED_PATCH.unlink(missing_ok=True)
    json.dump(art.structured_edits, open(config.STRUCTURED_EDITS, "w"),
              indent=2, ensure_ascii=False)
    json.dump(art.report, open(config.GENERATION_REPORT, "w"),
              indent=2, ensure_ascii=False)
