#!/usr/bin/env python3
"""Extract fixed_connections.json from the baseline DTS (plan.md v3 §3.3).

A "fixed peripheral connection" is a physically-wired link on the board:
a pin-managed peripheral controller (USART/I2C/SPI/SDMMC/FDCAN/ETH/OCTOSPI…)
plus whatever slave ICs hang off it in the baseline board DTS (PHY, camera,
eMMC, …). The file answers three questions per connection, deterministically:

  1. what is wired (controller node, pinctrl groups, pins, child ICs)
  2. may the Patch Agent manage it (enable/disable via plan)  -> `manageable`
  3. what binding info is needed to enable it (IC compatible / reg / props)

`manageable` is false when the node is boot-required or board-pin-locked
(boot sources), per data/<board>/dts_generation/boot_requirements.json
(config.REQUIRE; the former patch-side require.json — NOT the solver's
unrelated base/require.json). Everything comes from the DTS + that file;
nothing is guessed.

Run:  PYTHONPATH=src python3 tools/extract_fixed_connections.py
"""
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from patch_agent import config
from patch_agent.m1_dts_parser import build_index
from patch_agent.m1_dts_parser.index import family_of
from patch_agent.m2_validation_harness.harness import _boot_required_nodes, _locked_nodes


def _child_ics(node):
    """Flatten the IC-bearing children of a controller override (compatible-bearing
    nodes at any depth; mdio buses etc. are kept as the path prefix)."""
    out = []

    def rec(nd, path):
        p = path + [nd.name]
        compat = None
        raw = nd.props.get("compatible")
        if raw:
            compat = raw.strip().strip('"').split('"')[0]
        if compat:
            out.append({
                "path": "/".join(p),
                "label": nd.label,
                "compatible": compat,
                "reg": nd.props.get("reg"),
                "status": nd.status,
                "props": {k: v for k, v in nd.props.items()
                          if k not in ("compatible", "reg", "status")},
            })
        for c in nd.children:
            rec(c, p)

    for c in node.children:
        rec(c, [])
    return out


def extract(index=None, require=None, alias=None):
    index = index or build_index()
    require = require or json.load(open(config.REQUIRE))
    alias = alias or json.load(open(config.PERIPHERAL_NODE_ALIAS))
    alias_map = alias.get("aliases", {})

    boot_nodes = _boot_required_nodes(require)
    locked = require.get("board_pin_locked", {}).get("peripherals", [])
    locked_nodes = _locked_nodes(locked, alias_map)

    # node label -> plan-facing peripheral name (reverse alias)
    label_to_name = {v.get("node_label"): k for k, v in alias_map.items()}

    connections = {}
    for label, ov in sorted(index.overrides.items()):
        fam = family_of(label)
        if fam is None:
            continue                      # not a pin-managed peripheral controller
        # skip pinctrl-group overrides that happen to live in the board file
        if index.group(label):
            continue
        pins = sorted({e.pin for gl in ov.pinctrl_0
                       for e in (index.group(gl).entries if index.group(gl) else [])})
        boot = label in boot_nodes
        lock = label in locked_nodes
        nd_raw = None
        for nd in index.files.get(config.BOARD_DTS.name, []):
            if nd.ref == label:
                nd_raw = nd
                break
        connections[label] = {
            "peripheral": label_to_name.get(label),
            "target_node": f"&{label}",
            "family": fam,
            "enabled_in_baseline": ov.enabled,
            "pinctrl_groups": list(ov.pinctrl_0),
            "pins": pins,
            "ics": _child_ics(nd_raw) if nd_raw else [],
            "boot_required": boot,
            "board_locked": lock,
            "manageable": not (boot or lock),
        }
    return {
        "board": config.BOARD,
        "description": "Fixed peripheral connections physically wired on the board, "
                       "extracted from the baseline board DTS. `manageable` = the Patch "
                       "Agent may enable/disable this connection from plan.csv content; "
                       "boot-required / board-locked connections are never disabled.",
        "source": str(config.BOARD_DTS.relative_to(config.REPO_ROOT)),
        "generated_by": "tools/extract_fixed_connections.py",
        "connections": connections,
    }


def main():
    out = extract()
    path = config.FIXED_CONNECTIONS
    path.parent.mkdir(parents=True, exist_ok=True)
    json.dump(out, open(path, "w"), indent=2, ensure_ascii=False)
    print(f"wrote {path} ({len(out['connections'])} connections)")
    for label, c in out["connections"].items():
        tag = "manageable" if c["manageable"] else \
              ("boot-required" if c["boot_required"] else "board-locked")
        ics = ", ".join(i["compatible"] for i in c["ics"]) or "-"
        print(f"  {label:12s} {c['family']:8s} enabled={str(c['enabled_in_baseline']):5s} "
              f"{tag:13s} ics: {ics}")


if __name__ == "__main__":
    main()
