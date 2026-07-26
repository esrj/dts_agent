from __future__ import annotations

import hashlib
import json
import os
import subprocess

from .paths import CACHE_DIR


def _sha256(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def extract_pages(pdf_path: str) -> list[str]:
    """
    PDF -> 逐頁純文字(index 0 = 第 1 頁)。

    用 pdftotext -layout 保留表格欄位對齊(pinmux 表靠這個才讀得出欄位),
    以 form-feed 切頁。結果快取於 cache/knowledge_extract/pages/,以 PDF
    的 sha256 判斷是否需要重新轉換 —— 整個 cache/ 目錄可任意刪除。
    """
    stem = os.path.splitext(os.path.basename(pdf_path))[0]
    cache_path = os.path.join(CACHE_DIR, "pages", f"{stem}.json")
    digest = _sha256(pdf_path)

    if os.path.exists(cache_path):
        try:
            with open(cache_path, "r", encoding="utf-8") as f:
                cached = json.load(f)
            if cached.get("sha256") == digest:
                return cached["pages"]
        except (OSError, json.JSONDecodeError, KeyError):
            pass

    result = subprocess.run(
        ["pdftotext", "-layout", "-enc", "UTF-8", pdf_path, "-"],
        capture_output=True,
        check=True,
    )
    pages = result.stdout.decode("utf-8", errors="replace").split("\f")

    os.makedirs(os.path.dirname(cache_path), exist_ok=True)
    with open(cache_path, "w", encoding="utf-8") as f:
        json.dump({"sha256": digest, "pages": pages}, f, ensure_ascii=False)

    return pages
