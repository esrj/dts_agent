"""Stage 2 — Target Resolution (plan.md §6 / ROADMAP M3).

Deterministic: takes M2's validated_plan_ir + M1's DtsIndex and decides, per
peripheral, WHERE and HOW the patch lands. Replaces v1's file/node localization.
No LLM.

Per peripheral it resolves:
  - target_node / target_file (always the board .dts)
  - enabled_in_baseline + existing pinctrl-0 group(s) and their (pin, af) set
  - reuse_or_generate hint (reuse iff the plan's (pin,af) set already exists as a group)
  - cross-peripheral conflicts (a plan pin currently owned by another enabled node)
"""
from dataclasses import dataclass, field, asdict

from .. import config
from ..m1_dts_parser import build_index


@dataclass
class Conflict:
    pin: str
    plan_signal: str
    baseline_owner: str          # node label currently using the pin
    owner_group: str
    owner_in_plan: bool          # owner is also being reassigned this plan
    owner_boot_critical: bool
    resolution: str              # human/agent next step


@dataclass
class TargetResolution:
    peripheral: str
    target_node: str
    target_file: str
    family: str
    enabled_in_baseline: bool
    existing_pinctrl_0: list = field(default_factory=list)   # group labels
    existing_group_set: list = field(default_factory=list)   # [[pin, af], ...]
    plan_set: list = field(default_factory=list)             # [[pin, af], ...]
    reuse_or_generate: str = "generate"
    reuse_group: list = field(default_factory=list)          # group labels to reuse
    new_group_label: str = None                              # if generate
    conflicts: list = field(default_factory=list)


@dataclass
class Stage2Result:
    resolutions: dict = field(default_factory=dict)
    has_blocking_conflict: bool = False

    def to_dict(self):
        return {
            "has_blocking_conflict": self.has_blocking_conflict,
            "resolutions": {k: asdict(v) for k, v in self.resolutions.items()},
        }


def _node_label(target_node, peripheral):
    if target_node:
        return target_node.lstrip("&")
    return peripheral.lower()


def _baseline_pin_owner(index):
    """pin -> (node_label, group_label) for every active pin of an enabled override."""
    owner = {}
    for label, ov in index.enabled_overrides().items():
        for gl in ov.pinctrl_0:
            g = index.group(gl)
            if not g:
                continue
            for e in g.entries:
                if e.af is not None:
                    owner.setdefault(e.pin, (label, gl))
    return owner


def resolve(validated_plan_ir, index, boot_required_nodes=None):
    boot = set(boot_required_nodes or [])
    owner = _baseline_pin_owner(index)
    peripherals = validated_plan_ir.get("peripherals", {})
    # node label -> plan peripheral (to know if a conflicting owner is also in the plan)
    label_to_plan = {_node_label(info.get("target_node"), per): per
                     for per, info in peripherals.items()}

    result = Stage2Result()
    for per, info in peripherals.items():
        label = _node_label(info.get("target_node"), per)
        ov = index.override(label)
        existing_p0 = list(ov.pinctrl_0) if ov else []
        existing_set = set()
        for gl in existing_p0:
            g = index.group(gl)
            if g:
                existing_set |= g.pin_af_set()
        plan_set = {(s["pin"], s["af"]) for s in info["signals"].values()}

        # --- reuse vs generate ---
        reuse_group, hint, new_label = [], "generate", None
        if existing_set and plan_set == existing_set:
            reuse_group, hint = existing_p0, "reuse"
        else:
            # any single existing group whose set == plan_set?
            match = [lbl for lbl, g in index.pinctrl_groups.items()
                     if g.pin_af_set() == plan_set]
            if match:
                reuse_group, hint = match[:1], "reuse"
        if hint == "generate":
            new_label = f"{label}_pins_generated"

        # --- cross-peripheral conflicts ---
        conflicts = []
        for sig, s in info["signals"].items():
            pin = s["pin"]
            o = owner.get(pin)
            if o and o[0] != label:
                owner_label, owner_group = o
                in_plan = owner_label in label_to_plan
                boot_crit = owner_label in boot
                if boot_crit:
                    res = f"STOP: {pin} owned by boot-critical &{owner_label}"
                elif in_plan:
                    res = f"&{owner_label} is also reassigned this plan (should free {pin})"
                else:
                    res = f"free/disable &{owner_label} (not in plan, not boot-critical)"
                conflicts.append(Conflict(pin, sig, owner_label, owner_group,
                                          in_plan, boot_crit, res))
                if boot_crit:
                    result.has_blocking_conflict = True

        result.resolutions[per] = TargetResolution(
            peripheral=per,
            target_node=info.get("target_node") or f"&{label}",
            target_file=config.BOARD_DTS.name,
            family=info.get("family"),
            enabled_in_baseline=bool(ov and ov.enabled),
            existing_pinctrl_0=existing_p0,
            existing_group_set=sorted([list(x) for x in existing_set]),
            plan_set=sorted([list(x) for x in plan_set]),
            reuse_or_generate=hint,
            reuse_group=reuse_group,
            new_group_label=new_label,
            conflicts=conflicts,
        )
    return result


def run(plan_csv=None):
    """Convenience: run Stage 1, build the index, resolve. Returns (stage1, stage2)."""
    from ..m2_validation_harness import run as stage1_run
    s1 = stage1_run(plan_csv=plan_csv)
    idx = build_index()
    s2 = resolve(s1.validated_plan_ir, idx,
                 boot_required_nodes=s1.board_regression_checks.get("boot_required_nodes"))
    return s1, s2
