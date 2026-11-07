"""包版本（唯一声明处）。

独立成模块的原因：`steps_reuse` 需要把包版本计入环境指纹，而 `cli` 导入 `preflight`，
若版本常量留在 `cli` 里，`preflight → steps_reuse → cli` 构成导入环。此处只放一个常量，
无任何运行时依赖。
"""

from __future__ import annotations

VERSION = "0.4.0"
"""包版本。"""
