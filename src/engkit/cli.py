"""命令行入口：`engkit <子命令>`。

子命令：`links` / `docs-scope` / `selection` / `run-selected` / `coverage` / `preflight` /
`run-once` / `guard` / `kiwi`。各仓的既有入口脚本只做转发，参数与退出码保持不变。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from . import coverage, docs_links, docs_scope, eol, gitutil, guard, kiwi, preflight, runonce, selection
from .config import CONFIG_NAME, Config, ConfigError, load_for
from .console import configure_stdio, error, out

VERSION = "0.1.0"
"""包版本。"""


def _build_parser() -> argparse.ArgumentParser:
    """构造参数解析器。

    Returns:
        argparse.ArgumentParser: 解析器。
    """
    parser = argparse.ArgumentParser(prog="engkit", description="跨仓工程化工具包")
    parser.add_argument("--version", action="version", version=f"engkit {VERSION}")
    subparsers = parser.add_subparsers(dest="command", required=True)

    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--root", default="", help="仓库根（缺省为当前目录）")
    common.add_argument("--config", default="", help=f"配置路径（缺省按仓根查找 {CONFIG_NAME}）")

    links = subparsers.add_parser("links", parents=[common], help="文档链接与锚点校验")
    links.add_argument("--quiet", action="store_true", help="只打印结论行")

    scope = subparsers.add_parser("docs-scope", parents=[common], help="文档口径护栏（进度类字段落点）")
    scope.add_argument("--self-test", action="store_true", help="只跑内置判定向量")

    choose = subparsers.add_parser("selection", parents=[common], help="变更 → 受影响测试 / 前端包")
    choose.add_argument("--scope", default="backend", choices=("backend", "frontend"), help="推导范围")
    choose.add_argument("--changed-from", default="", help="对比基准（git 引用）")
    choose.add_argument("--changed-file", action="append", default=[], help="显式变更文件（可重复）")
    choose.add_argument("--changed-file-list", default="", help="变更清单文件（一行一个）")
    choose.add_argument("--force-full", action="store_true", help="强制退化全量")
    choose.add_argument(
        "--format",
        default="json",
        choices=("json", "pytest-args", "cov-args", "projects", "packages", "changed-services"),
        help="输出格式",
    )
    choose.add_argument("--base-threshold", type=int, default=None, help="基础模块绝对阈值")
    choose.add_argument("--base-ratio", type=float, default=None, help="基础模块占比阈值")

    selected = subparsers.add_parser("run-selected", parents=[common], help="定向执行器（选测 + 覆盖率 + 扩圈）")
    selected.add_argument("--changed-from", default="", help="对比基准")
    selected.add_argument("--changed-file", action="append", default=[], help="显式变更文件（可重复）")
    selected.add_argument("--changed-file-list", default="", help="变更清单文件")
    selected.add_argument("--force-full", action="store_true", help="强制退化全量")
    selected.add_argument("--pytest-cmd", default="", help="执行器前缀（缺省取配置）")
    selected.add_argument("--pytest-cwd", default="", help="工作目录（缺省取配置）")
    selected.add_argument("--pytest-arg", action="append", default=[], help="透传 pytest 参数")
    selected.add_argument("--jobs", default=preflight.DEFAULT_JOBS, help="并行档位：auto / 正整数 / off")
    selected.add_argument("--cov", dest="cov", action="store_true", default=True, help="开启覆盖率门禁（缺省开）")
    selected.add_argument("--no-cov", dest="cov", action="store_false", help="跳过覆盖率门禁")
    selected.add_argument("--cov-fail-under", type=float, default=None, help="门禁阈值（缺省取配置）")
    selected.add_argument("--max-rounds", type=int, default=None, help="扩圈轮次上限（缺省取配置）")
    selected.add_argument("--cov-json", default="", help="覆盖率报告落点")
    selected.add_argument("--timeout", type=float, default=0.0, help="单轮超时（秒；0 不设限）")

    gate = subparsers.add_parser("coverage", parents=[common], help="覆盖率门禁（合并并行采集数据）")
    gate.add_argument("--data-file", required=True, help="覆盖率数据文件基址")
    gate.add_argument("--out", required=True, help="JSON 报告落点")
    gate.add_argument("--module", action="append", default=[], help="门禁范围模块（可重复）")
    gate.add_argument("--fail-under", type=float, default=100.0, help="门禁阈值")
    gate.add_argument("--quiet", action="store_true", help="不打印度量行")
    gate.add_argument("--clean", action="store_true", help="判定前先清理旧采集文件")

    pre = subparsers.add_parser("preflight", parents=[common], help="本地预检")
    mode = pre.add_mutually_exclusive_group()
    mode.add_argument("--fast", action="store_true", help="秒级自检（跳过测试段）")
    mode.add_argument("--full", action="store_true", help="全量档（显式申请）")
    pre.add_argument("--no-scope", action="store_true", help="全量档跳过逐工程范围")
    pre.add_argument("--no-cov", action="store_true", help="跳过覆盖率门禁")
    pre.add_argument("--jobs", default=preflight.DEFAULT_JOBS, help="并行档位")
    pre.add_argument("--budget-seconds", type=float, default=None, help="总体预算（秒）")
    pre.add_argument("--step-seconds", type=float, default=None, help="单步预算（秒）")
    pre.add_argument("--self-test", action="store_true", help="只跑引擎自检")

    once = subparsers.add_parser("run-once", help="重命令包装（缓存回放）")
    once.add_argument("--force", action="store_true", help="忽略缓存真跑")
    once.add_argument("--show", action="store_true", help="只看缓存状态，不执行")
    once.add_argument("--window-seconds", type=int, default=None, help="缓存窗口（秒）")
    once.add_argument("--cache-dir", default="", help="缓存目录（缺省取配置）")
    once.add_argument("--root", default="", help="仓库根")
    once.add_argument("--config", default="", help="配置路径")
    once.add_argument("argv", nargs=argparse.REMAINDER, help="`--` 之后的命令")

    hook = subparsers.add_parser("guard", parents=[common], help="重命令拦截")
    actions = hook.add_subparsers(dest="action", required=True)
    check = actions.add_parser("check", parents=[common], help="判定一次调用是否拦截")
    check.add_argument("argv", nargs=argparse.REMAINDER, help="`--` 之后的命令")
    for name, text in (
        ("install-hook", "安装 .pth 启动钩子"),
        ("check-hook", "校验 .pth 启动钩子"),
        ("remove-hook", "卸载 .pth 启动钩子"),
    ):
        action = actions.add_parser(name, parents=[common], help=text)
        action.add_argument("--venv", default="", help="虚拟环境目录（缺省取配置）")

    eol_parser = subparsers.add_parser("eol", parents=[common], help="行尾护栏（工作树 CRLF 检查）")
    eol_parser.add_argument("--quiet", action="store_true", help="只打印结论行")
    eol_parser.add_argument("--fix", action="store_true", help="就地修正为 LF（写回工作树）")

    kiwi_parser = subparsers.add_parser("kiwi", parents=[common], help="Kiwi TCMS 用例登记与回读")
    kiwi_actions = kiwi_parser.add_subparsers(dest="action", required=True)
    register = kiwi_actions.add_parser("register", parents=[common], help="批量登记用例")
    register.add_argument("--cases", required=True, help="用例清单（TOML）")
    register.add_argument("--dry-run", action="store_true", help="只打印将执行的脚本")
    register.add_argument("--author", default="admin", help="用例作者账号")
    listing = kiwi_actions.add_parser("list", parents=[common], help="回读最近用例")
    listing.add_argument("--limit", type=int, default=10, help="条数")
    kiwi_actions.add_parser("ping", parents=[common], help="探测远端可达")

    base_parser = subparsers.add_parser("base", parents=[common], help="基座校验器（规则取自 [rules.*]）")
    base_actions = base_parser.add_subparsers(dest="checker", required=True)
    base_named = (
        ("base", "基座完整性（清单 / 跨层 / 命名 / 设计登记）"),
        ("backend-base", "后端基类体系（继承链 / 错误码 / 迁移链）"),
        ("bare-collections", "集合声明白名单"),
        ("system-fields", "系统字段（form_key / data_source_key）"),
        ("prototype-review", "界面「实现 ↔ 原型」对照"),
        ("env-example", "环境变量模板键位"),
        ("docs-status", "需求 / 任务 / 计划一致性"),
    )
    for name, text in base_named:
        action = base_actions.add_parser(name, parents=[common], help=text)
        if name in (
            "base",
            "backend-base",
            "service-boundaries",
            "bare-collections",
            "system-fields",
            "prototype-review",
            "env-example",
        ):
            action.add_argument("--self-test", action="store_true", help="只跑内置判定向量")
        if name in ("backend-base", "service-boundaries", "bare-collections"):
            action.add_argument("--update-baseline", action="store_true", help="按当前扫描结果重写基线")
        if name in ("service-boundaries", "bare-collections"):
            action.add_argument("--json", action="store_true", help="以 JSON 输出结果")
        if name == "bare-collections":
            action.add_argument("--report", action="store_true", help="只打印统计报告")
        if name == "prototype-review":
            action.add_argument("--changed", nargs="?", const="", default=None, help="比较基准（缺省取配置）")
            action.add_argument("--task", default="", help="只核对单个任务目录")
        if name == "docs-status":
            action.add_argument("--stage", default="", help="阶段目录名（缺省取配置）")
            action.add_argument("--all", action="store_true", help="核对全部阶段")
            action.add_argument("--strict", action="store_true", help="软提示同样阻断")
        if name == "boundary-metrics":
            action.add_argument("--metrics-url", default="", help="运行期指标端点（可选）")
            action.add_argument("--out", default="", help="结果写为 JSON")
            action.add_argument("--fail-on-violation", action="store_true", help="静态越界非零时退码 1")

    return parser


def _root(args: argparse.Namespace) -> Path:
    """解析仓库根。

    Args:
        args: 解析结果。

    Returns:
        Path: 仓库根绝对路径。
    """
    return Path(args.root or ".").resolve()


def _load(args: argparse.Namespace) -> Config:
    """加载配置。

    Args:
        args: 解析结果。

    Returns:
        Config: 配置对象。
    """
    return load_for(_root(args), getattr(args, "config", ""))


def _cmd_links(args: argparse.Namespace) -> int:
    """执行链接校验。

    Args:
        args: 解析结果。

    Returns:
        int: 退出码。
    """
    try:
        report = docs_links.check(_load(args))
    except FileNotFoundError as exc:
        error(f"[links] {exc}")
        return 2
    if not args.quiet:
        for item in report.findings[:50]:
            error(f"[{item.kind}] {item.file}:{item.line} -> {item.target}")
    out(f"链接自洽：断链 {report.broken} 处，失效锚点 {report.anchors} 处（扫描根：{_root(args)}）")
    return 1 if report.findings else 0


def _cmd_docs_scope(args: argparse.Namespace) -> int:
    """执行文档口径检查。

    Args:
        args: 解析结果。

    Returns:
        int: 退出码。
    """
    cfg = _load(args)
    if args.self_test:
        return _scope_self_test(cfg)
    try:
        report = docs_scope.check(cfg)
    except FileNotFoundError as exc:
        error(f"[docs-scope] {exc}")
        return 2
    for item in report.findings:
        error(f"{item.file}:{item.line}: [{item.label}] {item.snippet}")
    out(f"[docs-scope] 检查 {report.files} 份文档：违规 {len(report.findings)} 处")
    if report.findings:
        error("进度类字段只允许出现在阶段计划与总体规划；详见《计划文档规范》「排期唯一落点与边界」节")
        return 1
    return 0


def _scope_self_test(cfg: Config) -> int:
    """文档口径护栏的内置判定向量。

    Args:
        cfg: 仓配置。

    Returns:
        int: 退出码（0 通过 / 1 失败）。
    """
    samples: tuple[tuple[str, bool], ...] = (
        ("本任务 4h 交付", True),
        ("合计 34.5h", True),
        ("工时台账已回写", True),
        ("剩余排期见计划", True),
        ("M1 门禁达成", True),
        ("甘特图见计划", True),
        ("按波次推进", True),
        ("工期偏差 20%", True),
        ("本任务交付会话管理三端点，用例 Kiwi 2195", False),
        ("恢复：RTO ≤4h、RPO ≤1h", False),
        ("版本 3.14 与 1.5 倍", False),
        ("见《计划文档规范》「排期窗口格式」节 <!-- docs-scope:allow:引用规范原文 -->", False),
    )
    failures: list[str] = []
    for text, expected in samples:
        actual = bool(docs_scope.scan_text(text, cfg))
        if actual != expected:
            failures.append(f"{text!r}：实际 {actual}，期望 {expected}")
    whitelist_samples: tuple[tuple[str, bool], ...] = (
        ("规范/计划文档规范.md", True),
        ("项目/06_认证与安全/计划/01_计划_认证与安全.md", True),
        ("项目/06_认证与安全/任务/01_认证与会话/01_认证与会话.md", False),
        ("设计/架构设计/03_架构设计_总体架构.md", False),
    )
    if cfg.docs.scope.plan_dir_pattern:
        for rel, expected in whitelist_samples:
            actual = docs_scope.is_whitelisted(rel, cfg)
            if actual != expected:
                failures.append(f"{rel!r}：白名单判定 {actual}，期望 {expected}")
    if failures:
        out("[docs-scope --self-test] 失败：")
        for item in failures:
            out(f"  - {item}")
        return 1
    out("[docs-scope --self-test] 全部通过")
    return 0


def _cmd_selection(args: argparse.Namespace) -> int:
    """推导选测结果并打印。

    Args:
        args: 解析结果。

    Returns:
        int: 退出码。
    """
    cfg = _load(args)
    try:
        changed = selection.read_changed(
            cfg.root,
            # 未显式给基准时按仓配置的 `[preflight].since`（与预检 / 执行器同一口径）
            changed_from=args.changed_from or cfg.preflight.since,
            changed_files=tuple(args.changed_file),
            changed_file_list=args.changed_file_list,
        )
    except RuntimeError as exc:
        error(f"[test-selection] {exc}（确需全量请加 --force-full）")
        return 1
    if args.format == "changed-services":
        services = (
            [] if cfg.backend is None else gitutil.changed_services(changed, cfg.backend.root, cfg.backend.groups)
        )
        out(" ".join(services))
        return 0
    if args.scope == "frontend":
        result = selection.build_frontend_selection(cfg, changed, force_full=args.force_full)
        out(_format_frontend(result, args.format))
        return 0
    try:
        picked = selection.build_selection(
            cfg,
            changed,
            base_threshold=args.base_threshold,
            base_ratio=args.base_ratio,
            force_full=args.force_full,
        )
    except RuntimeError as exc:
        error(f"[test-selection] {exc}")
        return 1
    out(_format_backend(picked, args.format))
    return 0


def _format_backend(result: selection.Selection, fmt: str) -> str:
    """按格式输出后端选测结果。

    Args:
        result: 选测结果。
        fmt: 输出格式。

    Returns:
        str: 输出文本。
    """
    if fmt == "pytest-args":
        return " ".join(result.test_paths)
    if fmt == "cov-args":
        return " ".join(f"--cov={module}" for module in result.cov_modules)
    if fmt == "projects":
        return " ".join(metric.name for metric in result.projects)
    payload = {
        "test_paths": list(result.test_paths),
        "cov_modules": list(result.cov_modules),
        "degraded": result.degraded,
        "reason": result.reason,
        "picked": result.picked,
        "total": result.total,
        "projects": {
            metric.name: {"picked": metric.picked, "total": metric.total, "degraded": metric.degraded}
            for metric in result.projects
        },
        "notes": list(result.notes),
    }
    return json.dumps(payload, ensure_ascii=False, indent=2)


def _format_frontend(result: selection.FrontendSelection, fmt: str) -> str:
    """按格式输出前端选测结果。

    Args:
        result: 前端选测结果。
        fmt: 输出格式。

    Returns:
        str: 输出文本。
    """
    if fmt == "packages":
        return " ".join(result.packages)
    payload = {
        "packages": list(result.packages),
        "degraded": result.degraded,
        "reason": result.reason,
        "notes": list(result.notes),
    }
    return json.dumps(payload, ensure_ascii=False, indent=2)


def _cmd_run_selected(args: argparse.Namespace) -> int:
    """执行定向测试。

    Args:
        args: 解析结果。

    Returns:
        int: 退出码。
    """
    cfg = _load(args)
    return preflight.run_selected(
        cfg,
        changed_from=args.changed_from,
        changed_files=tuple(args.changed_file),
        changed_file_list=args.changed_file_list,
        force_full=args.force_full,
        jobs=args.jobs,
        cov=args.cov,
        cov_fail_under=args.cov_fail_under,
        max_rounds=args.max_rounds,
        timeout=args.timeout,
        pytest_cmd=args.pytest_cmd,
        pytest_cwd=args.pytest_cwd,
        pytest_args=tuple(args.pytest_arg),
        report_path=args.cov_json,
    )


def _cmd_coverage(args: argparse.Namespace) -> int:
    """执行覆盖率门禁。

    Args:
        args: 解析结果。

    Returns:
        int: 退出码。
    """
    data_file = Path(args.data_file).resolve()
    if args.clean:
        coverage.cleanup(data_file)
    try:
        return coverage.check(
            data_file,
            Path(args.out).resolve(),
            tuple(args.module),
            fail_under=args.fail_under,
            quiet=args.quiet,
        )
    except RuntimeError as exc:
        error(f"[coverage] {exc}")
        return 2


def _cmd_preflight(args: argparse.Namespace) -> int:
    """执行预检。

    Args:
        args: 解析结果。

    Returns:
        int: 退出码。
    """
    cfg = _load(args)
    if args.self_test:
        return preflight.self_test(cfg)
    mode = "full" if args.full else ("fast" if args.fast else "scoped")
    return preflight.run_preflight(
        cfg,
        mode=mode,
        jobs=args.jobs,
        no_cov=args.no_cov,
        no_scope=args.no_scope,
        step_seconds=args.step_seconds,
        budget_seconds=args.budget_seconds,
    )


def _cmd_run_once(args: argparse.Namespace) -> int:
    """执行重命令包装。

    Args:
        args: 解析结果。

    Returns:
        int: 退出码。
    """
    argv = list(args.argv)
    if argv and argv[0] == "--":
        argv = argv[1:]
    if not argv:
        error("[run-once] 缺少命令：用法 `engkit run-once [选项] -- <命令...>`")
        return 2
    cfg = _load(args)
    cache_dir = Path(args.cache_dir) if args.cache_dir else cfg.root / cfg.runonce.cache_dir
    window = cfg.runonce.window_seconds if args.window_seconds is None else args.window_seconds
    return runonce.run(
        cfg.root,
        argv,
        cache_dir=cache_dir,
        window_seconds=window,
        force=args.force,
        show_only=args.show,
    )


def _cmd_guard(args: argparse.Namespace) -> int:
    """执行重命令拦截相关动作。

    Args:
        args: 解析结果。

    Returns:
        int: 退出码。
    """
    cfg = _load(args)
    if args.action == "check":
        argv = list(args.argv)
        if argv and argv[0] == "--":
            argv = argv[1:]
        if not argv:
            error("[guard] 缺少命令：用法 `engkit guard check -- <命令...>`")
            return 2
        kind = guard.should_block(argv, cfg.guard)
        if kind:
            error(guard.heavy_guidance(kind))
            return 1
        out("[guard] 放行")
        return 0
    venv = Path(args.venv) if args.venv else cfg.root / ".venv"
    found = guard.site_packages(venv)
    if found is None:
        error(f"[guard] 未找到 venv 的 site-packages：{venv}")
        return 1
    source_root = Path(__file__).resolve().parents[1]
    if args.action == "install-hook":
        target = guard.install_hook(found, source_root)
        out(f"[guard] 已安装：{target}")
        return 0
    if args.action == "check-hook":
        return guard.check_hook(found, source_root)
    return guard.remove_hook(found)


def _cmd_eol(args: argparse.Namespace) -> int:
    """执行行尾检查（可选就地修正）。

    Args:
        args: 解析结果。

    Returns:
        int: 退出码。
    """
    cfg = _load(args)
    report = eol.check(cfg.root, cfg.eol)
    if report.skipped:
        out(f"[eol] {report.skipped}")
        return 0
    if args.fix and report.findings:
        fixed = eol.normalize(cfg.root, report)
        out(f"[eol] 已把 {len(fixed)} 个文件改回 LF")
        return 0
    out(f"[eol] 检查 {report.files} 个文件：行尾违规 {len(report.findings)} 个")
    if not args.quiet:
        for item in report.findings[:50]:
            error(f"{item.file}: 工作树为 {item.actual}，按 .gitattributes 应为 {item.expected}")
        if len(report.findings) > 50:
            error(f"...（其余 {len(report.findings) - 50} 个从略）")
    if report.findings:
        if not args.quiet:
            error("改用 LF 保存：编辑器设 files.eol，脚本写文件显式指定 newline；或执行 engkit eol --fix")
        return 1
    return 0


def _cmd_kiwi(args: argparse.Namespace) -> int:
    """执行用例登记相关动作。

    Args:
        args: 解析结果。

    Returns:
        int: 退出码。
    """
    cfg = _load(args)
    if args.action == "register":
        return kiwi.register(cfg, Path(args.cases), dry_run=args.dry_run, author=args.author)
    if args.action == "list":
        return kiwi.list_cases(cfg, limit=args.limit)
    return kiwi.ping(cfg)


def _cmd_base(args: argparse.Namespace) -> int:
    """执行基座校验器（规则取自仓配置 `[rules.*]`）。

    Args:
        args: 解析结果。

    Returns:
        int: 退出码。
    """
    cfg = _load(args)
    name = args.checker
    if name == "base":
        from .base import base_docs

        return base_docs.run(cfg, as_self_test=bool(args.self_test))
    if name == "backend-base":
        from .base import backend_base

        return backend_base.run(cfg, as_self_test=bool(args.self_test), update_baseline=bool(args.update_baseline))
    if name == "bare-collections":
        from .base import bare_collections

        return bare_collections.run(
            cfg,
            json_output=bool(args.json),
            report_mode=bool(args.report),
            update_baseline=bool(args.update_baseline),
            as_self_test=bool(args.self_test),
        )
    if name == "system-fields":
        from .base import system_fields

        return system_fields.run(cfg, as_self_test=bool(args.self_test))
    if name == "prototype-review":
        from .base import prototype_review

        rule = cfg.prototype_rules
        fallback = rule.base if rule is not None else ""
        changed = "" if args.changed is None else (str(args.changed) or fallback)
        return prototype_review.run(cfg, changed=changed, task=str(args.task), as_self_test=bool(args.self_test))
    if name == "env-example":
        from .base import env_example

        return env_example.run(cfg, as_self_test=bool(args.self_test))
    if name == "docs-status":
        from .base import docs_status

        return docs_status.run(cfg, stage=str(args.stage), all_stages=bool(args.all), strict=bool(args.strict))
    out(f"[base] 未实现的校验器：{name}")
    return 2


_HANDLERS = {
    "base": _cmd_base,
    "links": _cmd_links,
    "docs-scope": _cmd_docs_scope,
    "selection": _cmd_selection,
    "run-selected": _cmd_run_selected,
    "coverage": _cmd_coverage,
    "preflight": _cmd_preflight,
    "run-once": _cmd_run_once,
    "guard": _cmd_guard,
    "eol": _cmd_eol,
    "kiwi": _cmd_kiwi,
}
"""子命令 → 处理函数。"""


def main(argv: list[str] | None = None) -> int:
    """命令行入口。

    Args:
        argv: 参数（缺省取 `sys.argv[1:]`）。

    Returns:
        int: 退出码。
    """
    configure_stdio()
    args = _build_parser().parse_args(sys.argv[1:] if argv is None else argv)
    try:
        return _HANDLERS[args.command](args)
    except ConfigError as exc:
        error(f"[engkit] {exc}")
        return 2


if __name__ == "__main__":  # pragma: no cover - 模块直接执行
    raise SystemExit(main())
