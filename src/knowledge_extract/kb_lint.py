"""增項 E:產出自我驗收(lint)+ REVIEW.md 待審查清單。

分級:
- FAIL —— 資料自相矛盾(pin_map 的 AF 與 af_table 衝突、baseline.csv 撞腳、
  schema 錯、cpp 展開失敗、啟用周邊引用的 &label 找不到)→ 不出貨
- WARN —— 已知且已標注的缺口(af_table 缺 pad/mux 的聯集條目、上游 DTS bug)
  → 列入 REVIEW.md,人工簽核後放行

與 DTS_agent 端的 kb lint 對齊(規則以 upgrade_plan.md 增項 E 為準)。
"""
from __future__ import annotations

import csv
import json
import os
import re
import subprocess
from datetime import datetime

from .paths import DATA_DIR, LIVE_DATA_DIR

_GEN_REQUIRED_KEYS = {
    "peripheral_node_alias.json": ["aliases"],
    "gpio_pins.json": ["protected_pins", "reserved_by_disabled_only"],
    "board_config.json": ["peripherals"],
    "dts_property_bindings.json": ["families"],
    "fixed_connections.json": ["connections"],
    "boot_requirements.json": ["board_pin_locked", "peripherals"],
}
_ACTIONS = {"emit_fixed_assignment", "reserve_only"}


def _af_index(af_table: dict):
    """(pin 大寫→muxes, 訊號大寫集合, 訊號原文集合, 大寫→原文對照)。

    P5(2026-07-25 UART0_CTSn 事故):Σ 以 af_table **原文**為準——TI 訊號含
    小寫(CTSn/RTSn),輸出檔把它大寫化會讓 DTS_agent 端對不上。lint 因此
    要能區分「不在 Σ」與「大小寫不一致」兩種錯。"""
    pins = {p.upper(): m for p, m in af_table.items()}
    exact = {
        s.strip()
        for m in af_table.values()
        for cell in m.values()
        for s in cell.split("/") if s.strip()
    }
    canon: dict[str, str] = {}
    for s in sorted(exact):
        canon.setdefault(s.upper(), s)
    return pins, set(canon), exact, canon


def _true_af(pins: dict, sig: str, pin: str) -> int | None:
    for mux, cell in (pins.get(pin.upper()) or {}).items():
        if sig.upper() in [s.strip().upper() for s in cell.split("/")]:
            return int(mux)
    return None




def run(
    board: str,
    result: dict,
    af_table: dict,
    boot_groups: dict,
    *,
    out_root: str,
    log=print,
    af_diff: dict | None = None,
) -> dict:
    fails: list[str] = []
    warns: list[str] = []
    pins_idx, sigma, sigma_exact, sigma_canon = _af_index(af_table)
    board_dir = os.path.join(out_root, board)

    def _case_check(where: str, sig: str) -> bool:
        """P5:sig 大寫命中 Σ 但原文不中 → 大小寫不一致(FAIL)。回傳是否通過。"""
        if sig in sigma_exact:
            return True
        if sig.upper() in sigma:
            fails.append(f"{where}:{sig} 與 af_table 大小寫不一致"
                         f"(Σ 原文為 {sigma_canon[sig.upper()]})")
            return False
        return True          # 完全不在 Σ:交由既有「不在 Σ」檢查回報

    def _load(rel):
        path = os.path.join(board_dir, rel)
        if not os.path.isfile(path) and out_root != LIVE_DATA_DIR:
            path = os.path.join(LIVE_DATA_DIR, board, rel)   # 唯讀 fallback(D3)
        if not os.path.isfile(path):
            return None
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)

    # ---- 1. 交叉一致性 -------------------------------------------------- #
    require = _load(os.path.join("base", "require.json")) or {}
    for name, spec in (require.get("boot_pin_locked", {})
                       .get("groups", {})).items():
        action = spec.get("solver_action")
        if action not in _ACTIONS:
            fails.append(f"require:{name} solver_action 非法({action})")
        for row in spec.get("pin_map") or []:
            sig, pin, af = str(row[0]), str(row[1]), row[2]
            if sig.upper() not in sigma:
                fails.append(f"require:{name} 訊號 {sig} 不在 af_table 的 Σ")
                continue
            if not _case_check(f"require:{name}", sig):
                continue
            if pin.upper() not in pins_idx:
                fails.append(f"require:{name} pin {pin} 不在 af_table")
                continue
            true = _true_af(pins_idx, sig, pin)
            if af is None or not re.fullmatch(r"\d+", str(af)):
                fails.append(f"require:{name} [{sig},{pin}] AF 非整數({af!r})")
            elif true is None:
                fails.append(f"require:{name} {sig} 不是 {pin} 的合法功能")
            elif int(af) != true:
                fails.append(f"require:{name} [{sig},{pin}] AF={af} 與 "
                             f"af_table 真值 {true} 矛盾")

    # R7「Σ 完整性」:同源生成後輸出不該再有 af_table 查不到的條目——違反即 FAIL
    s2p = _load(os.path.join("dts", "signal_to_pin.json")) or {}
    for sig, pin in (s2p.get("signal_to_pin") or {}).items():
        if sig.upper() not in sigma:
            fails.append(f"signal_to_pin:{sig} 不在 af_table 的 Σ")
        elif not _case_check("signal_to_pin", sig):
            pass
        elif pin.upper() not in pins_idx:
            fails.append(f"signal_to_pin:{sig} 的 pin {pin} 不在 af_table")
        elif _true_af(pins_idx, sig, pin) is None:
            fails.append(f"signal_to_pin:{sig} 不是 {pin} 的合法功能")

    csv_path = os.path.join(board_dir, "baseline", "baseline.csv")
    if os.path.isfile(csv_path):
        seen_pins: dict[str, str] = {}
        with open(csv_path, encoding="utf-8") as f:
            for row in csv.DictReader(f):
                sig, pin, af = row["signal"], row["pin"], row["af"]
                if pin in seen_pins:
                    fails.append(f"baseline.csv:pin {pin} 重複"
                                 f"({seen_pins[pin]} vs {sig})")
                seen_pins[pin] = sig
                # R7「af 必為整數」+「Σ 完整性」:空欄與查不到都 FAIL
                if not re.fullmatch(r"\d+", af or ""):
                    fails.append(f"baseline.csv:{sig}@{pin} AF 非整數({af!r})")
                elif sig.upper() not in sigma:
                    fails.append(f"baseline.csv:{sig} 不在 af_table 的 Σ")
                elif not _case_check("baseline.csv", sig):
                    pass
                elif pin.upper() not in pins_idx:
                    fails.append(f"baseline.csv:{sig} 的 pin {pin} 不在 af_table")
                else:
                    true = _true_af(pins_idx, sig, pin)
                    if true is None:
                        fails.append(f"baseline.csv:{sig} 不是 {pin} 的合法功能")
                    elif int(af) != true:
                        fails.append(f"baseline.csv:{sig}@{pin} AF={af} ≠ "
                                     f"af_table 真值 {true}")
    else:
        fails.append("baseline.csv 缺檔")

    # ---- 2. schema ------------------------------------------------------ #
    for name, keys in _GEN_REQUIRED_KEYS.items():
        obj = _load(os.path.join("dts_generation", name))
        if obj is None:
            fails.append(f"dts_generation/{name} 缺檔")
            continue
        for key in keys:
            if key not in obj:
                fails.append(f"dts_generation/{name} 缺頂層鍵 {key}")

    yaml_path = os.path.join(board_dir, "board.yaml")
    if not os.path.isfile(yaml_path):
        fails.append("board.yaml 缺檔")
    else:
        text = open(yaml_path, encoding="utf-8").read()
        m = re.search(r"^\s*type:\s*(\S+)", text, re.M)
        if not m or m.group(1) not in ("cubemx", "script", "none"):
            fails.append(f"board.yaml validation.type 非法"
                         f"({m.group(1) if m else '缺'})")

    # ---- 3. baseline 完整性 ---------------------------------------------- #
    bl_dir = os.path.join(board_dir, "baseline", "dts")
    dts_files = [n for n in os.listdir(bl_dir) if n.endswith(".dts")] \
        if os.path.isdir(bl_dir) else []
    if len(dts_files) != 1:
        fails.append(f"baseline/dts 應恰有一個 .dts,實際 {dts_files}")

    expanded, cpp_errors = _cpp_expand(bl_dir, result["baseline"])
    if cpp_errors:
        fails.append(f"cpp 展開失敗:{cpp_errors[:300]}")

    # R5 label 閉包:在 cpp 展開後的輸出上稽核(匯入修復後的出貨狀態;
    # #if 0 停用區塊與註解都已被前處理器移除)——引用而未定義的 &label 一律 FAIL
    for ref, spec in result["peripherals"].items():
        for g in spec["pinctrl_groups"]:
            if g not in result["labels"]:
                fails.append(f"&{g}(啟用周邊 {ref} 的 pinctrl)找不到定義")
    if expanded:
        defs = {m.group(1)
                for m in re.finditer(r"(?m)^\s*([\w-]+)\s*:", expanded)}
        refs = {m.group(1)
                for m in re.finditer(r"&([A-Za-z_][\w-]*)", expanded)}
        for missing in sorted(refs - defs):
            fails.append(f"baseline 檔組引用 &{missing} 但未定義(label 閉包破洞)")

    # baseline 板檔健全性(2026-07-25 事故):選錯 baseline(如 IOT2050 的
    # 24 行 PG2 覆蓋殼)→ enabled 週邊趨近零 → boot 判定/signal_to_pin/
    # baseline.csv 全部空洞化,產出的知識庫是廢的。真板檔的頂層 &override
    # 動輒數十個——過低即擋,訊息引導改選(通常是 base-board/evm/evb 檔)。
    if len(result.get("enabled") or {}) < 3:
        fails.append(
            f"baseline 板檔({result.get('baseline')})只有 "
            f"{len(result.get('enabled') or {})} 個有效啟用週邊——疑似選錯"
            "(選到 overlay 殼檔?)。請改選這塊板的主板檔"
            "(通常含 base-board/evm/evb 字樣),或在 boards.ini [baseline] 更正")

    # R1 族群完整性自查:instance 編號空洞 → 待人工確認
    from .af_repair import family_gaps
    gaps = family_gaps(af_table)
    warns += [f"af_table 族群空洞:{g}" for g in gaps]

    # ---- P1/P6:boot 群組健全性(2026-07-25 事故) ------------------------ #
    emit_groups = [n for n, bg in boot_groups.items()
                   if bg.get("solver_action") == "emit_fixed_assignment"]
    if boot_groups and not emit_groups:
        warns.append("boot 群組沒有任何 emit_fixed_assignment——plan 不會自動"
                     "帶開機組(eMMC/SD/console 全缺?請人工確認是否刻意)")
    # 資料驅動的 boot 候選(console/boot media)沒被任何群組涵蓋 → WARN
    from .require_enrich import _boot_candidates
    covered = {bg.get("dts_label") for bg in boot_groups.values()}
    for ref, basis in _boot_candidates(result).items():
        if ref not in covered:
            warns.append(f"boot 候選 &{ref} 未被任何 require 群組涵蓋({basis})")

    # ---- Prompt 5:pinmux 渲染供料(非 ST 板必備) ------------------------- #
    if result["vendor"] != "st":
        _lint_pinmux_supply(board_dir, result, s2p, _load, fails, warns)

    # ---- 4. REVIEW.md ---------------------------------------------------- #
    review_path = os.path.join(board_dir, "REVIEW.md")
    _write_review(review_path, board, boot_groups, result, fails, warns,
                  af_diff=af_diff)

    verdict = "FAIL(不出貨)" if fails else ("PASS(帶待審查項)" if warns else "PASS")
    log(f"[{board}] lint:{verdict} —— FAIL {len(fails)} 項、WARN {len(warns)} 項"
        f"(詳見 {os.path.relpath(review_path, out_root)})")
    for f_ in fails:
        log(f"  ✗ {f_}")
    return {"fails": fails, "warns": warns, "review": review_path}


_STYLE_WHITELIST = {"stm32", "k3-iopad", "nuvoton-mfp"}


def _lint_pinmux_supply(
    board_dir: str, result: dict, s2p: dict, _load, fails: list, warns: list
) -> None:
    """pinmux 供料出廠規則:style 白名單/詞彙、pad 覆蓋率、鍵空間、抽查。"""
    style = _load(os.path.join("dts_generation", "pinmux_style.json"))
    pads_obj = _load(os.path.join("dts_generation", "pad_params.json"))
    if style is None:
        fails.append("dts_generation/pinmux_style.json 缺檔(非 ST 板必備)")
    if pads_obj is None:
        fails.append("dts_generation/pad_params.json 缺檔(非 ST 板必備)")
    closure_text = "\n".join(t for _, t in result["closure"])

    if style is not None:
        if style.get("style") not in _STYLE_WHITELIST:
            fails.append(f"pinmux_style:style 不在白名單({style.get('style')})")
        if not style.get("macros"):
            fails.append("pinmux_style:macros 為空")
        if not style.get("role_flags"):
            fails.append("pinmux_style:role_flags 為空")
        # 詞彙必須出現在官方 baseline 原文(模板欄位取 { 前綴比對)
        for name in (style.get("macros") or {}).values():
            token = name.split("{", 1)[0]
            if token and token not in closure_text:
                fails.append(f"pinmux_style:巨集 {name} 未出現於官方 DTS 原文")
        for role, flag in (style.get("role_flags") or {}).items():
            if str(flag).lstrip("&") not in closure_text:
                fails.append(f"pinmux_style:flag {flag}(role {role})"
                             "未出現於官方 DTS 原文")

    if pads_obj is None:
        return
    pads = pads_obj.get("pads") or {}
    # 覆蓋率:baseline.csv 與 signal_to_pin 用到的每個 pad 都要有參數
    used = set((s2p.get("signal_to_pin") or {}).values()) \
        | set(s2p.get("gpio_pins") or [])
    for pin in sorted(used):
        if pin not in pads:
            fails.append(f"pad_params:缺 {pin}(baseline/signal_to_pin 用到"
                         "——渲染時查不到 offset 必炸)")
    # 鍵空間唯一 + 合法十六進位
    seen: dict[tuple, str] = {}
    for pin, spec in pads.items():
        key = (spec.get("domain"), spec.get("offset"), spec.get("shift"))
        if not re.fullmatch(r"0x[0-9a-fA-F]+", str(spec.get("offset") or "")):
            fails.append(f"pad_params:{pin} offset 非十六進位"
                         f"({spec.get('offset')!r})")
        if key in seen:
            fails.append(f"pad_params:{pin} 與 {seen[key]} 的鍵空間重複{key}")
        seen[key] = pin
        if style and spec.get("domain") not in (style.get("macros") or {}):
            fails.append(f"pad_params:{pin} 的 domain {spec.get('domain')} "
                         "不在 pinmux_style.macros")
    # 抽查:3 個 baseline DTS 實抓錨點,三值須與供料一致(TI);
    # Nuvoton 以巨集名回構驗證(pin+signal → 巨集必須逐字存在於官方 DTS)
    anchors = result.get("ti_pads") or {}
    for pad_u, (domain, offset) in sorted(anchors.items())[:3]:
        entry = next((v for k, v in pads.items() if k.upper() == pad_u), None)
        if entry is None:
            fails.append(f"pad_params 抽查:錨點 {pad_u} 無條目")
        elif (entry["domain"], int(entry["offset"], 16)) != (domain, offset):
            fails.append(
                f"pad_params 抽查:{pad_u} 供料 ({entry['domain']},"
                f"{entry['offset']}) ≠ 官方 DTS ({domain},{hex(offset)})")
    if result["vendor"] == "nuvoton":
        for sig, pin in sorted((s2p.get("signal_to_pin") or {}).items())[:3]:
            m = re.match(r"P([A-N])(\d+)$", pin)
            if not m:
                continue
            half = "L" if int(m.group(2)) <= 7 else "H"
            macro = (f"SYS_GP{m.group(1)}_MFP{half}_P{m.group(1)}"
                     f"{m.group(2)}MFP_{sig}")
            if macro not in closure_text:
                fails.append(f"pad_params 抽查:{macro} 未出現於官方 DTS"
                             "(巨集回構失敗)")


def _cpp_expand(bl_dir: str, baseline: str) -> tuple[str | None, str | None]:
    """增項 B 驗收:cpp 展開零 error。回傳 (展開文字, 錯誤文字)。"""
    path = os.path.join(bl_dir, baseline)
    if not os.path.isfile(path):
        return None, f"{baseline} 不存在"
    proc = subprocess.run(
        ["cc", "-E", "-nostdinc", "-undef", "-D__DTS__",
         "-x", "assembler-with-cpp",
         "-I", os.path.join(bl_dir, "include"), "-I", bl_dir, path],
        capture_output=True, text=True,
    )
    if proc.returncode != 0:
        return None, proc.stderr.strip()
    return proc.stdout, None


def _write_review(
    path: str, board: str, boot_groups: dict, result: dict,
    fails: list[str], warns: list[str], *, af_diff: dict | None = None,
) -> None:
    lines = [
        f"# {board} 知識庫待審查清單(REVIEW)",
        "",
        f"產出時間:{datetime.now():%Y-%m-%d %H:%M}(knowledge_extract 自動生成)",
        "",
        "## boot 群組 emit/reserve 判定(增項 A)",
        "",
        "分錯的後果:「開機組缺席」或「kernel 搶 bootloader 的腳」。"
        "請逐列確認後在方框打勾。",
        "",
        "| ✓ | 群組 | action | DTS 節點 | 依據 | 信心 |",
        "|---|---|---|---|---|---|",
    ]
    for name, bg in sorted(boot_groups.items()):
        node = f"&{bg['dts_label']}" if bg.get("dts_label") else "—"
        locked = "(人工定案,未動)" if bg.get("locked") else ""
        lines.append(
            f"| ☐ | {name}{locked} | {bg['solver_action']} | {node} "
            f"| {bg['basis']} | {bg['confidence']} |")
    if af_diff:
        lines += [
            "", "## af_table 權威重建(R1)", "",
            f"- 來源:{af_diff['source']}(廠商 pinfunc header,MFP 真值)",
            f"- 規模:{af_diff['pins']} pins;相對舊表 +{af_diff['added']} / "
            f"-{af_diff['removed']} 條目",
            f"- 舊表為 LLM 序列式解析,掉 token 會使整列 mux 位移——以 header 為準",
            f"- 新增樣本:{af_diff['sample_added'][:4]}",
            f"- 移除樣本:{af_diff['sample_removed'][:4]}",
        ]
    rejected = result.get("rejected") or []
    analog = [r for r in rejected if r["class"] == "analog"]
    missing = [r for r in rejected if r["class"] != "analog"]
    if analog:
        lines += ["", "## 類比腳清單(R2:不進 pinmux 輸出,人工決定)", ""]
        lines += [f"- {r['owner']}:{r['signal']} @ {r['pin']}({r['reason']})"
                  for r in analog]
    if missing:
        lines += ["", "## 剔除清單(R4:af_table 查不到,不寫進輸出——"
                      "通常是 af_table 漏抓,補完重產即消失)", ""]
        lines += [f"- {r['owner']}:{r['signal']} @ {r['pin']}({r['reason']})"
                  for r in missing]
    lines += ["", "## lint 結果", ""]
    lines += [f"- ✗ FAIL:{f}" for f in fails] or ["- FAIL:無"]
    lines += [f"- ⚠ WARN:{w}" for w in warns] or ["- WARN:無"]
    lines += ["", "## 抽取警告(dts 步驟)", ""]
    lines += [f"- {w}" for w in result["warnings"]] or ["- 無"]
    lines += [
        "",
        "---",
        "簽核:上述項目已逐一確認 ☐  簽核人:________  日期:________",
        "",
    ]
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))
