"""扫描范围约束（增量）：把「改动文件清单」裁剪成护栏的扫描面。

为什么需要：裸集合与产品字面量的规则都是**文件局部**的（单文件 AST / 单文件正则），
逐文件独立、互不引用，因此「只扫改动文件」与「全量扫描」的**新增判定等价**——
变化只是扫描面与耗时。全量扫描的耗时随文件数线性增长，开发环按增量跑才不会被代码规模拖住。

口径：

- 清单为**一行一个相对仓根路径**（`/` 分隔）；空行与 `#` 注释忽略；
- 只接受存在的文件，扩展名与排除目录判定仍由各护栏自己的规则负责（此处不做二次判断）；
- 清单里的重复项按首次出现去重，保持清单顺序（便于复现与逐项定位）。
"""

from __future__ import annotations

from pathlib import Path

from ..console import error

__all__ = ["read_file_list", "select_files"]


def read_file_list(path: Path) -> list[str] | None:
    """读取改动文件清单。

    Args:
        path: 清单文件路径。

    Returns:
        list[str] | None: 相对仓根的路径清单（去空行 / 注释 / 重复）；读不到返回 None。
    """
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        error(f"[扫描范围] 清单不可读：{path}：{exc}")
        return None
    return normalize_lines(text.splitlines())


def normalize_lines(lines: list[str]) -> list[str]:
    """规范化清单行（去空白 / 去注释 / 去重，保留首次出现顺序）。

    Args:
        lines: 原始行。

    Returns:
        list[str]: 相对路径清单。
    """
    seen: set[str] = set()
    picked: list[str] = []
    for raw in lines:
        rel = raw.strip().replace("\\", "/")
        if not rel or rel.startswith("#") or rel in seen:
            continue
        seen.add(rel)
        picked.append(rel)
    return picked


def select_files(root: Path, files: list[str]) -> list[Path]:
    """把相对清单裁剪为「仓内真实存在」的绝对路径清单。

    Args:
        root: 仓根。
        files: 相对路径清单。

    Returns:
        list[Path]: 绝对路径清单（存在的文件，保持清单顺序）。
    """
    selected: list[Path] = []
    for rel in files:
        path = root / rel
        if path.is_file():
            selected.append(path)
    return selected
