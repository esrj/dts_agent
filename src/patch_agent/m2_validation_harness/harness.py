"""Stage 1 — Validation Harness (plan.md §5 / ROADMAP M2).

Deterministically validates the new plan (plan.csv) against the SoC capability +
board constraints, and emits the IRs the later stages consume. No LLM.

Checks:
  1. af_table       : every (pin, af) actually maps to the claimed signal.
  2. gpio_pins      : no plan pin is a protected GPIO.
  3. plan internal  : no pin double-booked by two signals.
  4. profiles       : each peripheral's signal set covers a complete mode.
  5. require        : board-pin-locked peripherals stay on official pins; their
                      pins aren't stolen by other signals.

Outputs (in-memory Stage1Result; the runner can dump to JSON):
  - validated_plan_ir
  - expected_dts_requirements
  - board_regression_checks
  - diff (new vs baseline)
"""
import re
import csv
import json
from dataclasses import dataclass, field, asdict

from .. import config

# ---- error types (align with plan.md §7.5 classifier where applicable) ---
AF_MISMATCH = "AF_MISMATCH"
INVALID_PIN = "INVALID_PIN"
PROTECTED_GPIO_CONFLICT = "PROTECTED_GPIO_CONFLICT"
PIN_DOUBLE_BOOKED = "PIN_DOUBLE_BOOKED"
INCOMPLETE_PERIPHERAL = "INCOMPLETE_PERIPHERAL"
REQUIRE_CONFLICT = "REQUIRE_CONFLICT"
UNKNOWN_PERIPHERAL = "UNKNOWN_PERIPHERAL"
PLAN_CONTRADICTION = "PLAN_CONTRADICTION"


@dataclass
class Error:
    error_type: str
    message: str
    peripheral: str = None
    signal: str = None
    pin: str = None
    af: int = None


@dataclass
class Stage1Result:
    passed: bool
    errors: list = field(default_factory=list)
    warnings: list = field(default_factory=list)
    peripherals_to_disable: list = field(default_factory=list)   # safe conflict owners (node labels)
    validated_plan_ir: dict = field(default_factory=dict)
    expected_dts_requirements: dict = field(default_factory=dict)
    board_regression_checks: dict = field(default_factory=dict)
    diff: dict = None

    def error_types(self):
        return sorted({e.error_type for e in self.errors})

    def to_dict(self):
        d = asdict(self)
        d["error_types"] = self.error_types()
        return d


# ---- helpers -------------------------------------------------------------
def load_plan(path):
    rows = []
    for r in csv.DictReader(open(path, encoding="utf-8-sig")):
        if not r.get("peripheral"):
            continue
        if (r.get("signal") or "").strip().upper() == "DISABLE":
            continue    # disable 指令列（pin/af 為空）——由 m5 Locator 消費
        rows.append({"peripheral": r["peripheral"].strip(),
                     "signal": r["signal"].strip(),
                     "pin": r["pin"].strip(),
                     "af": int(r["af"])})
    return rows


def _role(peripheral, signal):
    """Signal -> role suffix. USART2_TX->TX ; OCTOSPIM_P1_CLK->CLK ; ETH1_RGMII_TXD0->RGMII_TXD0."""
    return re.sub(rf"^{re.escape(peripheral)}(_P\d+)?_", "", signal)


def _profile_family(peripheral, profiles):
    p = peripheral.upper()
    p = profiles.get("aliases", {}).get(p, p)          # CAN->FDCAN, OCTOSPIM->OCTOSPI1
    base = re.sub(r"\d+$", "", p).lower()              # FDCAN1->fdcan, OCTOSPI1->octospi
    return base if base in profiles.get("families", {}) else None


def _diff(new_rows, base_rows):
    def index(rows):
        return {(r["peripheral"], r["signal"]): {"pin": r["pin"], "af": r["af"]} for r in rows}
    n, b = index(new_rows), index(base_rows)
    added = [{"peripheral": k[0], "signal": k[1], **v} for k, v in n.items() if k not in b]
    changed = [{"peripheral": k[0], "signal": k[1], "from": b[k], "to": v}
               for k, v in n.items() if k in b and b[k] != v]
    removed = [{"peripheral": k[0], "signal": k[1], **v} for k, v in b.items() if k not in n]
    return {"added": added, "changed": changed, "removed": removed}


# ---- core ----------------------------------------------------------------
def _boot_required_nodes(require):
    nodes = set()
    for v in require.get("peripherals", {}).values():
        nd = v.get("dts_node", "") if isinstance(v, dict) else ""
        if not nd or nd.startswith("("):
            continue
        for tok in re.split(r"[\s/()]+", nd):
            if re.fullmatch(r"[a-z][\w]*", tok):
                nodes.add(tok)
    return nodes


def _locked_nodes(locked, alias_map):
    out = set()
    for lk in locked:
        name = re.sub(r"_P\d+$", "", lk)
        tn = (alias_map.get(name) or alias_map.get(lk) or {}).get("target_node")
        if tn:
            out.add(tn.lstrip("&"))
    return out


def validate_plan(plan_rows, *, af_table, profiles, gpio_pins, require,
                  signal_to_pin, alias, baseline_rows=None, baseline_owner=None):
    errors, warnings = [], []
    protected = set(gpio_pins.get("protected_pins", []))
    alias_map = alias.get("aliases", {})
    sig2pin = signal_to_pin.get("signal_to_pin", signal_to_pin)
    locked = require.get("board_pin_locked", {}).get("peripherals", [])
    locked_pins = {sig2pin[s]: s for s in sig2pin
                   if any(s.startswith(lk) for lk in locked)}
    boot_nodes = _boot_required_nodes(require)
    unsafe_nodes = boot_nodes | _locked_nodes(locked, alias_map)

    by_per, pin_users = {}, {}
    for r in plan_rows:
        by_per.setdefault(r["peripheral"], []).append(r)
    # node labels this plan (re)configures
    plan_nodes = {(alias_map.get(p) or {}).get("target_node", "").lstrip("&") or p.lower()
                  for p in by_per}

    # --- per-signal: af_table, protected, board-lock ---
    for r in plan_rows:
        per, sig, pin, af = r["peripheral"], r["signal"], r["pin"], r["af"]
        entry = af_table.get(pin)
        if entry is None:
            errors.append(Error(INVALID_PIN, f"pin {pin} not in af_table", per, sig, pin, af))
        elif str(af) not in entry:
            errors.append(Error(AF_MISMATCH,
                                f"{pin} has no AF{af} (available: {sorted(entry, key=int)})",
                                per, sig, pin, af))
        elif sig not in entry[str(af)].split("/"):
            errors.append(Error(AF_MISMATCH,
                                f"{pin} AF{af} = {entry[str(af)]!r}, not {sig}", per, sig, pin, af))
        if pin in protected:
            errors.append(Error(PROTECTED_GPIO_CONFLICT,
                                f"{pin} is a protected GPIO (reserved on this board)", per, sig, pin, af))
        # board-lock (a): a locked peripheral's signal moved off its official pin
        for lk in locked:
            if sig.startswith(lk):
                off = sig2pin.get(sig)
                if off and off != pin:
                    errors.append(Error(REQUIRE_CONFLICT,
                                        f"{sig} is board-pin-locked to {off}, not {pin}", per, sig, pin, af))
        pin_users.setdefault(pin, set()).add(sig)

    # --- board-lock (b): a locked pin grabbed by some other signal ---
    for pin, sigs in pin_users.items():
        if pin in locked_pins:
            stealers = sorted(s for s in sigs if s != locked_pins[pin])
            if stealers:
                errors.append(Error(REQUIRE_CONFLICT,
                                    f"{pin} is locked to {locked_pins[pin]}; also requested by {stealers}", pin=pin))

    # --- plan internal: double-booked pin ---
    for pin, sigs in pin_users.items():
        if len(sigs) > 1:
            errors.append(Error(PIN_DOUBLE_BOOKED,
                                f"{pin} assigned to multiple signals: {sorted(sigs)}", pin=pin))

    # --- cross-baseline conflict: plan pin owned by an enabled baseline node
    #     that is NOT in the plan. Safe (not boot/locked) -> disable owner to free
    #     it; unsafe -> hard error. (baseline_owner = {pin: enabled node label}) ---
    to_disable = set()
    if baseline_owner:
        for r in plan_rows:
            pin, per, sig = r["pin"], r["peripheral"], r["signal"]
            owner = baseline_owner.get(pin)
            if not owner or owner in plan_nodes:
                continue
            if owner in unsafe_nodes:
                errors.append(Error(REQUIRE_CONFLICT,
                    f"{pin} ({sig}) is used by boot-critical/locked &{owner} "
                    f"(not in plan) — cannot free it", per, sig, pin, r["af"]))
            else:
                to_disable.add(owner)
                warnings.append(
                    f"{pin} ({sig}) taken from enabled &{owner} (not in plan, "
                    f"not boot-critical) → &{owner} will be DISABLED to free it.")

    # --- per-peripheral: required-signal / mode completeness + build IR ---
    ir = {}
    for per, rows in sorted(by_per.items()):
        fam = _profile_family(per, profiles)
        roles = {_role(per, r["signal"]) for r in rows}
        mode = None
        if fam:
            modes = profiles["families"][fam]["modes"]
            satisfied = [m for m, rl in modes.items() if set(rl) <= roles]
            if satisfied:
                mode = max(satisfied, key=lambda m: len(modes[m]))
            else:
                default = profiles["families"][fam].get("default")
                missing = sorted(set(modes.get(default, [])) - roles)
                errors.append(Error(INCOMPLETE_PERIPHERAL,
                                    f"{per}: signals do not cover any complete mode; "
                                    f"for default '{default}' missing roles {missing}", per))
        ir[per] = {
            "family": fam,
            "target_node": (alias_map.get(per) or {}).get("target_node"),
            "mode": mode,
            "signals": {r["signal"]: {"pin": r["pin"], "af": r["af"]} for r in rows},
        }

    validated_ir = {"board": config.BOARD, "peripherals": ir}
    expected = {per: {"target_node": info["target_node"], "required_status": "okay",
                      "signals": info["signals"]} for per, info in ir.items()}
    regression = {
        "protected_pins": sorted(protected),
        "board_pin_locked": locked,
        "boot_required_peripherals": sorted(require.get("peripherals", {})),
        "boot_required_nodes": sorted(boot_nodes),
        "peripherals_to_disable": sorted(to_disable),
        "do_not_modify": "nodes unrelated to the plan peripherals must stay byte-identical",
    }
    diff = _diff(plan_rows, baseline_rows) if baseline_rows is not None else None
    return Stage1Result(passed=(len(errors) == 0), errors=errors, warnings=warnings,
                        peripherals_to_disable=sorted(to_disable),
                        validated_plan_ir=validated_ir, expected_dts_requirements=expected,
                        board_regression_checks=regression, diff=diff)


def _load(path):
    return json.load(open(path))


def _baseline_owner_map():
    """pin -> enabled baseline node label (active pins of enabled overrides)."""
    from ..m1_dts_parser import build_index
    idx = build_index()
    owner = {}
    for label, ov in idx.enabled_overrides().items():
        for gl in ov.pinctrl_0:
            g = idx.group(gl)
            if g:
                for e in g.entries:
                    if e.af is not None:
                        owner.setdefault(e.pin, label)
    return owner


def run(plan_csv=None, with_baseline=True, check_baseline_conflicts=True):
    """Convenience entry: load every input from config and validate plan_csv."""
    plan_csv = plan_csv or config.PLAN_CSV
    baseline_rows = load_plan(config.BASELINE_CSV) if with_baseline else None
    owner = _baseline_owner_map() if check_baseline_conflicts else None
    return validate_plan(
        load_plan(plan_csv),
        af_table=_load(config.AF_TABLE),
        profiles=_load(config.PERIPHERAL_PROFILES),
        gpio_pins=_load(config.GPIO_PINS),
        require=_load(config.REQUIRE),
        signal_to_pin=_load(config.SIGNAL_TO_PIN),
        alias=_load(config.PERIPHERAL_NODE_ALIAS),
        baseline_rows=baseline_rows,
        baseline_owner=owner,
    )
