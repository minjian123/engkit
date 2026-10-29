"""D6 出口导出面（`public_export_baseline`）用例：解析 / 结构非法 / 来源判定 / 端到端 / 输出。

夹具用 `sb.base_files()` + `sb.sample_rule()`（与自检矩阵同源），不复制第二份表格登记。
"""

from __future__ import annotations

import ast
import dataclasses
import json
from pathlib import Path

import pytest

from engkit.base import service_boundaries as sb
from engkit.config import Config


def make_repo(tmp_path: Path, **files: str) -> Path:
    """造一份合规夹具仓库（可按需覆盖写文件）。

    Args:
        tmp_path: 临时仓根。
        **files: 覆盖写文件（相对路径 → 文本）。

    Returns:
        Path: 仓根。
    """
    sb.write_files(tmp_path, sb.base_files())
    if files:
        sb.write_files(tmp_path, dict(files))
    return tmp_path


def cfg_of(root: Path) -> Config:
    """造配置对象（仓根即可，规则段由 `sb.sample_rule()` 提供）。

    Args:
        root: 仓根。

    Returns:
        Config: 配置对象。
    """
    return sb.mini_cfg(root)


def test_public_exports_of_shapes() -> None:
    """导出面解析：`__all__` 声明 + 顶层绑定（`as` 别名、属性导出、星号与私有名跳过）。"""
    tree = ast.parse(
        "from __future__ import annotations\n"
        "from alpha.dto import Foo as Foo\n"
        "from alpha.dto import *\n"
        "import alpha.models.m\n"
        "import alpha.models.n as n\n"
        "\n"
        "\n"
        "class Local:\n"
        "    pass\n"
        "\n"
        "\n"
        "async def pull():\n"
        "    pass\n"
        "\n"
        "\n"
        "def push():\n"
        "    pass\n"
        "\n"
        "\n"
        "VALUE = 1\n"
        "COUNT: int = 2\n"
        "BOTH = OTHER = 0\n"
        "_private = 0\n"
        "__all__ = ['Foo', 'Local', 1]\n"
        "__all__ += ['push']\n"
    )
    declared, exports = sb.public_exports_of(tree)
    assert declared == ("Foo", "Local", "push")
    assert [item.symbol for item in exports] == ["Foo", "alpha", "n", "Local", "pull", "push", "VALUE", "COUNT"]
    assert exports[0].source == "alpha.dto"
    assert exports[3].source == ""
    assert sb.string_items(ast.parse("X = 1").body[0].value) == []


def test_load_public_exports_errors(tmp_path: Path) -> None:
    """白名单结构非法分支：版本 / entries 形状 / 条目形状 / 缺字段 / 重复登记；缺文件返回空。"""
    target = tmp_path / "export.json"
    assert sb.load_public_exports(target) == ()
    good = {"domain": "alpha", "symbol": "CONST", "kind": "constant", "reason": "理由", "owner": "归口"}
    cases = [
        ({"version": 2, "entries": []}, "版本非法"),
        ({"version": 1, "entries": {}}, "须为数组"),
        ({"version": 1, "entries": ["x"]}, "须为对象"),
        ({"version": 1, "entries": [{"domain": "alpha"}]}, "字段缺失或为空"),
        ({"version": 1, "entries": [good, good]}, "重复登记"),
    ]
    for payload, marker in cases:
        target.write_text(json.dumps(payload), encoding="utf-8")
        with pytest.raises(ValueError, match=marker):
            sb.load_public_exports(target)
    target.write_text(json.dumps({"version": 1, "entries": [good]}), encoding="utf-8")
    assert len(sb.load_public_exports(target)) == 1


def test_public_export_kind_of(tmp_path: Path) -> None:
    """来源判定：模型 / 仓储 / api / 域内实现各自命中；共享库包与包外文件不判。"""
    base = "pkg/services/alpha/src/alpha/domains/alpha"
    root = make_repo(tmp_path, **{f"{base}/models.py": '__tablename__ = "sys_alpha"\n'})
    rule = sb.sample_rule()
    cfg = cfg_of(root)
    checker = sb.Checker(cfg, rule, sb.load_registry(cfg, rule))
    checker.packages = sb.discover_packages(cfg, rule)
    assert checker.public_export_kind_of(f"{base}/models.py") == "orm"
    assert checker.public_export_kind_of(f"{base}/repositories/a.py") == "repository"
    assert checker.public_export_kind_of(f"{base}/api/a.py") == "api_layer"
    assert checker.public_export_kind_of(f"{base}/services/a.py") == "service_impl"
    assert checker.public_export_kind_of(f"{base}/dto.py") is None
    assert checker.public_export_kind_of("pkg/libs/core/src/core/api/a.py") is None
    assert checker.public_export_kind_of("elsewhere/x.py") is None


def test_public_export_check_and_finish(tmp_path: Path) -> None:
    """端到端：导出模型拦截 / 声明与绑定不一致 / 白名单放行 / 陈旧条目失败 / 未配置即停用。"""
    layout_path = "deploy/boundaries/domain_layout.json"
    baseline_path = "deploy/boundaries/domain_public_export_baseline.json"
    public = "pkg/services/alpha/src/alpha/domains/alpha/public.py"
    layout = {
        "version": 1,
        "domains": [
            {
                "domain_key": "alpha",
                "service_key": "alpha",
                "status": "placement",
                "paths": ["pkg/services/alpha/src/alpha/domains/alpha/**/*.py"],
                "public_api": ["alpha.domains.alpha.public"],
            }
        ],
        "shared_paths": [
            *sb.SAMPLE_SHARED_PATHS,
            "pkg/services/alpha/src/alpha/domains/__init__.py",
            "pkg/services/alpha/src/alpha/domains/alpha/__init__.py",
        ],
    }
    root = make_repo(
        tmp_path,
        **{
            layout_path: json.dumps(layout),
            "pkg/services/alpha/src/alpha/domains/__init__.py": "",
            "pkg/services/alpha/src/alpha/domains/alpha/__init__.py": "",
            "pkg/services/alpha/src/alpha/domains/alpha/models.py": '__tablename__ = "sys_alpha"\n',
            "pkg/services/alpha/src/alpha/domains/alpha/dto.py": "VALUE = 0\n",
        },
    )
    rule = sb.sample_rule()
    cfg = cfg_of(root)

    sb.write_files(root, {public: "from alpha.domains.alpha.models import SysThing as SysThing\n"})
    assert sb.check(cfg, rule).counts["public_export_violation"] == 1

    sb.write_files(root, {public: 'VALUE = 0\n__all__ = ["Ghost"]\n'})
    assert sb.check(cfg, rule).counts["public_export_violation"] == 1

    sb.write_files(root, {public: "from alpha.domains.alpha.dto import VALUE as VALUE\n__all__ = ['VALUE']\n"})
    assert sb.check(cfg, rule).counts["public_export_violation"] == 0

    sb.write_files(root, {public: "from alpha.not_exist import Thing as Thing\n"})
    assert sb.check(cfg, rule).counts["public_export_violation"] == 0

    entry = {"domain": "alpha", "symbol": "SysThing", "kind": "constant", "reason": "夹具", "owner": "夹具"}
    sb.write_files(
        root,
        {
            public: "from alpha.domains.alpha.models import SysThing as SysThing\n",
            baseline_path: json.dumps({"version": 1, "entries": [entry]}),
        },
    )
    allowed = sb.check(cfg, rule)
    assert allowed.counts["public_export_exempt_total"] == 1
    assert allowed.counts["public_export_exempt_used"] == 1
    assert allowed.counts["public_export_violation"] == 0

    sb.write_files(root, {public: "from alpha.domains.alpha.dto import VALUE as VALUE\n"})
    stale = sb.check(cfg, rule)
    assert any("出口白名单陈旧" in problem for problem in stale.problems)

    off = dataclasses.replace(rule, public_export_baseline="")
    assert sb.check(cfg, off).counts["public_export_violation"] == 0


def test_emit_export_line(capsys: pytest.CaptureFixture[str]) -> None:
    """输出：出口导出面一行只在有登记或违规时打印。"""
    sb.emit(sb.Outcome(counts={"public_export_exempt_total": 2, "public_export_exempt_used": 1}), as_json=False)
    assert "出口导出面" in capsys.readouterr().out
    sb.emit(sb.Outcome(counts={}), as_json=False)
    assert "出口导出面" not in capsys.readouterr().out


def test_public_export_skipped_and_broken(tmp_path: Path) -> None:
    """早退与容错：出口数不为 1 / 出口模块缺失 / 出口文件语法非法 / 白名单结构非法。"""
    layout_path = "deploy/boundaries/domain_layout.json"
    baseline_path = "deploy/boundaries/domain_public_export_baseline.json"
    public = "pkg/services/alpha/src/alpha/domains/alpha/public.py"
    shared = [
        *sb.SAMPLE_SHARED_PATHS,
        "pkg/services/alpha/src/alpha/domains/__init__.py",
        "pkg/services/alpha/src/alpha/domains/alpha/__init__.py",
    ]
    root = make_repo(
        tmp_path,
        **{
            "pkg/services/alpha/src/alpha/domains/__init__.py": "",
            "pkg/services/alpha/src/alpha/domains/alpha/__init__.py": "",
        },
    )
    rule = sb.sample_rule()
    cfg = cfg_of(root)

    def write_layout(public_api: list[str]) -> None:
        layout = {
            "version": 1,
            "domains": [
                {
                    "domain_key": "alpha",
                    "service_key": "alpha",
                    "status": "placement",
                    "paths": ["pkg/services/alpha/src/alpha/domains/alpha/**/*.py"],
                    "public_api": public_api,
                }
            ],
            "shared_paths": shared,
        }
        sb.write_files(root, {layout_path: json.dumps(layout)})

    write_layout([])
    assert sb.check(cfg, rule).counts["public_export_violation"] == 0

    write_layout(["alpha.domains.alpha.missing"])
    assert sb.check(cfg, rule).counts["public_export_violation"] == 0

    write_layout(["alpha.domains.alpha.public"])
    sb.write_files(root, {public: "", baseline_path: "{"})
    assert any("[出口白名单]" in problem for problem in sb.check(cfg, rule).problems)
