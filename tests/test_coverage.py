"""覆盖率门禁用例。"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from engkit import coverage

_COMPLETE = "X = 1\nY = 2\n"
_PARTIAL = "X = 1\n\n\ndef unused():\n    return 3\n"


def _make_data(data_file: Path, tmp_path: Path, *, complete: bool = True, suffix: str = "") -> Path:
    """在独立进程里用 coverage 生成一份真实采集数据（避免干扰本进程度量）。

    Args:
        data_file: 数据文件基址。
        tmp_path: 临时目录（放被测脚本）。
        complete: 是否让脚本全部行都被执行（决定门禁是否达标）。
        suffix: 并行进程后缀（模拟 `<data-file>.<pid>`）。

    Returns:
        Path: 实际数据文件路径。
    """
    module = tmp_path / f"sample{suffix}.py"
    module.write_text(_COMPLETE if complete else _PARTIAL, encoding="utf-8")
    target = data_file if not suffix else data_file.with_name(f"{data_file.name}.{suffix}")
    script = (
        "import coverage\n"
        f"instance = coverage.Coverage(data_file={str(target)!r}, branch=True, "
        "data_suffix=False, config_file=False)\n"
        "instance.start()\n"
        f"exec(compile(open({str(module)!r}, encoding='utf-8').read(), {str(module)!r}, 'exec'), {{}})\n"
        "instance.stop()\n"
        "instance.save()\n"
    )
    subprocess.run([sys.executable, "-c", script], check=True, capture_output=True)
    return target


def test_data_files_and_cleanup(tmp_path: Path) -> None:
    """采集文件枚举与清理（含并行进程后缀）。"""
    base = tmp_path / ".coverage"
    _make_data(base, tmp_path)
    _make_data(base, tmp_path, suffix="123")
    assert len(coverage.data_files(base)) == 2
    coverage.cleanup(base)
    assert coverage.data_files(base) == ()


def test_combine_and_export(tmp_path: Path) -> None:
    """合并并行数据并导出 JSON 报告。"""
    base = tmp_path / ".coverage"
    _make_data(base, tmp_path)
    _make_data(base, tmp_path, suffix="456")
    assert coverage.combine(base) == 2
    payload = coverage.export_json(base, tmp_path / "out" / "coverage.json")
    assert payload["files"]


def test_combine_without_data(tmp_path: Path) -> None:
    """无采集数据时合并返回 0。"""
    assert coverage.combine(tmp_path / ".coverage") == 0


def test_module_matches_and_name() -> None:
    """模块匹配与路径归一。"""
    assert coverage.module_matches("a/src/pkg/core.py", ()) is True
    assert coverage.module_matches("a/src/pkg/core.py", ("pkg.core",)) is True
    assert coverage.module_matches("a/src/pkg/__init__.py", ("pkg",)) is True
    assert coverage.module_matches("a/src/pkg/core.py", ("other",)) is False
    assert coverage.module_name("a/src/pkg/core.py") == "pkg.core"
    assert coverage.module_name("a/src/pkg/__init__.py") == "pkg"
    assert coverage.module_name("plain.py") == "plain"


def test_filtered_totals() -> None:
    """按范围汇总：缺行、缺分支与百分比。"""
    payload = {
        "files": {
            "a/src/pkg/core.py": {
                "summary": {"missing_lines": 2, "num_partial_branches": 1, "num_statements": 10, "covered_lines": 8}
            },
            "a/src/pkg/util.py": {
                "summary": {"missing_lines": 0, "num_partial_branches": 0, "num_statements": 5, "covered_lines": 5}
            },
            "a/src/other/x.py": {
                "summary": {"missing_lines": 1, "num_partial_branches": 0, "num_statements": 1, "covered_lines": 0}
            },
        }
    }
    totals = coverage.filtered_totals(payload, ("pkg",))
    assert totals.files == 2
    assert totals.missing_lines == 2
    assert totals.partial_branches == 1
    assert totals.missing_modules == ("pkg.core",)
    assert totals.passed is False
    assert "percent=" in coverage.describe(totals)
    assert coverage.filtered_totals(payload, ()).files == 3
    assert coverage.filtered_totals({"files": {}}, ()).percent == 0.0


def test_check_pass(tmp_path: Path, capsys) -> None:
    """覆盖率达标的通过路径（打印度量行）。"""
    base = tmp_path / ".coverage"
    _make_data(base, tmp_path)
    assert coverage.check(base, tmp_path / "report.json", ()) == 0
    assert "[coverage] files=" in capsys.readouterr().out


def test_check_without_data(tmp_path: Path, capsys) -> None:
    """未采集到数据即未达标。"""
    assert coverage.check(tmp_path / ".coverage", tmp_path / "r.json", ()) == 1
    assert "未采集到覆盖率数据" in capsys.readouterr().err


def test_check_unmatched_modules(tmp_path: Path, capsys) -> None:
    """门禁范围未匹配到文件即未达标（且可静默度量行）。"""
    base = tmp_path / ".coverage"
    _make_data(base, tmp_path)
    assert coverage.check(base, tmp_path / "r.json", ("no.such.module",), quiet=True) == 1
    captured = capsys.readouterr()
    assert "未匹配到任何文件" in captured.err
    assert captured.out == ""


def test_check_fail_under_100(tmp_path: Path, capsys) -> None:
    """100% 口径下缺行即未达标并列出模块。"""
    base = tmp_path / ".coverage"
    _make_data(base, tmp_path, complete=False)
    assert coverage.check(base, tmp_path / "r.json", ()) == 1
    assert "未达标：缺行" in capsys.readouterr().err


def test_check_low_threshold_pass(tmp_path: Path) -> None:
    """阈值低于 100 时按百分比判定（达标）。"""
    base = tmp_path / ".coverage"
    _make_data(base, tmp_path, complete=False)
    assert coverage.check(base, tmp_path / "r.json", (), fail_under=1) == 0


def test_check_low_threshold_fail(tmp_path: Path, capsys) -> None:
    """阈值低于 100 时按百分比判定（未达标）。"""
    base = tmp_path / ".coverage"
    _make_data(base, tmp_path, complete=False)
    assert coverage.check(base, tmp_path / "r.json", (), fail_under=99.9) == 1
    assert "未达标：" in capsys.readouterr().err
