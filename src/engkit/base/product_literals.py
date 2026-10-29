"""产品字面量护栏（`engkit base product-literals`）。

口径：平台类仓的**禁入面**（平台源码 / 种子 / 前端常量）不得出现产品标识；判定面、标识清单、
允许面、排除面、注释开关与基线全部来自 `[rules.product_literals]`，规则本体不含任何仓名 / 产品名。
存量以「路径 + 行内容指纹」基线豁免，新增即失败；基线只减不增由 `--baseline-ref` 机器断言。
"""

from __future__ import annotations

import ast
import dataclasses
import hashlib
import io
import json
import re
import tempfile
import tokenize
from datetime import date
from pathlib import Path
from typing import NamedTuple

from ..config import Config, ProductLiteralsRules, parse
from ..console import out
from ..gitutil import read_at
from .rules import RulesError, require
from .scan_scope import read_file_list, select_files

MIN_KEYS = ("deny_globs", "literals")
"""未登记时报错给出的最小键指引。"""

_GLOB_SPECIAL = frozenset("*?[")
_FIXTURE_TAG = "PRODUCT_TAG"
_SCRIPT_SUFFIXES = (".ts", ".tsx", ".js", ".mjs", ".jsx")
_DOCSTRING_NODES = (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)


class BaselineError(Exception):
    """基线快照非法（格式 / 版本 / 字段）。"""


class Hit(NamedTuple):
    """一处命中。

    Attributes:
        file: 相对仓库根的文件路径。
        line_no: 行号（1 起）。
        text: 该行原文（去首尾空白）。
        literal: 命中的产品标识。
        in_comment: 是否位于注释 / docstring 内。
    """

    file: str
    line_no: int
    text: str
    literal: str
    in_comment: bool


@dataclasses.dataclass
class Outcome:
    """一次扫描 / 判定的结果。

    Attributes:
        hits: 命中清单。
        problems: 问题清单。
        warnings: 提示清单（不影响退出码）。
        counts: 分类计数。
    """

    hits: list[Hit] = dataclasses.field(default_factory=list)
    problems: list[str] = dataclasses.field(default_factory=list)
    warnings: list[str] = dataclasses.field(default_factory=list)
    counts: dict[str, int] = dataclasses.field(default_factory=dict)


def glob_regex(pattern: str) -> re.Pattern[str]:
    """把 posix glob 编成正则（`**/` 允许零级目录，`**` 跨目录，`*` / `?` 不跨目录）。

    Args:
        pattern: glob 形态（`/` 分隔）。

    Returns:
        re.Pattern[str]: 整串匹配正则。
    """
    parts: list[str] = ["^"]
    index = 0
    while index < len(pattern):
        char = pattern[index]
        if char == "*" and pattern.startswith("**/", index):
            parts.append("(?:.*/)?")
            index += 3
        elif char == "*" and pattern.startswith("**", index):
            parts.append(".*")
            index += 2
        elif char == "*":
            parts.append("[^/]*")
            index += 1
        elif char == "?":
            parts.append("[^/]")
            index += 1
        else:
            parts.append(re.escape(char))
            index += 1
    parts.append("$")
    return re.compile("".join(parts))


def matches(rel: str, patterns: tuple[str, ...]) -> bool:
    """判断相对路径是否命中任一 glob。

    Args:
        rel: 相对仓库根的 posix 路径。
        patterns: glob 清单。

    Returns:
        bool: 命中返回 True。
    """
    return any(glob_regex(pattern).match(rel) is not None for pattern in patterns)


def scan_roots(patterns: tuple[str, ...]) -> list[str]:
    """从 glob 清单派生扫描根（取首个含通配符的段之前的字面前缀）。

    Args:
        patterns: glob 清单。

    Returns:
        list[str]: 相对仓根的目录（去重，插入序）。
    """
    roots: list[str] = []
    for pattern in patterns:
        parts: list[str] = []
        for part in pattern.split("/"):
            if _GLOB_SPECIAL & set(part):
                break
            parts.append(part)
        rel = "/".join(parts) or "."
        if rel not in roots:
            roots.append(rel)
    return roots


def _span_offsets(source: str) -> list[int]:
    """逐行起始字符偏移。

    Args:
        source: 源码文本。

    Returns:
        list[int]: 每行首字符在全文中的偏移（行号 1 对应下标 0）。
    """
    offsets = [0]
    for line in source.splitlines(keepends=True):
        offsets.append(offsets[-1] + len(line))
    return offsets


def _mark(mask: list[bool], start: int, end: int) -> None:
    """把掩码区间标记为注释面。

    Args:
        mask: 字符掩码。
        start: 起始偏移（含）。
        end: 结束偏移（不含）。
    """
    for index in range(max(0, start), min(len(mask), end)):
        mask[index] = True


def python_comment_mask(source: str) -> list[bool] | None:
    """算 Python 源码的注表面掩码（`#` 注释 + 模块 / 类 / 函数 docstring）。

    Args:
        source: 源码文本。

    Returns:
        list[bool] | None: 字符掩码；语法不可解析时返回 None（调用方降级为全文扫描）。
    """
    try:
        tree = ast.parse(source)
        tokens = list(tokenize.generate_tokens(io.StringIO(source).readline))
    except (SyntaxError, tokenize.TokenError, ValueError):
        return None
    mask = [False] * len(source)
    offsets = _span_offsets(source)
    for token in tokens:
        if token.type != tokenize.COMMENT:
            continue
        row, column = token.start
        _mark(mask, offsets[row - 1] + column, offsets[row - 1] + token.end[1])
    for node in ast.walk(tree):
        if not isinstance(node, _DOCSTRING_NODES) or ast.get_docstring(node, clean=False) is None:
            continue
        first = node.body[0]
        last_row = first.end_lineno or first.lineno
        _mark(mask, offsets[first.lineno - 1] + first.col_offset, offsets[last_row - 1] + (first.end_col_offset or 0))
    return mask


def script_comment_mask(source: str) -> list[bool]:
    """算脚本类源码（TS / JS / Vue）的注表面掩码（`//` 行注释与 `/* */` 块注释）。

    Args:
        source: 源码文本。

    Returns:
        list[bool]: 字符掩码（未闭合注释按到文件末尾计）。
    """
    mask = [False] * len(source)
    length = len(source)
    quote = ""
    index = 0
    while index < length:
        char = source[index]
        if quote:
            if char == "\\":
                index += 2
                continue
            if char == quote:
                quote = ""
            index += 1
            continue
        if char in "\"'`":
            quote = char
            index += 1
            continue
        if char == "/" and source.startswith("//", index):
            end = source.find("\n", index)
            end = length if end < 0 else end
            _mark(mask, index, end)
            index = end
            continue
        if char == "/" and source.startswith("/*", index):
            found = source.find("*/", index + 2)
            end = length if found < 0 else found + 2
            _mark(mask, index, end)
            index = end
            continue
        index += 1
    return mask


def comment_mask(rel: str, source: str) -> list[bool] | None:
    """按文件形态算注表面掩码。

    Args:
        rel: 相对仓库根的路径。
        source: 源码文本。

    Returns:
        list[bool] | None: 掩码；Python 不可解析时返回 None。
    """
    if rel.endswith(".py"):
        return python_comment_mask(source)
    if rel.endswith(_SCRIPT_SUFFIXES):
        return script_comment_mask(source)
    return [False] * len(source)


def literal_regex(literal: str, suffixes: tuple[str, ...]) -> re.Pattern[str]:
    """编译单个标识的匹配正则（左词边界；右侧由 `accept_right` 判定，区分大小写）。

    Args:
        literal: 产品标识。
        suffixes: 组合形态后缀（保留签名一致；判定在 `accept_right`）。

    Returns:
        re.Pattern[str]: 匹配正则。
    """
    del suffixes
    return re.compile(rf"(?<![A-Za-z0-9_]){re.escape(literal)}")


def accept_right(line: str, end: int, suffixes: tuple[str, ...]) -> bool:
    """判定命中右侧是否为合法边界（词边界或组合形态后缀）。

    Args:
        line: 行文本。
        end: 命中结束位置。
        suffixes: 组合形态后缀。

    Returns:
        bool: 命中成立返回 True。
    """
    if end >= len(line):
        return True
    char = line[end]
    if char.isalnum():
        return False
    if char == "_" or char in ".:-":
        return char in suffixes
    return True


def normalize_line(text: str) -> str:
    """规范化行内容（去首尾空白 + 折叠内部空白），用于基线指纹。

    Args:
        text: 行原文。

    Returns:
        str: 规范化文本。
    """
    return re.sub(r"\s+", " ", text.strip())


def fingerprint(text: str) -> str:
    """行内容指纹（`sha256` 前 16 位）。

    Args:
        text: 行原文。

    Returns:
        str: 指纹。
    """
    return hashlib.sha256(normalize_line(text).encode("utf-8")).hexdigest()[:16]


def iter_target_files(cfg: Config, rule: ProductLiteralsRules, *, changed: list[str] | None = None) -> list[Path]:
    """枚举禁入面内的目标文件（按扩展名过滤，去重保序）。

    增量模式（`changed` 非 None）只取清单内、命中禁入面的现存文件：本护栏的规则是**文件局部**的
    （单文件正则 / 注释 / docstring），故增量与全量的新增判定等价，差别只在扫描面。

    Args:
        cfg: 仓配置。
        rule: 规则段。
        changed: 增量模式的改动文件清单（相对仓根）；None 表示全量。

    Returns:
        list[Path]: 文件清单（增量模式保持清单顺序）。
    """
    if changed is not None:
        picked: list[Path] = []
        for path in select_files(cfg.root, changed):
            if path.suffix not in rule.include_extensions:
                continue
            rel = path.relative_to(cfg.root).as_posix()
            if matches(rel, rule.deny_globs):
                picked.append(path)
        return picked
    files: list[Path] = []
    seen: set[str] = set()
    for root_rel in scan_roots(rule.deny_globs):
        base = cfg.root / root_rel
        if not base.is_dir():
            continue
        for path in sorted(base.rglob("*")):
            if not path.is_file() or path.suffix not in rule.include_extensions:
                continue
            rel = path.relative_to(cfg.root).as_posix()
            if rel in seen or not matches(rel, rule.deny_globs):
                continue
            seen.add(rel)
            files.append(path)
    return files


def scan_source(rel: str, source: str, rule: ProductLiteralsRules) -> tuple[list[Hit], bool]:
    """扫描单个文件，收集命中。

    Args:
        rel: 相对仓库根的路径。
        source: 源码文本。
        rule: 规则段。

    Returns:
        tuple[list[Hit], bool]: （命中清单, 是否降级为全文扫描）。
    """
    mask = comment_mask(rel, source)
    degraded = mask is None
    if mask is None:
        mask = [False] * len(source)
    hits: list[Hit] = []
    offset = 0
    for number, raw in enumerate(source.splitlines(keepends=True), start=1):
        line = raw.rstrip("\r\n")
        exempt_reason = ""
        if rule.exempt_marker:
            marker_at = line.find(rule.exempt_marker)
            if marker_at >= 0:
                exempt_reason = line[marker_at + len(rule.exempt_marker) :].strip()
        if exempt_reason:
            offset += len(raw)
            continue
        for literal in rule.literals:
            for match in literal_regex(literal, rule.combine_suffixes).finditer(line):
                if not accept_right(line, match.end(), rule.combine_suffixes):
                    continue
                position = offset + match.start()
                in_comment = mask[position]
                if in_comment and not rule.include_comments:
                    continue
                hits.append(Hit(rel, number, line.strip(), literal, in_comment))
        offset += len(raw)
    return hits, degraded


def collect(
    cfg: Config,
    rule: ProductLiteralsRules,
    outcome: Outcome,
    *,
    changed: list[str] | None = None,
) -> list[Hit]:
    """扫描禁入面并分类统计（允许面 / 排除面 / 降级）。

    增量模式下分类计数只反映清单内文件，输出会标注「增量模式」，避免与全量口径混淆。

    Args:
        cfg: 仓配置。
        rule: 规则段。
        outcome: 结果累积器。
        changed: 增量模式的改动文件清单；None 表示全量。

    Returns:
        list[Hit]: 命中清单。
    """
    hits: list[Hit] = []
    files = 0
    allowed = 0
    excluded = 0
    degraded = 0
    for path in iter_target_files(cfg, rule, changed=changed):
        rel = path.relative_to(cfg.root).as_posix()
        if matches(rel, rule.exclude_globs):
            excluded += 1
            continue
        if matches(rel, rule.allow_globs):
            allowed += 1
            continue
        files += 1
        try:
            source = path.read_text(encoding="utf-8", errors="ignore")
            found, is_degraded = scan_source(rel, source, rule)
        except OSError as exc:
            outcome.problems.append(f"[扫描异常] {rel}：{exc}")
            continue
        if is_degraded:
            degraded += 1
            outcome.warnings.append(f"[降级] {rel}：语法不可解析，按全文扫描")
        hits.extend(found)
    outcome.hits = hits
    outcome.counts["files"] = files
    outcome.counts["allowed"] = allowed
    outcome.counts["excluded"] = excluded
    outcome.counts["degraded"] = degraded
    return hits


def baseline_key(hit: Hit) -> tuple[str, str]:
    """基线与比对键（文件 + 行内容指纹；不含行号）。

    Args:
        hit: 命中。

    Returns:
        tuple[str, str]: 键。
    """
    return (hit.file, fingerprint(hit.text))


def tally(keys: list[tuple[str, str]]) -> dict[tuple[str, str], int]:
    """把命中键聚合为计数表。

    Args:
        keys: 命中键清单。

    Returns:
        dict[tuple[str, str], int]: 计数表（插入序）。
    """
    table: dict[tuple[str, str], int] = {}
    for key in keys:
        table[key] = table.get(key, 0) + 1
    return table


def _parse_baseline(payload: str, rule: ProductLiteralsRules, label: str) -> dict[tuple[str, str], int]:
    """解析基线 JSON 文本。

    Args:
        payload: 文本内容。
        rule: 规则段。
        label: 基线来源描述（报错 / 告警用）。

    Returns:
        dict[tuple[str, str], int]: 基线计数。

    Raises:
        BaselineError: 格式 / 版本 / 字段非法。
    """
    try:
        data = json.loads(payload)
    except json.JSONDecodeError as exc:
        raise BaselineError(f"基线快照不可解析：{label}（{exc}）") from exc
    if not isinstance(data, dict) or data.get("version") != rule.baseline_version:
        raise BaselineError(f"基线快照版本非法（须为 {rule.baseline_version}）：{label}")
    entries = data.get("entries")
    if not isinstance(entries, list):
        raise BaselineError(f"基线快照 entries 非法（须为数组）：{label}")
    counter: dict[tuple[str, str], int] = {}
    for index, entry in enumerate(entries):
        if not isinstance(entry, dict):
            raise BaselineError(f"基线快照条目非法（第 {index + 1} 条须为对象）：{label}")
        try:
            key = (str(entry["path"]), str(entry["fingerprint"]))
            count = int(entry.get("count", 1))
        except (KeyError, TypeError, ValueError) as exc:
            raise BaselineError(f"基线快照条目字段缺失或非法（第 {index + 1} 条）：{label}") from exc
        if count < 1:
            raise BaselineError(f"基线快照条目 count 非法（第 {index + 1} 条须为正整数）：{label}")
        counter[key] = counter.get(key, 0) + count
    return counter


def read_baseline(path: Path, rule: ProductLiteralsRules) -> dict[tuple[str, str], int]:
    """读取基线快照（文件不存在视为空基线）。

    Args:
        path: 基线文件路径。
        rule: 规则段。

    Returns:
        dict[tuple[str, str], int]: 基线计数。

    Raises:
        BaselineError: 文件存在但格式非法。
    """
    if not path.is_file():
        return {}
    return _parse_baseline(path.read_text(encoding="utf-8"), rule, str(path))


def write_baseline(path: Path, hits: list[Hit], rule: ProductLiteralsRules) -> None:
    """按当前扫描结果重写基线快照。

    Args:
        path: 基线文件路径。
        hits: 命中清单。
        rule: 规则段。
    """
    aggregate = tally([baseline_key(hit) for hit in hits])
    buckets: dict[tuple[str, str], set[str]] = {}
    for hit in hits:
        buckets.setdefault(baseline_key(hit), set()).add(hit.literal)
    entries = [
        {"path": key[0], "fingerprint": key[1], "count": count, "samples": sorted(buckets.get(key, ()))[:2]}
        for key, count in sorted(aggregate.items())
    ]
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {"version": rule.baseline_version, "generated_at": date.today().isoformat(), "entries": entries}
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="\n")


def ref_baseline(cfg: Config, rule: ProductLiteralsRules, outcome: Outcome) -> dict[tuple[str, str], int] | None:
    """读取比较基准上的基线快照（不可解析即告警，不失败）。

    Args:
        cfg: 仓配置。
        rule: 规则段。
        outcome: 结果累积器（记告警）。

    Returns:
        dict[tuple[str, str], int] | None: 基准基线；不可用时为 None。
    """
    payload = read_at(cfg.root, rule.baseline_ref, rule.baseline)
    if payload is None:
        outcome.warnings.append(f"[基线只减不增] 基准 `{rule.baseline_ref}` 不可解析（浅克隆 / 无远端），本次跳过断言")
        return None
    try:
        return _parse_baseline(payload, rule, f"{rule.baseline_ref}:{rule.baseline}")
    except BaselineError as exc:
        outcome.warnings.append(f"[基线只减不增] {exc}；本次跳过断言")
        return None


def compare_baseline(current: dict[tuple[str, str], int], reference: dict[tuple[str, str], int]) -> list[str]:
    """基线只减不增断言。

    Args:
        current: 当前基线计数。
        reference: 基准基线计数。

    Returns:
        list[str]: 违规说明（空列表表示通过）。
    """
    problems: list[str] = []
    for (path, digest), count in sorted(current.items()):
        previous = reference.get((path, digest), 0)
        if previous == 0:
            problems.append(f"[基线只减不增] 新增条目：{path}（{digest} × {count}）")
        elif count > previous:
            problems.append(f"[基线只减不增] 条目计数增加：{path}（{digest}：{previous} → {count}）")
    return problems


def summarize(hits: list[Hit], outcome: Outcome) -> None:
    """填充分类计数。

    Args:
        hits: 命中清单。
        outcome: 结果累积器。
    """
    outcome.counts["total"] = len(hits)
    outcome.counts["comment"] = sum(1 for hit in hits if hit.in_comment)
    outcome.counts["code"] = outcome.counts["total"] - outcome.counts["comment"]


def _in_scope(rel: str, changed: list[str] | None) -> bool:
    """基线条目是否落在本次比对范围内（全量恒真；增量按改动清单，含已删除文件）。

    Args:
        rel: 基线条目的相对路径。
        changed: 增量清单；None 表示全量。

    Returns:
        bool: 参与本次 `stale` 统计返回 True。
    """
    return True if changed is None else rel in set(changed)


def check(cfg: Config, rule: ProductLiteralsRules, *, changed: list[str] | None = None) -> Outcome:
    """执行护栏：扫描 → 基线比对 → 只减不增断言 → 问题清单。

    增量模式只比对**清单内文件**的基线条目（未改动文件不会因本次改动新增违规，故结论与全量等价）；
    「基线只减不增」的跨基线断言（`baseline_ref`）在增量下跳过——基线文件本身没被本次改动触碰。

    Args:
        cfg: 仓配置。
        rule: 规则段.
        changed: 增量模式的改动文件清单；None 表示全量。

    Returns:
        Outcome: 结果。
    """
    outcome = Outcome()
    hits = collect(cfg, rule, outcome, changed=changed)
    summarize(hits, outcome)
    actual = tally([baseline_key(hit) for hit in hits])
    baseline: dict[tuple[str, str], int] = {}
    if rule.baseline:
        try:
            baseline = read_baseline(cfg.root / rule.baseline, rule)
        except (OSError, BaselineError) as exc:
            outcome.problems.append(f"[基线非法] {exc}")
            return outcome
    added: dict[tuple[str, str], int] = {}
    for key, count in actual.items():
        extra = count - baseline.get(key, 0)
        if extra > 0:
            added[key] = extra
    outcome.counts["baseline_entries"] = sum(baseline.values())
    outcome.counts["added"] = sum(added.values())
    outcome.counts["stale"] = sum(
        max(0, count - actual.get(key, 0)) for key, count in baseline.items() if _in_scope(key[0], changed)
    )
    positions: dict[tuple[str, str], str] = {}
    for hit in hits:
        positions.setdefault(baseline_key(hit), f"{hit.file}:{hit.line_no}")
    for key, count in sorted(added.items()):
        outcome.problems.append(
            f"[新增] {positions.get(key, key[0])} 命中 {count} 处（{key[1]}）"
            " → 改为按能力名 / 服务目录取用，或登记到产品自持声明"
        )
    if rule.baseline and rule.baseline_ref and changed is None:
        reference = ref_baseline(cfg, rule, outcome)
        if reference is not None:
            outcome.problems.extend(compare_baseline(baseline, reference))
    if outcome.counts["stale"] and not added:
        out(
            f"  基线可递减：{outcome.counts['stale']} 处已修复但仍在基线，"
            "跑 `--update-baseline` 同步（不影响本次结论）。"
        )
    return outcome


def _payload(outcome: Outcome) -> dict[str, object]:
    """构造 JSON 输出载荷。

    Args:
        outcome: 结果。

    Returns:
        dict[str, object]: 载荷。
    """
    return {
        "passed": not outcome.problems,
        "counts": outcome.counts,
        "problems": outcome.problems,
        "warnings": outcome.warnings,
    }


def emit(outcome: Outcome, *, as_json: bool) -> None:
    """输出检查结果（文本或 JSON）。

    Args:
        outcome: 结果。
        as_json: 是否 JSON 输出。
    """
    if as_json:
        out(json.dumps(_payload(outcome), ensure_ascii=False, indent=2))
        return
    for warning in outcome.warnings:
        out("  " + warning)
    counts = outcome.counts
    summary = (
        f"检查 {counts.get('files', 0)} 个文件"
        f"（排除 {counts.get('excluded', 0)} / 允许 {counts.get('allowed', 0)}），"
        f"命中 {counts.get('total', 0)} 处（新增 {counts.get('added', 0)} / 基线 {counts.get('baseline_entries', 0)}）"
    )
    if outcome.problems:
        out(f"\n[check_product_literals] 不通过：{len(outcome.problems)} 项")
        out("  " + summary)
        for problem in outcome.problems:
            out("  " + problem)
        return
    out(f"[check_product_literals] 通过：无新增产品字面量。{summary}")


FIXTURE_SOURCE = '''"""模块说明：PRODUCT_TAG 旧口径（历史写法保留在此）。"""

# 注释里的 PRODUCT_TAG 也应判定
ALLOWED = "PRODUCT_TAG_VALUE"


def demo(value: str = "PRODUCT_TAG") -> str:
    """docstring 里的 PRODUCT_TAG。"""
    return value  # PRODUCT_TAG 行尾注释
'''

FIXTURE_SCRIPT = """const a = "PRODUCT_TAG"  // PRODUCT_TAG 行内注释
/* PRODUCT_TAG 块注释
   继续 PRODUCT_TAG */
const b = "\\PRODUCT_TAG"; // 转义前缀
const c = "PRODUCT_TAG"; /* 未闭合块注释起
"""


def _fixture(literals: tuple[str, ...]) -> dict[str, str]:
    """构造自检夹具文件（标识取自规则，规则本体不含产品名）。

    Args:
        literals: 标识清单（取首条注入夹具）。

    Returns:
        dict[str, str]: 相对路径 → 源码文本。
    """
    tag = literals[0] if literals else "tag"
    return {
        "svc/mod.py": FIXTURE_SOURCE.replace(_FIXTURE_TAG, tag),
        "svc/cap.py": f'CAPABILITY = "{tag}.org.user-roles"\n',
        "svc/tests/test_mod.py": f'assert "{tag}"\n',
        "svc/broken.py": f'def broken(:\n    "{tag}"\n',
        "gen/out.ts": f'// {tag} 生成物\nconst x = "{tag}"\n',
        "gen/skip.md": f"{tag}\n",
        "web/app.ts": FIXTURE_SCRIPT.replace(_FIXTURE_TAG, tag),
        "web/a.ts": f'const z = "{tag}";\n',
        "web/eof.ts": f'const d = "{tag}"; /* 未闭合块注释 {tag}\n',
        "web/eof2.ts": f'const e = "{tag}"; // 未闭合行注释 {tag}',
        "web/none.txt": f"{tag}\n",
    }


def _sample_rule(rule: ProductLiteralsRules) -> ProductLiteralsRules:
    """造一份自检用规则（扫描面与基线指向临时目录）。

    Args:
        rule: 仓规则。

    Returns:
        ProductLiteralsRules: 自检规则。
    """
    return dataclasses.replace(
        rule,
        enabled=True,
        deny_globs=("svc/**/*.py", "web/?.ts", "web/*.ts", "gen/**/*.ts", "gen/*.md"),
        allow_globs=("**/tests/**", "svc/cap.py"),
        exclude_globs=("gen/**",),
        include_comments=True,
        baseline="baseline.json",
        baseline_ref="",
    )


def self_test(rule: ProductLiteralsRules) -> int:
    """自检矩阵（临时仓库夹具）：命中形态 / 注释开关 / 允许排除 / 豁免 / 基线 / 降级。

    Args:
        rule: 仓规则（自检在其基础上构造夹具规则）。

    Returns:
        int: 退出码（0 通过 / 1 失败）。
    """
    sample = _sample_rule(rule)
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
        for rel, content in _fixture(sample.literals).items():
            path = root / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8")
        cfg = _mini_cfg(root)
        first = Outcome()
        hits = collect(cfg, sample, first)

        expect(any(hit.file == "svc/mod.py" and hit.in_comment for hit in hits), "命中注释")
        expect(any(hit.file == "svc/mod.py" and not hit.in_comment for hit in hits), "命中代码字符串")
        expect(any(hit.file == "svc/mod.py" and "docstring" in hit.text for hit in hits), "命中 docstring")
        expect(all(hit.file != "svc/tests/test_mod.py" for hit in hits), "允许面（测试）不判定")
        expect(all(hit.file != "svc/cap.py" for hit in hits), "允许面（能力声明文件）不判定")
        expect(all(hit.file != "gen/out.ts" for hit in hits), "排除面（生成物）不扫描")
        expect(all(hit.file != "web/none.txt" for hit in hits), "扩展名过滤生效")
        expect(first.counts["excluded"] == 1, "排除面计入统计")
        expect(first.counts["allowed"] == 2, "允许面计数")
        expect(any(hit.file == "svc/broken.py" for hit in hits), "语法错误文件降级后仍命中")
        expect(first.counts["degraded"] == 1, "降级计数")
        expect(any(hit.file == "web/app.ts" and "行内注释" in hit.text for hit in hits), "脚本行内注释计入")
        expect(any(hit.file == "web/eof.ts" for hit in hits), "未闭合块注释按到末尾")
        expect(any(hit.file == "web/eof2.ts" for hit in hits), "未闭合行注释不影响同行代码")

        (root / "web/boundary.ts").write_text(f'const g = "x{sample.literals[0]}y";\n', encoding="utf-8")
        boundary = collect(cfg, sample, Outcome())
        expect(all(hit.file != "web/boundary.ts" for hit in boundary), "词边界不误伤长串")

        no_comments = dataclasses.replace(sample, include_comments=False)
        plain = collect(cfg, no_comments, Outcome())
        expect(all(not hit.in_comment for hit in plain), "关闭后注释不判定")
        expect(any(hit.file == "svc/mod.py" for hit in plain), "关闭后代码面仍判定")

        marker = sample.exempt_marker
        (root / "svc/exempt.py").write_text(
            f'X = "{sample.literals[0]}"  # {marker}兼容历史分支\nY = "{sample.literals[0]}"  # {marker}\n',
            encoding="utf-8",
        )
        exempt_lines = [hit.text for hit in collect(cfg, sample, Outcome()) if hit.file == "svc/exempt.py"]
        expect(len(exempt_lines) == 1 and "兼容历史分支" not in exempt_lines[0], "空理由豁免按违规报")

        all_hits = collect(cfg, sample, Outcome())
        baseline_path = root / sample.baseline
        write_baseline(baseline_path, all_hits, sample)
        second = check(cfg, sample)
        expect(not second.problems, "基线吸收后无新增违规")
        expect(second.counts["baseline_entries"] == len(all_hits), "基线条数与命中数一致")
        (root / "svc/exempt.py").unlink()
        third = check(cfg, sample)
        expect(not third.problems and third.counts["stale"] > 0, "命中减少即记可递减（仍通过）")
        (root / "svc/extra.py").write_text(f'Z = "{sample.literals[0]}"\n', encoding="utf-8")
        fourth = check(cfg, sample)
        expect(bool(fourth.problems), "新增违规即失败")
        emit(first, as_json=False)
        emit(fourth, as_json=True)

        key = ("svc/mod.py", "abc")
        expect(not compare_baseline({key: 1}, {key: 1}), "条目未增长即通过")
        expect(bool(compare_baseline({("svc/new.py", "abc"): 1}, {key: 1})), "新增条目即违规")
        expect(bool(compare_baseline({key: 2}, {key: 1})), "条目计数增加即违规")
        extra = Outcome()
        expect(
            ref_baseline(cfg, dataclasses.replace(sample, baseline_ref="no-such-ref"), extra) is None,
            "基准不可解析返回 None",
        )
        expect(bool(extra.warnings), "基准不可解析即告警")

        baseline_path.unlink()
        expect(check(cfg, sample).counts["baseline_entries"] == 0, "基线缺失视为空基线")
        baseline_path.write_text("{ not json", encoding="utf-8")
        expect(bool(check(cfg, sample).problems), "基线不可解析即失败")
        baseline_path.write_text('{"version": 99, "entries": []}', encoding="utf-8")
        expect(bool(check(cfg, sample).problems), "基线版本非法即失败")
    out(f"\n[check_product_literals self-test] {'通过' if ok else '不通过'}")
    return 0 if ok else 1


def _mini_cfg(root: Path) -> Config:
    """造一份最小配置（自检夹具）。

    Args:
        root: 临时仓库根。

    Returns:
        Config: 配置对象。
    """
    return parse(
        {
            "docs": {"root": "docs"},
            "backend": {"root": "backend", "groups": ["libs", "services"]},
            "rules": {"product_literals": {"deny_globs": ["svc/**/*.py"], "literals": ["tag"]}},
        },
        root / "engkit.toml",
    )


def _read_changed(cfg: Config, files_from: str) -> list[str] | None:
    """读取增量清单（空串表示全量）。

    Args:
        cfg: 仓配置。
        files_from: 清单路径（可为相对仓根）。

    Returns:
        list[str] | None: 改动文件清单；全量模式返回 None。

    Raises:
        SystemExit: 清单不可读（退出码 2，避免「清单丢了还当全量跑」）。
    """
    if not files_from:
        return None
    path = Path(files_from)
    if not path.is_absolute():
        path = cfg.root / files_from
    listed = read_file_list(path)
    if listed is None:
        raise SystemExit(2)
    return listed


def run(
    cfg: Config,
    *,
    json_output: bool,
    update_baseline: bool,
    baseline_ref: str,
    as_self_test: bool,
    files_from: str = "",
) -> int:
    """执行产品字面量护栏（可限定为改动文件清单 = 增量模式）。

    Args:
        cfg: 仓配置。
        json_output: 是否 JSON 输出。
        update_baseline: 是否按当前扫描结果重写基线（不得与增量模式同时使用）。
        baseline_ref: 只减不增断言的比较基准（空串取配置）。
        as_self_test: 是否只跑内置自检。
        files_from: 改动文件清单路径（一行一个相对仓根路径）；空串表示全量。

    Returns:
        int: 退出码（0 通过 / 1 有新增违规 / 2 规则缺失或参数冲突）。
    """
    try:
        rule = require(cfg.product_literals_rules, "product_literals", MIN_KEYS)
    except RulesError as exc:
        out(f"[check_product_literals] {exc}")
        return 2
    if as_self_test:
        return self_test(rule)
    if not rule.enabled:
        out("[check_product_literals] 未启用（`[rules.product_literals].enabled = false`），跳过")
        return 0
    if baseline_ref:
        rule = dataclasses.replace(rule, baseline_ref=baseline_ref)
    try:
        changed = _read_changed(cfg, files_from)
    except SystemExit:
        return 2
    if update_baseline and changed is not None:
        out("[check_product_literals] 基线必须按全量重写：`--update-baseline` 不接受增量清单")
        return 2
    if update_baseline:
        outcome = Outcome()
        hits = collect(cfg, rule, outcome)
        write_baseline(cfg.root / rule.baseline, hits, rule)
        out(f"[check_product_literals] 基线已更新：{len(hits)} 条 → {rule.baseline}")
        return 0
    if changed is not None:
        out(f"[check_product_literals] 增量模式：只扫 {len(changed)} 个改动文件")
    outcome = check(cfg, rule, changed=changed)
    emit(outcome, as_json=json_output)
    return 1 if outcome.problems else 0
