"""集合声明静态护栏（`engkit base bare-collections`）。

口径：集合声明一律落插入序集合类（白名单），**未命中白名单即违规**；覆盖类字段注解、函数签名注解
（参数 / 返回）与函数体带注解局部变量。存量以基线快照豁免、新增违规即失败。扫描面、白名单、
违规形态集、基线与豁免标记全部来自 `[rules.bare_collections]`。
"""

from __future__ import annotations

import ast
import dataclasses
import json
import re
import tempfile
from datetime import date
from pathlib import Path
from typing import NamedTuple

from ..config import BareCollectionsRules, Config, parse
from ..console import out
from .rules import RulesError, require

MIN_KEYS = ("scan_targets", "whitelist", "baseline")
"""未登记时报错给出的最小键指引。"""

POSITION_CLASS_FIELD = "class_field"
POSITION_SIGNATURE_PARAM = "signature_param"
POSITION_SIGNATURE_RETURN = "signature_return"
POSITION_LOCAL = "local"
"""命中位置分类标识（报告与 JSON 输出的键）。"""


class BaselineError(Exception):
    """基线快照非法（格式 / 版本 / 字段）。"""


class Hit(NamedTuple):
    """一处命中（违规集合声明位置）。

    Attributes:
        file: 相对仓库根的文件路径。
        line: 规范化后的声明行内容。
        container: 命中的集合形态名。
        position: 命中位置分类。
        base_object: 是否基座对象类的类字段。
    """

    file: str
    line: str
    container: str
    position: str
    base_object: bool


@dataclasses.dataclass
class Outcome:
    """一次扫描 / 判定的结果。

    Attributes:
        hits: 命中清单。
        problems: 问题清单。
        counts: 分类计数。
    """

    hits: list[Hit] = dataclasses.field(default_factory=list)
    problems: list[str] = dataclasses.field(default_factory=list)
    counts: dict[str, int] = dataclasses.field(default_factory=dict)


def _forms(rule: BareCollectionsRules) -> tuple[set[str], set[str], set[str]]:
    """取（白名单, 判违规形态集, 未登记形态集）。

    Args:
        rule: 规则段。

    Returns:
        tuple[set[str], set[str], set[str]]: 三集合。
    """
    whitelist = set(rule.whitelist)
    banned = set(rule.banned_bare) | set(rule.banned_readonly) | set(rule.banned_ascending)
    return whitelist, banned, set(rule.unregistered)


def _head_name(node: ast.expr) -> str:
    """取注解头名（`Name.id` 或 `Attribute.attr`）。

    Args:
        node: 注解表达式节点。

    Returns:
        str: 头名（无法识别返回空串）。
    """
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    return ""


def _base_name(node: ast.expr) -> str:
    """取基类名（`Name.id` / `Attribute.attr` / 下标头名）。

    Args:
        node: 基类表达式节点。

    Returns:
        str: 基类名（无法识别返回空串）。
    """
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    if isinstance(node, ast.Subscript):
        return _base_name(node.value)
    return ""


def _is_homogeneous_tuple(node: ast.Subscript) -> bool:
    """判断 `tuple[...]` 是否同构（末元素为 `Ellipsis`）。

    Args:
        node: `tuple` 下标表达式节点。

    Returns:
        bool: 同构返回 True。
    """
    if not isinstance(node.slice, ast.Tuple) or not node.slice.elts:
        return False
    last = node.slice.elts[-1]
    return isinstance(last, ast.Constant) and last.value is Ellipsis


def _string_annotations(rule: BareCollectionsRules) -> tuple[re.Pattern[str], re.Pattern[str]]:
    """字符串前向引用注解的两条判定正则。

    Args:
        rule: 规则段。

    Returns:
        tuple[re.Pattern[str], re.Pattern[str]]: （违规形态名, 同构 tuple）。
    """
    _whitelist, banned, unregistered = _forms(rule)
    names = {*banned, *unregistered, *rule.tuple_forms}
    pattern = re.compile(r"\b(" + "|".join(sorted(names)) + r")\b")
    return pattern, re.compile(r"\btuple\[[^\[\]]*\.\.\.")


def annotation_violations(node: ast.expr | None, rule: BareCollectionsRules) -> list[str]:
    """递归取注解中的违规集合声明形态名（白名单判定，未命中白名单即违规）。

    Args:
        node: 注解表达式（可为 None）。
        rule: 规则段。

    Returns:
        list[str]: 命中形态名（可能重复，调用方去重）。
    """
    if node is None:
        return []
    whitelist, banned, unregistered = _forms(rule)
    tuple_forms = set(rule.tuple_forms)
    if isinstance(node, ast.Subscript):
        head = _head_name(node.value)
        if head in tuple_forms:
            if not _is_homogeneous_tuple(node):
                return annotation_violations(node.slice, rule)
            return ["tuple", *annotation_violations(node.slice, rule)]
        if head in whitelist:
            return annotation_violations(node.slice, rule)
        if head in banned or head in unregistered:
            return [head, *annotation_violations(node.slice, rule)]
        return annotation_violations(node.slice, rule)
    if isinstance(node, ast.Name | ast.Attribute):
        name = _head_name(node)
        if name in whitelist or name in rule.protocol_forms:
            return []
        if name in tuple_forms:
            return ["bare_tuple"]
        if name in banned or name in unregistered:
            return [name]
        return []
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.BitOr):
        return [*annotation_violations(node.left, rule), *annotation_violations(node.right, rule)]
    if isinstance(node, ast.Tuple | ast.List):
        found: list[str] = []
        for item in node.elts:
            found.extend(annotation_violations(item, rule))
        return found
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        names: list[str] = []
        container_re, homolog_re = _string_annotations(rule)
        if homolog_re.search(node.value):
            names.append("tuple")
        names.extend(name for name in sorted(set(container_re.findall(node.value))) if name not in tuple_forms)
        return names
    return []


def _excluded(path: Path, root: Path, rule: BareCollectionsRules) -> bool:
    """判断文件是否在排除范围（缓存 / 虚拟环境等非源码目录）。

    Args:
        path: 文件路径。
        root: 仓库根。
        rule: 规则段。

    Returns:
        bool: 排除返回 True。
    """
    return any(part in rule.exclude_parts for part in path.relative_to(root).parts)


def iter_target_files(cfg: Config, rule: BareCollectionsRules) -> list[Path]:
    """枚举扫描目标下的 Python 文件。

    `[backend].groups` 下的服务目录按其「源码 + 测试」两个子目录展开；其余目标整体递归。

    Args:
        cfg: 仓配置。
        rule: 规则段。

    Returns:
        list[Path]: 文件清单（按发现序）。
    """
    files: list[Path] = []
    seen: set[Path] = set()

    def add(path: Path) -> None:
        if path in seen or _excluded(path, cfg.root, rule):
            return
        seen.add(path)
        files.append(path)

    for rel in rule.scan_targets:
        base = cfg.root / rel
        if not base.is_dir():
            continue
        candidates: list[Path] = [base]
        if cfg.backend is not None and rel == f"{cfg.backend.root}/{cfg.backend.groups[-1]}":
            candidates = [child / sub for child in sorted(base.iterdir()) if child.is_dir() for sub in ("src", "tests")]
        for candidate in candidates:
            if not candidate.is_dir():
                continue
            for path in sorted(candidate.rglob("*.py")):
                add(path)
    return files


def scan_source(rel: str, source: str, rule: BareCollectionsRules) -> list[Hit]:
    """扫描单个源文件，收集违规集合声明命中。

    Args:
        rel: 相对仓库根的路径。
        source: 源码文本。
        rule: 规则段。

    Returns:
        list[Hit]: 命中清单（按行序稳定）。
    """
    tree = ast.parse(source)
    lines = source.splitlines()
    hits: list[Hit] = []
    class_fields: dict[int, bool] = {}
    for cls in ast.walk(tree):
        if not isinstance(cls, ast.ClassDef):
            continue
        base_object = any(_base_name(base) == "BaseObject" for base in cls.bases)
        for stmt in cls.body:
            if isinstance(stmt, ast.AnnAssign):
                class_fields[id(stmt)] = base_object

    def add(node: ast.AST, container: str, position: str, *, base_object: bool = False) -> None:
        lineno = getattr(node, "lineno", 0)
        text = lines[lineno - 1].strip() if 0 < lineno <= len(lines) else ""
        window = "\n".join(lines[max(0, lineno - 2) : lineno])
        if rule.allow_marker in window:
            return
        hits.append(Hit(rel, text, container, position, base_object))

    for node in ast.walk(tree):
        if isinstance(node, ast.AnnAssign):
            is_field = id(node) in class_fields
            position = POSITION_CLASS_FIELD if is_field else POSITION_LOCAL
            for container in dict.fromkeys(annotation_violations(node.annotation, rule)):
                add(node, container, position, base_object=is_field and class_fields[id(node)])
        elif isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            for container in dict.fromkeys(annotation_violations(node.returns, rule)):
                add(node.returns or node, container, POSITION_SIGNATURE_RETURN)
            args = (*node.args.posonlyargs, *node.args.args, *node.args.kwonlyargs)
            for arg in args:
                if arg.annotation is None:
                    continue
                for container in dict.fromkeys(annotation_violations(arg.annotation, rule)):
                    add(arg.annotation, container, POSITION_SIGNATURE_PARAM)
    return hits


def collect(cfg: Config, rule: BareCollectionsRules, outcome: Outcome) -> list[Hit]:
    """扫描目标文件，收集命中（扫描异常记入 `outcome.problems`）。

    Args:
        cfg: 仓配置。
        rule: 规则段。
        outcome: 结果累积器。

    Returns:
        list[Hit]: 命中清单。
    """
    hits: list[Hit] = []
    implementation = set(rule.implementation_files)
    for path in iter_target_files(cfg, rule):
        rel = path.relative_to(cfg.root).as_posix()
        if rel in implementation:
            continue
        try:
            hits.extend(scan_source(rel, path.read_text(encoding="utf-8"), rule))
        except (OSError, SyntaxError, UnicodeDecodeError) as exc:
            outcome.problems.append(f"[扫描异常] {rel}：{exc}")
    outcome.hits = hits
    return hits


def baseline_key(hit: Hit) -> tuple[str, str, str]:
    """基线与比对键（文件 + 规范化行内容 + 容器；不含行号）。

    Args:
        hit: 命中。

    Returns:
        tuple[str, str, str]: 键。
    """
    return (hit.file, hit.line, hit.container)


def tally(keys: list[tuple[str, str, str]]) -> dict[tuple[str, str, str], int]:
    """把命中键聚合为计数表。

    Args:
        keys: 命中键清单。

    Returns:
        dict[tuple[str, str, str], int]: 计数表（插入序）。
    """
    table: dict[tuple[str, str, str], int] = {}
    for key in keys:
        table[key] = table.get(key, 0) + 1
    return table


def read_baseline(path: Path, rule: BareCollectionsRules) -> dict[tuple[str, str, str], int]:
    """读取基线快照。

    Args:
        path: 基线文件路径。
        rule: 规则段。

    Returns:
        dict[tuple[str, str, str], int]: 基线命中计数。

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
    counter: dict[tuple[str, str, str], int] = {}
    for index, entry in enumerate(entries):
        if not isinstance(entry, dict):
            raise BaselineError(f"基线快照条目非法（第 {index + 1} 条须为对象）：{path}")
        try:
            key = (str(entry["file"]), str(entry["line"]), str(entry["container"]))
            count = int(entry.get("count", 1))
        except (KeyError, TypeError, ValueError) as exc:
            raise BaselineError(f"基线快照条目字段缺失或非法（第 {index + 1} 条）：{path}") from exc
        if count < 1:
            raise BaselineError(f"基线快照条目 count 非法（第 {index + 1} 条须为正整数）：{path}")
        counter[key] = counter.get(key, 0) + count
    return counter


def write_baseline(path: Path, hits: list[Hit], rule: BareCollectionsRules) -> None:
    """以本次扫描结果重写基线快照（自然完成递减）。

    Args:
        path: 基线文件路径。
        hits: 本次命中清单。
        rule: 规则段。
    """
    aggregate = tally([baseline_key(hit) for hit in hits])
    positions: dict[tuple[str, str, str], str] = {}
    for hit in hits:
        positions.setdefault(baseline_key(hit), hit.position)
    entries = [
        {
            "file": key[0],
            "line": key[1],
            "container": key[2],
            "position": positions.get(key, POSITION_LOCAL),
            "count": count,
        }
        for key, count in sorted(aggregate.items())
    ]
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {"version": rule.baseline_version, "generated_at": date.today().isoformat(), "entries": entries}
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def summarize(hits: list[Hit], outcome: Outcome) -> None:
    """填充分类计数（供 JSON 与报告模式）。

    Args:
        hits: 命中清单。
        outcome: 结果累积器。
    """
    outcome.counts.clear()
    outcome.counts["total"] = len(hits)
    outcome.counts["class_field"] = sum(1 for hit in hits if hit.position == POSITION_CLASS_FIELD)
    outcome.counts["class_field_base_object"] = sum(
        1 for hit in hits if hit.position == POSITION_CLASS_FIELD and hit.base_object
    )
    outcome.counts["signature_param"] = sum(1 for hit in hits if hit.position == POSITION_SIGNATURE_PARAM)
    outcome.counts["signature_return"] = sum(1 for hit in hits if hit.position == POSITION_SIGNATURE_RETURN)
    outcome.counts["local"] = sum(1 for hit in hits if hit.position == POSITION_LOCAL)


def _positions(hits: list[Hit]) -> dict[tuple[str, str, str], str]:
    """命中键 → 首次出现的位置分类。

    Args:
        hits: 命中清单。

    Returns:
        dict[tuple[str, str, str], str]: 位置映射。
    """
    positions: dict[tuple[str, str, str], str] = {}
    for hit in hits:
        positions.setdefault(baseline_key(hit), hit.position)
    return positions


def check(cfg: Config, rule: BareCollectionsRules) -> Outcome:
    """执行护栏：扫描 → 基线比对 → 问题清单。

    Args:
        cfg: 仓配置。
        rule: 规则段。

    Returns:
        Outcome: 结果。
    """
    outcome = Outcome()
    hits = collect(cfg, rule, outcome)
    summarize(hits, outcome)
    actual = tally([baseline_key(hit) for hit in hits])
    positions = _positions(hits)
    try:
        baseline = read_baseline(cfg.root / rule.baseline, rule)
    except BaselineError as exc:
        outcome.problems.append(f"[基线非法] {exc}")
        return outcome
    banned = set(rule.banned_bare) | set(rule.banned_readonly) | set(rule.banned_ascending)
    added: dict[tuple[str, str, str], int] = {}
    for key, count in actual.items():
        extra = count - baseline.get(key, 0)
        if extra > 0:
            added[key] = extra
    stale_total = sum(max(0, count - actual.get(key, 0)) for key, count in baseline.items())
    outcome.counts["baseline_entries"] = sum(baseline.values())
    outcome.counts["added"] = sum(added.values())
    outcome.counts["stale"] = stale_total
    for (file, line, container), count in sorted(added.items()):
        reason = rule.reasons.get(container)
        if reason is None:
            reason = rule.reasons.get("banned" if container in banned else "unregistered", "")
        position = positions.get((file, line, container), "?")
        outcome.problems.append(f"[新增] {file}（{position} / {container} × {count}）：{line}｜{reason}")
    if stale_total and not added:
        out(f"  基线可递减：{stale_total} 处已修复但仍在基线，跑 `--update-baseline` 同步（不影响本次结论）。")
    return outcome


def _payload(outcome: Outcome) -> dict[str, object]:
    """构造 JSON 输出载荷。

    Args:
        outcome: 结果。

    Returns:
        dict[str, object]: 载荷。
    """
    return {"passed": not outcome.problems, "counts": outcome.counts, "problems": outcome.problems}


def report(cfg: Config, rule: BareCollectionsRules, *, as_json: bool) -> Outcome:
    """报告模式：仅统计与清单，恒退出 0。

    Args:
        cfg: 仓配置。
        rule: 规则段。
        as_json: 是否 JSON 输出。

    Returns:
        Outcome: 结果。
    """
    outcome = Outcome()
    hits = collect(cfg, rule, outcome)
    summarize(hits, outcome)
    backend_prefix = f"{cfg.backend.root}/" if cfg.backend is not None else ""
    by_area: dict[str, int] = {}
    by_file: dict[str, int] = {}
    by_container: dict[str, int] = {}
    for hit in hits:
        parts = hit.file.split("/")
        area = parts[1] if backend_prefix and hit.file.startswith(backend_prefix) and len(parts) > 1 else parts[0]
        by_area[area] = by_area.get(area, 0) + 1
        by_file[hit.file] = by_file.get(hit.file, 0) + 1
        by_container[hit.container] = by_container.get(hit.container, 0) + 1
    if as_json:
        payload = _payload(outcome)
        payload["by_area"] = dict(sorted(by_area.items()))
        payload["by_container"] = dict(sorted(by_container.items()))
        payload["top_files"] = dict(sorted(by_file.items(), key=lambda item: -item[1])[:20])
        out(json.dumps(payload, ensure_ascii=False, indent=2))
        return outcome
    out(f"[check_bare_collections] 报告：命中 {outcome.counts.get('total', 0)} 处（仅统计，不影响退出码）")
    out(
        f"  位置：类字段 {outcome.counts.get('class_field', 0)}"
        f"（含 BaseObject {outcome.counts.get('class_field_base_object', 0)}）"
        f" / 签名参数 {outcome.counts.get('signature_param', 0)}"
        f" / 签名返回 {outcome.counts.get('signature_return', 0)}"
        f" / 局部变量 {outcome.counts.get('local', 0)}"
    )
    out("  容器：" + "、".join(f"{name} {num}" for name, num in sorted(by_container.items(), key=lambda kv: -kv[1])))
    out("  区域：" + "、".join(f"{name} {num}" for name, num in sorted(by_area.items(), key=lambda kv: -kv[1])))
    out("  高频文件 TOP 10：")
    for name, num in sorted(by_file.items(), key=lambda kv: -kv[1])[:10]:
        out(f"    {num:>4}  {name}")
    for problem in outcome.problems:
        out("  " + problem)
    return outcome


def emit(outcome: Outcome, *, as_json: bool) -> None:
    """输出检查结果（文本或 JSON）。

    Args:
        outcome: 结果。
        as_json: 是否 JSON 输出。
    """
    if as_json:
        out(json.dumps(_payload(outcome), ensure_ascii=False, indent=2))
        return
    if outcome.problems:
        out(f"\n[check_bare_collections] 不通过：{len(outcome.problems)} 项")
        for problem in outcome.problems:
            out("  " + problem)
        return
    out(
        "[check_bare_collections] 通过：集合声明无新增违规（类字段 / 函数签名 / 局部变量）"
        f"（基线内 {outcome.counts.get('baseline_entries', 0)} 处存量）。"
    )


FIXTURE_SOURCE = (
    "from collections import Counter, OrderedDict, deque\n"
    "from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence\n"
    "from typing import AbstractSet, ClassVar\n"
    "\n"
    "class Plain:\n"
    "    tags: list[str] = []\n"
    "    ordered: StableList[str] = StableList()\n"
    "    frozen: FrozenList[str] = FrozenList()\n"
    "    ascending: SortedList[int] = SortedList()\n"
    "    constant: ClassVar[frozenset[str]] = frozenset()\n"
    "    homogeneous: tuple[str, ...] = ()\n"
    "    fixed: tuple[str, int] = ('', 0)\n"
    "    bare: tuple = ()\n"
    "    queued: deque[int] = deque()\n"
    "    counted: Counter[str] = Counter()\n"
    "    lru: OrderedDict[str, int] = OrderedDict()\n"
    "    allowed_lru: OrderedDict[str, int] = OrderedDict()  # ALLOW_MARKER\n"
    "\n"
    "class Model(BaseObject):\n"
    "    payload: dict[str, object] = {}\n"
    "\n"
    "class Demo(BaseSettings):\n"
    "    paths: list[str] = []\n"
    "\n"
    "def f(a: dict[str, int], b: Sequence[str], c: Mapping[str, int], d: AbstractSet[str]) -> set[int]:\n"
    "    return set()\n"
    "\n"
    "def g(items: Iterable[int], stream: Iterator[int], cb: Callable[[int], int]) -> None:\n"
    "    del items, stream, cb\n"
    "\n"
    "def h() -> 'list[int]':\n"
    "    return []\n"
    "\n"
    "def inner() -> None:\n"
    "    local: dict[str, int] = {}\n"
    "    allowed: dict[str, int] = {}  # ALLOW_MARKER\n"
    "    del local, allowed\n"
)
"""自检矩阵源码（形态名占位符在运行时替换为规则中的词表）。"""


def _sample_rule(rule: BareCollectionsRules) -> BareCollectionsRules:
    """造一份自检用规则（扫描面与基线指向临时目录，词表取本仓配置）。

    Args:
        rule: 仓规则。

    Returns:
        BareCollectionsRules: 自检规则。
    """
    whitelist = list(rule.whitelist)
    ascending = list(rule.banned_ascending)
    return dataclasses.replace(
        rule,
        scan_targets=("src", "svc", "ops", "tools"),
        whitelist=tuple(whitelist),
        banned_ascending=tuple(ascending),
        implementation_files=("src/impl/collection.py",),
        baseline="baseline.json",
    )


def _fixture(rule: BareCollectionsRules) -> dict[str, str]:
    """构造自检夹具文件。

    Args:
        rule: 自检规则。

    Returns:
        dict[str, str]: 相对路径 → 源码文本。
    """
    whitelist = list(rule.whitelist)
    ascending = list(rule.banned_ascending)
    ordered = whitelist[0] if whitelist else "StableList"
    frozen = whitelist[3] if len(whitelist) > 3 else ordered
    sorted_form = ascending[0] if ascending else "SortedList"
    source = (
        FIXTURE_SOURCE.replace("StableList", ordered)
        .replace("FrozenList", frozen)
        .replace("SortedList", sorted_form)
        .replace("ALLOW_MARKER", rule.allow_marker.lstrip("# "))
    )
    unregistered = next((form for form in rule.unregistered if form[:1].isupper()), "Counter")
    return {
        "src/demo/mod.py": source,
        "src/demo/tests/test_local.py": "class X:\n    bad: list[int] = []\n",
        "src/impl/collection.py": "def to_list(self) -> list[int]:\n    return []\n",
        "svc/org/mod.py": "class A:\n    data: List[int] = []\n",
        "ops/op.py": f"def run(payload: {unregistered}[str, int]) -> None:\n    del payload\n",
        "tools/thing.py": "def x() -> frozenset[str]:\n    return frozenset()\n",
        "cache/junk.py": "class J:\n    data: list[int] = []\n",
    }


def self_test(rule: BareCollectionsRules) -> int:
    """自检矩阵（临时仓库夹具）：检出 / 白名单 / tuple 分形态 / 未登记形态 / 位置 / 排除 / 基线语义。

    Args:
        rule: 仓规则（自检在其基础上构造夹具规则）。

    Returns:
        int: 退出码（0 通过 / 1 失败）。
    """
    sample = _sample_rule(rule)
    whitelist = list(sample.whitelist)
    ok = True

    def expect(condition: bool, label: str) -> None:
        nonlocal ok
        if condition:
            out(f"  [自检通过] {label}")
        else:
            ok = False
            out(f"  [自检失败] {label}")

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        files = _fixture(sample)
        for rel, content in files.items():
            path = root / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8")
        cfg = _mini_cfg(root)
        outcome = Outcome()
        hits = collect(cfg, sample, outcome)
        found = {(hit.file, hit.line, hit.container, hit.position) for hit in hits}
        demo = "src/demo/mod.py"
        func_line = "def f(a: dict[str, int], b: Sequence[str], c: Mapping[str, int], d: AbstractSet[str]) -> set[int]:"
        expect((demo, "tags: list[str] = []", "list", POSITION_CLASS_FIELD) in found, "命中类字段 list（裸容器）")
        expect(
            (demo, "payload: dict[str, object] = {}", "dict", POSITION_CLASS_FIELD) in found, "命中基座对象类字段 dict"
        )
        expect((demo, func_line, "dict", POSITION_SIGNATURE_PARAM) in found, "命中签名参数 dict")
        expect((demo, func_line, "set", POSITION_SIGNATURE_RETURN) in found, "命中签名返回 set")
        expect((demo, func_line, "Sequence", POSITION_SIGNATURE_PARAM) in found, "抽象落点 Sequence 报")
        expect((demo, func_line, "Mapping", POSITION_SIGNATURE_PARAM) in found, "抽象落点 Mapping 报")
        expect((demo, func_line, "AbstractSet", POSITION_SIGNATURE_PARAM) in found, "抽象落点 AbstractSet 报")
        ascending_line = f"ascending: {sample.banned_ascending[0]}[int] = {sample.banned_ascending[0]}()"
        expect(
            (demo, ascending_line, sample.banned_ascending[0], POSITION_CLASS_FIELD) in found,
            "升序形态报",
        )
        expect(all(hit.container != whitelist[0] for hit in hits), "白名单命中不报（插入序形态）")
        expect(
            all(hit.container != (whitelist[3] if len(whitelist) > 3 else whitelist[0]) for hit in hits),
            "白名单命中不报（不可变形态）",
        )
        expect(all(hit.container not in sample.protocol_forms for hit in hits), "迭代 / 调用协议不报")
        expect(
            (demo, "constant: ClassVar[frozenset[str]] = frozenset()", "frozenset", POSITION_CLASS_FIELD) in found,
            "类级常量报（豁免已取消）",
        )
        expect((demo, "paths: list[str] = []", "list", POSITION_CLASS_FIELD) in found, "配置字段报（豁免已取消）")
        expect((demo, "local: dict[str, int] = {}", "dict", POSITION_LOCAL) in found, "函数体局部变量报")
        expect(
            not any(hit.container == "dict" and hit.line.startswith("allowed:") for hit in hits),
            "框架边界标记行跳过",
        )
        expect(not any(hit.line.startswith("allowed_lru:") for hit in hits), "内部结构标记行跳过")
        expect(any(hit.file.endswith("demo/tests/test_local.py") for hit in hits), "测试目录纳入扫描")
        expect(
            ("svc/org/mod.py", "data: List[int] = []", "List", POSITION_CLASS_FIELD) in found,
            "命中 typing 别名 List",
        )
        expect(not any(hit.file.startswith("src/impl/") for hit in hits), "集合体系实现文件不属约束对象")
        expect(not any(hit.file.startswith("cache/") for hit in hits), "非扫描目标不纳入")

        # 基线语义：写基线 → 再扫描无新增；去掉一条命中 → 递减提示
        baseline_path = root / sample.baseline
        write_baseline(baseline_path, hits, sample)
        second = check(cfg, sample)
        expect(not second.problems, "基线吸收后无新增违规")
        expect(second.counts.get("baseline_entries", 0) == len(hits), "基线条数与命中数一致")
        (root / "tools/thing.py").unlink()
        third = check(cfg, sample)
        expect(not third.problems and third.counts.get("stale", 0) > 0, "命中减少即记可递减（仍通过）")
        (root / "ops/extra.py").parent.mkdir(parents=True, exist_ok=True)
        (root / "ops/extra.py").write_text("class B:\n    data: set[int] = set()\n", encoding="utf-8")
        fourth = check(cfg, sample)
        expect(bool(fourth.problems), "新增违规即失败")
        payload = Outcome(hits=fourth.hits, problems=[], counts=fourth.counts)
        report(cfg, sample, as_json=True)
        report(cfg, sample, as_json=False)
        emit(payload, as_json=True)
        emit(Outcome(), as_json=False)

    out(f"\n[check_bare_collections self-test] {'通过' if ok else '不通过'}")
    return 0 if ok else 1


def _mini_cfg(root: Path) -> Config:
    """造一份最小配置（自检夹具）。

    Args:
        root: 临时仓库根。

    Returns:
        Config: 配置对象。
    """
    return parse(
        {"docs": {"root": "docs"}, "backend": {"root": "backend", "groups": ["libs", "services"]}},
        root / "engkit.toml",
    )


def run(cfg: Config, *, json_output: bool, report_mode: bool, update_baseline: bool, as_self_test: bool) -> int:
    """执行集合声明校验。

    Args:
        cfg: 仓配置。
        json_output: 是否 JSON 输出。
        report_mode: 是否只出统计报告。
        update_baseline: 是否按当前扫描结果重写基线。
        as_self_test: 是否只跑内置自检。

    Returns:
        int: 退出码（0 通过 / 1 有新增违规 / 2 规则或基线缺失）。
    """
    try:
        rule = require(cfg.collections_rules, "bare_collections", MIN_KEYS)
    except RulesError as exc:
        out(f"[check_bare_collections] {exc}")
        return 2
    if as_self_test:
        return self_test(rule)
    if update_baseline:
        outcome = Outcome()
        hits = collect(cfg, rule, outcome)
        write_baseline(cfg.root / rule.baseline, hits, rule)
        out(f"[check_bare_collections] 基线已更新：{len(hits)} 条 → {rule.baseline}")
        return 0
    if report_mode:
        report(cfg, rule, as_json=json_output)
        return 0
    outcome = check(cfg, rule)
    emit(outcome, as_json=json_output)
    return 1 if outcome.problems else 0
