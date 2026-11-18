"""覆盖率门禁用例。"""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
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
    """采集文件枚举与清理（含并行进程后缀；同目录报告残留不算分片）。"""
    base = tmp_path / ".coverage"
    _make_data(base, tmp_path)
    _make_data(base, tmp_path, suffix="123")
    legacy = tmp_path / ".coverage.json"
    legacy.write_text("{}", encoding="utf-8")
    assert len(coverage.data_files(base)) == 2
    coverage.cleanup(base)
    assert coverage.data_files(base) == ()
    assert not legacy.exists()


def test_data_files_ignores_non_numeric_siblings(tmp_path: Path) -> None:
    """非纯数字后缀 / 目录 / 非 ASCII 数字后缀都不算采集分片。"""
    base = tmp_path / ".coverage"
    backup = tmp_path / ".coverage.backup"
    backup.write_text("", encoding="utf-8")
    (tmp_path / ".coverage.777").mkdir()
    (tmp_path / ".coverage.²").write_text("", encoding="utf-8")
    assert coverage.data_files(base) == ()
    coverage.cleanup(base)
    assert backup.exists()


def test_report_path_isolated_from_shards(tmp_path: Path) -> None:
    """报告落在 `report.json`：不进入 `<data-file>.*` 分片命名空间。"""
    base = tmp_path / "cache" / ".coverage"
    report = coverage.report_path(base)
    assert report == tmp_path / "cache" / "report.json"
    assert not report.name.startswith(f"{base.name}.")


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


def test_ensure_data_dir_creates_parent(tmp_path: Path) -> None:
    """落点可位于尚未创建的缓存目录（`.cache/coverage/`）：自动建父目录且可重复调用。"""
    base = tmp_path / ".cache" / "coverage" / ".coverage"

    assert coverage.ensure_data_dir(base) == tmp_path / ".cache" / "coverage"
    assert base.parent.is_dir()
    assert coverage.ensure_data_dir(base) == base.parent


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
                "summary": {"missing_lines": 2, "num_partial_branches": 1, "num_statements": 10, "covered_lines": 8},
                "missing_lines": [3, 4],
                "missing_branches": [[8, 10], [12, None]],
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
    assert [gap.module for gap in totals.gaps] == ["pkg.core"]
    assert totals.gaps[0].lines == (3, 4)
    assert totals.gaps[0].branches == ("8->10", "12->?")
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
    """100% 口径下缺行即未达标，并逐模块列出缺的行号 / 分支（可定位）。"""
    base = tmp_path / ".coverage"
    _make_data(base, tmp_path, complete=False)
    assert coverage.check(base, tmp_path / "r.json", ()) == 1
    err = capsys.readouterr().err
    assert "未达标：缺行" in err
    assert "sample: 缺行" in err


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


def _git_repo(path: Path) -> Path:
    """把临时目录初始化为 git 仓（提交身份直接写进仓配置）。

    Args:
        path: 仓库目录。

    Returns:
        Path: 仓库目录。
    """
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.email", "t@example.com"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.name", "tester"], cwd=path, check=True)
    return path


def test_changed_lines_reads_staged_hunks(tmp_path: Path) -> None:
    """暂存区改动行：修改与新增行被收集，删除行不参与。"""
    repo = _git_repo(tmp_path / "repo")
    module = repo / "a.py"
    module.write_text("x = 1\ny = 2\nz = 3\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-qm", "init"], cwd=repo, check=True)
    module.write_text("x = 1\ny = 9\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=repo, check=True)
    assert coverage.changed_lines(repo, "a.py") == (2,)


def test_statements_of_marks_executable_lines() -> None:
    """语句集合 = executed ∪ missing；不可执行行天然不在集合里。"""
    payload = {"files": {"a/src/pkg/x.py": {"executed_lines": [1, 2], "missing_lines": [5]}}}
    assert coverage.statements_of(payload, "a/src/pkg/x.py") == ({1, 2, 5}, {5})
    assert coverage.statements_of({"files": {}}, "x.py") is None
    assert coverage.statements_of({"files": {"x.py": {}}}, "x.py") is None
    assert coverage.statements_of({"files": {"x.py": {"executed_lines": [1]}}}, "x.py") is None


def test_check_changed_passes_when_changed_lines_executed(tmp_path: Path, capsys) -> None:
    """改动行已执行：通过。"""
    repo = _git_repo(tmp_path / "ok")
    data = repo / ".coverage"
    _make_data(data, repo, complete=True)
    module = repo / "sample.py"
    subprocess.run(["git", "add", "."], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-qm", "base"], cwd=repo, check=True)
    module.write_text("X = 2\nY = 2\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=repo, check=True)
    assert coverage.check_changed(repo, data, repo / "r.json", ("sample.py",)) == 0
    assert "改动行均已被执行" in capsys.readouterr().out


def test_check_changed_flags_unexecuted_lines(tmp_path: Path, capsys) -> None:
    """改动行未执行：失败并列出该文件。"""
    repo = _git_repo(tmp_path / "bad")
    data = repo / ".coverage"
    module = repo / "sample.py"
    module.write_text(_PARTIAL, encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-qm", "base"], cwd=repo, check=True)
    _make_data(data, repo, complete=False)
    module.write_text("X = 1\n\n\ndef unused():\n    return 4\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=repo, check=True)
    assert coverage.check_changed(repo, data, repo / "r.json", ("sample.py",)) == 1
    assert "sample.py" in capsys.readouterr().err


def test_check_changed_skips_uncaptured_and_without_data(tmp_path: Path, capsys) -> None:
    """未采集文件跳过并提示；完全无数据按未达标。"""
    repo = _git_repo(tmp_path / "skip")
    data = repo / ".coverage"
    _make_data(data, repo, complete=True)
    assert coverage.check_changed(repo, data, repo / "r.json", ("other.py",)) == 0
    assert "跳过 1 个未采集文件" in capsys.readouterr().out
    assert coverage.check_changed(repo, repo / "missing-data", repo / "r.json", ("x.py",)) == 2
    assert "未采集到覆盖率数据" in capsys.readouterr().err


def test_format_branch_and_clip_edges() -> None:
    """分支对非法输入回落 `?`；明细超限给出总数（避免刷屏）。"""
    assert coverage._format_branch("bad") == "?"
    assert coverage._format_branch([1]) == "?"
    assert coverage._format_branch([3, None]) == "3->?"
    assert coverage._clip((1, 2)) == "1、2"
    assert (
        coverage._clip(tuple(range(25)))
        == "0、1、2、3、4、5、6、7、8、9、10、11、12、13、14、15、16、17、18、19 …（共 25 处）"
    )


def test_check_lists_line_and_branch_gaps(monkeypatch, capsys) -> None:
    """未达标时逐模块列出缺行与缺分支（分支明细超限追加总数；只有分支时不打空「缺行 0」）。"""
    payload = {
        "files": {
            "a/src/pkg/core.py": {
                "summary": {"missing_lines": 1, "num_partial_branches": 25, "num_statements": 10, "covered_lines": 9},
                "missing_lines": [3],
                "missing_branches": [[index, None] for index in range(25)],
            },
            "a/src/pkg/branch_only.py": {
                "summary": {"missing_lines": 0, "num_partial_branches": 1, "num_statements": 5, "covered_lines": 5},
                "missing_lines": [],
                "missing_branches": [[4, None]],
            },
        }
    }
    monkeypatch.setattr(coverage, "combine", lambda data_file: 1)
    monkeypatch.setattr(coverage, "export_json", lambda data_file, out: payload)
    assert coverage.check(Path("data"), Path("out.json"), ()) == 1
    err = capsys.readouterr().err
    assert "缺行 1（3）" in err
    assert "缺分支 25（" in err and "共 25 处" in err
    assert "branch_only: 缺分支 1" in err


def test_changed_lines_git_failure_returns_empty() -> None:
    """取不到 git 判定时返回空集合（不抛错，交上层按未采集处理）。

    用**仓库外**的系统临时目录：`tmp_path` 位于本仓 `.cache/pytest` 下，向上能找到
    本仓 `.git`，`git diff` 仍会成功返回空输出。
    """
    with tempfile.TemporaryDirectory() as folder:
        assert coverage.changed_lines(Path(folder), "x.py") == ()


def test_statements_of_rejects_non_mapping_payload() -> None:
    """报告不是映射时返回 None（防御损坏的 JSON）。"""
    assert coverage.statements_of([], "x.py") is None
    assert coverage.statements_of("bad", "x.py") is None


def test_check_changed_skips_non_py_and_test_paths(tmp_path: Path, capsys) -> None:
    """非 `.py` 与测试路径不参与判定；无可判文件与通过两条路径都能静默。"""
    repo = _git_repo(tmp_path / "types")
    data = repo / ".coverage"
    _make_data(data, repo, complete=True)
    assert coverage.check_changed(repo, data, repo / "r.json", ("a.txt", "tests/test_x.py")) == 0
    assert "无可判文件" in capsys.readouterr().out
    assert coverage.check_changed(repo, data, repo / "r.json", ("a.txt",), quiet=True) == 0
    assert coverage.check_changed(repo, data, repo / "r.json", ("sample.py",), quiet=True) == 0
    assert capsys.readouterr().out == ""


def _make_module_data(
    data_file: Path,
    root: Path,
    relative: str,
    *,
    complete: bool = True,
    suffix: str = "1",
) -> Path:
    """在 `root/<relative>` 生成被测模块并采集真实覆盖率数据。

    与 `_make_data` 的区别：模块落在指定相对路径（如 `src/pkg/sample.py`），
    以便验证按文件清单判定时的 `src` 之内 / 之外区分。每次采集写独立分片
    （`<data-file>.<数字>`），`check` 合并后不会互相覆盖。

    Args:
        data_file: 数据文件基址。
        root: 仓根目录。
        relative: 模块相对路径（posix 形态）。
        complete: 是否让脚本全部行都被执行（决定门禁是否达标）。
        suffix: 采集分片后缀（纯数字）。

    Returns:
        Path: 实际数据文件路径。
    """
    module = root / relative
    module.parent.mkdir(parents=True, exist_ok=True)
    module.write_text(_COMPLETE if complete else _PARTIAL, encoding="utf-8")
    shard = data_file.with_name(f"{data_file.name}.{suffix}")
    script = (
        "import coverage\n"
        f"instance = coverage.Coverage(data_file={str(shard)!r}, branch=True, "
        "data_suffix=False, config_file=False)\n"
        "instance.start()\n"
        f"exec(compile(open({str(module)!r}, encoding='utf-8').read(), {str(module)!r}, 'exec'), {{}})\n"
        "instance.stop()\n"
        "instance.save()\n"
    )
    subprocess.run([sys.executable, "-c", script], check=True, capture_output=True)
    return data_file


def test_target_files_classifies_and_dedups() -> None:
    """文件清单划分为判定目标与「不参与」项（去重、反斜杠归一、原因逐条给出）。"""
    targets, ignored = coverage.target_files(
        [
            "a/src/pkg/core.py",
            "a/src/pkg/core.py",
            "",
            "   ",
            "a/src/pkg/data.json",
            "a/src/pkg/tests/test_x.py",
            "a/test/helper.py",
            "scripts/tool.py",
            "b/src/other/__init__.py",
            "back\\slash\\src\\x.py",
        ]
    )
    assert targets == ("a/src/pkg/core.py", "b/src/other/__init__.py", "back/slash/src/x.py")
    reasons = {item.path: item.reason for item in ignored}
    assert reasons == {
        "a/src/pkg/data.json": "非 Python 源文件",
        "a/src/pkg/tests/test_x.py": "测试文件不参与覆盖率",
        "a/test/helper.py": "测试文件不参与覆盖率",
        "scripts/tool.py": "不在 src 下（配置 / 迁移 / 运维脚本）",
    }


def test_path_matches_and_in_scope() -> None:
    """路径精确匹配（兼容绝对 / 反斜杠形态）与「模块 ∪ 文件」范围判定。"""
    assert coverage._path_matches("C:/repo/a/src/pkg/core.py", ("a/src/pkg/core.py",)) is True
    assert coverage._path_matches("C:\\repo\\a\\src\\pkg\\core.py", ("a/src/pkg/core.py",)) is True
    assert coverage._path_matches("a/src/pkg/core.py", ("a/src/pkg/util.py",)) is False
    assert coverage._path_matches("a/src/pkg/core.py", ()) is False
    assert coverage._in_scope("any.py", (), ()) is True
    assert coverage._in_scope("a/src/pkg/core.py", ("pkg",), ()) is True
    assert coverage._in_scope("a/src/pkg/core.py", (), ("a/src/pkg/core.py",)) is True
    assert coverage._in_scope("b/src/x.py", ("pkg",), ("a/src/pkg/core.py",)) is False


def test_filtered_totals_by_paths() -> None:
    """按文件清单汇总：只统计清单内文件，与模块范围取并集。"""
    payload = {
        "files": {
            "a/src/pkg/core.py": {
                "summary": {"missing_lines": 2, "num_partial_branches": 0, "num_statements": 10, "covered_lines": 8},
                "missing_lines": [3, 4],
                "missing_branches": [],
            },
            "a/src/pkg/util.py": {
                "summary": {"missing_lines": 0, "num_partial_branches": 0, "num_statements": 5, "covered_lines": 5}
            },
            "a/src/other/x.py": {
                "summary": {"missing_lines": 1, "num_partial_branches": 0, "num_statements": 1, "covered_lines": 0}
            },
        }
    }
    totals = coverage.filtered_totals(payload, (), ("a/src/pkg/core.py", "a/src/other/x.py"))
    assert totals.files == 2
    assert totals.missing_lines == 3
    assert totals.missing_modules == ("pkg.core", "other.x")
    union = coverage.filtered_totals(payload, ("pkg.util",), ("a/src/pkg/core.py",))
    assert union.files == 2
    assert coverage.filtered_totals(payload, (), ("a/src/pkg/util.py",)).passed is True


def test_file_gaps_statuses() -> None:
    """逐文件结论：达标 / 缺口（完整行号与分支）/ 未采集，坏报告按未采集处理。"""
    payload = {
        "files": {
            "C:\\repo\\a\\src\\pkg\\ok.py": {
                "summary": {"missing_lines": 0, "num_partial_branches": 0},
                "missing_lines": [],
                "missing_branches": [],
            },
            "a/src/pkg/gap.py": {
                "summary": {"missing_lines": 2, "num_partial_branches": 1},
                "missing_lines": [1, 2],
                "missing_branches": [[3, None]],
            },
            "a/src/pkg/broken.py": "not-a-mapping",
        }
    }
    entries = coverage.file_gaps(
        payload,
        ("a/src/pkg/ok.py", "a/src/pkg/gap.py", "a/src/pkg/absent.py", "a/src/pkg/broken.py"),
    )
    assert [entry.status for entry in entries] == [
        coverage.TARGET_OK,
        coverage.TARGET_GAP,
        coverage.TARGET_MISSING,
        coverage.TARGET_MISSING,
    ]
    assert entries[1].module == "pkg.gap"
    assert entries[1].lines == (1, 2)
    assert entries[1].branches == ("3->?",)
    assert entries[2].module == "pkg.absent"
    assert coverage.file_gaps([], ("a/src/pkg/x.py",))[0].status == coverage.TARGET_MISSING
    assert coverage.file_gaps("bad", ("a/src/pkg/x.py",))[0].status == coverage.TARGET_MISSING


def test_target_summary_lists_all_lines() -> None:
    """按文件明细：达标计数、不参与项、缺口明细完整不截断、未采集显式列出。"""
    entries = (
        coverage.FileGap("a/src/ok.py", "ok", coverage.TARGET_OK),
        coverage.FileGap("a/src/gap.py", "gap", coverage.TARGET_GAP, tuple(range(25)), ("1->2",)),
        coverage.FileGap("a/src/branch.py", "branch", coverage.TARGET_GAP, (), ("7->9",)),
        coverage.FileGap("a/src/absent.py", "absent", coverage.TARGET_MISSING),
    )
    text = "\n".join(coverage._target_summary(entries, ()))
    assert "目标文件：4（达标 1 / 缺口 2 / 未采集 1）" in text
    assert "不参与判定" not in text
    assert "✗ a/src/gap.py（gap）：缺行 25（0、1、2" in text
    assert "24）" in text
    assert "共 25 处" not in text
    assert "✗ a/src/branch.py（branch）：缺分支 1（7->9）" in text
    assert "✗ a/src/branch.py（branch）：缺行" not in text
    assert "? a/src/absent.py：未采集" in text
    with_ignored = "\n".join(
        coverage._target_summary(entries, (coverage.IgnoredTarget("docs/a.md", "非 Python 源文件"),))
    )
    assert "不参与判定：1 个" in with_ignored
    assert "docs/a.md：非 Python 源文件" in with_ignored


def test_write_gaps_txt_and_json(tmp_path: Path) -> None:
    """缺口明细落盘：人读文本 + 结构化 JSON（逐文件状态、行号与不参与项）。"""
    payload = {
        "files": {
            "a/src/pkg/gap.py": {
                "summary": {"missing_lines": 1, "num_partial_branches": 0, "num_statements": 2, "covered_lines": 1},
                "missing_lines": [2],
                "missing_branches": [],
            }
        }
    }
    totals = coverage.filtered_totals(payload, (), ("a/src/pkg/gap.py",))
    entries = coverage.file_gaps(payload, ("a/src/pkg/gap.py",))
    ignored = (coverage.IgnoredTarget("docs/a.md", "非 Python 源文件"),)
    path = tmp_path / "nested" / coverage.GAPS_TEXT_NAME
    coverage.write_gaps(path, totals, entries, ignored, "[coverage] 正文")
    assert path.read_text(encoding="utf-8") == "[coverage] 正文\n"
    payload_json = json.loads((tmp_path / "nested" / coverage.GAPS_JSON_NAME).read_text(encoding="utf-8"))
    assert payload_json["totals"]["files"] == 1
    assert payload_json["totals"]["missing_lines"] == 1
    assert payload_json["files"] == [
        {
            "path": "a/src/pkg/gap.py",
            "module": "pkg.gap",
            "status": coverage.TARGET_GAP,
            "missing_lines": [2],
            "missing_branches": [],
        }
    ]
    assert payload_json["ignored"] == [{"path": "docs/a.md", "reason": "非 Python 源文件"}]


def test_check_with_paths_reports_gap_and_ignored(tmp_path: Path, capsys) -> None:
    """批量查：一次列出全部目标文件结论、不参与项与完整行号，并落盘 gaps.txt / gaps.json。"""
    root = tmp_path / "repo"
    data = _make_module_data(root / ".coverage", root, "src/pkg/ok.py", suffix="1")
    _make_module_data(data, root, "src/pkg/gap.py", complete=False, suffix="2")
    gaps_out = root / ".cache" / "coverage" / coverage.GAPS_TEXT_NAME
    code = coverage.check(
        data,
        root / "report.json",
        (),
        paths=(
            "src/pkg/ok.py",
            "src/pkg/gap.py",
            "src/pkg/absent.py",
            "tests/test_gap.py",
            "scripts/tool.py",
            "docs/a.md",
        ),
        gaps_out=gaps_out,
    )
    assert code == 1
    err = capsys.readouterr().err
    assert "目标文件：3（达标 1 / 缺口 1 / 未采集 1）" in err
    assert "不参与判定：3 个" in err
    assert "✗ src/pkg/gap.py（pkg.gap）：缺行 1（5）" in err
    assert "? src/pkg/absent.py：未采集" in err
    assert "tests/test_gap.py：测试文件不参与覆盖率" in err
    assert "scripts/tool.py：不在 src 下（配置 / 迁移 / 运维脚本）" in err
    assert "docs/a.md：非 Python 源文件" in err
    assert "未达标：缺行 1 / 缺分支 0（逐文件明细见上）" in err
    assert "共 1 处" not in err
    text = gaps_out.read_text(encoding="utf-8")
    assert "目标文件：3" in text
    payload = json.loads((gaps_out.parent / coverage.GAPS_JSON_NAME).read_text(encoding="utf-8"))
    assert [item["status"] for item in payload["files"]] == [
        coverage.TARGET_OK,
        coverage.TARGET_GAP,
        coverage.TARGET_MISSING,
    ]
    assert len(payload["ignored"]) == 3


def test_check_with_paths_missing_and_low_threshold(tmp_path: Path, capsys) -> None:
    """批量查：报告里没有目标文件即未达标；低阈值口径下未采集仍不可放过。"""
    root = tmp_path / "repo"
    data = _make_module_data(root / ".coverage", root, "src/pkg/gap.py", complete=False)
    assert coverage.check(data, root / "r.json", (), paths=("src/pkg/absent.py",)) == 1
    captured = capsys.readouterr()
    assert "目标文件：1（达标 0 / 缺口 0 / 未采集 1）" in captured.err
    assert "未采集：1 个目标文件不在本轮报告里" in captured.err
    assert coverage.check(data, root / "r.json", (), fail_under=1, paths=("src/pkg/gap.py",)) == 0
    assert "目标文件：1（达标 0 / 缺口 1 / 未采集 0）" in capsys.readouterr().err


def test_check_with_paths_all_ignored(tmp_path: Path, capsys) -> None:
    """批量清单全是「不参与」项时显式列出并放行，**不得**退化成「判全仓」。"""
    root = tmp_path / "repo"
    data = _make_module_data(root / ".coverage", root, "src/pkg/ok.py")
    gaps_out = root / ".cache" / "coverage" / coverage.GAPS_TEXT_NAME
    code = coverage.check(
        data,
        root / "r.json",
        (),
        paths=("tests/test_gap.py", "scripts/tool.py", "docs/a.md"),
        gaps_out=gaps_out,
    )
    assert code == 0
    err = capsys.readouterr().err
    assert "本次改动无可判文件：3 个均不参与覆盖率判定" in err
    assert "docs/a.md：非 Python 源文件" in err
    payload = json.loads((gaps_out.parent / coverage.GAPS_JSON_NAME).read_text(encoding="utf-8"))
    assert payload["totals"]["files"] == 0
    assert len(payload["ignored"]) == 3
    assert coverage.check(data, root / "r2.json", (), paths=("docs/a.md",)) == 0
    assert "本次改动无可判文件：1 个" in capsys.readouterr().err
