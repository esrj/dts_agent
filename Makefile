# dts_agent — 常用指令入口
# 只是把 README/CLAUDE.md 裡原本要手打的指令包一層，行為完全不變。

VENV_PY := venv/bin/python

.PHONY: help web solve patch-run patch-locate patch-dry-run

help:
	@echo "可用指令："
	@echo "  make web             啟動 web UI（http://127.0.0.1:5001）"
	@echo "  make solve           solver 快速實驗（src/main.py）"
	@echo "  make patch-run       跑第二段 DTS patch pipeline（讀 output/plan/plan.csv）"
	@echo "  make patch-locate    只定位，不用 LLM（改動後的煙霧測試）"
	@echo "  make patch-dry-run   印 LLM prompt，不呼叫 API"

web:
	$(VENV_PY) src/web/app.py

solve:
	$(VENV_PY) src/main.py

patch-run:
	PYTHONPATH=src $(VENV_PY) -m patch_agent run

patch-locate:
	PYTHONPATH=src $(VENV_PY) -m patch_agent locate

patch-dry-run:
	PYTHONPATH=src $(VENV_PY) -m patch_agent dry-run
