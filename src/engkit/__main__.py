"""`python -m engkit` 入口。"""

from __future__ import annotations

from .cli import main

if __name__ == "__main__":  # pragma: no cover - 模块直接执行
    raise SystemExit(main())
