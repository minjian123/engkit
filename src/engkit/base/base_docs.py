"""基座完整性校验（`engkit base base`）。

校验六项：① 基座文件不含跨层相对链接；② 基座文件不含项目专属措辞残留；③ 清单元数据与磁盘一致；
④ 跨文档引用不得只写编号；⑤ 数据库设计登记与目录双向一致且状态一致；⑥ 登记面命名形态（登记目录 /
包名 / 脚本名 + 运行期登记值）一律 snake_case。规则数据全部来自 `[rules.base]`。
"""

from __future__ import annotations

import json
import re
import tempfile
from pathlib import Path
from typing import Any

from ..config import BaseRules, Config, DesignRegistryRules, NamingRules, RuntimeIdentifierRule
from ..console import out
from .rules import RulesError, read_text, require

MIN_KEYS = ("manifest", "base_dirs", "cross_layer")
"""未登记时报错给出的最小键指引。"""

LINK_RE = re.compile(r"\]\(([^)\s]+)\)")
"""Markdown 相对链接。"""

CODE_RE = re.compile(r"`[^`]*`")
"""行内代码（内容不参与校验）。"""

SECTION_REF_RE = re.compile(r"《(.+?)》[）)，、\s]*(?:第\s*\d+(?:\.\d+)?\s*[章节]|\d+(?:\.\d+)?\s*[章节])")
"""跨文档编号引用（必须有「节 / 章」字样才判定）。"""

STATUS_RE = re.compile(r"^\|\s*状态\s*\|\s*([^|]+?)\s*\|")
"""表文件「状态」行。"""

DIR_IDENT_RE = re.compile(r"^[a-z][a-z0-9_]*$")
"""目录 / 业务域标识形态。"""

SCRIPT_STEM_RE = re.compile(r"^[a-z0-9][a-z0-9_]*$")
"""工具脚本文件名主体形态。"""

SNAKE_PATTERN = r"^[a-z][a-z0-9_]*$"
"""登记值形态。"""

WIDGET_LITERAL_RE = re.compile(r'widget="([^"]+)"')
"""后端 `widget="…"` 字面量。"""

FIELD_WIDGETS_BLOCK_RE = re.compile(r"export const FIELD_WIDGETS[^=]*=\s*\[(.*?)\]", re.DOTALL)
"""前端 `FIELD_WIDGETS` 数组块。"""

TS_WIDGET_RE = re.compile(r"'([^']+)'")
"""前端块内单引号字符串。"""

CODE_FIELD_PATTERN_RE = re.compile(r'code: str = Field\(\s*min_length=1,\s*max_length=64,\s*pattern=r"([^"]+)"')
"""业务码 / 动作码 `pattern`。"""

ICON_CODE_PATTERN_RE = re.compile(r'_ICON_CODE_RE = re\.compile\(r"([^"]+)"\)')
"""自建图标码正则。"""

MANIFEST_BLOCK_RE = re.compile(r"```text\n(.*?)```", re.S)
"""清单文本块。"""

MANIFEST_LINE_RE = re.compile(r"^([│\s]*)(?:├──|└──)\s+(.+?)\s*$")
"""清单目录树行。"""

NAMING_CASES = 12
"""命名自检用例数（登记面 6 个 + 运行期登记值 6 个）。"""


def strip_code(line: str) -> str:
    """把行内代码替换为等长空白（避免代码里的举例被误判）。

    Args:
        line: 行文本。

    Returns:
        str: 处理后的行。
    """
    return CODE_RE.sub(lambda match: " " * len(match.group(0)), line)


def _md_files(base: Path) -> list[Path]:
    """列出目录下全部 `.md` 文件（字典序）。

    Args:
        base: 目录。

    Returns:
        list[Path]: 文件路径。
    """
    if not base.exists():
        return []
    return sorted(path for path in base.rglob("*.md") if path.is_file())


def check_cross_layer(cfg: Config, rule: BaseRules) -> tuple[int, list[str]]:
    """① 基座文件不得相对链接到跨层（平台专属）文档。

    Args:
        cfg: 仓配置。
        rule: 规则段。

    Returns:
        tuple[int, list[str]]: （命中数, 问题清单）。
    """
    problems: list[str] = []
    for directory in rule.base_dirs:
        for path in _md_files(cfg.root / directory):
            relative = path.relative_to(cfg.root).as_posix()
            for number, line in enumerate(read_text(path).splitlines(), start=1):
                for match in LINK_RE.finditer(strip_code(line)):
                    target = match.group(1).strip().lstrip("?")
                    if target.startswith(("http", "mailto")):
                        continue
                    if not target.split("#")[0].endswith(tuple(rule.link_suffixes)):
                        continue
                    resolved = (path.parent / target.split("#")[0]).resolve()
                    try:
                        joined = resolved.relative_to(cfg.root).as_posix()
                    except ValueError:
                        continue
                    if any(joined == item.rstrip("/") or joined.startswith(item) for item in rule.cross_layer):
                        problems.append(f"[跨层引用] {relative}:{number} -> {target}")
    return len(problems), problems


def check_wording(cfg: Config, rule: BaseRules) -> tuple[int, list[str]]:
    """② 基座文件不得含项目专属措辞残留。

    Args:
        cfg: 仓配置。
        rule: 规则段。

    Returns:
        tuple[int, list[str]]: （命中数, 问题清单）。
    """
    problems: list[str] = []
    for directory in rule.base_dirs:
        for path in _md_files(cfg.root / directory):
            relative = path.relative_to(cfg.root).as_posix()
            for number, line in enumerate(read_text(path).splitlines(), start=1):
                for word in rule.bad_wording:
                    if word in line:
                        problems.append(f"[措辞残留] {relative}:{number} 含「{word}」")
    return len(problems), problems


def manifest_files(cfg: Config, rule: BaseRules) -> list[str]:
    """解析清单文本块，返回登记的基座文件相对路径。

    Args:
        cfg: 仓配置。
        rule: 规则段。

    Returns:
        list[str]: 相对仓根的路径清单。
    """
    manifest = cfg.root / rule.manifest
    if not manifest.is_file():
        return []
    match = MANIFEST_BLOCK_RE.search(read_text(manifest))
    if not match:
        return []
    stack: list[str] = []
    entries: list[str] = []
    for line in match.group(1).splitlines():
        if not line.strip():
            continue
        parsed = MANIFEST_LINE_RE.match(line)
        if not parsed:
            continue
        depth = len(parsed.group(1)) // 4
        name = parsed.group(2).split("#")[0].strip()
        stack = stack[:depth]
        if name.endswith("/"):
            stack.append(name.rstrip("/"))
        else:
            entries.append(f"{cfg.docs.root}/" + "/".join([*stack, name]))
    return entries


def check_manifest(cfg: Config, rule: BaseRules) -> tuple[int, int, int, list[str]]:
    """③ 清单登记与磁盘一致（登记文件均存在 + 数量一致）。

    Args:
        cfg: 仓配置。
        rule: 规则段。

    Returns:
        tuple[int, int, int, list[str]]: （登记数, 磁盘数, 缺失数, 问题清单）。
    """
    entries = [item for item in manifest_files(cfg, rule) if not item.startswith(tuple(rule.local_only))]
    missing = [item for item in entries if not (cfg.root / item).exists()]
    disk = sum(len([p for p in (cfg.root / directory).rglob("*") if p.is_file()]) for directory in rule.base_dirs)
    problems = [f"[清单缺文件] {rule.manifest} 登记但磁盘不存在：{item}" for item in missing]
    if disk != len(entries):
        problems.append(f"[清单数量不符] 清单登记 {len(entries)} 个，磁盘 {disk} 个（新增 / 删除基座文件后须同步清单）")
    return len(entries), disk, len(missing), problems


def check_section_refs(cfg: Config, rule: BaseRules) -> tuple[int, list[str]]:
    """④ 跨文档引用若只写编号（第 N 节 / N.M 节 / N 章）即报警。

    Args:
        cfg: 仓配置。
        rule: 规则段。

    Returns:
        tuple[int, list[str]]: （命中数, 问题清单）。
    """
    problems: list[str] = []
    root = cfg.root / (rule.section_ref_root or cfg.docs.root)
    for path in _md_files(root):
        relative = path.relative_to(cfg.root).as_posix()
        stem = path.stem
        for number, line in enumerate(read_text(path).splitlines(), start=1):
            for match in SECTION_REF_RE.finditer(strip_code(line)):
                raw = match.group(1)
                name = raw.split("]")[0].lstrip("[") if "]" in raw else raw
                name = name.strip()
                if name == stem or name.replace(" ", "") == stem or stem in name or name in stem:
                    continue
                problems.append(f"[编号引用] {relative}:{number} -> {match.group(0)[:60]}（跨文档引用请写章节名）")
    return len(problems), problems


def parse_design_overview(text: str, rule: DesignRegistryRules) -> tuple[dict[str, str], dict[str, str]]:
    """解析总览登记表。

    Args:
        text: 总览文档全文。
        rule: 设计登记规则。

    Returns:
        tuple[dict[str, str], dict[str, str]]: （表登记 {表名: 状态}, 方言登记 {文件名: 状态}）。
    """
    tables: dict[str, str] = {}
    dialects: dict[str, str] = {}
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped.startswith("|"):
            continue
        cells = [cell.strip() for cell in stripped.strip("|").split("|")]
        if len(cells) < 3:
            continue
        if f"{rule.table_subdir}/" in cells[1]:
            tables[cells[0].strip("`")] = cells[-1]
        elif f"{rule.dialect_subdir}/" in cells[1]:
            match = re.search(rf"\({rule.dialect_subdir}/([^)]+)\)", cells[1])
            if match:
                dialects[match.group(1)] = cells[-1]
    return tables, dialects


def status_head(value: str) -> str:
    """取状态首值（枚举；括号补充不计）。

    Args:
        value: 状态单元格。

    Returns:
        str: 首值。
    """
    return re.split(r"[（(]", value.strip())[0].strip()


def diff_design_registry(
    overview_text: str,
    table_files: set[str],
    dialect_files: set[str],
    table_status: dict[str, str],
    rule: DesignRegistryRules,
) -> list[str]:
    """纯函数：比对总览登记与目录 / 表文件状态。

    Args:
        overview_text: 总览文档全文。
        table_files: 表文件名集合（含 `.md`）。
        dialect_files: 方言特性文件名集合（含 `.md`）。
        table_status: 表名 → 状态值。
        rule: 设计登记规则。

    Returns:
        list[str]: 问题清单。
    """
    tables, dialects = parse_design_overview(overview_text, rule)
    found: list[str] = []
    for name, status in sorted(tables.items()):
        filename = f"{name}.md"
        if filename not in table_files:
            found.append(f"[表文件缺失] 总览登记 `{name}` 但 `{rule.table_subdir}/{filename}` 不存在")
            continue
        actual = table_status.get(name)
        if actual is None:
            found.append(f"[表文件缺状态] `{rule.table_subdir}/{filename}` 无「状态」行")
            continue
        if status_head(actual) != status_head(status):
            found.append(f"[状态不一致] `{name}`：总览登记「{status_head(status)}」/ 表文件「{status_head(actual)}」")
        if status_head(status) not in rule.status_values:
            allowed = " / ".join(rule.status_values)
            found.append(f"[状态取值非法] `{name}`：{status_head(status)}（须为 {allowed}）")
    for filename in sorted(table_files):
        if filename[:-3] not in tables:
            found.append(f"[未登记表文件] `{rule.table_subdir}/{filename}` 未在总览「已设计数据表登记」登记")
    for filename in sorted(dialects):
        if filename not in dialect_files:
            found.append(f"[方言文件缺失] 总览登记 `{filename}` 但 `{rule.dialect_subdir}/{filename}` 不存在")
    for filename in sorted(dialect_files):
        if filename not in dialects:
            found.append(f"[未登记方言文件] `{rule.dialect_subdir}/{filename}` 未在总览「方言特性登记」登记")
    return found


def read_table_status(path: Path) -> str | None:
    """读表文件「状态」行的取值。

    Args:
        path: 表文件路径。

    Returns:
        str | None: 状态值；无该行返回 None。
    """
    for line in read_text(path).splitlines():
        match = STATUS_RE.match(strip_code(line.strip()))
        if match:
            return match.group(1).strip()
    return None


def check_design_registry(cfg: Config, rule: BaseRules) -> tuple[str, list[str]]:
    """⑤ 数据库设计登记一致。

    Args:
        cfg: 仓配置。
        rule: 规则段。

    Returns:
        tuple[str, list[str]]: （结论行, 问题清单）。
    """
    design = rule.design
    if design is None:
        return "5. 数据库设计登记一致：跳过（未登记 [rules.base.design]）", []
    overview = cfg.root / design.overview
    if not overview.is_file():
        return f"5. 数据库设计登记一致：跳过（{design.overview} 不存在）", []
    table_dir = cfg.root / design.dir / design.table_subdir
    dialect_dir = cfg.root / design.dir / design.dialect_subdir
    table_files = {item.name for item in table_dir.glob("*.md")} if table_dir.is_dir() else set()
    dialect_files = {item.name for item in dialect_dir.glob("*.md")} if dialect_dir.is_dir() else set()
    status = {name[:-3]: read_table_status(table_dir / name) or "" for name in sorted(table_files)}
    found = diff_design_registry(read_text(overview), table_files, dialect_files, status, design)
    line = (
        f"5. 数据库设计登记一致：{len(found)} 处问题（表文件 {len(table_files)} 个 / 方言特性 {len(dialect_files)} 个）"
    )
    return line, found


def name_problems(cfg: Config, naming: NamingRules) -> list[str]:
    """⑥-1 登记面命名形态（目录 / 包名 / 脚本名）。

    Args:
        cfg: 仓配置。
        naming: 命名规则。

    Returns:
        list[str]: 问题清单。
    """
    found: list[str] = []
    tool_root = cfg.root / naming.tool_root if naming.tool_root else None
    if tool_root is not None and tool_root.is_dir():
        for child in sorted(tool_root.iterdir()):
            if child.is_dir() and not DIR_IDENT_RE.match(child.name):
                found.append(f"[命名] 工具目录名须为 snake_case：{naming.tool_root}/{child.name}")
        for path in sorted(tool_root.rglob("*")):
            if not path.is_file() or path.suffix not in (".py", ".sh"):
                continue
            if not all(ord(char) < 128 for char in path.stem):
                continue
            if not SCRIPT_STEM_RE.match(path.stem):
                found.append(f"[命名] 工具脚本名须为 snake_case：{path.relative_to(cfg.root).as_posix()}")
    for package_root in naming.package_roots:
        base = cfg.root / package_root
        if not base.is_dir():
            continue
        for child in sorted(base.iterdir()):
            if not child.is_dir():
                continue
            if not DIR_IDENT_RE.match(child.name):
                found.append(f"[命名] 前端包目录名须为 snake_case：{package_root}/{child.name}")
            package_name = _package_name(child / "package.json")
            if package_name and "-" in package_name:
                found.append(f"[命名] 包名须为 snake_case（不含连字符）：{package_name}（{package_root}/{child.name}）")
    versions_root = cfg.root / naming.versions_root if naming.versions_root else None
    if versions_root is not None and versions_root.is_dir():
        for child in sorted(versions_root.iterdir()):
            if child.is_dir() and not DIR_IDENT_RE.match(child.name):
                found.append(f"[命名] 清单目录名须为 snake_case：{naming.versions_root}/{child.name}")
    return found


def _package_name(manifest: Path) -> str:
    """读 `package.json` 的 `name` 字段（缺失 / 非法返回空串）。

    Args:
        manifest: 清单路径。

    Returns:
        str: 包名。
    """
    try:
        data = json.loads(read_text(manifest))
    except (OSError, ValueError):
        return ""
    name = data.get("name") if isinstance(data, dict) else ""
    return name.strip() if isinstance(name, str) else ""


def runtime_problems(cfg: Config, rule: BaseRules) -> list[str]:
    """⑥-2 运行期登记值形态（控件语义键 / 业务码 / 图标码）。

    Args:
        cfg: 仓配置。
        rule: 规则段。

    Returns:
        list[str]: 问题清单。
    """
    found: list[str] = []
    for item in rule.runtime:
        path = cfg.root / item.path
        if not path.is_file():
            continue
        found.extend(_runtime_values(item, read_text(path)))
    return found


def _runtime_values(item: RuntimeIdentifierRule, text: str) -> list[str]:
    """按提取方式取出登记值并校验形态。

    Args:
        item: 运行期登记值规则。
        text: 来源文件全文。

    Returns:
        list[str]: 问题清单。
    """
    found: list[str] = []
    if item.extract == "ts_widgets":
        block = FIELD_WIDGETS_BLOCK_RE.search(text)
        if block is None:
            return [f"[命名] 未找到前端控件语义键清单 FIELD_WIDGETS：{item.path}"]
        literals = TS_WIDGET_RE.findall(block.group(1))
        for literal in literals:
            if not DIR_IDENT_RE.match(literal):
                found.append(f"[命名] 控件语义键须为 snake_case：{item.path} '{literal}'")
        return found
    pattern = {
        "widget_literal": WIDGET_LITERAL_RE,
        "code_pattern": CODE_FIELD_PATTERN_RE,
        "icon_pattern": ICON_CODE_PATTERN_RE,
    }.get(item.extract)
    if pattern is None:
        return found
    values = pattern.findall(text)
    if len(values) < item.expect_count:
        found.append(f"[命名] 未找到登记值（期望 ≥{item.expect_count} 处）：{item.path}")
    for value in values:
        if item.extract == "widget_literal":
            if not DIR_IDENT_RE.match(value):
                found.append(f'[命名] 控件语义键须为 snake_case：{item.path} widget="{value}"')
        elif value != SNAKE_PATTERN:
            found.append(f"[命名] 登记值形态须为 {SNAKE_PATTERN}：{item.path} → {value}")
    return found


def check_naming(cfg: Config, rule: BaseRules) -> tuple[int, list[str]]:
    """⑥ 登记面命名形态（登记目录 / 包名 / 脚本名 + 运行期登记值）。

    Args:
        cfg: 仓配置。
        rule: 规则段。

    Returns:
        tuple[int, list[str]]: （命中数, 问题清单）。
    """
    found = (name_problems(cfg, rule.naming) if rule.naming is not None else []) + runtime_problems(cfg, rule)
    return len(found), found


def collect(cfg: Config, rule: BaseRules) -> tuple[list[str], list[str]]:
    """执行全部六项，返回（输出行, 问题清单）。

    Args:
        cfg: 仓配置。
        rule: 规则段。

    Returns:
        tuple[list[str], list[str]]: （各步骤结论行, 问题清单）。
    """
    lines: list[str] = []
    problems: list[str] = []
    cross, cross_problems = check_cross_layer(cfg, rule)
    lines.append(f"1. 基座跨层引用：{cross} 处（应为 0，跨层一律写「平台《文档名》」）")
    wording, wording_problems = check_wording(cfg, rule)
    lines.append(f"2. 项目专属措辞残留：{wording} 处（应为 0）")
    entries, disk, missing, manifest_problems = check_manifest(cfg, rule)
    lines.append(f"3. 清单一致：登记 {entries} 个，磁盘 {disk} 个，缺失 {missing} 个")
    refs, ref_problems = check_section_refs(cfg, rule)
    lines.append(f"4. 跨文档编号引用：{refs} 处（应为 0，跨文档引用优先写章节名）")
    design_line, design_problems = check_design_registry(cfg, rule)
    lines.append(design_line)
    naming, naming_problems_found = check_naming(cfg, rule)
    lines.append(
        f"6. 登记面命名形态：{naming} 处（应为 0，登记目录 / 包名 / 脚本名与控件键 / 业务动作码 / "
        "自建图标码一律 snake_case）"
    )
    problems.extend(cross_problems + wording_problems + manifest_problems + ref_problems)
    problems.extend(f"[设计登记] {item}" for item in design_problems)
    problems.extend(naming_problems_found)
    return lines, problems


def self_test() -> int:
    """自检：设计登记 4 例 + 命名 12 例（正常 + 反例）。

    Returns:
        int: 退出码（0 通过 / 1 失败）。
    """
    overview = (
        "\n| 表 | 数据表文件 | 所属库 | 模块 | 状态 |\n| --- | --- | --- | --- | --- |\n"
        "| sys_tenant | [sys_tenant.md](数据表设计/sys_tenant.md) | 平台库 | 26-租户管理 | 已落库 |\n\n"
        "| 库 | 方言特性文件 | 适用版本 | 应用侧驱动 | 状态 |\n| --- | --- | --- | --- | --- |\n"
        "| MySQL | [01_MySQL.md](方言特性/01_MySQL.md) | 8.x | `aiomysql` | 已实测 |\n"
    )
    design = DesignRegistryRules(
        dir="docs/数据库设计",
        overview="docs/数据库设计/01_总览.md",
        table_subdir="数据表设计",
        dialect_subdir="方言特性",
        status_values=("已设计", "待落库", "已落库", "已废弃"),
    )
    cases: list[tuple[str, set[str], set[str], dict[str, str], int]] = [
        ("正常：登记与目录 / 状态一致", {"sys_tenant.md"}, {"01_MySQL.md"}, {"sys_tenant": "已落库（平台链）"}, 0),
        ("反例 A：目录多出未登记表文件", {"sys_tenant.md", "sys_module.md"}, {"01_MySQL.md"}, {}, 1),
        ("反例 B：总览状态与表文件状态不一致", {"sys_tenant.md"}, {"01_MySQL.md"}, {"sys_tenant": "待落库"}, 1),
        ("反例 C：方言特性目录多出未登记文件", {"sys_tenant.md"}, {"01_MySQL.md", "02_PG.md"}, {}, 1),
    ]
    bad = 0
    for title, tables, dialects, status, expect in cases:
        merged = {"sys_tenant": "已落库"}
        merged.update(status)
        found = diff_design_registry(overview, tables, dialects, merged, design)
        flag = "OK" if len(found) == expect else "FAIL"
        bad += flag == "FAIL"
        out(f"  [{flag}] {title}：期望 {expect} 项，实得 {len(found)} 项")
        for item in found:
            out(f"        {item}")
    bad += naming_self_test()
    total = len(cases) + NAMING_CASES
    out(
        f"\n[base self-test] {'通过' if not bad else '不通过'}：第 4、5、6 项断言用例共 {total} 个"
        f"（命名 {NAMING_CASES} 个）"
    )
    return 1 if bad else 0


def _build_naming_tree(root: Path, *, tool_dir: str, script: str, package_dir: str, package_name: str) -> None:
    """搭一棵最小仓库树（命名自检夹具）。

    Args:
        root: 临时仓库根。
        tool_dir: 工具目录名。
        script: 脚本文件名。
        package_dir: 前端包目录名。
        package_name: 包名。
    """
    tool = root / "tools" / tool_dir
    tool.mkdir(parents=True, exist_ok=True)
    (tool / script).write_text("", encoding="utf-8")
    package = root / "ui" / package_dir
    package.mkdir(parents=True, exist_ok=True)
    (package / "package.json").write_text(json.dumps({"name": package_name}), encoding="utf-8")
    (root / "versions" / "user").mkdir(parents=True, exist_ok=True)


def _build_runtime_tree(root: Path, *, widget: str, ts_widgets: list[str], code: str, icon: str) -> None:
    """搭运行期登记值自检夹具。

    Args:
        root: 临时仓库根。
        widget: 后端控件键。
        ts_widgets: 前端控件键清单。
        code: 业务码正则。
        icon: 图标码正则。
    """
    files = {
        "runtime/fieldtype.py": f'SimpleFieldType(key="x", widget="{widget}")\n',
        "runtime/render.ts": "export const FIELD_WIDGETS: readonly string[] = [\n"
        + "".join(f"  '{item}',\n" for item in ts_widgets)
        + "]\n",
        "runtime/schema.py": (
            "    code: str = Field(\n        min_length=1,\n        max_length=64,\n"
            f'        pattern=r"{code}",\n    )\n'
        )
        + (
            "    code: str = Field(\n        min_length=1,\n        max_length=64,\n"
            f'        pattern=r"{code}",\n    )\n'
        ),
        "runtime/icon.py": f'_ICON_CODE_RE = re.compile(r"{icon}")\n',
    }
    for rel, content in files.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")


def naming_self_test() -> int:
    """命名自检：登记面 6 例 + 运行期登记值 5 例。

    Returns:
        int: 失败用例数。
    """
    naming = NamingRules(tool_root="tools", package_roots=("ui",), versions_root="versions")
    ok = {"tool_dir": "base_check", "script": "check_base.py", "package_dir": "ui_ep", "package_name": "@app/ui_ep"}
    tree_cases: list[tuple[str, dict[str, str], int]] = [
        ("正例：全 snake_case 通过", ok, 0),
        ("反例 A：工具目录连字符", {**ok, "tool_dir": "bad-name"}, 1),
        ("反例 B：工具脚本连字符", {**ok, "script": "check-bad.py"}, 1),
        ("反例 C：前端包目录与包名连字符", {**ok, "package_dir": "bad-name", "package_name": "@app/bad-name"}, 2),
        ("正例：中文名脚本豁免", {**ok, "script": "安装-重启.py"}, 0),
        ("反例 D：清单目录连字符", {**ok, "tool_dir": "Bad-Dir"}, 1),
    ]
    runtime_ok = {
        "widget": "dict_select",
        "ts_widgets": ["dict_select", "i18n_name"],
        "code": SNAKE_PATTERN,
        "icon": SNAKE_PATTERN,
    }
    runtime_cases: list[tuple[str, dict[str, Any], int]] = [
        ("正例：控件键 / 码值 / 图标码全 snake_case", runtime_ok, 0),
        ("反例 E：后端控件键连字符", {**runtime_ok, "widget": "dict-select"}, 1),
        ("反例 F：前端控件键连字符", {**runtime_ok, "ts_widgets": ["dict-select", "i18n-name"]}, 2),
        ("反例 G：业务动作码允许连字符", {**runtime_ok, "code": r"^[a-z][a-z0-9-]*$"}, 2),
        ("反例 H：自建图标码正则 kebab", {**runtime_ok, "icon": r"^[a-z][a-z0-9]*(?:-[a-z0-9]+)*$"}, 1),
    ]
    bad = 0
    for title, spec, expect in tree_cases:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _build_naming_tree(root, **spec)  # type: ignore[arg-type]
            found = name_problems(_mini_cfg(root), naming)
        bad += _report(title, found, expect)
    runtime_rule = BaseRules(
        manifest="docs/清单.md",
        base_dirs=("docs",),
        cross_layer=(),
        runtime=(
            RuntimeIdentifierRule(path="runtime/fieldtype.py", extract="widget_literal", expect_count=1),
            RuntimeIdentifierRule(path="runtime/render.ts", extract="ts_widgets"),
            RuntimeIdentifierRule(path="runtime/schema.py", extract="code_pattern", expect_count=2),
            RuntimeIdentifierRule(path="runtime/icon.py", extract="icon_pattern"),
        ),
    )
    for title, spec, expect in runtime_cases:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _build_runtime_tree(root, **spec)  # type: ignore[arg-type]
            found = runtime_problems(_mini_cfg(root), runtime_rule)
        bad += _report(title, found, expect)
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        found = runtime_problems(_mini_cfg(root), runtime_rule)
        bad += _report("反例 I：登记源文件缺失即静默跳过（不误报）", found, 0)
    return bad


def _mini_cfg(root: Path) -> Config:
    """造一份最小配置（自检夹具）。

    Args:
        root: 临时仓库根。

    Returns:
        Config: 配置对象。
    """
    from .. import config as config_module

    return config_module.parse({"docs": {"root": "docs"}}, root / "engkit.toml")


def _report(title: str, found: list[str], expect: int) -> int:
    """打印单条自检结论。

    Args:
        title: 用例标题。
        found: 实得问题清单。
        expect: 期望问题数。

    Returns:
        int: 失败计数（0 / 1）。
    """
    flag = "OK" if len(found) == expect else "FAIL"
    out(f"  [{flag}] {title}：期望 {expect} 项，实得 {len(found)} 项")
    for item in found:
        out(f"        {item}")
    return 0 if flag == "OK" else 1


def run(cfg: Config, *, as_self_test: bool) -> int:
    """执行基座完整性校验。

    Args:
        cfg: 仓配置。
        as_self_test: 是否只跑内置自检。

    Returns:
        int: 退出码（0 通过 / 1 有问题 / 2 规则缺失）。
    """
    if as_self_test:
        return self_test()
    try:
        rule = require(cfg.base_rules, "base", MIN_KEYS)
    except RulesError as exc:
        out(f"[base-integrity] {exc}")
        return 2
    lines, problems = collect(cfg, rule)
    for line in lines:
        out(line)
    if problems:
        out(f"\n[base-integrity] 不通过：{len(problems)} 项")
        for item in problems[:50]:
            out("  " + item)
        if len(problems) > 50:
            out(f"  ... 其余 {len(problems) - 50} 项")
        return 1
    out(
        "\n[base-integrity] 通过：无跨层引用、无措辞残留、清单与磁盘一致、跨文档引用均写章节名、"
        "数据库设计登记一致、登记面命名均为 snake_case。"
    )
    return 0
