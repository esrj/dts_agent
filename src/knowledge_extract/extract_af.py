from __future__ import annotations

import re

from llm_provider import LLMError, LLMProvider

from .jsonutil import complete_json

_AF_SYSTEM = """\
You extract pin-multiplexing tables from SoC datasheet text (converted from PDF
with layout preserved, so table columns are aligned by whitespace).

Different vendors name the mux selector differently — treat them all the same:
- ST: "alternate function" AF0..AF15 columns
- TI: "MUXMODE" values 0..15 per ball
- Nuvoton: multi-function "MFP" values 0..15 per pin

Reply with ONLY a JSON object:
{
  "numbering": "explicit" | "positional" | "none",
  "pins": {
    "<PIN>": { "<mux number>": "<SIGNAL>", ... },
    ...
  }
}

numbering:
- "explicit"   — the table states the mux number of each signal (AF column
  headers, MUXMODE column, MFP value). Use those numbers verbatim.
- "positional" — the table only lists each pin's functions as an ordered list
  (e.g. "PG.3 / EPWM0_CH5 / UART9_TXD / ..."). Use the 0-based position in the
  printed order as the mux number (the GPIO/pin name itself is position 0).
- "none"       — the text has no pin-mux information at all; "pins" must be {}.
  Use this for pin tables that merely mention alternate/additional function
  NAMES without any numbering or ordered-list semantics — do NOT guess numbers
  for those.

Rules:
- <PIN> is the GPIO pad / pin identity used by software (e.g. "PA0", "PB8",
  "GPMC0_AD0"). Normalize by removing dots ("PB.8" -> "PB8"). NEVER use a
  package ball coordinate ("AA24") or a bare package pin number ("144") as
  <PIN>: when a row is keyed by ball/pin number, take the pad name — usually
  the GPIO name or the mux-0 / position-0 function name in that row.
- <SIGNAL> is the signal name exactly as printed. If one cell lists several
  shared signals, join them with "/" (e.g. "DCMI_D9/PSSI_D9/DCMIPP_D9").
- Skip power/ground/no-connect rows, reserved/empty cells, and analog-only
  pads that have NO mux number at all (crystal pins, dedicated ADC-only
  balls). Analog functions that DO carry a mux number stay.
- INCLUDE single-function digital pins (e.g. an I2C0_SCL pad whose only mux 0
  entry is its own function): downstream tools cross-check every signal the
  kernel DTS uses against this table, so dropping them breaks validation.
- Mux numbers are 4-bit: valid range 0..15. Never output a mux number
  above 15 — re-read the row instead.
- Extract EVERY pin row visible in the text, even partially — completeness
  matters more than skipping ambiguous rows. Watch for rows that wrap across
  multiple text lines or are interrupted by page-margin artifacts: join the
  continuation before numbering positions. If a row is truly unreadable,
  omit it.
"""


def _chunks(
    pages: list[str], runs: list[tuple[int, int]], pages_per_chunk: int = 3
) -> list[tuple[str, str]]:
    """把候選頁區段切成 (標籤, 文字) 塊;以整頁為界,避免切斷表格列。"""
    out: list[tuple[str, str]] = []
    for start, end in runs:
        for lo in range(start, end + 1, pages_per_chunk):
            hi = min(lo + pages_per_chunk - 1, end)
            text = "\n\n--- PAGE BREAK ---\n\n".join(pages[lo : hi + 1])
            out.append((f"p{lo + 1}-{hi + 1}", text))
    return out


_PIN_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_.\-]{0,23}$")
_BALL_COORD_RE = re.compile(r"^[A-Z]{1,2}\d{1,2}$")   # BGA 球號,如 AA24、D10
_GPIO_NAME_RE = re.compile(r"^P[A-Z]\d{1,2}$")        # Pxn 型 GPIO pad 名
_SIGNAL_RE = re.compile(r"^[A-Z][A-Z0-9_]{2,}$")      # 訊號/pad 名 token


def _normalize_pin(pin: str, muxes: dict[str, str]) -> str | None:
    """
    pin key 正規化:LLM 若仍用球號當 key,改用 mux0 的 pad 名 —— Pxn 型
    GPIO 名(Nuvoton 慣例:MFP0 = GPIO 本身)或訊號型 pad 名(TI 慣例:
    muxmode 0 = pad 名)。Pxn 型 key(如 PA0)本身就是正解,不動。

    單一功能的專用腳(mux0 = 自身名)一律保留:kernel DTS 會以 mux 0 使用
    這些 pad(如 AM65x 的 I2C0_SCL),丟棄會讓下游 Σ 交叉驗證誤判漏抓。
    """
    mux0 = muxes.get("0", "").split("/", 1)[0]
    if (
        _BALL_COORD_RE.match(pin)
        and not _GPIO_NAME_RE.match(pin)
        and (_GPIO_NAME_RE.match(mux0) or _SIGNAL_RE.match(mux0))
    ):
        pin = mux0
    return pin


def _shift_positional(pin: str, muxes: dict[str, str]) -> dict[str, str]:
    """
    序列式編號校正:MFP/位置 0 就是 GPIO 本身。若 mux0 是 pin 自己
    (GPIO 佔位)就拿掉該項;若 mux0 已是替代功能,表示 LLM 從 0 起編,
    全部 +1 —— 對齊「第一個替代功能 = 1」的範本語意。
    """
    mux0 = muxes.get("0", "").replace(".", "").upper()
    if not muxes.get("0"):
        return muxes
    if mux0 == pin:
        return {k: v for k, v in muxes.items() if k != "0"}
    return {str(int(k) + 1): v for k, v in muxes.items()}


def _merge_chunk(
    table: dict[str, dict[str, str]],
    pins: dict,
    *,
    label: str,
    positional: bool,
    warnings: list[str],
) -> None:
    for raw_pin, raw_muxes in pins.items():
        pin = str(raw_pin).replace(".", "").replace(" ", "").upper()
        if not _PIN_RE.match(pin) or not isinstance(raw_muxes, dict):
            continue
        # pdftotext 偶爾在訊號名中插空白(MCU_MDIO0_MDI O),一律去除
        muxes = {
            str(k).strip(): re.sub(r"\s+", "", str(v)) for k, v in raw_muxes.items()
        }
        normalized = _normalize_pin(pin, muxes)
        if normalized is None:
            continue
        pin = normalized
        if positional:
            if pin in table:
                continue  # 序列式編號僅補明確編號沒涵蓋的 pin,不與其混寫
            muxes = _shift_positional(pin, muxes)
        slot = table.setdefault(pin, {})
        for raw_mux, signal in muxes.items():
            try:
                mux_i = int(raw_mux)
            except ValueError:
                continue
            if not signal:
                continue
            if not 0 <= mux_i <= 15:     # mux 欄位 4 bit,超界=解析錯誤
                warnings.append(f"{label}: {pin} mux {mux_i} 超出 0-15,丟棄")
                continue
            mux = str(mux_i)
            if mux in slot and slot[mux] != signal:
                warnings.append(
                    f"{label}: conflict {pin}[{mux}] "
                    f"'{slot[mux]}' vs '{signal}' (kept first)"
                )
                continue
            slot[mux] = signal


def extract_af_table(
    pages: list[str],
    runs: list[tuple[int, int]],
    provider: LLMProvider,
    *,
    log=print,
) -> tuple[dict[str, dict[str, str]], list[str]]:
    """
    對每個候選塊各呼叫一次 LLM,合併成 af_table 結構
    {pin: {mux(str int): signal}}。回傳 (table, warnings)。

    合併分兩輪:明確編號(AF/MUXMODE/MFP 值)先進表;序列式編號(僅有排序、
    位置當編號,如 Nuvoton pin description)只補缺、絕不覆寫明確編號 ——
    序列式的編號屬推斷值,報告中會提醒需人工核對。
    """
    warnings: list[str] = []
    explicit: list[tuple[str, dict]] = []
    positional: list[tuple[str, dict]] = []

    chunks = _chunks(pages, runs)
    for idx, (label, text) in enumerate(chunks, 1):
        log(f"    af chunk {idx}/{len(chunks)} ({label}) ...")
        try:
            result = complete_json(provider, _AF_SYSTEM, text, max_tokens=32768)
        except (ValueError, LLMError) as exc:
            # 單塊失敗只損失該塊的 pin,記警告續跑,別讓整本手冊重來
            warnings.append(f"{label}: {exc}")
            continue
        pins = result.get("pins") or {}
        if not pins:
            continue
        if result.get("numbering") == "positional":
            positional.append((label, pins))
        else:
            explicit.append((label, pins))

    table: dict[str, dict[str, str]] = {}
    for label, pins in explicit:
        _merge_chunk(table, pins, label=label, positional=False, warnings=warnings)
    if positional:
        filled_before = len(table)
        for label, pins in positional:
            _merge_chunk(table, pins, label=label, positional=True, warnings=warnings)
        warnings.append(
            f"{len(table) - filled_before} pins 的 mux 編號為序列式推斷"
            "(來源表未標明編號,以列出順序當編號)——寫入 DTS 前需對照 TRM 核對"
        )

    # mux 編號數字排序、pin 名稱字典序,輸出才穩定可 diff
    ordered = {
        pin: {k: table[pin][k] for k in sorted(table[pin], key=int)}
        for pin in sorted(table)
    }
    return ordered, warnings
