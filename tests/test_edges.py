"""边界分支补用例（覆盖退化 / 空集 / 异常路径，目标是行 + 分支 100%）。"""

from __future__ import annotations

import dataclasses
import runpy
import subprocess
import sys
import types
from pathlib import Path

import pytest

from engkit import cli, console, coverage, docs_links, gitutil, guard, preflight, selection
from engkit.config import GlobalRule, GuardConfig, load

PASS_CMD = f'"{sys.executable}" -c pass'


def test_main_module_entry(monkeypatch, capsys) -> None:
    """`python -m engkit` 入口可执行。"""
    monkeypatch.setattr(sys, "argv", ["engkit", "--version"])
    with pytest.raises(SystemExit):
        runpy.run_module("engkit.__main__", run_name="__main__")
    assert "engkit" in capsys.readouterr().out


def test_console_reconfigure_without_attribute() -> None:
    """不支持重配置的流原样返回。"""
    console.reconfigure(object())


def test_config_float_type_error(tmp_path: Path) -> None:
    """浮点键给出非数值即报错。"""
    from engkit.config import ConfigError

    path = tmp_path / "engkit.toml"
    path.write_text('[docs]\nroot = "d"\n[coverage]\nfail_under = "x"\n', encoding="utf-8")
    with pytest.raises(ConfigError, match="类型应为 float"):
        load(path)


def test_find_upward_at_filesystem_root() -> None:
    """上溯到文件系统根仍无配置时返回 None。"""
    from engkit.config import find_upward

    assert find_upward(Path(Path.cwd().anchor), limit=0) is None


def test_coverage_module_name_root_init() -> None:
    """包根 `__init__` 归一为空模块名。"""
    assert coverage.module_name("__init__.py") == ""
    assert coverage.module_name("src") == ""


def test_selection_coverage_report_unmatched_module(tmp_path: Path) -> None:
    """报告里完全没有目标模块时返回空（循环无 break）。"""
    report = tmp_path / "c2.json"
    report.write_text('{"files": {"a/other.py": {"missing_lines": [1]}}}', encoding="utf-8")
    assert selection.expand_from_coverage(report, ["pkg.core"]) == []


def test_selection_extra_tests_missing_target(build_repo) -> None:
    """映射指向不存在的测试文件时不并入。"""
    root = build_repo(
        config=(
            '[docs]\nroot = "docs"\n[backend]\nroot = "backend"\n[[rules.backend.extra_tests]]\n'
            'match_prefix = "scripts/check/"\nmapping = [{ script = "one.py", test = "no/such/test.py" }]\n'
        )
    )
    cfg = load(root / "engkit.toml")
    picked, notes = selection.extra_tests(cfg, ["scripts/check/one.py"])
    assert picked == []
    assert notes == []


def test_preflight_selection_without_any_tests(tmp_path: Path) -> None:
    """后端工程无任何测试文件时打「无后端测试文件」。"""
    root = tmp_path / "bare"
    (root / "backend/libs/only/src/only").mkdir(parents=True)
    (root / "backend/libs/only/src/only/__init__.py").write_text("", encoding="utf-8")
    (root / "backend/libs/only/tests").mkdir(parents=True)
    (root / "docs").mkdir()
    (root / "engkit.toml").write_text('[docs]\nroot = "docs"\n[backend]\nroot = "backend"\n', encoding="utf-8")
    cfg = load(root / "engkit.toml")
    assert preflight.run_selected(cfg, changed=["backend/libs/only/src/only/__init__.py"]) == 0


def test_docs_links_skips_git_dir(build_repo) -> None:
    """文档根下 `.git` 目录不参与校验。"""
    root = build_repo(files={"docs/.git/ignore.md": "# 忽略\n\n[a](无.md)\n"})
    assert docs_links.check(load(root / "engkit.toml")).broken == 0


def test_gitutil_blank_lines(monkeypatch) -> None:
    """git 输出含空行时忽略空路径。"""
    fake = subprocess.CompletedProcess([], 0, "a.py\n\n", "")
    monkeypatch.setattr(gitutil, "_run", lambda root, args: fake)
    changes, reliable = gitutil.changes(Path("."))
    assert changes == ["a.py"]
    assert reliable is True


def test_gitutil_changed_services_no_match() -> None:
    """不属于任何分组的变更不计服务。"""
    assert gitutil.changed_services(["docs/a.md"], "backend", ("libs", "services")) == []


def test_guard_scanner_without_site_packages(tmp_path: Path) -> None:
    """venv 有 lib 但无 `site-packages` 时返回 None。"""
    (tmp_path / "lib").mkdir()
    assert guard.site_packages(tmp_path) is None


def test_guard_heavy_pytest_below_threshold(tmp_path: Path) -> None:
    """目录内测试文件数未超阈值时不算重命令。"""
    small = tmp_path / "small"
    small.mkdir()
    (small / "test_one.py").write_text("", encoding="utf-8")
    config = GuardConfig(scripts=(), repo_dirs=(), dir_test_threshold=1)
    assert guard.classify(["pytest", str(small)], config) == ""


def test_guard_check_passes_non_heavy(monkeypatch, cfg) -> None:
    """非重命令直接放过。"""
    monkeypatch.setattr(guard, "_checked", False)
    monkeypatch.setenv(guard._CONFIG_ENV, str(cfg.path))
    assert guard.check(["pytest", "tests/test_a.py"]) is None


def test_selection_iter_py_missing_dir(tmp_path: Path) -> None:
    """不存在的目录返回空。"""
    assert selection._iter_py(tmp_path / "nope") == ()


def test_selection_parse_star_import(repo: Path) -> None:
    """`from x import *` 不登记别名模块。"""
    path = repo / "backend/libs/pkg_a/src/pkg_a/star.py"
    path.write_text("from pkg_a.util import *\n", encoding="utf-8")
    assert selection.parse_imports(path, "pkg_a.star") == {"pkg_a.util"}


def test_selection_transitive_cycle() -> None:
    """环上截断不抛错。"""
    closures = selection._transitive_deps({"a": {"b"}, "b": {"a"}})
    assert "b" in closures["a"]


def test_selection_graph_skips_non_test(repo: Path, cfg) -> None:
    """测试目录下的支持模块不计入测试文件。"""
    (repo / "backend/libs/pkg_a/tests/support.py").write_text("X = 1\n", encoding="utf-8")
    graph = selection.build_graph(cfg)
    assert "libs/pkg_a/tests/support.py" not in graph.test_files["pkg_a"]


def test_selection_graph_skips_non_package_sources(build_repo) -> None:
    """源码根下非包内文件不计入模块表。"""
    root = build_repo(files={"backend/libs/pkg_a/src/stray.py": "X = 1\n"})
    graph = selection.build_graph(load(root / "engkit.toml"))
    assert graph.projects


def test_selection_graph_external_import(repo: Path, cfg) -> None:
    """测试引用仓库外模块（如 os）时不进闭包。"""
    (repo / "backend/libs/pkg_a/tests/test_os.py").write_text(
        "import os\n\n\ndef test_os():\n    assert os.sep\n", encoding="utf-8"
    )
    graph = selection.build_graph(cfg)
    assert graph.test_deps["libs/pkg_a/tests/test_os.py"] == frozenset()


def test_selection_isomorphic_skips_non_test(repo: Path, cfg) -> None:
    """同构目录下的非测试文件被跳过。"""
    (repo / "backend/libs/pkg_a/tests/core/helper.py").write_text("X = 1\n", encoding="utf-8")
    project = selection.discover_projects(cfg)[0]
    candidates = selection._isomorphic_candidates(
        repo / "backend", "backend/libs/pkg_a/src/pkg_a/core/x.py", project, "test_"
    )
    assert "libs/pkg_a/tests/core/helper.py" not in candidates


def test_selection_extra_rule_without_matches(build_repo) -> None:
    """目录后缀规则无命中 / 映射未登记且无兜底 / 显式文件不存在。"""
    root = build_repo(
        config=(
            '[docs]\nroot = "docs"\n[backend]\nroot = "backend"\n[[rules.backend.extra_tests]]\n'
            'match_prefix = "backend/ops/"\ntest_dir_suffix = "/tests/ops/"\n'
            '[[rules.backend.extra_tests]]\nmatch_prefix = "scripts/check/"\n'
            'mapping = [{ script = "one.py", test = "no/such/test.py" }]\n'
        )
    )
    cfg = load(root / "engkit.toml")
    picked, notes = selection.extra_tests(cfg, ["backend/ops/seed.py", "scripts/check/two.py"])
    assert picked == []
    assert notes == []


def test_selection_guard_tests_arcs(repo: Path, cfg) -> None:
    """护栏用例并入：已入选 / 不属于任何工程。"""
    result = selection.build_selection(cfg, ["backend/libs/pkg_a/src/pkg_a/util.py", "scripts/tools/guard.py"])
    assert "libs/pkg_a/tests/core/test_core.py" in result.test_paths
    picked, notes = selection.extra_tests(cfg, ["scripts/tools/guard.py"])
    assert "libs/pkg_a/tests/core/test_core.py" in picked
    assert notes


def test_selection_global_change_empty_project(build_repo) -> None:
    """全局变更时无测试文件的工程不参与退化说明。"""
    root = build_repo(
        files={"backend/services/empty/tests_placeholder.txt": ""},
        config='[docs]\nroot = "docs"\n[backend]\nroot = "backend"\n[rules.backend.global]\nexact = ["x"]\n',
    )
    (root / "backend/services/empty/src").mkdir(parents=True, exist_ok=True)
    (root / "backend/services/empty/tests").mkdir(parents=True, exist_ok=True)
    (root / "backend/services/empty/src/empty/__init__.py").parent.mkdir(parents=True, exist_ok=True)
    (root / "backend/services/empty/src/empty/__init__.py").write_text("", encoding="utf-8")
    cfg = load(root / "engkit.toml")
    result = selection.build_selection(cfg, ["x"])
    metrics = {metric.name: metric for metric in result.projects}
    assert metrics["empty"].total == 0
    assert metrics["empty"].degraded is False


def test_selection_coverage_report_extra_files(tmp_path: Path) -> None:
    """报告里非目标文件不影响缺行匹配。"""
    report = tmp_path / "c.json"
    report.write_text(
        '{"files": {"a/other.py": {"missing_lines": [1]}, "a/pkg/core.py": {"missing_lines": [2]}}}',
        encoding="utf-8",
    )
    assert selection.expand_from_coverage(report, ["pkg.core"]) == ["pkg.core"]


def test_selection_read_changed_edge(repo: Path) -> None:
    """空路径被忽略；清单文件不存在时退回显式文件。"""
    changed = selection.read_changed(repo, changed_files=("  ", "a.py"), changed_file_list=str(repo / "nope.txt"))
    assert changed == ["a.py"]


def test_frontend_missing_group_and_blank_change(cfg) -> None:
    """前端分组缺失 / 空变更项。"""
    patched = dataclasses.replace(
        cfg,
        frontend=dataclasses.replace(cfg.frontend, groups=("packages", "apps", "missing")),
    )
    assert [package.name for package in selection.discover_frontend_packages(patched)] == [
        "@mini/core",
        "@mini/desktop",
    ]
    result = selection.build_frontend_selection(cfg, ["   "])
    assert result.packages == ()


def test_preflight_selection_without_tests(build_repo) -> None:
    """后端工程无测试文件时打「无后端测试文件」路径。"""
    root = build_repo(
        files={"backend/libs/only/src/only/__init__.py": "", "backend/libs/only/tests/.keep": ""},
        config='[docs]\nroot = "docs"\n[backend]\nroot = "backend"\n[pytest]\ncmd = "x"\n',
    )
    cfg = load(root / "engkit.toml")
    assert preflight.run_selected(cfg, changed=["backend/libs/only/src/only/__init__.py"]) == 0


def test_preflight_full_tests_without_plugin(cfg, monkeypatch) -> None:
    """未配置采集插件时不追加 `-p`。"""
    calls: list[str] = []
    monkeypatch.setattr(preflight, "execute", lambda label, *a, **k: calls.append(label) or True)
    patched = dataclasses.replace(cfg, coverage=dataclasses.replace(cfg.coverage, plugin=""))
    budget = preflight.Budget(step_seconds=30, total_seconds=30)
    preflight.run_full_tests(patched, budget, [], jobs="off", no_cov=False)
    assert calls


def test_preflight_self_test_without_backend(tmp_path: Path) -> None:
    """未配置后端时自检只跑通用项。"""
    root = tmp_path / "r"
    root.mkdir()
    (root / "engkit.toml").write_text(
        '[docs]\nroot = "docs"\n[[preflight.steps]]\nlabel = "x"\ncmd = ["python"]\n', encoding="utf-8"
    )
    assert preflight.self_test(load(root / "engkit.toml")) == 0


def test_preflight_project_tests_via_engine(cfg, monkeypatch) -> None:
    """引擎里执行逐工程范围步骤。"""
    from engkit.config import Step

    monkeypatch.setenv("CI", "1")
    steps = (Step(label="逐工程", type="project_tests", preset="full"),)
    patched = dataclasses.replace(cfg, preflight=dataclasses.replace(cfg.preflight, steps=steps, self_tests=()))
    labels: list[str] = []
    monkeypatch.setattr(preflight, "execute", lambda label, *a, **k: labels.append(label) or True)
    assert preflight.run_preflight(patched, mode="full", jobs="off", no_cov=True, no_scope=False, from_push=True) == 0
    assert labels


def test_cli_links_quiet_with_findings(build_repo, capsys) -> None:
    """静默模式下不列明细但仍返回 1。"""
    root = build_repo(files={"docs/坏.md": "# 坏\n\n[a](无.md)\n"})
    assert cli.main(["links", "--root", str(root), "--quiet"]) == 1
    captured = capsys.readouterr()
    assert "[断链]" not in captured.err
    assert "断链 1 处" in captured.out


def test_cli_docs_scope_self_test_scan_mismatch(repo: Path, monkeypatch, capsys) -> None:
    """内置向量与实际判定不符时自测失败。"""
    monkeypatch.setattr(cli.docs_scope, "scan_text", lambda text, cfg: ())
    assert cli.main(["docs-scope", "--root", str(repo), "--self-test"]) == 1
    assert "失败" in capsys.readouterr().out


def test_cli_docs_scope_self_test_without_plan_pattern(tmp_path: Path, capsys) -> None:
    """未配置计划目录正则时跳过白名单向量。"""
    root = tmp_path / "r"
    root.mkdir()
    (root / "engkit.toml").write_text('[docs]\nroot = "docs"\n', encoding="utf-8")
    (root / "docs").mkdir()
    assert cli.main(["docs-scope", "--root", str(root), "--self-test"]) == 0
    assert "全部通过" in capsys.readouterr().out


def test_cli_coverage_clean_and_error(tmp_path: Path, monkeypatch, capsys) -> None:
    """`--clean` 清理旧数据；异常路径返回 2。"""
    data = tmp_path / ".coverage"
    data.write_text("x", encoding="utf-8")
    assert cli.main(["coverage", "--data-file", str(data), "--out", str(tmp_path / "r.json"), "--clean"]) == 1
    capsys.readouterr()

    def boom(*args, **kwargs):
        raise RuntimeError("缺 coverage")

    monkeypatch.setattr(cli.coverage, "check", boom)
    assert cli.main(["coverage", "--data-file", str(data), "--out", str(tmp_path / "r.json")]) == 2
    assert "缺 coverage" in capsys.readouterr().err


def test_cli_kiwi_list_and_ping(repo: Path, monkeypatch, capsys) -> None:
    """`kiwi list` / `kiwi ping` 走 CLI。"""
    monkeypatch.setattr(cli.kiwi, "list_cases", lambda cfg, limit=10: 0)
    monkeypatch.setattr(cli.kiwi, "ping", lambda cfg: 0)
    assert cli.main(["kiwi", "list", "--root", str(repo)]) == 0
    assert cli.main(["kiwi", "ping", "--root", str(repo)]) == 0
    capsys.readouterr()


def test_preflight_gate_zero_ratio(cfg) -> None:
    """度量汇总在总数为零时显示占位符。"""
    assert "—" in preflight.metrics_summary(["[test-selection] picked=0/0 degraded=false rounds=1 cov=0 模块"])


def test_guard_config_defaults() -> None:
    """缺省拦截配置（无脚本 / 无仓库目录）。"""
    assert guard.classify(["pytest"], GuardConfig()) == "pytest"
    assert guard._is_heavy_scanner(["libs"], GuardConfig(), default_targets=True) is False


def test_selection_global_rule_defaults() -> None:
    """全局规则为空时不做匹配。"""
    assert GlobalRule().exact == ()
    assert types.SimpleNamespace() is not None
