"""
overrides.py — pin 覆寫 DAO（USER_SYSTEM_PLAN Part III）。

三層正規化：override_sets（user×board；一組 active）→ override_peripherals
（official_group 錨點＋group_name）→ pin_overrides（official_signal 錨點＋
signal/pin/af）；另 gpio_must_overrides（set 1:N add/remove）。

- **只存差異**：與官方 require.json 相同的群組/列不落庫（比對在 web 端點層，
  這裡只管存取）。空 set＝無覆寫。
- af 欄是 af_table 反查後的**快取**（權威仍是 af_table）。
- 每次儲存 version+1（樂觀鎖＋快取失效）並寫 override_history 快照。
- 真實知識庫 data/<board>/ 永不被本模組觸碰。
"""
from __future__ import annotations

import json

from . import db


class VersionConflict(Exception):
    """樂觀鎖不符（別的視窗/分頁已先儲存）。current＝DB 目前版本。"""

    def __init__(self, current: int):
        super().__init__(f"版本衝突（伺服器目前為第 {current} 版）")
        self.current = current


def get_active_set(user_id: int, board_slug: str) -> dict | None:
    """該 user×board 的 active 覆寫組 {id, name, version}；無 → None。"""
    conn = db.connect()
    try:
        row = conn.execute(
            "SELECT id, name, version FROM override_sets "
            "WHERE user_id = ? AND board_slug = ? AND is_active = 1 "
            "ORDER BY id LIMIT 1", (user_id, board_slug)).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def _set_data(conn, set_id: int) -> dict:
    """單一連線上讀 set 的全部覆寫內容（load_set_data / load_active 共用）。"""
    periphs: dict[str, dict] = {}
    for p in conn.execute(
            "SELECT id, official_group, group_name FROM override_peripherals "
            "WHERE set_id = ? ORDER BY id", (set_id,)).fetchall():
        pins = {}
        for r in conn.execute(
                "SELECT official_signal, signal, pin, af FROM pin_overrides "
                "WHERE periph_id = ? ORDER BY id", (p["id"],)).fetchall():
            pins[r["official_signal"]] = {
                "signal": r["signal"], "pin": r["pin"], "af": r["af"]}
        periphs[p["official_group"]] = {
            "group_name": p["group_name"], "pins": pins}
    gpio = [dict(r) for r in conn.execute(
        "SELECT pin, action FROM gpio_must_overrides "
        "WHERE set_id = ? ORDER BY id", (set_id,)).fetchall()]
    return {"peripherals": periphs, "gpio": gpio}


def load_set_data(set_id: int) -> dict:
    """set 的全部覆寫內容（GET 端點與 effective_require 共用的巢狀形狀）：
    {peripherals: {official_group: {group_name, pins: {official_signal:
    {signal, pin, af}}}}, gpio: [{pin, action}]}"""
    conn = db.connect()
    try:
        return _set_data(conn, set_id)
    finally:
        conn.close()


def load_active(user_id: int, board_slug: str) -> dict | None:
    """active set＋全部內容，**單一交易快照**讀取 → {set: {id, name, version},
    data: {...}}；無 → None。version 與 data 必須一致：若拆成 get_active_set
    ＋load_set_data 兩次連線，中間插入一次儲存就會拿到「舊 version＋新 data」，
    污染 (board, set_id, version) 的 _Board 快取。"""
    conn = db.connect()
    try:
        conn.execute("BEGIN")                 # WAL：交易內讀取見一致快照
        row = conn.execute(
            "SELECT id, name, version FROM override_sets "
            "WHERE user_id = ? AND board_slug = ? AND is_active = 1 "
            "ORDER BY id LIMIT 1", (user_id, board_slug)).fetchone()
        if row is None:
            return None
        return {"set": dict(row), "data": _set_data(conn, int(row["id"]))}
    finally:
        try:
            conn.rollback()
        finally:
            conn.close()


def save_diff(user_id: int, board_slug: str, base_version: int,
              periph_diff: dict) -> tuple[int, int]:
    """交易內：整組差異覆蓋落庫、version+1、寫 history 快照。

    periph_diff 形狀同 load_set_data()["peripherals"]（只含與官方不同者）。
    gpio_must_overrides 不在 web 表格範圍內，**保留不動**（未來擴充）。
    回傳 (set_id, new_version)；base_version 與 DB 不符丟 VersionConflict。
    """
    conn = db.connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT id, version FROM override_sets "
            "WHERE user_id = ? AND board_slug = ? AND is_active = 1 "
            "ORDER BY id LIMIT 1", (user_id, board_slug)).fetchone()
        if row is None:
            if base_version:                      # 首存必從 0 開始
                raise VersionConflict(0)
            cur = conn.execute(
                "INSERT INTO override_sets (user_id, board_slug) VALUES (?, ?)",
                (user_id, board_slug))
            set_id, version = int(cur.lastrowid), 0
        else:
            set_id, version = int(row["id"]), int(row["version"])
            if base_version != version:
                raise VersionConflict(version)
        # 覆蓋式寫入：pin_overrides 隨 override_peripherals ON DELETE CASCADE
        conn.execute("DELETE FROM override_peripherals WHERE set_id = ?", (set_id,))
        for og, p in periph_diff.items():
            cur = conn.execute(
                "INSERT INTO override_peripherals (set_id, official_group, "
                "group_name) VALUES (?, ?, ?)", (set_id, og, p["group_name"]))
            pid = int(cur.lastrowid)
            for osig, v in p["pins"].items():
                conn.execute(
                    "INSERT INTO pin_overrides (periph_id, official_signal, "
                    "signal, pin, af) VALUES (?, ?, ?, ?, ?)",
                    (pid, osig, v["signal"], v["pin"], v["af"]))
        new_version = version + 1
        conn.execute(
            "UPDATE override_sets SET version = ?, updated_at = datetime('now') "
            "WHERE id = ?", (new_version, set_id))
        conn.execute(
            "INSERT INTO override_history (set_id, snapshot_json) VALUES (?, ?)",
            (set_id, json.dumps({"version": new_version,
                                 "peripherals": periph_diff},
                                ensure_ascii=False)))
        conn.commit()
        return set_id, new_version
    except BaseException:
        conn.rollback()
        raise
    finally:
        conn.close()
