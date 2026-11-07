"""推送入口（`engkit push`）：预检是推送的内部步骤，本地不再有独立的预检命令。

判据从「工作树是否干净」换成「是否真要推送」——只有存在领先远端的提交时才可能触发；
待推提交全为文档时自动降为快速档（文档改动不触发用例与覆盖率）。
"""

from __future__ import annotations

import fnmatch
import os
import re
import subprocess
import time

from . import gitutil, preflight
from .config import Config
from .console import note, out

MODE_FAST = "fast"
"""文档改动档位：只静态 + 护栏 + 引擎自测。"""

MODE_SCOPED = "scoped"
"""代码改动档位：静态 + 定向用例 + 覆盖率门禁。"""

MODE_FULL = "full"
"""全量档位（显式申请）：全量静态 + 全部自测 + 定向用例；全量测试只由 CI 执行。"""

_SERIAL_ADDOPTS_RE = re.compile(r"(?<!\S)-n\s*0(?!\S)")
"""`PYTEST_ADDOPTS` 里强制串行的 `-n 0` 形态。"""


def serial_reason(jobs: str) -> str:
    """判定本次推送是否被人为串行化（空串表示未命中）。

    Args:
        jobs: 并行档位取值。

    Returns:
        str: 拒绝原因；未命中返回空串。
    """
    try:
        if not preflight.jobs_args(jobs):
            return f"--jobs {jobs!r} 会把推送环强制串行化"
    except ValueError:
        pass  # 非法取值由 run_preflight 统一报错
    addopts = os.environ.get("PYTEST_ADDOPTS", "")
    if _SERIAL_ADDOPTS_RE.search(addopts):
        return f"PYTEST_ADDOPTS={addopts!r} 含 -n 0，会把推送环强制串行化"
    return ""


def is_doc(path: str, globs: tuple[str, ...]) -> bool:
    """路径是否属于文档（按文件名模式匹配，目录不参与）。

    Args:
        path: 相对仓库根的路径。
        globs: 文件名模式（如 `*.md`）。

    Returns:
        bool: 命中任一模式返回 True。
    """
    name = path.rsplit("/", 1)[-1]
    return any(fnmatch.fnmatch(name, pattern) for pattern in globs)


def all_docs(files: list[str], globs: tuple[str, ...]) -> bool:
    """待推文件是否「全为文档」（空集合不算）。

    Args:
        files: 待推提交涉及的文件。
        globs: 文件名模式。

    Returns:
        bool: 非空且全部命中文档模式返回 True。
    """
    return bool(files) and all(is_doc(item, globs) for item in files)


def resolve_mode(files: list[str], globs: tuple[str, ...], *, full: bool) -> str:
    """解析推送时的预检档位。

    Args:
        files: 待推提交涉及的文件。
        globs: 文件名模式（文档判定）。
        full: 是否显式申请全量档。

    Returns:
        str: 档位（`fast` / `scoped` / `full`）。
    """
    if full:
        return MODE_FULL
    return MODE_FAST if all_docs(files, globs) else MODE_SCOPED


def _describe(mode: str, commits: list[str], files: list[str], known: bool) -> str:
    """生成档位判定说明。

    Args:
        mode: 档位。
        commits: 待推提交。
        files: 待推文件。
        known: 远端引用是否可比较。

    Returns:
        str: 说明文本。
    """
    if mode == MODE_FULL:
        return "[push] 档位：全量静态 + 定向用例（显式 --full；全量测试只由 CI 执行）"
    scope = f"待推 {len(commits)} 个提交、涉及 {len(files)} 个文件"
    if not known:
        return f"[push] 档位：定向（远端无该分支，按代码改动处理；{scope}）"
    if mode == MODE_FAST:
        return f"[push] 档位：快速（{scope}，全为文档，跳过用例与覆盖率）"
    return f"[push] 档位：定向（{scope}）"


def run_push(
    cfg: Config,
    *,
    full: bool = False,
    remote: str = "",
    branch: str = "",
    jobs: str,
    step_seconds: float | None = None,
    budget_seconds: float | None = None,
) -> int:
    """执行「预检 + 推送」。

    Args:
        cfg: 仓配置。
        full: 全量档（全量静态 + 全部自测 + 定向用例；不含全量测试）。
        remote: 远端名（空串取配置）。
        branch: 目标分支（空串取配置或当前分支）。
        jobs: 并行档位。
        step_seconds: 单步预算覆盖。
        budget_seconds: 总体预算覆盖。

    Returns:
        int: 退出码（0 通过 / 1 被拒绝或预检失败 / push 的退出码）。
    """
    started = time.monotonic()
    reason = serial_reason(jobs)
    if reason:
        print(f"[push] 拒绝执行：{reason}（项目默认 -n auto 并行，不动共享测试库与写锁）。")
        print("  单次 push 全链路受 600s 硬上限约束；去掉 --jobs off / -n 0 后重试。")
        return 1
    root = cfg.root
    target_remote = remote or cfg.push.remote
    target_branch = branch or cfg.push.branch or gitutil.current_branch(root)
    if not target_branch:
        print("[push] 拒绝执行：当前处于游离 HEAD，无法确定推送分支（用 --branch 指定）。")
        return 1
    entries, _ = gitutil.worktree_dirty(root)
    if entries:
        sample = "".join(f"\n    - {item}" for item in entries[:5])
        more = "" if len(entries) <= 5 else f"\n    …（其余 {len(entries) - 5} 处从略）"
        print(
            f"[push] 拒绝执行：工作树有 {len(entries)} 处未提交改动，推送前须先提交。\n"
            f"  未提交条目：{sample}{more}\n"
            "  改完直接提交：提交前预检按改动面跑全（静态 + 护栏 + 定向用例 + 改动行覆盖率），\n"
            "  一次拿到完整失败清单；准备推送时先提交，再跑 engkit push。"
        )
        return 1
    commits, files, known = gitutil.ahead(root, target_remote, target_branch)
    if known and not commits:
        out(f"[push] 没有待推送的提交（{target_remote}/{target_branch} 已是最新）。")
        return 0
    mode = resolve_mode(files, cfg.push.doc_globs, full=full)
    out(_describe(mode, commits, files, known))
    out(
        f"[push] 时长闸门：单次 push 全链路 ≤ {preflight.PUSH_MAX_TOTAL_SECONDS:.0f}s"
        "（预检 + git push；超限立即报错且绝不执行 git push）"
    )
    effective_budget = min(
        budget_seconds if budget_seconds is not None else preflight.PUSH_MAX_TOTAL_SECONDS,
        preflight.PUSH_MAX_TOTAL_SECONDS,
    )
    effective_step = min(
        step_seconds if step_seconds is not None else preflight.PUSH_MAX_TOTAL_SECONDS,
        preflight.PUSH_MAX_TOTAL_SECONDS,
    )
    code = preflight.run_preflight(
        cfg,
        mode=mode,
        jobs=jobs,
        no_cov=False,
        no_scope=False,
        step_seconds=effective_step,
        budget_seconds=effective_budget,
        from_push=True,
        wall_started=started,
    )
    elapsed = time.monotonic() - started
    if elapsed >= preflight.PUSH_MAX_TOTAL_SECONDS:
        verdict = "通过" if code == 0 else "未通过"
        print(
            f"[push] 拒绝推送：单次 push 全链路已耗时 {elapsed:.1f}s，超过 "
            f"{preflight.PUSH_MAX_TOTAL_SECONDS:.0f}s 硬上限（预检结论：{verdict}）；本次不执行 git push。"
        )
        print("  处置：按预检日志的耗时汇总定位慢点，不放宽上限 / 不降覆盖率门槛 / 不自动重试。")
        return 1
    if code != 0:
        note("[push] 预检未通过，已中止推送。")
        return code
    out(f"[push] 预检通过（耗时 {elapsed:.1f}s），推送 {target_remote}/{target_branch} ……")
    remaining = preflight.PUSH_MAX_TOTAL_SECONDS - elapsed
    try:
        result = subprocess.run(
            ["git", "push", target_remote, f"HEAD:{target_branch}"],
            cwd=str(root),
            check=False,
            timeout=max(1.0, remaining),
        )
    except subprocess.TimeoutExpired:
        print(
            f"[push] 拒绝推送：git push 触及 {preflight.PUSH_MAX_TOTAL_SECONDS:.0f}s 硬上限，已中止"
            "（远端可能已部分接收，请核对后重试）。"
        )
        return 1
    if result.returncode != 0:
        note("[push] 推送失败（预检结论仍有效；排除网络 / 冲突后重试 engkit push）。")
        return result.returncode
    out(f"[push] 推送完成（全链路耗时 {time.monotonic() - started:.1f}s）。")
    hint = cfg.push.ci_trigger_hint or "仓内 CI 手工触发入口（位置见仓侧测试 / 部署规范）"
    out(f"[push] 待 CI 复核：本仓流水线不因推送触发；全量回归归 CI，请按需手工触发 {hint}。")
    return 0
