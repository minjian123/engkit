"""需求 / 任务 / 计划三类项目文档的一致性核对（`engkit base docs-status`）。

口径：需求文档只承载「做什么与验收标准」，进度（状态 / 完成日期 / 数值 / 窗口）以计划表为唯一落点；
需求侧只校验**编号集合**（域文档 ↔ 总览总清单），任务侧只校验编号集合与需求关联。

规则分两层：**硬规则**默认阻断（结构性约束）；**软提示**默认告警（统计与叙述类），`--strict` 时阻断。
阶段目录、编号形态、状态取值与表列数来自 `[rules.docs_status]`。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

from ..config import Config, DocsStatusRules
from ..console import out
from .rules import RulesError, require

MIN_KEYS = ("project_dir", "stage", "statuses", "todo_statuses")
"""未登记时报错给出的最小键指引。"""

DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
"""完成日期形态。"""

WINDOW_RE = re.compile(r"\d{4}-\d{2}-\d{2}( ~ \d{4}-\d{2}-\d{2})?")
"""排期窗口形态（`起` 或 `起 ~ 止`）。"""

HOURS_RE = re.compile(r"^(\d+)h$")
"""数值单元格形态（前导数字 + 单位）。"""

HOURS_LEAD_RE = re.compile(r"(\d+)h")
"""数值单元格取前导数字（兼容「21h（重估：…）」说明形态）。"""

REQ_HEADING_RE = re.compile(r"^## \d+\. 需求 (?P<id>\d{2}-\d+)：")
"""需求域文档的需求标题。"""

ROW_RE = re.compile(r"^\|(?P<cells>.*)\|\s*$")
"""Markdown 表格行。"""

GANTT_NODE_RE = re.compile(r"^\s*(?P<id>\d{2}_\d{2})\s")
"""甘特节点行（首列为计划编号）。"""

STATS_HEAD_RE = re.compile(r"已完成\s*(?P<done>\d+)\s*项，剩余\s*(?P<todo>\d+)\s*项")
"""计划头部项数统计。"""

EFFORT_RE = re.compile(
    r"已完成\s*\*{0,2}(?P<done>\d+)h\*{0,2}；剩余\s*\*{0,2}(?P<todo>\d+)h\*{0,2}"
    r".*?阶段合计\s*\*{0,2}(?P<total>\d+)h\*{0,2}"
)
"""计划头部数值统计（三种既有写法均兼容）。"""

TASK_META_SECTION_RE = r"^## 1\. 任务信息"
"""任务文档「任务信息」节标题。"""

DOMAIN_SECTION_RE = r"^## 2\. 子任务清单"
"""域总览「子任务清单」节标题。"""

PLAN_DONE_SECTION_RE = r"^## 2\. 已完成任务"
"""计划「已完成任务」节标题。"""

PLAN_TODO_SECTION_RE = r"^## 3\. 剩余任务排期"
"""计划「剩余任务」节标题。"""


@dataclass(frozen=True)
class Finding:
    """一条核对发现。

    Attributes:
        level: 硬 / 软。
        code: 规则编码。
        message: 说明。
    """

    level: str
    code: str
    message: str


@dataclass(frozen=True)
class TaskDoc:
    """顶层任务文档的关键字段（任务不承载进度）。

    Attributes:
        path: 任务文档路径。
        domain: 域编号。
        seq: 域内序号。
        reqs: 对应需求编号。
    """

    path: Path
    domain: str
    seq: str
    reqs: list[str]


@dataclass
class Report:
    """核对结果累积器。

    Attributes:
        stage: 阶段目录名。
        hard: 硬规则发现。
        soft: 软提示发现。
        checks: 已核对项数。
    """

    stage: str
    hard: list[Finding] = field(default_factory=list)
    soft: list[Finding] = field(default_factory=list)
    checks: int = 0

    def ok(self, count: int = 1) -> None:
        """记一次通过。

        Args:
            count: 次数。
        """
        self.checks += count

    def fail(self, level: str, code: str, message: str) -> None:
        """记一条发现。

        Args:
            level: 硬 / 软。
            code: 规则编码。
            message: 说明。
        """
        self.checks += 1
        target = self.hard if level == "硬" else self.soft
        target.append(Finding(level, code, message))


@dataclass(frozen=True)
class Spec:
    """编译后的规则（编号形态与列数契约）。

    Attributes:
        rule: 规则段。
        req_id: 需求编号形态。
        task_id: 嵌套子任务编号形态。
        plan_id: 计划编号形态。
    """

    rule: DocsStatusRules
    req_id: re.Pattern[str]
    task_id: re.Pattern[str]
    plan_id: re.Pattern[str]


def build_spec(rule: DocsStatusRules) -> Spec:
    """编译规则中的编号形态。

    Args:
        rule: 规则段。

    Returns:
        Spec: 编译结果。
    """
    return Spec(
        rule=rule,
        req_id=re.compile(rule.req_id_pattern),
        task_id=re.compile(rule.task_id_pattern),
        plan_id=re.compile(rule.plan_id_pattern),
    )


def docs_hours(cell: str) -> int | None:
    """单元格取前导数值（兼容说明后缀）。

    Args:
        cell: 单元格文本。

    Returns:
        int | None: 数值；无法解析返回 None。
    """
    match = HOURS_LEAD_RE.match(cell.strip())
    return int(match.group(1)) if match else None


def split_row(line: str) -> list[str] | None:
    """把 Markdown 表格行切成单元格（表头分隔行返回 None）。

    Args:
        line: 行文本。

    Returns:
        list[str] | None: 单元格；非表格行或分隔行返回 None。
    """
    match = ROW_RE.match(line)
    if not match:
        return None
    cells = [cell.strip() for cell in match.group("cells").split("|")]
    if all(set(cell) <= set("-: ") and cell for cell in cells):
        return None
    return cells


def read_section(text: str, heading_re: str) -> str:
    """截取从匹配标题的行到下一个二级标题之间的正文。

    Args:
        text: 文档全文。
        heading_re: 二级标题正则。

    Returns:
        str: 节内容（未命中返回空串）。
    """
    lines = text.splitlines()
    start: int | None = None
    for index, line in enumerate(lines):
        if re.search(heading_re, line):
            start = index + 1
            break
    if start is None:
        return ""
    end = len(lines)
    for index in range(start, len(lines)):
        if lines[index].startswith("## "):
            end = index
            break
    return "\n".join(lines[start:end])


def parse_req_domain(path: Path) -> set[str]:
    """解析需求域文档的需求编号集合。

    Args:
        path: 需求域文档。

    Returns:
        set[str]: 需求编号集合。
    """
    found: set[str] = set()
    for line in path.read_text(encoding="utf-8", errors="ignore").splitlines():
        heading = REQ_HEADING_RE.match(line)
        if heading:
            found.add(heading.group("id"))
    return found


def parse_overview(path: Path, spec: Spec) -> set[str]:
    """解析需求总览总清单表的需求编号集合。

    Args:
        path: 需求总览文档。
        spec: 编译后的规则。

    Returns:
        set[str]: 需求编号集合。
    """
    found: set[str] = set()
    for line in path.read_text(encoding="utf-8", errors="ignore").splitlines():
        cells = split_row(line)
        if cells and spec.req_id.match(cells[0]):
            found.add(cells[0])
    return found


def parse_task_meta(path: Path) -> dict[str, str]:
    """解析任务文档「任务信息」表（两列表格）。

    Args:
        path: 任务文档。

    Returns:
        dict[str, str]: 键值。
    """
    section = read_section(path.read_text(encoding="utf-8", errors="ignore"), TASK_META_SECTION_RE)
    meta: dict[str, str] = {}
    for line in section.splitlines():
        cells = split_row(line)
        if cells and len(cells) == 2:
            meta[cells[0]] = cells[1]
    return meta


def parse_domain_overview(path: Path) -> list[str]:
    """解析域总览子任务清单表的域内序号集合。

    Args:
        path: 域总览文档。

    Returns:
        list[str]: 序号（发现序）。
    """
    section = read_section(path.read_text(encoding="utf-8", errors="ignore"), DOMAIN_SECTION_RE)
    found: list[str] = []
    for line in section.splitlines():
        cells = split_row(line)
        if cells and len(cells) >= 2 and re.fullmatch(r"\d{2}", cells[0]):
            found.append(cells[0])
    return found


@dataclass(frozen=True)
class PlanData:
    """计划文档解析结果。

    Attributes:
        done: 已完成表（编号 → 数值 / 日期）。
        todo: 剩余表（编号 → 状态 / 数值 / 窗口）。
        stats: 头部项数统计。
        effort: 头部数值统计。
        gantt: 甘特节点编号集合。
    """

    done: dict[str, dict[str, str]]
    todo: dict[str, dict[str, str]]
    stats: tuple[int, int] | None
    effort: tuple[int, int, int] | None
    gantt: set[str]


def parse_plan(path: Path, spec: Spec) -> PlanData:
    """解析计划文档：两张任务表 + 头部统计 + 甘特节点。

    Args:
        path: 计划文档。
        spec: 编译后的规则。

    Returns:
        PlanData: 解析结果。
    """
    text = path.read_text(encoding="utf-8", errors="ignore")
    done_section = read_section(text, PLAN_DONE_SECTION_RE)
    todo_section = read_section(text, PLAN_TODO_SECTION_RE)
    done: dict[str, dict[str, str]] = {}
    todo: dict[str, dict[str, str]] = {}
    for line in done_section.splitlines():
        cells = split_row(line)
        if cells and len(cells) == spec.rule.plan_done_columns and spec.plan_id.match(cells[0]):
            done[cells[0]] = {"hours": cells[2], "date": cells[3]}
    for line in todo_section.splitlines():
        cells = split_row(line)
        if cells and len(cells) == spec.rule.plan_todo_columns and spec.plan_id.match(cells[0]):
            todo[cells[0]] = {"status": cells[2], "hours": cells[3], "window": cells[5]}
    head = STATS_HEAD_RE.search(text)
    effort = EFFORT_RE.search(text)
    gantt = {match.group("id") for match in (GANTT_NODE_RE.match(line) for line in text.splitlines()) if match}
    return PlanData(
        done=done,
        todo=todo,
        stats=(int(head.group("done")), int(head.group("todo"))) if head else None,
        effort=(int(effort.group("done")), int(effort.group("todo")), int(effort.group("total"))) if effort else None,
        gantt=gantt,
    )


def expand_reqs(cell: str) -> list[str]:
    """从「对应需求」单元格提取需求编号（`A ~ B` 形态按域内序号展开）。

    Args:
        cell: 单元格文本。

    Returns:
        list[str]: 需求编号清单。
    """
    ids = re.findall(r"\d{2}-\d+", cell)
    if "~" in cell and len(ids) == 2 and ids[0].split("-")[0] == ids[1].split("-")[0]:
        domain = ids[0].split("-")[0]
        start, end = int(ids[0].split("-")[1]), int(ids[1].split("-")[1])
        if start <= end:
            return [f"{domain}-{seq}" for seq in range(start, end + 1)]
    return list(ids)


def collect_tasks(stage_dir: Path, report: Report, spec: Spec, req_index: dict[str, str]) -> dict[str, TaskDoc]:
    """扫描顶层任务文档，返回计划编号 → 任务。

    Args:
        stage_dir: 阶段目录。
        report: 结果累积器。
        spec: 编译后的规则。
        req_index: 需求编号 → 域文档（跨阶段累积）。

    Returns:
        dict[str, TaskDoc]: 计划编号 → 任务。
    """
    task_root = stage_dir / "任务"
    tasks: dict[str, TaskDoc] = {}
    if not task_root.is_dir():
        return tasks
    for domain_dir in sorted(p for p in task_root.iterdir() if p.is_dir()):
        domain = domain_dir.name.split("_")[0]
        for task_dir in sorted(p for p in domain_dir.iterdir() if p.is_dir()):
            if task_dir.name in spec.rule.aid_dirs:
                continue
            docs = list(task_dir.glob("*.md"))
            if len(docs) != 1:
                report.fail("硬", "H5", f"任务目录文档数异常（{len(docs)}）：{task_dir}")
                continue
            path = docs[0]
            meta = parse_task_meta(path)
            seq = meta.get("编号", "").strip()
            if spec.task_id.match(seq):
                report.ok()
                continue
            if not re.fullmatch(r"\d{2}", seq):
                report.fail("硬", "H5", f"任务编号未识别：{path} → {seq!r}")
                continue
            plan_id = f"{domain}_{seq}"
            doc = TaskDoc(path=path, domain=domain, seq=seq, reqs=expand_reqs(meta.get("对应需求", "")))
            if plan_id in tasks:
                report.fail("硬", "H6", f"计划编号重复：{plan_id}（{path}）")
            tasks[plan_id] = doc
            report.ok()
            for req in doc.reqs:
                report.ok()
                if req not in req_index:
                    report.fail("硬", "H5", f"对应需求不存在：{path} → {req}")
    return tasks


def check_date(report: Report, label: str, status: str, date: str, done_status: str) -> None:
    """H3：已完成 ⇔ 完成日期为 `YYYY-MM-DD`。

    Args:
        report: 结果累积器。
        label: 计划编号。
        status: 状态取值。
        date: 完成日期单元格。
        done_status: 已完成状态取值。
    """
    if status == done_status:
        if not DATE_RE.match(date):
            report.fail("硬", "H3", f"{label} 已完成但完成日期非 YYYY-MM-DD：{date!r}")
    elif date not in ("—", ""):
        report.fail("硬", "H3", f"{label} 非已完成但填了完成日期：{status} / {date!r}")


def check_stage(stage_dir: Path, report: Report, spec: Spec, req_index: dict[str, str]) -> None:
    """单个阶段的三类文档核对。

    Args:
        stage_dir: 阶段目录。
        report: 结果累积器。
        spec: 编译后的规则。
        req_index: 需求编号 → 域文档（跨阶段累积）。
    """
    req_dir = stage_dir / "需求"
    plan_dir = stage_dir / "计划"
    if not req_dir.is_dir() or not plan_dir.is_dir():
        out(f"[{stage_dir.name}] 跳过：缺少 需求/ 或 计划/ 目录")
        return

    overview_files = sorted(req_dir.glob(spec.rule.overview_glob))
    if not overview_files:
        report.fail("硬", "H1", f"缺少需求总览：{req_dir}/{spec.rule.overview_glob}")
        return
    overview = parse_overview(overview_files[0], spec)

    domain_reqs: dict[str, set[str]] = {}
    for domain_path in sorted(p for p in req_dir.glob("*.md") if not p.name.startswith("00_")):
        domain = domain_path.name.split("_")[0]
        domain_reqs[domain] = parse_req_domain(domain_path)
        for req in sorted(domain_reqs[domain]):
            req_index[req] = str(domain_path)

    seen: dict[str, str] = {}
    for domain, reqs in domain_reqs.items():
        for req in sorted(reqs):
            if req in seen:
                report.fail("硬", "H1", f"需求编号重复：{req}（{seen[req]} / {domain}）")
            seen[req] = domain
            report.ok()
    for req in sorted(set(seen) - overview):
        report.fail("硬", "H1", f"域文档有而总清单缺：{req}")
    for req in sorted(overview - set(seen)):
        report.fail("硬", "H1", f"总清单有而域文档缺：{req}")

    tasks = collect_tasks(stage_dir, report, spec, req_index)

    task_root = stage_dir / "任务"
    for domain_dir in sorted(p for p in task_root.iterdir() if p.is_dir()):
        domain = domain_dir.name.split("_")[0]
        overview_doc = domain_dir / f"{domain_dir.name}.md"
        if not overview_doc.is_file():
            continue
        listed = parse_domain_overview(overview_doc)
        for seq in sorted(listed):
            plan_id = f"{domain}_{seq}"
            report.ok()
            if plan_id not in tasks:
                report.fail("硬", "H5", f"域总览列出而任务文档缺失：{plan_id}（{overview_doc.name}）")
        for plan_id in sorted(t for t in tasks if t.startswith(f"{domain}_")):
            report.ok()
            if plan_id.split("_")[1] not in listed:
                report.fail("硬", "H5", f"任务文档未登记进域总览：{plan_id}")

    plan_files = sorted(plan_dir.glob(spec.rule.plan_glob))
    if not plan_files:
        report.fail("硬", "H6", f"缺少计划文档：{plan_dir}/{spec.rule.plan_glob}")
        return
    plan = parse_plan(plan_files[0], spec)

    for plan_id in sorted(set(plan.done) & set(plan.todo)):
        report.fail("硬", "H6", f"计划编号同时出现在已完成与剩余表：{plan_id}")
    report.ok()
    for plan_id in sorted(set(plan.done) | set(plan.todo)):
        report.ok()
        if plan_id not in tasks:
            report.fail("硬", "H6", f"计划列出而任务文档缺失：{plan_id}")
    for plan_id in sorted(tasks):
        report.ok()
        if plan_id not in plan.done and plan_id not in plan.todo:
            report.fail("硬", "H6", f"任务文档未登记进计划：{plan_id}")

    for plan_id, row in sorted(plan.todo.items()):
        report.ok()
        if row["status"] not in spec.rule.todo_statuses:
            report.fail("硬", "H7", f"剩余任务表状态取值非法：{plan_id} → {row['status']}")
    for plan_id, row in sorted(plan.todo.items()):
        report.ok()
        window = row["window"]
        if window.startswith("搁置"):
            continue
        if not WINDOW_RE.fullmatch(window):
            report.fail("硬", "H8", f"排期窗口格式非法：{plan_id} → {window}")

    if plan.stats is not None:
        report.checks += 1
        if plan.stats != (len(plan.done), len(plan.todo)):
            report.fail(
                "软",
                "S1",
                f"计划头计数与明细不符：头部 {plan.stats[0]}/{plan.stats[1]}，明细 {len(plan.done)}/{len(plan.todo)}",
            )
    if plan.effort is not None:
        report.checks += 1
        sum_done = sum(docs_hours(row["hours"]) or 0 for row in plan.done.values())
        sum_todo = sum(docs_hours(row["hours"]) or 0 for row in plan.todo.values())
        if plan.effort != (sum_done, sum_todo, sum_done + sum_todo):
            report.fail(
                "软",
                "S1",
                f"计划数值统计与明细不符：头部 {plan.effort}，明细 {(sum_done, sum_todo, sum_done + sum_todo)}",
            )

    for plan_id in sorted(plan.todo):
        report.ok()
        if plan_id not in plan.gantt:
            report.fail("软", "S2", f"剩余任务表有而甘特缺：{plan_id}")
    for plan_id in sorted(plan.gantt & set(plan.done)):
        report.fail("软", "S2", f"甘特仍含已完成任务节点：{plan_id}")

    for plan_id, doc in sorted(tasks.items()):
        if plan_id not in plan.done:
            continue
        report.ok()
        base = doc.path.parent
        has_record = any((base / name).is_dir() for name in spec.rule.record_dirs) or any(
            (child / name).is_dir() for child in base.iterdir() if child.is_dir() for name in spec.rule.record_dirs
        )
        if not has_record:
            record_names = " / ".join(spec.rule.record_dirs)
            report.fail("软", "S3", f"已完成任务缺 {record_names} 记录：{plan_id}（{base.name}）")


def run(cfg: Config, *, stage: str, all_stages: bool, strict: bool) -> int:
    """执行需求 / 任务 / 计划一致性核对。

    Args:
        cfg: 仓配置。
        stage: 阶段目录名（空串取配置缺省）。
        all_stages: 是否核对全部阶段。
        strict: 软提示是否同样阻断。

    Returns:
        int: 退出码（0 通过 / 1 有不合规 / 2 规则或目录缺失）。
    """
    try:
        rule = require(cfg.docs_status_rules, "docs_status", MIN_KEYS)
    except RulesError as exc:
        out(f"[docs-status] {exc}")
        return 2
    spec = build_spec(rule)
    project_root = cfg.root / rule.project_dir
    if not project_root.is_dir():
        out(f"[docs-status] 未找到阶段目录：{rule.project_dir}")
        return 2
    stages = (
        sorted(p for p in project_root.iterdir() if p.is_dir())
        if all_stages
        else [project_root / (stage or rule.stage)]
    )
    if all_stages:
        out("提示：--all 含历史阶段，其文档可能为早期口径，结果仅供人工参考")

    total_hard = 0
    total_soft = 0
    total_checks = 0
    req_index: dict[str, str] = {}
    for stage_dir in stages:
        if not stage_dir.is_dir():
            out(f"[{stage_dir.name}] 跳过：目录不存在")
            continue
        report = Report(stage=stage_dir.name)
        check_stage(stage_dir, report, spec, req_index)
        total_hard += len(report.hard)
        total_soft += len(report.soft)
        total_checks += report.checks
        out(f"\n[{report.stage}] 检查 {report.checks} 项：硬规则不合规 {len(report.hard)}，软提示 {len(report.soft)}")
        for finding in [*report.hard, *report.soft]:
            out(f"  [{finding.level}] {finding.code} {finding.message}")

    out(f"\n汇总：检查 {total_checks} 项；硬规则不合规 {total_hard}，软提示 {total_soft}")
    if total_hard or (strict and total_soft):
        return 1
    return 0
