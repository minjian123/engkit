"""文档链接与锚点校验（`engkit links`）。

口径：扫描文档根下全部 `.md` 的相对链接——目标文件存在、锚点在目标内以 `id="…"` 出现。
跳过外部链接、行内代码与配置声明的本地凭据目录（那类文档不入库，存在与否不代表断链）。
链接目标按 URL 编码解码后再比较（`%20` 等是 Markdown 合法写法，目录名含空格时必须解码）。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import unquote

from .config import Config

LINK_RE = re.compile(r"\]\(([^)\s]+)\)")
"""Markdown 行内链接：`](目标)`。"""

CODE_RE = re.compile(r"`[^`]*`")
"""行内代码（其中的 `](…)` 不参与校验）。"""

_IGNORED_SCHEMES = ("http", "mailto", "data:")
"""外部链接前缀（不校验）。"""


@dataclass(frozen=True)
class LinkFinding:
    """一条链接问题。

    Attributes:
        file: 文档路径（相对仓库根）。
        line: 行号（1 起）。
        target: 链接原文。
        kind: `断链` 或 `失效锚点`。
    """

    file: str
    line: int
    target: str
    kind: str


@dataclass(frozen=True)
class LinkReport:
    """链接校验结果。

    Attributes:
        files: 参与校验的文档数。
        broken: 断链数。
        anchors: 失效锚点数。
        findings: 问题清单。
    """

    files: int
    broken: int
    anchors: int
    findings: tuple[LinkFinding, ...]


def strip_code(line: str) -> str:
    """抹掉行内代码（用等长空格占位，保持列位置不变）。

    Args:
        line: 单行文本。

    Returns:
        str: 抹掉行内代码后的文本。
    """
    return CODE_RE.sub(lambda match: " " * len(match.group(0)), line)


def _is_skipped(rel: str, skip: tuple[str, ...]) -> bool:
    """路径是否属于不参与校验的前缀。

    Args:
        rel: 相对仓库根的路径。
        skip: 不参与校验的前缀。

    Returns:
        bool: 跳过返回 True。
    """
    return any(rel.startswith(prefix) for prefix in skip)


def check(cfg: Config) -> LinkReport:
    """执行链接校验。

    Args:
        cfg: 仓配置。

    Returns:
        LinkReport: 校验结果。

    Raises:
        FileNotFoundError: 文档根不存在。
    """
    docs_root = cfg.docs_root
    if not docs_root.is_dir():
        raise FileNotFoundError(f"未找到文档根目录：{docs_root}")
    files = 0
    broken = 0
    anchors = 0
    findings: list[LinkFinding] = []
    for path in sorted(docs_root.rglob("*.md")):
        if ".git" in path.parts:
            continue
        rel_file = path.relative_to(cfg.root).as_posix()
        if _is_skipped(rel_file, cfg.docs.skip):
            continue
        files += 1
        for number, line in enumerate(path.read_text(encoding="utf-8", errors="ignore").splitlines(), start=1):
            for match in LINK_RE.finditer(strip_code(line)):
                target = match.group(1).strip()
                if not target or target.startswith(_IGNORED_SCHEMES):
                    continue
                rel_path, _, anchor = target.partition("#")
                # URL 编码解码：目录名含空格 / 中文时链接写作 `a%20b`，按编码比较会全部误判断链
                rel_path = unquote(rel_path).lstrip("?")
                anchor = unquote(anchor)
                if not rel_path:
                    continue
                resolved = (path.parent / rel_path).resolve()
                rel_target = _relative_to(resolved, cfg.root)
                if rel_target is not None and _is_skipped(rel_target, cfg.docs.skip):
                    continue
                if not resolved.exists():
                    broken += 1
                    findings.append(LinkFinding(rel_file, number, target, "断链"))
                    continue
                if anchor and resolved.suffix == ".md" and not _has_anchor(resolved, anchor):
                    anchors += 1
                    findings.append(LinkFinding(rel_file, number, target, "失效锚点"))
    return LinkReport(files=files, broken=broken, anchors=anchors, findings=tuple(findings))


def _relative_to(path: Path, root: Path) -> str | None:
    """路径相对仓库根的 posix 形态（不在仓库内返回 None）。

    Args:
        path: 目标路径。
        root: 仓库根。

    Returns:
        str | None: 相对路径。
    """
    try:
        return path.relative_to(root.resolve()).as_posix()
    except ValueError:
        return None


def _has_anchor(path: Path, anchor: str) -> bool:
    """目标文档是否存在该锚点（以 `id="锚点"` 形式出现）。

    Args:
        path: 目标文档。
        anchor: 锚点名。

    Returns:
        bool: 存在返回 True。
    """
    return f'id="{anchor}"' in path.read_text(encoding="utf-8", errors="ignore")
