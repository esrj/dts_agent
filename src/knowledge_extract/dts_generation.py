"""增項 C/D:dts_generation/ 六檔 + board.yaml(頂層鍵照 stm32mp257f-ev1 樣板)。

原則(upgrade_plan.md):寧可產 schema 正確的空骨架也不要缺檔——DTS_agent
對空骨架走 LLM 補償路徑,缺檔則「產生 DTS」功能直接關閉。每檔帶
board/source/description 溯源欄位。boot_requirements.json 與 require.json
是不同檔、不同消費者(patch 的 node 級 vs solver 的腳位級),判定來源共用
增項 A 的結果。
"""
from __future__ import annotations

import json
import os
import re

from . import identify
from .dts_extract import _INFRA_RE, family_of

_SKIP_PROPS = re.compile(r"^(pinctrl-|status$|bootph-)")

_VENDOR_META = {   # 解碼器 vendor → (board.yaml vendor, kernel 樹目錄)
    "st": ("ST", "st"),
    "ti": ("TI", "ti"),
    "nuvoton": ("Nuvoton", "nuvoton"),
}


def _write_json(path: str, obj, force: bool) -> str:
    if os.path.exists(path) and not force:
        return "skipped"
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=1)
        f.write("\n")
    return "written"


def _knobs(node) -> dict:
    """override 頂層的非 pinctrl/status 屬性(板級常數;布林屬性值為 null)。"""
    return {
        k: (v if v else None)
        for k, v in node.props.items()
        if not _SKIP_PROPS.match(k)
    }


def _child_devices(node, prefix: str = "") -> list[dict]:
    """子節點中帶 compatible 的裝置(遞迴,path 用 / 串接)。"""
    out = []
    for child in node.children:
        path = f"{prefix}{child.name}"
        entry = {
            "path": path,
            "label": child.label,
            "compatible": (child.props.get("compatible") or "").strip('"') or None,
            "reg": child.props.get("reg"),
            "status": (child.props.get("status") or "").strip('"') or None,
        }
        if entry["compatible"]:
            entry["props"] = {
                k: (v if v else None) for k, v in child.props.items()
                if k not in ("compatible", "reg", "status")
            }
            out.append(entry)
        out += _child_devices(child, prefix=f"{path}/")
    return out


# --------------------------------------------------------------------------- #
# 六檔生成
# --------------------------------------------------------------------------- #
def _peripheral_node_alias(board: str, result: dict) -> dict:
    from .derive import _prefix as _sig_prefix
    aliases = {}
    for ref, spec in result["peripherals"].items():
        inst = result["instances"][ref]
        aliases[inst] = {
            "node_label": ref,
            "target_node": f"&{ref}",
            "resolved_by": "signal-prefix" if spec["signals"] else "label",
            "enabled_in_baseline": True,
            "pinctrl_groups": spec["pinctrl_groups"],
            "csv_pins": len(spec["signals"]),
            "pins_matched_in_baseline": len(spec["signals"]),
        }
        # 同 pad 雙命名別名（2026-07-26 ma35d1 事故）：sdhci0 的官方群組混用
        # eMMC0_*（CLK/CMD/DAT）與 SD0_*（nCD/WP）兩套訊號名——把每個
        # signal-prefix 都登錄成別名、標 canonical 指回主鍵,DTS_agent 的
        # m5 歸戶時折回同一週邊,plan 才不會被切成兩個殘缺的假週邊。
        for sig in spec["signals"]:
            pre = _sig_prefix(sig)
            if pre and pre != inst and pre not in aliases:
                aliases[pre] = {
                    "node_label": ref,
                    "target_node": f"&{ref}",
                    "resolved_by": "signal-prefix-alias",
                    "canonical": inst,
                    "enabled_in_baseline": True,
                    "pinctrl_groups": spec["pinctrl_groups"],
                    "csv_pins": 0,
                    "pins_matched_in_baseline": 0,
                }

    # P2（2026-07-25 SPI1 事故）：覆蓋**全 instance**——SoC dtsi 有 node def
    # 但 baseline 未啟用的週邊（main_spi1..4、mcu_spi*…）也要進 alias，否則
    # DTS_agent 對新 instance 只能裸猜 &spi1（K3 label 是 main_spi1）。
    # 篩選：有 compatible 的 labeled node、非 pinctrl 群組、尚未收錄。
    from .dts_extract import _group_entries, _instance_name
    taken = {a["node_label"] for a in aliases.values()}
    for label, node in sorted(result.get("labels", {}).items()):
        if (label in taken or not node.props.get("compatible")
                or _group_entries(node)):
            continue
        inst = _instance_name(label, [])          # label 去 domain 前綴大寫
        if inst in aliases:
            continue                              # 名稱撞既有 instance:保守跳過
        aliases[inst] = {
            "node_label": label,
            "target_node": f"&{label}",
            "resolved_by": "soc-dtsi-def",
            "enabled_in_baseline": False,
            "pinctrl_groups": [],
            "csv_pins": 0,
            "pins_matched_in_baseline": 0,
        }

    return {
        "board": board,
        "description": (
            "Maps CSV peripheral name -> kernel DTS node label (&node). "
            "由 knowledge_extract dts 步驟從 baseline DTS 自動生成;"
            "resolved_by=soc-dtsi-def 的條目是 baseline 未啟用的 instance"
            "(P2:新需求的目標解析用)。"
        ),
        "source_dts": f"data/{board}/baseline/dts/",
        "aliases": dict(sorted(aliases.items())),
    }


def _gpio_pins(board: str, result: dict) -> dict:
    protected = set(result["gpio_pins"])
    af_used = set(result["signal_to_pin"].values())
    disabled_only = sorted({
        r["pin"] for r in result["gpio_records"]
        if not r["consumer_active"]
    } - protected - af_used)
    return {
        "board": board,
        "description": (
            "Protected GPIO pins reserved in the baseline DTS via "
            "*-gpios/gpios refs. `protected_pins` = reserved by an active "
            "consumer; `reserved_by_disabled_only` = reserved only by a "
            "disabled node (re-muxable only with care)."
        ),
        "source": f"data/{board}/baseline/dts/ (auto-extracted, status-aware)",
        "protected_pins": sorted(protected),
        "reserved_by_disabled_only": disabled_only,
        "count": len(protected) + len(disabled_only),
        "reservations": result["gpio_records"],
    }


def _board_config(board: str, result: dict) -> dict:
    peripherals = {}
    for ref, node in result["enabled"].items():
        status = (node.props.get("status") or "").strip('"')
        peripherals[ref] = {
            "family": family_of(ref),
            "status": status or "okay(default)",
            "board_knobs": _knobs(node),
            "deleted_properties": list(node.deleted),
            "child_devices": _child_devices(node),
        }
    return {
        "board": board,
        "description": (
            "Board-specific peripheral configuration from the baseline board "
            "DTS overrides (depth-1 non-pinctrl props + child devices + "
            "/delete-property directives). DTS-derived; nothing guessed."
        ),
        "source": f"data/{board}/baseline/dts/{result['baseline']}",
        "_needs_board_doc": [
            "connector_routing (which CN/header a peripheral routes to) — not in DTS.",
        ],
        "peripherals": dict(sorted(peripherals.items())),
    }


def _property_bindings(board: str, result: dict) -> dict:
    families: dict[str, dict] = {}
    for ref in result["overrides"]:
        if _INFRA_RE.match(ref):
            continue
        fam = family_of(ref)
        families.setdefault(fam, {"total_overrides": 0, "enabled_labels": []})
        families[fam]["total_overrides"] += 1
    for ref in result["enabled"]:
        fam = family_of(ref)
        families.setdefault(fam, {"total_overrides": 0, "enabled_labels": []})
        families[fam]["enabled_labels"].append(ref)

    for fam, spec in families.items():
        labels = spec["enabled_labels"]
        spec["enabled_instances"] = len(labels)
        props: dict[str, dict] = {}
        for ref in labels:
            for k, v in _knobs(result["enabled"][ref]).items():
                p = props.setdefault(k, {"occurrences": 0, "values": []})
                p["occurrences"] += 1
                if v is not None and v not in p["values"]:
                    p["values"].append(v)
        spec["extra_properties"] = {
            k: {
                "occurrences": p["occurrences"],
                "in_all_enabled_instances": p["occurrences"] == len(labels),
                "boolean_flag": not p["values"],
                "example_values": p["values"][:2],
            }
            for k, p in sorted(props.items())
        }
    return {
        "board": board,
        "description": (
            "Per-family non-pinctrl property template, aggregated from "
            "ENABLED board overrides (status-aware)."
        ),
        "source": f"data/{board}/baseline/dts/{result['baseline']}",
        "families": dict(sorted(families.items())),
    }


def _fixed_connections(board: str, result: dict, boot_groups: dict) -> dict:
    boot_refs = {bg["dts_label"] for bg in boot_groups.values()
                 if bg["solver_action"] == "emit_fixed_assignment"}
    connections = {}
    for ref, spec in result["peripherals"].items():
        pins = sorted({result["signal_to_pin"][s] for s in spec["signals"]
                       if s in result["signal_to_pin"]})
        ics = _child_devices(result["enabled"][ref])
        if not pins and not ics:
            continue
        boot_required = ref in boot_refs
        connections[ref] = {
            "peripheral": result["instances"][ref],
            "target_node": f"&{ref}",
            "family": family_of(ref),
            "enabled_in_baseline": True,
            "pinctrl_groups": spec["pinctrl_groups"],
            "pins": pins,
            "ics": ics,
            "boot_required": boot_required,
            "board_locked": False,
            "manageable": not boot_required,
        }
    return {
        "board": board,
        "description": (
            "Fixed peripheral connections physically wired on the board, "
            "extracted from the baseline board DTS. `manageable` = the Patch "
            "Agent may enable/disable this connection; boot-required "
            "connections are never disabled."
        ),
        "source": f"data/{board}/baseline/dts/{result['baseline']}",
        "generated_by": "knowledge_extract dts_generation",
        "connections": dict(sorted(connections.items())),
    }


def _boot_requirements(board: str, result: dict, boot_groups: dict) -> dict:
    soc = identify.get_soc(board) or board
    emit = {n: bg for n, bg in boot_groups.items()
            if bg["solver_action"] == "emit_fixed_assignment"}
    peripherals = {}
    for name, bg in boot_groups.items():
        key = bg["dts_label"] or name.lower()
        peripherals[key] = {
            "block": name,
            "why": bg["basis"],
            "signals": [r[0] for r in bg["pin_map"]],
            "dts_node": f"&{bg['dts_label']}" if bg["dts_label"] else None,
            "solver_action": bg["solver_action"],
        }
    return {
        "soc": soc,
        "source": (
            f"baseline DTS({result['baseline']})boot 節點判定 + "
            "手冊 boot 章節(require.json 草稿)交叉查證"
        ),
        "description": "開機必備(node 級,patch 消費;與 require.json 的腳位級判定同源)",
        "board_pin_locked": {
            "description": (
                "實體已繞線、必須鎖回官方腳位的開機介面;solver 會把這些腳"
                "從其他訊號的候選域移除。"
            ),
            "peripherals": sorted(emit),
            "why": "kernel 板 DTS 有效啟用(增項 A 判定為 emit_fixed_assignment)",
        },
        "peripherals": dict(sorted(peripherals.items())),
        "recommended_not_strictly_mandatory": {},
        "_review_status": "pending_human_review(與 require.json 的 emit/reserve 判定同批,須一併人工確認)",
    }


def _board_yaml(board: str, result: dict, out_root: str) -> str:
    vendor, arch_dir = _VENDOR_META[result["vendor"]]
    soc = identify.get_soc(board) or board
    has_cubemx = os.path.isfile(
        os.path.join(out_root, board, "base", "cubemx.json"))
    return (
        "# 板子策略檔——身份與驗證策略(knowledge_extract 自動產出)。\n"
        f"board_id: {board}\n"
        f"vendor: {vendor}\n"
        f"name: {soc}\n"
        "knowledge_base: .\n"
        f"kernel_dts_path: arch/arm64/boot/dts/{arch_dir}/{result['baseline']}\n"
        "validation:\n"
        f"  enabled: {'true' if has_cubemx else 'false'}\n"
        f"  type: {'cubemx' if has_cubemx else 'none'}\n"
        "  script: null\n"
    )


def write_all(
    board: str,
    result: dict,
    af_table: dict,
    boot_groups: dict,
    *,
    out_root: str,
    force: bool,
    log=print,
) -> dict:
    """產 dts_generation/ 六檔 + board.yaml,回傳 {檔名: written|skipped}。"""
    gen_dir = os.path.join(out_root, board, "dts_generation")
    files = {
        "peripheral_node_alias.json": _peripheral_node_alias(board, result),
        "gpio_pins.json": _gpio_pins(board, result),
        "board_config.json": _board_config(board, result),
        "dts_property_bindings.json": _property_bindings(board, result),
        "fixed_connections.json": _fixed_connections(board, result, boot_groups),
        "boot_requirements.json": _boot_requirements(board, result, boot_groups),
    }

    # Prompt 5:pinmux 渲染供料(僅非 ST 板;ST 缺檔 = DTS_agent 預設 stm32)
    from . import pad_supply

    supply_warns: list[str] = []
    style, pad_obj = pad_supply.build_supply(
        result["vendor"], board, result, af_table,
        result.get("pinfunc_path"), supply_warns)
    if style:
        files["pinmux_style.json"] = style
    if pad_obj:
        files["pad_params.json"] = pad_obj
    for w in supply_warns:
        log(f"[{board}] ⚠ {w}")
    result.setdefault("warnings", []).extend(supply_warns)
    report = {}
    for name, obj in files.items():
        report[f"dts_generation/{name}"] = _write_json(
            os.path.join(gen_dir, name), obj, force)

    yaml_path = os.path.join(out_root, board, "board.yaml")
    if os.path.exists(yaml_path) and not force:
        report["board.yaml"] = "skipped"
    else:
        with open(yaml_path, "w", encoding="utf-8") as f:
            f.write(_board_yaml(board, result, out_root))
        report["board.yaml"] = "written"

    for name, state in report.items():
        log(f"[{board}] {name}: {state}")
    return report
