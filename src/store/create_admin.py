"""
create_admin — admin 帳號的**唯一**註冊途徑（USER_SYSTEM_PLAN Part II）。

    PYTHONPATH=src venv/bin/python -m store.create_admin

web 上永遠不能產生 admin（含 admin 自己）——最高權限的發放綁在「拿得到
伺服器 shell」上。本工具與 web 共用 store/db.py 的同一路徑常數，
自動連上 var/dts_agent.db，不存在第二份設定。可重複執行建多個 admin；
也可作全帳號救援（密碼全忘 → 再建一個 admin 登入處理）。
"""
import getpass
import sys

from . import db, users


def main() -> None:
    print(f"建立 admin 帳號（寫入 {db.DB_PATH}）")
    username = input("username: ").strip()
    if not username:
        sys.exit("username 不可為空")
    display_name = input(f"display name（可空，預設 {username}）: ").strip()
    password = getpass.getpass("password: ")
    if len(password) < 6:
        sys.exit("密碼至少 6 個字元")
    if getpass.getpass("password（再一次）: ") != password:
        sys.exit("兩次密碼不一致")
    try:
        uid = users.create_user(username, display_name, password, role="admin")
    except ValueError as exc:
        sys.exit(f"建立失敗：{exc}")
    print(f"✔ admin '{username}' 建立完成（id={uid}）")


if __name__ == "__main__":
    main()
