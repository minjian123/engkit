"""服务边界校验的分支用例（登记源解析 / 扫描面穷尽 / 域级与登记一致性边角）。

主流程在 `test_base_boundaries.py`；本文件补「结构非法 / 跳过规则 / 双字段不一致」等分支，
以保证该校验器的行与分支覆盖率达标（口径见《AI开发规范》「覆盖率要求」节）。
"""

from __future__ import annotations

import ast
import dataclasses
import json
from pathlib import Path

import pytest

from engkit.base import service_boundaries as sb
from engkit.config import Config, MetricsRules

METRICS_RULE = MetricsRules(
    violation_keys=("cross_service_access_violation",),
    cross_access_key="cross_service_access_violation",
    registered_exceptions_key="registered_exceptions_total",
    metric_name="demo_boundary_total",
)

TABLES_PY = """
class Datasource(StrEnum):
    PLATFORM = "platform"
    BOTH = "both"


EVERY = "*"

TABLE_OWNERSHIP = stable(
    (
        rec(table_name="sys_alpha", owner="platform", domain="alpha", datasource=Datasource.PLATFORM),
        rec(table_name="beta_log", owner="beta", domain="beta", datasource=Datasource.TENANT),
        rec(table_name="alpha_second", owner="alpha", domain="beta", datasource=Datasource.TENANT),
        rec(table_name="alpha_third", owner="alpha", domain="beta", datasource=Datasource.TENANT),
        rec(table_name="sys_outbox", owner=EVERY, domain="", datasource=Datasource.BOTH),
    )
)
"""

CATALOG_PY = """
SERVICE_CATALOG = stable(
    (
        rec(module_key="sys", service_key="platform", table_prefix="sys_"),
        rec(module_key="alpha", table_prefix="alpha_"),
    )
)
"""


def sample(**overrides: object) -> sb.ServiceBoundariesRules:
    """取夹具规则（可按需覆盖字段）。

    Args:
        **overrides: 规则字段覆盖。

    Returns:
        sb.ServiceBoundariesRules: 规则。
    """
    rule = dataclasses.replace(
        sb.sample_rule(),
        ownership_module="pkg/tables.py",
        catalog_module="pkg/catalog.py",
        run_image_values=("platform", "*"),
        image_packages={"platform": ("core", "platform", "ops")},
    )
    return dataclasses.replace(rule, **overrides) if overrides else rule


def cfg_of(root: Path, rule: sb.ServiceBoundariesRules | None = None) -> Config:
    """造配置对象（规则段可空）。

    Args:
        root: 仓根。
        rule: 规则段。

    Returns:
        Config: 配置对象。
    """
    base = sb.mini_cfg(root)
    return dataclasses.replace(base, boundaries_rules=rule, metrics_rules=METRICS_RULE)


def build(tmp_path: Path, **files: object) -> Path:
    """造夹具仓库（基线文件 + 登记源 + 覆盖写）。

    Args:
        tmp_path: 临时仓根。
        **files: 覆盖写文件（相对路径 → 文本）。

    Returns:
        Path: 仓根。
    """
    sb.write_files(tmp_path, sb.base_files())
    sb.write_files(tmp_path, {"pkg/tables.py": TABLES_PY, "pkg/catalog.py": CATALOG_PY})
    if files:
        sb.write_files(tmp_path, {str(key): str(value) for key, value in files.items()})
    return tmp_path


def registry_of() -> sb.Registry:
    """取最小登记视图。

    Returns:
        sb.Registry: 表归属 + 服务目录。
    """
    return sb.Registry(
        tables=(
            sb.TableRecord(table_name="sys_alpha", owner="platform", domain="alpha"),
            sb.TableRecord(table_name="sys_outbox", owner="*", domain=""),
        ),
        catalog=(sb.CatalogRecord(module_key="sys", service_key="platform", table_prefix="sys_"),),
    )


def test_layout_branches(tmp_path: Path) -> None:
    """域布局：glob 展开 / 路径未命中 / 重复归属 / 与域外路径冲突 / 空路径。"""
    layout = sb.Layout(
        entries=(sb.DomainEntry(domain_key="a", service_key="s", paths=("missing/*.py",)),),
        shared=("nope/*.py",),
    )
    mapping, problems = layout.resolve_files(tmp_path)
    assert mapping == {}
    assert any("路径未命中文件" in item for item in problems)
    validated = layout.validate(tmp_path)
    assert any("路径未命中文件" in item for item in validated)
    assert any("域外路径未命中文件" in item for item in validated)

    sb.write_files(tmp_path, {"x/a.py": ""})
    duplicated = sb.Layout(
        entries=(
            sb.DomainEntry(domain_key="a", service_key="s", paths=("x/*.py",)),
            sb.DomainEntry(domain_key="b", service_key="s", paths=("x/*.py",)),
        ),
        shared=("x/*.py",),
    )
    _, problems = duplicated.resolve_files(tmp_path)
    assert any("同时归属" in item for item in problems)
    validated = duplicated.validate(tmp_path)
    assert any("同时归属" in item for item in validated)
    assert any("同时登记为域外路径" in item for item in validated)

    empty = sb.Layout(entries=(sb.DomainEntry(domain_key="a", service_key="s"),), shared=())
    assert any("未声明代码路径" in item for item in empty.validate(tmp_path))
    planned = sb.Layout(entries=(sb.DomainEntry(domain_key="a", service_key="s", status="planned"),), shared=())
    assert planned.validate(tmp_path) == []
    assert planned.public_api_of("ghost") == ()
    assert planned.domain_services() == {"a": "s"}
    assert planned.entry_of("ghost") is None
    assert planned.shared_files(tmp_path) == {}


def test_ast_helpers() -> None:
    """AST 辅助：赋值形态 / 清单展开 / 字段解析的兜底分支。"""
    tree = ast.parse("A = B = 'x'\nC: str = 'y'\nD = 1\n")
    assert sb.assignment_of(tree.body[0]) == (None, None)
    assert sb.assignment_of(tree.body[1])[0] == "C"
    assert sb.assignment_of(tree.body[2].value) == (None, None)  # type: ignore[arg-type]
    assert sb.module_constants(tree) == {"C": "y"}
    assert sb.sequence_of(ast.parse("x()").body[0].value) == []  # type: ignore[attr-defined]
    assert sb.sequence_of(ast.parse("x").body[0].value) == []  # type: ignore[attr-defined]
    assert sb.record_keywords(ast.parse("1").body[0].value) == {}  # type: ignore[attr-defined]
    assert sb.string_value(None, {}, {}) == ""
    assert sb.string_value(ast.parse("f'x'").body[0].value, {}, {}) == ""  # type: ignore[attr-defined]
    assert sb.string_value(ast.parse("obj.x").body[0].value, {}, {}) == ""  # type: ignore[attr-defined]
    assert sb.enum_members(tree) == {}


def test_registry_const_missing(tmp_path: Path) -> None:
    """服务目录常量缺失即报「登记源不可解析」。"""
    root = build(tmp_path, **{"pkg/catalog.py": "OTHER = 1\n"})
    with pytest.raises(sb.RegistryError, match="登记源不可解析"):
        sb.load_registry(cfg_of(root), sample())


def test_discover_packages_branches(tmp_path: Path) -> None:
    """包发现：缺分组目录 / 工程无 src / 源码目录下无包。"""
    root = build(
        tmp_path,
        **{
            "pkg/libs/empty/README.md": "x",
            "pkg/libs/nopkg/src/plain.py": "VALUE = 0\n",
        },
    )
    packages = sb.discover_packages(cfg_of(root), sample(shared_groups=("libs", "ghost"), service_groups=("services",)))
    assert "core" in packages
    assert "plain" not in packages


def test_import_candidates_relative(tmp_path: Path) -> None:
    """相对导入（无模块名）不计候选；`from pkg import sub` 产出两点形态。"""
    path = tmp_path / "mod.py"
    path.write_text("from . import local\nfrom alpha import api\nimport beta.models\n", encoding="utf-8")
    candidates = sb.iter_import_candidates(path)
    assert (2, "alpha.api") in candidates
    assert (3, "beta.models") in candidates
    assert all(module != "" for _lineno, module in candidates)


def test_cross_reference_scan(tmp_path: Path) -> None:
    """引用扫描：外键目标 / 裸字符串跳过 / 表名 token / 缓存与测试目录跳过。"""
    package = tmp_path / "pkg/services/alpha/src/alpha"
    sb.write_files(
        tmp_path,
        {
            "pkg/services/alpha/src/alpha/__init__.py": "",
            "pkg/services/alpha/src/alpha/repo.py": (
                '"""sys_alpha 文档串（裸字符串不计）"""\n'
                'FK = ForeignKey("sys_alpha.id")\n'
                "DYNAMIC = ForeignKey(name)\n"
                'TOKEN = "sys_alpha"\n'
                'SQL = "select * from sys_outbox"\n'
                "NONE = ForeignKey()\n"
            ),
            "pkg/services/alpha/src/alpha/tests/test_repo.py": 'SQL = "select * from sys_alpha"\n',
            "pkg/services/alpha/src/alpha/__pycache__/cached.py": 'SQL = "select * from sys_alpha"\n',
        },
    )
    found = sb.iter_cross_reference_tables(package, ("sys_alpha", "sys_outbox"), tmp_path)
    files = {rel for rel, _lineno, _table, _operation in found}
    assert files == {"pkg/services/alpha/src/alpha/repo.py"}
    operations = {(table, operation) for _rel, _lineno, table, operation in found}
    assert ("sys_alpha", "schema") in operations
    assert ("sys_alpha", "unknown") in operations
    assert ("sys_outbox", "read") in operations
    assert sb.foreign_key_target(ast.parse("ForeignKey(name)").body[0].value) is None  # type: ignore[arg-type]
    assert sb.foreign_key_target(ast.parse("ForeignKey()").body[0].value) is None  # type: ignore[arg-type]
    assert sb.foreign_key_target(ast.parse('mod.ForeignKey("t.c")').body[0].value) == "t"  # type: ignore[arg-type]


def test_service_of_prefix(tmp_path: Path) -> None:
    """包名前缀去前缀得服务标识；共享库包归平台服务。"""
    root = build(tmp_path)
    rule = sample(package_prefix="biz_", shared_lib_service="platform")
    checker = sb.Checker(cfg_of(root, rule), rule, registry_of())
    assert checker.service_of("biz_alpha") == "alpha"
    assert checker.service_of("core") == "platform"
    assert checker.service_of("other") == "other"
    assert checker.ops_package() == "ops"
    assert checker.rule_backend() == "pkg"


def test_domain_state_errors(tmp_path: Path) -> None:
    """域布局 / 域级基线结构非法时记违规并降级（域级断言停用）。"""
    root = build(
        tmp_path,
        **{
            "deploy/boundaries/domain_layout.json": "{ broken",
            "deploy/boundaries/domain_boundary_baseline.json": "[]",
        },
    )
    outcome = sb.check(cfg_of(root), sample())
    assert any("[域布局]" in item for item in outcome.problems)
    assert any("[域级基线]" in item for item in outcome.problems)
    assert outcome.counts["domain_files_registered"] == 0


def test_exceptions_state_errors(tmp_path: Path) -> None:
    """例外白名单结构非法 / 语义不合法时记 `table_ownership_violation`。"""
    root = build(tmp_path, **{"deploy/boundaries/data_ownership_exceptions.json": "{ broken"})
    outcome = sb.check(cfg_of(root), sample())
    assert any("[例外登记]" in item for item in outcome.problems)

    root = build(
        root,
        **{
            "deploy/boundaries/data_ownership_exceptions.json": json.dumps(
                {
                    "version": 1,
                    "exceptions": [
                        {
                            "service": "ghost",
                            "target_prefix": "nope",
                            "access": "read",
                            "exit": "readonly_projection",
                        }
                    ],
                },
                ensure_ascii=False,
            )
        },
    )
    outcome = sb.check(cfg_of(root), sample())
    assert outcome.counts["registered_exceptions_total"] == 1
    assert outcome.counts["table_ownership_violation"] >= 2


def test_declaration_branches(tmp_path: Path) -> None:
    """声明侧：未登记表名 / 共享模型子包 / 跨服务越权三支。"""
    root = build(
        tmp_path,
        **{
            "pkg/services/alpha/src/alpha/api/m.py": '__tablename__ = "zzz_unknown"\n',
            "pkg/libs/core/src/core/models/beta/m.py": '__tablename__ = "sys_alpha"\n',
            "pkg/services/beta/src/beta/models/m.py": '__tablename__ = "sys_alpha"\n',
        },
    )
    outcome = sb.check(cfg_of(root), sample(service_packages=("platform", "alpha", "beta")))
    assert outcome.counts["table_ownership_violation"] >= 2


def test_ops_and_image_branches(tmp_path: Path) -> None:
    """运维目录缺失 / 镜像包集合漂移 / 未声明运行镜像（含非跨包脚本放行）。"""
    root = build(tmp_path, **{"pkg/ops/plain.py": "VALUE = 1\n"})
    outcome = sb.check(cfg_of(root), sample())
    assert outcome.counts["ops_run_image_missing"] == 0
    assert outcome.counts["ops_import_closure_violation"] == 0

    missing_ops = dataclasses.replace(sample(), ops_dir="pkg/none")
    outcome = sb.check(cfg_of(root, missing_ops), missing_ops)
    assert not any("运行镜像" in item for item in outcome.problems)

    drift = dataclasses.replace(sample(), image_packages={"platform": ("core",)})
    outcome = sb.check(cfg_of(root, drift), drift)
    assert outcome.counts["ops_image_packages_drift"] >= 1


def test_runtime_dependency_branches(tmp_path: Path) -> None:
    """运行期依赖扫描：服务目录缺失 / 工程无 src / 缓存目录跳过。"""
    root = build(
        tmp_path,
        **{
            "pkg/services/lonely/README.md": "x",
            "pkg/services/alpha/src/alpha/api/__pycache__/cached.py": "from ops.seed import seed\n",
        },
    )
    outcome = sb.check(cfg_of(root), sample())
    assert outcome.counts["runtime_ops_dependency"] == 0


def test_domain_issue_branches(tmp_path: Path) -> None:
    """域级引用：跨服务越界不重复计 / 读侧例外放行计数 / 缓存目录跳过。"""
    alpha_file = "pkg/services/alpha/src/alpha/services/mod.py"
    beta_file = "pkg/services/alpha/src/alpha/services/other.py"
    beta_model = "pkg/services/beta/src/beta/models/m.py"
    root = build(
        tmp_path,
        **{
            beta_model: '__tablename__ = "beta_log"\n',
            alpha_file: (
                'SQL = "select * from beta_log"\n'
                'CROSS = "select * from alpha_second"\n'
                'READ = "select * from alpha_third"\n'
            ),
            "pkg/services/alpha/src/alpha/api/__pycache__/cached.py": 'SQL = "select * from beta_log"\n',
            "deploy/boundaries/data_ownership_exceptions.json": json.dumps(
                {
                    "version": 1,
                    "exceptions": [
                        {
                            "service": "alpha",
                            "target_prefix": "alpha_third",
                            "access": "read",
                            "exit": "readonly_projection",
                        }
                    ],
                },
                ensure_ascii=False,
            ),
        },
    )
    rule = sample(service_packages=("platform", "alpha", "beta"))
    shared = (
        "pkg/services/alpha/src/alpha/__init__.py",
        "pkg/services/alpha/src/alpha/api/__init__.py",
        "pkg/services/alpha/src/alpha/api/m.py",
        "pkg/services/alpha/src/alpha/api/mod.py",
        "pkg/services/alpha/src/alpha/api/repo.py",
        "pkg/services/alpha/src/alpha/api/runner.py",
        "pkg/services/alpha/src/alpha/services/__init__.py",
        "pkg/services/beta/src/beta/__init__.py",
        "pkg/services/beta/src/beta/api/__init__.py",
        "pkg/services/beta/src/beta/models/__init__.py",
        "pkg/services/platform/src/platform/__init__.py",
        "pkg/services/platform/src/platform/main.py",
    )
    sb.write_files(
        root,
        {rule.layout: json.dumps(sb.domain_layout([alpha_file], [beta_file, beta_model], shared=shared))},
    )
    outcome = sb.check(cfg_of(root, rule), rule)
    assert outcome.counts["cross_domain_table_reference"] >= 1
    assert outcome.counts["registered_exceptions_used"] >= 1


def test_layout_shape_branches(tmp_path: Path) -> None:
    """形态校验：模型单文件例外 / 公共出口不在域目录内。"""
    root = build(
        tmp_path,
        **{
            "pkg/services/alpha/src/alpha/domains/__init__.py": "",
            "pkg/services/alpha/src/alpha/domains/alpha/__init__.py": "",
            "pkg/services/alpha/src/alpha/domains/alpha/model.py": '__tablename__ = "sys_alpha"\n',
            "pkg/services/alpha/src/alpha/domains/alpha/public.py": "",
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
                    "pkg/services/beta/src/beta/models/m.py",
                ],
                "public_api": ["alpha.domains.alpha.public"],
            }
        ],
        "shared_paths": [
            "pkg/services/alpha/src/alpha/__init__.py",
            "pkg/services/alpha/src/alpha/api/__init__.py",
            "pkg/services/alpha/src/alpha/api/m.py",
            "pkg/services/alpha/src/alpha/api/mod.py",
            "pkg/services/alpha/src/alpha/api/repo.py",
            "pkg/services/alpha/src/alpha/api/runner.py",
            "pkg/services/alpha/src/alpha/services/__init__.py",
            "pkg/services/beta/src/beta/__init__.py",
            "pkg/services/beta/src/beta/api/__init__.py",
            "pkg/services/beta/src/beta/models/__init__.py",
            "pkg/services/platform/src/platform/__init__.py",
            "pkg/services/platform/src/platform/main.py",
            "pkg/services/alpha/src/alpha/domains/__init__.py",
        ],
    }
    sb.write_files(root, {rule.layout: json.dumps(layout)})
    outcome = sb.check(cfg_of(root, rule), rule)
    assert any("既不是域子包目录" in item for item in outcome.problems)
    assert any("公共出口不在该域目录内" in item for item in outcome.problems) is False


def test_validate_ownership_branches() -> None:
    """归属登记一致性：表名形态 / 归属服务 / 空域 / 标识非法 / 未登记 / 承载不一致 / 前缀越界 / 重复。"""
    rule = sample()
    registry = sb.Registry(
        tables=(
            sb.TableRecord(table_name="Bad-Name", owner="ghost", domain="x"),
            sb.TableRecord(table_name="sys_dup", owner="platform", domain="alpha"),
            sb.TableRecord(table_name="sys_dup", owner="platform", domain="alpha"),
            sb.TableRecord(table_name="sys_outbox", owner="*", domain="alpha"),
            sb.TableRecord(table_name="sys_nodom", owner="platform", domain=""),
            sb.TableRecord(table_name="sys_baddom", owner="platform", domain="A"),
            sb.TableRecord(table_name="alpha_one", owner="alpha", domain="alpha"),
            sb.TableRecord(table_name="bad_prefix_x", owner="alpha", domain="alpha"),
        ),
        catalog=(
            sb.CatalogRecord(module_key="sys", service_key="platform", table_prefix="sys_"),
            sb.CatalogRecord(module_key="alpha", table_prefix="alpha_"),
        ),
    )
    errors = sb.validate_ownership(registry, rule, {"alpha": "platform"})
    joined = "\n".join(errors)
    assert "表名非法" in joined
    assert "归属服务未登记" in joined
    assert "表名重复登记" in joined
    assert "不得登记业务域" in joined
    assert "未登记归属业务域" in joined
    assert "归属业务域标识非法" in joined
    assert "归属业务域未登记" in joined
    assert "与归属服务 alpha 不一致" in joined
    assert "表前缀 bad_ 不属于归属服务 alpha" in joined
    assert sb.validate_ownership(registry, rule, None) != errors


def test_self_test_failure_branch(monkeypatch: pytest.MonkeyPatch) -> None:
    """自检矩阵有失败项时退 1（并打印失败标签）。"""
    monkeypatch.setattr(
        sb,
        "check",
        lambda *args, **kwargs: sb.Outcome(problems=["boom"], counts=dict.fromkeys(sb.SAMPLE_COUNTERS, 0)),
    )
    assert sb.self_test() == 1
