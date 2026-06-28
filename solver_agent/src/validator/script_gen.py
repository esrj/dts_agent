"""script_gen.py — assignment -> STM32CubeMX console script（G5 validator 第 1 段）。

Spike 結論（摘要見 PROJECT_OVERVIEW.md「CubeMX 整合要點」）：不逆向產 .ioc，改走 CubeMX 官方 console
（`-i`）逐條 `set pin`——CubeMX 對每條指令即時套用官方規則（腳位存在、訊號可
承載、衝突），失敗會在 log 留下該行錯誤與 KO 計數；`csv pinout` 匯出機器可讀的
最終 pinout（驗證證據）。本模組是純函式：領域知識（MCU 名、保留腳、mode 對映）
全部由呼叫端從知識庫帶入，這裡只做格式翻譯。

DT 生成（2026-07-04 probe B–F）：CubeMX 有隱藏指令
`generate deviceTree <dbPath> <projectPath> <manifestVersion> <dtGenDir>`，
可離線生成 kernel / u-boot / tf-a / optee-os 四套 device tree。前置條件：
  1. 基底必須用 `config load <官方板 .ioc>`（tinyload 缺 RIF context 會 NPE）；
     載入後 `clearpinout` 得到乾淨 pinout（實測非 AllConfig 板 ioc 殘留最少）。
  2. 必須先 `project name` / `project path`（DTGen 讀 ProjectManager.ProjectName）。
  3. dbPath 結尾必須帶路徑分隔符（CubeMX 內部為天真字串串接）。
  4. DT 節點由 IP mode 驅動——`set pin` 只映射腳位；要讓周邊進 DT，需在
     csv 匯出「之後」補 `set context` + `set mode`（實測 mode 不會搬動已放好的
     腳，pre/post csv 零差異）。順序刻意如此：mode 失敗只損失該周邊的 DT 節點，
     驗證判定（csv diff）完全不受影響。
"""
from __future__ import annotations

import os


def expected_pin_map(rows: list) -> dict:
    """assignment rows -> {PIN: SIGNAL}（report 比對 CubeMX 匯出的期望值）。
    同一腳出現多次時後者覆蓋（dict 語意）——solver 的解必為單射不會發生；
    手工 rows 若違反單射，該腳只驗最後一筆。"""
    out = {}
    for r in rows or []:
        pin = (r.get("pin") or "").upper()
        sig = (r.get("signal") or "").upper()
        if pin and sig:
            out[pin] = sig
    return out


def plan_instances(rows: list) -> list:
    """assignment rows -> 排序去重的周邊 instance 清單（DT mode 規劃用）。
    優先取 row 的 peripheral 欄；缺時退回 signal 前綴（"I2C2_SCL" -> "I2C2"）。"""
    seen = set()
    for r in rows or []:
        inst = (r.get("peripheral") or "").upper()
        if not inst:
            sig = (r.get("signal") or "").upper()
            inst = sig.split("_", 1)[0] if sig else ""
        if inst:
            seen.add(inst)
    return sorted(seen)


def dt_mode_plan(instances: list, dt_modes: dict,
                 instance_map: dict | None = None) -> dict:
    """instance 清單 -> {CubeMX instance: CubeMX mode}（未對映到的略過）。

    dt_modes     : family 前綴 -> CubeMX mode 名（data/<board>/cubemx.json，
                   純資料、換板可改）。以「最長前綴」對映（USART1 配 USART
                   不會誤配 UART）。
    instance_map : 知識庫 instance 名 -> CubeMX InstanceName（如 USBH->USBH_HS）。
    """
    out = {}
    imap = instance_map or {}
    for inst in instances or []:
        cinst = imap.get(inst, inst)
        fam = max((k for k in (dt_modes or {}) if cinst.startswith(k)),
                  key=len, default=None)
        if fam:
            out[cinst] = dt_modes[fam]
    return out


def build_script(rows: list, *, mcu_name: str, artifacts_dir: str,
                 reserved_pins: set | None = None, dt: dict | None = None) -> str:
    """組出一份完整的 CubeMX console script。

    rows          : [{peripheral, signal, pin, …}] 已驗證的 SAT 分配。
    mcu_name      : CubeMX MCU 名（data/<board>/cubemx.json，不寫死）。
    artifacts_dir : csv / ioc 產物目錄（單一位置覆寫制）。
    reserved_pins : gpio_must_pins ∪ reserve_only 腳——以官方 `set pinreservation`
                    宣告，讓 CubeMX 也知道這些腳不可用。
    dt            : None -> 舊行為（tinyload、僅驗證）。dict -> 附加 DT 生成：
                    {board_ioc, db_path, project_name, project_dir, context,
                     modes: {instance: mode}, manifest_version, out_dir}。
                    路徑與 mode 全由呼叫端算好（知識庫 + CubeMX 安裝位置）。
    """
    if dt:
        # config load 官方板 .ioc（~15s）：帶完整 RIF/project context，
        # clearpinout 後得到乾淨 pinout 且 set pin 正常套用。
        lines = [f"config load {dt['board_ioc']}",
                 "clearpinout",
                 f"project name {dt['project_name']}",
                 f"project path {dt['project_dir']}"]
    else:
        # tinyload = pinout-only 載入：秒級、set pin/csv pinout 全可用，
        # 但無法生成 DT（RIF context 缺）。
        lines = [f"tinyload {mcu_name}"]

    for pin, sig in sorted(expected_pin_map(rows).items()):
        lines.append(f"set pin {pin} {sig}")
    for pin in sorted(reserved_pins or ()):
        lines.append(f"set pinreservation {pin}")

    if dt:
        # csv 先匯出（驗證真相），mode/DT 之後才動——mode 失敗不影響判定。
        lines.append(f"csv pinout {os.path.join(artifacts_dir, 'pinout.csv')}")
        for inst, mode in sorted((dt.get("modes") or {}).items()):
            lines.append(f"set context {dt['context']} {inst}")
            lines.append(f"set mode {inst} {mode}")
        db = dt["db_path"].rstrip(os.sep) + os.sep      # 尾分隔符必要（見模組 docstring）
        lines.append(f"generate deviceTree {db} {dt['project_dir']} "
                     f"{dt['manifest_version']} {dt['out_dir']}")
        lines.append(f"config saveas {os.path.join(artifacts_dir, 'validated.ioc')}")
    else:
        lines += [
            "pinout check",
            f"csv pinout {os.path.join(artifacts_dir, 'pinout.csv')}",
            f"config saveas {os.path.join(artifacts_dir, 'validated.ioc')}",
        ]
    lines.append("exit")
    return "\n".join(lines) + "\n"


if __name__ == "__main__":
    demo = [{"peripheral": "I2C2", "signal": "I2C2_SCL", "pin": "PB5"},
            {"peripheral": "I2C2", "signal": "I2C2_SDA", "pin": "PB4"}]
    print(build_script(demo, mcu_name="STM32MP257FAIx", artifacts_dir="/tmp/v",
                       reserved_pins={"PI13"},
                       dt={"board_ioc": "/x/Board.ioc", "db_path": "/x/db",
                           "project_name": "plan", "project_dir": "/tmp/v/project",
                           "context": "CortexA35NSOS", "modes": {"I2C2": "I2C"},
                           "manifest_version": "1.0", "out_dir": "/tmp/v/devicetree"}))
