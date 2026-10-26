"""基座校验器用例共用夹具：规则段样例 + 迷你仓库构造。

`test_base_check.py` 覆盖主流程；`test_base_check_edges.py` 覆盖各器的失败 / 跳过分支，
两处共用本文件的规则样例与仓库构造，避免夹具漂移。
"""

from __future__ import annotations

import tomllib
from pathlib import Path

from engkit.config import Config, parse

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
domain_layout = "deploy/domain_layout.json"

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


def write(root: Path, files: dict[str, str]) -> None:
    """按「相对路径 → 文本」写文件。

    Args:
        root: 仓根。
        files: 文件内容映射。
    """
    for rel, content in files.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")


def config(tmp_path: Path, extra: str = "") -> Config:
    """按规则样例造配置对象。

    Args:
        tmp_path: 临时仓根。
        extra: 追加的 TOML 片段。

    Returns:
        Config: 配置。
    """
    raw = tomllib.loads(RULES_TOML + extra)
    raw.setdefault("docs", {"root": "docs"})
    return parse(raw, tmp_path / "engkit.toml")


def status_stage(tmp_path: Path) -> Path:
    """搭最小阶段文档树（需求 / 任务 / 计划齐备且自洽）。

    Args:
        tmp_path: 临时仓根。

    Returns:
        Path: 阶段目录。
    """
    stage = tmp_path / "docs/项目/01_阶段"
    write(
        tmp_path,
        {
            "docs/项目/01_阶段/需求/00_需求_总览.md": "| 编号 | 标题 |\n| --- | --- |\n| 01-1 | 甲 |\n",
            "docs/项目/01_阶段/需求/01_需求_域.md": "## 1. 需求 01-1：甲\n",
            "docs/项目/01_阶段/任务/01_域/01_域.md": (
                "## 2. 子任务清单\n\n| 序号 | 任务 |\n| --- | --- |\n| 01 | 甲 |\n| 02 | 乙 |\n"
            ),
            "docs/项目/01_阶段/任务/01_域/01_任务/01_任务.md": (
                "## 1. 任务信息\n\n| 项 | 值 |\n| --- | --- |\n| 编号 | 01 |\n| 对应需求 | 01-1 |\n"
            ),
            "docs/项目/01_阶段/任务/01_域/02_任务/02_任务.md": (
                "## 1. 任务信息\n\n| 项 | 值 |\n| --- | --- |\n| 编号 | 02 |\n| 对应需求 | 01-1 |\n"
            ),
            "docs/项目/01_阶段/计划/01_计划_阶段.md": (
                "# 计划\n\n已完成 1 项，剩余 1 项\n\n"
                "已完成 **8h**；剩余 **8h**，阶段合计 **16h**\n\n"
                "## 2. 已完成任务\n\n"
                "| 编号 | 任务 | 工时 | 完成日期 | 备注 | 记录 |\n| --- | --- | --- | --- | --- | --- |\n"
                "| 01_01 | 甲 | 8h | 2026-10-20 | — | 有 |\n\n"
                "## 3. 剩余任务排期\n\n"
                "| 编号 | 任务 | 状态 | 工时 | 依赖 | 排期窗口 | 备注 | 归属 |\n"
                "| --- | --- | --- | --- | --- | --- | --- | --- |\n"
                "| 01_02 | 乙 | 未开始 | 8h | — | 2026-10-21 ~ 2026-10-22 | — | minjian |\n\n"
                "```mermaid\ngantt\n01_02 乙\n```\n"
            ),
        },
    )
    (stage / "任务/01_域/01_任务/实施").mkdir(parents=True, exist_ok=True)
    return stage


def backend_repo(tmp_path: Path) -> None:
    """搭最小后端基类体系仓库（合规）。

    Args:
        tmp_path: 临时仓根。
    """
    write(
        tmp_path,
        {
            "src/pkg/base.py": "class BaseObj(object):\n    pass\n\n\nclass BaseMid(BaseObj):\n    pass\n",
            "docs/契约.md": "## 7. 继承链与代码位置\n\n- `BaseMid → BaseObj`。\n\n体系根清单\n\n- `BaseObj`\n",
            "docs/基类清单.md": "| BaseObj | 体系根 |\n",
            "src/errors.py": "class ErrorCode:\n    OK = 10001\n",
            "versions/platform/tenant/0001_init.py": (
                'revision = "0001"\ndown_revision = None\nbranch_labels = ("platform:tenant",)\n'
            ),
            "alembic.ini": "[alembic]\n",
        },
    )
