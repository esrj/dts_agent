"""engines.py — 可替換的板級驗證引擎（多板支援階段 A）。

驗證方式由 data/<board>/board.yaml 的 validation 區塊決定（dataio.
load_board_manifest；缺檔預設＝現行為：DEFAULT_BOARD 走 CubeMX、其他板不驗證）：

  CubeMXEngine : STM32CubeMX 官方驗證（G5）——script_gen → runner → report
                 ＋ DT 生成。原 orchestrator/tools._run_validator_locked 的
                 主體平移至此，行為不變（含 CubeMX 未裝回 error＋安裝提示、
                 cubemx.json 缺檔回 error、全域鎖序列化）。
  ScriptEngine : 自訂驗證腳本（validation.type: script）——階段 B（Knowledge
                 Extractor 上傳腳本）啟用；本階段為可用骨架。
  NullEngine   : 不驗證（validation.enabled: false / type: none）——回
                 status="skipped" 並照樣落 result.json，讓背景驗證佇列、
                 前端輪詢與 fingerprint 對賬機制對所有引擎一視同仁。

三個引擎共用同一個回傳契約（result.json 同構 dict）：
  {status: "pass"|"fail"|"error"|"skipped", conflicts[], checked_pins,
   validated{instances,pins,fingerprint}, artifacts_dir, board, ran_at, …}
skipped 是「終態」——不是失敗，呼叫端（模型／前端）不應重試。

呼叫端唯一入口：engine_for(board).validate(rows, board, load_board)。
load_board 是 service.Pipeline._load_board 型的 callable（只有 CubeMX 需要
板子 bundle 的 must_gpio；Null/Script 忽略），engines 因此不 import service
（避免把 LLM provider 拖進驗證層）。
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import threading
import time

from util.dataio import DATA, DEFAULT_BOARD, OUTPUT, board_paths, load_board_manifest
from validator.script_gen import (build_script, dt_mode_plan, expected_pin_map,
                                  plan_instances)
from validator.runner import cubemx_resources_dir, run_script
from validator.report import parse_result, validated_summary

# CubeMX 一次只能跑一個（產物目錄覆寫制）；自動背景驗證（web 層）與模型顯式
# 驗證可能並發，兩個行程同時跑必互踩——鎖住整段驗證＋落檔。
_CUBEMX_LOCK = threading.Lock()

_SCRIPT_TIMEOUT = 300     # ScriptEngine 預設逾時（秒）；board.yaml 可覆寫


def _artifacts_dir() -> str:
    return os.path.join(OUTPUT, "validator")


def _persist(out: dict, artifacts_dir: str) -> dict:
    """共用尾巴：ran_at ＋ result.json 覆寫落地（/api/validator/status 讀它、
    zip 打包也帶上）。寫檔失敗不影響回傳。"""
    try:
        os.makedirs(artifacts_dir, exist_ok=True)
        out["ran_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
        with open(os.path.join(artifacts_dir, "result.json"), "w",
                  encoding="utf-8") as fh:
            json.dump(out, fh, ensure_ascii=False, indent=2)
    except OSError:
        pass
    return out


def _rebuild_dir(artifacts_dir: str) -> None:
    """覆寫制：清掉上一輪（可能是別的引擎）的殘留產物——skipped 結果配上
    舊 pinout.csv/devicetree 會造成「可下載官方驗證產物」的假象。"""
    shutil.rmtree(artifacts_dir, ignore_errors=True)
    os.makedirs(artifacts_dir, exist_ok=True)


class CubeMXEngine:
    """STM32CubeMX 官方驗證（G5）＋ DT 生成。行為＝改造前的 run_validator。"""

    def validate(self, rows: list, board: str, load_board) -> dict:
        with _CUBEMX_LOCK:
            return self._validate_locked(rows, board, load_board)

    def _validate_locked(self, rows: list, board: str, load_board) -> dict:
        artifacts_dir = _artifacts_dir()
        try:
            b = load_board(board)
        except Exception as exc:
            return {"status": "error", "artifacts_dir": artifacts_dir,
                    "message": f"無法載入板子 {board}：{exc}"}

        try:
            with open(board_paths(board)["cubemx"], encoding="utf-8") as fh:
                mcu = json.load(fh)
        except (OSError, ValueError) as exc:
            return {"status": "error", "artifacts_dir": artifacts_dir,
                    "message": f"此板未提供 cubemx.json（CubeMX MCU 常數）：{exc}"}

        # DT 生成（附加產物）：CubeMX 裝了、且板 ioc 找得到才附掛；缺任一項
        # 就退回純驗證（tinyload），行為與過去完全相同。mode 對映不到的周邊
        # 只是不進 DT——驗證判定（pinout.csv diff）永遠不受 DT 階段影響。
        dt_cfg, dt_dir = None, os.path.join(artifacts_dir, "devicetree")
        res_dir = cubemx_resources_dir()
        if res_dir and mcu.get("board_ioc"):
            board_ioc = os.path.join(res_dir, mcu["board_ioc"])
            if os.path.isfile(board_ioc):
                dt_cfg = {
                    "board_ioc": board_ioc,
                    "db_path": os.path.join(res_dir, "db"),
                    "project_name": "plan",
                    "project_dir": os.path.join(artifacts_dir, "project"),
                    "context": mcu.get("dt_context", "CortexA35NSOS"),
                    "modes": dt_mode_plan(plan_instances(rows),
                                          mcu.get("dt_modes") or {},
                                          mcu.get("dt_instance_map") or {}),
                    "manifest_version": mcu.get("dt_manifest_version", "1.0"),
                    "out_dir": dt_dir,
                }

        expected = expected_pin_map(rows)
        script = build_script(rows, mcu_name=mcu["mcu_name"],
                              artifacts_dir=artifacts_dir,
                              reserved_pins=b.must_gpio, dt=dt_cfg)
        run = run_script(script, artifacts_dir)
        out = parse_result(expected, artifacts_dir, run.get("log", ""),
                           ran_ok=run.get("ok", False),
                           run_error=run.get("error"),
                           dt_requested=dt_cfg is not None, dt_dir=dt_dir)
        out["board"] = board
        out["mcu"] = mcu.get("mcu_name")
        # runner 已在本輪重建目錄，這裡寫入不會殘留舊輪結果。
        return _persist(out, artifacts_dir)


class ScriptEngine:
    """自訂驗證腳本（board.yaml validation.type: script）——階段 B 啟用。

    契約：script 以 `<script> --assignment <rows.json 路徑>` 執行；
    stdout 印出 result.json 同構 JSON（至少 {status, conflicts[]}）。
    路徑相對於板子資料夾（data/<board>/）解析；非零 exit / 逾時 / 非 JSON
    輸出一律回 error（驗證器行為必須明確，不猜）。
    """

    def validate(self, rows: list, board: str, load_board=None) -> dict:
        artifacts_dir = _artifacts_dir()
        v = load_board_manifest(board).get("validation") or {}
        rel = v.get("script")
        if not rel:
            return _persist({
                "status": "error", "conflicts": [], "board": board,
                "artifacts_dir": artifacts_dir,
                "message": "validation.type=script 但 board.yaml 未提供 script 路徑。",
            }, artifacts_dir)
        script_path = os.path.normpath(os.path.join(DATA, board, rel))
        if not os.path.isfile(script_path):
            return _persist({
                "status": "error", "conflicts": [], "board": board,
                "artifacts_dir": artifacts_dir,
                "message": f"驗證腳本不存在：{script_path}",
            }, artifacts_dir)

        _rebuild_dir(artifacts_dir)
        rows_path = os.path.join(artifacts_dir, "assignment.json")
        with open(rows_path, "w", encoding="utf-8") as fh:
            json.dump(rows, fh, ensure_ascii=False, indent=2)

        expected = expected_pin_map(rows)
        timeout = v.get("timeout") or _SCRIPT_TIMEOUT
        try:
            run = subprocess.run([script_path, "--assignment", rows_path],
                                 capture_output=True, text=True, timeout=timeout)
        except subprocess.TimeoutExpired:
            return _persist({
                "status": "error", "conflicts": [], "board": board,
                "artifacts_dir": artifacts_dir,
                "validated": validated_summary(expected),
                "message": f"驗證腳本逾時（{timeout}s）：{script_path}",
            }, artifacts_dir)
        except OSError as exc:
            return _persist({
                "status": "error", "conflicts": [], "board": board,
                "artifacts_dir": artifacts_dir,
                "validated": validated_summary(expected),
                "message": f"驗證腳本無法執行：{exc}",
            }, artifacts_dir)

        try:
            out = json.loads(run.stdout)
            assert isinstance(out, dict) and out.get("status")
        except (ValueError, AssertionError):
            return _persist({
                "status": "error", "conflicts": [], "board": board,
                "artifacts_dir": artifacts_dir,
                "validated": validated_summary(expected),
                "message": ("驗證腳本輸出不是合法的 result JSON"
                            + (f"（exit={run.returncode}）" if run.returncode else "")),
                "log_errors": [ln for ln in (run.stderr or "").splitlines()[:12]],
            }, artifacts_dir)

        out.setdefault("conflicts", [])
        out.setdefault("validated", validated_summary(expected))
        out.setdefault("checked_pins", len(expected))
        out["board"] = board
        out["artifacts_dir"] = artifacts_dir
        return _persist(out, artifacts_dir)


class NullEngine:
    """不驗證（該板 manifest 未啟用官方驗證）。skipped 是終態、不是失敗。"""

    def validate(self, rows: list, board: str, load_board=None) -> dict:
        artifacts_dir = _artifacts_dir()
        _rebuild_dir(artifacts_dir)
        expected = expected_pin_map(rows)
        out = {
            "status": "skipped",
            "conflicts": [],
            "checked_pins": len(expected),
            "validated": validated_summary(expected),   # 前端 fingerprint 對賬用
            "artifacts_dir": artifacts_dir,
            "board": board,
            "message": ("此板未啟用官方驗證（board.yaml validation.enabled=false）"
                        "——腳位正確性由 solver 知識庫（AF table）保證。"),
        }
        return _persist(out, artifacts_dir)


def engine_for(board: str | None = None):
    """board.yaml validation -> 引擎實例。缺 manifest 的預設＝現行為
    （DEFAULT_BOARD -> CubeMX；其他板 -> Null）。"""
    v = load_board_manifest(board or DEFAULT_BOARD).get("validation") or {}
    if not v.get("enabled") or v.get("type") == "none":
        return NullEngine()
    if v.get("type") == "script":
        return ScriptEngine()
    return CubeMXEngine()
