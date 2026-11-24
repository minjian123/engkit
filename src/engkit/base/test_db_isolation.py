"""测试库隔离静态护栏（`engkit base test-db-isolation`）。

口径（需求 04-6 / 任务 04-6-1，2026-11-01 拍板；13_08 扩围）：

- **G1 自建库**：测试代码不得为“图省事”自建 SQLite（连接串字面量 / 直接构造引擎 /
  `tmp_path` 派生 `*.db`）；“建库即被测”的用例打 `@pytest.mark.own_db("理由")` 进白名单。
- **G2 裸 int ID**：原生 SQL 的 ID 参数必须经测试基座的 `sqlite_id()` 包装；
  绑定 int 常量即违规（SQLite 落 38 位补零 TEXT，裸 int 查不到）。
- **G3 清理他人数据**：单库叠加下不得 `drop_all` / `create_all` / `TRUNCATE` / `DROP TABLE` /
  无 `where` 的 `delete()`（白名单内的自建表除外）——建表与清表都会打到共享库上他人数据。
- **G4 假等待**：用例里不得 `sleep()` 等状态；等待应以确定性断言（轮询条件 / 事件）替代。
- **G5 直连共享库**（13_08 T7）：测试辅助模块（`gate_bypass_targets` 下的**全部** `.py`）不得
  直接 `create_async_engine` / `create_engine` 去开共享库——这类直连不取读写门，长读事务会在
  SQLite 层挡住走门的写者，表现为「有人持写门很久」。统一走 `gated_session` / `write_window`
  或收进共享测试基座（`base_modules`）。
- **G6 门外写**（13_08 续修）：本文件内创建的裸引擎（`create_async_engine` / `create_engine`）
  及其派生的裸会话，`begin()` / `connect()` / `add()` / `flush()` / `commit()` / 写语句
  **必须**在门区间内（`shared_window` / `write_window` / `gate_scope` / `gated_session` /
  `write_session` / `gated_sessionmaker`）。门外写会让 SQLite 写锁在门已放之后仍被占着，
  别的 worker 门内写只能报 `database is locked`；`own_db` 作用域与行尾 `# own_db:` 放行。

判定用 AST（不用正则），避免注释与字符串误判；支持 `--files-from` 增量与 `--list-own-db` 白名单导出。
扩围后的存量违规由 `[rules.test_db_isolation].baseline` 快照登记，**只减不增**：同一文件同一
规则码的命中数不得超过基线值，新增即失败；基线随治理递减重写（`--update-baseline`）。
"""

from __future__ import annotations

import ast
import json
import sys
from pathlib import Path
from typing import NamedTuple

from ..config import Config, TestDbIsolationRules
from ..console import out
from .rules import RulesError, read_text, require
from .scan_scope import STDIN_LIST

MIN_KEYS = ("scan_targets",)
"""未登记时报错给出的最小键指引。"""

_SQL_CALLS = ("execute", "exec_driver_sql")
"""原生 SQL 执行入口名（G2 / G3 判定范围）。"""

_DELETE_NAME = "delete"
"""SQLAlchemy 全表删除构造名（G3 判定范围）。"""

G1 = "G1"
G2 = "G2"
G3 = "G3"
G4 = "G4"
G5 = "G5"
G6 = "G6"
"""规则码（基线快照按「文件 + 规则码」计数，行号变动不失效）。"""

_SESSION_FACTORIES = ("async_sessionmaker", "sessionmaker")
"""裸会话工厂构造名（对应门版是 `gated_sessionmaker`）。"""

_GATE_WRAPPERS = (
    "shared_window",
    "write_window",
    "platform_db_write",
    "tenant_db_write",
    "db_access_gate",
    "gate_scope",
    "gated_session",
    "write_session",
    "gated_sessionmaker",
)
"""门区间入口名：这些调用所在的 `with` 块视为已在门内。"""

_WRITE_SESSION_METHODS = ("add", "add_all", "delete", "flush", "commit")
"""裸会话上的写入口名（进入会话即写 / 提交）。"""

_WRITE_SQL_HEADS = ("INSERT", "UPDATE", "DELETE", "REPLACE", "CREATE", "DROP", "ALTER", "TRUNCATE")
"""原生 SQL 的写语句开头（G6 判定范围）。"""

_WRITE_BUILDERS = ("insert", "update", "delete")
"""SQLAlchemy 写语句构造名（G6 判定范围）。"""


class Hit(NamedTuple):
    """一处命中。

    Attributes:
        file: 相对仓库根的 POSIX 路径。
        code: 规则码（G1-G6）。
        lineno: 行号（仅用于报告）。
        message: 问题描述。
    """

    file: str
    code: str
    lineno: int
    message: str


class BaselineError(Exception):
    """基线快照非法（格式 / 版本 / 字段）。"""


def _is_allowed(node: ast.AST, marker: str) -> bool:
    """判断节点（模块 / 类 / 函数）是否带白名单 marker。

    Args:
        node: AST 节点。
        marker: marker 名（如 `own_db`）。

    Returns:
        bool: 带 `@pytest.mark.<marker>(...)` 或 `@pytest.mark.<marker>` 为 True。
    """
    for decorator in getattr(node, "decorator_list", []):
        target = decorator.func if isinstance(decorator, ast.Call) else decorator
        if isinstance(target, ast.Attribute) and target.attr == marker:
            return True
    return False


def _marker_reason(node: ast.AST, marker: str) -> str:
    """取白名单 marker 的理由参数（缺省空串）。

    Args:
        node: AST 节点。
        marker: marker 名。

    Returns:
        str: 第一个字符串常量参数；无参数或非字符串返回空串。
    """
    for decorator in getattr(node, "decorator_list", []):
        if not isinstance(decorator, ast.Call):
            continue
        target = decorator.func
        if isinstance(target, ast.Attribute) and target.attr == marker:
            for arg in decorator.args:
                if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                    return arg.value
    return ""


def _pytestmark_reason(tree: ast.Module, marker: str) -> str:
    """模块级 `pytestmark = [pytest.mark.<marker>("理由")]` 的理由。

    为什么需要：`pytestmark` 是给整份文件打标的标准写法（比逐函数装饰器更少重复），
    只在装饰器上判白名单会把这类文件误判成违规。与装饰器同一口径：理由必填。

    Args:
        tree: 模块 AST。
        marker: 白名单 marker 名（如 `own_db`）。

    Returns:
        str: 理由；未打标或理由非字符串字面量时返回空串。
    """
    for node in tree.body:
        if isinstance(node, ast.Assign):
            targets = node.targets
            value = node.value
        elif isinstance(node, ast.AnnAssign):
            targets = [node.target]
            value = node.value
        else:
            continue
        if value is None or not any(isinstance(target, ast.Name) and target.id == "pytestmark" for target in targets):
            continue
        elements = value.elts if isinstance(value, (ast.List, ast.Tuple)) else [value]
        for element in elements:
            if not isinstance(element, ast.Call):
                continue
            target = element.func
            if not (isinstance(target, ast.Attribute) and target.attr == marker):
                continue
            for arg in element.args:
                if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                    return arg.value
    return ""


def _is_memory_db(value: str, rule: TestDbIsolationRules) -> bool:
    """连接串是否指向**内存库**（不落文件、不产生跨用例状态）。

    为什么豁免：G1 的原意是「禁止为图省事自建 SQLite **文件**」——文件才是并行干扰与磁盘膨胀的来源。
    内存库（`sqlite://`、`sqlite+aiosqlite://`、含 `:memory:`）每条连接独立、用完即消，
    是「需要一条干净连接来验证拦截 / 降级」之类用例的正当手段，不应被判违规。

    Args:
        value: 连接串字面量。
        rule: 规则段。

    Returns:
        bool: 内存库为 True。
    """
    if ":memory:" in value:
        return True
    return value in rule.memory_literals


def _engine_on_memory(node: ast.Call, rule: TestDbIsolationRules) -> bool:
    """引擎构造是否指向内存库（首个位置参数为内存连接串）。

    Args:
        node: 调用节点。
        rule: 规则段。

    Returns:
        bool: 内存库引擎为 True。
    """
    if not node.args:
        return False
    first = node.args[0]
    return isinstance(first, ast.Constant) and isinstance(first.value, str) and _is_memory_db(first.value, rule)


def _call_name(node: ast.AST) -> str:
    """取调用名（支持 `mod.func()` 与 `func()`）。

    Args:
        node: AST 节点。

    Returns:
        str: 函数名；非调用返回空串。
    """
    if not isinstance(node, ast.Call):
        return ""
    if isinstance(node.func, ast.Name):
        return node.func.id
    if isinstance(node.func, ast.Attribute):
        return node.func.attr
    return ""


def _is_write_expression(node: ast.AST) -> bool:
    """判断表达式是否写语句（原生 SQL 字符串或 SQLAlchemy 写构造）。

    Args:
        node: 表达式节点。

    Returns:
        bool: 写语句为 True。
    """
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        words = node.value.lstrip().split()
        return bool(words) and words[0].upper() in _WRITE_SQL_HEADS
    return _call_name(node) in _WRITE_BUILDERS


def _has_sqlalchemy_delete(tree: ast.Module) -> bool:
    """判断模块是否从 `sqlalchemy` 导入了 `delete`（G3 的判定前提）。

    Args:
        tree: 模块 AST。

    Returns:
        bool: 导入了 `delete` 为 True。
    """
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.ImportFrom)
            and (node.module or "").startswith("sqlalchemy")
            and any(alias.name == "delete" for alias in node.names)
        ):
            return True
    return False


class _Scanner(ast.NodeVisitor):
    """按作用域跟踪白名单 marker 的扫描器。"""

    def __init__(
        self,
        rel_path: str,
        rule: TestDbIsolationRules,
        *,
        sql_delete: bool = False,
        gate_mode: bool = False,
        lines: list[str] | None = None,
    ) -> None:
        """初始化。

        Args:
            rel_path: 相对仓库根的 POSIX 路径。
            rule: 规则段。
            sql_delete: 模块是否从 `sqlalchemy` 导入了 `delete`（只在该前提下判 G3 的 `delete()`）。
            gate_mode: 只判 G5 / G6（直连共享库与门外写）；此时不判 G1-G4，避免同一行重复报两遍。
            lines: 源文件按行切分（用于识别行尾 `own_db` 放行注释）。
        """
        self.rel_path = rel_path
        self.rule = rule
        self.sql_delete = sql_delete
        self.gate_mode = gate_mode
        self.lines = lines or []
        self.problems: list[str] = []
        self.hits: list[Hit] = []
        self._allowed = 0
        self._gated = 0
        self._raw_engines: set[str] = set()
        self._raw_factories: set[str] = set()
        self._raw_sessions: set[str] = set()
        self._delete_with_where: set[int] = set()

    def _scope(self, node: ast.AST) -> None:
        """进入作用域：带 marker 时整体放行；函数作用域的追踪集合不外泄给兄弟函数。

        Args:
            node: 模块 / 类 / 函数节点。
        """
        allowed = _is_allowed(node, self.rule.allow_marker)
        if not allowed and isinstance(node, ast.Module):
            allowed = bool(_pytestmark_reason(node, self.rule.allow_marker))
        is_function = isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        saved = (self._raw_engines, self._raw_factories, self._raw_sessions) if is_function else None
        if saved is not None:
            # 继承模块级引擎（模块级建库、函数里写是同一份库），但不把本函数新建的会话泄漏给兄弟函数
            self._raw_engines = set(saved[0])
            self._raw_factories = set(saved[1])
            self._raw_sessions = set(saved[2])
        self._allowed += 1 if allowed else 0
        self.generic_visit(node)
        self._allowed -= 1 if allowed else 0
        if saved is not None:
            self._raw_engines, self._raw_factories, self._raw_sessions = saved

    def visit_Module(self, node: ast.Module) -> None:
        """模块作用域。

        Args:
            node: 模块节点。
        """
        self._scope(node)

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        """类作用域。

        Args:
            node: 类节点。
        """
        self._scope(node)

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        """函数作用域。

        Args:
            node: 函数节点。
        """
        self._scope(node)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        """异步函数作用域。

        Args:
            node: 异步函数节点。
        """
        self._scope(node)

    def _line_mentions_own_db(self, node: ast.AST) -> bool:
        """判断命中行是否带 `own_db` 放行注释（行尾 `# own_db: 理由`）。

        为什么支持行尾注释：`tests_support` 里的辅助模块用「模块内建内存库」做各用例的干净
        数据面，不是 `test_*.py`，无法挂 `pytestmark`；行尾注释是它们现有的放行写法。

        Args:
            node: 命中节点。

        Returns:
            bool: 该行文本含白名单 marker 为 True。
        """
        lineno = getattr(node, "lineno", 0)
        if not 1 <= lineno <= len(self.lines):
            return False
        return self.rule.allow_marker in self.lines[lineno - 1]

    def _track_assignment(self, targets: list[ast.expr], value: ast.expr | None) -> None:
        """追踪「本地裸引擎 → 裸会话工厂 → 裸会话」的赋值链（G6 判定输入）。

        Args:
            targets: 赋值目标。
            value: 赋值右侧。
        """
        if value is None:
            return
        names = [target.id for target in targets if isinstance(target, ast.Name)]
        if not names:
            return
        if _call_name(value) in self.rule.engine_factories:
            call = value
            if (
                isinstance(call, ast.Call)
                and not self._line_mentions_own_db(call)
                and not _engine_on_memory(call, self.rule)
            ):
                self._raw_engines.update(names)
            return
        if _call_name(value) in _SESSION_FACTORIES:
            call = value
            first = call.args[0] if isinstance(call, ast.Call) and call.args else None
            if isinstance(first, ast.Name) and first.id in self._raw_engines:
                self._raw_factories.update(names)
            return
        inner = value.value if isinstance(value, ast.Await) else value
        if isinstance(inner, ast.Call) and isinstance(inner.func, ast.Name) and inner.func.id in self._raw_factories:
            self._raw_sessions.update(names)

    def visit_Assign(self, node: ast.Assign) -> None:
        """追踪赋值链。

        Args:
            node: 赋值节点。
        """
        self._track_assignment(node.targets, node.value)
        self.generic_visit(node)

    def visit_AnnAssign(self, node: ast.AnnAssign) -> None:
        """追踪带注解赋值。

        Args:
            node: 注解赋值节点。
        """
        self._track_assignment([node.target], node.value)
        self.generic_visit(node)

    def _visit_with(self, node: ast.With | ast.AsyncWith) -> None:
        """进入 `with` 块：门区间入口内的写视为合规；同时记录裸会话绑定。

        Args:
            node: `with` / `async with` 节点。
        """
        wrapped = any(
            isinstance(item.context_expr, ast.Call) and _call_name(item.context_expr) in _GATE_WRAPPERS
            for item in node.items
        )
        for item in node.items:
            target = item.optional_vars
            expression = item.context_expr
            if isinstance(target, ast.Name) and isinstance(expression, ast.Call):
                func = expression.func
                if isinstance(func, ast.Name) and func.id in self._raw_factories:
                    self._raw_sessions.add(target.id)
        self._gated += 1 if wrapped else 0
        self.generic_visit(node)
        self._gated -= 1 if wrapped else 0

    def visit_With(self, node: ast.With) -> None:
        """同步 `with` 块。

        Args:
            node: `with` 节点。
        """
        self._visit_with(node)

    def visit_AsyncWith(self, node: ast.AsyncWith) -> None:
        """异步 `with` 块。

        Args:
            node: `async with` 节点。
        """
        self._visit_with(node)

    def visit_Constant(self, node: ast.Constant) -> None:
        """G1：连接串字面量（内存库不算自建库）。

        Args:
            node: 常量节点。
        """
        if (
            isinstance(node.value, str)
            and node.value.startswith(self.rule.db_literal_prefixes)
            and not _is_memory_db(node.value, self.rule)
        ):
            self._report(node, G1, "G1 自建 SQLite：连接串字面量（应用共享夹具；确需建库请打 own_db 白名单）")
        self.generic_visit(node)

    def visit_Call(self, node: ast.Call) -> None:
        """G1 引擎构造 / G2 裸 int ID / G3 全表清理 / G5 直连共享库 / G6 门外写。

        Args:
            node: 调用节点。
        """
        name = _call_name(node)
        # `delete(X).where(...)` 的外层调用先于内层被访问：在这里登记「已带过滤条件」的内层
        # `delete(X)` 节点，避免把链式写法误判成「无 where 的全表删除」。
        func = node.func
        if (
            isinstance(func, ast.Attribute)
            and isinstance(func.value, ast.Call)
            and _call_name(func.value) == _DELETE_NAME
        ):
            self._delete_with_where.add(id(func.value))
        if self.gate_mode:
            if name in self.rule.engine_factories and not _engine_on_memory(node, self.rule):
                self._report(
                    node,
                    G5,
                    f"G5 直连共享库：{name}() 未经读写门（改用 gated_session / write_window，"
                    "或收进 base_modules 声明的共享测试基座）",
                )
            self._check_gate_bypass(node)
            self.generic_visit(node)
            return
        if name in self.rule.engine_factories and not _engine_on_memory(node, self.rule):
            self._report(node, G1, f"G1 自建 SQLite：直接构造引擎 {name}()（应用共享夹具）")
        if name in self.rule.cleanup_calls:
            self._report(node, G3, f"G3 清理他人数据：{name}() 会清空共享库（确需请打 own_db 白名单）")
        if name in self.rule.ddl_calls:
            self._report(node, G3, f"G3 共享库 DDL：{name}() 会建 / 删共享库的表（确需请打 own_db 白名单）")
        if name in self.rule.wait_calls:
            self._report(node, G4, f"G4 假等待：{name}() 取代了确定性断言（改为轮询条件 / 等事件）")
        if (
            name == _DELETE_NAME
            and self.sql_delete
            and isinstance(node.func, ast.Name)
            and len(node.args) == 1
            and id(node) not in self._delete_with_where
        ):
            self._report(node, G3, "G3 清理他人数据：delete() 未带 where 条件（单库叠加下会删掉他人数据）")
        if name in _SQL_CALLS:
            self._check_raw_sql(node)
        self._check_gate_bypass(node)
        self.generic_visit(node)

    def _check_gate_bypass(self, node: ast.Call) -> None:
        """G6：直连引擎 / 裸会话的写在门区间外执行。

        只判**本文件内创建的裸引擎**及其派生的裸会话（fixture 传入的引擎多为门代理，
        静态层面无法区分，误报代价高于收益）；`own_db` 作用域或行尾注释放行。

        Args:
            node: 调用节点。
        """
        if self._gated or self._allowed:
            return
        func = node.func
        if not isinstance(func, ast.Attribute) or not isinstance(func.value, ast.Name):
            return
        owner = func.value.id
        if func.attr in ("begin", "connect") and owner in self._raw_engines:
            self._report(
                node,
                G6,
                f"G6 门外写：直连引擎 {owner}.{func.attr}() 未经读写门"
                "（改用 shared_window / gated_session，或把引擎收进共享测试基座）",
            )
            return
        if owner not in self._raw_sessions:
            return
        if func.attr in _WRITE_SESSION_METHODS:
            self._report(
                node,
                G6,
                f"G6 门外写：裸会话 {owner}.{func.attr}() 未经读写门（改用 gated_session / write_session）",
            )
            return
        if func.attr in _SQL_CALLS and node.args and _is_write_expression(node.args[0]):
            self._report(
                node,
                G6,
                f"G6 门外写：裸会话 {owner}.{func.attr}() 执行写语句且未经读写门（改用 gated_session / write_session）",
            )

    def visit_BinOp(self, node: ast.BinOp) -> None:
        """G1：`tmp_path / "x.db"` 派生临时库。

        Args:
            node: 二元运算节点。
        """
        if isinstance(node.op, ast.Div) and isinstance(node.right, ast.Constant):
            value = node.right.value
            if isinstance(value, str) and value.endswith(self.rule.db_file_suffixes):
                self._report(node, G1, f"G1 自建 SQLite：临时目录派生库文件 {value}（应用共享夹具）")
        self.generic_visit(node)

    def _check_raw_sql(self, node: ast.Call) -> None:
        """G2：原生 SQL 绑定 int 常量到 ID 列。

        Args:
            node: 调用节点。
        """
        if not node.args or not isinstance(node.args[0], ast.Constant):
            return
        sql = node.args[0].value
        if not isinstance(sql, str):
            return
        lowered = sql.lower()
        for pattern in self.rule.ddl_sql_patterns:
            if pattern in lowered:
                self._report(node, G3, f"G3 共享库 DDL：原生 SQL 含 `{pattern.upper()}`（会打到共享库上他人数据）")
                break
        if not any(f"{column} =" in lowered or f"{column} in" in lowered for column in self.rule.id_columns):
            return
        for arg in node.args[1:]:
            if isinstance(arg, ast.Dict) and any(
                isinstance(value, ast.Constant) and isinstance(value.value, int) for value in arg.values
            ):
                self._report(node, G2, "G2 裸 int ID：原生 SQL 绑定 int 常量（应经 sqlite_id() 转补零字符串）")

    def _report(self, node: ast.AST, code: str, message: str) -> None:
        """记录问题（白名单作用域内跳过）。

        Args:
            node: 命中节点。
            code: 规则码（G1 / G2 / G3 / G4）。
            message: 问题描述。
        """
        if self._allowed:
            return
        lineno = getattr(node, "lineno", 0)
        self.problems.append(f"{self.rel_path}:{lineno}: {message}")
        self.hits.append(Hit(self.rel_path, code, lineno, message))


def scan_hits(rel_path: str, text: str, rule: TestDbIsolationRules) -> list[Hit]:
    """扫描单个文件文本（返回结构化命中，供基线与增量判定使用）。

    Args:
        rel_path: 相对仓库根的 POSIX 路径。
        text: 文件全文。
        rule: 规则段。

    Returns:
        list[Hit]: 命中清单。
    """
    try:
        tree = ast.parse(text)
    except SyntaxError:
        return []
    scanner = _Scanner(rel_path, rule, sql_delete=_has_sqlalchemy_delete(tree), lines=text.splitlines())
    scanner.visit(tree)
    return scanner.hits


def scan_text(rel_path: str, text: str, rule: TestDbIsolationRules) -> list[str]:
    """扫描单个文件文本（返回人读格式）。

    Args:
        rel_path: 相对仓库根的 POSIX 路径。
        text: 文件全文。
        rule: 规则段。

    Returns:
        list[str]: 问题清单（`路径:行号: 描述`）。
    """
    return [f"{hit.file}:{hit.lineno}: {hit.message}" for hit in scan_hits(rel_path, text, rule)]


def scan_gate_hits(rel_path: str, text: str, rule: TestDbIsolationRules) -> list[Hit]:
    """扫描单个辅助模块的 G5（直连共享库）命中。

    Args:
        rel_path: 相对仓库根的 POSIX 路径。
        text: 文件全文。
        rule: 规则段。

    Returns:
        list[Hit]: G5 命中清单。
    """
    try:
        tree = ast.parse(text)
    except SyntaxError:
        return []
    scanner = _Scanner(rel_path, rule, gate_mode=True, lines=text.splitlines())
    scanner.visit(tree)
    return scanner.hits


def scan_gate_text(rel_path: str, text: str, rule: TestDbIsolationRules) -> list[str]:
    """扫描单个辅助模块的 G5 命中（返回人读格式）。

    Args:
        rel_path: 相对仓库根的 POSIX 路径。
        text: 文件全文。
        rule: 规则段。

    Returns:
        list[str]: 问题清单（`路径:行号: 描述`）。
    """
    return [f"{hit.file}:{hit.lineno}: {hit.message}" for hit in scan_gate_hits(rel_path, text, rule)]


def tally(hits: list[Hit]) -> dict[tuple[str, str], int]:
    """把命中按「文件 + 规则码」聚合计数。

    Args:
        hits: 命中清单。

    Returns:
        dict[tuple[str, str], int]: 计数表。
    """
    table: dict[tuple[str, str], int] = {}
    for hit in hits:
        key = (hit.file, hit.code)
        table[key] = table.get(key, 0) + 1
    return table


def read_baseline(path: Path, rule: TestDbIsolationRules) -> dict[tuple[str, str], int]:
    """读取存量基线快照。

    Args:
        path: 基线文件路径（不存在视为空基线）。
        rule: 规则段。

    Returns:
        dict[tuple[str, str], int]: 基线计数（文件 + 规则码 → 允许条数）。

    Raises:
        BaselineError: 文件存在但格式 / 版本 / 字段非法。
    """
    if not path.is_file():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise BaselineError(f"基线快照不可解析：{path}（{exc}）") from exc
    if not isinstance(payload, dict) or payload.get("version") != rule.baseline_version:
        raise BaselineError(f"基线快照版本非法（须为 {rule.baseline_version}）：{path}")
    entries = payload.get("entries")
    if not isinstance(entries, list):
        raise BaselineError(f"基线快照 entries 非法（须为数组）：{path}")
    counter: dict[tuple[str, str], int] = {}
    for index, entry in enumerate(entries):
        if not isinstance(entry, dict):
            raise BaselineError(f"基线快照条目非法（第 {index + 1} 条须为对象）：{path}")
        try:
            key = (str(entry["file"]), str(entry["code"]))
            count = int(entry.get("count", 1))
        except (KeyError, TypeError, ValueError) as exc:
            raise BaselineError(f"基线快照条目字段缺失或非法（第 {index + 1} 条）：{path}") from exc
        if count < 1:
            raise BaselineError(f"基线快照条目 count 非法（第 {index + 1} 条须为正整数）：{path}")
        counter[key] = counter.get(key, 0) + count
    return counter


def write_baseline(path: Path, hits: list[Hit], rule: TestDbIsolationRules) -> int:
    """以本次扫描结果重写基线快照（自然完成递减）。

    Args:
        path: 基线文件路径。
        hits: 命中清单。
        rule: 规则段。

    Returns:
        int: 写入的条目数。
    """
    counts = tally(hits)
    entries = [{"file": file, "code": code, "count": count} for (file, code), count in sorted(counts.items())]
    payload = {"version": rule.baseline_version, "entries": entries}
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return len(entries)


def collect_own_db(rel_path: str, text: str, rule: TestDbIsolationRules) -> list[str]:
    """收集白名单登记项（文件 / 用例 / 理由）。

    Args:
        rel_path: 相对仓库根的 POSIX 路径。
        text: 文件全文。
        rule: 规则段。

    Returns:
        list[str]: `路径::用例名::理由` 清单；理由为空时标注「缺理由」。
    """
    try:
        tree = ast.parse(text)
    except SyntaxError:
        return []
    entries: list[str] = []
    module_reason = _pytestmark_reason(tree, rule.allow_marker)
    if module_reason:
        entries.append(f"{rel_path}::<module>::{module_reason}")
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            continue
        if not _is_allowed(node, rule.allow_marker):
            continue
        reason = _marker_reason(node, rule.allow_marker) or "缺理由"
        entries.append(f"{rel_path}::{node.name}::{reason}")
    return entries


def _iter_files(root: Path, rule: TestDbIsolationRules, files_from: str) -> list[str]:
    """枚举待扫描文件（相对根 POSIX 路径）。

    Args:
        root: 仓库根。
        rule: 规则段。
        files_from: 变更清单文件（空串表示全量）。

    Returns:
        list[str]: 相对路径清单。
    """
    if files_from:
        source = sys.stdin.read() if files_from == STDIN_LIST else read_text(Path(files_from))
        candidates = [line.strip().replace("\\", "/") for line in source.splitlines() if line.strip().endswith(".py")]
        # 只判**用例文件**（`test_*.py`）：与全量模式同一命名判据，两种模式结论必须一致
        # （`tests_support/` 下的共享测试基座本来就不在用例文件之列，其建库职责由
        # `base_modules` 显式声明，不靠文件名隐藏）。
        return [
            path
            for path in candidates
            if (root / path).is_file()
            and Path(path).name.startswith("test_")
            and path not in set(rule.base_modules)
            and any(path.startswith(f"{root_dir.strip('/')}/") for root_dir in rule.scan_targets)
        ]
    files: list[str] = []
    base_modules = set(rule.base_modules)
    for target in rule.scan_targets:
        base = root / target
        if not base.exists():
            continue
        for path in sorted(base.rglob("test_*.py")):
            if set(path.relative_to(root).parts) & set(rule.exclude_parts):
                continue
            rel = path.relative_to(root).as_posix()
            if rel in base_modules:
                continue
            files.append(rel)
    return files


def _iter_gate_files(root: Path, rule: TestDbIsolationRules, files_from: str) -> list[str]:
    """枚举 G5 / G6 待扫描文件——`gate_bypass_targets` 下**全部** `.py`。

    为什么与 `_iter_files` 分开：G1-G4 的语义是「用例文件」，而直连共享库与门外写恰恰发生在
    `tests_support/` 这类非 `test_` 前缀的辅助模块里，按 `test_*.py` 过滤会整片漏掉。

    Args:
        root: 仓库根。
        rule: 规则段。
        files_from: 变更清单文件（空串表示全量）。

    Returns:
        list[str]: 相对路径清单（已剔除 `base_modules` 与剪枝目录）。
    """
    base_modules = set(rule.base_modules)
    if files_from:
        source = sys.stdin.read() if files_from == STDIN_LIST else read_text(Path(files_from))
        candidates = [line.strip().replace("\\", "/") for line in source.splitlines() if line.strip().endswith(".py")]
        return [
            path
            for path in candidates
            if (root / path).is_file()
            and path not in base_modules
            and any(
                path == target.rstrip("/") or path.startswith(f"{target.rstrip('/')}/")
                for target in rule.gate_bypass_targets
            )
        ]
    files: list[str] = []
    for target in rule.gate_bypass_targets:
        base = root / target
        if not base.exists():
            continue
        candidates = [base] if base.is_file() else sorted(base.rglob("*.py"))
        for path in candidates:
            if set(path.relative_to(root).parts) & set(rule.exclude_parts):
                continue
            rel = path.relative_to(root).as_posix()
            if rel in base_modules or rel in files:
                continue
            files.append(rel)
    return files


def self_test(rule: TestDbIsolationRules) -> int:
    """自检：合规 / 各类违规 / 白名单 / 注释与字符串误判向量。

    Args:
        rule: 规则段。

    Returns:
        int: 退出码（0 通过 / 1 失败）。
    """
    good = 'async def test_ok(client):\n    rows = await client.get("/api/v1/x")\n'
    own = (
        "import pytest\n\n\n"
        '@pytest.mark.own_db("建删库是被测功能")\n'
        "def test_create_db(tmp_path):\n"
        '    url = "sqlite+aiosqlite:///" + str(tmp_path / "x.db")\n'
    )
    literal = 'def test_a():\n    url = "sqlite+aiosqlite:///x.db"\n'
    engine = 'def test_b():\n    e = create_async_engine("x")\n'
    raw_sql = 'async def test_c(session):\n    await session.execute("SELECT 1 FROM t WHERE id = :id", {"id": 1001})\n'
    cleanup = "def test_d(meta):\n    meta.drop_all()\n"
    create_all = "def test_g(meta):\n    meta.create_all(bind)\n"
    truncate = 'async def test_h(session):\n    await session.execute("TRUNCATE TABLE t")\n'
    drop_table = 'async def test_i(session):\n    await session.exec_driver_sql("drop table t")\n'
    wait = "async def test_j():\n    await sleep(1.5)\n"
    cache_delete = "def test_e(cache):\n    cache.delete('k')\n"
    sql_delete = "from sqlalchemy import delete\n\n\ndef test_f(meta, table):\n    meta.execute(delete(table))\n"
    commented = "# create_async_engine('sqlite+aiosqlite:///x.db')\nDOC = 'delete() 与 drop_all() 的说明'\n"
    module_mark = (
        "import pytest\n\n"
        'pytestmark = [pytest.mark.own_db("整份文件的建库即被测")]\n\n\n'
        "def test_a():\n    e = create_async_engine('x')\n"
    )
    gate_bypass = (
        "async def test_k():\n"
        "    engine = create_async_engine(url)\n"
        "    async with engine.begin() as connection:\n"
        "        await connection.execute('INSERT INTO t VALUES (1)')\n"
    )
    gate_wrapped = (
        "async def test_l():\n"
        "    engine = create_async_engine(url)\n"
        "    async with shared_window(engine), engine.begin() as connection:\n"
        "        await connection.execute('INSERT INTO t VALUES (1)')\n"
    )
    raw_session = (
        "async def test_m():\n"
        "    engine = create_async_engine(url)\n"
        "    factory = async_sessionmaker(engine)\n"
        "    session = factory()\n"
        "    session.add(row)\n"
    )
    marked_bypass = (
        "import pytest\n\n"
        'pytestmark = [pytest.mark.own_db("独立库")]\n\n\n'
        "async def test_n():\n"
        "    engine = create_async_engine(url)\n"
        "    async with engine.begin() as connection:\n"
        "        await connection.execute('INSERT INTO t VALUES (1)')\n"
    )
    lines_own_db = (
        "def helper(url):\n"
        "    engine = create_async_engine(url)  # own_db: 模块内建内存库\n"
        "    async with engine.begin() as connection:\n"
        "        connection.execute('INSERT INTO t VALUES (1)')\n"
    )
    vectors: list[tuple[str, str, int]] = [
        ("合规样本", good, 0),
        ("白名单放行", own, 0),
        ("模块级 pytestmark 放行", module_mark, 0),
        ("内存库不算自建库", 'def test_a():\n    e = create_async_engine("sqlite+aiosqlite://")\n', 0),
        ("连接串字面量", literal, 1),
        ("直接构造引擎", engine, 1),
        ("原生 SQL 裸 int ID", raw_sql, 1),
        ("无 where 清理", cleanup, 1),
        ("建表 DDL", create_all, 1),
        ("原生 TRUNCATE", truncate, 1),
        ("原生 DROP TABLE", drop_table, 1),
        ("假等待 sleep", wait, 1),
        ("SQL 无 where 删除", sql_delete, 1),
        ("非 SQL 的 delete 不误判", cache_delete, 0),
        ("注释与字符串不误判", commented, 0),
        ("门外直连写", gate_bypass, 2),
        ("门区间内直连写放行", gate_wrapped, 1),
        ("门外裸会话写", raw_session, 2),
        ("独立库白名单放行门外写", marked_bypass, 0),
        ("行尾 own_db 放行门外写", lines_own_db, 1),
    ]
    bad = 0
    for title, text, expect in vectors:
        found = scan_text("backend/tests/test_demo.py", text, rule)
        flag = "OK" if len(found) == expect else "FAIL"
        bad += flag == "FAIL"
        out(f"  [{flag}] {title}：期望 {expect} 项，实得 {len(found)} 项")
    gate_vectors: list[tuple[str, str, int]] = [
        ("直连共享库未取门", "def helper(url):\n    return create_async_engine(url)\n", 1),
        ("内存库不算直连", 'def helper():\n    return create_async_engine("sqlite+aiosqlite://")\n', 0),
        (
            "辅助函数白名单放行",
            "import pytest\n\n\n"
            '@pytest.mark.own_db("确认是独立库")\n'
            "def helper(url):\n    return create_async_engine(url)\n",
            0,
        ),
        (
            "辅助函数门外直连写",
            "def helper(url):\n"
            "    engine = create_async_engine(url)\n"
            "    with engine.begin() as connection:\n"
            "        connection.execute('INSERT INTO t VALUES (1)')\n",
            2,
        ),
    ]
    for title, text, expect in gate_vectors:
        found = scan_gate_text("backend/tests_support/demo.py", text, rule)
        flag = "OK" if len(found) == expect else "FAIL"
        bad += flag == "FAIL"
        out(f"  [{flag}] G5 {title}：期望 {expect} 项，实得 {len(found)} 项")
    out(
        f"\n[check_test_db_isolation self-test] {'通过' if not bad else '不通过'}："
        f"断言用例 {len(vectors) + len(gate_vectors)} 个"
    )
    return 1 if bad else 0


def run(
    cfg: Config,
    *,
    as_self_test: bool = False,
    files_from: str = "",
    list_own_db: bool = False,
    update_baseline: bool = False,
) -> int:
    """执行测试库隔离静态护栏。

    Args:
        cfg: 仓配置。
        as_self_test: 是否只跑内置自检。
        files_from: 变更清单文件（空串表示全量）。
        list_own_db: 只导出白名单清单。
        update_baseline: 按当前扫描结果重写存量基线。

    Returns:
        int: 退出码（0 通过 / 1 有问题 / 2 规则缺失）。
    """
    try:
        rule = require(cfg.test_db_isolation_rules, "test_db_isolation", MIN_KEYS)
    except RulesError as exc:
        out(f"[check_test_db_isolation] {exc}")
        return 2
    if as_self_test:
        return self_test(rule)
    files = _iter_files(cfg.root, rule, files_from)
    gate_files = _iter_gate_files(cfg.root, rule, files_from)
    if list_own_db:
        entries = [
            entry
            for rel_path in [*files, *gate_files]
            for entry in collect_own_db(rel_path, read_text(cfg.root / rel_path), rule)
        ]
        out(f"[check_test_db_isolation] 白名单 {len(entries)} 条")
        for entry in entries:
            out(f"  - {entry}")
        return 0
    hits = [hit for rel_path in files for hit in scan_hits(rel_path, read_text(cfg.root / rel_path), rule)]
    hits.extend(
        hit for rel_path in gate_files for hit in scan_gate_hits(rel_path, read_text(cfg.root / rel_path), rule)
    )
    baseline_path = cfg.root / rule.baseline if rule.baseline else None
    if update_baseline:
        if baseline_path is None:
            out("[check_test_db_isolation] 未配置 baseline，无法写入基线（请先在 [rules.test_db_isolation] 登记路径）")
            return 2
        count = write_baseline(baseline_path, hits, rule)
        out(f"[check_test_db_isolation] 存量基线已更新：{count} 条 → {rule.baseline}")
        return 0
    try:
        baseline = read_baseline(baseline_path, rule) if baseline_path is not None else {}
    except BaselineError as exc:
        out(f"[check_test_db_isolation] {exc}")
        return 2
    counts = tally(hits)
    excess = {key: count - baseline.get(key, 0) for key, count in counts.items() if count > baseline.get(key, 0)}
    if excess:
        total = sum(excess.values())
        out(f"[check_test_db_isolation] 不通过：新增 {total} 项（存量基线 {rule.baseline or '未启用'}）")
        for hit in hits:
            key = (hit.file, hit.code)
            if key in excess and excess[key] > 0:
                detail = f"{hit.file}:{hit.lineno}: {hit.message}"
                out(f"  - {key[1]} 新增（基线 {baseline.get(key, 0)} → 现 {counts[key]}）：{detail}")
        return 1
    shrink = {key: baseline[key] - counts.get(key, 0) for key in baseline if baseline[key] > counts.get(key, 0)}
    if shrink:
        out(
            f"[check_test_db_isolation] 通过（{len(files)} 个测试文件 + {len(gate_files)} 个辅助模块）；"
            f"另有 {len(shrink)} 个文件/规则码的存量比基线少，可跑 --update-baseline 递减基线"
        )
        return 0
    out(
        f"[check_test_db_isolation] 通过：{len(files)} 个测试文件 + {len(gate_files)} 个辅助模块"
        "无自建库 / 裸 int ID / 清理他人数据 / 假等待 / 直连共享库 / 门外写"
    )
    return 0
