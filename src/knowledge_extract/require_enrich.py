"""增項 A:require.json 的 boot 群組 action 判定與真實 AF 回填。

判定規則(upgrade_plan.md):
- boot 節點來源:DTS 的 bootph-* 屬性(TI K3)+ 手冊 boot 章節(既有草稿群組)
- 該周邊在 kernel 板 DTS 有效啟用 → emit_fixed_assignment(plan 自動帶上)
- 只有 bootloader 用 / kernel 未啟用 → reserve_only(移出候選域)
- pin_map 三元組 [signal, pin, af]:signal/pin 對齊 af_table、AF 填真值

安全邊界:只修改 profile_status == "needs_confirmation" 的草稿群組;
人工定案的群組(如 stm32 手工檔)一律不動,僅在 _review 報告差異。
每個被修改的群組附 _review(判定依據+信心),整檔標 pending_human_review。
"""
from __future__ import annotations

import json
import os
import re

from .paths import DATA_DIR, LIVE_DATA_DIR

_BOOTPH_PROPS = ("bootph-all", "bootph-pre-ram", "bootph-some-ram")


def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]", "", (s or "").lower())


def _bootph_labels(result: dict) -> dict[str, str]:
    """override 子樹帶 bootph-* 屬性的周邊 label → 命中的屬性名。"""
    out: dict[str, str] = {}
    for ref, node in result["overrides"].items():
        for sub in node.walk():
            hit = next((p for p in _BOOTPH_PROPS if p in sub.props), None)
            if hit:
                out[ref] = hit
                break
    return out


def _boot_candidates(result: dict) -> dict[str, str]:
    """bootph 之外的資料驅動 boot 候選：{ref: 判定依據}。

    2026-07-25 am6548 事故：AM654 年代的 kernel DTS 沒有 bootph-* 屬性，
    僅靠 bootph 的補群組什麼都補不進來 → 開機組整批缺席。兩個與 vendor
    無關的資料來源補上：
      1. console —— baseline 的 /chosen stdout-path（經 /aliases 解 ref）；
      2. boot media —— enabled 週邊的 compatible 含 mmc/sdhci/sdmmc。
    只提名 result['enabled'] 內的節點（kernel 會驅動 → emit 候選）。"""
    cands: dict[str, str] = {}
    tree = result.get("trees", {}).get(result.get("baseline"))

    # 1. console：chosen.stdout-path = "serial2:115200n8" 或直接 &label
    chosen_val, aliases = None, {}
    for top in (tree.children if tree is not None else []):
        for node in top.walk():
            if node.name == "chosen" and "stdout-path" in node.props:
                chosen_val = node.props["stdout-path"].strip().strip('"')
            elif node.name == "aliases":
                for k, v in node.props.items():
                    m = re.search(r"&([\w-]+)", v or "")
                    if m:
                        aliases[k] = m.group(1)
    if chosen_val:
        m = re.match(r"&([\w-]+)", chosen_val)
        ref = m.group(1) if m else aliases.get(chosen_val.split(":", 1)[0])
        if ref and ref in result["enabled"]:
            cands[ref] = f"/chosen stdout-path 指向 &{ref}（開機 console）"

    # 2. boot media：mmc/sdhci 類 compatible（eMMC/SD 是主流開機媒體）
    for ref in result["enabled"]:
        node = result.get("labels", {}).get(ref)
        compat = ((node.props.get("compatible") if node is not None else "")
                  or "").lower()
        if any(t in compat for t in ("sdhci", "mmc", "sdmmc")):
            cands.setdefault(
                ref, f"compatible {compat.strip()} ＝開機媒體（eMMC/SD）且有效啟用")
    return cands


def _dts_pin_map(ref: str, result: dict) -> list[list]:
    """周邊在 baseline DTS 的官方腳位 → [[signal, pin, af], …]。"""
    rows = []
    for sig in result["peripherals"][ref]["signals"]:
        pin = result["signal_to_pin"].get(sig)
        af = result["signal_af"].get(sig)
        if pin is not None and af is not None:   # R4 之後 af 必為真值整數
            rows.append([sig, pin, af])
    return rows


def _true_af(af_table: dict, signal: str, pin: str) -> int | None:
    """由 af_table 反查 (signal, pin) 的真實 mux;查無回 None。"""
    canon = {p.upper(): p for p in af_table}
    muxes = af_table.get(canon.get((pin or "").upper(), ""))
    for mux, cell in (muxes or {}).items():
        if signal.upper() in [s.strip().upper() for s in cell.split("/")]:
            return int(mux)
    return None


def _match_group(name: str, spec: dict, result: dict) -> str | None:
    """把 require 群組對到 DTS 周邊 label:先比 pin_map 訊號交集,再比名稱。"""
    group_sigs = {str(r[0]).upper() for r in (spec.get("pin_map") or [])}
    best, best_hit = None, 0
    for ref, p in result["peripherals"].items():
        hit = len(group_sigs & {s.upper() for s in p["signals"]})
        if hit > best_hit:
            best, best_hit = ref, hit
    if best:
        return best
    for ref in result["peripherals"]:
        if _norm(name) in (_norm(result["instances"][ref]), _norm(ref)):
            return ref
    return None


def enrich(
    board: str,
    result: dict,
    af_table: dict,
    *,
    out_root: str,
    log=print,
) -> dict:
    """回傳 boot 群組總表(給 dts_generation / lint 共用),並回寫 require.json。

    boot_groups: {群組名: {solver_action, dts_label, instance, pin_map,
                           basis, confidence, modified}}
    """
    # copy-on-write(D3):讀可以 fallback 到正式知識庫,**回寫一律寫 out_root**
    # ——extractor 永不寫 data/<board>/。
    req_path = os.path.join(out_root, board, "base", "require.json")
    src_path = req_path
    if not os.path.isfile(src_path):
        src_path = os.path.join(LIVE_DATA_DIR, board, "base", "require.json")
    if not os.path.isfile(src_path):
        log(f"[{board}] 無 require.json,跳過增項 A")
        return {}
    with open(src_path, "r", encoding="utf-8") as f:
        require = json.load(f)
    groups = require.get("boot_pin_locked", {}).get("groups", {})

    bootph = _bootph_labels(result)
    boot_groups: dict[str, dict] = {}
    changed = False

    for name, spec in groups.items():
        ref = _match_group(name, spec, result)
        enabled = ref in result["enabled"] if ref else False
        draft = spec.get("profile_status") == "needs_confirmation"

        if enabled:
            action = "emit_fixed_assignment"
            basis = (f"kernel 板 DTS &{ref} 有效啟用"
                     + (f"(帶 {bootph[ref]})" if ref in bootph else "")
                     + ",kernel 會驅動 → plan 必帶")
            confidence = "high" if (spec.get("pin_map") or ref in bootph) \
                else "medium"
            pin_map = _dts_pin_map(ref, result)
        else:
            action = "reserve_only"
            basis = ("DTS 無對應節點或未啟用——視為 bootloader/strap 持有,"
                     "kernel 不碰" if ref is None else
                     f"對應 &{ref} 在 baseline DTS 未啟用")
            confidence = "medium" if spec.get("pin_map") else "low"
            # 修正既有 pin_map 的 AF 佔位值(不准 0 佔位 → 回填真值);
            # af_table 查不到的列剔除(R3/R7:不留空欄或存疑值)
            pin_map, dropped = [], []
            for row in spec.get("pin_map") or []:
                sig, pin = str(row[0]), str(row[1])
                true_af = _true_af(af_table, sig, pin)
                if true_af is None:
                    dropped.append(f"{sig}@{pin}")
                else:
                    pin_map.append([sig, pin, true_af])
            if dropped:
                basis += f";剔除 af_table 查不到的列 {dropped}(需人工補表)"

        boot_groups[name] = {
            "solver_action": action,
            "dts_label": ref if enabled or ref else None,
            "instance": result["instances"].get(ref) if ref else None,
            "pin_map": pin_map,
            "basis": basis,
            "confidence": confidence,
            "modified": False,
        }

        if not draft:
            # 人工定案的群組不動;判定不一致時留給 REVIEW.md 報告
            boot_groups[name]["locked"] = True
            boot_groups[name]["existing_action"] = spec.get("solver_action")
            boot_groups[name]["pin_map"] = spec.get("pin_map") or pin_map
            continue

        new_spec_bits = {
            "solver_action": action,
            "in_kernel_dt": enabled,
            "_review": {"basis": basis, "confidence": confidence},
        }
        same_map = {tuple(map(str, r)) for r in (spec.get("pin_map") or [])} \
            == {tuple(map(str, r)) for r in pin_map}
        if not same_map and pin_map:
            new_spec_bits["pin_map"] = pin_map
        before = {k: spec.get(k) for k in new_spec_bits}
        if any(spec.get(k) != v for k, v in new_spec_bits.items()):
            spec.update(new_spec_bits)
            changed = True
            boot_groups[name]["modified"] = True
            log(f"[{board}] require: {name} → {action}"
                f"(原 {before.get('solver_action')},pin_map {len(pin_map)} 筆)")

    # boot 節點沒被任何群組涵蓋 → 補新群組(僅在檔案是草稿時)。
    # 候選來源：bootph-*（高信度）＋ 資料驅動 fallback（console/boot media，
    # 2026-07-25 事故修正——AM654 年代 DTS 無 bootph，僅靠它會整批漏補）。
    any_draft = (any(g.get("profile_status") == "needs_confirmation"
                     for g in groups.values())
                 or not groups)          # 群組全空的草稿檔也允許補
    supplements: dict[str, tuple[str, str]] = {}
    for ref, prop in bootph.items():
        supplements[ref] = (f"&{ref} 帶 {prop} 且有效啟用", "high")
    for ref, basis in _boot_candidates(result).items():
        supplements.setdefault(ref, (basis, "medium"))

    for ref, (basis, confidence) in supplements.items():
        covered = any(bg.get("dts_label") == ref for bg in boot_groups.values())
        if covered or not any_draft or ref not in result["enabled"]:
            continue
        inst = result["instances"].get(ref, ref.upper())
        pin_map = _dts_pin_map(ref, result)
        if not pin_map:
            log(f"[{board}] require: boot 候選 {inst}(&{ref}) 無可用 pin_map,"
                "跳過(訊號未進 af_table?)")
            continue
        groups[inst] = {
            "role": f"開機必備節點(&{ref})",
            "in_kernel_dt": True,
            "solver_action": "emit_fixed_assignment",
            "profile_status": "needs_confirmation",
            "pin_map": pin_map,
            "_review": {"basis": basis, "confidence": confidence},
        }
        boot_groups[inst] = {
            "solver_action": "emit_fixed_assignment", "dts_label": ref,
            "instance": inst, "pin_map": pin_map,
            "basis": basis, "confidence": confidence,
            "modified": True,
        }
        changed = True
        log(f"[{board}] require: 新增 boot 群組 {inst}(&{ref})——{basis}")

    if changed or src_path != req_path:
        require.setdefault("boot_pin_locked", {})["_review_status"] = \
            "pending_human_review(dts 步驟自動判定 action/AF——emit/reserve " \
            "分錯會導致開機組缺席或 kernel 搶 bootloader 的腳,務必人工確認)"
        os.makedirs(os.path.dirname(req_path), exist_ok=True)
        with open(req_path, "w", encoding="utf-8") as f:
            json.dump(require, f, ensure_ascii=False, indent=1)
            f.write("\n")
        log(f"[{board}] require.json 已回寫({req_path})")

    return boot_groups
