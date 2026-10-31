"""雪花 ID 静态护栏（`engkit base snowflake-ids`）。

口径（需求 03-6 / 任务 03_05）：

- **模型层**：雪花 ID 列（`id` / `*_id` / `*_by` / 登记的语义名）一律用 `SnowflakeId`，
  出现裸 `BigInteger` 即违规；例外（计数 / 字节度量列）在 `[rules.snowflake_ids].whitelist`
  逐条登记（`路径::列名`）。
- **契约层**：`schemas/` 下的 ID 字段不得残留 `int` 注解（须 `SnowflakeIdStr`）。
- 扫描根、ID 名与后缀、白名单全部来自 `[rules.snowflake_ids]`；支持 `--files-from` 增量。
"""

from __future__ import annotations

import re
from pathlib import Path

from ..config import Config, SnowflakeIdsRules
from ..console import out
from .rules import RulesError, read_text, require

MIN_KEYS = ("scan_targets", "id_names", "id_suffixes", "whitelist")
"""未登记时报错给出的最小键指引。"""

DECL = re.compile(r"^(?P<indent>\s*)(?P<name>\w+)\s*:\s*Mapped\[[^\]]*\]\s*=\s*mapped_column\((?:.*)$")
FIELD = re.compile(r"^(?P<indent>\s{4,})(?P<name>\w+):\s*(?:int \| None|Optional\[int\]|int)(?P<rest>\s*(?:=.*)?)$")
SCHEMA_DIR = "/schemas/"
"""契约层目录片段（命中才做 `int` 注解扫描）。"""


def is_id_name(name: str, rule: SnowflakeIdsRules) -> bool:
    """判断列 / 字段名是否属雪花 ID。

    Args:
        name: 列名或字段名。
        rule: 规则段。

    Returns:
        bool: 是 ID 名 True。
    """
    return name in rule.id_names or any(name.endswith(suffix) for suffix in rule.id_suffixes)


def scan_text(rel_path: str, text: str, rule: SnowflakeIdsRules) -> list[str]:
    """扫描单个文件文本。

    Args:
        rel_path: 相对仓库根的 POSIX 路径。
        text: 文件全文。
        rule: 规则段。

    Returns:
        list[str]: 问题清单。
    """
    whitelist = set(rule.whitelist)
    problems: list[str] = []
    lines = text.splitlines()
    index = 0
    while index < len(lines):
        match = DECL.match(lines[index])
        if not match:
            index += 1
            continue
        block = [lines[index]]
        balance = lines[index].count("(") - lines[index].count(")")
        end = index
        while balance > 0 and end + 1 < len(lines):
            end += 1
            block.append(lines[end])
            balance += lines[end].count("(") - lines[end].count(")")
        name = match.group("name")
        key = f"{rel_path}::{name}"
        if "BigInteger" in "\n".join(block) and is_id_name(name, rule) and key not in whitelist:
            problems.append(f"{rel_path}:{index + 1}: 雪花 ID 列 {name} 仍是裸 BigInteger（应改用 SnowflakeId）")
        index = end + 1
    if SCHEMA_DIR in f"/{rel_path}":
        for number, line in enumerate(lines, start=1):
            field = FIELD.match(line)
            if field and is_id_name(field.group("name"), rule):
                problems.append(
                    f"{rel_path}:{number}: ID 字段 {field.group('name')} 仍是 int 注解（应改用 SnowflakeIdStr）"
                )
    return problems


def _iter_files(root: Path, rule: SnowflakeIdsRules, files_from: str) -> list[str]:
    """枚举待扫描文件（相对根 POSIX 路径）。

    Args:
        root: 仓库根。
        rule: 规则段。
        files_from: 变更清单文件（空串表示全量）。

    Returns:
        list[str]: 相对路径清单。
    """
    if files_from:
        candidates = [
            line.strip().replace("\\", "/")
            for line in read_text(Path(files_from)).splitlines()
            if line.strip().endswith(".py")
        ]
        return [
            path
            for path in candidates
            if any(path.startswith(f"{root_dir.strip('/')}/") for root_dir in rule.scan_targets)
        ]
    files: list[str] = []
    for target in rule.scan_targets:
        base = root / target
        if not base.exists():
            continue
        for path in sorted(base.rglob("*.py")):
            rel_parts = set(path.relative_to(root).parts)
            if rel_parts & set(rule.exclude_parts):
                continue
            files.append(path.relative_to(root).as_posix())
    return files


def scan_files(cfg: Config, rule: SnowflakeIdsRules, files: list[str]) -> list[str]:
    """扫描文件清单。

    Args:
        cfg: 仓配置。
        rule: 规则段。
        files: 相对路径清单。

    Returns:
        list[str]: 问题清单。
    """
    problems: list[str] = []
    for rel_path in files:
        path = cfg.root / rel_path
        if not path.is_file():
            continue
        problems.extend(scan_text(rel_path, read_text(path), rule))
    return problems


def self_test(rule: SnowflakeIdsRules) -> int:
    """自检：合规样本通过；裸 BigInteger / schema 残留 / 白名单三类样本判定正确。

    Args:
        rule: 规则段。

    Returns:
        int: 退出码（0 通过 / 1 失败）。
    """
    id_name = rule.id_names[0] if rule.id_names else "id"
    model_path = f"{rule.scan_targets[0] if rule.scan_targets else 'backend'}/models/demo.py"
    schema_path = f"{rule.scan_targets[0] if rule.scan_targets else 'backend'}/schemas/demo.py"
    good_model = f"    {id_name}: Mapped[int] = mapped_column(SnowflakeId, primary_key=True)\n"
    bad_model = "    parent_id: Mapped[int] = mapped_column(BigInteger)\n"
    counted = "    size: Mapped[int] = mapped_column(BigInteger)\n"
    bad_schema = "    user_id: int\n"
    good_schema = "    user_id: SnowflakeIdStr\n"
    vectors: list[tuple[str, dict[str, str], tuple[str, ...], int]] = [
        ("合规样本", {model_path: good_model, schema_path: good_schema}, rule.whitelist, 0),
        ("非 ID 计数列", {model_path: counted}, rule.whitelist, 0),
        ("裸 BigInteger ID 列", {model_path: bad_model}, rule.whitelist, 1),
        ("schema 残留 int", {schema_path: bad_schema}, rule.whitelist, 1),
        (
            "白名单豁免",
            {model_path: f"    {id_name}: Mapped[int] = mapped_column(BigInteger)\n"},
            (*rule.whitelist, f"{model_path}::{id_name}"),
            0,
        ),
    ]
    bad = 0
    for title, texts, whitelist, expect in vectors:
        scoped = SnowflakeIdsRules(
            scan_targets=rule.scan_targets,
            id_names=rule.id_names,
            id_suffixes=rule.id_suffixes,
            whitelist=whitelist,
            exclude_parts=rule.exclude_parts,
        )
        found = [problem for path, text in texts.items() for problem in scan_text(path, text, scoped)]
        flag = "OK" if len(found) == expect else "FAIL"
        bad += flag == "FAIL"
        out(f"  [{flag}] {title}：期望 {expect} 项，实得 {len(found)} 项")
    out(f"\n[check_snowflake_ids self-test] {'通过' if not bad else '不通过'}：断言用例 {len(vectors)} 个")
    return 1 if bad else 0


def run(cfg: Config, *, as_self_test: bool = False, files_from: str = "") -> int:
    """执行雪花 ID 静态护栏。

    Args:
        cfg: 仓配置。
        as_self_test: 是否只跑内置自检。
        files_from: 变更清单文件（空串表示全量）。

    Returns:
        int: 退出码（0 通过 / 1 有问题 / 2 规则缺失）。
    """
    try:
        rule = require(cfg.snowflake_ids_rules, "snowflake_ids", MIN_KEYS)
    except RulesError as exc:
        out(f"[check_snowflake_ids] {exc}")
        return 2
    if as_self_test:
        return self_test(rule)
    files = _iter_files(cfg.root, rule, files_from)
    problems = scan_files(cfg, rule, files)
    if problems:
        out(f"[check_snowflake_ids] 不通过：{len(problems)} 项")
        for item in problems:
            out(f"  - {item}")
        return 1
    out(f"[check_snowflake_ids] 通过：{len(files)} 个文件无裸 BigInteger ID 列 / int 注解残留")
    return 0
