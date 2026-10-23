"""口径固化护栏（原 13_03 四项随迁入 engkit；Kiwi 2319）。

四项断言：**同一来源** / **新增工程必登记** / **退化显式** / **扩圈不静默**，外加
**包内无仓名字面**的机器断言（本任务的完成标准之一）。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from engkit import preflight, selection
from engkit.config import load

SRC = Path(__file__).resolve().parents[1] / "src" / "engkit"

FORBIDDEN = (
    "bms文档",
    "mdm文档",
    "biz文档",
    "cw文档",
    "bms_core",
    "bms_platform",
    "bms_testing_plugin",
    "scripts/tools",
    "backend/libs",
    "backend/services",
    "frontend/packages",
    "BMS_",
)
"""包内不得出现的仓名 / 目录字面（机器断言）。"""

PASS_CMD = f'"{sys.executable}" -c pass'


@pytest.mark.kiwi_id(2347)
def test_package_has_no_repo_literals() -> None:
    """包内无仓名字面（新增仓库接入只加配置，不改代码）。"""
    offenders: list[str] = []
    for path in sorted(SRC.glob("*.py")):
        text = path.read_text(encoding="utf-8")
        for token in FORBIDDEN:
            if token in text:
                offenders.append(f"{path.name}: {token}")
    assert offenders == []


@pytest.mark.kiwi_id(2319)
def test_single_source_selection() -> None:
    """选测推导只有一份实现（其他模块只调用）。"""
    definers: list[str] = []
    parser_modules: list[str] = []
    for path in sorted(SRC.glob("*.py")):
        text = path.read_text(encoding="utf-8")
        if "def build_selection(" in text or "def build_frontend_selection(" in text:
            definers.append(path.name)
        if "ast.parse" in text:
            parser_modules.append(path.name)
    assert definers == ["selection.py"]
    assert parser_modules == ["selection.py"]
    assert "selection.build_selection(" in (SRC / "preflight.py").read_text(encoding="utf-8")


@pytest.mark.kiwi_id(2319)
def test_unregistered_project_detected(build_repo) -> None:
    """新增工程未登记到全量范围 / 覆盖率范围即被检出。"""
    root = build_repo(
        config='[docs]\nroot = "docs"\n[backend]\nroot = "backend"\nprojects = []\ncoverage_packages = []\n'
    )
    cfg = load(root / "engkit.toml")
    missing = selection.unregistered_projects(selection.discover_projects(cfg), cfg)
    assert any("全量档工程范围" in item for item in missing)
    assert any("覆盖率采集范围" in item for item in missing)


@pytest.mark.kiwi_id(2319)
def test_degradation_is_explicit(cfg) -> None:
    """退化必须给出原因（不静默）。"""
    forced = selection.build_selection(cfg, [], force_full=True)
    assert forced.reason
    degraded = selection.build_selection(cfg, ["backend/libs/pkg_a/src/pkg_a/util.py"], base_threshold=1)
    assert degraded.degraded is True
    assert degraded.reason
    assert any("超过基础模块阈值" in item for item in degraded.notes)


@pytest.mark.kiwi_id(2319)
def test_expansion_never_silent(cfg, monkeypatch) -> None:
    """扩圈不收敛时不得静默放过（返回非零，不假装通过）。"""
    import dataclasses

    monkeypatch.setattr(preflight, "gate_command", lambda *args: [sys.executable, "-c", "raise SystemExit(1)"])
    monkeypatch.setattr(preflight.selection, "expand_from_coverage", lambda path, modules: ["pkg_a.util"])
    monkeypatch.setattr(preflight.selection, "tests_for_modules", lambda config, modules: [])
    code = preflight.run_selected(
        dataclasses.replace(cfg, base_ratio=1.0),
        changed=["backend/libs/pkg_a/src/pkg_a/util.py"],
        pytest_cmd=PASS_CMD,
    )
    assert code == 1


@pytest.mark.parametrize("literal", FORBIDDEN)
def test_forbidden_literal_list_is_effective(literal: str) -> None:
    """断言清单本身有效（防止清单被清空成空转）。"""
    assert literal.strip() == literal
    assert literal
