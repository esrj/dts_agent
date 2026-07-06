#!/usr/bin/env python3
"""Derive the peripheral -> DTS-node-label alias map for STM32MP257F-EV1.

WHY this exists: the CSVs name peripherals like FDCAN1 / OCTOSPIM / PCIE, but the
device tree labels them &m_can1 / &ommanager / &pcie_rc. Stage 2 (localization)
must translate CSV peripheral -> real &node before it can find anything.

HOW it derives (no hard-coded guesses):
  1. Parse every pinctrl GROUP in stm32mp25-pinctrl.dtsi -> set of (pin, af).
  2. Parse every enabled (&node { status="okay" }) override in the board .dts and
     collect the pinctrl-0 groups it references (including nested child nodes).
  3. For each peripheral in the CSVs, find the enabled node whose referenced
     groups cover the CSV's (pin, af) set. That node IS the alias.

Output: data/<board>/dts_generation/peripheral_node_alias.json

NOTE (merged repo): output/plan/plan.csv is now the LIVE solver output —
every /api/solve overwrites it. Regenerating this alias map therefore
depends on whatever was last solved; the committed file may contain
entries derived from historical plans (e.g. I2C1) that a fresh run would
drop. Diff before overwriting the committed knowledge file.
"""
import re, csv, os, json, sys

BOARD_DIR = os.path.join(os.path.dirname(__file__), "..", "data", "stm32mp257f-ev1")
DTS = os.path.join(BOARD_DIR, "baseline", "dts")
PINCTRL = os.path.join(DTS, "stm32mp25-pinctrl.dtsi")
EV1 = os.path.join(DTS, "stm32mp257f-ev1.dts")
CSVS = [os.path.join(BOARD_DIR, "baseline", "baseline.csv"),
        os.path.join(os.path.dirname(__file__), "..", "output", "plan", "plan.csv")]
OUT = os.path.join(BOARD_DIR, "dts_generation", "peripheral_node_alias.json")

AF = {'GPIO': -1, 'ANALOG': -2}
for i in range(16):
    AF[f'AF{i}'] = i


def brace_blocks(text, pattern):
    """Yield (label, body) for each `<label> ... {` with brace-matched body."""
    for m in re.finditer(pattern, text):
        s = m.end(); depth = 1; i = s
        while i < len(text) and depth > 0:
            depth += text[i] == '{'
            depth -= text[i] == '}'
            i += 1
        yield m.group(1), text[s:i - 1]


def parse_pinctrl_groups(text):
    groups = {}
    for label, body in brace_blocks(text, r'(\w+):\s*[\w-]+\s*\{'):
        pins = set()
        for pm in re.finditer(r"STM32_PINMUX\('([A-Z])',\s*(\d+),\s*(\w+)\)", body):
            pins.add((f"P{pm.group(1)}{int(pm.group(2))}", AF.get(pm.group(3), '?')))
        if pins:
            groups[label] = pins
    return groups


def parse_enabled_nodes(text):
    node_groups = {}
    for label, body in brace_blocks(text, r'&(\w+)\s*\{'):
        if 'status = "okay"' not in body:
            continue
        grps = [g for d in re.findall(r'pinctrl-0\s*=\s*<([^>]*)>', body)
                for g in re.findall(r'&(\w+)', d)]
        node_groups.setdefault(label, []).extend(grps)
    return node_groups


def all_node_labels(paths):
    """Every node label DEFINED (`label: node@addr {`) anywhere in the DTS tree,
    regardless of enabled/disabled status."""
    labels = set()
    for p in paths:
        text = open(p).read()
        for m in re.finditer(r'(?m)^\s*(\w+)\s*:\s*[\w@,.+-]+\s*\{', text):
            labels.add(m.group(1))
    return labels


def read_peripherals(paths):
    peris = {}  # peripheral -> set of (pin, af)
    for p in paths:
        if not os.path.exists(p):
            continue
        for r in csv.DictReader(open(p, encoding='utf-8-sig')):
            if not r.get('peripheral'):
                continue
            peris.setdefault(r['peripheral'].strip().upper(), set()).add(
                (r['pin'].strip(), int(r['af'])))
    return peris


def main():
    all_src = [os.path.join(DTS, f) for f in os.listdir(DTS) if f.endswith(('.dts', '.dtsi'))]
    groups = parse_pinctrl_groups(open(PINCTRL).read())
    node_groups = parse_enabled_nodes(open(EV1).read())   # enabled (status=okay) board overrides
    labels = all_node_labels(all_src)                     # every node label defined anywhere
    # node -> all pins reachable via its enabled pinctrl-0 groups
    node_pins = {n: set().union(*[groups.get(g, set()) for g in gs]) if gs else set()
                 for n, gs in node_groups.items()}
    peris = read_peripherals(CSVS)

    result = {}
    for per, want in sorted(peris.items()):
        same = per.lower()
        # 1. same-name node defined in the tree wins (e.g. I2C1 -> &i2c1, even if disabled)
        if same in labels:
            best, how = same, "same-name"
        else:
            # 2. renamed peripheral: pick the enabled node whose pins cover the CSV's
            best, cov, how = None, 0, "unresolved"
            for node in node_groups:
                c = len(want & node_pins[node])
                if c > cov:
                    best, cov, how = node, c, "pin-match"
        enabled = best in node_groups
        result[per] = {
            "node_label": best,
            "target_node": f"&{best}" if best else None,
            "resolved_by": how,
            "enabled_in_baseline": enabled,
            "pinctrl_groups": sorted(set(node_groups.get(best, []))),
            "csv_pins": len(want),
            "pins_matched_in_baseline": len(want & node_pins.get(best, set())),
        }

    doc = {
        "board": "stm32mp257f-ev1",
        "description": "Maps CSV peripheral name -> kernel DTS node label (&node). "
                       "Derived from baseline DTS by tools/derive_alias.py.",
        "source_dts": "data/stm32mp257f-ev1/baseline/dts/",
        "aliases": result,
    }
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    json.dump(doc, open(OUT, "w"), indent=2, ensure_ascii=False)
    print(f"wrote {OUT}\n")
    for per, info in result.items():
        flag = "OK " if info["target_node"] else "?? "
        en = "enabled" if info["enabled_in_baseline"] else "disabled"
        print(f"  {flag}{per:9} -> {info['target_node'] or '???':12} "
              f"[{info['resolved_by']:9} {en}]  baseline_pins={info['pins_matched_in_baseline']}/{info['csv_pins']}")


if __name__ == "__main__":
    main()
