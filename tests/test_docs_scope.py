"""文档口径护栏用例。"""

from __future__ import annotations

import dataclasses

import pytest

from engkit import docs_scope
from engkit.config import ScopeConfig, ScopeRule, load


def test_check_clean(cfg) -> None:
    """白名单与计划目录外的文档无违规。"""
    report = docs_scope.check(cfg)
    assert report.findings == ()
    assert report.files == 2


def test_check_reports_violation(build_repo) -> None:
    """任务文档出现进度类字段即报违规。"""
    root = build_repo(files={"docs/项目/X/任务/01_任务/02_子.md": "# 子\n\n本任务 4h 交付，排期见计划。\n"})
    report = docs_scope.check(load(root / "engkit.toml"))
    assert len(report.findings) == 1
    assert report.findings[0].file == "项目/X/任务/01_任务/02_子.md"
    assert report.findings[0].label == "排期字样"


def test_allow_marker(build_repo) -> None:
    """行内豁免标记生效。"""
    root = build_repo(
        files={"docs/项目/X/任务/01_任务/02_子.md": "# 子\n\n引用规范原文 <!-- docs-scope:allow:引用规范原文 -->\n"}
    )
    assert docs_scope.check(load(root / "engkit.toml")).findings == ()


def test_extra_rules_hit(cfg) -> None:
    """追加规则命中。"""
    scope = dataclasses.replace(cfg.docs.scope, extra_rules=(ScopeRule(r"死线", "死线字样"),))
    patched = dataclasses.replace(cfg, docs=dataclasses.replace(cfg.docs, scope=scope))
    findings = docs_scope.scan_text("死线在周五", patched)
    assert findings[0].label == "死线字样"


def test_whitelist_variants(cfg) -> None:
    """白名单三类（目录 / 文件 / 计划目录）。"""
    assert docs_scope.is_whitelisted("规范/某规范.md", cfg) is True
    assert docs_scope.is_whitelisted("规划/总体项目规划.md", cfg) is True
    assert docs_scope.is_whitelisted("项目/A/计划/01_计划.md", cfg) is True
    assert docs_scope.is_whitelisted("项目/A/任务/01/01.md", cfg) is False


def test_whitelist_empty_config(cfg) -> None:
    """白名单为空时一律不豁免。"""
    patched = dataclasses.replace(cfg, docs=dataclasses.replace(cfg.docs, scope=ScopeConfig()))
    assert docs_scope.is_whitelisted("规范/某规范.md", patched) is False
    assert docs_scope.is_whitelisted("项目/A/计划/01.md", patched) is False


def test_missing_docs_root(cfg) -> None:
    """文档根不存在时报错。"""
    patched = dataclasses.replace(cfg, docs=dataclasses.replace(cfg.docs, root="缺失"))
    with pytest.raises(FileNotFoundError):
        docs_scope.check(patched)


def test_default_rules_cover_samples(cfg) -> None:
    """内置规则覆盖各类词面（数字工时不误报时长约束）。"""
    assert docs_scope.scan_text("工时 8h", cfg)
    assert docs_scope.scan_text("M2 达成", cfg)
    assert not docs_scope.scan_text("RTO ≤4h，版本 3.14", cfg)
