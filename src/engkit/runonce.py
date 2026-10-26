"""重命令「跑一次」（`engkit run-once`）：同命令 + 同工作区状态下短窗内只真跑一次。

缓存键 =「命令 + 工作目录 + 工作区指纹」；工作区指纹含 `HEAD` 与全部变更文件内容哈希，
**改一个源文件即失效**。窗口内命中即回放上次完整输出与退出码，改完代码要立刻重验用 `--force`。
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import subprocess
import sys
import time
from dataclasses import asdict, dataclass
from pathlib import Path

from . import gitutil

WRAPPED_ENV = "ENGKIT_RUN_ONCE"
"""包装标记环境变量（本模块向子进程注入 `1`，重命令拦截据此放行）。"""

_MAX_OUTPUT_CHARS = 4_000_000
"""单流输出落盘上限（防异常命令把缓存撑爆；超出截断并标注）。"""


@dataclass
class Record:
    """一次真跑的完整记录。

    Attributes:
        key: 缓存键。
        argv: 命令参数。
        cwd: 工作目录。
        fingerprint: 工作区指纹。
        started_at: 开始时间（Unix 秒）。
        duration_seconds: 耗时（秒）。
        exit_code: 退出码。
        stdout: 输出全文（stdout / stderr 合流）。
        truncated: 是否发生输出截断。
    """

    key: str
    argv: tuple[str, ...] = ()
    cwd: str = ""
    fingerprint: str = ""
    started_at: float = 0.0
    duration_seconds: float = 0.0
    exit_code: int = 0
    stdout: str = ""
    truncated: bool = False


def cache_key(argv: list[str] | tuple[str, ...], cwd: str, fingerprint: str) -> str:
    """计算缓存键。

    Args:
        argv: 命令参数。
        cwd: 工作目录。
        fingerprint: 工作区指纹。

    Returns:
        str: 缓存键（SHA-256 前 32 位）。
    """
    material = "\0".join([*argv, cwd, fingerprint])
    return hashlib.sha256(material.encode()).hexdigest()[:32]


def cache_path(cache_dir: Path, key: str) -> Path:
    """取缓存文件路径。

    Args:
        cache_dir: 缓存目录。
        key: 缓存键。

    Returns:
        Path: 缓存文件路径。
    """
    return cache_dir / f"{key}.json"


def load(cache_dir: Path, key: str) -> Record | None:
    """读取缓存记录。

    Args:
        cache_dir: 缓存目录。
        key: 缓存键。

    Returns:
        Record | None: 记录；不存在或损坏时为 None。
    """
    path = cache_path(cache_dir, key)
    if not path.is_file():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    try:
        payload["argv"] = tuple(payload.get("argv") or ())
        return Record(**payload)
    except (TypeError, AttributeError, ValueError):
        return None


def save(cache_dir: Path, record: Record) -> None:
    """写入缓存记录。

    Args:
        cache_dir: 缓存目录。
        record: 运行记录。
    """
    cache_dir.mkdir(parents=True, exist_ok=True)
    payload = asdict(record)
    payload["argv"] = list(record.argv)
    cache_path(cache_dir, record.key).write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def clip(text: str) -> tuple[str, bool]:
    """按上限截断输出。

    Args:
        text: 原始输出。

    Returns:
        tuple[str, bool]: （截断后文本, 是否截断）。
    """
    if len(text) <= _MAX_OUTPUT_CHARS:
        return text, False
    return text[:_MAX_OUTPUT_CHARS] + "\n…（输出超出上限已截断，详见缓存文件）\n", True


def emit(text: str) -> None:
    """安全打印（空串不输出）。

    Args:
        text: 待打印文本。
    """
    if not text:
        return
    sys.stdout.write(text)
    if not text.endswith("\n"):
        sys.stdout.write("\n")
    sys.stdout.flush()


def run(
    root: Path,
    argv: list[str],
    *,
    cache_dir: Path,
    window_seconds: int,
    force: bool = False,
    show_only: bool = False,
    cwd: Path | None = None,
) -> int:
    """执行或回放命令。

    Args:
        root: 仓库根（指纹计算用）。
        argv: 命令参数。
        cache_dir: 缓存目录。
        window_seconds: 缓存窗口（秒）。
        force: 忽略缓存真跑。
        show_only: 只报告缓存状态而不执行。
        cwd: 命令工作目录（缺省当前目录）。

    Returns:
        int: 退出码（回放时为上次退出码）。
    """
    workdir = str((cwd or Path.cwd()).resolve())
    fingerprint = gitutil.fingerprint(root)
    key = cache_key(argv, workdir, fingerprint)
    record = load(cache_dir, key)
    age = None if record is None else max(0.0, time.time() - record.started_at)
    fresh = record is not None and age is not None and age <= window_seconds

    if show_only:
        if record is None:
            emit(f"[run-once] 无缓存：{key}（指纹 {fingerprint[:12]}）")
        else:
            emit(
                f"[run-once] 缓存命中：{key}（{age:.0f}s 前，退出码 {record.exit_code}，"
                f"窗口 {window_seconds}s，{'有效' if fresh else '已过期'}）"
            )
        return 0

    if fresh and not force and record is not None:
        emit(
            f"[run-once] 回放上次结果（{age:.0f}s 前真跑，退出码 {record.exit_code}；"
            f"工作区指纹 {fingerprint[:12]} 未变。确需重跑加 --force）"
        )
        emit(record.stdout)
        if record.truncated:
            emit("[run-once] 注意：上次输出曾截断。")
        return record.exit_code

    if record is not None and not force:
        emit(f"[run-once] 缓存已过期（{age:.0f}s > {window_seconds}s），真跑：{' '.join(argv)}")
    elif force:
        emit(f"[run-once] --force 真跑：{' '.join(argv)}")

    started_at = time.time()
    child_env = {**os.environ, WRAPPED_ENV: "1"}
    process = subprocess.Popen(
        argv,
        cwd=workdir,
        env=child_env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
        bufsize=1,
    )
    buffer = io.StringIO()
    assert process.stdout is not None
    for line in process.stdout:
        buffer.write(line)
        sys.stdout.write(line)
        sys.stdout.flush()
    returncode = process.wait()
    stdout, truncated = clip(buffer.getvalue())
    duration = time.time() - started_at
    save(
        cache_dir,
        Record(
            key=key,
            argv=tuple(argv),
            cwd=workdir,
            fingerprint=fingerprint,
            started_at=started_at,
            duration_seconds=round(duration, 3),
            exit_code=returncode,
            stdout=stdout,
            truncated=truncated,
        ),
    )
    emit(f"[run-once] 已记录（{duration:.1f}s，退出码 {returncode}，键 {key}）")
    return returncode
