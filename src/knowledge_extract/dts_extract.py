"""從 kernel 官方 DTS 生成 dts/ 與 baseline/ 知識庫(純程式,零 LLM)。

輸入:input/ 下的 DTS 檔(.dts/.dtsi,與手冊放同一資料夾)+ base/af_table.json
輸出:
  data/<board>/dts/signal_to_pin.json           官方預設腳位
  data/<board>/dts/official_dts_peripheral.json 官方 DTS 啟用的周邊 → signals
  data/<board>/baseline/baseline.csv            peripheral,signal,pin,af 攤平表
  data/<board>/baseline/dts/<baseline>.dts      官方 DTS 原檔複製(溯源)

核心邏輯:解析 baseline 板 .dts 的 include closure → 找啟用周邊的 pinctrl-0
群組 → 廠商解碼器(自動偵測 st / ti / nuvoton)拆出 (pin, af/signal) →
以 af_table 為命名權威交叉驗證。由 pipeline 的 dts 步驟呼叫 run_board()。
"""
from __future__ import annotations

import csv
import json
import os
import re
import shutil
from datetime import datetime

from . import identify
from .paths import DATA_DIR, INPUT_DIR, LIVE_DATA_DIR

# 非周邊 override:pinmux 控制器、GPIO bank、電源/安全/匯流排基礎設施
_INFRA_RE = re.compile(
    r"^(pinctrl|gpio[a-z0-9]*|.*_pmx\d*|scmi.*|optee|memory|cpu\d*|psci|"
    r"etzpc|rifsc|risaf.*|iac|bsec|syscfg|exti\d*|intc|rcc|pwr|ddr.*|"
    r"mailbox|mbox.*|k3_pds|k3_clks|k3_reset|dmsc|secure_proxy.*|"
    r"main_navss|mcu_navss|wkup_conf|sram.*|ospi_data|c7x.*|oc_sram|"
    r"ahbsr|mlahb|hpdma\d*)$"
)


# --------------------------------------------------------------------------- #
# DTS 解析:輕量遞迴 parser,屬性值保留原始文字(TI/ST 解碼要靠行內註解)
# --------------------------------------------------------------------------- #
class DNode:
    __slots__ = ("ref", "label", "name", "props", "children", "file", "deleted")

    def __init__(self, ref=None, label=None, name=None, file=""):
        self.ref = ref            # "&i2c2" 型 override 的目標標籤(去 &)
        self.label = label        # 定義時的標籤(label: name { })
        self.name = name
        self.props: dict[str, str] = {}
        self.children: list[DNode] = []
        self.file = file
        self.deleted: list[str] = []      # /delete-property/ 刪除的屬性名

    def walk(self):
        yield self
        for c in self.children:
            yield from c.walk()


_HEADER_RE = re.compile(
    r"(?:&(?P<ref>[\w-]+)|(?:(?P<label>[\w-]+)\s*:\s*)?(?P<name>[\w,.+@/-]+))\s*$"
)


def _strip_directives(text: str) -> str:
    """去掉 #include/#if 等前處理行與 /dts-v1/ 類指令(結構解析不需要)。"""
    text = re.sub(r"^\s*#(include|if\w*|else|endif|define|undef).*$", "", text,
                  flags=re.M)
    return re.sub(r"/(dts-v1|plugin)/\s*;", "", text)


def _skip_ws(text: str, i: int) -> int:
    n = len(text)
    while i < n:
        if text[i].isspace():
            i += 1
        elif text.startswith("//", i):
            j = text.find("\n", i)
            i = n if j < 0 else j + 1
        elif text.startswith("/*", i):
            j = text.find("*/", i + 2)
            i = n if j < 0 else j + 2
        else:
            break
    return i


def _scan_value_end(text: str, i: int) -> tuple[int, str]:
    """從 i 掃到本段落終點,回傳 (新位置, ';'|'{'|'}')——跳過字串與註解。"""
    n = len(text)
    while i < n:
        ch = text[i]
        if ch == '"':
            i += 1
            while i < n and text[i] != '"':
                i += 2 if text[i] == "\\" else 1
            i += 1
        elif text.startswith("//", i):
            j = text.find("\n", i)
            i = n if j < 0 else j + 1
        elif text.startswith("/*", i):
            j = text.find("*/", i + 2)
            i = n if j < 0 else j + 2
        elif ch in ";{}":
            return i, ch
        else:
            i += 1
    return n, ""


def parse_dts(text: str, file: str) -> DNode:
    """解析單一 DTS 檔為節點樹(root 為虛擬容器,children 是頂層節點)。"""
    text = _strip_directives(text)
    root = DNode(name="<top>", file=file)
    stack = [root]
    i = 0
    n = len(text)
    while i < n:
        i = _skip_ws(text, i)
        if i >= n:
            break
        end, tok = _scan_value_end(text, i)
        segment = text[i:end].strip()
        if tok == "{":
            m = _HEADER_RE.match(re.sub(r"/\*.*?\*/|//[^\n]*", "",
                                        segment, flags=re.S).strip())
            node = DNode(file=file)
            if m:
                node.ref = m.group("ref")
                node.label = m.group("label")
                node.name = m.group("name")
            else:
                node.name = segment or "?"
            stack[-1].children.append(node)
            stack.append(node)
            i = end + 1
        elif tok == ";":
            if segment and "=" in segment:
                key, _, value = segment.partition("=")
                key = key.strip()
                # ST 慣例把最後一條 pinmux 的註解放在分號後
                # (`<...>; /* ETH_RGMII_RX_CTL */`),補回值尾端供解碼用
                tail = re.match(r"[ \t]*(/\*[^*]*\*/)", text[end + 1:])
                if tail:
                    value += " " + tail.group(1)
                if not key.startswith("/"):
                    stack[-1].props[key] = value.strip()
            elif segment.startswith("/delete-property/"):
                stack[-1].deleted.append(
                    segment.split("/delete-property/", 1)[1].strip())
            elif segment and not segment.startswith("/"):
                stack[-1].props[segment] = ""    # 布林屬性(gpio-hog 等)
            i = end + 1
        elif tok == "}":
            if len(stack) > 1:
                stack.pop()
            i = end + 1
            i = _skip_ws(text, i)
            if i < n and text[i] == ";":
                i += 1
        else:
            break
    return root


def load_closure(dts_dir: str, baseline: str) -> list[tuple[str, str]]:
    """baseline dts + 遞迴引號 include 的 (檔名, 內容),include 先於引用檔。"""
    order: list[tuple[str, str]] = []
    seen: set[str] = set()

    def visit(name: str) -> None:
        if name in seen:
            return
        seen.add(name)
        path = os.path.join(dts_dir, name)
        if not os.path.isfile(path):
            return
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            text = f.read()
        for inc in re.findall(r'^\s*#include\s+"([^"]+)"', text, re.M):
            visit(inc)
        order.append((name, text))

    visit(baseline)
    return order


# --------------------------------------------------------------------------- #
# 廠商解碼器:pinctrl group 節點 → [(pin, af, 註解訊號)](缺項為 None)
# --------------------------------------------------------------------------- #
_ST_ENTRY = re.compile(
    r"STM32_PINMUX\(\s*'(\w)'\s*,\s*(\d+)\s*,\s*(\w+)\s*\)"
    r"[^S/]*(?:/\*\s*([^*]+?)\s*\*/)?"
)
_TI_ENTRY = re.compile(
    r"AM65X_(WKUP_)?IOPAD\(\s*0x([0-9a-fA-F]+)\s*,[^,)]*,\s*(\d+)\s*\)"
    r"\s*(?:/\*\s*([^*]+?)\s*\*/)?"
)
_NUVOTON_ENTRY = re.compile(r"SYS_GP\w_MFP[LH]_P([A-Z])(\d+)MFP_(\w+)")
_PINPROPS = ("pinmux", "pinctrl-single,pins", "nuvoton,pins", "pins")


def detect_vendor(closure: list[tuple[str, str]]) -> str:
    """由 DTS 內容判斷 pinmux 慣例(st / ti / nuvoton)。"""
    text = "\n".join(t for _, t in closure)
    if "STM32_PINMUX" in text:
        return "st"
    if "nuvoton,pins" in text:
        return "nuvoton"
    if "IOPAD" in text or "pinctrl-single,pins" in text:
        return "ti"
    raise RuntimeError("無法辨識 DTS 的 pinmux 慣例(st/ti/nuvoton 皆不符)")


def _group_entries(group: DNode) -> list[tuple[str, str]]:
    """group 節點(含子節點)中所有 pinmux 類屬性的 (屬性名, 原始值)。"""
    out = []
    for node in group.walk():
        for prop in _PINPROPS:
            value = node.props.get(prop)
            # ST 的 pins 子節點裡 "pins" 是字串屬性名單,只在無 pinmux 時才算
            if value and (prop != "pins" or "pinmux" not in node.props):
                out.append((prop, value))
    return out


def decode_group(vendor: str, group: DNode) -> list[dict]:
    """回傳 [{pin, af, hint}](hint = 註解/巨集內的訊號名,可能為 None)。"""
    rows: list[dict] = []
    for prop, value in _group_entries(group):
        if vendor == "st" and prop == "pinmux":
            for port, num, af, comment in _ST_ENTRY.findall(value):
                rows.append({
                    "pin": f"P{port.upper()}{int(num)}",
                    "af": (int(af[2:]) if af.upper().startswith("AF")
                           else af.upper()),          # 'GPIO' / 'ANALOG'
                    "hint": comment.strip() or None if comment else None,
                })
        elif vendor == "ti" and prop == "pinctrl-single,pins":
            for wkup, off, mux, comment in _TI_ENTRY.findall(value):
                comment = (comment or "").strip()
                # 註解格式 "(球號) PAD名.訊號" 或 "(球號) PAD名"(muxmode 0)
                m = re.match(r"\(([\w]+)\)\s*([\w.]+)", comment)
                pad = sig = None
                if m:
                    pad, _, sig = m.group(2).partition(".")
                    sig = sig or pad
                rows.append({
                    "pin": pad, "af": int(mux), "hint": sig,
                    "domain": "WKUP" if wkup else "MAIN",
                    "offset": int(off, 16),
                })
        elif vendor == "nuvoton" and prop == "nuvoton,pins":
            for bank, num, sig in _NUVOTON_ENTRY.findall(value):
                rows.append({"pin": f"P{bank}{int(num)}", "af": None,
                             "hint": sig})
    return rows


# --------------------------------------------------------------------------- #
# af_table 交叉驗證:pin+af → 正名 signal(af_table 為命名權威)
# --------------------------------------------------------------------------- #
def _pick_candidate(cands: list[str], hint: str | None, owner: str) -> str:
    """多值 cell(A/B/C)消歧:先比註解訊號,再比周邊 label,最後取第一個。"""
    if hint:
        for c in cands:
            if c.upper() == hint.upper():
                return c
        # 註解可能用泛名(ETH_RGMII_… vs ETH1_RGMII_…):去掉字首數字再比
        tail = hint.split("_", 1)[-1].upper()
        for c in cands:
            if c.split("_", 1)[-1].upper() == tail:
                return c
    owner_key = re.sub(r"[^a-z0-9]", "", owner.lower())
    for c in cands:
        prefix = re.sub(r"[^a-z0-9]", "", c.split("_", 1)[0].lower())
        if prefix and prefix in owner_key:
            return c
    return cands[0]


_ANALOG_RE = re.compile(r"(EADC|ADC\d|ACMP|COMP\d|DAC\d?_|VREF)", re.I)


def _reject(rejected: list, owner: str, pin, hint, reason: str) -> None:
    rejected.append({
        "owner": owner,
        "pin": pin,
        "signal": hint,
        "class": "analog" if hint and _ANALOG_RE.search(hint) else "missing",
        "reason": reason,
    })


def resolve_signal(
    af_table: dict, row: dict, owner: str, rejected: list
) -> tuple[str, str, int | None] | None:
    """把解碼列 {pin, af, hint} 對到 af_table,回傳 (signal, pin, af)。

    同源生成(R4):af_table 是唯一權威——查不到的條目**不寫進輸出**,
    記到 rejected(REVIEW.md 的剔除清單)待人工裁決;類比腳(R2)另歸類。
    """
    pin, af, hint = row["pin"], row["af"], row["hint"]
    if pin is None:
        _reject(rejected, owner, pin, hint, "無法解碼 pin")
        return None
    if isinstance(af, str):             # ST 的 GPIO / ANALOG 條目由呼叫端處理
        return None
    # pad key 大小寫不敏感查表(TI 手冊混用 CSn/CSN,af_table key 已上大寫)
    canon = {p.upper(): p for p in af_table}
    pin_key = canon.get(pin.upper())
    muxes = af_table.get(pin_key) if pin_key else None
    if af is None:                      # nuvoton:用訊號名反查 mux 號
        if muxes:
            for mux, cell in muxes.items():
                for s in cell.split("/"):
                    if s.strip().upper() == (hint or "").upper():
                        return s.strip(), pin_key, int(mux)
            # 手冊常帶實例後綴而 DTS 用基名(NAND_RDY0 vs NAND_RDY):
            # 唯一命中時採 af_table 的名字
            loose = [
                (s.strip(), mux)
                for mux, cell in muxes.items()
                for s in cell.split("/")
                if s.strip().upper().rstrip("0123456789") == (hint or "").upper()
            ]
            if len(loose) == 1:
                return loose[0][0], pin_key, int(loose[0][1])
        _reject(rejected, owner, pin, hint,
                f"af_table 的 {pin} 查無此訊號" if muxes
                else f"pin {pin} 不在 af_table")
        return None
    if not muxes or str(af) not in muxes:
        _reject(rejected, owner, pin, hint,
                f"{pin} 無 mux {af} 條目" if muxes
                else f"pin {pin} 不在 af_table")
        return None
    cands = [s.strip() for s in muxes[str(af)].split("/") if s.strip()]
    signal = _pick_candidate(cands, hint, owner)
    return signal, pin_key, af


# --------------------------------------------------------------------------- #
# 主抽取流程
# --------------------------------------------------------------------------- #
def extract(
    board: str,
    dts_dir: str,
    baseline: str,
    af_table: dict,
    *,
    log=print,
) -> dict:
    """解析 + 解碼 + 交叉驗證,回傳 {peripherals, signal_to_pin, …, warnings}。"""
    closure = load_closure(dts_dir, baseline)
    if not closure or closure[-1][0] != baseline:
        raise RuntimeError(f"{dts_dir} 下找不到 baseline {baseline}")
    vendor = detect_vendor(closure)
    log(f"[{board}] closure({vendor}): {', '.join(n for n, _ in closure)}")

    trees = {name: parse_dts(text, name) for name, text in closure}
    labels: dict[str, DNode] = {}
    label_container: dict[str, str] = {}   # label → 所屬頂層 &override 的 ref
    for name, _ in closure:
        for top in trees[name].children:
            for node in top.walk():
                if node.label and node.label not in labels:
                    labels[node.label] = node
                    if top.ref and node is not top:
                        label_container[node.label] = top.ref

    warnings: list[str] = []

    # 1. baseline 板 .dts 的頂層 &label override(板子明確碰過的節點)
    overrides: dict[str, DNode] = {}
    for node in trees[baseline].children:
        if node.ref:
            prev = overrides.get(node.ref)
            if prev:                     # 同檔多段 override:合併屬性
                prev.props.update(node.props)
                prev.children += node.children
            else:
                overrides[node.ref] = node

    def status_of(node: DNode) -> str | None:
        s = node.props.get("status")
        return s.strip().strip('"') if s else None

    # 2. 有效 status:override 沒寫就沿用 SoC dtsi 定義;kernel 語意上只要
    #    不是 okay/ok 都算停用(Nuvoton 廠商樹還有非標準拼寫 "disable")
    def is_enabled(ref: str, node: DNode) -> bool:
        s = status_of(node)
        if s is None:
            defn = labels.get(ref)
            s = status_of(defn) if defn is not None else None
        return s is None or s.lower() in ("okay", "ok")

    enabled: dict[str, DNode] = {}
    for ref, node in sorted(overrides.items()):
        if _INFRA_RE.match(ref):
            continue
        if not is_enabled(ref, node):
            continue
        target = labels.get(ref)
        if target is not None and _group_entries(target):
            continue                     # override 目標是 pinctrl 群組,非周邊
        enabled[ref] = node

    # 3. 逐周邊解 pinctrl-0(預設 state)群組 → 解碼 → af_table 正名
    peripherals: dict[str, dict] = {}
    signal_to_pin: dict[str, str] = {}
    signal_af: dict[str, int | None] = {}
    signal_owner: dict[str, str] = {}
    gpio_pins: set[str] = set()
    rejected: list[dict] = []      # R4:af_table 查不到的條目不進輸出,列 REVIEW
    ti_pads: dict[str, tuple[str, int]] = {}   # PAD大寫 → (domain, offset) 錨點

    for ref, node in enabled.items():
        group_labels = re.findall(r"&([\w-]+)", node.props.get("pinctrl-0", ""))
        signals: list[str] = []
        for glabel in group_labels:
            gnode = labels.get(glabel)
            if gnode is None:
                warnings.append(f"{ref}: pinctrl 群組 &{glabel} 找不到定義")
                continue
            for row in decode_group(vendor, gnode):
                if row.get("offset") is not None and row["pin"]:
                    # domain = 群組所屬的 pmx 節點 label(AM65 的 MAIN 有
                    # main_pmx0/main_pmx1 兩個節點,offset 各自起算)
                    ti_pads[row["pin"].upper()] = (
                        label_container.get(glabel, row["domain"]),
                        row["offset"])
                if row["af"] == "GPIO":          # ST 群組裡的 GPIO 腳
                    gpio_pins.add(row["pin"])
                    continue
                if row["af"] == "ANALOG":
                    continue
                resolved = resolve_signal(af_table, row, ref, rejected)
                if not resolved:
                    continue
                signal, pin, af = resolved
                if signal in signal_to_pin and signal_to_pin[signal] != pin:
                    warnings.append(
                        f"訊號 {signal} 重複指派:{signal_to_pin[signal]}"
                        f"(來自 {signal_owner[signal]})vs {pin}(來自 {ref})"
                        "——保留前者"
                    )
                    continue
                if signal not in signal_to_pin:
                    signal_to_pin[signal] = pin
                    signal_af[signal] = af
                    signal_owner[signal] = ref
                if signal not in signals:
                    signals.append(signal)
        peripherals[ref] = {"signals": signals, "pinctrl_groups": group_labels}

    # 3b. pad 錨點掃**全部** pinctrl 群組(P4,2026-07-25):未被 enabled 週邊
    #     引用的群組(如 wkup_uart0——SoC 預設啟用、板檔未 override)也有
    #     pad/offset 錨點,pad_params 覆蓋率才能達到「baseline DTS pinmux
    #     引用的每個 pad」。兩層防重:pad 名已登記者不動(enabled 迴圈優先);
    #     同 (domain, offset) 已有**別名** pad 者跳過——TI 註解的「pad 名」
    #     有時寫的是該 mux 的功能名(同一 pad 在 GPIO 群組叫 WKUP_GPIO0_24、
    #     在 OSPI 群組叫 MCU_OSPI0_CSN1),異名重登會撞鍵空間。
    taken_offsets = {v for v in ti_pads.values()}
    for glabel, gnode in labels.items():
        for row in decode_group(vendor, gnode):
            if row.get("offset") is None or not row["pin"]:
                continue
            pad = row["pin"].upper()
            key = (label_container.get(glabel, row["domain"]), row["offset"])
            if pad in ti_pads or key in taken_offsets:
                continue
            ti_pads[pad] = key
            taken_offsets.add(key)

    # 4. GPIO 腳位:baseline 板 dts 的 gpios 類屬性(LED/按鍵/reset 等)。
    #    走訪全部子樹並追蹤啟用狀態——active 消費者的腳進 protected,
    #    只有停用節點引用的腳另記(reserved_by_disabled_only)
    gpio_records: list[dict] = []

    def collect_gpios(node: DNode, active: bool) -> None:
        s = status_of(node)
        if s is not None and s.lower() not in ("okay", "ok"):
            active = False
        consumer = node.ref or node.label or node.name or "?"
        for key, value in node.props.items():
            if key == "gpios" or key.endswith("-gpios"):
                for glabel, num in re.findall(r"&([\w-]+)\s+(\d+)", value):
                    pin = _gpio_ref_to_pin(glabel, int(num), af_table)
                    if not pin:
                        continue
                    gpio_records.append({
                        "pin": pin,
                        "bank": glabel,
                        "line": int(num),
                        "property": key,
                        "consumer_node": consumer,
                        "consumer_active": active,
                        "consumer_compatible":
                            (node.props.get("compatible") or "").strip('"') or None,
                        "source": node.file,
                    })
                    if active:
                        gpio_pins.add(pin)
        for child in node.children:
            collect_gpios(child, active)

    for top in trees[baseline].children:
        top_active = True
        if top.ref and top.ref in overrides:
            top_active = is_enabled(top.ref, top)
        collect_gpios(top, top_active)

    # pin 衝突檢查:同一 pin 被多個訊號使用(baseline 應近乎一對一)
    pin_users: dict[str, list[str]] = {}
    for signal, pin in signal_to_pin.items():
        pin_users.setdefault(pin, []).append(signal)
    for pin, sigs in sorted(pin_users.items()):
        if len(sigs) > 1:
            warnings.append(
                f"pin 衝突:{pin} 被 {', '.join(sorted(sigs))} 同時使用"
                f"(來源 {', '.join(sorted({signal_owner[s] for s in sigs}))})"
            )

    gpio_pins -= set(signal_to_pin.values())     # AF 已佔用者不再列 GPIO

    # 周邊 instance 名(CSV/手冊命名):由該周邊 signals 的共同前綴推導
    instances = {
        ref: _instance_name(ref, spec["signals"])
        for ref, spec in peripherals.items()
    }
    return {
        "vendor": vendor,
        "baseline": baseline,
        "closure": closure,
        "trees": trees,
        "labels": labels,
        "overrides": overrides,
        "enabled": enabled,
        "peripherals": peripherals,
        "instances": instances,
        "signal_to_pin": signal_to_pin,
        "signal_af": signal_af,
        "signal_owner": signal_owner,
        "gpio_pins": sorted(gpio_pins),
        "gpio_records": gpio_records,
        "rejected": rejected,
        "ti_pads": ti_pads,
        "label_container": label_container,
        "warnings": warnings,
    }


def _instance_name(ref: str, signals: list[str]) -> str:
    """周邊 instance 名:signals 的最長共同底線前綴(I2C2_SCL/I2C2_SDA→I2C2);
    無 signals 時退回 label 大寫(去 main_ 前綴)。"""
    if signals:
        parts = signals[0].split("_")
        for i in range(len(parts) - 1, 0, -1):
            prefix = "_".join(parts[:i])
            if all(s == prefix or s.startswith(prefix + "_") for s in signals):
                return prefix
    return re.sub(r"^main_", "", ref).upper()


def family_of(ref: str) -> str:
    """周邊 family:label 去掉 domain 前綴與尾數字(main_i2c2→i2c、sdhci0→sdhci)。"""
    base = re.sub(r"^(main_|mcu_|wkup_)", "", ref)
    return re.sub(r"\d+$", "", base) or base


def _gpio_ref_to_pin(glabel: str, num: int, af_table: dict) -> str | None:
    """gpios = <&bank N …> 轉 pin 名。ST/Nuvoton:gpioX N → PXN;
    TI:bank 標籤(main_gpio0 → GPIO0)經 af_table 的 GPIO 訊號反查 pad。"""
    m = re.fullmatch(r"gpio([a-z])", glabel)
    if m:
        return f"P{m.group(1).upper()}{num}"
    prefix = re.sub(r"^main_", "", glabel).upper()
    if "GPIO" in prefix:
        target = f"{prefix}_{num}"
        for pin, muxes in af_table.items():
            for cell in muxes.values():
                if target in [s.strip() for s in cell.split("/")]:
                    return pin
    return None


# --------------------------------------------------------------------------- #
# 輸出檔生成
# --------------------------------------------------------------------------- #
def _write_json(path: str, obj, force: bool) -> str:
    if os.path.exists(path) and not force:
        return "skipped"
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=1)
        f.write("\n")
    return "written"


def write_outputs(
    board: str,
    result: dict,
    *,
    out_root: str,
    baseline: str,
    dts_dir: str,
    force: bool,
    log=print,
) -> dict:
    soc = identify.get_soc(board) or board
    board_dir = os.path.join(out_root, board)
    report: dict[str, str] = {}

    s2p = dict(sorted(result["signal_to_pin"].items()))
    obj = {
        "soc": soc,
        "description": (
            f"官方 {baseline} DTS 中 AF 信號對應的 pin。由 knowledge_extract "
            "dts_extract 自動生成;信號名以 base/af_table.json 命名為準。"
        ),
        "af_signal_count": len(s2p),
        "af_pin_count": len(set(s2p.values())),
        "gpio_pin_count": len(result["gpio_pins"]),
        "total_distinct_pins": len(set(s2p.values()) | set(result["gpio_pins"])),
        "signal_to_pin": s2p,
        "gpio_pins": result["gpio_pins"],
    }
    report["signal_to_pin.json"] = _write_json(
        os.path.join(board_dir, "dts", "signal_to_pin.json"), obj, force)

    obj = {
        "soc": soc,
        "source": f"{baseline} (kernel 官方 DTS)",
        "description": f"官方 {baseline} 啟用的 peripheral → signal",
        "peripherals": {
            ref: result["peripherals"][ref]
            for ref in sorted(result["peripherals"])
        },
    }
    report["official_dts_peripheral.json"] = _write_json(
        os.path.join(board_dir, "dts", "official_dts_peripheral.json"),
        obj, force)

    csv_path = os.path.join(board_dir, "baseline", "baseline.csv")
    if os.path.exists(csv_path) and not force:
        report["baseline.csv"] = "skipped"
    else:
        os.makedirs(os.path.dirname(csv_path), exist_ok=True)
        with open(csv_path, "w", encoding="utf-8", newline="") as f:
            # R3:統一 LF(csv 模組預設 \r\n)
            writer = csv.writer(f, lineterminator="\n")
            writer.writerow(["peripheral", "signal", "pin", "af"])
            for signal, pin in sorted(result["signal_to_pin"].items()):
                af = result["signal_af"].get(signal)
                writer.writerow([
                    signal.split("_", 1)[0], signal, pin,
                    "" if af is None else af,
                ])
        report["baseline.csv"] = "written"

    report.update(write_baseline(
        board, result, out_root=out_root, dts_dir=dts_dir, force=force))

    for name, state in report.items():
        log(f"[{board}] {name}: {state}")
    return report


def _find_include_root(dts_dir: str) -> str | None:
    """headers 根目錄:<dts_dir>/include 或其上層的 include(archive 佈局)。"""
    for cand in (os.path.join(dts_dir, "include"),
                 os.path.join(os.path.dirname(dts_dir), "include")):
        if os.path.isdir(os.path.join(cand, "dt-bindings")):
            return cand
    return None


def _needed_headers(closure: list[tuple[str, str]], include_root: str) -> list[str]:
    """closure 引用的 <...> headers,含 headers 之間的遞迴引用(相對路徑保留)。"""
    needed: list[str] = []
    queue = []
    for _, text in closure:
        queue += _INC_ANGLED_RE.findall(text)
    seen: set[str] = set()
    while queue:
        rel = queue.pop()
        if rel in seen:
            continue
        seen.add(rel)
        path = os.path.join(include_root, rel)
        if not os.path.isfile(path):
            continue
        needed.append(rel)
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            text = f.read()
        queue += _INC_ANGLED_RE.findall(text)
        # header 內的引號 include 相對於該 header 所在目錄
        for q in re.findall(r'^\s*#include\s+"([^"]+)"', text, re.M):
            queue.append(os.path.normpath(
                os.path.join(os.path.dirname(rel), q)))
    return sorted(needed)


_INC_ANGLED_RE = re.compile(r"^\s*#include\s+<([^>]+)>", re.M)


def write_baseline(
    board: str,
    result: dict,
    *,
    out_root: str,
    dts_dir: str,
    force: bool,
) -> dict:
    """baseline/dts/ = 完整可編譯檔組:include 鏈全複製 + headers + MANIFEST。

    DTS_agent 的 m5 定位與 m7 編譯用 `cpp -I include/` 展開,缺任一檔就失敗;
    板級 .dts 維持 kernel 原名,且一夾只放一個 .dts(定位規則)。
    """
    baseline = result["baseline"]
    dst_root = os.path.join(out_root, board, "baseline", "dts")
    marker = os.path.join(dst_root, "MANIFEST.md")
    if os.path.exists(marker) and not force:
        return {"baseline/dts": "skipped"}
    os.makedirs(dst_root, exist_ok=True)

    copied: list[str] = []
    for name, _ in result["closure"]:
        shutil.copy2(os.path.join(dts_dir, name), os.path.join(dst_root, name))
        copied.append(name)

    # R5:label 閉包自查 + 已知模式的上游缺定義修復(記錄於 MANIFEST/REVIEW)
    repairs = _repair_missing_pcfgs(dst_root, copied)

    include_root = _find_include_root(dts_dir)
    headers: list[str] = []
    if include_root:
        headers = _needed_headers(result["closure"], include_root)
        for rel in headers:
            dst = os.path.join(dst_root, "include", rel)
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            shutil.copy2(os.path.join(include_root, rel), dst)

    src_note = _source_note(dts_dir)
    lines = [
        f"# {board} — baseline kernel DTS 檔組(knowledge_extract 自動產出)",
        "",
        "完整可編譯的 kernel device-tree 源碼組:板級 .dts + include 鏈"
        "(.dtsi/.h)+ `include/` 下的 dt-bindings headers。",
        f"展開方式:`cpp -nostdinc -undef -D__DTS__ -x assembler-with-cpp "
        f"-I include -I . {baseline}`",
        "",
        "## 來源",
        "",
        *src_note,
        f"- 產出時間:{datetime.now():%Y-%m-%d %H:%M}",
        "",
        "## 匯入時的修復(Processing applied on import)",
        "",
        *([f"- {r}" for r in repairs] or ["- 無"]),
        "",
        "## 檔案清單",
        "",
        *[f"- {n}" for n in copied],
        *[f"- include/{h}" for h in headers],
        "",
    ]
    with open(marker, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))
    state = "written" if include_root else "written(找不到 include/,headers 未複製)"
    return {"baseline/dts": f"{state}:{len(copied)} 檔 + {len(headers)} headers"}


_PCFG_PATTERN = re.compile(r"^pcfg_(\w+?)_drive(\d+)_(1_8|3_3)V$")


def baseline_label_audit(files: dict[str, str]) -> tuple[set[str], set[str]]:
    """檔組的 (已定義 labels, 被引用 labels)。輸入 {檔名: 內容}。"""
    defs: set[str] = set()
    refs: set[str] = set()
    for text in files.values():
        clean = _COMMENT_STRIP_RE.sub("", text)
        defs |= {m.group(1) for m in re.finditer(r"(?m)^\s*([\w-]+)\s*:", clean)}
        refs |= {m.group(1) for m in re.finditer(r"&([A-Za-z_][\w-]*)", clean)}
    return defs, refs


_COMMENT_STRIP_RE = re.compile(r"/\*.*?\*/|//[^\n]*", re.S)


def _repair_missing_pcfgs(dst_root: str, copied: list[str]) -> list[str]:
    """上游漏定義的 pcfg_<fam>_drive<N>_<V> 節點:依兄弟樣式補進 baseline 副本。

    案例:OpenNuvoton 樹的 sdhci1 default state 引用 pcfg_sdhci_drive1_3_3V,
    但 dtsi 只定義了 drive0/2/4/7 等兄弟——意圖明確(drive-strength=1、3.3V),
    機械式補上並記錄。無法配對兄弟樣式的缺失不動,留給 lint FAIL。
    """
    files = {n: open(os.path.join(dst_root, n), encoding="utf-8",
                     errors="replace").read() for n in copied}
    defs, refs = baseline_label_audit(files)
    repairs: list[str] = []
    for label in sorted(refs - defs):
        m = _PCFG_PATTERN.match(label)
        if not m:
            continue
        fam, drive, volt = m.groups()
        sib_re = re.compile(
            rf"\n([ \t]*)(pcfg_{fam}_drive\d+_(?:1_8|3_3)V)\s*:"
            rf"[^\n{{]*\{{[^{{}}]*\}};", re.S)
        for name, text in files.items():
            sib = sib_re.search(text)
            if not sib:
                continue
            block = sib.group(0)
            new = block.replace(sib.group(2), label)
            new = re.sub(r"drive-strength\s*=\s*<\d+>",
                         f"drive-strength = <{int(drive)}>", new)
            new = re.sub(r"power-source\s*=\s*<\d+>",
                         f"power-source = <{3300 if volt == '3_3' else 1800}>",
                         new)
            new = (f"\n{sib.group(1)}/* knowledge_extract 匯入修復:上游缺 "
                   f"{label} 定義,依兄弟節點樣式補上 */" + new)
            text = text[:sib.end()] + new + text[sib.end():]
            files[name] = text
            with open(os.path.join(dst_root, name), "w",
                      encoding="utf-8") as f:
                f.write(text)
            repairs.append(
                f"補上 {label}(依 {sib.group(2)} 樣式,寫入 {name})")
            break
    return repairs


def _source_note(dts_dir: str) -> list[str]:
    """來源溯源:archive 佈局下有 dts_validation_report.json 就引用它。"""
    report = os.path.join(os.path.dirname(dts_dir), "dts_validation_report.json")
    if os.path.isfile(report):
        try:
            src = json.load(open(report, encoding="utf-8"))["source"]
            return [
                f"- repo:{src.get('repo')}",
                f"- branch/tag:{src.get('branch')}",
                f"- commit:{src.get('commit')}",
            ]
        except (json.JSONDecodeError, KeyError, OSError):
            pass
    return [f"- 手動提供的 DTS 檔組(目錄:{dts_dir})"]


# --------------------------------------------------------------------------- #
# pipeline 入口
# --------------------------------------------------------------------------- #
def _has_dts(d: str) -> bool:
    return os.path.isdir(d) and any(n.endswith(".dts") for n in os.listdir(d))


def find_dts_dir(input_dir: str) -> str | None:
    """input/ 下找 DTS 目錄:支援三種擺法,找不到回 None。

    1. input/ 直接放 .dts 檔(舊佈局)
    2. input/<資料夾>/ 放 .dts(+ include/)
    3. input/<資料夾>/dts/ 放 .dts(archive/dts/<board> 整夾拖入的佈局)
    多個候選資料夾時取字典序第一個並警告——input/ 一次只放一塊板。
    """
    if _has_dts(input_dir):
        return input_dir
    candidates = []
    for name in sorted(os.listdir(input_dir) if os.path.isdir(input_dir) else []):
        sub = os.path.join(input_dir, name)
        if not os.path.isdir(sub) or name == "include":
            continue
        if _has_dts(sub):
            candidates.append(sub)
        elif _has_dts(os.path.join(sub, "dts")):
            candidates.append(os.path.join(sub, "dts"))
    if len(candidates) > 1:
        print(f"⚠ input/ 下有多個 DTS 資料夾({candidates}),"
              f"取 {candidates[0]}——一次只放一塊板")
    return candidates[0] if candidates else None


def output_files(board: str, out_dir: str | None = None) -> list[str]:
    root = os.path.join(out_dir or DATA_DIR, board)
    return [
        os.path.join(root, "dts", "signal_to_pin.json"),
        os.path.join(root, "dts", "official_dts_peripheral.json"),
        os.path.join(root, "baseline", "baseline.csv"),
    ]


def resolve_baseline(board: str, dts_dir: str) -> str:
    """決定 baseline 板 .dts:boards.ini [baseline] 優先;目錄下恰一個 .dts
    就用它(並回寫 ini);多個又未登錄則要求人工指定。"""
    registered = identify.get_baseline(board)
    if registered and os.path.isfile(os.path.join(dts_dir, registered)):
        return registered
    candidates = sorted(
        n for n in os.listdir(dts_dir) if n.endswith(".dts")
    ) if os.path.isdir(dts_dir) else []
    if len(candidates) == 1:
        identify.save_baseline(board, candidates[0])
        return candidates[0]
    raise RuntimeError(
        f"無法決定 baseline dts(候選:{candidates})——"
        f"請在 boards.ini 的 [baseline] 登錄 {board} = <檔名>"
    )


def run_board(
    board: str,
    *,
    dts_dir: str | None = None,
    out_dir: str | None = None,
    force: bool = False,
    log=print,
) -> dict:
    """input/ 的 DTS → data/<board>/{dts,baseline}。回傳 {files, warnings}。"""
    dts_dir = dts_dir or find_dts_dir(INPUT_DIR)
    if dts_dir is None:
        raise RuntimeError(f"{INPUT_DIR} 下找不到 .dts(可放檔案或整份資料夾)")
    out_root = out_dir or DATA_DIR
    af_path = os.path.join(out_root, board, "base", "af_table.json")
    if not os.path.isfile(af_path):
        # copy-on-write(D3):staging 沒有 base 時**整套複製**正式知識庫的
        # base/ 進 staging 再處理——只複製 af_table 會讓 dts-only 重產的
        # staging 缺 profiles 等檔,落地後 solve 直接 FileNotFoundError
        # (2026-07-25 am6548 修復事故)。extractor 永不寫 data/<board>/。
        live_base = os.path.join(LIVE_DATA_DIR, board, "base")
        if os.path.isdir(live_base):
            os.makedirs(os.path.dirname(af_path), exist_ok=True)
            for f in os.listdir(live_base):
                if f.endswith(".json"):
                    shutil.copy2(os.path.join(live_base, f),
                                 os.path.join(os.path.dirname(af_path), f))
    if not os.path.isfile(af_path):
        raise RuntimeError(f"缺 {af_path}——請先跑手冊步驟產出 af_table")

    # R1:有廠商 pinfunc header(Nuvoton)就整表權威重建 af_table——
    # 手冊「序列式 pin 表」的 LLM 解析掉 token 會讓整列 mux 位移,header 是真值
    from . import af_repair, derive, identify as _identify

    af_diff = None
    pinfunc = af_repair.find_pinfunc_header(_find_include_root(dts_dir), board)
    if pinfunc:
        af_diff = af_repair.rebuild_af_table(af_path, pinfunc, board, log=log)
        if af_diff:
            with open(af_path, "r", encoding="utf-8") as f:
                rebuilt = json.load(f)
            allp_path = os.path.join(os.path.dirname(af_path),
                                     "all_peripheral.json")
            soc = _identify.get_soc(board) or board
            with open(allp_path, "w", encoding="utf-8") as f:
                json.dump(derive.derive_all_peripheral(rebuilt, soc), f,
                          ensure_ascii=False, indent=1)
                f.write("\n")
            log(f"[{board}] all_peripheral.json 隨 af_table 重生")

    with open(af_path, "r", encoding="utf-8") as f:
        af_table = json.load(f)

    baseline = resolve_baseline(board, dts_dir)
    result = extract(board, dts_dir, baseline, af_table, log=log)
    result["pinfunc_path"] = pinfunc      # 渲染供料(pad_params)也用它
    log(f"[{board}] 周邊 {len(result['peripherals'])} 個、"
        f"AF 訊號 {len(result['signal_to_pin'])} 條、"
        f"GPIO 腳 {len(result['gpio_pins'])} 支、"
        f"警告 {len(result['warnings'])} 則")
    for w in result["warnings"]:
        log(f"  ⚠ {w}")

    files = write_outputs(
        board, result,
        out_root=out_root, baseline=baseline, dts_dir=dts_dir,
        force=force, log=log,
    )

    # 增項 A/C/D/E:boot 分類回填 require、dts_generation 六檔、board.yaml、lint
    from . import dts_generation, kb_lint, require_enrich

    boot_groups = require_enrich.enrich(
        board, result, af_table, out_root=out_root, log=log)
    files.update(dts_generation.write_all(
        board, result, af_table, boot_groups,
        out_root=out_root, force=force, log=log))
    lint = kb_lint.run(
        board, result, af_table, boot_groups, out_root=out_root, log=log,
        af_diff=af_diff)

    return {"files": files, "warnings": result["warnings"], "lint": lint}
