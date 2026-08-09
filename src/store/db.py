"""
db.py — SQLite 連線與 schema（使用者系統唯一資料庫入口）。

DB 路徑是**單一常數、刻意不提供環境變數覆寫**：web 與 CLI
（store.create_admin）都 import 這裡，「CLI 自動連上同一個資料庫」
由此保證（USER_SYSTEM_PLAN §5）。要搬家改 DB_PATH 一處。

放 var/（非 output/：output 是覆寫制可拋棄產物，使用者資料必須持久；
非 data/：那是知識庫）。secret_key 同放 var/，首次啟動自動生成。
"""
import os
import secrets
import sqlite3

HERE = os.path.dirname(os.path.abspath(__file__))               # src/store
VAR = os.path.normpath(os.path.join(HERE, "..", "..", "var"))
DB_PATH = os.path.join(VAR, "dts_agent.db")
SECRET_KEY_PATH = os.path.join(VAR, "secret_key")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    username      TEXT NOT NULL UNIQUE,
    display_name  TEXT NOT NULL DEFAULT '',
    role          TEXT NOT NULL CHECK (role IN ('admin', 'user')),
    password_hash TEXT NOT NULL,
    is_active     INTEGER NOT NULL DEFAULT 1,
    last_login_at TEXT,
    created_at    TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS override_sets (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id    INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    board_slug TEXT NOT NULL,
    name       TEXT NOT NULL DEFAULT '預設',
    is_active  INTEGER NOT NULL DEFAULT 1,
    version    INTEGER NOT NULL DEFAULT 0,
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_sets_user_board
    ON override_sets(user_id, board_slug);

CREATE TABLE IF NOT EXISTS override_peripherals (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    set_id         INTEGER NOT NULL REFERENCES override_sets(id) ON DELETE CASCADE,
    official_group TEXT NOT NULL,
    group_name     TEXT NOT NULL,
    UNIQUE (set_id, official_group)
);

CREATE TABLE IF NOT EXISTS pin_overrides (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    periph_id       INTEGER NOT NULL REFERENCES override_peripherals(id) ON DELETE CASCADE,
    official_signal TEXT NOT NULL,
    signal          TEXT NOT NULL,
    pin             TEXT NOT NULL,
    af              INTEGER,
    UNIQUE (periph_id, official_signal)
);

CREATE TABLE IF NOT EXISTS gpio_must_overrides (
    id     INTEGER PRIMARY KEY AUTOINCREMENT,
    set_id INTEGER NOT NULL REFERENCES override_sets(id) ON DELETE CASCADE,
    pin    TEXT NOT NULL,
    action TEXT NOT NULL CHECK (action IN ('add', 'remove')),
    UNIQUE (set_id, pin)
);

CREATE TABLE IF NOT EXISTS override_history (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    set_id        INTEGER NOT NULL REFERENCES override_sets(id) ON DELETE CASCADE,
    snapshot_json TEXT NOT NULL,
    saved_at      TEXT NOT NULL DEFAULT (datetime('now'))
);
"""


def connect() -> sqlite3.Connection:
    """開一條連線（schema 冪等自建）。呼叫端負責 close；每請求開新連線即可。"""
    os.makedirs(VAR, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.executescript(_SCHEMA)
    return conn


def load_secret_key() -> bytes:
    """Flask session 簽章金鑰：var/secret_key，首次啟動自動生成（不進 git）。"""
    os.makedirs(VAR, exist_ok=True)
    if not os.path.exists(SECRET_KEY_PATH):
        with open(SECRET_KEY_PATH, "wb") as f:
            f.write(secrets.token_bytes(32))
        os.chmod(SECRET_KEY_PATH, 0o600)
    with open(SECRET_KEY_PATH, "rb") as f:
        return f.read()
