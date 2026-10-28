"""后端选测用例（目录同构 + AST 收敛 + 退化 + 特殊路径）。"""

from __future__ import annotations

import dataclasses
import json
from pathlib import Path

import pytest

from engkit import selection
from engkit.config import ConfigError, load


def _selection(cfg, changed: list[str], **kwargs):
    """便捷调用选测。

    Args:
        cfg: 配置。
        changed: 变更路径。
        **kwargs: 透传参数。

    Returns:
        selection.Selection: 选测结果。
    """
    return selection.build_selection(cfg, changed, **kwargs)


def test_discover_projects(cfg) -> None:
    """工程发现：库在前、服务在后，工程名取源码包名。"""
    projects = selection.discover_projects(cfg)
    assert [project.name for project in projects] == ["pkg_a", "svc"]
    assert projects[0].src == "backend/libs/pkg_a/src"
    assert projects[0].tests == "libs/pkg_a/tests"


def test_discover_skips_incomplete(build_repo) -> None:
    """缺 src / tests 或源码根无包时不计入。"""
    root = build_repo(
        files={
            "backend/libs/only_src/src/x.py": "",
            "backend/libs/empty/src/readme.txt": "",
            "backend/services/empty_pkg/src/dir_without_init/file.py": "",
            "backend/services/empty_pkg/tests/test_x.py": "",
        }
    )
    names = [project.name for project in selection.discover_projects(load(root / "engkit.toml"))]
    assert "only_src" not in names
    assert "empty_pkg" not in names


def test_backend_not_configured(build_repo) -> None:
    """未配置后端即报错。"""
    root = build_repo(config='[docs]\nroot = "docs"\n')
    with pytest.raises(RuntimeError, match="backend"):
        selection.discover_projects(load(root / "engkit.toml"))


def test_build_graph_without_projects(build_repo) -> None:
    """未发现任何工程即报错。"""
    root = build_repo(config='[docs]\nroot = "docs"\n[backend]\nroot = "nothing"\n')
    with pytest.raises(RuntimeError, match="未发现任何后端工程"):
        selection.build_graph(load(root / "engkit.toml"))


def test_module_of_source(cfg) -> None:
    """源文件 → 模块名（含包根文件与 `__init__`）。"""
    project = selection.discover_projects(cfg)[0]
    assert selection.module_of_source("backend/libs/pkg_a/src/pkg_a/core.py", project) == "pkg_a.core"
    assert selection.module_of_source("backend/libs/pkg_a/src/pkg_a/__init__.py", project) == "pkg_a"
    assert selection.module_of_source("backend/libs/pkg_a/src/other/x.py", project) == ""
    assert selection.module_of_source("backend/libs/pkg_a/src/pkg_a/x.txt", project) == ""


def test_parse_imports_variants(repo: Path) -> None:
    """绝对导入、相对导入与语法错误文件的解析。"""
    mod = repo / "backend/libs/pkg_a/src/pkg_a/mod.py"
    mod.write_text("import os\nfrom .util import helper\nfrom pkg_a import core\n", encoding="utf-8")
    found = selection.parse_imports(mod, "pkg_a.mod")
    assert {"os", "pkg_a.util", "pkg_a.util.helper", "pkg_a", "pkg_a.core"} <= found
    bad = repo / "backend/libs/pkg_a/src/pkg_a/bad.py"
    bad.write_text("def broken(:\n", encoding="utf-8")
    assert selection.parse_imports(bad, "pkg_a.bad") == set()
    assert selection.parse_imports(repo / "no_such.py", "x") == set()


def test_parse_imports_relative_edge(repo: Path) -> None:
    """相对导入层级越界与无模块名的情形。"""
    path = repo / "backend/libs/pkg_a/src/pkg_a/rel.py"
    path.write_text("from . import x\n", encoding="utf-8")
    assert selection.parse_imports(path, "pkg_a") == set()
    path.write_text("from .. import y\n", encoding="utf-8")
    assert selection.parse_imports(path, "a.b.c") == {"a", "a.y"}
    assert selection.parse_imports(path, "a.b") == set()
    assert selection.parse_imports(path, "") == set()


def test_isomorphic_candidates(repo: Path, cfg) -> None:
    """同构候选：同名测试优先，无同名退回同构目录，都没有则为空。"""
    project = selection.discover_projects(cfg)[0]
    backend = repo / "backend"
    assert selection._isomorphic_candidates(backend, "backend/libs/pkg_a/src/pkg_a/core.py", project, "test_") == (
        "libs/pkg_a/tests/core/test_core.py",
    )
    (repo / "backend/libs/pkg_a/tests/core").mkdir(parents=True, exist_ok=True)
    assert "libs/pkg_a/tests/core/test_core.py" in selection._isomorphic_candidates(
        backend, "backend/libs/pkg_a/src/pkg_a/core/x.py", project, "test_"
    )
    assert selection._isomorphic_candidates(backend, "backend/libs/pkg_a/src/pkg_a/zzz/x.py", project, "test_") == ()
    assert selection._isomorphic_candidates(backend, "backend/libs/pkg_a/src/pkg_a/x.txt", project, "test_") == ()


def test_isomorphic_same_name_prefix(repo: Path, cfg) -> None:
    """同名前缀变体（`test_<原名>_*.py`）一并命中，且只在本工程内。"""
    project = selection.discover_projects(cfg)[0]
    backend = repo / "backend"
    extra = repo / "backend/libs/pkg_a/tests/core/test_collections_chain.py"
    extra.write_text("def test_x():\n    assert True\n", encoding="utf-8")
    other = repo / "backend/services/svc/tests/test_collections_chain.py"
    other.write_text("def test_x():\n    assert True\n", encoding="utf-8")
    found = selection._isomorphic_candidates(
        backend, "backend/libs/pkg_a/src/pkg_a/core/collections.py", project, "test_"
    )
    assert "libs/pkg_a/tests/core/test_collections_chain.py" in found
    assert "services/svc/tests/test_collections_chain.py" not in found


def test_selection_transitive_import(cfg) -> None:
    """AST 传递收敛：改 util 会选中引用 core 的用例。"""
    result = _selection(cfg, ["backend/libs/pkg_a/src/pkg_a/util.py"])
    assert "libs/pkg_a/tests/core/test_core.py" in result.test_paths
    assert result.cov_modules == ("pkg_a.util",)


def test_selection_test_file_change(cfg) -> None:
    """测试文件自身变更必选。"""
    result = _selection(cfg, ["backend/libs/pkg_a/tests/test_root.py"])
    assert "libs/pkg_a/tests/test_root.py" in result.test_paths


def test_selection_global_change(repo: Path, cfg) -> None:
    """全局配置变更退化为全量。"""
    (repo / "backend/pyproject.toml").write_text("", encoding="utf-8")
    result = _selection(cfg, ["backend/pyproject.toml"])
    assert result.degraded is True
    assert "全局配置" in result.reason
    assert result.picked == result.total


def test_selection_config_prefix_rule(repo: Path, cfg) -> None:
    """`config_prefix` + 后缀命中即全局。"""
    result = _selection(cfg, ["backend/config.toml"])
    assert result.degraded is True
    assert _selection(cfg, ["backend/config.txt"]).degraded is False


def test_selection_force_full(cfg) -> None:
    """强制退化全量。"""
    result = _selection(cfg, [], force_full=True)
    assert result.degraded is True
    assert "强制退化全量" in result.reason


def test_selection_threshold_degrade(cfg) -> None:
    """超过基础模块阈值退化工程级全量。"""
    result = _selection(cfg, ["backend/libs/pkg_a/src/pkg_a/util.py"], base_threshold=1)
    assert result.degraded is True
    assert any("超过基础模块阈值" in item for item in result.notes)


def test_selection_no_referrer_degrade(repo: Path, cfg) -> None:
    """变更模块无测试引用者 ⇒ 退化工程级全量并说明。"""
    orphan = repo / "backend/libs/pkg_a/src/pkg_a/db/orphan.py"
    orphan.parent.mkdir(parents=True, exist_ok=True)
    orphan.write_text("X = 1\n", encoding="utf-8")
    result = _selection(cfg, ["backend/libs/pkg_a/src/pkg_a/db/orphan.py"])
    assert result.degraded is True
    assert any("无测试引用者" in item for item in result.notes)


def test_extra_tests_dir_suffix(repo: Path, cfg) -> None:
    """特殊路径（目录后缀）并入同构测试，且不计覆盖率。"""
    target = repo / "backend/libs/pkg_a/tests/ops/test_seed.py"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("def test_seed():\n    assert True\n", encoding="utf-8")
    result = _selection(cfg, ["backend/ops/seed.py"])
    assert "libs/pkg_a/tests/ops/test_seed.py" in result.test_paths
    assert result.cov_modules == ()
    assert any("并入同构测试" in item for item in result.notes)


def test_extra_tests_explicit_files(cfg) -> None:
    """特殊路径（显式测试文件）。"""
    result = _selection(cfg, ["scripts/tools/guard.py"])
    assert "libs/pkg_a/tests/core/test_core.py" in result.test_paths
    assert any("并入其护栏用例" in item for item in result.notes)


def test_extra_tests_mapping_and_fallback(cfg) -> None:
    """特殊路径（精确映射命中与未登记兜底）。"""
    hit = _selection(cfg, ["scripts/check/one.py"])
    assert "libs/pkg_a/tests/core/test_core.py" in hit.test_paths
    fallback = _selection(cfg, ["scripts/check/two.py"])
    assert "libs/pkg_a/tests/boundary/test_boundary.py" in fallback.test_paths


def test_unregistered_projects(build_repo) -> None:
    """自动发现的工程未登记到全量范围 / 覆盖率范围。"""
    root = build_repo(
        config='[docs]\nroot = "docs"\n[backend]\nroot = "backend"\nprojects = []\ncoverage_packages = []\n'
    )
    cfg = load(root / "engkit.toml")
    missing = selection.unregistered_projects(selection.discover_projects(cfg), cfg)
    assert any("全量档工程范围" in item for item in missing)
    assert any("覆盖率采集范围" in item for item in missing)
    assert selection.unregistered_projects([], cfg) == []
    assert selection.unregistered_projects([], load(root / "engkit.toml")) == []


def test_all_test_paths_and_tests_for_modules(cfg) -> None:
    """全量与扩圈辅助函数。"""
    assert len(selection.all_test_paths(cfg)) == 4
    assert "libs/pkg_a/tests/core/test_core.py" in selection.tests_for_modules(cfg, ["pkg_a.util"])
    assert selection.tests_for_modules(cfg, ["no.such.module"]) == []


def test_expand_from_coverage(tmp_path: Path) -> None:
    """覆盖率报告缺行 → 返回对应模块。"""
    report = tmp_path / "coverage.json"
    report.write_text(
        json.dumps({"files": {"a/pkg_a/core.py": {"missing_lines": [3]}, "a/pkg_a/util.py": {"missing_lines": []}}}),
        encoding="utf-8",
    )
    assert selection.expand_from_coverage(report, ["pkg_a.core", "pkg_a.util"]) == ["pkg_a.core"]
    assert selection.expand_from_coverage(tmp_path / "none.json", ["pkg_a.core"]) == []
    bad = tmp_path / "bad.json"
    bad.write_text("not json", encoding="utf-8")
    assert selection.expand_from_coverage(bad, ["pkg_a.core"]) == []


def test_expand_from_coverage_init(repo: Path) -> None:
    """包 `__init__` 的缺行同样识别。"""
    report = repo / "coverage.json"
    report.write_text(json.dumps({"files": {"a/pkg_a/__init__.py": {"missing_lines": [1]}}}), encoding="utf-8")
    assert selection.expand_from_coverage(report, ["pkg_a"]) == ["pkg_a"]


def test_read_changed_explicit_and_list(repo: Path) -> None:
    """显式文件与清单文件合并去重。"""
    listing = repo / "changes.txt"
    listing.write_text(" a.py \n\nb.py\n", encoding="utf-8")
    changed = selection.read_changed(repo, changed_files=("a.py",), changed_file_list=str(listing))
    assert changed == ["a.py", "b.py"]


def test_read_changed_missing_ref(repo: Path) -> None:
    """基准不可解析即抛错（调用方退化全量）。"""
    with pytest.raises(RuntimeError, match="变更基准不可解析"):
        selection.read_changed(repo, changed_from="no-such-ref")


def test_read_changed_from_git(git_repo: Path) -> None:
    """按 git 基准取变更。"""
    (git_repo / "new.py").write_text("X = 1\n", encoding="utf-8")
    assert "new.py" in selection.read_changed(git_repo, changed_from="HEAD")


def test_selection_ignores_unrelated(cfg) -> None:
    """纯文档变更不引测试与覆盖率。"""
    result = _selection(cfg, ["docs/文档首页.md"])
    assert result.test_paths == ()
    assert result.cov_modules == ()


def test_metrics_ordering(cfg) -> None:
    """结果按工程序排列，度量含逐工程条目。"""
    result = _selection(cfg, ["backend/libs/pkg_a/src/pkg_a/util.py"])
    assert result.projects[0].name == "pkg_a"
    assert result.projects[1].name == "svc"
    assert result.total == 4


def test_select_with_none_backend(cfg) -> None:
    """未配置后端时 `unregistered_projects` 返回空。"""
    patched = dataclasses.replace(cfg, backend=None)
    assert selection.unregistered_projects([], patched) == []


def test_direct_depth_skips_transitive_closure(cfg) -> None:
    """直接依赖深度：不再沿 import 链扩散（改 util 不命中只引用 core 的用例）。"""
    target = "backend/libs/pkg_a/src/pkg_a/util.py"
    assert "libs/pkg_a/tests/core/test_core.py" in _selection(cfg, [target]).test_paths
    direct = _selection(cfg, [target], depth="direct")
    assert "libs/pkg_a/tests/core/test_core.py" not in direct.test_paths
    assert direct.degraded is False


def test_direct_depth_uses_conftest_imports(repo: Path, cfg) -> None:
    """直接依赖深度：conftest 的直接 import 计入所覆盖测试的依赖。"""
    conftest = repo / "backend/libs/pkg_a/tests/conftest.py"
    conftest.write_text("from pkg_a.util import helper\n\n__all__ = ['helper']\n", encoding="utf-8")
    direct = _selection(cfg, ["backend/libs/pkg_a/src/pkg_a/util.py"], depth="direct")
    assert "libs/pkg_a/tests/core/test_core.py" in direct.test_paths


def test_conftest_scope_outside_project(cfg) -> None:
    """不在工程测试根下的路径不纳入任何 conftest 依赖。"""
    project = selection.discover_projects(cfg)[0]
    assert selection._conftest_scope("services/svc/tests/test_app.py", project, {}) == set()


def test_direct_depth_keeps_global_degrade(cfg) -> None:
    """直接依赖深度：全局配置 / 迁移 / 部署变更仍退化全量。"""
    result = _selection(cfg, ["backend/pyproject.toml"], depth="direct")
    assert result.degraded is True
    assert result.picked == result.total


def test_direct_depth_no_threshold_degrade(cfg) -> None:
    """直接依赖深度：不因受影响测试数超阈值而退化工程级全量。"""
    changed = ["backend/libs/pkg_a/src/pkg_a/util.py"]
    assert _selection(cfg, changed, base_threshold=1).degraded is True
    direct = _selection(cfg, changed, base_threshold=1, depth="direct")
    assert direct.degraded is False
    assert not any("超过基础模块阈值" in item for item in direct.notes)


def test_direct_depth_no_hit_note(repo: Path, cfg) -> None:
    """直接依赖深度：变更模块无测试命中时只说明、不退化。"""
    orphan = repo / "backend/libs/pkg_a/src/pkg_a/db/orphan.py"
    orphan.parent.mkdir(parents=True, exist_ok=True)
    orphan.write_text("X = 1\n", encoding="utf-8")
    result = _selection(cfg, ["backend/libs/pkg_a/src/pkg_a/db/orphan.py"], depth="direct")
    assert result.degraded is False
    assert any("无测试命中" in item for item in result.notes)


def test_config_depth(cfg, build_repo) -> None:
    """`[selection] depth` 解析与取值校验。"""
    assert cfg.depth == "closure"
    config = '[docs]\nroot = "docs"\n[selection]\ndepth = "direct"\n'
    root = build_repo(config=config)
    assert load(root / "engkit.toml").depth == "direct"
    bad = build_repo(config=config.replace('"direct"', '"deep"'))
    with pytest.raises(ConfigError, match=r"selection\.depth"):
        load(bad / "engkit.toml")
