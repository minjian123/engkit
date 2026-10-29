"""服务边界的增量扫描（`--files-from`）用例。

口径：**逐文件判据**（跨服务 import / 分层反向依赖 / 表声明与引用 / 跨域 import 与表引用）
按改动清单裁剪，**全局判据**（表名与前缀跨服务唯一、归属登记对账、例外与归属双字段、
ops 镜像闭包、域文件覆盖与布局形态、域级基线陈旧）恒全量。

本文件的核心断言是一条等价性：**「按每个文件各跑一次增量」的违规并集 == 全量违规集合**——
逐文件判据全部落在这条并集里即证明裁剪没有漏判；反之只要有一处漏掉，并集就比全量少。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from test_base_boundaries import build, cfg_of, sample

from engkit.base import service_boundaries as sb

EMPTY_LAYOUT = {"version": 1, "domains": []}
"""空域布局（域级断言关闭的最小合法形态）。"""

DOMAIN_LAYOUT = {
    "version": 1,
    "domains": [
        {
            "domain_key": "alpha",
            "service_key": "alpha",
            "paths": [
                "pkg/services/alpha/src/alpha/__init__.py",
                "pkg/services/alpha/src/alpha/_secret.py",
                "pkg/services/alpha/src/alpha/api/**/*.py",
                "pkg/services/alpha/src/alpha/services/**/*.py",
                "pkg/services/alpha/src/alpha/other/**/*.py",
            ],
        },
        {
            "domain_key": "beta",
            "service_key": "alpha",
            "paths": ["pkg/services/alpha/src/alpha/mod/**/*.py"],
        },
        {
            "domain_key": "gamma",
            "service_key": "beta",
            "paths": ["pkg/services/beta/src/beta/**/*.py"],
        },
    ],
    "shared_paths": [],
}
"""同包双域（alpha / beta 都由服务 `alpha` 承载）+ 另一服务一个域：D1 与 D3 都能触发。"""

CROSS_DOMAIN_IMPORT = '''"""越界：同包内 alpha 域直接 import beta 域的内部模块。"""

from alpha.other.inner import VALUE

EXPOSED = VALUE
'''
"""D1 样本（同包跨域 import，非公共出口）。"""

CROSS_DOMAIN_SQL = 'SQL = "select * from alpha_gate"\n'
"""D3 样本（跨域表引用：同一服务内，`alpha` 域读 `beta` 域的表）。"""

TABLES_PY = """
class Datasource(StrEnum):
    PLATFORM = "platform"
    BOTH = "both"
    TENANT = "tenant"


EVERY = "*"

TABLE_OWNERSHIP = stable(
    (
        rec(table_name="alpha_home", owner="alpha", domain="alpha", datasource=Datasource.PLATFORM),
        rec(table_name="alpha_gate", owner="alpha", domain="beta", datasource=Datasource.TENANT),
        rec(table_name="sys_outbox", owner=EVERY, domain="", datasource=Datasource.BOTH),
    )
)
"""
"""表归属登记：两域都由服务 `alpha` 承载，D3 才不会被「跨服务库访问」抢先拦下。"""

DUPLICATE_TABLE = '"""表名冲突样本：与另一服务同名。"""\n\n__tablename__ = "sys_dup"\n'
"""规则 5 样本（跨服务同名表，全局判定）。"""

SERVICE_LEAK = '''"""越界：服务互相依赖 + 跨包私有引用。"""

import alpha.services.mod
from alpha._secret import VALUE

TOTAL = mod.VALUE + VALUE
'''
"""规则 1 / 4 样本（跨服务依赖 + 私有引用）。"""


def repo(tmp_path: Path, *, layout: dict[str, object] | None = None, **overrides: object) -> Path:
    """造夹具仓库（默认写空域布局，域级断言按需开启）。

    Args:
        tmp_path: 临时仓根。
        layout: 域布局（None 表示写空布局）。
        **overrides: 覆盖写文件（相对路径 → 文本）。

    Returns:
        Path: 仓根。
    """
    root = build(tmp_path, **overrides)
    sb.write_files(
        root,
        {
            "deploy/boundaries/domain_layout.json": json.dumps(EMPTY_LAYOUT if layout is None else layout),
            "pkg/tables.py": TABLES_PY,
        },
    )
    return root


def violation_repo(tmp_path: Path) -> Path:
    """造带多类越界的夹具仓库（D1 / D3 / 规则 1+4 / 全局表名冲突）。

    Args:
        tmp_path: 临时仓根。

    Returns:
        Path: 仓根。
    """
    return repo(
        tmp_path,
        layout=DOMAIN_LAYOUT,
        **{
            "pkg/services/alpha/src/alpha/mod/inner.py": "VALUE = 2\n",
            "pkg/services/alpha/src/alpha/other/inner.py": "VALUE = 1\n",
            "pkg/services/alpha/src/alpha/mod/cross.py": CROSS_DOMAIN_IMPORT,
            "pkg/services/alpha/src/alpha/services/raw.py": CROSS_DOMAIN_SQL,
            "pkg/services/alpha/src/alpha/_secret.py": "VALUE = 3\n",
            "pkg/services/beta/src/beta/services/leak.py": SERVICE_LEAK,
            "pkg/services/alpha/src/alpha/services/dup.py": DUPLICATE_TABLE,
            "pkg/services/beta/src/beta/services/dup.py": DUPLICATE_TABLE,
        },
    )


def repo_files(root: Path) -> list[str]:
    """列出仓内全部 `.py` 文件（相对仓根）。

    Args:
        root: 仓根。

    Returns:
        list[str]: 相对路径清单（排序）。
    """
    return sorted(path.relative_to(root).as_posix() for path in root.rglob("*.py"))


def test_fixture_triggers_every_rule_family(tmp_path: Path) -> None:
    """夹具自检：四类越界都在全量结果里（否则等价性断言形同虚设）。"""
    joined = "\n".join(sb.check(cfg_of(root := violation_repo(tmp_path)), sample()).problems)
    for tag in ("[跨域 import]", "[跨域表引用]", "[服务互相依赖]", "[跨包私有引用]", "[表名跨服务重复]"):
        assert tag in joined, tag
    assert root.is_dir()


def test_incremental_union_equals_full(tmp_path: Path) -> None:
    """等价性：逐文件增量的违规并集 == 全量违规集合。"""
    root = violation_repo(tmp_path)
    cfg = cfg_of(root)
    rule = sample()
    full = sb.check(cfg, rule)
    per_file: set[str] = set()
    for rel in repo_files(root):
        per_file.update(sb.check(cfg, rule, changed=[rel]).problems)
    assert per_file == set(full.problems)


def test_incremental_reports_only_changed_file_violations(tmp_path: Path) -> None:
    """裁剪生效：越界在 A 文件时，只改 B 的增量不报 A 的违规（全量仍报）。"""
    root = violation_repo(tmp_path)
    cfg = cfg_of(root)
    rule = sample()
    assert any("[跨域 import]" in item for item in sb.check(cfg, rule).problems)
    scoped = sb.check(cfg, rule, changed=["pkg/services/alpha/src/alpha/services/mod.py"])
    assert not any("[跨域 import]" in item for item in scoped.problems)


def test_incremental_keeps_global_assertions_full(tmp_path: Path) -> None:
    """全局判据恒全量：只改 alpha 的表声明，仍报出与 beta 的表名冲突。"""
    root = violation_repo(tmp_path)
    scoped = sb.check(cfg_of(root), sample(), changed=["pkg/services/alpha/src/alpha/services/dup.py"])
    assert any("[表名跨服务重复]" in item for item in scoped.problems)


def test_incremental_scope_of_cross_service_rules(tmp_path: Path) -> None:
    """规则 1 / 4 也属逐文件判据：只切到无关文件时不报，切到该文件时报。"""
    root = violation_repo(tmp_path)
    cfg = cfg_of(root)
    rule = sample()
    leak = "pkg/services/beta/src/beta/services/leak.py"
    hit = "\n".join(sb.check(cfg, rule, changed=[leak]).problems)
    assert "[服务互相依赖]" in hit
    assert "[跨包私有引用]" in hit
    miss = "\n".join(sb.check(cfg, rule, changed=["pkg/services/beta/src/beta/api/__init__.py"]).problems)
    assert "[服务互相依赖]" not in miss


def test_baseline_stale_judged_within_scope_only(tmp_path: Path) -> None:
    """域级基线陈旧：增量只判扫描面内文件，未扫文件的失效条目不会误报。"""
    root = repo(
        tmp_path,
        layout=DOMAIN_LAYOUT,
        **{
            "deploy/boundaries/domain_boundary_baseline.json": json.dumps(
                {
                    "version": 1,
                    "entries": [
                        {
                            "kind": "import",
                            "domain": "alpha",
                            "target": "beta",
                            "file": "pkg/services/alpha/src/alpha/services/mod.py",
                            "fingerprint": "deadbeef",
                        }
                    ],
                }
            )
        },
    )
    cfg = cfg_of(root)
    rule = sample()
    assert any("[域级基线陈旧]" in item for item in sb.check(cfg, rule).problems)
    scoped = sb.check(cfg, rule, changed=["pkg/services/beta/src/beta/api/__init__.py"])
    assert not any("[域级基线陈旧]" in item for item in scoped.problems)
    hit = sb.check(cfg, rule, changed=["pkg/services/alpha/src/alpha/services/mod.py"])
    assert any("[域级基线陈旧]" in item for item in hit.problems)


@pytest.mark.parametrize(
    ("pattern", "rel", "expected"),
    [
        ("deploy/**", "deploy/boundaries/domain_layout.json", True),
        ("deploy/**", "pkg/services/alpha/src/alpha/api/m.py", False),
        ("**/table_registry.py", "pkg/libs/core/src/core/services/table_registry.py", True),
        ("**/table_registry.py", "pkg/table_registry.py", True),
        ("**/table_registry.py", "pkg/table_registry_extra.py", False),
        ("pkg/*/x.py", "pkg/a/x.py", True),
        ("pkg/*/x.py", "pkg/a/b/x.py", False),
        ("pkg/**/x.py", "pkg/a/b/x.py", True),
        ("pkg/?.py", "pkg/a.py", True),
        ("pkg/?.py", "pkg/ab.py", False),
        ("engkit.toml", "engkit.toml", True),
    ],
)
def test_matches_glob(pattern: str, rel: str, expected: bool) -> None:
    """glob 编译：`**` 跨目录、`*` 不跨目录、`?` 单字符。"""
    assert sb.matches_glob(pattern, rel) is expected


def test_full_scan_reason_for_deleted_file(tmp_path: Path) -> None:
    """清单内有已删除文件 → 升为全量（结构变化，未改动文件的判定也可能变）。"""
    reason = sb.full_scan_reason(cfg_of(repo(tmp_path)), sample(), ["pkg/services/beta/src/beta/api/gone.py"])
    assert reason is not None and "已删除" in reason


@pytest.mark.parametrize(
    ("rel", "label"),
    [
        ("deploy/boundaries/domain_layout.json", "域布局"),
        ("deploy/boundaries/domain_boundary_baseline.json", "域级基线"),
        ("deploy/boundaries/data_ownership_exceptions.json", "读侧例外"),
        ("pkg/tables.py", "表归属登记"),
        ("pkg/catalog.py", "服务目录登记"),
    ],
)
def test_full_scan_reason_for_rule_sources(tmp_path: Path, rel: str, label: str) -> None:
    """触碰判据数据源（布局 / 基线 / 例外 / 归属登记 / 目录登记）→ 升为全量。"""
    reason = sb.full_scan_reason(cfg_of(repo(tmp_path)), sample(), [rel])
    assert reason is not None and label in reason


def test_full_scan_reason_for_dotted_module_source(tmp_path: Path) -> None:
    """登记源写成点分模块名（仓内常见形态）也能按末段名命中。"""
    root = repo(tmp_path, **{"pkg/core/services/table_registry.py": "X = 1\n"})
    rule = sample(ownership_module="core.services.table_registry")
    reason = sb.full_scan_reason(cfg_of(root), rule, ["pkg/core/services/table_registry.py"])
    assert reason is not None and "表归属登记" in reason


def test_full_scan_reason_for_repo_globs(tmp_path: Path) -> None:
    """仓侧 `incremental_full_when`：命中即升全量，未命中按增量。"""
    root = repo(tmp_path, **{"engkit.toml": ""})
    cfg = cfg_of(root)
    rule = sample(incremental_full_when=("deploy/**", "engkit.toml"))
    reason = sb.full_scan_reason(cfg, rule, ["engkit.toml"])
    assert reason is not None and "全量触发面" in reason
    assert sb.full_scan_reason(cfg, rule, ["pkg/services/beta/src/beta/api/__init__.py"]) is None


def test_run_rejects_update_baseline_with_incremental(tmp_path: Path) -> None:
    """`--update-baseline` 与增量互斥（基线必须按全量重写）。"""
    cfg = cfg_of(repo(tmp_path), sample())
    assert (
        sb.run(
            cfg,
            json_output=False,
            update_baseline=True,
            as_self_test=False,
            files_from="pkg/services/beta/src/beta/api/__init__.py",
        )
        == 2
    )


def test_run_unreadable_list_exits_two(tmp_path: Path) -> None:
    """清单不可读退 2，不静默退化成全量。"""
    cfg = cfg_of(repo(tmp_path), sample())
    assert (
        sb.run(
            cfg,
            json_output=False,
            update_baseline=False,
            as_self_test=False,
            files_from="tmp/not-there.txt",
        )
        == 2
    )


def test_run_incremental_marks_counts(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """增量通过：打印扫描面与「全局判据恒全量」，计数带上本次改动文件数。"""
    root = repo(tmp_path)
    sb.write_files(root, {"tmp/changed.txt": "pkg/services/beta/src/beta/api/__init__.py\n"})
    code = sb.run(
        cfg_of(root, sample()),
        json_output=True,
        update_baseline=False,
        as_self_test=False,
        files_from="tmp/changed.txt",
    )
    assert code == 0
    printed = capsys.readouterr().out
    assert "增量模式" in printed
    assert "全局判据" in printed
    payload = json.loads(printed[printed.index("{") :])
    assert int(payload["counts"]["incremental_files"]) == 1


def test_run_upgrades_to_full_on_trigger(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """命中触发面即升全量：输出说明原因，计数不带增量标记。"""
    root = repo(tmp_path)
    sb.write_files(root, {"tmp/changed.txt": "deploy/boundaries/domain_layout.json\n"})
    code = sb.run(
        cfg_of(root, sample()),
        json_output=True,
        update_baseline=False,
        as_self_test=False,
        files_from="tmp/changed.txt",
    )
    assert code == 0
    printed = capsys.readouterr().out
    assert "升为全量" in printed
    assert "域布局" in printed
    payload = json.loads(printed[printed.index("{") :])
    assert "incremental_files" not in payload["counts"]


def test_incremental_empty_list_scans_no_files(tmp_path: Path) -> None:
    """空清单：逐文件判据全不跑，全局判据照跑（域文件覆盖计数仍为全量值）。"""
    root = violation_repo(tmp_path)
    cfg = cfg_of(root)
    rule = sample()
    full = sb.check(cfg, rule)
    empty = sb.check(cfg, rule, changed=[])
    assert empty.counts["domain_files_registered"] == full.counts["domain_files_registered"]
    assert not any("[跨域 import]" in item for item in empty.problems)


def test_incremental_scoping_keeps_test_file_semantics(tmp_path: Path) -> None:
    """测试目录口径不变：规则 1（含测试）扫到改动测试文件；规则 6 等（跳过测试）不扫。"""
    root = repo(
        tmp_path,
        **{"pkg/services/beta/src/beta/tests/test_leak.py": "import alpha.services.mod\nVAR = 1\n"},
    )
    cfg = cfg_of(root)
    rule = sample()
    rel = "pkg/services/beta/src/beta/tests/test_leak.py"
    scoped = "\n".join(sb.check(cfg, rule, changed=[rel]).problems)
    assert "[服务互相依赖]" in scoped


def test_read_changed_empty_and_absolute(tmp_path: Path) -> None:
    """清单入口：空串即全量；绝对路径直接读（不拼仓根）。"""
    root = repo(tmp_path)
    cfg = cfg_of(root)
    assert sb.read_changed(cfg, "") is None
    sb.write_files(root, {"tmp/list.txt": "pkg/services/beta/src/beta/api/__init__.py\n"})
    absolute = str((root / "tmp/list.txt").resolve())
    assert sb.read_changed(cfg, absolute) == ["pkg/services/beta/src/beta/api/__init__.py"]
    assert sb.read_changed(cfg, "tmp/list.txt") == ["pkg/services/beta/src/beta/api/__init__.py"]


def test_full_scan_reason_tolerates_empty_rule_sources(tmp_path: Path) -> None:
    """登记源未配置（空串）时不参与触发判定（不能把任意同名文件当登记源）。"""
    rule = sample(ownership_module="", catalog_module="")
    reason = sb.full_scan_reason(cfg_of(repo(tmp_path)), rule, ["pkg/tables.py"])
    assert reason is None


def test_shared_glob_covers_skeleton_but_not_new_module(tmp_path: Path) -> None:
    """域外路径支持 glob：骨架 `__init__.py` 自动放行，同目录下的实现文件仍报未归属。"""
    layout = {
        "version": 1,
        "domains": [
            {
                "domain_key": "alpha",
                "service_key": "alpha",
                "paths": ["pkg/services/alpha/src/alpha/**/*.py"],
            },
            {"domain_key": "beta", "service_key": "beta", "paths": ["pkg/services/beta/src/beta/api/**/*.py"]},
        ],
        "shared_paths": ["pkg/services/*/src/*/*/__init__.py"],
    }
    root = repo(
        tmp_path,
        layout=layout,
        **{"pkg/services/beta/src/beta/utils/__init__.py": "", "pkg/services/beta/src/beta/utils/impl.py": "X = 1\n"},
    )
    joined = "\n".join(sb.check(cfg_of(root), sample()).problems)
    assert "utils/__init__.py" not in joined
    assert "[域实现未登记] pkg/services/beta/src/beta/utils/impl.py" in joined


def test_domain_and_shared_overlap_is_reported(tmp_path: Path) -> None:
    """域路径与域外声明**展开后**重叠即报错（glob 化后字符串比对已经不够）。"""
    layout = {
        "version": 1,
        "domains": [
            {
                "domain_key": "alpha",
                "service_key": "alpha",
                "paths": ["pkg/services/alpha/src/alpha/api/**/*.py"],
            },
            {"domain_key": "beta", "service_key": "beta", "paths": ["pkg/services/beta/src/beta/**/*.py"]},
        ],
        "shared_paths": ["pkg/services/alpha/src/alpha/api/__init__.py"],
    }
    root = repo(tmp_path, layout=layout)
    joined = "\n".join(sb.check(cfg_of(root), sample()).problems)
    assert "同时归属域 alpha 与域外路径" in joined
