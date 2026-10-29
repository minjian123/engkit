"""git 相关的基础能力：变更清单、基准可解析性、工作区指纹、变更服务名。"""

from __future__ import annotations

import hashlib
import subprocess
from pathlib import Path


def _run(root: Path, args: list[str]) -> subprocess.CompletedProcess[str] | None:
    """执行一条 git 命令。

    Args:
        root: 仓库根目录。
        args: git 子命令与参数。

    Returns:
        subprocess.CompletedProcess[str] | None: 执行结果；git 不可用时为 None。
    """
    try:
        return subprocess.run(
            ["git", "-c", "core.quotepath=false", *args],
            cwd=root,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
        )
    except OSError:  # pragma: no cover - 环境无 git
        return None


def norm(rel: str) -> str:
    """统一路径形态（反斜杠转正斜杠、去首尾空白）。

    Args:
        rel: 路径文本。

    Returns:
        str: posix 形态路径。
    """
    return rel.strip().replace("\\", "/")


def ref_exists(root: Path, ref: str) -> bool:
    """git 引用是否可解析（浅克隆 / 新分支下基准可能不在本地）。

    Args:
        root: 仓库根目录。
        ref: 引用名（分支 / 提交号）。

    Returns:
        bool: 可解析返回 True。
    """
    result = _run(root, ["rev-parse", "--verify", "--quiet", ref])
    return result is not None and result.returncode == 0


def read_at(root: Path, ref: str, rel: str) -> str | None:
    """读取某个 git 引用下的文件内容（基线只减不增断言用）。

    Args:
        root: 仓库根目录。
        ref: 引用名（分支 / 提交号）。
        rel: 相对仓库根的文件路径。

    Returns:
        str | None: 文件内容；引用或文件不可解析时返回 None。
    """
    result = _run(root, ["show", f"{ref}:{rel}"])
    if result is None or result.returncode != 0:
        return None
    return result.stdout


def changes(root: Path, since: str = "") -> tuple[list[str], bool]:
    """取本次改动路径（相对仓库根）并报告「是否可判定」。

    Args:
        root: 仓库根目录。
        since: 对比基准（空串表示只看工作区 + 未跟踪）。

    Returns:
        tuple[list[str], bool]: （去重后的改动路径, 是否至少一条 git 命令可用）。
    """
    collected: dict[str, None] = {}
    reliable = False
    commands = [["ls-files", "--others", "--exclude-standard"]]
    if since:
        commands.insert(0, ["diff", "--name-only", since])
    for args in commands:
        result = _run(root, args)
        if result is None or result.returncode != 0:
            continue
        reliable = True
        for line in result.stdout.splitlines():
            rel = norm(line)
            if rel:
                collected.setdefault(rel, None)
    return list(collected), reliable


def _hash_file(path: Path) -> str:
    """计算文件内容摘要。

    Args:
        path: 文件路径。

    Returns:
        str: SHA-256 十六进制摘要；不可读时返回占位串。
    """
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return "<unreadable>"


def worktree_dirty(root: Path) -> tuple[list[str], bool]:
    """取工作树的未提交条目（已跟踪改动 + 未跟踪文件；忽略 `.gitignore` 覆盖的产物）。

    预检闸门据此判定「是否处于开发中」：准备推送的工作树应当是干净的。

    Args:
        root: 仓库根目录。

    Returns:
        tuple[list[str], bool]: （去重后的条目路径, git 是否可判定；无 git 时为 False，供调用方放行）。
    """
    result = _run(root, ["status", "--porcelain", "-z", "--untracked-files=all"])
    if result is None or result.returncode != 0:
        return [], False
    entries: dict[str, None] = {}
    for item in result.stdout.split("\0"):
        rel = norm(item[3:])
        if rel:
            entries.setdefault(rel, None)
    return list(entries), True


def fingerprint(root: Path) -> str:
    """计算工作区指纹：`HEAD` + 全部变更文件内容哈希。

    Args:
        root: 仓库根目录。

    Returns:
        str: 指纹（git 不可用时为 `nogit`）。
    """
    head = _run(root, ["rev-parse", "HEAD"])
    status = _run(root, ["status", "--porcelain", "-z", "--untracked-files=all"])
    if head is None or status is None or head.returncode != 0 or status.returncode != 0:
        return "nogit"
    digest = hashlib.sha256(head.stdout.strip().encode())
    for entry in sorted(item for item in status.stdout.split("\0") if item):
        path = entry[3:] if len(entry) > 3 else entry
        digest.update(path.encode())
        digest.update(_hash_file(root / path).encode())
    return digest.hexdigest()


def changed_services(changes_list: list[str], backend_root: str, groups: tuple[str, ...]) -> list[str]:
    """变更集中直接命中的后端服务名（`<后端根>/<分组>/<服务>/...`）。

    Args:
        changes_list: 变更路径（相对仓库根）。
        backend_root: 后端根（相对仓库根）。
        groups: 工程分组目录名。

    Returns:
        list[str]: 服务名（去重，保变更顺序）。
    """
    prefixes = [f"{backend_root}/{group}/" for group in groups]
    found: dict[str, None] = {}
    for item in changes_list:
        rel = norm(item)
        for prefix in prefixes:
            if rel.startswith(prefix):
                name = rel[len(prefix) :].split("/", 1)[0]
                if name:
                    found.setdefault(name, None)
                break
    return list(found)
