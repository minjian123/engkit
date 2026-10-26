"""服务边界与跨库访问度量采集（`engkit base boundary-metrics`；只读）。

采集「服务边界越界数 / 跨库访问数 / 已登记例外数」：

1. **静态**：同进程调用 `service-boundaries` 的判定，取各分类计数与违规明细；
2. **运行时**（可选）：抓取运行期指标端点（`--metrics-url`），不可达 / 未配置则标注降级。

默认退出码 0（只作度量与留痕）；`--fail-on-violation` 时静态越界数非零退 1。
越界计数键、跨库访问键、例外登记键与指标名一律取配置 `[rules.metrics]`。
"""

from __future__ import annotations

import json
import re
import urllib.request
from datetime import date
from pathlib import Path

from ..config import Config, MetricsRules
from ..console import out
from . import service_boundaries
from .rules import RulesError, require

MIN_KEYS = ("violation_keys", "cross_access_key", "registered_exceptions_key", "metric_name")
"""未登记时报错给出的最小键指引。"""

SCRAPE_TIMEOUT = 10
"""指标端点抓取超时（秒）。"""


def metric_line_pattern(metric_name: str) -> re.Pattern[str]:
    """构造指标行正则（标签集可省略）。

    Args:
        metric_name: 指标名。

    Returns:
        re.Pattern[str]: 多行匹配的指标行正则。
    """
    return re.compile(rf"^{re.escape(metric_name)}(?:\{{[^}}]*\}})?\s+([0-9.eE+]+)\s*$", re.MULTILINE)


def run_static(cfg: Config) -> dict[str, object]:
    """运行静态边界校验并取其结果（结构同 `service-boundaries --json`）。

    Args:
        cfg: 仓配置。

    Returns:
        dict[str, object]: 校验结果（`counts` / `problems` / `passed`）；规则缺失时 `error`。
    """
    try:
        boundaries = require(cfg.boundaries_rules, "service_boundaries", service_boundaries.MIN_KEYS)
    except RulesError as exc:
        return {"error": str(exc)}
    try:
        outcome = service_boundaries.check(cfg, boundaries)
    except (RulesError, service_boundaries.RegistryError) as exc:
        return {"error": f"边界校验不可执行：{exc}"}
    return {"passed": not outcome.problems, "counts": dict(outcome.counts), "problems": list(outcome.problems)}


def scrape_runtime(metrics_url: str, metric_name: str) -> dict[str, object]:
    """抓取运行期跨库访问计数（未配置 / 不可达则降级）。

    Args:
        metrics_url: 指标端点地址（空串表示未配置）。
        metric_name: 指标名。

    Returns:
        dict[str, object]: `available` + `cross_access_total`（或 `reason`）。
    """
    if not metrics_url:
        return {"available": False, "reason": "未配置 --metrics-url（运行期遥测随可观测栈）"}
    try:
        with urllib.request.urlopen(metrics_url, timeout=SCRAPE_TIMEOUT) as response:
            text = response.read().decode("utf-8", errors="ignore")
    except Exception as exc:
        return {"available": False, "reason": f"指标端点不可达：{type(exc).__name__}"}
    matches = [float(value) for value in metric_line_pattern(metric_name).findall(text)]
    return {"available": True, "cross_access_total": sum(matches) if matches else 0.0}


def summarize(static: dict[str, object], rule: MetricsRules) -> dict[str, object]:
    """汇总越界数 / 跨库访问数 / 例外登记数。

    Args:
        static: 静态校验结果。
        rule: 度量规则段。

    Returns:
        dict[str, object]: 汇总（静态不可执行时三项为 None）。
    """
    if "error" in static:
        return {"boundary_violations": None, "cross_db_access": None, "registered_exceptions": None}
    counts = static.get("counts")
    table = counts if isinstance(counts, dict) else {}
    violations = sum(int(table.get(key, 0)) for key in rule.violation_keys)
    return {
        "boundary_violations": violations,
        "cross_db_access": int(table.get(rule.cross_access_key, 0)),
        "registered_exceptions": table.get(rule.registered_exceptions_key),
    }


def report(payload: dict[str, object], rule: MetricsRules) -> None:
    """打印度量结论。

    Args:
        payload: 采集结果（`collected_at` / `static` / `runtime` / `summary`）。
        rule: 度量规则段。
    """
    static = payload["static"]
    runtime = payload["runtime"]
    summary = payload["summary"]
    counts = static.get("counts") if isinstance(static, dict) else None
    table = counts if isinstance(counts, dict) else {}
    out(f"== 服务边界与跨库访问度量（{payload['collected_at']}）==")
    if isinstance(static, dict) and "error" in static:
        out(f"  静态校验失败：{static['error']}")
    else:
        assert isinstance(summary, dict)
        out(
            f"  静态：服务边界越界数 {summary['boundary_violations']}（跨库访问 {summary['cross_db_access']}、"
            f"表归属越界 {table.get('table_ownership_violation', 0)}、"
            f"表名前缀冲突 {int(table.get('table_name_conflict', 0)) + int(table.get('table_prefix_conflict', 0))}）"
        )
        out(
            f"  例外白名单：登记 {table.get(rule.registered_exceptions_key, 0)} 条、"
            f"命中 {table.get('registered_exceptions_used', 0)} 条"
        )
        out(
            f"  域级：布局登记文件 {table.get('domain_files_registered', 0)}、"
            f"未归属域 {table.get('domain_files_unregistered', 0)}"
        )
        out(
            f"  域级越界：跨域 import {table.get('cross_domain_import', 0)}、"
            f"表引用 {table.get('cross_domain_table_reference', 0)}、写 {table.get('cross_domain_write', 0)}、"
            f"表声明 {table.get('cross_domain_table_declare', 0)}；"
            f"域级基线 {table.get('domain_baseline_total', 0)} 条（命中 {table.get('domain_baseline_used', 0)} 条）"
        )
        problems = static.get("problems") if isinstance(static, dict) else None
        if isinstance(problems, list) and problems:
            out(f"  违规明细（{len(problems)} 项）：")
            for problem in problems:
                out(f"    {problem}")
    assert isinstance(runtime, dict)
    if runtime.get("available"):
        out(f"  运行时：跨库访问计数 {runtime.get('cross_access_total')}")
    else:
        out(f"  运行时：未采集（{runtime.get('reason')}）")


def run(cfg: Config, *, metrics_url: str, out_path: str, fail_on_violation: bool) -> int:
    """采集静态与运行时计数并打印 / 落盘。

    Args:
        cfg: 仓配置。
        metrics_url: 运行期指标端点（空串表示未配置）。
        out_path: 结果写为 JSON 的路径（空串表示只打印）。
        fail_on_violation: 静态越界非零时退码 1。

    Returns:
        int: 退出码（0 采集完成 / 1 有越界 / 2 规则未登记）。
    """
    try:
        rule = require(cfg.metrics_rules, "metrics", MIN_KEYS)
    except RulesError as exc:
        out(f"[boundary_metrics] {exc}")
        return 2
    static = run_static(cfg)
    runtime = scrape_runtime(metrics_url, rule.metric_name)
    summary = summarize(static, rule)
    payload: dict[str, object] = {
        "collected_at": date.today().isoformat(),
        "static": static,
        "runtime": runtime,
        "summary": summary,
    }
    report(payload, rule)
    if out_path:
        target = Path(out_path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        out(f"已写入 JSON：{out_path}")
    if fail_on_violation and summary["boundary_violations"]:
        return 1
    return 0
