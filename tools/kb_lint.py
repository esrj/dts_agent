#!/usr/bin/env python
"""kb_lint — 板級知識庫進場檢查。

    venv/bin/python tools/kb_lint.py <board>       # 全綠 exit 0；任一 FAIL exit 1

只讀不寫（不碰 output/）。規則與 Knowledge Extractor 端的出廠 lint 對齊：
extractor 出廠前擋一次、本工具進場再擋一次——格式錯誤在這裡攔下，
不流到 solver/patch 端靜默失效。

檢查類別：
  交叉一致性  require.json pin_map／signal_to_pin／baseline.csv ↔ af_table
  schema      solver_action 白名單、dts_generation 六檔頂層鍵、board.yaml type
  baseline    恰一個 .dts、板檔 &label 皆可解析
  提示性      boot 群組全 reserve_only（plan 不帶開機組，是否刻意？）

程式呼叫：`lint_board(board, data_root=None,
echo=…)` —— data_root 指到 output/staging/ 可對落地前的知識庫做同一套
檢查。非執行緒安全（模組級收集器）；web 端由 single-flight 序列化。
"""
import argparse
import csv
import json
import os
import re
import sys

sys.path.insert(0, os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

from util.dataio import (DATA, _BOARD_FILES, _BOARD_FILES_OPTIONAL,  # noqa: E402
                         load_board_manifest)

_RESULTS = []                     # [(level, message)]（lint_board 執行期重綁）
_ECHO = print
_ACTIONS = {"emit_fixed_assignment", "reserve_only"}
_VALIDATION_TYPES = {"cubemx", "script", "none"}

# dts_generation 六檔：檔名 -> (必要?, 期望頂層鍵之一)。骨架定義見
# m2_validation_harness.harness._KB_DEFAULTS（缺選配檔時 pipeline 以空骨架降級）。
_DTS_GEN_FILES = {
    "boot_requirements.json": (True, ("peripherals", "board_pin_locked")),
    "gpio_pins.json": (False, ("protected_pins",)),
    "peripheral_node_alias.json": (False, ("aliases",)),
    "board_config.json": (False, ("peripherals",)),
    "dts_property_bindings.json": (False, ("families",)),
    "fixed_connections.json": (False, ("connections",)),
}


def report(level, msg):
    _RESULTS.append((level, msg))
    _ECHO(f"{level:4s}  {msg}")


def _read_json(path):
    """回 (obj, err)。缺檔 -> (None, 'missing')；壞 JSON -> (None, 訊息)。"""
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh), None
    except FileNotFoundError:
        return None, "missing"
    except ValueError as exc:
        return None, f"JSON 損壞：{exc}"


def parse_af(path):
    """af_table.json -> {pin: {signal: af 或 None}}。兩種佈局都收
    （AF-preserving dict 有 af 值；legacy flat list 無——AF 檢查降級跳過）。
    Dual-function（"A/B"）拆分、'-' 佔位丟棄，與 dataio.load_af 同規則。"""
    raw, err = _read_json(path)
    if err:
        report("FAIL", f"af_table.json 無法讀取（{err}）：{path}")
        return None
    table = {}
    for pin, sigs in raw.items():
        entry = {}
        pairs = (sigs.items() if isinstance(sigs, dict)
                 else ((None, v) for v in sigs))
        for af, cell in pairs:
            for s in str(cell).split("/"):
                s = s.strip().upper()
                if s and s != "-":
                    entry[s] = int(af) if af is not None and str(af).lstrip("-").isdigit() else None
        table[pin.upper()] = entry
    return table


def _check_triple(where, sig, pin, af, af_table):
    """(signal, pin, af) 對 af_table 的合法性；回報到 _RESULTS。"""
    sig, pin = (sig or "").upper(), (pin or "").upper()
    entry = af_table.get(pin)
    if entry is None:
        report("FAIL", f"{where}: pin {pin!r} 不在 af_table")
        return
    if sig not in entry:
        report("FAIL", f"{where}: {pin} 接不了 signal {sig!r}（不在該腳的 AF 表）")
        return
    want = entry[sig]
    if want is not None and af is not None and int(af) != want:
        report("FAIL", f"{where}: {sig}@{pin} 的 AF 應為 {want}，檔內為 {af}")


def check_require(paths, af_table, sigma):
    req, err = _read_json(paths["require"])
    if err:
        report("FAIL", f"require.json 無法讀取（{err}）")
        return
    groups = ((req.get("boot_pin_locked") or {}).get("groups") or {})
    if not groups:
        report("WARN", "require.json 無 boot_pin_locked.groups——此板沒有任何開機腳知識")
        return
    actions = set()
    for name, blk in groups.items():
        action = (blk or {}).get("solver_action")
        if action not in _ACTIONS:
            report("FAIL", f"require.json [{name}]: solver_action {action!r} 不在 {sorted(_ACTIONS)}")
            continue
        actions.add(action)
        for row in (blk or {}).get("pin_map") or []:
            if not (isinstance(row, (list, tuple)) and len(row) == 3):
                report("FAIL", f"require.json [{name}]: pin_map 列格式應為 [signal, pin, af]：{row!r}")
                continue
            sig, pin, af = row
            if (sig or "").upper() not in sigma:
                report("FAIL", f"require.json [{name}]: signal {sig!r} 不在 Σ（af_table 的 signal 全集）")
            _check_triple(f"require.json [{name}]", sig, pin, af, af_table)
    if actions == {"reserve_only"}:
        report("WARN", "boot 群組全為 reserve_only（無 emit_fixed_assignment）——"
                       "此板的 plan 不會自動帶開機組，是否刻意？（am6548 事故的坑）")
    report("PASS", f"require.json：{len(groups)} 個 boot 群組檢畢")


def check_signal_to_pin(paths, af_table, sigma):
    raw, err = _read_json(paths["signals"])
    if err:
        report("FAIL", f"signal_to_pin.json 無法讀取（{err}）")
        return
    s2p = raw.get("signal_to_pin") or {}
    bad = 0
    for sig, pin in s2p.items():
        if sig.upper() not in sigma:
            report("FAIL", f"signal_to_pin: signal {sig!r} 不在 Σ")
            bad += 1
        elif af_table.get(str(pin).upper()) is None:
            report("FAIL", f"signal_to_pin: {sig} 的 pin {pin!r} 不在 af_table")
            bad += 1
        elif sig.upper() not in af_table[str(pin).upper()]:
            report("FAIL", f"signal_to_pin: {pin} 接不了 {sig}")
            bad += 1
    if not bad:
        report("PASS", f"signal_to_pin.json：{len(s2p)} 條對照檢畢")


def check_baseline_csv(base_dir, af_table):
    path = os.path.join(base_dir, "baseline", "baseline.csv")
    if not os.path.isfile(path):
        report("WARN", "baseline/baseline.csv 缺席——第二段（DTS patch）不可用")
        return
    seen = {}
    bad_af = 0
    with open(path, encoding="utf-8-sig", newline="") as fh:
        for i, row in enumerate(csv.DictReader(fh), start=2):
            sig = (row.get("signal") or "").strip()
            pin = (row.get("pin") or "").strip().upper()
            if not sig or sig.upper() == "DISABLE":
                continue
            if pin in seen:
                report("FAIL", f"baseline.csv 第 {i} 行：pin {pin} 重複（第 {seen[pin]} 行已用）")
            seen[pin] = i
            af = (row.get("af") or "").strip()
            if not af.lstrip("-").isdigit():
                # patch 端 load_plan 需要整數 AF——空欄會讓「產生 DTS」直接
                # exception（ma35d1 事故：int('')）。補真值或移除該列。
                bad_af += 1
                report("FAIL", f"baseline.csv 第 {i} 行：af 欄必須是整數，"
                               f"實際為 {af!r}（{sig}@{pin}）")
                continue
            _check_triple(f"baseline.csv 第 {i} 行", sig, pin, int(af), af_table)
    report("PASS", f"baseline.csv：{len(seen)} 支腳檢畢"
                   + (f"（{bad_af} 列 af 非法）" if bad_af else ""))


def check_dts_generation(base_dir):
    gen = os.path.join(base_dir, "dts_generation")
    if not os.path.isdir(gen):
        report("FAIL", "dts_generation/ 目錄缺席——第二段不可用（board_ready=false）")
        return
    for fname, (required, keys) in _DTS_GEN_FILES.items():
        obj, err = _read_json(os.path.join(gen, fname))
        if err == "missing":
            report("FAIL" if required else "WARN",
                   f"dts_generation/{fname} 缺席"
                   + ("——board_ready=false" if required else "（pipeline 以空骨架降級）"))
        elif err:
            report("FAIL", f"dts_generation/{fname}：{err}")
        elif not any(k in obj for k in keys):
            report("WARN", f"dts_generation/{fname}：頂層缺 {keys} 之一（schema 疑異）")
    report("PASS", "dts_generation/ 檢畢")


def check_board_yaml(board, manifest_path, live=True):
    if not os.path.isfile(manifest_path or ""):
        report("WARN", "board.yaml 缺席——採預設驗證策略（預設板 cubemx、其他板 none）；"
                       "且 patch diff 檔頭將以 vendor 推導（建議明寫 kernel_dts_path）")
        return
    if live:
        m = load_board_manifest(board)             # 已含 type 白名單正規化＋warn
    else:
        # staging 模式：load_board_manifest 只查正式 data/，這裡直讀檔案
        import yaml
        try:
            m = yaml.safe_load(open(manifest_path, encoding="utf-8")) or {}
        except Exception as exc:
            report("FAIL", f"board.yaml 無法讀取：{exc}")
            return
    v = m.get("validation") or {}
    if v.get("type") not in _VALIDATION_TYPES:
        report("FAIL", f"board.yaml validation.type {v.get('type')!r} 不合法")
    else:
        report("PASS", f"board.yaml：validation={v.get('type')}（enabled={v.get('enabled')}）")


_PINMUX_STYLES = {"stm32", "k3-iopad", "nuvoton-mfp"}


def check_pinmux_style(base_dir, af_table):
    """pinmux 渲染 style：style 白名單；非 stm32 板
    必須有 pad_params.json 且覆蓋 baseline.csv 用到的每個 pad（渲染時查不到
    offset 必炸，進場就攔）。ST 板不放 style 檔＝stm32 預設，PASS。"""
    gen = os.path.join(base_dir, "dts_generation")
    style_path = os.path.join(gen, "pinmux_style.json")
    style_obj, err = _read_json(style_path)
    if err == "missing":
        report("PASS", "pinmux_style.json 缺席——採 stm32 預設渲染")
        return
    if err:
        report("FAIL", f"pinmux_style.json：{err}")
        return
    style = (style_obj.get("style") or "").lower()
    if style not in _PINMUX_STYLES:
        report("FAIL", f"pinmux_style.json：style {style!r} 不在 {sorted(_PINMUX_STYLES)}")
        return
    if style == "stm32":
        report("PASS", "pinmux_style=stm32")
        return
    # 非 stm32：macros/role_flags 必要、pad_params 覆蓋率
    for key in ("macros", "role_flags"):
        if not style_obj.get(key):
            report("FAIL", f"pinmux_style.json：style={style} 但缺 {key}（渲染必需）")
    pads_obj, perr = _read_json(os.path.join(gen, "pad_params.json"))
    if perr:
        report("FAIL", f"style={style} 但 pad_params.json 不可用（{perr}）——"
                       "渲染需 pad→offset 表（knowledge_extract 供料，見 src/knowledge_extract/README.md）")
        return
    pads = pads_obj.get("pads") or {}
    csv_path = os.path.join(base_dir, "baseline", "baseline.csv")
    missing = set()
    if os.path.isfile(csv_path):
        with open(csv_path, encoding="utf-8-sig", newline="") as fh:
            for row in csv.DictReader(fh):
                pin = (row.get("pin") or "").strip().upper()
                sig = (row.get("signal") or "").strip().upper()
                if pin and sig != "DISABLE" and pin not in pads:
                    missing.add(pin)
    if missing:
        report("FAIL", f"pad_params.json 覆蓋率不足：baseline.csv 用到但缺參數的 pad "
                       f"{sorted(missing)[:8]}{'…' if len(missing) > 8 else ''}"
                       f"（共 {len(missing)}）")
    else:
        report("PASS", f"pinmux_style={style}：pad_params 覆蓋 baseline 全部 pad")


_LABEL_DEF = re.compile(r"^\s*([A-Za-z_]\w*)\s*:", re.M)
_LABEL_REF = re.compile(r"&([A-Za-z_]\w*)")


def check_baseline_dts(base_dir):
    dts_dir = os.path.join(base_dir, "baseline", "dts")
    if not os.path.isdir(dts_dir):
        report("WARN", "baseline/dts/ 缺席——第二段不可用")
        return
    board_dts = [f for f in os.listdir(dts_dir) if f.endswith(".dts")]
    if len(board_dts) != 1:
        report("FAIL", f"baseline/dts 應恰有一個 .dts（板檔），實際 {len(board_dts)}：{board_dts}")
        if not board_dts:
            return
    defs = set()
    for root, _dirs, files in os.walk(dts_dir):
        for f in files:
            if f.endswith((".dts", ".dtsi")):
                text = open(os.path.join(root, f), encoding="utf-8",
                            errors="replace").read()
                defs |= set(_LABEL_DEF.findall(text))
    board_text = open(os.path.join(dts_dir, board_dts[0]), encoding="utf-8",
                      errors="replace").read()
    missing = sorted(set(_LABEL_REF.findall(board_text)) - defs)
    if missing:
        report("FAIL", f"板檔引用了檔組內找不到定義的 label（多半是 dtsi 沒抓齊）："
                       f"{missing[:8]}{'…' if len(missing) > 8 else ''}")
    else:
        report("PASS", f"baseline/dts：板檔 {board_dts[0]} 的 &label 全數可解析")


def lint_board(board, data_root=None, echo=print):
    """對一塊板跑全部檢查（M2 函式化；CLI 與 web gate 共用）。

    data_root：None＝正式知識庫 data/；指到 output/staging/ 可檢查落地前
    的產出。路徑一律以 dataio 的 _BOARD_FILES 權威表拼（紅線 2）。
    回 {"ok": bool, "fails": n, "warns": n, "findings": [(level, msg)]}；
    board 目錄不存在時 findings 只有一筆 FAIL。
    """
    global _RESULTS, _ECHO
    _RESULTS, _ECHO = [], echo

    root = data_root or DATA
    base_dir = os.path.join(root, board)
    if not os.path.isdir(base_dir):
        # 不做任何 fallback——lint 不存在的板必須明確失敗
        report("FAIL", f"{os.path.relpath(base_dir)}/ 不存在")
        return {"ok": False, "fails": 1, "warns": 0, "findings": list(_RESULTS)}

    paths = {k: os.path.join(base_dir, v)
             for k, v in {**_BOARD_FILES, **_BOARD_FILES_OPTIONAL}.items()}
    # 必要五檔存在性（list_boards 的偵測條件）：缺任一檔該板不會出現在
    # 選單、solve 直接 FileNotFoundError——lint 必須先攔（2026-07-25：
    # dts-only 重產缺 profiles 卻 lint 全綠的事故）
    for role, rel in _BOARD_FILES.items():
        if not os.path.isfile(os.path.join(base_dir, rel)):
            report("FAIL", f"必要檔缺失：{rel}——該板不會被偵測、solve 會炸")
    af_table = parse_af(paths["af"])
    if af_table is not None:
        sigma = {s for entry in af_table.values() for s in entry}
        report("PASS", f"af_table.json：{len(af_table)} pins / {len(sigma)} signals")

        check_require(paths, af_table, sigma)
        check_signal_to_pin(paths, af_table, sigma)
        check_baseline_csv(base_dir, af_table)
        check_dts_generation(base_dir)
        check_pinmux_style(base_dir, af_table)
        check_board_yaml(board, paths.get("manifest"), live=data_root is None)
        check_baseline_dts(base_dir)

    fails = sum(1 for lv, _ in _RESULTS if lv == "FAIL")
    warns = sum(1 for lv, _ in _RESULTS if lv == "WARN")
    return {"ok": not fails, "fails": fails, "warns": warns,
            "findings": list(_RESULTS)}


def main():
    ap = argparse.ArgumentParser(description="板級知識庫進場檢查（只讀）")
    ap.add_argument("board", help="data/ 下的板子 id")
    ap.add_argument("--data-root", default=None,
                    help="知識庫根目錄（預設 data/；可指 output/staging 檢查落地前產出）")
    args = ap.parse_args()

    base_dir = os.path.join(args.data_root or DATA, args.board)
    if not os.path.isdir(base_dir):
        print(f"FAIL  data/{args.board}/ 不存在" if args.data_root is None
              else f"FAIL  {base_dir}/ 不存在")
        return 1
    print(f"kb_lint — {args.board}\n" + "-" * 56)
    res = lint_board(args.board, data_root=args.data_root)
    print("-" * 56)
    print(f"{'ALL GREEN' if res['ok'] else 'LINT FAILED'}"
          f"  (FAIL={res['fails']}, WARN={res['warns']})")
    return 0 if res["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
