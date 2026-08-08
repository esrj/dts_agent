"""
tools.py — the orchestrator's LOCKED action set (Phase 0 of the upgrade plan).

Every tool here is a clean, typed, side-effect-light wrapper over the EXISTING
deterministic machinery. No solver logic is reimplemented: `solve_pinmux` drives
the very same `service.Pipeline._continue` path the web pipeline uses, so the
anti-corruption layer (resolver / counts -> validated SolveSpec) is enforced
identically. The LLM's `intent` JSON NEVER reaches `solve.py` directly — only a
validated `SolveSpec` does.

The orchestrator (agent.py) calls these in an agentic loop and decides — on its
own — how many times to call `solve_pinmux` (e.g. try, read the Hall witness on
UNSAT, adjust the intent, try again). Because every attempt re-enters the
deterministic core, "LLM free to retry" and "results are correct" do not
conflict: each retry is re-verified by the CSP solver.

Tools (the whole surface; do not widen without updating ANTHROPIC_TOOLS):
  solve_pinmux(intent, board)   -> {sat | unsat(+hall_violator) | clarify | invalid}
  get_capabilities(board)       -> board's families/instances/boot set (read-only)
  emit_plan(assignment, board)  -> write plan.csv / plan.xlsx
"""
from __future__ import annotations

import copy
import json
import os
import re
import sys
import time
import urllib.request

# Root the imports at src/ (same convention as the other modules).
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from service import Pipeline
from util import dataio                      # 模組引用：功能旗標於呼叫時讀取
from util.dataio import DEFAULT_BOARD, OUTPUT, board_paths, plan_csv_text, plan_xlsx_bytes
from solver.resolver import ResolveError, reserved_owner
from solver.counts import _instances_in_sigma
from solver.peripherals import _split_family
from solver.solve import Instance, hall_violator

# st linux 的 DT bindings 樹（G4 的唯一外部來源；raw 供抓取、blob 供人看）。
# （CubeMX 序列化鎖已隨引擎搬至 validator/engines.py 的 _CUBEMX_LOCK。）
BINDINGS_REF = "v6.6-stm32mp"
BINDINGS_RAW = ("https://raw.githubusercontent.com/STMicroelectronics/linux/"
                f"{BINDINGS_REF}/Documentation/devicetree/bindings/")
BINDINGS_BLOB = ("https://github.com/STMicroelectronics/linux/blob/"
                 f"{BINDINGS_REF}/Documentation/devicetree/bindings/")


def _fetch_binding_doc(doc_path: str, timeout: float = 10.0) -> str:
    """抓一份 binding yaml/txt 原文（tests 以 monkeypatch 換掉本函式）。"""
    with urllib.request.urlopen(BINDINGS_RAW + doc_path, timeout=timeout) as resp:
        return resp.read().decode("utf-8", errors="replace")


def _summarize_binding(text: str) -> dict:
    """binding yaml 原文 -> 摘要（title / 頂層 required 屬性清單）。
    只做輕量截取（binding 很長，整檔不入庫）；抓不到就回空欄位，不編造。"""
    title = ""
    m = re.search(r"^title:\s*(.+)$", text, re.M)
    if m:
        title = m.group(1).strip()
    required: list[str] = []
    m = re.search(r"^required:\n((?:\s+-\s+\S+\n?)+)", text, re.M)
    if m:
        required = re.findall(r"-\s+(\S+)", m.group(1))
    maintainers = re.findall(r"^\s+-\s+(.+@.+)$", text[:1200], re.M)[:2]
    return {"title": title, "required_properties": required,
            "maintainers": maintainers}


class SolverTools:
    """Stateful only in the sense that it caches per-board data (via Pipeline).

    One instance is shared across all chat sessions; `_pipeline` owns the board
    cache, so repeated calls for the same board are cheap.
    """

    def __init__(self) -> None:
        self._pipeline = Pipeline()

    # ----------------------------------------------------------------------- #
    # CORE: the LLM may call this as many times as it wants                    #
    # ----------------------------------------------------------------------- #
    def solve_pinmux(self, intent: dict, board: str | None = None) -> dict:
        """Run one structured request through the deterministic core.

        `intent` is an IntentIR dict (same schema the parser emits — see the
        orchestrator system prompt). It is validated by the resolver/counts
        anti-corruption layer before any signal reaches the CSP solver.

        Returns exactly one of (all JSON-friendly):
          {status:"sat",     assignment, chosen, notes, stats, ...}
          {status:"unsat",   reason, hall_violator, required, must_bind, notes}
          {status:"clarify", question:{summary, options, ...}}
          {status:"invalid", reason}     # resolver rejected the request shape
          {status:"error",   message}    # unexpected failure
        """
        board = board or DEFAULT_BOARD
        try:
            b = self._pipeline._load_board(board)
        except Exception as exc:                       # bad board id / data
            return {"status": "error", "board": board,
                    "message": f"無法載入板子 {board}：{exc}"}

        try:
            out = self._pipeline._continue(copy.deepcopy(intent or {}), b, board)
        except ResolveError as exc:
            # The anti-corruption layer refused it (unknown signal/family, pin
            # conflict, impossible AF, magnitude…). The message is already
            # human-readable — hand it back as repair material.
            return {"status": "invalid", "board": board, "reason": str(exc)}
        except Exception as exc:
            return {"status": "error", "board": board, "message": str(exc)}

        if out.get("status") == "clarify":
            q = out.get("question") or {}
            return {
                "status": "clarify",
                "board": board,
                "question": {
                    "summary": q.get("summary"),
                    "kind": q.get("kind"),
                    "pin": q.get("pin"),
                    "options": [
                        # 全欄位保留：mode 與 bindings（count_af 選項的 [{signal,pin,af}]
                        # 固定綁定集）折回 intent 時不可少，否則 AF 約束會靜默流失。
                        {"label": o.get("label"), "note": o.get("note"),
                         "signal": o.get("signal"), "instance": o.get("instance"),
                         "pin": o.get("pin"), "af": o.get("af"),
                         "mode": o.get("mode"), "bindings": o.get("bindings") or []}
                        for o in (q.get("options") or [])
                    ],
                },
            }

        # status == "done"
        # optional（條件式需求）的取捨欄位原樣透傳，模型不必從 notes 文字反推。
        optional_fields = {k: out[k] for k in
                           ("optional_included", "optional_dropped", "optional_reason")
                           if k in out}
        if out.get("sat"):
            spec = out.get("spec") or {}
            return {
                "status": "sat",
                "board": board,
                "request_type": out.get("request_type"),
                "source": out.get("source"),               # "baseline" when default
                "assignment": out.get("assignment"),
                "chosen": out.get("chosen"),               # per-family auto-selection
                "notes": spec.get("notes"),
                "boot_relaxed": out.get("boot_relaxed", False),
                "boot_moved": out.get("boot_moved"),
                "stats": out.get("stats"),
                **optional_fields,
            }

        # UNSAT — attach a structured Hall witness as repair material.
        spec = out.get("spec") or {}
        return {
            "status": "unsat",
            "board": board,
            "reason": out.get("reason"),
            "required": spec.get("required"),
            "must_bind": spec.get("must_bind"),
            "notes": spec.get("notes"),
            "hall_violator": self._hall_witness(
                b, spec.get("required") or [], spec.get("must_bind") or {}),
            **optional_fields,
        }

    # ----------------------------------------------------------------------- #
    # READ-ONLY: let the model construct valid intents / reason about limits   #
    # ----------------------------------------------------------------------- #
    def get_capabilities(self, board: str | None = None,
                         instance: str | None = None) -> dict:
        """What this board can do: families & their instances present in Σ, each
        family's modes, the boot-essential set already provided (deductible from
        a count), and how many pins are GPIO-locked. The model uses this to build
        a satisfiable intent and to explain magnitude limits ("only 2 ETH free").

        instance（可選）：加查該 instance 每個 signal 的合法候選腳（Σ 域）——
        「換腳/讓腳」需求先看這個：usable 只剩一支＝無法換腳，直接回答不可行。"""
        board = board or DEFAULT_BOARD
        try:
            b = self._pipeline._load_board(board)
        except Exception as exc:
            return {"status": "error", "board": board,
                    "message": f"無法載入板子 {board}：{exc}"}
        kb = b.kb

        families: dict[str, list[str]] = {}
        modes: dict[str, dict] = {}
        for key, fdef in kb.profiles["families"].items():
            # reserve_only instances (require.json, e.g. the secure-world PMIC
            # I2C) are not requestable — keep them out of the availability list.
            insts = [t for t in _instances_in_sigma(key, kb.sigma, kb.profiles)
                     if t not in b.reserved_instances]
            if not insts:
                continue
            families[key.upper()] = insts
            modes[key.upper()] = {
                "default": fdef.get("default"),
                "modes": sorted((fdef.get("modes") or {}).keys()),
            }

        # Standalone (unnumbered) peripherals — declared per-instance in
        # profiles["peripherals"] and outside the numbered-families model
        # (PCIE, USBH, LCD, …). Requested as level="peripheral" with
        # instance=<name>; an empty default_required_signals list means the
        # peripheral runs on dedicated pads and costs no GPIO AF pin.
        standalone: dict[str, dict] = {}
        for name, prof in (kb.profiles.get("peripherals") or {}).items():
            if not isinstance(prof, dict):
                continue
            if _split_family(str(name).upper(), kb.profiles["families"]):
                continue                   # numbered instance — already under families
            standalone[str(name).upper()] = {
                "mode": prof.get("mode"),
                "default_required_signals": prof.get("default_required_signals") or [],
                "optional_signals": prof.get("optional_signals") or [],
            }

        # Instances the board's boot-essential set already supplies, by family —
        # a count of N for that family only needs max(0, N - provided) NEW pins.
        boot_provided: dict[str, set] = {}
        for sig in sorted(b.boot_set):
            tok = sig.split("_", 1)[0]
            fam = _split_family(tok, kb.profiles["families"])
            if fam:
                boot_provided.setdefault(fam[2].upper(), set()).add(tok)

        # 指定 instance 的 signal 候選腳明細（唯讀；「換腳/讓腳」判斷的依據）
        instance_detail = None
        if instance:
            tok = str(instance).strip().upper()
            doms: dict[str, set] = {}
            for pin, sigs in b.af.items():
                for s in sigs:
                    if str(s).upper().split("_", 1)[0] == tok:
                        doms.setdefault(str(s), set()).add(pin)
            official = {s: p for s, p in b.baseline.items()
                        if s.split("_", 1)[0].upper() == tok}
            instance_detail = {
                "instance": tok,
                "signals": {
                    s: {"candidate_pins": sorted(v),
                        "usable_pins": sorted(v - b.must_gpio),
                        "official_pin": official.get(s)}
                    for s, v in sorted(doms.items())
                },
                "note": ("candidate_pins = Σ 全部合法腳；usable_pins 已扣掉板級"
                         "保留腳（gpio/secure）。usable 只剩 1 支＝該 signal 無法"
                         "換腳；為空＝完全不可用。"),
            } if doms else {
                "instance": tok,
                "signals": {},
                "note": "此 instance 在 Σ 中沒有任何 signal——名稱可能有誤。",
            }

        return {
            "status": "ok",
            "board": board,
            "families": families,
            "modes": modes,
            **({"instance_detail": instance_detail} if instance_detail else {}),
            "standalone_peripherals": standalone,
            "boot_provided": {k: sorted(v) for k, v in boot_provided.items()},
            "boot_required_signals": sorted(b.boot_required),
            "pin_locked_interfaces": sorted(
                {s.split("_", 1)[0] for s in b.pin_locked.values()}),
            "reserved_instances": sorted(b.reserved_instances),
            "reserved_pin_count": len(b.reserved_pins),
            "gpio_locked_pin_count": len(b.gpio_pins),
            "note": ("families = 此板可請求的編號週邊與 instance（已排除 secure 保留者）。"
                     "boot_provided = 開機必備已固定佔用的 instance——不可沿用，count "
                     "需求一律另配其他 instance。reserved_instances = secure/bootloader "
                     "持有的保留週邊（require.json reserve_only），kernel 不可請求、其"
                     "腳位不可分配、不輸出到 plan。standalone_peripherals = 無編號週邊"
                     "（PCIE/USBH…），以 level=peripheral、instance=<名稱> 請求；"
                     "default_required_signals 為空代表使用專用腳位、不需 pin "
                     "assignment；optional_signals 預設不啟用，使用者明確要求時以 "
                     "signal-level item 帶入。"),
        }

    # ----------------------------------------------------------------------- #
    # READ-ONLY + CACHE: external-IC Device Tree binding lookup (G4)           #
    # ----------------------------------------------------------------------- #
    @staticmethod
    def _binding_cache_path(board: str) -> str:
        return board_paths(board)["binding_cache"]

    def _load_binding_cache(self, board: str) -> dict:
        try:
            with open(self._binding_cache_path(board), encoding="utf-8") as fh:
                cache = json.load(fh)
            return cache if isinstance(cache, dict) else {}
        except (OSError, ValueError):
            return {}

    def _save_binding_cache(self, board: str, cache: dict) -> None:
        try:
            path = self._binding_cache_path(board)
            os.makedirs(os.path.dirname(path), exist_ok=True)   # 新板子還沒有 cache/
            with open(path, "w", encoding="utf-8") as fh:
                json.dump(cache, fh, ensure_ascii=False, indent=2)
        except OSError:
            pass                                   # 快取寫不進去不影響查詢結果

    def lookup_binding(self, peripheral: str | None = None,
                       board: str | None = None) -> dict:
        """板上外部 IC 的 Device Tree binding 查詢（兩段式，全部可溯源）：

          1. KB：data/<board>/bindings/board_components.json（instance ↔ IC ↔ compatible
             ↔ binding_doc，手工整理自原理圖與官方 DTS，每筆帶 source）。
          2. binding 細節：先查 data/<board>/cache/binding_cache.json；miss 才抓
             st linux v6.6-stm32mp 的 bindings 樹，摘要（title / required
             properties）寫回快取——離線時 KB/快取命中仍可用。

        回傳（擇一）：
          {status:"ok", ic, compatible, binding_doc, source_url, kb_source, …}
          {status:"no_ic"}      # 真周邊但板上無外部 IC（純 SoC 內部／插槽）
          {status:"not_found"}  # 不認識的周邊名——絕不編造
        LLM 只能引用這裡回傳的內容；查無就是查無。"""
        board = board or DEFAULT_BOARD
        try:
            b = self._pipeline._load_board(board)
        except Exception as exc:
            return {"status": "error", "board": board,
                    "message": f"無法載入板子 {board}：{exc}"}

        per = (peripheral or "").strip().upper()
        if not per:
            return {"status": "error", "message": "peripheral 不可為空"}

        comp = b.components.get(per)
        if comp is None:
            # family 名（如 ETH）→ 指路到有 IC 的 instance
            prefixed = sorted(k for k in b.components if k.startswith(per))
            if prefixed:
                return {"status": "error", "board": board,
                        "message": f"請用 instance 名查詢；「{per}」開頭且板上有"
                                   f"外部 IC 的：{prefixed}"}
            known = {s.split("_", 1)[0] for s in b.kb.sigma}
            if per in known or any(str(k).upper() == per
                                   for k in (b.kb.profiles.get("peripherals") or {})):
                return {"status": "no_ic", "board": board, "peripheral": per,
                        "message": f"{per} 在本板沒有對應的外部 IC（純 SoC 內部周邊"
                                   "或僅接插槽），plan 的 ic 欄留空。"}
            return {"status": "not_found", "board": board, "peripheral": per,
                    "message": f"未知周邊「{per}」。本板有外部 IC 的周邊："
                               f"{sorted(b.components)}"}

        out = {
            "status": "ok",
            "board": board,
            "peripheral": per,
            "ic": comp.get("ic"),
            "role": comp.get("role"),
            "compatible": comp.get("compatible"),
            "binding_doc": comp.get("binding_doc"),
            "source_url": (BINDINGS_BLOB + comp["binding_doc"])
                           if comp.get("binding_doc") else None,
            "kb_source": comp.get("source"),
            "notes": comp.get("notes"),
        }

        doc = comp.get("binding_doc")
        if doc:
            cache = self._load_binding_cache(board)
            entry = cache.get(doc)
            if entry is None:
                try:
                    text = _fetch_binding_doc(doc)
                    entry = _summarize_binding(text)
                    entry["source_url"] = BINDINGS_BLOB + doc
                    entry["fetched_at"] = time.strftime("%Y-%m-%d")
                    cache[doc] = entry
                    self._save_binding_cache(board, cache)
                except Exception as exc:
                    out["web"] = (f"binding 原文暫不可得（{exc}）；以上為 KB 內容，"
                                  "來源見 kb_source/source_url。")
            if entry:
                out["binding"] = entry
        return out

    # ----------------------------------------------------------------------- #
    # VERIFY-THEN-PROPOSE: suggestion cards (G6) — 先驗證再提案的機械閘門      #
    # ----------------------------------------------------------------------- #
    def propose_suggestion(self, summary: str | None, intent: dict | None,
                           board: str | None = None) -> dict:
        """把「修改建議」提交成前端可一鍵採納的卡片——**伺服器先重解驗證**，
        只有真的 SAT 的 intent 才會被接受（先驗證再提案不再只是 prompt 規範，
        而是機械強制）。回 ok 時由 agent dispatch 存進 session.suggestions。"""
        summary = (summary or "").strip()
        if not summary or not isinstance(intent, dict) or not intent.get("items"):
            return {"status": "rejected",
                    "reason": "需要 summary（一句人話）與含 items 的完整 intent。"}
        out = self.solve_pinmux(intent, board)
        if out.get("status") != "sat":
            return {"status": "rejected",
                    "reason": (f"此建議未通過驗證（{out.get('status')}）："
                               f"{out.get('reason') or out.get('message') or ''} "
                               "——不可端給使用者。"),
                    "verify": {k: out.get(k) for k in ("status", "reason")}}
        return {"status": "ok", "summary": summary,
                "verified": {
                    "signals": len(out.get("assignment") or []),
                    "chosen": out.get("chosen"),
                    "optional_dropped": out.get("optional_dropped"),
                }}

    # ----------------------------------------------------------------------- #
    # DETERMINISTIC (green): official板級驗證（G5；引擎依 board.yaml 分派）      #
    # ----------------------------------------------------------------------- #
    def run_validator(self, assignment: list | None,
                      board: str | None = None) -> dict:
        """把一份已 SAT 的 assignment 交給該板 manifest 指定的驗證引擎
        （data/<board>/board.yaml validation：cubemx / script / none——見
        validator/engines.py）。產物與 result.json 覆寫至 output/validator/。

        回傳 {status:"pass"|"fail"|"error"|"skipped", conflicts[{pin,signal,
        message}], artifacts_dir, …}。skipped＝該板未啟用官方驗證——終態，
        不是失敗、不需重試。CubeMX 板行為與過去相同：未安裝 → error +
        明確安裝提示（不擋既有功能）；CubeMX 執行由引擎內全域鎖序列化
        （自動背景驗證與模型顯式驗證可能並發，產物目錄覆寫制必互踩）。
        跟 emit_plan 一樣：呼叫端（agent dispatch）只餵伺服器保存的已驗證解。"""
        from validator.engines import engine_for

        board = board or DEFAULT_BOARD
        rows = assignment or []
        if not rows:
            return {"status": "error",
                    "artifacts_dir": os.path.join(OUTPUT, "validator"),
                    "message": "沒有可驗證的分配（assignment 為空）——請先成功求解。"}
        return engine_for(board).validate(rows, board, self._pipeline._load_board)

    # ----------------------------------------------------------------------- #
    # SIDE EFFECT: persist a confirmed plan to disk                            #
    # ----------------------------------------------------------------------- #
    def emit_plan(self, assignment: list | None, board: str | None = None,
                  fmt: str = "csv") -> dict:
        """Write an assignment ([{peripheral, signal, pin, af}]) to
        output/plan/plan.csv (+ plan.xlsx when fmt='xlsx'), reusing the same
        styling as the web export. Returns the paths written.

        Defense-in-depth: every row is re-validated against this board's AF table
        before anything is written — a signal must exist in Σ and the pin must be
        able to carry it. So even a caller that hands us hand-crafted rows can
        never persist an invalid pin/signal/af triple to disk (the anti-corruption
        invariant extends to file output)."""
        board = board or DEFAULT_BOARD
        rows = assignment or []
        if not rows:
            return {"status": "error", "message": "沒有可輸出的分配（assignment 為空）"}
        try:
            b = self._pipeline._load_board(board)
        except Exception as exc:
            return {"status": "error", "message": f"無法載入板子 {board}：{exc}"}

        bad = []
        for r in rows:
            # signal 先試原大小寫（TI 等板含小寫，如 UART0_CTSn），再退大寫（ST 慣例）
            raw = (r.get("signal") or "")
            sig = raw if raw in b.kb.sigma else raw.upper()
            pin = (r.get("pin") or "").upper()
            owner = reserved_owner(sig, b.reserved_instances)
            if sig not in b.kb.sigma:
                bad.append(f"{sig or '∅'}（未知 signal）")
            elif owner:
                bad.append(f"{sig}（保留週邊 {owner}，由 secure/bootloader 持有，"
                           "不得輸出到 kernel plan）")
            elif pin not in b.kb.af or sig not in b.kb.af.get(pin, set()):
                bad.append(f"{sig}@{pin or '∅'}（{pin or '∅'} 接不了該 signal）")
        if bad:
            return {"status": "error",
                    "message": "拒絕輸出未通過驗證的分配：" + "；".join(bad[:8])
                               + ("…" if len(bad) > 8 else "")}

        norm = [
            {"peripheral": r.get("peripheral") or (r.get("signal") or "").split("_", 1)[0],
             "signal": r.get("signal"), "pin": r.get("pin"),
             "af": "" if r.get("af") is None else r.get("af"),
             **({"ic": r.get("ic") or b.ic_of((r.get("signal") or "").upper())}
                if dataio.IC_BINDING_ENABLED else {}),
             **({"function": r["function"]} if r.get("function") else {})}
            for r in rows
        ]
        outdir = os.path.join(OUTPUT, "plan")
        os.makedirs(outdir, exist_ok=True)
        csv_path = os.path.join(outdir, "plan.csv")
        with open(csv_path, "w", newline="", encoding="utf-8") as fh:
            fh.write(plan_csv_text(norm))
        written = [csv_path]
        if (fmt or "csv").lower() == "xlsx":
            xlsx_path = os.path.join(outdir, "plan.xlsx")
            try:
                with open(xlsx_path, "wb") as fh:
                    fh.write(plan_xlsx_bytes(norm))
                written.append(xlsx_path)
            except Exception as exc:                   # openpyxl missing etc.
                return {"status": "ok", "rows": len(norm), "files": written,
                        "warning": f"xlsx 略過：{exc}"}
        return {"status": "ok", "rows": len(norm), "files": written}

    # ----------------------------------------------------------------------- #
    # internal                                                                 #
    # ----------------------------------------------------------------------- #
    @staticmethod
    def _hall_witness(b, required: list, must_bind: dict) -> dict | None:
        """Best-effort Hall's-condition certificate for an UNSAT result: a
        deficient signal set S whose candidate pins N(S) satisfy |N(S)| < |S|.
        Returns None when no cheap witness exists (e.g. the infeasibility comes
        from forced must_bind rather than a pure pigeonhole) — the `reason`
        string still carries the solver's precise explanation."""
        try:
            inst = Instance(af=b.kb.af, required=list(required),
                            must_gpio=set(b.must_gpio), must_bind=dict(must_bind))
            witness = hall_violator(inst)
        except Exception:
            return None
        if not witness:
            return None
        S, NS = witness
        return {
            "deficient_signals": list(S),
            "shared_pins": sorted(NS),
            "explanation": (
                f"{len(S)} 個訊號擠在 {len(NS)} 支腳上："
                f"S={list(S)} 只能用 N(S)={sorted(NS)}"
                f"（|N(S)|={len(NS)} < |S|={len(S)}），鴿籠原理下無法同時佈線。"),
        }


# --------------------------------------------------------------------------- #
# Self-test (no orchestrator / API key needed for solve_pinmux & capabilities) #
# --------------------------------------------------------------------------- #
if __name__ == "__main__":
    import json

    tools = SolverTools()
    print("=== get_capabilities ===")
    caps = tools.get_capabilities()
    print("families:", list(caps["families"].keys()))
    print("boot_provided:", caps["boot_provided"])

    print("\n=== solve_pinmux: 2 ETH + 1 I2C (count) ===")
    r = tools.solve_pinmux({
        "items": [
            {"level": "count", "family": "ETH", "mode": None, "count": 2},
            {"level": "count", "family": "I2C", "mode": None, "count": 1},
        ]})
    print("status:", r["status"])
    if r["status"] == "sat":
        print("chosen:", r["chosen"])
        print(f"{len(r['assignment'])} signals placed")

    print("\n=== solve_pinmux: impossible (20 I2C) ===")
    r = tools.solve_pinmux({
        "items": [{"level": "count", "family": "I2C", "mode": None, "count": 20}]})
    print(json.dumps(r, ensure_ascii=False, indent=2)[:600])
