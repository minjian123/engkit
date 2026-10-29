"""推送入口（`engkit push`）：预检是推送的内部步骤，本地不再有独立的预检命令。

判据从「工作树是否干净」换成「是否真要推送」——只有存在领先远端的提交时才可能触发；
待推提交全为文档时自动降为快速档（文档改动不触发用例与覆盖率）。
"""

from __future__ import annotations

import fnmatch
import subprocess

from . import gitutil, preflight
from .config import Config
from .console import note, out

MODE_FAST = "fast"
"""文档改动档位：只静态 + 护栏 + 引擎自测。"""

MODE_SCOPED = "scoped"
"""代码改动档位：静态 + 定向用例 + 覆盖率门禁。"""

MODE_FULL = "full"
"""全量档位（显式申请）。"""


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
        return "[push] 档位：全量（显式 --full）"
    scope = f"待推 {len(commits)} 个提交、涉及 {len(files)} 个文件"
    if not known:
        return f"[push] 档位：定向（远端无该分支，按代码改动处理；{scope}）"
    if mode == MODE_FAST:
        return f"[push] 档位：快速（{scope}，全为文档，跳过用例与覆盖率）"
    return f"[push] 档位：定向（{scope}）"


def run_push(
    cfg: Config,
    *,
    check: bool = False,
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
        check: 只跑预检、不推送。
        full: 全量档（隐含定向档的全部步骤 + 全量用例）。
        remote: 远端名（空串取配置）。
        branch: 目标分支（空串取配置或当前分支）。
        jobs: 并行档位。
        step_seconds: 单步预算覆盖。
        budget_seconds: 总体预算覆盖。

    Returns:
        int: 退出码（0 通过 / 1 被拒绝或预检失败 / push 的退出码）。
    """
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
            "  开发中改完即测请用开发环（engkit run-changed）；准备推送时先提交，再跑 engkit push。"
        )
        return 1
    commits, files, known = gitutil.ahead(root, target_remote, target_branch)
    if known and not commits:
        out(f"[push] 没有待推送的提交（{target_remote}/{target_branch} 已是最新）。")
        return 0
    mode = resolve_mode(files, cfg.push.doc_globs, full=full)
    out(_describe(mode, commits, files, known))
    code = preflight.run_preflight(
        cfg,
        mode=mode,
        jobs=jobs,
        no_cov=False,
        no_scope=False,
        step_seconds=step_seconds,
        budget_seconds=budget_seconds,
        from_push=True,
    )
    if code != 0:
        note("[push] 预检未通过，已中止推送。")
        return code
    if check:
        out(f"[push] 预检通过（--check 未推送；待推 {len(commits)} 个提交）。")
        return 0
    out(f"[push] 预检通过，推送 {target_remote}/{target_branch} ……")
    result = subprocess.run(
        ["git", "push", target_remote, f"HEAD:{target_branch}"],
        cwd=str(root),
        check=False,
    )
    if result.returncode != 0:
        note("[push] 推送失败（预检结论仍有效；排除网络 / 冲突后重试 engkit push）。")
        return result.returncode
    out("[push] 推送完成。")
    return 0
