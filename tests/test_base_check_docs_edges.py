"""文档类基座校验器（`base base` / `base docs-status`）的分支用例。

覆盖：跨层引用 / 措辞 / 清单 / 编号引用的判定分支、数据库设计登记与命名形态、运行期登记值，
以及需求 / 任务 / 计划一致性的 H1 / H3 / H5 / H6 / H7 / H8 与 S1 / S2 / S3 各条失败分支。
"""

from __future__ import annotations

import dataclasses
import json
from pathlib import Path

import pytest
from base_fixtures import config, status_stage, write

from engkit.base import base_docs, docs_status
from engkit.config import DesignRegistryRules, NamingRules, RuntimeIdentifierRule

DESIGN = DesignRegistryRules(
    dir="docs/数据库设计",
    overview="docs/数据库设计/01_总览.md",
    table_subdir="数据表设计",
    dialect_subdir="方言特性",
    status_values=("已设计", "待落库", "已落库", "已废弃"),
)

OVERVIEW = (
    "\n| 表 | 数据表文件 | 所属库 | 模块 | 状态 |\n| --- | --- | --- | --- | --- |\n"
    "| sys_tenant | [sys_tenant.md](数据表设计/sys_tenant.md) | 平台库 | 26-租户管理 | 已落库 |\n\n"
    "| 库 | 方言特性文件 | 适用版本 | 应用侧驱动 | 状态 |\n| --- | --- | --- | --- | --- |\n"
    "| MySQL | [01_MySQL.md](方言特性/01_MySQL.md) | 8.x | `aiomysql` | 已实测 |\n"
)


def test_md_files_missing(tmp_path: Path) -> None:
    """目录不存在时文件清单为空。"""
    assert base_docs._md_files(tmp_path / "missing") == []


def test_cross_layer_and_wording(tmp_path: Path) -> None:
    """跨层引用（外部链接 / 非文档后缀 / 越界路径 / 命中跨层前缀）与措辞残留。"""
    cfg = config(tmp_path)
    rule = cfg.base_rules
    assert rule is not None
    write(
        tmp_path,
        {
            "docs/规范/a.md": (
                "[外链](https://example.com)\n"
                "[不在内](other.txt)\n"
                "[越界](../../../outside.md)\n"
                "[同层](b.md)\n"
                "[跨层](../项目/x.md)\n"
                "[带锚点](../项目/x.md#sec)\n"
            ),
            "docs/项目/x.md": "# x\n",
            "docs/规范/b.md": "本项目项目\n",
        },
    )
    cross, problems = base_docs.check_cross_layer(cfg, rule)
    assert cross == 2
    assert all("[跨层引用]" in item for item in problems)
    wording, wproblems = base_docs.check_wording(cfg, rule)
    assert wording == 1
    assert "[措辞残留]" in wproblems[0]


def test_manifest_files_branches(tmp_path: Path) -> None:
    """清单解析：文件缺失 / 无清单块 / 含空行与无法识别行 / 数量不符。"""
    cfg = config(tmp_path)
    rule = cfg.base_rules
    assert rule is not None
    assert base_docs.manifest_files(cfg, rule) == []
    write(tmp_path, {"docs/清单.md": "# 无清单块\n"})
    assert base_docs.manifest_files(cfg, rule) == []
    write(
        tmp_path,
        {
            "docs/清单.md": (
                "```text\n"
                "bms文档/\n"
                "├── 规范/\n"
                "│   ├── 规范.md\n"
                "\n"
                "│   └── 其他.md   # 备注\n"
                "└── 用户文档/\n"
                "    └── 本地.md\n"
                "```\n"
                "```text\n无关块\n```\n"
            ),
            "docs/规范/规范.md": "# 规范\n",
        },
    )
    entries = base_docs.manifest_files(cfg, rule)
    assert entries == ["docs/规范/规范.md", "docs/规范/其他.md", "docs/用户文档/本地.md"]
    count, disk, missing, problems = base_docs.check_manifest(cfg, rule)
    assert count == 2
    assert disk == 1
    assert missing == 1
    assert any("[清单缺文件]" in item for item in problems)
    assert any("[清单数量不符]" in item for item in problems)


def test_section_refs(tmp_path: Path) -> None:
    """跨文档编号引用：写章节名放行，只写编号即报。"""
    cfg = config(tmp_path)
    rule = cfg.base_rules
    assert rule is not None
    write(
        tmp_path,
        {
            "docs/规范/规范.md": "见《其他手册》第 3 节；见《规范》第 2 节；见《规范附录》2.1 章。\n",
        },
    )
    refs, problems = base_docs.check_section_refs(cfg, rule)
    assert refs == 1
    assert "《其他手册》" in problems[0]


def test_design_registry_branches(tmp_path: Path) -> None:
    """设计登记：未配置 / 总览缺失 / 各类不一致 / 表文件缺状态行。"""
    cfg = config(tmp_path)
    rule = cfg.base_rules
    assert rule is not None
    line, problems = base_docs.check_design_registry(cfg, rule)
    assert "跳过" in line and problems == []

    with_design = dataclasses.replace(rule, design=DESIGN)
    line, problems = base_docs.check_design_registry(cfg, with_design)
    assert "不存在" in line and problems == []

    write(
        tmp_path,
        {
            "docs/数据库设计/01_总览.md": OVERVIEW,
            "docs/数据库设计/数据表设计/sys_tenant.md": "| 状态 | 已落库（平台链） |\n",
            "docs/数据库设计/方言特性/01_MySQL.md": "# MySQL\n",
        },
    )
    line, problems = base_docs.check_design_registry(cfg, with_design)
    assert "0 处问题" in line and problems == []

    found = base_docs.diff_design_registry(
        OVERVIEW,
        {"sys_tenant.md"},
        {"01_MySQL.md"},
        {"sys_tenant": "待落库"},
        DESIGN,
    )
    assert any("[状态不一致]" in item for item in found)

    found = base_docs.diff_design_registry(OVERVIEW, {"sys_tenant.md"}, {"01_MySQL.md"}, {}, DESIGN)
    assert any("[表文件缺状态]" in item for item in found)

    bad_status = OVERVIEW.replace("| 已落库 |", "| 已定稿 |")
    found = base_docs.diff_design_registry(
        bad_status, {"sys_tenant.md"}, {"01_MySQL.md"}, {"sys_tenant": "已定稿"}, DESIGN
    )
    assert any("[状态取值非法]" in item for item in found)

    found = base_docs.diff_design_registry(OVERVIEW, set(), set(), {}, DESIGN)
    assert any("[表文件缺失]" in item for item in found)
    assert any("[方言文件缺失]" in item for item in found)

    found = base_docs.diff_design_registry(
        OVERVIEW, {"sys_tenant.md", "sys_other.md"}, {"01_MySQL.md", "02_PG.md"}, {}, DESIGN
    )
    assert any("[未登记表文件]" in item for item in found)
    assert any("[未登记方言文件]" in item for item in found)

    write(tmp_path, {"docs/规范/状态.md": "无状态行\n"})
    assert base_docs.read_table_status(tmp_path / "docs/规范/状态.md") is None
    write(tmp_path, {"docs/规范/状态.md": "| 状态 | 已设计 |\n"})
    assert base_docs.read_table_status(tmp_path / "docs/规范/状态.md") == "已设计"

    tables, dialects = base_docs.parse_design_overview(
        "| 表 |\n| a | b | c |\n| MySQL | 方言特性/无括号.md | x | y | 已实测 |", DESIGN
    )
    assert tables == {} and dialects == {}


def test_name_problems_branches(tmp_path: Path) -> None:
    """命名形态：工具目录 / 脚本名 / 前端包目录与包名 / 清单目录。"""
    cfg = config(tmp_path)
    naming = NamingRules(
        tool_root="scripts",
        package_roots=("frontend/packages", "frontend/missing"),
        versions_root="docs/版本",
    )
    write(
        tmp_path,
        {
            "scripts/Bad-Dir/keep.txt": "",
            "scripts/bad-script.py": "",
            "scripts/说明.py": "",
            "scripts/good_script.py": "",
            "scripts/plain.txt": "",
            "frontend/packages/BadPkg/package.json": json.dumps({"name": "bad-name"}),
            "frontend/packages/loose.txt": "",
            "frontend/packages/ok_pkg/package.json": "{ broken",
            "docs/版本/Bad-Dir/keep.txt": "",
        },
    )
    problems = base_docs.name_problems(cfg, naming)
    joined = "\n".join(problems)
    assert "工具目录名须为 snake_case" in joined
    assert "工具脚本名须为 snake_case" in joined
    assert "前端包目录名须为 snake_case" in joined
    assert "包名须为 snake_case" in joined
    assert "清单目录名须为 snake_case" in joined
    empty = NamingRules(tool_root="", package_roots=(), versions_root="")
    assert base_docs.name_problems(cfg, empty) == []


def test_runtime_problems_branches(tmp_path: Path) -> None:
    """运行期登记值：控件键清单缺失 / 非法提取方式 / 数量不足 / 取值形态非法。"""
    cfg = config(tmp_path)
    write(
        tmp_path,
        {
            "frontend/widgets.ts": "export const OTHER = ['a'];\n",
            "src/widget.py": 'x = widget="Bad-Key"\n',
            "src/code.py": 'code = Field(min_length=1, max_length=64, pattern=r"^[A-Z]+$")\n',
            "src/icon.py": '_ICON_CODE_RE = re.compile(r"^[0-9]+$")\n',
        },
    )
    rule = dataclasses.replace(
        cfg.base_rules,
        runtime=(
            RuntimeIdentifierRule(path="frontend/widgets.ts", extract="ts_widgets", expect_count=1),
            RuntimeIdentifierRule(path="src/widget.py", extract="widget_literal", expect_count=1),
            RuntimeIdentifierRule(path="src/code.py", extract="code_pattern", expect_count=2),
            RuntimeIdentifierRule(path="src/icon.py", extract="icon_pattern", expect_count=1),
            RuntimeIdentifierRule(path="src/missing.py", extract="code_pattern", expect_count=1),
            RuntimeIdentifierRule(path="src/icon.py", extract="unknown", expect_count=1),
        ),
    )
    problems = base_docs.runtime_problems(cfg, rule)
    joined = "\n".join(problems)
    assert "未找到前端控件语义键清单" in joined
    assert "控件语义键须为 snake_case" in joined
    assert "未找到登记值" in joined
    assert "登记值形态须为" in joined


def test_base_docs_run_paths(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """`run`：未登记规则退 2 / 自检通过 / 问题超过 50 项时给出余数。"""
    cfg = config(tmp_path)
    assert base_docs.run(dataclasses.replace(cfg, base_rules=None), as_self_test=False) == 2
    assert base_docs.run(cfg, as_self_test=True) == 0
    rule = cfg.base_rules
    assert rule is not None
    many = "".join(f"本项目项目{index}\n" for index in range(51))
    write(tmp_path, {"docs/规范/many.md": many})
    assert base_docs.run(cfg, as_self_test=False) == 1
    assert "其余" in capsys.readouterr().out


# ---------- 文档一致性（docs-status） ----------

PLAN = "docs/项目/01_阶段/计划/01_计划_阶段.md"
TASK_DOMAIN = "docs/项目/01_阶段/任务/01_域"


def docs_cfg(tmp_path: Path):
    """造阶段夹具 + 配置。

    Args:
        tmp_path: 临时仓根。

    Returns:
        tuple[Config, Path]: （配置, 阶段目录）。
    """
    return config(tmp_path), status_stage(tmp_path)


def test_docs_hours_and_split_row(tmp_path: Path) -> None:
    """工时解析与表格行切分。"""
    assert docs_status.docs_hours("8h") == 8
    assert docs_status.docs_hours("—") is None
    assert docs_status.split_row("| a | b |") == ["a", "b"]
    assert docs_status.split_row("非表格行") is None
    assert docs_status.read_section("## 1. 甲\n\n内容\n\n## 2. 乙\n", r"^## 1") == "\n内容\n"
    assert docs_status.read_section("无小节", r"^## 1") == ""


def test_expand_reqs() -> None:
    """需求编号展开：区间 / 逆向区间不展开 / 单个。"""
    assert docs_status.expand_reqs("01-1 ~ 01-3") == ["01-1", "01-2", "01-3"]
    assert docs_status.expand_reqs("01-3 ~ 01-1") == ["01-3", "01-1"]
    assert docs_status.expand_reqs("01-1") == ["01-1"]


def test_check_date_branches() -> None:
    """H3：已完成须有日期；非已完成不得填日期。"""
    report = docs_status.Report("01_阶段")
    docs_status.check_date(report, "01_01", "已完成", "下周", "已完成")
    docs_status.check_date(report, "01_02", "未开始", "2026-10-20", "已完成")
    docs_status.check_date(report, "01_03", "已完成", "2026-10-20", "已完成")
    docs_status.check_date(report, "01_04", "未开始", "—", "已完成")
    assert len(report.hard) == 2


def test_docs_stage_skip_and_missing(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """阶段目录缺 需求/ 或 计划/ 即跳过；缺总览 / 缺计划各报硬规则。"""
    cfg, stage = docs_cfg(tmp_path)
    (stage / "计划").rename(stage / "计划_bak")
    assert docs_status.run(cfg, stage="", all_stages=False, strict=True) == 0
    assert "跳过：缺少" in capsys.readouterr().out

    (stage / "计划_bak").rename(stage / "计划")
    (stage / "需求/00_需求_总览.md").unlink()
    assert docs_status.run(cfg, stage="", all_stages=False, strict=True) == 1

    docs_cfg(tmp_path)
    write(tmp_path, {"docs/项目/01_阶段/需求/00_需求_总览.md": "| 编号 | 标题 |\n| --- | --- |\n| 01-1 | 甲 |\n"})
    (tmp_path / PLAN).unlink()
    assert docs_status.run(config(tmp_path), stage="", all_stages=False, strict=True) == 1


def test_docs_duplicate_req(tmp_path: Path) -> None:
    """需求编号跨域文档重复即报 H1。"""
    cfg, _stage = docs_cfg(tmp_path)
    write(tmp_path, {"docs/项目/01_阶段/需求/02_需求_乙.md": "## 1. 需求 01-1：乙\n\n说明文字\n"})
    assert docs_status.run(cfg, stage="", all_stages=False, strict=True) == 1


def test_docs_task_branches(tmp_path: Path) -> None:
    """任务侧：文档数异常 / 编号非两位 / 已含计划编号 / 计划编号重复 / 对应需求不存在。"""
    cfg, _stage = docs_cfg(tmp_path)
    write(tmp_path, {"docs/项目/01_阶段/任务/01_域/01_任务/99_副文档.md": "## 1. 任务信息\n"})
    assert docs_status.run(cfg, stage="", all_stages=False, strict=True) == 1

    (tmp_path / "docs/项目/01_阶段/任务/01_域/01_任务/99_副文档.md").unlink()
    write(
        tmp_path,
        {
            "docs/项目/01_阶段/任务/01_域/03_任务/03_任务.md": (
                "## 1. 任务信息\n\n| 项 | 值 |\n| --- | --- |\n| 编号 | abc |\n| 对应需求 | 01-1 |\n"
            )
        },
    )
    assert docs_status.run(cfg, stage="", all_stages=False, strict=True) == 1

    (tmp_path / "docs/项目/01_阶段/任务/01_域/03_任务/03_任务.md").unlink()
    write(
        tmp_path,
        {
            "docs/项目/01_阶段/任务/01_域/03_任务/03_任务.md": (
                "## 1. 任务信息\n\n| 项 | 值 |\n| --- | --- |\n| 编号 | 01-1-1 |\n| 对应需求 | 01-1 |\n"
            )
        },
    )
    assert docs_status.run(cfg, stage="", all_stages=False, strict=True) == 0

    write(
        tmp_path,
        {
            "docs/项目/01_阶段/任务/01_域/03_任务/03_任务.md": (
                "## 1. 任务信息\n\n| 项 | 值 |\n| --- | --- |\n| 编号 | 01 |\n| 对应需求 | 09-9 |\n"
            )
        },
    )
    assert docs_status.run(cfg, stage="", all_stages=False, strict=True) == 1


def test_docs_task_root_missing_and_aid_dir(tmp_path: Path) -> None:
    """任务根缺失 / 附属目录（设计 / 实施 / 测试）不计为任务。"""
    cfg, stage = docs_cfg(tmp_path)
    (stage / "任务/01_域/设计").mkdir()
    assert docs_status.run(cfg, stage="", all_stages=False, strict=True) == 0
    (stage / "任务/01_域/01_域.md").rename(stage / "任务/01_域/总览_bak.md")
    assert docs_status.run(cfg, stage="", all_stages=False, strict=True) == 0
    (stage / "任务").rename(stage / "任务_bak")
    assert docs_status.run(cfg, stage="", all_stages=False, strict=True) == 1


def test_docs_plan_branches(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """计划侧：同号两表 / 计划列出而任务缺失 / 任务未进计划 / 窗口搁置 / 甘特含已完成 / 数值不符。"""
    cfg, _stage = docs_cfg(tmp_path)
    text = (tmp_path / PLAN).read_text(encoding="utf-8")
    base = text

    doubled = base.replace(
        "| 01_02 | 乙 | 未开始 | 8h | — | 2026-10-21 ~ 2026-10-22 | — | minjian |\n",
        "| 01_02 | 乙 | 未开始 | 8h | — | 2026-10-21 ~ 2026-10-22 | — | minjian |\n"
        "| 01_01 | 甲 | 未开始 | 8h | — | 2026-10-21 ~ 2026-10-22 | — | minjian |\n",
    )
    (tmp_path / PLAN).write_text(doubled, encoding="utf-8")
    assert docs_status.run(cfg, stage="", all_stages=False, strict=True) == 1

    (tmp_path / PLAN).write_text(
        base.replace("| 01_02 | 乙 | 未开始 | 8h | — | 2026-10-21 ~ 2026-10-22 | — | minjian |\n", ""),
        encoding="utf-8",
    )
    assert docs_status.run(cfg, stage="", all_stages=False, strict=True) == 1

    (tmp_path / PLAN).write_text(
        base.replace("2026-10-21 ~ 2026-10-22", "搁置（待排期）"),
        encoding="utf-8",
    )
    assert docs_status.run(cfg, stage="", all_stages=False, strict=True) == 0

    (tmp_path / PLAN).write_text(base.replace("01_02 乙\n", "01_02 乙\n01_01 甲\n"), encoding="utf-8")
    assert docs_status.run(cfg, stage="", all_stages=False, strict=False) == 0

    (tmp_path / PLAN).write_text(
        base.replace("已完成 **8h**；剩余 **8h**，阶段合计 **16h**", "已完成 **9h**；剩余 **8h**，阶段合计 **17h**"),
        encoding="utf-8",
    )
    assert docs_status.run(cfg, stage="", all_stages=False, strict=False) == 0
    assert docs_status.run(cfg, stage="", all_stages=False, strict=True) == 1
    capsys.readouterr()


def test_docs_record_and_all_stages(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """已完成任务缺记录即 S3；`--all` 跳过目录不存在的阶段。"""
    cfg, stage = docs_cfg(tmp_path)
    (stage / "任务/01_域/01_任务/实施").rmdir()
    assert docs_status.run(cfg, stage="", all_stages=False, strict=False) == 0
    assert docs_status.run(cfg, stage="", all_stages=False, strict=True) == 1

    (stage / "任务/01_域/01_任务/实施").mkdir()
    (tmp_path / "docs/项目/02_空阶段").mkdir()
    assert docs_status.run(cfg, stage="", all_stages=True, strict=True) == 0
    assert "跳过：缺少" in capsys.readouterr().out
    assert docs_status.run(cfg, stage="02_不存在", all_stages=False, strict=True) == 0
    assert "目录不存在" in capsys.readouterr().out
