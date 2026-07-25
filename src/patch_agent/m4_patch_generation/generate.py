"""Stage 3 — DTS Patch Generation (plan.md §7 / ROADMAP M4).

Deterministic renderer. Consumes M2's validated_plan_ir + M3's resolutions + M1's
index and produces the managed-region patch:
  - reuse peripherals already correct in baseline  -> skipped (no-op)
  - generate peripherals                            -> new pinctrl group(s) for
    EVERY declared pinctrl state (default/idle/sleep, §7.4a) + node override

Outputs to output/generated/: generated .dts (baseline+patch merged),
generated.patch (unified diff), structured_edits.json, generation_report.json.
No LLM on the happy path.
"""
import re
import json
import difflib
from dataclasses import dataclass, field

from .. import config
from ..m1_dts_parser import build_index

# patch diff 檔頭的 kernel 樹路徑改由 config.KERNEL_DTS_PATH 供給（多板：
# 隨 init_board 重算；board.yaml 可用 kernel_dts_path 欄位覆寫）。
MARK_BEGIN = "// >>> stm-agent: managed region (DO NOT EDIT BY HAND) >>>"
MARK_END = "// <<< stm-agent: managed region <<<"


# ---- electrical property cascade (§7.5) ----------------------------------
def _role(peripheral, signal):
    return re.sub(rf"^{re.escape(peripheral)}(_P\d+)?_", "", signal)


def _sig_family(signal):
    base = re.sub(r"\d+$", "", signal.split("_")[0]).lower()
    return {"can": "fdcan", "octospim": "octospi"}.get(base, base)


def _default_electrical(family, role):
    r = role.upper()
    if family in ("i2c", "i3c") or r in ("SCL", "SDA"):
        return {"bias-pull-up": None, "drive-open-drain": None, "slew-rate": "<0>"}
    if r in ("RX", "MISO") or "RXD" in r or r in ("MDIO",):
        return {"bias-disable": None}
    return {"bias-disable": None, "drive-push-pull": None, "slew-rate": "<0>"}


def resolve_electrical(index, existing_p0, family, signal, role):
    # P1: same peripheral, same signal (existing default group)
    for gl in existing_p0:
        g = index.group(gl)
        if g:
            for e in g.entries:
                if e.signal and signal in e.signal.split("/"):
                    return dict(e.electrical), f"baseline:{gl}:{signal}"
    # P2: same family, same role suffix anywhere
    for gl, g in index.pinctrl_groups.items():
        for e in g.entries:
            if e.af is None or not e.signal:
                continue
            for s in e.signal.split("/"):
                if s.endswith("_" + role) and _sig_family(s) == family:
                    return dict(e.electrical), f"baseline-family:{gl}:{role}"
    # P3: profile default
    return _default_electrical(family, role), "profile-default"


# ---- rendering -----------------------------------------------------------
def _pinmux(pin, mode):
    return f"STM32_PINMUX('{pin[1]}', {int(pin[2:])}, {mode})"


def _render_subnode(name, pin, mode, electrical, signal):
    out = [f"\t\t{name} {{"]
    cm = f"\t/* {signal} */" if signal else ""
    out.append(f"\t\t\tpinmux = <{_pinmux(pin, mode)}>;{cm}")
    for k, v in electrical.items():
        out.append(f"\t\t\t{k};" if v is None else f"\t\t\t{k} = {v};")
    out.append("\t\t};")
    return "\n".join(out)


def _render_group(label, name, entries):
    out = [f"\t{label}: {name} {{"]
    for sub, pin, mode, elec, sig in entries:
        out.append(_render_subnode(sub, pin, mode, elec, sig))
    out.append("\t};")
    return "\n".join(out)


def _render_override(label, names, pinctrl_map, status="okay"):
    out = [f"&{label} {{"]
    if names:
        out.append('\tpinctrl-names = ' + ", ".join(f'"{n}"' for n in names) + ";")
    for i in range(len(names)):
        key = f"pinctrl-{i}"
        if key in pinctrl_map:
            out.append(f"\t{key} = <&{pinctrl_map[key]}>;")
    out.append(f'\tstatus = "{status}";')
    out.append("};")
    return "\n".join(out)


# ---- per-peripheral generation -------------------------------------------
def _signal_to_oldpin(index, group_labels):
    m = {}
    for gl in group_labels:
        g = index.group(gl)
        if not g:
            continue
        for e in g.entries:
            if e.signal:
                for s in e.signal.split("/"):
                    m.setdefault(s, e.pin)
    return m


def _pin_state(index, group_labels):
    m = {}
    for gl in group_labels:
        g = index.group(gl)
        if not g:
            continue
        for e in g.entries:
            m[e.pin] = (e.af is None, dict(e.electrical))
    return m


def _generate_one(per, ir_info, resolution, index, bindings=None):
    label = resolution.target_node.lstrip("&")
    ov = index.override(label)
    signals = ir_info["signals"]            # {signal: {pin, af}} (insertion order)
    family = ir_info.get("family")
    sig_order = list(signals)
    report = {"peripheral": per, "node": resolution.target_node, "action": "generate",
              "groups": [], "states": [], "pinmux": [], "lm_used": False, "warnings": []}

    # newly-enabled peripheral: surface (don't guess) the family's conventional
    # extra props so it doesn't silently fall back to kernel defaults (§7.6).
    if not resolution.enabled_in_baseline and bindings and family:
        binfo = bindings.get("families", {}).get(family, {})
        recs = [k for k, v in binfo.get("extra_properties", {}).items()
                if v.get("in_all_enabled_instances")
                and not k.startswith(("/", "pinctrl"))]
        if recs:
            report["warnings"].append(
                f"newly enabled: family '{family}' conventionally sets {recs}; "
                f"not emitted (board-specific value) → kernel defaults apply. "
                f"Set via board_config if a non-default is required.")

    # --- default-state entries (electrical via cascade) ---
    default_entries = []
    for i, sig in enumerate(sig_order, 1):
        s = signals[sig]
        role = _role(per, sig)
        elec, src = resolve_electrical(index, resolution.existing_pinctrl_0, family, sig, role)
        default_entries.append((f"pins{i}", s["pin"], f"AF{s['af']}", elec, sig))
        report["pinmux"].append({"signal": sig, "pin": s["pin"], "af": f"AF{s['af']}",
                                 "af_source": "validated_plan_ir", "electrical_source": src})

    groups, pinctrl_map = [], {}
    new_default = resolution.new_group_label or f"{label}_pins_generated"
    groups.append((new_default, f"{label}-generated", default_entries))
    pinctrl_map["pinctrl-0"] = new_default
    report["groups"].append(new_default)
    report["states"].append("default")

    # --- other declared states (idle/sleep…) on the NEW pins (§7.4a) ---
    names = ov.pinctrl_names if (ov and ov.pinctrl_names) else ["default"]
    if ov and len(names) > 1:
        sig2old = _signal_to_oldpin(index, ov.pinctrl.get("pinctrl-0", []))
        for i, name in enumerate(names):
            if i == 0:                       # default already done
                continue
            key = f"pinctrl-{i}"
            base_state = ov.pinctrl.get(key, [])
            pstate = _pin_state(index, base_state)
            entries = []
            for j, sig in enumerate(sig_order, 1):
                s = signals[sig]
                old_pin = sig2old.get(sig)
                is_analog, elec = pstate.get(old_pin, (True, {})) if old_pin else (True, {})
                mode = "ANALOG" if is_analog else f"AF{s['af']}"
                entries.append((f"pins{j}", s["pin"], mode, elec, sig if not is_analog else None))
            glabel = f"{label}_{name}_pins_generated"
            groups.append((glabel, f"{label}-{name}-generated", entries))
            pinctrl_map[key] = glabel
            report["groups"].append(glabel)
            report["states"].append(name)
    else:
        names = ["default"]

    override = _render_override(label, names, pinctrl_map)
    group_texts = [_render_group(*g) for g in groups]
    return group_texts, override, report


def _reuse_one(per, ir_info, resolution, index):
    """Disabled peripheral whose plan matches an existing group: enable + point at it."""
    label = resolution.target_node.lstrip("&")
    grp = resolution.reuse_group
    override = _render_override(label, ["default"], {"pinctrl-0": grp[0]})
    return [], override, {"peripheral": per, "node": resolution.target_node,
                          "action": "reuse", "groups": grp, "states": ["default"],
                          "lm_used": False, "warnings": []}


# ---- top-level -----------------------------------------------------------
@dataclass
class GeneratedArtifacts:
    managed_region: str = ""
    generated_dts: str = ""
    patch: str = ""
    structured_edits: list = field(default_factory=list)
    report: dict = field(default_factory=dict)


def generate(validated_plan_ir, stage2, index, skip_peripherals=None, to_disable=None):
    skip = set(skip_peripherals or [])
    peripherals = validated_plan_ir.get("peripherals", {})
    all_groups, all_overrides, reports, edits = [], [], [], []
    try:
        bindings = json.load(open(config.DTS_PROPERTY_BINDINGS))
    except Exception:
        bindings = None

    for per, info in peripherals.items():
        res = stage2.resolutions.get(per)
        if res is None:
            continue
        if per in skip:
            reports.append({"peripheral": per, "action": "skipped",
                            "warnings": ["has Stage 1 errors; not generated"]})
            continue
        plan_set, existing_set = set(map(tuple, res.plan_set)), set(map(tuple, res.existing_group_set))
        if res.enabled_in_baseline and plan_set == existing_set:
            reports.append({"peripheral": per, "action": "noop",
                            "warnings": ["already correct in baseline; unchanged"]})
            continue
        if res.reuse_or_generate == "reuse":
            gt, ov, rep = _reuse_one(per, info, res, index)
        else:
            gt, ov, rep = _generate_one(per, info, res, index, bindings)
        all_groups += gt
        all_overrides.append(ov)
        reports.append(rep)
        edits.append({"type": "node_override", "target": res.target_node, "action": rep["action"]})

    # §7.7 conflict resolution: disable enabled baseline owners whose pins the plan
    # took (safe set only — Stage 1 already vetted: not in plan, not boot/locked).
    for owner in sorted(to_disable or []):
        all_overrides.append(_render_override(owner, [], {}, status="disabled"))
        reports.append({"peripheral": owner, "node": f"&{owner}", "action": "disable_conflict",
                        "warnings": [f"&{owner} disabled to free pin(s) taken by the plan"]})
        edits.append({"type": "disable_node", "target": f"&{owner}"})

    art = GeneratedArtifacts()
    changed = [r for r in reports if r.get("action") in ("generate", "reuse")]
    if not all_overrides:
        art.report = {"board": config.BOARD, "peripherals": reports,
                      "note": "no changes needed (all peripherals already correct or skipped)"}
        art.generated_dts = open(config.BOARD_DTS).read()
        return art

    # --- assemble managed region ---
    summary = "; ".join(f"{r['peripheral']}={r['action']}" for r in changed)
    header = [MARK_BEGIN, "/*",
              " * Auto-generated by stm-agent (Stage 3 / M4).",
              f" * Board: {config.BOARD}",
              f" * Changes: {summary}",
              " * Multi-state: idle/sleep regenerated on new pins where applicable (§7.4a).",
              " */"]
    body = list(header)
    if all_groups:
        body.append("&pinctrl {")
        body += all_groups
        body.append("};")
        body.append("")
    body += [t + ("" if t.endswith("\n") else "") for t in _interleave(all_overrides)]
    body.append(MARK_END)
    managed = "\n".join(body)

    baseline = open(config.BOARD_DTS).read()
    base_noeol = baseline.rstrip("\n")
    generated = base_noeol + "\n\n" + managed + "\n"

    patch = "".join(difflib.unified_diff(
        baseline.splitlines(keepends=True), generated.splitlines(keepends=True),
        fromfile=f"a/{config.KERNEL_DTS_PATH}", tofile=f"b/{config.KERNEL_DTS_PATH}"))

    edits.insert(0, {"type": "append_managed_region", "file": config.BOARD_DTS.name,
                     "region_id": "stm-agent", "content": managed})
    art.managed_region = managed
    art.generated_dts = generated
    art.patch = patch
    art.structured_edits = edits
    art.report = {"board": config.BOARD, "peripherals": reports}
    return art


def _interleave(overrides):
    out = []
    for ov in overrides:
        out.append("")
        out.append(ov)
    return out[1:]  # drop leading blank


def run(plan_csv=None, write=False):
    """Run Stage 1+2+3. Returns (stage1, stage2, GeneratedArtifacts)."""
    from ..m3_target_resolution import run as stage2_run
    s1, s2 = stage2_run(plan_csv=plan_csv)
    index = build_index()
    skip = {e.peripheral for e in s1.errors if e.peripheral}
    art = generate(s1.validated_plan_ir, s2, index, skip_peripherals=skip,
                   to_disable=s1.peripherals_to_disable)
    if write:
        write_outputs(art)
    return s1, s2, art


def write_outputs(art):
    config.OUTPUT_GEN.mkdir(parents=True, exist_ok=True)
    open(config.GENERATED_DTS, "w").write(art.generated_dts)
    if art.patch:
        open(config.GENERATED_PATCH, "w").write(art.patch)
    json.dump(art.structured_edits, open(config.STRUCTURED_EDITS, "w"), indent=2, ensure_ascii=False)
    json.dump(art.report, open(config.GENERATION_REPORT, "w"), indent=2, ensure_ascii=False)