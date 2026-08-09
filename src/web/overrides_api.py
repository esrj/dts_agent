"""
overrides_api.py — 開機/PMIC 腳位覆寫端點（USER_SYSTEM_PLAN Part III）。

GET/PUT /api/boards/<board>/pin-overrides，scope 到**當前登入 user** 的
active 覆寫組（auth.before_request 已把 g.user 掛好）。

- 真實知識庫 data/<board>/ 永不被修改——差異存 DB（store/overrides.py），
  求解時由 dataio.effective_require() 疊加（M5）。
- 只存差異：PUT 送整張表的目前值，這裡與 require.json 逐欄比對，
  與官方相同＝不落庫。
- 對齊錨點：official_group＋official_signal 永遠指向 require.json 那一列；
  官方知識庫升級後對不上的 DB 列 GET 標 stale、不參與呈現與合併。
- 寫入驗證（III.4）依序：①格式 ②腳位存在（af_table）③訊號可達→AF 反查
  落庫（快取，權威仍是 af_table）④instance rename 必須存在於 Σ
  ⑤內部無撞腳（含 gpio_must）⑥樂觀鎖 base_version（不符 409）。
  儲存時不做全域可解性檢查（定案：由求解時既有人話診斷承接）。
"""
from __future__ import annotations

import json
import re

from flask import Blueprint, g, jsonify, request

from store import overrides as ov_store
from util.dataio import af_of, board_paths, list_boards, load_af, load_af_map, sigma_of

bp = Blueprint("overrides_api", __name__)

# 格式檢查只做寬鬆 sanity（III.4 ①）；命名慣例**不寫死**（紅線 2：TI/nuvoton
# 板的訊號含小寫、pad 命名不一定是 P<字母><數字>）——「腳位存在」「訊號可達」
# 由該板 af_table 把關（②③），那才是真正的守門。
_GROUP_RE = re.compile(r"^[A-Za-z0-9_]{2,}$")
_SIG_RE = re.compile(r"^[A-Za-z0-9_/]{2,}$")
_PIN_RE = re.compile(r"^[A-Za-z0-9_.]{2,}$")


def _pin_rows(block) -> list:
    """群組的 pin_map 列，壞列（非 list 或欄數不足）直接跳過——與 dataio.
    _pin_map_rows 同樣的容忍度，不讓一列壞資料把整個端點炸成 500。"""
    rows = block.get("pin_map") if isinstance(block, dict) else None
    return [r for r in (rows if isinstance(rows, list) else [])
            if isinstance(r, (list, tuple)) and len(r) >= 2]


def _load_require(board: str) -> dict:
    """官方 require.json（整份）；缺檔/壞檔回 {}。"""
    try:
        with open(board_paths(board)["require"], encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _boot_groups(require_data: dict) -> dict:
    groups = (require_data.get("boot_pin_locked") or {}).get("groups") or {}
    return groups if isinstance(groups, dict) else {}


def _gpio_pins(require_data: dict) -> set:
    pins = (require_data.get("gpio_must_pins") or {}).get("pins") or []
    return {str(p).upper() for p in pins}


@bp.get("/api/boards/<board>/pin-overrides")
def ov_get(board: str):
    if board not in list_boards():
        return jsonify(error=f"未知板子：{board}"), 404
    groups = _boot_groups(_load_require(board))
    active = ov_store.load_active(g.user["id"], board)   # 單一交易快照
    st = active["set"] if active else None
    data = active["data"] if active else {"peripherals": {}, "gpio": []}
    entries, known = [], set()
    for gname, block in groups.items():
        role = (block.get("role") or "").strip()
        p_ov = data["peripherals"].get(gname) or {}
        new_gname = p_ov.get("group_name")
        for row in _pin_rows(block):
            sig, pin = str(row[0]), str(row[1]).upper()
            af = row[2] if len(row) > 2 else None
            known.add((gname, sig))
            r_ov = (p_ov.get("pins") or {}).get(sig)
            entries.append({
                "role": role, "group": gname, "signal": sig,
                "default_pin": pin, "af": af,
                "override_group": (new_gname
                                   if new_gname and new_gname != gname else None),
                "override_signal": (r_ov["signal"]
                                    if r_ov and r_ov["signal"] != sig else None),
                "override_pin": (r_ov["pin"]
                                 if r_ov and r_ov["pin"] != pin else None),
            })
    # 官方知識庫升級後對不上的孤兒列：標 stale 給前端顯示，不參與合併
    stale = []
    for og, p in data["peripherals"].items():
        if og not in groups:
            stale.append(og)
            continue
        stale.extend(f"{og}/{osig}" for osig in (p.get("pins") or {})
                     if (og, osig) not in known)
    has_ov = any(e["override_group"] or e["override_signal"] or e["override_pin"]
                 for e in entries)
    return jsonify(
        entries=entries,
        set=({"id": st["id"], "name": st["name"], "version": st["version"]}
             if st else None),
        base_version=(st["version"] if st else 0),
        source=("db" if has_ov else "official"),
        stale=stale)


@bp.put("/api/boards/<board>/pin-overrides")
def ov_put(board: str):
    if board not in list_boards():
        return jsonify(error=f"未知板子：{board}"), 404
    payload = request.get_json(silent=True) or {}
    raw = payload.get("entries")
    if not isinstance(raw, list):
        return jsonify(error="缺少 entries"), 400
    try:
        base_version = int(payload.get("base_version") or 0)
    except (TypeError, ValueError):
        return jsonify(error="base_version 必須是整數"), 400

    require_data = _load_require(board)
    groups = _boot_groups(require_data)
    if not groups:
        return jsonify(error="此板沒有開機腳位資料（require.json 無 boot 群組）"), 400

    # 官方索引（對齊錨點 → 官方 pin/af）與群組列序
    official = {}                     # (group, signal) -> {"pin": .., "af": ..}
    for gname, block in groups.items():
        for row in _pin_rows(block):
            official[(gname, str(row[0]))] = {
                "pin": str(row[1]).upper(),
                "af": row[2] if len(row) > 2 else None}

    # -- 對齊：每列都要帶 official_group/official_signal，且整張表要齊 -------- #
    errors: list[str] = []
    aligned: dict[tuple, dict] = {}
    for i, e in enumerate(raw):
        if not isinstance(e, dict):
            errors.append(f"第 {i + 1} 列不是物件")
            continue
        key = (str(e.get("official_group") or ""), str(e.get("official_signal") or ""))
        if key not in official:
            errors.append(f"第 {i + 1} 列對不上官方表"
                          f"（{key[0]}/{key[1]}）——請重新載入設定頁")
        elif key in aligned:
            errors.append(f"{key[0]}/{key[1]} 重複出現")
        else:
            # 訊號/介面保留原大小寫（TI 板訊號含小寫，硬大寫會對不上 Σ——
            # 同 dataio._pin_map_rows 的慣例）；pad 鍵一律大寫正規化。
            aligned[key] = {
                "group": str(e.get("group") or "").strip(),
                "signal": str(e.get("signal") or "").strip(),
                "pin": str(e.get("pin") or "").strip().upper(),
            }
    missing = [k for k in official if k not in aligned]
    if missing:
        errors.append("缺少官方列：" + "、".join(f"{a}/{b}" for a, b in missing))
    if errors:
        return jsonify(error="驗證未通過", details=errors), 400

    # -- ① 格式（前端已驗，後端重驗——前端驗證只是 UX） --------------------- #
    for (og, osig), cur in aligned.items():
        if not _GROUP_RE.match(cur["group"]):
            errors.append(f"{og}/{osig}: 介面格式不正確（{cur['group']!r}）")
        if not _SIG_RE.match(cur["signal"]):
            errors.append(f"{og}/{osig}: 訊號格式不正確（{cur['signal']!r}）")
        if not _PIN_RE.match(cur["pin"]):
            errors.append(f"{og}/{osig}: 腳位格式不正確（{cur['pin']!r}，例：PD15）")
    if errors:
        return jsonify(error="驗證未通過", details=errors), 400

    # 群組一致性＋rename 正規化：介面名整組同步（前端行為一致）；
    # group 改名且訊號未動 → 訊號前綴自動跟著換（計劃 III.2 rename 語意）
    new_group: dict[str, str] = {}
    for gname in groups:
        vals = {aligned[k]["group"] for k in aligned if k[0] == gname}
        if not vals:                          # 空 pin_map 群組：無列可改，維持原名
            new_group[gname] = gname
        elif len(vals) > 1:
            errors.append(f"{gname}: 介面名不一致（{'、'.join(sorted(vals))}）——"
                          "同一組的介面名必須相同")
        else:
            new_group[gname] = next(iter(vals))
    # rename 撞名：兩個群組同名會在合併時互相覆蓋（一組 boot 常數被吞掉）
    finals = list(new_group.values())
    for dup in sorted({n for n in finals if finals.count(n) > 1}):
        errors.append(f"介面名互撞：{dup}——兩個群組不能同名")
    if errors:
        return jsonify(error="驗證未通過", details=errors), 400
    for (og, osig), cur in aligned.items():
        ng = new_group[og]
        if ng != og and cur["signal"] == osig and osig.startswith(og + "_"):
            cur["signal"] = ng + osig[len(og):]
        if not cur["signal"].startswith(cur["group"] + "_"):
            errors.append(f"{og}/{osig}: 訊號 {cur['signal']} 與介面 "
                          f"{cur['group']} 前綴不一致")
    if errors:
        return jsonify(error="驗證未通過", details=errors), 400

    # -- ②③④ 資料驅動檢查：全部以該板 af_table 為準（零寫死） --------------- #
    paths = board_paths(board)
    af_map = load_af_map(paths["af"])
    sigma = sigma_of(load_af(paths["af"]))
    for og, ng in new_group.items():
        if ng != og and not any(s.startswith(ng + "_") for s in sigma):
            errors.append(f"{og}: 此板沒有 instance {ng}（Σ 查無 {ng}_* 訊號）")
    af_cache: dict[tuple, int | None] = {}
    for (og, osig), cur in aligned.items():
        # 大小寫容錯（同 emit_plan）：先試原樣，再退大寫——ST 訊號全大寫，
        # 使用者打小寫仍可對上；TI 等含小寫訊號的板原樣即中。
        if cur["signal"] not in sigma and cur["signal"].upper() in sigma:
            cur["signal"] = cur["signal"].upper()
        if cur["pin"] not in af_map:
            errors.append(f"{og}/{osig}: 此板沒有腳位 {cur['pin']}")
            continue
        af_val = af_of(af_map, cur["pin"], cur["signal"])
        if af_val is None:
            cands = sorted(p for p in af_map
                           if af_of(af_map, p, cur["signal"]) is not None)
            errors.append(
                f"{og}/{osig}: 腳位 {cur['pin']} 無法輸出 {cur['signal']}"
                + (f"；可用腳位：{'、'.join(cands)}" if cands
                   else f"——此板沒有任何腳位能輸出 {cur['signal']}"))
        af_cache[(og, osig)] = af_val
    if errors:
        return jsonify(error="驗證未通過", details=errors), 400

    # -- ⑤ 內部無撞腳：覆寫後 boot 腳＋（覆寫後）gpio_must 兩兩不重複 -------- #
    active = ov_store.load_active(g.user["id"], board)
    gpio_eff = _gpio_pins(require_data)
    for r in (active["data"]["gpio"] if active else []):
        (gpio_eff.add if r["action"] == "add" else gpio_eff.discard)(r["pin"])
    used: dict[str, str] = {}
    for (og, osig), cur in aligned.items():
        if cur["pin"] in used:
            errors.append(f"撞腳：{cur['signal']} 與 {used[cur['pin']]} "
                          f"都指到 {cur['pin']}")
        else:
            used[cur["pin"]] = cur["signal"]
        if cur["pin"] in gpio_eff:
            errors.append(f"撞腳：{cur['signal']} 指到 {cur['pin']}，"
                          "但它是保留 GPIO（gpio_must_pins）")
    if errors:
        return jsonify(error="驗證未通過", details=errors), 400

    # -- 只存差異 ----------------------------------------------------------- #
    periph_diff: dict[str, dict] = {}
    for (og, osig), cur in aligned.items():
        off = official[(og, osig)]
        renamed = new_group[og] != og
        if not (renamed or cur["signal"] != osig or cur["pin"] != off["pin"]):
            continue
        p = periph_diff.setdefault(og, {"group_name": new_group[og], "pins": {}})
        p["pins"][osig] = {"signal": cur["signal"], "pin": cur["pin"],
                           "af": af_cache[(og, osig)]}

    # -- ⑥ 樂觀鎖＋交易落庫（version++、history 快照） ----------------------- #
    try:
        set_id, version = ov_store.save_diff(
            g.user["id"], board, base_version, periph_diff)
    except ov_store.VersionConflict as exc:
        return jsonify(error="這張表在別處被修改過（版本衝突）——"
                             "請重新開啟設定頁載入最新內容後再改。",
                       current_version=exc.current), 409
    return jsonify(ok=True, set_id=set_id, version=version,
                   source=("db" if periph_diff else "official"))
