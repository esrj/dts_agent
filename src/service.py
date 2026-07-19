"""
service.py — Web 用的 pipeline 封裝。

把「自然語言 -> intent -> (必要時反問確認) -> SolveSpec -> 求解」的流程打包，
回傳 JSON-friendly 的 dict 給 web/app.py 使用。

反問採無狀態的一問一答（與 main.py 的 CLI loop 同一套邏輯，只是拆到 HTTP 上）：
  start(text, board)            : 解析 + 偵測；若 under-specified 回 {status:"clarify", ...}
  answer(payload, board)        : 把使用者選的候選折回 intent，再續解。
每輪都把 intent 帶回前端，下一輪原封帶回來，所以伺服器不需保存任何 session。
因為無狀態，board 也必須每輪都帶（start 與 answer），且每個回應都回帶 board，
讓前端能在反問來回中維持一致的目標板子。

多板支援：每塊板子的資料（af_table / profiles / 官方 DTS / signal_to_pin /
require.json）放在 data/boards/<board>/，由 _Board 載入並依 board id 快取。每次請求依
前端選的 board 套用對應資料：
  1. 用該板的 af_table.json 查 pin/signal（kb.af / kb.sigma）。
  2. 用該板的 peripheral / DTS 展開 signal（kb.profiles / kb.dts_index）。
  3. 自動把該板 require.json 的 gpio_must_pins 與 reserve_only 保留腳加入
     must_gpio（不可分配）；reserve_only 的 instance（I2C7…）同時被 resolver /
     counts 封鎖，不得用於一般週邊請求、不輸出到 plan。
  4. 自動把該板 require.json 的開機固定腳（emit_fixed_assignment 群組的
     pin_map，過濾到 Σ）注入需求並鎖定官方腳位。

main.py 不依賴本檔；兩者只是各自呼叫同一批底層模組，求解邏輯只有一份。
"""
import copy
import itertools
import json
import os

from util import dataio                      # 模組引用：功能旗標於呼叫時讀取
from util.dataio import (
    DEFAULT_BOARD, af_of, board_paths, load_board_components, load_gpio_pins,
    load_pin_locked, load_require_signals, load_reserved, load_signal_to_pin,
)
from solver.runner import solve_signals
from solver.resolver import ResolveError, load_knowledge, reserved_owner
from solver.counts import plan
from solver.clarify import (
    Clarify, Option, Question, apply_answer, phrase_question, validate_clarify,
)
from llm_provider import get_provider, Message, Role


class _Board:
    """One board's data bundle, loaded once and cached by board id.

    kb            : Knowledge(af, af_map, sigma, profiles, dts_index) for this board
    gpio_pins     : require.json['gpio_must_pins'] -> 必須保持 GPIO 的腳位
    reserved_pins : require.json reserve_only 群組的 pin_map 腳位（secure/bootloader
                    持有，kernel 不可分配）
    reserved_instances : reserve_only 群組名（I2C7、OCTOSPIM_P1…）——instance 級保留，
                    一般週邊請求不得選用（resolver/counts 據此拒絕）
    must_gpio     : gpio_pins ∪ reserved_pins，餵給 solver 的「不可分配」集合
    boot_required : emit_fixed_assignment 群組的開機必要 signals，已過濾到 Σ
    boot_set      : boot_required ∪ pin_locked 的 signals，供標記輸出 row（boot 徽章）
    pin_locked    : {pin: signal}，emit 群組 pin_map 的固定指派 -> must_bind
                    （[signal, pin, af] 為板級常數，直接採用，不再查 signal_to_pin）
    baseline      : signal_to_pin（官方預設版，bootable_default 直接採用；輸出時
                    過濾掉 reserve_only 訊號）
    """

    def __init__(self, board: str):
        paths = board_paths(board)
        self.kb = load_knowledge(
            af_path=paths["af"], profiles_path=paths["profiles"], dts_path=paths["dts"])
        self.af = self.kb.af
        self.gpio_pins = load_gpio_pins(paths["require"], af_keys=set(self.kb.af.keys()))
        self.boot_required = load_require_signals(paths["require"], self.kb.sigma)
        # 開機固定腳（emit 群組 pin_map）-> must_bind（{pin: signal}）
        self.pin_locked = load_pin_locked(paths["require"])
        # secure/bootloader 保留（reserve_only 群組）：pin 移出候選域 + instance 封鎖
        self.reserved_pins, self.reserved_instances = load_reserved(paths["require"])
        self.must_gpio = self.gpio_pins | self.reserved_pins
        self.boot_set = set(self.boot_required) | set(self.pin_locked.values())
        self.baseline = load_signal_to_pin(paths["signals"])
        # 板上外部 IC 對照（G4，optional 檔）：instance -> {ic, compatible, …}
        self.components = load_board_components(paths.get("components"))

    def ic_of(self, signal: str) -> str:
        """該 signal 所屬 instance 的板上外部 IC 型號；無 IC 周邊回空字串。"""
        comp = self.components.get(signal.split("_", 1)[0].upper()) or {}
        return comp.get("ic") or ""


class Pipeline:
    """啟動時只載入與板子無關的部分（provider / system_prompt）；各板資料於首次用到時
    載入並依 board id 快取（無 hot-reload：改板子資料後需重啟）。"""

    def __init__(self):
        system_prompt_path = os.path.join(
            os.path.dirname(os.path.abspath(__file__)),
            "llm_provider", "system_prompt.md",
        )
        self.system_prompt = open(system_prompt_path, encoding="utf-8").read()
        self.provider = get_provider(module="parse")   # 讀 llm_modules.ini[parse] 選 provider/model
        self._cache: dict[str, _Board] = {}

    # ----- board cache ---------------------------------------------------- #
    def _load_board(self, board: str | None) -> _Board:
        board = board or DEFAULT_BOARD
        if board not in self._cache:
            self._cache[board] = _Board(board)
        return self._cache[board]

    # ----- 公開入口 ------------------------------------------------------- #
    def start(self, user_text: str, board: str | None = None) -> dict:
        """第一輪：自然語言 -> intent -> 偵測/求解（套用所選板子的資料）。"""
        board = board or DEFAULT_BOARD
        b = self._load_board(board)
        resp = self.provider.complete(
            [
                Message(role=Role.SYSTEM, content=self.system_prompt),
                Message(role=Role.USER, content=user_text),
            ],
            json_mode=True,
            temperature=0,
        )
        intent = json.loads(resp.content)
        print(self.provider.name)
        print("LLM parse result:")
        print(json.dumps(intent, indent=2, ensure_ascii=False))
        return self._continue(intent, b, board)

    def answer(self, payload: dict, board: str | None = None) -> dict:
        """後續輪：把使用者選的候選折回 intent，再續解。"""
        board = board or DEFAULT_BOARD
        b = self._load_board(board)
        intent = payload.get("intent") or {}
        qd = payload.get("question") or {}
        od = payload.get("option") or {}
        question = Question(
            kind=qd.get("kind", ""), span="", summary="",
            item_index=qd.get("item_index", -1), pin=qd.get("pin"))
        option = Option(
            label=od.get("label", ""), pin=od.get("pin"),
            instance=od.get("instance"), signal=od.get("signal"),
            af=od.get("af"), mode=od.get("mode"), note=od.get("note", ""),
            bindings=od.get("bindings") or [])
        return self._continue(apply_answer(intent, question, option), b, board)

    # ----- 內部：注入開機需求 + 偵測 or 求解 ------------------------------ #
    def _inject_boot(self, intent: dict, boot_required: list, pin_locked: dict,
                     *, relax: set | None = None) -> dict:
        """把該板的開機必要 signals 以 signal-level item 形式 *前置* 注入 intent。

        必須在 clarify / plan 之前注入，這些 signal 才會進入 resolver 的 base.required，
        被 counts.py 的 DFS 看見（否則 count 選 instance 可能撞掉開機腳位、最後 UNSAT）。
        兩類來源都來自板級資料（換板即換一套），與 LLM 輸入無關：
          1. pin_locked {pin: signal} — 實體已繞線的介面（eMMC/SD/NOR…）。預設以『帶 pin
             的 signal item』注入 → resolver 轉成 must_bind，把腳位釘死且從其他訊號的
             候選域移除（solver 證明上搶不走）。
          2. boot_required — 其餘開機必要 signals（如 USART2 debug console），required-only
             注入：必須被排進某個合法腳位但不釘死。

        relax：要『放走走線』的 pin_locked instance 集合（如 {"USART2"}；僅含 emit
        群組——reserve_only 群組不注入、無從放寬）。在這集合裡的介面改成 required-only
        注入（pin=None）——signal 仍保證被排進去，但腳位不再釘死、可被搬到別腳。供
        _solve 在硬鎖 UNSAT 時逐步釋放最少介面用（固定板：被搬動者即代表需實體改線）。

        冪等：每次先移除自己上輪注入的 item（以 source 標記辨識），再依當前 relax 重注入；
        所以無狀態來回、以及 lock/relax 多次嘗試之間切換都乾淨可重入。
        """
        if not boot_required and not pin_locked:
            return intent
        relax = relax or set()
        intent = copy.deepcopy(intent)
        # 先清掉自己上輪注入的（只清自己加的，使用者/LLM 的 item 不動），再重注入。
        kept = [it for it in (intent.get("items") or [])
                if it.get("source") not in ("board_pin_locked", "boot_required")]
        present = {
            (it.get("signal") or "").upper()
            for it in kept
            if (it.get("level") or "").lower() == "signal" and it.get("signal")
        }
        injected = []
        # 1) 板級鎖腳：未被 relax 者帶 pin（-> must_bind 硬保留）；被 relax 者 required-only。
        for pin, sig in (pin_locked or {}).items():
            if sig.upper() in present:
                continue
            present.add(sig.upper())
            freed = sig.split("_", 1)[0] in relax
            injected.append({
                "level": "signal", "family": sig.split("_", 1)[0],
                "signal": sig,
                "pin": None if freed else pin,
                "af": None,
                "pin_mode": None if freed else "required",
                "source": "board_pin_locked",
            })
        # 2) 其餘開機必要 signals：required-only（會被排到某個合法腳，但不釘死）
        for sig in boot_required:
            if sig.upper() in present:
                continue
            present.add(sig.upper())
            injected.append({
                "level": "signal", "family": sig.split("_", 1)[0],
                "signal": sig, "pin": None, "af": None, "pin_mode": None,
                "source": "boot_required",
            })
        intent["items"] = injected + kept       # 前置，當成已指定的固定 prefix
        return intent

    def _inject_official(self, intent: dict, b: _Board) -> dict:
        """bootable_default 與額外需求並存（「官方 plan 再加一組 SPI」）時，把官方
        預設週邊以「已綁官方腳的 signal item」*前置* 注入 intent。

        來源是該板 signal_to_pin.json（官方 DTS 解析），逐 signal 帶 pin、
        pin_mode="required" -> resolver 轉成 must_bind：官方腳被釘死、並從其他
        訊號的候選域移除，新需求天生避開官方腳。counts 的 `used` 集合會把注入的
        instance 排除在 count domain 外，所以「再多一組 X」一定配新 instance，
        不會吃掉官方那組。

        跳過三類：(1) boot 範圍（boot_set）——那是 _inject_boot 的職責（含
        relax 機制，不可越權釘死）；(2) reserve_only（secure/bootloader 持有）；
        (3) 使用者點名的 instance（peripheral/signal item）——以使用者要求為準，
        該 instance 的官方列不注入，避免與使用者的腳位指定互相矛盾。

        冪等：先清掉自己上輪注入的（source 標記），再重注入——clarify 來回、
        answer() 折返都乾淨可重入。"""
        intent = copy.deepcopy(intent)
        kept = [it for it in (intent.get("items") or [])
                if it.get("source") != "official_default"]
        claimed = set()                      # 使用者點名的 instance token
        for it in kept:
            lvl = (it.get("level") or "").lower()
            if lvl == "peripheral" and it.get("instance"):
                claimed.add(str(it["instance"]).upper())
            elif lvl == "signal" and it.get("signal"):
                claimed.add(str(it["signal"]).upper().split("_", 1)[0])
        injected = []
        for sig, pin in b.baseline.items():
            if (sig in b.boot_set or sig not in b.kb.sigma
                    or reserved_owner(sig, b.reserved_instances) is not None
                    or sig.split("_", 1)[0].upper() in claimed):
                continue
            injected.append({
                "level": "signal", "family": sig.split("_", 1)[0],
                "signal": sig, "pin": pin, "af": None,
                "pin_mode": "required", "source": "official_default",
            })
        intent["items"] = injected + kept
        return intent

    def _continue(self, intent: dict, b: _Board, board: str) -> dict:
        """有歧義就回一題待確認；否則注入開機需求後求解並回最終結果。"""
        # 無具體需求 / 要可開機 / 要預設版 -> 直接給官方 baseline，不注入、不反問、不求解。
        # 官方預設「加上」額外需求（items 非空）-> 注入官方列後照常求解（官方腳鎖定）。
        if intent.get("bootable_default"):
            if not (intent.get("items") or []):
                return self._baseline_result(intent, b, board)
            intent = self._inject_official(intent, b)
        # 全鎖版：先以此偵測歧義（鎖腳的是已指定 signal+pin，不影響 clarify 判斷）。
        injected = self._inject_boot(intent, b.boot_required, b.pin_locked)
        res = validate_clarify(injected, b.kb, must_gpio=b.must_gpio,
                               reserved_instances=b.reserved_instances)
        if isinstance(res, Clarify) and res.questions:
            q = res.questions[0]                       # 一次只問一題（折回最穩，無 index 漂移）
            return {
                "status": "clarify",
                "board": board,                        # 回帶 board，反問來回維持同一塊板
                "intent": injected,                    # 原封帶回，下一輪 answer() 再帶進來
                "remaining": len(res.questions),
                "question": {
                    "kind": q.kind,
                    "summary": q.summary,
                    "pin": q.pin,
                    "item_index": q.item_index,
                    "prompt": phrase_question(q, self.provider),   # LLM 用人話問
                    "options": [self._ser_option(o) for o in q.options],
                },
            }
        return self._solve_optional(intent, b, board)

    # ----- 條件式需求：A + optional B ---------------------------------------- #
    @staticmethod
    def _item_label(it: dict) -> str:
        """一個 item 的人話短標籤（回報 optional 取捨用）。"""
        lvl = (it.get("level") or "").lower()
        if lvl == "count":
            return f"{(it.get('family') or '?').upper()}×{it.get('count', 1)}"
        if lvl == "peripheral":
            return (it.get("instance") or it.get("family") or "?").upper()
        if lvl == "signal":
            return (it.get("signal") or "?").upper()
        return str(it)

    @staticmethod
    def _strip_optional(intent: dict, *, drop: bool) -> dict:
        """拆解 optional 語意——送進反腐層前的最後一站（resolver 不認識 optional）。

        drop=False：保留全部 items；drop=True：移除 optional items（降級版）。
        兩種輸出的 items 一律不帶 optional 鍵；原 intent 不被改動（deepcopy）。
        """
        intent = copy.deepcopy(intent)
        kept = []
        for it in intent.get("items") or []:
            if it.pop("optional", False) and drop:
                continue
            kept.append(it)
        intent["items"] = kept
        return intent

    def _solve_optional(self, intent: dict, b: _Board, board: str) -> dict:
        """「我要 A，可以的話還要 B」：先解「必要 + 全部 optional」；解不了（UNSAT
        或 optional 引起的 ResolveError）就自動降級——移除全部 optional items 重試
        一次，不反問。哪些 optional 被放棄、為什麼，以欄位與 note 回報：
          optional_included：SAT 且全數納入時的 optional 標籤
          optional_dropped / optional_reason：降級後（或雙雙失敗時）的取捨與理由
        必要項本身不合法（ResolveError）時照舊向上拋——降級救不了、也不該吞掉。"""
        opt_labels = [self._item_label(it) for it in (intent.get("items") or [])
                      if it.get("optional")]
        if not opt_labels:
            return self._solve(self._strip_optional(intent, drop=False), b, board)

        try:
            out = self._solve(self._strip_optional(intent, drop=False), b, board)
            if out["sat"]:
                out["optional_included"] = opt_labels
                out["spec"]["notes"].append(
                    "可選項目已全數納入：" + "、".join(opt_labels))
                return out
            fail_reason = out.get("reason") or "無法佈線"
        except ResolveError as exc:
            fail_reason = str(exc)

        out = self._solve(self._strip_optional(intent, drop=True), b, board)
        out["optional_dropped"] = opt_labels
        out["optional_reason"] = fail_reason
        if out["sat"]:
            out["spec"]["notes"].insert(0,
                f"⚠ 可選項目（{'、'.join(opt_labels)}）無法與其餘需求同時佈線，"
                f"已自動略過：{fail_reason}")
        return out

    @staticmethod
    def _locked_instances(pin_locked: dict) -> list[str]:
        """pin_locked 的 instance 名集合（如 ['SDMMC1','SDMMC2','USART2']），排序。
        以 instance 為釋放單位——一個介面的所有 signal 一起鎖／一起放（= 一次改線）。"""
        return sorted({sig.split("_", 1)[0] for sig in (pin_locked or {}).values()})

    @staticmethod
    def _moved_boot_pins(assignment: list, b: _Board, relax: set) -> list[dict]:
        """relax 掉的開機介面中，實際被排到『非官方腳』的 signal（= 真的需要改線者）。"""
        official = {sig: pin for pin, sig in b.pin_locked.items()}   # signal -> 官方腳
        moved = []
        for a in assignment:
            sig = a["signal"]
            if (sig.split("_", 1)[0] in relax and sig in official
                    and a["pin"] != official[sig]):
                moved.append({"peripheral": sig.split("_", 1)[0], "signal": sig,
                              "from": official[sig], "to": a["pin"]})
        return moved

    def _solve(self, intent: dict, b: _Board, board: str) -> dict:
        """固定板策略：開機走線（boot_pin_locked）預設硬鎖（must_bind）以保證對得上實體板。
        只有在硬鎖下無法佈線時，才『釋放最少數量』的開機介面（required-only，可被搬腳），
        並把實際被搬離官方腳的介面標出來（boot_relaxed/boot_moved）供前端提示需改線。
        逐步釋放：先試全鎖，再試放 1 個、2 個…的所有組合，第一個可解的（介面數最少）勝出。

        前提（重要）：此 fallback 只有在『開機訊號具 ≥2 個候選腳』時才可能改變結果。單一
        候選腳的訊號（如 stm32mp257f-ev1 的 SDMMC1/SDMMC2 群組）must_bind 與
        required-only 對求解完全等價（AC propagation 自會把單腳 required 訊號收斂到那
        唯一腳並從他人候選域移除），硬鎖下的 UNSAT 是真實硬體衝突、放寬也救不了；多候選
        腳的訊號（如 USART2 console，pin_map 取官方展開）才可能真的被搬腳並回報
        boot_moved。reserve_only 群組不注入求解，不參與此機制。"""
        insts = self._locked_instances(b.pin_locked)
        candidates = [set()]                                   # [0] = 全鎖（物理正確）
        for size in range(1, len(insts) + 1):
            candidates += [set(c) for c in itertools.combinations(insts, size)]

        base_fail = None        # 全鎖版的 UNSAT 結果（最貼近使用者實際走線，用於回報）
        base_exc = None         # 全鎖版的 ResolveError（如未知週邊；放寬也救不了）
        for idx, relax in enumerate(candidates):
            try:
                out = self._solve_once(
                    self._inject_boot(intent, b.boot_required, b.pin_locked, relax=relax),
                    b, board)
            except ResolveError as exc:
                if idx == 0:
                    base_exc = exc
                continue
            if out["sat"]:
                if relax:                                      # 動到了某些開機介面才提示
                    moved = self._moved_boot_pins(out["assignment"], b, relax)
                    if moved:
                        names = "、".join(sorted({m["peripheral"] for m in moved}))
                        out["boot_relaxed"] = True
                        out["boot_moved"] = moved
                        out["spec"]["notes"].insert(0,
                            f"⚠ 在不動走線下無法佈線；此配置已將開機介面 {names} "
                            f"排到非官方腳，需實體改線才能套用。")
                return out
            if idx == 0:
                base_fail = out
        # 連『放走所有開機介面』都無解 -> 回報全鎖版的失敗（最貼近實際走線）。
        if base_fail is not None:
            return base_fail
        raise base_exc

    def _solve_once(self, intent: dict, b: _Board, board: str) -> dict:
        cp = plan(intent, b.kb, must_gpio=b.must_gpio,
                  reserved_instances=b.reserved_instances)
        spec = cp.spec
        boot_in_spec = [s for s in spec.required if s in b.boot_set]
        if boot_in_spec:
            spec.notes.append("自動保留開機必要 signals（require.json）：" + ", ".join(boot_in_spec))
        # 官方預設注入列（bootable_default + 額外需求）：官方腳鎖定、標記輸出 row
        off_sigs = {(it.get("signal") or "").upper()
                    for it in (intent.get("items") or [])
                    if it.get("source") == "official_default"}
        if off_sigs:
            insts = sorted({s.split("_", 1)[0] for s in off_sigs})
            spec.notes.append("已保留官方預設週邊（官方腳鎖定）：" + ", ".join(insts))
        required, result = solve_signals(
            b.af, spec.required, spec.must_gpio, spec.must_bind, af_map=b.kb.af_map)
        return {
            "status": "done",
            "board": board,
            "request_type": intent.get("request_type"),
            "chosen": cp.chosen,
            "spec": {
                "required": spec.required,
                "must_bind": spec.must_bind,
                "notes": spec.notes,
            },
            "sat": result.sat,
            "assignment": [
                {
                    "peripheral": sig.split("_", 1)[0],
                    "signal": sig,
                    "pin": result.assignment[sig],
                    "af": af_of(b.kb.af_map, result.assignment[sig], sig),
                    "boot_reserved": sig in b.boot_set,
                    "official_default": sig in off_sigs,
                    # G4 停用時不帶 ic 鍵：表格/匯出的 ic 欄都是資料驅動地消失
                    **({"ic": b.ic_of(sig)} if dataio.IC_BINDING_ENABLED else {}),
                }
                for sig in required
            ] if result.sat else [],
            "reason": "" if result.sat else result.reason,
            "stats": {
                "propagated": result.propagated,
                "k": result.k,
                "ms": round(result.elapsed * 1e3, 2),
            },
        }

    def _baseline_result(self, intent: dict, b: _Board, board: str) -> dict:
        """官方預設版：直接把該板的 signal_to_pin 當最終答案回傳。
        訊號已綁好腳位，不經 clarify / plan / solver，結構與 _solve 一致，
        前端零改動即可 render / 匯出（多帶 source=baseline 供 UI 標示）。
        reserve_only 週邊（secure/bootloader 持有）不屬於 kernel plan，即使
        出現在官方對照表也一律濾除。"""
        assignment = [
            {"peripheral": sig.split("_", 1)[0], "signal": sig, "pin": pin,
             "af": af_of(b.kb.af_map, pin, sig), "boot_reserved": sig in b.boot_set,
             "official_default": sig not in b.boot_set,
             **({"ic": b.ic_of(sig)} if dataio.IC_BINDING_ENABLED else {})}
            for sig, pin in b.baseline.items()
            if reserved_owner(sig, b.reserved_instances) is None
        ]
        notes = ["官方預設版本，直接採用該板 signal_to_pin.json，未經求解"]
        if b.reserved_instances:
            notes.append("reserve_only 週邊（" + ", ".join(sorted(b.reserved_instances))
                         + "）由 secure/bootloader 持有，不列入 kernel plan")
        return {
            "status": "done",
            "board": board,
            "request_type": intent.get("request_type"),
            "source": "baseline",
            "chosen": [],
            "spec": {
                "required": [a["signal"] for a in assignment],
                "must_bind": {},
                "notes": notes,
            },
            "sat": True,
            "assignment": assignment,
            "reason": "",
            "stats": {"propagated": 0, "k": 0, "ms": 0.0},
        }

    @staticmethod
    def _ser_option(o: Option) -> dict:
        return {
            "label": o.label, "pin": o.pin, "instance": o.instance,
            "signal": o.signal, "af": o.af, "mode": o.mode, "note": o.note,
            "bindings": o.bindings,
        }
