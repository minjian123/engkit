"""推送环静态范围（Kiwi 2446）：反向依赖闭包与退化条件。"""

from __future__ import annotations

import dataclasses
from typing import Any

import pytest

from engkit import preflight, selection
from engkit.config import Config, load

pytestmark = pytest.mark.kiwi_id(2446)

UTIL = "backend/libs/pkg_a/src/pkg_a/util.py"
CORE = "backend/libs/pkg_a/src/pkg_a/core.py"


def _cfg(build_repo: Any) -> tuple[Config, Any]:
    """造缺省迷你仓并取配置。

    Args:
        build_repo: 造仓工厂。

    Returns:
        tuple[Config, Any]: （配置, 仓库根）。
    """
    root = build_repo()
    return load(root / "engkit.toml"), root


def test_reverse_scope_contains_dependents(build_repo: Any) -> None:
    """改被依赖的模块 → 闭包含其反向依赖者，不含无关工程。"""
    cfg, _ = _cfg(build_repo)
    scope = selection.reverse_scope(cfg, [UTIL])
    assert scope.degraded is False
    assert UTIL in scope.files
    assert CORE in scope.files
    assert "backend/services/svc/src/svc/app.py" not in scope.files


def test_global_change_degrades(build_repo: Any) -> None:
    """全局输入（依赖清单）→ 退化全仓。"""
    cfg, _ = _cfg(build_repo)
    scope = selection.reverse_scope(cfg, ["backend/pyproject.toml"])
    assert scope.degraded is True
    assert "全局变更" in scope.reason


def test_unmapped_file_degrades(build_repo: Any) -> None:
    """改动文件不在已登记工程内 → 退化全仓。"""
    cfg, _ = _cfg(build_repo)
    scope = selection.reverse_scope(cfg, ["backend/libs/pkg_a/src/other/x.py"])
    assert scope.degraded is True
    assert "不在已登记工程内" in scope.reason


def test_non_source_change_degrades(build_repo: Any) -> None:
    """改动不含后端源码 → 退化全仓。"""
    cfg, _ = _cfg(build_repo)
    scope = selection.reverse_scope(cfg, ["docs/文档首页.md"])
    assert scope.degraded is True
    assert "不含后端源码" in scope.reason


def test_without_backend_config(build_repo: Any) -> None:
    """未配置后端 → 退化（不抛异常）。"""
    cfg, _ = _cfg(build_repo)
    scope = selection.reverse_scope(dataclasses.replace(cfg, backend=None), [UTIL])
    assert scope.degraded is True
    assert "未配置" in scope.reason


def test_without_projects(build_repo: Any) -> None:
    """后端根下无工程 → 退化。"""
    cfg, _ = _cfg(build_repo)
    assert cfg.backend is not None
    narrowed = dataclasses.replace(cfg, backend=dataclasses.replace(cfg.backend, root="noback"))
    scope = selection.reverse_scope(narrowed, [UTIL])
    assert scope.degraded is True
    assert "未发现" in scope.reason


def test_threshold_degrades(build_repo: Any) -> None:
    """闭包规模接近全仓 → 退化。"""
    cfg, _ = _cfg(build_repo)
    scope = selection.reverse_scope(dataclasses.replace(cfg, base_ratio=0.1), [UTIL])
    assert scope.degraded is True
    assert "接近全仓" in scope.reason


def test_reverse_edges_skips_external_imports() -> None:
    """第三方导入不产生反向边（不在模块表内的前缀直接忽略）。"""
    assert selection._reverse_edges({"a": {"numpy", "b.c"}}) == {}


def test_apply_scope_appends_when_no_root_token(build_repo: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    """命令里没有仓根参数时把清单追加到末尾。"""
    cfg, _ = _cfg(build_repo)
    step = dataclasses.replace(_step(), scope="changed-closure")
    fake = selection.ReverseScope(files=(UTIL,), degraded=False)
    monkeypatch.setattr(selection, "reverse_scope", lambda *_args: fake)
    assert preflight._apply_scope(cfg, step, ["pyright"], [UTIL]) == ["pyright", UTIL]


def test_apply_scope_degrades_on_argv_budget(build_repo: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    """闭包清单超出命令行预算 → 退化全仓。"""
    cfg, _ = _cfg(build_repo)
    step = dataclasses.replace(_step(), scope="changed-closure")
    wide = selection.ReverseScope(files=tuple(f"backend/{'x' * 50}{index}.py" for index in range(200)), degraded=False)
    monkeypatch.setattr(selection, "reverse_scope", lambda *_args: wide)
    assert preflight._apply_scope(cfg, step, ["pyright", "."], [UTIL]) == ["pyright", "."]


def test_apply_scope_degrades_when_reason(build_repo: Any) -> None:
    """闭包退化时原样执行并保留原命令。"""
    cfg, _ = _cfg(build_repo)
    step = dataclasses.replace(_step(), scope="changed-closure")
    assert preflight._apply_scope(cfg, step, ["pyright", "."], ["backend/pyproject.toml"]) == ["pyright", "."]


def _step() -> Any:
    """构造一个 pyright 步骤。

    Returns:
        Any: 步骤对象。
    """
    from engkit.config import Step

    return Step(label="静态：pyright", cmd=("pyright", "."))
