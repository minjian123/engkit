"""测试库隔离静态护栏（`engkit base test-db-isolation`）。

口径（需求 04-6 / 任务 04-6-1，2026-11-01 拍板）：

- **G1 自建库**：测试代码不得为“图省事”自建 SQLite（连接串字面量 / 直接构造引擎 /
  `tmp_path` 派生 `*.db`）；“建库即被测”的用例打 `@pytest.mark.own_db("理由")` 进白名单。
- **G2 裸 int ID**：原生 SQL 的 ID 参数必须经测试基座的 `sqlite_id()` 包装；
  绑定 int 常量即违规（SQLite 落 38 位补零 TEXT，裸 int 查不到）。
- **G3 清理他人数据**：单库叠加下不得 `drop_all` / 无 `where` 的 `delete()`（白名单内的自建表除外）。

判定用 AST（不用正则），避免注释与字符串误判；支持 `--files-from` 增量与 `--list-own-db` 白名单导出。
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

from ..config import Config, TestDbIsolationRules
from ..console import out
from .rules import RulesError, read_text, require
from .scan_scope import STDIN_LIST

MIN_KEYS = ("scan_targets",)
"""未登记时报错给出的最小键指引。"""

_SQL_CALLS = ("execute", "exec_driver_sql")
"""原生 SQL 执行入口名（G2 判定范围）。"""

_DELETE_NAME = "delete"
"""SQLAlchemy 全表删除构造名（G3 判定范围）。"""


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

    def __init__(self, rel_path: str, rule: TestDbIsolationRules, *, sql_delete: bool = False) -> None:
        """初始化。

        Args:
            rel_path: 相对仓库根的 POSIX 路径。
            rule: 规则段。
            sql_delete: 模块是否从 `sqlalchemy` 导入了 `delete`（只在该前提下判 G3 的 `delete()`）。
        """
        self.rel_path = rel_path
        self.rule = rule
        self.sql_delete = sql_delete
        self.problems: list[str] = []
        self._allowed = 0

    def _scope(self, node: ast.AST) -> None:
        """进入作用域：带 marker 时整体放行。

        Args:
            node: 模块 / 类 / 函数节点。
        """
        allowed = _is_allowed(node, self.rule.allow_marker)
        if not allowed and isinstance(node, ast.Module):
            allowed = bool(_pytestmark_reason(node, self.rule.allow_marker))
        self._allowed += 1 if allowed else 0
        self.generic_visit(node)
        self._allowed -= 1 if allowed else 0

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
            self._report(node, "G1 自建 SQLite：连接串字面量（应用共享夹具；确需建库请打 own_db 白名单）")
        self.generic_visit(node)

    def visit_Call(self, node: ast.Call) -> None:
        """G1 引擎构造 / G2 裸 int ID / G3 全表清理。

        Args:
            node: 调用节点。
        """
        name = _call_name(node)
        if name in self.rule.engine_factories and not _engine_on_memory(node, self.rule):
            self._report(node, f"G1 自建 SQLite：直接构造引擎 {name}()（应用共享夹具）")
        if name in self.rule.cleanup_calls:
            self._report(node, f"G3 清理他人数据：{name}() 会清空共享库（确需请打 own_db 白名单）")
        if name == _DELETE_NAME and self.sql_delete and isinstance(node.func, ast.Name) and len(node.args) == 1:
            self._report(node, "G3 清理他人数据：delete() 未带 where 条件（单库叠加下会删掉他人数据）")
        if name in _SQL_CALLS:
            self._check_raw_sql(node)
        self.generic_visit(node)

    def visit_BinOp(self, node: ast.BinOp) -> None:
        """G1：`tmp_path / "x.db"` 派生临时库。

        Args:
            node: 二元运算节点。
        """
        if isinstance(node.op, ast.Div) and isinstance(node.right, ast.Constant):
            value = node.right.value
            if isinstance(value, str) and value.endswith(self.rule.db_file_suffixes):
                self._report(node, f"G1 自建 SQLite：临时目录派生库文件 {value}（应用共享夹具）")
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
        if not any(f"{column} =" in lowered or f"{column} in" in lowered for column in self.rule.id_columns):
            return
        for arg in node.args[1:]:
            if isinstance(arg, ast.Dict) and any(
                isinstance(value, ast.Constant) and isinstance(value.value, int) for value in arg.values
            ):
                self._report(node, "G2 裸 int ID：原生 SQL 绑定 int 常量（应经 sqlite_id() 转补零字符串）")

    def _report(self, node: ast.AST, message: str) -> None:
        """记录问题（白名单作用域内跳过）。

        Args:
            node: 命中节点。
            message: 问题描述。
        """
        if self._allowed:
            return
        self.problems.append(f"{self.rel_path}:{getattr(node, 'lineno', 0)}: {message}")


def scan_text(rel_path: str, text: str, rule: TestDbIsolationRules) -> list[str]:
    """扫描单个文件文本。

    Args:
        rel_path: 相对仓库根的 POSIX 路径。
        text: 文件全文。
        rule: 规则段。

    Returns:
        list[str]: 问题清单（`路径:行号: 描述`）。
    """
    try:
        tree = ast.parse(text)
    except SyntaxError:
        return []
    scanner = _Scanner(rel_path, rule, sql_delete=_has_sqlalchemy_delete(tree))
    scanner.visit(tree)
    return scanner.problems


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


def self_test(rule: TestDbIsolationRules) -> int:
    """自检：合规 / 三条违规 / 白名单 / 注释与字符串误判五类向量。

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
    cache_delete = "def test_e(cache):\n    cache.delete('k')\n"
    sql_delete = "from sqlalchemy import delete\n\n\ndef test_f(meta, table):\n    meta.execute(delete(table))\n"
    commented = "# create_async_engine('sqlite+aiosqlite:///x.db')\nDOC = 'delete() 与 drop_all() 的说明'\n"
    module_mark = (
        "import pytest\n\n"
        'pytestmark = [pytest.mark.own_db("整份文件的建库即被测")]\n\n\n'
        "def test_a():\n    e = create_async_engine('x')\n"
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
        ("SQL 无 where 删除", sql_delete, 1),
        ("非 SQL 的 delete 不误判", cache_delete, 0),
        ("注释与字符串不误判", commented, 0),
    ]
    bad = 0
    for title, text, expect in vectors:
        found = scan_text("backend/tests/test_demo.py", text, rule)
        flag = "OK" if len(found) == expect else "FAIL"
        bad += flag == "FAIL"
        out(f"  [{flag}] {title}：期望 {expect} 项，实得 {len(found)} 项")
    out(f"\n[check_test_db_isolation self-test] {'通过' if not bad else '不通过'}：断言用例 {len(vectors)} 个")
    return 1 if bad else 0


def run(
    cfg: Config,
    *,
    as_self_test: bool = False,
    files_from: str = "",
    list_own_db: bool = False,
) -> int:
    """执行测试库隔离静态护栏。

    Args:
        cfg: 仓配置。
        as_self_test: 是否只跑内置自检。
        files_from: 变更清单文件（空串表示全量）。
        list_own_db: 只导出白名单清单。

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
    if list_own_db:
        entries = [
            entry for rel_path in files for entry in collect_own_db(rel_path, read_text(cfg.root / rel_path), rule)
        ]
        out(f"[check_test_db_isolation] 白名单 {len(entries)} 条")
        for entry in entries:
            out(f"  - {entry}")
        return 0
    problems = [problem for rel_path in files for problem in scan_text(rel_path, read_text(cfg.root / rel_path), rule)]
    if problems:
        out(f"[check_test_db_isolation] 不通过：{len(problems)} 项")
        for item in problems:
            out(f"  - {item}")
        return 1
    out(f"[check_test_db_isolation] 通过：{len(files)} 个测试文件无自建库 / 裸 int ID / 清理他人数据")
    return 0
