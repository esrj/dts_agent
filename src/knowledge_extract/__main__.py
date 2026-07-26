from __future__ import annotations

import argparse
import json
import os
import sys

# Allow running directly: `python src/knowledge_extract` (put <root>/src on path)
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from knowledge_extract import dts_extract, pipeline  # noqa: E402
from knowledge_extract.paths import CACHE_DIR, DATA_DIR, INPUT_DIR  # noqa: E402

_ALL_STEPS = {"af", "profiles", "require", "dts"}


class _LazyProvider:
    """第一次真的要呼叫 LLM 時才建立 provider——純 dts 步驟不需要 API key。"""

    def __init__(self, module: str):
        self._module = module
        self._provider = None

    def _real(self):
        if self._provider is None:
            from llm_provider import get_provider

            self._provider = get_provider(module=self._module, allow_mock=False)
            print(f"LLM: provider={self._provider.name} "
                  f"model={getattr(self._provider, 'model_name', '?')} "
                  f"(模組 [{self._module}])")
        return self._provider

    def __getattr__(self, item):
        return getattr(self._real(), item)


def _parse_pages(value: str) -> tuple[int, int]:
    try:
        lo, hi = value.split("-", 1)
        lo_i, hi_i = int(lo), int(hi)
        if lo_i < 1 or hi_i < lo_i:
            raise ValueError
        return lo_i, hi_i
    except ValueError:
        raise argparse.ArgumentTypeError("格式須為 START-END(1-based 頁碼)")


def main() -> int:
    parser = argparse.ArgumentParser(
        prog="knowledge_extract",
        description=(
            "input/ 下的 手冊 PDF + kernel DTS(一次一塊板)→ data/<board>/ "
            "知識庫:base(LLM 手冊抽取)+ dts/baseline(純程式 DTS 解析)。"
            "已存在的輸出檔一律跳過,--force 才重生。"
        ),
    )
    parser.add_argument(
        "--manual", action="append",
        help="只處理指定 PDF 檔名(可重複;預設 input/ 下全部)",
    )
    parser.add_argument(
        "--board",
        help="板名 slug:有手冊時覆寫辨識結果;無手冊、只跑 dts 步驟時必填",
    )
    parser.add_argument(
        "--steps", default="af,profiles,require,dts",
        help="要跑的步驟,逗號分隔:af(含衍生 all_peripheral)/profiles/require/dts",
    )
    parser.add_argument(
        "--pages", type=_parse_pages,
        help="手動指定 pinmux 表頁碼範圍 START-END(跳過自動定位;僅單一手冊時可用)",
    )
    parser.add_argument("--force", action="store_true", help="覆寫既有輸出檔")
    parser.add_argument(
        "--out", help="輸出根目錄(預設 output/staging/——extractor 只寫 staging,落地另行)"
    )
    parser.add_argument(
        "--module", default="knowledge_extract",
        help="llm_modules.ini 的模組區段名(預設 knowledge_extract)",
    )
    args = parser.parse_args()

    steps = {s.strip() for s in args.steps.split(",") if s.strip()}
    unknown = steps - _ALL_STEPS
    if unknown:
        parser.error(f"未知步驟: {sorted(unknown)}")

    manuals = pipeline.list_manuals()
    if args.manual:
        missing = [m for m in args.manual if m not in manuals]
        if missing:
            parser.error(f"input/ 下找不到: {missing}")
        manuals = args.manual
    if (args.board or args.pages) and len(manuals) > 1:
        parser.error("--board / --pages 只能搭配單一手冊使用")

    # 無手冊:只可能跑 dts 步驟(板名必須指定,af_table 需已存在)
    if not manuals:
        if "dts" not in steps or not pipeline.has_dts_input():
            print(f"input/ 下沒有手冊 PDF 或 DTS,無事可做。({INPUT_DIR})")
            return 0
        if not args.board:
            parser.error("input/ 只有 DTS 沒有手冊時,需以 --board 指定板名")
        try:
            report = dts_extract.run_board(
                args.board, out_dir=args.out, force=args.force)
        except RuntimeError as exc:
            print(f"[{args.board}] 失敗:{exc}", file=sys.stderr)
            return 1
        return 1 if report.get("lint", {}).get("fails") else 0

    provider = _LazyProvider(args.module)

    # 單一執行鎖:兩個抽取行程同時跑會互搶輸出檔、加倍燒 API 額度
    lock_path = os.path.join(CACHE_DIR, ".extract.lock")
    os.makedirs(CACHE_DIR, exist_ok=True)
    try:
        fd = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        os.write(fd, str(os.getpid()).encode())
        os.close(fd)
    except FileExistsError:
        try:
            other = int(open(lock_path).read().strip() or 0)
            os.kill(other, 0)                    # 只探測,不送訊號
            print(f"另一個抽取行程 (pid={other}) 正在執行,請等它結束再跑。",
                  file=sys.stderr)
            return 1
        except (ProcessLookupError, ValueError, PermissionError):
            with open(lock_path, "w") as f:      # 殘留鎖:接手
                f.write(str(os.getpid()))

    reports = []
    failed = False
    try:
        for pdf_name in manuals:
            try:
                report = pipeline.run_manual(
                    pdf_name,
                    provider,
                    force=args.force,
                    steps=steps,
                    out_dir=args.out,
                    pages_override=args.pages,
                    board_override=args.board,
                )
            except Exception as exc:  # 單本失敗不擋其他手冊
                failed = True
                report = {"manual": pdf_name, "error": str(exc)}
                print(f"[{pdf_name}] 失敗: {exc}", file=sys.stderr)
            if report.get("lint_failed"):
                failed = True     # lint FAIL = 不出貨
            reports.append(report)
    finally:
        try:
            os.unlink(lock_path)
        except FileNotFoundError:
            pass

    print("\n=== 執行摘要 ===")
    print(json.dumps(reports, ensure_ascii=False, indent=1))
    out_root = args.out or DATA_DIR       # 預設 staging（D3）
    print(f"(輸出:{out_root})")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
