"""覆盖率门禁（`engkit coverage`）：合并并行采集数据、导出 JSON 并判定「行 + 分支」。

采集侧由各仓的 pytest 插件（在**被测模块导入前**启动采集）按 pid 各落一份
`<data-file>.<pid>`；本模块负责合并、导出与判定。门禁口径：默认 100%（行 + 分支），
低于 100 时按合并百分比判定。
"""

from __future__ import annotations

import contextlib
import json
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

SOURCE_MARKER = "src"
"""模块名归一的路径标记（`…/src/<包>/…` 之后的部分即模块路径）。"""


@dataclass(frozen=True)
class Totals:
    """覆盖率汇总（按门禁范围过滤后）。

    Attributes:
        files: 参与判定的文件数。
        missing_lines: 未覆盖行数。
        partial_branches: 未走全的分支数。
        percent: 行 + 分支合并百分比（0~100）。
        missing_modules: 存在缺行 / 缺分支的模块名（供扩圈）。
    """

    files: int
    missing_lines: int
    partial_branches: int
    percent: float
    missing_modules: tuple[str, ...]

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
            missing_modules.append(module_name(str(path)))
    percent = (covered / statements * 100.0) if statements else 0.0
    return Totals(
        files=matched,
        missing_lines=missing_lines,
        partial_branches=partial,
        percent=percent,
        missing_modules=tuple(missing_modules),
    )


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
        return 1
    if totals.percent + 1e-9 >= fail_under:
        return 0
    print(f"[coverage] 未达标：{totals.percent:.2f}% < {fail_under}%", file=sys.stderr)
    return 1
