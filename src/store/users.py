"""
users.py — 使用者 DAO。

密碼雜湊（werkzeug generate/check_password_hash）只在這層進出：
對外回傳的 user dict 一律不含 password_hash，驗證走 authenticate()。
任何地方不存、不記 log 明文密碼。
"""
from __future__ import annotations

import sqlite3

from werkzeug.security import check_password_hash, generate_password_hash

from . import db

_PUBLIC_COLS = "id, username, display_name, role, is_active, last_login_at, created_at"

# 時間拉平用的假雜湊：帳號不存在/已停用時也做一次 check_password_hash，
# 否則回應時間差（無雜湊 ~1ms vs scrypt 數十 ms）就是帳號枚舉 oracle，
# 「帳號或密碼錯誤」的統一訊息會被計時打穿。
_DUMMY_HASH = generate_password_hash("timing-equalizer-not-a-real-password")


def _row_to_dict(row: sqlite3.Row | None) -> dict | None:
    return dict(row) if row is not None else None


def create_user(username: str, display_name: str, password: str,
                role: str = "user") -> int:
    """建帳號；username 撞名丟 ValueError。回傳新 user id。"""
    if role not in ("admin", "user"):
        raise ValueError(f"未知角色: {role}")
    if not username or not password:
        raise ValueError("username 與 password 不可為空")
    conn = db.connect()
    try:
        cur = conn.execute(
            "INSERT INTO users (username, display_name, role, password_hash) "
            "VALUES (?, ?, ?, ?)",
            (username, display_name or username, role,
             generate_password_hash(password)),
        )
        conn.commit()
        return int(cur.lastrowid)
    except sqlite3.IntegrityError:
        raise ValueError(f"username '{username}' 已存在") from None
    finally:
        conn.close()


def authenticate(username: str, password: str) -> dict | None:
    """帳密正確且帳號啟用 → user dict（無 hash）；否則 None（不區分原因）。"""
    conn = db.connect()
    try:
        row = conn.execute(
            "SELECT * FROM users WHERE username = ?", (username,)
        ).fetchone()
        if row is None or not row["is_active"]:
            check_password_hash(_DUMMY_HASH, password)   # 時間拉平（見上）
            return None
        if not check_password_hash(row["password_hash"], password):
            return None
        conn.execute(
            "UPDATE users SET last_login_at = datetime('now') WHERE id = ?",
            (row["id"],),
        )
        conn.commit()
        pub = conn.execute(
            f"SELECT {_PUBLIC_COLS} FROM users WHERE id = ?", (row["id"],)
        ).fetchone()
        return _row_to_dict(pub)
    finally:
        conn.close()


def get_by_id(uid: int) -> dict | None:
    conn = db.connect()
    try:
        row = conn.execute(
            f"SELECT {_PUBLIC_COLS} FROM users WHERE id = ?", (uid,)
        ).fetchone()
        return _row_to_dict(row)
    finally:
        conn.close()


def list_users() -> list[dict]:
    conn = db.connect()
    try:
        rows = conn.execute(
            f"SELECT {_PUBLIC_COLS} FROM users ORDER BY id"
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def any_user_exists() -> bool:
    """Bootstrap 判斷：一個帳號都沒有時，登入頁顯示 create_admin 引導。"""
    conn = db.connect()
    try:
        return conn.execute("SELECT 1 FROM users LIMIT 1").fetchone() is not None
    finally:
        conn.close()


def set_password(uid: int, password: str) -> None:
    if not password:
        raise ValueError("password 不可為空")
    conn = db.connect()
    try:
        cur = conn.execute(
            "UPDATE users SET password_hash = ? WHERE id = ?",
            (generate_password_hash(password), uid),
        )
        if cur.rowcount == 0:
            raise ValueError(f"user id {uid} 不存在")
        conn.commit()
    finally:
        conn.close()


def set_active(uid: int, active: bool) -> None:
    conn = db.connect()
    try:
        cur = conn.execute(
            "UPDATE users SET is_active = ? WHERE id = ?",
            (1 if active else 0, uid),
        )
        if cur.rowcount == 0:
            raise ValueError(f"user id {uid} 不存在")
        conn.commit()
    finally:
        conn.close()
