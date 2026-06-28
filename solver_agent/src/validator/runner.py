"""runner.py — 執行 STM32CubeMX console script（G5 validator 第 2 段）。

macOS 實測（spike）：`-q <script>` 啟動後停在 GUI 事件圈不執行；`-i` 讀 stdin
則可靠。故以 subprocess 啟動 `<binary> -i`、把 script 灌進 stdin、合併
stdout/stderr 收進 output/validator/cubemx.log（單一位置覆寫制）。

CubeMX 未安裝 → 回 {status:"error"}，不擋任何既有功能（可用性檢查獨立成
cubemx_binary()，tools 層先查再跑）。
"""
from __future__ import annotations

import os
import shutil
import subprocess
import time

# 預設安裝位置（macOS / Linux）；環境變數 STM32CUBEMX_PATH 優先。
# 注意：macOS 的 MacOs/STM32CubeMX 是指向 Resources/STM32CubeMX 的 symlink——
# CubeMX GUI 的自動更新器動到 bundle 時，symlink 有短暫懸空的視窗（實測
# 2026-07-03/04 各撞上一次），所以把 symlink 目標也列為獨立候選。
_DEFAULT_BINARIES = [
    "/Applications/STMicroelectronics/STM32CubeMX.app/Contents/MacOs/STM32CubeMX",
    "/Applications/STMicroelectronics/STM32CubeMX.app/Contents/Resources/STM32CubeMX",
    os.path.expanduser("~/STM32CubeMX/STM32CubeMX"),
    "/usr/local/STM32CubeMX/STM32CubeMX",
]

# JVM 啟動 + db 載入實測約 60–90s，再加逐條 set pin 與匯出的餘裕。
DEFAULT_TIMEOUT = 420


def _candidates() -> list[str]:
    env = os.environ.get("STM32CUBEMX_PATH")
    return ([env] if env else []) + _DEFAULT_BINARIES


def cubemx_binary() -> str | None:
    """找可執行的 CubeMX；沒有就回 None（呼叫端據此回報 error）。

    掃兩輪、間隔 2 秒：CubeMX 自動更新器替換 bundle 的瞬間，isfile() 會短暫
    失敗——一次重掃就能跨過這種瞬態視窗，代價相對 2 分鐘的驗證可忽略。"""
    for attempt in range(2):
        for cand in _candidates():
            if cand and os.path.isfile(cand) and os.access(cand, os.X_OK):
                return cand
        if attempt == 0:
            time.sleep(2)
    return None


def cubemx_resources_dir(binary: str | None = None) -> str | None:
    """CubeMX 執行檔 -> 其 Resources 目錄（內含 db/、官方板 .ioc）。

    macOS：MacOs/STM32CubeMX 是指向 Resources/STM32CubeMX 的 symlink，
    realpath 後 dirname 即 Resources；Linux 安裝則 db/ 與執行檔同層。
    DT 生成需要它（generate deviceTree 的 dbPath 與 config load 的板 ioc）；
    找不到 -> None，呼叫端據此退回「僅驗證、不生 DT」。"""
    binary = binary or cubemx_binary()
    if not binary:
        return None
    d = os.path.dirname(os.path.realpath(binary))
    for cand in (d, os.path.join(os.path.dirname(d), "Resources")):
        if os.path.isdir(os.path.join(cand, "db")):
            return cand
    return None


def binary_diagnostics() -> str:
    """逐一回報每個候選路徑失敗的原因（下次「找不到」時不再瞎猜）。"""
    lines = []
    for cand in _candidates():
        note = " (symlink)" if os.path.islink(cand) else ""
        lines.append(f"{cand}{note}: isfile={os.path.isfile(cand)} "
                     f"exec={os.access(cand, os.X_OK)}")
    return "；".join(lines) or "（無候選路徑）"


def run_script(script_text: str, artifacts_dir: str, *,
               binary: str | None = None,
               timeout: float = DEFAULT_TIMEOUT) -> dict:
    """跑一份 console script，產物與 log 全部落在 artifacts_dir（先清後寫）。

    回傳 {ok, log, log_path, script_path, timed_out}；ok 只代表行程正常結束，
    衝突判定交給 report.parse_result（它讀 log 與匯出的 pinout.csv）。
    """
    binary = binary or cubemx_binary()
    if binary is None:
        return {"ok": False, "log": "", "log_path": None, "script_path": None,
                "timed_out": False,
                "error": ("找不到 STM32CubeMX。請安裝官方工具，或以環境變數 "
                          "STM32CUBEMX_PATH 指向執行檔；若 CubeMX 剛在自動更新，"
                          "稍等片刻再試一次。已檢查：" + binary_diagnostics())}

    # 單一位置、每次覆寫：整個目錄重建，殘留的舊產物不會混進本輪判定。
    shutil.rmtree(artifacts_dir, ignore_errors=True)
    os.makedirs(artifacts_dir, exist_ok=True)
    script_path = os.path.join(artifacts_dir, "cubemx_script.txt")
    with open(script_path, "w", encoding="utf-8") as fh:
        fh.write(script_text)

    log_path = os.path.join(artifacts_dir, "cubemx.log")
    timed_out = False
    try:
        proc = subprocess.run(
            [binary, "-i"], input=script_text.encode("utf-8"),
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=timeout)
        log = proc.stdout.decode("utf-8", errors="replace")
        ok = True
    except subprocess.TimeoutExpired as exc:
        log = (exc.stdout or b"").decode("utf-8", errors="replace")
        log += f"\n[runner] TIMEOUT after {timeout}s — CubeMX killed."
        ok, timed_out = False, True
    except OSError as exc:
        return {"ok": False, "log": "", "log_path": None,
                "script_path": script_path, "timed_out": False,
                "error": f"無法啟動 CubeMX：{exc}"}

    with open(log_path, "w", encoding="utf-8") as fh:
        fh.write(log)
    return {"ok": ok, "log": log, "log_path": log_path,
            "script_path": script_path, "timed_out": timed_out}
