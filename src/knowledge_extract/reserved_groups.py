"""增項 B：上傳時人工填寫的「安全保留群組」（PMIC 等）併入 require.json。

來源：web 上傳表單（board_create 存進 staging 的 input/reserved_groups.json）
或 CLI 使用者自行放進 input/。時機：manual 抽取的 require 步驟**之後**——
此時該板 af_table 已存在，逐列做資料驅動驗證（pin 存在、訊號可達 →
AF 回填真值，大小寫以 af_table 原文為準）。

輸入格式（list）：
    [{"name": "I2C7", "role": "電源管理 PMIC (STPMIC25)",
      "owner": "TF-A/OP-TEE (secure)",
      "pins": [{"signal": "I2C7_SCL", "pin": "PD15"}, ...]}, ...]

原則：
- 一律 reserve_only + in_kernel_dt=false：這些匯流排由 secure/bootloader
  持有（PMIC 的 I2C、secure flash…），kernel 永不輸出到 plan。
- 驗證失敗的列**剔除並記在 _review.basis**（完成頁 boot 判定表可見）——
  kb_lint 對 pin_map 是硬 FAIL（signal∉Σ／pin∉af_table／AF 非整數都擋落地），
  不能讓一個打錯字作廢整趟分鐘級 LLM 抽取；落地後編輯 require.json 補回
  再跑 kb_lint 即可。與 require_enrich 的 R3/R7（不留空欄或存疑值）一致。
- 不覆蓋既有群組（手冊抽取／DTS 判定優先）；但**自己先前併入的群組**
  （_review.source 標記）重跑可更新——CLI 增量執行才改得動自己。
"""
from __future__ import annotations

import json
import os

_BASIS = "使用者上傳時填寫（原理圖）"
_SOURCE = "reserved_groups"


def _resolve_row(af_table: dict, sig: str, pin: str):
    """(訊號, 腳位) → (canonical 訊號, canonical 腳位, af int)；查無回 None。
    比對不分大小寫，落庫用 af_table 原文——kb_lint 的 Σ 檢查是嚴格比對。"""
    canon_pin = {p.upper(): p for p in af_table}.get(pin.upper())
    if canon_pin is None:
        return None
    for mux, cell in (af_table.get(canon_pin) or {}).items():
        for atom in str(cell).split("/"):
            atom = atom.strip()
            if atom and atom.upper() == sig.upper():
                try:
                    return atom, canon_pin, int(mux)
                except (TypeError, ValueError):
                    return None
    return None


def merge(board: str, *, input_dir: str, req_path: str, af_path: str,
          log=print) -> list[str]:
    """input_dir/reserved_groups.json → req_path 的 boot_pin_locked.groups。
    回傳 warnings（一律不硬失敗）；無輸入檔＝no-op 回 []。"""
    src = os.path.join(input_dir, "reserved_groups.json")
    if not os.path.isfile(src):
        return []
    warnings: list[str] = []
    try:
        with open(src, encoding="utf-8") as f:
            groups_in = json.load(f)
        if not isinstance(groups_in, list):
            raise ValueError("頂層必須是 list")
    except (OSError, ValueError) as exc:
        return [f"reserved_groups.json 無法解析（{exc}）——人工保留群組未併入"]
    try:
        with open(req_path, encoding="utf-8") as f:
            require = json.load(f)
        if not isinstance(require, dict):
            raise ValueError("頂層必須是 object")
    except (OSError, ValueError) as exc:
        return [f"require.json 缺席/壞檔（{exc}）——人工保留群組未併入"]
    try:
        with open(af_path, encoding="utf-8") as f:
            af_table = json.load(f)
        if not isinstance(af_table, dict) or not af_table:
            raise ValueError("空表")
    except (OSError, ValueError) as exc:
        return [f"af_table 缺席/壞檔（{exc}）——人工保留群組未併入（腳位無從驗證）"]

    spec = require.setdefault("boot_pin_locked", {
        "policy": "fixed_pins_are_constants; reserve_peripheral_instance",
        "pin_map_columns": ["signal", "pin", "af"],
        "groups": {},
    })
    groups = spec.setdefault("groups", {})
    changed = False
    for g in groups_in:
        if not isinstance(g, dict):
            continue
        name = str(g.get("name") or "").strip()
        if not name:
            continue
        prev = groups.get(name)
        if prev is not None and \
                (prev.get("_review") or {}).get("source") != _SOURCE:
            warnings.append(f"[{name}] 群組已存在（手冊抽取/DTS 判定建立）——"
                            "人工填寫未覆蓋；落地後如需修改請編輯 require.json")
            continue
        pin_map, dropped = [], []
        for row in (g.get("pins") or []):
            if not isinstance(row, dict):
                continue
            sig = str(row.get("signal") or "").strip()
            pin = str(row.get("pin") or "").strip()
            if not sig or not pin:
                continue
            hit = _resolve_row(af_table, sig, pin)
            if hit is None:
                dropped.append(f"{sig}@{pin}")
                continue
            sig, pin, af = hit
            if not (sig == name or sig.startswith(name + "_")):
                warnings.append(f"[{name}] 訊號 {sig} 前綴與群組名不一致——"
                                "instance 級封鎖以群組名比對，群組名請用 "
                                "instance 名（訊號前綴）")
            pin_map.append([sig, pin, af])
        basis = _BASIS
        if dropped:
            basis += (f"；剔除 af_table 查不到的列 {dropped}"
                      "（打錯字？落地後編輯 require.json 補回）")
            warnings.append(f"[{name}] {len(dropped)} 列在 af_table 查不到，"
                            f"已剔除：{'、'.join(dropped)}")
        groups[name] = {
            "role": str(g.get("role") or "").strip() or f"安全保留 ({name})",
            "owner": (str(g.get("owner") or "").strip()
                      or "secure/bootloader（上傳時人工填寫）"),
            "in_kernel_dt": False,
            "solver_action": "reserve_only",
            "profile_status": "user_provided",
            "pin_source": _BASIS,
            "pin_map": pin_map,
            "_review": {"basis": basis, "confidence": "human",
                        "source": _SOURCE},
        }
        changed = True
        log(f"[{board}] require: 併入人工保留群組 {name}（{len(pin_map)} 腳"
            + (f"，剔除 {len(dropped)}" if dropped else "") + "）")
    if changed:
        with open(req_path, "w", encoding="utf-8") as f:
            json.dump(require, f, ensure_ascii=False, indent=1)
            f.write("\n")
        log(f"[{board}] require.json 已併入人工保留群組（{req_path}）")
    return warnings
