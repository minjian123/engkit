"""前端基类体系校验（`engkit base frontend-base`）。

与 `backend-base` 的产品形态（`profile = "product"`）同构：产品仓清单 / 契约自持，
只判本仓段；父类经 `base_source_dirs` 指向的平台源码面解析，链必须接到平台体系根。

校验五项：① 清单 ↔ 源码对账（类存在、父类一致、代码位置正确）；② 契约 §7 链 ↔ 清单对账；
③ 未登记基类（源码里有类直接继承平台基类却未登记）；④ 体系根可达与不自造根系；
⑤ 体系根清单小节存在（`root_bases_marker`）。

规则数据全部来自 `[rules.frontend_base]`，包内不出现任何仓名 / 产品名。
"""

from __future__ import annotations

import dataclasses
import os
import re
from pathlib import Path

from ..config import Config, FrontendBaseRules
from ..console import out
from .rules import RulesError, read_text, require
from .scan_scope import read_file_list, select_files

MIN_KEYS = ("manifest", "chain_doc", "source_dirs", "base_source_dirs", "root_bases_marker")
"""未登记时报错给出的最小键指引。"""

CLASS_RE = re.compile(
    r"^(?:export\s+)?(?:default\s+)?(?:abstract\s+)?class\s+(\w+)"
    r"(?:<[^>]*>)?\s+extends\s+([\w.]+)",
    re.M,
)
"""TS 类定义与直接父类（泛型参数与 `implements` 不参与继承链）。"""

TICKS_RE = re.compile(r"`([^`]+)`")
"""行内代码标识。"""

CHAIN_ITEM_RE = re.compile(r"`(\w+)`\s*(?:→|->)\s*`(\w+)`")
"""实现契约 §7 的一条链（子类 → 父类）。"""

SKIP_DIR_NAMES = ("node_modules", "dist", "coverage", "tests", "__pycache__", ".cache", ".git")
"""不参与类扫描的目录名。"""


@dataclasses.dataclass
class Outcome:
    """校验结果。"""

    problems: list[str] = dataclasses.field(default_factory=list)
    checked: int = 0


def strip_ticks(raw: str) -> str:
    """去掉行内代码标识的反引号与首尾空白。

    Args:
        raw: 原始文本。

    Returns:
        str: 归一化后的标识。
    """
    found = TICKS_RE.search(raw)
    return (found.group(1) if found else raw).strip()


def parse_manifest(text: str) -> dict[str, tuple[str, str]]:
    """解析基类登记表的表格行（类名 / 层 / 父类 / 代码位置 / 职责 / 状态）。

    Args:
        text: 清单全文。

    Returns:
        dict[str, tuple[str, str]]: 类名 → （父类, 代码位置）。
    """
    result: dict[str, tuple[str, str]] = {}
    for line in text.splitlines():
        if not line.lstrip().startswith("|"):
            continue
        cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
        if len(cells) < 4:
            continue
        name = strip_ticks(cells[0])
        if name in ("", "类名", "---") or set(name) <= {"-", ":"}:
            continue
        parent = strip_ticks(cells[2])
        location = strip_ticks(cells[3])
        if not parent or not location:
            continue
        result[name] = (parent, location)
    return result


def parse_chain_items(text: str) -> list[tuple[str, str]]:
    """解析实现契约里的子类 → 父类条目。

    Args:
        text: 实现契约全文。

    Returns:
        list[tuple[str, str]]: （子类, 父类）条目。
    """
    return [(match.group(1), match.group(2)) for match in CHAIN_ITEM_RE.finditer(text)]


def parse_root_bases(text: str, marker: str) -> tuple[str, ...]:
    """解析「体系根清单」小节内的类名。

    Args:
        text: 实现契约全文。
        marker: 小节标记文本。

    Returns:
        tuple[str, ...]: 体系根类名。
    """
    lines = text.splitlines()
    for index, line in enumerate(lines):
        if marker not in line:
            continue
        roots: list[str] = []
        for follow in lines[index + 1 :]:
            stripped = follow.strip()
            if stripped.startswith("## ") or stripped.startswith("**") or stripped.startswith("> "):
                if roots:
                    break
                continue
            if stripped.startswith("- ") or stripped.startswith("* "):
                found = TICKS_RE.search(stripped)
                if found and found.group(1) not in roots:
                    roots.append(found.group(1))
                continue
            if stripped == "" and not roots:
                continue
            if stripped != "" and roots:
                break
        return tuple(roots)
    return ()


def iter_ts_files(root: Path, source_dirs: tuple[str, ...]) -> list[Path]:
    """列出源码面下的 TS 文件。

    Args:
        root: 仓根。
        source_dirs: 源码目录（仓根相对）。

    Returns:
        list[Path]: TS 文件绝对路径。
    """
    files: list[Path] = []
    for rel in source_dirs:
        base = root / rel
        if not base.is_dir():
            continue
        for path in base.rglob("*.ts"):
            if path.name.endswith(".d.ts"):
                continue
            if any(part in SKIP_DIR_NAMES for part in path.parts):
                continue
            files.append(path)
    return sorted(set(files))


def index_classes(root: Path, files: list[Path]) -> dict[str, list[tuple[str, str]]]:
    """建立类名 → （父类, 仓根相对路径）索引。

    Args:
        root: 仓根（用于生成相对路径）。
        files: TS 文件。

    Returns:
        dict[str, list[tuple[str, str]]]: 类索引。
    """
    index: dict[str, list[tuple[str, str]]] = {}
    for path in files:
        text = read_text(path)
        # 平台源码面可在仓外（`base_source_dirs` 指向相邻克隆），relpath 同时覆盖两种落点。
        rel = os.path.relpath(path.resolve(), root.resolve()).replace("\\", "/")
        for match in CLASS_RE.finditer(text):
            index.setdefault(match.group(1), []).append((match.group(2), rel))
    return index


def _reaches_root(
    name: str,
    index: dict[str, list[tuple[str, str]]],
    roots: frozenset[str],
    seen: frozenset[str],
) -> bool:
    """判断类沿父类链能否到达体系根。

    Args:
        name: 起始类名。
        index: 合并后的类索引。
        roots: 体系根集合。
        seen: 已访问类名（防环）。

    Returns:
        bool: 是否可达体系根。
    """
    if name in roots:
        return True
    if name in seen:
        return False
    entries = index.get(name, [])
    if not entries:
        return False
    return any(_reaches_root(parent, index, roots, seen | {name}) for parent, _ in entries)


def collect(cfg: Config, rule: FrontendBaseRules, scope: set[str] | None = None) -> tuple[Outcome, list[str]]:
    """执行全部五项校验。

    Args:
        cfg: 仓配置。
        rule: 规则段。
        scope: 作用域（仓内相对路径集合；None 表示全量）。

    Returns:
        tuple[Outcome, list[str]]: （结果, 步骤结论行）。
    """
    outcome = Outcome()
    root = cfg.root.resolve()
    manifest_text = read_text(cfg.root / rule.manifest)
    chain_text = read_text(cfg.root / rule.chain_doc)
    manifest = parse_manifest(manifest_text)
    chains = parse_chain_items(chain_text)
    roots = parse_root_bases(chain_text, rule.root_bases_marker)
    product_index = index_classes(root, iter_ts_files(root, rule.source_dirs))
    platform_index = index_classes(root, iter_ts_files(root, rule.base_source_dirs))
    merged: dict[str, list[tuple[str, str]]] = {}
    for source in (product_index, platform_index):
        for name, entries in source.items():
            merged.setdefault(name, []).extend(entries)
    roots_set = frozenset(roots)

    for name, (parent, location) in manifest.items():
        outcome.checked += 1
        entries = product_index.get(name, [])
        if not entries:
            outcome.problems.append(f"[清单] {name} 在源码面中不存在（登记位置 {location}）")
            continue
        if not any(item[0] == parent for item in entries):
            outcome.problems.append(f"[清单] {name} 的父类应为 {parent}，源码为 {[item[0] for item in entries]}")
        if not any(item[1] == location for item in entries):
            outcome.problems.append(f"[清单] {name} 的代码位置应为 {location}，源码为 {[item[1] for item in entries]}")
        if parent == "BaseObject" and name not in roots_set:
            outcome.problems.append(f"[体系根] {name} 直接继承总基类，构成新增体系根（应挂平台链下游）")
        if not _reaches_root(name, merged, roots_set, frozenset()):
            outcome.problems.append(f"[体系根] {name} 的父类链无法到达体系根 {sorted(roots_set)}")

    chain_map = dict(chains)
    for name, (parent, _) in manifest.items():
        if name not in chain_map:
            outcome.problems.append(f"[契约 §7] 缺 {name} 的继承链条目")
        elif chain_map[name] != parent:
            outcome.problems.append(f"[契约 §7] {name} 的父类为 {chain_map[name]}，清单为 {parent}")
    for name, _ in chains:
        if name not in manifest:
            outcome.problems.append(f"[契约 §7] {name} 的链未登记到清单")

    for name, entries in product_index.items():
        for parent, rel in entries:
            if scope is not None and rel not in scope:
                continue
            if parent not in platform_index and parent not in roots_set:
                continue
            if name not in manifest:
                outcome.problems.append(f"[未登记] {rel} 的 {name} 继承平台基类 {parent}，但清单未登记")

    lines = [
        f"1. 清单 ↔ 源码对账：检查 {outcome.checked} 个登记类（父类 / 代码位置）",
        f"2. 契约 §7 链 ↔ 清单对账：检查 {len(chains)} 条链",
        f"3. 未登记基类：扫描 {len(product_index)} 个本仓类",
        f"4. 体系根可达与不自造根系：平台类 {len(platform_index)} 个，体系根 {len(roots)} 个",
        f"5. 体系根清单小节（{rule.root_bases_marker}）：{'存在' if roots else '缺失'}",
    ]
    if scope is not None:
        lines.append("（增量模式：未登记基类只判清单内文件；清单 / 契约 / 体系根判据恒全量）")
    if not roots:
        outcome.problems.append(f"[契约 §7] 缺「{rule.root_bases_marker}」小节或小节为空")
    return outcome, lines


def self_test() -> int:
    """纯函数自检：TS 类解析 / 表格解析 / 链解析 / 体系根小节解析。

    Returns:
        int: 退出码（0 通过 / 1 失败）。
    """
    ok = True

    def expect(condition: bool, label: str) -> None:
        nonlocal ok
        if not condition:
            ok = False
            out(f"  [frontend-base self-test] 失败：{label}")

    sample = "\n".join(
        (
            "export class BaseMsgReadOnlyView extends BaseTable<MsgRow> {",
            "  private readonly state = 1",
            "}",
            "abstract class MsgProbe extends BaseDataState {",
            "}",
        )
    )
    classes = [(m.group(1), m.group(2)) for m in CLASS_RE.finditer(sample)]
    expect(classes == [("BaseMsgReadOnlyView", "BaseTable"), ("MsgProbe", "BaseDataState")], "TS 类与父类解析")

    table = "\n".join(
        (
            "| 类名 | 层 | 父类 | 代码位置 | 职责 | 状态 |",
            "| --- | --- | --- | --- | --- | --- |",
            "| `BaseMsgReadOnlyView` | 组件基类 | `BaseTable` | "
            "`src/base/BaseMsgReadOnlyView.ts` | 只读列表横切 | 已交付 |",
        )
    )
    parsed = parse_manifest(table)
    expect(
        parsed.get("BaseMsgReadOnlyView") == ("BaseTable", "src/base/BaseMsgReadOnlyView.ts"),
        "清单表格解析",
    )
    expect(
        parse_chain_items("- `BaseMsgReadOnlyView` → `BaseTable`") == [("BaseMsgReadOnlyView", "BaseTable")],
        "链解析",
    )
    contract = "\n".join(
        (
            "**体系根清单（允许直接继承）**：",
            "",
            "- `BaseObject`",
            "- `BaseComponent`",
            "",
            "**本仓继承链**：",
            "- `BaseMsgReadOnlyView` → `BaseTable`",
        )
    )
    expect(parse_root_bases(contract, "体系根清单") == ("BaseObject", "BaseComponent"), "体系根小节解析")
    expect(parse_root_bases("无小节", "体系根清单") == (), "缺小节返回空")

    index = {
        "BaseMsgReadOnlyView": [("BaseTable", "src/a.ts")],
        "BaseTable": [("BaseDataState", "core/table.ts")],
        "BaseDataState": [("BaseComponent", "core/data-state.ts")],
        "BaseComponent": [("BaseObject", "core/component.ts")],
    }
    expect(_reaches_root("BaseMsgReadOnlyView", index, frozenset({"BaseObject"}), frozenset()), "链可达体系根")
    expect(not _reaches_root("Orphan", index, frozenset({"BaseObject"}), frozenset()), "孤立类不可达")

    out("[frontend-base self-test] 通过" if ok else "[frontend-base self-test] 不通过")
    return 0 if ok else 1


def run(cfg: Config, *, as_self_test: bool, files_from: str = "") -> int:
    """执行前端基类体系校验。

    Args:
        cfg: 仓配置。
        as_self_test: 是否只跑内置自检。
        files_from: 改动文件清单（增量只判清单内文件；空串全量）。

    Returns:
        int: 退出码（0 通过 / 1 有问题 / 2 规则或登记源缺失）。
    """
    if as_self_test:
        return self_test()
    try:
        rule = require(cfg.frontend_base_rules, "frontend_base", MIN_KEYS)
    except RulesError as exc:
        out(f"[check_frontend_base] {exc}")
        return 2
    for rel, label in (
        (rule.manifest, "前端基类清单"),
        (rule.chain_doc, "前端基类实现契约"),
    ):
        if not (cfg.root / rel).is_file():
            out(f"[check_frontend_base] {label}缺失：{rel}")
            return 2
    scope: set[str] | None = None
    if files_from:
        listed = read_file_list(Path(files_from))
        if listed is None:
            return 2
        root = cfg.root.resolve()
        scope = {item.resolve().relative_to(root).as_posix() for item in select_files(cfg.root, listed)}
        out(f"[check_frontend_base] 增量模式：只判清单内 {len(scope)} 个文件")
    outcome, lines = collect(cfg, rule, scope)
    for line in lines:
        out(line)
    if outcome.problems:
        out(f"\n[check_frontend_base] 不通过：{len(outcome.problems)} 项")
        for item in outcome.problems:
            out("  " + item)
        return 1
    out("[check_frontend_base] 通过：清单 / 契约链 / 未登记基类 / 体系根全部对齐。")
    return 0
