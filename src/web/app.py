"""
app.py — Flask entry point。

只負責 HTTP <-> service：載入 Pipeline 一次，把 POST 進來的文字丟給它，
回傳結果 JSON。求解邏輯都在 service.py / solver / llm_provider。

啟動：
    source venv/bin/activate
    python src/web/app.py        # http://127.0.0.1:5001

注意：macOS 的 5000 埠預設被 AirPlay Receiver 佔用（會回 403），
所以預設用 5001；可用環境變數 PORT 覆寫。
"""
import copy
import io
import json
import os
import sys
import threading
import zipfile

# 既有 code 都用 `from util... / from solver...`（root 是 src），
# 所以把 src 加進 sys.path 讓 service / 底層模組可被 import。
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from flask import Flask, request, jsonify, send_from_directory, Response

from service import Pipeline
from util.dataio import (DEFAULT_BOARD, OUTPUT, list_boards, plan_csv_text,
                         plan_xlsx_bytes)
from orchestrator import Orchestrator, SessionStore, SolverTools
from validator import expected_pin_map, plan_fingerprint

app = Flask(__name__, static_folder="static", static_url_path="")
pipeline = Pipeline()        # 啟動時載入一次（provider / system_prompt；各板資料延後載入並快取）

# 聊天式 orchestrator（agentic tool-use loop）。延後初始化：第一個 /api/chat 請求才
# 建立，避免在沒有 ANTHROPIC_API_KEY 的環境下影響既有 /api/solve（deterministic）路徑。
_sessions = SessionStore()
_orchestrator = None

# --------------------------------------------------------------------------- #
# 自動 CubeMX 驗證：每個新 SAT plan 都在背景驗一次（使用者要求：每次生成 plan
# 都要 validate）。單一工作執行緒 + latest-wins：密集求解時只保證「最新的 plan
# 一定被驗到」，中間過渡 plan 可被跳過；tools.run_validator 內部有全域鎖，
# 與模型顯式驗證不會互踩產物目錄。
# --------------------------------------------------------------------------- #
_vtools = SolverTools.__new__(SolverTools)   # 共用既有 pipeline 的板子快取，
_vtools._pipeline = pipeline                 # 不另建 LLM provider
_auto = {"lock": threading.Lock(), "running": False, "pending": None}


def _auto_worker():
    while True:
        with _auto["lock"]:
            job, _auto["pending"] = _auto["pending"], None
            if job is None:
                _auto["running"] = False
                return
        rows, board = job
        try:
            _vtools.run_validator(rows, board)     # 產物+result.json 覆寫落地
        except Exception:
            pass                                   # 背景驗證失敗不影響對話路徑


def _kick_validation(rows, board):
    """排入一份 plan 的背景驗證（latest-wins）。"""
    if not rows:
        return
    with _auto["lock"]:
        _auto["pending"] = (copy.deepcopy(rows), board)
        if _auto["running"]:
            return                                 # 現役 worker 跑完會接手 pending
        _auto["running"] = True
    threading.Thread(target=_auto_worker, daemon=True).start()


def _validating() -> bool:
    with _auto["lock"]:
        return _auto["running"] or _auto["pending"] is not None


def _with_auto_validation(out, board):
    """/api/solve 的出口掛鉤：SAT 且有 assignment → 附 plan_fingerprint 並排入
    背景驗證（含 baseline——它也是一份 plan）。"""
    if isinstance(out, dict) and out.get("sat") and out.get("assignment"):
        out["plan_fingerprint"] = plan_fingerprint(expected_pin_map(out["assignment"]))
        _kick_validation(out["assignment"], board)
    return out


def _get_orchestrator():
    global _orchestrator
    if _orchestrator is None:
        _orchestrator = Orchestrator()
    return _orchestrator


@app.get("/")
def index():
    return send_from_directory(app.static_folder, "index.html")


@app.get("/api/boards")
def boards():
    """前端下拉選單用：自動偵測 data/boards/ 下有哪些板子。"""
    available = list_boards()
    default = DEFAULT_BOARD if DEFAULT_BOARD in available else (
        available[0] if available else DEFAULT_BOARD)
    return jsonify(boards=available, default=default)


@app.post("/api/solve")
def solve():
    data = request.get_json(silent=True) or {}
    board = data.get("board") or DEFAULT_BOARD   # 無狀態：board 每輪都帶進來
    try:
        if "intent" in data:                 # 反問的後續輪：折回使用者選的候選
            return jsonify(_with_auto_validation(
                pipeline.answer(data, board=board), board))

        # 第一輪：自然語言
        text = (data.get("text") or "").strip()
        if not text:
            return jsonify(error="empty input"), 400
        return jsonify(_with_auto_validation(
            pipeline.start(text, board=board), board))
    except Exception as exc:      # ResolveError / LLM / JSON 解析失敗
        return jsonify(error=str(exc)), 422


def _adopt_message(adopt: dict) -> str:
    """建議卡片「一鍵採納」→ 折成一則標準使用者訊息（intent 原樣入訊，模型只需
    照著送 solve_pinmux；方案本身在提案時已被伺服器驗證過 SAT）。"""
    summary = (adopt.get("summary") or "").strip() or "（未命名方案）"
    intent = adopt.get("intent") or {}
    return (f"我採納建議方案：「{summary}」。\n"
            "請直接用下面這個 intent 呼叫 solve_pinmux 求解（不要改動它），"
            "然後照常回報結果：\n"
            f"```json\n{json.dumps(intent, ensure_ascii=False)}\n```")


@app.post("/api/chat")
def chat():
    """聊天式 orchestrator 入口（有狀態：以 session_id 在伺服器端保存對話）。

    請求：{session_id?, message?, adopt?, board?}
      adopt : {summary, intent} —— 建議卡片一鍵採納；折成標準訊息走同一條路。
    回應：{session_id, reply, plan?, suggestions?, clarify?, validator?, trace?}
      reply : 助理的人話回覆（含「為什麼」）。
      plan  : 最近一次 SAT 的 assignment（[{peripheral,signal,pin,af,ic,...}]）；
              非空時前端用既有表格 render，並可打 /api/export 匯出。
      suggestions : 本輪「驗證過可 SAT」的建議卡片 [{summary, intent}]。
      clarify : 本輪待答的歧義 {summary, kind, options[...]}——前端渲染成
              選項按鈕（與 /api/solve 的反問同一套 UI），點選即送回對話。
    """
    data = request.get_json(silent=True) or {}
    adopt = data.get("adopt")
    message = (data.get("message") or "").strip()
    if not message and isinstance(adopt, dict):
        message = _adopt_message(adopt)
    if not message:
        return jsonify(error="empty message"), 400
    board = data.get("board") or DEFAULT_BOARD
    sess = _sessions.get_or_create(data.get("session_id"), board)
    if data.get("board") and data["board"] != sess.board:
        # 中途切板：對話歷史是針對舊板算的（Σ/DTS/腳位都不同），不能與新板的 system
        # prompt 並存，否則模型會拿舊板結論套到新板。直接重置這個 session 的對話。
        sess.board = data["board"]
        sess.messages = []
        sess.trace = []
        sess.last_plan = None
    turn_start = len(sess.trace)                 # 本輪 trace 的起點（見 validator）
    try:
        reply = _get_orchestrator().step(sess, message)
    except Exception as exc:                     # LLM / 設定 / 工具層失敗
        return jsonify(error=str(exc), session_id=sess.session_id), 502
    # 只取「本輪」的 CubeMX 驗證結果——跨輪的舊結果不可污染新 plan 的徽章。
    turn_validator = None
    for t in sess.trace[turn_start:]:
        if t.get("kind") == "tool" and t.get("name") == "run_validator":
            turn_validator = t.get("output")
    # 每個新 plan 自動排入背景驗證；本輪模型已顯式驗過（turn_validator）就不重跑。
    fp = None
    if reply.plan:
        fp = plan_fingerprint(expected_pin_map(reply.plan))
        if turn_validator is None:
            _kick_validation(reply.plan, sess.board)
    return jsonify(
        session_id=sess.session_id,
        board=sess.board,
        reply=reply.text,
        plan=reply.plan or [],
        plan_fingerprint=fp,                     # 前端據此對上自動驗證的 result
        suggestions=reply.suggestions,           # 驗證過的建議卡片（可一鍵採納）
        clarify=reply.clarify,                   # 待答歧義（前端渲染選項按鈕；無則 null）
        validator=turn_validator,                # 本輪驗證結果（無則 null）
        trace=sess.trace[-12:],                  # 末段 trace，供前端 debug（可忽略）
    )


# --------------------------------------------------------------------------- #
# validator（G5/G7）：狀態徽章資料 + 產物 zip 下載
# --------------------------------------------------------------------------- #
@app.get("/api/validator/status")
def validator_status():
    """最近一次 CubeMX 驗證的結果摘要（output/validator/result.json，覆寫制）。
    前端據此渲染 pass / fail / 未驗證 徽章與衝突清單、決定下載鈕啟用。"""
    vdir = os.path.join(OUTPUT, "validator")
    result_path = os.path.join(vdir, "result.json")
    running = _validating()                      # 背景自動驗證進行中（前端輪詢用）
    if not os.path.isfile(result_path):
        return jsonify(exists=False, downloadable=False, running=running)
    try:
        with open(result_path, encoding="utf-8") as fh:
            result = json.load(fh)
    except (OSError, ValueError) as exc:
        return jsonify(exists=False, downloadable=False, running=running,
                       error=str(exc))
    files = _validator_files(vdir)
    return jsonify(exists=True, result=result, files=files, running=running,
                   downloadable=any(f != "result.json" for f in files))


def _validator_files(vdir: str) -> list:
    """output/validator/ 全部產物的相對路徑（遞迴：devicetree/kernel/*.dts、
    project/… 都涵蓋），排序穩定。"""
    out = []
    if os.path.isdir(vdir):
        for root, _dirs, files in os.walk(vdir):
            for f in files:
                out.append(os.path.relpath(os.path.join(root, f), vdir))
    return sorted(out)


@app.get("/api/validator/download")
def validator_download():
    """把 output/validator/ 打包成 zip 回傳（log、pinout.csv、script、result.json、
    devicetree/ 下的 kernel/u-boot/tf-a/optee-os DT…）。無產物 → 404（前端把按鈕
    disable）。"""
    vdir = os.path.join(OUTPUT, "validator")
    files = _validator_files(vdir)
    if not files:
        return jsonify(error="尚無驗證產物——請先執行一次 CubeMX 驗證。"), 404
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for f in files:
            zf.write(os.path.join(vdir, f), arcname=f)
    buf.seek(0)
    return Response(
        buf.read(), mimetype="application/zip",
        headers={"Content-Disposition":
                 "attachment; filename=stm32cubemx_validation.zip"})


@app.post("/api/export")
def export():
    """把某一則結果的 assignment 匯出成 csv / xlsx 下載（per-message，不依賴
    伺服器上最後寫出的檔），重用 write_plan 同一套 grouped 樣式。"""
    data = request.get_json(silent=True) or {}
    rows = data.get("rows") or []
    fmt = (data.get("format") or "csv").lower()
    if not rows:
        return jsonify(error="no rows to export"), 400
    try:
        if fmt == "csv":
            return Response(
                plan_csv_text(rows).encode("utf-8-sig"),   # BOM -> Excel 開中文不亂碼
                mimetype="text/csv",
                headers={"Content-Disposition": "attachment; filename=plan.csv"})
        if fmt == "xlsx":
            return Response(
                plan_xlsx_bytes(rows),
                mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                headers={"Content-Disposition": "attachment; filename=plan.xlsx"})
    except Exception as exc:
        return jsonify(error=str(exc)), 500
    return jsonify(error=f"unknown format: {fmt}"), 400


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5001))
    app.run(host="127.0.0.1", port=port, debug=True)
