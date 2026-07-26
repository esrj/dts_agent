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
import shutil
import sys
import threading
import time
import zipfile

# 既有 code 都用 `from util... / from solver...`（root 是 src），
# 所以把 src 加進 sys.path 讓 service / 底層模組可被 import。
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from flask import Flask, request, jsonify, send_from_directory, Response

from service import Pipeline
from util.dataio import (DEFAULT_BOARD, OUTPUT, list_boards,
                         load_board_manifest, plan_csv_text, plan_xlsx_bytes)
from orchestrator import Orchestrator, SessionStore, SolverTools
from validator import expected_pin_map, plan_fingerprint
import board_create

app = Flask(__name__, static_folder="static", static_url_path="")
app.config["MAX_CONTENT_LENGTH"] = 220 * 1024 * 1024   # 上傳（PDF＋DTS 樹）上限
app.register_blueprint(board_create.bp)                # 上傳新增板子（M3）
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
        _remember_plan(out["assignment"], board, out["plan_fingerprint"])
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
    """前端下拉選單用：自動偵測 data/ 下有哪些板子。
    names＝board.yaml 的 display name（缺 manifest 退回板子 id）；
    can_create＝「上傳新增板子」功能旗標（M3/M4）。"""
    available = list_boards()
    default = DEFAULT_BOARD if DEFAULT_BOARD in available else (
        available[0] if available else DEFAULT_BOARD)
    names = {}
    for b in available:
        try:
            names[b] = (load_board_manifest(b).get("name") or b)
        except Exception:
            names[b] = b
    return jsonify(boards=available, default=default, names=names,
                   can_create=board_create.FEATURE_ON)


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
        _remember_plan(reply.plan, sess.board, fp)
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


# --------------------------------------------------------------------------- #
# DTS patch 生成（兩段式流程的第二段：plan.csv -> kernel DTS patch）
#
# 防偽不變式（與 emit_plan / run_validator 同一條紅線）：/api/dts/generate 絕不
# 接受 client 提供的 rows——plan 來源只能是伺服器端保存的最後一份 SAT 解
# （_last_plan，由 /api/solve 與 /api/chat 的出口掛鉤寫入）。client 只傳
# fingerprint 指認「畫面上這份 plan」，與伺服器保存的不一致即拒絕（409），
# 保證 pipeline 讀到的 plan.csv 與使用者確認的內容一致。
#
# 執行模式：single-flight 背景執行緒（同時最多一個 DTS 生成工作；進行中再點
# 回 409），結果存 _dts["result"] 供 /api/dts/status 輪詢；產物全部落在
# output/generated/（patch_agent 既有的覆寫制目錄）。
# --------------------------------------------------------------------------- #
_last_plan = {"lock": threading.Lock(), "rows": None, "board": None, "fp": None}
_dts = {"lock": threading.Lock(), "running": False, "result": None,
        "started_fp": None}


def _remember_plan(rows, board, fp):
    """伺服器端保存「最新一份 SAT plan」——/api/dts/generate 的唯一 plan 來源。"""
    with _last_plan["lock"]:
        _last_plan["rows"] = copy.deepcopy(rows)
        _last_plan["board"] = board
        _last_plan["fp"] = fp


def _dts_available(board: str) -> bool:
    """該板具備 DTS patch 生成的知識庫（baseline.csv + baseline/dts/*.dts +
    dts_generation/，選配檔語意——缺夾時 plan 流程照常，只有本功能不可用）。
    多板：純路徑檢查（config.board_ready），不動 patch config 全域；
    實際切板在 _dts_worker 起跑時 init_board。"""
    try:
        from patch_agent import config as pconfig
    except Exception:
        return False
    return pconfig.board_ready(board)


def _dts_files() -> list:
    return _validator_files(os.path.join(OUTPUT, "generated"))


def _dts_worker(board: str, fp: str, snapshot_csv: str):
    started = time.time()
    try:
        from patch_agent import config as pconfig
        from patch_agent.cli import _write_locator_reports, summarize
        from patch_agent.m8_repairer import run as m8_run
        # 多板：single-flight 保證同時只有一個 DTS 工作，這裡是唯一允許
        # 切板（改 patch config 全域）的 web 呼叫點。
        pconfig.init_board(board)
        # 清掉上一輪 run 的產物檔（保留 llm_cache/ 與本輪的 plan.used.csv）：
        # 失敗的 run 不會覆寫全部檔案，若不清理，artifacts 會把舊 plan 的
        # generated.patch 掛在新 fingerprint 下（逆向驗證發現的錯標問題）。
        # *.generated.dts 用 glob——切板後別板的舊產物也要清，否則 zip 打包
        # 會把 A 板的 dts 混進 B 板的產物。
        for stale in (pconfig.GENERATED_PATCH,
                      pconfig.STRUCTURED_EDITS, pconfig.GENERATION_REPORT,
                      pconfig.VALIDATION_REPORT, pconfig.FAILURE_REPORT,
                      pconfig.DIFF_PLAN, pconfig.LOCATOR_REPORT,
                      *(pconfig.OUTPUT_GEN.glob("*.generated.dts")
                        if pconfig.OUTPUT_GEN.is_dir() else ())):
            try:
                stale.unlink()
            except FileNotFoundError:
                pass
        # dtc/gcc 缺席時退化為不編譯（結構檢查照跑），狀態如實回報 compiled=False
        run_compile = bool(shutil.which(pconfig.DTC)) and bool(shutil.which("gcc"))
        # pipeline 讀 run 專屬快照而非共用的 output/plan/plan.csv——後者隨時可能
        # 被並行的 /api/solve、emit_plan 覆寫（TOCTOU），會讓產物與 fingerprint
        # 指認的 plan 不一致。plan.used.csv 同時留在產物夾作為溯源紀錄。
        res = m8_run(plan_csv=snapshot_csv, write=True,
                     run_compile=run_compile)
        _write_locator_reports(res)                  # diff_plan / locator_report 落地
        result = {
            "passed": bool(res.passed),
            "no_changes": bool(getattr(res, "no_changes", False)),
            "stop_reason": res.stop_reason,
            "ask_user": [a if isinstance(a, (dict, str)) else str(a)
                         for a in (res.ask_user or [])],
            "repair_rounds": res.rounds,
            "compiled": run_compile,
            "checks": dict(res.validation.checks) if res.validation is not None else {},
            "peripherals": list((res.art.report.get("peripherals") or [])
                                if res.art is not None else []),
            "summary": summarize(res),
        }
        # 全程未呼叫 LLM（純 deterministic 路徑，通常 <1 秒就跑完）時，補一段
        # 等待再回報——使用者要求生成過程至少呈現約 30 秒的進行狀態，避免
        # 「按下去瞬間完成」的體感落差。有呼叫 LLM（含修復輪）就不加。
        used_llm = (any(p.get("lm_used") for p in result["peripherals"])
                    or bool(res.repair_usage))
        if not used_llm:
            time.sleep(30)
    except Exception as exc:                          # pipeline 例外（缺 key、壞資料…）
        result = {"passed": False, "stop_reason": "exception", "error": str(exc),
                  "ask_user": [], "repair_rounds": 0, "compiled": False,
                  "checks": {}, "peripherals": [], "summary": ""}
    result.update(fingerprint=fp, board=board,
                  ran_at=time.strftime("%Y-%m-%dT%H:%M:%S"),
                  seconds=round(time.time() - started, 1),
                  artifacts=_dts_files())
    with _dts["lock"]:
        _dts["result"] = result
        _dts["running"] = False
        _dts["started_fp"] = None


@app.post("/api/dts/generate")
def dts_generate():
    """以伺服器保存的最後一份 SAT plan 產生 kernel DTS patch（背景執行）。
    請求：{fingerprint}——畫面上那份 plan 的指紋（防偽比對用）。"""
    data = request.get_json(silent=True) or {}
    fp = data.get("fingerprint")
    with _last_plan["lock"]:
        rows = _last_plan["rows"]
        board = _last_plan["board"]
        have_fp = _last_plan["fp"]
    if not rows:
        return jsonify(error="伺服器尚無已求解的 plan——請先求解一次。"), 409
    if not fp:
        # fingerprint 必填：紅線是「client 指認畫面上的 plan、與伺服器保存解
        # 不符即拒絕」；缺席視同無法指認，不得當萬用符通過。
        return jsonify(error="缺少 plan fingerprint——請從最新求解結果的"
                             "「產生 DTS」按鈕觸發。"), 409
    if fp != have_fp:
        return jsonify(error="這份 plan 已不是伺服器上最新的解——"
                             "請重新求解後，再從最新結果產生 DTS。"), 409
    if not _dts_available(board):
        return jsonify(error=f"板子 {board} 缺少 DTS 生成知識庫"
                             "（data/<board>/baseline/ 與 dts_generation/）。"), 404
    with _dts["lock"]:
        if _dts["running"]:
            return jsonify(error="已有一個 DTS 生成工作進行中，請稍候。",
                           running=True), 409
        _dts["running"] = True
        _dts["started_fp"] = have_fp
    try:
        csv_text = plan_csv_text(rows)
        # 交棒介面落地：plan.csv 只寫伺服器保存的 rows（防偽），覆寫制單一位置
        plan_dir = os.path.join(OUTPUT, "plan")
        os.makedirs(plan_dir, exist_ok=True)
        with open(os.path.join(plan_dir, "plan.csv"), "w", newline="",
                  encoding="utf-8") as fh:
            fh.write(csv_text)
        # 另存 run 專屬快照給 pipeline 讀（plan.csv 隨時可能被並行的求解覆寫；
        # 快照留在產物夾兼作「這份 patch 是根據哪份 plan 生成」的溯源紀錄）
        gen_dir = os.path.join(OUTPUT, "generated")
        os.makedirs(gen_dir, exist_ok=True)
        snapshot_csv = os.path.join(gen_dir, "plan.used.csv")
        with open(snapshot_csv, "w", newline="", encoding="utf-8") as fh:
            fh.write(csv_text)
        threading.Thread(target=_dts_worker, args=(board, have_fp, snapshot_csv),
                         daemon=True).start()
    except Exception as exc:                     # 落地/啟動失敗：釋放 single-flight
        with _dts["lock"]:
            _dts["running"] = False
            _dts["started_fp"] = None
        return jsonify(error=f"無法啟動 DTS 生成：{exc}"), 500
    return jsonify(started=True, fingerprint=have_fp), 202


@app.get("/api/dts/status")
def dts_status():
    """DTS 生成狀態（前端輪詢）：{available, running, started_fingerprint,
    result}。result.fingerprint 讓前端把結果對回正確的 plan（比照 CubeMX
    validator 的 fingerprint 機制）。"""
    board = request.args.get("board") or DEFAULT_BOARD
    with _dts["lock"]:
        running = _dts["running"]
        started_fp = _dts["started_fp"]
        result = _dts["result"]
    return jsonify(available=_dts_available(board), running=running,
                   started_fingerprint=started_fp, result=result)


@app.get("/api/dts/file")
def dts_file():
    """回傳單一產物檔的純文字內容（前端行內展開看程式碼用）。name 一律以
    basename 比對白名單（只有 generated.patch 與生成的 .dts 可讀），杜絕路徑
    穿越。無檔 → 404。"""
    try:
        from patch_agent import config as pconfig
    except Exception:
        return jsonify(error="DTS 功能不可用"), 404
    # 白名單改 pattern：generated.patch 或任意 <board>.generated.dts（多板：
    # 產物名隨板變，且伺服器重啟後 config.BOARD 可能不是產物所屬板）。
    # 一律取 basename 再對 OUTPUT_GEN 拼相對路徑，杜絕路徑穿越。
    name = os.path.basename(request.args.get("name") or "")
    if name == pconfig.GENERATED_PATCH.name or name.endswith(".generated.dts"):
        path = pconfig.OUTPUT_GEN / name
    else:
        return jsonify(error="不允許的檔名"), 400
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return jsonify(error="檔案尚未生成"), 404
    return Response(text, mimetype="text/plain; charset=utf-8")


@app.get("/api/dts/download")
def dts_download():
    """打包 output/generated/（generated.patch、*.generated.dts、各 report、
    llm_cache/）為 zip。無產物 → 404。"""
    gdir = os.path.join(OUTPUT, "generated")
    files = _validator_files(gdir)
    if not files:
        return jsonify(error="尚無 DTS 產物——請先執行一次「產生 DTS」。"), 404
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for f in files:
            zf.write(os.path.join(gdir, f), arcname=f)
    buf.seek(0)
    return Response(
        buf.read(), mimetype="application/zip",
        headers={"Content-Disposition":
                 "attachment; filename=dts_patch_artifacts.zip"})


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5001))
    app.run(host="127.0.0.1", port=port, debug=True)
