"""
dataio.py — file locations + all data I/O for the pin-assignment demo.

Merges what used to be three tiny modules (paths + loaders + report):
  paths   : AF_TABLE / SIGNALS / PROFILES / DTS / OUTPUT
  input   : load_af / sigma_of / load_signal_to_pin
  output  : write_plan (csv + xlsx) / cross_check

Deliberately imports nothing from solver/, so it stays a leaf utility module
(no import cycles).
"""
import csv
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))                 # src/util
DATA = os.path.normpath(os.path.join(HERE, "..", "..", "data"))
OUTPUT = os.path.normpath(os.path.join(HERE, "..", "..", "output"))

# --------------------------------------------------------------------------- #
# Multi-board data layout                                                      #
# --------------------------------------------------------------------------- #
# Each board is a SELF-CONTAINED folder DIRECTLY under data/<board>/. Adding a
# board = dropping a new folder there; the web UI auto-detects them via
# list_boards(). Within a board, files are grouped BY RESPONSIBILITY（詳見
# data/README.md）——手工核心、官方 DTS 衍生、IC/binding 知識、自動快取分開放，
# 核心資料永遠不會跟可拋棄的 cache 混在一起：
#   data/<board>/base/af_table.json              手工核心（solver 判定依據）
#   data/<board>/base/peripheral_profiles.json
#   data/<board>/base/require.json
#   data/<board>/base/all_peripheral.json        （af_table 再生索引，參考用）
#   data/<board>/base/cubemx.json                板級工具常數（CubeMX validator）
#   data/<board>/dts/signal_to_pin.json          官方 DTS 解析（board-proven）
#   data/<board>/dts/official_dts_peripheral.json
#   data/<board>/bindings/board_components.json  板上外部 IC / binding 知識（G4）
#   data/<board>/cache/binding_cache.json        自動產生，可整夾刪除重建
DEFAULT_BOARD = "stm32mp257f-ev1"
_BOARD_FILES = {
    "af": os.path.join("base", "af_table.json"),
    "profiles": os.path.join("base", "peripheral_profiles.json"),
    "require": os.path.join("base", "require.json"),
    "dts": os.path.join("dts", "official_dts_peripheral.json"),
    "signals": os.path.join("dts", "signal_to_pin.json"),
}

# Optional per-board files: absent files are fine (loaders fall back to {}),
# so they must NOT gate board detection in list_boards().
_BOARD_FILES_OPTIONAL = {
    "components": os.path.join("bindings", "board_components.json"),  # 板上外部 IC 對照（G4）
    "binding_cache": os.path.join("cache", "binding_cache.json"),     # lookup_binding 查過即存
    "cubemx": os.path.join("base", "cubemx.json"),                    # CubeMX validator 板級常數（G5）
}

# G4（外部 IC binding / ic 欄）功能開關：實作完整但暫時停用（詳見
# PROJECT_OVERVIEW.md「功能旗標」）。off 時：assignment/plan 不帶 ic 欄、lookup_binding
# 不註冊給編排 LLM；資料檔與程式碼全數保留。重新啟用：環境變數
# FEATURE_IC_BINDING=1（或把預設改回 "1"）。
IC_BINDING_ENABLED = os.environ.get("FEATURE_IC_BINDING", "0") == "1"


def _warn(msg: str) -> None:
    print(f"[dataio] {msg}", file=sys.stderr)


def list_boards() -> list:
    """Sorted board ids = subfolders of data/ that contain all required files.

    Powers the front-end's auto-detecting board dropdown.
    """
    if not os.path.isdir(DATA):
        return []
    out = []
    for name in os.listdir(DATA):
        d = os.path.join(DATA, name)
        if os.path.isdir(d) and all(
            os.path.isfile(os.path.join(d, f)) for f in _BOARD_FILES.values()
        ):
            out.append(name)
    return sorted(out)


def board_paths(board: str = DEFAULT_BOARD) -> dict:
    """Resolve a board id to its five data-file paths under data/<board>/.

    Falls back to the default board's folder if the requested one is absent, so
    callers (incl. load_knowledge() with no args) always get a valid set.
    """
    d = os.path.join(DATA, board or DEFAULT_BOARD)
    if not os.path.isdir(d):
        d = os.path.join(DATA, DEFAULT_BOARD)
    paths = {role: os.path.join(d, fname) for role, fname in _BOARD_FILES.items()}
    paths.update({role: os.path.join(d, fname)
                  for role, fname in _BOARD_FILES_OPTIONAL.items()})
    return paths


# Single-board path constants = the default board's files. Kept so the CLI /
# __main__ demos and load_knowledge()'s defaults work without passing a board.
_DEFAULT_PATHS = board_paths(DEFAULT_BOARD)
AF_TABLE = _DEFAULT_PATHS["af"]
PROFILES = _DEFAULT_PATHS["profiles"]
DTS = _DEFAULT_PATHS["dts"]
SIGNALS = _DEFAULT_PATHS["signals"]
REQUIRE = _DEFAULT_PATHS["require"]


def load_gpio_pins(require_path: str, af_keys=None) -> set:
    """require.json["gpio_must_pins"]["pins"] -> set(pin). These pads must stay
    plain GPIO and are fed to the solver as must_gpio so they are never
    auto-assigned to a peripheral. (GPIO reservation moved here from
    signal_to_pin.json's old "gpio_pins" key; the sibling "silicon_fixed" map is
    annotation only and is deliberately not parsed.)

    Defensive: returns set() if the key is missing/malformed. When af_keys is
    given, warns for any gpio pin not in the AF table (the solver would silently
    ignore such a lock, since it only ever filters pins it actually knows about).
    """
    try:
        with open(require_path, encoding="utf-8") as fh:
            raw = (json.load(fh).get("gpio_must_pins") or {}).get("pins", [])
    except (OSError, ValueError, AttributeError) as exc:
        _warn(f"could not read gpio_must_pins from {require_path}: {exc}")
        return set()
    if not isinstance(raw, list):
        _warn(f"gpio_must_pins.pins in {require_path} is not a list; ignoring")
        return set()
    pins = {str(p).upper() for p in raw if p}
    if af_keys is not None:
        unknown = sorted(p for p in pins if p not in af_keys)
        if unknown:
            _warn(f"gpio_must_pins not in AF table (lock has no effect): {unknown}")
    return pins


def _load_boot_groups(require_path: str) -> dict:
    """require.json["boot_pin_locked"]["groups"] -> {name: block}. Defensive:
    a missing/malformed file or section yields {} (with a warning), so callers
    degrade to "no boot constraints" instead of crashing."""
    try:
        with open(require_path, encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError) as exc:
        _warn(f"could not read boot_pin_locked from {require_path}: {exc}")
        return {}
    spec = data.get("boot_pin_locked") if isinstance(data, dict) else None
    groups = spec.get("groups") if isinstance(spec, dict) else None
    return groups if isinstance(groups, dict) else {}


def _pin_map_rows(name: str, block) -> list:
    """One group's pin_map ([[signal, pin, af], …]) -> [(SIGNAL, PIN)], the af
    column dropped: (pin, signal) fixes the AF uniquely in the AF table, so the
    solver/report resolve it via af_of and stay consistent by construction.
    Malformed rows are warned about and skipped."""
    rows = block.get("pin_map") if isinstance(block, dict) else None
    out = []
    for row in rows if isinstance(rows, list) else []:
        if not (isinstance(row, (list, tuple)) and len(row) >= 2):
            _warn(f"boot_pin_locked[{name}]: malformed pin_map row {row!r}; skipped")
            continue
        sig, pin = str(row[0]).strip().upper(), str(row[1]).strip().upper()
        if sig and pin:
            out.append((sig, pin))
    return out


def _solver_action(block) -> str:
    """Group's solver_action, defaulting to the historic emit behaviour."""
    if isinstance(block, dict) and block.get("solver_action"):
        return str(block["solver_action"]).strip().lower()
    return "emit_fixed_assignment"


def load_require_signals(require_path: str, sigma) -> list:
    """Boot-essential signals the KERNEL plan must place, FILTERED to Σ,
    order-preserving: pin_map column 0 of every boot_pin_locked group whose
    solver_action is emit_fixed_assignment. reserve_only groups (secure- /
    bootloader-owned, e.g. the PMIC I2C) are deliberately EXCLUDED — their
    signals must never enter the kernel solution (see load_reserved). The
    groups' other keys (role / dt_pin_groups / in_kernel_dt / owner) are
    documentation for DTS generation and are not parsed here.
    """
    kept, dropped, seen = [], [], set()
    for name, block in _load_boot_groups(require_path).items():
        if _solver_action(block) != "emit_fixed_assignment":
            continue
        for sig, _pin in _pin_map_rows(name, block):
            if sig in seen:
                continue
            seen.add(sig)
            (kept if sig in sigma else dropped).append(sig)
    if dropped:
        _warn(f"require.json: dropped {len(dropped)} non-Σ boot signals: {dropped}")
    return kept


# --------------------------------------------------------------------------- #
# input                                                                        #
# --------------------------------------------------------------------------- #
def _atoms(cell):
    """One AF cell -> atomic signal names. Splits dual names on '/', drops '-'."""
    return [s for s in (p.strip() for p in str(cell).split("/")) if s and s != "-"]


def load_af(path):
    """af_table.json -> {pin: set(signals)}.

    Accepts both layouts:
      * AF-preserving dict  {"PF0": {"2": "SPI3_SCK/I2S3_CK", "15": "EVENTOUT"}}
      * legacy flat list    {"PF0": ["SPI3_SCK", "I2S3_CK", "EVENTOUT"]}
    Dual-function cells ("A/B") are split and '-' markers dropped, so the solver
    sees the same atomic Σ regardless of layout.
    """
    with open(path, encoding="utf-8") as fh:
        raw = json.load(fh)
    table = {}
    for pin, sigs in raw.items():
        values = sigs.values() if isinstance(sigs, dict) else sigs
        out = set()
        for v in values:
            out.update(_atoms(v))
        table[pin] = out
    return table


def load_af_map(path):
    """af_table.json (AF-preserving dict form) -> {pin: {af_int: [signals]}}.

    Use this when you need the AF NUMBER (e.g. DTS pinmux generation, or the
    resolver's AF tiebreak). For the legacy list form (no AF numbers) each pin
    maps to {} since the AF is unrecoverable from that layout.
    """
    with open(path, encoding="utf-8") as fh:
        raw = json.load(fh)
    out = {}
    for pin, sigs in raw.items():
        if isinstance(sigs, dict):
            out[pin] = {int(af): _atoms(cell) for af, cell in sigs.items()}
        else:
            out[pin] = {}
    return out


def af_of(af_map, pin, signal):
    """Return the AF number exposing `signal` on `pin` (from load_af_map), or None."""
    for af, sigs in af_map.get(pin, {}).items():
        if signal in sigs:
            return af
    return None


def sigma_of(af):
    """Σ — every distinct signal across all pins."""
    return {s for sigs in af.values() for s in sigs}


def load_signal_to_pin(path):
    """signal_to_pin.json  ->  {signal: official_pin} (for the cross-check)."""
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)["signal_to_pin"]


def load_board_components(path) -> dict:
    """board_components.json -> {INSTANCE: {ic, role, compatible, binding_doc,
    source, notes}}. 板上外部 IC 對照（G4）：無外部 IC 的周邊不列。Optional 檔案
    ——缺檔/壞檔回 {}（板子沒有 IC 知識時，ic 欄留空、lookup_binding 回 no_ic）。"""
    try:
        with open(path, encoding="utf-8") as fh:
            comp = json.load(fh).get("components") or {}
    except FileNotFoundError:
        return {}
    except (OSError, ValueError, AttributeError) as exc:
        _warn(f"could not read board components from {path}: {exc}")
        return {}
    if not isinstance(comp, dict):
        _warn(f"components in {path} is not a dict; ignoring")
        return {}
    return {str(k).upper(): v for k, v in comp.items() if isinstance(v, dict)}


def load_pin_locked(require_path) -> dict:
    """Fixed boot assignments -> a must_bind map {pin: signal}, straight from
    the emit_fixed_assignment groups' pin_map in require.json (the [signal,
    pin, af] triples are board constants — no signal_to_pin lookup involved).
    The must_bind both pins the signal AND removes the pad from every other
    signal's candidate domain. reserve_only groups contribute nothing here:
    their pads are reserved via load_reserved() and never bound in the kernel
    plan. Returns {} when the section is absent (no fixed routing declared).
    """
    locked = {}
    for name, block in _load_boot_groups(require_path).items():
        if _solver_action(block) != "emit_fixed_assignment":
            continue
        for sig, pin in _pin_map_rows(name, block):
            if pin in locked and locked[pin] != sig:
                _warn(f"boot_pin_locked: {pin} mapped to both {locked[pin]} "
                      f"and {sig}; keeping {locked[pin]}")
                continue
            locked[pin] = sig
    return locked


def load_reserved(require_path) -> tuple:
    """reserve_only groups -> (pins, instances) the KERNEL may never touch.

    pins      : every pad in those groups' pin_map. Fed to the solver together
                with gpio_must_pins, so no kernel signal can land on them.
    instances : the group NAMES (e.g. I2C7, OCTOSPIM_P1). A signal belongs to a
                reserved instance iff it equals the name or starts with
                "<NAME>_" (so OCTOSPIM_P1 never shadows OCTOSPIM_P2). Used to
                refuse expanding these peripherals for kernel requests — "兩個
                I2C" must pick instances other than I2C7 — and to keep them out
                of plan.csv.
    """
    pins, instances = set(), set()
    for name, block in _load_boot_groups(require_path).items():
        if _solver_action(block) != "reserve_only":
            continue
        instances.add(str(name).strip().upper())
        for _sig, pin in _pin_map_rows(name, block):
            pins.add(pin)
    return pins, instances


# --------------------------------------------------------------------------- #
# output                                                                       #
# --------------------------------------------------------------------------- #
# Single, overwritten output location (no more accumulating plan_1, plan_2, …).
PLAN_DIR = os.path.join(OUTPUT, "plan")


def write_plan(required, result, af_map=None, outdir=PLAN_DIR):
    """Write the assignment to output/plan/plan.csv
    (peripheral, signal, pin, af), overwriting any previous plan.  The peripheral
    column is the signal's prefix (ETH1_RGMII_TXD0 -> ETH1); af is the integer
    alternate-function number exposing that signal on that pin (pin+signal -> a
    fixed af, looked up via af_of).  Pass the board's af_map for multi-board
    correctness; when None, defaults to the default board's AF table.  Returns the
    path written, or None when UNSAT.
    """
    if not result.sat:
        print(f"  UNSAT — no plan written ({result.reason})")
        return None
    if af_map is None:
        af_map = load_af_map(AF_TABLE)
    os.makedirs(outdir, exist_ok=True)
    path = os.path.join(outdir, "plan.csv")
    with open(path, "w", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh)
        writer.writerow(["peripheral", "signal", "pin", "af"])
        for sig in required:
            pin = result.assignment[sig]
            af = af_of(af_map, pin, sig)
            writer.writerow([sig.split("_", 1)[0], sig, pin, "" if af is None else af])
    print(f"  wrote {path}  ({len(required)} rows)")

    # Also (over)write the styled .xlsx beside it (best-effort: a missing
    # openpyxl only skips the spreadsheet, the CSV above is already written).
    xlsx = os.path.join(outdir, "plan.xlsx")
    try:
        from util.csv2xlsx import csv_to_grouped_xlsx
        csv_to_grouped_xlsx(path, xlsx)
    except Exception as exc:
        print(f"  (xlsx skipped: {exc})")
    return path


def plan_csv_text(rows) -> str:
    """rows: [{peripheral, signal, pin, af, ic?}] -> CSV text (for download).

    ic 欄是**資料驅動**的：只有任一 row 真的帶 ic（板上外部 IC，G4 功能開啟時
    由 service 填入）才輸出該欄；G4 停用或板上無 IC 時維持四欄，舊消費者不受
    影響。"""
    import io
    buf = io.StringIO()
    writer = csv.writer(buf)
    has_ic = any(r.get("ic") for r in rows)
    writer.writerow(["peripheral", "signal", "pin", "af"] + (["ic"] if has_ic else []))
    for r in rows:
        writer.writerow([r.get("peripheral", ""), r.get("signal", ""),
                         r.get("pin", ""), r.get("af", "")]
                        + ([r.get("ic", "")] if has_ic else []))
    return buf.getvalue()


def plan_xlsx_bytes(rows) -> bytes:
    """rows -> grouped .xlsx bytes (reuses the same styling as write_plan)."""
    import tempfile
    from util.csv2xlsx import csv_to_grouped_xlsx
    text = plan_csv_text(rows)
    with tempfile.TemporaryDirectory() as td:
        cpath = os.path.join(td, "plan.csv")
        xpath = os.path.join(td, "plan.xlsx")
        with open(cpath, "w", newline="", encoding="utf-8") as fh:
            fh.write(text)
        csv_to_grouped_xlsx(cpath, xpath)
        with open(xpath, "rb") as fh:
            return fh.read()


def cross_check(required, result, s2p):
    """Print how many assigned pins match the official EV1 pinout (s2p).

    Only signals present in s2p are compared, so it works for both the
    signal-first set (all present) and a peripheral-first set (subset).
    """
    if not result.sat:
        return
    common = [s for s in required if s in s2p]
    same = 0
    for s in common:
        if result.assignment[s] == s2p[s]:
            same += 1
        else:
            print(f"  (diff) {s:20} {result.assignment[s]:10} vs {s2p[s]:10}")
    print(f"\n  cross-check vs official EV1 pinout: {same}/{len(common)} identical")
    print("  (differences are alternative valid matchings, not errors)")
