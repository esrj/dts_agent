#!/usr/bin/env python3
"""Deterministically extract board-level data from the baseline DTS into:
  - gpio_pins.json            protected GPIO pins (every pin reservation)
  - board_config.json         board-specific knobs per peripheral
  - dts_property_bindings.json per-family non-pinctrl property template

Everything is sourced from data/<board>/baseline/dts/ ; nothing is invented.
The DTS parser now lives in the shared M1 module (src/patch_agent/m1_dts_parser);
this tool only holds the extraction/aggregation logic on top of it.

Run:  python3 tools/extract_board_data.py
"""
import os, re, json, sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
from patch_agent.m1_dts_parser import parse_dts, walk, family_of, gpio_pin, compat_primary
from patch_agent.m1_dts_parser.parser import GPIO_REF

BOARD = "stm32mp257f-ev1"
ROOT = os.path.join(os.path.dirname(__file__), "..", "data", BOARD)
DTS = os.path.join(ROOT, "baseline", "dts")
BOARD_FILE = "stm32mp257f-ev1.dts"
SOC_FILE = "stm32mp251.dtsi"          # where peripheral nodes are defined

# --------------------------------------------------------------------------
# Extractor 1: gpio_pins
# --------------------------------------------------------------------------
def extract_gpio_pins(trees_by_file):
    reservations = []
    for fn, nodes in trees_by_file.items():
        for nd, active in walk(nodes):
            owner = nd.ident
            # (a) value-form refs in any property: &gpioX N  (gpios/cs-gpios/
            #     reset-gpios/interrupts-extended/...)
            for key, val in nd.props.items():
                if val is None:
                    continue
                for m in GPIO_REF.finditer(val):
                    reservations.append(_resv(fn, nd, owner, active, key,
                                              gpio_pin(*m.groups()), m.group(1), m.group(2)))
            # (b) split interrupt form: interrupt-parent=<&gpioX> + interrupts=<line ...>
            ip = nd.props.get('interrupt-parent')
            ints = nd.props.get('interrupts')
            if ip and ints:
                pm = re.search(r'&gpio([a-z]+)', ip)
                lm = re.search(r'<\s*(\d+)', ints)
                if pm and lm and not GPIO_REF.search(ip):  # bank phandle w/o inline line
                    reservations.append(_resv(fn, nd, owner, active, 'interrupts',
                                              gpio_pin(pm.group(1), lm.group(1)),
                                              pm.group(1), lm.group(1)))
    by_pin = {}
    for r in reservations:
        by_pin.setdefault(r["pin"], []).append(r)
    active_pins = sorted([p for p, rs in by_pin.items() if any(r["consumer_active"] for r in rs)],
                         key=lambda p: (p[1], int(p[2:])))
    disabled_only = sorted([p for p, rs in by_pin.items() if not any(r["consumer_active"] for r in rs)],
                           key=lambda p: (p[1], int(p[2:])))
    return {
        "board": BOARD,
        "description": "Protected GPIO pins reserved in the baseline DTS via "
                       "*-gpios/gpios/cs-gpios refs and GPIO-as-interrupt lines. "
                       "These must NOT be re-muxed to a peripheral AF. "
                       "`protected_pins` = reserved by an active consumer; "
                       "`reserved_by_disabled_only` = reserved only by a disabled "
                       "node (re-muxable only with care).",
        "source": "data/%s/baseline/dts/ (auto-extracted, status-aware)" % BOARD,
        "protected_pins": active_pins,
        "reserved_by_disabled_only": disabled_only,
        "count": len(active_pins),
        "reservations": sorted(reservations, key=lambda r: (r["pin"], r["source"])),
    }

def _resv(fn, nd, owner, active, prop, pin, bank, line):
    return {
        "pin": pin, "bank": f"gpio{bank}", "line": int(line),
        "property": prop, "consumer_node": owner,
        "consumer_active": active,
        "consumer_compatible": compat_primary(nd.props.get('compatible')),
        "source": f"{fn}:{nd.line}",
    }

# --------------------------------------------------------------------------
# Extractors 2 & 3: board_config + dts_property_bindings
# --------------------------------------------------------------------------
SKIP_PROPS = {'status'}
def is_pinctrl(k):
    return k.startswith('pinctrl')

def child_summary(nd, depth=3):
    out = []
    for c in nd.children:
        knobs = {k: v for k, v in c.props.items()
                 if not is_pinctrl(k) and k not in SKIP_PROPS and not k.startswith('#')}
        entry = {
            "name": c.name, "label": c.label,
            "compatible": compat_primary(c.props.get('compatible')),
            "status": c.status,
            "props": knobs,
        }
        nested = child_summary(c, depth - 1) if depth > 1 and c.children else []
        if nested:
            entry["children"] = nested
        out.append(entry)
    return out

def extract_board_config_and_bindings(board_nodes, soc_nodes):
    # gather top-level &overrides of pinmux families from the board file
    overrides = [(nd, fam) for nd in board_nodes
                 if nd.ref and (fam := family_of(nd.ref))]

    # ---- board_config ----
    peripherals = {}
    for nd, fam in sorted(overrides, key=lambda x: x[0].ref):
        knobs = {k: v for k, v in nd.props.items()
                 if not is_pinctrl(k) and k not in SKIP_PROPS}
        children = [c for c in child_summary(nd) if c["props"] or c["compatible"]]
        if not knobs and not nd.deletions and not children:
            continue
        peripherals[nd.ref] = {
            "family": fam,
            "status": nd.status,
            "source_line": nd.line,
            "board_knobs": knobs,
            "deleted_properties": nd.deletions,
            "child_devices": children,
        }
    board_config = {
        "board": BOARD,
        "description": "Board-specific peripheral configuration from the baseline "
                       "board DTS overrides (all depth-1 non-pinctrl props + child "
                       "slave devices + /delete-property directives). DTS-derived; "
                       "nothing guessed. `status` reflects the override.",
        "source": "data/%s/baseline/dts/%s" % (BOARD, BOARD_FILE),
        "_needs_board_doc": [
            "connector_routing (which CN/header a peripheral routes to) — not in DTS.",
            "alternate pin choices for peripherals not already wired on the board.",
        ],
        "peripherals": peripherals,
    }

    # ---- bindings (status-aware, every family represented) ----
    fam_nodes = {}
    for nd, fam in overrides:
        fam_nodes.setdefault(fam, []).append(nd)

    # SoC structural props per family (read node definitions)
    soc_struct = {}
    for nd, _ in walk(soc_nodes):
        fam = family_of(nd.label)
        if fam and fam not in soc_struct:
            struct = {k: nd.props.get(k) for k in
                      ('#address-cells', '#size-cells', '#interrupt-cells')
                      if k in nd.props}
            if struct:
                soc_struct[fam] = {
                    "defined_in": f"{SOC_FILE}:{nd.line}",
                    "example_label": nd.label,
                    "structural_cells": struct,
                    "has_reg": 'reg' in nd.props,
                    "has_compatible": 'compatible' in nd.props,
                }

    families = {}
    for fam, nds in sorted(fam_nodes.items()):
        enabled = [n for n in nds if n.status != 'disabled']
        keys = {}
        for n in enabled:
            for k, v in n.props.items():
                if is_pinctrl(k) or k in SKIP_PROPS:
                    continue
                info = keys.setdefault(k, {"count": 0, "examples": set(), "boolean": v is None})
                info["count"] += 1
                if v is not None:
                    info["examples"].add(v[:80])
        families[fam] = {
            "total_overrides": len(nds),
            "enabled_instances": len(enabled),
            "enabled_labels": sorted(n.ref for n in enabled),
            "extra_properties": {
                k: {
                    "occurrences": info["count"],
                    "in_all_enabled_instances": len(enabled) > 0 and info["count"] >= len(enabled),
                    "boolean_flag": info["boolean"],
                    "example_values": sorted(info["examples"])[:5],
                }
                for k, info in sorted(keys.items())
            },
            "soc_definition_props": soc_struct.get(fam),
        }
    bindings = {
        "board": BOARD,
        "description": "Per-family non-pinctrl property template, aggregated from "
                       "ENABLED board overrides (status-aware). Empty "
                       "extra_properties means the family's nodes carry only "
                       "pinctrl+status on this board. `soc_definition_props` lists "
                       "structural cells from the SoC node definition that a "
                       "generated child-bearing node must (re)declare.",
        "source": "data/%s/baseline/dts/{%s,%s}" % (BOARD, BOARD_FILE, SOC_FILE),
        "families": families,
    }
    return board_config, bindings

# --------------------------------------------------------------------------
def main():
    trees = {}
    for fn in sorted(f for f in os.listdir(DTS) if f.endswith(('.dts', '.dtsi'))):
        trees[fn] = parse_dts(open(os.path.join(DTS, fn)).read())

    gpio = extract_gpio_pins(trees)
    board_config, bindings = extract_board_config_and_bindings(
        trees[BOARD_FILE], trees[SOC_FILE])

    # All three are DTS-patch derived data -> dts_generation/ (merged layout;
    # gpio_pins moved there from the old flat top level, see data/README.md)
    GEN = os.path.join(ROOT, "dts_generation")
    os.makedirs(GEN, exist_ok=True)
    for path, doc in [(os.path.join(GEN, "gpio_pins.json"), gpio),
                      (os.path.join(GEN, "board_config.json"), board_config),
                      (os.path.join(GEN, "dts_property_bindings.json"), bindings)]:
        json.dump(doc, open(path, "w"), indent=2, ensure_ascii=False)
        print("wrote", os.path.relpath(path, ROOT))

    print(f"\ngpio_pins: {gpio['count']} protected -> {gpio['protected_pins']}")
    print(f"           disabled-only -> {gpio['reserved_by_disabled_only']}")
    print(f"board_config: {len(board_config['peripherals'])} peripherals")
    print(f"bindings: families -> {list(bindings['families'])}")

if __name__ == "__main__":
    main()
