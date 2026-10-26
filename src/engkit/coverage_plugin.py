"""通用覆盖率采集与重命令拦截的 pytest 入口插件（各仓 `addopts` 加 `-p engkit.coverage_plugin`）。

为什么需要这个模块：覆盖率必须**先于任何被测模块导入**启动，否则「模块导入期执行的行」永久漏计
（表现为覆盖率长期停在某个百分比、怎么补用例都不动）。pytest 处理 `-p` 时先解析 `addopts`，
插件模块的导入早于 `pytest_load_initial_conftests`，因此入口只能放在**顶层插件模块**里。

本模块**顶层只依赖标准库与 coverage**：

- 导入即按仓配置声明的环境变量启动采集（`[coverage].data_env` 给数据文件基址、`source_env` 给采集范围，
  由 `engkit preflight` / `engkit run-selected` 注入；按进程号落盘，`engkit coverage` 合并判定）；
- 未配置采集（环境变量缺省）或定位不到配置时静默不采（不影响正常跑测试）；
- 同时把重命令守卫（`engkit.guard`）挂到 pytest 的 `addoption` / `configure` 两个钩子上，
  各仓无需再写自己的采集或拦截实现。
"""

from __future__ import annotations

import atexit
import contextlib
import os
from pathlib import Path
from typing import Any

from . import guard
from .config import Config
from .coverage import ensure_data_dir

_coverage: Any = None
"""本进程的采集实例（未启用时为 None）。"""


def start() -> Any:
    """按仓配置声明的环境变量启动覆盖率采集。

    Returns:
        Any: coverage 实例；未启用采集、定位不到配置或未安装 coverage 时返回 None。
    """
    cfg = guard.config_for_hook()
    if cfg is None:
        return None
    data_file = os.environ.get(cfg.coverage.data_env, "")
    if not data_file:
        return None
    instance = _new_coverage(cfg, data_file)
    if instance is None:  # pragma: no cover - 被测仓未装覆盖率工具时的退化路径
        return None
    instance.start()
    return instance


def _new_coverage(cfg: Config, data_file: str) -> Any:
    """构造 coverage 实例（行 + 分支、按进程号落盘）。

    Args:
        cfg: 仓配置。
        data_file: 数据文件基址（由 `[coverage].data_env` 指向）。

    Returns:
        Any: `coverage.Coverage` 实例；未安装 coverage 时返回 None。
    """
    try:
        import coverage
    except ModuleNotFoundError:  # pragma: no cover - 被测仓未装覆盖率工具时的退化路径
        return None
    source = tuple(item.strip() for item in os.environ.get(cfg.coverage.source_env, "").split(",") if item.strip())
    ensure_data_dir(Path(data_file))
    # 后缀固定为进程号：xdist worker 各写一份，合并时按文件名唯一（`data_suffix=True` 的随机后缀会撞名）
    return coverage.Coverage(data_file=data_file, data_suffix=f"{os.getpid()}", branch=True, source=source or None)


def stop() -> None:
    """停止并落盘本进程的采集数据（幂等：`pytest_sessionfinish` 与 `atexit` 都会调用）。"""
    global _coverage
    if _coverage is not None:
        _coverage.stop()
        _coverage.save()
        _coverage = None


def pytest_addoption(parser: Any) -> None:
    """注册守卫的 `--full` 选项（须在 collection 前完成注册）。

    Args:
        parser: pytest 的 Parser 对象。
    """
    guard.pytest_addoption(parser)


def pytest_configure(config: Any) -> None:
    """重命令 / 全量守卫：命中拦截条件即抛 `UsageError`，用例零执行。

    Args:
        config: pytest 配置对象。
    """
    guard.pytest_configure(config)


def pytest_sessionfinish(session: Any, exitstatus: int) -> None:
    """测试会话结束：落盘本进程的采集数据（xdist worker 各自落一份）。

    Args:
        session: pytest 会话对象（未使用）。
        exitstatus: 退出码（未使用）。
    """
    with contextlib.suppress(Exception):
        stop()


_coverage = start()
atexit.register(stop)
