import json
import os
import sys

from util.dataio import (
    AF_TABLE, SIGNALS, board_paths, load_af, load_gpio_pins, load_signal_to_pin,
)
from solver.runner import solve_signals, solve_peripherals
from solver.resolver import load_knowledge, ResolveError
from solver.counts import plan
from solver.clarify import validate_clarify, Clarify, Ready, apply_answer, phrase_question
from llm_provider import get_provider, Message, Role






if __name__ == "__main__":

    # =======================================================================
    # 單純測試 solver（signal 級：solve_signals；peripheral 級：solve_peripherals。
    # 領域知識一律來自 data/<board>/ 的知識庫檔案，不在程式碼裡列腳位/訊號。）
    # =======================================================================

    # af = load_af(AF_TABLE)
    # MUST_GPIO = load_gpio_pins(board_paths()["require"])

    # ==================== peripheral ====================
    # kb = load_knowledge()
    # intent = {
    #     "items": [
    #         {
    #             "level": "count",
    #             "family": "eth",
    #             "count": 1,
    #             "mode": None                
    #         },
    #         {
    #             "level": "count",
    #             "family": "uart",
    #             "count": 2,
    #             "mode": None                
    #         }
    #     ]
    # }
    # cp = plan(intent, None, must_gpio=MUST_GPIO)
    # solve_signals(af, cp.spec.required, cp.spec.must_gpio, cp.spec.must_bind)







    # =======================================================================
    # 將 llm 輸入透過 resolver 轉成 Solver 的輸入格式
    # =======================================================================

    MUST_GPIO = load_gpio_pins(board_paths()["require"])   # gpio_must_pins from board data

    SYSTEM_PROMPT = os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "llm_provider", "system_prompt.md"
    )
    provider = get_provider(module="parse")            # 讀 llm_modules.ini[parse] 選 provider/model
    system_prompt = open(SYSTEM_PROMPT, encoding="utf-8").read()
    kb = load_knowledge()                              # af_table / profiles / 官方 DTS
    
    inputs = "我要啟用 ETH 兩個跟 I2C 一個"   


    print("\n" + "=" * 100)
    print("使用者輸入:", inputs)
    print("\n" + "=" * 100)

    # step1 自然語言 
    try:
        resp = provider.complete(
            [Message(role=Role.SYSTEM, content=system_prompt),Message(role=Role.USER, content=inputs)],
            json_mode=True, 
            temperature=0,
        )
        
        intent = json.loads(resp.content)
    except Exception as exc:
        print("  LLM 解析失敗:", exc)

    print("\n" + "=" * 100)
    print(f"\n LLM response")
    print("\n" + "=" * 100)
    print(json.dumps(intent, ensure_ascii=False, indent=2))


    # # 2 轉成 Solver 可吃的格式（count 需求由 plan() 自動選 instance）
    # try:
    #     cp = plan(intent, kb, must_gpio=MUST_GPIO)
    #     spec = cp.spec
    #     if cp.chosen:
    #         print("\n--- count 自動選擇 ---")
    #         for c in cp.chosen:
    #             print(f"  {c['family']}×{c['count']} -> {c['instances']}")
    #     print("\n--- 轉換後（solver 輸入格式）---")
    #     print("  required_signals:", spec.required)
    #     print("  must_bind       :", spec.must_bind)
    #     print("  must_gpio       :", sorted(spec.must_gpio))
    #     if spec.notes:
    #         print("  notes           :", spec.notes)

    #     # 3 Solver 求解
    #     af = load_af(AF_TABLE)
    #     required, result = solve_signals(
    #         af, spec.required, spec.must_gpio, spec.must_bind
    #     )

    #     print("\n--- Solver 結果 ---")
    #     if result.sat:
    #         for sig in required:
    #             print(f"  {sig:<24} -> {result.assignment[sig]}")
    #         print(f"  (AC-propagated={result.propagated}, backtrack k={result.k}, "
    #               f"{result.elapsed * 1e3:.2f} ms)")
    #     else:
    #         print("  UNSAT:", result.reason)

    # except ResolveError as exc:
    #     print("\n--- 解析失敗 ---\n", exc)

