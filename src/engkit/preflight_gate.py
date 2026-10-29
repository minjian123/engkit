"""预检场景闸门与调用审计（防止把推送前门禁当开发中的反馈环用）。

预检的成本在「全仓静态 + 定向用例 + 覆盖率门禁」这一档，开发中每改一个文件就跑一次毫无收益：
真正需要的是开发环（`engkit run-changed`，只跑改动相关、不采集覆盖率）。本模块把「什么时候该跑预检」
从文案变成机器判定——**工作树有未提交改动（已跟踪改动或未跟踪文件）即拒绝执行**，命令零步骤执行。

审计记录三类事件（真跑 / 被拒 / 绕过 `--allow-dirty`），一行一条 JSON，供事后复盘闸门是否有效；
写盘失败一律 fail-open，不影响预检结论。CI 环境不设闸门（流水线里没有「开发中」这一场景）。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from . import gitutil, guard
from .config import Config

EVENT_RUN = "run"
"""事件：预检真跑。"""

EVENT_REJECTED = "rejected"
"""事件：脏工作树被拒（零步骤执行）。"""

EVENT_BYPASS = "bypass"
"""事件：脏工作树经 `--allow-dirty` 绕过。"""

REASON_DIRTY = "dirty-tree"
"""拒绝原因：脏工作树。"""

REASON_ALLOW_DIRTY_SCOPED = "allow-dirty-scoped"
"""拒绝原因：`--allow-dirty` 只对快速档开放（定向 / 全量档必须干净树）。"""

_SAMPLE = 5
"""审计里保留的脏条目样例条数（完整清单在 git status 里可查，故不整份落盘）。"""


@dataclass(frozen=True)
class Decision:
    """闸门判定结果。

    Attributes:
        blocked: 是否拒绝执行。
        entries: 脏工作树条目（已跟踪改动 + 未跟踪文件）。
        bypassed: 是否经 `--allow-dirty` 放行。
        reliable: git 是否可判定（不可判定时放行，不误拦）。
        reason: 拒绝原因（放行时为空串）。
    """

    blocked: bool
    entries: tuple[str, ...] = ()
    bypassed: bool = False
    reliable: bool = True
    reason: str = ""


def evaluate(cfg: Config, *, allow_dirty: bool, mode: str) -> Decision:
    """判定本次预检是否放行。

    Args:
        cfg: 仓配置。
        allow_dirty: 是否显式声明「脏工作树也放行」。
        mode: 档位（`--allow-dirty` 只对 `fast` 开放，定向 / 全量档必须干净树）。

    Returns:
        Decision: 判定结果（CI 环境、仓配置关闭闸门或 git 不可判定时一律放行）。
    """
    if not cfg.preflight.gate.dirty_tree or guard.is_ci():
        return Decision(blocked=False)
    entries, reliable = gitutil.worktree_dirty(cfg.root)
    if not entries:
        return Decision(blocked=False, reliable=reliable)
    if allow_dirty and mode == "fast":
        return Decision(blocked=False, entries=tuple(entries), bypassed=True, reliable=reliable)
    reason = REASON_ALLOW_DIRTY_SCOPED if allow_dirty else REASON_DIRTY
    return Decision(blocked=True, entries=tuple(entries), reliable=reliable, reason=reason)


def guidance(decision: Decision) -> str:
    """生成拒绝提示（指向开发环与显式绕过开关）。

    Args:
        decision: 判定结果。

    Returns:
        str: 提示文本。
    """
    sample = "".join(f"\n    - {item}" for item in decision.entries[:_SAMPLE])
    more = "" if len(decision.entries) <= _SAMPLE else f"\n    …（其余 {len(decision.entries) - _SAMPLE} 处从略）"
    scoped_hint = (
        "\n  `--allow-dirty` 只对快速档开放：定向档 `--push` / 全量档 `--full` 必须干净树（先提交再跑）。"
        if decision.reason == REASON_ALLOW_DIRTY_SCOPED
        else ""
    )
    return (
        f"[预检闸门] 拒绝执行：工作树有 {len(decision.entries)} 处未提交改动。"
        "预检是**推送前门禁**，不承担开发中的反馈环。\n"
        f"  未提交条目：{sample}{more}\n"
        "  开发中改完即测请用开发环（只跑改动相关，秒级到十几秒）：\n"
        "    engkit run-changed\n"
        "  准备推送时：先提交，再跑预检（定向档 `--push`；无参默认 `--fast`）。\n"
        "  确需在脏树上验证护栏本身（仅快速档、会记入审计）：加 `--allow-dirty`"
        f"{scoped_hint}"
    )


def audit_path(cfg: Config) -> Path | None:
    """解析审计文件路径。

    Args:
        cfg: 仓配置。

    Returns:
        Path | None: 审计文件路径；配置为空串时返回 None（不记录）。
    """
    configured = cfg.preflight.gate.audit_file.strip()
    if not configured:
        return None
    path = Path(configured)
    return path if path.is_absolute() else cfg.root / path


def write(
    cfg: Config,
    *,
    event: str,
    mode: str,
    decision: Decision | None = None,
    duration_seconds: float | None = None,
    exit_code: int | None = None,
) -> None:
    """追加一条审计记录（失败 fail-open，不抛异常、不影响退出码）。

    Args:
        cfg: 仓配置。
        event: 事件类型（`run` / `rejected` / `bypass`）。
        mode: 档位（`fast` / `scoped` / `full`）。
        decision: 闸门判定（被拒与绕过时带脏条目）。
        duration_seconds: 真跑耗时（秒）。
        exit_code: 真跑退出码。
    """
    target = audit_path(cfg)
    if target is None:
        return
    entries = () if decision is None else decision.entries
    payload = {
        "ts": datetime.now().astimezone().isoformat(timespec="seconds"),
        "event": event,
        "mode": mode,
        "reason": "" if decision is None or not decision.blocked else decision.reason,
        "dirty": len(entries),
        "entries": list(entries[:_SAMPLE]),
        "bypassed": bool(decision is not None and decision.bypassed),
        "reliable": bool(decision is None or decision.reliable),
        "duration_seconds": None if duration_seconds is None else round(duration_seconds, 3),
        "exit_code": exit_code,
        "fingerprint": "" if event == EVENT_REJECTED else gitutil.fingerprint(cfg.root),
    }
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open("a", encoding="utf-8", newline="\n") as handle:
            handle.write(json.dumps(payload, ensure_ascii=False) + "\n")
    except OSError:
        return
