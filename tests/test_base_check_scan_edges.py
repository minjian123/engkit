"""扫描面与登记源解析的边角用例（服务边界校验的剩余分支）。

覆盖：服务目录前缀归集、枚举 / 表声明解析的兜底、跨库引用去重与豁免、运维脚本闭包环、
镜像 COPY 面的两类来源、域级 import 与表声明的跳过条件、`placement` 形态的模型单文件例外。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from engkit.base import service_boundaries as sb


def sample(**overrides: object) -> sb.ServiceBoundariesRules:
    """取夹具规则。

    Args:
        **overrides: 字段覆盖。

    Returns:
        sb.ServiceBoundariesRules: 规则。
    """
    rule = sb.sample_rule()
    return dataclasses_replace(rule, **overrides) if overrides else rule


def dataclasses_replace(rule: sb.ServiceBoundariesRules, **overrides: object) -> sb.ServiceBoundariesRules:
    """替换规则字段（避免各处重复 import dataclasses）。

    Args:
        rule: 规则。
        **overrides: 字段覆盖。

    Returns:
        sb.ServiceBoundariesRules: 新规则。
    """
    import dataclasses

    return dataclasses.replace(rule, **overrides)


def cfg_of(root: Path, rule: sb.ServiceBoundariesRules | None = None) -> object:
    """造配置对象。

    Args:
        root: 仓根。
        rule: 规则段。

    Returns:
        object: 配置。
    """
    import dataclasses

    return dataclasses.replace(sb.mini_cfg(root), boundaries_rules=rule, metrics_rules=None)


def build(tmp_path: Path, **files: object) -> Path:
    """造夹具仓库。

    Args:
        tmp_path: 临时仓根。
        **files: 覆盖写文件。

    Returns:
        Path: 仓根。
    """
    sb.write_files(tmp_path, sb.base_files())
    if files:
        sb.write_files(tmp_path, {str(key): str(value) for key, value in files.items()})
    return tmp_path


def test_registry_prefix_and_enum_edges() -> None:
    """服务目录前缀归集：空标签 / 空前缀跳过，同服务重复前缀只记一次；枚举类无字符串成员跳过。"""
    registry = sb.Registry(
        tables=(sb.TableRecord(table_name="sys_a", owner="alpha", domain="alpha"),),
        catalog=(
            sb.CatalogRecord(module_key="", table_prefix="x_"),
            sb.CatalogRecord(module_key="alpha", table_prefix=""),
            sb.CatalogRecord(module_key="alpha", table_prefix="alpha_"),
            sb.CatalogRecord(module_key="alpha", table_prefix="alpha_"),
        ),
    )
    assert registry.service_prefixes() == {"alpha": ("alpha_",)}


def test_parse_module_edges(tmp_path: Path) -> None:
    """枚举与模块常量解析的兜底分支（无值标注 / 无字符串成员 / 多目标赋值）。"""
    root = build(
        tmp_path,
        **{
            "pkg/odd.py": (
                "class Empty:\n    VALUE = 1\n\n"
                "class Mixed:\n    A = 'a'\n    B: str\n\n"
                "TABLE_OWNERSHIP = (rec(table_name='sys_a', owner='alpha', domain='alpha'),)\n"
            )
        },
    )
    rule = sample(ownership_module="pkg/odd.py")
    registry = sb.load_registry(cfg_of(root, rule), rule)
    assert registry.names() == ("sys_a",)


def test_declared_tables_non_constant(tmp_path: Path) -> None:
    """`__tablename__` 非字符串常量不计声明。"""
    path = tmp_path / "m.py"
    path.write_text('__tablename__ = name\n__tablename__ = "sys_a"\n', encoding="utf-8")
    assert sb.declared_tables(path) == [(2, "sys_a")]


def test_table_prefix_of_without_underscore() -> None:
    """无下划线表名前缀原样返回（例外匹配与文案口径）。"""
    assert sb.table_prefix_of("demo") == "demo"
    assert sb.table_prefix_of("sys_a") == "sys_"


def test_cross_reference_token_and_dedup(tmp_path: Path) -> None:
    """表名 token 扫描（标识符形态）与同行同表去重。"""
    package = tmp_path / "pkg/services/alpha/src/alpha"
    sb.write_files(
        tmp_path,
        {
            "pkg/services/alpha/src/alpha/__init__.py": "",
            "pkg/services/alpha/src/alpha/repo.py": 'PAIR = "sys_a.sys_a"\nDOC = "包含 sys_a 的说明"\n',
        },
    )
    found = sb.iter_cross_reference_tables(package, ("sys_a",), tmp_path)
    assert [(table, operation) for _rel, _line, table, operation in found] == [("sys_a", "unknown")]


def test_exempt_module_and_duplicate_reference(tmp_path: Path) -> None:
    """豁免模块不计跨库访问；同文件重复 SQL 只计一次。"""
    root = build(
        tmp_path,
        **{
            "pkg/registry/services_catalog.py": 'SQL = "select * from beta_log"\n',
            "pkg/services/alpha/src/alpha/api/repo.py": ('A, B = "select * from beta_log", "select * from beta_log"\n'),
            "pkg/services/alpha/src/alpha/api/m.py": "",
        },
    )
    rule = sample()
    sb.write_files(
        root,
        {"pkg/services/beta/src/beta/models/m.py": '__tablename__ = "beta_log"\n'},
    )
    outcome = sb.check(cfg_of(root, rule), rule)
    assert outcome.counts["cross_service_access_violation"] == 1


def test_ops_closure_cycle(tmp_path: Path) -> None:
    """运维脚本互导成环时闭包收敛（不重复展开）。"""
    root = build(
        tmp_path,
        **{
            "pkg/ops/a.py": 'RUN_IMAGE = "ci"\nimport ops.b\nfrom alpha import models\n',
            "pkg/ops/b.py": 'RUN_IMAGE = "ci"\nimport ops.a\n',
        },
    )
    rule = sample()
    outcome = sb.check(cfg_of(root, rule), rule)
    assert outcome.counts["ops_import_closure_violation"] == 0


def test_dockerfile_branches(tmp_path: Path) -> None:
    """镜像 COPY 面：非 COPY 行跳过 / 服务源码非参数化 COPY / 非共享库包。"""
    root = build(
        tmp_path,
        **{
            "pkg/Dockerfile": (
                "FROM python:3.14\n"
                "COPY libs/core/src libs/core/src\n"
                "COPY services/alpha/src services/alpha/src\n"
                "COPY ops ./ops\n"
            )
        },
    )
    rule = sample()
    outcome = sb.check(cfg_of(root, rule), rule)
    assert outcome.counts["ops_image_packages_drift"] >= 1


def test_runtime_dependency_group_missing(tmp_path: Path) -> None:
    """服务分组目录缺失 / 服务分组为空时运行期扫描降级。"""
    root = build(tmp_path)
    rule = sample(service_groups=("services", "ghost"))
    outcome = sb.check(cfg_of(root, rule), rule)
    assert outcome.counts["runtime_ops_dependency"] == 0
    empty = sample(service_groups=())
    assert sb.check(cfg_of(root, empty), empty).counts["runtime_ops_dependency"] == 0


def test_domain_baseline_hit_twice(tmp_path: Path) -> None:
    """同一行文本的跨域引用命中基线后再次出现时只计一次命中。"""
    alpha_file = "pkg/services/alpha/src/alpha/services/mod.py"
    beta_file = "pkg/services/alpha/src/alpha/services/other.py"
    root = build(
        tmp_path,
        **{
            beta_file: "VALUE = 0\n",
            alpha_file: "import alpha.services.other\n# x\nimport alpha.services.other\n",
        },
    )
    rule = sample()
    fingerprint = sb.fingerprint("import alpha.services.other")
    sb.write_files(
        root,
        {
            rule.layout: json.dumps(sb.domain_layout([alpha_file], [beta_file])),
            rule.baseline: json.dumps(
                {
                    "version": 1,
                    "entries": [
                        {
                            "kind": "import",
                            "domain": "alpha",
                            "target": "beta",
                            "file": alpha_file,
                            "fingerprint": fingerprint,
                        }
                    ],
                }
            ),
        },
    )
    outcome = sb.check(cfg_of(root, rule), rule)
    assert outcome.counts["domain_baseline_used"] == 1


def test_domain_import_skips(tmp_path: Path) -> None:
    """域级 import：目标模块不可解析 / 跨包非共享模型 / 目标无域或同域均跳过。"""
    alpha_file = "pkg/services/alpha/src/alpha/services/mod.py"
    root = build(
        tmp_path,
        **{
            alpha_file: (
                "import alpha.services.ghost\nimport beta.models.m\nimport core.core\nimport alpha.services.mod\n"
            ),
            "pkg/services/beta/src/beta/models/m.py": "",
            "pkg/libs/core/src/core/core/__init__.py": "",
        },
    )
    rule = sample(service_packages=("platform", "alpha", "beta"))
    sb.write_files(
        root,
        {rule.layout: json.dumps(sb.domain_layout([alpha_file], ["pkg/services/alpha/src/alpha/services/other.py"]))},
    )
    sb.write_files(root, {"pkg/services/alpha/src/alpha/services/other.py": "VALUE = 0\n"})
    outcome = sb.check(cfg_of(root, rule), rule)
    assert outcome.counts["cross_domain_import"] == 0


def test_domain_table_declaration_skips(tmp_path: Path) -> None:
    """表声明：无归属域跳过；跨域声明计入 D3 债务。"""
    alpha_file = "pkg/services/alpha/src/alpha/services/mod.py"
    root = build(
        tmp_path,
        **{
            alpha_file: '__tablename__ = "demo"\nOTHER = 1\n',
            "pkg/services/alpha/src/alpha/services/other.py": '__tablename__ = "beta_log"\n',
        },
    )
    rule = sample(service_packages=("platform", "alpha", "beta"))
    sb.write_files(
        root,
        {
            rule.layout: json.dumps(
                sb.domain_layout(
                    [alpha_file, "pkg/services/alpha/src/alpha/services/other.py"],
                    ["pkg/services/alpha/src/alpha/services/second.py"],
                )
            )
        },
    )
    sb.write_files(root, {"pkg/services/alpha/src/alpha/services/second.py": "VALUE = 0\n"})
    outcome = sb.check(cfg_of(root, rule), rule)
    assert outcome.counts["cross_domain_table_declare"] >= 1


def test_domain_table_skip_branches(tmp_path: Path) -> None:
    """跨域表引用：本文件已声明 / 同服务越界 / 重复命中 三条跳过路径。"""
    alpha_file = "pkg/services/alpha/src/alpha/services/mod.py"
    root = build(
        tmp_path,
        **{
            alpha_file: (
                '__tablename__ = "alpha_second"\nA = "select * from alpha_second"\nB = "select * from beta_log"\n'
            ),
            "pkg/services/alpha/src/alpha/services/dup.py": ('A, C = "select * from alpha_second", "alpha_second"\n'),
            "pkg/services/beta/src/beta/models/m.py": '__tablename__ = "beta_log"\n',
            "pkg/tables.py": (
                "TABLE_OWNERSHIP = (\n"
                '    rec(table_name="sys_alpha", owner="alpha", domain="alpha"),\n'
                '    rec(table_name="beta_log", owner="beta", domain="beta"),\n'
                '    rec(table_name="alpha_second", owner="alpha", domain="beta"),\n'
                ")\n"
            ),
            "pkg/services/alpha/src/alpha/services/other.py": "VALUE = 0\n",
        },
    )
    rule = sample(service_packages=("platform", "alpha", "beta"))
    sb.write_files(
        root,
        {
            rule.layout: json.dumps(
                sb.domain_layout(
                    [alpha_file, "pkg/services/alpha/src/alpha/services/dup.py"],
                    [
                        "pkg/services/alpha/src/alpha/services/other.py",
                        "pkg/services/beta/src/beta/models/m.py",
                    ],
                )
            )
        },
    )
    outcome = sb.check(cfg_of(root, rule), rule)
    assert outcome.counts["cross_domain_table_reference"] == 1


def test_placement_model_file_and_public_api_outside(tmp_path: Path) -> None:
    """`placement`：模型单文件例外放行；公共出口不在域目录内即报。"""
    root = build(
        tmp_path,
        **{
            "pkg/services/alpha/src/alpha/domains/__init__.py": "",
            "pkg/services/alpha/src/alpha/domains/alpha/__init__.py": "",
            "pkg/services/alpha/src/alpha/domains/alpha/model.py": '__tablename__ = "sys_alpha"\n',
            "pkg/services/beta/src/beta/models/m.py": "",
            "pkg/services/beta/src/beta/api/public.py": "",
        },
    )
    rule = sample()
    layout = {
        "version": 1,
        "domains": [
            {
                "domain_key": "alpha",
                "service_key": "platform",
                "status": "placement",
                "paths": ["pkg/services/alpha/src/alpha/domains/alpha/**/*.py"],
                "public_api": ["beta.api.public"],
            }
        ],
        "shared_paths": [*sb.SAMPLE_SHARED_PATHS, "pkg/services/alpha/src/alpha/domains/__init__.py"],
    }
    sb.write_files(root, {rule.layout: json.dumps(layout)})
    outcome = sb.check(cfg_of(root, rule), rule)
    assert any("公共出口不在该域目录内" in item for item in outcome.problems)
    assert not any("既不是域子包目录" in item for item in outcome.problems)


def test_prefix_bucket_and_seen_dedup(tmp_path: Path) -> None:
    """同服务同前缀的表只登记一次归属；同行同表的不同操作只计一次引用。"""
    root = build(
        tmp_path,
        **{
            "pkg/services/alpha/src/alpha/models/m.py": ('__tablename__ = "alpha_one"\n__tablename__ = "alpha_two"\n'),
            "pkg/services/alpha/src/alpha/api/repo.py": ('A, B = "select * from beta_log", "beta_log"\n'),
            "pkg/services/beta/src/beta/models/m.py": '__tablename__ = "beta_log"\n',
        },
    )
    rule = sample()
    outcome = sb.check(cfg_of(root, rule), rule)
    assert outcome.counts["table_prefix_conflict"] == 0
    assert outcome.counts["cross_service_access_violation"] == 1


def test_exempt_module_inside_package(tmp_path: Path) -> None:
    """包内豁免模块（按定义引用全部表名）不参与引用越界扫描：服务维与域级各一处。"""
    alpha_file = "pkg/services/alpha/src/alpha/services/mod.py"
    exempt = "pkg/services/alpha/src/alpha/services/services_catalog.py"
    root = build(
        tmp_path,
        **{
            exempt: 'SQL = "select * from beta_log"\n',
            alpha_file: 'SQL = "select * from beta_second"\n',
            "pkg/services/alpha/src/alpha/services/other.py": "VALUE = 0\n",
            "pkg/services/alpha/src/alpha/services/second.py": "VALUE = 0\n",
            "pkg/services/beta/src/beta/models/m.py": '__tablename__ = "beta_log"\n',
        },
    )
    rule = sample(service_packages=("platform", "alpha", "beta"))
    sb.write_files(
        root,
        {
            rule.layout: json.dumps(
                sb.domain_layout(
                    [alpha_file],
                    [
                        "pkg/services/alpha/src/alpha/services/other.py",
                        "pkg/services/alpha/src/alpha/services/second.py",
                        "pkg/services/beta/src/beta/models/m.py",
                    ],
                )
            )
        },
    )
    outcome = sb.check(cfg_of(root, rule), rule)
    assert outcome.counts["cross_domain_table_reference"] == 0


def test_placement_explicit_model_file(tmp_path: Path) -> None:
    """`placement` 域显式声明的模型单文件按例外放行（路径不在域子包内）。"""
    root = build(
        tmp_path,
        **{
            "pkg/services/alpha/src/alpha/domains/__init__.py": "",
            "pkg/services/alpha/src/alpha/domains/alpha/__init__.py": "",
            "pkg/services/alpha/src/alpha/domains/alpha/public.py": "",
            "pkg/libs/core/src/core/models/alpha/model.py": '__tablename__ = "sys_alpha"\n',
            "pkg/services/beta/src/beta/models/m.py": "",
        },
    )
    rule = sample()
    layout = {
        "version": 1,
        "domains": [
            {
                "domain_key": "alpha",
                "service_key": "platform",
                "status": "placement",
                "paths": [
                    "pkg/services/alpha/src/alpha/domains/alpha/**/*.py",
                    "pkg/libs/core/src/core/models/alpha/model.py",
                ],
                "public_api": ["alpha.domains.alpha.public"],
            }
        ],
        "shared_paths": [
            *sb.SAMPLE_SHARED_PATHS,
            "pkg/services/alpha/src/alpha/domains/__init__.py",
            "pkg/libs/core/src/core/models/alpha/__init__.py",
        ],
    }
    sb.write_files(
        root,
        {
            rule.layout: json.dumps(layout),
            "pkg/libs/core/src/core/models/alpha/__init__.py": "",
        },
    )
    outcome = sb.check(cfg_of(root, rule), rule)
    assert not any("既不是域子包目录" in item for item in outcome.problems)


def test_self_test_failure_output(monkeypatch: pytest.MonkeyPatch) -> None:
    """自检矩阵失败时打印失败标签并退 1。"""
    monkeypatch.setattr(
        sb,
        "check",
        lambda *args, **kwargs: sb.Outcome(problems=["boom"], counts=dict.fromkeys(sb.SAMPLE_COUNTERS, 0)),
    )
    assert sb.self_test() == 1
