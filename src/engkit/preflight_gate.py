"""预检的脏工作树闸门与调用审计。

预检是**推送的内部步骤**（`engkit push`），本地没有独立入口；本模块只做两件事：
① 把「工作树必须已提交」变成机器判定（推送前的工作树本就应当是干净的）；
② 记录每次真跑 / 被拒，一行一条 JSON，供事后复盘闸门是否有效（写盘失败 fail-open）。

CI 环境不设闸门（流水线里没有「开发中」这一场景）。
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

REASON_DIRTY = "dirty-tree"
"""拒绝原因：脏工作树。"""

_SAMPLE = 5
"""审计里保留的脏条目样例条数（完整清单在 git status 里可查，故不整份落盘）。"""


@dataclass(frozen=True)
class Decision:
    """闸门判定结果。

    Attributes:
        blocked: 是否拒绝执行。
        entries: 脏工作树条目（已跟踪改动 + 未跟踪文件）。
        reliable: git 是否可判定（不可判定时放行，不误拦）。
        reason: 拒绝原因（放行时为空串）。
    """

    blocked: bool
    entries: tuple[str, ...] = ()
    reliable: bool = True
    reason: str = ""


def evaluate(cfg: Config) -> Decision:
    """判定本次预检是否放行。

    Args:
        cfg: 仓配置。

    Returns:
        Decision: 判定结果（CI 环境、仓配置关闭闸门或 git 不可判定时一律放行）。
    """
    if not cfg.preflight.gate.dirty_tree or guard.is_ci():
        return Decision(blocked=False)
    entries, reliable = gitutil.worktree_dirty(cfg.root)
    if not entries:
        return Decision(blocked=False, reliable=reliable)
    return Decision(blocked=True, entries=tuple(entries), reliable=reliable, reason=REASON_DIRTY)


def guidance(decision: Decision) -> str:
    """生成拒绝提示（指向开发环与推送入口）。

    Args:
        decision: 判定结果。

    Returns:
        str: 提示文本。
    """
    sample = "".join(f"\n    - {item}" for item in decision.entries[:_SAMPLE])
    more = "" if len(decision.entries) <= _SAMPLE else f"\n    …（其余 {len(decision.entries) - _SAMPLE} 处从略）"
    return (
        f"[预检闸门] 拒绝执行：工作树有 {len(decision.entries)} 处未提交改动。"
        "预检是**推送的内部步骤**，不承担开发中的反馈环。\n"
        f"  未提交条目：{sample}{more}\n"
        "  开发中改完即测请用开发环（只跑改动相关，秒级到十几秒）：\n"
        "    engkit run-changed\n"
        "  准备推送时：先提交，再跑 `engkit push`（预检通过即自动推送）。"
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
        event: 事件类型（`run` / `rejected`）。
        mode: 档位（`fast` / `scoped` / `full`）。
        decision: 闸门判定（被拒时带脏条目）。
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
