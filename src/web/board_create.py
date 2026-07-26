"""board_create.py — 「上傳新增板子」web 流程（EXTRACTOR_MERGE_PLAN M3）。

上傳 手冊 PDF＋整包 DTS＋板子名稱（＋是否啟用 plan 驗證）→ single-flight
背景 job 跑 knowledge_extract（manual＋dts 步驟）→ 兩道 lint gate →
**lint 全綠直接原子落地** data/<slug>/ → 板子自動出現。
（REVIEW.md 與 boot 判定表留作落地後資訊；要改判 emit/reserve：編輯
data/<slug>/base/require.json 後跑 tools/kb_lint.py 重新驗證。）

紅線：
  - extractor 只寫 output/staging/<slug>/（D3）；落地是本模組的原子搬移。
  - lint FAIL 絕不落地（含 baseline 殼檔 gate：enabled 週邊過低即擋）。
  - FEATURE_BOARD_CREATE=0 時本 Blueprint 全部端點回 404（D8）。

執行模式：一次一個 job（single-flight，同 /api/dts/generate 慣例）；
extractor 的手冊抽取是分鐘～數十分鐘級，前端輪詢 status、關頁可回來續看。
"""
from __future__ import annotations

import json
import os
import re
import shutil
import threading
import time
import zipfile

from flask import Blueprint, jsonify, request, send_file

from util.dataio import DATA

bp = Blueprint("board_create", __name__)

FEATURE_ON = os.environ.get("FEATURE_BOARD_CREATE", "1") == "1"

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
STAGING_ROOT = os.path.join(_ROOT, "output", "staging")

_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]{1,63}$")
_MAX_PDF = 80 * 1024 * 1024          # 80MB
_MAX_DTS_TOTAL = 120 * 1024 * 1024   # 解包後總量上限

_job = {
    "lock": threading.Lock(),
    "running": False,
    "stage": None,        # unpack|manual|dts|board_yaml|lint|review_wait|landing|done|failed
    "slug": None,
    "name": None,
    "validate": False,
    "overwrite": False,
    "error": None,
    "lint": None,         # DTS_agent kb_lint 結果 {ok, fails, warns, findings}
    "review": None,       # {"boot_groups": {...}, "review_md": str, "needs_emit_confirm": bool}
    "landed": None,       # 落地完成的 slug（前端刷新選單用）
}


def _pub():
    """status 回應（不含 lock 等內部欄位）。"""
    out = {k: _job.get(k) for k in ("running", "stage", "slug", "name",
                                    "validate", "error", "lint", "review",
                                    "landed", "note", "tokens")}
    started = _job.get("started_at")
    ended = _job.get("ended_at")            # done/failed 時凍結，完成畫面顯示最終值
    out["elapsed"] = int((ended or time.time()) - started) if started else None
    return out


class _UsageTracker:
    """包住 LLM provider：每次 complete/complete_raw 後把 usage 累進 _job，
    前端等待畫面即時顯示「已消耗 tokens」。其餘屬性全部透傳。"""

    def __init__(self, real):
        self._real = real

    def __getattr__(self, item):
        attr = getattr(self._real, item)
        if item in ("complete", "complete_raw") and callable(attr):
            def wrapped(*a, **kw):
                resp = attr(*a, **kw)
                u = getattr(resp, "usage", None) or {}
                tot = (u.get("total_tokens")
                       or (u.get("prompt_tokens", 0) + u.get("completion_tokens", 0))
                       or 0)
                with _job["lock"]:
                    _job["tokens"] = (_job.get("tokens") or 0) + int(tot)
                return resp
            return wrapped
        return attr


def _set(**kw):
    with _job["lock"]:
        _job.update(kw)


def _slugify(name: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", (name or "").strip().lower()).strip("-")
    return s


def _staging(slug: str) -> str:
    return os.path.join(STAGING_ROOT, slug)


def _safe_relpath(p: str) -> str | None:
    """上傳檔名（可含資料夾相對路徑）→ 安全相對路徑；不安全回 None。"""
    p = (p or "").replace("\\", "/").lstrip("/")
    norm = os.path.normpath(p)
    if not norm or norm.startswith("..") or os.path.isabs(norm):
        return None
    return norm


# --------------------------------------------------------------------------- #
# 背景 job
# --------------------------------------------------------------------------- #
def _worker(slug: str, name: str, validate: bool, baseline: str | None = None):
    stage_dir = _staging(slug)
    input_dir = os.path.join(stage_dir, "input")
    try:
        # baseline 板檔（上傳含多個 .dts 時由前端指定）先登錄 boards.ini，
        # resolve_baseline 才不會因多候選而要求人工改檔
        if baseline:
            from knowledge_extract import identify
            identify.save_baseline(slug, os.path.basename(baseline))
        # ---- manual：手冊抽取（af/profiles/require；LLM，分鐘級起跳） ----
        _set(stage="manual")
        from llm_provider import get_provider
        from knowledge_extract import pipeline as ke_pipeline
        provider = _UsageTracker(
            get_provider(module="knowledge_extract", allow_mock=False))
        pdf_name = next(f for f in os.listdir(input_dir)
                        if f.lower().endswith(".pdf"))
        report = ke_pipeline.run_manual(
            pdf_name, provider,
            steps={"af", "profiles", "require"},
            out_dir=STAGING_ROOT, board_override=slug,
            input_dir=input_dir, force=True)
        hard = [w for w in report.get("warnings", [])
                if "找不到 pinmux" in w or "結果為空" in w]
        if hard:
            raise RuntimeError("手冊抽取失敗：" + "；".join(hard))

        # ---- dts：DTS 解析＋知識庫生成（純程式）＋ extractor 出廠 lint ----
        _set(stage="dts")
        from knowledge_extract import dts_extract
        dts_dir = dts_extract.find_dts_dir(os.path.join(input_dir, "dts"))
        if dts_dir is None:
            raise RuntimeError("上傳的 DTS 資料夾中找不到 .dts 檔")
        dts_report = dts_extract.run_board(
            slug, dts_dir=dts_dir, out_dir=STAGING_ROOT, force=True)
        ke_lint = dts_report.get("lint") or {}
        if ke_lint.get("fails"):
            raise RuntimeError("extractor 出廠 lint FAIL："
                               + "；".join(ke_lint["fails"][:5]))

        # ---- board.yaml：display name ＋ 驗證勾選（D9） ----
        _set(stage="board_yaml")
        _apply_board_yaml(stage_dir, slug, name, validate)

        # ---- lint：DTS_agent 進場檢查（第二道 gate） ----
        _set(stage="lint")
        lint = _run_lint(slug)
        if not lint["ok"]:
            _set(stage="failed", lint=lint, running=False,
                 error="kb_lint FAIL——staging 保留供除錯（可下載產物）",
                 ended_at=time.time())
            return

        # ---- lint 綠 → 直接落地（使用者要求：上傳完即用）。REVIEW.md 留在
        # 板夾供查閱；boot 判定表隨 status 回前端做完成畫面的唯讀資訊。
        # 要改判（emit/reserve）：編輯 data/<slug>/base/require.json 後跑
        # kb_lint 重新驗證。
        _set(stage="landing", lint=lint)
        review = _build_review(stage_dir)
        note = _land(slug)
        _set(stage="done", running=False, landed=slug, review=review,
             note=note, error=None, ended_at=time.time())
    except Exception as exc:
        _set(stage="failed", error=str(exc), running=False,
             ended_at=time.time())


def _land(slug: str) -> str:
    """staging → data/<slug> 原子落地（上傳材料移出；同名板依 overwrite 備份）。"""
    stage_dir = _staging(slug)
    dest = os.path.join(DATA, slug)
    if os.path.isdir(dest):
        if not _job["overwrite"]:
            raise RuntimeError(f"板子 {slug} 已存在——上傳時傳 overwrite=1 "
                               "才會備份舊版並替換")
        backup = os.path.join(
            STAGING_ROOT, f"{slug}.backup-{time.strftime('%Y%m%d-%H%M%S')}")
        shutil.move(dest, backup)
    materials = os.path.join(stage_dir, "input")
    if os.path.isdir(materials):
        dst = os.path.join(STAGING_ROOT, f"{slug}.materials")
        shutil.rmtree(dst, ignore_errors=True)
        shutil.move(materials, dst)
    os.replace(stage_dir, dest)
    return ("已落地。" + ("覆寫既有板：伺服器板子快取仍是舊資料，"
                          "請重啟 Flask 服務。" if _job["overwrite"] else ""))


def _run_lint(slug: str) -> dict:
    import sys
    tools = os.path.join(_ROOT, "tools")
    if tools not in sys.path:
        sys.path.insert(0, tools)
    from kb_lint import lint_board
    return lint_board(slug, data_root=STAGING_ROOT, echo=lambda *_: None)


def _apply_board_yaml(stage_dir: str, slug: str, name: str, validate: bool):
    """extractor 產的 board.yaml → 覆寫 display name 與驗證策略（勾選＝cubemx）。"""
    import yaml
    path = os.path.join(stage_dir, "board.yaml")
    try:
        doc = yaml.safe_load(open(path, encoding="utf-8")) or {}
    except FileNotFoundError:
        doc = {"board_id": slug}
    doc["name"] = name
    doc["validation"] = {
        "enabled": bool(validate),
        "type": "cubemx" if validate else "none",   # D9：預留 script 擴充
        "script": None,
    }
    with open(path, "w", encoding="utf-8") as f:
        yaml.safe_dump(doc, f, allow_unicode=True, sort_keys=False)


def _build_review(stage_dir: str) -> dict:
    """REVIEW 確認資料：boot 群組判定表（可改判）＋ REVIEW.md 原文。"""
    boot = {}
    try:
        req = json.load(open(os.path.join(stage_dir, "base", "require.json"),
                             encoding="utf-8"))
        for gname, spec in (req.get("boot_pin_locked", {})
                            .get("groups", {})).items():
            boot[gname] = {
                "action": spec.get("solver_action"),
                "pins": len(spec.get("pin_map") or []),
                "basis": (spec.get("_review") or {}).get("basis")
                         or spec.get("why") or "",
            }
    except (OSError, ValueError):
        pass
    review_md = ""
    try:
        review_md = open(os.path.join(stage_dir, "REVIEW.md"),
                         encoding="utf-8").read()
    except OSError:
        pass
    emit_n = sum(1 for g in boot.values()
                 if g["action"] == "emit_fixed_assignment")
    return {"boot_groups": boot, "review_md": review_md,
            "needs_emit_confirm": emit_n == 0}    # D7：無 emit＝必答確認題


# --------------------------------------------------------------------------- #
# 端點
# --------------------------------------------------------------------------- #
def _gate():
    if not FEATURE_ON:
        return jsonify(error="board create 功能未啟用"), 404
    return None


@bp.post("/api/boards/create")
def create():
    off = _gate()
    if off:
        return off
    name = (request.form.get("name") or "").strip()
    validate = (request.form.get("validate") or "0") in ("1", "true", "on")
    overwrite = (request.form.get("overwrite") or "0") in ("1", "true")
    baseline = (request.form.get("baseline") or "").strip() or None
    if not name:
        return jsonify(error="缺少板子名稱"), 400
    slug = _slugify(name)
    if not _SLUG_RE.match(slug or ""):
        return jsonify(error=f"板子名稱無法轉成合法 id（{slug!r}）——"
                             "請用含英數字的名稱"), 400
    if os.path.isdir(os.path.join(DATA, slug)) and not overwrite:
        return jsonify(error=f"板子 {slug} 已存在——換個名稱，或明確傳 "
                             "overwrite=1（落地時會備份舊版並需重啟服務）",
                       slug=slug), 409

    pdf = request.files.get("pdf")
    dts_files = request.files.getlist("dts")
    if pdf is None or not pdf.filename:
        return jsonify(error="缺少手冊 PDF"), 400
    if not dts_files:
        return jsonify(error="缺少 DTS 資料夾（或 zip）"), 400

    with _job["lock"]:
        if _job["running"]:
            return jsonify(error="已有一個新增板子的工作進行中。",
                           stage=_job["stage"]), 409
        _job.update(running=True, stage="unpack", slug=slug, name=name,
                    validate=validate, overwrite=overwrite, error=None,
                    lint=None, review=None, landed=None, note=None,
                    tokens=0, started_at=time.time(), ended_at=None)

    try:
        stage_dir = _staging(slug)
        shutil.rmtree(stage_dir, ignore_errors=True)    # 前次殘留清掉重來
        input_dir = os.path.join(stage_dir, "input")
        dts_root = os.path.join(input_dir, "dts")
        os.makedirs(dts_root, exist_ok=True)

        pdf_path = os.path.join(input_dir, f"{slug}.pdf")
        pdf.save(pdf_path)
        if os.path.getsize(pdf_path) > _MAX_PDF:
            raise ValueError("PDF 超過大小上限")

        total = 0
        if len(dts_files) == 1 and dts_files[0].filename.lower().endswith(".zip"):
            zpath = os.path.join(input_dir, "dts_upload.zip")
            dts_files[0].save(zpath)
            with zipfile.ZipFile(zpath) as zf:
                for m in zf.infolist():
                    rel = _safe_relpath(m.filename)
                    if rel is None or m.is_dir():
                        continue
                    total += m.file_size
                    if total > _MAX_DTS_TOTAL:
                        raise ValueError("DTS 內容超過大小上限")
                    dest = os.path.join(dts_root, rel)
                    os.makedirs(os.path.dirname(dest), exist_ok=True)
                    with zf.open(m) as src, open(dest, "wb") as out:
                        shutil.copyfileobj(src, out)
            os.unlink(zpath)
        else:
            for f in dts_files:
                rel = _safe_relpath(f.filename)
                if rel is None:
                    continue
                dest = os.path.join(dts_root, rel)
                os.makedirs(os.path.dirname(dest), exist_ok=True)
                f.save(dest)
                total += os.path.getsize(dest)
                if total > _MAX_DTS_TOTAL:
                    raise ValueError("DTS 內容超過大小上限")

        threading.Thread(target=_worker, args=(slug, name, validate, baseline),
                         daemon=True).start()
    except Exception as exc:
        _set(running=False, stage="failed", error=str(exc))
        return jsonify(error=f"上傳失敗：{exc}"), 400
    return jsonify(started=True, slug=slug), 202


@bp.get("/api/boards/create/status")
def status():
    off = _gate()
    if off:
        return off
    with _job["lock"]:
        return jsonify(**_pub())


@bp.delete("/api/boards/create")
def abort():
    off = _gate()
    if off:
        return off
    with _job["lock"]:
        slug = _job["slug"]
        if _job["running"]:
            # extractor 步驟不可中斷（LLM 呼叫進行中）；失敗態才可放棄清理
            return jsonify(error="生成進行中無法中止——完成或失敗後可放棄。"), 409
        _job.update(running=False, stage=None, slug=None, name=None,
                    error=None, lint=None, review=None, landed=None, note=None)
    if slug:
        shutil.rmtree(_staging(slug), ignore_errors=True)
    return jsonify(aborted=True)


@bp.get("/api/boards/create/artifacts")
def artifacts():
    off = _gate()
    if off:
        return off
    slug = _job["slug"]
    if not slug or not os.path.isdir(_staging(slug)):
        return jsonify(error="沒有可下載的 staging 產物"), 404
    import io
    buf = io.BytesIO()
    root = _staging(slug)
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for r, _d, files in os.walk(root):
            for f in files:
                p = os.path.join(r, f)
                zf.write(p, arcname=os.path.relpath(p, root))
    buf.seek(0)
    return send_file(buf, mimetype="application/zip", as_attachment=True,
                     download_name=f"{slug}-staging.zip")
