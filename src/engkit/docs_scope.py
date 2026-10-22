"""文档口径护栏（`engkit docs-scope`）：进度类字段只允许出现在阶段计划与总体规划。

规则本身是通用的「进度类词面 + 数据形态」，不绑定任何仓库；白名单目录、白名单文件与
阶段计划目录形态由各仓配置声明。
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from .config import Config

DEFAULT_RULES: tuple[tuple[str, str], ...] = (
    (r"工时", "工时字样"),
    (r"排期", "排期字样"),
    (r"里程碑", "里程碑字样"),
    (r"甘特", "甘特字样"),
    (r"波次", "波次字样"),
    (r"工期", "工期字样"),
    (r"(?<![0-9A-Za-z])M\d{1,2}(?![0-9A-Za-z])", "里程碑编号"),
    # 工时数字：`{数字}h`；带比较符（≤ < ≥ > ～）的是时长约束（如 RTO ≤4h），不判工时。
    (r"(?<![\w.≤<≥>～~])\d+(?:\.\d+)?h(?![0-9A-Za-z])", "工时数字"),
)
"""内置规则：（正则, 违规类型）。"""


@dataclass(frozen=True)
class ScopeFinding:
    """一条口径违规。

    Attributes:
        file: 文档路径（相对文档根）。
        line: 行号（1 起）。
        label: 违规类型。
        snippet: 命中片段。
    """

    file: str
    line: int
    label: str
    snippet: str


@dataclass(frozen=True)
class ScopeReport:
    """口径检查结果。

    Attributes:
        files: 参与检查的文档数。
        findings: 违规清单。
    """

    files: int
    findings: tuple[ScopeFinding, ...]


def _compile(cfg: Config) -> tuple[tuple[re.Pattern[str], str], ...]:
    """编译内置规则与配置追加规则。

    Args:
        cfg: 仓配置。

    Returns:
        tuple[tuple[re.Pattern[str], str], ...]: 编译后的规则。
    """
    rules = [(re.compile(pattern), label) for pattern, label in DEFAULT_RULES]
    rules.extend((re.compile(rule.pattern), rule.label) for rule in cfg.docs.scope.extra_rules)
    return tuple(rules)


def is_whitelisted(rel: str, cfg: Config) -> bool:
    """判断文档根下的相对路径是否在白名单内（可不写进度类字段）。

    Args:
        rel: 相对文档根的 posix 路径。
        cfg: 仓配置。

    Returns:
        bool: 白名单内返回 True。
    """
    scope = cfg.docs.scope
    if rel in scope.whitelist_files:
        return True
    if scope.whitelist_dirs and rel.startswith(scope.whitelist_dirs):
        return True
    return bool(scope.plan_dir_pattern) and re.match(scope.plan_dir_pattern, rel) is not None


def scan_text(text: str, cfg: Config) -> tuple[ScopeFinding, ...]:
    """扫描单份文档正文（不含文件名与白名单判定）。

    Args:
        text: 文档正文。
        cfg: 仓配置。

    Returns:
        tuple[ScopeFinding, ...]: 违规清单（file 字段为空串）。
    """
    marker = cfg.docs.scope.allow_marker
    rules = _compile(cfg)
    findings: list[ScopeFinding] = []
    for number, line in enumerate(text.splitlines(), start=1):
        if marker and marker in line:
            continue
        for pattern, label in rules:
            match = pattern.search(line)
            if match is not None:
                findings.append(ScopeFinding(file="", line=number, label=label, snippet=match.group(0)))
                break
    return tuple(findings)


def check(cfg: Config) -> ScopeReport:
    """执行全量口径检查。

    Args:
        cfg: 仓配置。

    Returns:
        ScopeReport: 检查结果。

    Raises:
        FileNotFoundError: 文档根不存在。
    """
    docs_root = cfg.docs_root
    if not docs_root.is_dir():
        raise FileNotFoundError(f"未找到文档根目录：{docs_root}")
    files = 0
    findings: list[ScopeFinding] = []
    for path in sorted(docs_root.rglob("*.md")):
        rel = path.relative_to(docs_root).as_posix()
        if is_whitelisted(rel, cfg):
            continue
        files += 1
        for item in scan_text(path.read_text(encoding="utf-8", errors="ignore"), cfg):
            findings.append(ScopeFinding(file=rel, line=item.line, label=item.label, snippet=item.snippet))
    return ScopeReport(files=files, findings=tuple(findings))
