"""
agent.py — the LLM orchestrator: an Anthropic tool-use agentic loop.

This is the core of the upgrade: control flow is no longer a hard-wired pipeline
(service.py) but a loop in which the model itself decides the next action from a
LOCKED action set. In particular the model decides — on its own — HOW MANY times
to call the solver: try a config, read the Hall witness on UNSAT, adjust the
intent, try again, … until SAT or provably no solution.

Correctness is never delegated to the model: every `solve_pinmux` call re-enters
the deterministic anti-corruption layer + CSP solver (see tools.py). The model
plans and explains; it never assigns a pin.

Invariants (the design red lines):
  1. action space is locked (ANTHROPIC_TOOLS below — do not widen casually);
  2. anything entering the deterministic core goes through the resolver;
  3. MAX_STEPS bounds the per-turn loop (cost / runaway guard);
  4. every decision + tool I/O is recorded in session.trace (replayable audit).
"""
from __future__ import annotations

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import time

from llm_provider import get_provider
from orchestrator.tools import SolverTools
from orchestrator.session import ChatSession
from util import dataio                      # 功能旗標（IC_BINDING_ENABLED）

# 本輪 LLM 活動的即時計數（/api/chat/progress 輪詢用；前端等待畫面顯示
# 「已思考 X 秒 ・ 已消耗 N tokens」）。單執行緒對話流：欄位覆寫即可。
LIVE = {"running": False, "started_at": None, "tokens": 0}

# Per-turn loop bound: each tool round is one iteration. Generous enough for a
# few UNSAT→adjust→retry cycles, low enough to cap cost / stop a runaway.
MAX_STEPS = 12

_PROMPT_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                            "system_prompt.md")

# IntentIR shape advertised to the model (the system prompt carries the detail).
_INTENT_SCHEMA = {
    "type": "object",
    "description": "IntentIR — 見 system prompt。只放使用者真正表達的需求，沒有的用 null/[]。",
    "properties": {
        "request_type": {"type": "string",
                         "enum": ["count", "peripheral", "signal", "mixed"]},
        "bootable_default": {"type": "boolean"},
        "items": {"type": "array", "items": {"type": "object"},
                  "description": "每個 item 一個 level（count / peripheral / signal）；"
                                 "條件式需求的 item 可帶 optional:true（見 system prompt）。"
                                 "使用者用功能名稱呼介面時（RS232/RS485/console…），該功能"
                                 "自成一個 item 並帶 label:\"<原詞>\"——同 family 多個具名"
                                 "功能各自一個 count item（各 count 1），不可合併；label 會"
                                 "成為 plan 的 function 欄，漏掉＝使用者無法區分同類介面。"},
        "loose_pins": {"type": "array", "items": {"type": "string"}},
        "reserve_gpio": {"type": "array", "items": {"type": "string"},
                         "description": "要讓出/保留給其他用途的腳位（PXn），或 instance "
                                        "名（如 \"I2C2\"＝該週邊的官方腳整組，伺服器展開，"
                                        "不要自己猜腳位）。這些腳會從所有 signal 候選域"
                                        "剔除，官方基底中用到它們的週邊整組讓出。"},
        "unresolved": {"type": "array", "items": {"type": "string"}},
    },
}

# The LOCKED action set. The model can pick only from these.
ANTHROPIC_TOOLS = [
    {
        "name": "solve_pinmux",
        "description": (
            "把結構化需求 intent 送進確定性求解器，回傳 sat / unsat / clarify / "
            "invalid。可重複呼叫：unsat 時依回傳的 hall_violator 調整 intent 再呼叫"
            "一次。這是取得腳位的唯一方式——你不可自己編腳位。"),
        "input_schema": {
            "type": "object",
            "properties": {
                "intent": _INTENT_SCHEMA,
                "board": {"type": "string", "description": "板子 id；省略則用目前目標板"},
            },
            "required": ["intent"],
        },
    },
    {
        "name": "get_capabilities",
        "description": (
            "查這塊板可用的 family/instance、各 family 的 mode、standalone 無編號"
            "週邊(standalone_peripherals)、開機必備已固定佔用的 instance"
            "(boot_provided，不可沿用)、secure/bootloader 保留週邊"
            "(reserved_instances，不可請求)、GPIO 鎖腳數。不確定週邊是否存在或"
            "數量上限時先呼叫。帶 instance 可加查該 instance 每個 signal 的合法"
            "候選腳（instance_detail）——「換腳/讓腳/避開某腳」的需求先查這個： "
            "usable_pins 只剩一支＝無法換腳，直接告知使用者，不要嘗試重解。"),
        "input_schema": {
            "type": "object",
            "properties": {
                "board": {"type": "string"},
                "instance": {"type": "string",
                             "description": "加查此 instance 的 signal 候選腳明細"
                                            "（如 \"I2C2\"）"},
            },
        },
    },
    {
        "name": "emit_plan",
        "description": (
            "把『最近一次 solve_pinmux 求得的 sat 分配』寫成 plan.csv / plan.xlsx。"
            "只有使用者要求匯出/存檔時才呼叫；要先有一次成功的求解。你不需要、也不能"
            "提供分配內容——一律使用伺服器保存的已驗證結果。"),
        "input_schema": {
            "type": "object",
            "properties": {
                "board": {"type": "string"},
                "format": {"type": "string", "enum": ["csv", "xlsx"]},
            },
        },
    },
    {
        "name": "run_validator",
        "description": (
            "把『最近一次 solve_pinmux 求得的 sat 分配』交給官方 STM32CubeMX 驗證"
            "（官方腳位規則；耗時約 1–3 分鐘）。回 pass / fail(conflicts[{pin,"
            "signal,message}]) / error（如 CubeMX 未安裝——如實轉告，不影響其他"
            "功能）。只在使用者要求輸出/存檔前、或明說要「驗證」時呼叫；fail 時讀"
            " conflicts 決定調整重解或回報使用者。你不能提供分配內容——一律驗證"
            "伺服器保存的解。"),
        "input_schema": {
            "type": "object",
            "properties": {"board": {"type": "string"}},
        },
    },
    {
        "name": "lookup_binding",
        "description": (
            "查某周邊 instance 在本板對應的外部 IC 與 Device Tree binding"
            "（KB→快取→st linux v6.6-stm32mp bindings 樹，兩段式、可溯源）。"
            "回 ok（含 ic/compatible/binding_doc/source_url）、no_ic（板上無外部"
            " IC）或 not_found。只在解涉及外部 IC、或使用者問 binding/DTS 細節時"
            "呼叫；答覆只能引用回傳內容，不可自創 compatible 或屬性。"),
        "input_schema": {
            "type": "object",
            "properties": {
                "peripheral": {"type": "string",
                               "description": "instance 名（如 ETH1、USBH、SDMMC2）"},
                "board": {"type": "string"},
            },
            "required": ["peripheral"],
        },
    },
    {
        "name": "propose_suggestion",
        "description": (
            "把一個「修改建議」提交成前端可一鍵採納的卡片。伺服器會用同一條確定性"
            "路徑重解該 intent **驗證**：真的 sat 才收（ok），否則 rejected——"
            "先驗證再提案是機械強制的。無解/量級錯誤時，把每個可行替代方案（例如"
            "減量、換 instance、放棄某項）各提交一張；最多 3 張。summary 用一句"
            "人話寫「這個方案是什麼」；intent 用完整 IntentIR。回覆文字仍要向使用"
            "者解釋為什麼這樣建議。"),
        "input_schema": {
            "type": "object",
            "properties": {
                "summary": {"type": "string",
                            "description": "一句人話，卡片上顯示的方案描述"},
                "intent": _INTENT_SCHEMA,
                "board": {"type": "string"},
            },
            "required": ["summary", "intent"],
        },
    },
]

# 每輪最多收的建議卡片數（多的直接拒收，避免卡片牆）。
MAX_SUGGESTIONS = 3

# validator 修復迴圈的機械停損（M5）：每輪最多跑 3 次 CubeMX 驗證 =
# 1 次初驗 + 2 輪「fail → 調整重解 → 重驗」。到頂後 dispatch 直接拒絕再驗
# （回 blocked），模型只能轉 propose_suggestion / 如實回報衝突——上限是
# 伺服器強制的，不依賴模型自律（每次驗證 1–3 分鐘，失控迴圈成本很高）。
MAX_VALIDATOR_RUNS = 3

# G4（IC binding）停用時要從 system prompt 剔除的區塊標記（文字保留在
# system_prompt.md 內，夾在這對 HTML 註解之間；重新啟用即自動恢復）。
_IC_BEGIN = "<!-- IC_BINDING:BEGIN -->"
_IC_END = "<!-- IC_BINDING:END -->"


def _active_tools() -> list:
    """依功能旗標過濾要註冊給模型的工具（呼叫時讀旗標，測試可 monkeypatch）。"""
    if dataio.IC_BINDING_ENABLED:
        return ANTHROPIC_TOOLS
    return [t for t in ANTHROPIC_TOOLS if t["name"] != "lookup_binding"]


def _strip_disabled_sections(text: str) -> str:
    """把 system prompt 中被停用功能的說明區塊剔除（G4 off 時模型不該看到
    lookup_binding 的用法——prompt 宣稱「唯一可用動作」，不可列它叫不到的工具）。"""
    if dataio.IC_BINDING_ENABLED:
        return text
    while True:
        i = text.find(_IC_BEGIN)
        j = text.find(_IC_END)
        if i < 0 or j < 0:
            return text
        text = text[:i] + text[j + len(_IC_END):]


class ChatReply:
    """What one user turn produces: the assistant's prose + (optionally) the most
    recent SAT plan for the front-end to render/export + verified suggestion
    cards (G6) the user can adopt with one click + the pending clarify question
    (so the front-end renders question.options as CLICKABLE BUTTONS, exactly like
    the deterministic /api/solve flow — the model must not re-type the list)."""

    def __init__(self, text: str, plan: list | None = None,
                 suggestions: list | None = None, clarify: dict | None = None,
                 usage_tokens: int = 0, seconds: float = 0.0):
        self.text = text
        self.plan = plan
        self.suggestions = suggestions or []
        self.clarify = clarify
        self.usage_tokens = usage_tokens      # 本輪 LLM 消耗（全部呼叫合計）
        self.seconds = seconds                # 本輪耗時（秒）


class Orchestrator:
    def __init__(self):
        self.tools = SolverTools()
        self.provider = get_provider(module="orchestrator")  # 讀 llm_modules.ini[orchestrator] 選 provider/model
        with open(_PROMPT_PATH, encoding="utf-8") as fh:
            self._system = fh.read()

    # ----------------------------------------------------------------------- #
    def step(self, session: ChatSession, user_msg: str) -> ChatReply:
        """Advance the conversation by one user turn. Runs the tool-use loop
        until the model produces a final (non-tool) message or hits MAX_STEPS."""
        session.messages.append({"role": "user", "content": user_msg})
        system = self._system + f"\n\n# 目前目標板\nboard = {session.board}（呼叫工具時若未指定 board，預設用這塊）。"

        turn_plan = None        # plan produced THIS turn (so we don't re-render a stale one)
        turn_clarify = None     # 本輪待答的 clarify（前端渲染成按鈕；後續 solve 會覆蓋/清掉）
        session.suggestions = []          # 建議卡片屬於「這一輪」——每輪重置
        session.validator_runs = 0        # 修復迴圈停損計數——每輪重置
        system = _strip_disabled_sections(system)
        t0 = time.time()
        turn_tokens = 0
        LIVE.update(running=True, started_at=t0, tokens=0)
        for _ in range(MAX_STEPS):
            resp = self.provider.complete_raw(
                system=system, messages=session.messages,
                tools=_active_tools(), temperature=0)
            u = resp.usage or {}
            turn_tokens += int(u.get("total_tokens")
                               or (u.get("prompt_tokens", 0)
                                   + u.get("completion_tokens", 0)) or 0)
            LIVE["tokens"] = turn_tokens

            # Append the assistant turn verbatim (so tool_use blocks are replayed).
            # Never append an empty content (the API rejects an empty assistant turn).
            assistant_content = resp.raw_blocks if resp.raw_blocks else resp.content
            if assistant_content:
                session.messages.append(
                    {"role": "assistant", "content": assistant_content})
            session.trace.append({
                "kind": "assistant", "stop_reason": resp.stop_reason,
                "text": resp.content,
                "tool_calls": [{"name": c.name, "input": c.input} for c in resp.tool_calls],
            })

            if resp.stop_reason != "tool_use" or not resp.tool_calls:
                # Final answer (or a degenerate tool_use with no blocks — treat as final
                # rather than send back an empty user turn, which the API rejects).
                LIVE["running"] = False
                return ChatReply(text=resp.content or "（沒有產生回覆）",
                                 plan=turn_plan,
                                 suggestions=session.suggestions,
                                 clarify=turn_clarify,
                                 usage_tokens=turn_tokens,
                                 seconds=round(time.time() - t0, 1))

            # Run each requested tool, feed the results back as tool_result blocks.
            tool_results = []
            for call in resp.tool_calls:
                out = self._dispatch(call.name, call.input or {}, session)
                if call.name == "solve_pinmux":
                    # clarify 問題跟著「最新一次求解」走：後續 solve 解掉了就清掉，
                    # 又冒出新歧義就換成新的（前端只渲染最終那份的按鈕）。
                    turn_clarify = (out.get("question")
                                    if out.get("status") == "clarify" else None)
                if call.name == "solve_pinmux" and out.get("status") == "sat":
                    turn_plan = out.get("assignment")     # render only a fresh plan
                session.trace.append({
                    "kind": "tool", "name": call.name,
                    "input": call.input, "output": out})
                tool_results.append({
                    "type": "tool_result", "tool_use_id": call.id,
                    "content": json.dumps(out, ensure_ascii=False),
                })
            session.messages.append({"role": "user", "content": tool_results})

        LIVE["running"] = False
        return ChatReply(
            text="（已達單輪步數上限，請把需求拆小一點、或換個說法再試。）",
            plan=turn_plan, suggestions=session.suggestions, clarify=turn_clarify,
            usage_tokens=turn_tokens, seconds=round(time.time() - t0, 1))

    # ----------------------------------------------------------------------- #
    def _dispatch(self, name: str, inp: dict, session: ChatSession) -> dict:
        """Execute one tool call. Defaults board to the session's board, and
        records the latest SAT plan so the front-end can render/export it."""
        try:
            if name == "solve_pinmux":
                board = inp.get("board") or session.board
                out = self.tools.solve_pinmux(inp.get("intent") or {}, board)
                if out.get("status") == "sat":
                    session.last_plan = out.get("assignment")
                return out
            if name == "get_capabilities":
                return self.tools.get_capabilities(inp.get("board") or session.board,
                                                   instance=inp.get("instance"))
            if name == "lookup_binding":
                return self.tools.lookup_binding(
                    inp.get("peripheral"), inp.get("board") or session.board)
            if name == "run_validator":
                # 同 emit_plan 的防偽原則：只驗證伺服器保存的已 SAT 解。
                if not session.last_plan:
                    return {"status": "error",
                            "message": "尚無可驗證的分配，請先成功求解一次。"}
                # 修復迴圈停損（M5）：本輪到頂就拒絕，逼迫轉建議/如實回報。
                if session.validator_runs >= MAX_VALIDATOR_RUNS:
                    return {"status": "blocked",
                            "message": (f"本輪 CubeMX 驗證已達上限"
                                        f"（{MAX_VALIDATOR_RUNS} 次 = 初驗 + 2 輪修復）。"
                                        "不要再驗證：把仍存在的衝突如實回報使用者，"
                                        "並用 propose_suggestion 提交驗證過的替代方案。")}
                session.validator_runs += 1
                return self.tools.run_validator(
                    session.last_plan, inp.get("board") or session.board)
            if name == "propose_suggestion":
                if len(session.suggestions) >= MAX_SUGGESTIONS:
                    return {"status": "rejected",
                            "reason": f"本輪建議已達上限（{MAX_SUGGESTIONS} 張）。"}
                out = self.tools.propose_suggestion(
                    inp.get("summary"), inp.get("intent"),
                    inp.get("board") or session.board)
                if out.get("status") == "ok":
                    # 只有通過伺服器重解驗證的方案才成為卡片（intent 原樣保存，
                    # 採納時送回 /api/chat 重跑同一條路徑）。
                    session.suggestions.append(
                        {"summary": out["summary"], "intent": inp.get("intent")})
                return out
            if name == "emit_plan":
                # Anti-corruption: ONLY the server-held, SAT-validated plan may be
                # written to disk. The LLM cannot supply rows (no `assignment`
                # param), so unvalidated signal/pin/af triples can never be
                # persisted as a "plan".
                if not session.last_plan:
                    return {"status": "error",
                            "message": "尚無可匯出的分配，請先成功求解一次。"}
                return self.tools.emit_plan(
                    session.last_plan,
                    inp.get("board") or session.board,
                    inp.get("format") or "csv")
            return {"status": "error", "message": f"未知的工具：{name}"}
        except Exception as exc:                          # never crash the loop
            return {"status": "error", "message": str(exc)}
