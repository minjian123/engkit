"""后端基类体系校验（`engkit base backend-base`）。

校验六项：① 继承链对账（实现契约 ↔ 代码）；② 清单对账（直接继承基座根的类须登记）；
③ 直继承合法性（体系根白名单 + 存量基线）；④ 数据类体系语义（值对象须 frozen dataclass、
框架对象不得 dataclass）；⑤ 错误码段位；⑥ 迁移链分链（单 head / 不断链 / 分支标签与配置段一致）。
清单、契约文档、源码面、错误码模块、迁移清单与基线全部来自 `[rules.backend_base]`。
"""

from __future__ import annotations

import ast
import dataclasses
import itertools
import json
import re
import tempfile
from pathlib import Path

from ..config import BackendBaseRules, Config, parse
from ..console import out
from .rules import RulesError, read_text, require

MIN_KEYS = ("manifest", "chain_doc", "source_dirs", "error_codes", "versions_dir")
"""未登记时报错给出的最小键指引。"""

CHAIN_SECTION = "## 7. 继承链与代码位置"
"""实现契约里继承链与代码位置的节标题（文档结构约定）。"""

PAREN_RE = re.compile(r"[（(][^）)]*[）)]")
"""括号补充说明（链路解析时剔除）。"""

CLASS_RE = re.compile(r"^class\s+(\w+)(?:\[[^\]]*\])?\s*\(([^)]*)\)", re.M)
"""类定义（单行父类列表）。"""

ERROR_CODE_RE = re.compile(r"^\s{4}([A-Z][A-Z0-9_]*)\s*=\s*(\d+)", re.M)
"""错误码常量（四位缩进的类属性）。"""

REVISION_RE = re.compile(r"^revision(?::\s*str)?\s*=\s*[\"']([^\"']+)", re.M)
"""迁移脚本 revision。"""

DOWN_REVISION_RE = re.compile(r"^down_revision[^=]*=\s*(None|[\"']([^\"']+)[\"'])", re.M)
"""迁移脚本 down_revision。"""

BRANCH_LABELS_RE = re.compile(r"^branch_labels[^=]*=\s*(None|\([^)]*\))", re.M)
"""迁移脚本 branch_labels。"""

SKIP_DIR_NAMES = ("__pycache__", "tests")
"""不参与类扫描的目录名。"""

CHAIN_GROUP_SEPARATORS = ("、", "，", "；")
"""链间分隔符（逐条独立解析，避免跨链错配）。"""


@dataclasses.dataclass
class Outcome:
    """一次校验的结果。

    Attributes:
        problems: 问题清单。
        checked: 各项受检数量。
    """

    problems: list[str] = dataclasses.field(default_factory=list)
    checked: dict[str, int] = dataclasses.field(default_factory=dict)


def parse_chains(raw_line: str) -> list[list[str]]:
    """解析清单链条目：支持 `` `A` / `B` → `C` / `D` `` 组配对与 `+` 多父类说明。

    Args:
        raw_line: 清单行文本。

    Returns:
        list[list[str]]: 链清单（组内非法标识符过滤后等长配对）。
    """
    line = PAREN_RE.sub("", raw_line).replace("`", "")
    if "→" not in line:
        return []
    if any(separator in line for separator in CHAIN_GROUP_SEPARATORS):
        chains: list[list[str]] = []
        for part in re.split(r"[、，；]", line):
            chains.extend(parse_chains(part))
        return chains
    groups: list[list[str]] = []
    for segment in line.split("→"):
        cleaned = re.sub(r"[A-Za-z_][A-Za-z0-9_]*\*", "", segment)
        words = re.findall(r"[A-Za-z_][A-Za-z0-9_]*", cleaned)
        group = [word for word in words if re.search(r"[a-z]", word)]
        if group:
            groups.append(group)
    if not groups:
        return []
    length = max(len(group) for group in groups)
    if not all(len(group) in (1, length) for group in groups):
        return []
    return [[group[0] if len(group) == 1 else group[index] for group in groups] for index in range(length)]


def split_bases(raw: str) -> list[str]:
    """按顶层逗号切分类声明的父类列表（尊重 `[` `]` 嵌套）。

    Args:
        raw: `class X(...)` 括号内的原始文本。

    Returns:
        list[str]: 父类表达式清单。
    """
    parts: list[str] = []
    depth = 0
    current = ""
    for char in raw:
        if char in "[(":
            depth += 1
        elif char in "])":
            depth = max(0, depth - 1)
        if char == "," and depth == 0:
            parts.append(current)
            current = ""
            continue
        current += char
    parts.append(current)
    return [part for part in (item.strip() for item in parts) if part]


def normalize_parent(raw: str) -> str:
    """归一化父类名（去泛型参数后取末段）。

    Args:
        raw: 父类表达式。

    Returns:
        str: 归一化类名。
    """
    return re.sub(r"\[.*\]", "", raw, flags=re.S).strip().split(".")[-1].strip()


def iter_source_files(cfg: Config, rule: BackendBaseRules) -> list[Path]:
    """枚举源码面上的 Python 文件（跳过缓存目录与测试目录）。

    Args:
        cfg: 仓配置。
        rule: 规则段。

    Returns:
        list[Path]: 文件清单（按扫描面与目录序遍历）。
    """
    files: list[Path] = []
    for rel in rule.source_dirs:
        base = cfg.root / rel
        if not base.is_dir():
            continue
        for path in sorted(base.rglob("*.py")):
            parts = path.relative_to(base).parts
            if any(part in SKIP_DIR_NAMES for part in parts[:-1]):
                continue
            files.append(path)
    return files


def index_classes(cfg: Config, rule: BackendBaseRules) -> dict[str, list[list[str]]]:
    """类名 → 父类名列表（重复定义保留全部）。

    Args:
        cfg: 仓配置。
        rule: 规则段。

    Returns:
        dict[str, list[list[str]]]: 继承索引。
    """
    index: dict[str, list[list[str]]] = {}
    for path in iter_source_files(cfg, rule):
        text = read_text(path)
        for cls, bases in CLASS_RE.findall(text):
            parents = [normalize_parent(base) for base in split_bases(bases)]
            index.setdefault(cls, []).append(parents)
    return index


def check_inheritance(cfg: Config, rule: BackendBaseRules, index: dict[str, list[list[str]]], outcome: Outcome) -> int:
    """① 继承链对账（实现契约 §7 ↔ 代码）。

    Args:
        cfg: 仓配置。
        rule: 规则段。
        index: 继承索引。
        outcome: 结果累积器。

    Returns:
        int: 核对过的相邻关系数。
    """
    text = read_text(cfg.root / rule.chain_doc)
    start = text.find(CHAIN_SECTION)
    section = text[start:] if start >= 0 else text
    checked = 0
    for raw_line in section.splitlines():
        for chain in parse_chains(raw_line):
            for parent, child in itertools.pairwise(chain):
                checked += 1
                definitions = index.get(parent)
                if not definitions:
                    outcome.problems.append(
                        f"[继承链] 清单登记 `{parent}` 起始链，但代码未找到该类（链：{' → '.join(chain)}）"
                    )
                    continue
                if not any(child in parents for parents in definitions):
                    found = " | ".join(",".join(parents) or "object" for parents in definitions)
                    outcome.problems.append(
                        f"[继承链] `{parent}` 应为 `{child}` 子类，实际父类：{found}（链：{' → '.join(chain)}）"
                    )
    return checked


def check_manifest_coverage(
    cfg: Config, rule: BackendBaseRules, index: dict[str, list[list[str]]], outcome: Outcome
) -> int:
    """② 清单对账：直接继承基座根的类必须出现在基类清单文本中。

    Args:
        cfg: 仓配置。
        rule: 规则段。
        index: 继承索引。
        outcome: 结果累积器。

    Returns:
        int: 受检类数。
    """
    manifest_text = read_text(cfg.root / rule.manifest)
    classes = sorted(
        name
        for name, definitions in index.items()
        if not name.startswith("_") and any("BaseObject" in parents for parents in definitions)
    )
    for name in classes:
        if not re.search(rf"\b{name}\b", manifest_text):
            outcome.problems.append(f"[清单对账] 直接继承 BaseObject 的 `{name}` 未在基类清单登记")
    return len(classes)


def parse_root_bases(cfg: Config, rule: BackendBaseRules) -> tuple[str, ...]:
    """③ 解析实现契约「体系根清单」小节的体系根类名（白名单）。

    Args:
        cfg: 仓配置。
        rule: 规则段。

    Returns:
        tuple[str, ...]: 体系根类名（稳定序；小节缺失时为空）。
    """
    text = read_text(cfg.root / rule.chain_doc)
    start = text.find(CHAIN_SECTION)
    section = text[start:] if start >= 0 else text
    marker_index = section.find(rule.root_bases_marker)
    if marker_index < 0:
        return ()
    roots: set[str] = set()
    for raw_line in section[marker_index:].splitlines()[1:]:
        line = raw_line.strip()
        if not line:
            continue
        if not line.startswith("- "):
            break
        match = re.match(r"-\s*`([A-Za-z_][A-Za-z0-9_]*)`", line)
        if match:
            roots.add(match.group(1))
    return tuple(sorted(roots))


def load_direct_baseline(cfg: Config, rule: BackendBaseRules, outcome: Outcome) -> tuple[tuple[str, str], ...]:
    """读取直继承存量基线（缺文件视为空基线）。

    Args:
        cfg: 仓配置。
        rule: 规则段。
        outcome: 结果累积器。

    Returns:
        tuple[tuple[str, str], ...]: （相对文件, 类名）条目。
    """
    path = cfg.root / rule.direct_baseline
    if not path.is_file():
        return ()
    try:
        payload = json.loads(read_text(path))
    except (OSError, json.JSONDecodeError) as exc:
        outcome.problems.append(f"[直继承] 基线快照不可解析：{rule.direct_baseline}（{exc}）")
        return ()
    if not isinstance(payload, dict) or payload.get("version") != 1:
        outcome.problems.append(f"[直继承] 基线快照版本非法（须为 1）：{rule.direct_baseline}")
        return ()
    entries = payload.get("entries")
    if not isinstance(entries, list):
        outcome.problems.append(f"[直继承] 基线快照 entries 非法（须为数组）：{rule.direct_baseline}")
        return ()
    baseline: set[tuple[str, str]] = set()
    for entry in entries:
        if not isinstance(entry, dict) or "file" not in entry or "class" not in entry:
            outcome.problems.append(f"[直继承] 基线快照条目非法（须含 file / class）：{rule.direct_baseline}")
            continue
        baseline.add((str(entry["file"]), str(entry["class"])))
    return tuple(sorted(baseline))


def scan_direct_base_object(cfg: Config, rule: BackendBaseRules) -> list[tuple[str, str]]:
    """扫描直接继承基座根的类（返回（相对文件, 类名））。

    Args:
        cfg: 仓配置。
        rule: 规则段。

    Returns:
        list[tuple[str, str]]: 命中清单（去重、稳定序）。
    """
    found: list[tuple[str, str]] = []
    for path in iter_source_files(cfg, rule):
        for cls, bases in CLASS_RE.findall(read_text(path)):
            parents = [normalize_parent(base) for base in split_bases(bases)]
            if "BaseObject" in parents:
                item = (path.relative_to(cfg.root).as_posix(), cls)
                if item not in found:
                    found.append(item)
    return found


def check_direct_inheritance(cfg: Config, rule: BackendBaseRules, outcome: Outcome) -> int:
    """③ 直继承合法性（体系根 / 基线放行，其余拦截）。

    Args:
        cfg: 仓配置。
        rule: 规则段。
        outcome: 结果累积器。

    Returns:
        int: 受检类数。
    """
    roots = parse_root_bases(cfg, rule)
    if not roots:
        outcome.problems.append(f"[直继承] 实现契约未登记「{rule.root_bases_marker}」小节（白名单缺失，按拒绝处理）")
        return 0
    baseline = load_direct_baseline(cfg, rule, outcome)
    found = scan_direct_base_object(cfg, rule)
    for rel, cls in found:
        if cls in roots or (rel, cls) in baseline:
            continue
        outcome.problems.append(
            f"[直继承] `{cls}` 直接继承 BaseObject（{rel}）：仅体系根可直继承，请归位到所属体系基类"
        )
    return len(found)


def dataclass_shape(node: ast.ClassDef) -> tuple[bool, bool]:
    """判定类节点的 dataclass 形态。

    Args:
        node: 类定义节点。

    Returns:
        tuple[bool, bool]: （是否 dataclass, 是否 frozen）。
    """
    for decorator in node.decorator_list:
        call = decorator if isinstance(decorator, ast.Call) else None
        target = call.func if call is not None else decorator
        name = target.attr if isinstance(target, ast.Attribute) else getattr(target, "id", "")
        if name != "dataclass":
            continue
        frozen = False
        if call is not None:
            frozen = any(
                keyword.arg == "frozen" and getattr(keyword.value, "value", False) is True for keyword in call.keywords
            )
        return True, frozen
    return False, False


def scan_class_shapes(cfg: Config, rule: BackendBaseRules) -> dict[str, list[tuple[bool, bool]]]:
    """扫描源目录，返回 类名 → [（是否 dataclass, 是否 frozen）]。

    Args:
        cfg: 仓配置。
        rule: 规则段。

    Returns:
        dict[str, list[tuple[bool, bool]]]: 类形态索引。
    """
    shapes: dict[str, list[tuple[bool, bool]]] = {}
    for path in iter_source_files(cfg, rule):
        try:
            tree = ast.parse(read_text(path))
        except (OSError, SyntaxError):
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.ClassDef):
                shapes.setdefault(node.name, []).append(dataclass_shape(node))
    return shapes


def descendants(index: dict[str, list[list[str]]], root: str) -> set[str]:
    """按类名计算 `root` 的传递子类集合（不含自身）。

    Args:
        index: 继承索引。
        root: 起始类名。

    Returns:
        set[str]: 子类类名集合。
    """
    children: dict[str, set[str]] = {}
    for child, definitions in index.items():
        for parents in definitions:
            for parent in parents:
                children.setdefault(parent, set()).add(child)
    found: set[str] = set()
    pending = list(children.get(root, ()))
    while pending:
        current = pending.pop()
        if current in found:
            continue
        found.add(current)
        pending.extend(children.get(current, ()))
    return found


def check_data_class_semantics(
    cfg: Config, rule: BackendBaseRules, index: dict[str, list[list[str]]], outcome: Outcome
) -> int:
    """④ 数据类体系语义（值对象须 frozen dataclass、框架对象不得 dataclass）。

    Args:
        cfg: 仓配置。
        rule: 规则段。
        index: 继承索引。
        outcome: 结果累积器。

    Returns:
        int: 受检的类形态条目数。
    """
    shapes = scan_class_shapes(cfg, rule)
    value_tree = descendants(index, rule.value_object_root)
    framework_tree = descendants(index, rule.framework_object_root)
    checked = 0
    for name in sorted(shapes):
        if name not in value_tree and name not in framework_tree:
            continue
        for is_dataclass, frozen in shapes[name]:
            checked += 1
            if name in value_tree and not (is_dataclass and frozen):
                outcome.problems.append(
                    f"[数据类语义] `{name}` 属值对象体系（{rule.value_object_root}），须声明 "
                    f"@dataclass(frozen=True)（实际 dataclass={is_dataclass} / frozen={frozen}）"
                )
            elif name in framework_tree and is_dataclass:
                outcome.problems.append(
                    f"[数据类语义] `{name}` 属框架对象体系（{rule.framework_object_root}），不得为 dataclass"
                    "（框架对象为非数据对象，不参与值语义与序列化输出）"
                )
    return checked


def check_error_segments(cfg: Config, rule: BackendBaseRules, outcome: Outcome) -> int:
    """⑤ 错误码段位（5 位平台码且万位 1~9）。

    Args:
        cfg: 仓配置。
        rule: 规则段。
        outcome: 结果累积器。

    Returns:
        int: 受检错误码数。
    """
    text = read_text(cfg.root / rule.error_codes)
    start = text.find("class ErrorCode")
    body = text[start:] if start >= 0 else text
    count = 0
    for name, raw in ERROR_CODE_RE.findall(body):
        value = int(raw)
        count += 1
        if not (10000 <= value <= 99999):
            outcome.problems.append(f"[错误码] {name} = {value} 不是 5 位平台码（产品段 10xxxx 起不得混入）")
    return count


def revision_fields(text: str) -> tuple[str | None, str | None, tuple[str, ...]]:
    """解析迁移脚本的 revision / down_revision / branch_labels。

    Args:
        text: 脚本内容。

    Returns:
        tuple[str | None, str | None, tuple[str, ...]]: （revision, down_revision, 分支标签）。
    """
    rev = REVISION_RE.search(text)
    down = DOWN_REVISION_RE.search(text)
    branch_line = BRANCH_LABELS_RE.search(text)
    labels: tuple[str, ...] = ()
    if branch_line and branch_line.group(1) != "None":
        labels = tuple(re.findall(r"[\"']([^\"']+)[\"']", branch_line.group(1)))
    parent = None if (not down or down.group(1) == "None") else down.group(2)
    return (rev.group(1) if rev else None), parent, labels


def known_domains(cfg: Config, rule: BackendBaseRules) -> set[str] | None:
    """从域布局声明静态提取已登记业务域（布局缺失返回 None）。

    Args:
        cfg: 仓配置。
        rule: 规则段。

    Returns:
        set[str] | None: 域标识集合（含归档占位）。
    """
    if not rule.domain_layout:
        return None
    path = cfg.root / rule.domain_layout
    if not path.is_file():
        return None
    domains: set[str] = {"archive"}
    try:
        payload = json.loads(read_text(path))
    except ValueError:
        return None
    if not isinstance(payload, dict):
        return domains
    for item in payload.get("domains", []):
        key = item.get("domain_key") if isinstance(item, dict) else None
        if isinstance(key, str) and key:
            domains.add(key)
    return domains


def iter_chain_dirs(cfg: Config, rule: BackendBaseRules, outcome: Outcome) -> list[tuple[str, str, Path]]:
    """遍历版本目录 `versions/{域}/{数据源}/`（两级）。

    Args:
        cfg: 仓配置。
        rule: 规则段。
        outcome: 结果累积器。

    Returns:
        list[tuple[str, str, Path]]: （域, 数据源, 目录）。
    """
    base = cfg.root / rule.versions_dir
    found: list[tuple[str, str, Path]] = []
    domains = known_domains(cfg, rule)
    for domain_path in sorted(base.iterdir()):
        if domain_path.name.startswith(".") or domain_path.name == "__pycache__":
            continue
        if domain_path.is_file():
            if domain_path.suffix == ".py":
                outcome.problems.append(
                    f"[迁移清单] {domain_path.name} 位于版本根目录（迁移脚本须按 versions/<域>/<数据源>/ 两级存放）"
                )
            continue
        if domains is not None and domain_path.name not in domains:
            outcome.problems.append(f"[迁移清单] 域目录 {domain_path.name} 未在域布局声明内（domain_key 须已登记）")
        for datasource_path in sorted(domain_path.iterdir()):
            if datasource_path.name.startswith(".") or datasource_path.name == "__pycache__":
                continue
            if datasource_path.is_file():
                if datasource_path.suffix == ".py":
                    outcome.problems.append(
                        f"[迁移清单] {domain_path.name}/{datasource_path.name} 位于域目录"
                        f"（版本目录须为 {domain_path.name}/<数据源>/）"
                    )
                continue
            if datasource_path.name not in rule.datasources:
                allowed = " / ".join(rule.datasources)
                outcome.problems.append(
                    f"[迁移清单] {domain_path.name}/{datasource_path.name} 数据源段非法（允许 {allowed}）"
                )
            found.append((domain_path.name, datasource_path.name, datasource_path))
    return found


def check_alembic_chain(cfg: Config, rule: BackendBaseRules, outcome: Outcome) -> int:
    """⑥ 迁移链分链校验（单 head / 不断链 / 分支标签与配置段一致）。

    Args:
        cfg: 仓配置。
        rule: 规则段。
        outcome: 结果累积器。

    Returns:
        int: revision 总数。
    """
    base = cfg.root / rule.versions_dir
    if not base.is_dir():
        outcome.problems.append(f"[迁移链] 未找到 {rule.versions_dir}/")
        return 0
    ini_path = base.parent / "alembic.ini"
    ini_text = read_text(ini_path) if ini_path.is_file() else ""
    total = 0
    for domain, datasource, path in iter_chain_dirs(cfg, rule, outcome):
        chain = f"{domain}:{datasource}"
        revisions: dict[str, str | None] = {}
        labels: dict[str, tuple[str, ...]] = {}
        for item in sorted(path.iterdir()):
            if item.suffix != ".py":
                continue
            rev, parent, branch_labels = revision_fields(read_text(item))
            if not rev:
                outcome.problems.append(f"[迁移链] {chain}/{item.name} 缺少 revision 定义")
                continue
            if rev in revisions:
                outcome.problems.append(f"[迁移链] 链 {chain} 内 revision「{rev}」重复")
            revisions[rev] = parent
            labels[rev] = branch_labels
        if not revisions:
            continue
        parents = {value for value in revisions.values() if value}
        heads = [rev for rev in revisions if rev not in parents]
        if len(heads) != 1:
            outcome.problems.append(
                f"[迁移链] 链 {chain} 的 head 应为 1 个，实际 {len(heads)} 个：{', '.join(sorted(heads))}"
            )
        for rev, parent in revisions.items():
            if parent and parent not in revisions:
                outcome.problems.append(f"[迁移链] {chain}/{rev} 的 down_revision「{parent}」不在本链（断链）")
            if parent is None and chain not in labels[rev]:
                outcome.problems.append(f'[迁移链] 链 {chain} 的链首 {rev} 须声明 branch_labels=("{chain}",)')
            if parent is not None and labels[rev]:
                outcome.problems.append(f"[迁移链] {chain}/{rev} 非链首不应声明 branch_labels")
        section = f"[alembic:{chain}]" if chain != rule.default_chain else "[alembic]"
        if ini_text and section not in ini_text:
            outcome.problems.append(f"[迁移链] 链 {chain} 未在 alembic.ini 登记配置段 {section}")
        total += len(revisions)
    if total == 0:
        out("  （迁移版本目录为空——迁移随后续阶段建立，跳过链检查）")
    return total


def collect(cfg: Config, rule: BackendBaseRules) -> tuple[Outcome, list[str]]:
    """执行全部六项校验。

    Args:
        cfg: 仓配置。
        rule: 规则段。

    Returns:
        tuple[Outcome, list[str]]: （结果, 步骤结论行）。
    """
    outcome = Outcome()
    index = index_classes(cfg, rule)
    chains = check_inheritance(cfg, rule, index, outcome)
    covered = check_manifest_coverage(cfg, rule, index, outcome)
    direct = check_direct_inheritance(cfg, rule, outcome)
    shapes = check_data_class_semantics(cfg, rule, index, outcome)
    codes = check_error_segments(cfg, rule, outcome)
    revs = check_alembic_chain(cfg, rule, outcome)
    lines = [
        f"1. 继承链对账：检查 {chains} 条相邻关系（实现契约 §7 ↔ 代码）",
        f"2. 清单对账（补充）：直接继承 BaseObject 的 {covered} 个基座类均已登记",
        f"3. 直继承合法性：检查 {direct} 个直接继承 BaseObject 的类（体系根 / 基线放行）",
        f"4. 数据类体系语义：检查 {shapes} 个类形态（值对象须 frozen dataclass / 框架对象不得 dataclass）",
        f"5. 错误码段位：检查 {codes} 个平台码",
        f"6. 迁移链：检查 {revs} 个 revision",
    ]
    return outcome, lines


def write_direct_baseline(cfg: Config, rule: BackendBaseRules) -> int:
    """按当前扫描结果重写直继承存量基线。

    Args:
        cfg: 仓配置。
        rule: 规则段。

    Returns:
        int: 写入条目数。
    """
    entries = scan_direct_base_object(cfg, rule)
    path = cfg.root / rule.direct_baseline
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "version": 1,
        "entries": [{"file": rel, "class": name} for rel, name in entries],
    }
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return len(entries)


def self_test() -> int:
    """纯函数自检：链解析 / 父类切分 / 归一化 / 迁移字段 / dataclass 形态 / 错误码段位。

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

    expect(
        parse_chains("- `BaseObj → BaseMid → BaseLeaf`。") == [["BaseObj", "BaseMid", "BaseLeaf"]],
        "链路解析（单链）",
    )
    expect(
        parse_chains("- `BaseObj` / `BaseAlt` → `BaseMid` / `BaseOther`。")
        == [["BaseObj", "BaseMid"], ["BaseAlt", "BaseOther"]],
        "链路解析（组配对）",
    )
    expect(parse_chains("- 无箭头") == [], "链路解析（无箭头即空）")
    expect(
        parse_chains("- `BaseObj → BaseMid`、`BaseLeaf → BaseRoot`。")
        == [["BaseObj", "BaseMid"], ["BaseLeaf", "BaseRoot"]],
        "链路解析（顿号分隔多链）",
    )
    expect(split_bases("A[T, SortedList[T]], B") == ["A[T, SortedList[T]]", "B"], "父类按顶层逗号切分")
    expect(normalize_parent("bc.Base[Y, Z]") == "Base", "父类名归一化去泛型与模块前缀")
    expect(
        revision_fields('revision: str = "0001_a"\ndown_revision: str | None = None\n') == ("0001_a", None, ()),
        "迁移字段解析（链首）",
    )
    expect(
        revision_fields('revision = "0002"\ndown_revision = "0001"\nbranch_labels = ("x:y",)\n')
        == ("0002", "0001", ("x:y",)),
        "迁移字段解析（非链首 + 分支标签）",
    )
    node = ast.parse("@dataclass(frozen=True)\nclass A:\n    pass\n").body[0]
    expect(isinstance(node, ast.ClassDef) and dataclass_shape(node) == (True, True), "dataclass 形态（frozen）")
    plain = ast.parse("class B:\n    pass\n").body[0]
    expect(isinstance(plain, ast.ClassDef) and dataclass_shape(plain) == (False, False), "dataclass 形态（非数据类）")

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        error_file = root / "errors.py"
        error_file.parent.mkdir(parents=True, exist_ok=True)
        error_file.write_text("class ErrorCode:\n    OK = 10001\n    BAD = 100001\n    LOW = 2345\n", encoding="utf-8")
        cfg = parse({"docs": {"root": "docs"}}, root / "engkit.toml")
        rule = BackendBaseRules(
            manifest="docs/清单.md",
            chain_doc="docs/契约.md",
            source_dirs=(),
            error_codes="errors.py",
            versions_dir="versions",
            direct_baseline="baseline.json",
            root_bases_marker="体系根清单",
            value_object_root="BaseValueObject",
            framework_object_root="BaseFrameworkObject",
            datasources=("platform", "tenant"),
            default_chain="platform:tenant",
        )
        outcome = Outcome()
        checked = check_error_segments(cfg, rule, outcome)
        expect(checked == 3, "错误码段位：受检 3 个")
        expect(len(outcome.problems) == 2, "错误码段位：越段与位数非法各报 1")
    out(f"\n[check_backend_base self-test] {'通过' if ok else '不通过'}：断言用例 13 个")
    return 0 if ok else 1


def run(cfg: Config, *, as_self_test: bool, update_baseline: bool) -> int:
    """执行后端基类体系校验。

    Args:
        cfg: 仓配置。
        as_self_test: 是否只跑内置自检。
        update_baseline: 是否按当前扫描结果重写直继承基线。

    Returns:
        int: 退出码（0 通过 / 1 有问题 / 2 规则或登记源缺失）。
    """
    if as_self_test:
        return self_test()
    try:
        rule = require(cfg.backend_base_rules, "backend_base", MIN_KEYS)
    except RulesError as exc:
        out(f"[check_backend_base] {exc}")
        return 2
    if update_baseline:
        count = write_direct_baseline(cfg, rule)
        out(f"[check_backend_base] 直继承基线已更新：{count} 条 → {rule.direct_baseline}")
        return 0
    for rel, label in ((rule.manifest, "基类清单"), (rule.chain_doc, "实现契约文档"), (rule.error_codes, "错误码模块")):
        if not (cfg.root / rel).is_file():
            out(f"[check_backend_base] {label}缺失：{rel}")
            return 2
    outcome, lines = collect(cfg, rule)
    for line in lines:
        out(line)
    if outcome.problems:
        out(f"\n[check_backend_base] 不通过：{len(outcome.problems)} 项")
        for item in outcome.problems:
            out("  " + item)
        return 1
    out("[check_backend_base] 通过：继承链 / 清单登记 / 直继承合法性 / 数据类体系语义 / 错误码段位 / 迁移链全部对齐。")
    return 0
