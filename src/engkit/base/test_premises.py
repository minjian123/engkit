"""测试状态前提静态护栏（`engkit base test-premises`）。

口径（13_10，2026-11-23 业主拍板）：

- **P1 空库前提断言**：断言对象是**库的整表口径**（分页 `total` / 条数 `count`，或 `list` / `items` /
  `rows` 全等空列表）时，必须写明「本次写入」前提才算合规。写法：断言行或所属函数头部注
  `# premise: own-data <说明>`——说明要能复核「这条断言只看本用例自己写的行」。
  真实运行里库长期累积、他人随时写入，把 `total == 0` 当稳态是错的。
- **P2 逐例重装**：`service_app` 一类「建应用 + 走 lifespan」的夹具不得留在函数级——进程长驻，
  启动只发生一次。终态 scope 为 `module` / `session` / `package`；确不能常驻的在夹具头部注
  `# premise: per-case <说明>`。
- **P3 独占库滥用**：`@pytest.mark.own_db("理由", scope="…")` 必须带 `scope`，取值限
  `[rules.test_premises].own_scopes`；`external`（真外部服务联调）仅当同一作用域带
  `integration` 标记时成立，避免被当成逃生口。
- **P4 全局状态独占锁**：`file_lock("platform-global-state")` 这类「整段全局状态独占」在真实系统
  里不存在，使用面清零（存量进基线只减不增）。
- **P5 全局装配状态写入**：用例不得改「全系统装配一次」的结果——`app.dependency_overrides[...] = ...`
  （含整体赋值、`.clear()` / `.update()` / `.pop()`）、`app.state.<属性> = ...`、
  `get_settings()` 返回值或 `settings` 变量的属性赋值，以及 `monkeypatch.setattr(...)` / `delattr`
  指向这三类容器。真实运行里装配结果由系统装配器唯一决定，用例改它等于伪造运行形态。
  确认为了让装配模型统一，P5 **不留行内豁免**，存量只能靠基线递减。

判定用 AST（不用正则），避免注释与字符串误判；支持 `--files-from` 增量、`--list-own-db` 台账导出、
`--update-baseline` 递减。存量违规由 `[rules.test_premises].baseline` 快照登记，**只减不增**。
"""

from __future__ import annotations

import ast
import json
import sys
from pathlib import Path
from typing import NamedTuple

from ..config import Config, TestPremisesRules
from ..console import out
from .rules import RulesError, read_text, require
from .scan_scope import STDIN_LIST

MIN_KEYS = ("scan_targets",)
"""未登记时报错给出的最小键指引。"""

P1 = "P1"
P2 = "P2"
P3 = "P3"
P4 = "P4"
P5 = "P5"
"""规则码（基线快照按「文件 + 规则码」计数，行号变动不失效）。"""

_FIXTURE_DECORATORS = ("fixture",)
"""夹具装饰器名（`pytest.fixture` / `pytest_asyncio.fixture` 同形）。"""

_OWN_DATA_KIND = "own-data"
"""P1 豁免注释的类别词。"""

_PER_CASE_KIND = "per-case"
"""P2 豁免注释的类别词。"""


class Hit(NamedTuple):
    """一处命中。

    Attributes:
        file: 相对仓库根的 POSIX 路径。
        code: 规则码（P1-P4）。
        lineno: 行号（仅用于报告）。
        message: 问题描述。
    """

    file: str
    code: str
    lineno: int
    message: str


class BaselineError(Exception):
    """基线快照非法（格式 / 版本 / 字段）。"""


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


def _dotted(node: ast.AST) -> str:
    """拼属性链文本（`app.state.x` / `get_settings()`），用于匹配容器。

    Args:
        node: 表达式节点。

    Returns:
        str: 点分文本；无法识别的节点返回空串。
    """
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        base = _dotted(node.value)
        return f"{base}.{node.attr}" if base else node.attr
    if isinstance(node, ast.Call):
        base = _dotted(node.func)
        return f"{base}()" if base else ""
    return ""


def _write_targets(node: ast.AST) -> list[ast.expr]:
    """展开赋值目标（元组 / 列表解包逐个取出）。

    Args:
        node: 赋值目标节点。

    Returns:
        list[ast.expr]: 叶子目标清单。
    """
    if isinstance(node, (ast.Tuple, ast.List)):
        leaves: list[ast.expr] = []
        for element in node.elts:
            leaves.extend(_write_targets(element))
        return leaves
    if isinstance(node, ast.Starred):
        return [node.value]
    if isinstance(node, ast.expr):
        return [node]
    return []


def _is_empty_literal(node: ast.AST) -> bool:
    """判字面空集合（`[]` / `{}` / `()` / `set()`）。

    AST 里容器的空字面量不是 `Constant`，必须按容器节点判，否则「与空列表全等」整类漏判。

    Args:
        node: 比较右值节点。

    Returns:
        bool: 是空字面量返回 True。
    """
    if isinstance(node, (ast.List, ast.Tuple)) and not node.elts:
        return True
    if isinstance(node, ast.Dict) and not node.keys:
        return True
    return isinstance(node, ast.Call) and _call_name(node) == "set" and not node.args


def _decorator_attrs(node: ast.AST) -> list[str]:
    """取节点装饰器名清单（`pytest.mark.own_db(...)` → `own_db`）。

    Args:
        node: 模块 / 类 / 函数节点。

    Returns:
        list[str]: 装饰器属性名清单。
    """
    names: list[str] = []
    for decorator in getattr(node, "decorator_list", []):
        target = decorator.func if isinstance(decorator, ast.Call) else decorator
        if isinstance(target, ast.Attribute):
            names.append(target.attr)
    return names


def _marker_call(decorator: ast.AST, marker: str) -> ast.Call | None:
    """装饰器是否是指定 marker 的调用形式。

    Args:
        decorator: 装饰器节点。
        marker: marker 名（如 `own_db`）。

    Returns:
        ast.Call | None: 命中时返回调用节点，否则 None。
    """
    if not isinstance(decorator, ast.Call) or not isinstance(decorator.func, ast.Attribute):
        return None
    return decorator if decorator.func.attr == marker else None


def _marker_scope(call: ast.Call) -> str:
    """取 marker 的 `scope=` 关键字（缺省空串）。

    Args:
        call: marker 调用节点。

    Returns:
        str: scope 取值；未给或非字符串常量返回空串。
    """
    for keyword in call.keywords:
        if keyword.arg == "scope" and isinstance(keyword.value, ast.Constant) and isinstance(keyword.value.value, str):
            return keyword.value.value
    return ""


def _marker_reason(call: ast.Call) -> str:
    """取 marker 的第一个字符串理由参数（缺省空串）。

    Args:
        call: marker 调用节点。

    Returns:
        str: 理由；缺省返回空串。
    """
    for arg in call.args:
        if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
            return arg.value
    return ""


def _pytestmark_entries(tree: ast.Module) -> list[ast.AST]:
    """取模块级 `pytestmark` 的全部条目（调用式与裸标记都算）。

    Args:
        tree: 模块 AST。

    Returns:
        list[ast.AST]: `pytest.mark.x` 或 `pytest.mark.x(...)` 条目清单。
    """
    entries: list[ast.AST] = []
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
        entries.extend(elements)
    return entries


def _pytestmark_calls(tree: ast.Module, marker: str) -> list[ast.Call]:
    """取模块级 `pytestmark` 里指定 marker 的调用节点。

    为什么需要：`pytestmark = [pytest.mark.own_db(...)]` 是整份文件打标的标准写法，只判装饰器
    会把这类文件误判成「缺 scope」。

    Args:
        tree: 模块 AST。
        marker: marker 名（如 `own_db`）。

    Returns:
        list[ast.Call]: 命中的调用节点。
    """
    return [
        entry
        for entry in _pytestmark_entries(tree)
        if isinstance(entry, ast.Call) and isinstance(entry.func, ast.Attribute) and entry.func.attr == marker
    ]


def _pytestmark_has(tree: ast.Module, marker: str) -> bool:
    """模块级 `pytestmark` 是否声明了指定标记（`pytest.mark.integration` 与带参形式同判）。

    Args:
        tree: 模块 AST。
        marker: 标记名。

    Returns:
        bool: 命中返回 True。
    """
    for entry in _pytestmark_entries(tree):
        target = entry.func if isinstance(entry, ast.Call) else entry
        if isinstance(target, ast.Attribute) and target.attr == marker:
            return True
    return False


class _Scanner(ast.NodeVisitor):
    """按作用域扫描 P1-P4 的访问器。"""

    def __init__(self, rel_path: str, rule: TestPremisesRules, lines: list[str]) -> None:
        """初始化。

        Args:
            rel_path: 相对仓库根的 POSIX 路径。
            rule: 规则段。
            lines: 源文件按行切分（豁免注释按行判定）。
        """
        self.rel_path = rel_path
        self.rule = rule
        self.lines = lines
        self.hits: list[Hit] = []
        self._headers: list[range] = []
        self._module_integration = False

    def _premise_kind(self, lineno: int) -> str:
        """取某行的豁免注释类别（`# premise: <kind> <说明>`）。

        Args:
            lineno: 行号（1 起）。

        Returns:
            str: 类别词（`own-data` / `per-case` 等）；无注释或说明为空返回空串。
        """
        if not 1 <= lineno <= len(self.lines):
            return ""
        line = self.lines[lineno - 1]
        index = line.find(self.rule.premise_marker)
        if index < 0 or line.find("#", 0, index) < 0:
            return ""
        rest = line[index + len(self.rule.premise_marker) :].strip()
        parts = rest.split(None, 1)
        if len(parts) < 2 or not parts[1].strip():
            return ""
        return parts[0]

    def _header_lines(self, node: ast.AST) -> range:
        """取节点头部行区间（装饰器首行到 `def` / `class` 行）。

        Args:
            node: 函数 / 类节点。

        Returns:
            range: 头部行号区间（含端点；以 range(start, end + 1) 表达）。
        """
        decorators = getattr(node, "decorator_list", [])
        start = min((getattr(item, "lineno", 1) for item in decorators), default=getattr(node, "lineno", 1))
        return range(start, getattr(node, "lineno", start) + 1)

    def _exempt(self, lineno: int, kind: str) -> bool:
        """断言行或所属作用域头部是否声明了指定类别的豁免。

        Args:
            lineno: 命中行号。
            kind: 期望的豁免类别（`own-data` / `per-case`）。

        Returns:
            bool: 已声明返回 True。
        """
        for candidate in (lineno, *[line for header in self._headers for line in header]):
            if self._premise_kind(candidate) == kind:
                return True
        return False

    def _enter(self, node: ast.AST) -> None:
        """进入作用域：记录头部行区间后递归。

        Args:
            node: 模块 / 类 / 函数节点。
        """
        header = self._header_lines(node)
        self._headers.append(header)
        self.generic_visit(node)
        self._headers.pop()

    def visit_Module(self, node: ast.Module) -> None:
        """模块作用域。

        Args:
            node: 模块节点。
        """
        self._module_integration = _pytestmark_has(node, self.rule.integration_marker)
        for call in _pytestmark_calls(node, self.rule.own_marker):
            self._check_scope(call.lineno, _marker_scope(call), self._module_integration)
        self._enter(node)

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        """类作用域。

        Args:
            node: 类节点。
        """
        self._check_own_marker(node)
        self._enter(node)

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        """函数作用域。

        Args:
            node: 函数节点。
        """
        self._check_fixture(node)
        self._check_own_marker(node)
        self._enter(node)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        """异步函数作用域。

        Args:
            node: 函数节点。
        """
        self._check_fixture(node)
        self._check_own_marker(node)
        self._enter(node)

    def _check_fixture(self, node: ast.FunctionDef | ast.AsyncFunctionDef) -> None:
        """P2：装配夹具的 scope 必须常驻。

        Args:
            node: 函数节点。
        """
        if node.name not in self.rule.fixture_names:
            return
        decorators = getattr(node, "decorator_list", [])
        if not any(attr in _FIXTURE_DECORATORS for attr in _decorator_attrs(node)):
            return
        scope = "function"
        for decorator in decorators:
            if not isinstance(decorator, ast.Call):
                continue
            for keyword in decorator.keywords:
                if (
                    keyword.arg == "scope"
                    and isinstance(keyword.value, ast.Constant)
                    and isinstance(keyword.value.value, str)
                ):
                    scope = keyword.value.value
        if scope in self.rule.resident_scopes:
            return
        header = self._header_lines(node)
        if any(self._premise_kind(line) == _PER_CASE_KIND for line in header):
            return
        self._report(
            node.lineno,
            P2,
            f"P2 逐例重装：夹具 {node.name} 的 scope 为 {scope}（应为 {'/'.join(self.rule.resident_scopes)}）"
            f"；确不能常驻请在本行注 `# {self.rule.premise_marker} {_PER_CASE_KIND} <说明>`",
        )

    def _check_own_marker(self, node: ast.AST) -> None:
        """P3：`own_db` 必须带合法 scope；`external` 必须同时带 integration 标记。

        Args:
            node: 类 / 函数节点。
        """
        for decorator in getattr(node, "decorator_list", []):
            call = _marker_call(decorator, self.rule.own_marker)
            if call is None:
                continue
            integration = self._module_integration or self.rule.integration_marker in _decorator_attrs(node)
            self._check_scope(getattr(call, "lineno", 0), _marker_scope(call), integration)

    def _check_scope(self, lineno: int, scope: str, integration: bool) -> None:
        """校验一处 `own_db` 的 scope 声明。

        Args:
            lineno: 行号。
            scope: 声明的 scope（空串表示未声明）。
            integration: 同作用域是否带 integration 标记。
        """
        if not scope:
            self._report(
                lineno,
                P3,
                f"P3 独占库滥用：{self.rule.own_marker} 缺 scope 分类"
                f"（取值限 {'/'.join(self.rule.own_scopes)}；口径见《测试规范》§2.1）",
            )
            return
        if scope not in self.rule.own_scopes:
            self._report(
                lineno,
                P3,
                f"P3 独占库滥用：scope=`{scope}` 不在允许集合（{'/'.join(self.rule.own_scopes)}）",
            )
            return
        if scope == self.rule.external_scope and not integration:
            self._report(
                lineno,
                P3,
                f"P3 独占库滥用：scope=`{scope}` 只允许真外部服务联调用例（须同时带 "
                f"`{self.rule.integration_marker}` 标记）",
            )

    def visit_Call(self, node: ast.Call) -> None:
        """P4：全局状态独占锁；P5：容器方法与反射写属性。

        Args:
            node: 调用节点。
        """
        if _call_name(node) in self.rule.lock_calls and node.args:
            first = node.args[0]
            if isinstance(first, ast.Constant) and first.value in self.rule.lock_targets:
                self._report(
                    node.lineno,
                    P4,
                    f"P4 全局状态独占锁：{_call_name(node)}(`{first.value}`) 把整段全局状态独占；"
                    "真实系统里没有这种窗口，改用唯一命名空间 + 只处理本用例数据",
                )
        if isinstance(node.func, ast.Attribute):
            container = _dotted(node.func.value)
            if node.func.attr in self.rule.state_methods and self._is_state_container(container):
                self._report(
                    node.lineno,
                    P5,
                    f"P5 全局装配状态写入：`{ast.unparse(node)}` 用容器方法改了装配结果"
                    "（装配结果由系统装配器唯一决定，改为请求级注入或自带应用）",
                )
            elif node.func.attr in self.rule.setattr_calls and node.args:
                target = _dotted(node.args[0])
                if self._is_state_container(target):
                    self._report(
                        node.lineno,
                        P5,
                        f"P5 全局装配状态写入：`{ast.unparse(node)}` 反射改了装配结果"
                        "（装配结果由系统装配器唯一决定，改为请求级注入或自带应用）",
                    )
        self.generic_visit(node)

    def visit_Assign(self, node: ast.Assign) -> None:
        """P5：赋值写装配状态（含解包目标）。

        Args:
            node: 赋值节点。
        """
        for target in node.targets:
            self._check_write(target, node.lineno)
        self.generic_visit(node)

    def visit_AnnAssign(self, node: ast.AnnAssign) -> None:
        """P5：带注解赋值写装配状态（仅当有右值）。

        Args:
            node: 赋值节点。
        """
        if node.value is not None:
            self._check_write(node.target, node.lineno)
        self.generic_visit(node)

    def visit_AugAssign(self, node: ast.AugAssign) -> None:
        """P5：增量赋值写装配状态。

        Args:
            node: 赋值节点。
        """
        self._check_write(node.target, node.lineno)
        self.generic_visit(node)

    def _is_state_container(self, text: str) -> bool:
        """判表达式是否指向受保护的装配状态容器。

        Args:
            text: 属性链文本（`app.state` / `app.dependency_overrides` / `get_settings()`）。

        Returns:
            bool: 命中装配状态返回 True。
        """
        if not text:
            return False
        if text.endswith("()"):
            return text[:-2].split(".")[-1] in self.rule.settings_getters
        segments = text.split(".")
        if segments[0] in self.rule.settings_names:
            return True
        last = segments[-1]
        if last in self.rule.override_attrs:
            return True
        if len(segments) >= 2 and last in self.rule.state_attr_names:
            return True
        return len(segments) >= 2 and segments[-2] in self.rule.state_attr_names

    def _check_write(self, target: ast.expr, lineno: int) -> None:
        """P5：赋值目标是否落在装配状态上。

        Args:
            target: 赋值目标节点。
            lineno: 行号。
        """
        for leaf in _write_targets(target):
            container = False
            if isinstance(leaf, ast.Subscript):
                base: ast.expr = leaf.value
                while isinstance(base, ast.Subscript):
                    base = base.value
                container = self._is_state_container(_dotted(base))
            elif isinstance(leaf, ast.Attribute):
                container = self._is_state_container(_dotted(leaf)) or self._is_state_container(_dotted(leaf.value))
            if not container:
                continue
            self._report(
                lineno,
                P5,
                f"P5 全局装配状态写入：`{ast.unparse(leaf)}` 改了装配结果"
                "（装配结果由系统装配器唯一决定，改为请求级注入或自带应用）",
            )

    def visit_Assert(self, node: ast.Assert) -> None:
        """P1：整表口径断言无「本次写入」前提。

        Args:
            node: 断言节点。
        """
        test = node.test
        if isinstance(test, ast.Compare) and len(test.ops) == 1 and isinstance(test.ops[0], ast.Eq):
            left, right = test.left, test.comparators[0]
            if not isinstance(left, ast.Subscript) and isinstance(right, ast.Subscript):
                left, right = right, left
            self._check_premise(node.lineno, left, right)
        self.generic_visit(node)

    def _check_premise(self, lineno: int, left: ast.expr, right: ast.expr) -> None:
        """判定一处「字段 ↔ 字面空值」比较是否属整表口径断言。

        Args:
            lineno: 断言行号。
            left: 比较左值。
            right: 比较右值。
        """
        if not isinstance(left, ast.Subscript):
            return
        key = left.slice.value if isinstance(left.slice, ast.Constant) else None
        zero = (
            isinstance(right, ast.Constant)
            and isinstance(right.value, int)
            and not isinstance(right.value, bool)
            and right.value == 0
        )
        whole = (key in self.rule.aggregate_keys and zero) or (key in self.rule.list_keys and _is_empty_literal(right))
        if not whole or self._exempt(lineno, _OWN_DATA_KIND):
            return
        self._report(
            lineno,
            P1,
            f"P1 空库前提断言：断言 `{key}` 的整表口径（库里有他人数据时不成立）"
            f"；只断言本次写入请在本行或所属函数头部注 `# {self.rule.premise_marker} {_OWN_DATA_KIND} <说明>`",
        )

    def _report(self, lineno: int, code: str, message: str) -> None:
        """记录命中。

        Args:
            lineno: 行号。
            code: 规则码。
            message: 描述。
        """
        self.hits.append(Hit(self.rel_path, code, lineno, message))


def scan_hits(rel_path: str, text: str, rule: TestPremisesRules) -> list[Hit]:
    """扫描单个文件文本（结构化命中，供基线与增量判定使用）。

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
    scanner = _Scanner(rel_path, rule, text.splitlines())
    scanner.visit(tree)
    return scanner.hits


def scan_text(rel_path: str, text: str, rule: TestPremisesRules) -> list[str]:
    """扫描单个文件文本（人读格式）。

    Args:
        rel_path: 相对仓库根的 POSIX 路径。
        text: 文件全文。
        rule: 规则段。

    Returns:
        list[str]: 问题清单（`路径:行号: 描述`）。
    """
    return [f"{hit.file}:{hit.lineno}: {hit.message}" for hit in scan_hits(rel_path, text, rule)]


def collect_own_db(rel_path: str, text: str, rule: TestPremisesRules) -> list[str]:
    """导出 `own_db` 台账（文件 / 作用域 / scope / 理由）。

    Args:
        rel_path: 相对仓库根的 POSIX 路径。
        text: 文件全文。
        rule: 规则段。

    Returns:
        list[str]: `路径::作用域::scope::理由` 清单；未声明时标注「缺 scope」/「缺理由」。
    """
    try:
        tree = ast.parse(text)
    except SyntaxError:
        return []
    entries: list[str] = []
    for call in _pytestmark_calls(tree, rule.own_marker):
        entries.append(_entry(rel_path, "<module>", call))
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            continue
        for decorator in getattr(node, "decorator_list", []):
            call = _marker_call(decorator, rule.own_marker)
            if call is not None:
                entries.append(_entry(rel_path, node.name, call))
    return entries


def _entry(rel_path: str, scope_name: str, call: ast.Call) -> str:
    """拼一条台账记录。

    Args:
        rel_path: 文件相对路径。
        scope_name: 作用域名（`<module>` 或函数 / 类名）。
        call: marker 调用节点。

    Returns:
        str: `路径::作用域::scope::理由`。
    """
    scope = _marker_scope(call) or "缺 scope"
    reason = _marker_reason(call) or "缺理由"
    return f"{rel_path}::{scope_name}::{scope}::{reason}"


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


def read_baseline(path: Path, rule: TestPremisesRules) -> dict[tuple[str, str], int]:
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


def write_baseline(path: Path, hits: list[Hit], rule: TestPremisesRules) -> int:
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
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="\n")
    return len(entries)


def _iter_files(root: Path, rule: TestPremisesRules, files_from: str) -> list[str]:
    """枚举待扫描文件（`test_*.py` 与 `conftest.py`，相对根 POSIX 路径）。

    为什么含 `conftest.py`：P2 判的是装配夹具，它定义在 conftest 里而不在用例文件里；
    只扫 `test_*.py` 会让「逐例重装」整片漏掉。

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
        return [
            path
            for path in candidates
            if (root / path).is_file()
            and (Path(path).name == "conftest.py" or Path(path).name.startswith("test_"))
            and any(path.startswith(f"{target.strip('/')}/") for target in rule.scan_targets)
        ]
    files: list[str] = []
    for target in rule.scan_targets:
        base = root / target
        if not base.exists():
            continue
        for path in sorted(base.rglob("*.py")):
            if set(path.relative_to(root).parts) & set(rule.exclude_parts):
                continue
            if path.name != "conftest.py" and not path.name.startswith("test_"):
                continue
            files.append(path.relative_to(root).as_posix())
    return files


def self_test(rule: TestPremisesRules) -> int:
    """自检：P1-P4 正例 / 反例 / 豁免注释 / 值域校验。

    Args:
        rule: 规则段。

    Returns:
        int: 退出码（0 通过 / 1 失败）。
    """
    p1_hit = (
        'async def test_a(client):\n    body = (await client.get("/x")).json()\n    assert body["data"]["total"] == 0\n'
    )
    p1_ok = (
        'async def test_a(client):\n    body = (await client.get("/x")).json()\n'
        '    assert body["data"]["total"] == 0  # premise: own-data 查询按本用例前缀过滤\n'
    )
    p1_header = (
        "async def test_a(client):  # premise: own-data 查询按本用例前缀过滤\n"
        '    assert (await client.get("/x")).json()["total"] == 0\n'
    )
    p1_list = 'def test_b(body):\n    assert body["items"] == []\n'
    p1_other = (
        'def test_c(body):\n    assert body["name"] == 0\n    assert body["total"] == 3\n    assert body.total == 0\n'
    )
    p1_blank_reason = 'def test_d(body):\n    assert body["count"] == 0  # premise: own-data\n'
    p2_hit = "import pytest\n\n\n@pytest.fixture\nasync def service_app():\n    yield 1\n"
    p2_ok = 'import pytest\n\n\n@pytest.fixture(scope="module")\nasync def service_app():\n    yield 1\n'
    p2_plain = "async def service_app():\n    yield 1\n"
    p2_exempt = (
        "import pytest\n\n\n@pytest.fixture  # premise: per-case 该服务没有长驻对端\n"
        "async def service_app():\n    yield 1\n"
    )
    p3_missing = 'import pytest\n\n\n@pytest.mark.own_db("建库即被测")\ndef test_a():\n    pass\n'
    p3_ok = 'import pytest\n\n\n@pytest.mark.own_db("建库即被测", scope="ddl")\ndef test_a():\n    pass\n'
    p3_bad = 'import pytest\n\n\n@pytest.mark.own_db("全库口径", scope="session-global")\ndef test_a():\n    pass\n'
    p3_external = 'import pytest\n\n\n@pytest.mark.own_db("真机联调", scope="external")\ndef test_a():\n    pass\n'
    p3_external_ok = (
        "import pytest\n\npytestmark = [pytest.mark.integration]\n\n\n"
        '@pytest.mark.own_db("真机联调", scope="external")\ndef test_a():\n    pass\n'
    )
    p4_hit = 'def test_a():\n    with file_lock("platform-global-state", timeout=180.0):\n        pass\n'
    p4_other = 'def test_a():\n    with file_lock("other-lock", timeout=180.0):\n        pass\n'
    p5_override = "def test_a(app, dep, fn):\n    app.dependency_overrides[dep] = fn\n"
    p5_override_all = "def test_a(app):\n    app.dependency_overrides = {}\n"
    p5_state = "def test_a(app):\n    app.state.registry = None\n"
    p5_state_method = "def test_a(app):\n    app.state.registry.clear()\n"
    p5_pop = "def test_a(app, key):\n    app.dependency_overrides.pop(key)\n"
    p5_setattr_state = 'def test_a(app, monkeypatch):\n    monkeypatch.setattr(app.state, "x", 1)\n'
    p5_setattr_settings = 'def test_a(settings, monkeypatch):\n    monkeypatch.setattr(settings, "x", 1)\n'
    p5_settings_var = "def test_a(settings):\n    settings.max_active = 3\n"
    p5_settings_getter = "def test_a():\n    get_settings().max_active = 3\n"
    p5_read = "def test_a(app):\n    assert app.state.registry is None\n    assert app.dependency_overrides == {}\n"
    p5_setattr_other = 'def test_a(client, monkeypatch):\n    monkeypatch.setattr(client, "x", 1)\n'
    p5_local_cache = 'def test_a():\n    cache = {}\n    cache["k"] = 1\n    state = {}\n    state["k"] = 1\n'
    p5_getter_copy = "def test_a():\n    assert get_settings().model_copy().x == 1\n"
    broken = "def (:\n"
    module_mark = "import pytest\n\npytestmark = [pytest.mark.own_db('整份文件建库即被测', scope='ddl')]\n"
    module_mark_missing = "import pytest\n\npytestmark = [pytest.mark.own_db('整份文件建库即被测')]\n"
    vectors: list[tuple[str, str, list[str]]] = [
        ("整表口径断言被拦", p1_hit, [P1]),
        ("行尾前提注释放行", p1_ok, []),
        ("函数头部前提注释放行", p1_header, []),
        ("列表全等空被拦", p1_list, [P1]),
        ("非整表口径不误判", p1_other, []),
        ("豁免说明为空仍拦", p1_blank_reason, [P1]),
        ("函数级装配夹具被拦", p2_hit, [P2]),
        ("模块级装配夹具放行", p2_ok, []),
        ("非夹具同名函数不判", p2_plain, []),
        ("逐例装配注释放行", p2_exempt, []),
        ("缺 scope 被拦", p3_missing, [P3]),
        ("合法 scope 放行", p3_ok, []),
        ("非法 scope 被拦", p3_bad, [P3]),
        ("external 无 integration 被拦", p3_external, [P3]),
        ("external 带 integration 放行", p3_external_ok, []),
        ("全局状态锁被拦", p4_hit, [P4]),
        ("其它锁不误判", p4_other, []),
        ("依赖覆盖下标写被拦", p5_override, [P5]),
        ("依赖覆盖整体写被拦", p5_override_all, [P5]),
        ("应用状态属性写被拦", p5_state, [P5]),
        ("应用状态容器方法被拦", p5_state_method, [P5]),
        ("依赖覆盖弹出被拦", p5_pop, [P5]),
        ("反射改应用状态被拦", p5_setattr_state, [P5]),
        ("反射改全局配置被拦", p5_setattr_settings, [P5]),
        ("全局配置变量属性写被拦", p5_settings_var, [P5]),
        ("全局配置取值链写被拦", p5_settings_getter, [P5]),
        ("只读装配状态不误判", p5_read, []),
        ("反射改普通对象不误判", p5_setattr_other, []),
        ("局部变量与字典写不误判", p5_local_cache, []),
        ("配置副本读取不误判", p5_getter_copy, []),
        ("语法错误不崩", broken, []),
        ("模块级 marker 合法放行", module_mark, []),
        ("模块级 marker 缺 scope 被拦", module_mark_missing, [P3]),
    ]
    bad = 0
    for title, text, expect in vectors:
        found = [hit.code for hit in scan_hits("backend/tests/test_demo.py", text, rule)]
        flag = "OK" if found == expect else "FAIL"
        bad += flag == "FAIL"
        out(f"  [{flag}] {title}：期望 {expect or '无命中'}，实得 {found or '无命中'}")
    out(f"\n[check_test_premises self-test] {'通过' if not bad else '不通过'}：断言用例 {len(vectors)} 个")
    return 1 if bad else 0


def run(
    cfg: Config,
    *,
    as_self_test: bool = False,
    files_from: str = "",
    list_own_db: bool = False,
    update_baseline: bool = False,
) -> int:
    """执行测试状态前提静态护栏。

    Args:
        cfg: 仓配置。
        as_self_test: 是否只跑内置自检。
        files_from: 变更清单文件（空串表示全量）。
        list_own_db: 只导出 `own_db` 台账。
        update_baseline: 按当前扫描结果重写存量基线。

    Returns:
        int: 退出码（0 通过 / 1 有问题 / 2 规则缺失）。
    """
    try:
        rule = require(cfg.test_premises_rules, "test_premises", MIN_KEYS)
    except RulesError as exc:
        out(f"[check_test_premises] {exc}")
        return 2
    if as_self_test:
        return self_test(rule)
    files = _iter_files(cfg.root, rule, files_from)
    if list_own_db:
        entries = [
            entry for rel_path in files for entry in collect_own_db(rel_path, read_text(cfg.root / rel_path), rule)
        ]
        out(f"[check_test_premises] 独占库台账 {len(entries)} 条")
        for entry in entries:
            out(f"  - {entry}")
        return 0
    hits = [hit for rel_path in files for hit in scan_hits(rel_path, read_text(cfg.root / rel_path), rule)]
    baseline_path = cfg.root / rule.baseline if rule.baseline else None
    if update_baseline:
        if baseline_path is None:
            out("[check_test_premises] 未配置 baseline，无法写入基线（请先在 [rules.test_premises] 登记路径）")
            return 2
        count = write_baseline(baseline_path, hits, rule)
        out(f"[check_test_premises] 存量基线已更新：{count} 条 → {rule.baseline}")
        return 0
    try:
        baseline = read_baseline(baseline_path, rule) if baseline_path is not None else {}
    except BaselineError as exc:
        out(f"[check_test_premises] {exc}")
        return 2
    counts = tally(hits)
    excess = {key: count - baseline.get(key, 0) for key, count in counts.items() if count > baseline.get(key, 0)}
    if excess:
        total = sum(excess.values())
        out(f"[check_test_premises] 不通过：新增 {total} 项（存量基线 {rule.baseline or '未启用'}）")
        for hit in hits:
            key = (hit.file, hit.code)
            if excess.get(key, 0) > 0:
                out(f"  - {hit.file}:{hit.lineno}: {hit.message}")
        return 1
    shrink = {key: baseline[key] - counts.get(key, 0) for key in baseline if baseline[key] > counts.get(key, 0)}
    if shrink:
        out(
            f"[check_test_premises] 通过（{len(files)} 个文件）；"
            f"另有 {len(shrink)} 个文件/规则码的存量比基线少，可跑 --update-baseline 递减基线"
        )
        return 0
    tolerated = sum(counts.values())
    if tolerated:
        out(
            f"[check_test_premises] 通过：{len(files)} 个文件；"
            f"存量 {tolerated} 项仍在基线内（只减不增，改造后跑 --update-baseline 递减）"
        )
        return 0
    out(
        f"[check_test_premises] 通过：{len(files)} 个文件无空库前提断言 / 逐例重装 / "
        "无分类独占库 / 全局状态锁 / 装配状态写入"
    )
    return 0
