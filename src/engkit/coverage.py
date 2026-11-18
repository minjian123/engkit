"""覆盖率门禁（`engkit coverage`）：合并并行采集数据、导出 JSON 并判定「行 + 分支」。

采集侧由各仓的 pytest 插件（在**被测模块导入前**启动采集）按 pid 各落一份
`<data-file>.<pid>`；本模块负责合并、导出与判定。门禁口径：默认 100%（行 + 分支），
低于 100 时按合并百分比判定。
"""

from __future__ import annotations

import contextlib
import json
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

SOURCE_MARKER = "src"
"""模块名归一的路径标记（`…/src/<包>/…` 之后的部分即模块路径）。"""

_HUNK = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@")
"""unified diff 的 hunk 头（取新文件起始行与行数）。"""

_TEST_SEGMENTS = ("/tests/", "/test/")
"""测试目录不参与改动行判定（它们本身不是被测对象）。"""

REPORT_NAME = "report.json"
"""门禁报告落点名（与数据文件同目录）。

**不得**取 `<data-file>.<后缀>` 形态：`coverage` 库 `combine()` 会把数据文件同目录下
`<data-file>.*` 的文件都当作待合并分片，报告落在该命名空间会触发
`CoverageWarning: Couldn't use data file … file is not a database`，并在「只剩报告」时
被误判成「已采集到数据」。
"""

GAPS_TEXT_NAME = "gaps.txt"
"""缺口明细（人读）落点名：与门禁报告同目录。"""

GAPS_JSON_NAME = "gaps.json"
"""缺口明细（结构化）落点名：逐文件给出行号与分支，供批量补测直接消费。"""

TARGET_OK = "ok"
"""目标文件状态：已采集且行 + 分支达标。"""

TARGET_GAP = "gap"
"""目标文件状态：已采集但存在缺行 / 缺分支。"""

TARGET_MISSING = "missing"
"""目标文件状态：报告里没有该文件（无同名测试或本轮未跑到）。"""


@dataclass(frozen=True)
class ModuleGap:
    """单模块覆盖率缺口（分钟级定位用：缺哪些行、哪些分支没走全）。

    Attributes:
        module: 模块名（`src` 之后的点分路径）。
        lines: 未覆盖行号（升序）。
        branches: 未走全的分支（`from->to`；漏异常分支的 `to` 记为 `?`）。
    """

    module: str
    lines: tuple[int, ...]
    branches: tuple[str, ...]


@dataclass(frozen=True)
class FileGap:
    """单个目标文件的覆盖率结论（按文件清单批量判定时使用）。

    Attributes:
        path: 仓根相对路径（posix 形态）。
        module: 点分模块名（`src` 之后的部分）。
        status: `ok` / `gap` / `missing`（见 `TARGET_*`）。
        lines: 未覆盖行号（升序）。
        branches: 未走全的分支（`from->to`）。
    """

    path: str
    module: str
    status: str
    lines: tuple[int, ...] = ()
    branches: tuple[str, ...] = ()


@dataclass(frozen=True)
class IgnoredTarget:
    """不参与覆盖率判定的文件（显式列出，避免静默丢弃）。

    Attributes:
        path: 仓根相对路径（posix 形态）。
        reason: 不参与的原因（测试文件 / 非 Python / 不在 `src` 下）。
    """

    path: str
    reason: str


@dataclass(frozen=True)
class Totals:
    """覆盖率汇总（按门禁范围过滤后）。

    Attributes:
        files: 参与判定的文件数。
        missing_lines: 未覆盖行数。
        partial_branches: 未走全的分支数。
        percent: 行 + 分支合并百分比（0~100）。
        missing_modules: 存在缺行 / 缺分支的模块名（供扩圈）。
        gaps: 逐模块缺口明细（未达标时逐条打印，便于直接定位）。
    """

    files: int
    missing_lines: int
    partial_branches: int
    percent: float
    missing_modules: tuple[str, ...]
    gaps: tuple[ModuleGap, ...] = ()

    @property
    def passed(self) -> bool:
        """是否达标（100% 口径：无缺行、无缺分支、至少匹配到一个文件）。"""
        return self.files > 0 and self.missing_lines == 0 and self.partial_branches == 0


def data_files(data_file: Path) -> tuple[Path, ...]:
    """列出待合并的采集文件（`<data-file>` 与 `<data-file>.<pid>` 分片）。

    只认**纯数字后缀**的分片（采集插件按进程号落盘），同目录的其它同名文件
    （如门禁报告 `report.json` 的历史命名 `<data-file>.json`）不参与合并。

    Args:
        data_file: 数据文件基址。

    Returns:
        tuple[Path, ...]: 存在的采集文件（基址在前，分片按名称排序）。
    """
    prefix = f"{data_file.name}."
    shards = tuple(
        path
        for path in sorted(data_file.parent.glob(f"{data_file.name}.*"))
        if path.is_file() and (suffix := path.name.removeprefix(prefix)).isascii() and suffix.isdigit()
    )
    base = (data_file,) if data_file.is_file() else ()
    return (*base, *shards)


def report_path(data_file: Path) -> Path:
    """门禁报告的落点（与数据文件同目录，且不落入分片通配命名空间）。

    Args:
        data_file: 数据文件基址。

    Returns:
        Path: 报告文件路径。
    """
    return data_file.parent / REPORT_NAME


def cleanup(data_file: Path) -> None:
    """清理上一轮的采集文件。

    同时删除历史命名 `<data-file>.json` 的报告残留：该名字落在分片通配命名空间内，
    留着会让 `coverage` 库合并时报警。

    Args:
        data_file: 数据文件基址。
    """
    legacy = data_file.parent / f"{data_file.name}.json"
    for path in (*data_files(data_file), legacy):
        with contextlib.suppress(OSError):
            path.unlink()


def ensure_data_dir(data_file: Path) -> Path:
    """确保数据文件所在目录存在（落点可在尚未创建的缓存目录下，如 `.cache/coverage/`）。

    Args:
        data_file: 数据文件基址。

    Returns:
        Path: 数据文件的父目录。
    """
    parent = data_file.parent
    parent.mkdir(parents=True, exist_ok=True)
    return parent


def _coverage_module() -> Any:
    """导入 coverage 库。

    Returns:
        Any: coverage 模块。

    Raises:
        RuntimeError: 运行环境缺 coverage（应由被测仓的 venv 提供）。
    """
    try:
        import coverage
    except ModuleNotFoundError as error:  # pragma: no cover - 环境缺失时的显式报错
        raise RuntimeError("缺少 coverage：请用被测仓的 venv 运行") from error
    return coverage


def combine(data_file: Path) -> int:
    """合并并行 worker 的采集文件。

    Args:
        data_file: 数据文件基址。

    Returns:
        int: 合并的采集文件数（0 表示没采集到数据）。
    """
    ensure_data_dir(data_file)
    collected = data_files(data_file)
    if not collected:
        return 0
    instance = _coverage_module().Coverage(data_file=str(data_file))
    instance.combine()
    instance.save()
    return len(collected)


def export_json(data_file: Path, out: Path) -> Any:
    """把合并后的数据导出为 JSON 报告。

    Args:
        data_file: 数据文件基址。
        out: JSON 报告落点。

    Returns:
        Any: 报告内容（`totals` + `files`）。
    """
    out.parent.mkdir(parents=True, exist_ok=True)
    instance = _coverage_module().Coverage(data_file=str(data_file))
    instance.load()
    instance.json_report(outfile=str(out))
    return json.loads(out.read_text(encoding="utf-8"))


def module_matches(path: str, modules: tuple[str, ...] | list[str]) -> bool:
    """判断报告文件是否属于给定模块（`a.b.c` ⇒ `a/b/c.py` 或 `a/b/c/__init__.py`，或整包）。

    Args:
        path: 报告里的文件路径。
        modules: 门禁范围模块；空表示全部。

    Returns:
        bool: 属于门禁范围返回 True。
    """
    if not modules:
        return True
    normal = path.replace("\\", "/")
    for module in modules:
        suffix = module.replace(".", "/")
        if normal.endswith(f"{suffix}.py") or f"/{suffix}/" in f"{normal}/":
            return True
    return False


def _path_matches(path: str, paths: tuple[str, ...] | list[str]) -> bool:
    """判断报告文件是否命中给定的仓根相对路径（文件级精确匹配）。

    Args:
        path: 报告里的文件路径。
        paths: 仓根相对路径清单；空表示不按路径过滤。

    Returns:
        bool: 命中任一文件返回 True。
    """
    if not paths:
        return False
    normal = path.replace("\\", "/")
    return any(normal.endswith(item) for item in paths)


def _in_scope(path: str, modules: tuple[str, ...] | list[str], paths: tuple[str, ...] | list[str]) -> bool:
    """判定报告文件是否在门禁范围内（模块前缀 ∪ 文件路径）。

    Args:
        path: 报告里的文件路径。
        modules: 模块范围（空表示不限）。
        paths: 文件范围（空表示不限）。

    Returns:
        bool: 在范围内返回 True。
    """
    if not modules and not paths:
        return True
    if modules and module_matches(path, modules):
        return True
    return _path_matches(path, paths)


def target_files(
    files: tuple[str, ...] | list[str],
) -> tuple[tuple[str, ...], tuple[IgnoredTarget, ...]]:
    """把文件清单划分为覆盖率判定目标与「不参与」项。

    批量查改动文件时，测试文件、非 Python、`src` 之外的路径都不参与判定——但**必须显式列出**，
    否则「没查到」会被误读成「已达标」。

    Args:
        files: 仓根相对路径清单（可含重复与空项）。

    Returns:
        tuple[tuple[str, ...], tuple[IgnoredTarget, ...]]: （判定目标, 不参与项）。
    """
    targets: list[str] = []
    ignored: list[IgnoredTarget] = []
    seen: set[str] = set()
    for raw in files:
        rel = str(raw).strip().replace("\\", "/")
        if not rel or rel in seen:
            continue
        seen.add(rel)
        if not rel.endswith(".py"):
            ignored.append(IgnoredTarget(rel, "非 Python 源文件"))
            continue
        if any(segment in f"/{rel}" for segment in _TEST_SEGMENTS):
            ignored.append(IgnoredTarget(rel, "测试文件不参与覆盖率"))
            continue
        if "/src/" not in f"/{rel}":
            ignored.append(IgnoredTarget(rel, "不在 src 下（配置 / 迁移 / 运维脚本）"))
            continue
        targets.append(rel)
    return tuple(targets), tuple(ignored)


def _entry_for(table: Any, relative: str) -> Any:
    """在报告文件表里按仓根相对路径找条目（兼容报告路径为绝对 / 反斜杠形态）。

    Args:
        table: 报告里的 `files` 表。
        relative: 仓根相对路径。

    Returns:
        Any: 报告条目；找不到返回 None。
    """
    wanted = relative.replace("\\", "/")
    for path, entry in table.items():
        if not isinstance(entry, dict):
            continue
        if str(path).replace("\\", "/").endswith(wanted):
            return entry
    return None


def file_gaps(payload: Any, targets: tuple[str, ...] | list[str]) -> tuple[FileGap, ...]:
    """逐文件给出覆盖率结论（达标 / 缺口 / 未采集）。

    Args:
        payload: `export_json` 的报告内容。
        targets: 判定目标文件（仓根相对）。

    Returns:
        tuple[FileGap, ...]: 与 `targets` 同序的结论。
    """
    table = payload.get("files") if isinstance(payload, dict) else None
    entries = table if isinstance(table, dict) else {}
    result: list[FileGap] = []
    for rel in targets:
        entry = _entry_for(entries, rel)
        if entry is None:
            result.append(FileGap(rel, module_name(rel), TARGET_MISSING))
            continue
        summary = entry.get("summary") or {}
        lines = tuple(int(number) for number in (entry.get("missing_lines") or []))
        branches = tuple(_format_branch(pair) for pair in (entry.get("missing_branches") or []))
        missing = int(summary.get("missing_lines") or 0)
        partial = int(summary.get("num_partial_branches") or 0)
        result.append(
            FileGap(rel, module_name(rel), TARGET_GAP if (missing or partial) else TARGET_OK, lines, branches)
        )
    return tuple(result)


def module_name(path: str) -> str:
    """把报告路径归一为模块名（`…/src/<包>/db/keys.py` ⇒ `<包>.db.keys`）。

    Args:
        path: 报告里的文件路径。

    Returns:
        str: 模块名。
    """
    parts = path.replace("\\", "/").split("/")
    if SOURCE_MARKER in parts:
        parts = parts[parts.index(SOURCE_MARKER) + 1 :]
    if parts and parts[-1] == "__init__.py":
        parts = parts[:-1]
    elif parts:
        parts[-1] = parts[-1].removesuffix(".py")
    return ".".join(parts)


def filtered_totals(
    payload: Any,
    modules: tuple[str, ...] | list[str],
    paths: tuple[str, ...] | list[str] = (),
) -> Totals:
    """按门禁范围汇总覆盖率。

    Args:
        payload: `export_json` 的报告内容。
        modules: 门禁范围模块（空表示不限）。
        paths: 门禁范围文件（仓根相对；空表示不限）。与 `modules` 取并集。

    Returns:
        Totals: 汇总结果。
    """
    files = payload.get("files") or {}
    matched = 0
    missing_lines = 0
    partial = 0
    covered = 0
    statements = 0
    missing_modules: list[str] = []
    gaps: list[ModuleGap] = []
    for path, entry in files.items():
        if not _in_scope(str(path), modules, paths):
            continue
        summary = entry.get("summary") or {}
        matched += 1
        lines_missing = int(summary.get("missing_lines") or 0)
        branches_partial = int(summary.get("num_partial_branches") or 0)
        missing_lines += lines_missing
        partial += branches_partial
        statements += int(summary.get("num_statements") or 0)
        covered += int(summary.get("covered_lines") or 0)
        if lines_missing or branches_partial:
            name = module_name(str(path))
            missing_modules.append(name)
            gaps.append(
                ModuleGap(
                    module=name,
                    lines=tuple(int(number) for number in (entry.get("missing_lines") or [])),
                    branches=tuple(_format_branch(pair) for pair in (entry.get("missing_branches") or [])),
                )
            )
    percent = (covered / statements * 100.0) if statements else 0.0
    return Totals(
        files=matched,
        missing_lines=missing_lines,
        partial_branches=partial,
        percent=percent,
        missing_modules=tuple(missing_modules),
        gaps=tuple(gaps),
    )


def _format_branch(pair: Any) -> str:
    """把 coverage 的分支对格式化为 `from->to`（漏异常分支时 `to` 记为 `?`）。

    Args:
        pair: 分支对（`[from, to]`，`to` 可能为 None）。

    Returns:
        str: 可读分支描述。
    """
    if not isinstance(pair, list | tuple) or len(pair) != 2:
        return "?"
    start, end = pair
    return f"{start}->{'?' if end is None else end}"


def _clip(items: tuple[Any, ...], limit: int = 20) -> str:
    """截取明细前缀（超出部分给出总数，避免刷屏）。

    Args:
        items: 待展示明细。
        limit: 最多展示条数。

    Returns:
        str: 展示文本。
    """
    shown = "、".join(str(item) for item in items[:limit])
    if len(items) <= limit:
        return shown
    return f"{shown} …（共 {len(items)} 处）"


def describe(totals: Totals) -> str:
    """门禁结果的单行度量（供日志采集）。

    Args:
        totals: 汇总结果。

    Returns:
        str: 度量行。
    """
    return (
        f"[coverage] files={totals.files} missing_lines={totals.missing_lines} "
        f"partial_branches={totals.partial_branches} percent={totals.percent:.2f}"
    )


def _target_summary(entries: tuple[FileGap, ...], ignored: tuple[IgnoredTarget, ...]) -> list[str]:
    """生成按文件判定的完整明细（达标计数 + 逐文件缺口 + 未采集 + 不参与项）。

    Args:
        entries: 逐文件结论。
        ignored: 不参与判定的文件。

    Returns:
        list[str]: 输出行（调用方统一打到 stderr）。
    """
    ok = sum(1 for entry in entries if entry.status == TARGET_OK)
    gap = sum(1 for entry in entries if entry.status == TARGET_GAP)
    missing = sum(1 for entry in entries if entry.status == TARGET_MISSING)
    lines = [f"[coverage] 目标文件：{len(entries)}（达标 {ok} / 缺口 {gap} / 未采集 {missing}）"]
    if ignored:
        lines.append(f"[coverage] 不参与判定：{len(ignored)} 个")
        for item in ignored:
            lines.append(f"[coverage]   - {item.path}：{item.reason}")
    for entry in entries:
        if entry.status != TARGET_GAP:
            continue
        parts: list[str] = []
        if entry.lines:
            parts.append(f"缺行 {len(entry.lines)}（{_join(entry.lines)}）")
        if entry.branches:
            parts.append(f"缺分支 {len(entry.branches)}（{_join(entry.branches)}）")
        lines.append(f"[coverage]   ✗ {entry.path}（{entry.module}）：{'，'.join(parts)}")
    for entry in entries:
        if entry.status == TARGET_MISSING:
            lines.append(f"[coverage]   ? {entry.path}：未采集（无同名测试或本轮未跑到）")
    return lines


def _join(items: tuple[Any, ...]) -> str:
    """完整拼接明细（不截断：批量补测需要全部行号）。

    Args:
        items: 待展示明细。

    Returns:
        str: 以「、」分隔的完整文本。
    """
    return "、".join(str(item) for item in items)


def write_gaps(
    path: Path,
    totals: Totals,
    entries: tuple[FileGap, ...],
    ignored: tuple[IgnoredTarget, ...],
    text: str,
) -> None:
    """把缺口明细落盘：人读文本 + 结构化 JSON（同目录 `gaps.json`）。

    Args:
        path: 文本明细落点（`<目录>/gaps.txt`）。
        totals: 汇总结果。
        entries: 逐文件结论。
        ignored: 不参与判定的文件。
        text: 终端输出的完整正文。
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text + "\n", encoding="utf-8", newline="\n")
    payload = {
        "totals": {
            "files": totals.files,
            "missing_lines": totals.missing_lines,
            "partial_branches": totals.partial_branches,
            "percent": round(totals.percent, 2),
        },
        "files": [
            {
                "path": entry.path,
                "module": entry.module,
                "status": entry.status,
                "missing_lines": list(entry.lines),
                "missing_branches": list(entry.branches),
            }
            for entry in entries
        ],
        "ignored": [{"path": item.path, "reason": item.reason} for item in ignored],
    }
    json_path = path.with_name(GAPS_JSON_NAME)
    json_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
        newline="\n",
    )


def check(
    data_file: Path,
    out: Path,
    modules: tuple[str, ...] | list[str] = (),
    *,
    fail_under: float = 100,
    quiet: bool = False,
    paths: tuple[str, ...] | list[str] = (),
    gaps_out: Path | None = None,
) -> int:
    """合并 → 导出 → 判定。

    Args:
        data_file: 数据文件基址。
        out: JSON 报告落点。
        modules: 门禁范围模块（空表示全部）。
        fail_under: 门禁阈值（100 时按「行 + 分支全达标」判定，否则按合并百分比）。
        quiet: 是否抑制度量行输出。
        paths: 门禁范围文件（仓根相对；与 `modules` 取并集，按文件精确匹配）。
        gaps_out: 缺口明细落点（人读文本；同目录另写 `gaps.json`）；None 表示不落盘。

    Returns:
        int: 退出码（0 达标 / 1 未达标）。
    """
    module_tuple = tuple(modules)
    targets, ignored = target_files(paths)
    if combine(data_file) == 0:
        print(f"[coverage] 未采集到覆盖率数据（{data_file}）：不支持跳过，视为未达标", file=sys.stderr)
        return 1
    payload = export_json(data_file, out)
    if paths and not targets:
        # 批量查改动文件时，清单可能全是测试 / 配置 / 文档：必须显式列出「不参与」，
        # 且**不得**退化成「判全仓」——那会把无关模块的缺口误当成本次改动的缺口。
        lines = [f"[coverage] 本次改动无可判文件：{len(ignored)} 个均不参与覆盖率判定"]
        for item in ignored:
            lines.append(f"[coverage]   - {item.path}：{item.reason}")
        for line in lines:
            print(line, file=sys.stderr)
        if gaps_out is not None:
            write_gaps(gaps_out, Totals(0, 0, 0, 0.0, ()), (), ignored, "\n".join(lines))
        return 0
    totals = filtered_totals(payload, module_tuple, targets)
    entries = file_gaps(payload, targets)
    missing = tuple(entry for entry in entries if entry.status == TARGET_MISSING)
    if not quiet:
        print(describe(totals))
    if totals.files == 0 and not targets:
        print(f"[coverage] 门禁范围未匹配到任何文件：{'、'.join(module_tuple) or '（全部）'}", file=sys.stderr)
        return 1
    lines: list[str] = []
    if entries:
        lines.extend(_target_summary(entries, ignored))
    if fail_under >= 100:
        failed = not totals.passed or bool(missing)
        if not totals.passed and not entries:
            lines.append(
                f"[coverage] 未达标：缺行 {totals.missing_lines} / 缺分支 {totals.partial_branches}"
                f"（{'、'.join(totals.missing_modules[:10])}）"
            )
            for gap in totals.gaps[:20]:
                parts: list[str] = []
                if gap.lines:
                    parts.append(f"缺行 {len(gap.lines)}（{_clip(gap.lines)}）")
                if gap.branches:
                    parts.append(f"缺分支 {len(gap.branches)}（{_clip(gap.branches)}）")
                lines.append(f"[coverage]   {gap.module}: {'，'.join(parts)}")
        elif not totals.passed:
            lines.append(
                f"[coverage] 未达标：缺行 {totals.missing_lines} / 缺分支 {totals.partial_branches}（逐文件明细见上）"
            )
        if missing:
            lines.append(f"[coverage] 未采集：{len(missing)} 个目标文件不在本轮报告里（补测试或重新采集后再查）")
    else:
        failed = totals.percent + 1e-9 < fail_under or bool(missing)
        if failed:
            lines.append(f"[coverage] 未达标：{totals.percent:.2f}% < {fail_under}%")
    for line in lines:
        print(line, file=sys.stderr)
    if gaps_out is not None:
        write_gaps(gaps_out, totals, entries, ignored, "\n".join([describe(totals), *lines]))
    return 1 if failed else 0


def changed_lines(root: Path, relative: str) -> tuple[int, ...]:
    """取暂存区里某文件**新增 / 修改**的行号（删除行不参与）。

    Args:
        root: 仓库根（git 命令工作目录）。
        relative: 仓根相对路径。

    Returns:
        tuple[int, ...]: 新文件行号（升序）。
    """
    completed = subprocess.run(
        ["git", "diff", "--cached", "-U0", "--", relative],
        cwd=root,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if completed.returncode != 0:
        return ()
    lines: set[int] = set()
    for raw in completed.stdout.splitlines():
        match = _HUNK.match(raw)
        if match is None:
            continue
        start = int(match.group(1))
        count = 1 if match.group(2) is None else int(match.group(2))
        for offset in range(count):
            lines.add(start + offset)
    return tuple(sorted(lines))


def statements_of(payload: Any, relative: str) -> tuple[set[int], set[int]] | None:
    """从覆盖率报告里取某文件的可执行语句行与未执行行。

    可执行行 = `executed_lines ∪ missing_lines`；改动行若两者都不是（注释 / 空行 / 文档串），
    说明它根本不可执行，不参与判定。

    Args:
        payload: `export_json` 的报告内容。
        relative: 仓根相对路径。

    Returns:
        tuple[set[int], set[int]] | None: （可执行行, 未执行行）；文件未被采集返回 None。
    """
    files = payload.get("files") if isinstance(payload, dict) else None
    if not isinstance(files, dict):
        return None
    wanted = relative.replace("\\", "/")
    for path, entry in files.items():
        if not str(path).replace("\\", "/").endswith(wanted) or not isinstance(entry, dict):
            continue
        executed = entry.get("executed_lines")
        missing = entry.get("missing_lines")
        if not isinstance(executed, list) or not isinstance(missing, list):
            return None
        statements = {int(number) for number in [*executed, *missing]}
        return statements, {int(number) for number in missing}
    return None


def check_changed(
    root: Path,
    data_file: Path,
    out: Path,
    files: tuple[str, ...],
    *,
    quiet: bool = False,
) -> int:
    """改动行覆盖率门禁：只要求「本次改动的可执行行」被本轮测试执行。

    与模块级 `check` 的分工：`check` 要求整模块 100%（推送环 / CI 用），`check_changed`
    只判 diff 触达的可执行行（提交环用）——改一行不必为整文件补齐用例。

    Args:
        root: 仓库根（git 命令工作目录）。
        data_file: 覆盖率数据文件基址。
        out: JSON 报告落点。
        files: 改动文件（仓根相对）。
        quiet: 是否抑制通过时的度量输出。

    Returns:
        int: 退出码（0 达标 / 1 存在未覆盖的改动行 / 2 未采集到数据）。
    """
    if combine(data_file) == 0:
        print(f"[diff-coverage] 未采集到覆盖率数据（{data_file}）：不支持跳过，视为未达标", file=sys.stderr)
        return 2
    payload = export_json(data_file, out)
    gaps: dict[str, tuple[int, ...]] = {}
    checked = 0
    skipped = 0
    for relative in files:
        if not relative.endswith(".py"):
            continue
        if any(segment in f"/{relative}" for segment in _TEST_SEGMENTS):
            continue
        probe = statements_of(payload, relative)
        if probe is None:
            skipped += 1
            continue
        statements, missing = probe
        checked += 1
        uncovered = tuple(
            number for number in changed_lines(root, relative) if number in statements and number in missing
        )
        if uncovered:
            gaps[relative] = uncovered
    if skipped:
        print(f"[diff-coverage] 跳过 {skipped} 个未采集文件（无同名测试，交推送环）")
    if not checked:
        if not quiet:
            print("[diff-coverage] 无可判文件：本轮改动的源文件均未被覆盖率采集")
        return 0
    if not gaps:
        if not quiet:
            print(f"[diff-coverage] 通过：{checked} 个文件的改动行均已被执行")
        return 0
    print("[diff-coverage] 未通过：以下文件的改动行未被本轮测试执行", file=sys.stderr)
    for relative, numbers in gaps.items():
        shown = "、".join(str(number) for number in numbers[:20])
        suffix = "" if len(numbers) <= 20 else f" …（共 {len(numbers)} 行）"
        print(f"[diff-coverage]   {relative}: {shown}{suffix}", file=sys.stderr)
    return 1
