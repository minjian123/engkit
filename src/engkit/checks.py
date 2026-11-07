"""护栏检查单进程 runner（`engkit checks` 的唯一实现）。

为什么单进程：10+ 条检查各起一个解释器时，固定成本（启动 + 导入）合计约 47s，占提交环 40%；
检查本身多为秒级静态判定。同进程按声明序执行可把这部分压到一个解释器的成本。

隔离契约（见《预检耗时治理 · 详细设计》§5）：

- 每条执行前后恢复 `sys.argv` / `sys.path` / 环境变量快照；
- 单条抛异常只记该条失败、继续后续条目，末尾统一汇总；
- **不清理**新增的 `sys.modules`（清理会破坏 engkit 自身已导入模块），故约定「检查脚本不得设置
  影响后续的全局状态」；确有需要时把 `[preflight].checks_runner` 改成 `process` 回退逐条子进程。

**预算口径差异**：同进程无法中断单条执行，故本模式**不做单条超时**（超时保护由 `process` 模式提供）；
总体预算由调用方在条目之间判定。
"""

from __future__ import annotations

import os
import runpy
import subprocess
import sys
import time
from collections.abc import Sequence
from pathlib import Path

from . import steps_reuse
from .config import Config, SelfTest
from .console import note, out


def run_batch(
    cfg: Config,
    entries: Sequence[SelfTest],
    *,
    changed: Sequence[str] = (),
    only: Sequence[str] = (),
) -> list[str]:
    """按声明序同进程执行护栏检查。

    Args:
        cfg: 仓配置。
        entries: 检查清单（命令中的占位符须已展开）。
        changed: 当次改动文件（结果复用的输入集依据）。
        only: 只跑这些标签（空表示全部）。

    Returns:
        list[str]: 失败的标签（空表示全部通过）。
    """
    picked = [entry for entry in entries if not only or entry.label in only]
    if not picked:
        out("\n[checks] 无可执行的检查（清单为空或过滤后为空）")
        return []
    out(f"\n[checks] 单进程执行 {len(picked)} 条检查（不做单条超时；需要超时保护请改 checks_runner = process）")
    failures: list[str] = []
    for entry in picked:
        argv = list(entry.cmd)
        key = steps_reuse.key_for(cfg, argv, entry.deps, changed)
        if key is not None:
            hit = steps_reuse.lookup(cfg, key)
            if hit is not None:
                out(
                    f"\n[checks] {entry.label}\n"
                    f"  复用（上次通过于 {hit.passed_at}，耗时 {hit.duration:.1f}s；命令与输入未变）"
                )
                continue
        out(f"\n[checks] {entry.label}")
        started = time.monotonic()
        code, detail = _run_entry(cfg, entry)
        elapsed = time.monotonic() - started
        if key is not None:
            if code == 0:
                steps_reuse.record(cfg, key, entry.label, elapsed)
            else:
                steps_reuse.forget(cfg, key)
        if code == 0:
            note(f"通过（{elapsed:.1f}s）")
            continue
        failures.append(entry.label)
        note(f"失败（{detail or f'退出码 {code}'}；{elapsed:.1f}s）")
    passed = len(picked) - len(failures)
    out(f"\n[checks] 通过 {passed} / 失败 {len(failures)}")
    if failures:
        out(f"[checks] 失败项：{'、'.join(failures)}")
    return failures


def _run_entry(cfg: Config, entry: SelfTest) -> tuple[int, str]:
    """执行一条检查（异常与退出码都换算成结果，不外抛）。

    只有「`python <脚本> [参数…]`」形态才走同进程；其他形态（如直接调用某个可执行文件）
    回退子进程执行——**回退到执行而非跳过**，语义与改造前一致。

    Args:
        cfg: 仓配置。
        entry: 检查登记项（命令占位符已展开）。

    Returns:
        tuple[int, str]: （退出码, 失败说明；通过时说明为空串）。
    """
    located = _locate(entry.cmd, cfg.root)
    if located is None:
        note("  形态不支持同进程执行，回退子进程")
        return _run_process(cfg, entry)
    script, args = located
    if not script.is_file():
        return 1, f"脚本不存在：{script}"
    saved_argv = list(sys.argv)
    saved_path = list(sys.path)
    saved_env = dict(os.environ)
    try:
        sys.argv = [str(script), *args]
        sys.path.insert(0, str(script.parent))
        runpy.run_path(str(script), run_name="__main__")
        return 0, ""
    except SystemExit as exc:
        code = exc.code
        if code is None or code == 0:
            return 0, ""
        if isinstance(code, int):
            return code, ""
        return 1, str(code)
    except Exception as exc:  # 单条异常不得中断整批（契约见模块文档）
        return 1, f"{type(exc).__name__}: {exc}"
    finally:
        sys.argv = saved_argv
        sys.path = saved_path
        os.environ.clear()
        os.environ.update(saved_env)


def _run_process(cfg: Config, entry: SelfTest) -> tuple[int, str]:
    """子进程执行一条检查（同进程形态不支持时的回退通路）。

    Args:
        cfg: 仓配置。
        entry: 检查登记项。

    Returns:
        tuple[int, str]: （退出码, 失败说明）。
    """
    argv = list(entry.cmd)
    if not argv:
        return 1, "命令为空"
    try:
        result = subprocess.run(argv, cwd=str(cfg.root), check=False)
    except FileNotFoundError:
        return 127, f"命令不存在：{argv[0]}"
    except OSError as exc:
        return 1, f"{type(exc).__name__}: {exc}"
    return (0, "") if result.returncode == 0 else (result.returncode, "")


def _locate(cmd: Sequence[str], root: Path) -> tuple[Path, tuple[str, ...]] | None:
    """从命令里取出脚本路径与参数。

    Args:
        cmd: 已展开占位符的命令。
        root: 仓根（相对脚本路径的基准）。

    Returns:
        tuple[Path, tuple[str, ...]] | None: （脚本绝对路径, 参数）；形态不支持返回 None。
    """
    tokens = list(cmd)
    if not tokens:
        return None
    if tokens[0].endswith(".py"):
        return _resolve(root, tokens[0]), tuple(tokens[1:])
    if len(tokens) >= 2 and tokens[1].endswith(".py"):
        return _resolve(root, tokens[1]), tuple(tokens[2:])
    return None


def _resolve(root: Path, rel: str) -> Path:
    """把脚本路径解析为绝对路径。

    Args:
        root: 仓根。
        rel: 命令里的脚本路径。

    Returns:
        Path: 绝对路径。
    """
    path = Path(rel)
    return path if path.is_absolute() else (root / path)
