"""系统字段（`form_key` / `data_source_key` / `kind`）校验（`engkit base system-fields`）。

校验四项：清单 ↔ 数据表文件双向一致且 `form_key` 跨表全局唯一；`data_source_key` 回填闭合
（非空且落在数据源清单）；两份清单逐条**显式声明** `kind` 且取值合法。
清单模块、常量名、枚举名与表文件目录全部来自 `[rules.system_fields]`。
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

from ..config import Config, RegistrySpec, SystemFieldsRules
from ..console import out
from .rules import RulesError, read_text, require

MIN_KEYS = ("catalog", "datasource", "table_dir")
"""未登记时报错给出的最小键指引。"""

KEY_RE = re.compile(r"^[a-z][a-z0-9_]*$")
"""登记标识形态（口径谓词，非仓数据）。"""


def _unwrap_collection(value: ast.expr | None) -> ast.Tuple | ast.List | None:
    """拆掉集合类包装，取内层元素序列节点（`Stable((...))` → `(...)`）。

    构建期清单可能落插入序集合类，护栏需同时支持裸字面量与集合类包装两种形态。

    Args:
        value: 常量赋值右侧的 AST 节点。

    Returns:
        ast.Tuple | ast.List | None: 元素序列节点；无法识别时返回 None。
    """
    if isinstance(value, ast.Tuple | ast.List):
        return value
    if not isinstance(value, ast.Call):
        return None
    for arg in value.args:
        if isinstance(arg, ast.Tuple | ast.List):
            return arg
    return None


def _sequence(path: Path, const_name: str) -> list[ast.expr]:
    """取模块内某常量赋值的元素序列节点。

    Args:
        path: 模块文件。
        const_name: 常量名。

    Returns:
        list[ast.expr]: 元素节点清单（文件缺失或目标不存在时为空）。
    """
    if not path.is_file():
        return []
    tree = ast.parse(read_text(path))
    for node in tree.body:
        value: ast.expr | None = None
        if isinstance(node, ast.Assign):
            targets = [t.id for t in node.targets if isinstance(t, ast.Name)]
            if const_name in targets:
                value = node.value
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name) and node.target.id == const_name:
            value = node.value
        sequence = _unwrap_collection(value)
        if sequence is not None:
            return list(sequence.elts)
    return []


def parse_records(cfg: Config, spec: RegistrySpec) -> list[dict[str, str]]:
    """按 AST 解析清单常量的关键字字面量参数。

    Args:
        cfg: 仓配置。
        spec: 清单定位。

    Returns:
        list[dict[str, str]]: 每条记录的字符串关键字参数（非字面量参数跳过）。
    """
    records: list[dict[str, str]] = []
    for item in _sequence(cfg.root / spec.path, spec.const):
        if not isinstance(item, ast.Call):
            continue
        record: dict[str, str] = {}
        for keyword in item.keywords:
            if keyword.arg and isinstance(keyword.value, ast.Constant) and isinstance(keyword.value.value, str):
                record[keyword.arg] = keyword.value.value
        records.append(record)
    return records


def parse_enum_values(cfg: Config, spec: RegistrySpec) -> dict[str, str]:
    """解析 `<enum>(StrEnum)` 的成员名 → 取值映射。

    Args:
        cfg: 仓配置。
        spec: 清单定位（`enum` 为空返回空映射）。

    Returns:
        dict[str, str]: 成员名 → 取值。
    """
    path = cfg.root / spec.path
    if not spec.enum or not path.is_file():
        return {}
    tree = ast.parse(read_text(path))
    values: dict[str, str] = {}
    for node in tree.body:
        if not isinstance(node, ast.ClassDef) or node.name != spec.enum:
            continue
        for item in node.body:
            if not (isinstance(item, ast.Assign) and isinstance(item.value, ast.Constant)):
                continue
            if not isinstance(item.value.value, str):
                continue
            for target in item.targets:
                if isinstance(target, ast.Name):
                    values[target.id] = item.value.value
    return values


def resolve_kind_node(node: ast.expr, enum_name: str, enum_values: dict[str, str]) -> str | None:
    """解析 `kind` 取值节点：字符串字面量直取，`<Enum>.<MEMBER>.value` 经枚举映射解析。

    Args:
        node: 取值节点。
        enum_name: 枚举类名。
        enum_values: 成员名 → 取值。

    Returns:
        str | None: 解析出的取值；无法解析返回 None。
    """
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if (
        isinstance(node, ast.Attribute)
        and node.attr == "value"
        and isinstance(node.value, ast.Attribute)
        and isinstance(node.value.value, ast.Name)
        and node.value.value.id == enum_name
    ):
        return enum_values.get(node.value.attr)
    return None


def parse_kind_declarations(cfg: Config, spec: RegistrySpec) -> list[tuple[str, bool, str | None]]:
    """解析清单每条记录的 `kind` 显式声明（键存在性 + 取值）。

    Args:
        cfg: 仓配置。
        spec: 清单定位。

    Returns:
        list[tuple[str, bool, str | None]]: （定位键, 是否出现 `kind`, 解析出的取值）。
    """
    enum_values = parse_enum_values(cfg, spec)
    declarations: list[tuple[str, bool, str | None]] = []
    for item in _sequence(cfg.root / spec.path, spec.const):
        if not isinstance(item, ast.Call):
            continue
        locator = ""
        declared = False
        kind_value: str | None = None
        for keyword in item.keywords:
            if keyword.arg == spec.key_field and isinstance(keyword.value, ast.Constant):
                if isinstance(keyword.value.value, str):
                    locator = keyword.value.value
            elif keyword.arg == spec.kind_field:
                declared = True
                kind_value = resolve_kind_node(keyword.value, spec.enum, enum_values)
        declarations.append((locator or "?", declared, kind_value))
    return declarations


def diff_explicit_kind(declarations: list[tuple[str, bool, str | None]], valid_kinds: set[str]) -> list[str]:
    """纯函数：断言清单逐条显式声明 `kind` 且取值合法。

    Args:
        declarations: （定位键, 是否声明, 取值）列表。
        valid_kinds: 合法取值集合。

    Returns:
        list[str]: 问题描述（空 = 通过）。
    """
    issues: list[str] = []
    for locator, declared, kind_value in declarations:
        if not declared:
            issues.append(f"未显式声明 kind（现依赖数据类默认值）：{locator!r}")
        elif kind_value is None or kind_value not in valid_kinds:
            allowed = " / ".join(sorted(valid_kinds))
            issues.append(f"kind 取值非法或无法解析：{locator!r} → {kind_value!r}（须 {allowed}）")
    return issues


def row_pattern(field: str) -> re.Pattern[str]:
    """表文件中「字段行」的匹配正则。

    Args:
        field: 字段名（如 `form_key`）。

    Returns:
        re.Pattern[str]: 行首匹配正则。
    """
    return re.compile(rf"^\|\s*`{re.escape(field)}`\s*\|", re.MULTILINE)


def diff_form_catalog(
    catalog: list[tuple[str, str]],
    table_texts: dict[str, str],
    *,
    row_form_key: str,
    row_parent_id: str,
    table_dir: str,
) -> list[str]:
    """纯函数：比对清单与表文件文本（双向一致 + 全局唯一 + 必备字段行）。

    Args:
        catalog: （form_key, table）列表。
        table_texts: {表名: 表文件文本}。
        row_form_key: 表单键行名。
        row_parent_id: 父级行名。
        table_dir: 表文件目录名（文案用）。

    Returns:
        list[str]: 问题描述（空 = 通过）。
    """
    issues: list[str] = []
    form_row, parent_row = row_pattern(row_form_key), row_pattern(row_parent_id)
    seen_keys: set[str] = set()
    seen_tables: set[str] = set()
    for form_key, table in catalog:
        if not KEY_RE.match(form_key):
            issues.append(f"{row_form_key} 格式非法：{form_key!r}（须 ^[a-z][a-z0-9_]*$）")
        if form_key in seen_keys:
            issues.append(f"{row_form_key} 跨表重复：{form_key!r}（须全局唯一）")
        seen_keys.add(form_key)
        if table in seen_tables:
            issues.append(f"表名重复登记：{table!r}")
        seen_tables.add(table)
        text = table_texts.get(table)
        if text is None:
            issues.append(f"清单表缺表文件：{table}.md（未在 {table_dir} 找到）")
            continue
        if not form_row.search(text):
            issues.append(f"表文件缺 {row_form_key} 行：{table}.md")
        if not parent_row.search(text):
            issues.append(f"字典表文件缺 {row_parent_id} 行：{table}.md")
    for table, text in sorted(table_texts.items()):
        if form_row.search(text) and table not in seen_tables:
            issues.append(f"表文件有 {row_form_key} 行但未登记清单：{table}.md")
    return issues


def diff_data_source_closure(pairs: list[tuple[str, str]], known_keys: set[str]) -> list[str]:
    """纯函数：`form_key → data_source_key` 回填闭合（非空且已登记）。

    Args:
        pairs: （form_key, data_source_key）列表。
        known_keys: 已登记数据源标识集合。

    Returns:
        list[str]: 问题描述（空 = 通过）。
    """
    issues: list[str] = []
    for form_key, data_source_key in pairs:
        if not data_source_key:
            issues.append(f"form_key 未回填 data_source_key：{form_key!r}")
        elif not KEY_RE.match(data_source_key):
            issues.append(f"data_source_key 格式非法：{form_key!r} → {data_source_key!r}")
        elif data_source_key not in known_keys:
            issues.append(f"data_source_key 未登记：{form_key!r} → {data_source_key!r}")
    return issues


def load_table_texts(cfg: Config, rule: SystemFieldsRules) -> dict[str, str]:
    """读取表文件目录下全部表文件文本。

    Args:
        cfg: 仓配置。
        rule: 规则段。

    Returns:
        dict[str, str]: {表名: 文本}。
    """
    directory = cfg.root / rule.table_dir
    if not directory.is_dir():
        return {}
    return {item.stem: read_text(item) for item in sorted(directory.glob("*.md"))}


def collect_problems(cfg: Config, rule: SystemFieldsRules) -> list[str]:
    """执行全部校验项，返回问题清单。

    Args:
        cfg: 仓配置。
        rule: 规则段。

    Returns:
        list[str]: 问题描述。
    """
    problems: list[str] = []
    catalog = [
        (record[rule.catalog.key_field], record[rule.catalog.table_field])
        for record in parse_records(cfg, rule.catalog)
        if record.get(rule.catalog.key_field) and record.get(rule.catalog.table_field)
    ]
    if not catalog:
        problems.append(f"[form_key] 清单不可解析或为空：{rule.catalog.path}")
        return problems
    problems.extend(
        f"[form_key] {item}"
        for item in diff_form_catalog(
            catalog,
            load_table_texts(cfg, rule),
            row_form_key=rule.row_form_key,
            row_parent_id=rule.row_parent_id,
            table_dir=rule.table_dir,
        )
    )
    known_keys = {
        record[rule.datasource.key_field]
        for record in parse_records(cfg, rule.datasource)
        if record.get(rule.datasource.key_field)
    }
    if not known_keys:
        problems.append(f"[data_source_key] 数据源清单不可解析或为空：{rule.datasource.path}")
        return problems
    pairs = [
        (record[rule.catalog.key_field], record.get(rule.datasource.key_field, ""))
        for record in parse_records(cfg, rule.catalog)
        if record.get(rule.catalog.key_field)
    ]
    problems.extend(f"[data_source_key] {item}" for item in diff_data_source_closure(pairs, known_keys))
    for label, spec in (("[form_key]", rule.catalog), ("[data_source_key]", rule.datasource)):
        if not spec.enum:
            continue
        enum_values = parse_enum_values(cfg, spec)
        if not enum_values:
            problems.append(f"{label} 枚举不可解析：{spec.const} → {spec.enum}")
            continue
        declarations = parse_kind_declarations(cfg, spec)
        if not declarations:
            problems.append(f"{label} 清单不可解析或为空：{spec.const}")
            continue
        problems.extend(f"{label} {item}" for item in diff_explicit_kind(declarations, set(enum_values.values())))
    return problems


def self_test() -> int:
    """纯函数反例自检（清单 / 回填闭合 / kind 显式声明三组）。

    Returns:
        int: 退出码（0 通过 / 1 失败）。
    """
    ok_catalog = [("role", "sys_role"), ("tenant", "sys_tenant")]
    ok_tables = {
        "sys_role": "| `form_key` | VARCHAR(64) |\n| `parent_id` | BIGINT |\n",
        "sys_tenant": "| `form_key` | VARCHAR(64) |\n| `parent_id` | BIGINT |\n",
    }
    shared = {"row_form_key": "form_key", "row_parent_id": "parent_id", "table_dir": "数据表设计"}
    cases: list[tuple[str, list[tuple[str, str]], dict[str, str], int]] = [
        ("正常：清单与表文件一致", ok_catalog, ok_tables, 0),
        ("反例 A：form_key 跨表重复", [("role", "sys_role"), ("role", "sys_tenant")], ok_tables, 1),
        (
            "反例 B：表文件缺 parent_id 行",
            ok_catalog,
            {"sys_role": "| `form_key` | VARCHAR(64) |\n", "sys_tenant": "| `form_key` |\n| `parent_id` |\n"},
            1,
        ),
        ("反例 C：表文件有 form_key 行但未登记清单", ok_catalog, dict(ok_tables, sys_extra=_both_rows()), 1),
        ("反例 D：form_key 格式非法", [("Role", "sys_role")], {"sys_role": _both_rows()}, 1),
        ("反例 E：清单表缺表文件", [("role", "sys_ghost")], {"sys_role": _both_rows()}, 2),
    ]
    closure_cases: list[tuple[str, list[tuple[str, str]], set[str], int]] = [
        ("正常：回填值均已登记", [("role", "role"), ("tenant", "tenant")], {"role", "tenant"}, 0),
        ("反例 A：未回填", [("role", "")], {"role"}, 1),
        ("反例 B：回填值未登记", [("role", "ghost")], {"role"}, 1),
        ("反例 C：回填值格式非法", [("role", "Bad-Key")], {"role"}, 1),
    ]
    kind_cases: list[tuple[str, list[tuple[str, bool, str | None]], set[str], int]] = [
        ("正常：逐条显式声明且取值合法", [("role", True, "dict"), ("order", True, "form")], {"dict", "form"}, 0),
        ("反例 A：未显式声明 kind", [("role", False, None)], {"dict", "form"}, 1),
        ("反例 B：取值非法", [("role", True, "ghost")], {"dict", "form"}, 1),
        ("反例 C：取值无法解析", [("role", True, None)], {"dict", "form"}, 1),
    ]
    bad = 0
    for title, catalog, tables, expect in cases:
        found = diff_form_catalog(catalog, tables, **shared)
        bad += _report(title, found, expect)
    for title, pairs, known, expect in closure_cases:
        bad += _report(title, diff_data_source_closure(pairs, known), expect)
    for title, declarations, valid, expect in kind_cases:
        bad += _report(title, diff_explicit_kind(declarations, valid), expect)
    total = len(cases) + len(closure_cases) + len(kind_cases)
    out(f"\n[system-fields self-test] {'通过' if not bad else '不通过'}：断言用例 {total} 个")
    return 1 if bad else 0


def _both_rows() -> str:
    """造一份含两行必备字段的表文件文本（自检夹具）。

    Returns:
        str: 表文件片段。
    """
    return "| `form_key` | VARCHAR(64) |\n| `parent_id` | BIGINT |\n"


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
    """执行系统字段校验。

    Args:
        cfg: 仓配置。
        as_self_test: 是否只跑内置自检。

    Returns:
        int: 退出码（0 通过 / 1 有问题 / 2 规则缺失）。
    """
    if as_self_test:
        return self_test()
    try:
        rule = require(cfg.system_fields_rules, "system_fields", MIN_KEYS)
    except RulesError as exc:
        out(f"[system-fields] {exc}")
        return 2
    problems = collect_problems(cfg, rule)
    if problems:
        out(f"[system-fields] 不通过：{len(problems)} 项")
        for item in problems[:50]:
            out(f"  {item}")
        if len(problems) > 50:
            out(f"  ... 其余 {len(problems) - 50} 项")
        return 1
    out("[system-fields] 通过：清单与表文件双向一致、跨表全局唯一，数据源回填闭合，kind 均显式声明且取值合法")
    return 0
