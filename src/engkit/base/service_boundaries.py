"""服务边界与跨库访问校验（`engkit base service-boundaries`）。

服务维十项（《后端开发规范》依赖方向与模块边界）：

1. 共享库不得依赖服务；
2. 服务不得互相依赖；
3. 分层依赖单向（配置给出「源子包 → 禁止依赖的目标子包」黑名单）；
4. 跨包不得引用私有实现（模块 / 属性名下划线前缀）；
5. 表名 / 表前缀跨服务唯一（平台共享前缀按表级归属判定、不判前缀冲突），声明的表名须在表归属登记中；
6. 跨服务库访问：声明越权与引用越界（原始 SQL / 外键目标 / 表名 token），基础设施表每服务自有放行，
   共享模型子包声明场景只校验「表已登记」；
7. 读侧出口例外白名单结构 / 字段 / 归属校验，命中即放行并计数；
8. `ops` 运行镜像声明（`RUN_IMAGE` 合法取值）；
9. `ops` 导入闭包 ⊇ 声明镜像所含包，镜像包集合须与镜像构建文件的 COPY 面一致；
10. 运行期代码不得依赖运维通道、非平台服务不得依赖平台包。

域级断言（维度从「服务包」下沉到「业务域」）：

- D1 跨域 import 禁止；D2 跨域只经公共面（目标为源域 `public_api` 出口时放行）；
- D3 跨域表引用禁止（读侧例外可放行）；D4 跨域写禁止（无例外）；
- 另有：域布局完整性（路径重复归属 / 未命中文件 / 未登记的域实现）、表归属双字段一致
  （`owner` == 域承载服务、基础设施表与不归域表 `domain` 恒空）、存量跨域引用基线（**只减不增**）。

**规则与判定分离**：表归属登记、服务目录、域布局、例外白名单与域基线都是**仓内唯一事实源**，
本模块只做 AST 解析与判定（配置键见 `[rules.service_boundaries]`），不写第二份数据。
"""

from __future__ import annotations

import ast
import hashlib
import json
import re
import tempfile
from collections.abc import Sequence
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

from ..config import Config, ServiceBoundariesRules, parse
from ..console import out
from . import table_index
from .rules import RulesError, read_text, require
from .scan_scope import read_file_list
from .table_index import TableIndex

MIN_KEYS = (
    "shared_libs",
    "service_packages",
    "ownership",
    "catalog",
    "layout",
    "exceptions",
    "baseline",
    "counters",
)
"""未登记时报错给出的最小键指引。"""

SQL_HINTS = ("select", "insert", "update", "delete", "from", "join")
"""字符串常量按 SQL 认定的关键词（命中才做语句分析）。"""

WRITE_OPERATIONS = frozenset({"write", "ddl"})
"""跨域写判定：SQL 写操作与 DDL（D4 无例外）。"""

IDENTIFIER_LIKE = re.compile(r"^[A-Za-z0-9_.]+$")
"""标识符形态字符串（整串即表名 / 点分名；避免文档文案误报）。"""

TABLE_NAME_RE = re.compile(r"^[a-z][a-z0-9_]*$")
"""登记表名与业务域标识的形态谓词。"""

PLACEMENT = "placement"
"""域布局状态：域实现已有域子包载体。"""

PLANNED = "planned"
"""域布局状态：域未落地（允许无代码路径）。"""

BASELINE_VERSION = 1
"""域级基线文件格式版本（输出契约，不是仓数据）。"""


class RegistryError(RuntimeError):
    """登记源不可解析或常量为空（命令行入口据此退 1）。"""


@dataclass(frozen=True)
class Package:
    """一个工作区包。

    Attributes:
        path: 包源码目录。
        shared: 是否共享库包。
        project: 所在工程目录名（`libs/<工程>` / `services/<工程>`）。
    """

    path: Path
    shared: bool
    project: str


@dataclass(frozen=True)
class TableRecord:
    """表归属登记记录（字段取自登记模块的常量条目）。

    Attributes:
        table_name: 表名。
        owner: 归属服务标识（`owner_every_service` 表示每服务自有）。
        domain: 归属业务域（空串 = 不归域）。
        datasource: 库类别取值。
        status: 登记状态取值。
    """

    table_name: str
    owner: str
    domain: str = ""
    datasource: str = ""
    status: str = ""


@dataclass(frozen=True)
class CatalogRecord:
    """服务目录登记记录。

    Attributes:
        module_key: 模块标识。
        service_key: 服务标识（空串回落到 `module_key`）。
        table_prefix: 该服务登记的表前缀。
    """

    module_key: str
    service_key: str = ""
    table_prefix: str = ""


@dataclass(frozen=True)
class Registry:
    """表归属 + 服务目录的合并视图（AST 解析结果）。

    Attributes:
        tables: 表归属记录（声明序）。
        catalog: 服务目录记录（声明序）。
        owner_every_service: 每服务自有哨兵。
    """

    tables: tuple[TableRecord, ...]
    catalog: tuple[CatalogRecord, ...]
    owner_every_service: str = "*"

    def record_of(self, table: str) -> TableRecord | None:
        """取表归属记录（未登记返回 None）。

        Args:
            table: 表名。

        Returns:
            TableRecord | None: 登记记录。
        """
        return next((record for record in self.tables if record.table_name == table), None)

    def names(self) -> tuple[str, ...]:
        """全部已登记表名（声明序去重）。

        Returns:
            tuple[str, ...]: 表名。
        """
        return tuple(dict.fromkeys(record.table_name for record in self.tables))

    def owner_of(self, table: str) -> str | None:
        """取表归属（未登记返回 None）。

        Args:
            table: 表名。

        Returns:
            str | None: 归属标签。
        """
        record = self.record_of(table)
        return None if record is None else record.owner

    def domain_of(self, table: str) -> str | None:
        """取表归属业务域（未登记 None；不归域空串）。

        Args:
            table: 表名。

        Returns:
            str | None: 业务域标识。
        """
        record = self.record_of(table)
        return None if record is None else record.domain

    def services(self) -> tuple[str, ...]:
        """服务目录登记的服务标识（`service_key` 优先，回落 `module_key`）。

        Returns:
            tuple[str, ...]: 服务标识（声明序去重）。
        """
        return tuple(dict.fromkeys(record.service_key or record.module_key for record in self.catalog))

    def prefixes(self) -> tuple[str, ...]:
        """例外登记可用的表前缀集合（服务目录前缀 ∪ 归属登记表名前缀）。

        Returns:
            tuple[str, ...]: 前缀集合（声明序去重）。
        """
        catalog = [record.table_prefix for record in self.catalog if record.table_prefix]
        owned = [table_prefix_of(record.table_name) for record in self.tables]
        return tuple(dict.fromkeys(catalog + owned))

    def service_prefixes(self) -> dict[str, tuple[str, ...]]:
        """服务标识 → 其登记的表前缀（表名前缀归属校验用）。

        Returns:
            dict[str, tuple[str, ...]]: 归属标签到前缀集合。
        """
        grouped: dict[str, list[str]] = {}
        for record in self.catalog:
            label = record.service_key or record.module_key
            if not label or not record.table_prefix:
                continue
            bucket = grouped.setdefault(label, [])
            if record.table_prefix not in bucket:
                bucket.append(record.table_prefix)
        return {label: tuple(values) for label, values in grouped.items()}


@dataclass(frozen=True)
class Violation:
    """一条数据所有权越界。

    Attributes:
        table: 被访问表名。
        owner: 表归属服务标识。
        owner_domain: 表归属业务域。
    """

    table: str
    owner: str
    owner_domain: str = ""


@dataclass(frozen=True)
class TableRef:
    """SQL 中引用的表。

    Attributes:
        table: 表名（小写；schema 限定取末段）。
        operation: 操作归类（read / write / ddl / unknown）。
    """

    table: str
    operation: str


@dataclass(frozen=True)
class OwnershipException:
    """一条读侧出口例外登记。

    Attributes:
        service: 消费方服务标识。
        target: 目标表名或表前缀。
        access: 访问类型（配置只允许只读）。
        exit: 出口类型。
        domain: 消费方业务域（可选）。
    """

    service: str
    target: str
    access: str
    exit: str
    domain: str = ""


@dataclass(frozen=True)
class DomainEntry:
    """一条域布局。

    Attributes:
        domain_key: 业务域标识。
        service_key: 当前承载服务标识。
        status: 布局状态。
        paths: 域实现路径（相对仓根，允许 glob）。
        public_api: 域内公共出口模块名。
    """

    domain_key: str
    service_key: str
    status: str = "legacy"
    paths: tuple[str, ...] = ()
    public_api: tuple[str, ...] = ()


@dataclass(frozen=True)
class BaselineEntry:
    """一条域级基线（存量跨域引用）。

    Attributes:
        kind: 条目种类（import / table_ref / table_declare）。
        domain: 源业务域。
        target: 目标业务域。
        file: 源文件（相对仓根）。
        fingerprint: 行指纹。
    """

    kind: str
    domain: str
    target: str
    file: str
    fingerprint: str


@dataclass(frozen=True)
class Layout:
    """域布局全集：域清单 + 文件归属索引 + 域外路径。

    Attributes:
        entries: 域布局条目（声明序）。
        shared: 域外路径（装配 / 基座 / 基础设施面）。
    """

    entries: tuple[DomainEntry, ...]
    shared: tuple[str, ...]

    def domain_keys(self) -> tuple[str, ...]:
        """全部业务域标识（声明序）。

        Returns:
            tuple[str, ...]: 域标识。
        """
        return tuple(entry.domain_key for entry in self.entries)

    def entry_of(self, domain_key: str) -> DomainEntry | None:
        """取域布局条目（未登记返回 None）。

        Args:
            domain_key: 业务域标识。

        Returns:
            DomainEntry | None: 域布局条目。
        """
        return next((entry for entry in self.entries if entry.domain_key == domain_key), None)

    def public_api_of(self, domain_key: str) -> tuple[str, ...]:
        """取域内公共出口模块名（D2 放行面）。

        Args:
            domain_key: 业务域标识。

        Returns:
            tuple[str, ...]: 模块名。
        """
        entry = self.entry_of(domain_key)
        return () if entry is None else entry.public_api

    def domain_services(self) -> dict[str, str]:
        """域 → 承载服务映射。

        Returns:
            dict[str, str]: 域标识到承载服务标识。
        """
        return {entry.domain_key: entry.service_key for entry in self.entries}

    def shared_files(self, root: Path) -> dict[str, None]:
        """展开域外声明为**文件集合**（支持 glob，按 `Path.glob` 语义）。

        域外路径与域路径同权支持 glob：骨架面（`__init__.py` / 装配入口 / 路由）按目录派生，
        新形态目录仍需逐条登记。展开而非字符串比对，是为了让「域外」与「路径命中文件」
        用同一套 pathlib 语义——自己写通配匹配迟早与 `expand` 判得不一样。

        Args:
            root: 仓库根。

        Returns:
            dict[str, None]: 域外文件相对路径（保序去重）。
        """
        found: dict[str, None] = {}
        for declared in self.shared:
            for item in expand(root, declared):
                found.setdefault(item, None)
        return found

    def resolve_files(self, root: Path) -> tuple[dict[str, str], list[str]]:
        """展开声明路径为「文件 → 域」索引。

        Args:
            root: 仓库根。

        Returns:
            tuple[dict[str, str], list[str]]: （文件 → 域映射, 问题清单）。
        """
        mapping: dict[str, str] = {}
        problems: list[str] = []
        for entry in self.entries:
            for declared in entry.paths:
                matched = expand(root, declared)
                if not matched:
                    problems.append(f"[域布局] {entry.domain_key} 的路径未命中文件：{declared}")
                    continue
                for path in matched:
                    existing = mapping.get(path)
                    if existing is not None and existing != entry.domain_key:
                        problems.append(f"[域布局] {path} 同时归属 {existing} 与 {entry.domain_key}")
                        continue
                    mapping[path] = entry.domain_key
        return mapping, problems

    def validate(self, root: Path) -> list[str]:
        """语义校验：空域 / 路径重复归属 / 与域外路径冲突 / 路径是否存在。

        Args:
            root: 仓库根。

        Returns:
            list[str]: 问题清单。
        """
        problems: list[str] = []
        declared: dict[str, str] = {}
        shared = set(self.shared)
        for entry in self.entries:
            if entry.status != PLANNED and not entry.paths:
                problems.append(f"[域布局] {entry.domain_key} 状态 {entry.status} 但未声明代码路径")
            for path in entry.paths:
                target = normalize(path)
                if target in shared:
                    problems.append(f"[域布局] {entry.domain_key} 的路径同时登记为域外路径：{target}")
                owner = declared.get(target)
                if owner is not None:
                    problems.append(f"[域布局] {target} 同时归属 {owner} 与 {entry.domain_key}")
                    continue
                declared[target] = entry.domain_key
        for declared_path in declared:
            if not expand(root, declared_path):
                problems.append(f"[域布局] 路径未命中文件：{declared_path}")
        for shared_path in self.shared:
            if not expand(root, shared_path):
                problems.append(f"[域布局] 域外路径未命中文件：{shared_path}")
        return problems


@dataclass
class Outcome:
    """一次校验的结果。

    Attributes:
        problems: 违规明细。
        counts: 分类计数（键取配置 `counters`，JSON 输出与度量消费的契约）。
        service_tables: 各服务声明的表名清单（结论行列出）。
    """

    problems: list[str] = field(default_factory=list)
    counts: dict[str, int] = field(default_factory=dict)
    service_tables: dict[str, list[str]] = field(default_factory=dict)


def normalize(path: str) -> str:
    """路径规范化（去 `./` / 反斜杠转 posix / 去首尾空白）。

    Args:
        path: 原始路径。

    Returns:
        str: 规范化路径。
    """
    return path.strip().replace("\\", "/").removeprefix("./")


def expand(root: Path, declared: str) -> list[str]:
    """展开声明路径为文件列表（含 glob；相对仓库根 posix）。

    Args:
        root: 仓库根。
        declared: 声明路径（可含 `**` / `*`）。

    Returns:
        list[str]: 命中的文件路径（稳定序）。
    """
    target = normalize(declared)
    if any(char in target for char in "*?["):
        matched = sorted(path for path in root.glob(target) if path.is_file())
    else:
        direct = root / target
        matched = [direct] if direct.is_file() else []
    return [path.relative_to(root).as_posix() for path in matched]


def table_prefix_of(table: str) -> str:
    """取表名前缀（首个 `_` 前段 + `_`；无 `_` 返回原表名）。

    Args:
        table: 表名。

    Returns:
        str: 表前缀。
    """
    return table.split("_", 1)[0] + "_" if "_" in table else table


def fingerprint(line: str) -> str:
    """行指纹：规范化后的 SHA-1 前 12 位（行号漂移不影响基线匹配）。

    Args:
        line: 源文件行文本。

    Returns:
        str: 指纹。
    """
    return hashlib.sha1(" ".join(line.split()).encode("utf-8")).hexdigest()[:12]


def rel_posix(path: Path, root: Path) -> str:
    """取相对仓库根的 posix 路径。

    Args:
        path: 目标路径。
        root: 仓库根。

    Returns:
        str: 相对路径。
    """
    return path.relative_to(root).as_posix()


def is_test_path(path: Path, root: Path) -> bool:
    """源文件是否位于测试目录（引用扫描跳过测试）。

    Args:
        path: 源文件路径。
        root: 包根目录。

    Returns:
        bool: 测试目录下返回 True。
    """
    return "tests" in path.relative_to(root).as_posix().split("/")


# ---------- SQL 分析 ----------

_READ_KEYWORDS = frozenset({"select", "with"})
_WRITE_KEYWORDS = frozenset({"insert", "update", "delete", "replace", "merge", "upsert"})
_DDL_KEYWORDS = frozenset({"create", "drop", "alter", "truncate", "rename", "comment"})
_IGNORED_TABLES = frozenset({"select", "where", "set", "values", "on", "as", "dual", "only", "lateral", "from", "join"})
_BLOCK_COMMENT = re.compile(r"/\*.*?\*/", re.DOTALL)
_LINE_COMMENT = re.compile(r"--[^\n]*")
_IDENTIFIER_QUOTES = re.compile(r"[`\"\[\]]")
_FIRST_WORD = re.compile(r"[a-z]+")
_TABLE_PATTERNS = (
    re.compile(r"\bfrom\s+([a-z_][\w]*(?:\.[a-z_][\w]*)?)", re.IGNORECASE),
    re.compile(r"\bjoin\s+([a-z_][\w]*(?:\.[a-z_][\w]*)?)", re.IGNORECASE),
    re.compile(r"\binto\s+([a-z_][\w]*(?:\.[a-z_][\w]*)?)", re.IGNORECASE),
    re.compile(r"\bupdate\s+([a-z_][\w]*(?:\.[a-z_][\w]*)?)", re.IGNORECASE),
)


def clean_sql(statement: str) -> str:
    """去注释与标识符引号（保留字面量内容）。

    Args:
        statement: 原始 SQL。

    Returns:
        str: 清洗后的 SQL。
    """
    text = _BLOCK_COMMENT.sub(" ", statement)
    text = _LINE_COMMENT.sub(" ", text)
    return _IDENTIFIER_QUOTES.sub("", text)


def operation_of(statement: str) -> str:
    """语句操作归类。

    Args:
        statement: SQL 语句。

    Returns:
        str: `read` / `write` / `ddl` / `unknown`。
    """
    cleaned = clean_sql(statement).lstrip("( \t\r\n")
    match = _FIRST_WORD.match(cleaned.lower())
    word = match.group(0) if match else ""
    if word in _READ_KEYWORDS:
        return "read"
    if word in _WRITE_KEYWORDS:
        return "write"
    if word in _DDL_KEYWORDS:
        return "ddl"
    return "unknown"


def extract_tables(statement: str) -> list[str]:
    """提取语句涉及的表名（保序去重）。

    Args:
        statement: SQL 语句。

    Returns:
        list[str]: 表名（小写；schema 限定取末段）。
    """
    found: dict[str, None] = {}
    for pattern in _TABLE_PATTERNS:
        for raw in pattern.findall(clean_sql(statement)):
            table = raw.split(".")[-1].lower()
            if table in _IGNORED_TABLES or table in found:
                continue
            found[table] = None
    return list(found)


def analyze(statement: str) -> list[TableRef]:
    """按操作归类产出表引用列表。

    Args:
        statement: SQL 语句。

    Returns:
        list[TableRef]: 表引用（同表仅出现一次）。
    """
    operation = operation_of(statement)
    return [TableRef(table=table, operation=operation) for table in extract_tables(statement)]


# ---------- 登记源（AST 解析） ----------


def assignment_of(node: ast.stmt) -> tuple[str | None, ast.expr | None]:
    """取「单目标赋值」的目标名与右值。

    Args:
        node: 语句节点。

    Returns:
        tuple[str | None, ast.expr | None]: （目标名, 右值）；非单目标赋值返回 (None, None)。
    """
    if isinstance(node, ast.Assign):
        if len(node.targets) == 1 and isinstance(node.targets[0], ast.Name):
            return node.targets[0].id, node.value
        return None, None
    if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
        return node.target.id, node.value
    return None, None


def module_constants(tree: ast.Module) -> dict[str, str]:
    """取模块级字符串常量（`NAME = "..."`）。

    Args:
        tree: 语法树。

    Returns:
        dict[str, str]: 常量名 → 取值。
    """
    consts: dict[str, str] = {}
    for node in tree.body:
        name, value = assignment_of(node)
        if name is None or value is None:
            continue
        if isinstance(value, ast.Constant) and isinstance(value.value, str):
            consts[name] = value.value
    return consts


def enum_members(tree: ast.Module) -> dict[str, dict[str, str]]:
    """取模块内枚举类（类体字符串成员）。

    Args:
        tree: 语法树。

    Returns:
        dict[str, dict[str, str]]: 类名 → （成员名 → 取值）。
    """
    enums: dict[str, dict[str, str]] = {}
    for node in tree.body:
        if not isinstance(node, ast.ClassDef):
            continue
        members: dict[str, str] = {}
        for stmt in node.body:
            name, value = assignment_of(stmt)
            if name is None or value is None:
                continue
            if isinstance(value, ast.Constant) and isinstance(value.value, str):
                members[name] = value.value
        if members:
            enums[node.name] = members
    return enums


def string_value(node: ast.expr | None, consts: dict[str, str], enums: dict[str, dict[str, str]]) -> str:
    """把登记条目里的字段表达式解析为字符串（常量引用 / 枚举成员 / 字面量）。

    Args:
        node: 字段表达式（缺失为 None）。
        consts: 模块级字符串常量。
        enums: 模块内枚举成员。

    Returns:
        str: 解析结果（无法解析为空串）。
    """
    if node is None:
        return ""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.Name):
        return consts.get(node.id, "")
    if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name):
        return enums.get(node.value.id, {}).get(node.attr, "")
    return ""


def sequence_of(node: ast.expr | None) -> list[ast.expr]:
    """把「清单常量」展开为元素列表（支持 `容器((条目, ...))` 包装）。

    Args:
        node: 表达式。

    Returns:
        list[ast.expr]: 元素表达式。
    """
    if isinstance(node, ast.Call):
        return sequence_of(node.args[0]) if node.args else []
    if isinstance(node, (ast.List, ast.Tuple)):
        return list(node.elts)
    return []


def record_keywords(node: ast.expr) -> dict[str, ast.expr]:
    """取登记条目调用的关键字参数。

    Args:
        node: 元素表达式。

    Returns:
        dict[str, ast.expr]: 关键字名 → 值表达式。
    """
    if not isinstance(node, ast.Call):
        return {}
    return {keyword.arg: keyword.value for keyword in node.keywords if keyword.arg is not None}


def constant_node(tree: ast.Module, name: str) -> ast.expr | None:
    """取模块级常量的右值表达式。

    Args:
        tree: 语法树。
        name: 常量名。

    Returns:
        ast.expr | None: 右值（未找到为 None）。
    """
    for node in tree.body:
        target, value = assignment_of(node)
        if target == name:
            return value
    return None


def parse_module(path: Path, label: str) -> ast.Module:
    """解析登记源模块（结构非法抛 `RegistryError`）。

    Args:
        path: 模块文件路径。
        label: 模块用途（报错文案用）。

    Returns:
        ast.Module: 语法树。

    Raises:
        RegistryError: 语法非法。
    """
    try:
        return ast.parse(read_text(path), filename=str(path))
    except SyntaxError as exc:
        raise RegistryError(f"登记源不可解析：{label}（{exc.msg}）") from exc


def load_registry(cfg: Config, rule: ServiceBoundariesRules) -> Registry:
    """读取表归属登记与服务目录（两块登记源各自独立解析）。

    Args:
        cfg: 仓配置。
        rule: 规则段。

    Returns:
        Registry: 合并视图。

    Raises:
        RulesError: 登记源文件缺失。
        RegistryError: 登记源不可解析或常量为空。
    """
    tables = _load_table_records(cfg, rule)
    catalog = _load_catalog_records(cfg, rule)
    return Registry(tables=tables, catalog=catalog, owner_every_service=rule.owner_every_service)


def _require_module(cfg: Config, rel: str, label: str) -> ast.Module:
    """取登记源模块语法树（文件缺失抛 `RulesError`）。

    Args:
        cfg: 仓配置。
        rel: 相对仓根的模块路径。
        label: 模块用途。

    Returns:
        ast.Module: 语法树。

    Raises:
        RulesError: 文件缺失。
    """
    path = cfg.root / rel
    if not path.is_file():
        raise RulesError(f"{label}缺失：{rel}")
    return parse_module(path, label)


def _load_table_records(cfg: Config, rule: ServiceBoundariesRules) -> tuple[TableRecord, ...]:
    """解析表归属登记常量。

    Args:
        cfg: 仓配置。
        rule: 规则段。

    Returns:
        tuple[TableRecord, ...]: 归属记录（声明序）。

    Raises:
        RegistryError: 常量缺失或条目不可解析。
    """
    tree = _require_module(cfg, rule.ownership_module, "表归属登记模块")
    consts = module_constants(tree)
    enums = enum_members(tree)
    node = constant_node(tree, rule.ownership_const)
    if node is None:
        raise RegistryError(f"登记源不可解析：{rule.ownership_module} → {rule.ownership_const}")
    records: list[TableRecord] = []
    for item in sequence_of(node):
        fields = record_keywords(item)
        name = string_value(fields.get("table_name"), consts, enums)
        if not name:
            continue
        records.append(
            TableRecord(
                table_name=name,
                owner=string_value(fields.get("owner"), consts, enums),
                domain=string_value(fields.get("domain"), consts, enums),
                datasource=string_value(fields.get("datasource"), consts, enums),
                status=string_value(fields.get("status"), consts, enums),
            )
        )
    if not records:
        raise RegistryError(f"登记源常量为空：{rule.ownership_module} → {rule.ownership_const}")
    return tuple(records)


def _load_catalog_records(cfg: Config, rule: ServiceBoundariesRules) -> tuple[CatalogRecord, ...]:
    """解析服务目录常量。

    Args:
        cfg: 仓配置。
        rule: 规则段。

    Returns:
        tuple[CatalogRecord, ...]: 目录记录（声明序）。

    Raises:
        RegistryError: 常量缺失。
    """
    tree = _require_module(cfg, rule.catalog_module, "服务目录模块")
    consts = module_constants(tree)
    enums = enum_members(tree)
    node = constant_node(tree, rule.catalog_const)
    if node is None:
        raise RegistryError(f"登记源不可解析：{rule.catalog_module} → {rule.catalog_const}")
    records: list[CatalogRecord] = []
    for item in sequence_of(node):
        fields = record_keywords(item)
        records.append(
            CatalogRecord(
                module_key=string_value(fields.get("module_key"), consts, enums),
                service_key=string_value(fields.get("service_key"), consts, enums),
                table_prefix=string_value(fields.get("table_prefix"), consts, enums),
            )
        )
    return tuple(records)


# ---------- 域布局 / 例外白名单 / 基线 ----------


def load_layout(path: Path) -> Layout:
    """载入域布局声明（缺文件视为空布局）。

    Args:
        path: 声明文件路径。

    Returns:
        Layout: 域布局。

    Raises:
        ValueError: JSON 非法 / 根结构非法 / 版本非法 / 条目非法 / 域标识重复。
    """
    data = _read_json_object(path, "域布局")
    if data is None:
        return Layout(entries=(), shared=())
    if data.get("version") != BASELINE_VERSION:
        raise ValueError(f"域布局版本非法（应为 {BASELINE_VERSION}）")
    raw = data.get("domains")
    if not isinstance(raw, list):
        raise ValueError("域布局 domains 须为数组")
    entries: list[DomainEntry] = []
    seen: set[str] = set()
    for index, item in enumerate(raw, start=1):
        entry = _domain_entry(item, index)
        if not TABLE_NAME_RE.match(entry.domain_key):
            raise ValueError(f"域布局第 {index} 条 domain_key 非法：{entry.domain_key}")
        if entry.domain_key in seen:
            raise ValueError(f"域布局 domain_key 重复：{entry.domain_key}")
        seen.add(entry.domain_key)
        entries.append(entry)
    return Layout(entries=tuple(entries), shared=_text_list(data, "shared_paths", required=False))


def _read_json_object(path: Path, label: str) -> dict[str, object] | None:
    """读 JSON 对象（缺文件返回 None）。

    Args:
        path: 文件路径。
        label: 用途（报错文案用）。

    Returns:
        dict[str, object] | None: 解析结果。

    Raises:
        ValueError: JSON 非法或根不是对象。
    """
    if not path.is_file():
        return None
    try:
        parsed: object = json.loads(read_text(path))
    except json.JSONDecodeError as exc:
        raise ValueError(f"{label} JSON 非法（{exc}）") from exc
    if not isinstance(parsed, dict):
        raise ValueError(f"{label}根须为对象")
    return {str(key): value for key, value in parsed.items()}


def _domain_entry(item: object, index: int) -> DomainEntry:
    """解析一条域布局（结构非法抛 `ValueError`）。

    Args:
        item: JSON 条目。
        index: 条目序号（报错文案用）。

    Returns:
        DomainEntry: 域布局条目。

    Raises:
        ValueError: 条目非法。
    """
    if not isinstance(item, dict):
        raise ValueError(f"域布局第 {index} 条须为对象")
    data = {str(key): value for key, value in item.items()}
    status = data.get("status", "legacy")
    if not isinstance(status, str):
        raise ValueError(f"域布局第 {index} 条状态非法")
    return DomainEntry(
        domain_key=_text_value(data, "domain_key"),
        service_key=_text_value(data, "service_key"),
        status=status,
        paths=_text_list(data, "paths"),
        public_api=_text_list(data, "public_api", required=False),
    )


def _text_value(data: dict[str, object], key: str) -> str:
    """取必填字符串字段。

    Args:
        data: 条目字段映射。
        key: 字段名。

    Returns:
        str: 去空白后的取值。

    Raises:
        ValueError: 字段缺失 / 非字符串 / 为空。
    """
    value = data.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"字段缺失或为空：{key}")
    return value.strip()


def _text_list(data: dict[str, object], key: str, *, required: bool = True) -> tuple[str, ...]:
    """取字符串数组字段（`paths` 族按路径规范化）。

    Args:
        data: 条目字段映射。
        key: 字段名。
        required: True 时缺失即非法。

    Returns:
        tuple[str, ...]: 取值列表。

    Raises:
        ValueError: 字段缺失（required）或非数组 / 元素非字符串。
    """
    raw = data.get(key)
    if raw is None and not required:
        return ()
    if not isinstance(raw, list):
        raise ValueError(f"字段须为数组：{key}")
    items: list[str] = []
    for item in raw:
        if not isinstance(item, str) or not item.strip():
            raise ValueError(f"字段 {key} 的元素须为非空字符串")
        items.append(normalize(item) if key in ("paths", "shared_paths") else item.strip())
    return tuple(items)


def load_baseline(path: Path) -> tuple[BaselineEntry, ...]:
    """载入域级基线（缺文件返回空）。

    Args:
        path: 基线文件路径。

    Returns:
        tuple[BaselineEntry, ...]: 基线条目。

    Raises:
        ValueError: JSON 非法 / 根结构非法 / 版本非法 / 条目非法。
    """
    data = _read_json_object(path, "域级基线")
    if data is None:
        return ()
    if data.get("version") != BASELINE_VERSION:
        raise ValueError(f"域级基线版本非法（应为 {BASELINE_VERSION}）")
    raw = data.get("entries")
    if not isinstance(raw, list):
        raise ValueError("域级基线 entries 须为数组")
    entries: list[BaselineEntry] = []
    for index, item in enumerate(raw, start=1):
        if not isinstance(item, dict):
            raise ValueError(f"域级基线第 {index} 条须为对象")
        fields = {str(key): value for key, value in item.items()}
        entries.append(
            BaselineEntry(
                kind=_text_value(fields, "kind"),
                domain=_text_value(fields, "domain"),
                target=_text_value(fields, "target"),
                file=_text_value(fields, "file"),
                fingerprint=_text_value(fields, "fingerprint"),
            )
        )
    return tuple(entries)


def dump_baseline(path: Path, entries: tuple[BaselineEntry, ...]) -> int:
    """写出域级基线（按 kind / file / domain / target / fingerprint 稳定排序）。

    Args:
        path: 基线文件路径。
        entries: 基线条目。

    Returns:
        int: 写入条目数。
    """
    ordered = sorted(entries, key=lambda entry: (entry.kind, entry.file, entry.domain, entry.target, entry.fingerprint))
    payload = {
        "version": BASELINE_VERSION,
        "entries": [
            {
                "kind": entry.kind,
                "domain": entry.domain,
                "target": entry.target,
                "file": entry.file,
                "fingerprint": entry.fingerprint,
            }
            for entry in ordered
        ],
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return len(ordered)


def load_exceptions(path: Path) -> tuple[OwnershipException, ...]:
    """载入例外白名单（缺文件返回空）。

    Args:
        path: 白名单文件路径。

    Returns:
        tuple[OwnershipException, ...]: 例外条目。

    Raises:
        ValueError: JSON 非法 / 根结构非法 / 版本非法 / 条目非法 / 访问类型非法。
    """
    data = _read_json_object(path, "例外白名单")
    if data is None:
        return ()
    if data.get("version") != BASELINE_VERSION:
        raise ValueError(f"例外白名单版本非法（应为 {BASELINE_VERSION}）")
    raw = data.get("exceptions")
    if not isinstance(raw, list):
        raise ValueError("例外白名单 exceptions 须为数组")
    entries: list[OwnershipException] = []
    for index, item in enumerate(raw, start=1):
        if not isinstance(item, dict):
            raise ValueError(f"例外登记第 {index} 条须为对象")
        fields = {str(key): value for key, value in item.items()}
        access = _text_value(fields, "access")
        if access != "read":
            raise ValueError(f"例外登记第 {index} 条 access 只允许 'read'（写侧硬禁无例外）")
        entries.append(
            OwnershipException(
                service=_text_value(fields, "service"),
                target=_text_value(fields, "target_prefix"),
                access=access,
                exit=_text_value(fields, "exit"),
                domain=str(fields.get("domain") or "").strip(),
            )
        )
    return tuple(entries)


def validate_exceptions(
    entries: tuple[OwnershipException, ...],
    *,
    registry: Registry,
    known_domains: tuple[str, ...] | None,
) -> list[str]:
    """语义校验例外登记（服务 / 域 / 目标 / 访问类型）。

    Args:
        entries: 例外条目。
        registry: 表归属 + 服务目录视图。
        known_domains: 域布局声明的域标识；None 时跳过域校验。

    Returns:
        list[str]: 违规明细。
    """
    problems: list[str] = []
    services = registry.services()
    prefixes = registry.prefixes()
    names = registry.names()
    for entry in entries:
        if entry.service not in services:
            problems.append(f"[例外登记] service 未在服务目录：{entry.service}")
        if entry.domain and known_domains is not None and entry.domain not in known_domains:
            problems.append(f"[例外登记] domain 未在业务域清单：{entry.domain}")
        if entry.target not in names and entry.target not in prefixes:
            problems.append(f"[例外登记] 目标既非已登记表名也非已知前缀：{entry.target}")
    return problems


def exception_allows(
    entries: tuple[OwnershipException, ...],
    *,
    service: str,
    table: str,
    operation: str,
    domain: str = "",
) -> bool:
    """命中判定：消费方匹配（服务或域）且目标（表名或表前缀）匹配，操作须为只读。

    Args:
        entries: 例外条目。
        service: 消费方服务标识。
        table: 被访问表名。
        operation: 操作归类。
        domain: 消费方业务域标识（非空时按域匹配）。

    Returns:
        bool: 命中返回 True。
    """
    if operation != "read":
        return False
    prefix = table_prefix_of(table)
    return any(
        (entry.service == service or (bool(domain) and entry.domain == domain))
        and entry.access == "read"
        and entry.target in (table, prefix)
        for entry in entries
    )


def assess_table(
    registry: Registry,
    rule: ServiceBoundariesRules,
    table: str,
    *,
    service: str,
    operation: str,
    exceptions: tuple[OwnershipException, ...] = (),
    domain: str = "",
) -> Violation | None:
    """单表越界判定（按表级归属；传入 `domain` 时改按归属域判定）。

    Args:
        registry: 表归属视图。
        rule: 规则段。
        table: 被访问表名。
        service: 当前服务标识。
        operation: 操作归类。
        exceptions: 例外条目。
        domain: 当前业务域标识（空串 = 服务维判定）。

    Returns:
        Violation | None: 越界返回违规项；放行返回 None。
    """
    if domain:
        owner_domain = registry.domain_of(table)
        if owner_domain is None or owner_domain == "" or owner_domain == domain:
            return None
        if exception_allows(exceptions, service=service, table=table, operation=operation, domain=domain):
            return None
        return Violation(table=table, owner=registry.owner_of(table) or "", owner_domain=owner_domain)
    owner = registry.owner_of(table)
    if owner is None or owner in (rule.owner_every_service, service):
        return None
    if exception_allows(exceptions, service=service, table=table, operation=operation):
        return None
    return Violation(table=table, owner=owner, owner_domain=registry.domain_of(table) or "")


# ---------- 源码扫描 ----------


def discover_packages(cfg: Config, rule: ServiceBoundariesRules) -> dict[str, Package]:
    """发现工作区包（包名 → 包信息）。

    Args:
        cfg: 仓配置。
        rule: 规则段。

    Returns:
        dict[str, Package]: 包名 → 包信息。
    """
    backend = cfg.backend.root if cfg.backend is not None else "backend"
    packages: dict[str, Package] = {}
    for group, shared in ((rule.shared_groups, True), (rule.service_groups, False)):
        for group_dir in group:
            base = cfg.root / backend / group_dir
            if not base.is_dir():
                continue
            for project in sorted(base.iterdir()):
                src = project / "src"
                if not src.is_dir():
                    continue
                for pkg in sorted(src.iterdir()):
                    if pkg.is_dir() and (pkg / "__init__.py").is_file():
                        packages[pkg.name] = Package(path=pkg, shared=shared, project=project.name)
    return packages


def iter_imports(path: Path) -> list[tuple[int, str]]:
    """解析文件并产出受管包导入（任意工作区包名开头）。

    Args:
        path: Python 源文件路径。

    Returns:
        list[tuple[int, str]]: 行号与导入模块名。
    """
    tree = ast.parse(read_text(path))
    found: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.extend((node.lineno, alias.name) for alias in node.names)
            continue
        if isinstance(node, ast.ImportFrom) and node.module:
            found.append((node.lineno, node.module))
    return found


def iter_import_candidates(path: Path) -> list[tuple[int, str]]:
    """产出文件内全部可解析导入目标候选（含 `from pkg import sub` 的 `pkg.sub` 形态）。

    Args:
        path: Python 源文件路径。

    Returns:
        list[tuple[int, str]]: 行号与候选模块名。
    """
    tree = ast.parse(read_text(path))
    found: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.extend((node.lineno, alias.name) for alias in node.names)
            continue
        if not isinstance(node, ast.ImportFrom) or not node.module:
            continue
        found.append((node.lineno, node.module))
        found.extend((node.lineno, f"{node.module}.{alias.name}") for alias in node.names)
    return found


def declared_tables(path: Path) -> list[tuple[int, str]]:
    """取文件内 `__tablename__` 表声明（委托 `table_index`；本模块保留该入口）。

    Args:
        path: 源文件路径。

    Returns:
        list[tuple[int, str]]: 行号与表名。
    """
    return table_index.declared_tables(path)


def bare_string_nodes(tree: ast.AST) -> set[int]:
    """收集裸字符串表达式常量节点 id（docstring / 属性文档等，扫描时排除）。

    Args:
        tree: 语法树。

    Returns:
        set[int]: 节点 id 集合。
    """
    ids: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant) and isinstance(node.value.value, str):
            ids.add(id(node.value))
    return ids


def foreign_key_target(node: ast.Call) -> str | None:
    """识别 `ForeignKey("tbl.col")` 目标表名。

    Args:
        node: 调用节点。

    Returns:
        str | None: 目标表名；非外键调用返回 None。
    """
    name = node.func.attr if isinstance(node.func, ast.Attribute) else getattr(node.func, "id", None)
    if name != "ForeignKey" or not node.args:
        return None
    first = node.args[0]
    if isinstance(first, ast.Constant) and isinstance(first.value, str):
        return first.value.split(".")[0].lower()
    return None


def iter_cross_reference_tables(
    root: Path,
    known_tables: tuple[str, ...],
    repo_root: Path,
    *,
    files: Sequence[Path] | None = None,
) -> list[tuple[str, int, str, str]]:
    """扫描源码的跨库表引用（原始 SQL / 外键 / 已声明表名 token）。

    Args:
        root: 包根目录（`files` 缺省时按此递归扫描）。
        known_tables: 全仓已声明表名。
        repo_root: 仓库根（相对路径用）。
        files: 指定扫描文件（增量模式用）；None 表示递归 `root`。

    Returns:
        list[tuple[str, int, str, str]]: （相对路径, 行号, 表名, 操作）。
    """
    found: dict[tuple[str, int, str, str], None] = {}
    alternatives = "|".join(re.escape(table) for table in sorted(known_tables))
    token_re = re.compile(r"(?<![A-Za-z0-9_])(" + alternatives + r")(?![A-Za-z0-9_])") if known_tables else None
    for path in sorted(root.rglob("*.py")) if files is None else files:
        if "__pycache__" in path.parts or is_test_path(path, root):
            continue
        rel = rel_posix(path, repo_root)
        tree = ast.parse(read_text(path))
        bare = bare_string_nodes(tree)
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                target = foreign_key_target(node)
                if target is not None:
                    found.setdefault((rel, node.lineno, target, "schema"), None)
                continue
            if not (isinstance(node, ast.Constant) and isinstance(node.value, str)):
                continue
            if id(node) in bare:
                continue
            value = node.value
            if operation_of(value) != "unknown" and any(hint in value.lower() for hint in SQL_HINTS):
                for ref in analyze(value):
                    found.setdefault((rel, node.lineno, ref.table, ref.operation), None)
                continue
            if token_re is not None and IDENTIFIER_LIKE.fullmatch(value.strip()):
                for match in token_re.findall(value):
                    found.setdefault((rel, node.lineno, match, "unknown"), None)
    return list(found)


def module_file_of(module: str, packages: dict[str, Package], root: Path) -> str | None:
    """模块名 → 相对仓库根的源文件路径（模块 / 包不存在返回 None）。

    Args:
        module: 模块名。
        packages: 工作区包表。
        root: 仓库根。

    Returns:
        str | None: 源文件相对路径。
    """
    top = module.split(".")[0]
    package = packages.get(top)
    if package is None:
        return None
    parts = module.split(".")[1:]
    candidates = [
        package.path.joinpath(*parts).with_suffix(".py") if parts else None,
        package.path.joinpath(*parts, "__init__.py"),
    ]
    found = next((candidate for candidate in candidates if candidate is not None and candidate.is_file()), None)
    return None if found is None else rel_posix(found, root)


def module_name_of(path: Path, cwd_root: Path) -> str:
    """源文件 → 模块名（去掉仓内目录前缀，只保留可导入的包路径）。

    Args:
        path: 源文件路径。
        cwd_root: 后端根目录（模块名的起点）。

    Returns:
        str: 模块名（`__init__.py` 取包名）。
    """
    parts = path.relative_to(cwd_root).as_posix()[: -len(".py")].split("/")
    if parts[-1] == "__init__":
        parts = parts[:-1]
    return ".".join(parts)


def run_image_of(tree: ast.Module) -> str | None:
    """取模块级 `RUN_IMAGE` 声明值。

    Args:
        tree: 语法树。

    Returns:
        str | None: 声明值；未声明或非字符串常量返回 None。
    """
    for node in tree.body:
        name, value = assignment_of(node)
        if name != "RUN_IMAGE" or value is None:
            continue
        if isinstance(value, ast.Constant) and isinstance(value.value, str):
            return value.value
        return None
    return None


# ---------- 校验主流程 ----------


class Checker:
    """一次服务边界校验的上下文（包表 / 域布局 / 基线 / 例外）。"""

    def __init__(
        self,
        cfg: Config,
        rule: ServiceBoundariesRules,
        registry: Registry,
        *,
        update_baseline: bool = False,
        changed: Sequence[str] | None = None,
        cache: bool = True,
    ) -> None:
        """初始化。

        Args:
            cfg: 仓配置。
            rule: 规则段。
            registry: 表归属 + 服务目录视图。
            update_baseline: 是否按当前扫描结果重写域级基线。
            changed: 增量模式的改动文件清单（相对仓根）；None 表示全量。
            cache: 是否启用表声明索引缓存（`--no-cache` 时关闭，权威门禁走现扫）。
        """
        self.cfg = cfg
        self.rule = rule
        self.registry = registry
        self.root = cfg.root
        self.update_baseline = update_baseline
        self.changed: frozenset[str] | None = None if changed is None else frozenset(normalize(rel) for rel in changed)
        cache_path = cfg.root / rule.table_index_cache if rule.table_index_cache and cache else None
        self.tables = TableIndex(cfg.root, cache_path)
        """表声明索引：表名跨服务唯一性须看全仓，这条全局判据也走它取数（命中缓存不读文件）。"""
        self.outcome = Outcome(counts=dict.fromkeys(rule.counters, 0))
        self.packages: dict[str, Package] = {}
        self.layout = Layout(entries=(), shared=())
        self.file_domain: dict[str, str] = {}
        self.shared_files: dict[str, None] = {}
        """域外文件集合（由域外声明展开而来，支持 glob）。"""
        self.domain_enabled = False
        self.baseline: dict[tuple[str, str, str, str, str], bool] = {}
        self.pending: list[BaselineEntry] = []
        self.exceptions: tuple[OwnershipException, ...] = ()
        self.known_tables: tuple[str, ...] = ()
        self.shared_declarations: set[tuple[str, str]] = set()

    # ---- 基础 ----

    def record(self, message: str, counter: str) -> None:
        """记录一条违规并累加分类计数。

        Args:
            message: 违规文案。
            counter: 计数键。
        """
        self.outcome.problems.append(message)
        self.outcome.counts[counter] = self.outcome.counts.get(counter, 0) + 1

    def bump(self, counter: str) -> None:
        """累加分类计数（不记违规）。

        Args:
            counter: 计数键。
        """
        self.outcome.counts[counter] = self.outcome.counts.get(counter, 0) + 1

    def scoped(self, rel: str) -> bool:
        """该文件是否落在本次扫描面内（全量恒真；增量按改动清单）。

        Args:
            rel: 相对仓库根的源文件路径。

        Returns:
            bool: 需要扫描 True。
        """
        return self.changed is None or normalize(rel) in self.changed

    def package_files(self, pkg: Package, *, skip_tests: bool) -> list[Path]:
        """列出某包待扫描的源文件（增量模式只取改动清单内、属于该包的文件）。

        **逐文件判据**（跨服务 import / 分层反向依赖 / 表声明与引用 / 跨域 import 与表引用）
        都通过本方法取扫描面：全量时等同原来的 `rglob("*.py")`，增量时按清单裁剪，
        两者对同一文件集合的判定完全一致。

        Args:
            pkg: 包信息。
            skip_tests: 是否跳过包内 `tests` 目录（与各判据原有口径一致）。

        Returns:
            list[Path]: 文件清单（按路径排序）。
        """
        if self.changed is None:
            candidates = sorted(pkg.path.rglob("*.py"))
        else:
            prefix = f"{rel_posix(pkg.path, self.root)}/"
            candidates = [self.root / rel for rel in sorted(self.changed) if rel.startswith(prefix)]
        picked: list[Path] = []
        for path in candidates:
            if "__pycache__" in path.parts or not path.is_file():
                continue
            if skip_tests and is_test_path(path, pkg.path):
                continue
            picked.append(path)
        return picked

    def service_of(self, package: str) -> str:
        """包名 → 归属服务标识。

        Args:
            package: 包名。

        Returns:
            str: 服务标识。
        """
        if package in self.rule.shared_libs:
            return self.rule.shared_lib_service or package
        prefix = self.rule.package_prefix
        if prefix and package.startswith(prefix):
            return package[len(prefix) :]
        return package

    def platform_packages(self) -> tuple[str, ...]:
        """平台工程内的包名（运行期反向依赖判定用）。

        Returns:
            tuple[str, ...]: 包名。
        """
        return tuple(name for name, pkg in self.packages.items() if pkg.project == self.rule.shared_lib_service)

    def ops_package(self) -> str:
        """运维脚本目录对应的包名。

        Returns:
            str: 包名（未配置运维目录为空串）。
        """
        return Path(self.rule.ops_dir).name if self.rule.ops_dir else ""

    def is_shared_model(self, rel: str) -> bool:
        """判定文件是否位于共享模型子包（其 `__tablename__` 归属必然散布在多个服务）。

        Args:
            rel: 相对仓库根的源文件路径。

        Returns:
            bool: 位于共享模型子包内 True。
        """
        marker = self.rule.shared_models_marker
        if not marker or marker not in f"/{rel}":
            return False
        return "/" in rel.split(marker, 1)[1]

    def subpackage(self, package: str, path: Path) -> str:
        """取源文件子包名（域子包取「域内层名」）。

        Args:
            package: 包名。
            path: 源文件路径。

        Returns:
            str: 子包名。
        """
        parts = path.relative_to(self.packages[package].path).as_posix().split("/")
        if parts[0] == self.rule.domain_subdir and len(parts) > 2:
            return parts[2]
        return parts[0] if len(parts) > 1 else package

    # ---- 校验项 ----

    def run(self) -> Outcome:
        """执行全部检查。

        **两类判据，扫描面不同**：

        - **逐文件判据**（规则 1~4、6、10 与 D1~D4）：单个文件的 AST 就足以判定，
          增量模式只扫改动文件（判定结果与文件集合外的内容无关）；
        - **全局判据**（表名 / 表前缀跨服务唯一、表归属登记对账、例外与归属双字段、ops 镜像闭包、
          域文件覆盖与布局形态、域级基线陈旧）：结论依赖全仓视图，**恒全量**——
          这类断言无法局部化，裁剪即失效（表名冲突只有看全所有声明才成立）。

        Returns:
            Outcome: 校验结果。
        """
        self.packages = discover_packages(self.cfg, self.rule)
        names = set(self.packages)
        self.load_domain_state()
        self.check_imports(names)
        self.check_tables()
        self.load_exceptions_state()
        self.check_ownership_declarations()
        self.check_cross_references()
        self.check_ops()
        self.check_runtime_dependencies()
        self.check_domain_imports()
        self.check_domain_tables()
        self.check_domain_files()
        self.check_domain_layout_shape()
        self.check_domain_ownership()
        self.finish_domain_baseline()
        self.tables.save(prune=self.changed is None)
        return self.outcome

    def load_domain_state(self) -> None:
        """载入域布局与域级基线（结构非法记违规）。"""
        try:
            self.layout = load_layout(self.root / self.rule.layout)
        except ValueError as exc:
            self.record(f"[域布局] {exc}", "domain_layout_violation")
            self.layout = Layout(entries=(), shared=())
        self.domain_enabled = bool(self.layout.domain_keys())
        try:
            entries = load_baseline(self.root / self.rule.baseline)
        except ValueError as exc:
            self.record(f"[域级基线] {exc}", "domain_layout_violation")
            entries = ()
        self.outcome.counts["domain_baseline_total"] = len(entries)
        self.baseline = {(e.kind, e.domain, e.target, e.file, e.fingerprint): False for e in entries}
        self.file_domain, problems = self.layout.resolve_files(self.root)
        self.shared_files = self.layout.shared_files(self.root)
        for message in problems:
            self.record(message, "domain_layout_violation")
        for message in self.layout.validate(self.root):
            self.record(message, "domain_layout_violation")
        for rel in self.file_domain:
            if rel in self.shared_files:
                self.record(
                    f"[域布局] {rel} 同时归属域 {self.file_domain[rel]} 与域外路径"
                    "（域与域外声明展开后重叠，须收窄其中一侧）",
                    "domain_layout_violation",
                )

    def check_imports(self, names: set[str]) -> None:
        """规则 1 ~ 4：跨服务依赖、分层反向依赖、跨包私有引用。

        Args:
            names: 工作区包名集合。
        """
        for package, pkg in self.packages.items():
            layers = self.rule.layers_shared if pkg.shared else self.rule.layers_service
            for path in self.package_files(pkg, skip_tests=False):
                rel = rel_posix(path, self.root)
                source = self.subpackage(package, path)
                for lineno, module in iter_imports(path):
                    top = module.split(".")[0]
                    if top not in names:
                        continue
                    detail = f"{rel}:{lineno} import {module}"
                    target_shared = self.packages[top].shared
                    if pkg.shared and not target_shared:
                        self.record(f"[共享库依赖服务] {detail}", "cross_service_dependency")
                    if not pkg.shared and top != package and not target_shared:
                        self.record(f"[服务互相依赖] {detail}", "cross_service_dependency")
                    parts = module.split(".")
                    target = parts[1] if len(parts) > 1 else top
                    if top == package and target in layers.get(source, ()):
                        self.record(f"[分层反向依赖] {detail}", "layer_violation")
                    if top != package and any(part.startswith("_") for part in parts[1:]):
                        self.record(f"[跨包私有引用] {detail}", "private_reference")

    def tables_of(self, pkg: Package) -> list[str]:
        """取某包声明的全部表名（保序去重）。

        逐文件走 `tables` 索引：文件没变就命中缓存、连读都不读——这条是全局判据里唯一
        随文件数线性增长的部分，也是增量模式下的耗时地板。

        Args:
            pkg: 包信息。

        Returns:
            list[str]: 表名清单（按文件路径序、包内去重）。
        """
        found: dict[str, None] = {}
        for path in sorted(pkg.path.rglob("*.py")):
            for _lineno, table in self.tables.declarations(path):
                found.setdefault(table, None)
        return list(found)

    def check_tables(self) -> None:
        """规则 5：表名 / 前缀跨服务唯一 + 声明的表名须在归属登记中。"""
        owners: dict[str, str] = {}
        prefixes: dict[str, list[str]] = {}
        known: dict[str, None] = {}
        registered = self.registry.names()
        for package, pkg in self.packages.items():
            declared = self.tables_of(pkg)
            for table in declared:
                known.setdefault(table, None)
            if pkg.shared:
                continue
            self.outcome.service_tables[package] = declared
            for table in declared:
                owner = owners.get(table)
                if owner is not None and owner != package:
                    self.record(f"[表名跨服务重复] {table}：{owner} / {package}", "table_name_conflict")
                else:
                    owners[table] = package
                bucket = prefixes.setdefault(table_prefix_of(table), [])
                if package not in bucket:
                    bucket.append(package)
        for prefix, prefix_owners in sorted(prefixes.items()):
            if prefix not in self.rule.shared_table_prefixes and len(prefix_owners) > 1:
                self.record(f"[表前缀跨服务重复] {prefix}：{'、'.join(sorted(prefix_owners))}", "table_prefix_conflict")
        for package, declared in self.outcome.service_tables.items():
            for table in declared:
                if table not in registered:
                    self.record(
                        f"[表归属未登记] {package} 声明 {table}（表归属登记无此表）", "table_ownership_violation"
                    )
        self.known_tables = tuple(known)

    def load_exceptions_state(self) -> None:
        """规则 7：例外白名单核对（命中放行由规则 6 / D3 使用）。"""
        try:
            self.exceptions = load_exceptions(self.root / self.rule.exceptions)
        except ValueError as exc:
            self.record(f"[例外登记] {exc}", "table_ownership_violation")
            self.exceptions = ()
        domains = self.layout.domain_keys() if self.domain_enabled else None
        for message in validate_exceptions(self.exceptions, registry=self.registry, known_domains=domains):
            self.record(message, "table_ownership_violation")
        self.outcome.counts["registered_exceptions_total"] = len(self.exceptions)

    def check_ownership_declarations(self) -> None:
        """规则 6①：声明越权（`__tablename__` 归属他服务；未登记表名同拦）。"""
        shared: set[tuple[str, str]] = set()
        registered = self.registry.names()
        for package, pkg in self.packages.items():
            service = self.service_of(package)
            for path in self.package_files(pkg, skip_tests=True):
                rel = rel_posix(path, self.root)
                for lineno, table in self.tables.declarations(path):
                    if table not in registered:
                        self.record(
                            f"[表归属未登记] {rel}:{lineno} 声明 {table}（表归属登记无此表）",
                            "table_ownership_violation",
                        )
                        continue
                    if self.is_shared_model(rel):
                        shared.add((rel, table))
                        continue
                    violation = assess_table(self.registry, self.rule, table, service=service, operation="declare")
                    if violation is not None:
                        self.record(
                            f"[跨服务表归属] {rel}:{lineno} 声明 {table}（归属 {violation.owner}）",
                            "table_ownership_violation",
                        )
        self.shared_declarations = shared

    def check_cross_references(self) -> None:
        """规则 6②：引用越界（原始 SQL / 外键 / 已知表名 token；未登记表名无归属可判、放行）。"""
        for package, pkg in self.packages.items():
            service = self.service_of(package)
            seen: set[tuple[str, int, str]] = set()
            files = self.package_files(pkg, skip_tests=True)
            for rel, lineno, table, operation in iter_cross_reference_tables(
                pkg.path, self.known_tables, self.root, files=files
            ):
                if any(rel.endswith(suffix) for suffix in self.rule.exempt_modules):
                    continue
                if self.is_shared_model(rel) and (rel, table) in self.shared_declarations:
                    continue
                key = (rel, lineno, table)
                if key in seen:
                    continue
                seen.add(key)
                violation = assess_table(
                    self.registry, self.rule, table, service=service, operation=operation, exceptions=self.exceptions
                )
                if violation is None:
                    bare = assess_table(self.registry, self.rule, table, service=service, operation=operation)
                    if bare is not None:
                        self.bump("registered_exceptions_used")
                    continue
                self.record(
                    f"[跨服务库访问] {rel}:{lineno} {table}（归属 {violation.owner}，操作 {operation}）",
                    "cross_service_access_violation",
                )

    def scan_ops(self) -> tuple[dict[str, set[str]], dict[str, str | None]]:
        """扫描运维脚本目录：各模块导入面与 `RUN_IMAGE` 声明。

        Returns:
            tuple[dict[str, set[str]], dict[str, str | None]]: （模块 → 导入模块集合, 模块 → 声明值）。
        """
        imports: dict[str, set[str]] = {}
        run_images: dict[str, str | None] = {}
        ops_dir = self.root / self.rule.ops_dir if self.rule.ops_dir else None
        if ops_dir is None or not ops_dir.is_dir():
            return imports, run_images
        backend = self.cfg.root / (self.cfg.backend.root if self.cfg.backend is not None else "backend")
        for path in sorted(ops_dir.glob("*.py")):
            if path.name == "__init__.py":
                continue
            module = module_name_of(path, backend)
            imports[module] = {name for _lineno, name in iter_imports(path)}
            run_images[module] = run_image_of(ast.parse(read_text(path)))
        return imports, run_images

    def ops_closure(self, script: str, imports: dict[str, set[str]]) -> set[str]:
        """计算脚本的传递导入闭包（含运维脚本内部互导）。

        Args:
            script: 脚本模块名。
            imports: 模块 → 导入模块集合。

        Returns:
            set[str]: 闭包内全部被导入模块名。
        """
        seen: set[str] = set()
        pending = list(imports.get(script, set()))
        while pending:
            module = pending.pop()
            if module in seen:
                continue
            seen.add(module)
            pending.extend(imports.get(module, set()))
        return seen

    def needed_packages(self, closure: set[str]) -> set[str]:
        """从闭包取所需服务包。

        Args:
            closure: 导入闭包。

        Returns:
            set[str]: 所需服务包名。
        """
        return {module.split(".")[0] for module in closure if module.split(".")[0] in self.rule.service_packages}

    def dockerfile_packages(self) -> tuple[set[str], list[str]]:
        """解析镜像构建文件的 COPY 面，推导镜像所含工作区包。

        Returns:
            tuple[set[str], list[str]]: （固定包集合, 问题列表）。
        """
        packages: set[str] = set()
        issues: list[str] = []
        path = self.root / self.rule.ops_dockerfile if self.rule.ops_dockerfile else None
        if path is None or not path.is_file():
            issues.append(f"[镜像包集合] 缺 {self.rule.ops_dockerfile}（无法断言镜像 COPY 面）")
            return packages, issues
        backend = self.rule_backend()
        ops_rel = self.rule.ops_dir.removeprefix(f"{backend}/")
        parameterized = False
        shared_copied = False
        ops_copied = False
        for line in read_text(path).splitlines():
            stripped = line.strip()
            if not stripped.upper().startswith("COPY "):
                continue
            parts = stripped.split()
            if len(parts) < 2:  # pragma: no cover - COPY 行必有源
                continue
            source = normalize(parts[1])
            if "${" in source:
                parameterized = True
                continue
            if source == ops_rel:
                ops_copied = True
                packages.add(self.ops_package())
                continue
            if not source.endswith("/src"):
                continue
            target = (self.root / backend / source).resolve()
            for name, pkg in self.packages.items():
                if pkg.path.parent.resolve() == target:
                    packages.add(name)
                    if pkg.shared:
                        shared_copied = True
        if not shared_copied:
            issues.append("[镜像包集合] 镜像构建文件未 COPY 共享库源码（镜像不含基座）")
        if not ops_copied:
            issues.append("[镜像包集合] 镜像构建文件未 COPY 运维脚本目录（运维脚本不在镜像内）")
        if not parameterized:
            issues.append("[镜像包集合] 镜像构建文件未按 `${...}` 参数化 COPY 目标服务源码")
        return packages, issues

    def rule_backend(self) -> str:
        """后端根目录（相对仓根）。

        Returns:
            str: 后端根目录。
        """
        return self.cfg.backend.root if self.cfg.backend is not None else "backend"

    def check_ops(self) -> None:
        """规则 8 / 9：`ops` 运行镜像声明与导入闭包。"""
        imports, run_images = self.scan_ops()
        values = self.rule.run_image_values
        for module in sorted(run_images):
            declared = run_images[module]
            needed = self.needed_packages(self.ops_closure(module, imports))
            if declared is None:
                if needed:
                    self.record(
                        f"[运行镜像未声明] {module} 跨包导入 {sorted(needed)} 但未声明 RUN_IMAGE"
                        f"（合法取值：{' / '.join(values)}）",
                        "ops_run_image_missing",
                    )
                continue
            if declared not in values:
                self.record(
                    f"[运行镜像非法] {module} 声明 RUN_IMAGE = {declared!r}（合法取值：{' / '.join(values)}）",
                    "ops_run_image_missing",
                )
        docker_packages, docker_issues = self.dockerfile_packages()
        for message in docker_issues:
            self.record(message, "ops_image_packages_drift")
        for image, declared_packages in self.rule.image_packages.items():
            if docker_packages - set(declared_packages):
                self.record(
                    f"[镜像包集合漂移] 声明 {image} 含 {sorted(declared_packages)}，"
                    f"但镜像构建文件实际含 {sorted(docker_packages)}",
                    "ops_image_packages_drift",
                )
                break
        for module in sorted(run_images):
            declared = run_images[module]
            if declared is None or declared not in values:
                continue
            needed = self.needed_packages(self.ops_closure(module, imports))
            if not needed:
                continue
            allowed = set(self.rule.image_packages.get(declared, ()))
            missing = needed - allowed
            if not missing:
                continue
            if declared == "*":
                self.record(
                    f"[导入闭包越界] {module} 声明 RUN_IMAGE = '*'（仅含基座包），但闭包需要服务包 {sorted(needed)}",
                    "ops_import_closure_violation",
                )
                continue
            self.record(
                f"[导入闭包越界] {module} 声明 RUN_IMAGE = {declared!r}（含 {sorted(allowed)}），"
                f"但闭包需要 {sorted(missing)}",
                "ops_import_closure_violation",
            )

    def check_runtime_dependencies(self) -> None:
        """规则 10：运行期代码不得依赖运维通道 / 平台包。"""
        if not self.rule.service_groups:
            return
        ops_package = self.ops_package()
        platform = set(self.platform_packages())
        for group_dir in self.rule.service_groups:
            services_dir = self.root / self.rule_backend() / group_dir
            if not services_dir.is_dir():
                continue
            for project in sorted(services_dir.iterdir()):
                src = project / "src"
                if not src.is_dir():
                    continue
                is_platform = project.name == self.rule.shared_lib_service
                for path in sorted(src.rglob("*.py")):
                    if "__pycache__" in path.parts:
                        continue
                    rel = rel_posix(path, self.root)
                    if not self.scoped(rel):
                        continue
                    for lineno, module in iter_imports(path):
                        top = module.split(".")[0]
                        if ops_package and top == ops_package:
                            self.record(
                                f"[运行期依赖运维通道] {rel}:{lineno} import {module}"
                                f"（服务运行期代码不得导入 {ops_package}；种子 / 编排归基座）",
                                "runtime_ops_dependency",
                            )
                        elif top in platform and not is_platform:
                            self.record(
                                f"[运行期跨服务依赖] {rel}:{lineno} import {module}"
                                f"（服务 {project.name} 不得导入平台包 {top}）",
                                "runtime_cross_service_dependency",
                            )

    # ---- 域级断言 ----

    def domain_issue(
        self,
        message: str,
        *,
        kind: str,
        domain: str,
        target: str,
        rel: str,
        line: str,
        counter: str,
    ) -> None:
        """处置一条域级越界：基线命中即放行计数；`--update-baseline` 收集；否则记违规。

        Args:
            message: 违规文案。
            kind: 基线条目种类。
            domain: 源业务域。
            target: 目标业务域。
            rel: 源文件相对路径。
            line: 触发越界的源行文本（指纹用）。
            counter: 计数键。
        """
        digest = fingerprint(line)
        key = (kind, domain, target, rel, digest)
        if key in self.baseline:
            if not self.baseline[key]:
                self.baseline[key] = True
                self.bump("domain_baseline_used")
            return
        if self.update_baseline:
            self.pending.append(BaselineEntry(kind=kind, domain=domain, target=target, file=rel, fingerprint=digest))
            return
        self.record(message, counter)

    def check_domain_imports(self) -> None:
        """D1 / D2：跨域 import 断言（同包内 + 共享库模型）。"""
        if not self.domain_enabled:
            return
        for package, pkg in self.packages.items():
            for path in self.package_files(pkg, skip_tests=True):
                rel = rel_posix(path, self.root)
                source_domain = self.file_domain.get(rel)
                if source_domain is None:
                    continue
                lines = read_text(path).splitlines()
                for lineno, module in iter_import_candidates(path):
                    top = module.split(".")[0]
                    target_rel = module_file_of(module, self.packages, self.root)
                    if target_rel is None:
                        continue
                    if top != package and self.rule.shared_models_marker not in f"/{target_rel}":
                        continue
                    target_domain = self.file_domain.get(target_rel)
                    if target_domain is None or target_domain == source_domain:
                        continue
                    if module in self.layout.public_api_of(target_domain):
                        continue
                    line = lines[lineno - 1] if 0 < lineno <= len(lines) else module
                    self.domain_issue(
                        f"[跨域 import] {rel}:{lineno} {source_domain} → {target_domain}（{module}）",
                        kind="import",
                        domain=source_domain,
                        target=target_domain,
                        rel=rel,
                        line=line,
                        counter="cross_domain_import",
                    )

    def check_domain_tables(self) -> None:
        """D3 / D4 / 域实现登记：跨域表声明与跨域表引用（写操作归 D4、读侧例外可放行）。"""
        if not self.domain_enabled:
            return
        declared_by_file: dict[str, set[str]] = {}
        scanned: dict[Path, list[Path]] = {}
        for _package, pkg in self.packages.items():
            files = self.package_files(pkg, skip_tests=True)
            scanned[pkg.path] = files
            for path in files:
                rel = rel_posix(path, self.root)
                declared = self.check_table_declarations(rel, read_text(path).splitlines())
                if declared:
                    declared_by_file[rel] = declared
        for package, pkg in self.packages.items():
            service = self.service_of(package)
            seen: set[tuple[str, int, str]] = set()
            for rel, lineno, table, operation in iter_cross_reference_tables(
                pkg.path, self.known_tables, self.root, files=scanned[pkg.path]
            ):
                if any(rel.endswith(suffix) for suffix in self.rule.exempt_modules):
                    continue
                source_domain = self.file_domain.get(rel)
                target_domain = self.registry.domain_of(table)
                if source_domain is None or not target_domain or target_domain == source_domain:
                    continue
                if table in declared_by_file.get(rel, set()):
                    continue
                if assess_table(self.registry, self.rule, table, service=service, operation=operation) is not None:
                    continue
                key = (rel, lineno, table)
                if key in seen:
                    continue
                seen.add(key)
                line = self.line_of(rel, lineno, table)
                if operation in WRITE_OPERATIONS:
                    self.domain_issue(
                        f"[跨域写] {rel}:{lineno} {source_domain} 写 {table}（归属域 {target_domain}，"
                        f"操作 {operation}；跨域写须走能力契约或事件）",
                        kind="table_ref",
                        domain=source_domain,
                        target=target_domain,
                        rel=rel,
                        line=line,
                        counter="cross_domain_write",
                    )
                    continue
                if exception_allows(
                    self.exceptions, service=service, table=table, operation=operation, domain=source_domain
                ):
                    self.bump("registered_exceptions_used")
                    continue
                self.domain_issue(
                    f"[跨域表引用] {rel}:{lineno} {source_domain} 引用 {table}"
                    f"（归属域 {target_domain}，操作 {operation}）",
                    kind="table_ref",
                    domain=source_domain,
                    target=target_domain,
                    rel=rel,
                    line=line,
                    counter="cross_domain_table_reference",
                )

    def check_table_declarations(self, rel: str, lines: list[str]) -> set[str]:
        """表声明侧：声明他域表（D3 债务）与「声明了已登记表却未归域」的域实现。

        Args:
            rel: 源文件相对路径。
            lines: 源文件行（指纹用）。

        Returns:
            set[str]: 本文件声明的表名集合。
        """
        declared: set[str] = set()
        if not any("__tablename__" in line for line in lines):
            return declared
        for lineno, table in self.tables.declarations(self.root / rel):
            declared.add(table)
            target_domain = self.registry.domain_of(table)
            if not target_domain:
                continue
            source_domain = self.file_domain.get(rel)
            if source_domain is None:
                self.record(
                    f"[域实现未登记] {rel}:{lineno} 声明 {table}（归属域 {target_domain}；须在域布局登记该文件）",
                    "domain_layout_violation",
                )
                continue
            if target_domain == source_domain:
                continue
            self.domain_issue(
                f"[跨域表声明] {rel}:{lineno} {source_domain} 声明 {table}（归属域 {target_domain}）",
                kind="table_declare",
                domain=source_domain,
                target=target_domain,
                rel=rel,
                line=lines[lineno - 1] if 0 < lineno <= len(lines) else table,
                counter="cross_domain_table_declare",
            )
        return declared

    def line_of(self, rel: str, lineno: int, fallback: str) -> str:
        """取源文件指定行文本（越界时用表名兜底）。

        Args:
            rel: 源文件相对路径。
            lineno: 行号（1 起）。
            fallback: 兜底文本。

        Returns:
            str: 行文本。
        """
        lines = read_text(self.root / rel).splitlines()
        return lines[lineno - 1] if 0 < lineno <= len(lines) else fallback

    def check_domain_files(self) -> None:
        """域实现覆盖：登记文件数 / 未归属域的实现文件数（硬门禁）。"""
        if not self.domain_enabled:
            self.outcome.counts["domain_files_registered"] = 0
            self.outcome.counts["domain_files_unregistered"] = 0
            return
        registered = 0
        unregistered: list[str] = []
        for _package, pkg in self.packages.items():
            for path in sorted(pkg.path.rglob("*.py")):
                if "__pycache__" in path.parts or is_test_path(path, pkg.path):
                    continue
                rel = rel_posix(path, self.root)
                if pkg.shared and self.rule.shared_models_marker not in f"/{rel}":
                    continue
                if rel in self.file_domain:
                    registered += 1
                elif rel not in self.shared_files:
                    unregistered.append(rel)
        self.outcome.counts["domain_files_registered"] = registered
        self.outcome.counts["domain_files_unregistered"] = len(unregistered)
        for rel in unregistered:
            self.record(
                f"[域实现未登记] {rel}（既不在任何域 paths，也不在 shared_paths；分包后不允许未归属）",
                "domain_layout_violation",
            )

    def check_domain_layout_shape(self) -> None:
        """`placement` 形态校验：域子包目录 / 唯一公共出口 / 模型单文件例外。"""
        if not self.domain_enabled:
            return
        for entry in self.layout.entries:
            if entry.status != PLACEMENT:
                continue
            expected = f"/{self.rule.domain_subdir}/{entry.domain_key}/"
            for declared in entry.paths:
                if expected in f"/{declared}":
                    continue
                if declared.endswith(".py") and self.is_model_module(declared):
                    continue
                self.record(
                    f"[域布局形态] {entry.domain_key} 的路径既不是域子包目录（{expected}）也不是模型单文件：{declared}",
                    "domain_layout_violation",
                )
            if len(entry.public_api) != 1:
                self.record(
                    f"[域布局形态] {entry.domain_key} 的 public_api 须恰好 1 个（现 {len(entry.public_api)} 个）",
                    "domain_layout_violation",
                )
                continue
            module = entry.public_api[0]
            target = module_file_of(module, self.packages, self.root)
            if target is None:
                self.record(
                    f"[域布局形态] {entry.domain_key} 的公共出口模块不存在：{module}",
                    "domain_layout_violation",
                )
                continue
            if expected not in f"/{target}":
                self.record(
                    f"[域布局形态] {entry.domain_key} 的公共出口不在该域目录内：{module} → {target}",
                    "domain_layout_violation",
                )

    def is_model_module(self, rel: str) -> bool:
        """文件是否声明 ORM 表（`__tablename__` 常量赋值即视为模型模块）。

        Args:
            rel: 相对仓库根 posix 路径。

        Returns:
            bool: 声明表返回 True。
        """
        try:
            return bool(self.tables.declarations(self.root / rel))
        except (OSError, SyntaxError):
            return False

    def check_domain_ownership(self) -> None:
        """表归属双字段一致性（`owner` == 域承载服务；基础设施表与不归域表 `domain` 恒空）。"""
        domain_services = self.layout.domain_services() if self.domain_enabled else None
        for message in validate_ownership(self.registry, self.rule, domain_services):
            self.record(f"[表归属域] {message}", "domain_ownership_violation")

    def finish_domain_baseline(self) -> None:
        """基线与双字段收尾：陈旧基线条目即失败；`--update-baseline` 时重写基线。

        增量模式只判**扫描面内文件**的陈旧条目：未扫文件本轮未命中是必然的（不是「已修复」），
        一并判陈旧会把全量口径的结论误报成失败。已删除文件仍在清单内，故其条目照常判陈旧。
        """
        if not self.domain_enabled:
            return
        stale = [key for key, used in self.baseline.items() if not used and self.scoped(key[3])]
        if self.update_baseline:
            merged = [
                BaselineEntry(kind=key[0], domain=key[1], target=key[2], file=key[3], fingerprint=key[4])
                for key, used in self.baseline.items()
                if used
            ]
            merged.extend(self.pending)
            written = dump_baseline(self.root / self.rule.baseline, tuple(merged))
            self.outcome.counts["domain_baseline_total"] = written
            return
        for key in stale:
            self.record(
                f"[域级基线陈旧] {key[0]} {key[1]} → {key[2]}（{key[3]}，指纹 {key[4]}）已不再命中，"
                "基线只减不增，请从基线删除该条",
                "domain_layout_violation",
            )


def validate_ownership(
    registry: Registry,
    rule: ServiceBoundariesRules,
    domain_services: dict[str, str] | None,
) -> list[str]:
    """校验归属登记：表名形态与唯一 + 归属服务 + 归属业务域 + 前缀归属。

    Args:
        registry: 表归属视图。
        rule: 规则段。
        domain_services: 业务域 → 承载服务映射（None 时不做成员校验）。

    Returns:
        list[str]: 非法明细。
    """
    errors: list[str] = []
    services = registry.services()
    service_prefixes = registry.service_prefixes()
    no_domain = set(rule.no_domain_tables)
    for record in registry.tables:
        name = record.table_name
        if not TABLE_NAME_RE.match(name):
            errors.append(f"{name}：表名非法")
        if record.owner != rule.owner_every_service and record.owner not in services:
            errors.append(f"{name}：归属服务未登记（{record.owner}）")
        errors.extend(_validate_domain(record, rule, domain_services, no_domain))
        errors.extend(_validate_prefix(record, rule, service_prefixes))
    counted: dict[str, int] = {}
    for record in registry.tables:
        counted[record.table_name] = counted.get(record.table_name, 0) + 1
    for name in dict.fromkeys(record.table_name for record in registry.tables):
        if counted[name] > 1:
            errors.append(f"表名重复登记：{name}")
    return errors


def _validate_domain(
    record: TableRecord,
    rule: ServiceBoundariesRules,
    domain_services: dict[str, str] | None,
    no_domain: set[str],
) -> list[str]:
    """校验归属业务域：不归域范围 / 标识形态 / 已登记 / 与 `owner` 一致。

    Args:
        record: 归属记录。
        rule: 规则段。
        domain_services: 业务域 → 承载服务映射。
        no_domain: 不归业务域的表。

    Returns:
        list[str]: 非法明细。
    """
    name = record.table_name
    if record.owner == rule.owner_every_service or name in no_domain:
        if record.domain:
            return [f"{name}：基础设施表 / 不归域表不得登记业务域（现 {record.domain}）"]
        return []
    if not record.domain:
        return [f"{name}：未登记归属业务域"]
    if not TABLE_NAME_RE.match(record.domain):
        return [f"{name}：归属业务域标识非法（{record.domain}）"]
    if domain_services is None:
        return []
    carrier = domain_services.get(record.domain)
    if carrier is None:
        return [f"{name}：归属业务域未登记（{record.domain}）"]
    if carrier != record.owner:
        return [f"{name}：归属业务域 {record.domain} 由 {carrier} 承载，与归属服务 {record.owner} 不一致"]
    return []


def _validate_prefix(
    record: TableRecord,
    rule: ServiceBoundariesRules,
    service_prefixes: dict[str, tuple[str, ...]],
) -> list[str]:
    """校验表名前缀归属（共享前缀与无前缀表跳过）。

    Args:
        record: 归属记录。
        rule: 规则段。
        service_prefixes: 服务标识 → 登记前缀。

    Returns:
        list[str]: 非法明细。
    """
    name = record.table_name
    if "_" not in name or any(name.startswith(prefix) for prefix in rule.shared_table_prefixes):
        return []
    prefix = table_prefix_of(name)
    allowed = service_prefixes.get(record.owner, ())
    if prefix not in allowed:
        return [f"{name}：表前缀 {prefix} 不属于归属服务 {record.owner}（已登记前缀 {sorted(allowed) or '无'}）"]
    return []


# ---------- 入口 ----------


def check(
    cfg: Config,
    rule: ServiceBoundariesRules,
    *,
    registry: Registry | None = None,
    update_baseline: bool = False,
    changed: Sequence[str] | None = None,
    cache: bool = True,
) -> Outcome:
    """执行全部检查（登记源可注入，供自检使用）。

    Args:
        cfg: 仓配置。
        rule: 规则段。
        registry: 表归属视图（缺省从登记源解析）。
        update_baseline: 是否按当前扫描结果重写域级基线。
        changed: 增量模式的改动文件清单（相对仓根）；None 表示全量。
        cache: 是否启用表声明索引缓存。

    Returns:
        Outcome: 校验结果。
    """
    view = registry if registry is not None else load_registry(cfg, rule)
    return Checker(cfg, rule, view, update_baseline=update_baseline, changed=changed, cache=cache).run()


def emit(outcome: Outcome, *, as_json: bool) -> None:
    """输出检查结果（文本或 JSON）。

    Args:
        outcome: 校验结果。
        as_json: 是否输出 JSON（供度量脚本解析）。
    """
    if as_json:
        payload = {"passed": not outcome.problems, "counts": dict(outcome.counts), "problems": list(outcome.problems)}
        out(json.dumps(payload, ensure_ascii=False, indent=2))
        return
    if outcome.problems:
        out(f"\n[check_service_boundaries] 不通过：{len(outcome.problems)} 项")
        for problem in outcome.problems:
            out("  " + problem)
        return
    out("[check_service_boundaries] 通过：共享库 / 服务边界、分层依赖单向、表 / 表前缀跨服务唯一、跨服务库访问硬校验。")
    for package, tables in sorted(outcome.service_tables.items()):
        listing = "、".join(f"{table}（{table_prefix_of(table)}）" for table in tables) or "无表声明"
        out(f"  - {package}：{listing}")
    out(
        f"  例外白名单：登记 {outcome.counts.get('registered_exceptions_total', 0)} 条、"
        f"命中 {outcome.counts.get('registered_exceptions_used', 0)} 条。"
    )
    registered_files = outcome.counts.get("domain_files_registered", 0)
    unregistered_files = outcome.counts.get("domain_files_unregistered", 0)
    domain_scope = (
        f"域布局登记文件 {registered_files} / 未归属域 {unregistered_files}"
        if registered_files or unregistered_files
        else "域布局未提供（域级断言停用）"
    )
    out(
        f"  域级断言：{domain_scope}；跨域 import {outcome.counts.get('cross_domain_import', 0)}、"
        f"表引用 {outcome.counts.get('cross_domain_table_reference', 0)}、"
        f"写 {outcome.counts.get('cross_domain_write', 0)}、"
        f"表声明 {outcome.counts.get('cross_domain_table_declare', 0)}；"
        f"域级基线 {outcome.counts.get('domain_baseline_total', 0)} 条"
        f"（命中 {outcome.counts.get('domain_baseline_used', 0)} 条）。"
    )


# ---------- 增量扫描 ----------


@lru_cache(maxsize=64)
def glob_regex(pattern: str) -> str:
    """把仓侧 glob 编译成正则（`**` 跨目录、`*` 不跨目录、`?` 单字符）。

    Args:
        pattern: glob 模式（`/` 分隔）。

    Returns:
        str: 可用于 `re.fullmatch` 的正则。
    """
    parts: list[str] = []
    index = 0
    while index < len(pattern):
        char = pattern[index]
        if char == "*":
            if pattern.startswith("**/", index):
                parts.append("(?:.*/)?")
                index += 3
                continue
            if pattern.startswith("**", index):
                parts.append(".*")
                index += 2
                continue
            parts.append("[^/]*")
            index += 1
            continue
        if char == "?":
            parts.append("[^/]")
            index += 1
            continue
        parts.append(re.escape(char))
        index += 1
    return "".join(parts)


def matches_glob(pattern: str, rel: str) -> bool:
    """相对路径是否命中 glob 模式。

    Args:
        pattern: glob 模式。
        rel: 相对仓根路径（`/` 分隔）。

    Returns:
        bool: 命中 True。
    """
    return re.fullmatch(glob_regex(pattern), normalize(rel)) is not None


def rule_trigger_of(rule: ServiceBoundariesRules, rel: str) -> str | None:
    """改动是否触碰「判据自身的数据源」（布局 / 基线 / 例外 / 归属登记 / 目录登记）。

    这些文件一变，**未改动文件**的判定结论也可能跟着变（例如域布局新增路径后，原本无归属的文件
    变成有归属；表归属登记改归属后，另一服务的声明从合法变越权），故必须升为全量。

    Args:
        rule: 规则段。
        rel: 相对仓根路径。

    Returns:
        str | None: 命中项的中文名；未命中返回 None。
    """
    path = normalize(rel)
    for declared, label in (
        (rule.layout, "域布局"),
        (rule.baseline, "域级基线"),
        (rule.exceptions, "读侧例外"),
    ):
        if declared and normalize(declared) == path:
            return label
    stem = Path(path).stem
    for module, label in ((rule.ownership_module, "表归属登记"), (rule.catalog_module, "服务目录登记")):
        # 登记源既可能是点分模块名（`<pkg>.services.table_registry`），也可能是相对路径
        # （`pkg/tables.py`）；两种形态都归一成「末段名」再比对，宁可多触发全量也不漏。
        if not module:
            continue
        last = Path(module).stem if module.endswith(".py") else module.rsplit(".", 1)[-1]
        if last == stem:
            return label
    return None


def full_scan_reason(cfg: Config, rule: ServiceBoundariesRules, changed: Sequence[str]) -> str | None:
    """判断增量清单是否必须升为全量扫描。

    三条触发：①清单内有已删除文件（结构变化）；②触碰判据数据源（见 `rule_trigger_of`）；
    ③命中仓侧 `incremental_full_when`（补足仓特有的生成物与清单目录）。

    Args:
        cfg: 仓配置。
        rule: 规则段。
        changed: 改动文件清单。

    Returns:
        str | None: 升级原因；None 表示可按增量执行。
    """
    for rel in changed:
        if not (cfg.root / rel).is_file():
            return f"清单内文件已删除或不在工作树：{rel}"
        label = rule_trigger_of(rule, rel)
        if label is not None:
            return f"改动触碰{label}：{rel}"
        for pattern in rule.incremental_full_when:
            if matches_glob(pattern, rel):
                return f"改动命中全量触发面「{pattern}」：{rel}"
    return None


def read_changed(cfg: Config, files_from: str) -> list[str] | None:
    """读取增量清单（空串表示全量，返回 None；不可读返回 None 并由调用方判为参数错误）。

    Args:
        cfg: 仓配置。
        files_from: 清单路径（可为相对仓根）。

    Returns:
        list[str] | None: 改动文件清单；全量模式返回 None。
    """
    if not files_from:
        return None
    path = Path(files_from)
    if not path.is_absolute():
        path = cfg.root / files_from
    return read_file_list(path)


def run(
    cfg: Config,
    *,
    json_output: bool,
    update_baseline: bool,
    as_self_test: bool,
    files_from: str = "",
    cache: bool = True,
) -> int:
    """执行服务边界校验（可限定为改动文件清单 = 增量模式）。

    增量只作用于**逐文件判据**；全局判据（跨服务表名 / 前缀唯一、归属登记对账、ops 镜像闭包、
    域文件覆盖与布局形态、基线陈旧）恒全量。三条触发会把整个校验升为全量，见 `full_scan_reason`。

    Args:
        cfg: 仓配置。
        json_output: 是否 JSON 输出。
        update_baseline: 是否按当前扫描结果重写域级基线（**不得与增量同时使用**）。
        as_self_test: 是否只跑内置自检。
        files_from: 改动文件清单路径；空串表示全量。
        cache: 是否启用表声明索引缓存（`--no-cache` 关掉，权威门禁走现扫）。

    Returns:
        int: 退出码（0 通过 / 1 违规或登记源不可解析 / 2 规则未登记、登记源缺失或参数冲突）。
    """
    try:
        rule = require(cfg.boundaries_rules, "service_boundaries", MIN_KEYS)
    except RulesError as exc:
        out(f"[check_service_boundaries] {exc}")
        return 2
    if as_self_test:
        return self_test()
    if update_baseline and files_from:
        out("[check_service_boundaries] 基线必须按全量重写：`--update-baseline` 不接受增量清单")
        return 2
    changed: Sequence[str] | None = None
    if files_from:
        listed = read_changed(cfg, files_from)
        if listed is None:
            return 2
        reason = full_scan_reason(cfg, rule, listed)
        if reason is not None:
            out(f"[check_service_boundaries] 升为全量：{reason}")
        else:
            changed = listed
            out(
                f"[check_service_boundaries] 增量模式：{len(listed)} 个改动文件（逐文件判据）；"
                "全局判据（表名唯一 / 归属对账 / 镜像闭包 / 域覆盖 / 基线陈旧）恒全量"
            )
    try:
        outcome = check(cfg, rule, update_baseline=update_baseline, changed=changed, cache=cache)
    except RulesError as exc:
        out(f"[check_service_boundaries] {exc}")
        return 2
    except RegistryError as exc:
        out(f"[check_service_boundaries] {exc}")
        return 1
    if changed is not None:
        outcome.counts["incremental_files"] = len(changed)
    emit(outcome, as_json=json_output)
    return 1 if outcome.problems else 0


# ---------- 自检 ----------


SAMPLE_TABLES = '''"""自检夹具：表归属登记（与仓内登记同构的 AST 形态）。"""


class Datasource(StrEnum):
    PLATFORM = "platform"
    TENANT = "tenant"
    BOTH = "both"


EVERY = "*"

TABLE_OWNERSHIP = stable(
    (
        rec(table_name="sys_alpha", owner="alpha", domain="alpha", datasource=Datasource.PLATFORM),
        rec(table_name="sys_icon", owner="alpha", domain="alpha", datasource=Datasource.PLATFORM),
        rec(table_name="sys_outbox", owner=EVERY, domain="", datasource=Datasource.BOTH),
        rec(table_name="beta_log", owner="beta", domain="beta", datasource=Datasource.TENANT),
        rec(table_name="beta_second", owner="beta", domain="beta", datasource=Datasource.TENANT),
        rec(table_name="demo", owner="platform", domain="", datasource=Datasource.TENANT),
    )
)
'''
"""自检夹具的表归属登记模块。"""

SAMPLE_CATALOG = '''"""自检夹具：服务目录登记。"""

SERVICE_CATALOG = stable(
    (
        rec(module_key="sys", service_key="platform", table_prefix="sys_"),
        rec(module_key="alpha", service_key="alpha", table_prefix="alpha_"),
        rec(module_key="beta", service_key="beta", table_prefix="beta_"),
    )
)
'''
"""自检夹具的服务目录模块。"""

SAMPLE_DOCKERFILE = (
    "COPY libs/core/src libs/core/src\nCOPY services/${SERVICE}/src services/${SERVICE}/src\nCOPY ops ./ops\n"
)
"""自检夹具的镜像构建文件。"""

SAMPLE_COUNTERS = (
    "cross_service_dependency",
    "layer_violation",
    "private_reference",
    "table_name_conflict",
    "table_prefix_conflict",
    "table_ownership_violation",
    "cross_service_access_violation",
    "registered_exceptions_total",
    "registered_exceptions_used",
    "ops_run_image_missing",
    "ops_import_closure_violation",
    "ops_image_packages_drift",
    "runtime_ops_dependency",
    "runtime_cross_service_dependency",
    "cross_domain_import",
    "cross_domain_table_reference",
    "cross_domain_write",
    "cross_domain_table_declare",
    "domain_layout_violation",
    "domain_ownership_violation",
    "domain_baseline_total",
    "domain_baseline_used",
    "domain_files_registered",
    "domain_files_unregistered",
)
"""自检夹具的计数键（与仓配置 `counters` 同口径）。"""


def sample_rule() -> ServiceBoundariesRules:
    """造自检夹具规则（路径与登记源全部指向临时仓库）。

    Returns:
        ServiceBoundariesRules: 夹具规则。
    """
    return ServiceBoundariesRules(
        shared_groups=("libs",),
        service_groups=("services",),
        shared_libs=("core",),
        service_packages=("platform", "alpha", "beta"),
        runtime_packages=("core", "ops"),
        layers_shared={"core": ("api",)},
        layers_service={"services": ("api",), "repositories": ("api", "services")},
        ownership_module="pkg/tables.py",
        ownership_const="TABLE_OWNERSHIP",
        owner_every_service="*",
        infrastructure_tables=("sys_outbox",),
        exempt_modules=("services_catalog.py",),
        catalog_module="pkg/services_catalog.py",
        catalog_const="SERVICE_CATALOG",
        layout="deploy/boundaries/domain_layout.json",
        exceptions="deploy/boundaries/data_ownership_exceptions.json",
        baseline="deploy/boundaries/domain_boundary_baseline.json",
        shared_models_marker="/libs/core/src/core/models/",
        ops_dir="pkg/ops",
        ops_dockerfile="pkg/Dockerfile",
        run_image_values=("platform", "ci", "*"),
        image_packages={"platform": ("core", "platform", "ops"), "ci": ("core", "platform", "alpha", "beta", "ops")},
        counters=SAMPLE_COUNTERS,
        no_domain_tables=("demo",),
        package_prefix="",
        shared_lib_service="platform",
        domain_subdir="domains",
        shared_table_prefixes=("sys_",),
    )


def mini_cfg(root: Path) -> Config:
    """造一份最小配置（自检夹具）。

    Args:
        root: 临时仓库根。

    Returns:
        Config: 配置对象。
    """
    return parse(
        {"docs": {"root": "docs"}, "backend": {"root": "pkg", "groups": ["libs", "services"]}},
        root / "engkit.toml",
    )


def write_files(root: Path, files: dict[str, str]) -> None:
    """按「相对路径 → 文本」写夹具文件（覆盖写）。

    Args:
        root: 临时仓库根。
        files: 文件内容映射。
    """
    for rel, content in files.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")


def base_files() -> dict[str, str]:
    """自检夹具的基线文件集（合规仓库）。

    Returns:
        dict[str, str]: 相对路径 → 文本。
    """
    return {
        "pkg/tables.py": SAMPLE_TABLES,
        "pkg/services_catalog.py": SAMPLE_CATALOG,
        "pkg/Dockerfile": SAMPLE_DOCKERFILE,
        "pkg/ops/__init__.py": "",
        "pkg/libs/core/src/core/__init__.py": "",
        "pkg/libs/core/src/core/core/__init__.py": "",
        "pkg/libs/core/src/core/models/__init__.py": "",
        "pkg/services/platform/src/platform/__init__.py": "",
        "pkg/services/alpha/src/alpha/__init__.py": "",
        "pkg/services/alpha/src/alpha/api/__init__.py": "",
        "pkg/services/alpha/src/alpha/services/__init__.py": "",
        "pkg/services/alpha/src/alpha/services/mod.py": "VALUE = 1\n",
        "pkg/services/alpha/src/alpha/services/other.py": "VALUE = 0\n",
        "pkg/services/alpha/src/alpha/services/second.py": "VALUE = 0\n",
        "pkg/services/beta/src/beta/__init__.py": "",
        "pkg/services/beta/src/beta/api/__init__.py": "",
        "deploy/boundaries/data_ownership_exceptions.json": json.dumps({"version": 1, "exceptions": []}),
        "deploy/boundaries/domain_boundary_baseline.json": json.dumps({"version": 1, "entries": []}),
    }


def self_test() -> int:
    """自检矩阵（临时仓库夹具）：服务维十项 + 域级 D1~D5 + 基线 + 形态校验。

    Returns:
        int: 退出码（0 通过 / 1 失败）。
    """
    ok = True

    def expect(condition: bool, label: str) -> None:
        nonlocal ok
        if condition:
            out(f"  [自检通过] {label}")
        else:
            ok = False
            out(f"  [自检失败] {label}")

    sample = sample_rule()
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        write_files(root, base_files())
        cfg = mini_cfg(root)

        def scan() -> Outcome:
            return check(cfg, sample)

        def counter(outcome: Outcome, key: str) -> int:
            return int(outcome.counts.get(key, 0))

        expect(not scan().problems, "合规仓库放行（共享库 / 服务 / 表 / 例外 / 镜像 / 域级）")

        # ---- 规则 1 ~ 4：依赖方向 ----
        write_files(root, {"pkg/libs/core/src/core/core/probe.py": "import alpha\n"})
        expect(counter(scan(), "cross_service_dependency") >= 1, "共享库依赖服务拦截")
        write_files(root, {"pkg/libs/core/src/core/core/probe.py": "VALUE = 0\n"})

        write_files(root, {"pkg/services/alpha/src/alpha/api/mod.py": "import beta\n"})
        expect(counter(scan(), "cross_service_dependency") >= 1, "服务互相依赖拦截")
        write_files(root, {"pkg/services/alpha/src/alpha/api/mod.py": "VALUE = 0\n"})

        write_files(root, {"pkg/services/alpha/src/alpha/services/mod.py": "import alpha.api\n"})
        expect(counter(scan(), "layer_violation") >= 1, "分层反向依赖拦截")
        write_files(root, {"pkg/services/alpha/src/alpha/services/mod.py": "VALUE = 1\n"})

        write_files(root, {"pkg/libs/core/src/core/core/_secret.py": "VALUE = 0\n"})
        write_files(root, {"pkg/services/alpha/src/alpha/api/mod.py": "import core.core._secret\n"})
        expect(counter(scan(), "private_reference") >= 1, "跨包私有引用拦截")
        write_files(root, {"pkg/services/alpha/src/alpha/api/mod.py": "VALUE = 0\n"})

        # ---- 规则 5 / 6：表归属与跨库访问 ----
        write_files(
            root,
            {"pkg/services/beta/src/beta/models/__init__.py": "", "pkg/services/beta/src/beta/models/m.py": ""},
        )
        write_files(root, {"pkg/services/alpha/src/alpha/api/m.py": '__tablename__ = "sys_alpha"\n'})
        write_files(root, {"pkg/services/beta/src/beta/models/m.py": '__tablename__ = "sys_alpha"\n'})
        expect(counter(scan(), "table_name_conflict") >= 1, "表名跨服务重复拦截")

        write_files(root, {"pkg/services/alpha/src/alpha/api/m.py": '__tablename__ = "alpha_one"\n'})
        write_files(root, {"pkg/services/beta/src/beta/models/m.py": '__tablename__ = "alpha_two"\n'})
        expect(counter(scan(), "table_prefix_conflict") >= 1, "表前缀跨服务重复拦截")

        write_files(root, {"pkg/services/beta/src/beta/models/m.py": '__tablename__ = "zzz_unknown"\n'})
        expect(counter(scan(), "table_ownership_violation") >= 1, "未登记表名拦截")

        write_files(root, {"pkg/services/alpha/src/alpha/api/m.py": '__tablename__ = "beta_log"\n'})
        write_files(root, {"pkg/services/beta/src/beta/models/m.py": ""})
        expect(counter(scan(), "table_ownership_violation") >= 1, "跨服务声明越权拦截")

        write_files(root, {"pkg/libs/core/src/core/models/beta/__init__.py": ""})
        write_files(root, {"pkg/libs/core/src/core/models/beta/m.py": '__tablename__ = "beta_log"\n'})
        write_files(root, {"pkg/services/alpha/src/alpha/api/m.py": ""})
        expect(not scan().problems, "共享模型子包声明他归属表放行")
        (root / "pkg/libs/core/src/core/models/beta/m.py").unlink()
        (root / "pkg/libs/core/src/core/models/beta/__init__.py").unlink()
        (root / "pkg/libs/core/src/core/models/__init__.py").unlink()

        write_files(root, {"pkg/services/alpha/src/alpha/api/repo.py": 'SQL = "select * from beta_log"\n'})
        expect(counter(scan(), "cross_service_access_violation") >= 1, "跨服务库访问（原始 SQL）拦截")

        write_files(
            root,
            {
                "deploy/boundaries/data_ownership_exceptions.json": json.dumps(
                    {
                        "version": 1,
                        "exceptions": [
                            {
                                "service": "alpha",
                                "target_prefix": "beta_log",
                                "access": "read",
                                "exit": "readonly_projection",
                                "consumer": "alpha 报表投影",
                                "reason": "自检样例",
                                "alternative": "自检替代方案",
                                "expiry": "自检后移除",
                                "registered_at": "2026-09-23",
                            }
                        ],
                    },
                    ensure_ascii=False,
                )
            },
        )
        hit = scan()
        expect(not hit.problems, "命中例外白名单（表名）放行")
        expect(counter(hit, "registered_exceptions_used") >= 1, "例外命中有计数")
        write_files(root, {"pkg/services/alpha/src/alpha/api/repo.py": "VALUE = 0\n"})
        write_files(
            root,
            {"deploy/boundaries/data_ownership_exceptions.json": json.dumps({"version": 1, "exceptions": []})},
        )

        # ---- 规则 8 / 9 / 10：运维镜像与运行期依赖 ----
        write_files(root, {"pkg/ops/clean.py": 'RUN_IMAGE = "*"\nimport core\n'})
        expect(not scan().problems, "运行镜像声明 * 且闭包只含基座放行")

        write_files(root, {"pkg/ops/clean.py": 'RUN_IMAGE = "*"\nfrom alpha import models\n'})
        expect(counter(scan(), "ops_import_closure_violation") >= 1, "声明 * 但闭包含服务包拦截")

        write_files(root, {"pkg/ops/clean.py": 'RUN_IMAGE = "ci"\nfrom alpha import models\n'})
        expect(not scan().problems, "声明 ci 覆盖全部包放行")

        write_files(root, {"pkg/ops/clean.py": 'RUN_IMAGE = "platform"\nfrom alpha import models\n'})
        expect(counter(scan(), "ops_import_closure_violation") >= 1, "声明 platform 但需要 alpha 拦截")

        write_files(root, {"pkg/ops/clean.py": "from alpha import models\n"})
        expect(counter(scan(), "ops_run_image_missing") >= 1, "跨包但未声明运行镜像拦截")

        write_files(root, {"pkg/ops/clean.py": 'RUN_IMAGE = "ghost"\nfrom alpha import models\n'})
        expect(counter(scan(), "ops_run_image_missing") >= 1, "运行镜像取值非法拦截")

        write_files(root, {"pkg/ops/clean.py": 'RUN_IMAGE = "ci"\nfrom alpha import models\n'})
        write_files(root, {"pkg/ops/relay.py": 'RUN_IMAGE = "*"\nfrom ops.clean import models\n'})
        expect(counter(scan(), "ops_import_closure_violation") >= 1, "间接跨包（传递闭包）拦截")
        (root / "pkg/ops/clean.py").unlink()
        (root / "pkg/ops/relay.py").unlink()
        expect(not scan().problems, "运维脚本清理后放行")

        write_files(root, {"pkg/services/alpha/src/alpha/api/runner.py": "from ops.seed import seed\n"})
        expect(counter(scan(), "runtime_ops_dependency") >= 1, "服务运行期导入运维脚本拦截")
        write_files(root, {"pkg/services/alpha/src/alpha/api/runner.py": "import platform\n"})
        expect(counter(scan(), "runtime_cross_service_dependency") >= 1, "非平台服务导入平台包拦截")
        write_files(root, {"pkg/services/platform/src/platform/main.py": "import platform\n"})
        write_files(root, {"pkg/services/alpha/src/alpha/api/runner.py": "VALUE = 0\n"})
        expect(not scan().problems, "平台服务导入平台包放行")

        write_files(root, {"pkg/Dockerfile": "COPY services/${SERVICE}/src services/${SERVICE}/src\n"})
        expect(counter(scan(), "ops_image_packages_drift") >= 1, "镜像构建文件缺 COPY 基座 / 运维脚本时拦截")
        write_files(root, {"pkg/Dockerfile": SAMPLE_DOCKERFILE})

        # ---- 域级断言（D1 ~ D5 / 基线 / 形态） ----
        layout_path = sample.layout
        alpha_domain = "pkg/services/alpha/src/alpha/services/mod.py"
        beta_domain = "pkg/services/alpha/src/alpha/services/other.py"
        beta_second = "pkg/services/alpha/src/alpha/services/second.py"
        beta_model = "pkg/services/beta/src/beta/models/m.py"
        alpha_part = "pkg/services/beta/src/beta/api/alpha_part.py"
        alpha_paths = [alpha_domain, alpha_part]
        beta_paths = [beta_domain, beta_second, beta_model]
        line = "import alpha.services.other"
        write_files(root, {beta_model: '__tablename__ = "beta_log"\n', alpha_part: "VALUE = 0\n"})

        def baseline_of(digest: str) -> str:
            return json.dumps(
                {
                    "version": 1,
                    "entries": [
                        {
                            "kind": "import",
                            "domain": "alpha",
                            "target": "beta",
                            "file": alpha_domain,
                            "fingerprint": digest,
                        }
                    ],
                }
            )

        write_files(root, {layout_path: json.dumps(domain_layout(alpha_paths, beta_paths))})
        write_files(root, {alpha_domain: f"{line}\n"})
        expect(counter(scan(), "cross_domain_import") >= 1, "同服务内跨域 import 拦截")

        write_files(root, {sample.baseline: baseline_of(fingerprint(line))})
        expect(not scan().problems, "跨域 import 命中基线放行")

        write_files(root, {alpha_domain: f"{line}\nimport alpha.services.second\n"})
        expect(counter(scan(), "cross_domain_import") >= 1, "基线外新增跨域 import 拦截")

        write_files(root, {sample.baseline: json.dumps({"version": 1, "entries": []})})
        write_files(
            root,
            {layout_path: json.dumps(domain_layout(alpha_paths, beta_paths, beta_public_api=["alpha.services.other"]))},
        )
        write_files(root, {alpha_domain: f"{line}\n"})
        expect(not scan().problems, "目标为域公共出口放行（D2）")

        write_files(root, {layout_path: json.dumps(domain_layout(alpha_paths, beta_paths))})
        write_files(root, {alpha_part: 'SQL = "select * from beta_second"\n'})
        expect(counter(scan(), "cross_domain_table_reference") >= 1, "跨域表引用拦截（D3）")

        write_files(root, {alpha_part: 'SQL = "update beta_second set value = 2"\n'})
        expect(counter(scan(), "cross_domain_write") >= 1, "跨域写拦截（D4，无例外）")

        write_files(root, {alpha_part: "VALUE = 0\n"})
        write_files(
            root,
            {layout_path: json.dumps(domain_layout(alpha_paths, beta_paths, alpha_service="beta"))},
        )
        expect(counter(scan(), "domain_ownership_violation") >= 1, "表归属域与承载服务不一致拦截")

        write_files(
            root,
            {layout_path: json.dumps(domain_layout(alpha_paths, beta_paths, duplicate=True))},
        )
        expect(counter(scan(), "domain_layout_violation") >= 1, "同一路径归属两个域拦截")

        write_files(root, {layout_path: json.dumps(domain_layout(alpha_paths, beta_paths))})
        write_files(root, {"pkg/services/alpha/src/alpha/extra/orphan.py": '__tablename__ = "beta_log"\n'})
        expect(counter(scan(), "domain_layout_violation") >= 1, "域实现未登记拦截（声明已登记表但文件未在布局）")
        (root / "pkg/services/alpha/src/alpha/extra/orphan.py").unlink()

        write_files(root, {alpha_domain: "VALUE = 2\n"})
        write_files(root, {sample.baseline: baseline_of(fingerprint(line))})
        expect(counter(scan(), "domain_layout_violation") >= 1, "陈旧基线条目拦截（只减不增）")
        write_files(root, {sample.baseline: json.dumps({"version": 1, "entries": []})})

        (root / layout_path).unlink()
        write_files(root, {alpha_domain: f"{line}\n"})
        expect(not scan().problems, "未提供域布局时域级断言停用")

        # ---- placement 形态校验 ----
        write_files(
            root,
            {
                "pkg/services/alpha/src/alpha/domains/__init__.py": "",
                "pkg/services/alpha/src/alpha/domains/alpha/__init__.py": "",
                "pkg/services/alpha/src/alpha/domains/alpha/api/__init__.py": "",
                "pkg/services/alpha/src/alpha/domains/alpha/api/mod.py": "VALUE = 0\n",
            },
        )
        (root / alpha_domain).unlink()
        (root / alpha_part).unlink()
        placement = "pkg/services/alpha/src/alpha/domains/alpha/api/mod.py"
        shared = (*SAMPLE_SHARED_PATHS, "pkg/services/alpha/src/alpha/domains/__init__.py")
        write_files(
            root,
            {
                layout_path: json.dumps(
                    domain_layout([placement], beta_paths, placement=True, public_api=[], shared=shared)
                )
            },
        )
        expect(counter(scan(), "domain_layout_violation") >= 1, "placement 域缺公共出口拦截（L3）")

        write_files(
            root,
            {
                layout_path: json.dumps(
                    domain_layout(
                        [placement],
                        beta_paths,
                        placement=True,
                        public_api=["alpha.domains.alpha.public"],
                        shared=shared,
                    )
                )
            },
        )
        expect(counter(scan(), "domain_layout_violation") >= 1, "placement 域公共出口模块缺失拦截（L3）")

        write_files(root, {"pkg/services/alpha/src/alpha/domains/alpha/public.py": ""})
        write_files(
            root,
            {
                layout_path: json.dumps(
                    domain_layout(
                        [placement],
                        beta_paths,
                        placement=True,
                        public_api=["alpha.domains.alpha.public"],
                        shared=shared,
                    )
                )
            },
        )
        expect(not scan().problems, "placement 域子包 + 公共出口放行")

        write_files(
            root,
            {
                layout_path: json.dumps(
                    domain_layout(
                        [placement],
                        beta_paths,
                        placement=True,
                        public_api=["alpha.domains.alpha.public"],
                        extra_path=beta_domain,
                        shared=shared,
                    )
                )
            },
        )
        expect(counter(scan(), "domain_layout_violation") >= 1, "placement 域路径越出域子包拦截（L1）")

        # ---- 基线写回与输出 ----
        write_files(
            root,
            {
                layout_path: json.dumps(
                    domain_layout(
                        [placement],
                        beta_paths,
                        placement=True,
                        public_api=["alpha.domains.alpha.public"],
                        shared=shared,
                    )
                )
            },
        )
        write_files(root, {placement: f"{line}\n"})
        check(cfg, sample, update_baseline=True)
        written = json.loads((root / sample.baseline).read_text(encoding="utf-8"))
        expect(bool(written["entries"]), "--update-baseline 把存量跨域引用写回基线")
        expect(not scan().problems, "基线写回后复扫通过")
        emit(Outcome(counts=dict.fromkeys(SAMPLE_COUNTERS, 0)), as_json=True)
        emit(Outcome(counts=dict.fromkeys(SAMPLE_COUNTERS, 0)), as_json=False)
        emit(Outcome(problems=["样例"], counts=dict.fromkeys(SAMPLE_COUNTERS, 0)), as_json=False)

    out(f"\n[check_service_boundaries self-test] {'通过' if ok else '不通过'}")
    return 0 if ok else 1


def domain_layout(
    alpha_paths: list[str],
    beta_paths: list[str],
    *,
    alpha_service: str = "alpha",
    public_api: list[str] | None = None,
    beta_public_api: list[str] | None = None,
    duplicate: bool = False,
    placement: bool = False,
    extra_path: str = "",
    shared: tuple[str, ...] = (),
) -> dict[str, object]:
    """造域布局 JSON（自检夹具：alpha / beta 两个域）。

    Args:
        alpha_paths: alpha 域的代码路径。
        beta_paths: beta 域的代码路径。
        alpha_service: alpha 域的承载服务。
        public_api: alpha 域的公共出口模块。
        beta_public_api: beta 域的公共出口模块。
        duplicate: 是否把 alpha 域首条路径同时登记到 beta 域（跨域重复归属）。
        placement: alpha 域是否用域子包目录 glob（`placement` 状态）。
        extra_path: 追加到 alpha 域 paths 的路径。
        shared: 域外路径（缺省取夹具常量）。

    Returns:
        dict[str, object]: 域布局声明。
    """
    alpha_declared = ["pkg/services/alpha/src/alpha/domains/alpha/**/*.py"] if placement else list(alpha_paths)
    if extra_path:
        alpha_declared.append(extra_path)
    beta_declared = list(beta_paths)
    if duplicate:
        beta_declared.append(alpha_paths[0])
    return {
        "version": 1,
        "domains": [
            {
                "domain_key": "alpha",
                "service_key": alpha_service,
                "status": "placement" if placement else "legacy",
                "paths": alpha_declared,
                "public_api": public_api or [],
            },
            {
                "domain_key": "beta",
                "service_key": "beta",
                "status": "legacy",
                "paths": beta_declared,
                "public_api": beta_public_api or [],
            },
        ],
        "shared_paths": list(shared or SAMPLE_SHARED_PATHS),
    }


SAMPLE_SHARED_PATHS = (
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
"""自检夹具的域外路径（装配 / 基座面，不计入域实现）。"""
