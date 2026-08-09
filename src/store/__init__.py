"""
store/ — 使用者系統資料層（USER_SYSTEM_PLAN §1）。

SQLite（WAL）單一資料庫 var/dts_agent.db；所有 SQL 集中在這個 package：
  db.py         連線 + schema（DB 路徑唯一常數，web 與 CLI 共用）
  users.py      使用者 DAO（密碼雜湊只在這層進出，永不外流）
  overrides.py  pin 覆寫 DAO（三層正規化 + gpio_must + history）
  create_admin  CLI：唯一的 admin 註冊途徑（python -m store.create_admin）
"""
