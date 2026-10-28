"""开发环：按 git 改动只跑「直接依赖测试 + 前端受影响包 + 改动文件 ruff」。

口径（与推送前预检分工明确）：
- 依赖深度固定 `direct`（不做传递 import 闭包），不退化工程级全量；
- 不跑覆盖率、不跑仓级护栏、不做全仓静态——这些留推送前预检与 CI；
- ruff 只查本次改动的 `.py` 文件。
"""

from __future__ import annotations

import shlex
import shutil
import subprocess
import time
from pathlib import Path

from . import preflight, selection
from .config import Config
from .console import error, out

_LINT_CHUNK = 50
"""ruff 单批文件数（避免命令行过长）。"""


def _exec(argv: list[str], cwd: Path) -> int:
    """执行子进程（输出直通终端）。

    Args:
        argv: 命令与参数（首项按 `PATH` + `PATHEXT` 解析，兼容 Windows 的 `.cmd`）。
        cwd: 工作目录。

    Returns:
        int: 退出码（不可执行时返回 1）。
    """
    shown = " ".join(argv)
    out(f"  $ {shown}")
    resolved = shutil.which(argv[0]) or argv[0]
    try:
        return subprocess.run([resolved, *argv[1:]], cwd=cwd, check=False).returncode
    except OSError as exc:
        error(f"  执行失败：{exc}")
        return 1


def _chunks(items: list[str], size: int) -> list[list[str]]:
    """按固定大小切分清单。

    Args:
        items: 清单。
        size: 每批条数。

    Returns:
        list[list[str]]: 分批结果。
    """
    return [items[index : index + size] for index in range(0, len(items), size)]


def _lint_changed(cfg: Config, changed: list[str]) -> int:
    """只对改动文件跑 ruff check + format --check。

    Args:
        cfg: 仓配置。
        changed: 变更路径（相对仓库根）。

    Returns:
        int: 退出码（0 通过 / 非 0 首个失败码 / 未配置 ruff 时 0）。
    """
    if not cfg.ruff_cmd:
        return 0
    files = [rel for rel in changed if rel.endswith(".py") and (cfg.root / rel).is_file()]
    if not files:
        out("[devloop] 改动不含 Python 文件 ⇒ 跳过 ruff")
        return 0
    base = shlex.split(cfg.ruff_cmd)
    config = ["--config", cfg.ruff_config] if cfg.ruff_config else []
    out(f"[devloop] 静态：ruff {len(files)} 个改动文件")
    for chunk in _chunks(files, _LINT_CHUNK):
        code = _exec([*base, "check", *config, *chunk], cfg.root)
        if code != 0:
            return code
    for chunk in _chunks(files, _LINT_CHUNK):
        code = _exec([*base, "format", "--check", *config, *chunk], cfg.root)
        if code != 0:
            return code
    return 0


def _frontend_changed(cfg: Config, changed: list[str]) -> int:
    """按受影响前端包逐个跑测试命令。

    Args:
        cfg: 仓配置。
        changed: 变更路径（相对仓库根）。

    Returns:
        int: 退出码（0 通过 / 非 0 首个失败码 / 未配置前端时 0）。
    """
    if cfg.frontend is None:
        return 0
    picked = selection.build_frontend_selection(cfg, changed, depth="direct")
    if not picked.packages:
        out("[devloop] 前端：无受影响包 ⇒ 跳过")
        return 0
    for note in picked.notes:
        out(f"[devloop] 前端说明：{note}")
    out(f"[devloop] 前端：{'、'.join(picked.packages)}")
    for package in picked.packages:
        argv = [token.replace("{package}", package) for token in shlex.split(cfg.frontend.test_cmd)]
        code = _exec(argv, cfg.root)
        if code != 0:
            return code
    return 0


def run_changed(
    cfg: Config,
    *,
    changed_from: str = "",
    changed_files: tuple[str, ...] = (),
    changed_file_list: str = "",
    jobs: str = preflight.DEFAULT_JOBS,
    timeout: float = 0.0,
    lint: bool = True,
    frontend: bool = True,
) -> int:
    """开发环主流程：ruff（改动文件）→ 后端直接依赖测试 → 前端受影响包测试。

    Args:
        cfg: 仓配置。
        changed_from: git 基准（空串取 `[preflight].since`）。
        changed_files: 显式变更文件。
        changed_file_list: 变更清单文件。
        jobs: 并行档位。
        timeout: 后端单轮超时（秒；0 不设限）。
        lint: 是否跑改动文件 ruff。
        frontend: 是否跑前端受影响包。

    Returns:
        int: 退出码（0 全部通过 / 非 0 首个失败环节）。
    """
    started = time.monotonic()
    base = changed_from or cfg.preflight.since
    try:
        changed = selection.read_changed(
            cfg.root,
            changed_from=base,
            changed_files=changed_files,
            changed_file_list=changed_file_list,
        )
    except RuntimeError as exc:
        error(f"[devloop] {exc}")
        return 1
    out(f"[devloop] 变更 {len(changed)} 个文件（基准 {base or '工作区'}）")
    if not changed:
        out("[devloop] 未发现变更 ⇒ 无需测试")
        return 0
    if lint:
        code = _lint_changed(cfg, changed)
        if code != 0:
            out("[devloop] 静态未过 ⇒ 先修 lint，测试未执行")
            return code
    code = preflight.run_selected(cfg, changed=changed, jobs=jobs, cov=False, timeout=timeout, depth="direct")
    if code != 0:
        return code
    if frontend:
        code = _frontend_changed(cfg, changed)
        if code != 0:
            return code
    out(f"[devloop] 完成（{time.monotonic() - started:.1f}s）")
    return 0
