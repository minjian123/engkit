"""重命令拦截（`engkit guard`）：本地非 CI、未经包装跑重命令即报错，命令零执行。

两条挂接通道共用同一判定：

- **pytest 插件**：`pytest_addoption` / `pytest_configure`（由各仓测试插件在最早期转调）；
- **`.pth` 启动钩子**：解释器启动即判定，覆盖不经过 pytest 的解释器入口（如 pyright 启动器）。

已知边界：`ruff` 为原生二进制、不经过 Python 解释器，本钩子无法覆盖，其全仓调用靠规范与预检兜底。
"""

from __future__ import annotations

import os
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

from .config import Config, ConfigError, GuardConfig, find_upward, load
from .runonce import WRAPPED_ENV

ALLOW_ENV = "ENGKIT_ALLOW_UNWRAPPED"
"""显式绕过包装检查的环境变量（排查包装器自身问题时用）。"""

FULL_ENV = "ENGKIT_ALLOW_FULL_TESTS"
"""显式声明「本次要跑全量测试」的环境变量。"""

FULL_FLAG = "--full"
"""显式全量开关（pytest 自定义选项，由本模块注册）。"""

CI_ENVS = ("CI", "GITLAB_CI", "GITHUB_ACTIONS", "JENKINS_URL", "BUILDKITE", "TF_BUILD")
"""CI 环境标识（任一存在即视为 CI，不拦）。"""

HOOK_ENV = "ENGKIT_HEAVY_GUARD"
HOOK_SKIP_ENV = "ENGKIT_HEAVY_GUARD_SKIP"
"""`.pth` 钩子内部标记（防重复执行 / 钩子自身退出）。"""

PTH_NAME = "engkit_heavy_guard.pth"
"""钩子文件名。"""

_CONFIG_ENV = "ENGKIT_CONFIG"
"""显式配置路径环境变量（钩子类入口在任意工作目录时用）。"""

_SUBCOMMANDS = ("check", "format", "rule", "config", "linter", "clean", "server", "analyze", "version")
"""工具子命令（不算目标路径）。"""

_VALUE_OPTIONS = ("-k", "-m", "--deselect", "--cov", "--cov-report", "-p", "--rootdir", "-c")
"""带取值的选项（其取值不算目标路径）。"""

_SCOPE_OPTIONS = ("-k", "-m", "--lf", "--ff", "--deselect", "--changed-file-list")
"""收窄测试范围的选项（存在即视为「已指定范围」）。"""

_checked = False
"""进程内已检查标记（`.pth` 可能多次加载，防重复判定）。"""


def _truthy(name: str) -> bool:
    """环境变量是否为真值。

    Args:
        name: 变量名。

    Returns:
        bool: `1` / `true` / `yes` / `on` 返回 True。
    """
    return os.environ.get(name, "").strip().lower() in {"1", "true", "yes", "on"}


def is_ci() -> bool:
    """是否 CI 环境。

    Returns:
        bool: CI 环境返回 True。
    """
    return any(os.environ.get(name) for name in CI_ENVS)


def is_wrapped() -> bool:
    """本次调用是否经 `engkit run_once` 包装。

    Returns:
        bool: 已包装返回 True。
    """
    return os.environ.get(WRAPPED_ENV, "").strip() == "1"


def allow_unwrapped() -> bool:
    """是否显式声明绕过包装检查。

    Returns:
        bool: 显式声明返回 True。
    """
    return _truthy(ALLOW_ENV)


def allow_full() -> bool:
    """是否显式声明本次跑全量测试。

    Returns:
        bool: 显式声明返回 True。
    """
    return _truthy(FULL_ENV)


def positional(args: list[str] | tuple[str, ...]) -> list[str]:
    """取命令行中的位置参数（跳过选项、选项取值与工具子命令）。

    Args:
        args: 命令行参数（不含程序名）。

    Returns:
        list[str]: 位置参数。
    """
    found: list[str] = []
    skip_next = False
    for arg in args:
        if skip_next:
            skip_next = False
            continue
        if arg in _VALUE_OPTIONS:
            skip_next = True
            continue
        if arg.startswith("-"):
            continue
        if arg in _SUBCOMMANDS:
            continue
        found.append(arg)
    return found


def count_test_files(target: Path) -> int:
    """统计目录下的测试文件数（`test_*.py` / `*_test.py`）。

    Args:
        target: 目标路径。

    Returns:
        int: 测试文件数（非目录为 0）。
    """
    if not target.is_dir():
        return 0
    return sum(1 for item in target.rglob("*.py") if item.name.startswith("test_") or item.name.endswith("_test.py"))


def _is_heavy_pytest(args: list[str], guard: GuardConfig) -> bool:
    """pytest 调用是否属重命令（无范围 / 范围覆盖的测试文件数超阈值）。

    Args:
        args: 命令行参数（不含程序名）。
        guard: 拦截配置。

    Returns:
        bool: 属重命令返回 True。
    """
    targets = positional(args)
    if not targets:
        return True
    for item in targets:
        if "::" in item:
            continue
        target = Path(item)
        if target.suffix.lower() == ".py":
            continue
        if count_test_files(target) > guard.dir_test_threshold:
            return True
    return False


def _is_heavy_scanner(args: list[str], guard: GuardConfig, *, default_targets: bool) -> bool:
    """扫描类命令（ruff / pyright）是否属重命令（无参数扫缺省目标，或指向仓库级目录）。

    Args:
        args: 命令行参数（不含程序名）。
        guard: 拦截配置。
        default_targets: 无参数时是否默认扫全仓。

    Returns:
        bool: 属重命令返回 True。
    """
    targets = positional(args)
    if not targets:
        return default_targets
    for item in targets:
        normalized = item.replace("\\", "/").strip("/")
        if normalized in (".", "") or normalized in guard.repo_dirs:
            return True
    return False


def classify(argv: list[str] | tuple[str, ...], guard: GuardConfig) -> str:
    """判定本次调用是否重命令并给出类别。

    Args:
        argv: 完整命令行（含程序名 / 解释器路径）。
        guard: 拦截配置。

    Returns:
        str: 类别（`script` / `pytest` / `pyright` / `ruff`）；非重命令返回空串。
    """
    if not argv:
        return ""
    program = Path(argv[0]).name.lower()
    rest = list(argv[1:])
    if program in ("python", "python3", "python.exe", "python3.exe") and len(rest) >= 2 and rest[0] == "-m":
        program = rest[1].lower()
        rest = rest[2:]
    if any(Path(item).name in guard.scripts for item in argv):
        return "script"
    if program.startswith("pytest"):
        return "pytest" if _is_heavy_pytest(rest, guard) else ""
    if program.startswith("pyright"):
        return "pyright" if _is_heavy_scanner(rest, guard, default_targets=True) else ""
    if program.startswith("ruff"):
        return "ruff" if _is_heavy_scanner(rest, guard, default_targets=True) else ""
    return ""


def heavy_guidance(kind: str) -> str:
    """生成重命令拦截提示。

    Args:
        kind: 类别。

    Returns:
        str: 提示文本。
    """
    return (
        f"[重命令守卫] 拒绝执行：本地非 CI 环境未经包装跑重命令（{kind}）。\n"
        "  重命令（分钟级 / 数十秒级）一律经包装调用，避免重复执行与为挑报错重跑：\n"
        "    engkit run_once -- <原命令>\n"
        "  包装口径：缓存键 =「命令 + 工作目录 + 工作区指纹」，窗口内命中即回放；\n"
        "  改完代码想立刻重验加 `--force`，只想看缓存状态用 `--show`。\n"
        "  单文件 / 单节点调用（秒级）不受此限。\n"
        f"  确需裸跑请显式声明：{ALLOW_ENV}=1 <原命令>"
    )


def should_block(argv: list[str] | tuple[str, ...], guard: GuardConfig) -> str:
    """判定是否应拦截本次调用。

    Args:
        argv: 完整命令行。
        guard: 拦截配置。

    Returns:
        str: 应拦截返回类别，放行返回空串。
    """
    if is_ci() or is_wrapped() or allow_unwrapped():
        return ""
    return classify(argv, guard)


def has_explicit_scope(args: list[str] | tuple[str, ...]) -> bool:
    """命令行是否已显式指定测试范围。

    Args:
        args: 命令行参数（不含程序名）。

    Returns:
        bool: 存在位置参数或收窄选项返回 True。
    """
    for arg in args:
        if arg in _SCOPE_OPTIONS or any(arg.startswith(f"{option}=") for option in _SCOPE_OPTIONS):
            return True
        if arg.startswith("-"):
            continue
        return True
    return False


def full_test_should_block(args: list[str] | tuple[str, ...]) -> bool:
    """判定是否应拦截本次 pytest 调用（跑全量而未显式声明）。

    Args:
        args: 命令行参数（不含程序名）。

    Returns:
        bool: 应拦截返回 True。
    """
    if is_ci() or allow_full() or is_wrapped():
        return False
    if FULL_FLAG in args:
        return False
    return not has_explicit_scope(args)


def full_test_guidance() -> str:
    """生成全量测试拦截提示。

    Returns:
        str: 提示文本。
    """
    return (
        "[全量测试守卫] 拒绝执行：本地非 CI 环境既未声明全量、也未指定测试范围。\n"
        "  请改用定向档（按变更自动推导，约数秒）：\n"
        "    engkit run-selected --changed-from HEAD\n"
        "  确需全量请显式声明其一：\n"
        "    pytest --full\n"
        f"    {FULL_ENV}=1 pytest\n"
        "  口径见《测试规范》「变更影响分析」与《AI开发规范》「覆盖率要求」。"
    )


def pytest_addoption(parser: Any) -> None:
    """注册 `--full` 显式全量开关。

    Args:
        parser: pytest 的 Parser 对象（类型放宽以避免硬依赖 pytest）。
    """
    addoption = getattr(parser, "addoption", None)
    if addoption is None:  # pragma: no cover - 非 pytest 环境调用
        return
    addoption(FULL_FLAG, action="store_true", default=False, help="显式声明本次跑全量（本地非 CI 环境必需）")


def config_for_hook() -> Config | None:
    """为钩子类入口定位配置（显式路径优先，其次自当前目录上溯）。

    Returns:
        Config | None: 配置对象；定位失败返回 None（fail-open）。
    """
    override = os.environ.get(_CONFIG_ENV, "")
    try:
        if override:
            return load(Path(override))
        found = find_upward(Path.cwd())
        return None if found is None else load(found)
    except ConfigError:
        return None


def pytest_configure(config: Any) -> None:
    """pytest 入口拦截（收集之前，命中即 `UsageError`，零用例执行）。

    Args:
        config: pytest 配置对象。

    Raises:
        Exception: 命中拦截条件时抛出 `pytest.UsageError`。
    """
    import pytest

    params = getattr(config, "invocation_params", None)
    args: list[str] = list(params.args) if params is not None else []
    loaded = config_for_hook()
    guard_config = loaded.guard if loaded is not None else GuardConfig()
    kind = "" if is_ci() or is_wrapped() or allow_unwrapped() else classify(["pytest", *args], guard_config)
    if kind:
        raise pytest.UsageError(heavy_guidance(kind))
    if full_test_should_block(args):
        raise pytest.UsageError(full_test_guidance())


def site_packages(venv: Path) -> Path | None:
    """定位 venv 的 `site-packages`（Windows / POSIX 两种布局）。

    Args:
        venv: 虚拟环境目录。

    Returns:
        Path | None: `site-packages` 路径；找不到时为 None。
    """
    windows = venv / "Lib" / "site-packages"
    if windows.is_dir():
        return windows
    lib = venv / "lib"
    if lib.is_dir():
        for candidate in sorted(lib.glob("python*/site-packages")):
            return candidate
    return None


def render_pth(source_root: Path) -> str:
    """渲染 `.pth` 内容（单行：把 engkit 源码根加入 `sys.path` 后调用钩子）。

    Args:
        source_root: engkit 源码根（含 `engkit/` 包的目录）。

    Returns:
        str: `.pth` 文件内容。
    """
    return (
        "import sys; "
        f"sys.path.insert(0, r'{source_root.as_posix()}'); "
        "import engkit.guard as _engkit_guard; "
        "_engkit_guard.boot()"
    )


def install_hook(site_packages_dir: Path, source_root: Path) -> Path:
    """写入 `.pth`（幂等覆盖）。

    Args:
        site_packages_dir: venv 的 `site-packages`。
        source_root: engkit 源码根。

    Returns:
        Path: 钩子文件路径。
    """
    target = site_packages_dir / PTH_NAME
    target.write_text(render_pth(source_root) + "\n", encoding="utf-8")
    return target


def check_hook(site_packages_dir: Path, source_root: Path) -> int:
    """校验 `.pth` 是否存在且内容与当前源码路径一致。

    Args:
        site_packages_dir: venv 的 `site-packages`。
        source_root: engkit 源码根。

    Returns:
        int: 0 一致 / 1 缺失或过期。
    """
    target = site_packages_dir / PTH_NAME
    if not target.is_file():
        print(f"[guard-hook] 缺失：{target}（请运行安装；依赖重装后需重跑）")
        return 1
    if target.read_text(encoding="utf-8").strip() != render_pth(source_root):
        print(f"[guard-hook] 过期：{target}（源码路径已变，请重跑安装）")
        return 1
    print(f"[guard-hook] 正常：{target}")
    return 0


def remove_hook(site_packages_dir: Path) -> int:
    """卸载 `.pth`（幂等）。

    Args:
        site_packages_dir: venv 的 `site-packages`。

    Returns:
        int: 退出码（恒 0）。
    """
    target = site_packages_dir / PTH_NAME
    if target.is_file():
        target.unlink()
        print(f"[guard-hook] 已卸载：{target}")
    else:
        print(f"[guard-hook] 无需卸载：{target}")
    return 0


def check(argv: list[str] | None = None) -> None:
    """判定当前调用是否重命令；命中即抛出 `SystemExit`（便于单测）。

    Args:
        argv: 命令行（缺省取 `sys.argv`）。

    Raises:
        SystemExit: 命中拦截条件（退出码 1）。
    """
    global _checked
    if _checked or os.environ.get(HOOK_SKIP_ENV) == "1" or os.environ.get(HOOK_ENV) == "1":
        return
    loaded = config_for_hook()
    if loaded is None:
        return
    kind = should_block(list(sys.argv if argv is None else argv), loaded.guard)
    if not kind:
        return
    _checked = True
    os.environ[HOOK_ENV] = "1"
    sys.stderr.write(heavy_guidance(kind) + "\n")
    raise SystemExit(1)


def boot(hard_exit: Callable[[int], Any] = os._exit) -> None:
    """解释器启动钩子：命中即硬退出（`.pth` 在 site 初始化期执行，`SystemExit` 噪声大且退出码不可控）。

    Args:
        hard_exit: 硬退出函数（单测注入替身）。
    """
    try:
        check()
    except SystemExit:
        sys.stderr.flush()
        hard_exit(1)
