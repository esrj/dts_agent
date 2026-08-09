"""
auth.py — 登入/登出與全站保護（USER_SYSTEM_PLAN Part I）。

- Session：Flask 簽章 cookie（HttpOnly＋SameSite=Lax），內容只放 user id；
  每請求由 DB 重查 user（含 is_active——被停用的帳號下一個請求即失效）。
- 保護範圍：before_request 全站攔截；白名單只有登入端點、/api/me
  （自帶 401 語意）、`/` 與靜態資源（登入畫面就在 index.html 的遮罩層，
  HTML/JS 本身不含資料）。其餘一律未登入回 401。
- 暴力嘗試防護：同 username 連續失敗 5 次鎖 60 秒（in-memory，單機夠用；
  多 worker 部署時需搬 DB/Redis——見計劃 §5）。
- 密碼驗證在 store/users.py（werkzeug 雜湊）；登入失敗訊息統一
  「帳號或密碼錯誤」，不洩漏帳號是否存在。
"""
import threading
import time
from datetime import timedelta

from flask import Blueprint, g, jsonify, request, session

from store import db as store_db
from store import users as store_users

bp = Blueprint("auth", __name__)

_MAX_FAILS = 5
_LOCK_SECONDS = 60
_fails: dict[str, dict] = {}          # username -> {"n": int, "until": ts}
_fails_lock = threading.Lock()

# 未登入也放行的 endpoint（Flask endpoint 名）：靜態資源與首頁是登入畫面的
# 載體；auth.me 回 401＋bootstrap 資訊，是前端開機的第一個呼叫。
_PUBLIC_ENDPOINTS = {"static", "index", "auth.login", "auth.me"}


def current_user() -> dict | None:
    """session cookie -> DB user dict（無 hash）；未登入/已停用 -> None。"""
    uid = session.get("uid")
    if uid is None:
        return None
    user = store_users.get_by_id(uid)
    if user is None or not user["is_active"]:
        return None
    return user


def require_admin():
    """非 admin 回 (json, 403)；admin 回 None。呼叫端：err = require_admin();
    if err: return err。g.user 由 before_request 掛好。"""
    user = getattr(g, "user", None)
    if user is None or user["role"] != "admin":
        return jsonify(error="需要 admin 權限"), 403
    return None


def _locked_for(username: str) -> int:
    """該帳號還要鎖幾秒（0＝未鎖）。"""
    with _fails_lock:
        rec = _fails.get(username)
        if not rec:
            return 0
        remain = rec.get("until", 0) - time.time()
        return int(remain) + 1 if remain > 0 else 0


def _record_attempt(username: str) -> None:
    """在驗證**之前**記一次嘗試（成功才清零）：check-then-record 若拆兩步，
    並發突刺會在第一筆失敗落帳前全部通過檢查，5 次上限形同虛設。"""
    with _fails_lock:
        if len(_fails) > 1024:                    # 防無限成長：清掉過期項
            now = time.time()
            for k in [k for k, v in _fails.items()
                      if v.get("until", 0) < now and v.get("n", 0) == 0]:
                _fails.pop(k, None)
        rec = _fails.setdefault(username, {"n": 0, "until": 0})
        rec["n"] += 1
        if rec["n"] >= _MAX_FAILS:
            rec["n"] = 0
            rec["until"] = time.time() + _LOCK_SECONDS


def _clear_fails(username: str) -> None:
    with _fails_lock:
        _fails.pop(username, None)


@bp.post("/api/login")
def login():
    data = request.get_json(silent=True) or {}
    username = (data.get("username") or "").strip()
    password = data.get("password") or ""
    if not username or not password:
        return jsonify(error="帳號或密碼錯誤"), 401
    wait = _locked_for(username)
    if wait:
        return jsonify(error=f"嘗試次數過多，請 {wait} 秒後再試"), 429
    _record_attempt(username)                # 先記後驗（成功即清零）
    user = store_users.authenticate(username, password)
    if user is None:
        return jsonify(error="帳號或密碼錯誤"), 401
    _clear_fails(username)
    session.clear()
    session["uid"] = user["id"]
    session.permanent = True                     # 7 天（init_app 設定）
    return jsonify(username=user["username"], display_name=user["display_name"],
                   role=user["role"])


@bp.post("/api/logout")
def logout():
    session.clear()
    return jsonify(ok=True)


# --------------------------------------------------------------------------- #
# 使用者管理（Part II）：全部 admin-only。web 上永遠建不出 admin（含 admin
# 自己）——role 參數一律拒收，admin 帳號的建立/管理只在 CLI（store.create_admin）。
# --------------------------------------------------------------------------- #
@bp.get("/api/users")
def users_list():
    err = require_admin()
    if err:
        return err
    return jsonify(users=store_users.list_users())


@bp.post("/api/users")
def users_create():
    err = require_admin()
    if err:
        return err
    data = request.get_json(silent=True) or {}
    if "role" in data:
        return jsonify(error="不接受 role 參數——web 只能建立一般 user；"
                             "admin 請在伺服器執行 store.create_admin"), 400
    username = (data.get("username") or "").strip()
    password = data.get("password") or ""
    if not username:
        return jsonify(error="username 不可為空"), 400
    if len(password) < 6:
        return jsonify(error="初始密碼至少 6 個字元"), 400
    try:
        uid = store_users.create_user(username,
                                      (data.get("display_name") or "").strip(),
                                      password, role="user")
    except ValueError as exc:
        return jsonify(error=str(exc)), 400
    return jsonify(id=uid, ok=True), 201


@bp.patch("/api/users/<int:uid>")
def users_patch(uid: int):
    """{password?} 重設密碼／{is_active?} 停用/啟用。只能管理一般 user——
    admin 帳號（含自己）一律 CLI 管理，web 不能改 role、不能停用 admin。"""
    err = require_admin()
    if err:
        return err
    data = request.get_json(silent=True) or {}
    if "role" in data:
        return jsonify(error="不可變更角色"), 400
    target = store_users.get_by_id(uid)
    if target is None:
        return jsonify(error="使用者不存在"), 404
    if target["role"] == "admin":
        return jsonify(error="admin 帳號僅能在伺服器 CLI 管理"
                             "（store.create_admin 可另建 admin 救援）"), 403
    if "is_active" in data:
        store_users.set_active(uid, bool(data["is_active"]))
    if "password" in data:                       # 帶了 key 就必須合法——空字串
        pw = data.get("password") or ""          # 不得被靜默忽略又回報成功
        if len(pw) < 6:
            return jsonify(error="密碼至少 6 個字元"), 400
        store_users.set_password(uid, pw)
    return jsonify(ok=True, user=store_users.get_by_id(uid))


@bp.get("/api/me")
def me():
    """前端開機第一個呼叫：200＝已登入進主畫面；401＝顯示登入畫面。
    users_exist=false 時登入頁顯示「請先在伺服器執行 create_admin」引導
    （bootstrap，計劃 I.4）——只透露系統是否已初始化，不透露任何帳號。"""
    user = current_user()
    if user is None:
        return jsonify(error="未登入",
                       users_exist=store_users.any_user_exists()), 401
    return jsonify(username=user["username"], display_name=user["display_name"],
                   role=user["role"])


def init_app(app) -> None:
    """掛上 secret key、session cookie 政策與全站 before_request 保護。"""
    app.secret_key = store_db.load_secret_key()
    app.permanent_session_lifetime = timedelta(days=7)
    app.config["SESSION_COOKIE_HTTPONLY"] = True
    app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
    app.register_blueprint(bp)

    @app.before_request
    def _require_login():
        if request.endpoint in _PUBLIC_ENDPOINTS:
            return None
        user = current_user()
        if user is None:
            return jsonify(error="未登入"), 401
        g.user = user
        return None
