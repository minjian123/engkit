"""基座校验器共用的规则读取与登记源访问。

两类失败分开表达：**规则未登记**（`[rules.<名>]` 缺段）与**登记源不可用**（声明的文件缺失）
一律抛 `RulesError`，由命令行入口转成退出码 2——不靠缺省值掩盖，也不静默跳过。
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from pathlib import Path
from typing import Any, TypeVar

from ..config import Config

_R = TypeVar("_R")


class RulesError(RuntimeError):
    """规则未登记或登记源不可用（命令行入口据此退 2）。"""


def require(value: _R | None, section: str, keys: Sequence[str] = ()) -> _R:
    """取已登记的规则段（缺段即报错）。

    Args:
        value: 配置里对应段的取值（未登记为 None）。
        section: 段名（`[rules.<section>]`）。
        keys: 该段最小键清单（报错时给出指引）。

    Returns:
        _R: 规则对象。

    Raises:
        RulesError: 该段未登记。
    """
    if value is None:
        hint = f"（最小键：{' / '.join(keys)}）" if keys else ""
        raise RulesError(f"未登记规则 `[rules.{section}]`：按《接入说明》登记后启用{hint}")
    return value


def path_of(cfg: Config, rel: str) -> Path:
    """把仓根相对路径解析为绝对路径。

    Args:
        cfg: 仓配置。
        rel: 相对仓根的路径。

    Returns:
        Path: 绝对路径。
    """
    return cfg.root / rel


def require_file(cfg: Config, rel: str, label: str) -> Path:
    """取规则声明的文件（缺失即报错）。

    Args:
        cfg: 仓配置。
        rel: 相对仓根的路径。
        label: 文件用途（报错文案用）。

    Returns:
        Path: 绝对路径。

    Raises:
        RulesError: 文件不存在。
    """
    target = cfg.root / rel
    if not target.is_file():
        raise RulesError(f"{label}缺失：{rel}")
    return target


def read_text(path: Path) -> str:
    """读文本文件（UTF-8，容错）。

    Args:
        path: 文件路径。

    Returns:
        str: 文件内容。
    """
    return path.read_text(encoding="utf-8", errors="ignore")


def read_json(path: Path, label: str) -> Any:
    """读 JSON 登记文件。

    Args:
        path: 文件路径。
        label: 文件用途（报错文案用）。

    Returns:
        Any: 解析结果。

    Raises:
        RulesError: 文件不存在或不是合法 JSON。
    """
    if not path.is_file():
        raise RulesError(f"{label}缺失：{path.name}")
    try:
        return json.loads(read_text(path))
    except json.JSONDecodeError as exc:
        raise RulesError(f"{label}不是合法 JSON：{path.name}（{exc}）") from exc


def rel_posix(path: Path, root: Path) -> str:
    """取相对仓库根的 posix 路径。

    Args:
        path: 目标路径。
        root: 仓库根。

    Returns:
        str: 相对路径（`/` 分隔）。
    """
    return path.relative_to(root).as_posix()


def sub_dirs(path: Path) -> list[Path]:
    """列出一级子目录（不存在返回空列表，字典序）。

    Args:
        path: 目录。

    Returns:
        list[Path]: 子目录路径。
    """
    if not path.is_dir():
        return []
    return sorted(item for item in path.iterdir() if item.is_dir())


def read_lines(path: Path) -> list[str]:
    """按行读文本（保留原行内容）。

    Args:
        path: 文件路径。

    Returns:
        list[str]: 行清单。
    """
    return read_text(path).splitlines()
