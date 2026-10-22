"""控制台输出与结论汇总（统一 UTF-8，Windows 管道 / 重定向下同样稳妥）。"""

from __future__ import annotations

import sys
from typing import Any


def reconfigure(stream: Any) -> None:
    """把单个流切到 UTF-8（流不支持重配置时原样返回）。

    Args:
        stream: 文本流对象（支持 `reconfigure` 时按其重配）。
    """
    if hasattr(stream, "reconfigure"):
        stream.reconfigure(encoding="utf-8", errors="replace")


def configure_stdio() -> None:
    """把标准输出与错误输出切到 UTF-8。"""
    for stream in (sys.stdout, sys.stderr):
        reconfigure(stream)


def out(message: str = "") -> None:
    """打印一行普通输出。

    Args:
        message: 输出文本（空串打印空行）。
    """
    print(message, flush=True)


def section(label: str) -> None:
    """打印步骤标题（与子命令输出保持同一前缀口径）。

    Args:
        label: 步骤标签。
    """
    print(f"\n[engkit] {label}", flush=True)


def note(message: str) -> None:
    """打印缩进说明行。

    Args:
        message: 说明文本。
    """
    print(f"  {message}", flush=True)


def error(message: str) -> None:
    """打印错误行（标准错误）。

    Args:
        message: 错误文本。
    """
    print(message, file=sys.stderr, flush=True)


def finish(title: str, failures: list[str]) -> int:
    """打印结论并给出退出码。

    Args:
        title: 结论标题（如 `preflight`）。
        failures: 失败项清单。

    Returns:
        int: 有失败项返回 1，否则 0。
    """
    print(f"\n================ {title} 结论 ================", flush=True)
    if failures:
        print(f"失败 {len(failures)} 项：", flush=True)
        for item in failures:
            print(f"  - {item}", flush=True)
        return 1
    print("全部通过", flush=True)
    return 0
