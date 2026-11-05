"""通用覆盖率采集与重命令拦截的 pytest 入口插件（各仓 `addopts` 加 `-p engkit.coverage_plugin`）。

为什么需要这个模块：覆盖率必须**先于任何被测模块导入**启动，否则「模块导入期执行的行」永久漏计
（表现为覆盖率长期停在某个百分比、怎么补用例都不动）。pytest 处理 `-p` 时先解析 `addopts`，
插件模块的导入早于 `pytest_load_initial_conftests`，因此入口只能放在**顶层插件模块**里。

**本模块顶层只依赖标准库**（连 `engkit` 自己的子模块都不导入）：

- 采集启动只需要配置里的两个**环境变量名**（`[coverage].data_env` / `source_env`），故此处用
  `tomllib` 直读 `engkit.toml` 的该段，不走 `engkit.config` 的完整解析——完整校验仍由 `Config`
  负责，这里只做「够启动采集」的最小读取；
- 导入即按环境变量启动采集（按进程号落盘，`engkit coverage` 合并判定）；未配置采集（环境变量
  缺省）或定位不到配置时静默不采（不影响正常跑测试）；
- `guard` / `engkit.coverage` 等内部模块一律**延迟导入**（在 `pytest_configure` 等钩子里），
  避免把被测模块拉到采集启动之前——`engkit` 自举时插件属于被测包，提前导入会直接污染自身覆盖率
  （2026-11-05 #1597 实测：`addopts` 加载插件后自举覆盖率从 100% 跌到 91.71%）。
"""

from __future__ import annotations

import atexit
import contextlib
import os
import tomllib
from pathlib import Path
from typing import Any

_coverage: Any = None
"""本进程的采集实例（未启用时为 None）。"""

_CONFIG_ENV = "ENGKIT_CONFIG"
"""显式配置路径环境变量（与 `engkit.guard._CONFIG_ENV` 同键；此处不导入 guard 以免污染采集）。"""

_DEFAULT_DATA_ENV = "ENGKIT_COVERAGE_DATA"
"""`[coverage].data_env` 的缺省键名（与 `engkit.config.CoverageConfig` 同值）。"""

_DEFAULT_SOURCE_ENV = "ENGKIT_COVERAGE_SOURCE"
"""`[coverage].source_env` 的缺省键名（与 `engkit.config.CoverageConfig` 同值）。"""


def _config_path() -> Path | None:
    """定位配置文件：显式路径优先，其次自当前目录上溯。

    Returns:
        Path | None: `engkit.toml` 路径；定位不到返回 None。
    """
    override = os.environ.get(_CONFIG_ENV, "")
    if override:
        path = Path(override)
        return path if path.is_file() else None
    for directory in (Path.cwd(), *Path.cwd().parents):
        candidate = directory / "engkit.toml"
        if candidate.is_file():
            return candidate
    return None


def _coverage_env_names() -> tuple[str, str] | None:
    """读取 `[coverage]` 段的两个环境变量名（最小读取，不加载完整配置）。

    Returns:
        tuple[str, str] | None: `(data_env, source_env)`；配置文件缺失或 TOML 解析失败返回 None
        （与 `guard.config_for_hook()` 的 fail-open 口径一致）；段缺失时返回缺省键名。
    """
    path = _config_path()
    if path is None:
        return None
    try:
        with path.open("rb") as handle:
            data = tomllib.load(handle)
    except (OSError, tomllib.TOMLDecodeError):
        return None
    table = data.get("coverage")
    if not isinstance(table, dict):
        return (_DEFAULT_DATA_ENV, _DEFAULT_SOURCE_ENV)
    return (
        str(table.get("data_env", _DEFAULT_DATA_ENV)),
        str(table.get("source_env", _DEFAULT_SOURCE_ENV)),
    )


def start() -> Any:
    """按仓配置声明的环境变量启动覆盖率采集。

    Returns:
        Any: coverage 实例；未启用采集、定位不到配置或未安装 coverage 时返回 None。
    """
    names = _coverage_env_names()
    if names is None:
        return None
    data_env, source_env = names
    data_file = os.environ.get(data_env, "")
    if not data_file:
        return None
    source = tuple(item.strip() for item in os.environ.get(source_env, "").split(",") if item.strip())
    instance = _new_coverage(data_file, source)
    if instance is None:  # pragma: no cover - 被测仓未装覆盖率工具时的退化路径
        return None
    instance.start()
    return instance


def _new_coverage(data_file: str, source: tuple[str, ...]) -> Any:
    """构造 coverage 实例（行 + 分支、按进程号落盘）。

    Args:
        data_file: 数据文件基址（由 `[coverage].data_env` 指向）。
        source: 采集范围（包名序列；空表示不按包过滤）。

    Returns:
        Any: `coverage.Coverage` 实例；未安装 coverage 时返回 None。
    """
    try:
        import coverage
    except ModuleNotFoundError:  # pragma: no cover - 被测仓未装覆盖率工具时的退化路径
        return None
    # 与 `engkit.coverage.ensure_data_dir` 同语义；此处内联，避免采集启动前导入被测模块
    Path(data_file).parent.mkdir(parents=True, exist_ok=True)
    # 后缀固定为进程号：xdist worker 各写一份，合并时按文件名唯一（`data_suffix=True` 的随机后缀会撞名）
    return coverage.Coverage(data_file=data_file, data_suffix=f"{os.getpid()}", branch=True, source=source or None)


def stop() -> None:
    """停止并落盘本进程的采集数据（幂等：`pytest_sessionfinish` 与 `atexit` 都会调用）。"""
    global _coverage
    if _coverage is not None:
        _coverage.stop()
        _coverage.save()
        _coverage = None


def ensure_basetemp_parent(config: Any) -> None:
    """补齐 `--basetemp` 的父目录（pytest 只 `mkdir` 末级、不递归建父目录）。

    各仓 `addopts` 里的 `--basetemp=.cache/pytest` 指向仓内缓存目录，而
    `TmpPathFactory.getbasetemp()` 实现为 `basetemp.mkdir(mode=0o700)`；父目录 `.cache`
    不存在时，xdist 在 `pytest_sessionstart` 起 worker 即 `FileNotFoundError` 崩掉。
    本函数在 `pytest_configure` 阶段调用（早于 `pytest_sessionstart`），保证父目录就位。

    对 `config` 的形态做防御：pytest 正常传入 `Config` 对象，用例可用轻量假对象；
    取不到 `basetemp` 时静默跳过（不影响既有流程）。

    Args:
        config: pytest 配置对象（或兼容的假对象）。
    """
    option = getattr(config, "option", None)
    basetemp = getattr(option, "basetemp", "") or ""
    if not basetemp:
        return
    # `abspath` 结果必然含父目录，无需再判空
    os.makedirs(os.path.dirname(os.path.abspath(os.fspath(basetemp))), exist_ok=True)


def pytest_configure(config: Any) -> None:
    """重命令 / 全量守卫：命中拦截条件即抛 `UsageError`，用例零执行。

    同时补齐 `--basetemp` 的父目录（见 `ensure_basetemp_parent`）。

    Args:
        config: pytest 配置对象。
    """
    ensure_basetemp_parent(config)
    from . import guard  # 延迟导入：采集已启动，不会再漏计其导入期行

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
