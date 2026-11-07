"""行尾护栏（`engkit eol`）：工作树里「应为 LF」的文本文件不得残留 CRLF。

判定口径全部来自仓库自身的 `.gitattributes`（`* text=auto eol=lf` 与 `*.bat text eol=crlf`
一类的例外），因此工具包内不出现任何仓库名或扩展名字面：哪些文件该是 LF、哪些该是 CRLF，
一律以 git 的属性解析结果为准（`git ls-files --eol`）。

背景：Windows 上 `Set-Content` / `Out-File` / Python `Path.write_text()` 默认写 CRLF，
AI 助手与脚本改完文件常常把 LF 悄悄变成 CRLF；而 `text=auto` 会在比对时把差异规范化掉，
`git status` 看着干净，问题因此长期潜伏。本护栏把「工作树实际字节」当作判据。

**失效即失败**：期望行尾来自 `.gitattributes`，仓内缺该文件（或其中没写 `eol=lf`）时每条记录
都判不出期望值，检查会「通过 0 违规」——护栏空转而不自知（实测某产品仓长期无 `.gitattributes`，
护栏一直报 0 违规）。故 `EolReport.ineffective` 成立时按配置错误退 2，把空转变显式失败。
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path

from .config import EolConfig

CRLF = b"\r\n"
"""CRLF 行尾字节。"""


@dataclass(frozen=True)
class EolFinding:
    """一处行尾违规。

    Attributes:
        file: 文件路径（相对仓库根，posix 形态）。
        actual: 实际行尾（`crlf` / `mixed`）。
        expected: 期望行尾（取自 `.gitattributes`）。
    """

    file: str
    actual: str
    expected: str


@dataclass(frozen=True)
class EolReport:
    """行尾检查结果。

    Attributes:
        files: 参与检查的文件数。
        findings: 违规清单。
        skipped: 因无 git 而跳过的说明（正常为 None）。
        uncovered: 仓内 `.gitattributes` 未给出期望行尾的文件数（判定口径缺失）。
    """

    files: int
    findings: tuple[EolFinding, ...]
    skipped: str | None = None
    uncovered: int = 0

    @property
    def ineffective(self) -> bool:
        """护栏是否失效（有文件但全无期望行尾）。

        为什么需要：期望行尾全部来自仓根 `.gitattributes`。仓内缺该文件（或未写 `eol=lf`）时，
        每条记录都判不出期望值，检查会「通过 0 违规」——护栏空转而不自知。此属性用于把
        空转变成显式失败。

        Returns:
            bool: 有文件参与且无一有期望值返回 True。
        """
        return self.files > 0 and self.uncovered >= self.files


def _git(root: Path, args: list[str]) -> subprocess.CompletedProcess[str] | None:
    """执行一条 git 命令。

    Args:
        root: 仓库根目录。
        args: git 子命令与参数。

    Returns:
        subprocess.CompletedProcess[str] | None: 执行结果；git 不可用时为 None。
    """
    try:
        return subprocess.run(
            ["git", "-c", "core.quotepath=false", *args],
            cwd=root,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
        )
    except OSError:  # pragma: no cover - 环境无 git
        return None


def _parse_eol_line(line: str) -> tuple[str, str, str] | None:
    """解析 `git ls-files --eol` 的一行。

    行形态：`i/<index>  w/<worktree>  attr/<attrs>\t<path>`。

    Args:
        line: 单行文本。

    Returns:
        tuple[str, str, str] | None: （工作树行尾, 期望行尾, 路径）；无法解析时为 None。
    """
    if "\t" not in line:
        return None
    head, path = line.split("\t", 1)
    fields = head.split()
    worktree = ""
    attrs: list[str] = []
    seen_attr = False
    for field in fields:
        if field.startswith("w/"):
            worktree = field[2:]
        elif field.startswith("attr/"):
            seen_attr = True
            attrs.append(field[5:])
        elif seen_attr:
            # 属性段可能被空白切成多个 token（attr/text=auto eol=lf），须续接
            attrs.append(field)
    if not worktree:
        return None
    joined = " ".join(attrs)
    expected = "lf" if "eol=lf" in joined else "crlf" if "eol=crlf" in joined else ""
    return worktree, expected, path.strip()


def _matches(path: str, cfg: EolConfig) -> bool:
    """判断路径是否纳入检查范围。

    Args:
        path: 相对仓库根的 posix 路径。
        cfg: 行尾配置。

    Returns:
        bool: 纳入返回 True。
    """
    if cfg.skip_prefixes and path.startswith(cfg.skip_prefixes):
        return False
    if cfg.include_suffixes:
        return path.endswith(cfg.include_suffixes)
    return True


def has_crlf(path: Path) -> bool:
    """文件是否含 CRLF 字节。

    Args:
        path: 文件路径。

    Returns:
        bool: 含 CRLF 返回 True（不可读时按 False 处理，交由其他检查负责）。
    """
    try:
        return CRLF in path.read_bytes()
    except OSError:  # pragma: no cover - 不可读由调用方兜底
        return False


def check(root: Path, cfg: EolConfig) -> EolReport:
    """执行行尾检查。

    Args:
        root: 仓库根目录。
        cfg: 行尾配置。

    Returns:
        EolReport: 检查结果。
    """
    result = _git(root, ["ls-files", "--eol"])
    if result is None or result.returncode != 0:
        return EolReport(files=0, findings=(), skipped="未找到 git，行尾检查跳过")
    files = 0
    uncovered = 0
    findings: list[EolFinding] = []
    for line in result.stdout.splitlines():
        parsed = _parse_eol_line(line)
        if parsed is None:
            continue
        worktree, expected, path = parsed
        if not _matches(path, cfg):
            continue
        files += 1
        if expected != "lf":
            if not expected:
                uncovered += 1
            continue
        if worktree in ("crlf", "mixed"):
            findings.append(EolFinding(file=path, actual=worktree, expected=expected))
    return EolReport(files=files, findings=tuple(findings), uncovered=uncovered)


def normalize(root: Path, report: EolReport) -> list[str]:
    """把违规文件的工作树字节改成 LF（写回同一文件）。

    Args:
        root: 仓库根目录。
        report: 检查结果。

    Returns:
        list[str]: 已修正的文件清单。
    """
    fixed: list[str] = []
    for item in report.findings:
        path = root / item.file
        try:
            data = path.read_bytes()
        except OSError:  # pragma: no cover - 不可读由调用方兜底
            continue
        path.write_bytes(data.replace(CRLF, b"\n"))
        fixed.append(item.file)
    return fixed


def staged_files(root: Path) -> list[str]:
    """取暂存区新增 / 修改的文件（相对仓根 posix 路径）。

    Args:
        root: 仓库根目录。

    Returns:
        list[str]: 路径清单（无 git 或命令失败时为空）。
    """
    result = _git(root, ["diff", "--cached", "--name-only", "--diff-filter=ACM"])
    if result is None or result.returncode != 0:
        return []
    return [line.strip() for line in result.stdout.splitlines() if line.strip()]


def fix_staged(root: Path, cfg: EolConfig) -> list[str]:
    """把**已暂存**文件的工作树字节改成 LF，并重新暂存（提交钩子用）。

    为什么需要：写侧默认值控制不住（Windows 上 `Path.write_text` / `Set-Content` / 各类编辑器
    都可能产 CRLF），但「进入提交的内容」必须恒为 LF。`.gitattributes` 的 clean 过滤器已能保证
    **只改行尾**时不污染历史，本函数进一步把工作树字节也纠正掉，使提交前门禁不必人工 `--fix`。

    只处理暂存清单内的文件，不触碰用户未暂存的改动。

    Args:
        root: 仓库根目录。
        cfg: 行尾配置。

    Returns:
        list[str]: 已修正并重新暂存的文件清单。
    """
    fixed: list[str] = []
    for rel in staged_files(root):
        if not _matches(rel, cfg):
            continue
        path = root / rel
        try:
            data = path.read_bytes()
        except OSError:  # pragma: no cover - 已删除 / 不可读由调用方兜底
            continue
        if CRLF not in data:
            continue
        path.write_bytes(data.replace(CRLF, b"\n"))
        _git(root, ["add", "--", rel])
        fixed.append(rel)
    return fixed
