"""M7 — Validator (plan.md v3 §7 / ROADMAP M7).

Deterministic, zero-trust gate on the ASSEMBLED artifact. M6's guards check
each target's edits before rendering; M7 independently re-checks the final
generated DTS as a whole (so a hand-edited file, a poisoned cache entry or an
assembly bug is still caught), then compiles it. Six layers, fail-fast — the
first failing layer stops the run and its structured errors go to M8 Repairer:

  1. schema/allowlist  — structured edits well-formed; every edit target is in
                         the diff plan (enable/update or disable)
  2. plan consistency  — per target: default pinmux == plan (pin,af) verbatim;
                         af_table confirms each (pin,af)->signal; status okay;
                         reuse points at the resolved group; noop left alone;
                         plan L3/L4 extras (dt_property / IC binding) applied
  3. multi-state       — pinctrl-names == baseline-declared states; every
                         pinctrl-N present and resolvable (§7.4a)
  4. regression        — protected GPIOs unused; boot-required never disabled;
                         baseline byte-identical outside the managed region;
                         to_disable all rendered; untouched nodes absent
  5. structural        — no label collision/duplication; STM32_PINMUX macro
                         not expanded; pinctrl phandles resolvable
  6. compile           — cpp preprocess -> dtc -I dts -O dtb

Output: ValidationReport -> output/generated/validation_report.json
(passed / failed_layer / errors[error_type, message, peripheral, file, line]).
"""
import json
import os
import re
import subprocess
import tempfile
from dataclasses import dataclass, field, asdict

from .. import config
from ..m1_dts_parser import build_index
from ..m1_dts_parser.parser import parse_dts, iter_all
from ..m2_validation_harness.harness import _boot_required_nodes
from ..m4_patch_generation.generate import MARK_BEGIN, MARK_END
from ..pinmux_style import get_style

# ---- error types (M8 Repairer classifier consumes these, plan.md §8.2) ----
SCHEMA_VIOLATION = "SCHEMA_VIOLATION"
PLAN_MISMATCH = "PLAN_MISMATCH"
AF_MISMATCH = "AF_MISMATCH"
BINDING_ERROR = "BINDING_ERROR"
MISSING_STATE = "MISSING_STATE"
PROTECTED_GPIO_CONFLICT = "PROTECTED_GPIO_CONFLICT"
REQUIRE_CONFLICT = "REQUIRE_CONFLICT"
REGRESSION_OUTSIDE_REGION = "REGRESSION_OUTSIDE_REGION"
DISABLE_NOT_APPLIED = "DISABLE_NOT_APPLIED"
UNTOUCHED_MODIFIED = "UNTOUCHED_MODIFIED"
DUPLICATE_LABEL = "DUPLICATE_LABEL"
LABEL_NOT_FOUND = "LABEL_NOT_FOUND"
MACRO_EXPANDED = "MACRO_EXPANDED"
SYNTAX_ERROR = "SYNTAX_ERROR"
DTC_ERROR = "DTC_ERROR"

# pinmux 巨集解析已平移至 pinmux_style（多廠商：依板的 style 分派，缺檔預設 stm32）


@dataclass
class VError:
    error_type: str
    message: str
    layer: int
    peripheral: str = None
    # default_factory：切板後的 VError 要指向當下板的 .dts 名（多板）。
    file: str = field(default_factory=lambda: config.BOARD_DTS.name)
    line: int = None            # absolute line in the generated .dts

    def to_dict(self):
        return {k: v for k, v in asdict(self).items() if v is not None}


@dataclass
class ValidationReport:
    passed: bool = True
    failed_layer: int = None
    errors: list = field(default_factory=list)
    warnings: list = field(default_factory=list)
    checks: dict = field(default_factory=dict)     # layer name -> "pass"/"fail"/"skipped"

    def error_types(self):
        return sorted({e.error_type for e in self.errors})

    def to_dict(self):
        return {"passed": self.passed, "failed_layer": self.failed_layer,
                "error_types": self.error_types(),
                "errors": [e.to_dict() for e in self.errors],
                "warnings": self.warnings, "checks": self.checks}


# ---- artifact parsing -------------------------------------------------------
class _Artifact:
    """Parsed view of a generated .dts: baseline prefix + managed region."""

    def __init__(self, generated_dts):
        self.text = generated_dts
        self.region = None
        self.region_start = None                   # 0-based line of MARK_BEGIN
        if MARK_BEGIN in generated_dts and MARK_END in generated_dts:
            s = generated_dts.index(MARK_BEGIN)
            e = generated_dts.index(MARK_END) + len(MARK_END)
            self.region = generated_dts[s:e]
            self.region_start = generated_dts[:s].count("\n")
        self.groups = {}                           # label -> [(pin, mode, af)]
        self.group_props = {}                      # label -> [per-subnode prop dict]
        self.overrides = {}                        # ref -> Node
        self.nodes = []                            # parsed top-level region nodes
        if self.region:
            self.nodes = parse_dts(self.region)
            style = get_style()
            containers = style.containers()
            for n in self.nodes:
                if n.ref in containers:
                    for g in n.children:
                        entries, props = [], []
                        for sub, text in style.pin_sources(g):
                            props.append({k: v for k, v in sub.props.items()
                                          if k not in style.GROUP_PROPS})
                            for pin, af, mode in style.iter_pinmux(text, domain=n.ref):
                                entries.append((pin, mode, af))
                        self.groups[g.label] = entries
                        self.group_props[g.label] = props
                elif n.ref:
                    self.overrides[n.ref] = n

    def line_of(self, needle):
        """Absolute 1-based line in the generated file of the first `needle` in the region."""
        if not self.region:
            return None
        i = self.region.find(needle)
        if i < 0:
            return None
        return self.region_start + self.region[:i].count("\n") + 1

    def active_set(self, group_labels):
        """{(pin, mode)} across groups, ANALOG/GPIO excluded."""
        out = set()
        for gl in group_labels:
            for pin, mode, af in self.groups.get(gl, []):
                if af is not None:
                    out.add((pin, mode))
        return out

    def all_pins(self):
        return {pin for ents in self.groups.values() for pin, _m, _a in ents}

    def pinctrl_refs(self, node, key):
        return re.findall(r"&(\w+)", node.props.get(key, ""))


# ---- layer 1: schema / allowlist ---------------------------------------------
def _layer1_schema(dp, edits, errs):
    allowed = ({t["target_node"] for t in dp["to_enable_or_update"]} |
               {d["node"] for d in dp["to_disable"]})
    for e in edits or []:
        et = e.get("type")
        if et == "append_managed_region":
            continue
        if et not in ("pinctrl_group", "node_override", "disable_override"):
            errs.append(VError(SCHEMA_VIOLATION, f"unknown edit type {et!r}", 1))
            continue
        if not e.get("source") or not e.get("reason"):
            errs.append(VError(SCHEMA_VIOLATION,
                               f"edit missing source/reason: {et} "
                               f"{e.get('target') or e.get('label') or ''}".strip(), 1))
        tgt = e.get("target")
        if et in ("node_override", "disable_override") and tgt not in allowed:
            errs.append(VError(SCHEMA_VIOLATION,
                               f"edit target {tgt!r} is not in the diff plan", 1,
                               peripheral=(tgt or "").lstrip("&")))


# ---- layer 2: plan consistency -------------------------------------------------
def _layer2_plan(dp, art, af_table, errs):
    for t in dp["to_enable_or_update"]:
        per, action = t["peripheral"], t["action"]
        node = t["target_node"].lstrip("&")
        ov = art.overrides.get(node)

        if action == "noop":
            if ov is not None:
                errs.append(VError(PLAN_MISMATCH,
                                   f"{per}: noop target &{node} was modified in the managed region",
                                   2, per, line=art.line_of(f"&{node} {{")))
            continue
        if ov is None:
            errs.append(VError(PLAN_MISMATCH,
                               f"{per}: node override &{node} missing from managed region", 2, per))
            continue
        if ov.status != "okay":
            errs.append(VError(PLAN_MISMATCH,
                               f"{per}: &{node} status is {ov.status!r}, expected \"okay\"",
                               2, per, line=art.line_of(f"&{node} {{")))

        if action == "reuse":
            want = set(t["resolution"]["reuse_group"])
            got = set(art.pinctrl_refs(ov, "pinctrl-0"))
            if got != want:
                errs.append(VError(PLAN_MISMATCH,
                                   f"{per}: reuse pinctrl-0 {sorted(got)} != resolved group {sorted(want)}",
                                   2, per, line=art.line_of(f"&{node} {{")))
            continue

        # action == generate: default pinmux must equal the plan, verbatim
        style = get_style()
        plan_set = {(r["pin"], style.mode_token(r["af"])) for r in t["plan_rows"]}
        got = art.active_set(art.pinctrl_refs(ov, "pinctrl-0"))
        if got != plan_set:
            only_gen = sorted(got - plan_set)
            only_plan = sorted(plan_set - got)
            errs.append(VError(PLAN_MISMATCH,
                               f"{per}: default pinmux != plan "
                               f"(generated-only: {only_gen}, plan-only: {only_plan})",
                               2, per, line=art.line_of(f"&{node} {{")))
        # af_table re-confirms every plan (pin, af) -> signal
        for r in t["plan_rows"]:
            entry = af_table.get(r["pin"], {})
            sig = entry.get(str(r["af"]), "")
            if r["signal"] not in sig.split("/"):
                errs.append(VError(AF_MISMATCH,
                                   f"{per}: af_table says {r['pin']}/"
                                   f"{style.mode_token(r['af'])} = {sig!r}, "
                                   f"plan says {r['signal']}", 2, per))
        # plan L3/L4 extras must be applied
        extras = {}
        for r in t["plan_rows"]:
            extras.update(r.get("extras") or {})
        if extras.get("dt_property"):
            key = extras["dt_property"].split("=", 1)[0].strip()
            if key not in ov.props:
                errs.append(VError(BINDING_ERROR,
                                   f"{per}: plan dt_property {key!r} not applied on &{node}",
                                   2, per, line=art.line_of(f"&{node} {{")))
        if (extras.get("connected_ic") or extras.get("ic_address")) and not ov.children:
            errs.append(VError(BINDING_ERROR,
                               f"{per}: plan binds an IC but &{node} has no child node",
                               2, per, line=art.line_of(f"&{node} {{")))


# ---- layer 3: multi-state (§7.4a) ------------------------------------------------
def _layer3_multistate(dp, art, base_labels, errs):
    for t in dp["to_enable_or_update"]:
        if t["action"] != "generate":
            continue
        per = t["peripheral"]
        node = t["target_node"].lstrip("&")
        ov = art.overrides.get(node)
        if ov is None:
            continue                              # layer 2 already reported
        states = t["context_pack"]["declared_pinctrl_states"]
        names = re.findall(r'"([^"]+)"', ov.props.get("pinctrl-names", ""))
        if names != states:
            errs.append(VError(MISSING_STATE,
                               f"{per}: pinctrl-names {names} != baseline-declared {states}",
                               3, per, line=art.line_of(f"&{node} {{")))
        known = set(art.groups) | base_labels
        for i, st in enumerate(states):
            refs = art.pinctrl_refs(ov, f"pinctrl-{i}")
            if not refs:
                errs.append(VError(MISSING_STATE,
                                   f"{per}: pinctrl-{i} ({st!r}) missing on &{node}",
                                   3, per, line=art.line_of(f"&{node} {{")))
                continue
            for lbl in refs:
                if lbl not in known:
                    errs.append(VError(LABEL_NOT_FOUND,
                                       f"{per}: pinctrl-{i} references undefined &{lbl}",
                                       3, per, line=art.line_of(f"&{lbl}")))


# ---- layer 4: regression -----------------------------------------------------------
def _layer4_regression(dp, art, baseline, gpio_pins, require, errs):
    protected = set(gpio_pins.get("protected_pins", []))
    for pin in sorted(art.all_pins() & protected):
        errs.append(VError(PROTECTED_GPIO_CONFLICT,
                           f"generated pinmux touches protected GPIO {pin}", 4,
                           line=art.line_of(pin)))

    boot = _boot_required_nodes(require)
    for ref, ov in art.overrides.items():
        if ref in boot and ov.status == "disabled":
            errs.append(VError(REQUIRE_CONFLICT,
                               f"boot-required &{ref} is disabled by the managed region",
                               4, ref, line=art.line_of(f"&{ref} {{")))

    # zero diff outside the managed region
    base_noeol = baseline.rstrip("\n")
    if not art.text.startswith(base_noeol):
        errs.append(VError(REGRESSION_OUTSIDE_REGION,
                           "baseline is not preserved byte-identically before the managed region", 4))
    elif art.region:
        tail = art.text[len(base_noeol):]
        if tail.strip() != art.region.strip():
            errs.append(VError(REGRESSION_OUTSIDE_REGION,
                               "content exists outside the managed region", 4))

    # every to_disable rendered as disabled
    for d in dp["to_disable"]:
        ref = d["node"].lstrip("&")
        ov = art.overrides.get(ref)
        if ov is None or ov.status != "disabled":
            errs.append(VError(DISABLE_NOT_APPLIED,
                               f"{d['node']} must be disabled ({d['source']}) but is "
                               f"{'missing' if ov is None else ov.status!r}", 4, ref))

    # untouched nodes must not appear in the region
    untouched = {u["node"].lstrip("&") for u in dp.get("untouched", [])}
    for ref in sorted(set(art.overrides) & untouched):
        errs.append(VError(UNTOUCHED_MODIFIED,
                           f"&{ref} is marked untouched but the managed region modifies it",
                           4, ref, line=art.line_of(f"&{ref} {{")))


# ---- layer 5: structural ------------------------------------------------------------
def _layer5_structural(art, base_labels, defs, errs, warns):
    """Label rules differ by where a label is (re)defined:
    - under &pinctrl (a group): the label must be brand new — re-using a
      baseline group label would MERGE INTO and mutate the shared baseline
      group, silently affecting every other node that references it.
    - inside another node override (IC children): re-declaring a baseline
      label is legitimate DTS overlay merging IFF it names the same node
      (same node name, e.g. `phy1_eth1: ethernet-phy@4` re-declared inside
      &eth1) — dtc only rejects a duplicate label on a *different* node."""
    seen = {}
    _containers = get_style().containers()
    for top in art.nodes:
        in_pinctrl = (top.ref in _containers)
        if not top.ref:
            continue
        for nd in iter_all(top.children):
            if not nd.label:
                continue
            line = art.region_start + nd.line
            if nd.label in seen and seen[nd.label] != nd.name:
                errs.append(VError(DUPLICATE_LABEL,
                                   f"label {nd.label!r} defined twice in the managed "
                                   f"region on different nodes", 5, line=line))
            seen[nd.label] = nd.name
            if nd.label not in base_labels:
                continue
            if in_pinctrl:
                errs.append(VError(DUPLICATE_LABEL,
                                   f"generated group label {nd.label!r} collides with a "
                                   f"baseline pinctrl group (would mutate the shared group)",
                                   5, line=line))
            else:
                d = defs.get(nd.label)
                if d is not None and d.name == nd.name:
                    warns.append(f"&{top.ref}: re-declares baseline label "
                                 f"{nd.label!r} on the same node {nd.name!r} "
                                 f"(legitimate overlay merge)")
                else:
                    errs.append(VError(DUPLICATE_LABEL,
                                       f"label {nd.label!r} re-used on a different node "
                                       f"({nd.name!r} vs baseline "
                                       f"{d.name if d else 'unknown'!r})", 5, line=line))

    _style = get_style()
    for m in _style.RAW_NUMBER_RE.finditer(art.region):
        line = art.region_start + art.region[:m.start()].count("\n") + 1
        errs.append(VError(MACRO_EXPANDED,
                           f"pinmux written as raw numbers; must stay {_style.macro_label}",
                           5, line=line))

    known = set(art.groups) | base_labels
    for ref, ov in art.overrides.items():
        for key in ov.props:
            if key.startswith("pinctrl-") and key != "pinctrl-names":
                for lbl in art.pinctrl_refs(ov, key):
                    if lbl not in known:
                        errs.append(VError(LABEL_NOT_FOUND,
                                           f"&{ref}: {key} -> &{lbl} does not resolve",
                                           5, ref, line=art.line_of(f"&{lbl}")))


# ---- layer 6: compile ------------------------------------------------------------------
def _layer6_compile(generated_dts, errs):
    """cpp -> dtc. Returns dtc warning lines (first 20) on success."""
    d = str(config.DTS_DIR)
    tmp = tempfile.NamedTemporaryFile("w", suffix=".dts", dir=d, delete=False)
    tmp.write(generated_dts)
    tmp.close()
    pre = tmp.name + ".pre"
    try:
        cpp = subprocess.run(
            ["gcc", "-E", "-nostdinc", "-undef", "-D__DTS__",
             "-x", "assembler-with-cpp", "-I", d, "-I", d + "/include",
             tmp.name, "-o", pre], capture_output=True, text=True)
        if cpp.returncode != 0:
            line, msg = _first_error(cpp.stderr)
            errs.append(VError(SYNTAX_ERROR, f"cpp preprocess failed: {msg}", 6, line=line))
            return []
        dtc = subprocess.run(
            [config.DTC, "-I", "dts", "-O", "dtb", "-o", os.devnull, pre],
            capture_output=True, text=True)
        if dtc.returncode != 0:
            line, msg = _first_error(dtc.stderr)
            errs.append(VError(DTC_ERROR, f"dtc compile failed: {msg}", 6, line=line))
            return []
        return [l for l in dtc.stderr.splitlines() if "Warning" in l][:20]
    finally:
        for p in (tmp.name, pre):
            if os.path.exists(p):
                os.unlink(p)


def _first_error(stderr):
    for ln in stderr.splitlines():
        if "rror" in ln or "FATAL" in ln:
            m = re.search(r"\.dtsi?:(\d+)", ln) or re.search(r":(\d+)[:.]", ln)
            return (int(m.group(1)) if m else None), ln.strip()
    lines = stderr.strip().splitlines()
    return None, (lines[0] if lines else "unknown error")


# ---- top-level ---------------------------------------------------------------------------
def validate(generated_dts, diff_plan, structured_edits=None, *, index=None,
             af_table=None, gpio_pins=None, require=None, baseline=None,
             run_compile=True):
    """Validate an assembled artifact against its diff plan (M5 DiffPlan or dict)."""
    dp = diff_plan.to_dict() if hasattr(diff_plan, "to_dict") else diff_plan
    index = index or build_index()
    base_labels = set(index.all_labels)
    af_table = af_table if af_table is not None else json.load(open(config.AF_TABLE))
    gpio_pins = gpio_pins if gpio_pins is not None else json.load(open(config.GPIO_PINS))
    require = require if require is not None else json.load(open(config.REQUIRE))
    baseline = baseline if baseline is not None else open(config.BOARD_DTS).read()

    rep = ValidationReport()
    art = _Artifact(generated_dts)
    if art.region is None:
        # 無事可做的合法情境：plan 與 baseline 一致（全 noop、零 disable、
        # 零 edits）→ 沒有 managed region 是正確結果，不是 SCHEMA_VIOLATION。
        # （歷史 bug：「官方預設」plan 在此被誤判成 unrepairable。）
        actionable = ([t for t in (dp.get("to_enable_or_update") or [])
                       if t.get("action") != "noop"]
                      or dp.get("to_disable"))
        if not structured_edits and not actionable:
            rep.checks["managed_region"] = "skipped (no changes needed)"
            return rep                                # passed=True：不需要 patch
        rep.passed, rep.failed_layer = False, 0
        rep.errors.append(VError(SCHEMA_VIOLATION, "no stm-agent managed region found", 0))
        rep.checks["managed_region"] = "fail"
        return rep

    layers = [
        ("schema_allowlist", lambda e: _layer1_schema(dp, structured_edits, e)),
        ("plan_consistency", lambda e: _layer2_plan(dp, art, af_table, e)),
        ("multi_state",      lambda e: _layer3_multistate(dp, art, base_labels, e)),
        ("regression",       lambda e: _layer4_regression(dp, art, baseline, gpio_pins, require, e)),
        ("structural",       lambda e: _layer5_structural(art, base_labels, index.defs,
                                                          e, rep.warnings)),
    ]
    for num, (name, fn) in enumerate(layers, 1):
        before = len(rep.errors)
        fn(rep.errors)
        ok = len(rep.errors) == before
        rep.checks[name] = "pass" if ok else "fail"
        if not ok:                                  # fail-fast
            rep.passed, rep.failed_layer = False, num
            for name2, _ in layers[num:]:
                rep.checks[name2] = "skipped"
            if run_compile:
                rep.checks["compile"] = "skipped"
            return rep

    if run_compile:
        before = len(rep.errors)
        rep.warnings += _layer6_compile(generated_dts, rep.errors)
        ok = len(rep.errors) == before
        rep.checks["compile"] = "pass" if ok else "fail"
        if not ok:
            rep.passed, rep.failed_layer = False, 6
    else:
        rep.checks["compile"] = "skipped"
    return rep


def write_report(rep):
    config.OUTPUT_GEN.mkdir(parents=True, exist_ok=True)
    json.dump(rep.to_dict(), open(config.VALIDATION_REPORT, "w"),
              indent=2, ensure_ascii=False)


def run(plan_csv=None, provider=None, model=None, write=True, run_compile=True):
    """Full chain M5 -> M6 (cached) -> M7. Returns (DiffPlan, artifacts, report)."""
    from ..m6_generator import run as gen_run
    dp, art = gen_run(plan_csv=plan_csv, provider=provider, model=model, write=False)
    if not art.passed:
        rep = ValidationReport(passed=False, failed_layer=0)
        rep.errors.append(VError(SCHEMA_VIOLATION,
                                 "generation produced no patch: " +
                                 (art.report.get("note") or "see generation_report"), 0))
        rep.checks["generation"] = "fail"
    else:
        rep = validate(art.generated_dts, dp, art.structured_edits,
                       run_compile=run_compile)
    if write:
        write_report(rep)
    return dp, art, rep
