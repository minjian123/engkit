"""engkit：跨仓工程化工具包（文档链接与口径护栏、预检与重命令包装、用例登记）。

**顶层只暴露版本号，且按需加载**：`engkit` 是被覆盖率采集的包，任何在采集启动前执行的
导入都会让「模块导入期行」永久漏计（历史实测：`addopts` 加载采集插件时连带导入 `cli` 全链，
自举覆盖率从 100% 跌到 91.71%）。故此处不 `from .cli import VERSION`，改为模块级 `__getattr__`
惰性取用；调用方写法不变（`from engkit import VERSION` 仍可用）。
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # 仅类型检查期可见；运行时由 `__getattr__` 惰性取用（避免导入期拉入 cli）
    from .cli import VERSION

__all__ = ["VERSION"]


def __getattr__(name: str) -> Any:
    """按需解析模块属性（目前只有 `VERSION`）。

    Args:
        name: 属性名。

    Returns:
        Any: 属性值。

    Raises:
        AttributeError: 未知属性。
    """
    if name == "VERSION":
        from .cli import VERSION

        return VERSION
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
