"""基座校验器（`engkit base <名>`）配置与判定用例。

覆盖：`[rules.*]` 解析与校验（缺段 / 未知键 / 类型错）、`rules.require` 报错口径、
七个已迁校验器的 `run()` 分支（未登记 / 自检 / 通过 / 有问题 / JSON·报告·基线）与
各器内置自检矩阵。
"""

from __future__ import annotations

import json
import tomllib
from pathlib import Path

import pytest

from engkit import cli
from engkit.base import (
    backend_base,
    bare_collections,
    base_docs,
    docs_status,
    env_example,
    prototype_review,
    system_fields,
)
from engkit.base.rules import RulesError, require, sub_dirs
from engkit.config import ConfigError, parse

RULES_TOML = r"""
[rules.base]
manifest = "docs/清单.md"
base_dirs = ["docs/规范"]
local_only = ["docs/用户文档/"]
cross_layer = ["docs/项目/"]
bad_wording = ["本项目项目"]

[rules.backend_base]
manifest = "docs/基类清单.md"
chain_doc = "docs/契约.md"
source_dirs = ["src"]
error_codes = "src/errors.py"
versions_dir = "versions"
direct_baseline = "baseline.json"
root_bases_marker = "体系根清单"
value_object_root = "BaseValueObject"
framework_object_root = "BaseFrameworkObject"
datasources = ["platform", "tenant"]
default_chain = "platform:tenant"

[rules.bare_collections]
scan_targets = ["src", "svc"]
whitelist = ["StableList", "StableSet", "StableDict", "StableFrozenList"]
unregistered = ["deque", "Counter"]
allow_marker = "# bare-collections:allow"
baseline = "baseline.json"

[rules.bare_collections.banned]
bare = ["dict", "list", "set", "frozenset", "List"]
readonly = ["Sequence", "Mapping", "AbstractSet"]
ascending = ["SortedList"]

[rules.bare_collections.reasons]
banned = "落白名单"

[rules.system_fields]
table_dir = "docs/表"
row_form_key = "form_key"
row_parent_id = "parent_id"

[rules.system_fields.catalog]
path = "src/forms.py"
const = "FORM_CATALOG"
enum = "FormKind"
key_field = "form_key"
table_field = "table"
kind_field = "kind"

[rules.system_fields.datasource]
path = "src/datasource.py"
const = "DATA_SOURCE_CATALOG"
enum = "DataSourceKind"
key_field = "data_source_key"

[rules.env_example]
removed_keys = ["OLD_KEY"]

[[rules.env_example.templates]]
path = "backend/.env.example"
required_keys = ["SECRET"]

[rules.docs_status]
project_dir = "docs/项目"
stage = "01_阶段"
statuses = ["未开始", "已完成"]
todo_statuses = ["未开始"]
done_status = "已完成"
req_id_pattern = '^\d{2}-\d+$'
task_id_pattern = '^\d{2}-\d+-\d+$'
plan_id_pattern = '^\d{2}_\d{2}$'

[rules.prototype]
record_pattern = '^docs/项目/[^/]+/任务/.+/测试/[^/]+\.md$'
context_pattern = '^docs/项目/[^/]+/任务/.+/(设计|实施|测试)/[^/]+\.md$'
prototype_ref_pattern = '(原型/[^\s()\[\]"`<>|]*\.html)'
ui_patterns = ['^frontend/.*\.vue$']
section_heading = '^##.*原型'
screenshot_pattern = '\]\(([^)\s]+\.png)\)'
unfinished_words = ["待补", "TODO"]
base = "origin/main"
"""


def _cfg(tmp_path: Path, extra: str = "") -> object:
    """造配置对象（规则段取内置样例）。

    Args:
        tmp_path: 临时目录（作仓根）。
        extra: 追加的 TOML 片段。

    Returns:
        Config: 配置。
    """
    raw = tomllib.loads(RULES_TOML + extra)
    raw.setdefault("docs", {"root": "docs"})
    return parse(raw, tmp_path / "engkit.toml")


def test_rules_sections_parsed(tmp_path: Path) -> None:
    """九组规则段按样例解析。"""
    cfg = _cfg(tmp_path)
    assert cfg.base_rules is not None and cfg.base_rules.manifest == "docs/清单.md"
    assert cfg.backend_base_rules is not None and cfg.backend_base_rules.source_dirs == ("src",)
    assert cfg.collections_rules is not None and cfg.collections_rules.whitelist[0] == "StableList"
    assert cfg.system_fields_rules is not None and cfg.system_fields_rules.row_form_key == "form_key"
    assert cfg.env_example_rules is not None and len(cfg.env_example_rules.templates) == 1
    assert cfg.docs_status_rules is not None and cfg.docs_status_rules.done_status == "已完成"
    assert cfg.prototype_rules is not None and cfg.prototype_rules.base == "origin/main"


def test_rules_sections_absent(tmp_path: Path) -> None:
    """未登记即 None（该校验器未启用）。"""
    cfg = parse({"docs": {"root": "docs"}}, tmp_path / "engkit.toml")
    assert cfg.base_rules is None
    assert cfg.collections_rules is None
    assert cfg.boundaries_rules is None
    assert cfg.metrics_rules is None


def test_rules_unknown_key_rejected(tmp_path: Path) -> None:
    """未知键即报错（不靠缺省值掩盖）。"""
    with pytest.raises(ConfigError, match="未知键"):
        _cfg(tmp_path, "\n[rules.metrics]\ngone = 1\n")


def test_rules_type_error_rejected(tmp_path: Path) -> None:
    """类型不符即报错。"""
    with pytest.raises(ConfigError, match="应为字符串数组"):
        _cfg(tmp_path, '\n[rules.metrics]\nviolation_keys = "not-a-list"\n')


def test_rules_require_messages(tmp_path: Path) -> None:
    """`require` 口径：缺段即报错并给出最小键指引，已登记则原样返回。"""
    with pytest.raises(RulesError, match="按《接入说明》登记后启用（最小键：manifest）"):
        require(None, "base", ("manifest",))
    rules = _cfg(tmp_path).env_example_rules
    assert require(rules, "env_example", ("templates",)) is rules


def test_sub_dirs_lists_sorted(tmp_path: Path) -> None:
    """一级子目录列举（缺目录即空）。"""
    assert sub_dirs(tmp_path / "missing") == []
    (tmp_path / "b").mkdir()
    (tmp_path / "a").mkdir()
    assert [item.name for item in sub_dirs(tmp_path)] == ["a", "b"]


def test_base_docs_run_paths(tmp_path: Path) -> None:
    """基座完整性：未登记 / 自检 / 通过 / 不通过。"""
    assert base_docs.run(parse({"docs": {"root": "docs"}}, tmp_path / "engkit.toml"), as_self_test=False) == 2
    cfg = _cfg(tmp_path)
    assert base_docs.run(cfg, as_self_test=True) == 0
    (tmp_path / "docs/规范").mkdir(parents=True)
    (tmp_path / "docs/规范/规范.md").write_text("# 规范\n", encoding="utf-8")
    (tmp_path / "docs/清单.md").write_text("```text\n└── 规范/\n    └── 规范.md\n```\n", encoding="utf-8")
    assert base_docs.run(cfg, as_self_test=False) == 0
    (tmp_path / "docs/规范/规范.md").write_text("# 本项目项目\n", encoding="utf-8")
    assert base_docs.run(cfg, as_self_test=False) == 1
    assert [line for line in base_docs.collect(cfg, cfg.base_rules)[1]]


def test_backend_base_run_paths(tmp_path: Path) -> None:
    """后端基类体系：未登记 / 登记源缺失 / 自检 / 通过 / 基线重写。"""
    assert (
        backend_base.run(
            parse({"docs": {"root": "docs"}}, tmp_path / "engkit.toml"), as_self_test=False, update_baseline=False
        )
        == 2
    )
    cfg = _cfg(tmp_path)
    assert backend_base.run(cfg, as_self_test=True, update_baseline=False) == 0
    assert backend_base.run(cfg, as_self_test=False, update_baseline=False) == 2
    assert backend_base.run(cfg, as_self_test=False, update_baseline=True) == 0
    (tmp_path / "baseline.json").write_text(json.dumps({"version": 1, "entries": []}), encoding="utf-8")


def test_bare_collections_run_paths(tmp_path: Path) -> None:
    """集合声明：未登记 / 自检 / 通过 / JSON / 报告 / 基线重写。"""
    assert (
        bare_collections.run(
            parse({"docs": {"root": "docs"}}, tmp_path / "engkit.toml"),
            json_output=False,
            report_mode=False,
            update_baseline=False,
            as_self_test=False,
        )
        == 2
    )
    cfg = _cfg(tmp_path)
    assert (
        bare_collections.run(cfg, json_output=False, report_mode=False, update_baseline=False, as_self_test=True) == 0
    )
    (tmp_path / "src").mkdir(parents=True)
    (tmp_path / "src/mod.py").write_text("def f(x: list[int]) -> None:\n    del x\n", encoding="utf-8")
    assert (
        bare_collections.run(cfg, json_output=False, report_mode=False, update_baseline=True, as_self_test=False) == 0
    )
    assert (
        bare_collections.run(cfg, json_output=False, report_mode=False, update_baseline=False, as_self_test=False) == 0
    )
    assert (
        bare_collections.run(cfg, json_output=True, report_mode=False, update_baseline=False, as_self_test=False) == 0
    )
    assert (
        bare_collections.run(cfg, json_output=False, report_mode=True, update_baseline=False, as_self_test=False) == 0
    )
    (tmp_path / "src/new.py").write_text("class A:\n    data: set[int] = set()\n", encoding="utf-8")
    assert (
        bare_collections.run(cfg, json_output=False, report_mode=False, update_baseline=False, as_self_test=False) == 1
    )
    (tmp_path / "baseline.json").write_text("{ broken", encoding="utf-8")
    assert (
        bare_collections.run(cfg, json_output=False, report_mode=False, update_baseline=False, as_self_test=False) == 1
    )


def test_bare_collections_baseline_errors(tmp_path: Path) -> None:
    """基线非法分支（版本 / 条目形态 / 计数）。"""
    cfg = _cfg(tmp_path)
    rule = cfg.collections_rules
    path = tmp_path / "baseline.json"
    for payload, match in (
        ({"version": 2, "entries": []}, "版本非法"),
        ({"version": 1, "entries": {}}, "entries 非法"),
        ({"version": 1, "entries": ["x"]}, "条目非法"),
        ({"version": 1, "entries": [{"file": "a"}]}, "字段缺失或非法"),
        ({"version": 1, "entries": [{"file": "a", "line": "b", "container": "c", "count": 0}]}, "count 非法"),
    ):
        path.write_text(json.dumps(payload), encoding="utf-8")
        with pytest.raises(bare_collections.BaselineError, match=match):
            bare_collections.read_baseline(path, rule)


def test_bare_collections_scan_error(tmp_path: Path) -> None:
    """扫描异常记入问题清单（语法错误文件）。"""
    cfg = _cfg(tmp_path)
    (tmp_path / "src").mkdir(parents=True)
    (tmp_path / "src/bad.py").write_text("def f(:\n", encoding="utf-8")
    outcome = bare_collections.check(cfg, cfg.collections_rules)
    assert any("扫描异常" in item for item in outcome.problems)


def test_env_example_run_paths(tmp_path: Path) -> None:
    """环境模板：未登记 / 自检 / 通过 / 缺失 / 不通过。"""
    assert env_example.run(parse({"docs": {"root": "docs"}}, tmp_path / "engkit.toml"), as_self_test=False) == 2
    cfg = _cfg(tmp_path)
    assert env_example.run(cfg, as_self_test=True) == 0
    assert env_example.run(cfg, as_self_test=False) == 2
    template = tmp_path / "backend/.env.example"
    template.parent.mkdir(parents=True)
    template.write_text("SECRET=...\n", encoding="utf-8")
    assert env_example.run(cfg, as_self_test=False) == 0
    template.write_text("OTHER=1\n", encoding="utf-8")
    assert env_example.run(cfg, as_self_test=False) == 1


def test_system_fields_run_paths(tmp_path: Path) -> None:
    """系统字段：未登记 / 自检 / 清单缺失 / 通过。"""
    assert system_fields.run(parse({"docs": {"root": "docs"}}, tmp_path / "engkit.toml"), as_self_test=False) == 2
    cfg = _cfg(tmp_path)
    assert system_fields.run(cfg, as_self_test=True) == 0
    assert system_fields.run(cfg, as_self_test=False) == 1
    (tmp_path / "src").mkdir(parents=True)
    (tmp_path / "src/forms.py").write_text(
        "class FormKind(StrEnum):\n    DICT = 'dict'\n\n"
        "FORM_CATALOG = (\n"
        "    FormRecord(form_key='role', table='sys_role', data_source_key='role', kind=FormKind.DICT.value),\n"
        ")\n",
        encoding="utf-8",
    )
    (tmp_path / "src/datasource.py").write_text(
        "class DataSourceKind(StrEnum):\n    DICT = 'dict'\n\n"
        "DATA_SOURCE_CATALOG = (\n"
        "    SourceRecord(data_source_key='role', kind=DataSourceKind.DICT.value),\n"
        ")\n",
        encoding="utf-8",
    )
    (tmp_path / "docs/表").mkdir(parents=True)
    (tmp_path / "docs/表/sys_role.md").write_text("| `form_key` | x |\n| `parent_id` | x |\n", encoding="utf-8")
    assert system_fields.run(cfg, as_self_test=False) == 0


def test_docs_status_run_paths(tmp_path: Path) -> None:
    """文档一致性：未登记 / 目录缺失 / 通过 / 不合规 / 指定阶段。"""
    assert (
        docs_status.run(
            parse({"docs": {"root": "docs"}}, tmp_path / "engkit.toml"), stage="", all_stages=True, strict=False
        )
        == 2
    )
    cfg = _cfg(tmp_path)
    assert docs_status.run(cfg, stage="", all_stages=False, strict=False) == 2
    stage = tmp_path / "docs/项目/01_阶段"
    (stage / "需求").mkdir(parents=True)
    (stage / "计划").mkdir(parents=True)
    (stage / "需求/00_需求_总览.md").write_text("| 编号 | 标题 |\n| --- | --- |\n| 01-1 | x |\n", encoding="utf-8")
    (stage / "需求/01_需求_域.md").write_text("## 1. 需求 01-1：x\n", encoding="utf-8")
    (stage / "计划/01_计划_阶段.md").write_text("# 计划\n", encoding="utf-8")
    (stage / "任务").mkdir()
    assert docs_status.run(cfg, stage="", all_stages=False, strict=True) == 0
    (stage / "需求/01_需求_域.md").write_text("## 1. 需求 01-2：y\n", encoding="utf-8")
    assert docs_status.run(cfg, stage="", all_stages=False, strict=False) == 1


def test_prototype_review_run_paths(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """界面复核：未登记 / 自检 / 巡检模式缺陷 / 变更模式跳过与非界面改动。"""
    assert (
        prototype_review.run(
            parse({"docs": {"root": "docs"}}, tmp_path / "engkit.toml"), changed="", task="", as_self_test=False
        )
        == 2
    )
    cfg = _cfg(tmp_path)
    assert prototype_review.run(cfg, changed="", task="", as_self_test=True) == 0
    assert prototype_review.run(cfg, changed="", task="", as_self_test=False) == 2
    (tmp_path / "docs/项目").mkdir(parents=True)
    assert prototype_review.run(cfg, changed="", task="", as_self_test=False) == 0
    monkeypatch.setattr(prototype_review, "git_changed", lambda root, base: ["docs/readme.md"])
    assert prototype_review.run(cfg, changed="origin/main", task="", as_self_test=False) == 0
    monkeypatch.setattr(prototype_review, "git_changed", lambda root, base: None)
    assert prototype_review.run(cfg, changed="origin/main", task="", as_self_test=False) == 0
    monkeypatch.setattr(prototype_review, "git_changed", lambda root, base: ["frontend/apps/desktop/src/views/a.vue"])
    assert prototype_review.run(cfg, changed="origin/main", task="", as_self_test=False) == 1
    task_dir = tmp_path / "docs/项目/01_阶段/任务/01_域/01_任务"
    (task_dir / "测试").mkdir(parents=True)
    assert prototype_review.run(cfg, changed="", task=str(tmp_path / "missing"), as_self_test=False) == 2
    assert prototype_review.run(cfg, changed="", task=str(task_dir), as_self_test=False) == 0


def _backend_repo(tmp_path: Path) -> None:
    """搭最小后端基类体系仓库（成功路径）。

    Args:
        tmp_path: 临时仓根。
    """
    (tmp_path / "src/pkg").mkdir(parents=True)
    (tmp_path / "src/pkg/base.py").write_text(
        "class BaseObj(object):\n    pass\n\n\nclass BaseMid(BaseObj):\n    pass\n", encoding="utf-8"
    )
    (tmp_path / "docs").mkdir(parents=True)
    (tmp_path / "docs/契约.md").write_text(
        "## 7. 继承链与代码位置\n\n- `BaseMid → BaseObj`。\n\n体系根清单\n\n- `BaseObj`\n", encoding="utf-8"
    )
    (tmp_path / "docs/基类清单.md").write_text("| BaseObj | 体系根 |\n", encoding="utf-8")
    (tmp_path / "src/errors.py").write_text("class ErrorCode:\n    OK = 10001\n", encoding="utf-8")
    (tmp_path / "versions/platform/tenant").mkdir(parents=True)
    (tmp_path / "versions/platform/tenant/0001_init.py").write_text(
        'revision = "0001"\ndown_revision = None\nbranch_labels = ("platform:tenant",)\n', encoding="utf-8"
    )
    (tmp_path / "alembic.ini").write_text("[alembic]\n", encoding="utf-8")


def test_backend_base_full_flow(tmp_path: Path) -> None:
    """后端基类体系：完整仓库通过；逐项制造违规即拦截。"""
    _backend_repo(tmp_path)
    cfg = _cfg(tmp_path)
    assert backend_base.run(cfg, as_self_test=False, update_baseline=False) == 0

    chain = tmp_path / "docs/契约.md"
    chain.write_text(
        "## 7. 继承链与代码位置\n\n- `BaseOther → BaseObj`。\n\n体系根清单\n\n- `BaseObj`\n", encoding="utf-8"
    )
    assert backend_base.run(cfg, as_self_test=False, update_baseline=False) == 1

    chain.write_text("## 7. 继承链与代码位置\n\n- `BaseMid → BaseObj`。\n", encoding="utf-8")
    assert backend_base.run(cfg, as_self_test=False, update_baseline=False) == 1

    chain.write_text(
        "## 7. 继承链与代码位置\n\n- `BaseMid → BaseObj`。\n\n体系根清单\n\n- `BaseObj`\n", encoding="utf-8"
    )
    (tmp_path / "src/errors.py").write_text("class ErrorCode:\n    BAD = 100001\n", encoding="utf-8")
    assert backend_base.run(cfg, as_self_test=False, update_baseline=False) == 1

    (tmp_path / "src/errors.py").write_text("class ErrorCode:\n    OK = 10001\n", encoding="utf-8")
    (tmp_path / "versions/platform/tenant/0002_b.py").write_text(
        'revision = "0002"\ndown_revision = "ghost"\nbranch_labels = None\n', encoding="utf-8"
    )
    assert backend_base.run(cfg, as_self_test=False, update_baseline=False) == 1


def test_backend_base_direct_inheritance_scan(tmp_path: Path) -> None:
    """直继承扫描：未登记体系根之外的直继承被拦，基线可吸收。"""
    _backend_repo(tmp_path)
    (tmp_path / "src/pkg/extra.py").write_text("class Direct(BaseObject):\n    pass\n", encoding="utf-8")
    (tmp_path / "docs/基类清单.md").write_text("| BaseObj | 体系根 |\n| Direct | 存量 |\n", encoding="utf-8")
    cfg = _cfg(tmp_path)
    assert backend_base.run(cfg, as_self_test=False, update_baseline=False) == 1
    assert backend_base.run(cfg, as_self_test=False, update_baseline=True) == 0
    assert backend_base.run(cfg, as_self_test=False, update_baseline=False) == 0


def test_cli_base_dispatch(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """CLI 分派：七个子命令均可解析，未知名退 2。"""
    config = tmp_path / "engkit.toml"
    config.write_text(RULES_TOML + '\n[docs]\nroot = "docs"\n', encoding="utf-8")
    root = ["--root", str(tmp_path), "--config", str(config)]
    assert cli.main(["base", "env-example", *root]) == 2
    assert cli.main(["base", "system-fields", "--self-test", *root]) == 0
    assert cli.main(["base", "docs-status", *root]) == 2
    assert cli.main(["base", "prototype-review", "--self-test", *root]) == 0
    assert cli.main(["base", "bare-collections", "--report", *root]) == 0
    assert cli.main(["base", "base", "--self-test", *root]) == 0
    assert cli.main(["base", "backend-base", "--self-test", *root]) == 0
    capsys.readouterr()


def _status_stage(tmp_path: Path) -> Path:
    """搭最小阶段文档树（需求 / 任务 / 计划齐备且自洽）。

    Args:
        tmp_path: 临时仓根。

    Returns:
        Path: 阶段目录。
    """
    stage = tmp_path / "docs/项目/01_阶段"
    (stage / "需求").mkdir(parents=True)
    (stage / "计划").mkdir(parents=True)
    (stage / "需求/00_需求_总览.md").write_text("| 编号 | 标题 |\n| --- | --- |\n| 01-1 | 甲 |\n", encoding="utf-8")
    (stage / "需求/01_需求_域.md").write_text("## 1. 需求 01-1：甲\n", encoding="utf-8")
    domain = stage / "任务/01_域"
    domain.mkdir(parents=True)
    (domain / "01_域.md").write_text(
        "## 2. 子任务清单\n\n| 序号 | 任务 |\n| --- | --- |\n| 01 | 甲 |\n| 02 | 乙 |\n", encoding="utf-8"
    )
    task = domain / "01_任务"
    (task / "实施").mkdir(parents=True)
    (task / "01_任务.md").write_text(
        "## 1. 任务信息\n\n| 项 | 值 |\n| --- | --- |\n| 编号 | 01 |\n| 对应需求 | 01-1 |\n", encoding="utf-8"
    )
    second = domain / "02_任务"
    second.mkdir()
    (second / "02_任务.md").write_text(
        "## 1. 任务信息\n\n| 项 | 值 |\n| --- | --- |\n| 编号 | 02 |\n| 对应需求 | 01-1 |\n", encoding="utf-8"
    )
    (stage / "计划/01_计划_阶段.md").write_text(
        "# 计划\n\n已完成 1 项，剩余 1 项\n\n"
        "已完成 **8h**；剩余 **8h**，阶段合计 **16h**\n\n"
        "## 2. 已完成任务\n\n"
        "| 编号 | 任务 | 工时 | 完成日期 | 备注 | 记录 |\n| --- | --- | --- | --- | --- | --- |\n"
        "| 01_01 | 甲 | 8h | 2026-10-20 | — | 有 |\n\n"
        "## 3. 剩余任务排期\n\n"
        "| 编号 | 任务 | 状态 | 工时 | 依赖 | 排期窗口 | 备注 | 归属 |\n"
        "| --- | --- | --- | --- | --- | --- | --- | --- |\n"
        "| 01_02 | 乙 | 未开始 | 8h | — | 2026-10-21 ~ 2026-10-22 | — | minjian |\n\n"
        "```mermaid\ngantt\n01_02 乙\n```\n",
        encoding="utf-8",
    )
    return stage


def test_docs_status_rule_branches(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """文档一致性：齐备通过；各硬 / 软规则分别拦截。"""
    stage = _status_stage(tmp_path)
    cfg = _cfg(tmp_path)
    assert docs_status.run(cfg, stage="", all_stages=False, strict=True) == 0
    capsys.readouterr()

    plan = stage / "计划/01_计划_阶段.md"
    baseline = plan.read_text(encoding="utf-8")
    plan.write_text(baseline.replace("| 01_02 | 乙 | 未开始 |", "| 01_02 | 乙 | 进行中 |"), encoding="utf-8")
    assert docs_status.run(cfg, stage="", all_stages=False, strict=False) == 1
    plan.write_text(baseline.replace("2026-10-21 ~ 2026-10-22", "下周"), encoding="utf-8")
    assert docs_status.run(cfg, stage="", all_stages=False, strict=False) == 1
    plan.write_text(baseline.replace("已完成 1 项，剩余 1 项", "已完成 2 项，剩余 2 项"), encoding="utf-8")
    assert docs_status.run(cfg, stage="", all_stages=True, strict=True) == 1
    plan.write_text(baseline.replace("01_02 乙\n", ""), encoding="utf-8")
    assert docs_status.run(cfg, stage="", all_stages=False, strict=True) == 1
    plan.write_text(baseline, encoding="utf-8")
    (stage / "任务/01_域/01_域.md").write_text(
        "## 2. 子任务清单\n\n| 序号 | 任务 |\n| --- | --- |\n| 01 | 甲 |\n| 02 | 乙 |\n| 03 | 丙 |\n",
        encoding="utf-8",
    )
    assert docs_status.run(cfg, stage="", all_stages=False, strict=False) == 1
    (stage / "任务/01_域/01_域.md").write_text(
        "## 2. 子任务清单\n\n| 序号 | 任务 |\n| --- | --- |\n| 01 | 甲 |\n", encoding="utf-8"
    )
    assert docs_status.run(cfg, stage="", all_stages=False, strict=False) == 1
    (stage / "任务/01_域/01_域.md").write_text(
        "## 2. 子任务清单\n\n| 序号 | 任务 |\n| --- | --- |\n| 01 | 甲 |\n| 02 | 乙 |\n", encoding="utf-8"
    )
    (stage / "任务/01_域/01_任务/实施").rmdir()
    assert docs_status.run(cfg, stage="", all_stages=False, strict=True) == 1
    capsys.readouterr()
