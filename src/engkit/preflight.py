"""预检引擎与定向执行器（`engkit preflight` / `engkit run-selected`）。

引擎只做调度：变更检测、档位过滤、预算护栏、步骤执行、自测按需、覆盖率门禁与扩圈、度量输出；
**具体校验命令一律来自仓配置**（`engkit.toml` 的 `[[preflight.steps]]`），包内不内置任何仓专属步骤。
"""

from __future__ import annotations

import contextlib
import os
import re
import shlex
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import coverage, gitutil, guard, preflight_gate, selection
from .config import Config, SelfTest, Step
from .console import finish, note, out, section

DEFAULT_JOBS = "auto"
"""缺省并行档位。"""

_OFF_VALUES = ("", "off", "none", "0")
"""串行取值。"""

_POSITIVE_INT = re.compile(r"[1-9]\d*")
"""正整数形态。"""

_METRICS_RE = re.compile(r"\[test-selection\] picked=(\d+)/(\d+) degraded=(true|false) rounds=(\d+) cov=(\d+) 模块")
"""执行器度量行（供预检汇总）。"""

TIMEOUT_CODE = 124
"""单轮超时退出码（与既有口径一致）。"""


@dataclass
class Budget:
    """预算护栏：单步 / 总体耗时双上限。

    Attributes:
        step_seconds: 单步耗时上限（秒）。
        total_seconds: 总体耗时上限（秒）。
        started: 起始时刻。
        exhausted: 总体预算是否已耗尽。
    """

    step_seconds: float
    total_seconds: float
    started: float = field(default_factory=time.monotonic)
    exhausted: bool = False

    def remaining(self) -> float:
        """总体剩余预算（秒；可为负）。"""
        return self.total_seconds - (time.monotonic() - self.started)


class Tee:
    """把输出同时写到原流与缓冲区（便于从子步骤输出里取度量行）。"""

    def __init__(self, stream: Any, sink: list[str]) -> None:
        """初始化。

        Args:
            stream: 原输出流。
            sink: 行缓冲（写入内容按行追加）。
        """
        self._stream = stream
        self._sink = sink
        self._buffer = ""

    def write(self, data: str) -> int:
        """写内容（原流实时透传，同时按行收集）。

        Args:
            data: 文本。

        Returns:
            int: 写入长度。
        """
        self._buffer += data
        while "\n" in self._buffer:
            line, self._buffer = self._buffer.split("\n", 1)
            self._sink.append(line)
        return self._stream.write(data)

    def flush(self) -> None:
        """刷新原流。"""
        self._stream.flush()


def jobs_args(value: str) -> tuple[str, ...]:
    """把并行档位取值转换为 pytest 参数。

    Args:
        value: `auto` / 正整数 / `off`（含 `none` / `0` / 空串）。

    Returns:
        tuple[str, ...]: pytest 并行参数（空元组表示串行）。

    Raises:
        ValueError: 取值非法。
    """
    normalized = value.strip().lower()
    if normalized in _OFF_VALUES:
        return ()
    if normalized == "auto":
        return ("-n", "auto")
    if _POSITIVE_INT.fullmatch(normalized):
        return ("-n", normalized)
    raise ValueError(f"--jobs 取值非法：{value!r}（合法：auto / 正整数 / off）")


def describe_jobs(value: str) -> str:
    """并行档位的可读描述。

    Args:
        value: 档位取值。

    Returns:
        str: 如 `并行 -n auto` / `串行（--jobs off）`。
    """
    args = jobs_args(value)
    return f"并行 {' '.join(args)}" if args else "串行（--jobs off）"


def xdist_available(cfg: Config) -> bool:
    """**pytest 那个解释器**里是否装了 pytest-xdist（`-n` 参数由它提供）。

    说明：分发包名 `pytest-xdist`、可导入模块名 `xdist`；探测必须用真正跑 pytest 的解释器
    （各仓的包装脚本可能由系统 Python 启动、pytest 却跑在 `uv run` 的 venv 里，用本进程判定会误报）。

    Args:
        cfg: 仓配置（解释器取 `[coverage].interpreter`，缺省用当前解释器）。

    Returns:
        bool: 已安装返回 True。
    """
    # 配置值可能带 `uv run python` 这类前缀（按 shlex 拆分）；缺省直接用当前解释器（不能再走 shlex：
    # Windows 路径里的反斜杠会被当转义符吃掉）
    prefix = shlex.split(cfg.coverage.interpreter) if cfg.coverage.interpreter else [sys.executable]
    try:
        completed = subprocess.run(
            [*prefix, "-c", "import xdist"],
            cwd=cfg.root,
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return completed.returncode == 0


def parallel_args(cfg: Config, value: str) -> tuple[str, ...]:
    """并行参数；未装 pytest-xdist 时**显式降级**为串行（不静默、不整条预检失败）。

    各仓测试框架插件不一定齐备（新仓尤甚），引擎不假定 `-n` 一定可用：缺装即打印降级原因并串行。

    Args:
        cfg: 仓配置。
        value: 并行档位取值（`auto` / 正整数 / `off`）。

    Returns:
        tuple[str, ...]: pytest 并行参数（空元组表示串行）。

    Raises:
        ValueError: 取值非法。
    """
    args = jobs_args(value)
    if args and not xdist_available(cfg):
        note("并行降级：pytest 解释器未安装 pytest-xdist ⇒ 串行执行（装上即可恢复并行）")
        return ()
    return args


def expand(cfg: Config, cmd: tuple[str, ...]) -> list[str]:
    """替换命令中的占位符（`{python}` / `{root}` / `{config}`）。

    Args:
        cfg: 仓配置。
        cmd: 命令模板。

    Returns:
        list[str]: 替换后的命令。
    """
    mapping = {"{python}": sys.executable, "{root}": str(cfg.root), "{config}": str(cfg.path)}
    return [mapping.get(token, token) for token in cmd]


def step_applies(step: Step, mode: str) -> bool:
    """步骤是否在当前档位执行。

    Args:
        step: 步骤。
        mode: 档位（`scoped` / `fast` / `full`）。

    Returns:
        bool: 适用返回 True。
    """
    return step.preset == "always" or step.preset == mode


def _resolve_cwd(cfg: Config, rel: str) -> Path:
    """把仓根相对目录解析为绝对路径。

    Args:
        cfg: 仓配置。
        rel: 相对路径（空串取预检缺省）。

    Returns:
        Path: 绝对路径。
    """
    return cfg.root / (rel or cfg.preflight.cwd)


def execute(
    label: str,
    argv: list[str],
    cwd: Path,
    budget: Budget,
    failures: list[str],
    *,
    timeout: float | None = None,
    env: dict[str, str] | None = None,
    capture: list[str] | None = None,
    optional: bool = False,
) -> bool:
    """执行一条命令（受预算护栏约束，逐行实时透传）。

    Args:
        label: 步骤标签。
        argv: 命令与参数。
        cwd: 工作目录。
        budget: 预算护栏。
        failures: 失败收集列表。
        timeout: 本步显式超时（秒）。
        env: 子进程环境变量。
        capture: 传入列表时收集全部输出行。
        optional: 命令不存在时只打印跳过、不计失败。

    Returns:
        bool: 是否成功。
    """
    if budget.exhausted:
        return False
    remaining = budget.remaining()
    if remaining <= 0:
        out(f"\n[engkit] {label}\n  中止（总体预算 {budget.total_seconds:.0f}s 已耗尽；全量交 CI）")
        budget.exhausted = True
        failures.append(f"预算护栏（总体超时）：{label}")
        return False
    limit = min(timeout, remaining) if timeout is not None else min(budget.step_seconds, remaining)
    out(f"\n[engkit] {label}\n  $ {' '.join(argv)}")
    try:
        process = subprocess.Popen(
            argv,
            cwd=cwd,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
        )
    except FileNotFoundError:
        if optional:
            note(f"跳过（未安装：{argv[0]}）")
            return True
        failures.append(f"{label}（命令不存在：{argv[0]}）")
        note(f"失败（命令不存在：{argv[0]}）")
        return False

    collected: list[str] = []
    reader = threading.Thread(target=_pump, args=(process, collected), daemon=True)
    reader.start()
    try:
        returncode = process.wait(timeout=limit)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait()
        reader.join(timeout=5)
        failures.append(f"预算护栏（单步超时 {limit:.0f}s）：{label}")
        note(f"超时中止（{limit:.0f}s）")
        return False
    reader.join()
    if capture is not None:
        capture.extend(collected)
    if returncode != 0:
        failures.append(label)
        note(f"失败（退出码 {returncode}）")
        return False
    note("通过")
    return True


def _pump(process: subprocess.Popen[str], collected: list[str]) -> None:
    """逐行透传子进程输出并收集。

    Args:
        process: 子进程。
        collected: 收集列表。
    """
    assert process.stdout is not None
    for raw in process.stdout:
        line = raw.rstrip("\n")
        collected.append(line)
        print(line, flush=True)


def _changes(cfg: Config) -> tuple[list[str], bool]:
    """取本次变更与「基准可判定」标志。

    Args:
        cfg: 仓配置。

    Returns:
        tuple[list[str], bool]: （变更路径, 是否可判定）。
    """
    return gitutil.changes(cfg.root, cfg.preflight.since)


def _self_test_selected(entry: SelfTest, changes: list[str]) -> bool:
    """变更集是否触及该自测（脚本自身或任一依赖前缀）。

    Args:
        entry: 自测登记项。
        changes: 变更路径。

    Returns:
        bool: 执行返回 True。
    """
    for path in changes:
        for dep in entry.deps:
            if path == dep or path.startswith(dep if dep.endswith("/") else f"{dep}/"):
                return True
    return False


def run_self_tests(
    cfg: Config,
    budget: Budget,
    failures: list[str],
    *,
    force_all: bool,
    mode: str,
) -> None:
    """执行护栏自测（按需 / 全跑 / 变更不可判定时全跑）。

    Args:
        cfg: 仓配置。
        budget: 预算护栏。
        failures: 失败收集列表。
        force_all: 是否强制全跑（`--fast` / `--full` 档）。
        mode: 当前档位（用于输出说明）。
    """
    if not cfg.preflight.self_tests:
        return
    changes, reliable = _changes(cfg)
    if force_all:
        out(f"\n[engkit] 护栏自测：全跑（--{mode} 档保留全跑口径）")
    elif not reliable:
        out("\n[engkit] 护栏自测：变更不可判定 ⇒ 全跑（不静默跳过）")
    for entry in cfg.preflight.self_tests:
        if not (force_all or not reliable or _self_test_selected(entry, changes)):
            out(f"\n[engkit] {entry.label}\n  跳过（变更未触及其依赖）")
            continue
        execute(entry.label, expand(cfg, entry.cmd), cfg.root, budget, failures)


def _coverage_env(cfg: Config, modules: list[str], cwd: Path) -> dict[str, str]:
    """构造覆盖率采集环境变量。

    Args:
        cfg: 仓配置。
        modules: 覆盖率目标模块（顶层包名去重）。
        cwd: pytest 工作目录。

    Returns:
        dict[str, str]: 子进程环境变量。
    """
    env = dict(os.environ)
    env[cfg.coverage.data_env] = str((cfg.root / cfg.coverage.data_file).resolve())
    env[cfg.coverage.source_env] = ",".join(sorted({module.split(".", 1)[0] for module in modules}))
    extra_paths = [str(cfg.root / item) for item in cfg.coverage.pythonpath]
    env["PYTHONPATH"] = os.pathsep.join(filter(None, [*extra_paths, str(cwd), env.get("PYTHONPATH", "")]))
    return env


def gate_command(cfg: Config, data_file: Path, report: Path, modules: list[str], fail_under: float) -> list[str]:
    """构造覆盖率门禁命令（在配置的解释器里跑 engkit 自身，不要求预先安装）。

    Args:
        cfg: 仓配置。
        data_file: 采集数据文件基址。
        report: JSON 报告落点。
        modules: 门禁范围模块。
        fail_under: 门禁阈值。

    Returns:
        list[str]: 命令。
    """
    source_root = Path(__file__).resolve().parents[1].as_posix()
    args = ["coverage", "--data-file", str(data_file), "--out", str(report), "--fail-under", str(fail_under)]
    for module in modules:
        args.extend(["--module", module])
    script = (
        "import sys; "
        f"sys.path.insert(0, {source_root!r}); "
        "from engkit.cli import main as engkit_main; "
        f"sys.exit(engkit_main({args!r}))"
    )
    interpreter = cfg.coverage.interpreter or cfg.pytest.cmd
    return [*shlex.split(interpreter), "-c", script]


def run_pytest_once(
    argv: list[str], cwd: Path, env: dict[str, str] | None, timeout: float | None
) -> tuple[int, list[str]]:
    """执行一轮命令并实时透传输出。

    Args:
        argv: 命令。
        cwd: 工作目录。
        env: 环境变量。
        timeout: 超时（秒；None 不设限）。

    Returns:
        tuple[int, list[str]]: （退出码, 输出行）。

    Raises:
        subprocess.TimeoutExpired: 超过 `timeout`（子进程已被结束）。
    """
    started = time.monotonic()
    process = subprocess.Popen(
        argv,
        cwd=cwd,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
        bufsize=1,
    )
    lines: list[str] = []
    assert process.stdout is not None
    for raw in process.stdout:
        line = raw.rstrip("\n")
        lines.append(line)
        print(line, flush=True)
        if timeout is not None and time.monotonic() - started > timeout:
            process.kill()
            process.wait()
            raise subprocess.TimeoutExpired(argv, timeout)
    return process.wait(), lines


def print_metrics(picked: int, total: int, degraded: bool, rounds: int, cov_modules: int) -> None:
    """打印统一度量行（供预检采集与复盘）。

    Args:
        picked: 选中测试文件数。
        total: 全部工程测试文件数。
        degraded: 是否退化。
        rounds: 执行轮次。
        cov_modules: 覆盖率目标模块数。
    """
    out(
        f"[test-selection] picked={picked}/{total} degraded={str(degraded).lower()} "
        f"rounds={rounds} cov={cov_modules} 模块"
    )


def metrics_summary(lines: list[str]) -> str:
    """从执行器输出里取最后一条度量行并转成汇总行。

    Args:
        lines: 执行器输出行。

    Returns:
        str: 汇总行正文；未取到返回空串。
    """
    for line in reversed(lines):
        match = _METRICS_RE.search(line)
        if match is None:
            continue
        picked, total, degraded, rounds, cov = match.groups()
        ratio = f"{int(picked) / int(total):.1%}" if int(total) else "—"
        return f"picked={picked}/{total}（{ratio}）degraded={degraded} rounds={rounds} cov={cov} 模块"
    return ""


def run_selected(
    cfg: Config,
    *,
    changed: list[str] | None = None,
    changed_from: str = "",
    changed_files: tuple[str, ...] = (),
    changed_file_list: str = "",
    force_full: bool = False,
    jobs: str = DEFAULT_JOBS,
    cov: bool = True,
    cov_fail_under: float | None = None,
    max_rounds: int | None = None,
    timeout: float = 0.0,
    pytest_cmd: str = "",
    pytest_cwd: str = "",
    pytest_args: tuple[str, ...] = (),
    report_path: str = "",
    depth: str = "",
) -> int:
    """推导 → 跑 pytest → 覆盖率判定 → 扩圈 → 退化 → 度量。

    Args:
        cfg: 仓配置。
        changed: 变更路径（已算出时直接给，避免二次 git diff）。
        changed_from: git 基准。
        changed_files: 显式变更文件。
        changed_file_list: 变更清单文件。
        force_full: 强制退化全量。
        jobs: 并行档位。
        cov: 是否开覆盖率门禁。
        cov_fail_under: 门禁阈值（缺省取配置）。
        max_rounds: 扩圈轮次上限（缺省取配置）。
        timeout: 单轮超时（秒；0 不设限）。
        pytest_cmd: 执行器前缀（缺省取配置）。
        pytest_cwd: 工作目录（相对仓根；缺省取配置）。
        pytest_args: 透传 pytest 参数。
        report_path: 覆盖率报告落点（相对仓根；缺省 `<数据文件>.json`）。
        depth: 依赖深度（空串取配置；预检固定传 `closure`）。

    Returns:
        int: pytest 退出码 / 覆盖率或推导异常 1 / 超时 124。
    """
    try:
        parallel = parallel_args(cfg, jobs)
    except ValueError as error:
        print(f"[test-selection] {error}", file=sys.stderr)
        return 1
    out(f"[test-selection] {describe_jobs(jobs)}")
    if changed is None:
        try:
            changed = selection.read_changed(
                cfg.root,
                # 未显式给基准时按仓配置的 `[preflight].since`（否则会「零变更 ⇒ 静默跳过」）
                changed_from=changed_from or cfg.preflight.since,
                changed_files=changed_files,
                changed_file_list=changed_file_list,
            )
        except RuntimeError as error:
            out(f"[test-selection] {error} ⇒ 显式退化全量")
            changed = []
            force_full = True
    try:
        result = selection.build_selection(cfg, changed, force_full=force_full, depth=depth)
    except RuntimeError as error:
        print(f"[test-selection] {error}", file=sys.stderr)
        return 1
    if result.reason:
        out(f"[test-selection] 退化：{result.reason}")
    for item in result.notes:
        out(f"[test-selection] 说明：{item}")
    if result.total:
        out(
            f"[test-selection] 变更 {len(changed)} 个文件；选中 {result.picked}/{result.total} 测试文件"
            f"（{result.picked / result.total:.1%}）"
        )
    else:
        out(f"[test-selection] 变更 {len(changed)} 个文件；无后端测试文件")
    fail_under = cfg.coverage.fail_under if cov_fail_under is None else cov_fail_under
    if result.cov_modules:
        out(f"[test-selection] 覆盖率门禁（行 + 分支 {fail_under:g}%）：{'、'.join(result.cov_modules)}")
    if not result.test_paths:
        out("[test-selection] 无受影响后端测试 ⇒ 跳过 pytest")
        return 0

    test_paths = list(result.test_paths)
    cov_modules = list(result.cov_modules)
    cov_enabled = cov and bool(cov_modules)
    data_file = (cfg.root / cfg.coverage.data_file).resolve()
    report = (cfg.root / (report_path or f"{cfg.coverage.data_file}.json")).resolve()
    workdir = cfg.root / (pytest_cwd or cfg.pytest.cwd)
    env = _coverage_env(cfg, cov_modules, workdir) if cov_enabled else dict(os.environ)
    if cov_enabled:
        out(f"[test-selection] 覆盖率采集：{data_file}（source={env[cfg.coverage.source_env]}）")
    rounds = 0
    expanded = 0
    degraded = result.degraded
    full_retried = degraded
    limit = timeout if timeout > 0 else None
    rounds_limit = cfg.pytest.max_rounds if max_rounds is None else max_rounds
    command_prefix = pytest_cmd or cfg.pytest.cmd

    while True:
        rounds += 1
        if cov_enabled:
            coverage.cleanup(data_file)
        argv = [*shlex.split(command_prefix), "-q", *test_paths]
        if cov_enabled and cfg.coverage.plugin:
            argv.extend(["-p", cfg.coverage.plugin])
        argv.extend(parallel)
        argv.extend(pytest_args)
        out(f"\n[test-selection] 第 {rounds} 轮：{len(test_paths)} 个测试文件")
        out(f"  $ {' '.join(argv)}")
        try:
            code, _lines = run_pytest_once(argv, workdir, env, limit)
        except subprocess.TimeoutExpired:
            out(f"[test-selection] 单轮超时（{limit:.0f}s）⇒ 中止；长命令交 CI")
            print_metrics(len(test_paths), result.total, degraded, rounds, len(cov_modules))
            return TIMEOUT_CODE

        if code != 0:
            print_metrics(len(test_paths), result.total, degraded, rounds, len(cov_modules))
            return code
        if not cov_enabled:
            print_metrics(len(test_paths), result.total, degraded, rounds, 0)
            return 0

        gate = subprocess.run(gate_command(cfg, data_file, report, cov_modules, fail_under), cwd=workdir, check=False)
        if gate.returncode == 0:
            print_metrics(len(test_paths), result.total, degraded, rounds, len(cov_modules))
            return 0
        if gate.returncode != 1:
            print_metrics(len(test_paths), result.total, degraded, rounds, len(cov_modules))
            return gate.returncode

        missing = selection.expand_from_coverage(report, cov_modules)
        if not missing:
            print_metrics(len(test_paths), result.total, degraded, rounds, len(cov_modules))
            return 1
        merged: list[str] = list(test_paths)
        for rel in selection.tests_for_modules(cfg, missing):
            if rel not in merged:
                merged.append(rel)
        if len(merged) == len(test_paths) or expanded >= rounds_limit:
            if full_retried:
                print_metrics(len(test_paths), result.total, degraded, rounds, len(cov_modules))
                return 1
            reason = "扩圈无新增测试" if len(merged) == len(test_paths) else "扩圈轮次用尽"
            out(f"[test-selection] {reason} ⇒ 退化工程级全量重跑（覆盖率缺口模块：{'、'.join(missing)}）")
            test_paths = selection.all_test_paths(cfg)
            degraded = True
            full_retried = True
            continue
        expanded += 1
        out(
            f"[test-selection] 扩圈第 {expanded} 轮：补 {len(merged) - len(test_paths)} 个测试文件"
            f"（缺行模块 {len(missing)} 个）"
        )
        test_paths = merged


def _run_scoped_step(
    cfg: Config, budget: Budget, failures: list[str], *, jobs: str, no_cov: bool, changes: list[str]
) -> None:
    """定向测试段：选测 + 覆盖率门禁 + 扩圈（子步骤输出实时透传，结束后汇总度量）。

    Args:
        cfg: 仓配置。
        budget: 预算护栏。
        failures: 失败收集列表。
        jobs: 并行档位。
        no_cov: 是否跳过覆盖率门禁。
        changes: 变更路径。
    """
    section("定向：按 git 变更推导受影响范围")
    try:
        preview = selection.build_selection(cfg, changes, depth="closure")
    except RuntimeError as error:
        failures.append(str(error))
        note(f"失败（{error}）")
        return
    if preview.reason:
        note(f"退化：{preview.reason}")
    for item in preview.notes:
        note(f"说明：{item}")
    if not preview.test_paths:
        out("  无后端变更 ⇒ 跳过 pytest（静态与护栏段已执行；全量交 CI）")
        return
    ratio = preview.picked / preview.total if preview.total else 0.0
    out(f"  测试文件：{preview.picked}/{preview.total}（{ratio:.1%}）")
    limit = min(budget.step_seconds, budget.remaining())
    if limit <= 0:
        budget.exhausted = True
        failures.append("预算护栏（总体超时）：后端测试：定向")
        return
    sink: list[str] = []
    out("\n[engkit] 后端测试：定向（选测 + 覆盖率门禁 + 扩圈）")
    with contextlib.redirect_stdout(Tee(sys.stdout, sink)):
        code = run_selected(cfg, changed=changes, jobs=jobs, cov=not no_cov, timeout=limit, depth="closure")
    summary = metrics_summary(sink)
    if summary:
        out(f"[engkit] 选测度量：{summary}")
    if code != 0:
        failures.append("后端测试：定向（选测 + 覆盖率门禁 + 扩圈）")


def run_full_tests(cfg: Config, budget: Budget, failures: list[str], *, jobs: str, no_cov: bool) -> None:
    """全量测试段：聚合全量 + 覆盖率门禁。

    Args:
        cfg: 仓配置。
        budget: 预算护栏。
        failures: 失败收集列表。
        jobs: 并行档位。
        no_cov: 是否跳过覆盖率门禁。
    """
    workdir = cfg.root / cfg.pytest.cwd
    argv = [*shlex.split(cfg.pytest.cmd), "-q"]
    env = dict(os.environ)
    if not no_cov:
        packages = list(cfg.backend.coverage_packages) if cfg.backend is not None else []
        coverage.cleanup((cfg.root / cfg.coverage.data_file).resolve())
        env = _coverage_env(cfg, packages, workdir)
        if cfg.coverage.plugin:
            argv.extend(["-p", cfg.coverage.plugin])
        out(f"\n[engkit] 覆盖率采集：{env[cfg.coverage.data_env]}（source={env[cfg.coverage.source_env]}）")
    argv.extend(parallel_args(cfg, jobs))
    execute("后端测试：聚合全量（--full）", argv, workdir, budget, failures, env=env)
    if no_cov:
        return
    data_file = (cfg.root / cfg.coverage.data_file).resolve()
    report = (cfg.root / f"{cfg.coverage.data_file}.json").resolve()
    execute(
        "覆盖率：全量门禁（行 + 分支）",
        gate_command(cfg, data_file, report, [], cfg.coverage.fail_under),
        workdir,
        budget,
        failures,
    )


def run_project_tests(cfg: Config, budget: Budget, failures: list[str], *, jobs: str) -> None:
    """全量档逐工程级范围。

    Args:
        cfg: 仓配置。
        budget: 预算护栏。
        failures: 失败收集列表。
        jobs: 并行档位。
    """
    if cfg.backend is None:
        return
    argv = [*shlex.split(cfg.pytest.cmd), "-q", *parallel_args(cfg, jobs)]
    for rel in cfg.backend.projects:
        execute(f"后端测试：工程级范围（{rel}）", argv, cfg.root / cfg.backend.root / rel, budget, failures)


def self_test(cfg: Config) -> int:
    """自测：预算、并行档位、步骤声明与工程登记对账（不触网、不跑测试）。

    Args:
        cfg: 仓配置。

    Returns:
        int: 退出码（0 通过 / 1 失败）。
    """
    out("[self-test] engkit preflight 引擎自检")
    zero = Budget(step_seconds=0.0, total_seconds=0.0)
    checks: list[tuple[str, bool]] = [
        ("零预算即判耗尽", zero.remaining() <= 0),
        ("并行默认 auto 且 off 即串行", jobs_args("auto") == ("-n", "auto") and jobs_args("off") == ()),
        ("命令步骤均声明 cmd", all(step.cmd for step in cfg.preflight.steps if step.type == "command")),
        ("步骤声明非空", bool(cfg.preflight.steps)),
        ("定向档总体预算小于全量档", cfg.preflight.budget.scoped[1] < cfg.preflight.budget.full[1]),
    ]
    if cfg.backend is not None:
        missing = selection.unregistered_projects(selection.discover_projects(cfg), cfg)
        checks.append(("自动发现的工程全部登记", not missing))
        for item in missing:
            note(item)
    failures = [name for name, ok in checks if not ok]
    for name, ok in checks:
        out(f"  {'通过' if ok else '失败'}（{name}）")
    if failures:
        out(f"[self-test] 失败 {len(failures)} 项：{'、'.join(failures)}")
        return 1
    out("[self-test] 全部通过")
    return 0


def preflight_blocked() -> bool:
    """预检入口是否应被重命令守卫拦截。

    Returns:
        bool: 应拦截返回 True。
    """
    return not (guard.is_ci() or guard.is_wrapped() or guard.allow_unwrapped())


def resolve_mode(*, push: bool, full: bool) -> str:
    """解析预检档位（无参即快速档；`--push` 走定向档；`--full` 优先）。

    Args:
        push: 是否显式声明推送前定向档。
        full: 是否显式申请全量档（隐含 `--push`）。

    Returns:
        str: 档位（`fast` / `scoped` / `full`）。
    """
    if full:
        return "full"
    if push:
        return "scoped"
    return "fast"


def run_preflight(
    cfg: Config,
    *,
    mode: str,
    jobs: str,
    no_cov: bool,
    no_scope: bool,
    step_seconds: float | None = None,
    budget_seconds: float | None = None,
    allow_dirty: bool = False,
) -> int:
    """执行预检。

    Args:
        cfg: 仓配置。
        mode: 档位（`scoped` / `fast` / `full`）。
        jobs: 并行档位。
        no_cov: 是否跳过覆盖率门禁。
        no_scope: 是否跳过逐工程范围（仅全量档有意义）。
        step_seconds: 单步预算覆盖。
        budget_seconds: 总体预算覆盖。
        allow_dirty: 脏工作树也放行（仅快速档；记入审计）。

    Returns:
        int: 退出码（0 全通过 / 1 有失败项或被闸门拒绝）。
    """
    if preflight_blocked():
        print(guard.heavy_guidance("script"), file=sys.stderr)
        return 1
    try:
        jobs_args(jobs)
    except ValueError as error:
        print(f"[engkit] {error}")
        return 1
    decision = preflight_gate.evaluate(cfg, allow_dirty=allow_dirty, mode=mode)
    if decision.blocked:
        print(preflight_gate.guidance(decision), file=sys.stderr)
        preflight_gate.write(cfg, event=preflight_gate.EVENT_REJECTED, mode=mode, decision=decision)
        return 1
    if decision.bypassed:
        out(f"[engkit] 预检闸门：`--allow-dirty` 放行（工作树 {len(decision.entries)} 处未提交改动，已记入审计）")
        preflight_gate.write(cfg, event=preflight_gate.EVENT_BYPASS, mode=mode, decision=decision)
    started = time.monotonic()
    scoped_budget, full_budget = cfg.preflight.budget.scoped, cfg.preflight.budget.full
    default_step, default_total = full_budget if mode == "full" else scoped_budget
    budget = Budget(
        step_seconds=default_step if step_seconds is None else step_seconds,
        total_seconds=default_total if budget_seconds is None else budget_seconds,
    )
    label = {"full": "全量（显式 --full）", "fast": "快速（只静态）"}.get(mode, "定向（快路径；全量请显式 --full）")
    out(f"[engkit] 档位：{label}")
    out(
        f"[engkit] 预算护栏：单步 {budget.step_seconds:.0f}s / 总体 {budget.total_seconds:.0f}s"
        "（超时即中止，全量交 CI）"
    )
    out(f"[engkit] {describe_jobs(jobs)}")
    failures: list[str] = []
    for step in cfg.preflight.steps:
        if not step_applies(step, mode):
            continue
        if step.type == "scoped_tests":
            changes, _ = _changes(cfg)
            _run_scoped_step(cfg, budget, failures, jobs=jobs, no_cov=no_cov, changes=changes)
            continue
        if step.type == "full_tests":
            run_full_tests(cfg, budget, failures, jobs=jobs, no_cov=no_cov)
            continue
        if step.type == "project_tests":
            if not no_scope:
                run_project_tests(cfg, budget, failures, jobs=jobs)
            continue
        execute(
            step.label,
            expand(cfg, step.cmd),
            _resolve_cwd(cfg, step.cwd),
            budget,
            failures,
            optional=step.optional,
        )
    run_self_tests(cfg, budget, failures, force_all=mode in ("fast", "full"), mode=mode)
    if budget.exhausted:
        out("\n[engkit] 提示：预算护栏已中止剩余步骤 —— 全量与集成交 CI（推送后按需触发流水线）。")
    code = finish("preflight", failures)
    preflight_gate.write(
        cfg,
        event=preflight_gate.EVENT_RUN,
        mode=mode,
        decision=decision,
        duration_seconds=time.monotonic() - started,
        exit_code=code,
    )
    return code
