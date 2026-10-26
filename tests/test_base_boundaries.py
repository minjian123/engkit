"""服务边界与边界度量（`engkit base service-boundaries` / `boundary-metrics`）用例。

覆盖：`[rules.service_boundaries]` / `[rules.metrics]` 解析、登记源 AST 解析与失败分支、
域布局 / 例外白名单 / 域级基线的结构非法分支、SQL 分析、越界判定（服务维与域维）、
运维镜像 COPY 面与导入闭包、域级断言的开 / 关两态、JSON / 文本输出与退出码。
"""

from __future__ import annotations

import dataclasses
import json
import tomllib
from pathlib import Path

import pytest

from engkit import cli
from engkit.base import metrics
from engkit.base import service_boundaries as sb
from engkit.config import Config, MetricsRules, parse

BOUNDARIES_TOML = r"""
[rules.service_boundaries]
shared_groups = ["libs"]
service_groups = ["services"]
shared_libs = ["core"]
service_packages = ["alpha"]
runtime_packages = ["core"]
layout = "deploy/boundaries/domain_layout.json"
exceptions = "deploy/boundaries/exceptions.json"
baseline = "deploy/boundaries/baseline.json"
shared_models_marker = "/libs/core/src/core/models/"
package_prefix = "biz_"
shared_lib_service = "platform"
domain_subdir = "domains"
shared_table_prefixes = ["sys_"]
counters = ["table_name_conflict", "domain_files_registered"]

[rules.service_boundaries.layers.shared]
core = ["api"]

[rules.service_boundaries.layers.service]
services = ["api"]

[rules.service_boundaries.ownership]
module = "pkg/tables.py"
const = "TABLE_OWNERSHIP"
owner_every_service = "*"
infrastructure_tables = ["sys_outbox"]
exempt_modules = ["tables.py"]
no_domain_tables = ["demo"]

[rules.service_boundaries.catalog]
module = "pkg/catalog.py"
const = "SERVICE_CATALOG"

[rules.service_boundaries.ops]
dir = "pkg/ops"
dockerfile = "pkg/Dockerfile"
run_image_values = ["platform", "*"]

[rules.service_boundaries.ops.image_packages]
platform = ["core", "ops"]

[rules.metrics]
violation_keys = ["table_name_conflict"]
cross_access_key = "cross_service_access_violation"
registered_exceptions_key = "registered_exceptions_total"
metric_name = "demo_boundary_total"
checker = "service-boundaries"
"""

METRICS_RULE = MetricsRules(
    violation_keys=("cross_service_access_violation", "table_name_conflict"),
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


def test_rules_sections_parsed(tmp_path: Path) -> None:
    """两段规则按样例解析（含通用化键与嵌套表）。"""
    raw = tomllib.loads(BOUNDARIES_TOML)
    raw["docs"] = {"root": "docs"}
    cfg = parse(raw, tmp_path / "engkit.toml")
    rules = cfg.boundaries_rules
    assert rules is not None
    assert rules.shared_groups == ("libs",)
    assert rules.layers_shared == {"core": ("api",)}
    assert rules.infrastructure_tables == ("sys_outbox",)
    assert rules.no_domain_tables == ("demo",)
    assert rules.package_prefix == "biz_"
    assert rules.shared_lib_service == "platform"
    assert rules.shared_table_prefixes == ("sys_",)
    assert rules.image_packages == {"platform": ("core", "ops")}
    assert cfg.metrics_rules is not None and cfg.metrics_rules.metric_name == "demo_boundary_total"


def build(tmp_path: Path, **overrides: object) -> Path:
    """造一份合规夹具仓库（表归属 / 服务目录 / 镜像 / 运维脚本齐备）。

    Args:
        tmp_path: 临时仓根。
        **overrides: 覆盖写文件（相对路径 → 文本）。

    Returns:
        Path: 仓根。
    """
    sb.write_files(tmp_path, sb.base_files())
    sb.write_files(tmp_path, {"pkg/tables.py": TABLES_PY, "pkg/catalog.py": CATALOG_PY})
    if overrides:
        sb.write_files(tmp_path, {str(key): str(value) for key, value in overrides.items()})
    return tmp_path


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
        shared_libs=("core",),
        run_image_values=("platform", "*"),
        image_packages={"platform": ("core", "platform", "ops")},
    )
    return dataclasses.replace(rule, **overrides) if overrides else rule


def cfg_of(root: Path, rule: sb.ServiceBoundariesRules | None = None) -> Config:
    """造配置对象（规则段可选，用于覆盖未登记分支）。

    Args:
        root: 仓根。
        rule: 规则段（None 表示未登记）。

    Returns:
        Config: 配置对象。
    """
    base = sb.mini_cfg(root)
    return dataclasses.replace(base, boundaries_rules=rule, metrics_rules=METRICS_RULE)


def test_check_clean_repo(tmp_path: Path) -> None:
    """合规仓库：无违规、服务表清单与计数齐备。"""
    root = build(tmp_path)
    outcome = sb.check(cfg_of(root), sample())
    assert outcome.problems == []
    assert outcome.counts["table_name_conflict"] == 0
    assert all(value == 0 for value in outcome.counts.values())


def test_run_exit_codes(tmp_path: Path) -> None:
    """退出码：规则未登记 2 / 登记源缺失 2 / 登记源不可解析 1 / 通过 0 / 违规 1。"""
    root = build(tmp_path)
    assert sb.run(cfg_of(root, None), json_output=False, update_baseline=False, as_self_test=False) == 2
    missing = dataclasses.replace(sample(), ownership_module="pkg/missing.py")
    assert sb.run(cfg_of(root, missing), json_output=False, update_baseline=False, as_self_test=False) == 2
    broken = dataclasses.replace(sample(), catalog_module="pkg/broken.py")
    sb.write_files(root, {"pkg/broken.py": "def f(:\n"})
    assert sb.run(cfg_of(root, broken), json_output=False, update_baseline=False, as_self_test=False) == 1
    assert sb.run(cfg_of(root, sample()), json_output=False, update_baseline=False, as_self_test=False) == 0
    sb.write_files(root, {"pkg/services/alpha/src/alpha/api/m.py": '__tablename__ = "sys_alpha"\n'})
    assert sb.run(cfg_of(root, sample()), json_output=True, update_baseline=False, as_self_test=False) == 1


def test_run_self_test(tmp_path: Path) -> None:
    """`--self-test` 只跑内置矩阵（不读仓登记源）。"""
    root = build(tmp_path)
    assert sb.run(cfg_of(root, sample()), json_output=False, update_baseline=False, as_self_test=True) == 0


def test_registry_errors(tmp_path: Path) -> None:
    """登记源解析失败分支：常量缺失 / 常量为空 / 语法非法。"""
    root = build(
        tmp_path,
        **{
            "pkg/empty.py": "TABLE_OWNERSHIP = stable(())\n",
            "pkg/no_const.py": "OTHER = 1\n",
        },
    )
    with pytest.raises(sb.RegistryError, match="登记源不可解析"):
        sb.load_registry(cfg_of(root), dataclasses.replace(sample(), ownership_module="pkg/no_const.py"))
    with pytest.raises(sb.RegistryError, match="登记源常量为空"):
        sb.load_registry(cfg_of(root), dataclasses.replace(sample(), ownership_module="pkg/empty.py"))
    sb.write_files(root, {"pkg/bad.py": "x = (\n"})
    with pytest.raises(sb.RegistryError, match="登记源不可解析"):
        sb.load_registry(cfg_of(root), dataclasses.replace(sample(), catalog_module="pkg/bad.py"))


def test_registry_resolution_variants(tmp_path: Path) -> None:
    """字段解析：常量引用 / 枚举成员 / 无法解析的表达式 / 缺表名的条目跳过。"""
    root = build(
        tmp_path,
        **{
            "pkg/odd.py": """
NAME = "sys_alpha"

TABLE_OWNERSHIP = (
    rec(table_name=NAME, owner="platform", domain=UNKNOWN, datasource=Datasource.PLATFORM),
    rec(owner="alpha"),
    rec(table_name="sys_icon", owner="platform", domain="", datasource=1 + 1),
)
"""
        },
    )
    registry = sb.load_registry(cfg_of(root), dataclasses.replace(sample(), ownership_module="pkg/odd.py"))
    assert registry.names() == ("sys_alpha", "sys_icon")
    assert registry.domain_of("sys_alpha") == ""
    assert registry.owner_of("sys_icon") == "platform"
    assert registry.owner_of("nope") is None
    assert registry.domain_of("nope") is None


def test_catalog_resolution(tmp_path: Path) -> None:
    """服务目录：`service_key` 回落 `module_key`、前缀清单与例外可用前缀集合。"""
    root = build(
        tmp_path,
        **{
            "pkg/cat.py": """
SERVICE_CATALOG = (
    rec(module_key="alpha", table_prefix="alpha_"),
    rec(module_key="sys", service_key="platform", table_prefix="sys_"),
)
"""
        },
    )
    registry = sb.load_registry(cfg_of(root), dataclasses.replace(sample(), catalog_module="pkg/cat.py"))
    assert registry.services() == ("alpha", "platform")
    assert registry.service_prefixes() == {"alpha": ("alpha_",), "platform": ("sys_",)}
    assert "sys_" in registry.prefixes()
    assert "alpha_" in registry.prefixes()


def test_layout_errors(tmp_path: Path) -> None:
    """域布局结构非法分支逐条报错。"""
    path = tmp_path / "layout.json"
    cases = (
        ("{ broken", "JSON 非法"),
        ("[]", "根须为对象"),
        (json.dumps({"version": 2}), "版本非法"),
        (json.dumps({"version": 1, "domains": {}}), "domains 须为数组"),
        (json.dumps({"version": 1, "domains": ["x"]}), "须为对象"),
        (
            json.dumps({"version": 1, "domains": [{"domain_key": "A", "service_key": "s", "paths": []}]}),
            "domain_key 非法",
        ),
        (
            json.dumps(
                {
                    "version": 1,
                    "domains": [
                        {"domain_key": "a", "service_key": "s", "paths": []},
                        {"domain_key": "a", "service_key": "s", "paths": []},
                    ],
                }
            ),
            "domain_key 重复",
        ),
        (json.dumps({"version": 1, "domains": [{"domain_key": "a", "service_key": "s", "status": 1}]}), "状态非法"),
        (json.dumps({"version": 1, "domains": [{"service_key": "s"}]}), "字段缺失或为空"),
        (json.dumps({"version": 1, "domains": [{"domain_key": "a", "service_key": "s", "paths": "x"}]}), "须为数组"),
        (
            json.dumps({"version": 1, "domains": [{"domain_key": "a", "service_key": "s", "paths": [""]}]}),
            "元素须为非空字符串",
        ),
    )
    for payload, message in cases:
        path.write_text(payload, encoding="utf-8")
        with pytest.raises(ValueError, match=message):
            sb.load_layout(path)
    assert sb.load_layout(tmp_path / "none.json").entries == ()


def test_baseline_errors_and_dump(tmp_path: Path) -> None:
    """域级基线结构非法分支与写回排序。"""
    path = tmp_path / "baseline.json"
    cases = (
        ("{ broken", "JSON 非法"),
        ("[]", "根须为对象"),
        (json.dumps({"version": 9}), "版本非法"),
        (json.dumps({"version": 1, "entries": {}}), "entries 须为数组"),
        (json.dumps({"version": 1, "entries": ["x"]}), "须为对象"),
        (json.dumps({"version": 1, "entries": [{"kind": "import"}]}), "字段缺失或为空"),
    )
    for payload, message in cases:
        path.write_text(payload, encoding="utf-8")
        with pytest.raises(ValueError, match=message):
            sb.load_baseline(path)
    assert sb.load_baseline(tmp_path / "none.json") == ()
    written = sb.dump_baseline(
        path,
        (
            sb.BaselineEntry(kind="table_ref", domain="b", target="a", file="z.py", fingerprint="ff"),
            sb.BaselineEntry(kind="import", domain="a", target="b", file="y.py", fingerprint="ee"),
        ),
    )
    assert written == 2
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert [entry["kind"] for entry in payload["entries"]] == ["import", "table_ref"]


def test_exceptions_errors(tmp_path: Path) -> None:
    """例外白名单结构非法分支与语义校验。"""
    path = tmp_path / "exceptions.json"
    cases = (
        ("{ broken", "JSON 非法"),
        ("[]", "根须为对象"),
        (json.dumps({"version": 3}), "版本非法"),
        (json.dumps({"version": 1, "exceptions": {}}), "须为数组"),
        (json.dumps({"version": 1, "exceptions": ["x"]}), "须为对象"),
        (
            json.dumps(
                {"version": 1, "exceptions": [{"service": "a", "target_prefix": "t", "access": "write", "exit": "e"}]}
            ),
            "只允许 'read'",
        ),
        (json.dumps({"version": 1, "exceptions": [{"service": "a", "target_prefix": "t"}]}), "字段缺失或为空"),
    )
    for payload, message in cases:
        path.write_text(payload, encoding="utf-8")
        with pytest.raises(ValueError, match=message):
            sb.load_exceptions(path)
    assert sb.load_exceptions(tmp_path / "none.json") == ()

    registry = sb.Registry(
        tables=(sb.TableRecord(table_name="sys_alpha", owner="platform", domain="alpha"),),
        catalog=(sb.CatalogRecord(module_key="alpha", table_prefix="alpha_"),),
    )
    entries = (
        sb.OwnershipException(service="ghost", target="nope", access="read", exit="readonly_projection", domain="x"),
    )
    problems = sb.validate_exceptions(entries, registry=registry, known_domains=("alpha",))
    assert len(problems) == 3
    without_domain = sb.validate_exceptions(entries, registry=registry, known_domains=None)
    assert len(without_domain) == 2
    assert not any("domain" in item for item in without_domain)


def test_exception_allows(tmp_path: Path) -> None:
    """命中判定：只读才放行；按服务或按域匹配；表名 / 前缀均可作为目标。"""
    entry = sb.OwnershipException(service="alpha", target="sys_", access="read", exit="readonly_projection")
    assert sb.exception_allows((entry,), service="alpha", table="sys_alpha", operation="read")
    assert not sb.exception_allows((entry,), service="alpha", table="sys_alpha", operation="write")
    domain_entry = sb.OwnershipException(
        service="other", target="sys_alpha", access="read", exit="readonly_projection", domain="alpha"
    )
    assert sb.exception_allows((domain_entry,), service="alpha", table="sys_alpha", operation="read", domain="alpha")
    assert not sb.exception_allows((domain_entry,), service="alpha", table="sys_alpha", operation="read", domain="beta")


def test_assess_variants() -> None:
    """越界判定：未登记 / 每服务自有 / 本服务放行；域维按归属域判定。"""
    rule = sample()
    registry = sb.Registry(
        tables=(
            sb.TableRecord(table_name="sys_alpha", owner="alpha", domain="alpha"),
            sb.TableRecord(table_name="sys_outbox", owner="*", domain=""),
            sb.TableRecord(table_name="beta_log", owner="beta", domain="beta"),
        ),
        catalog=(),
    )
    assert sb.assess_table(registry, rule, "nope", service="alpha", operation="read") is None
    assert sb.assess_table(registry, rule, "sys_outbox", service="alpha", operation="write") is None
    assert sb.assess_table(registry, rule, "sys_alpha", service="alpha", operation="write") is None
    violation = sb.assess_table(registry, rule, "beta_log", service="alpha", operation="read")
    assert violation is not None and violation.owner == "beta" and violation.owner_domain == "beta"
    assert sb.assess_table(registry, rule, "beta_log", service="alpha", operation="read", domain="alpha") is not None
    assert sb.assess_table(registry, rule, "beta_log", service="alpha", operation="read", domain="beta") is None
    assert sb.assess_table(registry, rule, "nope", service="alpha", operation="read", domain="alpha") is None
    assert sb.assess_table(registry, rule, "sys_outbox", service="alpha", operation="read", domain="alpha") is None
    allowed = sb.OwnershipException(service="alpha", target="beta_log", access="read", exit="readonly_projection")
    assert sb.assess_table(registry, rule, "beta_log", service="alpha", operation="read", exceptions=(allowed,)) is None
    by_domain = sb.OwnershipException(
        service="other", target="beta_log", access="read", exit="readonly_projection", domain="alpha"
    )
    assert (
        sb.assess_table(
            registry, rule, "beta_log", service="other", operation="read", exceptions=(by_domain,), domain="alpha"
        )
        is None
    )


def test_sql_helpers() -> None:
    """SQL 分析：操作归类 / 注释与引号清理 / 表名提取（schema 与关键字过滤）。"""
    assert sb.operation_of("select 1") == "read"
    assert sb.operation_of("  (WITH x as (select 1))") == "read"
    assert sb.operation_of("insert into t values (1)") == "write"
    assert sb.operation_of("drop table t") == "ddl"
    assert sb.operation_of("pragma table_info") == "unknown"
    assert sb.operation_of("") == "unknown"
    assert sb.extract_tables("select * from `db`.`sys_alpha` as a join sys_icon i on 1") == ["sys_alpha", "sys_icon"]
    assert sb.extract_tables("select * from only lateral") == []
    assert sb.clean_sql("/* c */ select -- x\n 1").split() == ["select", "1"]
    refs = sb.analyze("update t set a = 1")
    assert refs == [sb.TableRef(table="t", operation="write")]


def test_module_helpers(tmp_path: Path) -> None:
    """模块名 / 路径映射与 `RUN_IMAGE` 解析的分支。"""
    root = build(tmp_path)
    packages = sb.discover_packages(cfg_of(root), sample())
    assert sb.module_file_of("alpha.api", packages, root) == "pkg/services/alpha/src/alpha/api/__init__.py"
    assert sb.module_file_of("alpha.api.mod", packages, root) is None
    assert sb.module_file_of("ghost.api", packages, root) is None
    ops_path = root / "pkg/ops/seed.py"
    assert sb.module_name_of(ops_path, root / "pkg") == "ops.seed"
    assert sb.module_name_of(root / "pkg/ops/__init__.py", root / "pkg") == "ops"
    assert sb.run_image_of(__import__("ast").parse("RUN_IMAGE = 'ci'")) == "ci"
    assert sb.run_image_of(__import__("ast").parse("RUN_IMAGE = 1")) is None
    assert sb.run_image_of(__import__("ast").parse("VALUE = 1")) is None


def test_dockerfile_branches(tmp_path: Path) -> None:
    """镜像 COPY 面解析：缺文件 / 缺基座 / 缺运维脚本 / 缺参数化 / 未命中包。"""
    root = build(tmp_path)
    rule = sample()
    (root / "pkg/Dockerfile").unlink()
    outcome = sb.check(cfg_of(root), rule)
    assert any("无法断言镜像 COPY 面" in item for item in outcome.problems)

    sb.write_files(root, {"pkg/Dockerfile": "COPY other/thing ./x\n"})
    outcome = sb.check(cfg_of(root), rule)
    assert any("未 COPY 共享库源码" in item for item in outcome.problems)
    assert any("未 COPY 运维脚本目录" in item for item in outcome.problems)
    assert any("未按 `${...}` 参数化" in item for item in outcome.problems)


def test_ops_closure_branches(tmp_path: Path) -> None:
    """运维脚本：非字符串 RUN_IMAGE 视为未声明；`*` 经互导间接跨包即越界。"""
    root = build(
        tmp_path,
        **{
            "pkg/ops/plain.py": "RUN_IMAGE = 1\nfrom alpha import models\n",
            "pkg/ops/relay.py": 'RUN_IMAGE = "*"\nimport ops.plain\n',
        },
    )
    outcome = sb.check(cfg_of(root), sample())
    assert outcome.counts["ops_run_image_missing"] >= 1
    assert outcome.counts["ops_import_closure_violation"] >= 1


def test_runtime_and_domain_off(tmp_path: Path) -> None:
    """无服务目录 / 无域布局时各段降级（域级断言停用、计数归零）。"""
    root = build(tmp_path)
    outcome = sb.check(cfg_of(root, sample(service_groups=())), sample())
    assert outcome.counts["domain_files_registered"] == 0
    assert outcome.counts["domain_files_unregistered"] == 0
    assert outcome.problems == []


def test_is_model_module_error(tmp_path: Path) -> None:
    """文件不存在 / 语法非法时模型判定为否（不抛出）。"""
    root = build(tmp_path, **{"pkg/weird/model.py": "class X(:\n"})
    checker = sb.Checker(cfg_of(root), sample(), sb.load_registry(cfg_of(root), sample()))
    assert checker.is_model_module("pkg/weird/model.py") is False
    assert checker.is_model_module("pkg/weird/missing.py") is False


def test_emit_outputs(capsys: pytest.CaptureFixture[str]) -> None:
    """文本 / JSON 两种输出（通过、失败、JSON 结构）。"""
    counts = dict.fromkeys(sb.SAMPLE_COUNTERS, 0)
    sb.emit(sb.Outcome(counts=counts, service_tables={"alpha": ["sys_alpha"]}), as_json=False)
    sb.emit(sb.Outcome(counts=counts, service_tables={"alpha": []}), as_json=False)
    sb.emit(sb.Outcome(problems=["样例"], counts=counts), as_json=False)
    sb.emit(sb.Outcome(problems=["样例"], counts=counts), as_json=True)
    captured = capsys.readouterr().out
    assert "通过：共享库 / 服务边界" in captured
    assert "alpha：无表声明" in captured
    assert "不通过：1 项" in captured
    assert '"passed": false' in captured


def test_metrics_run_paths(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """度量采集：未登记 2 / 落盘 / 运行期可达 / `--fail-on-violation` 退 1。"""
    root = build(tmp_path)
    unregistered = dataclasses.replace(cfg_of(root, sample()), metrics_rules=None)
    assert metrics.run(unregistered, metrics_url="", out_path="", fail_on_violation=False) == 2
    target = tmp_path / "out" / "boundary.json"
    assert metrics.run(cfg_of(root, sample()), metrics_url="", out_path=str(target), fail_on_violation=False) == 0
    payload = json.loads(target.read_text(encoding="utf-8"))
    assert payload["summary"]["boundary_violations"] == 0
    assert payload["runtime"]["available"] is False

    class _Response:
        def __enter__(self) -> _Response:
            return self

        def __exit__(self, *args: object) -> None:
            return None

        def read(self) -> bytes:
            return b"demo_boundary_total{db='a'} 2\ndemo_boundary_total 3\n"

    monkeypatch.setattr(metrics.urllib.request, "urlopen", lambda *args, **kwargs: _Response())
    assert (
        metrics.run(
            cfg_of(root, sample()), metrics_url="http://127.0.0.1:9/metrics", out_path="", fail_on_violation=False
        )
        == 0
    )
    sb.write_files(root, {"pkg/services/alpha/src/alpha/api/m.py": '__tablename__ = "sys_alpha"\n'})
    assert metrics.run(cfg_of(root, sample()), metrics_url="", out_path="", fail_on_violation=True) == 1


def test_metrics_static_error(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """静态段不可执行时降级（不误报越界）。"""
    root = build(tmp_path)
    missing = dataclasses.replace(sample(), ownership_module="pkg/none.py")
    assert metrics.run(cfg_of(root, missing), metrics_url="", out_path="", fail_on_violation=True) == 0
    captured = capsys.readouterr().out
    assert "静态校验失败" in captured
    assert metrics.run_static(cfg_of(root, None))["error"]


def test_cli_dispatch(tmp_path: Path) -> None:
    """CLI 分派：两个子命令均可解析并透传参数。"""
    root = build(tmp_path)
    config = root / "engkit.toml"
    config.write_text(
        BOUNDARIES_TOML + '\n[docs]\nroot = "docs"\n[backend]\nroot = "backend"\ngroups = ["libs", "services"]\n',
        encoding="utf-8",
    )
    args = ["--root", str(root), "--config", str(config)]
    assert cli.main(["base", "service-boundaries", "--self-test", *args]) == 0
    assert cli.main(["base", "service-boundaries", "--json", *args]) in (0, 1)
    assert cli.main(["base", "boundary-metrics", *args]) in (0, 1)
