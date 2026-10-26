"""Kiwi TCMS 用例批量登记与回读（`engkit kiwi`）。

传输走 SSH + `manage.py shell`（与既有部署说明的「用例登记（脚本化批量）」口径一致）：
脚本经 stdin 传入容器，不落盘；主机与账号取自各仓 `deploy/.env` 或环境变量，**任何凭据不打印、不入库**。
"""

from __future__ import annotations

import os
import subprocess
import sys
import tomllib
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from .config import Config

_DEFAULT_AUTHOR = "admin"
"""用例作者缺省值（可用 `[kiwi].author` 覆盖）。"""


class KiwiError(Exception):
    """远端操作或凭据缺失。"""


@dataclass(frozen=True)
class Case:
    """一条待登记用例。

    Attributes:
        summary: 用例标题。
        category: 分类名。
        priority: 优先级（`P1`~`P5`）。
        status: 状态（如 `CONFIRMED`）。
        tags: 标签。
        text: 用例文本（覆盖范围与自动化文件路径）。
    """

    summary: str
    category: str
    priority: str = "P2"
    status: str = "CONFIRMED"
    tags: tuple[str, ...] = ("自动化",)
    text: str = ""


@dataclass(frozen=True)
class RemoteResult:
    """一次远端命令的执行结果。

    Attributes:
        code: 退出码。
        output: 合并后的输出。
    """

    code: int
    output: str = ""


Runner = Callable[[Config, Path, str], RemoteResult]


def load_cases(path: Path) -> tuple[Case, ...]:
    """读取用例清单（TOML，`[[case]]` 数组）。

    Args:
        path: 清单文件路径。

    Returns:
        tuple[Case, ...]: 用例清单。

    Raises:
        KiwiError: 文件不可读、无法解析或条目缺字段。
    """
    try:
        raw = tomllib.loads(path.read_text(encoding="utf-8"))
    except OSError as error:
        raise KiwiError(f"读取用例清单失败：{path}（{error}）") from error
    except tomllib.TOMLDecodeError as error:
        raise KiwiError(f"用例清单解析失败：{path}（{error}）") from error
    entries = raw.get("case")
    if not isinstance(entries, list) or not entries:
        raise KiwiError(f"用例清单缺少 `[[case]]` 条目：{path}")
    cases: list[Case] = []
    for index, entry in enumerate(entries):
        if not isinstance(entry, dict):
            raise KiwiError(f"用例清单第 {index + 1} 条不是表")
        summary = entry.get("summary")
        category = entry.get("category")
        if not isinstance(summary, str) or not summary or not isinstance(category, str) or not category:
            raise KiwiError(f"用例清单第 {index + 1} 条缺少 summary / category")
        raw_tags = entry.get("tags")
        if raw_tags is None:
            tags: tuple[str, ...] = ("自动化",)
        elif isinstance(raw_tags, list) and all(isinstance(tag, str) for tag in raw_tags):
            tags = tuple(raw_tags)
        else:
            raise KiwiError(f"用例清单第 {index + 1} 条 tags 应为字符串数组")
        cases.append(
            Case(
                summary=summary,
                category=category,
                priority=str(entry.get("priority") or "P2"),
                status=str(entry.get("status") or "CONFIRMED"),
                tags=tuple(tags),
                text=str(entry.get("text") or ""),
            )
        )
    return tuple(cases)


def read_env_file(path: Path) -> dict[str, str]:
    """读取 `.env` 形态的键值文件（不存在时返回空表）。

    Args:
        path: 文件路径。

    Returns:
        dict[str, str]: 键值表。
    """
    if not path.is_file():
        return {}
    values: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8", errors="ignore").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, _, value = stripped.partition("=")
        values[key.strip()] = value.strip()
    return values


def credentials(cfg: Config) -> dict[str, str]:
    """取主机与账号（环境变量优先，其次凭据文件）。

    Args:
        cfg: 仓配置。

    Returns:
        dict[str, str]: `{"host": …, "user": …}`。

    Raises:
        KiwiError: 缺 `host` 或 `user`。
    """
    stored = read_env_file(cfg.root / cfg.kiwi.env_file)
    host = os.environ.get(cfg.kiwi.host_env, "") or stored.get(cfg.kiwi.host_env, "")
    user = os.environ.get(cfg.kiwi.user_env, "") or stored.get(cfg.kiwi.user_env, "")
    if not host or not user:
        raise KiwiError(
            f"缺少 Kiwi 主机 / 账号：请在 {cfg.kiwi.env_file} 或环境变量给出 "
            f"{cfg.kiwi.host_env} 与 {cfg.kiwi.user_env}（凭据不入库）"
        )
    return {"host": host, "user": user}


def ssh_target(cfg: Config) -> str:
    """拼接 SSH 目标串。

    Args:
        cfg: 仓配置。

    Returns:
        str: `账号@主机`。

    Raises:
        KiwiError: 凭据缺失。
    """
    found = credentials(cfg)
    return f"{found['user']}@{found['host']}"


def remote_argv(cfg: Config) -> list[str]:
    """构造远端执行命令（经 SSH 进容器跑 `manage.py shell`）。

    Args:
        cfg: 仓配置。

    Returns:
        list[str]: 命令。

    Raises:
        KiwiError: 凭据缺失。
    """
    target = ssh_target(cfg)
    return ["ssh", target, f"docker exec -i {cfg.kiwi.container} {cfg.kiwi.manage} shell"]


def default_runner(cfg: Config, cwd: Path, script: str) -> RemoteResult:
    """真实执行远端脚本（stdin 传入，不落盘）。

    Args:
        cfg: 仓配置。
        cwd: 本地工作目录。
        script: 待执行 Python 脚本。

    Returns:
        RemoteResult: 执行结果。

    Raises:
        KiwiError: 无法启动 SSH。
    """
    try:
        completed = subprocess.run(
            remote_argv(cfg),
            cwd=cwd,
            input=script,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
        )
    except OSError as error:
        raise KiwiError(f"无法执行 ssh：{error}") from error
    return RemoteResult(code=completed.returncode, output=f"{completed.stdout}{completed.stderr}")


def _quote(value: str) -> str:
    """把文本渲染为 Python 字面量。

    Args:
        value: 原文。

    Returns:
        str: 字面量。
    """
    return repr(value)


def register_script(cases: tuple[Case, ...], author: str = _DEFAULT_AUTHOR) -> str:
    """生成批量登记脚本（在容器内 `manage.py shell` 执行）。

    Args:
        cases: 用例清单。
        author: 作者账号。

    Returns:
        str: Python 脚本。
    """
    lines = [
        "from django.contrib.auth import get_user_model",
        "from tcms.management.models import Priority, Tag",
        "from tcms.testcases.models import Category, TestCase, TestCaseStatus",
        f"author = get_user_model().objects.get(username={_quote(author)})",
        "created = []",
    ]
    for case in cases:
        lines.extend(
            [
                f"category = Category.objects.get(name={_quote(case.category)})",
                f"priority = Priority.objects.get(value={_quote(case.priority)})",
                f"status = TestCaseStatus.objects.get(name={_quote(case.status)})",
                "case = TestCase.objects.create(",
                f"    summary={_quote(case.summary)},",
                "    category=category,",
                "    priority=priority,",
                "    case_status=status,",
                "    author=author,",
                f"    text={_quote(case.text)},",
                ")",
                f"for name in {list(case.tags)!r}:",
                "    tag, _ = Tag.objects.get_or_create(name=name)",
                "    case.add_tag(tag)",
                "created.append(case.pk)",
                'print("created case id =", case.pk)',
            ]
        )
    lines.append('print("created total =", len(created))')
    return "\n".join(lines) + "\n"


def list_script(limit: int) -> str:
    """生成回读脚本（打印最近若干条用例）。

    Args:
        limit: 条数。

    Returns:
        str: Python 脚本。
    """
    return (
        "from tcms.testcases.models import TestCase as T\n"
        f"print([(case.pk, case.summary) for case in T.objects.order_by('-pk')[:{int(limit)}]])\n"
    )


def ping_script() -> str:
    """生成连通性探测脚本（容器内打印一行标记）。

    Returns:
        str: Python 脚本。
    """
    return "print('engkit-kiwi-ok')\n"


def register(
    cfg: Config,
    cases_path: Path,
    *,
    dry_run: bool = False,
    runner: Runner = default_runner,
    author: str = _DEFAULT_AUTHOR,
) -> int:
    """批量登记用例。

    Args:
        cfg: 仓配置。
        cases_path: 用例清单文件。
        dry_run: 只打印将执行的脚本，不连远端。
        runner: 远端执行器（单测注入替身）。
        author: 作者账号。

    Returns:
        int: 退出码（0 成功 / 1 远端失败 / 2 清单或凭据问题）。
    """
    try:
        cases = load_cases(cases_path)
    except KiwiError as error:
        print(f"[kiwi] {error}", file=sys.stderr)
        return 2
    script = register_script(cases, author)
    if dry_run:
        print(f"[kiwi] 待登记 {len(cases)} 条（--dry-run，不连远端）")
        print(script)
        return 0
    try:
        result = runner(cfg, cfg.root, script)
    except KiwiError as error:
        print(f"[kiwi] {error}", file=sys.stderr)
        return 2
    print(result.output, end="" if result.output.endswith("\n") else "\n")
    if result.code != 0:
        print(f"[kiwi] 登记失败（远端退出码 {result.code}）", file=sys.stderr)
        return 1
    expected = f"created total = {len(cases)}"
    if expected not in result.output:
        print(f"[kiwi] 回读不一致：未在输出中找到「{expected}」", file=sys.stderr)
        return 1
    print(f"[kiwi] 登记完成：{len(cases)} 条")
    return 0


def list_cases(cfg: Config, *, limit: int = 10, runner: Runner = default_runner) -> int:
    """回读最近若干条用例。

    Args:
        cfg: 仓配置。
        limit: 条数。
        runner: 远端执行器。

    Returns:
        int: 退出码（0 成功 / 1 远端失败 / 2 凭据问题）。
    """
    try:
        result = runner(cfg, cfg.root, list_script(limit))
    except KiwiError as error:
        print(f"[kiwi] {error}", file=sys.stderr)
        return 2
    print(result.output, end="" if result.output.endswith("\n") else "\n")
    if result.code != 0:
        print(f"[kiwi] 回读失败（远端退出码 {result.code}）", file=sys.stderr)
        return 1
    return 0


def ping(cfg: Config, *, runner: Runner = default_runner) -> int:
    """探测远端可达（SSH + 容器 + Django shell）。

    Args:
        cfg: 仓配置。
        runner: 远端执行器。

    Returns:
        int: 退出码（0 可达 / 1 不可达 / 2 凭据问题）。
    """
    try:
        result = runner(cfg, cfg.root, ping_script())
    except KiwiError as error:
        print(f"[kiwi] {error}", file=sys.stderr)
        return 2
    if result.code != 0 or "engkit-kiwi-ok" not in result.output:
        print(f"[kiwi] 不可达（退出码 {result.code}）：{result.output.strip()[:200]}", file=sys.stderr)
        return 1
    print("[kiwi] 可达")
    return 0
