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
    """列出待合并的采集文件（`<data-file>` 与 `<data-file>.<pid>`）。

    Args:
        data_file: 数据文件基址。

    Returns:
        tuple[Path, ...]: 存在的采集文件（按名称排序）。
    """
    candidates = [data_file, *sorted(data_file.parent.glob(f"{data_file.name}.*"))]
    return tuple(path for path in candidates if path.is_file())


def cleanup(data_file: Path) -> None:
    """清理上一轮的采集文件。

    Args:
        data_file: 数据文件基址。
    """
    for path in data_files(data_file):
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


def filtered_totals(payload: Any, modules: tuple[str, ...] | list[str]) -> Totals:
    """按门禁范围汇总覆盖率。

    Args:
        payload: `export_json` 的报告内容。
        modules: 门禁范围模块（空表示全部）。

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
        if not module_matches(str(path), modules):
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


def check(
    data_file: Path,
    out: Path,
    modules: tuple[str, ...] | list[str] = (),
    *,
    fail_under: float = 100,
    quiet: bool = False,
) -> int:
    """合并 → 导出 → 判定。

    Args:
        data_file: 数据文件基址。
        out: JSON 报告落点。
        modules: 门禁范围模块（空表示全部）。
        fail_under: 门禁阈值（100 时按「行 + 分支全达标」判定，否则按合并百分比）。
        quiet: 是否抑制度量行输出。

    Returns:
        int: 退出码（0 达标 / 1 未达标）。
    """
    module_tuple = tuple(modules)
    if combine(data_file) == 0:
        print(f"[coverage] 未采集到覆盖率数据（{data_file}）：不支持跳过，视为未达标", file=sys.stderr)
        return 1
    totals = filtered_totals(export_json(data_file, out), module_tuple)
    if not quiet:
        print(describe(totals))
    if totals.files == 0:
        print(f"[coverage] 门禁范围未匹配到任何文件：{'、'.join(module_tuple) or '（全部）'}", file=sys.stderr)
        return 1
    if fail_under >= 100:
        if totals.passed:
            return 0
        print(
            f"[coverage] 未达标：缺行 {totals.missing_lines} / 缺分支 {totals.partial_branches}"
            f"（{'、'.join(totals.missing_modules[:10])}）",
            file=sys.stderr,
        )
        for gap in totals.gaps[:20]:
            parts: list[str] = []
            if gap.lines:
                parts.append(f"缺行 {len(gap.lines)}（{_clip(gap.lines)}）")
            if gap.branches:
                parts.append(f"缺分支 {len(gap.branches)}（{_clip(gap.branches)}）")
            print(f"[coverage]   {gap.module}: {'，'.join(parts)}", file=sys.stderr)
        return 1
    if totals.percent + 1e-9 >= fail_under:
        return 0
    print(f"[coverage] 未达标：{totals.percent:.2f}% < {fail_under}%", file=sys.stderr)
    return 1


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
