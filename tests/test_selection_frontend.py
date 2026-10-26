"""前端选测用例（workspace 依赖反向闭包 + 根配置退化）。"""

from __future__ import annotations

import dataclasses

from engkit import selection


def test_discover_packages(cfg) -> None:
    """包发现：名称与工作区依赖。"""
    packages = selection.discover_frontend_packages(cfg)
    assert [package.name for package in packages] == ["@mini/core", "@mini/desktop"]
    assert packages[1].workspace_deps == ("@mini/core",)
    assert packages[0].path == "frontend/packages/core"


def test_discover_without_frontend(cfg) -> None:
    """未配置前端时返回空。"""
    assert selection.discover_frontend_packages(dataclasses.replace(cfg, frontend=None)) == []


def test_discover_skips_bad_manifests(build_repo) -> None:
    """坏清单（无法解析 / 缺 name / 非目录）被跳过。"""
    root = build_repo(
        files={
            "frontend/packages/broken/package.json": "{not json",
            "frontend/packages/noname/package.json": '{"version": "1.0.0"}',
            "frontend/packages/plain.txt": "x",
        }
    )
    from engkit.config import load

    names = [package.name for package in selection.discover_frontend_packages(load(root / "engkit.toml"))]
    assert names == ["@mini/core", "@mini/desktop"]


def test_frontend_closure(repo, cfg) -> None:
    """依赖反向闭包：改 core 会带上依赖它的 desktop。"""
    result = selection.build_frontend_selection(cfg, ["frontend/packages/core/src/index.ts"])
    assert result.packages == ("@mini/core", "@mini/desktop")
    assert result.degraded is False
    assert any("反向闭包" in item for item in result.notes)


def test_frontend_direct_only(cfg) -> None:
    """只改叶子包时只选中该包。"""
    result = selection.build_frontend_selection(cfg, ["frontend/apps/desktop/src/main.ts"])
    assert result.packages == ("@mini/desktop",)


def test_frontend_root_config_degrades(cfg) -> None:
    """根级配置变更退化为全部包。"""
    result = selection.build_frontend_selection(cfg, ["package.json"])
    assert result.degraded is True
    assert result.packages == ("@mini/core", "@mini/desktop")
    assert result.reason


def test_frontend_root_prefix_degrades(cfg) -> None:
    """根级共享脚本目录变更退化为全部包。"""
    assert selection.build_frontend_selection(cfg, ["scripts/build.mjs"]).degraded is True


def test_frontend_unattributed_file_degrades(cfg) -> None:
    """前端根下未归属任何包的变更退化为全部包。"""
    assert selection.build_frontend_selection(cfg, ["frontend/tsconfig.json"]).degraded is True


def test_frontend_force_full(cfg) -> None:
    """强制退化全量。"""
    result = selection.build_frontend_selection(cfg, [], force_full=True)
    assert result.degraded is True
    assert "强制退化全部前端包" in result.reason


def test_frontend_unrelated_change(cfg) -> None:
    """非前端变更不引包。"""
    result = selection.build_frontend_selection(cfg, ["backend/x.py"])
    assert result.packages == ()
    assert result.degraded is False


def test_frontend_not_configured(cfg) -> None:
    """未配置前端时返回空结果。"""
    result = selection.build_frontend_selection(dataclasses.replace(cfg, frontend=None), ["frontend/x.ts"])
    assert result.packages == () and result.degraded is False
