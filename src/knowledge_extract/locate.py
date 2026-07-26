from __future__ import annotations

import re

# 各家 pinmux 表的措辭差很多(ST: alternate function AF0-15;TI: MUXMODE;
# Nuvoton: multi-function MFP),用加權關鍵字給每頁打分,再把高分頁串成連續區段。
_PINMUX_PATTERNS: list[tuple[re.Pattern, int]] = [
    (re.compile(r"alternate\s+function", re.I), 2),
    (re.compile(r"\bAF(?:1[0-5]|[0-9])\b"), 1),          # 每頁計一次
    (re.compile(r"\bMUXMODE\b", re.I), 3),
    (re.compile(r"multi[- ]?function", re.I), 2),
    (re.compile(r"\bMFP[LH]?\b"), 1),
    (re.compile(r"pin\s+multiplex", re.I), 2),
    (re.compile(r"\bpinout\b|\bballout\b", re.I), 1),
]

# 「PG.3 / EPWM0_CH5 / UART9_TXD / ...」序列式 pin 功能列表(Nuvoton 風格):
# 靠關鍵字抓不到,改抓每頁的列證據 —— 這類列 >= 4 就視為 pinmux 頁。
_PIN_ROW_RE = re.compile(r"\bP[A-Z]\.?\d{1,2}\s*/")

_BOOT_PATTERNS: list[tuple[re.Pattern, int]] = [
    (re.compile(r"boot\s+(mode|configuration|pin|source|device|select)", re.I), 2),
    (re.compile(r"\bBOOT[0-3]?\b"), 1),
    (re.compile(r"power[- ]?on\s+setting", re.I), 2),    # Nuvoton 的說法
]


def _score(page: str, patterns: list[tuple[re.Pattern, int]]) -> int:
    return sum(w for pat, w in patterns if pat.search(page))


def _runs(indices: list[int], max_gap: int = 1) -> list[tuple[int, int]]:
    """把頁碼列表併成 (start, end) 連續區段(容許 max_gap 的空洞)。"""
    runs: list[tuple[int, int]] = []
    for i in sorted(indices):
        if runs and i - runs[-1][1] <= max_gap + 1:
            runs[-1] = (runs[-1][0], i)
        else:
            runs.append((i, i))
    return runs


def pinmux_page_runs(pages: list[str], min_score: int = 3) -> list[tuple[int, int]]:
    """
    回傳疑似 pinmux/AF 表的頁區段(0-based, 閉區間)。
    另外要求該頁看起來含表格資料(訊號名 token 密度),過濾掉純敘述頁。
    """
    signal_token = re.compile(r"\b[A-Z][A-Z0-9]{1,11}_[A-Z0-9_]{1,20}\b")
    hits = [
        i
        for i, page in enumerate(pages)
        if (
            _score(page, _PINMUX_PATTERNS) >= min_score
            or len(_PIN_ROW_RE.findall(page)) >= 4
        )
        and len(signal_token.findall(page)) >= 20
    ]
    runs = _runs(hits)
    # 表格尾頁常只剩幾列(訊號 token 掉到門檻下)而被漏掉 —— 例如 DS14284 的
    # Table 13 尾頁只有 PZ5-PZ9 五列。對每個區段向後單頁延伸:下一頁仍有
    # pinmux 特徵(分數>=1)且有一定訊號密度就併入。
    extended = []
    for start, end in runs:
        while end + 1 < len(pages) and (end + 1) not in hits:
            nxt = pages[end + 1]
            if _score(nxt, _PINMUX_PATTERNS) >= 1 and len(signal_token.findall(nxt)) >= 8:
                end += 1
            else:
                break
        extended.append((start, end))
    return _runs([i for a, b in extended for i in range(a, b + 1)])


def boot_pages(pages: list[str], min_score: int = 2, limit: int = 12) -> list[int]:
    """疑似 boot 設定章節的頁(0-based),取分數最高的前 limit 頁,依頁序回傳。"""
    scored = [(_score(p, _BOOT_PATTERNS), i) for i, p in enumerate(pages)]
    top = sorted((s, i) for s, i in scored if s >= min_score)[-limit:]
    return sorted(i for _, i in top)
