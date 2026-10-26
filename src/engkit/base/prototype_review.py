"""界面交付「实现 ↔ 原型」核对护栏（`engkit base prototype-review`）。

口径：界面类交付必须有原型依据 + 逐页对照结论。`--changed` 硬拦（变更集含界面源码时必须给出对照），
`--task` 单任务核对，缺省为全仓巡检（软提示）。界面源码路径、记录路径、对照节标题与未完成词表
来自 `[rules.prototype]`；原型引用按文档根解析。
"""

from __future__ import annotations

import dataclasses
import re
import subprocess
import tempfile
from pathlib import Path

from ..config import Config, PrototypeRules
from ..console import out
from .rules import RulesError, require

MIN_KEYS = ("record_pattern", "context_pattern", "prototype_ref_pattern", "ui_patterns")
"""未登记时报错给出的最小键指引。"""

TASKS_DIR = "项目"
"""标准文档布局：文档根下的项目阶段目录。"""

TABLE_HEADER_RE = re.compile(r"^\|(?![\s:|-]+\|$).*\|$")
"""表格行（排除表头分隔行）。"""


def git_changed(root: Path, base: str) -> list[str] | None:
    """取 `<base>...HEAD` 的改动文件清单。

    Args:
        root: 仓库根。
        base: 比较基准。

    Returns:
        list[str] | None: 相对仓库根的路径；git 不可用或基准不存在时返回 None。
    """
    proc = subprocess.run(
        # `core.quotepath=false`：中文路径不被转义，否则记录匹配不到
        ["git", "-C", str(root), "-c", "core.quotepath=false", "diff", "--name-only", f"{base}...HEAD"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    if proc.returncode != 0:
        return None
    return [line.strip().strip('"') for line in proc.stdout.splitlines() if line.strip()]


def read_section(text: str, heading_re: str) -> str:
    """取标题匹配 `heading_re` 的节内容（到下一个二级标题为止）。

    Args:
        text: 文档全文。
        heading_re: 二级标题正则。

    Returns:
        str: 节内容（未命中返回空串）。
    """
    lines = text.splitlines()
    start: int | None = None
    for index, line in enumerate(lines):
        if re.match(heading_re, line):
            start = index + 1
            continue
        if start is not None and line.startswith("## "):
            return "\n".join(lines[start:index])
    return "" if start is None else "\n".join(lines[start:])


def task_texts(root: Path, task_dir: Path, dirs: tuple[str, ...]) -> list[tuple[str, str]]:
    """取某任务目录下「设计 / 实施 / 测试」记录文本（原型依据常登记在详细设计里）。

    Args:
        root: 仓库根。
        task_dir: 任务目录。
        dirs: 要收的子目录名。

    Returns:
        list[tuple[str, str]]: （来源说明, 文本）。
    """
    texts: list[tuple[str, str]] = []
    for sub in dirs:
        for path in sorted((task_dir / sub).glob("*.md")):
            texts.append((path.relative_to(root).as_posix(), path.read_text(encoding="utf-8", errors="ignore")))
    return texts


def prototype_basis_problems(cfg: Config, texts: list[tuple[str, str]], rule: PrototypeRules) -> list[str]:
    """校验原型依据（引用存在且文件真实存在）。

    Args:
        cfg: 仓配置。
        texts: （来源说明, 文本）列表。
        rule: 规则段。

    Returns:
        list[str]: 问题描述（空即通过）。
    """
    problems: list[str] = []
    pattern = re.compile(rule.prototype_ref_pattern)
    found_ref = False
    for source, text in texts:
        for match in pattern.finditer(text):
            found_ref = True
            target = cfg.docs_root / match.group(1)
            if not target.is_file():
                problems.append(f"{source}：原型引用文件不存在 → {target.relative_to(cfg.root).as_posix()}")
    if not found_ref:
        problems.append("未登记原型依据：需在（设计 / 实施 / 测试）记录中引用原型 html（路径相对文档根）")
    return problems


def review_section_problems(record: Path, text: str, rule: PrototypeRules) -> list[str]:
    """校验原型对照节（节存在 + 结论表 + 截图 + 无未完成词）。

    Args:
        record: 测试记录路径。
        text: 记录全文。
        rule: 规则段。

    Returns:
        list[str]: 问题描述（空即通过）。
    """
    problems: list[str] = []
    section = read_section(text, rule.section_heading)
    if not section.strip():
        problems.append(f"{record}：缺「原型对照结论」节（按《原型审查规范》逐页对照）")
        return problems

    if rule.not_applicable_word in section:
        reason = re.sub(r"\s+", "", section.replace(rule.not_applicable_word, "", 1))
        if len(reason) < rule.min_reason_len:
            word = rule.not_applicable_word
            problems.append(f"{record}：声明「{word}」但未给理由（需 ≥{rule.min_reason_len} 字说明为何无界面可见变更）")
        return problems

    header = ""
    rows = 0
    for line in section.splitlines():
        stripped = line.strip()
        if not TABLE_HEADER_RE.match(stripped):
            continue
        if rule.table_header_keyword in stripped and not header:
            header = stripped
        elif header:
            rows += 1
    if not header:
        problems.append(f"{record}：原型对照节缺表头含「{rule.table_header_keyword}」的对照表")
    elif rows == 0:
        problems.append(f"{record}：原型对照表无数据行（逐页 / 逐项结论均需落行）")

    shots = 0
    for match in re.finditer(rule.screenshot_pattern, section):
        if (record.parent / match.group(1)).resolve().is_file():
            shots += 1
    if shots == 0:
        problems.append(f"{record}：原型对照节缺**存在**的截图证据（落 `测试/资源/<记录名>/`）")

    for word in rule.unfinished_words:
        if word in section:
            problems.append(f"{record}：原型对照节含未完成词「{word}」（对照须给出结论，不得留待办）")
            break
    return problems


def check_records(
    cfg: Config, records: list[Path], context_texts: list[tuple[str, str]], rule: PrototypeRules
) -> list[str]:
    """对给定记录集执行原型对照 + 原型依据校验。

    Args:
        cfg: 仓配置。
        records: 测试记录路径列表。
        context_texts: 原型依据候选文本。
        rule: 规则段。

    Returns:
        list[str]: 问题描述。
    """
    problems: list[str] = []
    for record in records:
        problems.extend(review_section_problems(record, record.read_text(encoding="utf-8", errors="ignore"), rule))
    problems.extend(prototype_basis_problems(cfg, context_texts, rule))
    return problems


def changed_mode(cfg: Config, base: str, rule: PrototypeRules) -> int:
    """推送前硬拦模式：含界面改动则必须有原型对照。

    Args:
        cfg: 仓配置。
        base: 比较基准。
        rule: 规则段。

    Returns:
        int: 退出码。
    """
    changed = git_changed(cfg.root, base)
    if changed is None:
        out(f"[check_prototype_review] 跳过：无法解析 `git diff {base}...HEAD`（CI / 浅克隆等场景）")
        return 0
    record_re = re.compile(rule.record_pattern)
    context_re = re.compile(rule.context_pattern)
    ui_res = [re.compile(item) for item in rule.ui_patterns]
    ui_files: list[str] = []
    records: list[Path] = []
    texts: list[tuple[str, str]] = []
    for name in changed:
        if any(pattern.search(name) for pattern in ui_res):
            ui_files.append(name)
        path = cfg.root / name
        if context_re.match(name) and path.is_file():
            texts.append((name, path.read_text(encoding="utf-8", errors="ignore")))
        if record_re.match(name) and path.is_file():
            records.append(path)
            texts.extend(task_texts(cfg.root, path.parent.parent, rule.context_dirs))

    if not ui_files:
        out(f"[check_prototype_review] 通过：本次改动无界面源码（{base}...HEAD 共 {len(changed)} 个文件）")
        return 0
    out(f"[check_prototype_review] 本次含界面改动 {len(ui_files)} 个，例：{ui_files[0]}")
    if not records:
        out(
            "[check_prototype_review] ✗ 含界面改动但变更集内**没有测试记录**——"
            "按《原型审查规范》须落「原型对照结论」（逐页 + 结论 + 截图）"
        )
        return 1
    problems = check_records(cfg, records, texts, rule)
    if problems:
        for item in problems:
            out(f"[check_prototype_review] ✗ {item}")
        return 1
    out(f"[check_prototype_review] 通过：原型依据 + 对照结论齐备（{records[0]}）")
    return 0


def task_mode(cfg: Config, task_dir: Path, rule: PrototypeRules) -> int:
    """单任务校验（硬）。

    Args:
        cfg: 仓配置。
        task_dir: 任务目录。
        rule: 规则段。

    Returns:
        int: 退出码。
    """
    if not task_dir.is_dir():
        out(f"[check_prototype_review] 目录不存在：{task_dir}")
        return 2
    records = sorted((task_dir / "测试").glob("*.md"))
    if not records:
        out(f"[check_prototype_review] {task_dir} 下无测试记录（非界面交付可忽略）")
        return 0
    problems = check_records(cfg, records, task_texts(cfg.root, task_dir, rule.context_dirs), rule)
    if problems:
        for item in problems:
            out(f"[check_prototype_review] ✗ {item}")
        return 1
    out(f"[check_prototype_review] 通过：{task_dir.relative_to(cfg.root)}（原型依据 + 对照结论齐备）")
    return 0


def inspect_mode(cfg: Config, rule: PrototypeRules) -> int:
    """全仓巡检（软提示）：列出未落原型对照的任务测试记录。

    Args:
        cfg: 仓配置。
        rule: 规则段。

    Returns:
        int: 退出码（恒 0 / 目录缺失 2）。
    """
    docs = cfg.docs_root / TASKS_DIR
    if not docs.is_dir():
        out(f"[check_prototype_review] 目录缺失：{docs}")
        return 2
    records = sorted(docs.glob("*/任务/**/测试/*.md"))
    missing: list[str] = []
    for record in records:
        if not read_section(record.read_text(encoding="utf-8", errors="ignore"), rule.section_heading).strip():
            missing.append(record.relative_to(cfg.root).as_posix())
    out(
        f"[check_prototype_review] 巡检：任务测试记录 {len(records)} 份，"
        f"未落「原型对照结论」{len(missing)} 份（软提示）"
    )
    for name in missing[:20]:
        out(f"  · {name}")
    if len(missing) > 20:
        out(f"  · …（余 {len(missing) - 20} 份）")
    out("[check_prototype_review] 提示：界面类任务须补对照结论；非界面类无需补（推送时按界面改动硬拦）")
    return 0


def self_test(cfg: Config, rule: PrototypeRules) -> int:
    """自检：构造样例文档树，校验通过 / 拦截分支。

    Args:
        cfg: 仓配置。
        rule: 规则段。

    Returns:
        int: 退出码（0 通过 / 1 失败）。
    """
    failures: list[str] = []
    proto_rel = "原型/样例.html"
    cases = 5
    sample_rule = dataclasses.replace(
        rule,
        prototype_ref_pattern=r"(原型/[^\s()\[\]\"'`<>|]*\.html)",
    )
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        docs_root = root / cfg.docs.root
        record_dir = docs_root / TASKS_DIR / "01_阶段" / "任务" / "01_域" / "01_任务" / "测试"
        (record_dir / "资源" / "01_记录").mkdir(parents=True)
        (record_dir / "资源" / "01_记录" / "实现.png").write_bytes(b"png")
        (docs_root / proto_rel).parent.mkdir(parents=True, exist_ok=True)
        (docs_root / proto_rel).write_text("<html></html>", encoding="utf-8")
        record = record_dir / "01_记录.md"
        sample = _sample_text(sample_rule, proto_rel)
        texts: list[tuple[str, str]] = [("record", sample)]
        temp_cfg = _with_root(cfg, root)
        rule = sample_rule

        record.write_text(sample, encoding="utf-8")
        if check_records(temp_cfg, [record], texts, rule):
            failures.append("样例 A（齐备）应通过")

        record.write_text("## 1. 测试信息\n\n无对照节\n", encoding="utf-8")
        if not check_records(temp_cfg, [record], [("record", "无对照节")], rule):
            failures.append("样例 B（缺对照节）应被拦")

        record.write_text(sample.replace("| 通过 |", "| 待补 |"), encoding="utf-8")
        if not check_records(temp_cfg, [record], texts, rule):
            failures.append("样例 C（含未完成词）应被拦")

        record.write_text(
            f"## 4. 原型对照结论\n\n{rule.not_applicable_word}：本次仅修复请求头拼写，界面无可见变更。\n",
            encoding="utf-8",
        )
        if check_records(temp_cfg, [record], texts, rule):
            failures.append("样例 D（不适用 + 理由）应通过")

        record.write_text(sample, encoding="utf-8")
        bad_ref = sample.replace(proto_rel, "不存在.html")
        if not check_records(temp_cfg, [record], [("record", bad_ref)], rule):
            failures.append("样例 E（原型引用文件不存在）应被拦")
    if failures:
        for item in failures:
            out(f"[check_prototype_review self-test] ✗ {item}")
        return 1
    out(f"[check_prototype_review self-test] 通过：断言用例 {cases} 个")
    return 0


def _sample_text(rule: PrototypeRules, proto_rel: str) -> str:
    """造一份齐备的对照结论样例文本（自检夹具）。

    Args:
        rule: 规则段。
        proto_rel: 原型相对文档根的路径。

    Returns:
        str: 记录全文。
    """
    keyword = rule.table_header_keyword
    return (
        "## 4. 原型对照结论（实现 ↔ 原型）\n\n"
        f"基准：[原型]({proto_rel})。\n\n"
        f"| # | 对照项 | 原型依据 | 实现实测 | {keyword} |\n| --- | --- | --- | --- | --- |\n"
        f"| 1 | 独立全屏 | 原型 | 无侧栏 / 页签 | 通过 |\n\n"
        f"| 截面 | 截图 |\n| --- | --- |\n"
        f"| 实现 | [实现.png](资源/01_记录/实现.png) |\n"
    )


def _with_root(cfg: Config, root: Path) -> Config:
    """把配置的仓根替换为临时仓根（自检夹具用）。

    Args:
        cfg: 原配置。
        root: 临时仓根。

    Returns:
        Config: 替换后的配置。
    """
    return dataclasses.replace(cfg, root=root)


def run(cfg: Config, *, changed: str, task: str, as_self_test: bool) -> int:
    """执行原型对照校验。

    Args:
        cfg: 仓配置。
        changed: 比较基准（空串表示未启用硬拦模式）。
        task: 任务目录（空串表示不按任务核对）。
        as_self_test: 是否只跑内置自检。

    Returns:
        int: 退出码（0 通过 / 1 违规 / 2 规则或目录缺失）。
    """
    try:
        rule = require(cfg.prototype_rules, "prototype", MIN_KEYS)
    except RulesError as exc:
        out(f"[check_prototype_review] {exc}")
        return 2
    if as_self_test:
        return self_test(cfg, rule)
    if changed:
        return changed_mode(cfg, changed, rule)
    if task:
        return task_mode(cfg, Path(task).resolve(), rule)
    return inspect_mode(cfg, rule)
