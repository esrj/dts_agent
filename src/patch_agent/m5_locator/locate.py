"""M5 — Locator (plan.md v3 §5 / ROADMAP M5).

Deterministic front half of the v3 pipeline. Wraps M2 (plan validation) + M3
(target resolution) and adds what the LLM Generator needs:

  1. flexible plan.csv parsing — L1 columns (peripheral,signal,pin,af) are
     validated deterministically; any extra columns (L2-L5: connected_ic,
     ic_address, dt_property, …) are carried through verbatim per row.
  2. the diff plan — plan.csv is the complete intent:
       to_enable_or_update : every peripheral in the plan (+ context pack)
       to_disable          : enabled in baseline ∧ manageable ∧ absent from plan
                             (plus M2's pin-conflict owners)
       untouched           : everything else enabled, each with a reason
  3. a per-target context pack: baseline node source, declared pinctrl states,
     existing groups with electrical props, fixed-connection / board-config /
     property-binding excerpts. No LLM here; the pack is the LLM's input later.
"""
import csv
import json
import re
from dataclasses import dataclass, field, asdict

from .. import config
from ..m1_dts_parser import build_index
from ..m1_dts_parser.index import family_of
from ..m2_validation_harness.harness import (
    validate_plan, load_plan, _boot_required_nodes, _locked_nodes)
from ..m3_target_resolution import resolve

L1_COLS = ("peripheral", "signal", "pin", "af")


# ---- flexible plan loading (§3.2) -----------------------------------------
def load_plan_flexible(path):
    """Parse a variable-schema plan.csv.

    Returns (rows, extra_columns): each row has the four L1 keys plus an
    `extras` dict holding every non-empty non-L1 column verbatim.
    """
    rows, extra_cols = [], []
    with open(path, encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        extra_cols = [c for c in (reader.fieldnames or []) if c and c not in L1_COLS]
        for r in reader:
            if not (r.get("peripheral") or "").strip():
                continue
            rows.append({
                "peripheral": r["peripheral"].strip(),
                "signal": r["signal"].strip(),
                "pin": r["pin"].strip(),
                "af": int(r["af"]),
                "extras": {c: r[c].strip() for c in extra_cols
                           if (r.get(c) or "").strip()},
            })
    return rows, extra_cols


# ---- result types ----------------------------------------------------------
@dataclass
class Target:
    peripheral: str
    action: str                 # generate | reuse | noop
    target_node: str
    family: str
    enabled_in_baseline: bool
    plan_rows: list = field(default_factory=list)     # L1 + extras, verbatim
    resolution: dict = field(default_factory=dict)    # M3 TargetResolution asdict
    context_pack: dict = field(default_factory=dict)


@dataclass
class DiffPlan:
    passed: bool
    board: str = config.BOARD
    errors: list = field(default_factory=list)
    warnings: list = field(default_factory=list)
    to_enable_or_update: list = field(default_factory=list)   # [Target]
    to_disable: list = field(default_factory=list)            # [{node, reason, source}]
    untouched: list = field(default_factory=list)             # [{node, reason}]
    has_blocking_conflict: bool = False
    extra_columns: list = field(default_factory=list)

    def to_dict(self):
        d = asdict(self)
        d["errors"] = [asdict(e) if hasattr(e, "__dataclass_fields__") else e
                       for e in self.errors]
        return d


# ---- context pack ----------------------------------------------------------
def _node_source(index, label):
    """Raw text of the &label override in the board file (brace-matched from
    the recorded start line), for the LLM to see the node exactly as written."""
    ov = index.override(label)
    if not ov:
        return None
    text = open(config.DTS_DIR / ov.file).read()
    lines = text.split("\n")
    start = ov.line - 1
    depth, out = 0, []
    for ln in lines[start:]:
        out.append(ln)
        depth += ln.count("{") - ln.count("}")
        if depth == 0 and "{" in "".join(out):
            break
    return "\n".join(out)


def _group_detail(index, labels):
    out = []
    for gl in labels:
        g = index.group(gl)
        if not g:
            continue
        out.append({"label": gl, "entries": [
            {"pin": e.pin, "mode": e.mode, "af": e.af, "signal": e.signal,
             "electrical": e.electrical} for e in g.entries]})
    return out


def _context_pack(per, info, res, index, fixed, board_cfg, bindings):
    label = res.target_node.lstrip("&")
    ov = index.override(label)
    states = ov.pinctrl_names if (ov and ov.pinctrl_names) else ["default"]
    existing = {}
    if ov:
        for key, gls in ov.pinctrl.items():
            existing[key] = _group_detail(index, gls)
    fam = info.get("family") or family_of(label)
    bcfg = (board_cfg or {}).get("peripherals", {}).get(label, {})
    return {
        "target_node": res.target_node,
        "target_file": res.target_file,
        "baseline_node_source": _node_source(index, label),
        "declared_pinctrl_states": states,
        "existing_pinctrl": existing,
        "fixed_connection": (fixed or {}).get("connections", {}).get(label),
        "board_knobs": bcfg.get("board_knobs", {}),
        "child_devices": bcfg.get("child_devices", []),
        "deleted_properties": bcfg.get("deleted_properties", []),
        "property_bindings": (bindings or {}).get("families", {}).get(fam),
    }


# ---- core -------------------------------------------------------------------
def locate(plan_rows, extra_columns, *, index, af_table, profiles, gpio_pins,
           require, signal_to_pin, alias, fixed=None, board_cfg=None,
           bindings=None, baseline_rows=None):
    """Run the deterministic Locator over already-parsed flexible plan rows."""
    alias_map = alias.get("aliases", {})
    boot_nodes = _boot_required_nodes(require)
    locked = require.get("board_pin_locked", {}).get("peripherals", [])
    unsafe = boot_nodes | _locked_nodes(locked, alias_map)

    # -- 1. L1 validation (M2) — extras never affect deterministic checks --
    l1_rows = [{k: r[k] for k in L1_COLS} for r in plan_rows]
    owner = _baseline_owner_map(index)
    s1 = validate_plan(l1_rows, af_table=af_table, profiles=profiles,
                       gpio_pins=gpio_pins, require=require,
                       signal_to_pin=signal_to_pin, alias=alias,
                       baseline_rows=baseline_rows, baseline_owner=owner)

    # -- 2. target resolution (M3) --
    s2 = resolve(s1.validated_plan_ir, index, boot_required_nodes=boot_nodes)

    dp = DiffPlan(passed=s1.passed, errors=list(s1.errors),
                  warnings=list(s1.warnings),
                  has_blocking_conflict=s2.has_blocking_conflict,
                  extra_columns=list(extra_columns))

    # -- 3. to_enable_or_update + context packs --
    rows_by_per = {}
    for r in plan_rows:
        rows_by_per.setdefault(r["peripheral"], []).append(r)
    err_pers = {e.peripheral for e in s1.errors if e.peripheral}

    plan_nodes = set()
    for per, info in s1.validated_plan_ir.get("peripherals", {}).items():
        res = s2.resolutions.get(per)
        if res is None:
            continue
        label = res.target_node.lstrip("&")
        plan_nodes.add(label)
        plan_set = set(map(tuple, res.plan_set))
        existing_set = set(map(tuple, res.existing_group_set))
        has_extras = any(r["extras"] for r in rows_by_per.get(per, []))
        if res.enabled_in_baseline and plan_set == existing_set and not has_extras:
            action = "noop"
        else:
            action = res.reuse_or_generate
        dp.to_enable_or_update.append(Target(
            peripheral=per, action=action,
            target_node=res.target_node,
            family=info.get("family") or family_of(label),
            enabled_in_baseline=res.enabled_in_baseline,
            plan_rows=rows_by_per.get(per, []),
            resolution=asdict(res),
            context_pack=_context_pack(per, info, res, index, fixed,
                                       board_cfg, bindings),
        ))
        if per in err_pers:
            dp.warnings.append(f"{per}: has validation errors; Generator must "
                               f"not emit it until they are resolved")

    # -- 4. to_disable: absent-from-plan manageable + M2 pin-conflict owners --
    manageable = {}
    if fixed:
        manageable = {lbl: c.get("manageable", False)
                      for lbl, c in fixed.get("connections", {}).items()}
    disabled, seen = [], set()
    for label, ov in sorted(index.enabled_overrides().items()):
        fam = family_of(label)
        if fam is None or index.group(label) or label in plan_nodes:
            continue
        is_manageable = manageable.get(label, label not in unsafe)
        if is_manageable:
            disabled.append({"node": f"&{label}", "family": fam,
                             "reason": "enabled in baseline, absent from plan, manageable",
                             "source": "absent_from_plan"})
            seen.add(label)
        else:
            why = "boot-required" if label in boot_nodes else "board-locked"
            dp.untouched.append({"node": f"&{label}",
                                 "reason": f"absent from plan but {why}; kept as-is"})
    for label in s1.peripherals_to_disable:          # conflict-driven (subset)
        if label not in seen and label not in plan_nodes:
            disabled.append({"node": f"&{label}", "family": family_of(label),
                             "reason": "enabled baseline owner of a pin the plan takes",
                             "source": "pin_conflict"})
    dp.to_disable = disabled

    # -- 5. untouched: every other enabled override, with a reason --
    for label, ov in sorted(index.enabled_overrides().items()):
        if label in plan_nodes or label in seen or index.group(label):
            continue
        if any(u["node"] == f"&{label}" for u in dp.untouched):
            continue
        if family_of(label) is None:
            dp.untouched.append({"node": f"&{label}",
                                 "reason": "not managed by pin assignment; out of scope"})
    return dp, s1, s2


def _baseline_owner_map(index):
    """pin -> enabled baseline node label (same semantics as M2)."""
    owner = {}
    for label, ov in index.enabled_overrides().items():
        for gl in ov.pinctrl_0:
            g = index.group(gl)
            if g:
                for e in g.entries:
                    if e.af is not None:
                        owner.setdefault(e.pin, label)
    return owner


def _load(path):
    try:
        return json.load(open(path))
    except Exception:
        return None


def run(plan_csv=None):
    """Convenience entry: load everything from config and locate.
    Returns (DiffPlan, Stage1Result, Stage2Result)."""
    plan_csv = plan_csv or config.PLAN_CSV
    rows, extra_cols = load_plan_flexible(plan_csv)
    index = build_index()
    return locate(
        rows, extra_cols, index=index,
        af_table=_load(config.AF_TABLE),
        profiles=_load(config.PERIPHERAL_PROFILES),
        gpio_pins=_load(config.GPIO_PINS),
        require=_load(config.REQUIRE),
        signal_to_pin=_load(config.SIGNAL_TO_PIN),
        alias=_load(config.PERIPHERAL_NODE_ALIAS),
        fixed=_load(config.FIXED_CONNECTIONS),
        board_cfg=_load(config.BOARD_CONFIG),
        bindings=_load(config.DTS_PROPERTY_BINDINGS),
        baseline_rows=load_plan(config.BASELINE_CSV),
    )
