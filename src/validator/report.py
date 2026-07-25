"""report.py — CubeMX 執行結果 -> 結構化衝突報告（G5 validator 第 3 段）。

判定策略（spike 結論）：**匯出的 pinout.csv 是唯一真相**——script 對每支腳做
`set pin`，CubeMX 只會套用通過官方規則的指派；最後 `csv pinout` 匯出「實際成立」
的 pinout。把它 diff 期望的 {pin: signal}：

  - 期望有、csv 沒有（或訊號不同）→ conflict（該 set pin 被官方規則拒絕/改派）
  - log 的 [ERROR] 行（unknown pin、reservation fail…）附為 message 佐證

status: pass（csv 齊、零衝突）/ fail（有衝突）/ error（CubeMX 沒跑完、csv 缺）。
"""
from __future__ import annotations

import csv
import os
import re

# CubeMX csv 的 Name 欄可能帶封裝註記（如 "PA13 (BOOT0)"）——取前導 pad token。
_PIN_TOKEN = re.compile(r"^(P[A-Z]{1,2}\d+)")
# log 中值得回報的錯誤行（訊息本身給人看，pin 能抓就抓）。
_LOG_ERROR = re.compile(r"\[ERROR\]\s+(.*)$")
# 每輪固定出現的環境噪音（更新器、廣告面板）——與驗證無關，不進報告。
_LOG_NOISE = re.compile(r"Updater is busy|advertisement|HomeAdPanel")


def _norm_pin(name: str) -> str | None:
    m = _PIN_TOKEN.match((name or "").strip().upper())
    return m.group(1) if m else None


def read_pinout_csv(path: str) -> dict:
    """CubeMX `csv pinout` 匯出 -> {PIN: SIGNAL}（空 Signal 的腳略過）。"""
    out: dict[str, str] = {}
    with open(path, encoding="utf-8-sig", newline="") as fh:
        for row in csv.DictReader(fh):
            pin = _norm_pin(row.get("Name") or "")
            sig = (row.get("Signal") or "").strip()
            if pin and sig:
                out[pin] = sig.upper()
    return out


def log_errors(log_text: str) -> list[str]:
    """CubeMX log 的 [ERROR] 行（去重、保序、濾噪音），供 conflict message 佐證。"""
    seen, out = set(), []
    for line in (log_text or "").splitlines():
        m = _LOG_ERROR.search(line)
        if m and m.group(1) not in seen and not _LOG_NOISE.search(m.group(1)):
            seen.add(m.group(1))
            out.append(m.group(1))
    return out


def plan_fingerprint(pins: dict) -> str:
    """{PIN: SIGNAL} -> sha1 前 12 碼。兩份 plan 只要有任何一支腳不同，
    指紋必不同；同一份 plan 重驗指紋相同。web 層用它把「前端這份 plan」
    與「result.json 驗的那份」對上（自動驗證的完成判定）。"""
    import hashlib
    items = sorted((pins or {}).items())
    return hashlib.sha1("|".join(f"{p}:{s}" for p, s in items)
                        .encode()).hexdigest()[:12]


def validated_summary(expected: dict) -> dict:
    """result 的「這次到底驗了什麼」區塊——沒有它，不同 plan 的 pass 報告
    看起來一模一樣（status/數字/檔名全同），無從對外證明驗證對象。
    公開函式：engines.NullEngine 也用它產 skipped 結果的 validated 區塊，
    讓前端 fingerprint 對賬機制對所有引擎一視同仁。"""
    pins = dict(sorted(expected.items()))
    insts = sorted({s.split("_", 1)[0] for s in pins.values()})
    return {"instances": insts, "pins": pins,
            "fingerprint": plan_fingerprint(pins)}


def dt_artifacts(dt_dir: str) -> list:
    """DT 產物目錄 -> 排序的相對路徑清單（如 kernel/stm32mp257f-plan-mx.dts）。"""
    out = []
    if dt_dir and os.path.isdir(dt_dir):
        for root, _dirs, files in os.walk(dt_dir):
            for f in files:
                out.append(os.path.relpath(os.path.join(root, f), dt_dir))
    return sorted(out)


def _dt_summary(dt_requested: bool, dt_dir: str | None) -> dict | None:
    """result 的 devicetree 區塊。附加產物性質：只描述有沒有、有哪些，
    絕不影響 status（驗證真相永遠是 pinout.csv diff）。"""
    if not dt_requested:
        return None
    files = dt_artifacts(dt_dir or "")
    return {
        "status": "ok" if files else "missing",
        "dir": dt_dir,
        "files": files,
        "note": ("CubeMX 生成的 device tree（kernel / u-boot / tf-a / optee-os）。"
                 "以 plan 與 pinout.csv 為準；mode 對映不到的周邊不會出現在 DT。"
                 if files else
                 "本輪未產出 DT（板 ioc/mode 對映缺、或 generate deviceTree 失敗）"
                 "——不影響驗證判定。"),
    }


def parse_result(expected: dict, artifacts_dir: str, log_text: str,
                 *, ran_ok: bool = True, run_error: str | None = None,
                 dt_requested: bool = False, dt_dir: str | None = None) -> dict:
    """組出 run_validator 的回傳契約。

    expected : {PIN: SIGNAL} 我們要求 CubeMX 套用的分配（script_gen.expected_pin_map）。
    dt_requested / dt_dir : 本輪是否附帶 DT 生成、其產物目錄——只增列
    devicetree 區塊，pass/fail 判定與 DT 完全解耦。
    """
    if run_error:
        return {"status": "error", "conflicts": [],
                "artifacts_dir": artifacts_dir, "message": run_error}

    dt = _dt_summary(dt_requested, dt_dir)
    validated = validated_summary(expected)

    csv_path = os.path.join(artifacts_dir, "pinout.csv")
    errors = log_errors(log_text)
    if not ran_ok or not os.path.isfile(csv_path):
        out = {"status": "error", "conflicts": [],
               "artifacts_dir": artifacts_dir,
               "message": "CubeMX 未完成驗證（逾時或未產出 pinout.csv）。",
               "validated": validated,
               "log_errors": errors[:12]}
        if dt:
            out["devicetree"] = dt
        return out

    actual = read_pinout_csv(csv_path)
    conflicts = []
    for pin, sig in sorted(expected.items()):
        got = actual.get(pin)
        if got == sig:
            continue
        related = [e for e in errors if pin in e or sig in e]
        conflicts.append({
            "pin": pin,
            "signal": sig,
            "message": (f"CubeMX 未接受 {sig}@{pin}"
                        + (f"（實際為 {got}）" if got else "（該腳未被指派）")
                        + (f"；log：{related[0]}" if related else "")),
        })

    out = {
        "status": "pass" if not conflicts else "fail",
        "conflicts": conflicts,
        "artifacts_dir": artifacts_dir,
        "checked_pins": len(expected),
        "validated": validated,
        "log_errors": errors[:12],
    }
    if dt:
        out["devicetree"] = dt
    return out
