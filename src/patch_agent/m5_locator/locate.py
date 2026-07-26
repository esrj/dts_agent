"""M5 — Locator (plan.md v3 §5 / ROADMAP M5).

Deterministic front half of the v3 pipeline. Wraps M2 (plan validation) + M3
(target resolution) and adds what the LLM Generator needs:

  1. flexible plan.csv parsing — L1 columns (peripheral,signal,pin,af) are
     validated deterministically; any extra columns (L2-L5: connected_ic,
     ic_address, dt_property, …) are carried through verbatim per row.
     A row whose signal is the keyword `DISABLE` (pin/af empty) is a
     directive: "this peripheral MUST be disabled in the final DT".
  2. the diff plan — plan.csv is the complete intent:
       to_enable_or_update : every peripheral in the plan (+ context pack)
       to_disable          : explicit DISABLE rows (source: user_request)
                             + M2's pin-conflict owners (source: pin_conflict)
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
    validate_plan, load_plan, _boot_required_nodes, _locked_nodes, _load,
    Error, REQUIRE_CONFLICT, UNKNOWN_PERIPHERAL, PLAN_CONTRADICTION)
from ..m3_target_resolution import resolve

L1_COLS = ("peripheral", "signal", "pin", "af")


# ---- flexible plan loading (§3.2) -----------------------------------------
def load_plan_flexible(path):
    """Parse a variable-schema plan.csv.

    Returns (rows, extra_columns, disable_requests): each row has the four L1
    keys plus an `extras` dict holding every non-empty non-L1 column verbatim.
    A row whose signal is `DISABLE`（大小寫不拘、pin/af 留空）不是腳位列而是
    指令列——其 peripheral 收進 disable_requests（去重、保序），不進 rows。
    """
    rows, extra_cols, disables = [], [], []
    with open(path, encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        extra_cols = [c for c in (reader.fieldnames or []) if c and c not in L1_COLS]
        for lineno, r in enumerate(reader, start=2):
            per = (r.get("peripheral") or "").strip()
            if not per:
                continue
            if (r.get("signal") or "").strip().upper() == "DISABLE":
                if per not in disables:
                    disables.append(per)
                continue
            af = (r.get("af") or "").strip()
            if not af.lstrip("-").isdigit():
                # 與 m2.load_plan 同規則：空欄/非整數 AF 要可定位，不裸炸 int('')
                raise ValueError(
                    f"{path} 第 {lineno} 行：af 欄必須是整數，實際為 {af!r}"
                    f"（{per},{r.get('signal')},{r.get('pin')}）")
            rows.append({
                "peripheral": per,
                "signal": r["signal"].strip(),
                "pin": r["pin"].strip(),
                "af": int(af),
                "extras": {c: r[c].strip() for c in extra_cols
                           if (r.get(c) or "").strip()},
            })
    return rows, extra_cols, disables


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
    # default_factory：init_board() 切板後新建的 DiffPlan 要拿到當下的 BOARD，
    # 不能在 import 時定格（多板）。
    board: str = field(default_factory=lambda: config.BOARD)
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


# ---- peripheral 欄正規化（多底線 instance 名，2026-07-25）--------------------
def _normalize_peripherals(plan_rows, alias_map):
    """以 alias 鍵（該板權威 instance 名）對 signal 做最長前綴匹配，修正
    plan 的 peripheral 欄。

    第一段的 peripheral 欄用 `signal.split("_", 1)[0]` 推導——對 K3 的多底線
    instance 名（MCU_I2C0_SCL、WKUP_UART0_CTSn）會切出假週邊「MCU」「WKUP」，
    導致定位到不存在的節點（&mcu → dtc `Label or path not found`）。
    alias 鍵是 extractor 從官方 DTS 導出的正確 instance 名（MCU_I2C0…），
    在消費端據此重新歸戶。單 token instance（stm32 全部、K3 主域）匹配結果
    與 split 相同——回歸零改變；匹配不到一律保留原值（不猜）。"""
    insts = sorted(alias_map, key=len, reverse=True)          # 最長前綴優先
    out = []
    for r in plan_rows:
        sig = (r.get("signal") or "").upper()
        per = r.get("peripheral")
        for tok in insts:
            if sig.startswith(tok.upper() + "_"):
                # 同 pad 雙命名別名（ma35d1 的 eMMC0_*/SD0_* 同屬 &sdhci0）：
                # 折回 canonical 主鍵，兩套訊號名歸戶到同一週邊，plan_set
                # 才是完整集合（否則各自殘缺、觸發 INCOMPLETE/錯誤定位）
                per = (alias_map[tok] or {}).get("canonical") or tok
                break
        out.append({**r, "peripheral": per})
    return out


# ---- core -------------------------------------------------------------------
def locate(plan_rows, extra_columns, *, index, af_table, profiles, gpio_pins,
           require, signal_to_pin, alias, fixed=None, board_cfg=None,
           bindings=None, baseline_rows=None, disable_requests=None):
    """Run the deterministic Locator over already-parsed flexible plan rows."""
    alias_map = alias.get("aliases", {})
    plan_rows = _normalize_peripherals(plan_rows, alias_map)
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

    # -- 4. to_disable: 使用者 DISABLE 指令列 ＋ 腳位衝突 ---------------------
    # 舊語意「官方有、plan 沒有 → 整個 node disable」已移除：absent-from-plan
    # 的官方節點一律保留（untouched），只有 plan 把該節點正在用的腳搶去做
    # 別的功能時（M2 的 baseline_owner 衝突偵測；boot/board-locked 擁有者在
    # M2 是硬錯 boot_conflict，不會流到這裡）才 disable 整個 node——缺腳的
    # 週邊本來就動不了。最終 DT ＝ 官方預設 ＋ plan 疊加。
    # 例外是 plan.csv 的明確指令列 `<peripheral>,DISABLE,,`（source:
    # user_request）：boot/board-locked 不准關（硬錯）、與一般列同時出現是
    # 自相矛盾（硬錯）、baseline 本來就沒開則為冪等 no-op（warning）。
    disabled, seen = [], set()
    enabled = index.enabled_overrides()
    for name in (disable_requests or []):
        tn = (alias_map.get(name.upper()) or {}).get("target_node")
        label = tn.lstrip("&") if tn else name.lower()
        if label in seen:
            continue
        if label not in index.all_labels:
            dp.errors.append(Error(UNKNOWN_PERIPHERAL,
                f"DISABLE {name}: no such peripheral/node in baseline DTS",
                peripheral=name))
        elif label in plan_nodes:
            dp.errors.append(Error(PLAN_CONTRADICTION,
                f"DISABLE {name}: the plan also configures &{label} — "
                f"cannot both enable and disable it", peripheral=name))
        elif label in unsafe:
            why = "boot-required" if label in boot_nodes else "board-locked"
            dp.errors.append(Error(REQUIRE_CONFLICT,
                f"DISABLE {name}: &{label} is {why} — cannot disable",
                peripheral=name))
        elif label not in enabled:
            dp.warnings.append(f"DISABLE {name}: &{label} is already disabled "
                               f"in baseline — nothing to do")
            dp.untouched.append({"node": f"&{label}",
                                 "reason": "plan requests disable; already "
                                           "disabled in baseline (no change)"})
            seen.add(label)
        else:
            disabled.append({"node": f"&{label}", "family": family_of(label),
                             "reason": f"explicitly disabled by plan "
                                       f"({name},DISABLE row)",
                             "source": "user_request"})
            seen.add(label)
    if len(dp.errors) > len(s1.errors):
        dp.passed = False
    for label in s1.peripherals_to_disable:          # conflict-driven
        if label in plan_nodes or label in seen:
            continue
        disabled.append({"node": f"&{label}", "family": family_of(label),
                         "reason": "enabled baseline owner of a pin the plan takes",
                         "source": "pin_conflict"})
        seen.add(label)
    dp.to_disable = disabled
    for label, ov in sorted(index.enabled_overrides().items()):
        fam = family_of(label)
        if fam is None or index.group(label) or label in plan_nodes or label in seen:
            continue
        why = ("boot-required" if label in boot_nodes
               else "board-locked" if label in unsafe
               else "official default")
        dp.untouched.append({"node": f"&{label}",
                             "reason": f"absent from plan; {why} kept (no pin conflict)"})

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


# _load 改用 m2 的統一版本（KB_ROBUSTNESS_PLAN 改動 2）：選配檔缺檔回空骨架、
# 必要檔缺檔／壞 JSON 一律明確 raise——取代舊版「except 全吞回 None」
# （None 會在下游 .get() 處以 AttributeError 炸開，離根因太遠）。


def run(plan_csv=None):
    """Convenience entry: load everything from config and locate.
    Returns (DiffPlan, Stage1Result, Stage2Result)."""
    plan_csv = plan_csv or config.PLAN_CSV
    rows, extra_cols, disables = load_plan_flexible(plan_csv)
    index = build_index()
    return locate(
        rows, extra_cols, index=index, disable_requests=disables,
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
