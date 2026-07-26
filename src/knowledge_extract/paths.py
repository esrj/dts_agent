from __future__ import annotations

import os

# this file: <root>/src/knowledge_extract/paths.py -> root is 3 levels up
ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

INPUT_DIR = os.path.join(ROOT, "input")     # CLI 待處理:一塊板的 手冊 PDF + DTS
CACHE_DIR = os.path.join(ROOT, "cache", "knowledge_extract")
BOARDS_INI = os.path.join(ROOT, "boards.ini")

# 輸出紅線(EXTRACTOR_MERGE_PLAN D3,紅線 5 延伸):extractor **只寫 staging**,
# 永不直接寫正式知識庫 data/<board>/——落地(staging → data/)一律由 web 的
# REVIEW 確認後原子搬移、或人工執行。
STAGING_DIR = os.path.join(ROOT, "output", "staging")
DATA_DIR = STAGING_DIR                      # 模組內 out_root 預設(相容別名)

# 正式知識庫(**唯讀** fallback):staging 沒有 base 檔時,run_board/lint/
# enrich 回這裡讀既有 af_table 等——讀可以,寫不行。
LIVE_DATA_DIR = os.path.join(ROOT, "data")


def base_dir(board: str, out_dir: str | None = None) -> str:
    return os.path.join(out_dir or DATA_DIR, board, "base")
