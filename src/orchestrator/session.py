"""
session.py — server-side chat state for the orchestrator.

The legacy /api/solve flow is stateless (the whole intent is round-tripped to the
browser each turn). The agentic tool-use loop can't work that way: its transcript
contains native Anthropic content blocks (tool_use / tool_result) that must be
replayed verbatim on the next turn. So chat state lives on the SERVER, keyed by a
session id the browser stores and sends back.

Kept deliberately small: an in-memory LRU dict is enough for a single-process
Flask app. Swap SessionStore for Redis if you ever run multiple workers.
"""
from __future__ import annotations

import threading
import uuid
from collections import OrderedDict
from dataclasses import dataclass, field


@dataclass
class ChatSession:
    session_id: str
    board: str
    messages: list = field(default_factory=list)   # native Anthropic message dicts
    trace: list = field(default_factory=list)       # decision/tool trace (audit)
    last_plan: list | None = None                   # most recent SAT assignment
    suggestions: list = field(default_factory=list) # 本輪「驗證過」的建議卡片
                                                    # [{summary, intent}]，每輪重置
    validator_runs: int = 0                         # 本輪 run_validator 次數
                                                    # （修復迴圈停損，每輪重置）
    override: dict | None = None                    # 本對話綁定的 pin 覆寫組
                                                    # {set_id, version, data}——
                                                    # 對話「開始」當下的版本；
                                                    # 儲存新覆寫不影響進行中對話
                                                    # （前端開新話題才生效，III.3）


class SessionStore:
    """In-memory, LRU-capped session store (newest = most-recently-used).

    Thread-safe: Flask's dev server (and most WSGI servers) handle each request
    on its own thread, so concurrent /api/chat calls race on the OrderedDict.
    All mutations are guarded by a lock.
    """

    def __init__(self, max_sessions: int = 200):
        self._store: "OrderedDict[str, ChatSession]" = OrderedDict()
        self._max = max_sessions
        self._lock = threading.RLock()

    def get_or_create(self, session_id: str | None, board: str,
                      ns: str = "") -> ChatSession:
        """ns（命名空間）：web 傳入登入 user id——session_id 是 client 自報的，
        不加命名空間任何人都能用別人的 id 接手他的對話（多使用者後是隱私洞）。
        內部儲存 key＝f"{ns}:{sid}"；session_id 本身維持原樣回給 client。"""
        with self._lock:
            key = f"{ns}:{session_id}" if session_id else None
            if key and key in self._store:
                sess = self._store.pop(key)                 # LRU touch
                self._store[key] = sess
                return sess
            sid = session_id or uuid.uuid4().hex            # honour client id if given
            sess = ChatSession(session_id=sid, board=board)
            self._store[f"{ns}:{sid}"] = sess
            while len(self._store) > self._max:             # evict oldest
                self._store.popitem(last=False)
            return sess
